"""Authenticated Casual/Battle LIVE start, result polling and original rewards.

Room transport provides the actual Mingle participant list and NPC result wires.
The controller persists the two HTTP stages separately: Finish acknowledges the
personal record, then GetBattleLiveResult grants the combined-score rewards.
"""
from __future__ import annotations

from contextlib import closing
from functools import cached_property
import json
import math
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.inventory import member_cards
from .music import record_score
from deathnote.game.progression.login_bonus import posted_notification
from deathnote.protocol.rpc import NativeRpcError
from .single import NativeSingleLive, int_value, parse_message, reward_resource
from deathnote.protocol.wire import integer_field, message_field, varint
from deathnote.protocol.decode import fields


START_BATTLE_METHOD = '/app.live.LiveService/StartBattleLive'
FINISH_BATTLE_METHOD = '/app.live.LiveService/FinishBattleLive'
GET_BATTLE_RESULT_METHOD = '/app.live.LiveService/GetBattleLiveResult'
FETCH_BATTLE_INVITATION_METHOD = '/app.live.LiveService/FetchBattleLiveInvitation'
GET_BATTLE_SEARCH_ID_METHOD = '/app.live.LiveService/GetBattleLiveSearchId'


class _MessagePackReader:
    """Read/skip the original snapshot wire without materializing its graphs."""
    def __init__(self, data):
        self.data = memoryview(data)
        self.position = 0

    def take(self, count):
        end = self.position + count
        if end > len(self.data):
            raise ValueError('Truncated battle log')
        value = self.data[self.position:end]
        self.position = end
        return value

    def byte(self):
        return self.take(1)[0]

    def unsigned(self, count):
        return int.from_bytes(self.take(count), 'big')

    def array(self):
        code = self.byte()
        if 0x90 <= code <= 0x9f:
            return code & 15
        if code in (0xdc, 0xdd):
            return self.unsigned(2 if code == 0xdc else 4)
        raise ValueError('Battle log array expected')

    def integer(self):
        code = self.byte()
        if code <= 0x7f:
            return code
        if code >= 0xe0:
            return code - 256
        if 0xcc <= code <= 0xcf:
            return self.unsigned(1 << (code - 0xcc))
        if 0xd0 <= code <= 0xd3:
            return int.from_bytes(self.take(1 << (code - 0xd0)), 'big', signed=True)
        raise ValueError('Battle log integer expected')

    def binary(self):
        code = self.byte()
        if code not in (0xc4, 0xc5, 0xc6):
            raise ValueError('Compressed battle log binary expected')
        return bytes(self.take(self.unsigned(1 << (code - 0xc4))))

    def extension(self):
        code = self.byte()
        if code in (0xc7, 0xc8, 0xc9):
            length = self.unsigned(1 << (code - 0xc7))
        elif 0xd4 <= code <= 0xd8:
            length = 1 << (code - 0xd4)
        else:
            raise ValueError('Compressed battle log extension expected')
        extension_type = self.byte()
        return extension_type, bytes(self.take(length))

    def skip(self):
        code = self.byte()
        if code <= 0x7f or code >= 0xe0 or code in (0xc0, 0xc2, 0xc3):
            return
        if 0xa0 <= code <= 0xbf:
            self.take(code & 31)
            return
        if 0x90 <= code <= 0x9f:
            count = code & 15
        elif 0x80 <= code <= 0x8f:
            count = (code & 15) * 2
        elif code in (0xdc, 0xdd, 0xde, 0xdf):
            count = self.unsigned(2 if code in (0xdc, 0xde) else 4)
            if code in (0xde, 0xdf):
                count *= 2
        else:
            if code in (0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9):
                size = self.unsigned(1 << ((code - 0xc4) % 3))
                self.take(size + (1 if code >= 0xc7 else 0))
            elif code in (0xd9, 0xda, 0xdb):
                self.take(self.unsigned(1 << (code - 0xd9)))
            elif 0xcc <= code <= 0xd3:
                self.take(1 << ((code - 0xcc) % 4))
            elif 0xd4 <= code <= 0xd8:
                self.take((1 << (code - 0xd4)) + 1)
            elif code in (0xca, 0xcb):
                self.take(4 if code == 0xca else 8)
            else:
                raise ValueError('Invalid battle log token')
            return
        for _ in range(count):
            self.skip()


def _snapshot_bytes(reference_log):
    """Original MessagePack TryDecompress ext99 / ext98 LZ4 formats."""
    reader = _MessagePackReader(reference_log)
    code = reader.data[0] if reader.data else -1
    if code in (0xc7, 0xc8, 0xc9) or 0xd4 <= code <= 0xd8:
        extension_type, payload = reader.extension()
        if extension_type != 99:
            raise ValueError('Invalid battle log compression extension')
        lengths = _MessagePackReader(payload)
        size = lengths.integer()
        blocks = [(bytes(lengths.data[lengths.position:]), size)]
    elif 0x90 <= code <= 0x9f or code in (0xdc, 0xdd):
        count = reader.array()
        next_code = reader.data[reader.position] if reader.position < len(reader.data) else -1
        if next_code not in (0xc7, 0xc8, 0xc9) and not 0xd4 <= next_code <= 0xd8:
            return reference_log
        extension_type, payload = reader.extension()
        if extension_type != 98:
            raise ValueError('Invalid battle log compression extension')
        lengths = _MessagePackReader(payload)
        sizes = [lengths.integer() for _ in range(count - 1)]
        blocks = [(reader.binary(), size) for size in sizes]
    else:
        raise ValueError('Invalid battle log root')
    from lz4.block import decompress, LZ4BlockError
    try:
        return b''.join(decompress(block, uncompressed_size=size) for block, size in blocks)
    except LZ4BlockError as error:
        raise ValueError('Invalid compressed battle log') from error


def _battle_log_end_time(reference_log):
    reader = _MessagePackReader(_snapshot_bytes(reference_log))
    count = reader.array()
    last_time = None
    for _ in range(count):
        fields_count = reader.array()
        if fields_count < 1:
            raise ValueError('Battle log snapshot lacks TimeMs')
        last_time = reader.integer()
        for _ in range(fields_count - 1):
            reader.skip()
    if last_time is None or last_time <= 0:
        raise ValueError('Battle log has no positive ending TimeMs')
    return last_time


def _messagepack_time(value):
    if value <= 0x7f:
        return bytes((value,))
    for code, size in ((0xcc, 1), (0xcd, 2), (0xce, 4), (0xcf, 8)):
        if value < 1 << (size * 8):
            return bytes((code,)) + value.to_bytes(size, 'big')
    raise ValueError('Battle log TimeMs exceeds integer range')


def dummy_battle_log(reference_log):
    """Two zero-score snapshots using the real result's exact ending TimeMs.

    The original detailed timeline computes lastTime / (snapshotCount - 1),
    so retain the real time range while keeping all dummy scores at zero.
    """
    end_time = _battle_log_end_time(reference_log)
    return (bytes.fromhex('929600809000000096') + _messagepack_time(end_time)
            + bytes.fromhex('8090000000'))


def dummy_player_result(player_id, reference_log):
    log = dummy_battle_log(reference_log)
    return (message_field(1, player_id.encode('utf8')) + message_field(3, bytes(3))
            + message_field(4, log) + message_field(5, b''))


def _text(values, number):
    value = values.get(number, b'')
    if not isinstance(value, bytes):
        raise NativeRpcError(3)
    try:
        return value.decode('utf8')
    except UnicodeDecodeError:
        raise NativeRpcError(3) from None


def _ranking_messages(payload):
    rankings = {}
    try:
        for number, value in fields(payload):
            if number != 3:
                continue
            if not isinstance(value, bytes):
                raise NativeRpcError(3, diagnostic={'code': 'battle_ranking_wire'})
            entry = parse_message(value)
            ranking_type = int_value(entry, 1)
            if ranking_type not in range(1, 5) or ranking_type in rankings:
                raise NativeRpcError(3, diagnostic={'code': 'battle_ranking_type',
                    'rankingType': ranking_type, 'duplicate': ranking_type in rankings})
            ranked_players = {}
            for field, player_wire in fields(value):
                if field == 2:
                    if not isinstance(player_wire, bytes):
                        raise NativeRpcError(3, diagnostic={'code': 'battle_ranking_player_wire'})
                    player = parse_message(player_wire)
                    player_id, rank = _text(player, 1), int_value(player, 2)
                    if not player_id or rank not in range(1, 6) or player_id in ranked_players:
                        raise NativeRpcError(3, diagnostic={'code': 'battle_ranking_rank',
                            'rankingType': ranking_type, 'rank': rank,
                            'hasPlayerId': bool(player_id), 'duplicate': player_id in ranked_players})
                    ranked_players[player_id] = rank
            rankings[ranking_type] = ranked_players
    except ValueError:
        raise NativeRpcError(3, diagnostic={'code': 'battle_ranking_parse'}) from None
    return rankings


def _check_finish_player_index(participants, player_id, submitted_index):
    expected_index = next(entry.get('player_index', index)
                          for index, entry in enumerate(participants)
                          if entry['player_id'] == player_id)
    if submitted_index != str(expected_index):
        raise NativeRpcError(3, diagnostic={'code': 'battle_finish_identity',
            'submittedPlayerIndex': (int(submitted_index) if submitted_index in '01234'
                                     and len(submitted_index) == 1 else -1),
            'expectedPlayerIndex': expected_index})


def _account_player_result(player_wire, finish_wire, player_id):
    """Original Finish sends a slot ID; GetResult identifies account UUIDs."""
    finish = parse_message(finish_wire)
    judgement = b''.join(integer_field(number, int_value(finish, number))
                         for number in (1, 2, 3))
    for number, value in fields(finish_wire):
        if number == 9:
            if not isinstance(value, bytes):
                raise NativeRpcError(3, diagnostic={'code': 'battle_judgement_wire'})
            judgement += message_field(4, value)
    result = message_field(1, player_id.encode('utf8'))
    for number, value in fields(player_wire):
        if number not in (1, 5):
            result += (message_field(number, value) if isinstance(value, bytes)
                       else integer_field(number, value))
    return result + message_field(5, judgement)


class NativeBattleLive:
    methods = frozenset((START_BATTLE_METHOD, FINISH_BATTLE_METHOD, GET_BATTLE_RESULT_METHOD,
                         GET_BATTLE_SEARCH_ID_METHOD))

    def __init__(self, login, player_data, master_directory: Path, *, clock=time.time,
                 rng=None, season_pass=None, mission=None, room_lookup=None, room_reserve=None,
                 room_release=None, room_complete=None):
        self.login = login
        self.player_data = player_data
        self.clock = clock
        self.room_lookup = room_lookup
        self.room_reserve = room_reserve
        self.room_release = room_release
        self.room_complete = room_complete
        self.single = NativeSingleLive(login, player_data, master_directory, clock=clock,
                                       rng=rng, season_pass=season_pass, mission=mission)
        self.progress = player_data.progress
        self.rewards = self.single.rewards
        self.rng = self.single.rng
        self.season_pass, self.mission = season_pass, mission
        self.master_directory = Path(master_directory)
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS battle_live_active (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    music_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    boost INTEGER NOT NULL,
                    deck_id INTEGER NOT NULL,
                    deck_wire BLOB NOT NULL,
                    room_id TEXT NOT NULL,
                    lobby_id INTEGER NOT NULL,
                    room_players TEXT NOT NULL,
                    started_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS battle_live_results (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    live_unique_id TEXT NOT NULL,
                    music_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    boost INTEGER NOT NULL,
                    deck_wire BLOB NOT NULL,
                    room_id TEXT NOT NULL,
                    room_players TEXT NOT NULL,
                    live_finish BLOB NOT NULL,
                    player_result BLOB NOT NULL,
                    rankings TEXT NOT NULL,
                    completed_at INTEGER NOT NULL,
                    finish_response BLOB NOT NULL,
                    result_response BLOB,
                    PRIMARY KEY(profile_id, live_unique_id)
                );
            ''')

    def _rows(self, name):
        return json.loads((self.master_directory / (name + '.json')).read_text('utf8'))['_allData']

    @property
    def rules(self):
        return self.single.rules

    @property
    def rating(self):
        return self.single.rating

    @cached_property
    def battle_rewards(self):
        return self.rules._groups(self._rows('MasterBattleLiveReward'), '_liveScoreRank')

    @cached_property
    def gekisou_rewards(self):
        return self._rows('MasterGekisouLiveRankReward')

    @cached_property
    def result_timeout(self):
        parameters = {row['_id']: row['_value'] for row in self._rows('MasterParameter')}
        return int(parameters['gekisou_ranking_reward_timeout_sec'])

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method == START_BATTLE_METHOD:
            return self.start(player, payload)
        if method == FINISH_BATTLE_METHOD:
            return self.finish(player, payload)
        if method == GET_BATTLE_RESULT_METHOD:
            parse_message(payload)
            return self.result(player)
        if method == FETCH_BATTLE_INVITATION_METHOD:
            parse_message(payload)
            return b''
        if method == GET_BATTLE_SEARCH_ID_METHOD:
            parse_message(payload)
            if self.room_reserve is None:
                raise NativeRpcError(9, diagnostic={'code': 'battle_transport_unavailable'})
            room = self.room_reserve(player['player_id'], player['profile_id'])
            return integer_field(1, room['search_id'])
        raise NotImplementedError(method)

    def start(self, player, payload):
        request = parse_message(payload)
        music_id, difficulty, boost, deck_id = (int_value(request, field) for field in range(1, 5))
        room_id, lobby_id = _text(request, 5), int_value(request, 6)
        try:
            self.rules.select_music(music_id, difficulty)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'battle_music_selection'}) from None
        if boost not in range(11) or not room_id or lobby_id < 0:
            raise NativeRpcError(3)
        room = self.room_lookup(room_id) if self.room_lookup is not None else None
        if room is None or room['closed']:
            raise NativeRpcError(9, diagnostic={'code': 'battle_room_missing'})
        if room.get('mode', 'battle') != 'battle':
            raise NativeRpcError(9, diagnostic={'code': 'battle_room_mode'})
        participants = room['players']
        if (len(participants) not in range(1, 6)
                or sum(entry['player_id'] == player['player_id'] for entry in participants) != 1):
            raise NativeRpcError(9, diagnostic={'code': 'battle_room_membership'})
        if room['music_id'] != music_id:
            raise NativeRpcError(9, diagnostic={'code': 'battle_room_music'})
        decks, _ = self.player_data.decks.load(player['profile_id'])
        deck = next((wire for wire in decks if int_value(parse_message(wire), 1) == deck_id), None)
        if deck is None:
            raise NativeRpcError(9, diagnostic={'code': 'battle_deck_missing'})
        members = self.single.deck_members(deck)
        if set(members) != set(range(5)) or len(set(members.values())) != 5:
            raise NativeRpcError(9, diagnostic={'code': 'battle_deck_incomplete'})
        now = int(self.clock())
        room_players = [dict({key: entry[key] for key in ('player_id', 'profile_id', 'is_dummy', 'disconnected')},
                             player_index=index) for index, entry in enumerate(participants)]
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            owned = member_cards(state, player['created_at'])
            if any(str(card_id) not in owned for card_id in members.values()):
                raise NativeRpcError(9, diagnostic={'code': 'battle_deck_incomplete'})
            current = state['live_boost']
            try:
                self.rules.snapshot_boost(current['amount'], current['previous_recovery_at'], now, boost)
            except ValueError:
                raise NativeRpcError(9, diagnostic={'code': 'battle_boost_balance'}) from None
            connection.execute('''
                INSERT INTO battle_live_active(profile_id,music_id,difficulty,boost,deck_id,deck_wire,
                                               room_id,lobby_id,room_players,started_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id) DO UPDATE SET music_id=excluded.music_id,
                    difficulty=excluded.difficulty,boost=excluded.boost,deck_id=excluded.deck_id,
                    deck_wire=excluded.deck_wire,room_id=excluded.room_id,lobby_id=excluded.lobby_id,
                    room_players=excluded.room_players,started_at=excluded.started_at
            ''', (player['profile_id'], music_id, difficulty, boost, deck_id, deck, room_id,
                  lobby_id, json.dumps(room_players, separators=(',', ':')), now))
        return b''

    def _personal_rewards(self, state, music_id, difficulty, score, combo, clear_status, deck, created_at):
        rank = self.rules.score_rank(music_id, score)
        note_count = self.rules.select_music(music_id, difficulty)['score']['_fullComboCount']
        music_key, combo_key = str(music_id), f'{music_id}:{difficulty}'
        score_claims = state['score_reward_ids'].setdefault(music_key, [])
        combo_claims = state['combo_reward_ids'].setdefault(combo_key, [])
        new_score = self.rules.score_rewards(music_id, rank, score_claims)
        new_combo = self.rules.combo_rewards(music_id, difficulty, combo, combo_claims, full_combo=clear_status >= 4)
        self.rewards.apply(state, [(row['_resourceType'], row['_resourceId'], row['_resourceCount'])
                                  for row in new_score + new_combo], now=int(self.clock()))
        score_claims.extend(row['_id'] for row in new_score)
        combo_claims.extend(row['_id'] for row in new_combo)
        if new_score:
            state['score_reward_gained'][music_key] = max(state['score_reward_gained'].get(music_key, 0),
                                                       max(row['_liveScoreRank'] for row in new_score))
        if new_combo:
            thresholds = (note_count // 4, note_count // 2, note_count * 3 // 4, note_count)
            state['combo_reward_gained'][combo_key] = max(state['combo_reward_gained'].get(combo_key, 0),
                max(thresholds[row['_comboRateType']] for row in new_combo))
        key = f'{music_id}:{difficulty}:1'
        previous_scores = list(state['scores'].values())
        previous = state['scores'].get(key)
        record_score(state, music_id=music_id, difficulty=difficulty, mode=1,
                     score=score, deck=deck, created_at=created_at)
        state['scores'][key] = {'music_id': music_id, 'difficulty': difficulty, 'mode': 1,
            'high_score': max(score, previous['high_score'] if previous else 0),
            'clear_status': max(clear_status, previous['clear_status'] if previous else 0),
            'max_combo': max(combo, previous['max_combo'] if previous else 0)}
        rating = self.rating.rewards(previous_scores, list(state['scores'].values()))
        self.rewards.apply(state, rating['resources'], now=int(self.clock()))
        response = b''.join(message_field(4, reward_resource(*resource)) for resource in rating['resources'])
        if rating['total_reward_ids']:
            response += message_field(8, b''.join(varint(value) for value in rating['total_reward_ids']))
        for band_id, reward_ids in rating['band_rewards'].items():
            response += message_field(9, integer_field(1, band_id) +
                                      message_field(2, b''.join(varint(value) for value in reward_ids)))
        return response + message_field(5, b'')

    def finish(self, player, payload):
        outer = parse_message(payload)
        finish_wire, player_wire = outer.get(1), outer.get(2)
        if not isinstance(finish_wire, bytes) or not isinstance(player_wire, bytes):
            raise NativeRpcError(3, diagnostic={'code': 'battle_finish_wire'})
        finish = parse_message(finish_wire)
        clear_status, score, combo = (int_value(finish, field) for field in range(1, 4))
        unique_id = _text(finish, 11)
        player_result = parse_message(player_wire)
        battle_score, count = int_value(player_result, 2), int_value(outer, 4)
        details = {'clearStatus': clear_status, 'score': score, 'combo': combo,
                   'battleScore': battle_score, 'playerCount': count}
        if (not unique_id or clear_status not in range(1, 6) or score not in range(0x80000000)
                or combo < 0 or battle_score not in range(0x80000000) or count not in range(6)):
            raise NativeRpcError(3, diagnostic={'code': 'battle_finish_values',
                'hasUniqueId': bool(unique_id), **details})
        submitted_index = _text(player_result, 1)
        rankings = _ranking_messages(payload)
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            cached = connection.execute('''
                SELECT finish_response,room_players,room_id,player_result FROM battle_live_results
                WHERE profile_id=? AND live_unique_id=?
            ''', (player['profile_id'], unique_id)).fetchone()
            if cached is not None:
                _check_finish_player_index(json.loads(cached[1]), player['player_id'], submitted_index)
                if self.room_complete is not None:
                    npc_log = dummy_battle_log(parse_message(cached[3])[4])
                    self.room_complete(cached[2], npc_log)
                return cached[0]
            active = connection.execute('''
                SELECT music_id,difficulty,boost,deck_wire,room_id,room_players
                FROM battle_live_active WHERE profile_id=?
            ''', (player['profile_id'],)).fetchone()
            if active is None:
                raise NativeRpcError(9, diagnostic={'code': 'battle_start_missing'})
            music_id, difficulty, boost, deck, room_id, room_players = active
            participants = json.loads(room_players)
            _check_finish_player_index(participants, player['player_id'], submitted_index)
            participant_ids = {entry['player_id'] for entry in participants}
            expected_count = sum(not entry['disconnected'] for entry in participants)
            if count and count != expected_count:
                raise NativeRpcError(3, diagnostic={'code': 'battle_player_count',
                    'playerCount': count, 'expectedPlayerCount': expected_count})
            if any(set(ranked) - participant_ids for ranked in rankings.values()):
                raise NativeRpcError(3, diagnostic={'code': 'battle_ranking_participant'})
            note_count = self.rules.select_music(music_id, difficulty)['score']['_fullComboCount']
            if combo > note_count:
                raise NativeRpcError(3, diagnostic={'code': 'battle_finish_combo',
                    'musicId': music_id, 'difficulty': difficulty, 'combo': combo, 'noteCount': note_count})
            if self.room_complete is not None:
                reference_log = player_result.get(4)
                if not isinstance(reference_log, bytes):
                    raise NativeRpcError(3, diagnostic={'code': 'battle_finish_log'})
                try:
                    npc_log = dummy_battle_log(reference_log)
                except ValueError:
                    raise NativeRpcError(3, diagnostic={'code': 'battle_finish_log'}) from None
            player_wire = _account_player_result(player_wire, finish_wire, player['player_id'])
            state = self.progress.load(connection, player['profile_id'])
            response = self._personal_rewards(state, music_id, difficulty, score, combo, clear_status,
                                              deck, player['created_at'])
            self.progress.save(connection, player['profile_id'], state)
            connection.execute('''
                INSERT INTO battle_live_results(profile_id,live_unique_id,music_id,difficulty,boost,
                    deck_wire,room_id,room_players,live_finish,player_result,rankings,completed_at,finish_response)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (player['profile_id'], unique_id, music_id, difficulty, boost, deck, room_id,
                  room_players, finish_wire, player_wire, json.dumps(rankings, separators=(',', ':')), now, response))
            connection.execute('DELETE FROM battle_live_active WHERE profile_id=?', (player['profile_id'],))
        if self.room_complete is not None:
            self.room_complete(room_id, npc_log)
        return response

    def battle_score_rank(self, music_id, total_score, count):
        group = self.rules.music[music_id]['_liveScoreRankGroup']
        eligible = [row for row in self.rules.rank_groups[group]
                    if total_score >= (int(row['_requiredScore'] * math.sqrt(5.0 / count) * count)
                                       if count > 0 else 0x7fffffff)]
        rank = max(eligible, key=lambda row: row['_requiredScore'])['_liveScoreRank'] if eligible else 0
        return 2 if rank == 1 else rank

    def _draw(self, rows):
        groups = self.rules._groups(rows, '_group')
        result = []
        for group in sorted(groups):
            value, cumulative = self.rng.randrange(10000), 0
            for row in groups[group]:
                cumulative += row['_probability']
                if value < cumulative:
                    result.append(row)
                    break
        return result

    def _apply_exp(self, connection, player, state, record, rank, now):
        boost, deck, music_id = record['boost'], record['deck_wire'], record['music_id']
        boost_state = state['live_boost']
        try:
            consumed = self.rules.snapshot_boost(boost_state['amount'], boost_state['previous_recovery_at'], now, boost)
        except ValueError:
            raise NativeRpcError(9, diagnostic={'code': 'battle_boost_balance'}) from None
        boost_state.update(amount=consumed['amount'], previous_recovery_at=consumed['previous_recovery_at'])
        rates, base = self.rules.boost_bonus(boost), self.rules.exp_base(rank)
        previous_rank = self.rules.player_rank(state['rank_exp'])
        state['rank_exp'] += base['_playerExp'] * rates['_playerExpRate']
        boost_state['amount'] += (self.rules.player_rank(state['rank_exp']) - previous_rank) * self.rules.rank_up_boost
        owned = member_cards(state, player['created_at'])
        members = self.single.deck_members(deck)
        card_exp = base['_memberCardExp'] * rates['_memberCardExpRate']
        for owned_id in members.values():
            key = str(owned_id)
            card, before = owned[key], state['member_exp'].get(key, 0)
            cap = self.rules.card_exp_cap(card['master_id'], card['awake_count'], card['card_rank'])
            gained = min(card_exp, max(0, cap - before))
            state['member_exp'][key] = before + gained
            if card_exp > gained:
                self.rewards.apply(state, ((1, 5, card_exp - gained),), now=now)
        center = self.rules.member_cards[owned[str(members[2])]['master_id']]['_characterID']
        friendship_exp = base['_friendshipExp'] * rates['_friendshipExpRate']
        characters = []
        for slot, owned_id in members.items():
            character = self.rules.member_cards[owned[str(owned_id)]['master_id']]['_characterID']
            characters.append(character)
            state['character_exp'][str(character)] = state['character_exp'].get(str(character), 0) + base['_characterRankExp']
            if slot != 2:
                friendship = self.single.friendship_masters.get(tuple(sorted((center, character))))
                if friendship is not None:
                    key = str(friendship)
                    state['friendship_exp'][key] = state['friendship_exp'].get(key, 0) + friendship_exp
        stamp = state['live_stamp']
        state['live_stamp'] = self.rules.update_live_stamp(stamp['live_completed'], stamp['reward_gained'],
                                                          stamp['last_live_completed_at'], now)
        finish = parse_message(record['live_finish'])
        cleared = int_value(finish, 1) >= 2
        if self.season_pass is not None:
            self.season_pass.record_live(connection, player['profile_id'], music_id=music_id,
                                         cleared=cleared, consumed_boost=boost, now=now)
            self.season_pass.record_counter(connection, player['profile_id'],
                                            124, rank, 0, 1, now)
        if self.mission is not None:
            self.mission.record_live(connection, player['profile_id'], music_id=music_id,
                                     character_ids=characters, cleared=cleared,
                                     difficulty=record['difficulty'], score_rank=rank, mode=2, now=now)
        self.single.skip_events.gain_challenge_points(state, deck, rank=rank, rates=rates, now=now)
        return rates

    def result(self, player):
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN IMMEDIATE')
            record = connection.execute('''
                SELECT * FROM battle_live_results WHERE profile_id=? ORDER BY completed_at DESC,rowid DESC LIMIT 1
            ''', (player['profile_id'],)).fetchone()
            if record is None:
                raise NativeRpcError(9, diagnostic={'code': 'battle_finish_missing'})
            if record['result_response'] is not None:
                return record['result_response']
            participants = json.loads(record['room_players'])
            room = self.room_lookup(record['room_id']) if self.room_lookup is not None else None
            available = room['results'] if room is not None else {}
            player_wires = {player['player_id']: record['player_result']}
            for participant in participants:
                player_id = participant['player_id']
                if player_id in available:
                    wire = available[player_id]
                    if _text(parse_message(wire), 1) != player_id:
                        raise NativeRpcError(3, diagnostic={'code': 'battle_result_identity'})
                    player_wires[player_id] = wire
                elif participant['is_dummy'] and not participant['disconnected']:
                    reference_log = parse_message(record['player_result']).get(4)
                    if not isinstance(reference_log, bytes):
                        raise NativeRpcError(3, diagnostic={'code': 'battle_result_log'})
                    try:
                        player_wires[player_id] = dummy_player_result(player_id, reference_log)
                    except ValueError:
                        raise NativeRpcError(3, diagnostic={'code': 'battle_result_log'}) from None
            missing = [entry for entry in participants if not entry['disconnected'] and entry['player_id'] not in player_wires]
            if missing and now - record['completed_at'] < self.result_timeout:
                return b''.join(message_field(1, wire) for wire in player_wires.values()) + message_field(6, b'')
            online = [entry for entry in participants if not entry['disconnected'] and entry['player_id'] in player_wires]
            total_score = sum(int_value(parse_message(player_wires[entry['player_id']]), 2) for entry in online)
            if total_score not in range(0x80000000):
                raise NativeRpcError(3)
            rank = self.battle_score_rank(record['music_id'], total_score, len(online))
            state = self.progress.load(connection, player['profile_id'])
            previous_rates = (self.single.offline_bonus.capture_rates(state, now=now)
                              if self.single.offline_bonus is not None else None)
            rates = self._apply_exp(connection, player, state, record, rank, now)
            event_wire, event_resources, event_posted = self.single.skip_events.settle_battle(
                state, record['deck_wire'], score_rank=rank, rates=rates,
                player_rank=self.rules.player_rank(state['rank_exp']), now=now, rng=self.rng)
            ordinary = self._draw(self.battle_rewards[rank])
            rewards = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'] * rates['_liveMusicRewardRate'], 0)
                       for row in ordinary]
            rankings = {int(key): value for key, value in json.loads(record['rankings']).items()}
            music = self.rules.music[record['music_id']]
            reward_types = {1: 1, **{index + 2: {1: 2, 2: 4, 3: 3}[music[f'_gekisouMission{index + 1}']]
                                      for index in range(3)}}
            for ranking_type, ranked_players in rankings.items():
                if ranked_players.get(player['player_id']) == 1:
                    extra = self._draw([row for row in self.gekisou_rewards
                                        if row['_liveScoreRank'] == rank and row['_type'] == reward_types[ranking_type]])
                    rewards.extend((row['_resourceType'], row['_resourceId'],
                                    row['_resourceCount'] * rates['_liveMusicRewardRate'], ranking_type) for row in extra)
            self.rewards.apply(state, [(kind, item_id, amount) for kind, item_id, amount, _ in rewards], now=now)
            self.rewards.apply(state, event_resources, now=now)
            total_ranking = rankings.get(1, {})
            before_wins = state.get('multi_battle_consecutive_win_count', 0)
            state['multi_battle_consecutive_win_count'] = before_wins + 1 if total_ranking.get(player['player_id']) == 1 else 0
            notification = (self.single.offline_bonus.stock_changed_rates(connection, player, state, previous_rates, now=now)
                            if self.single.offline_bonus is not None else b'')
            notification += posted_notification(event_posted)
            response = (b''.join(message_field(1, player_wires[entry['player_id']]) for entry in participants
                                 if entry['player_id'] in player_wires)
                        + integer_field(3, 2 if missing else 1) + message_field(6, notification))
            for kind, item_id, amount, ranking_type in rewards:
                resource = message_field(1, reward_resource(kind, item_id, amount))
                if ranking_type:
                    resource += integer_field(2, ranking_type)
                response += message_field(7, resource)
            for number, value in fields(event_wire):
                if number == 2:
                    response += message_field(5, value)
                elif number == 4:
                    response += message_field(8, value)
            self.progress.save(connection, player['profile_id'], state)
            connection.execute('''UPDATE battle_live_results SET result_response=?
                                  WHERE profile_id=? AND live_unique_id=?''',
                               (response, player['profile_id'], record['live_unique_id']))
        if self.room_release is not None:
            self.room_release(record['room_id'])
        return response

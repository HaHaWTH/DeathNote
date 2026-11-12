"""Local Arena state, 1.1.0 RPCs, configurable AP adjustments and season resets."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from functools import cached_property
import json
import math
from pathlib import Path
import sqlite3
import time

from .battle import (_account_player_result, _check_finish_player_index,
                                 _ranking_messages, _text, dummy_battle_log,
                                 dummy_player_result)
from deathnote.game.progression.event import deck_detail, repeated_ints
from deathnote.game.progression.band_rank import NativeBandRank, _scaled_power, _single
from deathnote.game.players.inventory import member_cards
from .event import JST
from .music import record_score
from deathnote.game.progression.login_bonus import posted_notification
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.calendar import NativeServiceCalendar
from .single import NativeSingleLive, int_value, parse_message, reward_resource
from deathnote.protocol.wire import integer_field, message_field, string_field, varint
from deathnote.protocol.decode import fields

ARENA_PREFIX = '/app.arena.ArenaService/'
START_ARENA = '/app.live.LiveService/StartArenaLive'
FINISH_ARENA = '/app.live.LiveService/FinishArenaLive'
RESULT_ARENA = '/app.live.LiveService/GetArenaLiveResult'
ARENA_METHODS = frozenset(ARENA_PREFIX + name for name in (
    'ArenaTop', 'ArenaMyRank', 'ArenaRanking', 'ArenaRewards', 'ConsecutiveWins',
    'DeckTrend', 'MatchingHistories', 'SaveDedicatedDecks', 'ReportPenalty',
    'ReceivePromotionReward', 'ReportSeasonResultShown'))


def _time(value):
    return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=JST).timestamp())


def _packed(number, values):
    return message_field(number, b''.join(varint(value) for value in values)) if values else b''


def _scalars(*pairs):
    return b''.join(integer_field(number, value) for number, value in pairs)


def _signed32(value):
    value &= 0xffffffff
    return value - 0x100000000 if value & 0x80000000 else value


def _ordinary_deck(wire):
    return b''.join(message_field(number, value) if isinstance(value, bytes)
                    else integer_field(number, value)
                    for number, value in fields(wire) if number in (1, 2, 3))


class NativeArena:
    methods = ARENA_METHODS | {START_ARENA, FINISH_ARENA, RESULT_ARENA}

    def __init__(self, login, player_data, master_directory, *, clock=time.time,
                 rng=None, season_pass=None, mission=None, profile_builder=None,
                 room_lookup=None, room_release=None, room_complete=None,
                 point_adjust=None, reset_scale=10000):
        self.login, self.player_data, self.clock = login, player_data, clock
        self.master_directory = Path(master_directory)
        self.progress = player_data.progress
        self.single = NativeSingleLive(login, player_data, master_directory, clock=clock,
            rng=rng, season_pass=season_pass, mission=mission)
        self.rewards, self.rng = self.single.rewards, self.single.rng
        self.season_pass, self.mission = season_pass, mission
        self.profile_builder = profile_builder
        self.room_lookup, self.room_release, self.room_complete = room_lookup, room_release, room_complete
        self.calendar = NativeServiceCalendar(master_directory)
        self.parameters = {row['_id']: row['_value'] for row in self._rows('MasterParameter')}
        self.point_adjust = int(self.parameters.get('arena_adjust_point', 50)) if point_adjust is None else int(point_adjust)
        self.reset_scale = int(reset_scale)
        if self.point_adjust <= 0 or self.reset_scale <= 0:
            raise ValueError('Arena local AP policy must have positive adjustment and reset scale')
        self.seasons = {row['_id']: row for row in self._rows('MasterArenaSeason')}
        self.leagues = {row['_id']: row for row in self._rows('MasterArenaLeague')}
        self.ranks = self._rows('MasterArenaRank')
        regulation_file = self.master_directory / 'MasterArenaLeagueRegulation.json'
        self.regulations = ({row['_id']: row for row in self._rows('MasterArenaLeagueRegulation')}
                            if regulation_file.is_file() else {})
        self.promotions = {row['_id']: row for row in self._rows('MasterArenaPromotionReward')}
        self.daily_rewards = {row['_id']: row for row in self._rows('MasterArenaDailyReward')}
        self.season_rewards = self._rows('MasterArenaSeasonReward')
        self.ranking_rewards = self._rows('MasterArenaRankingReward')
        self.characters = {row['_id']: row for row in self._rows('MasterCharacter')}
        self.live_rewards = self._rows('MasterLiveArenaReward')
        self.live_rank_rewards = self._rows('MasterLiveArenaRankReward')
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS arena_live_active (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    season_id INTEGER NOT NULL, regulation_id INTEGER NOT NULL,
                    league_id INTEGER NOT NULL, music_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL, boost INTEGER NOT NULL,
                    deck_wire BLOB NOT NULL, room_id TEXT NOT NULL,
                    room_players TEXT NOT NULL, started_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS arena_live_results (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    live_unique_id TEXT NOT NULL, season_id INTEGER NOT NULL,
                    regulation_id INTEGER NOT NULL, league_id INTEGER NOT NULL,
                    music_id INTEGER NOT NULL, difficulty INTEGER NOT NULL,
                    boost INTEGER NOT NULL, deck_wire BLOB NOT NULL,
                    room_id TEXT NOT NULL, room_players TEXT NOT NULL,
                    live_finish BLOB NOT NULL, player_result BLOB NOT NULL,
                    rankings TEXT NOT NULL, completed_at INTEGER NOT NULL,
                    finish_response BLOB NOT NULL, result_response BLOB,
                    PRIMARY KEY(profile_id,live_unique_id)
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

    def _season(self, season_id, *, playable=False):
        row = self.seasons.get(season_id)
        if row is None:
            raise NativeRpcError(3, diagnostic={'code': 'arena_season_id'})
        if playable and not _time(row['_startAt']) <= self.clock() < _time(row['_endAt']):
            raise NativeRpcError(9, diagnostic={'code': 'arena_season_closed'})
        return row

    def rank(self, league_id, point):
        band = self.leagues[league_id]['_bandId']
        rows = [row for row in self.ranks if row['_bandId'] == band and row['_lp'] <= point
                and (not row['_startAt'] or _time(row['_startAt']) <= self.clock())]
        return max(rows, key=lambda row: row['_lp'])

    def _arena(self, state, now):
        arena = state.setdefault('arena', {'seasons': {}, 'decks': {}, 'promotion_claims': [],
                                         'shown_seasons': [], 'last_band': 0})
        day = self.calendar.day(now)
        if arena.get('daily', {}).get('day') != day:
            arena['daily'] = {'day': day, 'rewards': []}
            arena['penalty_count'] = 0
        return arena

    def _record(self, arena, season_id):
        self._season(season_id)
        key = str(season_id)
        if key not in arena['seasons']:
            previous = [(self.seasons[int(sid)], value) for sid, value in arena['seasons'].items()
                        if _time(self.seasons[int(sid)]['_startAt']) < _time(self.seasons[season_id]['_startAt'])]
            if previous:
                latest = max(previous, key=lambda pair: _time(pair[0]['_startAt']))[0]
                intervening = [row for row in self.seasons.values()
                    if _time(latest['_startAt']) < _time(row['_startAt']) < _time(self.seasons[season_id]['_startAt'])]
                if intervening:
                    self._record(arena, max(intervening, key=lambda row: _time(row['_startAt']))['_id'])
                    previous = [(self.seasons[int(sid)], value) for sid, value in arena['seasons'].items()
                        if _time(self.seasons[int(sid)]['_startAt']) < _time(self.seasons[season_id]['_startAt'])]
            old = max(previous, key=lambda pair: _time(pair[0]['_startAt']))[1] if previous else None
            points = {}
            for league_id in self.leagues:
                point = old['lp'][str(league_id)] if old else 0
                reset = self.rank(league_id, point)['_seasonResetLp']
                points[str(league_id)] = max(0, point * (self.reset_scale - reset) // self.reset_scale)
            arena['seasons'][key] = {'lp': points, 'mp': int(self.parameters['arena_mp_default_value']),
                'max_point': max(points.values(), default=0), 'max_lp': dict(points), 'plays': 0,
                'band_plays': {}, 'wins': {}, 'decks': {}, 'history': [], 'settled': False,
                'season_reward_ids': [], 'ranking_reward_ids': []}
        return arena['seasons'][key]

    def _all_rankings(self, connection, season_id, league_id):
        if league_id == 0 and not any(row['_rank'] == 7 and
                (not row['_startAt'] or _time(row['_startAt']) <= self.clock()) for row in self.ranks):
            return []
        rows = []
        for row in connection.execute('''SELECT p.profile_id,p.player_id,p.created_at,e.name,e.updated_at
                FROM players p LEFT JOIN player_profile_edits e ON e.profile_id=p.profile_id
                ORDER BY p.profile_id''').fetchall():
            state = self.progress.load(connection, row[0])
            record = state.get('arena', {}).get('seasons', {}).get(str(season_id))
            if record is None or not (record['band_plays'].get(str(league_id), 0) if league_id else record['plays']):
                continue
            point = record['lp'][str(league_id)] if league_id else record['mp']
            rows.append((point, row, state, record))
        return sorted(rows, key=lambda value: (-value[0], value[1][0]))

    def _ranking_position(self, connection, profile_id, season_id, league_id):
        rows = self._all_rankings(connection, season_id, league_id)
        return next((index for index, row in enumerate(rows, 1) if row[1][0] == profile_id), 0), len(rows)

    def _settle_seasons(self, connection, player, arena, state, now):
        for sid, record in arena['seasons'].items():
            season_id = int(sid)
            following = [row for row in self.seasons.values()
                         if _time(row['_startAt']) > _time(self.seasons[season_id]['_startAt'])]
            if record['settled'] or not following or now < min(_time(row['_startAt']) for row in following):
                continue
            record['settled'] = True
            if not record['plays']:
                continue
            rewards = [row for row in self.season_rewards
                       if row['_lpStart'] <= record['max_point'] <= row['_lpEnd']]
            record['season_reward_ids'] = [row['_id'] for row in rewards]
            for league_id in (0, *self.leagues):
                ranking, _ = self._ranking_position(connection, player['profile_id'], season_id, league_id)
                if ranking:
                    selected = [row for row in self.ranking_rewards if row['_leagueId'] == league_id
                                and row['_rankStart'] <= ranking <= row['_rankEnd']]
                    rewards.extend(selected)
                    record['ranking_reward_ids'].extend(row['_id'] for row in selected)
            self.rewards.apply(state, [(row['_resourceType'], row['_resourceId'], row['_resourceCount'])
                                      for row in rewards], now=now)

    def _context(self, connection, player, season_id, now):
        state = self.progress.load(connection, player['profile_id'])
        arena = self._arena(state, now)
        record = self._record(arena, season_id)
        self._settle_seasons(connection, player, arena, state, now)
        return state, arena, record

    def _max_league_point(self, arena, league_id):
        return max((value['max_lp'][str(league_id)] for value in arena['seasons'].values()), default=0)

    def _claim_promotions(self, state, arena, rows, now):
        pending = [row for row in rows if row['_id'] not in arena['promotion_claims']]
        self.rewards.apply(state, [(row['_resourceType'], row['_resourceId'], row['_resourceCount'])
                                  for row in pending], now=now)
        arena['promotion_claims'].extend(row['_id'] for row in pending)
        return [row['_id'] for row in pending]

    def _top(self, player, season_id):
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state, arena, record = self._context(connection, player, season_id, now)
            self._initialize_decks(connection, player, state, arena, now)
            previous = [(int(sid), value) for sid, value in arena['seasons'].items()
                        if value['settled'] and value['plays'] and int(sid) != season_id]
            before, last = max(previous, default=(0, None))
            result = (message_field(1, integer_field(1, arena['last_band']) if arena['last_band'] else b'')
                      + integer_field(2, record['mp'])
                      + b''.join(message_field(3, _scalars((1, league['_bandId']), (2, record['lp'][str(lid)])))
                                 for lid, league in self.leagues.items())
                      + b''.join(message_field(4, integer_field(1, rid)) for rid in arena['daily']['rewards'])
                      + integer_field(5, arena['penalty_count']) + integer_field(6, before)
                      + integer_field(7, bool(before and before not in arena['shown_seasons']))
                      + b''.join(message_field(10, bytes.fromhex(wire)) for _, wire in sorted(arena['decks'].items()))
                      + message_field(11, b'')
                      + _packed(12, last['season_reward_ids'] if last else [])
                      + _packed(13, last['ranking_reward_ids'] if last else [])
                      + self._wins_wire(record, 14))
            self.progress.save(connection, player['profile_id'], state)
        return result

    @cached_property
    def _initial_deck_calculator(self):
        return NativeBandRank(self.master_directory)

    def _initial_member_power(self, state, card, total_bonus, item_maps):
        """Context-free LIVE CardPower: no band-evaluation SkillRating terms.

        Member.LivePower 0x6427100 -> Power 0x6425ee8 and the actual LIVE
        CalculateMemberPower 0x57ca174 supply level/awake/rank and flat rank
        bonuses. SlotPower applies each available BandItem component to that
        power. Music/type-link/leader bonuses require a chosen song/full deck.
        """
        calculator = self._initial_deck_calculator
        master = calculator.members[card['master_id']]
        level = calculator._level(calculator.levels, master['_memberCardLevelGroup'],
                                  state['member_exp'].get(str(card['id']), 0))
        awake = next(row for row in calculator.awakes
                     if row['_group'] == master['_memberCardAwakeGroup']
                     and row['_awakeCount'] == card['awake_count'])
        rank = next(row for row in calculator.member_ranks
                    if row['_group'] == master['_memberCardRankGroup']
                    and row['_rank'] == card['card_rank'])
        bonus = calculator._character_rank(state, master['_characterID'])['_bonus'] + total_bonus
        targets = {'_characterID': master['_characterID'],
                   '_bandID': self.characters[master['_characterID']]['_bandID'],
                   '_cardType': master['_cardType']}
        result = 0
        for component, (maximum, rate) in enumerate((('_performancePowerMax', '_performanceRate'),
                    ('_technicPowerMax', '_technicRate'), ('_visualPowerMax', '_visualRate'))):
            base = _scaled_power(master[maximum], level[rate])
            base += _scaled_power(master[maximum], rank[rate])
            base += math.floor(_single(_single(_single(awake[rate]) / _single(10000))
                                       * _single(master[maximum])))
            base += bonus
            item_rate = sum(item_maps[key].get(target, (0, 0, 0))[component]
                            for key, target in targets.items())
            result += base + base * item_rate // 10000
        return result

    def _equipped_supports(self, connection, player, state, owned):
        """Copy only existing ordinary equipment with its original constraints."""
        candidates = {}
        supports = state['support_cards']
        masters = self._initial_deck_calculator.supports
        for wire, in connection.execute('SELECT wire FROM player_decks WHERE profile_id=? ORDER BY deck_id',
                                        (player['profile_id'],)):
            for number, value in fields(wire):
                if number != 3:
                    continue
                slot = parse_message(value)
                member = owned.get(str(int_value(slot, 3)))
                support = supports.get(str(_signed32(int_value(slot, 4))))
                if member is None or support is None:
                    continue
                member_master = self.rules.member_cards[member['master_id']]
                support_master = masters[support['master_id']]
                character = member_master['_characterID']
                card_type = member_master['_cardType']
                if (character in support_master['_characterIDs']
                        and card_type == support_master['_cardType']):
                    candidates.setdefault((character, card_type), set()).add(support['id'])
        return candidates

    def _initial_support_value(self, support):
        calculator = self._initial_deck_calculator
        master = calculator.supports[support['master_id']]
        level = calculator._level(calculator.support_levels, master['_supportCardLevelGroup'], support['exp'])
        return sum(_scaled_power(master[maximum], level[rate])
                   for maximum, rate in (('_performancePowerMax', '_performanceRate'),
                    ('_technicPowerMax', '_technicRate'), ('_visualPowerMax', '_visualRate')))

    def _initialize_decks(self, connection, player, state, arena, now):
        missing = [league for lid, league in sorted(self.leagues.items())
                   if str(lid) not in arena['decks']
                   and (not league['_startAt'] or _time(league['_startAt']) <= now)]
        if not missing:
            return
        calculator = self._initial_deck_calculator
        owned = member_cards(state, player['created_at'])
        total_rank = sum(calculator._character_rank(state, character)['_rank']
                         for character in calculator.characters)
        total_bonus = max((row for row in calculator.total_ranks if row['_totalRank'] <= total_rank),
                          key=lambda row: row['_totalRank'])['_bonus']
        item_maps = calculator._band_item_maps(state)
        best = {}
        for card in owned.values():
            character = self.rules.member_cards[card['master_id']]['_characterID']
            power = self._initial_member_power(state, card, total_bonus, item_maps)
            candidate = power, -card['id'], card
            if character not in best or candidate[:2] > best[character][:2]:
                best[character] = candidate
        supports = self._equipped_supports(connection, player, state, owned)
        for league in missing:
            characters = sorted(character['_id'] for character in self.characters.values()
                                if character['_bandID'] == league['_bandId'] and not character['_isNonPlayable'])
            used_supports, cards = set(), []
            for position, character in enumerate(characters):
                card = best[character][2] if character in best else None
                support_id = -1
                if card is not None:
                    master = self.rules.member_cards[card['master_id']]
                    available = supports.get((character, master['_cardType']), set()) - used_supports
                    if available:
                        support_id = max(available, key=lambda key:
                            (self._initial_support_value(state['support_cards'][str(key)]), -key))
                        used_supports.add(support_id)
                cards.append(message_field(3, _scalars((1, position), (2, position),
                    (3, card['id'] if card is not None else 0), (4, support_id))))
            wire = integer_field(1, league['_id']) + b''.join(cards)
            arena['decks'][str(league['_id'])] = wire.hex()

    def _wins_wire(self, record, field):
        return b''.join(message_field(field, _scalars((1, league['_bandId']),
                          (2, record['wins'].get(str(lid), 0)))) for lid, league in self.leagues.items())

    def _validate_deck(self, player, state, wire):
        request = parse_message(wire)
        league_id = int_value(request, 1)
        if league_id not in self.leagues:
            raise NativeRpcError(3, diagnostic={'code': 'arena_deck_id'})
        _text(request, 2)
        cards, slots = [], set()
        owned = member_cards(state, player['created_at'])
        band = self.leagues[league_id]['_bandId']
        for number, value in fields(wire):
            if number != 3:
                continue
            if not isinstance(value, bytes):
                raise NativeRpcError(3, diagnostic={'code': 'arena_deck_wire'})
            slot = parse_message(value)
            index, card_id, support_id = (int_value(slot, field) for field in (1, 3, 4))
            support_id = _signed32(support_id)
            if index not in range(5) or index in slots or str(card_id) not in owned:
                raise NativeRpcError(9, diagnostic={'code': 'arena_deck_member'})
            card = self.rules.member_cards[owned[str(card_id)]['master_id']]
            character_id = card['_characterID']
            if self.characters[character_id]['_bandID'] != band:
                raise NativeRpcError(9, diagnostic={'code': 'arena_deck_band'})
            if support_id not in (-1, 0) and str(support_id) not in state['support_cards']:
                raise NativeRpcError(9, diagnostic={'code': 'arena_deck_support'})
            slots.add(index)
            cards.append(card_id)
        if slots != set(range(5)) or len(set(cards)) != 5:
            raise NativeRpcError(9, diagnostic={'code': 'arena_deck_incomplete'})
        return league_id, wire

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        request = parse_message(payload)
        if not self.regulations:
            raise NativeRpcError(9, diagnostic={'code': 'arena_regulation_not_available'})
        if method == START_ARENA:
            return self.start(player, payload)
        if method == FINISH_ARENA:
            return self.finish(player, payload)
        if method == RESULT_ARENA:
            return self.result(player, request)
        action = method.removeprefix(ARENA_PREFIX)
        if action == 'ArenaTop':
            return self._top(player, int_value(request, 1))
        if action == 'ArenaRanking':
            return self._ranking(player, request)
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            arena = self._arena(state, now)
            if action == 'SaveDedicatedDecks':
                wire = request.get(1)
                if not isinstance(wire, bytes):
                    raise NativeRpcError(3, diagnostic={'code': 'arena_deck_wire'})
                league_id, wire = self._validate_deck(player, state, wire)
                arena['decks'][str(league_id)] = wire.hex()
                response = b''
            elif action == 'ReceivePromotionReward':
                reward_id = int_value(request, 1)
                reward = self.promotions.get(reward_id)
                if reward is None:
                    raise NativeRpcError(3, diagnostic={'code': 'arena_promotion_reward_id'})
                required = next(row for row in self.ranks if row['_id'] == reward['_rankMatchId'])
                if self._max_league_point(arena, reward['_leagueId']) < required['_lp']:
                    raise NativeRpcError(9, diagnostic={'code': 'arena_promotion_not_reached'})
                self._claim_promotions(state, arena, [reward], now)
                response = integer_field(1, reward_id) + message_field(2, b'')
            elif action == 'ReportSeasonResultShown':
                season_id = int_value(request, 1)
                self._season(season_id)
                record = arena['seasons'].get(str(season_id))
                if record is None or not record['settled'] or not record['plays']:
                    raise NativeRpcError(9, diagnostic={'code': 'arena_season_result_unavailable'})
                if season_id not in arena['shown_seasons']:
                    arena['shown_seasons'].append(season_id)
                response = b''
            elif action == 'MatchingHistories':
                histories = [value for record in arena['seasons'].values() for value in record['history']]
                limit = int(self.parameters['arena_recent_match_limit'])
                response = b''.join(message_field(1, string_field(1, value['at'])
                    + b''.join(string_field(2, pid) for pid in value['players']))
                    for value in sorted(histories, key=lambda row: row['at'], reverse=True)[:limit])
            elif action == 'ReportPenalty':
                response = self._penalty(connection, player, state, arena, int_value(request, 1), now)
            else:
                season_id = int_value(request, 2 if action == 'DeckTrend' else 1)
                self._season(season_id)
                record = self._record(arena, season_id)
                self._settle_seasons(connection, player, arena, state, now)
                if action == 'ConsecutiveWins':
                    response = self._wins_wire(record, 1)
                elif action == 'ArenaMyRank':
                    response = self._my_rank(connection, player, season_id, record)
                elif action == 'ArenaRewards':
                    response = self._rewards_view(connection, player, season_id, arena, record)
                elif action == 'DeckTrend':
                    response = self._deck_trend(connection, request, season_id)
                else:
                    raise NotImplementedError(method)
            self.progress.save(connection, player['profile_id'], state)
        return response

    def _my_rank(self, connection, player, season_id, record):
        rank, _ = self._ranking_position(connection, player['profile_id'], season_id, 0)
        response = message_field(1, _scalars((1, record['mp']), (2, rank)))
        for league_id, league in self.leagues.items():
            rank, _ = self._ranking_position(connection, player['profile_id'], season_id, league_id)
            response += message_field(2, _scalars((1, league['_bandId']),
                (2, record['lp'][str(league_id)]), (3, rank)))
        return response

    def _rewards_view(self, connection, player, season_id, arena, record):
        season = integer_field(1, record['mp']) + b''.join(message_field(2,
            _scalars((1, league['_bandId']), (2, record['lp'][str(lid)]))) for lid, league in self.leagues.items())
        season += integer_field(3, record['max_point'])
        rank, total = self._ranking_position(connection, player['profile_id'], season_id, 0)
        response = message_field(1, season) + message_field(2, _scalars((1, rank), (2, total)))
        for league_id, league in self.leagues.items():
            rank, total = self._ranking_position(connection, player['profile_id'], season_id, league_id)
            response += message_field(3, _scalars((1, league['_bandId']), (2, rank), (3, total)))
        for reward_id in arena['promotion_claims']:
            reward = self.promotions.get(reward_id)
            if reward is not None:
                if reward['_leagueId'] == 0:
                    response += message_field(4, integer_field(1, reward_id))
                else:
                    response += message_field(5, _scalars((1, self.leagues[reward['_leagueId']]['_bandId']), (2, reward_id)))
        return response

    def _ranking(self, player, request):
        season_id, band, start, end = (int_value(request, n) for n in range(1, 5))
        self._season(season_id)
        by_band = {row['_bandId']: lid for lid, row in self.leagues.items()}
        if (2 in request and band not in by_band) or start < 1 or end < start:
            raise NativeRpcError(3, diagnostic={'code': 'arena_ranking_range'})
        league_id = by_band[band] if 2 in request else 0
        with closing(sqlite3.connect(self.login.database)) as connection:
            connection.execute('BEGIN')
            rows = self._all_rankings(connection, season_id, league_id)
            result = integer_field(2, len(rows))
            for rank, (point, profile, state, record) in enumerate(rows, 1):
                if start <= rank <= end:
                    profile_wire = self.profile_builder(connection, profile, state)
                    deck = bytes.fromhex(record['decks'].get(str(league_id), ''))
                    result += message_field(1, message_field(1, profile_wire)
                        + message_field(2, deck_detail(deck, state, profile[2]) if deck else b'')
                        + _scalars((3, rank), (4, point)))
        return result

    def _deck_trend(self, connection, request, season_id):
        music_id, band = int_value(request, 1), int_value(request, 3)
        if music_id not in self.rules.music or band not in {row['_bandId'] for row in self.leagues.values()}:
            raise NativeRpcError(3, diagnostic={'code': 'arena_trend_selection'})
        counts = ({}, {}, {})
        league_id = next(lid for lid, row in self.leagues.items() if row['_bandId'] == band)
        for wire, profile_id, created_at in connection.execute('''SELECT r.deck_wire,r.profile_id,p.created_at
                FROM arena_live_results r JOIN players p ON p.profile_id=r.profile_id
                WHERE r.season_id=? AND r.music_id=? AND r.league_id=? AND r.result_response IS NOT NULL''',
                (season_id, music_id, league_id)):
            state = self.progress.load(connection, profile_id)
            owned = member_cards(state, created_at)
            for number, value in fields(wire):
                if number == 3:
                    slot = parse_message(value)
                    card_id = owned[str(int_value(slot, 3))]['master_id']
                    support = state['support_cards'].get(str(_signed32(int_value(slot, 4))))
                    support_id = support['master_id'] if support else 0
                    if int_value(slot, 1) == 2:
                        counts[0][card_id] = counts[0].get(card_id, 0) + 1
                    counts[1][card_id] = counts[1].get(card_id, 0) + 1
                    if support_id > 0:
                        counts[2][support_id] = counts[2].get(support_id, 0) + 1
        return b''.join(message_field(field, integer_field(1, item_id))
            for field, group in enumerate(counts, 1)
            for item_id in sorted(group, key=lambda value: (-group[value], value))[:20])

    def _penalty(self, connection, player, state, arena, penalty, now):
        if penalty < 0:
            raise NativeRpcError(3, diagnostic={'code': 'arena_penalty_value'})
        active = connection.execute('SELECT season_id,league_id,room_id FROM arena_live_active WHERE profile_id=?',
                                    (player['profile_id'],)).fetchone()
        if active is None:
            return integer_field(1, 0) + message_field(9, b'')
        season_id, league_id, room_id = active
        record = self._record(arena, season_id)
        before = record['lp'][str(league_id)]
        before_rank = self.rank(league_id, before)
        after = self.apply_point(before, self.point_delta(5, 5), before_rank['_pointBehaviorType'])
        record['lp'][str(league_id)] = after
        record['wins'][str(league_id)] = 0
        arena['penalty_count'] += 1
        connection.execute('DELETE FROM arena_live_active WHERE profile_id=?', (player['profile_id'],))
        return (_scalars((1, 1), (2, self.leagues[league_id]['_bandId']), (3, after - before),
                         (4, after), (5, 0), (6, record['mp']), (7, before_rank['_id']),
                         (8, self.rank(league_id, after)['_id'])) + message_field(9, b''))

    def point_delta(self, place, count):
        if count not in range(2, 6) or place not in range(1, count + 1):
            raise NativeRpcError(3, diagnostic={'code': 'arena_place_or_count'})
        numerator = self.point_adjust * (count + 1 - 2 * place)
        return (abs(numerator) // (count - 1)) * (1 if numerator >= 0 else -1)

    @staticmethod
    def apply_point(before, delta, behavior):
        if behavior == 1:
            return before + max(0, delta)
        if behavior == 3:
            return max(0, before + delta)
        raise NativeRpcError(9, diagnostic={'code': 'arena_point_behavior_not_selected'})

    def start(self, player, payload):
        request = parse_message(payload)
        season_id, obsolete_music_id, difficulty, boost, deck_id = (int_value(request, n) for n in range(1, 6))
        room_id, lobby_id, music_id = _text(request, 6), int_value(request, 8), int_value(request, 9)
        self._season(season_id, playable=True)
        now = int(self.clock())
        if obsolete_music_id:
            raise NativeRpcError(3, diagnostic={'code': 'arena_music_id_obsolete'})
        if deck_id not in self.leagues or boost not in range(11) or lobby_id < 0 or not room_id:
            raise NativeRpcError(9, diagnostic={'code': 'arena_start_selection'})
        regulation = next((row for row in self.regulations.values() if row['_leagueId'] == deck_id
            and _time(row['_startAt']) <= now < _time(row['_endAt'])), None)
        if regulation is None:
            raise NativeRpcError(9, diagnostic={'code': 'arena_regulation_not_open'})
        league_id, regulation_id = deck_id, regulation['_id']
        try:
            self.rules.select_music(music_id, difficulty)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'arena_music_selection'}) from None
        band = self.leagues[league_id]['_bandId']
        if band not in self.rules.music[music_id]['_bandIDs']:
            raise NativeRpcError(9, diagnostic={'code': 'arena_music_band'})
        if repeated_ints(payload, 7):
            raise NativeRpcError(9, diagnostic={'code': 'arena_support_item_not_released'})
        room = self.room_lookup(room_id) if self.room_lookup is not None else None
        if (room is None or room['closed'] or room.get('mode', 'battle') != 'arena'
                or room['properties'].get('band') != str(band)):
            raise NativeRpcError(9, diagnostic={'code': 'arena_room_missing_or_mode',
                'roomFound': room is not None,
                'closed': room['closed'] if room is not None else None,
                'mode': room.get('mode') if room is not None else None,
                'band': room['properties'].get('band') if room is not None else None,
                'expectedBand': str(band)})
        participants = room['players']
        if (len(participants) not in range(2, 6) or room['music_id'] != music_id
                or sum(row['player_id'] == player['player_id'] for row in participants) != 1):
            raise NativeRpcError(9, diagnostic={'code': 'arena_room_membership_or_music'})
        room_players = [dict({key: value[key] for key in ('player_id', 'profile_id', 'is_dummy', 'disconnected')},
                             player_index=index) for index, value in enumerate(participants)]
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state, arena, record = self._context(connection, player, season_id, now)
            if arena['penalty_count'] >= int(self.parameters['arena_disconnect_penalty_threshold']):
                raise NativeRpcError(9, diagnostic={'code': 'arena_daily_matching_penalty'})
            wire = arena['decks'].get(str(deck_id))
            if wire is None:
                raise NativeRpcError(9, diagnostic={'code': 'arena_deck_missing'})
            deck = bytes.fromhex(wire)
            self._validate_deck(player, state, deck)
            deck = _ordinary_deck(deck)
            current = state['live_boost']
            try:
                self.rules.snapshot_boost(current['amount'], current['previous_recovery_at'], now, boost)
            except ValueError:
                raise NativeRpcError(9, diagnostic={'code': 'arena_boost_balance'}) from None
            connection.execute('''INSERT INTO arena_live_active VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id) DO UPDATE SET season_id=excluded.season_id,
                    regulation_id=excluded.regulation_id,league_id=excluded.league_id,
                    music_id=excluded.music_id,difficulty=excluded.difficulty,boost=excluded.boost,
                    deck_wire=excluded.deck_wire,room_id=excluded.room_id,
                    room_players=excluded.room_players,started_at=excluded.started_at''',
                (player['profile_id'], season_id, regulation_id, league_id, music_id, difficulty, boost,
                 deck, room_id, json.dumps(room_players, separators=(',', ':')), now))
            arena['last_band'] = band
            self.progress.save(connection, player['profile_id'], state)
        return b''

    def _personal_result(self, state, player, record, finish):
        music_id, difficulty, deck = record['music_id'], record['difficulty'], record['deck_wire']
        clear_status, score, combo = (int_value(finish, n) for n in (1, 2, 3))
        previous_scores = list(state['scores'].values())
        key = f'{music_id}:{difficulty}:1'
        old = state['scores'].get(key)
        record_score(state, music_id=music_id, difficulty=difficulty, mode=1, score=score,
                     deck=deck, created_at=player['created_at'])
        state['scores'][key] = {'music_id': music_id, 'difficulty': difficulty, 'mode': 1,
            'high_score': max(score, old['high_score'] if old else 0),
            'clear_status': max(clear_status, old['clear_status'] if old else 0),
            'max_combo': max(combo, old['max_combo'] if old else 0)}
        claims = state['combo_reward_ids'].setdefault(f'{music_id}:{difficulty}', [])
        combo_rows = self.rules.combo_rewards(music_id, difficulty, combo, claims, full_combo=clear_status >= 4)
        claims.extend(row['_id'] for row in combo_rows)
        resources = [(row['_resourceType'], row['_resourceId'], row['_resourceCount']) for row in combo_rows]
        note_count = self.rules.select_music(music_id, difficulty)['score']['_fullComboCount']
        if combo_rows:
            thresholds = (note_count // 4, note_count // 2, note_count * 3 // 4, note_count)
            combo_key = f'{music_id}:{difficulty}'
            state['combo_reward_gained'][combo_key] = max(state['combo_reward_gained'].get(combo_key, 0),
                max(thresholds[row['_comboRateType']] for row in combo_rows))
        rating = self.rating.rewards(previous_scores, list(state['scores'].values()))
        self.rewards.apply(state, resources + rating['resources'], now=int(self.clock()))
        response = b''.join(message_field(3, reward_resource(*value)) for value in resources + rating['resources'])
        response += message_field(4, b'') + _packed(5, rating['total_reward_ids'])
        for band, ids in rating['band_rewards'].items():
            response += message_field(6, integer_field(1, band) + _packed(2, ids))
        return response

    def finish(self, player, payload):
        outer = parse_message(payload)
        season_id, obsolete_music_id = int_value(outer, 1), int_value(outer, 2)
        if obsolete_music_id:
            raise NativeRpcError(3, diagnostic={'code': 'arena_music_id_obsolete'})
        finish_wire, player_wire = outer.get(3), outer.get(4)
        if not isinstance(finish_wire, bytes) or not isinstance(player_wire, bytes):
            raise NativeRpcError(3, diagnostic={'code': 'arena_finish_wire'})
        finish, player_result = parse_message(finish_wire), parse_message(player_wire)
        clear_status, score, combo = (int_value(finish, n) for n in (1, 2, 3))
        unique_id, submitted_index = _text(finish, 11), _text(player_result, 1)
        if (not unique_id or clear_status not in range(1, 6) or score not in range(0x80000000)
                or combo < 0 or int_value(player_result, 2) not in range(0x80000000)):
            raise NativeRpcError(3, diagnostic={'code': 'arena_finish_values'})
        rankings_wire = b''.join(message_field(3, value) for number, value in fields(payload) if number == 5)
        rankings = _ranking_messages(rankings_wire)
        reference_log = player_result.get(4)
        if not isinstance(reference_log, bytes):
            raise NativeRpcError(3, diagnostic={'code': 'arena_finish_log'})
        try:
            npc_log = dummy_battle_log(reference_log)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'arena_finish_log'}) from None
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN IMMEDIATE')
            cached = connection.execute('''SELECT * FROM arena_live_results
                WHERE profile_id=? AND live_unique_id=?''', (player['profile_id'], unique_id)).fetchone()
            if cached is not None:
                if cached['season_id'] != season_id:
                    raise NativeRpcError(9, diagnostic={'code': 'arena_finish_context'})
                _check_finish_player_index(json.loads(cached['room_players']), player['player_id'], submitted_index)
                response, room_id = cached['finish_response'], cached['room_id']
            else:
                record = connection.execute('SELECT * FROM arena_live_active WHERE profile_id=?',
                                            (player['profile_id'],)).fetchone()
                if record is None or record['season_id'] != season_id:
                    raise NativeRpcError(9, diagnostic={'code': 'arena_start_missing'})
                participants = json.loads(record['room_players'])
                _check_finish_player_index(participants, player['player_id'], submitted_index)
                ids = {row['player_id'] for row in participants}
                count = int_value(outer, 6)
                if (count and count != len(participants)) or any(set(value) - ids for value in rankings.values()):
                    raise NativeRpcError(3, diagnostic={'code': 'arena_finish_participants'})
                if combo > self.rules.select_music(record['music_id'], record['difficulty'])['score']['_fullComboCount']:
                    raise NativeRpcError(3, diagnostic={'code': 'arena_finish_combo'})
                state = self.progress.load(connection, player['profile_id'])
                response = self._personal_result(state, player, record, finish)
                self.progress.save(connection, player['profile_id'], state)
                player_wire = _account_player_result(player_wire, finish_wire, player['player_id'])
                connection.execute('''INSERT INTO arena_live_results
                    (profile_id,live_unique_id,season_id,regulation_id,league_id,music_id,difficulty,boost,
                     deck_wire,room_id,room_players,live_finish,player_result,rankings,completed_at,finish_response)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (player['profile_id'], unique_id, season_id, record['regulation_id'], record['league_id'],
                     record['music_id'], record['difficulty'], record['boost'], record['deck_wire'], record['room_id'],
                     record['room_players'], finish_wire, player_wire, json.dumps(rankings, separators=(',', ':')), now, response))
                connection.execute('DELETE FROM arena_live_active WHERE profile_id=?', (player['profile_id'],))
                room_id = record['room_id']
        if self.room_complete is not None:
            self.room_complete(room_id, npc_log)
        return response

    def _apply_live_rewards(self, connection, player, state, record, rank, now):
        boost = state['live_boost']
        try:
            consumed = self.rules.snapshot_boost(boost['amount'], boost['previous_recovery_at'], now, record['boost'])
        except ValueError:
            raise NativeRpcError(9, diagnostic={'code': 'arena_boost_balance'}) from None
        boost.update(amount=consumed['amount'], previous_recovery_at=consumed['previous_recovery_at'])
        rates = self.rules.boost_bonus(record['boost'])
        characters = self.single._apply_exp(state, record['deck_wire'], player, self.rules.exp_base(rank), rates)
        stamp = state['live_stamp']
        state['live_stamp'] = self.rules.update_live_stamp(stamp['live_completed'], stamp['reward_gained'],
                                                         stamp['last_live_completed_at'], now)
        cleared = int_value(parse_message(record['live_finish']), 1) >= 2
        if self.season_pass is not None:
            self.season_pass.record_live(connection, player['profile_id'], music_id=record['music_id'],
                                        cleared=cleared, consumed_boost=record['boost'], now=now)
            self.season_pass.record_counter(connection, player['profile_id'], 102, 0, 0, 1, now)
        if self.mission is not None:
            self.mission.record_live(connection, player['profile_id'], music_id=record['music_id'],
                character_ids=characters, cleared=cleared, difficulty=record['difficulty'], score_rank=rank, mode=3, now=now)
        drops = self.single.skip_events.draw([row for row in self.live_rewards if row['_liveScoreRank'] == rank], self.rng)
        resources = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'] * rates['_liveMusicRewardRate'])
                     for row in drops]
        self.rewards.apply(state, resources, now=now)
        event_wire, event_resources, posted = self.single.skip_events.settle(state, record['deck_wire'],
            rank=rank, rates=rates, player_rank=self.rules.player_rank(state['rank_exp']), now=now, rng=self.rng,
            live_type=2)
        self.rewards.apply(state, event_resources, now=now)
        self.single.skip_events.gain_challenge_points(state, record['deck_wire'], rank=rank, rates=rates, now=now)
        return drops, event_wire, posted

    def result(self, player, request):
        season_id, obsolete_music_id, league_id = (int_value(request, n) for n in range(1, 4))
        if obsolete_music_id:
            raise NativeRpcError(3, diagnostic={'code': 'arena_music_id_obsolete'})
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN IMMEDIATE')
            record = connection.execute('''SELECT * FROM arena_live_results WHERE profile_id=?
                ORDER BY completed_at DESC,rowid DESC LIMIT 1''', (player['profile_id'],)).fetchone()
            if record is None or (record['season_id'], record['league_id']) != (season_id, league_id):
                raise NativeRpcError(9, diagnostic={'code': 'arena_finish_missing'})
            if record['result_response'] is not None:
                return record['result_response']
            participants = json.loads(record['room_players'])
            wires = {player['player_id']: record['player_result']}
            reference = parse_message(record['player_result'])[4]
            for entry in participants:
                if entry['is_dummy'] and not entry['disconnected']:
                    wires[entry['player_id']] = dummy_player_result(entry['player_id'], reference)
            room = self.room_lookup(record['room_id']) if self.room_lookup is not None else None
            if room is not None:
                for pid, wire in room['results'].items():
                    if pid in {row['player_id'] for row in participants}:
                        wires[pid] = wire
            online = [entry for entry in participants if entry['player_id'] in wires and not entry['disconnected']]
            if len(online) != len([entry for entry in participants if not entry['disconnected']]):
                return b''.join(message_field(1, value) for value in wires.values()) + integer_field(3, 0) + message_field(16, b'')
            rankings = {int(key): value for key, value in json.loads(record['rankings']).items()}
            total_order = rankings.get(1, {})
            if player['player_id'] in total_order:
                place = total_order[player['player_id']]
            else:
                ordered = sorted(enumerate(online), key=lambda item: (
                    -int_value(parse_message(wires[item[1]['player_id']]), 2), item[0]))
                place = next(index for index, (_, row) in enumerate(ordered, 1) if row['player_id'] == player['player_id'])
            state, arena, season = self._context(connection, player, season_id, now)
            before_rates = (self.single.offline_bonus.capture_rates(state, now=now)
                            if self.single.offline_bonus is not None else None)
            before = season['lp'][str(league_id)]
            before_rank = self.rank(league_id, before)
            after = self.apply_point(before, self.point_delta(place, len(participants)), before_rank['_pointBehaviorType'])
            season['lp'][str(league_id)] = after
            season['max_lp'][str(league_id)] = max(after, season['max_lp'][str(league_id)])
            season['max_point'] = max(after, season['max_point'])
            season['plays'] += 1
            season['band_plays'][str(league_id)] = season['band_plays'].get(str(league_id), 0) + 1
            season['wins'][str(league_id)] = season['wins'].get(str(league_id), 0) + 1 if place == 1 else 0
            season['decks'][str(league_id)] = record['deck_wire'].hex()
            season['history'].append({'at': datetime.fromtimestamp(now, JST).isoformat(),
                                      'players': [row['player_id'] for row in participants]})
            season['history'] = season['history'][-int(self.parameters['arena_recent_match_limit']):]
            rewards = [row for row in self.promotions.values() if row['_leagueId'] == league_id
                       and next(rank for rank in self.ranks if rank['_id'] == row['_rankMatchId'])['_lp'] <= after]
            promotion_ids = self._claim_promotions(state, arena, rewards, now)
            daily_id = len(arena['daily']['rewards']) + 1
            if daily_id in self.daily_rewards:
                row = self.daily_rewards[daily_id]
                self.rewards.apply(state, [(row['_resourceType'], row['_resourceId'], row['_resourceCount'])], now=now)
                arena['daily']['rewards'].append(daily_id)
            else:
                daily_id = 0
            total_score = sum(int_value(parse_message(wire), 2) for wire in wires.values())
            group = self.rules.music[record['music_id']]['_liveScoreRankGroup']
            count = len(online)
            eligible = [row for row in self.rules.rank_groups[group]
                        if total_score >= int(row['_requiredScore'] * math.sqrt(5.0 / count) * count)]
            rank = max(eligible, key=lambda row: row['_requiredScore'])['_liveScoreRank'] if eligible else 0
            rank = 2 if rank == 1 else rank
            drops, event_wire, posted = self._apply_live_rewards(connection, player, state, record, rank, now)
            notification = posted_notification(posted)
            if self.single.offline_bonus is not None:
                notification += self.single.offline_bonus.stock_changed_rates(
                    connection, player, state, before_rates, now=now)
            response = (b''.join(message_field(1, wires[row['player_id']]) for row in participants if row['player_id'] in wires)
                + integer_field(3, 1) + _packed(4, promotion_ids) + _packed(5, [row['_id'] for row in drops])
                + _scalars((6, daily_id), (7, after), (8, after - before), (9, season['mp']), (10, 0))
                + message_field(16, notification))
            for field, value in fields(event_wire):
                if field == 2:
                    response += message_field(11, value)
                elif field == 4:
                    response += message_field(12, value)
            self.progress.save(connection, player['profile_id'], state)
            connection.execute('UPDATE arena_live_results SET result_response=? WHERE profile_id=? AND live_unique_id=?',
                               (response, player['profile_id'], record['live_unique_id']))
        if self.room_release is not None:
            self.room_release(record['room_id'])
        return response

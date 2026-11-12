"""Master-driven local event boxes, points, decks and challenge rankings."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import time

from deathnote.game.players.inventory import member_cards
from deathnote.game.live.event import NativeLiveEvent, active
from deathnote.game.live.music import _member_detail
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.live.single import int_value, parse_message
from deathnote.protocol.wire import integer_field, message_field, varint as encode_varint
from deathnote.protocol.decode import fields, varint

EVENT_PREFIX = '/app.event.EventService/'
EXECUTE_BOX_METHOD = EVENT_PREFIX + 'ExecuteBoxGacha'
RESET_BOX_METHOD = EVENT_PREFIX + 'ResetBoxGacha'
EVENT_DECK_METHOD = EVENT_PREFIX + 'GetDeck'
EVENT_RANK_METHOD = EVENT_PREFIX + 'GetRank'
EVENT_RANKING_METHOD = EVENT_PREFIX + 'GetRankingList'
CHALLENGE_RANKING_METHOD = EVENT_PREFIX + 'GetChallengeMusicRanking'


def deck_detail(deck, state, created_at):
    """Use the same historical member/support DTO as ordinary music rankings."""
    owned = member_cards(state, created_at)
    source = parse_message(deck)
    result = integer_field(1, int_value(source, 1)) + message_field(2, source.get(2, b''))
    for number, value in fields(deck):
        if number != 3:
            continue
        slot = parse_message(value)
        card = owned[str(int_value(slot, 3))]
        support = state['support_cards'].get(str(int_value(slot, 4)))
        support_wire = (integer_field(1, support['master_id']) + integer_field(2, support['exp'])
                        + integer_field(3, support['card_rank'])) if support is not None else b''
        result += message_field(3, integer_field(1, int_value(slot, 1))
            + integer_field(2, int_value(slot, 2)) + message_field(3, _member_detail(card, state))
            + message_field(4, support_wire))
    return result + integer_field(4, int_value(source, 4))


def repeated_ints(payload, tag):
    values = []
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        if number != tag:
            continue
        if isinstance(value, int):
            values.append(value)
        else:
            position = 0
            while position < len(value):
                item, position = varint(value, position)
                values.append(item)
    return list(dict.fromkeys(values))


class NativeEvent:
    methods = frozenset((EXECUTE_BOX_METHOD, RESET_BOX_METHOD, EVENT_DECK_METHOD,
                         EVENT_RANK_METHOD, EVENT_RANKING_METHOD, CHALLENGE_RANKING_METHOD))

    def __init__(self, login, player_data, master_directory, *, profile_builder,
                 clock=time.time, rng=None):
        self.login, self.player_data, self.clock = login, player_data, clock
        self.progress = player_data.progress
        self.profile_builder = profile_builder
        self.rng = random.SystemRandom() if rng is None else rng
        self.live_events = NativeLiveEvent(master_directory)
        self.rewards = player_data.gacha.rewards
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.events = {row['_id']: row for row in rows('MasterEvent')}
        self.challenge_music = {row['_id']: row for row in rows('MasterChallengeMusic')}
        self.boxes = {row['_eventId']: row for row in rows('MasterEventBoxGacha')}
        self.box_rewards = {row['_id']: row for row in rows('MasterEventBoxGachaReward')}

    def player_fields(self, state, *, now):
        self.live_events.initialize_challenge(state, now=now)
        return (b''.join(message_field(26, integer_field(1, value['event_id'])
                          + integer_field(2, value['reward_id']) + integer_field(3, value['count']))
                        for _, value in sorted(state.get('event_box_rewards', {}).items()))
                + b''.join(message_field(41, integer_field(1, int(event_id))
                            + integer_field(2, value['point']))
                           for event_id, value in sorted(state.get('challenge_lives', {}).items(),
                                                         key=lambda item: int(item[0]))))

    def _event(self, event_id):
        if event_id not in self.events:
            raise NativeRpcError(3, diagnostic={'code': 'event_id'})
        return self.events[event_id]

    def _box_pool(self, state, event_id):
        master = self.boxes.get(event_id)
        if master is None:
            raise NativeRpcError(9, diagnostic={'code': 'event_box_not_configured'})
        event = state.setdefault('events', {}).setdefault(str(event_id),
            {'point': 0, 'medal': 0, 'box_gacha_step': 0})
        rows = [row for row in self.box_rewards.values() if row['_eventId'] == event_id]
        steps = sorted({row['_step'] for row in rows})
        if not steps:
            raise NativeRpcError(9, diagnostic={'code': 'event_box_not_configured'})
        step = event['box_gacha_step']
        selected_step = min(max(step, steps[0]), steps[-1])
        return master, event, [row for row in rows if row['_step'] == selected_step]

    def _execute(self, player, event_id, count):
        event = self._event(event_id)
        now = int(self.clock())
        if not 0 < count <= 0x7fffffff:
            raise NativeRpcError(3, diagnostic={'code': 'event_box_count'})
        if not active(event, now):
            raise NativeRpcError(9, diagnostic={'code': 'event_closed'})
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            box, record, rows = self._box_pool(state, event_id)
            owned = state.setdefault('event_box_rewards', {})
            remaining = {row['_id']: row['_count'] - owned.get(str(row['_id']), {}).get('count', 0)
                         for row in rows}
            total = sum(remaining.values())
            item_key, price = str(event['_eventItemId']), box['_price'] * count
            if count > total or state['items'].get(item_key, 0) < price:
                raise NativeRpcError(9, diagnostic={'code': 'event_box_balance_or_empty'})
            selected, resources = [], []
            for _ in range(count):
                draw = self.rng.randrange(total)
                for row in rows:
                    reward_id = row['_id']
                    if draw < remaining[reward_id]:
                        remaining[reward_id] -= 1
                        total -= 1
                        selected.append(reward_id)
                        previous = owned.setdefault(str(reward_id),
                            {'event_id': event_id, 'reward_id': reward_id, 'count': 0})
                        previous['count'] += 1
                        resources.append((row['_resourceType'], row['_resourceId'], row['_resourceCount']))
                        break
                    draw -= remaining[reward_id]
            state['items'][item_key] -= price
            self.rewards.apply(state, resources, now=now)
            self.progress.save(connection, player['profile_id'], state)
        return message_field(1, b''.join(encode_varint(key) for key in selected)) + message_field(2, b'')

    def _reset(self, player, event_id):
        event = self._event(event_id)
        if not active(event, int(self.clock())):
            raise NativeRpcError(9, diagnostic={'code': 'event_closed'})
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            box, record, rows = self._box_pool(state, event_id)
            owned = state.setdefault('event_box_rewards', {})
            required = [row for row in rows if row['_reset']]
            if not required:
                required = rows
            if any(owned.get(str(row['_id']), {}).get('count', 0) < row['_count'] for row in required):
                raise NativeRpcError(9, diagnostic={'code': 'event_box_reset_target'})
            record['box_gacha_step'] = max(record['box_gacha_step'], rows[0]['_step']) + 1
            for row in rows:
                owned.pop(str(row['_id']), None)
            self.progress.save(connection, player['profile_id'], state)
        return b''

    def _profiles(self, connection):
        return connection.execute('''SELECT p.profile_id,p.player_id,p.created_at,e.name,e.updated_at
            FROM players p LEFT JOIN player_profile_edits e ON e.profile_id=p.profile_id
            ORDER BY p.profile_id''').fetchall()

    def _ranking(self, event_id):
        entries = []
        with closing(sqlite3.connect(self.login.database)) as connection:
            connection.execute('BEGIN')
            for row in self._profiles(connection):
                state = self.progress.load(connection, row[0])
                point = state.get('events', {}).get(str(event_id), {}).get('point', 0)
                if point:
                    entries.append((point, row[0], self.profile_builder(connection, row, state)))
        return sorted(entries, key=lambda value: (-value[0], value[1]))

    def _challenge_ranking(self, music_id, own_profile_id):
        if music_id not in self.challenge_music:
            raise NativeRpcError(3, diagnostic={'code': 'challenge_music_id'})
        entries = []
        with closing(sqlite3.connect(self.login.database)) as connection:
            connection.execute('BEGIN')
            for row in self._profiles(connection):
                state = self.progress.load(connection, row[0])
                record = state.get('challenge_scores', {}).get(str(music_id))
                if record is None or not record['score']:
                    continue
                profile = self.profile_builder(connection, row, state)
                wire = (message_field(1, profile) + integer_field(3, record['score'])
                        + message_field(4, bytes.fromhex(record['deck'])))
                entries.append((record['score'], row[0], wire))
        entries.sort(key=lambda value: (-value[0], value[1]))
        rank, score = next(((i, value[0]) for i, value in enumerate(entries, 1)
                            if value[1] == own_profile_id), (0, 0))
        return (b''.join(message_field(1, value[2]) for value in entries)
                + integer_field(2, rank) + integer_field(3, score))

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        request = parse_message(payload)
        if method == CHALLENGE_RANKING_METHOD:
            return self._challenge_ranking(int_value(request, 1), player['profile_id'])
        if method == EVENT_DECK_METHOD:
            event_id = int_value(request, 2)
            self._event(event_id)
            target = request.get(1, b'')
            if not isinstance(target, bytes):
                raise NativeRpcError(3)
            try:
                target = target.decode('utf8')
            except UnicodeError:
                raise NativeRpcError(3) from None
            with closing(sqlite3.connect(self.login.database)) as connection:
                row = connection.execute('SELECT profile_id FROM players WHERE player_id=?', (target,)).fetchone()
                if row is None:
                    raise NativeRpcError(5)
                state = self.progress.load(connection, row[0])
                wire = state.get('events', {}).get(str(event_id), {}).get('deck', '')
            return message_field(1, bytes.fromhex(wire))
        event_id = int_value(request, 1)
        event = self._event(event_id)
        if method == EXECUTE_BOX_METHOD:
            return self._execute(player, event_id, int_value(request, 2))
        if method == RESET_BOX_METHOD:
            return self._reset(player, event_id)
        ranking = [] if event['_isRankingDisabled'] else self._ranking(event_id)
        if method == EVENT_RANK_METHOD:
            rank = next((i for i, value in enumerate(ranking, 1) if value[1] == player['profile_id']), 0)
            return integer_field(1, rank)
        try:
            ranks = repeated_ints(payload, 2)
        except ValueError:
            raise NativeRpcError(3) from None
        if any(not 0 < value <= 0x7fffffff for value in ranks):
            raise NativeRpcError(3)
        requested = set(ranks)
        return b''.join(message_field(1, message_field(1, value[2])
            + integer_field(2, rank) + integer_field(3, value[0]))
            for rank, value in enumerate(ranking, 1) if rank in requested)

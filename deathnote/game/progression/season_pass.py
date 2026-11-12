"""Persistent season-pass missions and rewards for the original native client."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress
from deathnote.game.players.inventory import member_cards
from .rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.calendar import NativeServiceCalendar
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields, varint


FETCH_METHOD = '/app.seasonpass.SeasonPassService/Fetch'
RECEIVE_REWARD_METHOD = '/app.seasonpass.SeasonPassService/ReceiveReward'
RECEIVE_MISSION_METHOD = '/app.seasonpass.SeasonPassService/ReceiveMission'
SERVICE_TIMEZONE = timezone(timedelta(hours=9))
_COUNTER_SELECTORS = {79: '_bandId', 81: '_musicId', 124: '_scoreRank',
                      148: '_memberCardId', 153: '_memberCardId',
                      149: '_supportCardId', 155: '_supportCardId'}
_EXACT_SCORE_RANK_MIGRATION = '124-exact-score-rank-v1'


def _scalars(values):
    return b''.join(integer_field(number, value) for number, value in values if value)


def _master_time(value):
    return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(
        tzinfo=SERVICE_TIMEZONE).timestamp())


def _request(payload, include_missions):
    season_pass_id, mission_ids = 0, set()
    for number, value in fields(payload):
        if number == 1:
            if not isinstance(value, int) or not 0 < value <= 0x7fffffffffffffff:
                raise ValueError('Season-pass ID must be positive int64')
            season_pass_id = value
        elif number == 2 and include_missions:
            if isinstance(value, bytes):
                offset = 0
                while offset < len(value):
                    mission_id, offset = varint(value, offset)
                    mission_ids.add(mission_id)
            else:
                mission_ids.add(value)
    if not season_pass_id or any(not 0 < value <= 0x7fffffffffffffff for value in mission_ids):
        raise ValueError('Season-pass mission ID must be positive int64')
    return season_pass_id, mission_ids


class NativeSeasonPass:
    methods = frozenset((FETCH_METHOD, RECEIVE_REWARD_METHOD, RECEIVE_MISSION_METHOD))

    def __init__(self, login, master_directory, *, clock=time.time):
        self.login = login
        self.clock = clock
        self.database = login.database
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.passes = {row['_id']: row for row in rows('MasterSeasonPass')}
        self.periods = {key: (_master_time(row['_startAt']), _master_time(row['_endAt']))
                        for key, row in self.passes.items()}
        self.levels = rows('MasterSeasonPassLevel')
        self.level_rewards = rows('MasterSeasonPassLevelReward')
        self.reward_resources = {row['_id']: row for row in rows('MasterSeasonPassReward')}
        self.missions = {row['_id']: row for row in rows('MasterSeasonPassMission')}
        self.music = {row['_id']: row for row in rows('MasterLiveMusic')}
        self.rewards = NativeRewards(directory)
        self.progress = NativeLiveProgress(login)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_season_passes (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    season_pass_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY(profile_id, season_pass_id)
                );
                CREATE TABLE IF NOT EXISTS player_season_pass_counters (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    mission_type INTEGER NOT NULL,
                    param1 INTEGER NOT NULL,
                    param2 INTEGER NOT NULL,
                    progress INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, mission_type, param1, param2)
                );
                CREATE TABLE IF NOT EXISTS player_season_pass_login_days (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    day_start INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_season_pass_counter_migrations (
                    name TEXT PRIMARY KEY
                );
            ''')
            self._migrate_score_rank_counters(connection)

    @staticmethod
    def _migrate_score_rank_counters(connection):
        """Convert the former >=rank buckets into the client's exact-rank counters."""
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('''
            SELECT 1 FROM player_season_pass_counter_migrations WHERE name = ?
        ''', (_EXACT_SCORE_RANK_MIGRATION,)).fetchone() is not None:
            return
        groups = {}
        for profile_id, rank, param2, progress in connection.execute('''
            SELECT profile_id, param1, param2, progress FROM player_season_pass_counters
            WHERE mission_type = 124 AND param1 > 0 ORDER BY profile_id, param2, param1
        '''):
            groups.setdefault((profile_id, param2), []).append((rank, progress))
        for (profile_id, param2), ranks in groups.items():
            for index, (rank, progress) in enumerate(ranks):
                higher = ranks[index + 1][1] if index + 1 < len(ranks) else 0
                exact = progress - higher
                if exact < 0:
                    raise ValueError('Legacy mission counter 124 has nonmonotonic rank counts')
                connection.execute('''
                    UPDATE player_season_pass_counters SET progress = ?
                    WHERE profile_id = ? AND mission_type = 124 AND param1 = ? AND param2 = ?
                ''', (exact, profile_id, rank, param2))
        connection.execute('INSERT INTO player_season_pass_counter_migrations(name) VALUES (?)',
                           (_EXACT_SCORE_RANK_MIGRATION,))

    def _active(self, now):
        return {key for key, (start, end) in self.periods.items() if start <= now < end}

    @staticmethod
    def _counter_key(mission):
        mission_type = mission['_missionType']
        if mission_type in _COUNTER_SELECTORS:
            return mission_type, mission[_COUNTER_SELECTORS[mission_type]], 0
        if mission_type not in (1, 3, 76, 102, 114, 129):
            raise NotImplementedError('Season-pass mission type: ' + str(mission_type))
        return mission_type, mission['_value'], 0

    def _mission_counter(self, connection, profile_id, master, progress):
        kind = master['_missionType']
        if kind == 124:
            return connection.execute('''
                SELECT COALESCE(SUM(progress), 0) FROM player_season_pass_counters
                WHERE profile_id = ? AND mission_type = 124 AND param1 > 0 AND param1 >= ?
            ''', (profile_id, master['_scoreRank'])).fetchone()[0]
        if kind in (148, 153):
            cards = [card for card in member_cards(progress).values()
                     if card['master_id'] == master['_memberCardId']]
            return int(bool(cards)) if kind == 148 else max(
                (card['card_rank'] for card in cards), default=0)
        if kind in (149, 155):
            card = progress.get('support_cards', {}).get(str(master['_supportCardId']))
            return int(card is not None) if kind == 149 else (card['card_rank'] if card is not None else 0)
        return self._counter(connection, profile_id, self._counter_key(master))

    @staticmethod
    def _counter(connection, profile_id, key):
        row = connection.execute('''
            SELECT progress FROM player_season_pass_counters
            WHERE profile_id = ? AND mission_type = ? AND param1 = ? AND param2 = ?
        ''', (profile_id, *key)).fetchone()
        return 0 if row is None else row[0]

    @staticmethod
    def _increment(connection, profile_id, key, delta):
        connection.execute('''
            INSERT INTO player_season_pass_counters(profile_id, mission_type, param1, param2, progress)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(profile_id, mission_type, param1, param2)
            DO UPDATE SET progress = progress + excluded.progress
        ''', (profile_id, *key, delta))

    @staticmethod
    def _save(connection, profile_id, season_pass_id, state):
        connection.execute('''
            INSERT INTO player_season_passes(profile_id, season_pass_id, state) VALUES (?, ?, ?)
            ON CONFLICT(profile_id, season_pass_id) DO UPDATE SET state = excluded.state
        ''', (profile_id, season_pass_id, json.dumps(state, separators=(',', ':'))))

    def _sync(self, connection, profile_id, now):
        day = self.calendar.day_start(now)
        login_day = connection.execute('''
            SELECT day_start FROM player_season_pass_login_days WHERE profile_id = ?
        ''', (profile_id,)).fetchone()
        if login_day is None or login_day[0] < day:
            self._increment(connection, profile_id, (1, 0, 0), 1)
        if login_day is None or login_day[0] != day:
            connection.execute('''
                INSERT INTO player_season_pass_login_days(profile_id, day_start) VALUES (?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET day_start = excluded.day_start
            ''', (profile_id, day))
        active = self._active(now)
        progress = self.progress.load(connection, profile_id)
        premium_passes = set(progress.get('premium_passes', ()))
        saved = {row[0]: json.loads(row[1]) for row in connection.execute('''
            SELECT season_pass_id, state FROM player_season_passes WHERE profile_id = ?
        ''', (profile_id,))}
        states = {}
        for season_pass_id in sorted(active):
            state = saved.get(season_pass_id, {'point': 0, 'is_premium': False,
                'last_received_level': 0, 'premium_last_received_level': 0, 'missions': {}})
            state['is_premium'] |= season_pass_id in premium_passes
            for mission_id, master in self.missions.items():
                if master['_seasonPassId'] != season_pass_id:
                    continue
                key = str(mission_id)
                mission = state['missions'].get(key)
                daily = master['_missionCategory'] == 1
                if mission is None or daily and mission['start_at'] < day:
                    counter = self._mission_counter(connection, profile_id, master, progress)
                    initial = master['_initialValue']
                    start_count = (0 if master['_missionType'] in (148, 149, 153, 155) or initial == 0
                                   else counter - 1 if initial == 1 else counter)
                    mission = {'start_count': start_count, 'is_completed': False,
                               'is_received': False, 'start_at': day if daily else now}
                    state['missions'][key] = mission
                elif daily:
                    mission['start_at'] = day
                current = self._mission_counter(connection, profile_id, master, progress)
                mission['is_completed'] |= current - mission['start_count'] >= master['_achievementCount']
            self._save(connection, profile_id, season_pass_id, state)
            states[season_pass_id] = state
        return states

    def record_counter(self, connection, profile_id, mission_type, param1, param2, delta, now):
        """Update a native mission counter inside the caller's existing transaction."""
        self._sync(connection, profile_id, now)
        self._increment(connection, profile_id, (mission_type, param1, param2), delta)
        self._sync(connection, profile_id, now)

    def record_login(self, connection, profile_id, now):
        """Initialize active passes and count at most one login per service day."""
        self._sync(connection, profile_id, now)

    def record_live(self, connection, profile_id, *, music_id, cleared, consumed_boost, now):
        """Call once per committed LIVE, after the result's GUID cache lookup."""
        self._sync(connection, profile_id, now)
        if consumed_boost:
            self._increment(connection, profile_id, (114, 0, 0), consumed_boost)
        if cleared:
            self._increment(connection, profile_id, (76, 0, 0), 1)
            self._increment(connection, profile_id, (81, music_id, 0), 1)
            for band_id in self.music[music_id]['_bandIDs']:
                self._increment(connection, profile_id, (79, band_id, 0), 1)
        self._sync(connection, profile_id, now)

    def mission_counters(self, connection, profile_id, *, now=None):
        self.record_login(connection, profile_id, int(self.clock()) if now is None else now)
        counters = {tuple(row[:3]): row[3] for row in connection.execute('''
                SELECT mission_type, param1, param2, progress FROM player_season_pass_counters
                WHERE profile_id = ? ORDER BY mission_type, param1, param2
            ''', (profile_id,))}
        progress = self.progress.load(connection, profile_id)
        for master in self.missions.values():
            if master['_missionType'] in (148, 149, 153, 155):
                counters[self._counter_key(master)] = self._mission_counter(
                    connection, profile_id, master, progress)
        return b''.join(message_field(1, _scalars(((1, key[0]), (2, key[1]),
                                                (3, key[2]), (4, value))))
                       for key, value in sorted(counters.items()))

    def _fetch(self, connection, profile_id, states, now):
        output = [self.mission_counters(connection, profile_id, now=now)]
        for season_pass_id, state in states.items():
            base = _scalars(((1, season_pass_id), (2, state['point']), (3, state['is_premium']),
                (4, state['last_received_level']), (5, state['premium_last_received_level'])))
            detail = [message_field(1, base)]
            for mission_id, mission in sorted(state['missions'].items(), key=lambda item: int(item[0])):
                detail.append(message_field(2, _scalars(((1, season_pass_id), (2, int(mission_id)),
                    (3, mission['start_count']), (4, mission['is_completed']),
                    (5, mission['is_received']), (6, mission['start_at'])))))
            output.append(message_field(2, b''.join(detail)))
        return b''.join(output)

    def _receive_reward(self, connection, profile_id, season_pass_id, state, now):
        group = self.passes[season_pass_id]['_levelGroup']
        level = max(row['_level'] for row in self.levels
                    if row['_group'] == group and row['_point'] <= state['point'])
        resources = []
        for row in self.level_rewards:
            if row['_seasonPassId'] != season_pass_id or row['_level'] > level:
                continue
            premium = row['_isPremium']
            if premium and not state['is_premium']:
                continue
            mark = 'premium_last_received_level' if premium else 'last_received_level'
            if row['_level'] <= state[mark]:
                continue
            for reward_id in row['_rewardIds']:
                reward = self.reward_resources[reward_id]
                resources.append((reward['_resourceType'], reward['_resourceId'], reward['_resourceCount']))
        progress = self.progress.load(connection, profile_id)
        before = dict(progress['items'])
        self.rewards.apply(progress, resources, now=now)
        self.progress.save(connection, profile_id, progress)
        state['last_received_level'] = max(level, state['last_received_level'])
        if state['is_premium']:
            state['premium_last_received_level'] = max(level, state['premium_last_received_level'])
        self._save(connection, profile_id, season_pass_id, state)
        notification = b''.join(message_field(4, _scalars(((1, int(item_id)), (2, amount - before.get(item_id, 0)))))
            for item_id, amount in sorted(progress['items'].items(), key=lambda item: int(item[0]))
            if amount != before.get(item_id, 0))
        return message_field(1, notification)

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method not in self.methods:
            raise NotImplementedError(method)
        if method != FETCH_METHOD:
            try:
                season_pass_id, mission_ids = _request(payload, method == RECEIVE_MISSION_METHOD)
            except (ValueError, TypeError):
                raise NativeRpcError(3) from None
        else:
            try:
                list(fields(payload))
            except ValueError:
                raise NativeRpcError(3) from None
        profile_id, now = player['profile_id'], int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            states = self._sync(connection, profile_id, now)
            if method == FETCH_METHOD:
                return self._fetch(connection, profile_id, states, now)
            if season_pass_id not in states:
                raise NativeRpcError(9)
            state = states[season_pass_id]
            if method == RECEIVE_REWARD_METHOD:
                return self._receive_reward(connection, profile_id, season_pass_id, state, now)
            for mission_id in mission_ids:
                master = self.missions.get(mission_id)
                if master is None or master['_seasonPassId'] != season_pass_id:
                    raise NativeRpcError(3)
                mission = state['missions'][str(mission_id)]
                if not mission['is_completed']:
                    raise NativeRpcError(9)
                if not mission['is_received']:
                    mission['is_received'] = True
                    state['point'] += master['_seasonPassPoint']
            self._save(connection, profile_id, season_pass_id, state)
            return b''

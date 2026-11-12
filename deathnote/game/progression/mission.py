"""Original character missions and band-evaluation music-unlock missions."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import cached_property
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.inventory import member_cards
from deathnote.game.players.progress import NativeLiveProgress
from .rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields, varint


RECEIVE_CHARACTER_METHOD = '/app.mission.MissionService/ReceiveCharacter'
FETCH_METHOD = '/app.mission.MissionService/Fetch'
RECEIVE_METHOD = '/app.mission.MissionService/Receive'
FETCH_EVENT_METHOD = '/app.mission.MissionService/FetchEvent'
RECEIVE_EVENT_METHOD = '/app.mission.MissionService/ReceiveEvent'
RECEIVE_LIMITED_METHOD = '/app.mission.MissionService/ReceiveLimited'
SERVICE_TIMEZONE = timezone(timedelta(hours=9))


def _scalars(values):
    return b''.join(integer_field(number, value) for number, value in values if value)


def _request(payload):
    character_id, mission_ids = 0, set()
    try:
        for number, value in fields(payload):
            if number < 1:
                raise ValueError('Protobuf field zero')
            if number == 1:
                if not isinstance(value, int):
                    raise ValueError('Character ID must be int64')
                character_id = value
            elif number == 2:
                if isinstance(value, bytes):
                    offset = 0
                    while offset < len(value):
                        mission_id, offset = varint(value, offset)
                        mission_ids.add(mission_id)
                else:
                    mission_ids.add(value)
        if not 0 < character_id <= 0x7fffffffffffffff or any(
                not 0 < value <= 0x7fffffffffffffff for value in mission_ids):
            raise ValueError('Character and mission IDs must be positive int64')
    except ValueError:
        raise NativeRpcError(3) from None
    return character_id, mission_ids


def _mission_ids(payload):
    mission_ids = set()
    try:
        for number, value in fields(payload):
            if number < 1:
                raise ValueError('Protobuf field zero')
            if number != 1:
                continue
            if isinstance(value, bytes):
                offset = 0
                while offset < len(value):
                    mission_id, offset = varint(value, offset)
                    mission_ids.add(mission_id)
            else:
                mission_ids.add(value)
        if any(not 0 < value <= 0x7fffffffffffffff for value in mission_ids):
            raise ValueError('Mission IDs must be positive int64')
    except ValueError:
        raise NativeRpcError(3) from None
    return mission_ids


class NativeMission:
    methods = frozenset((FETCH_METHOD, RECEIVE_METHOD, RECEIVE_CHARACTER_METHOD,
                         FETCH_EVENT_METHOD, RECEIVE_EVENT_METHOD, RECEIVE_LIMITED_METHOD))

    def __init__(self, login, master_directory, *, progress=None, rewards=None,
                 season_pass=None, clock=time.time):
        self.login, self.database, self.clock = login, login.database, clock
        self.master_directory = Path(master_directory)
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        self.season_pass = season_pass
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.missions = {row['_id']: row for row in rows('MasterCharacterMission')}
        self.music_missions = {row['_id']: row for row in rows('MasterMission')
                               if row['_missionCategory'] == 15 and row['_missionType'] == 30}
        self.reward_masters = {row['_id']: row for row in rows('MasterMissionReward')}
        self.characters = {row['_id']: row for row in rows('MasterCharacter') if not row['_isNonPlayable']}
        self.member_masters = {row['_id']: row for row in rows('MasterMemberCard')}
        self.member_ranks = {(row['_group'], row['_rank']): row
                             for row in rows('MasterMemberCardRank')}
        self.support_masters = {row['_id']: row for row in rows('MasterSupportCard')}
        self.music_masters = {row['_id']: row for row in rows('MasterLiveMusic')}
        self.stamps = {row['_id']: row for row in rows('MasterStamp')}
        self.items = {row['_id']: row for row in rows('MasterItem')}
        self.friendships = rows('MasterCharacterFriendship')
        self.friendship_ranks = rows('MasterCharacterFriendshipRank')
        self.character_ranks = rows('MasterCharacterRank')
        self.limited_missions = {row['_id']: row for row in rows('MasterLimitedMission')}
        self.limited_groups = {row['_id']: row for row in rows('MasterLimitedMissionGroup')}
        self.event_missions = {row['_id']: row for row in rows('MasterEventMission')}
        self.event_masters = {row['_id']: row for row in rows('MasterEvent')}
        self.member_levels = rows('MasterMemberCardLevel')
        self.support_levels = rows('MasterSupportCardLevel')
        self.exchange_products = {row['_id']: row for row in rows('MasterExchangeProduct')}
        self.story_masters = {row['_id']: row for row in rows('MasterStoryEpisode')}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_character_mission_rewards (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    character_id INTEGER NOT NULL,
                    mission_id INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, character_id, mission_id)
                );
                CREATE TABLE IF NOT EXISTS player_character_live_counters (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    mission_type INTEGER NOT NULL,
                    character_id INTEGER NOT NULL,
                    music_id INTEGER NOT NULL,
                    progress INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, mission_type, character_id, music_id)
                );
                CREATE TABLE IF NOT EXISTS player_normal_missions (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    mission_id INTEGER NOT NULL,
                    start_count INTEGER NOT NULL,
                    is_completed INTEGER NOT NULL,
                    is_received INTEGER NOT NULL,
                    start_at INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, mission_id)
                );
                CREATE TABLE IF NOT EXISTS player_extra_missions (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    category TEXT NOT NULL, mission_id INTEGER NOT NULL,
                    start_count INTEGER NOT NULL, is_completed INTEGER NOT NULL,
                    is_received INTEGER NOT NULL, start_at INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, category, mission_id)
                );
                CREATE TABLE IF NOT EXISTS player_common_live_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    music_id INTEGER NOT NULL, difficulty INTEGER NOT NULL,
                    score_rank INTEGER NOT NULL, mode INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL
                );
            ''')

    @cached_property
    def band_rank(self):
        from .band_rank import NativeBandRank
        return NativeBandRank(self.master_directory)

    @cached_property
    def live_rules(self):
        from deathnote.game.live.rules import NativeLiveRules
        return NativeLiveRules(self.master_directory)

    @staticmethod
    def _time(value):
        return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(
            tzinfo=SERVICE_TIMEZONE).timestamp())

    def _active(self, master, now):
        start, end = master['_startAt'], master['_endAt']
        return ((start in ('', 'null') or self._time(start) <= now)
                and (end in ('', 'null') or now <= self._time(end)))

    def record_live(self, connection, profile_id, *, music_id, character_ids, cleared,
                    difficulty=0, score_rank=0, mode=1, now=None):
        """Count a committed, uncached clear inside the caller's result transaction."""
        if not cleared:
            return
        connection.execute('INSERT INTO player_common_live_history(profile_id,music_id,difficulty,score_rank,mode,completed_at) VALUES (?,?,?,?,?,?)',
            (profile_id, music_id, difficulty, score_rank, mode, int(self.clock()) if now is None else now))
        music = self.music_masters[music_id]
        music_characters = {key for key, character in self.characters.items()
                            if character['_bandID'] in music['_bandIDs']
                            or key in music['_vocalCharacterIDs']}
        counters = [(80, key, 0) for key in character_ids]
        counters.extend((111, key, music_id) for key in sorted(music_characters))
        connection.executemany('''
            INSERT INTO player_character_live_counters
                (profile_id, mission_type, character_id, music_id, progress)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(profile_id, mission_type, character_id, music_id)
            DO UPDATE SET progress = progress + 1
        ''', [(profile_id, *key) for key in counters])

    def _total(self, connection, profile_id, state, character_id, master):
        kind, value = master['_missionType'], master['_value']
        if kind in (80, 111):
            aggregate = 'SUM(progress)' if kind == 80 else 'COUNT(*)'
            return connection.execute(f'''
                SELECT COALESCE({aggregate}, 0) FROM player_character_live_counters
                WHERE profile_id = ? AND mission_type = ? AND character_id = ? AND progress > 0
            ''', (profile_id, kind, character_id)).fetchone()[0]
        if kind in (12, 42, 48, 54):
            cards = [card for card in member_cards(state).values()
                     if self.member_masters[card['master_id']]['_characterID'] == character_id]
            if kind == 12:
                return len(cards)
            if kind == 42:
                total = 0
                for card in cards:
                    group = self.member_masters[card['master_id']]['_memberCardRankGroup']
                    leader = self.member_ranks[(group, card['card_rank'])]['_leaderSkillLevel']
                    total += sum(level >= value for level in
                                 (leader, card['live_skill_level'], card['performance_skill_level']))
                return total
            key = 'awake_count' if kind == 48 else 'card_rank'
            return sum(card[key] >= value for card in cards)
        if kind in (14, 66):
            cards = [card for card in state.get('support_cards', {}).values()
                     if character_id in self.support_masters[card['master_id']]['_characterIDs']]
            return len(cards) if kind == 14 else sum(card['card_rank'] >= value for card in cards)
        if kind == 16:
            owned = [int(key) for key in state.get('stamps', {})]
            return sum(character_id in self.stamps[key]['_characterIds'] for key in owned)
        if kind == 25:
            total = 0
            for friendship in self.friendships:
                if character_id not in (friendship['_masterCharacterIdA'], friendship['_masterCharacterIdB']):
                    continue
                exp = state['friendship_exp'].get(str(friendship['_id']), 0)
                exp_rank = max(row['_rank'] for row in self.friendship_ranks if row['_exp'] <= exp)
                character_limits = [max(row['_rank'] for row in self.character_ranks
                                        if row['_exp'] <= state['character_exp'].get(str(key), 0))
                                    for key in (friendship['_masterCharacterIdA'],
                                                friendship['_masterCharacterIdB'])]
                total += min(exp_rank, *character_limits)
            return total
        raise NotImplementedError('Character mission type: ' + str(kind))

    def _normal_progress(self, connection, profile_id, state, now):
        ratings = {band_id: self.band_rank.evaluation_value(state, band_id)
                   for band_id in sorted({row['_bandId'] for row in self.music_missions.values()})}
        saved = {row[0]: {'start_count': row[1], 'is_completed': bool(row[2]),
                          'is_received': bool(row[3]), 'start_at': row[4]}
                 for row in connection.execute('''
                    SELECT mission_id,start_count,is_completed,is_received,start_at
                    FROM player_normal_missions WHERE profile_id=?
                 ''', (profile_id,))}
        current = {}
        for mission_id, master in sorted(self.music_missions.items()):
            record = saved.get(mission_id, {'start_count': 0, 'is_completed': False,
                'is_received': False, 'start_at': self._time(master['_startAt'])
                if master['_startAt'] not in ('', 'null') else 0})
            record['is_completed'] |= (self._active(master, now)
                and ratings[master['_bandId']] - record['start_count'] >= master['_achievementCount'])
            connection.execute('''
                INSERT INTO player_normal_missions
                    (profile_id,mission_id,start_count,is_completed,is_received,start_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(profile_id,mission_id)
                DO UPDATE SET is_completed=excluded.is_completed
            ''', (profile_id, mission_id, record['start_count'], record['is_completed'],
                  record['is_received'], record['start_at']))
            current[mission_id] = record
        return ratings, current

    @staticmethod
    def _counter_key(master):
        kind = master['_missionType']
        if kind == 5:
            return kind, master['_exchangeId'], 0
        if kind == 27:
            return kind, master['_characterId'], 0
        if kind == 39:
            return kind, master['_value'], 0
        if kind == 79:
            return kind, master['_bandId'], 0
        if kind == 81:
            return kind, master['_musicId'], 0
        if kind == 98:
            return kind, master['_scoreRank'], master['_cardType']
        if kind == 104:
            return kind, master['_storyChapterId'], master['_episodeId']
        if kind == 108:
            return kind, master['_musicId'], master['_musicDifficulty']
        if kind == 125:
            return kind, master['_scoreRank'], master['_bandId']
        return kind, 0, 0

    def _extra_total(self, connection, profile_id, state, master):
        kind = master['_missionType']
        if kind in (1, 3):
            row = connection.execute('SELECT progress FROM player_season_pass_counters WHERE profile_id=? AND mission_type=? AND param1=0 AND param2=0',
                                     (profile_id, kind)).fetchone()
            return row[0] if row else 0
        if kind == 2:
            return (connection.execute('SELECT COUNT(*) FROM player_normal_missions WHERE profile_id=? AND is_received=1', (profile_id,)).fetchone()[0]
                + connection.execute('SELECT COUNT(*) FROM player_extra_missions WHERE profile_id=? AND is_received=1', (profile_id,)).fetchone()[0]
                + connection.execute('SELECT COUNT(*) FROM player_character_mission_rewards WHERE profile_id=?', (profile_id,)).fetchone()[0])
        if kind == 5:
            products = [key for key, row in self.exchange_products.items() if row['_exchangeId'] == master['_exchangeId']]
            return sum(row[1] for row in connection.execute('SELECT product_id,count FROM player_exchange_counts WHERE profile_id=?', (profile_id,))
                       if row[0] in products)
        if kind == 7:
            return connection.execute('SELECT COUNT(*) FROM player_profile_edits WHERE profile_id=?', (profile_id,)).fetchone()[0]
        if kind == 10:
            return connection.execute('SELECT COUNT(*) FROM player_friendships WHERE lower_profile_id=? OR upper_profile_id=?',
                                      (profile_id, profile_id)).fetchone()[0]
        if kind == 27:
            character_id = master['_characterId']
            levels = []
            for friendship in self.friendships:
                if character_id not in (friendship['_masterCharacterIdA'], friendship['_masterCharacterIdB']):
                    continue
                exp = state['friendship_exp'].get(str(friendship['_id']), 0)
                rank = max(row['_rank'] for row in self.friendship_ranks if row['_exp'] <= exp)
                levels.append(min(rank, *(max(row['_rank'] for row in self.character_ranks
                    if row['_exp'] <= state['character_exp'].get(str(key), 0)) for key in
                    (friendship['_masterCharacterIdA'], friendship['_masterCharacterIdB']))))
            return max(levels, default=0)
        cards = list(member_cards(state).values())
        if kind == 34:
            return max((max(row['_level'] for row in self.member_levels
                if row['_group'] == self.member_masters[card['master_id']]['_memberCardLevelGroup']
                and row['_exp'] <= state['member_exp'].get(str(card['id']), 0)) for card in cards), default=0)
        if kind == 39:
            return sum(sum(level >= master['_value'] for level in
                (self.member_ranks[(self.member_masters[card['master_id']]['_memberCardRankGroup'], card['card_rank'])]['_leaderSkillLevel'],
                 card['live_skill_level'], card['performance_skill_level'])) for card in cards)
        if kind in (46, 52):
            field = 'awake_count' if kind == 46 else 'card_rank'
            return max((card[field] - 1 for card in cards), default=0)
        if kind == 58:
            return max((max(row['_level'] for row in self.support_levels
                if row['_group'] == self.support_masters[card['master_id']]['_supportCardLevelGroup']
                and row['_exp'] <= card['exp']) for card in state['support_cards'].values()), default=0)
        if kind == 64:
            return max((card['card_rank'] - 1 for card in state['support_cards'].values()), default=0)
        if kind == 104:
            if master['_episodeId']:
                return int(str(master['_episodeId']) in state.get('story_records', {}))
            return sum(str(row['_id']) in state.get('story_records', {}) for row in self.story_masters.values()
                       if row['_chapterId'] == master['_storyChapterId'])
        if kind == 82:
            return len({row['music_id'] for row in state['scores'].values() if row['clear_status'] >= 2})
        if kind in (79, 81, 98, 108, 125):
            history = list(connection.execute('SELECT music_id,difficulty,score_rank,mode FROM player_common_live_history WHERE profile_id=?', (profile_id,)))
            if kind == 79:
                row = connection.execute('SELECT progress FROM player_season_pass_counters WHERE profile_id=? AND mission_type=79 AND param1=? AND param2=0',
                                         (profile_id, master['_bandId'])).fetchone()
                return row[0] if row else 0
            if kind == 81:
                ordinary = connection.execute('SELECT COUNT(*) FROM free_live_results WHERE profile_id=? AND music_id=? AND clear_status>=2',
                                               (profile_id, master['_musicId'])).fetchone()[0]
                battle = sum(dict(fields(row[0])).get(1, 0) >= 2 for row in connection.execute(
                    'SELECT live_finish FROM battle_live_results WHERE profile_id=? AND music_id=?', (profile_id, master['_musicId'])))
                return ordinary + battle + sum(row[3] == 4 and row[0] == master['_musicId'] for row in history)
            if kind == 108:
                return max(sum((not master['_musicId'] or row[0] == master['_musicId'])
                           and row[1] == master['_musicDifficulty'] + 1 for row in history),
                           sum(row['clear_status'] >= 2 and (not master['_musicId'] or row['music_id'] == master['_musicId'])
                               and row['difficulty'] == master['_musicDifficulty'] + 1 for row in state['scores'].values()))
            if kind == 98:
                return max(sum(row[2] >= master['_scoreRank'] and (not master['_cardType']
                    or row[0] in self.music_masters and self.music_masters[row[0]]['_musicType'] == master['_cardType']) for row in history),
                    sum(self.live_rules.score_rank(row['music_id'], row['high_score']) >= master['_scoreRank']
                        and (not master['_cardType'] or self.music_masters[row['music_id']]['_musicType'] == master['_cardType'])
                        for row in state['scores'].values() if row['music_id'] in self.music_masters))
            return max(sum(row[3] == 2 and row[2] >= master['_scoreRank']
                       and master['_bandId'] in self.music_masters[row[0]]['_bandIDs']
                       for row in history if row[0] in self.music_masters),
                       sum(row['mode'] == 2 and self.live_rules.score_rank(row['music_id'], row['high_score']) >= master['_scoreRank']
                           and master['_bandId'] in self.music_masters[row['music_id']]['_bandIDs']
                           for row in state['scores'].values() if row['music_id'] in self.music_masters))
        key = self._counter_key(master)
        row = connection.execute('SELECT progress FROM player_season_pass_counters WHERE profile_id=? AND mission_type=? AND param1=? AND param2=?',
                                 (profile_id, *key)).fetchone()
        return row[0] if row else 0

    def _extra_progress(self, connection, profile_id, state, category, now, event_id=None):
        masters = self.limited_missions if category == 'limited' else self.event_missions
        owned = {row[0]: {'start_count': row[1], 'is_completed': bool(row[2]),
                          'is_received': bool(row[3]), 'start_at': row[4]}
                 for row in connection.execute('SELECT mission_id,start_count,is_completed,is_received,start_at FROM player_extra_missions WHERE profile_id=? AND category=?',
                                               (profile_id, category))}
        current, counters = {}, {}
        created_at = connection.execute('SELECT created_at FROM players WHERE profile_id=?', (profile_id,)).fetchone()[0]
        for mission_id, master in sorted(masters.items()):
            if category == 'event' and master['_eventId'] != event_id:
                continue
            total = self._extra_total(connection, profile_id, state, master)
            if category == 'limited':
                group = self.limited_groups[master['_limitedMissionGroupId']]
                start = max(self._time(group['_startAt']) if group['_startAt'] not in ('', 'null') else created_at,
                            self._time(master['_startAt']) if master['_startAt'] not in ('', 'null') else 0)
                start += max(0, master['_releaseDay'] - 1) * 86400
                available = self._active(master, now) and self._active(group, now) and now >= start
            else:
                start = self._time(master['_startAt']) if master['_startAt'] not in ('', 'null') else created_at
                available = self._active(master, now) and now >= start
            record = owned.get(mission_id)
            if record is None:
                initial = master['_initialValue']
                record = {'start_count': 0 if initial == 0 else total - 1 if initial == 1 else total,
                          'is_completed': False, 'is_received': False, 'start_at': start}
            record['is_completed'] |= available and total - record['start_count'] >= master['_achievementCount']
            connection.execute('''INSERT INTO player_extra_missions VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,category,mission_id) DO UPDATE SET is_completed=excluded.is_completed,
                is_received=excluded.is_received''', (profile_id, category, mission_id, record['start_count'],
                record['is_completed'], record['is_received'], record['start_at']))
            current[mission_id] = record
            counters[self._counter_key(master)] = total
        return current, counters

    @staticmethod
    def _extra_fields(progress, counters, field):
        return (b''.join(message_field(1, _scalars(((1, key[0]), (2, key[1]), (3, key[2]), (4, total))))
                        for key, total in sorted(counters.items()))
                + b''.join(message_field(field, _scalars(((1, mission_id), (2, value['start_count']),
                    (3, value['is_completed']), (4, value['is_received']), (5, value['start_at']))))
                    for mission_id, value in progress.items()))

    def _receive_extra(self, profile_id, mission_ids, category, now):
        masters = self.limited_missions if category == 'limited' else self.event_missions
        if not mission_ids <= masters.keys():
            raise NativeRpcError(3)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            if category == 'limited':
                progress, _ = self._extra_progress(connection, profile_id, state, category, now)
            else:
                progress = {}
                for event_id in {masters[key]['_eventId'] for key in mission_ids}:
                    values, _ = self._extra_progress(connection, profile_id, state, category, now, event_id)
                    progress.update(values)
            resources = []
            for mission_id in sorted(mission_ids):
                record, master = progress[mission_id], masters[mission_id]
                if record['is_received']:
                    continue
                group_active = (self._active(self.limited_groups[master['_limitedMissionGroupId']], now)
                                if category == 'limited' else True)
                if not record['is_completed'] or not self._active(master, now) or not group_active:
                    raise NativeRpcError(9, diagnostic={'code': 'mission_not_completed', 'missionId': mission_id})
                for reward_id in master['_missionRewardIds']:
                    reward = self.reward_masters[reward_id]
                    resources.append((reward['_resourceType'], reward['_resourceId'], reward['_resourceCount']))
                record['is_received'] = True
                connection.execute('UPDATE player_extra_missions SET is_received=1 WHERE profile_id=? AND category=? AND mission_id=?',
                                   (profile_id, category, mission_id))
            if category == 'limited':
                received_groups = state.setdefault('limited_group_reward_ids', [])
                for group_id, group in self.limited_groups.items():
                    ids = [key for key, row in masters.items() if row['_limitedMissionGroupId'] == group_id]
                    if group_id in received_groups or not ids or not all(progress[key]['is_received'] for key in ids):
                        continue
                    for reward_id in group['_completeRewardIds']:
                        reward = self.reward_masters[reward_id]
                        resources.append((reward['_resourceType'], reward['_resourceId'], reward['_resourceCount']))
                    received_groups.append(group_id)
            self.rewards.apply(state, resources, now=now)
            self.progress.save(connection, profile_id, state)
        return message_field(1, b'')

    def player_fields(self, connection, profile_id, *, state=None):
        """Inner PlayerMissionData: counters, normal progress and character claims."""
        if state is None:
            state = self.progress.load(connection, profile_id)
        ratings, normal = self._normal_progress(connection, profile_id, state, int(self.clock()))
        output = [message_field(1, _scalars(((1, kind), (2, character_id),
                                          (3, music_id), (4, progress))))
                  for kind, character_id, music_id, progress in connection.execute('''
                      SELECT mission_type, character_id, music_id, progress
                      FROM player_character_live_counters WHERE profile_id = ?
                      ORDER BY mission_type, character_id, music_id
                  ''', (profile_id,))]
        output.extend(message_field(1, _scalars(((1, 30), (2, band_id), (4, rating))))
                      for band_id, rating in ratings.items())
        output.extend(message_field(2, _scalars(((1, mission_id), (2, value['start_count']),
                      (3, value['is_completed']), (4, value['is_received']), (5, value['start_at']))))
                      for mission_id, value in normal.items())
        limited, counters = self._extra_progress(connection, profile_id, state, 'limited', int(self.clock()))
        counters = {key: value for key, value in counters.items() if key[0] not in (1, 3, 79)}
        output.append(self._extra_fields(limited, counters, 3))
        received = {}
        for character_id, mission_id in connection.execute('''
                SELECT character_id, mission_id FROM player_character_mission_rewards
                WHERE profile_id = ? ORDER BY character_id, mission_id
        ''', (profile_id,)):
            received.setdefault(character_id, []).append(mission_id)
        for character_id, mission_ids in received.items():
            output.append(message_field(4, integer_field(1, character_id)
                + b''.join(integer_field(2, mission_id) for mission_id in mission_ids)))
        return b''.join(output)

    def fetch(self, profile_id, payload):
        try:
            if any(number < 1 for number, _ in fields(payload)):
                raise ValueError('Protobuf field zero')
        except ValueError:
            raise NativeRpcError(3) from None
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            counters = (self.season_pass.mission_counters(connection, profile_id)
                        if self.season_pass is not None else b'')
            result = counters + self.player_fields(connection, profile_id, state=state)
            self.progress.save(connection, profile_id, state)
        return result

    def receive(self, profile_id, mission_ids, now):
        if not mission_ids <= self.music_missions.keys():
            raise NativeRpcError(3)
        if not mission_ids:
            return message_field(1, b'')
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            ratings, progress = self._normal_progress(connection, profile_id, state, now)
            pending = [mission_id for mission_id in sorted(mission_ids)
                       if not progress[mission_id]['is_received']]
            for mission_id in pending:
                master = self.music_missions[mission_id]
                active = self._active(master, now)
                if not active or not progress[mission_id]['is_completed']:
                    raise NativeRpcError(9, diagnostic={
                        'missionId': mission_id, 'type': 30, 'bandId': master['_bandId'],
                        'required': master['_achievementCount'], 'actual': ratings[master['_bandId']],
                        'completed': progress[mission_id]['is_completed'], 'active': active,
                    })
            resources = []
            for mission_id in pending:
                for reward_id in self.music_missions[mission_id]['_missionRewardIds']:
                    reward = self.reward_masters[reward_id]
                    resources.append((reward['_resourceType'], reward['_resourceId'], reward['_resourceCount']))
            self.rewards.apply(state, resources, now=now)
            connection.executemany('''
                UPDATE player_normal_missions SET is_received=1 WHERE profile_id=? AND mission_id=?
            ''', [(profile_id, mission_id) for mission_id in pending])
            self.progress.save(connection, profile_id, state)
        return message_field(1, b'')

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        if method == FETCH_METHOD:
            return self.fetch(player['profile_id'], payload)
        if method == RECEIVE_METHOD:
            return self.receive(player['profile_id'], _mission_ids(payload), int(self.clock()))
        if method in (RECEIVE_LIMITED_METHOD, RECEIVE_EVENT_METHOD):
            return self._receive_extra(player['profile_id'], _mission_ids(payload),
                'limited' if method == RECEIVE_LIMITED_METHOD else 'event', int(self.clock()))
        if method == FETCH_EVENT_METHOD:
            try:
                request = dict(fields(payload))
            except ValueError:
                raise NativeRpcError(3) from None
            event_id = request.get(1, 0)
            if not isinstance(event_id, int) or event_id not in self.event_masters:
                raise NativeRpcError(3)
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                state = self.progress.load(connection, player['profile_id'])
                progress, counters = self._extra_progress(connection, player['profile_id'], state,
                                                          'event', int(self.clock()), event_id)
                self.progress.save(connection, player['profile_id'], state)
                return self._extra_fields(progress, counters, 2) + message_field(3, b'')
        character_id, mission_ids = _request(payload)
        if character_id not in self.characters or not mission_ids <= self.missions.keys():
            raise NativeRpcError(3)
        profile_id, now = player['profile_id'], int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            received = {row[0] for row in connection.execute('''
                SELECT mission_id FROM player_character_mission_rewards
                WHERE profile_id = ? AND character_id = ?
            ''', (profile_id, character_id))}
            pending = sorted(mission_ids - received)
            if not pending:
                return message_field(1, b'')
            state = self.progress.load(connection, profile_id)
            self.rewards.entitlements.initialize_stamps(state)
            for mission_id in pending:
                master = self.missions[mission_id]
                active = self._active(master, now)
                actual = self._total(connection, profile_id, state, character_id, master)
                if not active or actual < master['_achievementCount']:
                    raise NativeRpcError(9, diagnostic={
                        'characterId': character_id, 'missionId': mission_id,
                        'type': master['_missionType'], 'value': master['_value'],
                        'required': master['_achievementCount'], 'actual': actual, 'active': active,
                    })
            resources, character_exp = [], 0
            for mission_id in pending:
                for reward_id in self.missions[mission_id]['_missionRewardIds']:
                    reward = self.reward_masters[reward_id]
                    kind, resource_id, count = (reward['_resourceType'], reward['_resourceId'], reward['_resourceCount'])
                    if kind == 1 and self.items[resource_id]['_type'] == 5:
                        character_exp += count
                    else:
                        resources.append((kind, resource_id, count))
            self.rewards.apply(state, resources, now=now)
            key = str(character_id)
            state['character_exp'][key] = state['character_exp'].get(key, 0) + character_exp
            connection.executemany('''
                INSERT INTO player_character_mission_rewards(profile_id, character_id, mission_id)
                VALUES (?, ?, ?)
            ''', [(profile_id, character_id, mission_id) for mission_id in pending])
            self.progress.save(connection, profile_id, state)
        return message_field(1, b'')

"""Real local challenge-song total rankings and event-top reward delivery.

Local settlement policy approved for this server: the first visit after event
end and before display end freezes that player's current local rank and posts
the matching master rewards once. During the event, VisitTop only returns rank.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.live.event import JST
from .login_bonus import posted_notification
from deathnote.game.live.single import int_value, parse_message
from deathnote.protocol.wire import integer_field, message_field


TOTAL_CHALLENGE_RANKING_METHOD = '/app.event.EventService/GetChallengeMusicTotalRanking'
VISIT_EVENT_TOP_METHOD = '/app.event.EventService/VisitTop'


def _timestamp(value):
    return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=JST).timestamp())


class NativeEventVisit:
    methods = frozenset((TOTAL_CHALLENGE_RANKING_METHOD, VISIT_EVENT_TOP_METHOD))

    def __init__(self, event, master_directory, *, clock=time.time):
        self.event = event
        self.login = event.login
        self.database = event.login.database
        self.progress = event.progress
        self.clock = clock
        directory = Path(master_directory)
        total_rewards = directory / 'MasterChallengeMusicTotalRankingReward.json'
        self.ranking_rewards = (json.loads(total_rewards.read_text('utf8'))['_allData']
                                if total_rewards.is_file() else [])
        self.reward_resources = {row['_id']: row for row in json.loads(
            (directory / 'MasterReward.json').read_text('utf8'))['_allData']}
        self.present_types = {row['_id']: row for row in json.loads(
            (directory / 'MasterPresentType.json').read_text('utf8'))['_allData']}
        self.windows = {key: tuple(_timestamp(row[name]) for name in (
            '_startAt', '_endAt', '_displayEndAt')) for key, row in event.events.items()}

    def _entries(self, connection, event_id):
        master = self.event._event(event_id)
        if master.get('_isTotalMusicRankingDisabled', False):
            return []
        music_ids = [str(row['_id']) for row in self.event.challenge_music.values()
                     if row['_eventId'] == event_id]
        entries = []
        for row in self.event._profiles(connection):
            state = self.progress.load(connection, row[0])
            scores = state.get('challenge_scores', {})
            total = sum(scores[music_id]['score'] for music_id in music_ids if music_id in scores)
            if total > 0:
                entries.append((total, row[0], row, state))
        return sorted(entries, key=lambda value: (-value[0], value[1]))

    @staticmethod
    def _my_rank(entries, profile_id):
        return next(((index, row[0]) for index, row in enumerate(entries, 1)
                     if row[1] == profile_id), (0, 0))

    def _ranking(self, event_id, profile_id):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('BEGIN')
            entries = self._entries(connection, event_id)
            rank, score = self._my_rank(entries, profile_id)
            players = []
            for total, _, row, state in entries:
                profile = self.event.profile_builder(connection, row, state)
                deck = state.get('events', {}).get(str(event_id), {}).get('deck', '')
                players.append(message_field(1, message_field(1, profile)
                    + integer_field(2, total) + message_field(3, bytes.fromhex(deck))))
        return b''.join(players) + integer_field(2, rank) + integer_field(3, score)

    def _visit(self, event_id, player):
        self.event._event(event_id)
        profile_id = player['profile_id']
        now = int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            previous = state.get('event_total_ranking_rewards', {}).get(str(event_id))
            if previous is not None:
                return message_field(1, b'') + integer_field(2, previous['rank'])
            entries = self._entries(connection, event_id)
            rank, score = self._my_rank(entries, profile_id)
            start, end, display_end = self.windows[event_id]
            if now < start or now < end or now >= display_end:
                return message_field(1, b'') + integer_field(2, rank)
            tier = next((row for row in self.ranking_rewards
                         if row['_eventId'] == event_id and row['_rankStart'] <= rank <= row['_rankEnd']), None)
            reward_ids = tier['_rewardIds'] if tier is not None else []
            posted = []
            for reward_id in reward_ids:
                reward = self.reward_resources[reward_id]
                present_type = self.present_types[37]
                present_id = state.get('last_present_id', 0) + 1
                state['last_present_id'] = present_id
                days = present_type['_expireDays']
                present = {'present_id': present_id, 'post_at': now,
                    'expire_at': now + days * 86400 if days else None,
                    'resource_type': reward['_resourceType'], 'resource_id': reward['_resourceId'],
                    'resource_count': reward['_resourceCount'], 'present_type_id': 37,
                    'param_1': '', 'param_2': ''}
                state.setdefault('present_box', {})[str(present_id)] = present
                posted.append(present)
            state.setdefault('event_total_ranking_rewards', {})[str(event_id)] = {
                'rank': rank, 'score': score, 'finalized_at': now,
                'reward_ids': list(reward_ids), 'present_ids': [value['present_id'] for value in posted]}
            response = message_field(1, posted_notification(posted)) + integer_field(2, rank)
            self.progress.save(connection, profile_id, state)
        return response

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        request = parse_message(payload)
        event_id = int_value(request, 1)
        if method == TOTAL_CHALLENGE_RANKING_METHOD:
            return self._ranking(event_id, player['profile_id'])
        return self._visit(event_id, player)

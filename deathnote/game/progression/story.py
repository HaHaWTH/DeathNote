"""Story completion, public unlock rules and once-only first-read rewards."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from .band_rank import NativeBandRank
from deathnote.game.players.progress import NativeLiveProgress
from .rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields


READ_STORY_METHOD = '/app.story.StoryService/ReadStoryEpisode'
READ_FRIENDSHIP_METHOD = '/app.story.StoryService/ReadFriendshipEpisode'
SERVICE_TIMEZONE = timezone(timedelta(hours=9))


def _timestamp(value):
    return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=SERVICE_TIMEZONE).timestamp())


def _active(row, now):
    return ((not row['_startAt'] or _timestamp(row['_startAt']) <= now)
            and (not row['_endAt'] or now <= _timestamp(row['_endAt'])))


def _request(payload):
    episode_id, skipped = 0, False
    try:
        for number, value in fields(payload):
            if number < 1:
                raise ValueError('Protobuf field zero')
            if number == 1:
                if not isinstance(value, int) or not 0 < value <= 0x7fffffffffffffff:
                    raise ValueError('Episode ID must be positive int64')
                episode_id = value
            elif number == 2:
                if not isinstance(value, int):
                    raise ValueError('is_skipped must be a protobuf bool')
                skipped = bool(value)
        if not episode_id:
            raise ValueError('Episode ID missing')
    except ValueError:
        raise NativeRpcError(3) from None
    return episode_id, skipped


class NativeStory:
    methods = frozenset((READ_STORY_METHOD, READ_FRIENDSHIP_METHOD))

    def __init__(self, login, master_directory, *, progress=None, rewards=None, spot=None, clock=time.time):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.spot = spot
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        self.band_rank = NativeBandRank(master_directory)
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.episodes = {row['_id']: row for row in rows('MasterStoryEpisode')}
        self.friendship_episodes = {row['_id']: row for row in rows('MasterStoryFriendshipEpisode')}
        self.chapters = {row['_id']: row for row in rows('MasterStoryChapter')}
        self.events = rows('MasterEvent')
        self.player_ranks = rows('MasterPlayerRank')
        self.character_ranks = rows('MasterCharacterRank')
        self.friendship_ranks = rows('MasterCharacterFriendshipRank')
        self.reward_groups = defaultdict(list)
        for row in rows('MasterStoryReward'):
            self.reward_groups[row['_group']].append(
                (row['_resourceType'], row['_resourceId'], row['_resourceCount']))
        story_groups = {
            row[key] for row in self.episodes.values()
            for key in ('_storyRewardGroupId', '_eventStoryRewardGroupId')
        } | {row['_storyRewardGroupId'] for row in self.friendship_episodes.values()}
        self.story_music_ids = frozenset(
            resource_id for group in story_groups
            for resource_type, resource_id, _ in self.reward_groups[group]
            if resource_type == 8)

    def unlock_all(self, profile_id):
        """Mark current episodes read and unlock their music, without first-read awards."""
        if type(profile_id) is not int or not 0 < profile_id <= 0x7fffffffffffffff:
            raise NativeRpcError(3)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM players WHERE profile_id = ?',
                                  (profile_id,)).fetchone() is None:
                raise NativeRpcError(3)
            state = self.progress.load(connection, profile_id)
            now = int(self.clock())
            added = []
            for key, masters in (('story_records', self.episodes),
                                 ('friendship_story_records', self.friendship_episodes)):
                seen = state.setdefault(key, {})
                missing = [episode_id for episode_id in masters if str(episode_id) not in seen]
                for episode_id in sorted(missing):
                    seen[str(episode_id)] = {'is_skipped': False, 'last_read_at': now}
                added.append(len(missing))
            music = state.setdefault('music_unlocked', {})
            missing_music = [music_id for music_id in self.story_music_ids if str(music_id) not in music]
            for music_id in sorted(missing_music):
                music[str(music_id)] = {'music_id': music_id, 'got_at': now}
            gained_spots = self.spot.unlock(state, now=now) if self.spot is not None else []
            if any(added) or missing_music or gained_spots:
                self.progress.save(connection, profile_id, state)
            return {
                'profileId': profile_id,
                'storyCount': len(self.episodes),
                'friendshipCount': len(self.friendship_episodes),
                'unlockedMusicCount': len(music),
                'addedStories': added[0],
                'addedFriendshipStories': added[1],
                'addedMusic': len(missing_music),
            }

    @staticmethod
    def _rank(rows, exp):
        return max((row['_rank'] for row in rows if row['_exp'] <= exp), default=0)

    def _holding_event(self, chapter_id, now):
        return next((row for row in self.events if row['_storyChapterId'] == chapter_id
                     and _active(row, now)), None)

    def _story_access(self, master, state, now):
        chapter = self.chapters[master['_chapterId']]
        seen = state.get('story_records', {})
        if str(master['_id']) in seen or chapter['_isSpecialStory']:
            return True
        if not _active(master, now):
            raise NativeRpcError(9)
        event = self._holding_event(master['_chapterId'], now)
        if event is not None:
            point = state.get('events', {}).get(str(event['_id']), {}).get('point', 0)
            if point < master['_eventPoint']:
                raise NativeRpcError(9)
        else:
            if not _active(chapter, now):
                raise NativeRpcError(9)
            if self._rank(self.player_ranks, state['rank_exp']) < master['_playerRank']:
                raise NativeRpcError(9)
            if self._rank(self.character_ranks, state['character_exp'].get(
                    str(master['_characterId']), 0)) < master['_characterRank']:
                raise NativeRpcError(9)
            if master['_bandRank'] and self.band_rank.rank(state, chapter['_bandId']) < master['_bandRank']:
                return False
            friendship = master['_storyFriendshipEpisodeId']
            if friendship and str(friendship) not in state.get('friendship_story_records', {}):
                raise NativeRpcError(9)
        prerequisite = master['_unlockEpisodeNumber']
        if prerequisite:
            previous = next((row for row in self.episodes.values()
                             if row['_chapterId'] == master['_chapterId']
                             and row['_episodeNumber'] == prerequisite), None)
            if previous is not None and str(previous['_id']) not in seen:
                raise NativeRpcError(9)
        if not master['_isAnotherEpisode']:
            previous = [row for row in self.episodes.values()
                        if row['_chapterId'] == master['_chapterId']
                        and not row['_isAnotherEpisode']
                        and row['_episodeNumber'] < master['_episodeNumber']]
            if previous and str(max(previous, key=lambda row: row['_episodeNumber'])['_id']) not in seen:
                raise NativeRpcError(9)
        return True

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        episode_id, skipped = _request(payload)
        friendship = method == READ_FRIENDSHIP_METHOD
        masters = self.friendship_episodes if friendship else self.episodes
        if episode_id not in masters:
            raise NativeRpcError(3)
        master, now = masters[episode_id], int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            source = 'friendship_story_records' if friendship else 'story_records'
            seen = state.setdefault(source, {})
            if friendship:
                exp = state['friendship_exp'].get(str(master['_characterFriendshipId']), 0)
                if str(episode_id) not in seen and self._rank(self.friendship_ranks, exp) < master['_unlockCharacterFriendshipLevel']:
                    raise NativeRpcError(9)
            elif not self._story_access(master, state, now):
                return message_field(2, b'')
            if str(episode_id) not in seen:
                group = master['_storyRewardGroupId']
                if not friendship and self._holding_event(master['_chapterId'], now) is not None:
                    group = master['_eventStoryRewardGroupId'] or group
                self.rewards.apply(state, self.reward_groups[group], now=now)
            seen[str(episode_id)] = {'is_skipped': skipped, 'last_read_at': now}
            if self.spot is not None:
                self.spot.unlock(state, now=now)
            self.progress.save(connection, player['profile_id'], state)
        return message_field(1, b'') if friendship else integer_field(1, 1) + message_field(2, b'')

"""Original single-player LIVE RPCs backed by local transactional progress."""
from __future__ import annotations

from contextlib import closing
from functools import cached_property
import json
from pathlib import Path
import random
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress
from .rating import NativeLiveRating
from .rules import NativeLiveRules
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.progression.rewards import NativeRewards
from deathnote.game.players.inventory import member_cards
from .music import record_score
from .event import NativeLiveEvent
from deathnote.game.progression.login_bonus import posted_notification
from deathnote.protocol.wire import integer_field, message_field, varint
from deathnote.protocol.decode import fields


CHECK_FREE_METHOD = '/app.live.LiveService/CheckMaintenanceFree'
START_FREE_METHOD = '/app.live.LiveService/StartFree'
FINISH_FREE_METHOD = '/app.live.LiveService/FinishFree'
SKIP_FREE_METHOD = '/app.live.LiveService/SkipFree'
GAIN_STAMP_METHOD = '/app.live.LiveService/GainStampReward'
GAIN_FRIENDSHIP_METHOD = '/app.character.CharacterService/GainFriendshipRankReward'


def parse_message(payload: bytes) -> dict:
    try:
        return dict(fields(payload))
    except ValueError:
        raise NativeRpcError(3) from None


def int_value(values: dict, number: int) -> int:
    value = values.get(number, 0)
    if not isinstance(value, int):
        raise NativeRpcError(3)
    return value


def reward_resource(resource_type: int, resource_id: int, count: int) -> bytes:
    return integer_field(1, resource_type) + integer_field(2, resource_id) + integer_field(3, count)


class NativeSingleLive:
    methods = frozenset((CHECK_FREE_METHOD, START_FREE_METHOD, FINISH_FREE_METHOD, SKIP_FREE_METHOD,
                         GAIN_STAMP_METHOD, GAIN_FRIENDSHIP_METHOD))

    def __init__(self, login, player_data, master_directory: Path, *, clock=time.time, rng=None,
                 season_pass=None, mission=None):
        self.login = login
        self.player_data = player_data
        self.progress: NativeLiveProgress = player_data.progress
        self.master_directory = Path(master_directory)
        self.rewards = NativeRewards(self.master_directory)
        self.clock = clock
        self.season_pass = season_pass
        self.mission = mission
        self.offline_bonus = None
        self.rng = random.SystemRandom() if rng is None else rng

    @cached_property
    def rules(self) -> NativeLiveRules:
        return NativeLiveRules(self.master_directory)

    @cached_property
    def rating(self) -> NativeLiveRating:
        return NativeLiveRating(self.master_directory)

    @cached_property
    def item_masters(self) -> dict:
        return self.rewards.item_masters

    @cached_property
    def skip_parameters(self) -> dict:
        rows = json.loads((self.master_directory / 'MasterParameter.json').read_text('utf8'))['_allData']
        values = {row['_id']: row['_value'] for row in rows}
        return {'max_count': int(values['live_skip_max_usage_count']),
                'score_rank': ('None', 'E', 'D', 'C', 'B', 'A', 'S', 'SS').index(
                    values['live_skip_result_score_rank'])}

    @cached_property
    def skip_events(self):
        return NativeLiveEvent(self.master_directory)

    @cached_property
    def friendship_masters(self) -> dict:
        data = json.loads((self.master_directory / 'MasterCharacterFriendship.json').read_text('utf8'))
        return {tuple(sorted((row['_masterCharacterIdA'], row['_masterCharacterIdB']))): row['_id']
                for row in data['_allData']}

    @cached_property
    def stamp_masters(self) -> dict:
        data = json.loads((self.master_directory / 'MasterLiveStamp.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def friendship_reward_masters(self) -> dict:
        data = json.loads((self.master_directory / 'MasterCharacterFriendshipRankReward.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def friendship_ranks(self) -> list[dict]:
        return json.loads((self.master_directory / 'MasterCharacterFriendshipRank.json').read_text('utf8'))['_allData']

    @staticmethod
    def deck_members(deck: bytes) -> dict[int, int]:
        members = {}
        for number, value in fields(deck):
            if number == 3:
                card = parse_message(value)
                members[int_value(card, 1)] = int_value(card, 3)
        return members

    def dispatch(self, method: str, payload: bytes, headers: dict[str, str]) -> bytes:
        player = self.login.authenticate(headers)
        if method == CHECK_FREE_METHOD:
            return b''
        if method == START_FREE_METHOD:
            return self.start(player, payload)
        if method == FINISH_FREE_METHOD:
            return self.finish(player, payload)
        if method == SKIP_FREE_METHOD:
            return self.skip(player, payload)
        if method == GAIN_STAMP_METHOD:
            return self.gain_stamp(player, payload)
        if method == GAIN_FRIENDSHIP_METHOD:
            return self.gain_friendship(player, payload)
        raise NotImplementedError(method)

    def start(self, player: dict, payload: bytes) -> bytes:
        request = parse_message(payload)
        music_id, difficulty, boost, deck_id = (int_value(request, number) for number in range(1, 5))
        def reject(status, code):
            return NativeRpcError(status, diagnostic={
                'code': code, 'musicId': music_id, 'difficulty': difficulty,
                'boost': boost, 'deckId': deck_id,
            })
        try:
            self.rules.select_music(music_id, difficulty)
        except ValueError:
            raise reject(3, 'live_music_selection') from None
        if boost not in range(11):
            raise reject(3, 'live_boost_range')
        decks, _ = self.player_data.decks.load(player['profile_id'])
        matching = [deck for deck in decks if int_value(parse_message(deck), 1) == deck_id]
        if not matching:
            raise reject(9, 'live_deck_missing')
        deck = matching[0]
        members = self.deck_members(deck)
        if set(members) != set(range(5)) or len(set(members.values())) != 5:
            raise reject(9, 'live_deck_incomplete')
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            owned = member_cards(state, player['created_at'])
            if any(str(card_id) not in owned for card_id in members.values()):
                raise reject(9, 'live_deck_incomplete')
            current = state['live_boost']
            try:
                self.rules.snapshot_boost(current['amount'], current['previous_recovery_at'], now, boost)
            except ValueError:
                raise reject(9, 'live_boost_balance') from None
            connection.execute('''
                INSERT INTO free_live_active(profile_id, music_id, difficulty, boost, deck_id, deck_wire, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET music_id = excluded.music_id,
                    difficulty = excluded.difficulty, boost = excluded.boost,
                    deck_id = excluded.deck_id, deck_wire = excluded.deck_wire,
                    started_at = excluded.started_at
            ''', (player['profile_id'], music_id, difficulty, boost, deck_id, deck, now))
        return b''

    def add_items(self, state: dict, resources) -> None:
        self.rewards.apply(state, resources)

    def _apply_exp(self, state, deck, player, base_exp, rates):
        previous_rank = self.rules.player_rank(state['rank_exp'])
        state['rank_exp'] += base_exp['_playerExp'] * rates['_playerExpRate']
        rank_increase = self.rules.player_rank(state['rank_exp']) - previous_rank
        state['live_boost']['amount'] += rank_increase * self.rules.rank_up_boost
        members = self.deck_members(deck)
        owned = member_cards(state, player['created_at'])
        card_exp = base_exp['_memberCardExp'] * rates['_memberCardExpRate']
        for member_id in members.values():
            key = str(member_id)
            card = owned[key]
            previous = state['member_exp'].get(key, 0)
            cap = self.rules.card_exp_cap(card['master_id'], card['awake_count'], card['card_rank'])
            gained = min(card_exp, max(0, cap - previous))
            state['member_exp'][key] = previous + gained
            overflow = card_exp - gained
            if overflow:
                self.add_items(state, ((1, 5, overflow),))
        center = self.rules.member_cards[owned[str(members[2])]['master_id']]['_characterID']
        friendship_exp = base_exp['_friendshipExp'] * rates['_friendshipExpRate']
        character_ids = []
        for slot, member_id in members.items():
            character_id = self.rules.member_cards[owned[str(member_id)]['master_id']]['_characterID']
            character_ids.append(character_id)
            state['character_exp'][str(character_id)] = (
                state['character_exp'].get(str(character_id), 0) + base_exp['_characterRankExp'])
            if slot == 2:
                continue
            friendship_id = self.friendship_masters.get(tuple(sorted((center, character_id))))
            if friendship_id is not None:
                key = str(friendship_id)
                state['friendship_exp'][key] = state['friendship_exp'].get(key, 0) + friendship_exp
        return character_ids

    def skip(self, player, payload):
        request = parse_message(payload)
        music_id, difficulty, boost, deck_id = (int_value(request, number) for number in range(1, 5))
        int_value(request, 5)
        try:
            self.rules.select_music(music_id, difficulty)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'skip_music_selection'}) from None
        if boost not in range(11):
            raise NativeRpcError(3, diagnostic={'code': 'skip_boost_range'})
        decks, _ = self.player_data.decks.load(player['profile_id'])
        matching = [deck for deck in decks if int_value(parse_message(deck), 1) == deck_id]
        if not matching:
            raise NativeRpcError(9, diagnostic={'code': 'skip_deck_missing'})
        deck = matching[0]
        members = self.deck_members(deck)
        if set(members) != set(range(5)) or len(set(members.values())) != 5:
            raise NativeRpcError(9, diagnostic={'code': 'skip_deck_incomplete'})
        profile_id, now = player['profile_id'], int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            owned = member_cards(state, player['created_at'])
            if any(str(card_id) not in owned for card_id in members.values()):
                raise NativeRpcError(9, diagnostic={'code': 'skip_deck_incomplete'})
            before_rates = self.offline_bonus.capture_rates(state, now=now) if self.offline_bonus is not None else None
            entitlements = self.rewards.entitlements
            entitlements._settle(state, now)
            bonus = max((entitlements.monthly_passes[value['monthly_pass_id']]['_addLiveSkip']
                         for value in state.get('monthly_passes', {}).values()
                         if value['expire_at'] > now), default=0)
            skip = state.get('live_skip', {'count': 0, 'last_skip': 0})
            offset = 9 * 3600 - self.rules.daily_reset_seconds
            count = skip['count'] if (skip['last_skip'] + offset) // 86400 == (now + offset) // 86400 else 0
            if count >= self.skip_parameters['max_count'] + bonus:
                raise NativeRpcError(9, diagnostic={'code': 'skip_daily_limit'})
            boost_state = state['live_boost']
            try:
                consumed = self.rules.snapshot_boost(boost_state['amount'], boost_state['previous_recovery_at'], now, boost)
            except ValueError:
                raise NativeRpcError(9, diagnostic={'code': 'skip_boost_balance'}) from None
            boost_state.update(amount=consumed['amount'], previous_recovery_at=consumed['previous_recovery_at'])
            rank = self.skip_parameters['score_rank']
            rates = self.rules.boost_bonus(boost)
            ordinary = self.rules.draw_free_rewards(rank, self.rng)
            drops = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'] * rates['_liveMusicRewardRate'])
                     for row in ordinary]
            self.add_items(state, drops)
            event_wire, event_resources, posted = self.skip_events.settle(state, deck, rank=rank, rates=rates,
                player_rank=self.rules.player_rank(state['rank_exp']), now=now, rng=self.rng)
            self.add_items(state, event_resources)
            self.skip_events.gain_challenge_points(state, deck, rank=rank, rates=rates, now=now)
            self._apply_exp(state, deck, player, self.rules.exp_base(rank), rates)
            state['live_skip'] = {'count': count + 1, 'last_skip': now}
            if self.season_pass is not None:
                self.season_pass.record_live(connection, profile_id, music_id=music_id,
                    cleared=False, consumed_boost=boost, now=now)
            notification = posted_notification(posted)
            if self.offline_bonus is not None:
                notification += self.offline_bonus.stock_changed_rates(connection, player, state, before_rates, now=now)
            self.progress.save(connection, profile_id, state)
            response = (event_wire + message_field(1, b''.join(varint(row['_id']) for row in ordinary))
                        + message_field(3, notification))
        return response

    def gain_stamp(self, player: dict, payload: bytes) -> bytes:
        reward_id = int_value(parse_message(payload), 1)
        if reward_id not in self.stamp_masters:
            raise NativeRpcError(3)
        reward = self.stamp_masters[reward_id]
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            stamp = state['live_stamp']
            offset = 9 * 3600 - self.rules.daily_reset_seconds
            current_week = (((int(self.clock()) + offset) // 86400) + 3) // 7
            completed_week = (((stamp['last_live_completed_at'] + offset) // 86400) + 3) // 7
            if current_week != completed_week:
                raise NativeRpcError(9)
            day = reward['_dayNo']
            if day <= stamp['reward_gained']:
                return message_field(1, b'')
            if day != stamp['reward_gained'] + 1 or day > stamp['live_completed']:
                raise NativeRpcError(9)
            self.add_items(state, ((reward['_resourceType'], reward['_resourceId'], reward['_resourceCount']),))
            stamp['reward_gained'] = day
            self.progress.save(connection, profile_id, state)
        return message_field(1, b'')

    def gain_friendship(self, player: dict, payload: bytes) -> bytes:
        rewards = set()
        try:
            for number, value in fields(payload):
                if number == 1:
                    if not isinstance(value, bytes):
                        raise NativeRpcError(3)
                    request = parse_message(value)
                    rewards.add((int_value(request, 1), int_value(request, 2)))
        except ValueError:
            raise NativeRpcError(3) from None
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            for friendship_id, reward_id in sorted(rewards):
                if friendship_id not in self.friendship_masters.values() or reward_id not in self.friendship_reward_masters:
                    raise NativeRpcError(3)
                key = str(friendship_id)
                claimed = state['friendship_reward_ids'].setdefault(key, [])
                if reward_id in claimed:
                    continue
                reward = self.friendship_reward_masters[reward_id]
                exp = state['friendship_exp'].get(key, 0)
                rank = max((row for row in self.friendship_ranks if exp >= row['_exp']),
                           key=lambda row: row['_exp'])['_rank']
                if reward['_rank'] > rank or reward['_characterFriendshipId'] not in (0, friendship_id):
                    raise NativeRpcError(9)
                self.add_items(state, ((reward['_resourceType'], reward['_resourceId'], reward['_resourceCount']),))
                claimed.append(reward_id)
            self.progress.save(connection, profile_id, state)
        return message_field(1, b'')

    def finish(self, player: dict, payload: bytes) -> bytes:
        outer = parse_message(payload)
        live_finish = outer.get(4)
        if not isinstance(live_finish, bytes):
            raise NativeRpcError(3)
        request = parse_message(live_finish)
        clear_status, score, combo = (int_value(request, number) for number in range(1, 4))
        unique_bytes = request.get(11)
        if not isinstance(unique_bytes, bytes):
            raise NativeRpcError(3)
        try:
            unique_id = unique_bytes.decode('utf8')
        except UnicodeDecodeError:
            raise NativeRpcError(3) from None
        if not unique_id or clear_status not in range(1, 6) or score not in range(0x80000000) or combo < 0:
            raise NativeRpcError(3)
        now = int(self.clock())
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            cached = connection.execute('''
                SELECT response FROM free_live_results WHERE profile_id = ? AND live_unique_id = ?
            ''', (profile_id, unique_id)).fetchone()
            if cached is not None:
                return cached[0]
            active = connection.execute('''
                SELECT music_id, difficulty, boost, deck_wire FROM free_live_active WHERE profile_id = ?
            ''', (profile_id,)).fetchone()
            if active is None:
                raise NativeRpcError(9)
            music_id, difficulty, boost, deck = active
            selected = self.rules.select_music(music_id, difficulty)
            note_count = selected['score']['_fullComboCount']
            if combo > note_count:
                raise NativeRpcError(3)
            state = self.progress.load(connection, profile_id)
            before_rates = self.offline_bonus.capture_rates(state, now=now) if self.offline_bonus is not None else None
            record_score(state, music_id=music_id, difficulty=difficulty, mode=1, score=score,
                         deck=deck, created_at=player['created_at'])
            boost_state = state['live_boost']
            try:
                consumed = self.rules.snapshot_boost(boost_state['amount'], boost_state['previous_recovery_at'], now, boost)
            except ValueError:
                raise NativeRpcError(9) from None
            boost_state.update(amount=consumed['amount'],
                               previous_recovery_at=consumed['previous_recovery_at'])
            rank = self.rules.score_rank(music_id, score)
            rates = self.rules.boost_bonus(boost)
            base_exp = self.rules.exp_base(rank)
            ordinary = self.rules.draw_free_rewards(rank, self.rng)
            drops = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'] * rates['_liveMusicRewardRate'])
                     for row in ordinary]
            music_key, combo_key = str(music_id), f'{music_id}:{difficulty}'
            score_claims = state['score_reward_ids'].setdefault(music_key, [])
            combo_claims = state['combo_reward_ids'].setdefault(combo_key, [])
            new_score = self.rules.score_rewards(music_id, rank, score_claims)
            new_combo = self.rules.combo_rewards(music_id, difficulty, combo, combo_claims, full_combo=clear_status >= 4)
            milestones = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'])
                          for row in new_score + new_combo]
            self.add_items(state, drops + milestones)
            event_wire, event_resources, posted = self.skip_events.settle(state, deck, rank=rank, rates=rates,
                player_rank=self.rules.player_rank(state['rank_exp']), now=now, rng=self.rng)
            self.add_items(state, event_resources)
            self.skip_events.gain_challenge_points(state, deck, rank=rank, rates=rates, now=now)
            score_claims.extend(row['_id'] for row in new_score)
            combo_claims.extend(row['_id'] for row in new_combo)
            if new_score:
                state['score_reward_gained'][music_key] = max(
                    state['score_reward_gained'].get(music_key, 0),
                    max(row['_liveScoreRank'] for row in new_score))
            if new_combo:
                thresholds = (note_count // 4, note_count // 2, note_count * 3 // 4, note_count)
                state['combo_reward_gained'][combo_key] = max(
                    state['combo_reward_gained'].get(combo_key, 0),
                    max(thresholds[row['_comboRateType']] for row in new_combo))
            character_ids = self._apply_exp(state, deck, player, base_exp, rates)
            score_key = f'{music_id}:{difficulty}:1'
            previous_scores = list(state['scores'].values())
            previous_score = state['scores'].get(score_key)
            state['scores'][score_key] = {
                'music_id': music_id, 'difficulty': difficulty, 'mode': 1,
                'high_score': max(score, previous_score['high_score'] if previous_score else 0),
                'clear_status': max(clear_status, previous_score['clear_status'] if previous_score else 0),
                'max_combo': max(combo, previous_score['max_combo'] if previous_score else 0),
            }
            rating_rewards = self.rating.rewards(previous_scores, list(state['scores'].values()))
            self.add_items(state, rating_rewards['resources'])
            stamp = state['live_stamp']
            state['live_stamp'] = self.rules.update_live_stamp(
                stamp['live_completed'], stamp['reward_gained'], stamp['last_live_completed_at'], now)
            if self.season_pass is not None:
                self.season_pass.record_live(connection, profile_id, music_id=music_id,
                                             cleared=clear_status >= 2,
                                             consumed_boost=boost, now=now)
            if self.mission is not None:
                self.mission.record_live(connection, profile_id, music_id=music_id,
                    character_ids=character_ids,
                    cleared=clear_status >= 2, difficulty=difficulty, score_rank=rank, mode=1, now=now)
            rating_payload = b''
            if rating_rewards['total_reward_ids']:
                rating_payload += message_field(3, b''.join(varint(value) for value in rating_rewards['total_reward_ids']))
            for band_id, reward_ids in rating_rewards['band_rewards'].items():
                rating_payload += message_field(4, integer_field(1, band_id) +
                    message_field(2, b''.join(varint(value) for value in reward_ids)))
            notification = posted_notification(posted)
            if self.offline_bonus is not None:
                notification += self.offline_bonus.stock_changed_rates(connection, player, state, before_rates, now=now)
            response = (rating_payload + b''.join(message_field(5, reward_resource(*drop))
                        for drop in drops + rating_rewards['resources'])
                        + b''.join(message_field(7 if number == 4 else number, value) for number, value in fields(event_wire))
                        + message_field(6, notification))
            self.progress.save(connection, profile_id, state)
            connection.execute('''
                INSERT INTO free_live_results(profile_id, live_unique_id, music_id, difficulty,
                    score, clear_status, max_combo, completed_at, response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (profile_id, unique_id, music_id, difficulty, score, clear_status, combo, now, response))
            connection.execute('DELETE FROM free_live_active WHERE profile_id = ?', (profile_id,))
        return response

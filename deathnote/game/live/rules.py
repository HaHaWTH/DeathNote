"""Single-player LIVE rules from the current public masters and native consumers.

This module performs calculations only. The controller owns authentication,
transactions, run identity, inventory mutation, and reward claim persistence.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Iterable, Protocol


class RewardRandom(Protocol):
    def randrange(self, stop: int) -> int: ...


class NativeLiveRules:
    """Use one immutable public master snapshot for a controller's lifetime."""

    def __init__(self, master_dir: Path):
        self.master_dir = Path(master_dir)

        def rows(name: str) -> list[dict]:
            with (self.master_dir / f'{name}.json').open(encoding='utf-8') as source:
                return json.load(source)['_allData']

        self.music = {row['_id']: row for row in rows('MasterLiveMusic')}
        self.scores = {row['_id']: row for row in rows('MasterLiveMusicScore')}
        self.rank_groups = self._groups(rows('MasterLiveScoreRank'), '_group')
        self.score_reward_groups = self._groups(rows('MasterLiveMusicScoreReward'), '_group')
        self.combo_reward_groups = self._groups(rows('MasterLiveMusicComboReward'), '_group')
        self.boost_bonuses = {row['_consumedLiveBoostCount']: row
                              for row in rows('MasterLiveMusicBoostBonus')}
        self.exp_rewards = {row['_liveScoreRank']: row for row in rows('MasterLiveMusicExpReward')}
        self.free_rewards = self._groups(rows('MasterLiveFreeReward'), '_liveScoreRank')
        self.player_ranks = sorted(rows('MasterPlayerRank'), key=lambda row: row['_exp'])
        self.member_cards = {row['_id']: row for row in rows('MasterMemberCard')}
        self.card_levels = self._groups(rows('MasterMemberCardLevel'), '_group')
        self.card_limits = {(row['_rarity'], row['_awakeCount']): row
                            for row in rows('MasterMemberCardLevelLimit')}
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.boost_recovery_seconds = int(parameters['live_boost_recovery_time'])
        self.rank_up_boost = int(parameters['live_boost_by_rank_up'])
        reset_hour, reset_minute = (int(part) for part in parameters['daily_reset_time'].split(':'))
        self.daily_reset_seconds = reset_hour * 3600 + reset_minute * 60

    @staticmethod
    def _groups(rows: list[dict], field: str) -> dict[int, list[dict]]:
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[field]].append(row)
        return dict(grouped)

    def select_music(self, music_id: int, difficulty: int) -> dict:
        """RPC difficulty is Easy 1 through Expert 4; master indices are 0..3."""
        fields = ('_easyID', '_normalID', '_hardID', '_expertID')
        if difficulty not in range(1, 5):
            raise ValueError('LIVE difficulty must be 1, 2, 3, or 4')
        try:
            music = self.music[music_id]
            score = self.scores[music[fields[difficulty - 1]]]
        except KeyError as error:
            raise ValueError('This song and difficulty have no master score') from error
        return {'music': music, 'score': score}

    def score_rank(self, music_id: int, score: int) -> int:
        """Highest required-score threshold; failed-clear status is independent."""
        music = self.music[music_id]
        eligible = [row for row in self.rank_groups[music['_liveScoreRankGroup']]
                    if score >= row['_requiredScore']]
        if not eligible:
            raise ValueError('Score is below this song\'s first score threshold')
        return max(eligible, key=lambda row: row['_requiredScore'])['_liveScoreRank']

    def recover_boost(self, amount: int, previous_recovery_at: int, now: int, *,
                      capacity_bonus: int = 0, recovery_speed_bonus: int = 0) -> dict:
        """Settle elapsed recovery, preserving accumulated partial interval time."""
        capacity = 10 + capacity_bonus
        interval = max(1, self.boost_recovery_seconds * (10000 - recovery_speed_bonus) // 10000)
        if amount >= capacity:
            return {'amount': amount, 'previous_recovery_at': now}
        elapsed = max(0, now - previous_recovery_at)
        recovered = elapsed // interval
        if amount + recovered >= capacity:
            return {'amount': capacity, 'previous_recovery_at': now}
        return {'amount': amount + recovered,
                'previous_recovery_at': previous_recovery_at + recovered * interval}

    def snapshot_boost(self, amount: int, previous_recovery_at: int, now: int,
                       boost: int, *, capacity_bonus: int = 0,
                       recovery_speed_bonus: int = 0) -> dict:
        """Calculate a requested consumption without committing player state."""
        if boost not in range(11):
            raise ValueError('LIVE boost consumption must be between 0 and 10')
        current = self.recover_boost(amount, previous_recovery_at, now,
                                     capacity_bonus=capacity_bonus,
                                     recovery_speed_bonus=recovery_speed_bonus)
        if boost > current['amount']:
            raise ValueError('Insufficient LIVE boost for this consumption')
        return {'amount': current['amount'] - boost,
                'previous_recovery_at': current['previous_recovery_at'],
                'consumed_boost': boost}

    def boost_bonus(self, boost: int) -> dict:
        """Five native reward multipliers; zero consumption uses 1 for each."""
        if boost == 0:
            return {'_consumedLiveBoostCount': 0, '_playerExpRate': 1,
                    '_memberCardExpRate': 1, '_friendshipExpRate': 1,
                    '_eventPointRate': 1, '_liveMusicRewardRate': 1}
        return self.boost_bonuses[boost]

    def exp_base(self, score_rank: int) -> dict:
        return self.exp_rewards[score_rank]

    def player_rank(self, exp: int) -> int:
        return max((row for row in self.player_ranks if exp >= row['_exp']),
                   key=lambda row: row['_exp'])['_rank']

    def update_live_stamp(self, live_completed: int, reward_gained: int,
                          last_live_completed_at: int, now: int) -> dict:
        """Native service calendar: JST, daily master reset, Monday week start."""
        offset = 9 * 3600 - self.daily_reset_seconds
        previous_day = (last_live_completed_at + offset) // 86400
        current_day = (now + offset) // 86400
        if (current_day + 3) // 7 > (previous_day + 3) // 7:
            return {'live_completed': 1, 'reward_gained': 0, 'last_live_completed_at': now}
        if current_day > previous_day:
            return {'live_completed': live_completed + 1, 'reward_gained': reward_gained,
                    'last_live_completed_at': now}
        return {'live_completed': live_completed, 'reward_gained': reward_gained,
                'last_live_completed_at': last_live_completed_at}

    def card_exp_cap(self, master_card_id: int, awake: int = 1, rank: int = 1) -> int:
        """Native cap uses rarity/awake and the card's level group; rank is separate."""
        card = self.member_cards[master_card_id]
        limit = self.card_limits[(card['_rarity'], awake)]['_limitLevel']
        levels = (row for row in self.card_levels[card['_memberCardLevelGroup']]
                  if row['_level'] <= limit)
        return max(levels, key=lambda row: row['_level'])['_exp']

    def score_rewards(self, music_id: int, score_rank: int,
                      already_ids: Iterable[int] = ()) -> list[dict]:
        claimed = set(already_ids)
        group = self.music[music_id]['_scoreRankRewardGroup']
        return [row for row in self.score_reward_groups[group]
                if row['_liveScoreRank'] <= score_rank and row['_id'] not in claimed]

    def combo_rewards(self, music_id: int, difficulty: int, combo: int,
                      already_ids: Iterable[int] = (), *, full_combo: bool = False) -> list[dict]:
        selected = self.select_music(music_id, difficulty)
        note_count = selected['score']['_fullComboCount']
        achieved = {0: combo >= note_count // 4,
                    1: combo >= note_count // 2,
                    2: combo >= note_count * 3 // 4,
                    3: full_combo}
        claimed = set(already_ids)
        return [row for row in self.combo_reward_groups[selected['music']['_comboRewardGroup']]
                if row['_difficulty'] == difficulty - 1 and achieved[row['_comboRateType']]
                and row['_id'] not in claimed]

    def draw_free_rewards(self, score_rank: int, rng: RewardRandom) -> list[dict]:
        """One weighted draw per public (rank, group), ordered by group number."""
        groups = self._groups(self.free_rewards[score_rank], '_group')
        selected = []
        for group_id in sorted(groups):
            candidates = groups[group_id]
            value = rng.randrange(10000)
            cumulative = 0
            for row in candidates:
                cumulative += row['_probability']
                if value < cumulative:
                    selected.append(row)
                    break
        return selected

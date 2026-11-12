"""High-score rating and crossed-tier rewards from the original LIVE consumers.

The controller owns score persistence, transactions and reward application.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import struct


def _float32(value: int | float) -> float:
    return struct.unpack('<f', struct.pack('<f', value))[0]


class NativeLiveRating:
    def __init__(self, master_directory: Path):
        master_directory = Path(master_directory)

        def rows(name: str) -> list[dict]:
            with (master_directory / f'{name}.json').open(encoding='utf8') as source:
                return json.load(source)['_allData']

        self.music = {row['_id']: row for row in rows('MasterLiveMusic')}
        self.master_rewards = {row['_id']: row for row in rows('MasterReward')}
        self.total_tiers = sorted(rows('MasterLiveTotalHighScoreRating'), key=lambda row: row['_rating'])
        self.band_tiers = sorted(rows('MasterLiveBandHighScoreRating'), key=lambda row: row['_rating'])
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.scale = _float32(int(parameters['music_rating_scale_factor']))
        self.minimum = int(parameters['music_min_high_score_rating'])

    def _ratings(self, scores: list[dict]) -> tuple[int, dict[int, int]]:
        top = {}
        for score in scores:
            if score['music_id'] not in self.music:
                continue
            high_score = score['high_score']
            rating = math.floor(_float32(_float32(high_score) / self.scale))
            if high_score >= 1:
                rating = max(self.minimum, rating)
            if rating < 1:
                continue
            music = self.music[score['music_id']]
            for band_id in music['_bandIDs']:
                key = (music['_musicType'], band_id)
                top[key] = max(top.get(key, 0), rating)
        bands = defaultdict(int)
        for (music_type, band_id), rating in top.items():
            if music_type != 99:
                bands[band_id] += rating
        return sum(top.values()), dict(bands)

    @staticmethod
    def _crossed(tiers: list[dict], before: int, after: int) -> list[dict]:
        return [tier for tier in tiers if before < tier['_rating'] <= after]

    def rewards(self, before_scores: list[dict], after_scores: list[dict]) -> dict:
        before_total, before_bands = self._ratings(before_scores)
        after_total, after_bands = self._ratings(after_scores)
        total_tiers = self._crossed(self.total_tiers, before_total, after_total)
        total_reward_ids = [tier['_id'] for tier in total_tiers]
        band_rewards = {}
        crossed_tiers = list(total_tiers)
        for band_id in sorted(after_bands):
            tiers = self._crossed(self.band_tiers, before_bands.get(band_id, 0), after_bands[band_id])
            if tiers:
                band_rewards[band_id] = [tier['_id'] for tier in tiers]
                crossed_tiers.extend(tiers)
        all_reward_ids = [reward_id for tier in crossed_tiers for reward_id in tier['_liveMusicRewardIds']]
        resources = []
        for reward_id in all_reward_ids:
            row = self.master_rewards[reward_id]
            resources.append((row['_resourceType'], row['_resourceId'], row['_resourceCount']))
        return {'total_reward_ids': total_reward_ids, 'band_rewards': band_rewards, 'resources': resources}

"""Band evaluation for the native server's persisted owned-card inventory.

Owned cards, EXP, support cards and current band-item levels are read here.
Original formulas: analysis/band-rank-1.0.6.consumer.md and band-item-consumer-106.
"""
from __future__ import annotations

from collections import defaultdict, deque
from functools import cached_property
import json
import math
from pathlib import Path
import struct

from deathnote.game.players.inventory import member_cards


def _single(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def _scaled_power(maximum, rate):
    return math.floor(_single(_single(maximum * rate) / _single(10000)))


class NativeBandRank:
    def __init__(self, master_directory):
        directory = Path(master_directory)
        self.master_directory = directory
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.members = {row['_id']: row for row in rows('MasterMemberCard')}
        self.characters = {row['_id']: row for row in rows('MasterCharacter')
                           if not row['_isNonPlayable']}
        self.levels = rows('MasterMemberCardLevel')
        self.awakes = rows('MasterMemberCardAwake')
        self.member_ranks = rows('MasterMemberCardRank')
        self.character_ranks = rows('MasterCharacterRank')
        self.total_ranks = rows('MasterCharacterTotalRank')
        self.skill_ratings = {(row['_skillType'], row['_skillLevel']): row['_value']
                              for row in rows('MasterSkillRating')}
        self.supports = {row['_id']: row for row in rows('MasterSupportCard')}
        self.support_levels = rows('MasterSupportCardLevel')
        self.support_ranks = rows('MasterSupportCardRank')
        self.band_ranks = rows('MasterBandRank')

    @cached_property
    def band_item_masters(self):
        def rows(name):
            return json.loads((self.master_directory / (name + '.json')).read_text('utf8'))['_allData']
        return {'effects': {(row['_bandItemId'], row['_level']): row
                            for row in rows('MasterBandItemSkillEffect')},
                'targets': {row['_id']: row for row in rows('MasterSkillTarget')}}

    def _band_item_maps(self, state):
        maps = {name: defaultdict(lambda: [0, 0, 0])
                for name in ('_characterID', '_bandID', '_cardType')}
        for master_id, level in state.get('band_items', {}).items():
            effect = self.band_item_masters['effects'].get((int(master_id), level))
            if effect is None:
                continue
            components = {1000: (0, 1, 2), 1001: (1,), 1002: (2,), 1003: (0,)}.get(
                effect['_skillEffectType'], ())
            for target_id in effect['_skillTargetIDs']:
                target = self.band_item_masters['targets'][target_id]
                for key, mapping in maps.items():
                    if target[key]:
                        for component in components:
                            mapping[target[key]][component] += effect['_effectValue']
        return maps

    @staticmethod
    def _level(rows, group, exp):
        return max((row for row in rows if row['_group'] == group and row['_exp'] <= exp),
                   key=lambda row: row['_level'])

    def _character_rank(self, state, character_id):
        exp = state['character_exp'].get(str(character_id), 0)
        return max((row for row in self.character_ranks if row['_exp'] <= exp),
                   key=lambda row: row['_rank'])

    def _member_power(self, state, card, total_rank_bonus, band_item_maps):
        master_id = card['master_id']
        master = self.members[master_id]
        exp = state['member_exp'].get(str(card['id']), 0)
        level = self._level(self.levels, master['_memberCardLevelGroup'], exp)
        awake = next(row for row in self.awakes
                     if row['_group'] == master['_memberCardAwakeGroup'] and row['_awakeCount'] == card['awake_count'])
        rank = next(row for row in self.member_ranks
                    if row['_group'] == master['_memberCardRankGroup'] and row['_rank'] == card['card_rank'])
        skill = self.skill_ratings.get((0, rank['_leaderSkillLevel']), 0)
        skill += sum(self.skill_ratings.get((kind, card[field]), 0) for kind, field in
                     ((1, 'live_skill_level'), (4, 'performance_skill_level')))
        bonus = self._character_rank(state, master['_characterID'])['_bonus'] + total_rank_bonus
        targets = {'_characterID': master['_characterID'],
                   '_bandID': self.characters[master['_characterID']]['_bandID'],
                   '_cardType': master['_cardType']}
        item_bp = [sum(band_item_maps[key].get(target, (0, 0, 0))[component]
                       for key, target in targets.items()) for component in range(3)]
        output = []
        for component, (maximum, rate) in enumerate((('_performancePowerMax', '_performanceRate'),
                              ('_technicPowerMax', '_technicRate'), ('_visualPowerMax', '_visualRate'))):
            base = _scaled_power(master[maximum], level[rate])
            base += _scaled_power(master[maximum], rank[rate])
            base += math.floor(_single(_single(_single(awake[rate]) / _single(10000))
                                       * _single(master[maximum])))
            before_item = base * (10000 + skill) + bonus * 10000
            output.append(before_item * (10000 + item_bp[component]) // 10000)
        return tuple(output)

    def _support_value(self, card):
        master = self.supports[card['master_id']]
        level = self._level(self.support_levels, master['_supportCardLevelGroup'], card['exp'])
        rank = next(row for row in self.support_ranks
                    if row['_group'] == master['_supportCardRankGroup'] and row['_rank'] == card['card_rank'])
        result = sum(_scaled_power(master[maximum], level[rate])
                     for maximum, rate in (('_performancePowerMax', '_performanceRate'),
                        ('_technicPowerMax', '_technicRate'), ('_visualPowerMax', '_visualRate')))
        for skill_id, skill_level in (('_supportSkillId01', '_supportSkill01Level'),
                ('_supportSkillId02', '_supportSkill02Level'),
                ('_gekisouSupportSkillId01', '_gekisouSupportSkill01Level'),
                ('_gekisouSupportSkillId02', '_gekisouSupportSkill02Level')):
            if master[skill_id]:
                result += self.skill_ratings.get((3, rank[skill_level]), 0)
        return result

    def _support_slots(self, state, members):
        """Assign every owned support once, as Player.GetTypeRank does."""
        slots = {key: [] for key in members}
        multiple = defaultdict(list)
        for card in state.get('support_cards', {}).values():
            master = self.supports[card['master_id']]
            characters = master['_characterIDs']
            record = (card['master_id'], self._support_value(card), characters)
            if len(characters) == 1:
                key = master['_cardType'], characters[0]
                if key in slots:
                    slots[key].append(record)
            else:
                multiple[master['_cardType']].append(record)
        for key, cards in slots.items():
            cards.sort(key=lambda card: card[1], reverse=True)
            del cards[len(members[key]):]
        for card_type, cards in multiple.items():
            pending = deque(cards)
            while pending:
                card = pending.popleft()
                candidates = [(card_type, character_id) for character_id in card[2]
                              if (card_type, character_id) in members]
                vacant = [key for key in candidates if len(slots[key]) < len(members[key])]
                target = None
                if vacant:
                    target = max(vacant, key=lambda key: sum(members[key][len(slots[key])]))
                else:
                    lowest = card[1]
                    for key in candidates:
                        for existing in slots[key]:
                            if existing[1] < lowest:
                                lowest = existing[1]
                                target = key
                if target is None:
                    continue
                assigned = slots[target]
                assigned.append(card)
                assigned.sort(key=lambda existing: existing[1], reverse=True)
                capacity = len(members[target])
                if len(assigned) > capacity:
                    displaced = assigned.pop()
                    if len(displaced[2]) > 1 and displaced[0] != card[0]:
                        pending.appendleft(displaced)
        return slots

    def evaluation_value(self, state, band_id):
        total_rank = sum(self._character_rank(state, character_id)['_rank']
                         for character_id in self.characters)
        total_bonus = max((row for row in self.total_ranks if row['_totalRank'] <= total_rank),
                          key=lambda row: row['_totalRank'])['_bonus']
        members = defaultdict(list)
        band_item_maps = self._band_item_maps(state)
        for card in member_cards(state).values():
            master = self.members[card['master_id']]
            character_id = master['_characterID']
            if self.characters[character_id]['_bandID'] == band_id:
                members[(master['_cardType'], character_id)].append(
                    self._member_power(state, card, total_bonus, band_item_maps))
        members = {key: sorted(powers, key=sum, reverse=True)[:3]
                   for key, powers in members.items()}
        supports = self._support_slots(state, members)
        result = 0
        for key, powers in members.items():
            support = supports[key]
            for slot, power in enumerate(powers):
                member_value = sum(component // 10000 for component in power)
                support_value = support[slot][1] if slot < len(support) else 0
                result += int(_single(member_value * (10000 + support_value) // 10000))
        return result

    def rank(self, state, band_id):
        value = self.evaluation_value(state, band_id)
        return max(row['_rank'] for row in self.band_ranks if row['_value'] <= value)

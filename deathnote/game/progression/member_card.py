"""Authenticated member-card level, awakening and skill upgrades."""
from __future__ import annotations

from contextlib import closing
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.inventory import member_cards
from deathnote.game.players.progress import NativeLiveProgress
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields


LEVEL_UP_METHOD = '/app.membercard.MemberCardService/LevelUp'
AWAKE_METHOD = '/app.membercard.MemberCardService/Awake'
LIVE_SKILL_LEVEL_UP_METHOD = '/app.membercard.MemberCardService/LiveSkillLevelUp'
PERFORMANCE_SKILL_LEVEL_UP_METHOD = '/app.membercard.MemberCardService/PerformanceSkillLevelUp'
RANK_UP_METHOD = '/app.membercard.MemberCardService/RankUp'
LEADER_SKILL_LEVEL_UP_METHOD = '/app.membercard.MemberCardService/LeaderSkillLevelUp'
LINK_SKILL_LEVEL_UP_METHOD = '/app.membercard.MemberCardService/LinkSkillLevelUp'
GEKISOU_SKILL_LEVEL_UP_METHOD = '/app.membercard.MemberCardService/GekisouSkillLevelUp'


class NativeMemberCard:
    methods = frozenset((LEVEL_UP_METHOD, AWAKE_METHOD, LIVE_SKILL_LEVEL_UP_METHOD,
                         PERFORMANCE_SKILL_LEVEL_UP_METHOD, RANK_UP_METHOD,
                         LEADER_SKILL_LEVEL_UP_METHOD, LINK_SKILL_LEVEL_UP_METHOD,
                         GEKISOU_SKILL_LEVEL_UP_METHOD))

    def __init__(self, login, master_directory, *, clock=time.time, offline_bonus=None):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.offline_bonus = offline_bonus
        self.progress = NativeLiveProgress(login)
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.masters = {row['_id']: row for row in rows('MasterMemberCard')}
        self.ranks = {(row['_group'], row['_rank']): row for row in rows('MasterMemberCardRank')}
        self.bands = {row['_id']: row for row in rows('MasterBand')}
        self.characters = {row['_id']: row for row in rows('MasterCharacter')}
        self.levels = {(row['_group'], row['_level']): row for row in rows('MasterMemberCardLevel')}
        self.limits = {(row['_rarity'], row['_awakeCount']): row['_limitLevel']
                       for row in rows('MasterMemberCardLevelLimit')}
        self.awake_maximums = defaultdict(int)
        for row in rows('MasterMemberCardAwake'):
            group = row['_group']
            self.awake_maximums[group] = max(self.awake_maximums[group], row['_awakeCount'])
        self.awake_resources = defaultdict(Counter)
        for row in rows('MasterMemberCardAwakeResource'):
            self.awake_resources[row['_group'], row['_awakeCount']][str(row['_itemId'])] += row['_count']
        self.awake_resources = dict(self.awake_resources)
        self.skill_resources = defaultdict(Counter)
        for row in rows('MasterSkillLevelResource'):
            self.skill_resources[row['_group'], row['_level']][str(row['_itemID'])] += row['_count']
        self.skill_resources = dict(self.skill_resources)
        self.live_skill_maximums = defaultdict(int)
        for row in rows('MasterLiveSkillEffect'):
            skill = row['_liveSkillID']
            self.live_skill_maximums[skill] = max(self.live_skill_maximums[skill], row['_level'])
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.common_piece_ids = {rarity: int(parameters['common_rank_up_item_id_for_rarity_' + name])
                                 for rarity, name in ((2, 'r'), (3, 'sr'), (4, 'ssr'), (20, 'bd'))}
        self.exchange_cost = int(parameters['member_exp_exchange_cost'])
        items = rows('MasterItem')
        self.exp_item_id = str(next(row['_id'] for row in items if row['_type'] == 3))
        self.sc_item_id = str(next(row['_id'] for row in items if row['_type'] == 14))

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method not in self.methods:
            raise NotImplementedError(method)
        if method == RANK_UP_METHOD:
            return self._rank_up(player, payload)
        try:
            values = dict(fields(payload))
            master_id = values.get(1, 0)
            target_level = values.get(2, 0)
            highest_total_power = values.get(3, 0)
            if (0 in values or type(master_id) is not int or not 0 < master_id <= 0x7fffffffffffffff
                    or type(target_level) is not int
                    or not (0 if method == LINK_SKILL_LEVEL_UP_METHOD else 1) <= target_level <= 0x7fffffff
                    or type(highest_total_power) is not int or not 0 <= highest_total_power <= 0x7fffffff):
                raise ValueError('Invalid member-card upgrade scalar')
        except (TypeError, ValueError):
            raise NativeRpcError(3) from None
        master = self.masters.get(master_id)
        if master is None:
            raise NativeRpcError(3)
        if method != LEVEL_UP_METHOD:
            return self._enhance(method, player, master, target_level)
        target = self.levels.get((master['_memberCardLevelGroup'], target_level))
        if target is None:
            raise NativeRpcError(3)
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            card = next((card for card in member_cards(state, player['created_at']).values()
                         if card['master_id'] == master_id), None)
            if card is None:
                raise NativeRpcError(9)
            limit = self.limits[(master['_rarity'], card['awake_count'])]
            if target_level > limit:
                raise NativeRpcError(9)
            card_id = str(card['id'])
            current_exp = state['member_exp'].get(card_id, 0)
            needed_exp = max(0, target['_exp'] - current_exp)
            costs = ((self.exp_item_id, needed_exp),
                     (self.sc_item_id, needed_exp * self.exchange_cost))
            if any(state['items'].get(item_id, 0) < amount for item_id, amount in costs):
                raise NativeRpcError(9)
            for item_id, amount in costs:
                if amount:
                    state['items'][item_id] -= amount
            before_rates = self.offline_bonus.capture_rates(state) if self.offline_bonus is not None else None
            if needed_exp:
                state['member_exp'][card_id] = target['_exp']
            notification = (self.offline_bonus.stock_changed_rates(connection, player, state, before_rates)
                            if before_rates is not None else b'')
            self.progress.save(connection, profile_id, state)
        return message_field(1, notification)

    def _enhance(self, method, player, master, target):
        if method == AWAKE_METHOD:
            property_name = 'awake_count'
            maximum = self.awake_maximums[master['_memberCardAwakeGroup']]
            resources = self.awake_resources
            group = master['_memberCardAwakeResourceGroup']
        elif method == LIVE_SKILL_LEVEL_UP_METHOD:
            property_name = 'live_skill_level'
            maximum = self.live_skill_maximums[master['_liveSkillID']]
            resources = self.skill_resources
            group = master['_liveSkillLevelResourceGroup']
        elif method in (PERFORMANCE_SKILL_LEVEL_UP_METHOD, GEKISOU_SKILL_LEVEL_UP_METHOD):
            property_name = 'performance_skill_level'
            maximum = 5
            resources = self.skill_resources
            group = master['_gekisouSkillLevelResourceGroup']
        else:
            property_name = None
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            card = next((card for card in member_cards(state, player['created_at']).values()
                         if card['master_id'] == master['_id']), None)
            if card is None:
                raise NativeRpcError(9)
            if property_name is None:
                current = (self.ranks[master['_memberCardRankGroup'], card['card_rank']]['_leaderSkillLevel']
                           if method == LEADER_SKILL_LEVEL_UP_METHOD else card['link_skill_level'])
                if target != current:
                    raise NativeRpcError(9)
                return message_field(1, b'')
            current = card[property_name]
            if target < current or target > maximum:
                raise NativeRpcError(9)
            if target == current:
                return message_field(1, b'')
            costs = Counter()
            for level in range(current + 1, target + 1):
                costs.update(resources[group, level])
            if any(state['items'].get(item_id, 0) < amount for item_id, amount in costs.items()):
                raise NativeRpcError(9)
            for item_id, amount in costs.items():
                state['items'][item_id] -= amount
            before_rates = self.offline_bonus.capture_rates(state) if self.offline_bonus is not None else None
            card[property_name] = target
            notification = (self.offline_bonus.stock_changed_rates(connection, player, state, before_rates)
                            if before_rates is not None else b'')
            self.progress.save(connection, profile_id, state)
        return message_field(1, notification)

    def _rank_up(self, player, payload):
        try:
            values, supplied = {}, []
            for number, value in fields(payload):
                if number < 1:
                    raise ValueError('Protobuf field zero')
                if number == 3:
                    if not isinstance(value, bytes):
                        raise ValueError('RankUpUseItem must be a message')
                    item = dict(fields(value))
                    item_id, amount, piece_type = (item.get(index, 0) for index in (1, 2, 3))
                    if (0 in item or type(item_id) is not int or not 0 < item_id <= 0x7fffffffffffffff
                            or type(amount) is not int or not 0 <= amount <= 0x7fffffff
                            or type(piece_type) is not int or piece_type not in (1, 2, 3)):
                        raise ValueError('Invalid rank-up piece')
                    supplied.append((piece_type, item_id, amount))
                else:
                    values[number] = value
            master_id, target, power = values.get(1, 0), values.get(2, 0), values.get(4, 0)
            if (type(master_id) is not int or not 0 < master_id <= 0x7fffffffffffffff
                    or type(target) is not int or not 0 < target <= 0x7fffffff
                    or type(power) is not int or not 0 <= power <= 0x7fffffff):
                raise ValueError('Invalid rank-up scalar')
        except (TypeError, ValueError):
            raise NativeRpcError(3) from None
        master = self.masters.get(master_id)
        if master is None:
            raise NativeRpcError(3)
        group = master['_memberCardRankGroup']
        if (group, target) not in self.ranks:
            raise NativeRpcError(9)
        band = self.bands[self.characters[master['_characterID']]['_bandID']]
        rarity_name = {2: 'R', 3: 'SR', 4: 'SSR', 20: 'BD'}[master['_rarity']]
        expected = {1: master['_rankUpItemID'],
                    2: band['_rankUpItemIdForRarity' + rarity_name],
                    3: self.common_piece_ids[master['_rarity']]}
        if any(item_id != expected[piece_type] for piece_type, item_id, _ in supplied):
            raise NativeRpcError(3)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            card = next((card for card in member_cards(state, player['created_at']).values()
                         if card['master_id'] == master_id), None)
            if card is None or target < card['card_rank']:
                raise NativeRpcError(9)
            if target == card['card_rank']:
                return message_field(1, b'')
            required = sum(self.ranks[group, rank]['_requiredRankUpItemCount']
                           for rank in range(card['card_rank'] + 1, target + 1))
            if sum(amount for _, _, amount in supplied) != required:
                raise NativeRpcError(9)
            costs = Counter()
            for _, item_id, amount in supplied:
                costs[str(item_id)] += amount
            if any(state['items'].get(item_id, 0) < amount for item_id, amount in costs.items()):
                raise NativeRpcError(9)
            for item_id, amount in costs.items():
                if amount:
                    state['items'][item_id] -= amount
            before_rates = self.offline_bonus.capture_rates(state) if self.offline_bonus is not None else None
            card['card_rank'] = target
            card['leader_skill_level'] = self.ranks[group, target]['_leaderSkillLevel']
            notification = (self.offline_bonus.stock_changed_rates(connection, player, state, before_rates)
                            if before_rates is not None else b'')
            self.progress.save(connection, player['profile_id'], state)
        return message_field(1, notification)

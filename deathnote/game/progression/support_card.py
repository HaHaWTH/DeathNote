"""Support-card target-level upgrades using original EXP and rank-limit rules."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields


LEVEL_UP_METHOD = '/app.supportcard.SupportCardService/LevelUp'
RANK_UP_METHOD = '/app.supportcard.SupportCardService/RankUp'


class NativeSupportCard:
    methods = frozenset((LEVEL_UP_METHOD, RANK_UP_METHOD))

    def __init__(self, login, master_directory, *, clock=time.time, offline_bonus=None):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.offline_bonus = offline_bonus
        self.progress = NativeLiveProgress(login)
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.masters = {row['_id']: row for row in rows('MasterSupportCard')}
        self.levels = {(row['_group'], row['_level']): row for row in rows('MasterSupportCardLevel')}
        self.ranks = {(row['_group'], row['_rank']): row for row in rows('MasterSupportCardRank')}
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.exchange_cost = int(parameters['support_exp_exchange_cost'])
        items = rows('MasterItem')
        self.exp_item_id = str(next(row['_id'] for row in items if row['_type'] == 4))
        self.sc_item_id = str(next(row['_id'] for row in items if row['_type'] == 14))

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method not in self.methods:
            raise NotImplementedError(method)
        try:
            values = dict(fields(payload))
            master_id, target_level, highest_total_power = values.get(1, 0), values.get(2, 0), values.get(3, 0)
            if (0 in values or type(master_id) is not int or not 0 < master_id <= 0x7fffffffffffffff
                    or type(target_level) is not int or not 0 < target_level <= 0x7fffffff
                    or type(highest_total_power) is not int or not 0 <= highest_total_power <= 0x7fffffff):
                raise ValueError('Invalid support-card level-up scalar')
        except (TypeError, ValueError):
            raise NativeRpcError(3) from None
        master = self.masters.get(master_id)
        if master is None:
            raise NativeRpcError(3)
        if method == RANK_UP_METHOD:
            return self._rank_up(player, master, target_level)
        target = self.levels.get((master['_supportCardLevelGroup'], target_level))
        if target is None:
            raise NativeRpcError(3)
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            card = state.get('support_cards', {}).get(str(master_id))
            if card is None:
                raise NativeRpcError(9)
            limit = self.ranks[(master['_supportCardRankGroup'], card['card_rank'])]['_limitLevel']
            if target_level > limit:
                raise NativeRpcError(9)
            needed_exp = max(0, target['_exp'] - card['exp'])
            costs = ((self.exp_item_id, needed_exp), (self.sc_item_id, needed_exp * self.exchange_cost))
            if any(state['items'].get(item_id, 0) < amount for item_id, amount in costs):
                raise NativeRpcError(9)
            for item_id, amount in costs:
                if amount:
                    state['items'][item_id] -= amount
            before_rates = self.offline_bonus.capture_rates(state) if self.offline_bonus is not None else None
            if needed_exp:
                card['exp'] = target['_exp']
            notification = (self.offline_bonus.stock_changed_rates(connection, player, state, before_rates)
                            if before_rates is not None else b'')
            self.progress.save(connection, profile_id, state)
        return message_field(1, notification)

    def _rank_up(self, player, master, target_rank):
        group = master['_supportCardRankGroup']
        if (group, target_rank) not in self.ranks:
            raise NativeRpcError(9)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            card = state.get('support_cards', {}).get(str(master['_id']))
            if card is None or target_rank < card['card_rank']:
                raise NativeRpcError(9)
            if target_rank == card['card_rank']:
                return message_field(1, b'')
            cost = sum(self.ranks[group, rank]['_requiredRankUpItemCount']
                       for rank in range(card['card_rank'] + 1, target_rank + 1))
            if card['duplicate_count'] < cost:
                raise NativeRpcError(9)
            card['duplicate_count'] -= cost
            before_rates = self.offline_bonus.capture_rates(state) if self.offline_bonus is not None else None
            card['card_rank'] = target_rank
            notification = (self.offline_bonus.stock_changed_rates(connection, player, state, before_rates)
                            if before_rates is not None else b'')
            self.progress.save(connection, player['profile_id'], state)
        return message_field(1, notification)

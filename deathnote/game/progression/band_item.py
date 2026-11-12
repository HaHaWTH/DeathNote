"""Original band-item target-level upgrades in the authenticated account."""
from __future__ import annotations
from collections import Counter
from contextlib import closing
from functools import cached_property
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields

LEVEL_UP_METHOD = '/app.banditem.BandItemService/LevelUp'


class NativeBandItem:
    methods = frozenset((LEVEL_UP_METHOD,))

    def __init__(self, login, master_directory, *, progress=None, clock=time.time):
        self.login = login
        self.master_directory = Path(master_directory)
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.clock = clock
        self.offline_bonus = None

    @cached_property
    def masters(self):
        def rows(name):
            return json.loads((self.master_directory / (name + '.json')).read_text('utf8'))['_allData']
        return {'items': {row['_id']: row for row in rows('MasterBandItem')},
                'levels': {(row['_bandItemId'], row['_level']): row for row in rows('MasterBandItemLevel')},
                'costs': rows('MasterSkillLevelResource'), 'ranks': rows('MasterPlayerRank')}

    @staticmethod
    def player_fields(state):
        return b''.join(message_field(8, integer_field(1, int(master_id)) + integer_field(2, level))
            for master_id, level in sorted(state.get('band_items', {}).items(), key=lambda row: int(row[0])))

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            request = dict(fields(payload))
            master_id, target = request.get(1, 0), request.get(2, 0)
            if not isinstance(master_id, int) or not isinstance(target, int):
                raise ValueError('Band item request requires scalar IDs and levels')
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'band_item_wire'}) from None
        master = self.masters['items'].get(master_id)
        target_master = self.masters['levels'].get((master_id, target))
        if master is None or target_master is None:
            raise NativeRpcError(3, diagnostic={'code': 'band_item_target', 'bandItemId': master_id, 'toLevel': target})
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            current = state.get('band_items', {}).get(str(master_id), 0)
            if target == current:
                return message_field(1, b'')
            if target < current:
                raise NativeRpcError(9, diagnostic={'code': 'band_item_level_order', 'currentLevel': current, 'toLevel': target})
            steps = [self.masters['levels'][(master_id, level)] for level in range(current + 1, target + 1)]
            required_rank = max(row['_playerRank'] for row in steps)
            rank = max(row['_rank'] for row in self.masters['ranks'] if row['_exp'] <= state['rank_exp'])
            if rank < required_rank:
                raise NativeRpcError(9, diagnostic={'code': 'band_item_player_rank', 'playerRank': rank, 'requiredRank': required_rank})
            costs = Counter()
            for row in self.masters['costs']:
                if row['_group'] == master['_resourceGroupId'] and current < row['_level'] <= target:
                    costs[row['_itemID']] += row['_count']
            for item_id, count in costs.items():
                available = state['items'].get(str(item_id), 0)
                if available < count:
                    raise NativeRpcError(9, diagnostic={'code': 'band_item_balance', 'itemId': item_id,
                                                       'required': count, 'available': available})
            for item_id, count in costs.items():
                state['items'][str(item_id)] -= count
            now = int(self.clock())
            previous_rates = (self.offline_bonus.capture_rates(state, now=now)
                              if self.offline_bonus is not None else None)
            state.setdefault('band_items', {})[str(master_id)] = target
            notification = (self.offline_bonus.stock_changed_rates(connection, player, state, previous_rates, now=now)
                            if self.offline_bonus is not None else b'')
            self.progress.save(connection, player['profile_id'], state)
        return message_field(1, notification)

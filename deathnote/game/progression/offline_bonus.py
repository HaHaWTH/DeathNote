"""Recovered offline-bonus RPCs with persistent local accrual and reward draws.

The APK defines DTOs, display rates, limits and reward consumption. The official
server RNG and tick-rounding code are absent from the APK. This local server
uses master-weighted cached draws and integer cumulative tick rounding.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import cached_property
import json
from pathlib import Path
import random
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress, scalar_message
from deathnote.game.calendar import NativeServiceCalendar
from .rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields, varint


OFFLINE_STATUS_METHOD = '/app.offlinebonus.OfflineBonusService/Status'
OFFLINE_DETAIL_METHOD = '/app.offlinebonus.OfflineBonusService/Detail'
OFFLINE_RECEIVE_METHOD = '/app.offlinebonus.OfflineBonusService/Receive'
OFFLINE_RECEIVE_STOCK_METHOD = '/app.offlinebonus.OfflineBonusService/ReceiveStock'
OFFLINE_SKIP_METHOD = '/app.offlinebonus.OfflineBonusService/Skip'
_EARN_FIELDS = ('_earnCoin', '_earnMemberExp', '_earnSupportExp', '_earnOfflineBonusExp')


def _request_ids(payload, *, repeated):
    values = []
    try:
        for number, value in fields(payload):
            if number < 1:
                raise ValueError('Protobuf field zero')
            if number != 1:
                continue
            if repeated and isinstance(value, bytes):
                position = 0
                while position < len(value):
                    value_id, position = varint(value, position)
                    values.append(value_id)
            elif isinstance(value, int):
                if repeated:
                    values.append(value)
                else:
                    values = [value]
            else:
                raise ValueError('Offline unit ID has the wrong protobuf wire type')
    except ValueError:
        raise NativeRpcError(3) from None
    if any(value < 1 or value > 0x7fffffffffffffff for value in values) or not repeated and not values:
        raise NativeRpcError(3)
    return sorted(set(values))


def _resource(resource_type, resource_id, count):
    return scalar_message(((1, resource_type), (2, resource_id), (3, count)))


class NativeOfflineBonus:
    methods = frozenset((OFFLINE_STATUS_METHOD, OFFLINE_DETAIL_METHOD, OFFLINE_RECEIVE_METHOD,
                         OFFLINE_RECEIVE_STOCK_METHOD, OFFLINE_SKIP_METHOD))

    def __init__(self, login, master_directory, *, progress=None, rewards=None, band_rank=None,
                 vip_bonus=None, clock=time.time, rng=None):
        self.login = login
        self.database = login.database
        self.master_directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(self.master_directory)
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        self.band_rank = band_rank
        self.vip_bonus = (lambda state, bonus_type: 0) if vip_bonus is None else vip_bonus
        self.clock = clock
        self.rng = random.SystemRandom() if rng is None else rng
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_offline_bonus_units (
                profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                unit_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                PRIMARY KEY(profile_id, unit_id)
            )''')

    def _rows(self, name):
        return json.loads((self.master_directory / (name + '.json')).read_text('utf8'))['_allData']

    @cached_property
    def units(self):
        return {row['_id']: row for row in self._rows('MasterOfflineBonusUnit')}

    @cached_property
    def levels(self):
        result = defaultdict(list)
        for row in self._rows('MasterOfflineBonusUnitLevel'):
            result[row['_offlineBonusUnitId']].append(row)
        return {key: sorted(rows, key=lambda row: row['_offlineBonusExp']) for key, rows in result.items()}

    @cached_property
    def parameters(self):
        return {row['_id']: row['_value'] for row in self._rows('MasterParameter')}

    @cached_property
    def lots(self):
        result = defaultdict(list)
        for row in self._rows('MasterOfflineBonusItemLot'):
            result[row['_groupId']].append(row)
        return dict(result)

    @cached_property
    def exp_factors(self):
        return {row['_bandRank']: row['_expFactor'] for row in self._rows('MasterOfflineBonusExpFactor')}

    def _rank(self, progress, unit):
        if self.band_rank is None:
            from .band_rank import NativeBandRank
            self.band_rank = NativeBandRank(self.master_directory).rank
        return self.band_rank(progress, self.units[unit['id']]['_bandId'])

    @staticmethod
    def _released_at(master):
        value = datetime.strptime(master['_startAt'], '%Y/%m/%d %H:%M:%S')
        return int(value.replace(tzinfo=timezone(timedelta(hours=9))).timestamp())

    def _active_ids(self, now):
        return {key for key, row in self.units.items() if self._released_at(row) <= now}

    def _load_units(self, connection, player, now):
        records = {key: json.loads(value) for key, value in connection.execute(
            'SELECT unit_id, state FROM player_offline_bonus_units WHERE profile_id = ?', (player['profile_id'],))}
        for unit_id in self._active_ids(now):
            if unit_id not in records:
                records[unit_id] = {'id': unit_id, 'exp': 0,
                                   'start_at': max(player['created_at'], self._released_at(self.units[unit_id])),
                                   'accrued_ticks': 0, 'draw_count': 0, 'lot_resources': {}}
        return records

    @staticmethod
    def _save_unit(connection, profile_id, unit):
        connection.execute('''INSERT INTO player_offline_bonus_units(profile_id, unit_id, state)
            VALUES (?, ?, ?) ON CONFLICT(profile_id, unit_id) DO UPDATE SET state = excluded.state''',
            (profile_id, unit['id'], json.dumps(unit, separators=(',', ':'))))

    def _level(self, unit):
        return max((row for row in self.levels[unit['id']] if row['_offlineBonusExp'] <= unit['exp']),
                   key=lambda row: row['_offlineBonusExp'])

    def _pending(self, unit, progress, now, *, rates=None):
        accrual_level = unit.setdefault('accrual_level', self._level(unit)['_offlineBonusLevel'])
        level = next(row for row in self.levels[unit['id']] if row['_offlineBonusLevel'] == accrual_level)
        rank, quantity_bonus, efficiency_bonus = self._rates(progress, unit['id']) if rates is None else rates
        interval = int(self.parameters['offline_bonus_reward_interval_min']) * 60
        elapsed = max(0, now - unit['start_at'])
        ticks = max(unit['accrued_ticks'], min(elapsed, level['_offlineLimitTime']) // interval)
        unit['accrued_ticks'] = ticks
        efficient_seconds = min(level['_offlineLimitTime'], level['_offlineEfficiencyTime'] + efficiency_bonus)
        efficient_ticks = min(ticks, efficient_seconds // interval)
        decay = int(self.parameters['offline_bonus_efficiency_decay_rate'])
        weight = efficient_ticks * 10000 + (ticks - efficient_ticks) * decay
        bonus_rate = 10000 + quantity_bonus
        denominator = 10000 * 10000
        amounts = {key: level[key] * weight * bonus_rate // denominator for key in _EARN_FIELDS}
        amounts['_earnOfflineBonusExp'] = amounts['_earnOfflineBonusExp'] * self.exp_factors[rank] // 10000
        draw_count = max(unit['draw_count'], level['_earnItemLot'] * weight * bonus_rate // denominator)
        lottery = self.lots[level['_itemLotGroupId']]
        total_weight = sum(row['_weight'] for row in lottery)
        for _ in range(unit['draw_count'], draw_count):
            selected = self.rng.randrange(total_weight)
            for row in lottery:
                selected -= row['_weight']
                if selected < 0:
                    key = str(row['_resourceType']) + ':' + str(row['_resourceId'])
                    unit['lot_resources'][key] = unit['lot_resources'].get(key, 0) + row['_resourceCount']
                    break
        unit['draw_count'] = draw_count
        return amounts, ticks, interval

    @staticmethod
    def _lite(unit):
        return scalar_message(((1, unit['id']), (2, unit['exp']), (3, unit['start_at']),
                               (4, int(bool(unit.get('stock'))))))

    @staticmethod
    def _lot_resources(unit):
        return [(int(key.split(':')[0]), int(key.split(':')[1]), amount)
                for key, amount in sorted(unit['lot_resources'].items()) if amount]

    def _full(self, unit, amounts):
        return (scalar_message(((1, unit['id']), (2, unit['exp']), (3, unit['start_at'])))
                + scalar_message(((4, amounts['_earnCoin']), (5, amounts['_earnMemberExp']),
                (6, amounts['_earnSupportExp']), (7, amounts['_earnOfflineBonusExp'])))
                + b''.join(message_field(8, _resource(*item)) for item in self._lot_resources(unit)))

    def _gain_exp(self, unit, amount, progress):
        rank = self._rank(progress, unit)
        levels = self.levels[unit['id']]
        restricted = [row['_offlineBonusExp'] - 1 for row in levels if row['_unlockBandRank'] > rank]
        maximum = min(restricted) if restricted else levels[-1]['_offlineBonusExp']
        unit['exp'] = max(unit['exp'], min(maximum, unit['exp'] + amount))

    def _rates(self, state, unit_id):
        return (self._rank(state, {'id': unit_id}), self.vip_bonus(state, 5), self.vip_bonus(state, 6))

    def capture_rates(self, state, *, now=None):
        """Capture only values which change accrual before a card/VIP mutation."""
        now = int(self.clock()) if now is None else int(now)
        return {unit_id: self._rates(state, unit_id) for unit_id in self._active_ids(now)}

    def _reset_pending(self, unit, start_at):
        unit.update(start_at=int(start_at), accrued_ticks=0, draw_count=0, lot_resources={},
                    accrual_level=self._level(unit)['_offlineBonusLevel'])

    def stock_before_change(self, connection, player, state, unit_ids, *, now=None, rates=None):
        """Freeze old production in the caller's transaction; return Notification 8.

        `rates` may hold pre-mutation tuples from capture_rates when the owned
        inventory has already changed. No item reward is applied until ReceiveStock.
        """
        now = int(self.clock()) if now is None else int(now)
        units = self._load_units(connection, player, now)
        notification = []
        for unit_id in sorted(set(unit_ids)):
            unit = units[unit_id]
            amounts, _, _ = self._pending(unit, state, now,
                                          rates=None if rates is None else rates[unit_id])
            if not any(amounts.values()) and not unit['lot_resources']:
                continue
            stock = unit.setdefault('stock', {'amounts': {key: 0 for key in _EARN_FIELDS}, 'lot_resources': {}})
            for key, amount in amounts.items():
                stock['amounts'][key] += amount
            for key, amount in unit['lot_resources'].items():
                stock['lot_resources'][key] = stock['lot_resources'].get(key, 0) + amount
            self._reset_pending(unit, now)
            self._save_unit(connection, player['profile_id'], unit)
            notification.append(message_field(8, scalar_message(((1, unit_id), (2, now)))))
        return b''.join(notification)

    def stock_changed_rates(self, connection, player, state, before_rates, *, now=None):
        """Settle only units whose band-rank/VIP production rates actually changed."""
        changed = [unit_id for unit_id, previous in before_rates.items()
                   if self._rates(state, unit_id) != previous]
        return self.stock_before_change(connection, player, state, changed, now=now, rates=before_rates) if changed else b''

    def _accumulated(self, amounts):
        return [(1, int(self.parameters[parameter]), amounts[field]) for parameter, field in (
            ('offline_bonus_coin_convert_item_id', '_earnCoin'),
            ('offline_bonus_member_exp_convert_item_id', '_earnMemberExp'),
            ('offline_bonus_support_exp_convert_item_id', '_earnSupportExp')) if amounts[field]]

    def _status(self, state, now):
        status = state.setdefault('offline_bonus', {'ad_view_count': 0})
        last = status.get('last_ad_viewed_at')
        if last is not None and self.calendar.day(last) != self.calendar.day(now):
            status['ad_view_count'] = 0
        return status

    @staticmethod
    def _status_message(status):
        return scalar_message(((1, status['ad_view_count']),)) + (
            scalar_message(((2, status['last_ad_viewed_at']),)) if 'last_ad_viewed_at' in status else b'')

    @staticmethod
    def _result(unit_message, accumulated, lottery):
        return (message_field(1, unit_message)
                + b''.join(message_field(2, _resource(*item)) for item in accumulated)
                + b''.join(message_field(3, _resource(*item)) for item in lottery))

    def _skip(self, unit, state, kind, item_id, now):
        status = self._status(state, now)
        if kind == 1:
            if item_id:
                raise NativeRpcError(3)
            if status['ad_view_count'] >= int(self.parameters['offline_bonus_skip_daily_ad_limit']):
                raise NativeRpcError(9)
        else:
            item = self.rewards.item_masters.get(item_id)
            if item is None or item['_type'] != 19:
                raise NativeRpcError(3)
            if state['items'].get(str(item_id), 0) < 1:
                raise NativeRpcError(9)
        duration = int(self.parameters['offline_bonus_skip_reward_duration']) * 60
        skipped = {'id': unit['id'], 'exp': unit['exp'], 'start_at': now - duration,
                   'accrued_ticks': 0, 'draw_count': 0, 'lot_resources': {}}
        amounts, _, _ = self._pending(skipped, state, now)
        accumulated, lottery = self._accumulated(amounts), self._lot_resources(skipped)
        pending, ticks, _ = self._pending(unit, state, now)
        if kind == 1:
            status['ad_view_count'] += 1
            status['last_ad_viewed_at'] = now
        else:
            state['items'][str(item_id)] -= 1
        self._gain_exp(unit, amounts['_earnOfflineBonusExp'], state)
        if not ticks:
            unit['accrual_level'] = self._level(unit)['_offlineBonusLevel']
        self.rewards.apply(state, accumulated + lottery, now=now)
        return (message_field(1, self._status_message(status))
                + message_field(2, self._result(self._full(unit, pending), accumulated, lottery))
                + message_field(3, b''))

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        now = int(self.clock())
        if method == OFFLINE_STATUS_METHOD:
            try:
                if any(number < 1 for number, _ in fields(payload)):
                    raise ValueError('Protobuf field zero')
            except ValueError:
                raise NativeRpcError(3) from None
            ids = None
        elif method == OFFLINE_SKIP_METHOD:
            try:
                request = dict(fields(payload))
                unit_id, kind, item_id = request.get(1, 0), request.get(2, 0), request.get(3, 0)
                if (0 in request or type(unit_id) is not int or not 0 < unit_id <= 0x7fffffffffffffff
                        or type(kind) is not int or kind not in (1, 2)
                        or type(item_id) is not int or not 0 <= item_id <= 0x7fffffffffffffff):
                    raise ValueError('Invalid skip request')
            except ValueError:
                raise NativeRpcError(3) from None
            ids = [unit_id]
        else:
            ids = _request_ids(payload, repeated=method == OFFLINE_RECEIVE_METHOD)
        if ids is not None and not set(ids).issubset(self._active_ids(now)):
            raise NativeRpcError(3)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            units = self._load_units(connection, player, now)
            progress = self.progress.load(connection, player['profile_id'])
            if method == OFFLINE_STATUS_METHOD:
                response = message_field(1, self._status_message(self._status(progress, now))) + b''.join(message_field(2, self._lite(units[key]))
                    for key in sorted(self._active_ids(now)))
            elif method == OFFLINE_DETAIL_METHOD:
                unit = units[ids[0]]
                amounts, _, _ = self._pending(unit, progress, now)
                response = message_field(1, self._full(unit, amounts))
            elif method == OFFLINE_SKIP_METHOD:
                response = self._skip(units[ids[0]], progress, kind, item_id, now)
            elif method == OFFLINE_RECEIVE_STOCK_METHOD:
                unit = units[ids[0]]
                pending, ticks, _ = self._pending(unit, progress, now)
                stock = unit.pop('stock', {'amounts': {key: 0 for key in _EARN_FIELDS}, 'lot_resources': {}})
                accumulated = self._accumulated(stock['amounts'])
                lottery = self._lot_resources(stock)
                self._gain_exp(unit, stock['amounts']['_earnOfflineBonusExp'], progress)
                if not ticks:
                    unit['accrual_level'] = self._level(unit)['_offlineBonusLevel']
                self.rewards.apply(progress, accumulated + lottery, now=now)
                response = message_field(1, self._result(self._full(unit, pending), accumulated, lottery)) + message_field(2, b'')
            else:
                results, received = [], []
                for unit_id in ids:
                    unit = units[unit_id]
                    amounts, ticks, interval = self._pending(unit, progress, now)
                    accumulated = self._accumulated(amounts)
                    lottery = self._lot_resources(unit)
                    received.extend(accumulated + lottery)
                    if ticks:
                        self._gain_exp(unit, amounts['_earnOfflineBonusExp'], progress)
                        start_at = max(unit['start_at'] + ticks * interval,
                                       now - max(0, now - unit['start_at']) % interval)
                        self._reset_pending(unit, start_at)
                    after = {field: 0 for field in amounts}
                    result = self._result(self._full(unit, after), accumulated, lottery)
                    results.append(message_field(1, result))
                self.rewards.apply(progress, received)
                response = b''.join(results) + message_field(2, b'')
            self.progress.save(connection, player['profile_id'], progress)
            for unit in units.values():
                self._save_unit(connection, player['profile_id'], unit)
            return response

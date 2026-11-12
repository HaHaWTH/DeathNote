"""Transactional recovery of LIVE boost using the original client's RPC."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import cached_property
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress, boost_message
from .rules import NativeLiveRules
from deathnote.game.progression.rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields


RECOVERY_METHOD = '/app.liveboost.LiveBoostService/Recovery'
_JST = timezone(timedelta(hours=9))


def _signed(value, bits: int) -> int:
    if not isinstance(value, int):
        raise ValueError('Recovery scalar has the wrong wire type')
    value &= (1 << bits) - 1
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _request(payload: bytes) -> tuple[int, int, dict[int, int]]:
    recovery_type, stars, items = 0, 0, {}
    try:
        for number, value in fields(payload):
            if number == 1:
                recovery_type = _signed(value, 32)
            elif number == 2:
                stars = _signed(value, 32)
            elif number == 3:
                if not isinstance(value, bytes):
                    raise ValueError('Recovery items map has the wrong wire type')
                entry = dict(fields(value))
                items[_signed(entry.get(1, 0), 64)] = _signed(entry.get(2, 0), 32)
    except ValueError:
        raise NativeRpcError(3) from None
    if recovery_type not in (1, 2, 3) or stars < 0 or any(key < 1 or count < 0 for key, count in items.items()):
        raise NativeRpcError(3)
    return recovery_type, stars, items


class NativeLiveBoost:
    methods = frozenset((RECOVERY_METHOD,))

    def __init__(self, login, master_directory: Path, *, clock=time.time):
        self.login = login
        self.master_directory = Path(master_directory)
        self.progress = NativeLiveProgress(login)
        self.rewards = NativeRewards(self.master_directory)
        self.clock = clock

    @cached_property
    def rules(self) -> NativeLiveRules:
        return NativeLiveRules(self.master_directory)

    @cached_property
    def parameters(self) -> dict:
        data = json.loads((self.master_directory / 'MasterParameter.json').read_text('utf8'))
        return {row['_id']: row['_value'] for row in data['_allData']}

    @staticmethod
    def _active_item(row: dict, now: int) -> bool:
        start, end = row['_startAt'], row['_endAt']
        def instant(value):
            return datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=_JST).timestamp()
        return (not start or instant(start) <= now) and (not end or now < instant(end))

    def _daily_reset(self, boost: dict, now: int) -> None:
        offset = 9 * 3600 - self.rules.daily_reset_seconds
        reset_at = ((now + offset) // 86400) * 86400 - offset
        if boost.get('last_daily_reset_at', 0) < reset_at:
            boost['daily_ad_view'] = 0
            boost['daily_full_consume_count'] = 0
            boost['last_daily_reset_at'] = reset_at

    def dispatch(self, method: str, payload: bytes, headers: dict[str, str]) -> bytes:
        player = self.login.authenticate(headers)
        if method != RECOVERY_METHOD:
            raise NotImplementedError(method)
        recovery_type, stars, items = _request(payload)
        if (recovery_type == 1 and (not stars or items)
                or recovery_type == 2 and (stars or items)
                or recovery_type == 3 and (stars or not any(items.values()))):
            raise NativeRpcError(3)
        now = int(self.clock())
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            boost = state['live_boost']
            boost.update(self.rules.recover_boost(boost['amount'], boost['previous_recovery_at'], now))
            self._daily_reset(boost, now)
            if boost['amount'] >= 99:
                raise NativeRpcError(9)
            if recovery_type == 1:
                cost_per_boost = int(self.parameters['live_boost_by_star'])
                if stars % cost_per_boost:
                    raise NativeRpcError(3)
                free = state['items'].get('1', 0)
                paid = state['items'].get('2', 0)
                if free + paid < stars:
                    raise NativeRpcError(9)
                free_cost = min(free, stars)
                state['items']['1'] = free - free_cost
                state['items']['2'] = paid - (stars - free_cost)
                recovered = stars // cost_per_boost
            elif recovery_type == 2:
                if boost.get('daily_ad_view', 0) >= int(self.parameters['live_boost_daily_ad_limit']):
                    raise NativeRpcError(9)
                boost['daily_ad_view'] = boost.get('daily_ad_view', 0) + 1
                recovered = int(self.parameters['live_boost_ad_repair'])
            else:
                recovered = 0
                for master_id, count in items.items():
                    row = self.rewards.item_masters.get(master_id)
                    if row is None or row['_type'] != 7:
                        raise NativeRpcError(3)
                    if not self._active_item(row, now) or state['items'].get(str(master_id), 0) < count:
                        raise NativeRpcError(9)
                    recovered += row['_value'] * count
                for master_id, count in items.items():
                    state['items'][str(master_id)] = state['items'].get(str(master_id), 0) - count
            boost['amount'] = min(99, boost['amount'] + recovered)
            self.progress.save(connection, player['profile_id'], state)
            response = message_field(1, boost_message(state)) + message_field(2, b'')
        return response

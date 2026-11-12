"""VIP daily points, daily rewards and rank-up presents for the local client.

The APK supplies reward selection and response-consumer contracts. Consecutive
login bookkeeping is a local-server policy: once per service day, the next day
continues a streak and a missed day starts a new one. It awards no points until
the original ReceivePoint RPC is called.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.progression.entitlements import DAY
from deathnote.game.calendar import NativeServiceCalendar
from deathnote.game.players.progress import NativeLiveProgress
from deathnote.game.progression.present import present_message
from deathnote.game.progression.rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, varint
from deathnote.protocol.decode import fields


PREFIX = '/app.vip.VipService/'
FETCH_METHOD = PREFIX + 'Fetch'
RECEIVE_POINT_METHOD = PREFIX + 'ReceivePoint'
RECEIVE_REWARD_METHOD = PREFIX + 'ReceiveReward'
RECEIVE_RANKUP_REWARD_METHOD = PREFIX + 'ReceiveRankupReward'

_VIP_FIELDS = ('point', 'consecutive_login_count', 'last_login_count_at',
               'last_daily_point_at', 'last_daily_reward_day_number',
               'last_daily_reward_at', 'gained_reward_rank')
_OPTIONAL_FIELDS = frozenset((3, 4, 6))


def vip_message(state):
    """Serialize entity.Vip from the shared progress state, including presence."""
    vip = state.get('vip', {})
    return b''.join(integer_field(number, vip[key])
                    for number, key in enumerate(_VIP_FIELDS, 1)
                    if key in vip and vip[key] is not None
                    and (number in _OPTIONAL_FIELDS or vip[key]))


def _request(payload, required):
    try:
        values = dict(fields(payload))
        if 0 in values:
            raise ValueError('Protobuf field zero')
        selected = [values.get(number, 0) for number in required]
        if any(type(value) is not int or not 0 < value <= 0x7fffffff for value in selected):
            raise ValueError('Invalid VIP request scalar')
        return selected
    except ValueError:
        raise NativeRpcError(3) from None


def _notification_items(before, state):
    return b''.join(message_field(4, integer_field(1, int(item_id))
                                 + integer_field(2, amount - before.get(item_id, 0)))
                    for item_id, amount in sorted(state['items'].items(), key=lambda pair: int(pair[0]))
                    if amount != before.get(item_id, 0))


class NativeVip:
    methods = frozenset((FETCH_METHOD, RECEIVE_POINT_METHOD, RECEIVE_REWARD_METHOD,
                         RECEIVE_RANKUP_REWARD_METHOD))

    def __init__(self, login, master_directory, *, progress=None, rewards=None, clock=time.time):
        self.login = login
        self.database = login.database
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        self.clock = clock
        self.offline_bonus = None
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory)

        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']

        self.ranks = sorted(rows('MasterVip'), key=lambda row: row['_point'])
        self.daily_points = sorted(rows('MasterVipDailyPoint'), key=lambda row: row['_consecutiveCount'])
        self.daily_rewards = defaultdict(list)
        for row in rows('MasterVipDailyReward'):
            self.daily_rewards[row['_vipRank'], row['_day']].append(row)
        self.rank_rewards = rows('MasterVipRankUpReward')
        self.present_type = next(row for row in rows('MasterPresentType') if row['_id'] == 31)

    def refresh_login(self, state, *, now):
        """Update local login-day bookkeeping in the caller's transaction."""
        vip = state.setdefault('vip', {})
        vip.setdefault('point', 0)
        vip.setdefault('last_daily_reward_day_number', 0)
        vip.setdefault('gained_reward_rank', 0)
        last_login = vip.get('last_login_count_at')
        current_day = self.calendar.day(now)
        if last_login is None or self.calendar.day(last_login) < current_day:
            consecutive = (vip.get('consecutive_login_count', 0) + 1
                           if last_login is not None and self.calendar.day(last_login) == current_day - 1 else 1)
            vip.update(consecutive_login_count=consecutive, last_login_count_at=now)
        return vip

    def rank(self, vip):
        return max(row['_vipRank'] for row in self.ranks if row['_point'] <= vip['point'])

    def daily_reward_day(self, vip):
        following = vip['last_daily_reward_day_number'] + 1
        return following if (self.rank(vip), following) in self.daily_rewards else 1

    def _received_today(self, vip, key, now):
        previous = vip.get(key)
        return previous is not None and previous >= self.calendar.day_start(now)

    def _point(self, state, consecutive, now):
        vip = state['vip']
        if self._received_today(vip, 'last_daily_point_at', now):
            return b''
        if consecutive != vip['consecutive_login_count']:
            raise NativeRpcError(9)
        point_row = max((row for row in self.daily_points if row['_consecutiveCount'] <= consecutive),
                        key=lambda row: row['_consecutiveCount'])
        before = vip['point']
        vip['point'] = min(0x7fffffff, before + point_row['_point'])
        vip['last_daily_point_at'] = now
        return message_field(5, integer_field(1, before) + integer_field(2, vip['point']))

    def _daily(self, state, requested_rank, requested_day, now):
        vip = state['vip']
        if self._received_today(vip, 'last_daily_reward_at', now):
            return b''
        if requested_rank != self.rank(vip) or requested_day != self.daily_reward_day(vip):
            raise NativeRpcError(9)
        rows = self.daily_rewards.get((requested_rank, requested_day))
        if not rows:
            raise NativeRpcError(9)
        before = state['items'].copy()
        self.rewards.apply(state, ((row['_resourceType'], row['_resourceId'], row['_resourceCount'])
                                   for row in rows), now=now)
        vip.update(last_daily_reward_day_number=requested_day, last_daily_reward_at=now)
        return _notification_items(before, state)

    def _rankup(self, state, requested_rank, now):
        vip = state['vip']
        gained = max(vip['gained_reward_rank'], 1)
        if requested_rank <= gained:
            return b''
        if requested_rank != self.rank(vip):
            raise NativeRpcError(9)
        box = state.setdefault('present_box', {})
        posted = []
        for row in self.rank_rewards:
            if not gained < row['_vipRank'] <= requested_rank:
                continue
            present_id = state.get('last_present_id', 0) + 1
            state['last_present_id'] = present_id
            present = {'present_id': present_id, 'post_at': now,
                       'expire_at': now + self.present_type['_expireDays'] * DAY,
                       'resource_type': row['_resourceType'], 'resource_id': row['_resourceId'],
                       'resource_count': row['_resourceCount'], 'present_type_id': 31,
                       'param_1': str(row['_vipRank']), 'param_2': ''}
            box[str(present_id)] = present
            posted.append(present)
        vip['gained_reward_rank'] = requested_rank
        return b''.join(message_field(1, present_message(present)) for present in posted)

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        required = () if method == FETCH_METHOD else (1, 2) if method == RECEIVE_REWARD_METHOD else (1,)
        request = _request(payload, required)
        now = int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            self.refresh_login(state, now=now)
            previous_rates = (self.offline_bonus.capture_rates(state, now=now)
                              if self.offline_bonus is not None and method == RECEIVE_POINT_METHOD else None)
            notification = b''
            if method == RECEIVE_POINT_METHOD:
                notification = self._point(state, request[0], now)
            elif method == RECEIVE_REWARD_METHOD:
                notification = self._daily(state, *request, now)
            elif method == RECEIVE_RANKUP_REWARD_METHOD:
                notification = self._rankup(state, request[0], now)
            if previous_rates is not None:
                notification += self.offline_bonus.stock_changed_rates(connection, player, state, previous_rates, now=now)
            self.progress.save(connection, player['profile_id'], state)
            response = message_field(1, vip_message(state))
            if method == FETCH_METHOD:
                purchased = sorted(set(state.get('purchased_vip_pack_ranks', ())))
                if purchased:
                    response += message_field(2, b''.join(varint(rank) for rank in purchased))
            else:
                response += message_field(2, notification)
        return response

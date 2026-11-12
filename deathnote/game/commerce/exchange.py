"""Native exchange recipes, costs and resettable purchase counts."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress, scalar_message
from deathnote.game.progression.rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.calendar import NativeServiceCalendar
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields


EXCHANGE_METHOD = '/app.exchange.ExchangeService/Exchange'
_JST = timezone(timedelta(hours=9))


def _instant(value):
    return (datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=_JST).timestamp()
            if value else None)


def _total_cost(product, purchased, amount):
    """Sum original one-based ordinal prices without iterating every purchase."""
    base = product['_paymentResourceCount']
    steps = product['_paymentSteps']
    if not steps:
        return base * amount
    first, last = purchased + 1, purchased + amount
    cost = base if first == 1 else 0
    first = max(first, 2)
    intervals = [(1, steps[0] - 1, base)]
    intervals.extend((start, steps[index + 1] - 1 if index + 1 < len(steps) else last,
                      product['_paymentStepResourceCounts'][index])
                     for index, start in enumerate(steps))
    for start, end, price in intervals:
        count = max(0, min(last, end) - max(first, start) + 1)
        cost += count * price
    return cost


class NativeExchange:
    methods = frozenset((EXCHANGE_METHOD,))

    def __init__(self, login, master_directory, *, clock=time.time, rewards=None):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.progress = NativeLiveProgress(login)
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.exchanges = {row['_id']: row for row in rows('MasterExchange')}
        self.products = {row['_id']: row for row in rows('MasterExchangeProduct')}
        self.degree_max = int(next(row['_value'] for row in rows('MasterParameter')
                                   if row['_id'] == 'same_degree_max_count'))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_exchange_counts (
                profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                product_id INTEGER NOT NULL, count INTEGER NOT NULL, last_exchange INTEGER NOT NULL,
                PRIMARY KEY(profile_id, product_id)
            )''')

    def _count(self, connection, profile_id, product_id, now):
        row = connection.execute('''SELECT count, last_exchange FROM player_exchange_counts
            WHERE profile_id = ? AND product_id = ?''', (profile_id, product_id)).fetchone()
        reset_type = self.products[product_id]['_resetType']
        anchor = self.calendar.period_starts(now)[reset_type - 2] if reset_type != 1 else int(now)
        if row is None:
            return 0, anchor
        count, last = row
        if reset_type != 1 and (last > anchor or last < anchor and now > anchor):
            count = 0 if last < anchor else count
            last = anchor
            connection.execute('''UPDATE player_exchange_counts SET count = ?, last_exchange = ?
                WHERE profile_id = ? AND product_id = ?''', (count, last, profile_id, product_id))
        return count, last

    def player_fields(self, connection, profile_id):
        output = []
        now = self.clock()
        products = connection.execute('''SELECT product_id FROM player_exchange_counts
            WHERE profile_id = ? ORDER BY product_id''', (profile_id,)).fetchall()
        for (product_id,) in products:
            if product_id not in self.products:
                continue
            count, last = self._count(connection, profile_id, product_id, now)
            output.append(message_field(21, scalar_message(((1, product_id), (2, count), (3, last)))))
        return b''.join(output)

    @staticmethod
    def _in_period(product, exchange, now):
        start = _instant(product['_startAt'] or exchange['_startAt'])
        end = _instant(product['_endAt'] or exchange['_endAt'])
        return (start is None or start <= now) and (end is None or now < end)

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method != EXCHANGE_METHOD:
            raise NotImplementedError(method)
        try:
            values = dict(fields(payload))
            product_id, amount = values.get(1, 0), values.get(2, 0)
            if (type(product_id) is not int or not 0 < product_id <= 0x7fffffffffffffff
                    or type(amount) is not int or not 0 < amount <= 0x7fffffff):
                raise ValueError('Invalid exchange selection')
        except (TypeError, ValueError):
            raise NativeRpcError(3) from None
        product = self.products.get(product_id)
        if product is None:
            raise NativeRpcError(3)
        exchange = self.exchanges[product['_exchangeId']]
        now = self.clock()
        if not self._in_period(product, exchange, now):
            raise NativeRpcError(9)
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            count, anchor = self._count(connection, profile_id, product_id, now)
            if product['_limitCount'] > 0 and count + amount > product['_limitCount']:
                raise NativeRpcError(9)
            kind, resource_id = product['_resourceType'], product['_resourceId']
            reward_count = product['_resourceCount'] * amount
            if kind == 1:
                cap = self.rewards.item_masters[resource_id]['_max']
                if cap > 0 and reward_count > max(0, cap - state['items'].get(str(resource_id), 0)):
                    raise NativeRpcError(9)
            elif kind == 17:
                current = state.get('degrees', {}).get(str(resource_id), {}).get('amount', 0)
                if reward_count > max(0, self.degree_max - current):
                    raise NativeRpcError(9)
            payment_type, payment_id = exchange['_paymentResourceType'], exchange['_paymentResourceId']
            cost = _total_cost(product, count, amount)
            if payment_type == 1:
                available = state['items'].get(str(payment_id), 0)
            elif payment_type == 7:
                row = connection.execute('''SELECT point FROM player_gacha_points
                    WHERE profile_id = ? AND gacha_id = ?''', (profile_id, payment_id)).fetchone()
                available = row[0] if row is not None else 0
            else:
                raise NotImplementedError('Exchange payment resource type: ' + str(payment_type))
            if available < cost:
                raise NativeRpcError(9)
            if payment_type == 1 and cost:
                state['items'][str(payment_id)] = available - cost
            elif payment_type == 7 and cost:
                connection.execute('''UPDATE player_gacha_points SET point = point - ?
                    WHERE profile_id = ? AND gacha_id = ?''', (cost, profile_id, payment_id))
            self.rewards.apply(state, ((kind, resource_id, reward_count),), now=now)
            self.progress.save(connection, profile_id, state)
            last = int(now) if product['_resetType'] == 1 else anchor
            connection.execute('''INSERT INTO player_exchange_counts(profile_id, product_id, count, last_exchange)
                VALUES (?, ?, ?, ?) ON CONFLICT(profile_id, product_id)
                DO UPDATE SET count = excluded.count, last_exchange = excluded.last_exchange''',
                (profile_id, product_id, count + amount, last))
        return message_field(1, b'')

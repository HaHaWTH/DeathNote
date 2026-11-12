"""Local shop inventory and explicit, no-payment cash-SKU simulation.

CreateOrder is rejected before the original client invokes its payment SDK.
Cash simulation is an explicit local management action, not an order response.
"""
from __future__ import annotations

from collections import defaultdict
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


SHOP_FETCH_METHOD = '/app.shop.ShopService/Fetch'
SHOP_PURCHASE_METHOD = '/app.shop.ShopService/Purchase'
SHOP_PURCHASE_CM_METHOD = '/app.shop.ShopService/PurchaseCM'
SHOP_PURCHASE_CASH_METHOD = '/app.shop.ShopService/PurchaseCash'
CREATE_ORDER_METHOD = '/app.order.OrderService/CreateOrder'
_JST = timezone(timedelta(hours=9))


def _instant(value):
    if value in ('', 'null'):
        return None
    form = '%Y/%m/%d %H:%M:%S' if value.count(':') == 2 else '%Y/%m/%d %H:%M'
    return int(datetime.strptime(value, form).replace(tzinfo=_JST).timestamp())


class NativeShop:
    methods = frozenset((SHOP_FETCH_METHOD, SHOP_PURCHASE_METHOD, SHOP_PURCHASE_CM_METHOD, SHOP_PURCHASE_CASH_METHOD,
                         CREATE_ORDER_METHOD))

    def __init__(self, login, master_directory, *, progress=None, rewards=None, clock=time.time):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory, reset_parameter='shop_reset_time')
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.shops = {row['_id']: row for row in rows('MasterShop')}
        self.products = defaultdict(list)
        for row in rows('MasterShopProduct'):
            self.products[row['_shopId']].append(row)
        self.player_ranks = rows('MasterPlayerRank')
        self.vip_ranks = rows('MasterVip')
        self.texts = {row['_id']: row for row in rows('MasterText')}
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_local_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                shop_id INTEGER NOT NULL,
                purchase_count INTEGER NOT NULL,
                executed_at INTEGER NOT NULL,
                simulated INTEGER NOT NULL,
                request_id TEXT,
                resources TEXT NOT NULL,
                UNIQUE(profile_id, request_id)
            )''')

    def _text(self, text_id):
        row = self.texts.get(text_id)
        return (row['_simplifiedChinese'] or row['_traditionalChinese'] or row['_japanese']) if row else text_id

    @staticmethod
    def _active(master, now):
        start, end = _instant(master['_startAt']), _instant(master['_endAt'])
        return (start is None or start <= now) and (end is None or now < end)

    def catalogue(self):
        now = int(self.clock())
        return [{'shop_id': row['_id'], 'label': self._text(row['_nameTextId']),
            'description': self._text(row['_descriptionTextId']), 'sku': row['_appStorePurchaseId'],
            'price': row['_price'], 'limit': row['_limitConsumeCount'], 'reset_type': row['_resetType'],
            'start_at': _instant(row['_startAt']), 'end_at': _instant(row['_endAt']),
            'resources': [{'type': product['_resourceType'], 'id': product['_resourceId'],
                           'count': product['_resourceCount'], 'bonus': product['_isBonus']}
                          for product in self.products[row['_id']]]}
            for row in sorted(self.shops.values(), key=lambda item: (item['_priority'], item['_id']))
            if row['_paymentType'] == 16 and self._active(row, now)]

    @staticmethod
    def _summary(row):
        return {'purchase_id': row[0], 'profile_id': row[1], 'shop_id': row[2], 'purchase_count': row[3],
                'executed_at': row[4], 'simulated': bool(row[5]), 'resources': json.loads(row[6])}

    def history(self, profile_id, limit=5):
        with closing(sqlite3.connect(self.database)) as connection:
            return [self._summary(row) for row in connection.execute('''
                SELECT id,profile_id,shop_id,purchase_count,executed_at,simulated,resources
                FROM player_local_purchases WHERE profile_id=? ORDER BY id DESC LIMIT ?''', (profile_id, limit))]

    def _counts(self, connection, profile_id, shop_id, now):
        starts = self.calendar.period_starts(now)
        rows = list(connection.execute('''SELECT purchase_count, executed_at FROM player_local_purchases
                                         WHERE profile_id=? AND shop_id=?''', (profile_id, shop_id)))
        return (sum(count for count, _ in rows),
                *(sum(count for count, when in rows if when >= start) for start in starts))

    def _purchase(self, profile_id, shop_id, count, *, simulated, request_id=None):
        if type(profile_id) is not int or type(shop_id) is not int or type(count) is not int or not 0 < count <= 0x7fffffff:
            raise NativeRpcError(3)
        master = self.shops.get(shop_id)
        if master is None or simulated and master['_paymentType'] != 16:
            raise NativeRpcError(3)
        now = int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                previous = connection.execute('''SELECT id,profile_id,shop_id,purchase_count,executed_at,simulated,resources
                    FROM player_local_purchases WHERE profile_id=? AND request_id=?''', (profile_id, request_id)).fetchone()
                if previous is not None:
                    if previous[2:4] != (shop_id, count):
                        raise NativeRpcError(3)
                    return self._summary(previous)
            if connection.execute('SELECT 1 FROM players WHERE profile_id=?', (profile_id,)).fetchone() is None:
                raise NativeRpcError(9)
            if not self._active(master, now):
                raise NativeRpcError(9)
            state = self.progress.load(connection, profile_id)
            player_rank = max(row['_rank'] for row in self.player_ranks if row['_exp'] <= state['rank_exp'])
            vip_point = state.get('vip', {}).get('point', 0)
            vip_rank = max(row['_vipRank'] for row in self.vip_ranks if row['_point'] <= vip_point)
            if player_rank < master['_playerRank'] or vip_rank < master['_vipRank']:
                raise NativeRpcError(9)
            counts = self._counts(connection, profile_id, shop_id, now)
            period_index = {1: 0, 2: 1, 3: 2, 4: 3}[master['_resetType']]
            if master['_limitConsumeCount'] and counts[period_index] + count > master['_limitConsumeCount']:
                raise NativeRpcError(9)
            if not simulated:
                price = master['_price'] * count
                free, paid = state['items'].get('1', 0), state['items'].get('2', 0)
                if master['_paymentType'] == 12:
                    free_cost = min(free, price)
                    paid_cost = price - free_cost
                elif master['_paymentType'] == 13:
                    free_cost, paid_cost = 0, price
                else:
                    raise NativeRpcError(9, diagnostic={'code': 'local_simulated_purchase_required'})
                if paid_cost > paid:
                    raise NativeRpcError(9)
                state['items']['1'], state['items']['2'] = free - free_cost, paid - paid_cost
            resources = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'] * count)
                         for row in self.products[shop_id]]
            self.rewards.apply(state, resources, now=now)
            self.progress.save(connection, profile_id, state)
            encoded_resources = json.dumps([{'type': kind, 'id': key, 'count': amount} for kind, key, amount in resources],
                                           separators=(',', ':'))
            cursor = connection.execute('''INSERT INTO player_local_purchases
                (profile_id,shop_id,purchase_count,executed_at,simulated,request_id,resources) VALUES(?,?,?,?,?,?,?)''',
                (profile_id, shop_id, count, now, int(simulated), request_id, encoded_resources))
            return self._summary((cursor.lastrowid, profile_id, shop_id, count, now, int(simulated), encoded_resources))

    def simulate_purchase(self, profile_id, shop_id, purchase_count=1, *, request_id):
        if not isinstance(request_id, str) or not request_id:
            raise NativeRpcError(3)
        return self._purchase(profile_id, shop_id, purchase_count, simulated=True, request_id=request_id)

    def _fetch(self, profile_id):
        now = int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection:
            shop_ids = [row[0] for row in connection.execute(
                'SELECT DISTINCT shop_id FROM player_local_purchases WHERE profile_id=? ORDER BY shop_id', (profile_id,))]
            return b''.join(message_field(1, scalar_message(((1, shop_id), *enumerate(
                self._counts(connection, profile_id, shop_id, now), 2)))) for shop_id in shop_ids)

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        values = {}
        try:
            for number, value in fields(payload):
                if number < 1:
                    raise ValueError('Protobuf field zero')
                values[number] = value
        except ValueError:
            raise NativeRpcError(3) from None
        if method in (CREATE_ORDER_METHOD, SHOP_PURCHASE_CASH_METHOD):
            raise NativeRpcError(9, diagnostic={'code': 'local_simulated_purchase_required'})
        if method == SHOP_FETCH_METHOD:
            return self._fetch(player['profile_id'])
        shop_id, count = values.get(1, 0), values.get(2, 1) if method == SHOP_PURCHASE_METHOD else 1
        self._purchase(player['profile_id'], shop_id, count, simulated=False)
        return message_field(1, b'')

"""Persistent local gacha execution using recovered DTOs and public master weights.

The original client receives prize IDs from its backend. This implementation's
weighted draws and conditional guaranteed slots are local server policies;
currency costs, master constraints and result consumption follow APK 1.0.6.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import json
from math import lcm
from pathlib import Path
import random
import sqlite3
import time

from deathnote.game.players.inventory import member_cards
from deathnote.game.progression.costume import unlocked_costume_notification
from deathnote.game.players.progress import NativeLiveProgress, scalar_message
from deathnote.game.progression.rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.calendar import NativeServiceCalendar
from deathnote.protocol.wire import integer_field, message_field, string_field, varint as encode_varint
from deathnote.protocol.decode import fields, varint


EXECUTE_METHOD = '/app.gacha.GachaService/Execute'
PROBABILITY_METHOD = '/app.gacha.GachaService/Probability'
HISTORY_METHOD = '/app.gacha.GachaService/History'
_JST = timezone(timedelta(hours=9))


def _request(payload, *, require_product=True):
    gacha_id, product_id, selected = 0, 0, []
    try:
        for number, value in fields(payload):
            if number < 1:
                raise ValueError('Protobuf field zero')
            if number in (1, 2):
                minimum = 1 if number == 1 or require_product else 0
                if not isinstance(value, int) or not minimum <= value <= 0x7fffffffffffffff:
                    raise ValueError('Gacha selection ID must be positive int64')
                if number == 1:
                    gacha_id = value
                else:
                    product_id = value
            elif number == 3:
                if isinstance(value, bytes):
                    offset = 0
                    while offset < len(value):
                        picked, offset = varint(value, offset)
                        selected.append(picked)
                else:
                    selected.append(value)
    except ValueError:
        raise NativeRpcError(3) from None
    if not gacha_id or require_product and not product_id or any(not 0 < value <= 0x7fffffffffffffff for value in selected):
        raise NativeRpcError(3)
    return gacha_id, product_id, selected


def _instant(value):
    if value in ('', 'null'):
        return None
    return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S').replace(tzinfo=_JST).timestamp())


def _in_period(row, now):
    start, end = _instant(row['_startAt']), _instant(row['_endAt'])
    return (start is None or start <= now) and (end is None or now < end)


def _packed(number, values):
    return message_field(number, b''.join(encode_varint(value) for value in values)) if values else b''


class NativeGacha:
    methods = frozenset((EXECUTE_METHOD, PROBABILITY_METHOD, HISTORY_METHOD))

    def __init__(self, login, master_directory, *, season_pass=None, clock=time.time, rng=None, rewards=None):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.offline_bonus = None
        self.rng = random.SystemRandom() if rng is None else rng
        self.season_pass = season_pass
        self.progress = NativeLiveProgress(login)
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory)
        self.rewards = NativeRewards(directory) if rewards is None else rewards
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.gachas = {row['_id']: row for row in rows('MasterGacha')}
        self.products = {row['_id']: row for row in rows('MasterGachaProduct')}
        self.prizes = {row['_id']: row for row in rows('MasterGachaPrize')}
        self.lots = defaultdict(list)
        self.prize_groups = defaultdict(list)
        self.bonus_lots = defaultdict(list)
        self.bonuses = {row['_id']: row for row in rows('MasterGachaBonus')}
        for row in rows('MasterGachaLot'):
            self.lots[row['_lotGroupId']].append(row)
        for row in self.prizes.values():
            self.prize_groups[row['_groupId']].append(row)
        for row in rows('MasterGachaBonusLot'):
            self.bonus_lots[row['_gachaBonusId']].append(row)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_gacha_products (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    gacha_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    consumed_count INTEGER NOT NULL,
                    last_execute INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, gacha_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS player_gacha_points (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    gacha_id INTEGER NOT NULL,
                    point INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, gacha_id)
                );
                CREATE TABLE IF NOT EXISTS player_gacha_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    gacha_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    free_gem INTEGER NOT NULL,
                    paid_gem INTEGER NOT NULL,
                    ticket INTEGER NOT NULL,
                    executed_at INTEGER NOT NULL,
                    prizes TEXT NOT NULL,
                    bonus_lot_ids TEXT NOT NULL
                );
            ''')

    def _consumed(self, connection, profile_id, gacha_id, product, now):
        row = connection.execute('''SELECT consumed_count, last_execute FROM player_gacha_products
            WHERE profile_id = ? AND gacha_id = ? AND product_id = ?''',
            (profile_id, gacha_id, product['_id'])).fetchone()
        if row is None:
            return 0
        if (product['_resetType'] in (2, 3)
                and row[1] < self.calendar.period_starts(now)[product['_resetType'] - 2]):
            return 0
        return row[0]

    @staticmethod
    def _cost(gacha, product, consumed, state, *, monthly_available=False, monthly_free=False):
        """GachaDrawCostCalculator.Calculate, current non-entitlement products."""
        count = product['_drawCount']
        price = max(0, product['_firstTimePrice']) if not consumed and product['_firstTimePrice'] else product['_price']
        point = product['_gachaPoint']
        currency = product['_itemType']
        ticket_id = gacha['_gachaTicketItemId']
        free, paid = state['items'].get('1', 0), state['items'].get('2', 0)
        tickets = state['items'].get(str(ticket_id), 0)
        if monthly_free:
            return {'count': count, 'free': 0, 'paid': 0, 'ticket_id': ticket_id, 'ticket': 0, 'point': 0}
        if currency == 23 and not monthly_available:
            raise NativeRpcError(9)
        if currency not in (2, 12, 13, 15, 23):
            raise NativeRpcError(3)
        if product['_drawCountType'] == 2:
            unit_price = price // count
            if currency == 2:
                available = tickets // unit_price
            elif currency == 12:
                available = tickets + (free + paid) // unit_price
            else:
                available = paid // unit_price
            actual_count = max(2, min(count, available))
            price = actual_count * unit_price
            point = point * actual_count // count
            count = actual_count
        ticket_cost, free_cost, paid_cost = 0, 0, 0
        if currency == 2:
            ticket_cost = price
        elif currency == 12:
            if price:
                ticket_cost = min(tickets, count)
                price -= ticket_cost * (price // count)
            free_cost = min(free, price)
            paid_cost = price - free_cost
        elif currency == 13:
            paid_cost = price
        if free_cost > free or paid_cost > paid or ticket_cost > tickets:
            raise NativeRpcError(9)
        return {'count': count, 'free': free_cost, 'paid': paid_cost,
                'ticket_id': ticket_id, 'ticket': ticket_cost, 'point': point}

    @staticmethod
    def _effective_limit(product, monthly_available):
        bonus_target = (product['_itemType'] == 13 and product['_resetType'] == 2
                        and product['_limitConsumeCount'] == 1 and bool(product['_monthlyPassIds']))
        return product['_limitConsumeCount'] + int(bonus_target and monthly_available)

    def _choose(self, weighted):
        denominator = lcm(*(weight.denominator for _, weight in weighted))
        scaled = [(value, weight.numerator * (denominator // weight.denominator)) for value, weight in weighted]
        selected = self.rng.randrange(sum(weight for _, weight in scaled))
        for value, weight in scaled:
            selected -= weight
            if selected < 0:
                return value
        raise AssertionError('Weighted gacha selection did not resolve')

    def _lot_prize_weights(self, gacha, product, now, *, guaranteed):
        result = []
        for lot in self.lots[gacha['_lotGroupId']]:
            if guaranteed:
                ensured_type = product['_ensuredType']
                if (ensured_type in (2, 3) and lot['_resourceTypeConstraint'] != ensured_type
                    or ensured_type == 4 and lot['_resourceTypeConstraint'] not in (2, 3)
                    or lot['_rarityConstraint'] < product['_ensuredRarity']):
                    continue
            prizes = [row for row in self.prize_groups[lot['_prizeGroupId']] if _in_period(row, now)]
            fixed = [row for row in prizes if row['_pickUpFixedRate']]
            normal = [row for row in prizes if not row['_pickUpFixedRate']]
            remainder = lot['_weight'] - sum(row['_pickUpFixedRate'] for row in fixed)
            weights = [(row['_id'], Fraction(row['_pickUpFixedRate'])) for row in fixed]
            if normal:
                weights.extend((row['_id'], Fraction(remainder, len(normal))) for row in normal if remainder)
            result.append((lot['_id'], weights))
        return result

    def _prize_weights(self, gacha, product, now, *, guaranteed):
        return [value for _, values in self._lot_prize_weights(gacha, product, now, guaranteed=guaranteed)
                for value in values]

    @staticmethod
    def _percent(weight, total):
        with localcontext() as context:
            context.prec = 28
            value = format(Decimal(weight.numerator) * Decimal(total.denominator) * 100
                           / (Decimal(weight.denominator) * Decimal(total.numerator)), 'f')
        return value.rstrip('0').rstrip('.') if '.' in value else value

    def _probability_data(self, gacha, product, now, *, guaranteed):
        lots = self._lot_prize_weights(gacha, product, now, guaranteed=guaranteed)
        total = sum((weight for _, values in lots for _, weight in values), Fraction())
        output, prizes = [], defaultdict(Fraction)
        for lot_id, values in lots:
            weight = sum((weight for _, weight in values), Fraction())
            if weight:
                output.append(message_field(1, integer_field(1, lot_id)
                    + string_field(2, self._percent(weight, total))))
            for prize_id, weight in values:
                prizes[prize_id] += weight
        for prize_id, weight in sorted(prizes.items()):
            if weight:
                output.append(message_field(2, integer_field(1, prize_id)
                    + string_field(2, self._percent(weight, total))))
        return b''.join(output)

    def probability(self, payload):
        gacha_id, requested_product, selected = _request(payload, require_product=False)
        gacha = self.gachas.get(gacha_id)
        if gacha is None or selected:
            raise NativeRpcError(3)
        product_ids = list(dict.fromkeys(gacha['_productId' + str(slot)] for slot in range(1, 5)
                                         if gacha['_productId' + str(slot)]))
        if requested_product and requested_product not in product_ids:
            raise NativeRpcError(3)
        now = int(self.clock())
        if not _in_period(gacha, now):
            raise NativeRpcError(9)
        normal = self._probability_data(gacha, self.products[product_ids[0]], now, guaranteed=False)
        groups = {normal: 1}
        output = []
        for product_id in product_ids:
            output.append(message_field(6, integer_field(1, product_id) + integer_field(2, 1)))
            product = self.products[product_id]
            if product['_ensuredCount']:
                ensured = self._probability_data(gacha, product, now, guaranteed=True)
                group_id = groups.setdefault(ensured, len(groups) + 1)
                output.append(message_field(7, integer_field(1, product_id) + integer_field(2, group_id)))
        output.extend(message_field(5, integer_field(1, group_id) + message_field(2, value))
                      for value, group_id in groups.items())
        return b''.join(output)

    def _bonus(self, gacha, slot, consumed):
        result = []
        for bonus_id in gacha['_gachaBonusIds' + str(slot)]:
            bonus = self.bonuses[bonus_id]
            required = bonus['_requiredGachaDrawCount']
            due = ((consumed + 1) // required - consumed // required if bonus['_bonusDrawTiming'] == 1
                   else int(consumed < required <= consumed + 1))
            weighted = [(row, Fraction(row['_weight'])) for row in self.bonus_lots[bonus_id]]
            for _ in range(due * bonus['_bonusDrawCount']):
                result.append(self._choose(weighted))
        return result

    def player_fields(self, profile_id, *, connection=None):
        """GetPlayerData fields 19 and 22, preserving original IDs and counters."""
        database = closing(sqlite3.connect(self.database)) if connection is None else nullcontext(connection)
        with database as connection:
            output = [message_field(19, scalar_message(((1, row[0]), (2, row[1]), (3, row[2]), (4, row[3]))))
                for row in connection.execute('''SELECT product_id, consumed_count, last_execute, gacha_id
                    FROM player_gacha_products WHERE profile_id = ? ORDER BY gacha_id, product_id''', (profile_id,))]
            output.extend(message_field(22, scalar_message(((1, row[0]), (2, row[1]))))
                for row in connection.execute('''SELECT gacha_id, point FROM player_gacha_points
                    WHERE profile_id = ? ORDER BY gacha_id''', (profile_id,)))
            return b''.join(output)

    def history(self, profile_id, payload):
        """Original empty HistoryRequest; execution rows retain their draw-time values."""
        try:
            if any(number < 1 for number, _ in fields(payload)):
                raise ValueError('Protobuf field zero')
        except ValueError:
            raise NativeRpcError(3) from None
        with closing(sqlite3.connect(Path(self.database).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            rows = connection.execute('''SELECT gacha_id, product_id, free_gem, paid_gem, ticket,
                executed_at, prizes FROM player_gacha_history WHERE profile_id = ?
                ORDER BY executed_at DESC, id DESC''', (profile_id,))
            output = []
            for row in rows:
                execution = scalar_message(enumerate(row[:6], 1))
                execution += b''.join(message_field(7, scalar_message(((1, prize['prize_id']),
                    (2, int(prize['converted']))))) for prize in json.loads(row[6]))
                output.append(message_field(2, execution))
            return b''.join(output)

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method == PROBABILITY_METHOD:
            return self.probability(payload)
        if method == HISTORY_METHOD:
            return self.history(player['profile_id'], payload)
        if method != EXECUTE_METHOD:
            raise NotImplementedError(method)
        gacha_id, product_id, selected = _request(payload)
        gacha, product = self.gachas.get(gacha_id), self.products.get(product_id)
        if gacha is None or product is None:
            raise NativeRpcError(3)
        slots = [slot for slot in range(1, 5) if gacha['_productId' + str(slot)] == product_id]
        if not slots or selected:
            raise NativeRpcError(3)
        now = int(self.clock())
        if not _in_period(gacha, now):
            raise NativeRpcError(9)
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            cards = member_cards(state, player['created_at'])
            previous_rates = (self.offline_bonus.capture_rates(state, now=now)
                              if self.offline_bonus is not None else None)
            costume_ids = set(state.get('character_unlocked_costumes', {}))
            consumed = self._consumed(connection, profile_id, gacha_id, product, now)
            monthly_available, monthly_free = self.rewards.entitlements.gacha_access(state, product, now)
            limit = self._effective_limit(product, monthly_available)
            if limit and consumed >= limit:
                raise NativeRpcError(9)
            cost = self._cost(gacha, product, consumed, state,
                              monthly_available=monthly_available, monthly_free=monthly_free)
            normal = self._prize_weights(gacha, product, now, guaranteed=False)
            ensured = self._prize_weights(gacha, product, now, guaranteed=True) if product['_ensuredCount'] else normal
            prize_ids = [self._choose(ensured if index >= cost['count'] - product['_ensuredCount'] else normal)
                         for index in range(cost['count'])]
            bonus = self._bonus(gacha, slots[0], consumed)
            resources, history = [], []
            seen = set(cards)
            for prize_id in prize_ids:
                prize = self.prizes[prize_id]
                key = str(prize['_resourceId'])
                converted = prize['_resourceType'] == 2 and key in seen
                if prize['_resourceType'] == 2:
                    seen.add(key)
                history.append({'prize_id': prize_id, 'converted': converted})
                resources.append((prize['_resourceType'], prize['_resourceId'], prize['_amount']))
            resources.extend((row['_bonusPrizeType'], row['_bonusPrizeId'], row['_bonusPrizeCount']) for row in bonus)
            self.rewards.apply(state, resources, now=now)
            for item_id, amount in (('1', cost['free']), ('2', cost['paid']), (str(cost['ticket_id']), cost['ticket'])):
                if amount:
                    state['items'][item_id] = state['items'].get(item_id, 0) - amount
            if monthly_free:
                self.rewards.entitlements.consume_free_gacha(state, now)
            offline_notification = (self.offline_bonus.stock_changed_rates(connection, player, state, previous_rates, now=now)
                                    if self.offline_bonus is not None else b'')
            self.progress.save(connection, profile_id, state)
            connection.execute('''INSERT INTO player_gacha_products(profile_id, gacha_id, product_id, consumed_count, last_execute)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(profile_id, gacha_id, product_id)
                DO UPDATE SET consumed_count = excluded.consumed_count, last_execute = excluded.last_execute''',
                (profile_id, gacha_id, product_id, consumed + 1, now))
            connection.execute('''INSERT INTO player_gacha_points(profile_id, gacha_id, point) VALUES (?, ?, ?)
                ON CONFLICT(profile_id, gacha_id) DO UPDATE SET point = point + excluded.point''',
                (profile_id, gacha_id, cost['point']))
            connection.execute('''INSERT INTO player_gacha_history(profile_id, gacha_id, product_id, free_gem, paid_gem,
                ticket, executed_at, prizes, bonus_lot_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (profile_id, gacha_id, product_id, cost['free'], cost['paid'], cost['ticket'], now,
                 json.dumps(history, separators=(',', ':')), json.dumps([row['_id'] for row in bonus])))
            if self.season_pass is not None:
                self.season_pass.record_counter(connection, profile_id, 3, 0, 0, cost['count'], now)
            notification = unlocked_costume_notification(costume_ids, state) + offline_notification
            return (_packed(1, prize_ids) + _packed(2, [row['_id'] for row in bonus])
                    + message_field(3, notification))

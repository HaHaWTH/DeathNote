"""Local external-payment reservations, with no payment SDK or resource delivery."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time

from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.live.single import int_value, parse_message
from deathnote.protocol.wire import integer_field
from deathnote.protocol.decode import fields


RESERVE_PAYMENT_METHOD = '/app.external_payments.ExternalPaymentsService/ReserveProvisionalPurchase'
RELEASE_PAYMENT_METHOD = '/app.external_payments.ExternalPaymentsService/ReleaseProvisionalPurchase'
NONE, LIMIT_REACHED, BY_OTHER_PAYMENT, TIME_LIMIT = range(4)


class NativePaymentReservation:
    methods = frozenset((RESERVE_PAYMENT_METHOD, RELEASE_PAYMENT_METHOD))

    def __init__(self, login, *, shop, clock=time.time):
        self.login, self.shop, self.clock = login, shop, clock
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_payment_reservations (
                profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                shop_id INTEGER NOT NULL, expires_seconds INTEGER NOT NULL,
                expires_nanos INTEGER NOT NULL, reserved_at INTEGER NOT NULL
            )''')

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            if any(number == 0 for number, _ in fields(payload)):
                raise ValueError('Invalid protobuf field zero')
        except ValueError:
            raise NativeRpcError(3) from None
        request = parse_message(payload)
        profile_id = player['profile_id']
        if method == RELEASE_PAYMENT_METHOD:
            with closing(sqlite3.connect(self.login.database)) as connection, connection:
                connection.execute('DELETE FROM player_payment_reservations WHERE profile_id=?', (profile_id,))
            return b''
        shop_id = int_value(request, 1)
        master = self.shop.shops.get(shop_id)
        if master is None or master['_paymentType'] != 16:
            raise NativeRpcError(3, diagnostic={'code': 'payment_reservation_shop'})
        timestamp = request.get(2, b'')
        if not isinstance(timestamp, bytes):
            raise NativeRpcError(3)
        timestamp = parse_message(timestamp)
        if 0 in timestamp:
            raise NativeRpcError(3)
        seconds, nanos = int_value(timestamp, 1), int_value(timestamp, 2)
        if seconds >= 1 << 63:
            seconds -= 1 << 64
        if not -62135596800 <= seconds <= 253402300799 or not 0 <= nanos < 1_000_000_000:
            raise NativeRpcError(3, diagnostic={'code': 'payment_reservation_timestamp'})
        now = int(self.clock())
        if (seconds, nanos) <= (now, 0) or not self.shop._active(master, now):
            return integer_field(1, TIME_LIMIT)
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.shop.progress.load(connection, profile_id)
            player_rank = max(row['_rank'] for row in self.shop.player_ranks if row['_exp'] <= state['rank_exp'])
            vip_point = state.get('vip', {}).get('point', 0)
            vip_rank = max(row['_vipRank'] for row in self.shop.vip_ranks if row['_point'] <= vip_point)
            if player_rank < master['_playerRank'] or vip_rank < master['_vipRank']:
                raise NativeRpcError(9, diagnostic={'code': 'payment_reservation_rank'})
            counts = self.shop._counts(connection, profile_id, shop_id, now)
            period = {1: 0, 2: 1, 3: 2, 4: 3}[master['_resetType']]
            if master['_limitConsumeCount'] and counts[period] >= master['_limitConsumeCount']:
                return integer_field(1, LIMIT_REACHED)
            previous = connection.execute('''SELECT shop_id,expires_seconds,expires_nanos
                FROM player_payment_reservations WHERE profile_id=?''', (profile_id,)).fetchone()
            if previous is not None and (previous[1], previous[2]) > (now, 0) and previous[0] != shop_id:
                return integer_field(1, BY_OTHER_PAYMENT)
            connection.execute('''INSERT INTO player_payment_reservations VALUES(?,?,?,?,?)
                ON CONFLICT(profile_id) DO UPDATE SET shop_id=excluded.shop_id,
                expires_seconds=excluded.expires_seconds, expires_nanos=excluded.expires_nanos,
                reserved_at=excluded.reserved_at''', (profile_id, shop_id, seconds, nanos, now))
        return integer_field(1, NONE)

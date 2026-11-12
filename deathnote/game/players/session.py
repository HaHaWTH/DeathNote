"""The 1.1.0 PlayerService.Login daily refresh over shared persistent state.

The TW/HK/MO APK retains its Bili login / GetPlayerData / Home flow. This
additional RPC shares those services' daily login bookkeeping and present box;
it never creates an independent daily reward counter or auto-claims VIP points.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time

from deathnote.game.progression.login_bonus import posted_notification, sheet_message
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.commerce.vip import vip_message
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields


PLAYER_SESSION_LOGIN_METHOD = '/app.player.PlayerService/Login'


def comeback_message(state):
    """Read existing Comeback values; eligibility generation belongs to its policy."""
    comeback = state.get('comeback', {})
    return b''.join(integer_field(number, comeback[key])
                    for number, key in enumerate(
                        ('last_login_date', 'comeback_at', 'comeback_days'), 1)
                    if comeback.get(key, 0))


class NativePlayerSession:
    methods = frozenset((PLAYER_SESSION_LOGIN_METHOD,))

    def __init__(self, login, *, progress, login_bonus, vip, clock=time.time):
        self.login = login
        self.database = login.database
        self.progress = progress
        self.login_bonus = login_bonus
        self.vip = vip
        self.clock = clock

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            if any(number == 0 for number, _ in fields(payload)):
                raise ValueError('Invalid protobuf field zero')
        except ValueError:
            raise NativeRpcError(3) from None
        now = int(self.clock())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            self.vip.refresh_login(state, now=now)
            received, _, posted = self.login_bonus.update(state, now=now)
            response = (
                b''.join(message_field(1, sheet_message(value)) for value in received)
                + message_field(2, comeback_message(state))
                + message_field(3, vip_message(state))
                + message_field(4, posted_notification(posted))
            )
            self.progress.save(connection, player['profile_id'], state)
        return response

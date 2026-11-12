"""Convert expired gacha exchange points using the APK's automatic-conversion flow."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress
from deathnote.game.progression.rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields, varint


CONVERT_POINT_METHOD = '/app.gacha.GachaService/ConvertPoint'
_JST = timezone(timedelta(hours=9))


def _exchange_ids(payload):
    result = set()
    try:
        for number, value in fields(payload):
            if number < 1:
                raise ValueError('Protobuf field zero')
            if number != 1:
                continue
            values = []
            if isinstance(value, bytes):
                position = 0
                while position < len(value):
                    item, position = varint(value, position)
                    values.append(item)
            else:
                values.append(value)
            if any(type(item) is not int or not 0 < item <= 0x7fffffffffffffff for item in values):
                raise ValueError('Invalid gacha exchange ID')
            result.update(values)
    except ValueError:
        raise NativeRpcError(3) from None
    if not result:
        raise NativeRpcError(3)
    return sorted(result)


class NativeGachaConvert:
    methods = frozenset((CONVERT_POINT_METHOD,))

    def __init__(self, login, master_directory, *, progress=None, rewards=None, clock=time.time):
        self.login = login
        self.database = login.database
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        self.clock = clock
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.exchanges = {row['_id']: row for row in rows('MasterExchange')}
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.rate = int(parameters['gacha_point_conversion_to_coin_rate'])
        self.item_id = int(parameters['gacha_point_convert_item_id'])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_gacha_points (
                profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                gacha_id INTEGER NOT NULL, point INTEGER NOT NULL,
                PRIMARY KEY(profile_id, gacha_id)
            )''')

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        selected = _exchange_ids(payload)
        now = int(self.clock())
        gacha_ids = set()
        for exchange_id in selected:
            exchange = self.exchanges.get(exchange_id)
            if exchange is None or exchange['_paymentResourceType'] != 7:
                raise NativeRpcError(3)
            end = exchange['_endAt']
            if not end or now < int(datetime.strptime(end, '%Y-%m-%d %H:%M:%S').replace(tzinfo=_JST).timestamp()):
                raise NativeRpcError(9)
            gacha_ids.add(exchange['_paymentResourceId'])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            points = 0
            for gacha_id in sorted(gacha_ids):
                row = connection.execute('''SELECT point FROM player_gacha_points
                    WHERE profile_id = ? AND gacha_id = ?''', (player['profile_id'], gacha_id)).fetchone()
                if row is not None:
                    points += row[0]
                    connection.execute('''UPDATE player_gacha_points SET point = 0
                        WHERE profile_id = ? AND gacha_id = ?''', (player['profile_id'], gacha_id))
            before = state['items'].get(str(self.item_id), 0)
            if points:
                self.rewards.apply(state, ((1, self.item_id, points * self.rate),), now=now)
            self.progress.save(connection, player['profile_id'], state)
            added = state['items'].get(str(self.item_id), 0) - before
            notification = (message_field(4, integer_field(1, self.item_id) + integer_field(2, added))
                            if added else b'')
        return message_field(1, notification)

"""Persistent present box used by original-client login rewards."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.progress import NativeLiveProgress
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields, varint


FETCH_PRESENT_METHOD = '/app.present.PresentService/Fetch'
OPEN_PRESENT_METHOD = '/app.present.PresentService/Open'
PRESENT_HISTORY_METHOD = '/app.present.PresentService/History'


def resource_message(value):
    return b''.join(integer_field(number, value[key]) for number, key in enumerate((
        'resource_type', 'resource_id', 'resource_count'), 1))


def title_message(value):
    return (integer_field(1, value['present_type_id'])
            + string_field(2, value['param_1']) + string_field(3, value['param_2']))


def present_message(value):
    return (integer_field(1, value['present_id']) + integer_field(2, value['post_at'])
            + (integer_field(3, value['expire_at']) if value['expire_at'] is not None else b'')
            + message_field(4, resource_message(value))
            + message_field(5, title_message(value)))


def history_message(value):
    return (integer_field(1, value['present_id']) + integer_field(2, value['post_at'])
            + integer_field(3, value['got_at'])
            + message_field(4, resource_message(value))
            + message_field(5, title_message(value))
            + message_field(6, b''))


def _request_ids(payload):
    result = []
    try:
        for number, value in fields(payload):
            if number == 0:
                raise ValueError('Invalid protobuf field zero')
            if number != 1:
                continue
            if isinstance(value, bytes):
                position = 0
                while position < len(value):
                    present_id, position = varint(value, position)
                    if not 0 < present_id <= 0xffffffffffffffff:
                        raise ValueError('Invalid present ID')
                    result.append(present_id)
            else:
                if not 0 < value <= 0xffffffffffffffff:
                    raise ValueError('Invalid present ID')
                result.append(value)
    except ValueError:
        raise NativeRpcError(3) from None
    return list(dict.fromkeys(result))


class NativePresent:
    methods = frozenset((FETCH_PRESENT_METHOD, OPEN_PRESENT_METHOD, PRESENT_HISTORY_METHOD))

    def __init__(self, login, master_directory, *, rewards, clock=time.time):
        self.login = login
        self.database = login.database
        self.progress = NativeLiveProgress(login)
        self.rewards = rewards
        self.clock = clock
        self.present_types = {row['_id']: row for row in json.loads(
            (Path(master_directory) / 'MasterPresentType.json').read_text('utf8'))['_allData']}

    def post_login_rewards(self, state, resources, master, login_days, *, now):
        present_type = self.present_types[3]
        box = state.setdefault('present_box', {})
        posted = []
        for resource_type, resource_id, count in resources:
            present_id = state.get('last_present_id', 0) + 1
            state['last_present_id'] = present_id
            value = {'present_id': present_id, 'post_at': now,
                     'expire_at': now + present_type['_expireDays'] * 86400,
                     'resource_type': resource_type, 'resource_id': resource_id,
                     'resource_count': count, 'present_type_id': 3,
                     'param_1': master['_nameTextID'], 'param_2': str(login_days)}
            box[str(present_id)] = value
            posted.append(value)
        return posted

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        now = int(self.clock())
        if method == OPEN_PRESENT_METHOD:
            return self.open(player['profile_id'], _request_ids(payload), now)
        state = self.progress.get(player['profile_id'])
        if method == PRESENT_HISTORY_METHOD:
            history = state.get('present_history', {}).values()
            return b''.join(message_field(1, history_message(value)) for value in sorted(
                history, key=lambda value: (value['got_at'], value['present_id']), reverse=True))
        return b''.join(message_field(1, present_message(value)) for _, value in sorted(
            state.get('present_box', {}).items(), key=lambda row: int(row[0]), reverse=True)
            if value['expire_at'] is None or value['expire_at'] > now)

    def open(self, profile_id, present_ids, now):
        unreceived = []
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            box = state.setdefault('present_box', {})
            history = state.setdefault('present_history', {})
            for present_id in present_ids:
                key = str(present_id)
                value = box.get(key)
                if value is None:
                    if key not in history:
                        unreceived.append(integer_field(1, present_id))
                    continue
                reason = 0
                if value['expire_at'] is not None and value['expire_at'] <= now:
                    reason = 2
                elif value['resource_type'] == 1:
                    maximum = self.rewards.item_masters[value['resource_id']]['_max']
                    if state['items'].get(str(value['resource_id']), 0) + value['resource_count'] > maximum:
                        reason = 1
                if reason:
                    unreceived.append(integer_field(1, present_id) + integer_field(2, reason))
                    continue
                self.rewards.apply(state, ((value['resource_type'], value['resource_id'],
                                            value['resource_count']),), now=now)
                history[key] = {**value, 'got_at': now}
                del box[key]
            self.progress.save(connection, profile_id, state)
        return b''.join(message_field(1, value) for value in unreceived) + message_field(2, b'')

"""Daily login sheets and awards from the current public login-bonus masters."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.calendar import NativeServiceCalendar
from deathnote.game.players.progress import NativeLiveProgress
from .present import present_message
from deathnote.protocol.wire import integer_field, message_field


UPDATE_LOGIN_BONUS_METHOD = '/app.loginbonus.LoginBonusService/Update'
SERVICE_TIMEZONE = timezone(timedelta(hours=9))


def sheet_message(value):
    return b''.join(integer_field(number, value[key])
                    for number, key in enumerate((
                        'login_bonus_id', 'sheet_no', 'slot_no', 'login_days'), 1))


def player_fields(received):
    return b''.join(message_field(23, sheet_message(value)) for value in received)


def posted_notification(posted):
    return b''.join(message_field(1, present_message(value)) for value in posted)


def _timestamp(value):
    return int(datetime.strptime(value, '%Y/%m/%d %H:%M:%S')
               .replace(tzinfo=SERVICE_TIMEZONE).timestamp())


class NativeLoginBonus:
    methods = frozenset((UPDATE_LOGIN_BONUS_METHOD,))

    def __init__(self, login, master_directory, *, presents, clock=time.time):
        self.login = login
        self.database = login.database
        self.progress = NativeLiveProgress(login)
        self.presents = presents
        self.clock = clock
        directory = Path(master_directory)
        self.calendar = NativeServiceCalendar(directory)
        self.masters = {row['_id']: row for row in json.loads(
            (directory / 'MasterLoginBonus.json').read_text('utf8'))['_allData']}
        self.windows = {key: (_timestamp(row['_startAt']) if row['_startAt'] else None,
                              _timestamp(row['_endAt']) if row['_endAt'] else None)
                        for key, row in self.masters.items()}
        grouped = defaultdict(lambda: defaultdict(list))
        for row in json.loads((directory / 'MasterLoginBonusSlot.json').read_text('utf8'))['_allData']:
            grouped[row['_loginBonusID']][(row['_sheetNo'], row['_slotNo'])].append(
                (row['_resourceType'], row['_resourceId'], row['_resourceCount']))
        self.slots = {key: sorted(positions.items()) for key, positions in grouped.items()}

    def _restore_receive_time(self, state, previous, master):
        if previous.get('last_receive_at') is None:
            receipts = [gift['post_at']
                for collection in ('present_box', 'present_history')
                for gift in state.get(collection, {}).values()
                if gift.get('present_type_id') == 3
                and gift.get('param_1') == master['_nameTextID']
                and gift.get('param_2') == str(previous['login_days'])]
            if receipts:
                previous['last_receive_at'] = max(receipts)
        if previous.get('last_receive_at') is not None:
            previous['last_receive_day'] = self.calendar.day(previous['last_receive_at'])

    def update(self, state, *, now):
        """Post each newly due daily slot inside the caller's transaction."""
        day = self.calendar.day(now)
        received, not_received, posted = [], [], []
        sheets = state.setdefault('login_bonuses', {})
        for bonus_id, master in sorted(self.masters.items()):
            key = str(bonus_id)
            previous = sheets.get(key)
            if previous is not None:
                self._restore_receive_time(state, previous, master)
            start, end = self.windows[bonus_id]
            if (start is not None and now < start) or (end is not None and now >= end):
                continue
            count = previous['login_days'] if previous is not None else 0
            slots = self.slots[bonus_id]
            if ((previous is not None and previous['last_receive_day'] >= day)
                    or (not master['_isLoop'] and count >= len(slots))):
                not_received.append(previous)
                continue
            (sheet_no, slot_no), resources = slots[count % len(slots)]
            value = {'login_bonus_id': bonus_id, 'sheet_no': sheet_no,
                     'slot_no': slot_no, 'login_days': count + 1,
                     'last_receive_day': day, 'last_receive_at': now}
            posted.extend(self.presents.post_login_rewards(state, resources, master,
                                                           count + 1, now=now))
            sheets[key] = value
            received.append(value)
        return received, not_received, posted

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            received, not_received, posted = self.update(state, now=int(self.clock()))
            self.progress.save(connection, player['profile_id'], state)
        return (b''.join(message_field(1, sheet_message(value)) for value in received)
                + b''.join(message_field(2, sheet_message(value)) for value in not_received)
                + message_field(3, posted_notification(posted)))

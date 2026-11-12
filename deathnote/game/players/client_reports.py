"""Persist diagnostic reports, including pre-login reports allowed by the APK."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field
from deathnote.protocol.decode import fields


REPORT_LOG_METHOD = '/app.playerext.PlayerExtService/ReportLog'


class NativeClientReports:
    methods = frozenset((REPORT_LOG_METHOD,))

    def __init__(self, login, *, clock=time.time):
        self.login, self.clock = login, clock
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_client_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER REFERENCES players(profile_id),
                tag TEXT NOT NULL, content TEXT NOT NULL, received_at INTEGER NOT NULL
            )''')

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        try:
            profile_id = self.login.authenticate(headers)['profile_id']
        except NativeRpcError as error:
            if error.status != 16:
                raise
            profile_id = None
        try:
            rows = fields(payload)
            if any(number == 0 for number, _ in rows):
                raise ValueError('Invalid protobuf field zero')
            request = dict(rows)
            tag, content = request.get(1, b''), request.get(2, b'')
            if not isinstance(tag, bytes) or not isinstance(content, bytes):
                raise ValueError('Expected string fields')
            tag, content = tag.decode('utf8'), content.decode('utf8')
        except (ValueError, UnicodeError):
            raise NativeRpcError(3) from None
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('''INSERT INTO player_client_reports
                (profile_id,tag,content,received_at) VALUES(?,?,?,?)''',
                (profile_id, tag, content, int(self.clock())))
        return integer_field(1, 1)

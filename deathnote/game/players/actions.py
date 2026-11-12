"""Persist original-client help/unlock acknowledgements and profile names."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields, varint


SHOWN_HELP_METHOD = '/app.carousel_help.CarouselHelpService/Shown'
SHOWN_CONTENT_UNLOCK_METHOD = '/app.content_unlock.ContentUnlockService/Shown'
EDIT_PROFILE_METHOD = '/app.player.PlayerService/EditProfile'


def _shown_ids(payload: bytes) -> set[int]:
    master_ids = set()
    for number, value in fields(payload):
        if number != 1:
            continue
        if isinstance(value, bytes):
            pos = 0
            while pos < len(value):
                master_id, pos = varint(value, pos)
                master_ids.add(master_id)
        else:
            master_ids.add(value)
    if any(master_id < 1 or master_id > 0x7fffffffffffffff for master_id in master_ids):
        raise ValueError('Shown master ID is outside the positive int64 range')
    return master_ids


class NativePlayerActions:
    methods = frozenset((SHOWN_HELP_METHOD, SHOWN_CONTENT_UNLOCK_METHOD, EDIT_PROFILE_METHOD))

    def __init__(self, login):
        self.login = login
        self.database = login.database
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_shown_carousel_helps (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    master_id INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, master_id)
                );
                CREATE TABLE IF NOT EXISTS player_profile_edits (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    name TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_shown_content_unlocks (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    master_id INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, master_id)
                );
            ''')

    def dispatch(self, method: str, payload: bytes, headers: dict[str, str]) -> bytes:
        player = self.login.authenticate(headers)
        profile_id = player['profile_id']
        if method in (SHOWN_HELP_METHOD, SHOWN_CONTENT_UNLOCK_METHOD):
            try:
                master_ids = _shown_ids(payload)
            except ValueError:
                raise NativeRpcError(3) from None
            table = ('player_shown_carousel_helps' if method == SHOWN_HELP_METHOD
                     else 'player_shown_content_unlocks')
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.executemany(f'''
                    INSERT INTO {table}(profile_id, master_id)
                    VALUES (?, ?) ON CONFLICT(profile_id, master_id) DO NOTHING
                ''', ((profile_id, master_id) for master_id in sorted(master_ids)))
            return b''
        if method == EDIT_PROFILE_METHOD:
            name = ''
            try:
                for number, value in fields(payload):
                    if number == 1:
                        if not isinstance(value, bytes):
                            raise ValueError('Profile name has the wrong wire type')
                        name = value.decode('utf8')
            except ValueError:
                raise NativeRpcError(3) from None
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute('''
                    INSERT INTO player_profile_edits(profile_id, name, updated_at)
                    VALUES (?, ?, ?) ON CONFLICT(profile_id) DO UPDATE SET
                        name = excluded.name, updated_at = excluded.updated_at
                ''', (profile_id, name, int(time.time())))
            return b''
        raise NotImplementedError(method)

    def get_profile(self, profile_id: int, default_name: str) -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute('''
                SELECT name, updated_at FROM player_profile_edits WHERE profile_id = ?
            ''', (profile_id,)).fetchone()
        if row is None:
            return {'name': default_name, 'updated_at': 0}
        return {'name': row[0], 'updated_at': row[1]}

    def shown_fields(self, profile_id: int) -> bytes:
        with closing(sqlite3.connect(self.database)) as connection:
            helps = connection.execute('''
                SELECT master_id FROM player_shown_carousel_helps
                WHERE profile_id = ? ORDER BY master_id
            ''', (profile_id,)).fetchall()
            unlocks = connection.execute('''
                SELECT master_id FROM player_shown_content_unlocks
                WHERE profile_id = ? ORDER BY master_id
            ''', (profile_id,)).fetchall()
        return (b''.join(message_field(33, integer_field(1, row[0])) for row in helps)
                + b''.join(message_field(39, integer_field(1, row[0])) for row in unlocks))

"""Persist local players for the original client's recovered login protocol."""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from deathnote.protocol.wire import (integer_field as _integer_field,
                          message_field as _message_field, string_field as _string_field)
from deathnote.protocol.decode import fields
from deathnote.protocol.rpc import NativeRpcError


PRELOGIN_METHOD = '/app.playerlogin.PlayerLoginService/PlayerPreLogin'
LOGIN_METHOD = '/app.playerlogin.PlayerLoginService/PlayerLogin'


class NativePlayerLogin:
    methods = frozenset((PRELOGIN_METHOD, LOGIN_METHOD))

    def __init__(self, state_dir: Path, *, server_id: str, server_name: str):
        self.database = Path(state_dir) / 'players.sqlite3'
        self.server_id = server_id
        self.server_name = server_name
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS players (
                    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_hash TEXT NOT NULL UNIQUE,
                    player_id TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_hash TEXT PRIMARY KEY,
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    device_id TEXT NOT NULL UNIQUE,
                    credential_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_deck_state (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    main_deck INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_decks (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    deck_id INTEGER NOT NULL,
                    wire BLOB NOT NULL,
                    PRIMARY KEY (profile_id, deck_id)
                );
            ''')

    def dispatch(self, method: str, payload: bytes) -> bytes:
        try:
            values = dict(fields(payload))
        except ValueError:
            raise NativeRpcError(3) from None
        sdk_uid = values.get(1, b'')
        if not isinstance(sdk_uid, bytes):
            raise NativeRpcError(3)
        account_hash = hashlib.sha256(sdk_uid).hexdigest()
        if method == PRELOGIN_METHOD:
            with closing(sqlite3.connect(self.database)) as connection:
                exists = connection.execute(
                    'SELECT 1 FROM players WHERE account_hash = ?', (account_hash,)).fetchone() is not None
            return _integer_field(1, 1) if sdk_uid and exists else b''
        if method != LOGIN_METHOD:
            raise NotImplementedError(method)
        if not sdk_uid:
            raise NativeRpcError(3)
        try:
            device = dict(fields(values.get(7, b'')))
        except (TypeError, ValueError):
            raise NativeRpcError(3) from None
        identifier = device.get(2, b'')
        if not isinstance(identifier, bytes):
            raise NativeRpcError(3)
        device_hash = hashlib.sha256(account_hash.encode('ascii') + b'\0' + identifier).hexdigest()
        credential = secrets.token_urlsafe(32)
        credential_hash = hashlib.sha256(credential.encode('ascii')).hexdigest()
        now = int(time.time())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            player = connection.execute(
                'SELECT profile_id, player_id FROM players WHERE account_hash = ?', (account_hash,)).fetchone()
            is_new_user = player is None
            if is_new_user:
                player_id = str(uuid.uuid4())
                cursor = connection.execute(
                    'INSERT INTO players(account_hash, player_id, created_at) VALUES (?, ?, ?)',
                    (account_hash, player_id, now))
                player = (cursor.lastrowid, player_id)
            profile_id, player_id = player
            previous_device = connection.execute(
                'SELECT device_id FROM devices WHERE device_hash = ?', (device_hash,)).fetchone()
            device_id = previous_device[0] if previous_device else str(uuid.uuid4())
            connection.execute('''
                INSERT INTO devices(device_hash, profile_id, device_id, credential_hash, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_hash) DO UPDATE SET
                    credential_hash = excluded.credential_hash,
                    updated_at = excluded.updated_at
            ''', (device_hash, profile_id, device_id, credential_hash, now))
        player_credential = (_string_field(1, player_id) + _string_field(2, credential)
                             + _string_field(3, device_id) + _integer_field(4, profile_id))
        return (_message_field(1, player_credential) + _string_field(2, self.server_id)
                + _string_field(3, self.server_name) + (_integer_field(4, 1) if is_new_user else b''))

    def authenticate(self, headers: dict[str, str]) -> dict:
        player_id = headers.get('x-player-id', '')
        credential = headers.get('x-player-credential', '')
        if not player_id or not credential:
            raise NativeRpcError(16)
        digest = hashlib.sha256(credential.encode('utf8')).hexdigest()
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute('''
                SELECT p.profile_id, p.player_id, p.created_at
                FROM players AS p JOIN devices AS d ON d.profile_id = p.profile_id
                WHERE p.player_id = ? AND d.credential_hash = ?
            ''', (player_id, digest)).fetchone()
        if row is None:
            raise NativeRpcError(16)
        return {'profile_id': row[0], 'player_id': row[1], 'created_at': row[2]}

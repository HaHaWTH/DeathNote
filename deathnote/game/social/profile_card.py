"""Persist native name cards, their local thumbnails and player favorites."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from deathnote.paths import ROOT

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields


FAVORITE_STATUS_METHOD = '/app.player.PlayerService/GetPlayerFavoriteStatus'
UPDATE_FAVORITE_METHOD = '/app.player.PlayerService/UpdatePlayerFavorite'
PROFILE_PREFIX = '/app.profilecard.ProfileCardService/'
OBJECT_PREFIX = '/profile-card/objects/'


def _string(value):
    if not isinstance(value, bytes):
        raise ValueError('Expected a protobuf string')
    return value.decode('utf8')


def _int32(value):
    if type(value) is not int or not 0 <= value <= 0xffffffffffffffff:
        raise ValueError('Expected a protobuf int32')
    value &= 0xffffffff
    return value - 0x100000000 if value & 0x80000000 else value


def _orders(payload):
    result = []
    for number, value in fields(payload):
        if number != 2:
            continue
        if isinstance(value, int):
            result.append(_int32(value))
        else:
            index = 0
            while index < len(value):
                integer, shift = 0, 0
                while True:
                    byte = value[index]
                    index += 1
                    integer |= (byte & 127) << shift
                    if byte < 128:
                        break
                    shift += 7
                    if shift >= 70:
                        raise ValueError('Malformed packed int32')
                result.append(_int32(integer))
    return result


def _object_message(index, text, url, order):
    return (integer_field(1, index) + string_field(2, text)
            + string_field(3, url) + integer_field(4, order))


def _image_content_type(body, content_type):
    if body.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if body.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if body[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if body.startswith(b'RIFF') and body[8:12] == b'WEBP':
        return 'image/webp'
    return content_type or 'application/octet-stream'


class NativeProfileCard:
    methods = frozenset((FAVORITE_STATUS_METHOD, UPDATE_FAVORITE_METHOD,
        *(PROFILE_PREFIX + name for name in ('SaveProfileCard', 'EditProfileCardSlot',
            'SettingProfileCard', 'PutObjects', 'UpdateProfileCardDispOrder', 'RemoveProfileCard'))))

    def __init__(self, login, master_directory, *, thumbnail_base_url, clock=time.time):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.thumbnail_base_url = thumbnail_base_url.rstrip('/')
        parameters = json.loads((Path(master_directory) / 'MasterParameter.json').read_text('utf8'))['_allData']
        self.slot_count = int(next(row['_value'] for row in parameters
                                   if row['_id'] == 'profile_card_initial_slot_count'))
        self.placeholder = ROOT / 'native-server/assets/missing-image.png'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_profile_card_slots (
                    profile_id INTEGER NOT NULL, slot INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '', is_setting INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(profile_id, slot)
                );
                CREATE TABLE IF NOT EXISTS player_profile_card_objects (
                    profile_id INTEGER NOT NULL, slot INTEGER NOT NULL,
                    object_index INTEGER NOT NULL, profile_object_json TEXT NOT NULL,
                    thumbnail_id TEXT NOT NULL, disp_order INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, slot, object_index)
                );
                CREATE TABLE IF NOT EXISTS player_profile_card_thumbnails (
                    object_id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL,
                    object_key TEXT NOT NULL, body BLOB NOT NULL, content_type TEXT NOT NULL,
                    UNIQUE(profile_id, object_key)
                );
                CREATE TABLE IF NOT EXISTS player_profile_card_favorites (
                    sender_profile_id INTEGER NOT NULL, target_player_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(sender_profile_id, target_player_id)
                );
            ''')

    def _initialize_slots(self, connection, profile_id):
        connection.executemany('''
            INSERT OR IGNORE INTO player_profile_card_slots(profile_id, slot) VALUES (?, ?)
        ''', ((profile_id, slot) for slot in range(1, self.slot_count + 1)))

    def _thumbnail(self, connection, profile_id, key):
        row = connection.execute('''
            SELECT object_id FROM player_profile_card_thumbnails
            WHERE profile_id = ? AND object_key = ?
        ''', (profile_id, key)).fetchone()
        if row is not None:
            return row[0]
        object_id = uuid.uuid4().hex
        connection.execute('''
            INSERT INTO player_profile_card_thumbnails
                (object_id, profile_id, object_key, body, content_type) VALUES (?, ?, ?, ?, ?)
        ''', (object_id, profile_id, key, self.placeholder.read_bytes(), 'image/png'))
        return object_id

    def _url(self, object_id):
        return self.thumbnail_base_url + OBJECT_PREFIX + object_id

    def fields(self, connection, profile_id):
        """Return outer PlayerData field 51 and the inner MyProfile field 8 value."""
        self._initialize_slots(connection, profile_id)
        result, selected = [], None
        for slot, name, setting in connection.execute('''
            SELECT slot, name, is_setting FROM player_profile_card_slots
            WHERE profile_id = ? ORDER BY slot
        ''', (profile_id,)):
            objects = connection.execute('''
                SELECT object_index, profile_object_json, thumbnail_id, disp_order
                FROM player_profile_card_objects WHERE profile_id = ? AND slot = ?
                ORDER BY disp_order, object_index
            ''', (profile_id, slot)).fetchall()
            detail = integer_field(1, slot) + string_field(2, name)
            if setting:
                detail += integer_field(3, 1)
                selected = (integer_field(1, slot) + string_field(2, name)
                            + b''.join(string_field(3, self._url(row[2])) for row in objects))
            detail += b''.join(message_field(4, _object_message(
                index, text, self._url(thumbnail), order)) for index, text, thumbnail, order in objects)
            result.append(message_field(51, detail))
        return b''.join(result), selected

    def http_object(self, path):
        match = re.fullmatch(re.escape(OBJECT_PREFIX) + r'([0-9a-f]{32})', urlsplit(path).path)
        if match is None:
            return None
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute('''
                SELECT body, content_type FROM player_profile_card_thumbnails WHERE object_id = ?
            ''', (match.group(1),)).fetchone()
        if row is None:
            return None
        body = bytes(row[0])
        return body, _image_content_type(body, row[1])

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method not in self.methods:
            raise NotImplementedError(method)
        try:
            values = dict(fields(payload))
            if method in (FAVORITE_STATUS_METHOD, UPDATE_FAVORITE_METHOD):
                target = _string(values.get(1, b''))
                if not target:
                    raise ValueError('Missing target player')
                cancel = _int32(values.get(2, 0)) != 0
            elif method == PROFILE_PREFIX + 'PutObjects':
                uploads = []
                for number, value in fields(payload):
                    if number != 1:
                        continue
                    item = dict(fields(value))
                    key = _string(item.get(1, b''))
                    body = item.get(2, b'')
                    content_type = _string(item.get(3, b''))
                    if not key or not isinstance(body, bytes) or '\r' in content_type or '\n' in content_type:
                        raise ValueError('Invalid thumbnail object')
                    uploads.append((key, body, _image_content_type(body, content_type)))
            else:
                slot = _int32(values.get(1, 0))
                if not 1 <= slot <= self.slot_count:
                    raise ValueError('Invalid profile-card slot')
                if method == PROFILE_PREFIX + 'SaveProfileCard':
                    index = _int32(values.get(2, 0))
                    text = _string(values.get(3, b''))
                    key = _string(values.get(4, b''))
                    order = _int32(values.get(5, 0))
                    if index <= 0 or not key:
                        raise ValueError('Invalid profile-card object')
                    json.loads(text)
                elif method == PROFILE_PREFIX + 'EditProfileCardSlot':
                    name = _string(values.get(2, b''))
                elif method == PROFILE_PREFIX + 'SettingProfileCard':
                    setting = _int32(values.get(2, 0)) != 0
                elif method == PROFILE_PREFIX + 'UpdateProfileCardDispOrder':
                    orders = _orders(payload)
                elif method == PROFILE_PREFIX + 'RemoveProfileCard':
                    index = _int32(values.get(2, 0))
        except (ValueError, TypeError, IndexError):
            raise NativeRpcError(3) from None
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if method == FAVORITE_STATUS_METHOD:
                total = connection.execute('''
                    SELECT COUNT(*) FROM player_profile_card_favorites WHERE target_player_id = ?
                ''', (target,)).fetchone()[0]
                sent = connection.execute('''
                    SELECT 1 FROM player_profile_card_favorites
                    WHERE sender_profile_id = ? AND target_player_id = ?
                ''', (profile_id, target)).fetchone() is not None
                return integer_field(1, total) + integer_field(2, int(sent)) if total or sent else b''
            if method == UPDATE_FAVORITE_METHOD:
                exists = connection.execute('SELECT 1 FROM players WHERE player_id = ?', (target,)).fetchone()
                if exists is None:
                    raise NativeRpcError(5)
                if cancel:
                    connection.execute('''
                        DELETE FROM player_profile_card_favorites
                        WHERE sender_profile_id = ? AND target_player_id = ?
                    ''', (profile_id, target))
                else:
                    connection.execute('''
                        INSERT OR IGNORE INTO player_profile_card_favorites
                            (sender_profile_id, target_player_id, created_at) VALUES (?, ?, ?)
                    ''', (profile_id, target, int(self.clock())))
                return b''
            self._initialize_slots(connection, profile_id)
            if method == PROFILE_PREFIX + 'PutObjects':
                urls = []
                for key, body, content_type in uploads:
                    thumbnail = self._thumbnail(connection, profile_id, key)
                    connection.execute('''
                        UPDATE player_profile_card_thumbnails SET body = ?, content_type = ?
                        WHERE object_id = ?
                    ''', (body, content_type, thumbnail))
                    urls.append(string_field(2, self._url(thumbnail)))
                return b''.join(urls)
            if method == PROFILE_PREFIX + 'SaveProfileCard':
                thumbnail = self._thumbnail(connection, profile_id, key)
                connection.execute('''
                    INSERT INTO player_profile_card_objects
                        (profile_id, slot, object_index, profile_object_json, thumbnail_id, disp_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, slot, object_index) DO UPDATE SET
                        profile_object_json = excluded.profile_object_json,
                        thumbnail_id = excluded.thumbnail_id, disp_order = excluded.disp_order
                ''', (profile_id, slot, index, text, thumbnail, order))
                return (integer_field(1, slot) + message_field(2, _object_message(
                    index, text, self._url(thumbnail), order)) + message_field(3, b''))
            if method == PROFILE_PREFIX + 'EditProfileCardSlot':
                connection.execute('''UPDATE player_profile_card_slots SET name = ?
                    WHERE profile_id = ? AND slot = ?''', (name, profile_id, slot))
            elif method == PROFILE_PREFIX + 'SettingProfileCard':
                connection.execute('''UPDATE player_profile_card_slots
                    SET is_setting = (slot = ? AND ?) WHERE profile_id = ?''', (slot, setting, profile_id))
            elif method == PROFILE_PREFIX + 'UpdateProfileCardDispOrder':
                order = 1
                for index in orders:
                    changed = connection.execute('''UPDATE player_profile_card_objects SET disp_order = ?
                        WHERE profile_id = ? AND slot = ? AND object_index = ?''',
                        (order, profile_id, slot, index)).rowcount
                    if changed:
                        order += 1
            elif method == PROFILE_PREFIX + 'RemoveProfileCard':
                connection.execute('''DELETE FROM player_profile_card_objects
                    WHERE profile_id = ? AND slot = ? AND object_index = ?''', (profile_id, slot, index))
        return b''

"""Original stamp favorites, stored in the authenticated player's progress."""
from __future__ import annotations

from contextlib import closing
import sqlite3

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields, varint


UPDATE_STAMP_FAVORITES_METHOD = '/app.stamp.StampService/UpdateStampFavorites'
UPDATE_STAMP_FAVORITE_NAME_METHOD = '/app.stamp.StampService/UpdateStampFavoriteName'

FAVORITE_GROUP_COUNT = 20
FAVORITE_SLOT_COUNT = 20
FAVORITE_NAME_MAX_LENGTH = 6


def stamp_favorite_name_fields(state):
    return b''.join(message_field(38, integer_field(1, int(favorite_id)) + string_field(2, name))
                    for favorite_id, name in sorted(state.get('stamp_favorite_names', {}).items(),
                                                    key=lambda row: int(row[0])))


def _read_favorites(payload):
    result = {}
    for number, value in fields(payload):
        if number != 1:
            continue
        if not isinstance(value, bytes):
            raise ValueError('Stamp favorites entry requires a message')
        favorite_id, stamp_ids = 0, []
        for field, entry in fields(value):
            if field == 1:
                if not isinstance(entry, int):
                    raise ValueError('Favorite ID requires an int32')
                favorite_id = entry
            elif field == 2:
                if isinstance(entry, bytes):
                    position = 0
                    while position < len(entry):
                        stamp_id, position = varint(entry, position)
                        stamp_ids.append(stamp_id)
                else:
                    stamp_ids.append(entry)
        if favorite_id in result:
            raise ValueError('Favorite group is repeated')
        result[favorite_id] = stamp_ids
    return result


def _read_name(payload):
    favorite_id, name = 0, ''
    for number, value in fields(payload):
        if number == 1:
            if not isinstance(value, int):
                raise ValueError('Favorite ID requires an int32')
            favorite_id = value
        elif number == 2:
            if not isinstance(value, bytes):
                raise ValueError('Favorite name requires a string')
            name = value.decode('utf8')
    return favorite_id, name


class NativeStamp:
    methods = frozenset((UPDATE_STAMP_FAVORITES_METHOD, UPDATE_STAMP_FAVORITE_NAME_METHOD))

    def __init__(self, login, progress, entitlements):
        self.login = login
        self.progress = progress
        self.entitlements = entitlements

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        if method == UPDATE_STAMP_FAVORITES_METHOD:
            try:
                updates = _read_favorites(payload)
            except ValueError:
                raise NativeRpcError(3, diagnostic={'code': 'stamp_favorites_wire'}) from None
            return self.update_favorites(player['profile_id'], updates)
        try:
            favorite_id, name = _read_name(payload)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_name_wire'}) from None
        return self.update_name(player['profile_id'], favorite_id, name)

    def update_favorites(self, profile_id, updates):
        for favorite_id, stamp_ids in updates.items():
            if not 1 <= favorite_id <= FAVORITE_GROUP_COUNT:
                raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_id'})
            if len(stamp_ids) > FAVORITE_SLOT_COUNT:
                raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_slots'})
            selected = [stamp_id for stamp_id in stamp_ids if stamp_id]
            if len(set(selected)) != len(selected):
                raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_duplicate'})
            if any(stamp_id not in self.entitlements.stamps for stamp_id in selected):
                raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_master'})

        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            self.entitlements.initialize_stamps(state)
            owned = state['stamps']
            if any(str(stamp_id) not in owned for stamp_ids in updates.values()
                   for stamp_id in stamp_ids if stamp_id):
                raise NativeRpcError(9, diagnostic={'code': 'stamp_favorite_not_owned'})
            for stamp in owned.values():
                stamp['favorites'] = [favorite for favorite in stamp['favorites']
                                      if favorite['favorite_id'] not in updates]
            for favorite_id, stamp_ids in updates.items():
                for slot_index, stamp_id in enumerate(stamp_ids):
                    if stamp_id:
                        owned[str(stamp_id)]['favorites'].append({
                            'favorite_id': favorite_id, 'slot_index': slot_index})
            self.progress.save(connection, profile_id, state)
        return b''

    def update_name(self, profile_id, favorite_id, name):
        if not 1 <= favorite_id <= FAVORITE_GROUP_COUNT:
            raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_id'})
        if not 1 <= len(name.encode('utf-16-le')) // 2 <= FAVORITE_NAME_MAX_LENGTH:
            raise NativeRpcError(3, diagnostic={'code': 'stamp_favorite_name_length'})
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            state.setdefault('stamp_favorite_names', {})[str(favorite_id)] = name
            self.progress.save(connection, profile_id, state)
        return b''

"""Original account preferences, LIVE option bytes and music save operations."""
from __future__ import annotations

import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from .inventory import member_cards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field, varint as encode_varint
from deathnote.protocol.decode import fields, varint


SAVE_SETTING_METHOD = '/app.live.LiveService/SaveSetting'
SAVE_MUSIC_FAVORITE_METHOD = '/app.livemusic.LiveMusicService/SaveFavorite'
SAVE_PUZZLE_METHOD = '/app.livemusic.LiveMusicService/SavePuzzle'
CHANGE_FAVORITE_MEMBER_METHOD = '/app.player.PlayerService/ChangeFavoriteMember'
GET_CHARACTER_ORDERS_METHOD = '/app.player.PlayerService/GetCharacterOrders'
SAVE_CHARACTER_ORDERS_METHOD = '/app.player.PlayerService/SaveCharacterOrders'
SET_NOTIFICATION_METHOD = '/app.player.PlayerService/SetNotificationSetting'


def favorite_member_card(owned, state):
    selected = owned.get(str(state.get('favorite_member_card_id')))
    if selected is not None:
        return selected
    return min(owned.values(), key=lambda card: (card['master_id'] != 1, card['id']))


def _packed_ids(payload):
    result = []
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        if number != 1:
            continue
        if isinstance(value, bytes):
            position = 0
            while position < len(value):
                member_id, position = varint(value, position)
                result.append(member_id)
        else:
            result.append(value)
    if any(not 0 < value <= 0x7fffffffffffffff for value in result):
        raise ValueError('Character IDs must be positive int64 values')
    if len(set(result)) != len(result):
        raise ValueError('Character IDs repeat')
    return result


def user_setting_fields(state):
    output = []
    if 'live_setting_all' in state:
        output.append(message_field(28, message_field(1, base64.b64decode(state['live_setting_all']))))
    if state.get('character_orders'):
        output.append(message_field(55, b''.join(encode_varint(value) for value in state['character_orders'])))
    if 'is_night_notice_off' in state:
        output.append(integer_field(58, int(state['is_night_notice_off'])))
    for key in sorted(state.get('music_puzzles', {}), key=int):
        puzzle = state['music_puzzles'][key]
        output.append(message_field(59, integer_field(1, puzzle['music_id'])
                                    + string_field(2, puzzle['result_json'])))
    return b''.join(output)


class NativeUserSettings:
    methods = frozenset((SAVE_SETTING_METHOD, SAVE_MUSIC_FAVORITE_METHOD, SAVE_PUZZLE_METHOD,
                         CHANGE_FAVORITE_MEMBER_METHOD, GET_CHARACTER_ORDERS_METHOD,
                         SAVE_CHARACTER_ORDERS_METHOD, SET_NOTIFICATION_METHOD))

    def __init__(self, login, master_directory, *, progress, clock=time.time):
        self.login = login
        self.progress = progress
        self.clock = clock
        root = Path(master_directory)
        characters = json.loads((root / 'MasterCharacter.json').read_text('utf8'))['_allData']
        self.character_ids = frozenset(row['_id'] for row in characters if not row['_isNonPlayable'])
        musics = json.loads((root / 'MasterLiveMusic.json').read_text('utf8'))['_allData']
        self.music_ids = frozenset(row['_id'] for row in musics)

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            entries = fields(payload)
            if any(number < 1 for number, _ in entries):
                raise ValueError('Protobuf field zero')
            values = dict(entries)
            orders = _packed_ids(payload) if method == SAVE_CHARACTER_ORDERS_METHOD else None
            if method in (CHANGE_FAVORITE_MEMBER_METHOD, SAVE_MUSIC_FAVORITE_METHOD):
                master_id = values.get(1, 0)
                if not isinstance(master_id, int) or not 0 < master_id <= 0x7fffffffffffffff:
                    raise ValueError('ID must be a positive int64')
            if method == SAVE_SETTING_METHOD:
                setting = values.get(1, b'')
                if not isinstance(setting, bytes):
                    raise ValueError('LIVE setting must be bytes')
            if method == SAVE_MUSIC_FAVORITE_METHOD:
                favorites = [values.get(number, 0) for number in (2, 3, 4)]
                if any(not isinstance(value, int) for value in favorites):
                    raise ValueError('Music favorites must be bools')
                favorites = [bool(value) for value in favorites]
            if method == SET_NOTIFICATION_METHOD:
                flag = values.get(1, 0)
                if not isinstance(flag, int):
                    raise ValueError('Night notification flag must be a bool')
            if method == SAVE_PUZZLE_METHOD:
                puzzle_bytes = values.get(1)
                if not isinstance(puzzle_bytes, bytes):
                    raise ValueError('Puzzle message is missing')
                puzzle = dict(fields(puzzle_bytes))
                master_id, result = puzzle.get(1, 0), puzzle.get(2, b'')
                if (not isinstance(master_id, int) or not 0 < master_id <= 0x7fffffffffffffff
                        or not isinstance(result, bytes)):
                    raise ValueError('Invalid puzzle fields')
                result = result.decode('utf8')
                if result:
                    json.loads(result)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'user_settings_wire'}) from None
        if method in (SAVE_MUSIC_FAVORITE_METHOD, SAVE_PUZZLE_METHOD) and master_id not in self.music_ids:
            raise NativeRpcError(3, diagnostic={'code': 'user_settings_music'})
        if orders is not None and any(value not in self.character_ids for value in orders):
            raise NativeRpcError(3, diagnostic={'code': 'user_settings_characters'})
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN' if method == GET_CHARACTER_ORDERS_METHOD else 'BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            if method == GET_CHARACTER_ORDERS_METHOD:
                selected = state.get('character_orders', sorted(self.character_ids))
                return (message_field(1, b''.join(encode_varint(value) for value in selected))
                        if selected else b'')
            if method == CHANGE_FAVORITE_MEMBER_METHOD:
                owned = member_cards(state, player['created_at'])
                if str(master_id) not in owned:
                    raise NativeRpcError(9, diagnostic={'code': 'favorite_member_not_owned'})
                if state.get('favorite_member_card_id') != master_id:
                    state['favorite_member_card_id'] = master_id
                    state['profile_updated_at'] = int(self.clock())
            elif method == SAVE_CHARACTER_ORDERS_METHOD:
                state['character_orders'] = orders
            elif method == SET_NOTIFICATION_METHOD:
                state['is_night_notice_off'] = bool(flag)
            elif method == SAVE_SETTING_METHOD:
                state['live_setting_all'] = base64.b64encode(setting).decode('ascii')
            elif method == SAVE_MUSIC_FAVORITE_METHOD:
                state.setdefault('music_favorites', {})[str(master_id)] = favorites
            elif method == SAVE_PUZZLE_METHOD:
                state.setdefault('music_puzzles', {})[str(master_id)] = {
                    'music_id': master_id, 'result_json': result}
            self.progress.save(connection, player['profile_id'], state)
        return b''

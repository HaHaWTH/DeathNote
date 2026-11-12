"""Native phone-chat preferences over the shared per-player progress store."""
from contextlib import closing
from functools import cached_property
import json
from pathlib import Path
import sqlite3

from deathnote.game.players.progress import NativeLiveProgress
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, varint as encode_varint
from deathnote.protocol.decode import fields, varint

PREFIX = '/app.chat.ClientChatService/'
GET_CHAT_SETTINGS_METHOD = PREFIX + 'GetChatSettings'
SET_CHAT_SETTINGS_METHOD = PREFIX + 'SetChatSettings'
GET_NOTIFICATIONS_METHOD = PREFIX + 'GetNotifications'
SYNC_MSG_METHOD = PREFIX + 'SyncMsg'
SKINS = ((1001, 'chat_theme_id', 'chat_themes'),
         (1002, 'chat_bubble_id', 'chat_bubbles'),
         (1003, 'chat_frame_id', 'chat_frames'))
DISPLAY_KEYS = ('world_channel_visible', 'world_filter_text',
                'world_filter_stamp', 'world_filter_entertain')


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 0x7fffffffffffffff:
        raise ValueError('Chat setting is outside its int64 range')
    return value


def _message(payload):
    result = {}
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        result[number] = value
    return result


def _focus(payload):
    result = []
    for number, value in fields(payload):
        if number != 1:
            continue
        if isinstance(value, bytes):
            offset = 0
            while offset < len(value):
                account_id, offset = varint(value, offset)
                result.append(_integer(account_id, minimum=1))
        else:
            result.append(_integer(value, minimum=1))
    return list(dict.fromkeys(result))


def display_message(settings):
    result = b''.join(integer_field(number, int(settings[key]))
                      for number, key in enumerate(DISPLAY_KEYS, 1) if settings[key])
    return result + b''.join(message_field(5, integer_field(1, row['topic_id'])
        + (integer_field(2, 1) if row['visible'] else b'')) for row in settings['topics'])


class NativeChatSettings:
    methods = frozenset((GET_CHAT_SETTINGS_METHOD, SET_CHAT_SETTINGS_METHOD,
                         GET_NOTIFICATIONS_METHOD))

    def __init__(self, login, master_directory, *, entitlements):
        self.login = login
        self.progress = NativeLiveProgress(login)
        self.master_directory = Path(master_directory)
        self.chat_masters = entitlements.chat_masters
        self.defaults = {name: min(key for key, row in self.chat_masters[kind].items()
                                  if row['_isInitialOwnership']) for kind, name, _ in SKINS}

    @cached_property
    def topic_ids(self):
        data = json.loads((self.master_directory / 'MasterBiliChatTopic.json').read_text('utf8'))
        return {row['_id'] for row in data['_allData']}

    def _defaults(self):
        return {**self.defaults, 'special_focus_account_ids': [],
                'display_settings': {**dict.fromkeys(DISPLAY_KEYS, True), 'topics': []}}

    def _display(self, payload):
        values = _message(payload)
        result = {}
        for number, key in enumerate(DISPLAY_KEYS, 1):
            value = values.get(number, 0)
            if type(value) is not int or value not in (0, 1):
                raise ValueError('Chat display flag must be bool')
            result[key] = bool(value)
        topics = {}
        for number, value in fields(payload):
            if number != 5:
                continue
            entry = _message(value)
            topic_id = _integer(entry.get(1, 0), minimum=1)
            visible = entry.get(2, 0)
            if topic_id not in self.topic_ids or type(visible) is not int or visible not in (0, 1):
                raise ValueError('Invalid chat topic setting')
            topics[topic_id] = {'topic_id': topic_id, 'visible': bool(visible)}
        result['topics'] = list(topics.values())
        return result

    def _decode(self, payload, previous):
        values = _message(payload)
        result = {'special_focus_account_ids': _focus(payload)}
        for number, (_, name, _) in enumerate(SKINS, 2):
            result[name] = _integer(values.get(number, 0)) or self.defaults[name]
        result['display_settings'] = (self._display(values[5]) if 5 in values
                                      else previous['display_settings'])
        return result

    @staticmethod
    def _response(settings):
        focus = settings['special_focus_account_ids']
        result = message_field(2, b''.join(encode_varint(value) for value in focus)) if focus else b''
        for number, (_, name, _) in enumerate(SKINS, 3):
            result += integer_field(number, settings[name])
        return result + message_field(6, display_message(settings['display_settings']))

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            _message(payload)
        except (ValueError, TypeError):
            raise NativeRpcError(3) from None
        if method == GET_NOTIFICATIONS_METHOD:
            return b''
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            settings = state.get('chat_settings', self._defaults())
            if method == SET_CHAT_SETTINGS_METHOD:
                try:
                    settings = self._decode(payload, settings)
                except (ValueError, TypeError):
                    raise NativeRpcError(3) from None
                for kind, name, owned_key in SKINS:
                    master = self.chat_masters[kind].get(settings[name])
                    if master is None:
                        raise NativeRpcError(3)
                    if not master['_isInitialOwnership'] and settings[name] not in state.get(owned_key, ()):
                        raise NativeRpcError(9)
            state['chat_settings'] = settings
            self.progress.save(connection, player['profile_id'], state)
            return self._response(settings) if method == GET_CHAT_SETTINGS_METHOD else b''

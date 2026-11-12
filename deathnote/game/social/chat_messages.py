"""Persist local native-chat messages and deliver the original SDK notification."""
from contextlib import closing
import sqlite3
import time

from deathnote.net.chat_realtime import envelope
from deathnote.protocol.rpc import NativeRpcError
from deathnote.net.websocket import MAX_MESSAGE_BYTES
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields

PREFIX = '/app.chat.ClientChatService/'
SEND_IM_MSG_METHOD = PREFIX + 'SendIMMsg'
SYNC_MSG_METHOD = PREFIX + 'SyncMsg'
IM_NOTIFICATION = 'app.chat.MessageIMNoti'
_COLUMNS = 'msg_id, sender, receiver, im_type, msg_type, content, sent_time'
_MAX_INT64 = 0x7fffffffffffffff


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value <= _MAX_INT64:
        raise ValueError('Chat value is outside its int64 range')
    return value


def _message(payload):
    if not isinstance(payload, bytes):
        raise ValueError('Chat message must be a protobuf message')
    result = {}
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        result[number] = value
    return result


def _wire(row):
    msg_id, sender, receiver, im_type, msg_type, content, sent_time = row
    return (integer_field(2, msg_id) + integer_field(3, sender)
            + integer_field(4, receiver) + integer_field(5, im_type)
            + integer_field(6, msg_type) + string_field(7, content)
            + integer_field(8, sent_time))


class NativeChatMessages:
    methods = frozenset((SEND_IM_MSG_METHOD, SYNC_MSG_METHOD))

    def __init__(self, login, *, hub=None, clock=time.time):
        self.login = login
        self.hub = hub
        self.clock = clock
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS chat_messages (
                    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender INTEGER NOT NULL REFERENCES players(profile_id),
                    receiver INTEGER NOT NULL,
                    im_type INTEGER NOT NULL,
                    msg_type INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    sent_time INTEGER NOT NULL,
                    accepted_at INTEGER NOT NULL,
                    UNIQUE(sender, receiver, im_type, sent_time, content)
                );
                CREATE INDEX IF NOT EXISTS chat_messages_room_time
                    ON chat_messages(im_type, receiver, sent_time, msg_id);
                CREATE INDEX IF NOT EXISTS chat_messages_sender_time
                    ON chat_messages(sender, im_type, sent_time, msg_id);
            ''')

    @staticmethod
    def _conversation(values, type_field, receiver_field):
        im_type = _integer(values.get(type_field, 0))
        receiver = _integer(values.get(receiver_field, 0))
        if im_type not in (0, 4) or (im_type == 4 and receiver != 0):
            raise ValueError('Unsupported chat conversation')
        return im_type, receiver

    def _publish(self, account_ids, rows):
        if self.hub is None:
            return
        overhead = len(envelope(IM_NOTIFICATION, b'')) + 4
        batch = bytearray()
        for row in rows:
            entry = message_field(3, _wire(row))
            if batch and len(batch) + len(entry) + overhead > MAX_MESSAGE_BYTES:
                self.hub.publish(account_ids, envelope(IM_NOTIFICATION, bytes(batch)))
                batch.clear()
            batch.extend(entry)
        if batch:
            self.hub.publish(account_ids, envelope(IM_NOTIFICATION, bytes(batch)))

    def _send(self, player, payload):
        values = _message(_message(payload).get(1, b''))
        sender = player['profile_id']
        if _integer(values.get(3, 0)) not in (0, sender):
            raise ValueError('Chat sender differs from the authenticated player')
        im_type, receiver = self._conversation(values, 5, 4)
        if im_type == 0 and not receiver:
            raise ValueError('Peer messages require a recipient')
        msg_type = _integer(values.get(6, 0))
        if msg_type not in range(5):
            raise ValueError('Unsupported client chat content type')
        content_bytes = values.get(7, b'')
        if not isinstance(content_bytes, bytes) or not content_bytes:
            raise ValueError('Chat content is empty')
        content = content_bytes.decode('utf8')
        sent_time = _integer(values.get(8, 0), minimum=1)
        provisional = (_MAX_INT64, sender, receiver, im_type, msg_type, content, sent_time)
        if len(envelope(IM_NOTIFICATION, message_field(3, _wire(provisional)))) > MAX_MESSAGE_BYTES:
            raise ValueError('Chat message exceeds the WebSocket limit')
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if im_type == 0 and connection.execute(
                    'SELECT 1 FROM players WHERE profile_id=?', (receiver,)).fetchone() is None:
                raise NativeRpcError(5)
            cursor = connection.execute('''
                INSERT INTO chat_messages(sender,receiver,im_type,msg_type,content,sent_time,accepted_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(sender,receiver,im_type,sent_time,content) DO NOTHING
            ''', (sender, receiver, im_type, msg_type, content, sent_time, int(self.clock())))
            inserted = cursor.rowcount == 1
            row = connection.execute('SELECT ' + _COLUMNS + ''' FROM chat_messages
                WHERE sender=? AND receiver=? AND im_type=? AND sent_time=? AND content=?
            ''', (sender, receiver, im_type, sent_time, content)).fetchone()
            account_ids = ((sender, receiver) if im_type == 0 else
                           tuple(value[0] for value in connection.execute('SELECT profile_id FROM players')))
        if inserted:
            self._publish(account_ids, (row,))
        return string_field(2, row[5])

    def _sync(self, player, payload):
        values = _message(payload)
        im_type, receiver = self._conversation(values, 1, 3)
        watermark = _integer(values.get(2, 0))
        sender = player['profile_id']
        with closing(sqlite3.connect(self.login.database)) as connection:
            if im_type == 4:
                condition, parameters = 'im_type=4 AND receiver=0', ()
            elif receiver:
                condition = 'im_type=0 AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?))'
                parameters = (sender, receiver, receiver, sender)
            else:
                condition = 'im_type=0 AND (sender=? OR receiver=?)'
                parameters = (sender, sender)
            rows = connection.execute('SELECT ' + _COLUMNS + ' FROM chat_messages WHERE '
                + condition + ' AND sent_time>=? ORDER BY sent_time,msg_id', (*parameters, watermark))
            self._publish((sender,), rows)
        return b''

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            return self._send(player, payload) if method == SEND_IM_MSG_METHOD else self._sync(player, payload)
        except (ValueError, TypeError):
            raise NativeRpcError(3) from None

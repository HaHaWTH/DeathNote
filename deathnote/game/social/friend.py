"""Original friend RPCs backed by relationships between local server accounts."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields


FIND_FRIEND_METHOD = '/app.friend.FriendService/FindByProfileID'
REQUEST_FRIEND_METHOD = '/app.friend.FriendService/FriendRequest'
WITHDRAW_FRIEND_METHOD = '/app.friend.FriendService/FriendRequestWithdrawal'
ANSWER_FRIEND_METHOD = '/app.friend.FriendService/FriendAnswer'
UNLINK_FRIEND_METHOD = '/app.friend.FriendService/FriendUnlink'

LOCAL_FRIEND_LIMITS = (100, 100, 100)
_PROFILE_SELECT = '''
    SELECT p.profile_id, p.player_id, p.created_at, e.name, e.updated_at
    FROM players p LEFT JOIN player_profile_edits e ON e.profile_id = p.profile_id
'''


def _request(method, payload):
    target, targets, answer = '', [], False
    profile_id = 0
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        if number == 1:
            if method == FIND_FRIEND_METHOD:
                if not isinstance(value, int) or not 0 < value <= 0x7fffffffffffffff:
                    raise ValueError('Profile ID must be a positive int64')
                profile_id = value
            else:
                if not isinstance(value, bytes):
                    raise ValueError('Player ID must be a string')
                target = value.decode('utf8')
                if not target:
                    raise ValueError('Player ID is empty')
                targets.append(target)
        elif number == 2 and method == ANSWER_FRIEND_METHOD:
            if not isinstance(value, int):
                raise ValueError('Friend answer must be a protobuf bool')
            answer = bool(value)
        elif number == 2 and method == REQUEST_FRIEND_METHOD:
            if not isinstance(value, bytes):
                raise ValueError('Search route must be a string')
            value.decode('utf8')
    if method == FIND_FRIEND_METHOD and not profile_id:
        raise ValueError('Profile ID is missing')
    if method != ANSWER_FRIEND_METHOD and method != FIND_FRIEND_METHOD and not target:
        raise ValueError('Player ID is missing')
    return profile_id, target, list(dict.fromkeys(targets)), answer


class NativeFriend:
    methods = frozenset((FIND_FRIEND_METHOD, REQUEST_FRIEND_METHOD, WITHDRAW_FRIEND_METHOD,
                         ANSWER_FRIEND_METHOD, UNLINK_FRIEND_METHOD))

    def __init__(self, login, player_data, *, profile_builder, clock=time.time,
                 limits=LOCAL_FRIEND_LIMITS):
        self.login = login
        self.player_data = player_data
        self.profile_builder = profile_builder
        self.clock = clock
        self.limits = limits
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_friend_requests (
                    sender_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    receiver_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    requested_at INTEGER NOT NULL,
                    PRIMARY KEY(sender_profile_id, receiver_profile_id)
                );
                CREATE INDEX IF NOT EXISTS player_friend_requests_receiver
                    ON player_friend_requests(receiver_profile_id);
                CREATE TABLE IF NOT EXISTS player_friendships (
                    lower_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    upper_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    accepted_at INTEGER NOT NULL,
                    PRIMARY KEY(lower_profile_id, upper_profile_id)
                );
                CREATE INDEX IF NOT EXISTS player_friendships_upper
                    ON player_friendships(upper_profile_id);
            ''')

    def _profile(self, connection, row):
        state = self.player_data.progress.load(connection, row[0])
        return self.profile_builder(connection, row, state)

    def _peer(self, connection, player_id, own_id):
        row = connection.execute(_PROFILE_SELECT + ' WHERE p.player_id = ?', (player_id,)).fetchone()
        if row is None:
            raise NativeRpcError(5, diagnostic={'code': 'friend_player_missing'})
        if row[0] == own_id:
            raise NativeRpcError(9, diagnostic={'code': 'friend_self'})
        return row

    @staticmethod
    def _pair(first, second):
        return tuple(sorted((first, second)))

    def _accepted(self, connection, first, second):
        return connection.execute('''SELECT 1 FROM player_friendships
            WHERE lower_profile_id = ? AND upper_profile_id = ?''',
            self._pair(first, second)).fetchone() is not None

    @staticmethod
    def _pending(connection, sender, receiver):
        return connection.execute('''SELECT 1 FROM player_friend_requests
            WHERE sender_profile_id = ? AND receiver_profile_id = ?''',
            (sender, receiver)).fetchone() is not None

    @staticmethod
    def _friend_count(connection, profile_id):
        return connection.execute('''SELECT COUNT(*) FROM player_friendships
            WHERE lower_profile_id = ? OR upper_profile_id = ?''',
            (profile_id, profile_id)).fetchone()[0]

    def _check_capacity(self, connection, first, second):
        if any(self._friend_count(connection, profile_id) >= self.limits[0]
               for profile_id in (first, second)):
            raise NativeRpcError(9, diagnostic={'code': 'friend_capacity'})

    def _accept(self, connection, first, second, now):
        self._check_capacity(connection, first, second)
        connection.execute('INSERT INTO player_friendships VALUES (?, ?, ?)',
                           (*self._pair(first, second), now))
        self._clear_requests(connection, first, second)

    @staticmethod
    def _clear_requests(connection, first, second):
        connection.execute('''DELETE FROM player_friend_requests
            WHERE (sender_profile_id = ? AND receiver_profile_id = ?)
               OR (sender_profile_id = ? AND receiver_profile_id = ?)''',
            (first, second, second, first))

    def friend_data(self, connection, profile_id):
        groups = [
            [row[0] for row in connection.execute('''
                SELECT CASE WHEN lower_profile_id = ? THEN upper_profile_id ELSE lower_profile_id END
                FROM player_friendships WHERE lower_profile_id = ? OR upper_profile_id = ?
                ORDER BY accepted_at, lower_profile_id, upper_profile_id''',
                (profile_id, profile_id, profile_id))],
            [row[0] for row in connection.execute('''SELECT receiver_profile_id FROM player_friend_requests
                WHERE sender_profile_id = ? ORDER BY requested_at, receiver_profile_id''', (profile_id,))],
            [row[0] for row in connection.execute('''SELECT sender_profile_id FROM player_friend_requests
                WHERE receiver_profile_id = ? ORDER BY requested_at, sender_profile_id''', (profile_id,))],
        ]
        output = []
        for number, ids in enumerate(groups, 1):
            profiles = integer_field(2, self.limits[number - 1])
            for target_id in ids:
                row = connection.execute(_PROFILE_SELECT + ' WHERE p.profile_id = ?', (target_id,)).fetchone()
                profiles += message_field(3, self._profile(connection, row))
            output.append(message_field(number, profiles))
        return b''.join(output)

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            profile_id, target, targets, answer = _request(method, payload)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'friend_request_wire'}) from None
        own_id = player['profile_id']
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN' if method == FIND_FRIEND_METHOD else 'BEGIN IMMEDIATE')
            if method == FIND_FRIEND_METHOD:
                row = connection.execute(_PROFILE_SELECT + ' WHERE p.profile_id = ?', (profile_id,)).fetchone()
                return message_field(2, self._profile(connection, row)) if row is not None else b''
            if method == ANSWER_FRIEND_METHOD:
                rows = [self._peer(connection, value, own_id) for value in targets]
                for row in rows:
                    peer_id = row[0]
                    accepted = self._accepted(connection, own_id, peer_id)
                    if answer and not accepted:
                        if not self._pending(connection, peer_id, own_id):
                            raise NativeRpcError(9, diagnostic={'code': 'friend_request_missing'})
                        self._accept(connection, own_id, peer_id, int(self.clock()))
                    else:
                        connection.execute('''DELETE FROM player_friend_requests
                            WHERE sender_profile_id = ? AND receiver_profile_id = ?''', (peer_id, own_id))
                return message_field(2, b'') + b''.join(message_field(3, self._profile(connection, row))
                                                        for row in rows)
            row = self._peer(connection, target, own_id)
            peer_id = row[0]
            if method == WITHDRAW_FRIEND_METHOD:
                connection.execute('''DELETE FROM player_friend_requests
                    WHERE sender_profile_id = ? AND receiver_profile_id = ?''', (own_id, peer_id))
                return b''
            if method == UNLINK_FRIEND_METHOD:
                connection.execute('''DELETE FROM player_friendships
                    WHERE lower_profile_id = ? AND upper_profile_id = ?''', self._pair(own_id, peer_id))
                return message_field(1, b'')
            accepted = self._accepted(connection, own_id, peer_id)
            if not accepted and self._pending(connection, peer_id, own_id):
                self._accept(connection, own_id, peer_id, int(self.clock()))
                accepted = True
            elif not accepted and not self._pending(connection, own_id, peer_id):
                self._check_capacity(connection, own_id, peer_id)
                sent = connection.execute('SELECT COUNT(*) FROM player_friend_requests WHERE sender_profile_id = ?',
                                          (own_id,)).fetchone()[0]
                received = connection.execute('SELECT COUNT(*) FROM player_friend_requests WHERE receiver_profile_id = ?',
                                              (peer_id,)).fetchone()[0]
                if sent >= self.limits[1] or received >= self.limits[2]:
                    raise NativeRpcError(9, diagnostic={'code': 'friend_pending_capacity'})
                connection.execute('INSERT INTO player_friend_requests VALUES (?, ?, ?)',
                                   (own_id, peer_id, int(self.clock())))
            return (integer_field(2, int(accepted)) + message_field(3, b'')
                    + message_field(4, self._profile(connection, row)))

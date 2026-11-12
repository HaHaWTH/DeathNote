"""Original-client social queries backed by this local server's own records."""
from __future__ import annotations

from contextlib import closing
from functools import cached_property
import json
from pathlib import Path
import secrets
import sqlite3

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field, string_field
from deathnote.protocol.decode import fields


PERSONAL_NOTIFICATION_FETCH_METHOD = '/app.personal_notification.PersonalNotificationService/Fetch'
INVITATION_GENERATE_METHOD = '/app.invitation.InvitationService/GenerateInvitationCode'
CHALLENGE_MUSIC_RANKING_METHOD = '/app.event.EventService/GetChallengeMusicRanking'


class NativeSocialQueries:
    methods = frozenset((PERSONAL_NOTIFICATION_FETCH_METHOD, INVITATION_GENERATE_METHOD))

    def __init__(self, login, master_directory):
        self.login = login
        self.database = login.database
        self.master_directory = Path(master_directory)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS player_invitation_codes (
                profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                code TEXT NOT NULL UNIQUE
            )''')

    @cached_property
    def challenge_music_ids(self):
        document = json.loads((self.master_directory / 'MasterChallengeMusic.json').read_text('utf8'))
        return frozenset(row['_id'] for row in document['_allData'])

    def dispatch(self, method: str, payload: bytes, headers: dict[str, str]) -> bytes:
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        challenge_music_id = 0
        try:
            for number, value in fields(payload):
                if number < 1:
                    raise ValueError('Protobuf field zero')
                if method == CHALLENGE_MUSIC_RANKING_METHOD and number == 1:
                    if not isinstance(value, int):
                        raise ValueError('Challenge music ID has the wrong wire type')
                    challenge_music_id = value
        except ValueError:
            raise NativeRpcError(3) from None
        if method == PERSONAL_NOTIFICATION_FETCH_METHOD:
            return message_field(1, b'')
        if method == INVITATION_GENERATE_METHOD:
            return string_field(1, self.invitation_code(player['profile_id']))
        if challenge_music_id not in self.challenge_music_ids:
            raise NativeRpcError(3)
        return b''

    def invitation_code(self, profile_id: int) -> str:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT code FROM player_invitation_codes WHERE profile_id = ?',
                                     (profile_id,)).fetchone()
            if row is not None:
                return row[0]
            while True:
                code = f'{secrets.randbelow(10 ** 10):010d}'
                try:
                    connection.execute('INSERT INTO player_invitation_codes(profile_id, code) VALUES (?, ?)',
                                       (profile_id, code))
                except sqlite3.IntegrityError:
                    continue
                return code

    def invitation_data(self, profile_id: int) -> bytes:
        return b''

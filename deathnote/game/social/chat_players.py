"""Public chat participant summaries for explicitly requested local account IDs."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .friend import _PROFILE_SELECT
from deathnote.game.players.inventory import member_cards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.players.settings import favorite_member_card
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields, varint


GET_CHAT_PLAYERS_METHOD = '/app.playerext.PlayerExtService/GetPlayerList'


def _account_ids(payload):
    result = []
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        if number != 1:
            continue
        if isinstance(value, bytes):
            position = 0
            while position < len(value):
                account_id, position = varint(value, position)
                result.append(account_id)
        else:
            result.append(value)
    if any(not 0 < account_id <= 0x7fffffffffffffff for account_id in result):
        raise ValueError('Account ID must be a positive int64')
    return list(dict.fromkeys(result))


class NativeChatPlayers:
    methods = frozenset((GET_CHAT_PLAYERS_METHOD,))

    def __init__(self, login, player_data, master_directory, *, chat_settings):
        self.login = login
        self.progress = player_data.progress
        self.chat_settings = chat_settings
        data = json.loads((Path(master_directory) / 'MasterPlayerRank.json').read_text('utf8'))
        self.player_ranks = data['_allData']

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        self.login.authenticate(headers)
        try:
            account_ids = _account_ids(payload)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'chat_players_request'}) from None
        result = []
        with closing(sqlite3.connect(self.login.database)) as connection:
            connection.execute('BEGIN')
            for account_id in account_ids:
                row = connection.execute(_PROFILE_SELECT + ' WHERE p.profile_id=?',
                                         (account_id,)).fetchone()
                if row is None:
                    continue
                profile_id, player_id, created_at, name, _ = row
                state = self.progress.load(connection, profile_id)
                favorite = favorite_member_card(member_cards(state, created_at), state)
                rank = max((row for row in self.player_ranks if row['_exp'] <= state['rank_exp']),
                           key=lambda row: row['_exp'])['_rank']
                settings = state.get('chat_settings', self.chat_settings.defaults)
                brief = (integer_field(1, profile_id) + string_field(2, player_id)
                    + string_field(3, name or f'本地玩家{profile_id}') + integer_field(4, rank)
                    + integer_field(5, settings['chat_frame_id'])
                    + integer_field(6, settings['chat_bubble_id'])
                    + integer_field(7, settings['chat_theme_id'])
                    + integer_field(8, favorite['master_id']))
                result.append(message_field(1, brief))
        return b''.join(result)

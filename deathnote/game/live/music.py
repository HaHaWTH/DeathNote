"""Original music ranking DTOs backed by locally persisted LIVE high scores.

The request contains only a music ID: ordinary Free and Battle selectors share
this endpoint. Historical formation details are captured during settlement,
before score mutation, and remain unchanged when cards are upgraded later.
"""
from __future__ import annotations

from contextlib import closing
from functools import cached_property
import json
from pathlib import Path
import sqlite3

from deathnote.game.players.inventory import member_cards
from deathnote.game.players.settings import favorite_member_card
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields


GET_RANKING_METHOD = '/app.livemusic.LiveMusicService/GetRanking'


def parse_message(payload):
    try:
        return dict(fields(payload))
    except ValueError:
        raise NativeRpcError(3) from None


def int_value(values, number):
    value = values.get(number, 0)
    if not isinstance(value, int):
        raise NativeRpcError(3)
    return value


def _member_detail(card, state):
    return b''.join(integer_field(number, value) for number, value in (
        (1, card['master_id']), (2, state['member_exp'].get(str(card['id']), 0)),
        (3, card['awake_count']), (4, card['card_rank']),
        (5, card['live_skill_level']), (6, card['performance_skill_level'])) if value)


def record_score(state, *, music_id, difficulty, mode, score, deck, created_at):
    """Capture a newly improved score's original DeckDetail inside its transaction."""
    key = f'{music_id}:{difficulty}:{mode}'
    previous = state['scores'].get(key)
    if score <= (previous['high_score'] if previous is not None else 0):
        return
    owned = member_cards(state, created_at)
    support = state['support_cards']
    source = parse_message(deck)
    output = integer_field(1, int_value(source, 1))
    output += message_field(2, source.get(2, b''))
    for number, value in fields(deck):
        if number != 3:
            continue
        slot = parse_message(value)
        member = owned[str(int_value(slot, 3))]
        support_id = int_value(slot, 4)
        support_card = support.get(str(support_id))
        member_wire = _member_detail(member, state)
        support_wire = (integer_field(1, support_card['master_id'])
                        + integer_field(2, support_card['exp'])
                        + integer_field(3, support_card['card_rank'])) if support_card is not None else b''
        output += message_field(3,
            integer_field(1, int_value(slot, 1)) + integer_field(2, int_value(slot, 2))
            + message_field(3, member_wire) + message_field(4, support_wire))
    output += integer_field(4, int_value(source, 4))
    state.setdefault('high_score_decks', {})[key] = {'score': score, 'wire': output.hex()}


class NativeLiveMusic:
    methods = frozenset((GET_RANKING_METHOD,))

    def __init__(self, login, player_data, master_directory: Path, *, profile_card=None):
        self.login = login
        self.player_data = player_data
        self.profile_card = profile_card
        self.master_directory = Path(master_directory)

    @cached_property
    def music_ids(self):
        master = json.loads((self.master_directory / 'MasterLiveMusic.json').read_text('utf8'))
        return frozenset(row['_id'] for row in master['_allData'])

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        request = parse_message(payload)
        music_id = int_value(request, 1)
        if music_id not in self.music_ids:
            raise NativeRpcError(3)
        return self.ranking(music_id, player['profile_id'])

    def _profile(self, connection, row, state):
        profile_id, player_id, created_at, name, updated_at = row
        owned = member_cards(state, created_at)
        favorite = favorite_member_card(owned, state)
        result = (string_field(1, player_id) + string_field(2, name or f'本地玩家{profile_id}')
                  + integer_field(3, state['rank_exp'])
                  + integer_field(4, max(updated_at or created_at, state.get('profile_updated_at', 0)))
                  + message_field(5, _member_detail(favorite, state))
                  + integer_field(6, profile_id) + integer_field(7, favorite['master_id']))
        if self.profile_card is not None:
            selected = connection.execute('''
                SELECT slot, name FROM player_profile_card_slots
                WHERE profile_id = ? AND is_setting = 1 ORDER BY slot LIMIT 1
            ''', (profile_id,)).fetchone()
            if selected is not None:
                slot, title = selected
                objects = connection.execute('''
                    SELECT thumbnail_id FROM player_profile_card_objects
                    WHERE profile_id = ? AND slot = ? ORDER BY disp_order, object_index
                ''', (profile_id, slot)).fetchall()
                card = integer_field(1, slot) + string_field(2, title)
                card += b''.join(string_field(3, self.profile_card._url(item[0])) for item in objects)
                result += message_field(8, card)
        return result

    def ranking(self, music_id, own_profile_id):
        entries = []
        with closing(sqlite3.connect(self.login.database)) as connection:
            connection.execute('BEGIN')
            for row in connection.execute('''
                SELECT p.profile_id, p.player_id, p.created_at, e.name, e.updated_at
                FROM players p LEFT JOIN player_profile_edits e ON e.profile_id = p.profile_id
                ORDER BY p.profile_id
            ''').fetchall():
                profile_id = row[0]
                state = self.player_data.progress.load(connection, profile_id)
                candidates = [(key, value) for key, value in state['scores'].items()
                              if value['music_id'] == music_id and value['mode'] in (1, 2)
                              and value['high_score'] > 0]
                if not candidates:
                    continue
                key, record = max(sorted(candidates), key=lambda pair: pair[1]['high_score'])
                score = record['high_score']
                snapshot = state.get('high_score_decks', {}).get(key)
                deck = bytes.fromhex(snapshot['wire']) if snapshot is not None and snapshot['score'] == score else b''
                wire = message_field(1, self._profile(connection, row, state))
                wire += integer_field(3, score) + message_field(4, deck)
                entries.append((score, profile_id, wire))
        entries.sort(key=lambda entry: (-entry[0], entry[1]))
        rank = next((index for index, entry in enumerate(entries, 1)
                     if entry[1] == own_profile_id), 0)
        return b''.join(message_field(1, entry[2]) for entry in entries) + integer_field(2, rank)

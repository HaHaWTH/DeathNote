"""Store the original client's local deck protobufs by player and deck ID."""
from __future__ import annotations

from contextlib import closing
import sqlite3

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields


SAVE_DECKS_METHOD = '/app.deck.DeckService/SaveDecks'


def starter_deck() -> bytes:
    slots = [message_field(3, (
        integer_field(1, slot)
        + integer_field(2, slot)
        + integer_field(3, slot + 1)
        + integer_field(4, -1)
    )) for slot in range(5)]
    return integer_field(1, 1) + string_field(2, 'MyGO!!!!!') + b''.join(slots)


def _int32(value: int) -> int:
    value &= 0xffffffff
    return value - 0x100000000 if value & 0x80000000 else value


class NativePlayerDecks:
    def __init__(self, login):
        self.login = login

    def load(self, profile_id: int) -> tuple[list[bytes], int]:
        with closing(sqlite3.connect(self.login.database)) as connection:
            decks = connection.execute('''
                SELECT wire FROM player_decks WHERE profile_id = ? ORDER BY deck_id
            ''', (profile_id,)).fetchall()
            if not decks:
                return [starter_deck()], 1
            main_deck = connection.execute('''
                SELECT main_deck FROM player_deck_state WHERE profile_id = ?
            ''', (profile_id,)).fetchone()[0]
        return [row[0] for row in decks], main_deck

    def save(self, payload: bytes, headers: dict[str, str]) -> bytes:
        player = self.login.authenticate(headers)
        decks = {}
        main_deck = 0
        try:
            for number, value in fields(payload):
                if number == 1:
                    if not isinstance(value, bytes):
                        raise ValueError('Deck message has the wrong wire type')
                    deck_id = dict(fields(value)).get(1, 0)
                    if not isinstance(deck_id, int):
                        raise ValueError('Deck ID has the wrong wire type')
                    decks[_int32(deck_id)] = value
                elif number == 2:
                    if not isinstance(value, int):
                        raise ValueError('Main deck has the wrong wire type')
                    main_deck = _int32(value)
        except ValueError:
            raise NativeRpcError(3) from None
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('''
                SELECT 1 FROM player_deck_state WHERE profile_id = ?
            ''', (profile_id,)).fetchone()
            if existing is None:
                connection.execute('''
                    INSERT INTO player_deck_state(profile_id, main_deck) VALUES (?, 1)
                ''', (profile_id,))
                connection.execute('''
                    INSERT INTO player_decks(profile_id, deck_id, wire) VALUES (?, 1, ?)
                ''', (profile_id, starter_deck()))
            connection.executemany('''
                INSERT INTO player_decks(profile_id, deck_id, wire) VALUES (?, ?, ?)
                ON CONFLICT(profile_id, deck_id) DO UPDATE SET wire = excluded.wire
            ''', ((profile_id, deck_id, wire) for deck_id, wire in decks.items()))
            if main_deck:
                connection.execute('''
                    UPDATE player_deck_state SET main_deck = ? WHERE profile_id = ?
                ''', (main_deck, profile_id))
        return b''

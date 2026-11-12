"""Transactional local single-player LIVE state and protobuf result cache."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from deathnote.protocol.wire import integer_field, message_field
from .gem_history import ensure_schema, record_change


def initial_progress() -> dict:
    return {
        'rank_exp': 0, 'items': {}, 'member_exp': {},
        'support_cards': {}, 'degrees': {},
        'character_exp': {}, 'friendship_exp': {}, 'scores': {},
        'friendship_reward_ids': {},
        'score_reward_ids': {}, 'combo_reward_ids': {},
        'score_reward_gained': {}, 'combo_reward_gained': {},
        'live_boost': {'amount': 0, 'previous_recovery_at': 0,
                       'daily_ad_view': 0, 'daily_full_consume_count': 0,
                       'last_daily_reset_at': 0, 'campaign_daily_count': 0},
        'live_stamp': {'live_completed': 0, 'reward_gained': 0, 'last_live_completed_at': 0},
    }


def scalar_message(values) -> bytes:
    return b''.join(integer_field(number, value) for number, value in values if value)


def gem_message(state: dict) -> bytes:
    return scalar_message(((1, state['items'].get('1', 0)), (2, state['items'].get('2', 0))))


def boost_message(state: dict) -> bytes:
    boost = state['live_boost']
    return scalar_message(((1, boost['amount']), (2, boost['previous_recovery_at']),
                           (3, boost.get('daily_ad_view', 0)),
                           (4, boost.get('daily_full_consume_count', 0)),
                           (5, boost.get('last_daily_reset_at', 0)),
                           (6, boost.get('campaign_daily_count', 0))))


def stamp_message(state: dict) -> bytes:
    stamp = state['live_stamp']
    return scalar_message(((1, stamp['live_completed']), (2, stamp['reward_gained']),
                           (3, stamp['last_live_completed_at'])))


def skip_message(state: dict) -> bytes:
    skip = state.get('live_skip', {'count': 0, 'last_skip': 0})
    return scalar_message(((1, skip['count']), (2, skip['last_skip'])))


def player_progress_fields(state: dict, *, music_ids=None) -> bytes:
    output = []
    for key in sorted(state.get('support_cards', {}), key=int):
        card = state['support_cards'][key]
        output.append(message_field(3, scalar_message((
            (1, card['id']), (2, card['master_id']), (3, card['exp']),
            (5, card['card_rank']), (6, card['gain_at']), (7, card['duplicate_count']),
        ))))
    for key in sorted(state.get('degrees', {}), key=int):
        degree = state['degrees'][key]
        output.append(message_field(43, scalar_message((
            (1, degree['master_id']), (2, degree['amount']), (3, degree['acquired_at']),
        ))))
    for key in sorted(state.get('music_unlocked', {}), key=int):
        music = state['music_unlocked'][key]
        if music_ids is not None and music['music_id'] not in music_ids:
            continue
        output.append(message_field(20, scalar_message(((1, music['music_id']), (2, music['got_at'])))))
    for field, source in ((16, 'story_records'), (17, 'friendship_story_records')):
        for key in sorted(state.get(source, {}), key=int):
            record = state[source][key]
            output.append(message_field(field, scalar_message((
                (1, int(key)), (2, record['is_skipped']), (3, record['last_read_at']),
            ))))
    for item_id, amount in sorted(state['items'].items(), key=lambda item: int(item[0])):
        if item_id not in ('1', '2') and amount:
            output.append(message_field(1, scalar_message(((1, int(item_id)), (2, amount)))))
    if state['rank_exp']:
        output.append(integer_field(9, state['rank_exp']))
    for key in sorted(state['scores']):
        score = state['scores'][key]
        if music_ids is not None and score['music_id'] not in music_ids:
            continue
        output.append(message_field(10, scalar_message((
            (1, score['high_score']), (2, score['clear_status']), (3, score['music_id']),
            (4, score['difficulty']), (5, score['mode']), (6, score['max_combo']),
        ))))
    for music_id, rank in sorted(state['score_reward_gained'].items(), key=lambda item: int(item[0])):
        if music_ids is not None and int(music_id) not in music_ids:
            continue
        output.append(message_field(11, scalar_message(((1, int(music_id)), (2, rank)))))
    for field, source in ((12, 'character_exp'), (13, 'friendship_exp')):
        for record_id, exp in sorted(state[source].items(), key=lambda item: int(item[0])):
            output.append(message_field(field, scalar_message(((1, int(record_id)), (2, exp)))))
    for friendship_id, reward_ids in sorted(state['friendship_reward_ids'].items(), key=lambda item: int(item[0])):
        for reward_id in sorted(reward_ids):
            output.append(message_field(15, scalar_message(((1, int(friendship_id)), (2, reward_id)))))
    for key in sorted(state['combo_reward_gained']):
        music_id, difficulty = map(int, key.split(':'))
        if music_ids is not None and music_id not in music_ids:
            continue
        output.append(message_field(36, scalar_message((
            (1, music_id), (2, difficulty), (3, state['combo_reward_gained'][key]),
        ))))
    return b''.join(output)


class NativeLiveProgress:
    def __init__(self, login):
        self.database = login.database
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_live_progress (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS free_live_active (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    music_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    boost INTEGER NOT NULL,
                    deck_id INTEGER NOT NULL,
                    deck_wire BLOB NOT NULL,
                    started_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS free_live_results (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    live_unique_id TEXT NOT NULL,
                    music_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    clear_status INTEGER NOT NULL,
                    max_combo INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL,
                    response BLOB NOT NULL,
                    PRIMARY KEY(profile_id, live_unique_id)
                );
            ''')
            ensure_schema(connection)

    @staticmethod
    def load(connection, profile_id: int) -> dict:
        row = connection.execute(
            'SELECT state FROM player_live_progress WHERE profile_id = ?',
            (profile_id,),
        ).fetchone()
        return initial_progress() if row is None else json.loads(row[0])

    @staticmethod
    def save(connection, profile_id: int, state: dict) -> None:
        previous = connection.execute('SELECT state FROM player_live_progress WHERE profile_id = ?',
                                      (profile_id,)).fetchone()
        connection.execute('''
            INSERT INTO player_live_progress(profile_id, state) VALUES (?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET state = excluded.state
        ''', (profile_id, json.dumps(state, ensure_ascii=False, separators=(',', ':'))))
        before = json.loads(previous[0]) if previous is not None else initial_progress()
        record_change(connection, profile_id, before, state)

    def get(self, profile_id: int) -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            return self.load(connection, profile_id)

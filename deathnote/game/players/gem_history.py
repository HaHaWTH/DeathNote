"""Transactional gem deltas recorded from the local server's save boundary."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import time

from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields


_context = ContextVar('native_gem_history_context', default=(0, 0, False, None))


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS player_gem_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL REFERENCES players(profile_id),
        changed_at INTEGER NOT NULL,
        free_amount INTEGER NOT NULL,
        paid_amount INTEGER NOT NULL,
        reason INTEGER NOT NULL,
        reason_id INTEGER NOT NULL,
        via_present_box INTEGER NOT NULL
    )''')


@contextmanager
def rpc_context(method, payload, *, now):
    """Keep a decoded RPC category with all writes in that same request."""
    reason, reason_id, present = 0, 0, False
    if method.startswith('/app.gacha.'):
        reason = 12
    elif method.startswith('/app.shop.'):
        reason = 11
    elif method.startswith('/app.story.'):
        reason = 9 if method.endswith('/ReadFriendshipEpisode') else 5
    elif method.startswith('/app.liveboost.'):
        reason = 13
    elif method.startswith('/app.seasonpass.'):
        reason = 15
    elif method.startswith('/app.event.'):
        reason = 4
    elif method.startswith('/app.present.'):
        reason, present = 1, True
    if reason and not present:
        try:
            first = next((value for number, value in fields(payload) if number == 1), 0)
        except ValueError:
            first = 0
        if isinstance(first, int):
            reason_id = first
    token = _context.set((reason, reason_id, present, int(now)))
    try:
        yield
    finally:
        _context.reset(token)


def record_change(connection, profile_id, before_state, after_state):
    if before_state is None:
        return
    before, after = before_state.get('items', {}), after_state['items']
    free = after.get('1', 0) - before.get('1', 0)
    paid = after.get('2', 0) - before.get('2', 0)
    if not free and not paid:
        return
    reason, reason_id, present, now = _context.get()
    connection.execute('''INSERT INTO player_gem_history
        (profile_id,changed_at,free_amount,paid_amount,reason,reason_id,via_present_box)
        VALUES (?,?,?,?,?,?,?)''',
        (profile_id, int(time.time()) if now is None else now, free, paid,
         reason, reason_id, int(present)))


def history_message(connection, profile_id):
    result = []
    for row in connection.execute('''SELECT changed_at,free_amount,paid_amount,reason,reason_id,via_present_box
        FROM player_gem_history WHERE profile_id=? ORDER BY id DESC''', (profile_id,)):
        result.append(message_field(1, b''.join(integer_field(tag, value)
                                               for tag, value in enumerate(row, 1) if value)))
    return b''.join(result)

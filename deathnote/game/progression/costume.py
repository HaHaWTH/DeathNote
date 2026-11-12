"""Original character-costume ownership, notifications and current selections."""
from __future__ import annotations

from contextlib import closing
import sqlite3

from deathnote.game.players.inventory import member_cards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field
from deathnote.protocol.decode import fields


SET_CURRENT_COSTUMES_METHOD = '/app.player.PlayerService/SetCurrentCostumes'


def unlocked_costume_message(value):
    return (integer_field(1, value['character_id'])
            + integer_field(2, value['costume_group_id'])
            + integer_field(3, value['got_at']))


def current_costume_message(value):
    return (integer_field(1, value['character_id'])
            + integer_field(2, value['costume_target'])
            + integer_field(3, value['costume_group_id']))


def unlocked_costume_notification(previous_ids, state):
    """Notification field 9 carries only groups gained by this transaction."""
    owned = state.get('character_unlocked_costumes', {})
    return b''.join(message_field(9, unlocked_costume_message(owned[key]))
                    for key in sorted(set(owned) - set(previous_ids), key=int))


def _read_current(payload):
    result = []
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        if number != 1:
            continue
        if not isinstance(value, bytes):
            raise ValueError('Current costume must be a message')
        selection = {1: 0, 2: 0, 3: 0}
        for field, scalar in fields(value):
            if field < 1:
                raise ValueError('Protobuf field zero')
            if field in selection:
                if not isinstance(scalar, int):
                    raise ValueError('Current costume fields must be integers')
                selection[field] = scalar
        if (not 0 < selection[1] <= 0x7fffffffffffffff
                or selection[2] != 1
                or not 0 < selection[3] <= 0x7fffffffffffffff):
            raise ValueError('Invalid current costume selection')
        result.append({'character_id': selection[1], 'costume_target': selection[2],
                       'costume_group_id': selection[3]})
    return result


class NativeCostume:
    methods = frozenset((SET_CURRENT_COSTUMES_METHOD,))

    def __init__(self, login, entitlements, progress):
        self.login = login
        self.entitlements = entitlements
        self.progress = progress

    def dispatch(self, method, payload, headers):
        if method != SET_CURRENT_COSTUMES_METHOD:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            selections = _read_current(payload)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'costume_selection_wire'}) from None
        if not selections:
            return b''
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            member_cards(state, player['created_at'])
            self.entitlements.initialize_costumes(state)
            for selection in selections:
                group = self.entitlements.costume_groups.get(selection['costume_group_id'])
                if (group is None or group['_characterID'] != selection['character_id']
                        or not group['_isChangeable']):
                    raise NativeRpcError(3, diagnostic={'code': 'costume_group'})
                if (not group['_isInitial']
                        and str(group['_id']) not in state.get('character_unlocked_costumes', {})):
                    raise NativeRpcError(9, diagnostic={'code': 'costume_not_owned'})
            current = state.setdefault('character_current_costumes', {})
            for selection in selections:
                key = str(selection['character_id']) + ':' + str(selection['costume_target'])
                current[key] = selection
            self.progress.save(connection, player['profile_id'], state)
        return b''

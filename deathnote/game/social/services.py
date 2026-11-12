"""Local history, referral rewards, serial codes, LIVE invitations and reports."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

from deathnote.paths import ROOT

from deathnote.game.players.gem_history import history_message
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields, varint


GEM_HISTORY_METHOD = '/app.gem.GemService/History'
SHOP_HISTORY_METHOD = '/app.shop.ShopService/History'
INPUT_INVITATION_METHOD = '/app.invitation.InvitationService/InputInvitationCode'
VIEW_INVITATION_METHOD = '/app.invitation.InvitationService/UpdateInvitationView'
REDEEM_SERIAL_METHOD = '/app.serial_code.SerialCodeService/RedeemSerialCode'
SEND_BATTLE_INVITATION_METHOD = '/app.live.LiveService/SendBattleLiveInvitation'
REMOVE_BATTLE_INVITATION_METHOD = '/app.live.LiveService/RemoveBattleLiveInvitation'
FETCH_BATTLE_INVITATION_METHOD = '/app.live.LiveService/FetchBattleLiveInvitation'
PLAYER_REPORT_METHOD = '/app.friend.FriendService/PlayerReport'

_PROFILE = '''SELECT p.profile_id,p.player_id,p.created_at,e.name,e.updated_at FROM players p
    LEFT JOIN player_profile_edits e ON e.profile_id=p.profile_id'''
_JST = timezone(timedelta(hours=9))


def _read(payload):
    result = {}
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        result.setdefault(number, []).append(value)
    return result


def _text(values, number, default=''):
    value = values.get(number, [default.encode()])[-1]
    if not isinstance(value, bytes):
        raise ValueError('Expected a string field')
    return value.decode('utf8')


def _texts(values, number):
    result = []
    for value in values.get(number, []):
        if not isinstance(value, bytes):
            raise ValueError('Expected repeated strings')
        result.append(value.decode('utf8'))
    return list(dict.fromkeys(result))


def _integer(values, number, default=0):
    value = values.get(number, [default])[-1]
    if not isinstance(value, int):
        raise ValueError('Expected an integer field')
    return value


def _integers(values, number):
    result = []
    for value in values.get(number, []):
        if isinstance(value, bytes):
            position = 0
            while position < len(value):
                item, position = varint(value, position)
                result.append(item)
        else:
            result.append(value)
    return list(dict.fromkeys(result))


def _instant(value):
    if value in (None, ''):
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_JST)
    return int(parsed.timestamp())


class NativeCommonSocial:
    methods = frozenset((GEM_HISTORY_METHOD, SHOP_HISTORY_METHOD, INPUT_INVITATION_METHOD,
        VIEW_INVITATION_METHOD, REDEEM_SERIAL_METHOD, SEND_BATTLE_INVITATION_METHOD,
        REMOVE_BATTLE_INVITATION_METHOD, FETCH_BATTLE_INVITATION_METHOD, PLAYER_REPORT_METHOD))

    def __init__(self, login, master_directory, *, progress, rewards, profile_builder,
                 serial_directory=None, room_lookup=None, clock=time.time):
        self.login, self.progress, self.rewards = login, progress, rewards
        self.profile_builder, self.clock = profile_builder, clock
        self.room_lookup = room_lookup
        self.serial_directory = (Path(serial_directory) if serial_directory is not None else
            ROOT / 'native-server/serial-codes')
        directory = Path(master_directory)
        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']
        self.invitation_rewards = [(r['_resourceType'], r['_resourceId'], r['_resourceCount'])
                                   for r in rows('MasterInvitationReward')]
        self.success_rewards = {r['_id']: (r['_resourceType'], r['_resourceId'], r['_resourceCount'])
                                for r in rows('MasterInvitationSuccessReward')}
        self.report_ids = {r['_id'] for r in rows('MasterPlayerReport')}
        parameters = {r['_id']: r['_value'] for r in rows('MasterParameter')}
        self.invitation_limit = int(parameters['invitation_establishment_max'])
        self.battle_timeout = int(parameters['battle_live_invitation_timeout_sec'])
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS player_invitation_codes (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    code TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS player_invitation_links (
                    invitee_profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    inviter_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    established_at INTEGER NOT NULL,
                    reward_index INTEGER NOT NULL,
                    viewed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS player_invitation_links_inviter
                    ON player_invitation_links(inviter_profile_id);
                CREATE TABLE IF NOT EXISTS player_serial_redemptions (
                    code_key TEXT NOT NULL,
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    redeemed_at INTEGER NOT NULL,
                    resources TEXT NOT NULL,
                    PRIMARY KEY(code_key,profile_id)
                );
                CREATE TABLE IF NOT EXISTS player_battle_invitations (
                    receiver_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    sender_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    host_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    lobby_id INTEGER NOT NULL,
                    sent_at INTEGER NOT NULL,
                    PRIMARY KEY(receiver_profile_id,lobby_id)
                );
                CREATE TABLE IF NOT EXISTS player_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    target_profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    report_type INTEGER NOT NULL,
                    report_ids TEXT NOT NULL,
                    reported_at INTEGER NOT NULL
                );
            ''')

    def _profile(self, connection, row):
        return self.profile_builder(connection, row, self.progress.load(connection, row[0]))

    def invitation_data(self, connection, profile_id):
        new_ids = connection.execute('''SELECT p.player_id FROM player_invitation_links l
            JOIN players p ON p.profile_id=l.invitee_profile_id
            WHERE l.inviter_profile_id=? AND l.viewed_at IS NULL ORDER BY l.reward_index''', (profile_id,))
        entered = connection.execute('SELECT 1 FROM player_invitation_links WHERE invitee_profile_id=?',
                                     (profile_id,)).fetchone() is not None
        return (b''.join(string_field(1, row[0]) for row in new_ids)
                + (integer_field(2, 1) if entered else b''))

    def home_fields(self, connection, profile_id):
        rows = connection.execute('''SELECT invitee_profile_id,established_at FROM player_invitation_links
            WHERE inviter_profile_id=? ORDER BY reward_index''', (profile_id,)).fetchall()
        output = []
        for invitee, established in rows:
            row = connection.execute(_PROFILE + ' WHERE p.profile_id=?', (invitee,)).fetchone()
            output.append(message_field(5, message_field(1, self._profile(connection, row))
                                        + integer_field(2, established)))
        return b''.join(output)

    def _input_code(self, connection, own_id, code, now):
        owner = connection.execute('SELECT profile_id FROM player_invitation_codes WHERE code=?', (code,)).fetchone()
        if owner is None or owner[0] == own_id:
            return b''
        inviter = owner[0]
        previous = connection.execute('SELECT inviter_profile_id FROM player_invitation_links WHERE invitee_profile_id=?',
                                      (own_id,)).fetchone()
        if previous is not None:
            return integer_field(1, int(previous[0] == inviter))
        count = connection.execute('SELECT COUNT(*) FROM player_invitation_links WHERE inviter_profile_id=?',
                                   (inviter,)).fetchone()[0]
        if count >= self.invitation_limit:
            return b''
        invited_state = self.progress.load(connection, own_id)
        inviter_state = self.progress.load(connection, inviter)
        self.rewards.apply(invited_state, self.invitation_rewards, now=now)
        self.rewards.apply(inviter_state, [self.success_rewards[count + 1]], now=now)
        self.progress.save(connection, own_id, invited_state)
        self.progress.save(connection, inviter, inviter_state)
        connection.execute('INSERT INTO player_invitation_links VALUES (?,?,?,?,NULL)',
                           (own_id, inviter, now, count + 1))
        return integer_field(1, 1)

    def _serial_catalog(self):
        result = {}
        for path in sorted(self.serial_directory.glob('*.json')):
            entry = json.loads(path.read_text('utf8'))
            if not entry.get('enabled', True):
                continue
            code = entry['code']
            if not isinstance(code, str) or not code:
                raise ValueError('Serial-code configuration requires a nonempty code')
            key = code.casefold()
            if key in result:
                raise ValueError('Serial-code configuration contains duplicate codes')
            resources = [(row['type'], row['id'], row['count']) for row in entry['resources']]
            if not resources or any(type(kind) is not int or type(master_id) is not int
                    or type(count) is not int or count <= 0 for kind, master_id, count in resources):
                raise ValueError('Serial-code resources require positive integer counts')
            maximum = entry.get('maxUses', 0)
            if type(maximum) is not int or maximum < 0:
                raise ValueError('Serial-code maxUses requires a nonnegative integer')
            result[key] = (resources, _instant(entry.get('startAt')), _instant(entry.get('endAt')), maximum)
        return result

    def _redeem(self, connection, own_id, code, now):
        key = code.casefold()
        entry = self._serial_catalog().get(key)
        if entry is None:
            return string_field(1, 'INVALID')
        resources, start, end, maximum = entry
        if (start is not None and now < start) or (end is not None and now >= end):
            return string_field(1, 'EXPIRED')
        if connection.execute('SELECT 1 FROM player_serial_redemptions WHERE code_key=? AND profile_id=?',
                              (key, own_id)).fetchone() is not None:
            return string_field(1, 'USED')
        if maximum and connection.execute('SELECT COUNT(*) FROM player_serial_redemptions WHERE code_key=?',
                                          (key,)).fetchone()[0] >= maximum:
            return string_field(1, 'REWARD_LIMIT')
        state = self.progress.load(connection, own_id)
        self.rewards.apply(state, resources, now=now)
        self.progress.save(connection, own_id, state)
        connection.execute('INSERT INTO player_serial_redemptions VALUES (?,?,?,?)',
                           (key, own_id, now, json.dumps(resources, separators=(',', ':'))))
        return (string_field(1, 'OK') + b''.join(message_field(2, integer_field(1, kind)
            + integer_field(2, master_id) + integer_field(3, count))
            for kind, master_id, count in resources) + message_field(3, b''))

    def _send_battle(self, connection, player, lobby_id, targets, host_id, now):
        if not 0 < lobby_id <= 0x7fffffffffffffff or any(not target for target in targets):
            raise NativeRpcError(3, diagnostic={'code': 'battle_invitation_request'})
        room = self.room_lookup(str(lobby_id)) if self.room_lookup is not None else None
        if room is None or room['closed']:
            raise NativeRpcError(9, diagnostic={'code': 'battle_invitation_room'})
        participants = [entry for entry in room['players'] if not entry['is_dummy']]
        if player['player_id'] not in {entry['player_id'] for entry in participants}:
            raise NativeRpcError(9, diagnostic={'code': 'battle_invitation_sender'})
        actual_host = participants[0]['player_id']
        if host_id and host_id != actual_host:
            raise NativeRpcError(9, diagnostic={'code': 'battle_invitation_host'})
        host = connection.execute('SELECT profile_id FROM players WHERE player_id=?', (actual_host,)).fetchone()[0]
        peers = []
        for target in targets:
            peer = connection.execute('SELECT profile_id FROM players WHERE player_id=?', (target,)).fetchone()
            if peer is None:
                raise NativeRpcError(5, diagnostic={'code': 'battle_invitation_target'})
            relation = tuple(sorted((player['profile_id'], peer[0])))
            if connection.execute('''SELECT 1 FROM player_friendships
                WHERE lower_profile_id=? AND upper_profile_id=?''', relation).fetchone() is None:
                raise NativeRpcError(9, diagnostic={'code': 'battle_invitation_friend'})
            peers.append(peer[0])
        connection.executemany('''INSERT INTO player_battle_invitations VALUES (?,?,?,?,?)
            ON CONFLICT(receiver_profile_id,lobby_id) DO UPDATE SET
                sender_profile_id=excluded.sender_profile_id,host_profile_id=excluded.host_profile_id,
                sent_at=excluded.sent_at''',
            ((peer, player['profile_id'], host, lobby_id, now) for peer in peers))
        return b''

    def _fetch_battle(self, connection, own_id, now):
        output = []
        for sender, host, lobby_id, sent_at in connection.execute('''SELECT sender_profile_id,host_profile_id,lobby_id,sent_at
            FROM player_battle_invitations WHERE receiver_profile_id=? AND sent_at>?
            ORDER BY sent_at DESC,lobby_id''', (own_id, now - self.battle_timeout)):
            room = self.room_lookup(str(lobby_id)) if self.room_lookup is not None else None
            if room is None or room['closed']:
                continue
            sender_row = connection.execute(_PROFILE + ' WHERE p.profile_id=?', (sender,)).fetchone()
            host_row = connection.execute(_PROFILE + ' WHERE p.profile_id=?', (host,)).fetchone()
            output.append(message_field(1, message_field(1, self._profile(connection, sender_row))
                + integer_field(2, lobby_id) + integer_field(3, sent_at)
                + message_field(4, self._profile(connection, host_row))))
        return b''.join(output)

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            values = _read(payload)
            text = _text(values, 1) if method in (INPUT_INVITATION_METHOD, REDEEM_SERIAL_METHOD, PLAYER_REPORT_METHOD) else ''
            targets = _texts(values, 1) if method == VIEW_INVITATION_METHOD else (
                _texts(values, 2) if method == SEND_BATTLE_INVITATION_METHOD else [])
            lobby = _integer(values, 1) if method in (SEND_BATTLE_INVITATION_METHOD, REMOVE_BATTLE_INVITATION_METHOD) else 0
            host = _text(values, 3) if method == SEND_BATTLE_INVITATION_METHOD else ''
            report_type = _integer(values, 2) if method == PLAYER_REPORT_METHOD else 0
            report_ids = _integers(values, 3) if method == PLAYER_REPORT_METHOD else []
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'common_social_wire'}) from None
        now, own_id = int(self.clock()), player['profile_id']
        read_only = method in (GEM_HISTORY_METHOD, SHOP_HISTORY_METHOD, FETCH_BATTLE_INVITATION_METHOD)
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN' if read_only else 'BEGIN IMMEDIATE')
            if method == GEM_HISTORY_METHOD:
                return history_message(connection, own_id)
            if method == SHOP_HISTORY_METHOD:
                return b''.join(message_field(1, integer_field(1, shop_id) + integer_field(2, purchased))
                    for shop_id, purchased in connection.execute('''SELECT shop_id,executed_at FROM player_local_purchases
                        WHERE profile_id=? ORDER BY id DESC''', (own_id,)))
            if method == INPUT_INVITATION_METHOD:
                return self._input_code(connection, own_id, text, now)
            if method == VIEW_INVITATION_METHOD:
                connection.executemany('''UPDATE player_invitation_links SET viewed_at=COALESCE(viewed_at,?)
                    WHERE inviter_profile_id=? AND invitee_profile_id=(SELECT profile_id FROM players WHERE player_id=?)''',
                    ((now, own_id, target) for target in targets))
                return b''
            if method == REDEEM_SERIAL_METHOD:
                return self._redeem(connection, own_id, text, now)
            if method == SEND_BATTLE_INVITATION_METHOD:
                return self._send_battle(connection, player, lobby, targets, host, now)
            if method == FETCH_BATTLE_INVITATION_METHOD:
                return self._fetch_battle(connection, own_id, now)
            if method == REMOVE_BATTLE_INVITATION_METHOD:
                if not 0 < lobby <= 0x7fffffffffffffff:
                    raise NativeRpcError(3)
                connection.execute('DELETE FROM player_battle_invitations WHERE receiver_profile_id=? AND lobby_id=?',
                                   (own_id, lobby))
                return b''
            target = connection.execute('SELECT profile_id FROM players WHERE player_id=?', (text,)).fetchone()
            if target is None:
                raise NativeRpcError(5)
            if target[0] == own_id or report_type not in (1, 2, 3) or not report_ids or not set(report_ids) <= self.report_ids:
                raise NativeRpcError(3, diagnostic={'code': 'player_report_values'})
            connection.execute('''INSERT INTO player_reports
                (reporter_profile_id,target_profile_id,report_type,report_ids,reported_at) VALUES(?,?,?,?,?)''',
                (own_id, target[0], report_type, json.dumps(report_ids, separators=(',', ':')), now))
            return b''

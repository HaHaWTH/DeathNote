"""Local BattleLive transport for the original 1.0.6 Mingle client.

The SRLV envelope is big endian; its individual payloads use .NET
BinaryReader/BinaryWriter little endian scalars and seven bit UTF-8 lengths.
"""
from __future__ import annotations

import copy
import dataclasses
import secrets
import socket
import socketserver
import struct
import threading
import time
import uuid


MAGIC = b'SRLV'
VERSION = 1
MAX_FRAME_BODY = 16 * 1024 * 1024


def varint(value):
    value = int(value) & ((1 << 64) - 1)
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def wire_string(value):
    data = value.encode('utf8')
    return varint(len(data)) + data


def decode_session_property(wire):
    """Mingle.SessionProperty.DecodeWire, 1.1.0 0xaf047dc.

    SRLV stores encoded strings on the wire. Keep those strings for responses;
    consumers of room snapshots need their original int/bool/string values.
    The original decoder also accepts unprefixed legacy string properties.
    """
    if len(wire) < 2 or wire[1] != ':':
        return wire
    kind, value = wire[0], wire[2:]
    if kind == 's':
        return value
    if kind == 'b':
        return value == '1'
    if kind == 'i':
        number = value.strip(' \t\n\r\v\f')
        digits = number[1:] if number[:1] in ('+', '-') else number
        if digits and all('0' <= digit <= '9' for digit in digits):
            parsed = int(number)
            if -(1 << 31) <= parsed < (1 << 31):
                return parsed
        return value
    return wire


class WireReader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.position = 0

    def take(self, size):
        if size < 0 or self.position + size > len(self.data):
            raise ValueError('Truncated Mingle payload')
        start = self.position
        self.position += size
        return bytes(self.data[start:self.position])

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def uint(self):
        return self.unpack('<I')

    def integer(self):
        return self.unpack('<i')

    def long(self):
        return self.unpack('<q')

    def boolean(self):
        return bool(self.unpack('<B'))

    def varint(self):
        value = 0
        for shift in range(0, 70, 7):
            byte = self.unpack('<B')
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('Mingle varint exceeds 64 bits')

    def string(self):
        return self.take(self.varint()).decode('utf8')

    def end(self):
        if self.position != len(self.data):
            raise ValueError('Unexpected trailing Mingle payload bytes')


@dataclasses.dataclass(frozen=True)
class Envelope:
    kind: int
    sequence: int
    timestamp_ms: int
    session: str
    request_id: int
    body: bytes


def decode_envelope(data):
    reader = WireReader(data)
    magic, version, kind, length = struct.unpack('>4sBBI', reader.take(10))
    if magic != MAGIC or version != VERSION or length != len(data) - 10:
        raise ValueError('Invalid Mingle SRLV header')
    sequence, timestamp = struct.unpack('>Iq', reader.take(12))
    session = reader.string()
    request_id = reader.unpack('>I')
    body = reader.take(len(data) - reader.position)
    return Envelope(kind, sequence, timestamp, session, request_id, body)


def encode_envelope(kind, sequence, session, request_id, body, *, timestamp_ms=None):
    stamp = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    tail = struct.pack('>Iq', sequence, stamp) + wire_string(session) + struct.pack('>I', request_id) + body
    return struct.pack('>4sBBI', MAGIC, VERSION, kind, len(tail)) + tail


def _signed(value, bits):
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


def read_typed(reader):
    tag = reader.unpack('<B')
    if tag < 2:
        return bool(tag)
    if tag == 2:
        return _signed(reader.varint(), 32)
    if tag == 3:
        return _signed(reader.varint(), 64)
    if tag == 4:
        return reader.unpack('<f')
    if tag == 5:
        return reader.unpack('<d')
    if tag == 6:
        return reader.string()
    if tag == 7:
        return reader.take(reader.varint())
    if tag == 8:
        return [read_typed(reader) for _ in range(reader.varint())]
    raise ValueError(f'Unknown Mingle typed value {tag}')


def write_typed(value):
    if isinstance(value, bool):
        return bytes((int(value),))
    if isinstance(value, int):
        return bytes((2 if -(1 << 31) <= value < (1 << 31) else 3,)) + varint(value)
    if isinstance(value, float):
        return b'\x05' + struct.pack('<d', value)
    if isinstance(value, str):
        return b'\x06' + wire_string(value)
    if isinstance(value, bytes):
        return b'\x07' + varint(len(value)) + value
    if isinstance(value, (list, tuple)):
        return b'\x08' + varint(len(value)) + b''.join(map(write_typed, value))
    raise TypeError(f'Unsupported Mingle value {type(value).__name__}')


def decode_typed_dict(data):
    reader = WireReader(data)
    result = {}
    for _ in range(reader.varint()):
        key = reader.string()
        result[key] = read_typed(reader)
    reader.end()
    return result


def encode_typed_dict(values):
    return varint(len(values)) + b''.join(wire_string(key) + write_typed(value) for key, value in values.items())


def _property_key(properties, name):
    return next((key for key in properties if key == name or key.endswith('.' + name) or key.endswith('_' + name)), name)


def _read_property(properties, name, default=b''):
    return properties.get(_property_key(properties, name), (default, 0))[0]


def _set_property(properties, name, value):
    key = _property_key(properties, name)
    previous = properties.get(key, (b'', 0))
    properties[key] = (value, previous[1] + 1)


@dataclasses.dataclass
class NetworkObject:
    object_id: int
    prefab_id: int
    input_authority: int
    state_authority: int
    prefab: str
    properties: dict


def read_spawn_request(body):
    reader = WireReader(body)
    obj = NetworkObject(reader.uint(), reader.uint(), reader.integer() + 1,
                        reader.integer() + 1, reader.string(), {})
    for _ in range(reader.integer()):
        key = reader.string()
        data = reader.take(reader.integer())
        version = reader.integer()
        obj.properties[key] = (data, version)
    reader.end()
    return obj


def encode_spawn(obj):
    return (struct.pack('<IIii', obj.object_id, obj.prefab_id, obj.input_authority - 1,
                        obj.state_authority - 1) + wire_string(obj.prefab) + varint(len(obj.properties)) +
            b''.join(wire_string(key) + b'\x08' + varint(len(value)) + value
                     for key, (value, _) in obj.properties.items()))


def read_delta(body):
    reader = WireReader(body)
    timestamp = reader.long()
    result = []
    for _ in range(reader.integer()):
        object_id = reader.uint()
        name = reader.string()
        value = reader.take(reader.integer())
        version = reader.integer()
        result.append((object_id, name, value, version))
    reader.end()
    return timestamp, result


def encode_delta(changes, *, timestamp_ms=None):
    stamp = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    return struct.pack('<qi', stamp, len(changes)) + b''.join(
        struct.pack('<I', object_id) + wire_string(name) + struct.pack('<i', len(value)) + value +
        struct.pack('<i', version) for object_id, name, value, version in changes)


@dataclasses.dataclass
class BattleRoom:
    room_id: str
    epoch_ms: int
    properties: dict
    players: list
    objects: dict = dataclasses.field(default_factory=dict)
    dummy_objects: list = dataclasses.field(default_factory=list)
    music_id: int = 0
    closed: bool = False
    join_closed: bool = False
    results: dict = dataclasses.field(default_factory=dict)
    reliable: list = dataclasses.field(default_factory=list)
    result_consumed: bool = False
    room_uuid: str = ''
    npc_log: bytes = b''
    dummy_logs_sent: bool = False
    mode: str = 'battle'


class _BattleTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        if self.owner.ssl_context is not None:
            try:
                connection = self.owner.ssl_context.wrap_socket(connection, server_side=True)
            except BaseException:
                connection.close()
                raise
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return connection, address


class _BattleConnection(socketserver.BaseRequestHandler):
    def setup(self):
        self.owner = self.server.owner
        self.sequence = 0
        self.session_id = 0
        self.profile_id = 0
        self.player_name = ''
        self.room = None
        self.send_lock = threading.Lock()
        with self.owner.lock:
            self.owner.connections.add(self)

    def send(self, kind, body=b'', request_id=0):
        with self.send_lock:
            self.sequence += 1
            session = str(self.session_id) if self.session_id else ''
            self.request.sendall(encode_envelope(kind, self.sequence, session, request_id, body))

    def _read_exactly(self, count):
        parts = bytearray()
        while len(parts) < count:
            part = self.request.recv(count - len(parts))
            if not part:
                raise EOFError
            parts.extend(part)
        return bytes(parts)

    def handle(self):
        try:
            while not self.owner.stopping.is_set():
                header = self._read_exactly(10)
                magic, version, _, length = struct.unpack('>4sBBI', header)
                if magic != MAGIC or version != VERSION or not 17 <= length <= MAX_FRAME_BODY:
                    raise ValueError('Invalid Mingle frame length/header')
                envelope = decode_envelope(header + self._read_exactly(length))
                self.owner.receive(self, envelope)
        except EOFError:
            pass
        except (OSError, ValueError, UnicodeError, struct.error) as error:
            self.owner.log('battle_transport_connection_error', {'error': str(error), 'roomId': self.room.room_id if self.room else None})

    def finish(self):
        with self.owner.lock:
            self.owner.connections.discard(self)
            if self.room is not None:
                self.room.players[0]['disconnected'] = True
        self.request.close()


class NativeBattleTransport:
    """One real client per room, four local dummy participants."""
    def __init__(self, host='0.0.0.0', port=9001, *, ssl_context=None, dummy_count=4, logger=None):
        if dummy_count not in range(5):
            raise ValueError('BattleLive supports at most five participants')
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.dummy_count = dummy_count
        self.logger = logger
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.connections = set()
        self.rooms = {}
        self.server = None
        self.thread = None
        self.next_session_id = 1

    def log(self, event, details):
        if self.logger is not None:
            self.logger(event, details)

    def start(self):
        with self.lock:
            if self.server is not None:
                return
            server = _BattleTCPServer((self.host, self.port), _BattleConnection, bind_and_activate=False)
            server.owner = self
            try:
                server.server_bind()
                server.server_activate()
            except BaseException:
                server.server_close()
                raise
            self.server = server
            self.port = server.server_address[1]
            self.stopping.clear()
            self.thread = threading.Thread(target=server.serve_forever, name='native-battle-transport', daemon=True)
            self.thread.start()
        self.log('battle_transport_started', {'host': self.host, 'port': self.port, 'tls': self.ssl_context is not None, 'dummyCount': self.dummy_count})

    def stop(self):
        with self.lock:
            server, self.server = self.server, None
            self.stopping.set()
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if server is not None:
            server.shutdown()
            server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None

    def snapshot(self):
        with self.lock:
            return {'running': self.server is not None, 'host': self.host, 'port': self.port,
                    'tls': self.ssl_context is not None, 'dummyCount': self.dummy_count,
                    'connections': len(self.connections), 'rooms': len(self.rooms)}

    def room_snapshot(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id) if room_id else next(reversed(self.rooms.values()), None)
            if room is None:
                for candidate in self.rooms.values():
                    if candidate.room_uuid == room_id:
                        room = candidate
                        break
            if room is None:
                return None
            return copy.deepcopy({'room_id': room_id or room.room_id, 'session_id': room.room_id,
                                  'search_id': int(room.room_id) if room.room_id.isdecimal() else 0,
                                  'players': room.players, 'music_id': room.music_id,
                                  'closed': room.closed, 'join_closed': room.join_closed, 'results': room.results,
                                  'reliable': room.reliable, 'mode': room.mode,
                                  'properties': {key: decode_session_property(value)
                                                 for key, value in room.properties.items()}})

    def reserve_room(self, player_id, profile_id):
        with self.lock:
            for room_id, room in list(self.rooms.items()):
                if room.players[0]['disconnected']:
                    del self.rooms[room_id]
            room_id = str(secrets.randbelow(900_000) + 100_000)
            while room_id in self.rooms:
                room_id = str(secrets.randbelow(900_000) + 100_000)
            players = self._make_players(player_id, profile_id, '', self.dummy_count + 1)
            self.rooms[room_id] = BattleRoom(room_id, int(time.time() * 1000), {}, players)
            return self.room_snapshot(room_id)

    def release_room(self, room_id):
        with self.lock:
            snapshot = self.room_snapshot(room_id)
            if snapshot is None:
                return
            room = self.rooms[snapshot['session_id']]
            room.result_consumed = True
            room.reliable.clear()
            room.results.clear()
            room.npc_log = b''
            if room.players[0]['disconnected']:
                del self.rooms[room.room_id]

    def complete_room(self, room_id, npc_log):
        """Queue the finished NPC log for the original CompleteLive barrier."""
        with self.lock:
            snapshot = self.room_snapshot(room_id)
            if snapshot is None:
                return False
            room = self.rooms[snapshot['session_id']]
            if room.closed or room.players[0]['disconnected']:
                return False
            if room.dummy_logs_sent:
                return True
            room.npc_log = npc_log
            connection = next((item for item in self.connections if item.room is room), None)
            if connection is not None:
                try:
                    self._send_dummy_logs(connection)
                except OSError as error:
                    self.log('battle_transport_connection_error', {'error': str(error), 'roomId': room.room_id})
                    return False
            return True

    def _send_dummy_logs(self, connection):
        room = connection.room
        if not room.npc_log or room.dummy_logs_sent or not room.dummy_objects:
            return
        real = next((obj for obj in room.objects.values() if obj.input_authority == 1), None)
        if real is None or struct.unpack('<i', _read_property(real.properties, 'PlayerStateNetworkProperty'))[0] < 40:
            return
        for dummy_id in room.dummy_objects:
            dummy = room.objects[dummy_id]
            token = struct.unpack('<i', _read_property(dummy.properties, 'AuthorityConnectTokenCacheNetwork'))[0]
            data = struct.pack('<iiB', token, 0, 0) + room.npc_log
            body = struct.pack('<6i', dummy.input_authority - 1, 1, 0, 0, 0, len(data)) + data
            connection.send(33, body)
        room.dummy_logs_sent = True
        self.log('battle_dummy_logs', {'roomId': room.room_id, 'count': len(room.dummy_objects),
                                      'logBytes': len(room.npc_log), 'reliableKey': [1, 0, 0, 0]})

    @staticmethod
    def _make_players(player_id, profile_id, player_name, count):
        players = [{'player_ref': 1, 'profile_id': profile_id, 'name': player_name,
                    'player_id': player_id, 'is_dummy': False, 'disconnected': False}]
        players.extend({'player_ref': number + 2, 'profile_id': 0, 'name': f'Dummy {number + 1}',
                        'player_id': str(uuid.uuid4()), 'is_dummy': True, 'disconnected': False}
                       for number in range(count - 1))
        return players

    @staticmethod
    def _session_info(room):
        return (struct.pack('<??i', not room.join_closed, not room.join_closed, len(room.properties)) +
                b''.join(wire_string(key) + wire_string(value) for key, value in room.properties.items()))

    def receive(self, connection, envelope):
        kind = envelope.kind
        with self.lock:
            if kind == 1:
                self._handshake(connection, envelope)
                return
            if not connection.session_id:
                raise ValueError('Mingle message before handshake')
            if kind == 3:
                connection.send(4, envelope.body, envelope.request_id)
                return
            if kind == 10:
                self._join(connection, envelope)
                return
            if kind == 12:
                self._leave(connection, envelope)
                return
            room = connection.room
            if room is None:
                raise ValueError('Mingle room message before JoinRoom')
            if kind == 40:
                self._spawn(connection, envelope)
            elif kind == 21:
                self._delta(connection, envelope)
            elif kind == 30:
                self._rpc(connection, envelope)
            elif kind == 33:
                self._reliable(connection, envelope)
            elif kind == 41:
                reader = WireReader(envelope.body)
                object_id = reader.uint()
                reader.end()
                room.objects.pop(object_id, None)
            elif kind == 42:
                reader = WireReader(envelope.body)
                object_id, authority = reader.uint(), reader.integer() + 1
                reader.end()
                room.objects[object_id].state_authority = authority
                connection.send(42, envelope.body, envelope.request_id)
            elif kind == 61:
                room.join_closed = True
                connection.send(60, self._session_info(room))
            elif kind == 62:
                reader = WireReader(envelope.body)
                for _ in range(reader.integer()):
                    key = reader.string()
                    room.properties[key] = reader.string()
                reader.end()
                connection.send(60, self._session_info(room))
            elif kind == 5:
                self._leave(connection, envelope)
            else:
                raise ValueError(f'Unsupported Mingle message type {kind}')

    def _handshake(self, connection, envelope):
        reader = WireReader(envelope.body)
        connection.profile_id = reader.long()
        connection.player_name = reader.string()
        reader.end()
        connection.session_id = self.next_session_id
        self.next_session_id += 1
        connection.send(2, struct.pack('<?I', True, connection.session_id), envelope.request_id)
        self.log('battle_handshake', {'profileId': connection.profile_id})

    def _join(self, connection, envelope):
        reader = WireReader(envelope.body)
        requested_room = reader.string()
        enable_creation = reader.boolean()
        player_limit = reader.integer()
        properties = {}
        for _ in range(reader.integer()):
            key = reader.string()
            properties[key] = reader.string()
        reader.end()
        if not enable_creation and requested_room not in self.rooms:
            connection.send(11, b'\x00' + struct.pack('<i', 1) + wire_string('Room not found'), envelope.request_id)
            self.log('battle_join_rejected', {'reason': 'room_not_found', 'propertyCount': len(properties)})
            return
        room_id = requested_room or str(uuid.uuid4())
        existing = self.rooms.get(room_id)
        if existing is not None and existing.objects and not existing.players[0]['disconnected']:
            connection.send(11, b'\x00' + struct.pack('<i', 2) + wire_string('Local room already has a player'), envelope.request_id)
            self.log('battle_join_rejected', {'reason': 'room_has_player', 'propertyCount': len(properties)})
            return
        count = min(player_limit, self.dummy_count + 1)
        player_id = existing.players[0]['player_id'] if existing is not None else ''
        players = self._make_players(player_id, connection.profile_id, connection.player_name, count)
        room = BattleRoom(room_id, int(time.time() * 1000), properties, players)
        session_type = decode_session_property(properties.get('type', ''))
        if isinstance(session_type, str) and session_type.startswith('arena_live_fom_lobby_'):
            room.mode = 'arena'
        for old_id, old in list(self.rooms.items()):
            if old.players[0]['disconnected']:
                del self.rooms[old_id]
        self.rooms[room_id] = room
        connection.room = room
        response = (b'\x01' + wire_string(room_id) + struct.pack('<iiqi', 0, 1, room.epoch_ms, 0) +
                    struct.pack('<??i', True, True, len(properties)) +
                    b''.join(wire_string(key) + wire_string(value) for key, value in properties.items()))
        connection.send(11, response, envelope.request_id)
        self.log('battle_join', {'roomId': room_id, 'playerCount': count, 'propertyCount': len(properties),
                                 'responseBytes': len(response), 'mode': room.mode,
                                 'sessionType': session_type,
                                 'band': decode_session_property(properties.get('band', ''))})

    def _leave(self, connection, envelope):
        if connection.room is not None:
            room = connection.room
            room.players[0]['disconnected'] = True
            room.closed = True
            room.objects.clear()
            room.dummy_objects.clear()
            room.npc_log = b''
            if room.result_consumed:
                self.rooms.pop(room.room_id, None)
            connection.room = None
        connection.send(13, b'\x01', envelope.request_id)

    def _spawn(self, connection, envelope):
        obj = read_spawn_request(envelope.body)
        room = connection.room
        room.objects[obj.object_id] = obj
        self._update_identity(room, obj)
        for candidate in list(room.objects.values()):
            self._ensure_dummies(connection, candidate)
        self.log('battle_spawn', {'roomId': room.room_id, 'objectId': obj.object_id, 'prefabId': obj.prefab_id,
                                 'inputAuthority': obj.input_authority, 'stateAuthority': obj.state_authority,
                                 'propertyCount': len(obj.properties), 'propertyNames': sorted(obj.properties)})

    def _update_identity(self, room, obj):
        room_uuid = _read_property(obj.properties, 'RoomUuId')
        if len(room_uuid) == 16 and any(room_uuid):
            room.room_uuid = str(uuid.UUID(bytes_le=room_uuid))
        if obj.input_authority != 1:
            return
        value = _read_property(obj.properties, 'PlayerIdNetworkProperty')
        if value:
            room.players[0]['player_id'] = value.decode('utf8')
        music = _read_property(obj.properties, 'SelectedBattleLiveMusicId')
        if len(music) == 8:
            room.music_id = struct.unpack('<q', music)[0] or room.music_id

    def _ensure_dummies(self, connection, template):
        room = connection.room
        formation = _read_property(template.properties, 'FormationDataBytesNetworkProperty')
        shared = any(obj.input_authority == 0 for obj in room.objects.values())
        if room.dummy_objects or not shared or not any(formation):
            return
        for player in room.players[1:]:
            props = copy.deepcopy(template.properties)
            overrides = {
                'AuthorityConnectTokenCacheNetwork': struct.pack('<i', player['player_ref']),
                'IsNPC': b'\x01', 'PlayerIdNetworkProperty': player['player_id'].encode('utf8'),
                'PlayerNameNetworkProperty': player['name'].encode('utf8'),
                'PlayerProfileCardUrlNetworkProperty': b'', 'PlayerStateNetworkProperty': struct.pack('<i', 10),
                'SpawnTimeMillSecNetworkProperty': struct.pack('<i',
                    struct.unpack('<i', _read_property(template.properties, 'SpawnTimeMillSecNetworkProperty'))[0]
                    + player['player_ref'] - 1),
                'IsBattleLiveMatchingReady': b'\x01', 'IsResourceLoadComplete': b'\x01',
                'LoadedResourceSize': struct.pack('<q', 1), 'TotalResourceSize': struct.pack('<q', 1),
                '_playerColorIndex': struct.pack('<i', player['player_ref'] - 1), '_updatedPlayerColor': b'\x01',
            }
            for name, value in overrides.items():
                _set_property(props, name, value)
            obj = NetworkObject(1_000_000 + player['player_ref'], template.prefab_id,
                                player['player_ref'], player['player_ref'], template.prefab, props)
            room.objects[obj.object_id] = obj
            room.dummy_objects.append(obj.object_id)
            connection.send(50, struct.pack('<i', player['player_ref'] - 1))
            connection.send(40, encode_spawn(obj))
        self.log('battle_dummy_spawn', {'roomId': room.room_id, 'count': len(room.dummy_objects),
                                       'playerRefs': [player['player_ref'] for player in room.players[1:]]})
        self._log_barrier(room, 'spawn')

    def _log_barrier(self, room, stage):
        states = []
        for obj in room.objects.values():
            if obj.input_authority == 0:
                continue
            states.append({'playerRef': obj.input_authority,
                           'state': struct.unpack('<i', _read_property(obj.properties, 'PlayerStateNetworkProperty'))[0],
                           'spawnTimeMs': struct.unpack('<i', _read_property(obj.properties, 'SpawnTimeMillSecNetworkProperty'))[0],
                           'ready': bool(_read_property(obj.properties, 'IsBattleLiveMatchingReady')[0]),
                           'loaded': bool(_read_property(obj.properties, 'IsResourceLoadComplete')[0]),
                           'confirmed': bool(_read_property(obj.properties, 'HasConfirmedMemberTokens')[0]),
                           'confirmedCount': struct.unpack('<i', _read_property(obj.properties, '_confirmedMemberTokenCount'))[0]})
        self.log('battle_state_barrier', {'roomId': room.room_id, 'stage': stage, 'players': states})

    def _delta(self, connection, envelope):
        _, changes = read_delta(envelope.body)
        room = connection.room
        dummy_changes = []
        for object_id, name, value, version in changes:
            obj = room.objects[object_id]
            obj.properties[name] = (value, version)
            self._update_identity(room, obj)
        for object_id in dict.fromkeys(change[0] for change in changes):
            obj = room.objects[object_id]
            self._ensure_dummies(connection, obj)
        barrier_changed = False
        for object_id, name, value, version in changes:
            obj = room.objects[object_id]
            suffix = name.rsplit('.', 1)[-1]
            barrier_changed |= suffix in {'PlayerStateNetworkProperty', 'IsBattleLiveMatchingReady',
                                          'IsResourceLoadComplete', 'HasConfirmedMemberTokens',
                                          '_confirmedMemberTokenCount', '_confirmedMemberTokens',
                                          'SpawnTimeMillSecNetworkProperty'}
            if obj.input_authority != 1:
                continue
            mirror = suffix in {'PlayerStateNetworkProperty', 'SelectedLiveDifficulty',
                                'SelectedBattleLiveMusicId', 'HasConfirmedMemberTokens',
                                '_confirmedMemberTokenCount', '_confirmedMemberTokens',
                                'FormationDataBytesNetworkProperty', 'SpawnTimeMillSecNetworkProperty'}
            if mirror:
                for dummy_id in room.dummy_objects:
                    dummy = room.objects[dummy_id]
                    mirrored_value = value
                    if suffix == 'SpawnTimeMillSecNetworkProperty':
                        real_spawn_time = struct.unpack('<i', value)[0]
                        mirrored_value = struct.pack('<i', real_spawn_time + dummy.input_authority - 1)
                    elif suffix == 'PlayerStateNetworkProperty':
                        current = struct.unpack('<i', _read_property(dummy.properties, suffix))[0]
                        incoming = struct.unpack('<i', value)[0]
                        mirrored_value = struct.pack('<i', max(current, incoming))
                        if mirrored_value == _read_property(dummy.properties, suffix):
                            continue
                    _set_property(dummy.properties, suffix, mirrored_value)
                    dummy_key = _property_key(dummy.properties, suffix)
                    raw, revision = dummy.properties[dummy_key]
                    dummy_changes.append((dummy_id, dummy_key, raw, revision))
        if dummy_changes:
            connection.send(21, encode_delta(dummy_changes))
        self._send_dummy_logs(connection)
        if barrier_changed:
            self._log_barrier(room, 'delta')

    def _rpc(self, connection, envelope):
        payload = decode_typed_dict(envelope.body)
        room = connection.room
        name = payload['RpcName']
        object_id = int(payload.get('ObjectId', 0))
        args = payload['Arguments']
        source = room.objects[object_id]
        if name == 'SetBattleLiveMusicIdRpc' and args:
            room.music_id = int(args[0])
        if name == 'InitializeStatusRpc' and source.input_authority == 1:
            for index, dummy_id in enumerate(room.dummy_objects):
                clone = dict(payload, ObjectId=dummy_id, Arguments=[room.players[index + 1]['name']])
                connection.send(32, encode_typed_dict(clone))
        elif name == 'SendGekisouResultRpc' and args and source.input_authority == 1:
            for dummy_id in room.dummy_objects:
                zero = list(args)
                zero[2:7] = [0] * 5
                zero[7] = True
                zero[8:] = [0] * len(zero[8:])
                connection.send(32, encode_typed_dict(dict(payload, ObjectId=dummy_id, Arguments=zero)))
        elif name == 'SendGekisouRankingScoreBonusRpc' and source.input_authority == 1:
            for dummy_id in room.dummy_objects:
                connection.send(32, encode_typed_dict(dict(payload, ObjectId=dummy_id, Arguments=[args[0], 0])))
        elif name == 'SetScoreRpc' and source.input_authority == 1:
            for dummy_id in room.dummy_objects:
                connection.send(32, encode_typed_dict(dict(payload, ObjectId=dummy_id, Arguments=[0, args[1]])))
        if envelope.request_id:
            connection.send(31, encode_typed_dict({'success': True}), envelope.request_id)
        if name not in {'SetScoreRpc', 'SendGekisouResultRpc', 'SendGekisouRankingScoreBonusRpc'}:
            self.log('battle_rpc', {'roomId': room.room_id, 'rpc': name, 'objectId': object_id,
                                    'argCount': len(args), 'fieldNames': sorted(payload)})
        if name in {'ReceivedLiveReadyRpc', 'SetLiveStartSimulateTimeRpc'}:
            self._log_barrier(room, name)

    def _reliable(self, connection, envelope):
        reader = WireReader(envelope.body)
        target = reader.integer() + 1
        key = [reader.integer() for _ in range(4)]
        data = reader.take(reader.integer())
        reader.end()
        room = connection.room
        room.reliable.append({'sender': 1, 'target': target, 'key': key, 'data': data})
        if target == 1:
            connection.send(33, struct.pack('<5ii', 0, *key, len(data)) + data)
        self.log('battle_reliable', {'roomId': room.room_id, 'target': target, 'key': key, 'bytes': len(data)})

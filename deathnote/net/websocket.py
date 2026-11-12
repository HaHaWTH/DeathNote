"""Small RFC 6455 stream transport for the native client's binary chat socket."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import struct


MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class _ProtocolError(Exception):
    def __init__(self, code, reason):
        self.code = code
        self.reason = reason


def _close_payload(code, reason):
    if not 1000 <= code < 5000 or code in (1004, 1005, 1006, 1015):
        raise ValueError('Invalid WebSocket close code')
    text = reason.encode('utf8')
    if len(text) > 123:
        raise ValueError('WebSocket close reason exceeds a control frame')
    return struct.pack('>H', code) + text


class NativeWebSocket:
    """One upgraded stream; handlers exchange complete binary/text messages."""

    def __init__(self, reader, writer, path, headers, notify):
        self.reader = reader
        self.writer = writer
        self.path = path
        self.headers = headers
        self.notify = notify
        self.closed = False
        self._close_sent = False
        self._write_lock = asyncio.Lock()
        self._fragment_opcode = None
        self._fragments = bytearray()

    async def _send_frame(self, opcode, payload):
        size = len(payload)
        header = bytes((0x80 | opcode, size if size < 126 else 126 if size < 65536 else 127))
        if size >= 65536:
            header += struct.pack('>Q', size)
        elif size >= 126:
            header += struct.pack('>H', size)
        async with self._write_lock:
            self.writer.write(header + payload)
            await self.writer.drain()

    async def send_binary(self, payload):
        if not isinstance(payload, bytes):
            raise TypeError('WebSocket binary message must be bytes')
        await self.send(payload)

    async def send(self, payload):
        if self.closed:
            raise ConnectionError('WebSocket is closed')
        if isinstance(payload, str):
            opcode, raw = 1, payload.encode('utf8')
        elif isinstance(payload, bytes):
            opcode, raw = 2, payload
        else:
            raise TypeError('WebSocket message must be bytes or str')
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError('WebSocket message exceeds its configured limit')
        await self._send_frame(opcode, raw)
        await self.notify({'event': 'websocket_message', 'direction': 'out',
                           'opcode': opcode, 'messageBytes': len(raw)})

    async def close(self, code=1000, reason=''):
        if self.closed:
            return
        payload = _close_payload(code, reason)
        self.closed = True
        if not self._close_sent:
            self._close_sent = True
            await self._send_frame(8, payload)
        await self.notify({'event': 'websocket_close', 'code': code, 'initiatedBy': 'server'})

    async def _read_frame(self):
        head = await self.reader.readexactly(2)
        final, opcode = bool(head[0] & 0x80), head[0] & 15
        if head[0] & 0x70 or opcode not in (0, 1, 2, 8, 9, 10):
            raise _ProtocolError(1002, 'Unsupported frame flags')
        if not head[1] & 0x80:
            raise _ProtocolError(1002, 'Client frames must be masked')
        size = head[1] & 127
        if size == 126:
            size = struct.unpack('>H', await self.reader.readexactly(2))[0]
            if size < 126:
                raise _ProtocolError(1002, 'Nonminimal frame length')
        elif size == 127:
            size = struct.unpack('>Q', await self.reader.readexactly(8))[0]
            if size < 65536 or size >> 63:
                raise _ProtocolError(1002, 'Invalid frame length')
        if opcode >= 8 and (not final or size > 125):
            raise _ProtocolError(1002, 'Invalid control frame')
        if size > MAX_MESSAGE_BYTES:
            raise _ProtocolError(1009, 'Message exceeds limit')
        mask = await self.reader.readexactly(4)
        payload = bytearray(await self.reader.readexactly(size))
        for index in range(size):
            payload[index] ^= mask[index & 3]
        return final, opcode, payload

    async def recv(self):
        """Return a full message, or None after a close/EOF/protocol rejection."""
        if self.closed:
            return None
        try:
            while True:
                final, opcode, payload = await self._read_frame()
                if opcode == 9:
                    await self._send_frame(10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode == 8:
                    if len(payload) == 1:
                        raise _ProtocolError(1002, 'Invalid close payload')
                    code = 1000
                    if payload:
                        code = struct.unpack_from('>H', payload)[0]
                        try:
                            _close_payload(code, bytes(payload[2:]).decode('utf8'))
                        except UnicodeError:
                            raise _ProtocolError(1007, 'Invalid close text') from None
                        except ValueError:
                            raise _ProtocolError(1002, 'Invalid close code') from None
                    self.closed = True
                    if not self._close_sent:
                        self._close_sent = True
                        await self._send_frame(8, payload)
                    await self.notify({'event': 'websocket_close', 'code': code, 'initiatedBy': 'client'})
                    return None
                if opcode == 0:
                    if self._fragment_opcode is None:
                        raise _ProtocolError(1002, 'Continuation without a message')
                    if len(self._fragments) + len(payload) > MAX_MESSAGE_BYTES:
                        raise _ProtocolError(1009, 'Message exceeds limit')
                    self._fragments.extend(payload)
                    if not final:
                        continue
                    opcode, payload = self._fragment_opcode, self._fragments
                    self._fragment_opcode, self._fragments = None, bytearray()
                else:
                    if self._fragment_opcode is not None:
                        raise _ProtocolError(1002, 'Interrupted fragmented message')
                    if not final:
                        self._fragment_opcode, self._fragments = opcode, payload
                        continue
                try:
                    message = bytes(payload).decode('utf8') if opcode == 1 else bytes(payload)
                except UnicodeError:
                    raise _ProtocolError(1007, 'Invalid message text') from None
                await self.notify({'event': 'websocket_message', 'direction': 'in',
                                   'opcode': opcode, 'messageBytes': len(payload)})
                return message
        except _ProtocolError as error:
            await self.close(error.code, error.reason)
            return None
        except (asyncio.IncompleteReadError, OSError):
            self.closed = True
            return None


async def serve_websocket(reader, writer, method, path, version, headers, handler, notify):
    """Validate an HTTP Upgrade then run one async application handler."""
    socket = None
    try:
        connection = {part.strip().lower() for part in headers.get('connection', '').split(',')}
        try:
            key = headers.get('sec-websocket-key', '').encode('ascii')
            nonce = base64.b64decode(key, validate=True)
        except (UnicodeError, binascii.Error):
            nonce = b''
        valid = (method == 'GET' and version == 'HTTP/1.1' and path == '/ws'
                 and headers.get('upgrade', '').lower() == 'websocket' and 'upgrade' in connection
                 and headers.get('sec-websocket-version') == '13' and len(nonce) == 16)
        if not valid:
            writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            await writer.drain()
            await notify({'event': 'websocket_upgrade', 'path': '/ws', 'httpStatus': 400})
            return
        accept = base64.b64encode(hashlib.sha1(key + _GUID).digest())
        writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                     b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        await writer.drain()
        await notify({'event': 'websocket_upgrade', 'path': '/ws', 'httpStatus': 101})
        socket = NativeWebSocket(reader, writer, path, headers, notify)
        await handler(socket)
    except OSError:
        pass
    except Exception as error:
        await notify({'event': 'websocket_error', 'errorType': type(error).__name__})
        if socket is not None:
            try:
                await socket.close(1011, 'Application error')
            except OSError:
                pass
    finally:
        if socket is not None:
            try:
                await socket.close()
            except OSError:
                pass
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        await notify({'event': 'connection_close', 'httpVersion': '1.1', 'websocket': True})

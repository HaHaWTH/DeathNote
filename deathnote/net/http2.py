"""HTTP/2 unary gRPC transport for the explicit native-client prototype.

``dispatch(path, payload, headers)`` is synchronous and runs in a worker thread.
It returns ``(protobuf_bytes, metadata_dict)``. ``NotImplementedError`` maps to
UNIMPLEMENTED (12); other dispatch failures map to INTERNAL (13). Invalid frames
use INVALID_ARGUMENT (3), oversized messages use RESOURCE_EXHAUSTED (8).
NativeRpcError preserves an application's explicit status; successful calls
have status 0. Metadata supplies ordinary response
headers, never HTTP or gRPC transport fields.

TLS uses the supplied SSLContext. A None context supports prior-knowledge h2c.
GET/HEAD from the native Unity image loader also accepts HTTP/1.1 through the
same explicit asset callback. An optional handler accepts WebSocket Upgrade at /ws.
RequestBytes/responseBytes in notifications count the gRPC DATA wire bytes,
including the five-byte message envelope. Notifications contain no payload,
authorization headers, peer addresses, or exception text.

An optional ``http_get(path)`` callback serves stored profile-card thumbnails
and explicitly routed public resources. Unmatched HTTP GET paths return 404
without filesystem routing. Bodies use the same HTTP/2 flow control as unary
responses; single byte ranges support the native resource downloaders.
"""
from __future__ import annotations

import asyncio
import ssl
import struct
from dataclasses import dataclass, field
from typing import Callable

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RemoteSettingsChanged,
    RequestReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
)
from h2.exceptions import H2Error, StreamClosedError
from deathnote.protocol.rpc import NativeRpcError
from .http_assets import select_byte_range, serve_http1_asset


MAX_REQUEST_BYTES = 4 * 1024 * 1024
_RESERVED_HEADERS = frozenset({
    'connection', 'content-length', 'content-type', 'keep-alive',
    'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade',
})


@dataclass
class _Request:
    headers: dict[str, str]
    body: bytearray = field(default_factory=bytearray)
    received_bytes: int = 0
    rejection_status: int = 0

    @property
    def path(self) -> str:
        return self.headers.get(':path', '')

    def receive(self, data: bytes) -> None:
        self.received_bytes += len(data)
        if self.rejection_status:
            return
        if self.received_bytes > MAX_REQUEST_BYTES + 5:
            self.rejection_status = 8
            self.body.clear()
            return
        self.body.extend(data)
        if len(self.body) >= 5:
            length = struct.unpack_from('>I', self.body, 1)[0]
            if length > MAX_REQUEST_BYTES or self.body[0] != 0:
                self.rejection_status = 8 if length > MAX_REQUEST_BYTES else 12 if self.body[0] == 1 else 3
                self.body.clear()

    def payload(self) -> bytes:
        if self.rejection_status or len(self.body) < 5:
            raise ValueError('Invalid unary gRPC frame')
        length = struct.unpack_from('>I', self.body, 1)[0]
        if self.body[0] != 0 or length != len(self.body) - 5:
            raise ValueError('Invalid unary gRPC frame')
        return bytes(self.body[5:])


class _Connection:
    def __init__(self, reader, writer, dispatch, on_request, use_tls, http_get=None):
        self.reader = reader
        self.writer = writer
        self.dispatch = dispatch
        self.on_request = on_request
        self.http_get = http_get
        self.protocol = H2Connection(H2Configuration(
            client_side=False, header_encoding='utf-8',
        ))
        self.requests: dict[int, _Request] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.window_updated = asyncio.Event()
        self.closed = False
        tls = writer.get_extra_info('ssl_object')
        self.summary = {
            'transport': 'tls' if use_tls else 'h2c',
            'tlsVersion': tls.version() if tls else None,
            'alpn': tls.selected_alpn_protocol() if tls else None,
        }

    async def notify(self, event: dict) -> None:
        if self.on_request is None:
            return
        try:
            self.on_request({**self.summary, **event})
        except Exception:
            pass

    async def flush(self) -> None:
        data = self.protocol.data_to_send()
        if data:
            self.writer.write(data)
            await self.writer.drain()

    async def send_body(self, stream_id, body) -> int:
        sent = 0
        while sent < len(body):
            if self.closed:
                return sent
            window = self.protocol.local_flow_control_window(stream_id)
            size = min(window, self.protocol.max_outbound_frame_size, len(body) - sent)
            if size <= 0:
                self.window_updated.clear()
                await self.window_updated.wait()
                continue
            self.protocol.send_data(stream_id, body[sent:sent + size])
            sent += size
            await self.flush()
        return sent

    async def reply(self, stream_id, payload, metadata, status) -> int:
        headers = [(':status', '200'), ('content-type', 'application/grpc')]
        if status == 0:
            for key, value in metadata.items():
                key = key.lower()
                if key.startswith((':', 'grpc-')) or key in _RESERVED_HEADERS:
                    continue
                headers.append((key, str(value)))
        self.protocol.send_headers(stream_id, headers)
        await self.flush()
        sent = 0
        if status == 0:
            message = b'\0' + struct.pack('>I', len(payload)) + payload
            sent = await self.send_body(stream_id, message)
        self.protocol.send_headers(
            stream_id, [('grpc-status', str(status))], end_stream=True,
        )
        await self.flush()
        return sent

    async def reply_http_asset(self, stream_id, request):
        status, body, content_type = 404, b'', 'application/octet-stream'
        try:
            resource = await asyncio.to_thread(self.http_get, request.path)
            if resource is not None:
                body, content_type = resource
                content_type.encode('ascii')
                if '\r' in content_type or '\n' in content_type:
                    raise ValueError('Invalid resource content type')
                status = 200
        except Exception:
            status, body, content_type = 500, b'', 'application/octet-stream'
        extra_headers = []
        if status == 200:
            status, body, extra_headers = select_byte_range(body, request.headers.get('range'))
        self.protocol.send_headers(stream_id, [(':status', str(status)),
            ('content-type', content_type), ('content-length', str(len(body))),
            ('cache-control', 'private, max-age=3600' if status in (200, 206) else 'no-store'),
            *extra_headers])
        await self.flush()
        sent = 0 if request.headers.get(':method') == 'HEAD' else await self.send_body(stream_id, body)
        self.protocol.end_stream(stream_id)
        await self.flush()
        await self.notify({'event': 'http_asset', 'path': request.path.partition('?')[0],
                           'httpStatus': status, 'responseBytes': sent})

    async def handle_request(self, stream_id: int, request: _Request) -> None:
        status = 0
        response = b''
        metadata = {}
        sent = 0
        diagnostic = None
        try:
            if request.headers.get(':method') in ('GET', 'HEAD') and self.http_get is not None:
                await self.reply_http_asset(stream_id, request)
                return
            try:
                if request.headers.get(':method') != 'POST':
                    status = 12
                content_type = request.headers.get('content-type', '').partition(';')[0].strip().lower()
                if not (content_type == 'application/grpc' or content_type.startswith('application/grpc+')):
                    status = 3
                if request.rejection_status:
                    status = request.rejection_status
                if status == 0:
                    try:
                        payload = request.payload()
                    except ValueError:
                        status = 3
                request.body.clear()
                if status == 0:
                    response, metadata = await asyncio.to_thread(
                        self.dispatch, request.path, payload, request.headers,
                    )
                if not isinstance(response, bytes) or not isinstance(metadata, dict):
                    raise TypeError('Dispatch must return bytes and a metadata dict')
                for key, value in metadata.items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        raise TypeError('Metadata keys and values must be strings')
                    key.encode('ascii')
                    value.encode('ascii')
                    if '\r' in key or '\n' in key or '\r' in value or '\n' in value:
                        raise ValueError('Invalid response metadata')
            except NativeRpcError as error:
                status = error.status
                diagnostic = error.diagnostic
                response, metadata = b'', {}
            except NotImplementedError:
                status = 12
                response, metadata = b'', {}
            except Exception:
                status = 13
                response, metadata = b'', {}
            sent = await self.reply(stream_id, response, metadata, status)
            await self.notify({
                'event': 'rpc', 'path': request.path.partition('?')[0], 'grpcStatus': status,
                'requestBytes': request.received_bytes, 'responseBytes': sent,
                'clientVersion': request.headers.get('x-client-version', '')[:256],
                'userAgent': request.headers.get('user-agent', '')[:512],
                **({'diagnostic': diagnostic} if diagnostic is not None else {}),
            })
        except (StreamClosedError, H2Error, OSError):
            pass
        finally:
            request.body.clear()
            self.requests.pop(stream_id, None)
            self.tasks.pop(stream_id, None)

    async def run(self, initial_data=b'') -> None:
        await self.notify({'event': 'connection_open'})
        try:
            self.protocol.initiate_connection()
            await self.flush()
            while not self.closed:
                if initial_data:
                    data, initial_data = initial_data, b''
                else:
                    data = await self.reader.read(65536)
                if not data:
                    break
                events = self.protocol.receive_data(data)
                for event in events:
                    if isinstance(event, RequestReceived):
                        request = _Request(dict(event.headers))
                        self.requests[event.stream_id] = request
                        await self.notify({
                            'event': 'request_received', 'streamId': event.stream_id,
                            'path': request.path.partition('?')[0],
                            'method': request.headers.get(':method', ''),
                        })
                    elif isinstance(event, DataReceived):
                        self.protocol.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id,
                        )
                        request = self.requests.get(event.stream_id)
                        if request is not None:
                            request.receive(event.data)
                    elif isinstance(event, StreamEnded):
                        request = self.requests[event.stream_id]
                        self.tasks[event.stream_id] = asyncio.create_task(
                            self.handle_request(event.stream_id, request),
                        )
                    elif isinstance(event, StreamReset):
                        self.requests.pop(event.stream_id, None)
                        task = self.tasks.pop(event.stream_id, None)
                        if task is not None:
                            task.cancel()
                        self.window_updated.set()
                    elif isinstance(event, RemoteSettingsChanged):
                        self.window_updated.set()
                        await self.notify({'event': 'h2_settings_received'})
                    elif isinstance(event, WindowUpdated):
                        self.window_updated.set()
                    elif isinstance(event, ConnectionTerminated):
                        self.closed = True
                        self.window_updated.set()
                        break
                await self.flush()
        except (H2Error, OSError):
            try:
                await self.flush()
            except OSError:
                pass
        finally:
            self.closed = True
            self.window_updated.set()
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.requests.clear()
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
            await self.notify({'event': 'connection_close'})


async def serve_native(
    host: str,
    port: int,
    ssl_context: ssl.SSLContext | None,
    dispatch: Callable,
    on_request: Callable | None = None,
    *,
    http_get: Callable | None = None,
    websocket_handler: Callable | None = None,
) -> asyncio.Server:
    """Create a listener; its caller owns server.close()/wait_closed().

    Each connection handles independent concurrent streams and observes both
    stream and connection flow control. Reset/close cancels response tasks;
    an already-running synchronous dispatch thread finishes independently.
    """
    async def connected(reader, writer):
        connection = _Connection(
            reader, writer, dispatch, on_request,
            use_tls=ssl_context is not None, http_get=http_get,
        )
        tls = writer.get_extra_info('ssl_object')
        if tls is not None:
            await connection.notify({'event': 'tls_connected'})
        if tls is not None and tls.selected_alpn_protocol() == 'h2':
            await connection.run()
            return
        try:
            async with asyncio.timeout(15):
                prefix = await reader.readexactly(4)
        except (asyncio.IncompleteReadError, OSError, TimeoutError) as error:
            await connection.notify({'event': 'connection_prefix_error', 'stage': 'application_prefix',
                                     'errorType': type(error).__name__,
                                     'receivedBytes': len(error.partial) if isinstance(error, asyncio.IncompleteReadError) else 0})
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        if prefix in (b'GET ', b'HEAD'):
            await serve_http1_asset(reader, writer, prefix, http_get, connection.notify,
                                    websocket_handler=websocket_handler)
        else:
            await connection.run(prefix)

    return await asyncio.start_server(
        connected, host, port, ssl=ssl_context,
    )

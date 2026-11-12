"""Native phone chat login and paced heartbeat over its protobuf WebSocket."""
import asyncio
from contextlib import closing, suppress
import sqlite3

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, string_field
from deathnote.protocol.decode import fields

LOGIN_REQUEST = 'app.chat.MessageLoginIMReq'
LOGIN_RESPONSE = 'app.chat.MessageLoginIMResp'
LOGIN_READY = 'app.chat.MessageRetNoti'
PING = 'app.chat.GRPCPing'


def envelope(kind, body):
    return string_field(2, kind) + message_field(3, body)


def _message(payload):
    result = {}
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Invalid protobuf field zero')
        result[number] = value
    return result


class NativeChatRealtime:
    def __init__(self, login, *, ping_interval=5.0, logger=None, hub=None):
        self.login = login
        self.ping_interval = ping_interval
        self.logger = logger
        self.hub = hub

    def _log(self, event, **values):
        if self.logger is not None:
            self.logger({'event': event, **values})

    def _authenticate(self, payload):
        values = _message(payload)
        account_id, token = values.get(2, 0), values.get(3, b'')
        if (type(account_id) is not int or not 0 < account_id <= 0x7fffffffffffffff
                or not isinstance(token, bytes)):
            raise NativeRpcError(16)
        try:
            credential = token.decode('utf8')
        except UnicodeDecodeError:
            raise NativeRpcError(16) from None
        with closing(sqlite3.connect(self.login.database)) as connection:
            row = connection.execute('SELECT player_id FROM players WHERE profile_id=?',
                                     (account_id,)).fetchone()
        if row is None:
            raise NativeRpcError(16)
        return self.login.authenticate({'x-player-id': row[0], 'x-player-credential': credential})

    async def _reply_ping(self, socket, payload):
        await asyncio.sleep(self.ping_interval)
        await socket.send_binary(envelope(PING, payload))

    async def serve(self, socket):
        player = None
        pending_ping = None
        registration = None
        self._log('chat_ws_open')
        try:
            while (payload := await socket.recv()) is not None:
                if not isinstance(payload, bytes):
                    await socket.close(1003)
                    return
                try:
                    outer = _message(payload)
                    kind = outer.get(2, b'').decode('utf8')
                    body = outer.get(3, b'')
                    if not kind or not isinstance(body, bytes):
                        raise ValueError('Invalid chat envelope')
                    _message(body)
                except (ValueError, TypeError, AttributeError):
                    self._log('chat_ws_protocol_error')
                    await socket.close(1002)
                    return
                if kind == LOGIN_REQUEST:
                    try:
                        player = self._authenticate(body)
                    except NativeRpcError:
                        self._log('chat_ws_login', status=16)
                        await socket.send_binary(envelope(LOGIN_RESPONSE, integer_field(2, 1)))
                        await socket.close(1008)
                        return
                    account_id = player['profile_id']
                    if registration is not None:
                        await self.hub.unregister(registration)
                        registration = None
                    response = integer_field(3, account_id)
                    await socket.send_binary(envelope(LOGIN_RESPONSE, response))
                    if self.hub is not None:
                        registration = await self.hub.register(account_id, socket)
                    await socket.send_binary(envelope(LOGIN_READY, message_field(3, b'')))
                    self._log('chat_ws_login', status=0, accountId=account_id)
                elif kind == PING:
                    if player is None:
                        await socket.close(1008)
                        return
                    if pending_ping is None or pending_ping.done():
                        pending_ping = asyncio.create_task(self._reply_ping(socket, body))
                else:
                    self._log('chat_ws_unknown_message')
                    await socket.close(1003)
                    return
        finally:
            if registration is not None:
                await self.hub.unregister(registration)
            if pending_ping is not None:
                pending_ping.cancel()
                with suppress(asyncio.CancelledError, ConnectionError, OSError):
                    await pending_ping
            self._log('chat_ws_close')

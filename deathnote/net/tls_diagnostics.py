"""Scoped TLS diagnostics; no certificates, sockets or trust settings are created."""
from __future__ import annotations

import asyncio
from asyncio import sslproto
from contextlib import contextmanager
import logging
import ssl


def _tls_protocol(value):
    if isinstance(value, sslproto.SSLProtocol):
        return value
    if isinstance(value, sslproto._SSLProtocolTransport):
        return value._ssl_protocol
    if isinstance(value, asyncio.Transport):
        protocol = value.get_protocol()
        if isinstance(protocol, sslproto.SSLProtocol):
            return protocol
    return None


def _stage(protocol, message=''):
    if (protocol is not None and protocol._state == sslproto.SSLProtocolState.DO_HANDSHAKE
            or message in ('SSL handshake failed', 'SSL handshake failed on verifying the certificate',
                           'Error on transport creation for incoming connection')):
        return 'handshake'
    return 'tls_io'


class _TLSFilter(logging.Filter):
    def __init__(self, emit, previous_level: int, previous_disabled: bool, context):
        super().__init__()
        self.emit = emit
        self.previous_level = previous_level
        self.previous_disabled = previous_disabled
        self.context = context

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        args = record.args if isinstance(record.args, tuple) else ()
        protocol = _tls_protocol(args[0]) if args else None
        own_transport = protocol is not None and protocol._sslcontext is self.context
        if isinstance(error, ssl.SSLError) or own_transport and isinstance(error, OSError):
            message = args[1] if len(args) > 1 and isinstance(args[1], str) else ''
            self.emit(error, _stage(protocol, message))
            return False
        return not self.previous_disabled and record.levelno >= self.previous_level


@contextmanager
def tls_diagnostics(context: ssl.SSLContext | None, observe):
    """Temporarily collect parsed ClientHello/SNI and safe SSL error fields.

    ``observe(dict)`` is the CLI's timestamped JSONL sink. Existing SNI selection
    and non-TLS exception handling continue through their original callbacks.
    """
    loop = asyncio.get_running_loop()
    logger = logging.getLogger("asyncio")
    previous_debug = loop.get_debug()
    previous_handler = loop.get_exception_handler()
    previous_level = logger.level
    previous_disabled = logger.disabled
    previous_sni = context.sni_callback if context is not None else None
    previous_connection_lost = sslproto.SSLProtocol.connection_lost

    def notify(event):
        try:
            observe(event)
        except Exception:
            pass

    def emit_error(error, stage):
        notify({"event": "tls_handshake_error", "type": type(error).__name__,
                "errno": error.errno, "stage": stage})

    def exception_handler(event_loop, event):
        error = event.get("exception")
        protocol = _tls_protocol(event.get('protocol')) or _tls_protocol(event.get('transport'))
        own_transport = protocol is not None and protocol._sslcontext is context
        if isinstance(error, ssl.SSLError) or own_transport and isinstance(error, OSError):
            emit_error(error, _stage(protocol, event.get('message', '')))
        elif previous_handler is not None:
            previous_handler(event_loop, event)
        else:
            event_loop.default_exception_handler(event)

    def sni_callback(ssl_object, server_name, initial_context):
        notify({"event": "tls_client_hello", "sni": server_name})
        if previous_sni is not None:
            return previous_sni(ssl_object, server_name, initial_context)
        return None

    def connection_lost(protocol, error):
        if (context is not None and protocol._sslcontext is context
                and protocol._state == sslproto.SSLProtocolState.DO_HANDSHAKE):
            if isinstance(error, OSError):
                emit_error(error, 'handshake')
            elif error is None:
                notify({'event': 'tls_handshake_error', 'type': 'EOF',
                        'errno': None, 'stage': 'handshake'})
        previous_connection_lost(protocol, error)

    record_filter = _TLSFilter(emit_error, logger.getEffectiveLevel(), previous_disabled, context)
    logger.filters.insert(0, record_filter)
    try:
        logger.disabled = False
        logger.setLevel(logging.DEBUG)
        loop.set_debug(True)
        loop.set_exception_handler(exception_handler)
        if context is not None:
            context.sni_callback = sni_callback
            sslproto.SSLProtocol.connection_lost = connection_lost
        yield
    finally:
        if context is not None:
            context.sni_callback = previous_sni
            sslproto.SSLProtocol.connection_lost = previous_connection_lost
        loop.set_exception_handler(previous_handler)
        loop.set_debug(previous_debug)
        logger.removeFilter(record_filter)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled

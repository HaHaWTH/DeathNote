"""Thread-safe fan-out of encoded messages to authenticated local chat sockets."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock


@dataclass
class _Registration:
    account_id: int
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    sender: asyncio.Task


class NativeChatHub:
    """One queue per connection, with publication ordered under a shared lock.

    Register/unregister are async lifecycle calls. Publish may run in a gRPC
    worker thread and returns the number of connections queued for delivery.
    Message bytes stay in memory and are never included in a log event.
    """

    def __init__(self):
        self._lock = Lock()
        self._registrations = {}
        self._accounts = {}

    async def register(self, account_id, socket):
        loop = asyncio.get_running_loop()
        token = object()
        queue = asyncio.Queue()
        sender = loop.create_task(self._send(token, queue, socket))
        registration = _Registration(account_id, loop, queue, sender)
        with self._lock:
            self._registrations[token] = registration
            self._accounts.setdefault(account_id, set()).add(token)
        return token

    def _drop_locked(self, token):
        registration = self._registrations.pop(token, None)
        if registration is not None:
            tokens = self._accounts[registration.account_id]
            tokens.remove(token)
            if not tokens:
                del self._accounts[registration.account_id]
        return registration

    def _drop(self, token):
        with self._lock:
            return self._drop_locked(token)

    @staticmethod
    async def _stop_sender(sender):
        sender.cancel()
        try:
            await sender
        except asyncio.CancelledError:
            pass

    async def unregister(self, token):
        registration = self._drop(token)
        if registration is None:
            return
        if asyncio.get_running_loop() is registration.loop:
            await self._stop_sender(registration.sender)
        else:
            stopped = asyncio.run_coroutine_threadsafe(
                self._stop_sender(registration.sender), registration.loop)
            await asyncio.wrap_future(stopped)

    def _enqueue(self, token, payload):
        with self._lock:
            registration = self._registrations.get(token)
        if registration is not None:
            registration.queue.put_nowait(payload)

    def publish(self, account_ids, payload_bytes):
        if not isinstance(payload_bytes, bytes):
            raise TypeError('Chat hub payload must be encoded bytes')
        queued = 0
        with self._lock:
            for account_id in dict.fromkeys(account_ids):
                for token in tuple(self._accounts.get(account_id, ())):
                    registration = self._registrations[token]
                    try:
                        registration.loop.call_soon_threadsafe(self._enqueue, token, payload_bytes)
                    except RuntimeError:
                        if not registration.loop.is_closed():
                            raise
                        self._drop_locked(token)
                    else:
                        queued += 1
        return queued

    async def _send(self, token, queue, socket):
        try:
            while True:
                payload = await queue.get()
                await socket.send_binary(payload)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self._drop(token)

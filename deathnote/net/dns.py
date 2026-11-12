"""Explicitly started UDP DNS mapping for the local native-game listener."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
import time
from collections.abc import Callable, Iterable


_TYPES = {1: "A", 28: "AAAA", 64: "SVCB", 65: "HTTPS"}
_UPSTREAM_TIMEOUT = 4.0


def _hostname(value: str) -> str:
    name = value.rstrip(".").encode("idna").decode("ascii").lower()
    if not name or len(name) > 253 or any(not label or len(label) > 63 for label in name.split(".")):
        raise ValueError("A DNS hostname must contain valid nonempty labels.")
    return name


def _question(packet: bytes) -> tuple[int, int, str, int, int, int]:
    if len(packet) < 12:
        raise ValueError("Truncated DNS header")
    identifier, flags, count, _, _, _ = struct.unpack_from("!6H", packet)
    if flags & 0x8000 or count != 1:
        raise ValueError("Expected one DNS query question")
    cursor, end, labels, visited = 12, None, [], set()
    while True:
        if cursor >= len(packet) or cursor in visited:
            raise ValueError("Invalid DNS name offset")
        visited.add(cursor)
        length = packet[cursor]
        if length & 0xC0 == 0xC0:
            if cursor + 1 >= len(packet):
                raise ValueError("Truncated DNS name pointer")
            target = ((length & 0x3F) << 8) | packet[cursor + 1]
            if target < 12 or target >= cursor:
                raise ValueError("Invalid DNS name pointer")
            if end is None:
                end = cursor + 2
            cursor = target
            continue
        if length & 0xC0:
            raise ValueError("Unsupported DNS label encoding")
        cursor += 1
        if not length:
            if end is None:
                end = cursor
            break
        if cursor + length > len(packet):
            raise ValueError("Truncated DNS label")
        labels.append(packet[cursor:cursor + length].decode("ascii"))
        cursor += length
    if end + 4 > len(packet):
        raise ValueError("Truncated DNS question")
    name = ".".join(labels).lower()
    if len(name) > 253:
        raise ValueError("DNS name exceeds the wire-format limit")
    qtype, qclass = struct.unpack_from("!HH", packet, end)
    return identifier, flags, name, qtype, qclass, end + 4


def _response(packet: bytes, question_end: int, flags: int, *, address: bytes | None = None,
              rcode: int = 0, recursion_available: bool = False) -> bytes:
    response_flags = 0x8000 | (flags & 0x7910) | rcode
    if recursion_available:
        response_flags |= 0x0080
    header = packet[:2] + struct.pack("!5H", response_flags, 1, int(address is not None), 0, 0)
    answer = b""
    if address is not None:
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, len(address)) + address
    return header + packet[12:question_end] + answer


class _UpstreamQuery(asyncio.DatagramProtocol):
    def __init__(self, packet: bytes, result: asyncio.Future[bytes]):
        self.packet = packet
        self.result = result

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        transport.sendto(self.packet)

    def datagram_received(self, data: bytes, addr) -> None:
        if (not self.result.done() and len(data) >= 12 and data[:2] == self.packet[:2]
                and data[2] & 0x80):
            self.result.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.result.done():
            self.result.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.result.done():
            self.result.set_exception(exc or ConnectionError("DNS upstream transport closed"))


async def _forward(packet: bytes, upstream: tuple[str, int]) -> bytes:
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UpstreamQuery(packet, result), remote_addr=upstream,
    )
    try:
        return await asyncio.wait_for(result, _UPSTREAM_TIMEOUT)
    finally:
        transport.close()


def _reply_metadata(packet: bytes) -> dict:
    """Record resolver answers without changing the DNS packet sent to the phone."""
    _, flags, questions, answers, _, _ = struct.unpack_from("!6H", packet)
    cursor = 12

    def name_end(offset):
        while True:
            length = packet[offset]
            if length & 0xC0 == 0xC0:
                if offset + 2 > len(packet):
                    raise ValueError("Truncated DNS name pointer")
                return offset + 2
            if length & 0xC0 or length > 63:
                raise ValueError("Unsupported DNS label encoding")
            offset += length + 1
            if offset > len(packet):
                raise ValueError("Truncated DNS name")
            if not length:
                return offset

    for _ in range(questions):
        cursor = name_end(cursor) + 4
    addresses, ttl = [], None
    for _ in range(answers):
        cursor = name_end(cursor)
        kind, qclass, lifetime, size = struct.unpack_from("!HHIH", packet, cursor)
        cursor += 10
        end = cursor + size
        if end > len(packet):
            raise ValueError("Truncated DNS answer")
        ttl = lifetime if ttl is None else min(ttl, lifetime)
        if qclass == 1 and kind in (1, 28):
            addresses.append(str(ipaddress.ip_address(packet[cursor:end])))
        cursor = end
    return {"rcode": flags & 15, "truncated": bool(flags & 0x0200),
            "addresses": addresses, "ttl": ttl}


class _LocalDNS(asyncio.DatagramProtocol):
    def __init__(self, address: bytes, hostnames: set[str], upstream: tuple[str, int] | None,
                 on_request: Callable[[dict], None] | None, passthrough: bool):
        self.address = address
        self.hostnames = hostnames
        self.upstream = upstream
        self.on_request = on_request
        self.passthrough = passthrough
        self.transport = None
        self.tasks: set[asyncio.Task] = set()

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        task = asyncio.create_task(self.handle(data, addr))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        for task in self.tasks:
            task.cancel()

    def log(self, name: str, qtype: int | None, status: str, **details) -> None:
        if self.on_request is not None:
            self.on_request({"host": name, "qtype": _TYPES.get(qtype, qtype),
                             "status": status, **details})

    async def handle(self, packet: bytes, addr) -> None:
        started = time.monotonic()
        try:
            _, flags, name, qtype, qclass, question_end = _question(packet)
        except (ValueError, UnicodeError):
            self.log("", None, "malformed")
            return
        available = self.upstream is not None
        if flags & 0x7800:
            reply = _response(packet, question_end, flags, rcode=4, recursion_available=available)
            status = "unsupported_opcode"
        elif not self.passthrough and name in self.hostnames and qclass == 1 and qtype in (1, 28, 64, 65):
            reply = _response(packet, question_end, flags,
                              address=self.address if qtype == 1 else None,
                              recursion_available=available)
            status = {1: 'local_a', 28: 'local_empty_aaaa',
                      64: 'local_empty_svcb', 65: 'local_empty_https'}[qtype]
        elif self.upstream is None:
            reply = _response(packet, question_end, flags, rcode=5)
            status = "no_upstream"
        else:
            try:
                reply = await _forward(packet, self.upstream)
                status = "passthrough" if self.passthrough else "forwarded"
            except TimeoutError:
                reply = _response(packet, question_end, flags, rcode=2, recursion_available=True)
                status = "upstream_timeout"
            except OSError:
                reply = _response(packet, question_end, flags, rcode=2, recursion_available=True)
                status = "upstream_error"
        if self.transport is not None:
            self.transport.sendto(reply, addr)
        try:
            details = _reply_metadata(reply)
        except (ValueError, IndexError, struct.error):
            details = {"answerMetadata": "malformed"}
        self.log(name, qtype, status, elapsedMs=round((time.monotonic() - started) * 1000, 3),
                 upstream=list(self.upstream) if status in ("forwarded", "passthrough",
                                                           "upstream_timeout", "upstream_error") else None,
                 **details)


async def serve_dns(host: str, port: int, lan_ip: str, hostnames: Iterable[str],
                    upstream: str | tuple[str, int] | None,
                    on_request: Callable[[dict], None] | None,
                    passthrough: bool = False) -> asyncio.DatagramTransport:
    """Bind UDP DNS only when called; the caller closes the returned transport.

    ``upstream`` is an explicit resolver IP or ``(IP, port)``; no system resolver
    configuration is read or changed. The callback receives routing status,
    answer addresses/TTL/rcode, elapsed milliseconds, and the upstream IP/port.
    """
    address = ipaddress.IPv4Address(lan_ip).packed
    names = {_hostname(name) for name in hostnames}
    remote = None
    if upstream is not None:
        resolver, resolver_port = (upstream, 53) if isinstance(upstream, str) else upstream
        remote = (str(ipaddress.ip_address(resolver)), int(resolver_port))
        if not 1 <= remote[1] <= 65535:
            raise ValueError("DNS upstream port must be between 1 and 65535.")
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _LocalDNS(address, names, remote, on_request, passthrough),
        local_addr=(host, port), family=socket.AF_INET,
    )
    return transport

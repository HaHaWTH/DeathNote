"""Small protobuf encoders for the recovered native-client RPC contracts."""
from __future__ import annotations


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def message_field(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def string_field(number: int, value: str) -> bytes:
    return message_field(number, value.encode('utf8'))


def integer_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value & 0xffffffffffffffff)

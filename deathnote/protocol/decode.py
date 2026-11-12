from __future__ import annotations


SERVER_FIELDS = {1: 'name', 2: 'cdnRoots', 3: 'apiServerRoots', 4: 'chatServerRoots',
                 5: 'atServerRoots', 6: 'liveServers', 7: 'displayName', 8: 'areaId', 9: 'ageIconSpriteName'}


def varint(data, pos):
    value = shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7
    raise ValueError('Server returned a truncated protobuf varint')


def fields(data):
    pos = 0
    result = []
    while pos < len(data):
        tag, pos = varint(data, pos)
        kind = tag & 7
        if kind == 2:
            size, pos = varint(data, pos)
            value = data[pos:pos + size]
            if len(value) != size:
                raise ValueError('Server returned a truncated protobuf field')
            pos += size
        elif kind == 0:
            value, pos = varint(data, pos)
        else:
            raise ValueError(f'Unsupported protobuf wire type in server response: {kind}')
        result.append((tag >> 3, value))
    return result

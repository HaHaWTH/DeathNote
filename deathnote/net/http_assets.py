"""Single byte-range selection shared by native HTTP resource transports."""
import asyncio
from http import HTTPStatus
import re


def select_byte_range(body, byte_range):
    headers = [('accept-ranges', 'bytes')]
    if byte_range is None:
        return 200, body, headers
    total = len(body)
    match = re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})', byte_range)
    start, end = 0, -1
    if match and (match[1] or match[2]) and total:
        if match[1]:
            start = int(match[1])
            end = min(int(match[2]), total - 1) if match[2] else total - 1
        elif int(match[2]) > 0:
            start, end = max(0, total - int(match[2])), total - 1
    if 0 <= start <= end < total:
        return 206, body[start:end + 1], headers + [('content-range', f'bytes {start}-{end}/{total}')]
    return 416, b'', headers + [('content-range', f'bytes */{total}')]


async def serve_http1_asset(reader, writer, prefix, http_get, notify, *, websocket_handler=None):
    """Serve one UnityWebRequest image request through the existing asset router."""
    status, body, content_type, extra = 400, b'', 'application/octet-stream', []
    path, method = '', ''
    await notify({'event': 'connection_open', 'httpVersion': '1.1'})
    try:
        async with asyncio.timeout(15):
            header = prefix + await reader.readuntil(b'\r\n\r\n')
        lines = header[:-4].decode('iso-8859-1').split('\r\n')
        method, path, version = lines[0].split(' ')
        if version not in ('HTTP/1.0', 'HTTP/1.1'):
            raise ValueError('Invalid HTTP version')
        headers = {}
        for line in lines[1:]:
            key, value = line.split(':', 1)
            headers[key.strip().lower()] = value.strip()
        if headers.get('content-length', '0') != '0' or 'transfer-encoding' in headers:
            raise ValueError('Asset GET/HEAD has no request body')
        await notify({'event': 'request_received', 'httpVersion': '1.1',
                      'path': path.partition('?')[0], 'method': method})
        if (websocket_handler is not None and path == '/ws'
                and headers.get('upgrade', '').lower() == 'websocket'):
            from .websocket import serve_websocket
            await serve_websocket(reader, writer, method, path, version, headers,
                                  websocket_handler, notify)
            return
        if method not in ('GET', 'HEAD'):
            status = 405
        else:
            resource = await asyncio.to_thread(http_get, path) if http_get is not None else None
            if resource is None:
                status = 404
            else:
                body, content_type = resource
                content_type.encode('ascii')
                if '\r' in content_type or '\n' in content_type:
                    raise ValueError('Invalid asset content type')
                status, body, extra = select_byte_range(body, headers.get('range'))
    except (ValueError, UnicodeError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
        status, body, content_type, extra = 400, b'', 'application/octet-stream', []
    except Exception:
        status, body, content_type, extra = 500, b'', 'application/octet-stream', []
    try:
        response_headers = [('content-type', content_type), ('content-length', str(len(body))),
                            ('connection', 'close'),
                            ('cache-control', 'private, max-age=3600' if status in (200, 206) else 'no-store'), *extra]
        head = f'HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n'
        head += ''.join(key + ': ' + value + '\r\n' for key, value in response_headers) + '\r\n'
        writer.write(head.encode('ascii'))
        sent = 0 if method == 'HEAD' else len(body)
        if sent:
            writer.write(body)
        await writer.drain()
        await notify({'event': 'http_asset', 'httpVersion': '1.1', 'path': path.partition('?')[0],
                      'httpStatus': status, 'responseBytes': sent})
    except OSError:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        await notify({'event': 'connection_close', 'httpVersion': '1.1'})

"""Serve published master files locally and relay anonymous iOS resources."""
from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import quote, unquote, urlsplit

import requests
import urllib3

from .public_update import _resolve_public
from deathnote.net.http_assets import select_byte_range


def resource_download_root(config, mirror):
    """Use the existing trusted image HTTPS listener for Unity downloads."""
    image_origin = config.get('announcementImageBaseUrl')
    if config.get('announcementImagesTls', {}).get('enabled', False) and image_origin:
        origin = image_origin.rstrip('/')
    else:
        origin = 'http://' + mirror.host + ':' + str(config.get('resourcePort', 8767))
    return origin + mirror.prefix.rstrip('/')


class NativeResourceMirror:
    def __init__(self, root, version, *, upstream=None):
        from .chart_candidates import NativeChartCandidates
        self.public = Path(root) / '.native-server/public'
        self.version = version
        self.candidate_assets = NativeChartCandidates(root).active_asset_overrides(version)
        cdn = urlsplit(version['cdnRoot'])
        self.host = cdn.hostname
        self.prefix = cdn.path.rstrip('/') + '/'
        self.pool = None
        self.pool_lock = Lock()
        self.upstream = upstream or self._fetch

    def _fetch(self, path):
        with self.pool_lock:
            if self.pool is None:
                self.pool = urllib3.HTTPSConnectionPool(
                    _resolve_public(self.host), port=443,
                    server_hostname=self.host, assert_hostname=self.host,
                    cert_reqs='CERT_REQUIRED', ca_certs=requests.certs.where(),
                    maxsize=8, timeout=urllib3.Timeout(connect=10, read=90),
                )
        response = self.pool.request(
            'GET', path, headers={'Host': self.host, 'Accept-Encoding': 'identity'},
            redirect=False, retries=False,
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise OSError('Official resource HTTP status ' + str(response.status))
        return bytes(response.data), response.headers.get('Content-Type', 'application/octet-stream')

    def http_object(self, request_path):
        parts = urlsplit(request_path)
        if (parts.scheme or parts.netloc or parts.query or parts.fragment
                or not parts.path.startswith(self.prefix)):
            return None
        relative = unquote(parts.path[len(self.prefix):])
        segments = relative.split('/')
        if any(segment in ('', '.', '..') or '\\' in segment or '\x00' in segment
               for segment in segments):
            return None
        cached = None
        content_type = 'application/octet-stream'
        if (len(segments) == 3 and segments[0] == 'master'
                and re.fullmatch('[0-9a-f]{32}', segments[1])
                and (segments[2] == 'MasterManifest.json'
                     or re.fullmatch(r'Master[A-Za-z0-9_]+\.bin', segments[2]))):
            name = 'manifest.json' if segments[2] == 'MasterManifest.json' else segments[2]
            cached = self.public / 'masterdata' / segments[1] / name
            if segments[2] == 'MasterManifest.json':
                content_type = 'application/json'
        elif len(segments) >= 3 and segments[:2] == ['asset', 'iOS']:
            if relative in self.candidate_assets:
                return self.candidate_assets[relative].read_bytes(), content_type
            if len(segments) == 3:
                catalog = re.fullmatch(r'catalog_([A-Za-z0-9_.-]+?)(?:_(en|ko|zh-Hans|zh-Hant))?\.(bin|hash)', segments[2])
                if catalog:
                    cached = self.public / 'resource-versions' / catalog[1] / segments[2]
        else:
            return None
        if cached is not None and cached.is_file():
            return cached.read_bytes(), content_type
        return self.upstream(self.prefix + quote(relative, safe='/._-'))

    def close(self):
        if self.pool is not None:
            self.pool.close()


def resource_server(host, port, mirror, on_request):
    """Startable HTTP/1 listener for the client's anonymous public downloads."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            self.reply(False)

        def do_HEAD(self):
            self.reply(True)

        def reply(self, head):
            status, body, content_type, extra = 404, b'', 'application/octet-stream', []
            error_type = None
            try:
                resource = mirror.http_object(self.path)
                if resource is not None:
                    body, content_type = resource
                    content_type.encode('ascii')
                    if '\r' in content_type or '\n' in content_type:
                        raise ValueError('Invalid resource content type')
                    status, body, extra = select_byte_range(body, self.headers.get('Range'))
            except Exception as error:
                status, body, content_type = 500, b'', 'application/octet-stream'
                error_type = type(error).__name__
            sent = 0
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'private, max-age=3600' if status in (200, 206) else 'no-store')
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if not head:
                    self.wfile.write(body)
                    sent = len(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            on_request({'event': 'http_asset', 'transport': 'http',
                        'path': self.path.partition('?')[0], 'method': 'HEAD' if head else 'GET',
                        'httpStatus': status, 'responseBytes': sent,
                        **({'errorType': error_type} if error_type is not None else {})})

        def log_message(self, *_):
            pass

    return ThreadingHTTPServer((host, port), Handler)

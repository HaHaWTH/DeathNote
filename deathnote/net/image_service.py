"""Trusted local HTTPS for images, thumbnails and public game resources."""
from __future__ import annotations

from pathlib import Path
import json
import ssl

from .http2 import serve_native


DEFAULT_PORT = 8768
DEFAULT_CERTIFICATE = '.native-server/announcement-tls/server.crt'
DEFAULT_KEY = '.native-server/announcement-tls/server.key'


def announcement_images_status(root, config):
    settings = config.get('announcementImagesTls', {})
    enabled = bool(settings.get('enabled', False))
    if not enabled:
        return {'enabled': False, 'port': None, 'baseUrl': None,
                'certificateAvailable': False, 'keyAvailable': False}
    root = Path(root)
    coverage_path = root / '.native-server/announcement-tls/coverage.json'
    coverage = json.loads(coverage_path.read_text('utf8')) if coverage_path.is_file() else None
    return {'enabled': True, 'port': settings.get('port', DEFAULT_PORT),
            'baseUrl': config.get('announcementImageBaseUrl'),
            'certificateAvailable': (root / settings.get('certFile', DEFAULT_CERTIFICATE)).is_file(),
            'keyAvailable': (root / settings.get('keyFile', DEFAULT_KEY)).is_file(),
            'coverage': coverage}


def _reject_rpc(path, payload, headers):
    raise NotImplementedError(path)


async def serve_announcement_images(host, root, config, announcement, on_request=None, *,
                                    profile_card=None, public_asset=None):
    """Serve image and explicit public download routes over the trusted image TLS."""
    settings = config.get('announcementImagesTls', {})
    if not settings.get('enabled', False):
        return None
    root = Path(root)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(['h2', 'http/1.1'])
    context.load_cert_chain(root / settings.get('certFile', DEFAULT_CERTIFICATE),
                            root / settings.get('keyFile', DEFAULT_KEY))
    port = settings.get('port', DEFAULT_PORT)

    def observe(summary):
        if on_request is not None:
            on_request({'listener': 'announcementImages', 'listenerPort': port, **summary})

    def image_object(path):
        image = announcement.http_object(path)
        if image is None and profile_card is not None:
            image = profile_card.http_object(path)
        if image is None and public_asset is not None:
            image = public_asset(path)
        return image

    listener = await serve_native(host, port, context, _reject_rpc, observe,
                                  http_get=image_object)
    port = listener.sockets[0].getsockname()[1]
    return listener

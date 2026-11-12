"""Check official public versions for an imported snapshot."""
from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import socket
import ssl
import struct
from urllib.parse import unquote, urlsplit

import h2.config
import h2.connection
import h2.events
import requests

from deathnote.protocol.decode import SERVER_FIELDS, fields

from .chart_candidates import NativeChartCandidates
from .version import read_version


class PublicUpdateError(RuntimeError):
    """The official version check failed."""


def _official_url(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname
            or not parts.hostname.endswith('.gamerfusiontech.com')
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.query or parts.fragment):
        raise ValueError('The public resource URL is outside the official service.')
    return parts


def _resolve_public(host):
    with requests.Session() as session:
        session.trust_env = False
        response = session.get('https://1.1.1.1/dns-query',
                               params={'name': host, 'type': 'A'},
                               headers={'Accept': 'application/dns-json'},
                               timeout=(10, 15), allow_redirects=False)
        response.raise_for_status()
        answer = response.json()
    addresses = [row['data'] for row in answer.get('Answer', [])
                 if row['type'] == 1 and ipaddress.ip_address(row['data']).is_global]
    if answer['Status'] != 0 or not addresses:
        raise ConnectionError('The official hostname did not resolve to a public address.')
    return addresses[0]


def _public_rpc(base_url, method, client_version):
    host = _official_url(base_url).hostname
    address = _resolve_public(host)
    context = ssl.create_default_context()
    context.set_alpn_protocols(['h2'])
    protocol = h2.connection.H2Connection(config=h2.config.H2Configuration(
        client_side=True, header_encoding='utf8'))
    body, headers = bytearray(), {}
    with socket.create_connection((address, 443), timeout=20) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            if tls.selected_alpn_protocol() != 'h2':
                raise ConnectionError('The official service did not negotiate HTTP/2.')
            protocol.initiate_connection()
            protocol.send_headers(1, [(':method', 'POST'), (':scheme', 'https'),
                (':authority', host), (':path', method), ('content-type', 'application/grpc'),
                ('te', 'trailers'), ('x-client-version', client_version),
                ('user-agent', 'grpc-dotnet/2.67.0')])
            protocol.send_data(1, b'\0\0\0\0\0', end_stream=True)
            tls.sendall(protocol.data_to_send())
            ended = False
            while not ended:
                incoming = tls.recv(65535)
                if not incoming:
                    raise ConnectionError('The official version request ended before completing.')
                for event in protocol.receive_data(incoming):
                    if isinstance(event, (h2.events.ResponseReceived, h2.events.TrailersReceived)):
                        headers.update(event.headers)
                    elif isinstance(event, h2.events.DataReceived):
                        body.extend(event.data)
                        protocol.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        ended = True
                    elif isinstance(event, (h2.events.StreamReset, h2.events.ConnectionTerminated)):
                        raise ConnectionError('The official version request was interrupted.')
                pending = protocol.data_to_send()
                if pending:
                    tls.sendall(pending)
    if headers.get(':status') != '200':
        raise PublicUpdateError('The official version request failed (HTTP ' + headers.get(':status', 'unknown') + ').')
    grpc_status = headers.get('grpc-status')
    if grpc_status != '0':
        if (grpc_status == '2'
                and unquote(headers.get('grpc-message', '')).strip().casefold() == 'under maintenance'):
            raise PublicUpdateError('The official service is under maintenance (gRPC 2).')
        status = grpc_status if grpc_status in tuple(str(value) for value in range(17)) else 'unknown'
        raise PublicUpdateError('The official version request failed (gRPC ' + status + ').')
    if len(body) < 5 or body[0] or struct.unpack_from('>I', body, 1)[0] != len(body) - 5:
        raise ValueError('Invalid frame in the official version response.')
    return bytes(body[5:])


def _query_public_version(previous, on_progress):
    from deathnote.game.bootstrap import BOOTSTRAP_HOST

    region = previous['region']
    client_version = previous['clientVersion']
    if on_progress is not None:
        on_progress({'phase': 'checking', 'message': 'Checking the official public version',
                     'completedFiles': 0, 'totalFiles': 0})
    payload = _public_rpc('https://' + BOOTSTRAP_HOST,
                          '/app.playerlogin.PlayerLoginService/GetServerList', client_version)
    servers = [{SERVER_FIELDS[number]: value.decode('utf8')
                for number, value in fields(entry) if number in SERVER_FIELDS}
               for number, entry in fields(payload) if number == 1]
    selected = next((row for row in servers if row['name'] == region), None)
    if selected is None:
        raise PublicUpdateError('The official server list does not include ' + region + '.')
    api_root, cdn_root = selected['apiServerRoots'].split('|')[0], selected['cdnRoots'].split('|')[0]
    _official_url(api_root)
    _official_url(cdn_root)
    payload = _public_rpc(api_root, '/app.masterdata.MasterdataService/Version', client_version)
    values = dict(fields(payload))
    master_version, resource_version = values[1].decode('utf8'), values[2].decode('utf8')
    if not re.fullmatch('[0-9a-f]{32}', master_version):
        raise ValueError('Invalid official master-data version.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', resource_version):
        raise ValueError('Invalid official resource version.')
    return {**previous, 'masterVersion': master_version, 'resourceVersion': resource_version,
            'apiRoot': api_root, 'cdnRoot': cdn_root,
            'masterManifestUrl': cdn_root + '/master/' + master_version + '/MasterManifest.json'}


def check_public_data(root, *, on_progress=None):
    root = Path(root)
    version_file = root / '.native-server/public/version.json'
    if not version_file.is_file():
        raise PublicUpdateError('Import a public snapshot with import-public before checking for updates.')
    previous = read_version(version_file)
    active = NativeChartCandidates(root)._active()
    comparison = previous
    if active is not None:
        if previous != active['activatedVersion']:
            raise PublicUpdateError('The active candidate does not match the published version.')
        comparison = active['baseVersion']
    version = _query_public_version(comparison, on_progress)
    changed = [name for name in ('clientVersion', 'masterVersion', 'resourceVersion', 'apiRoot', 'cdnRoot')
               if comparison.get(name) != version.get(name)]
    return {'currentVersion': previous, 'comparisonVersion': comparison, 'version': version,
            'hasUpdate': bool(changed), 'changedFields': changed}

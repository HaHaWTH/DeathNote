"""Refresh the native server's anonymous official version and master cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import struct
from urllib.parse import unquote, urlsplit

import h2.config
import h2.connection
import h2.events
import requests

from .apk import read_apk
from .catalog import Catalog
from .crypto import master
from .locking import workspace_lock
from .chart_candidates import NativeChartCandidates
from deathnote.protocol.decode import SERVER_FIELDS, fields


class PublicUpdateError(RuntimeError):
    """An official RPC failure with a safe message for the management page."""


def _official_url(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname
            or not parts.hostname.endswith('.gamerfusiontech.com')
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.query or parts.fragment):
        raise ValueError('The public resource URL is outside the official service.')
    return parts


def _download(url):
    _official_url(url)
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(url, timeout=(10, 30), allow_redirects=False)
        response.raise_for_status()
        return response.content


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


def _atomic(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')


def _progress(callback, phase, message, completed=0, total=0):
    if callback is not None:
        callback({'phase': phase, 'message': message,
                  'completedFiles': completed, 'totalFiles': total})


def _query_public_version(root, on_progress, apk_file=None):
    version_file = root / '.native-server/public/version.json'
    previous = json.loads(version_file.read_text('utf8')) if version_file.is_file() else {}
    apk_file = Path(apk_file) if apk_file is not None else root / 'client.apk'
    package = read_apk(apk_file)
    region = previous.get('region', 'TW/HK/MO')
    _progress(on_progress, 'checking', 'Checking the official public version')
    payload = _public_rpc(package['bootstrapUrl'],
                          '/app.playerlogin.PlayerLoginService/GetServerList', package['versionName'])
    servers = [{SERVER_FIELDS[number]: value.decode('utf8')
                for number, value in fields(entry) if number in SERVER_FIELDS}
               for number, entry in fields(payload) if number == 1]
    selected = next((row for row in servers if row['name'] == region), None)
    if selected is None:
        raise PublicUpdateError('The official server list does not include ' + region + '.')
    api_root, cdn_root = selected['apiServerRoots'].split('|')[0], selected['cdnRoots'].split('|')[0]
    _official_url(api_root)
    _official_url(cdn_root)
    payload = _public_rpc(api_root, '/app.masterdata.MasterdataService/Version', package['versionName'])
    values = dict(fields(payload))
    master_version, resource_version = values[1].decode('utf8'), values[2].decode('utf8')
    if not re.fullmatch('[0-9a-f]{32}', master_version):
        raise ValueError('Invalid official master-data version.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', resource_version):
        raise ValueError('Invalid official resource version.')
    version = {**previous, 'region': region, 'masterVersion': master_version, 'resourceVersion': resource_version,
               'apiRoot': api_root, 'cdnRoot': cdn_root, 'clientVersion': package['versionName']}
    version['masterManifestUrl'] = cdn_root + '/master/' + master_version + '/MasterManifest.json'
    return previous, package, version


def check_public_data(root, *, on_progress=None, apk_file=None):
    root = Path(root)
    previous, _, version = _query_public_version(root, on_progress, apk_file)
    active = NativeChartCandidates(root)._active()
    comparison = previous
    if active is not None:
        if previous != active['activatedVersion']:
            raise PublicUpdateError('The active candidate does not match the published version.')
        comparison = active['baseVersion']
    changed = [name for name in ('clientVersion', 'masterVersion', 'resourceVersion', 'apiRoot', 'cdnRoot')
               if comparison.get(name) != version.get(name)]
    return {'currentVersion': previous, 'comparisonVersion': comparison, 'version': version,
            'hasUpdate': bool(changed), 'changedFields': changed}


def _backup_public_data(root, previous):
    public = root / '.native-server/public'
    recorded_at = datetime.now(timezone.utc)
    destination = root / '.native-server/backups' / (recorded_at.strftime('%Y%m%dT%H%M%S%fZ')
                                                    + '-before-master-update')
    backup_public = destination / 'public'
    try:
        backup_public.mkdir(parents=True, exist_ok=False)
        for name in ('version.json', 'master-manifest.json', 'master-cache.json'):
            source = public / name
            if source.is_file():
                shutil.copyfile(source, backup_public / name)
        master_directory = public / 'masterdata' / previous['masterVersion']
        if master_directory.is_dir():
            shutil.copytree(master_directory, backup_public / 'masterdata' / previous['masterVersion'])
        database = root / '.native-server/players.sqlite3'
        account_backed_up = database.is_file()
        if account_backed_up:
            target = destination / 'PRIVATE/players.sqlite3'
            target.parent.mkdir()
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(target)) as backup:
                    source.backup(backup, pages=256)
                    if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise PublicUpdateError('The pre-update account backup failed its integrity check.')
        report = {'recordedAt': recorded_at.isoformat(), 'version': previous,
                  'accountBackedUp': account_backed_up}
        (destination / 'report.json').write_bytes(_json_bytes(report))
    except (OSError, sqlite3.Error) as error:
        raise PublicUpdateError('The pre-update backup failed.') from error
    return destination.relative_to(root).as_posix()


def _fetch_ios_catalogs(public, version, on_progress):
    resource_version = version['resourceVersion']
    directory = public / 'resource-versions' / resource_version
    directory.mkdir(parents=True, exist_ok=True)
    base = version['cdnRoot'] + '/asset/iOS/'
    locales = ('', '_en', '_ko', '_zh-Hans', '_zh-Hant')
    result = []
    _progress(on_progress, 'catalogs', 'Preparing iOS catalogs', 0, len(locales))
    for locale in locales:
        stem = 'catalog_' + resource_version + locale
        names = [stem + suffix for suffix in ('.bin', '.hash')]
        targets = [directory / name for name in names]
        data, catalog_hash = (path.read_bytes() if path.is_file() else _download(base + path.name)
                              for path in targets)
        if not re.fullmatch(rb'[0-9a-fA-F]{32}', catalog_hash):
            raise ValueError('Invalid iOS catalog hash: ' + stem)
        try:
            remotes = [row['internalId'] for row in Catalog(data).parse()['locations']
                       if row['internalId'].startswith(('http://', 'https://'))]
            if not any('/asset/iOS/' in url for url in remotes) or any('/asset/Android/' in url for url in remotes):
                raise ValueError('The catalog is not for iOS: ' + stem)
        finally:
            Catalog.string.cache_clear()
            Catalog.type_info.cache_clear()
            Catalog.location.cache_clear()
        for target, content in zip(targets, (data, catalog_hash)):
            if not target.is_file():
                _atomic(target, content)
            result.append({'name': target.name, 'bytes': len(content),
                           'sha256': hashlib.sha256(content).hexdigest()})
        _progress(on_progress, 'catalogs', 'Preparing iOS catalogs', len(result) // 2, len(locales))
    return result


def update_public_data(root, *, on_progress=None, backup_before_update=False, apk_file=None,
                       include_ios_catalogs=False):
    root = Path(root)
    with workspace_lock(root):
        if NativeChartCandidates(root)._active() is not None:
            raise PublicUpdateError('Revoke the active chart candidate before updating public data.')
        return _update_public_data(root, on_progress, backup_before_update, apk_file, include_ios_catalogs)


def _update_public_data(root, on_progress, backup_before_update, apk_file, include_ios_catalogs):
    public = root / '.native-server/public'
    previous, package, version = _query_public_version(root, on_progress, apk_file)
    master_version, cdn_root = version['masterVersion'], version['cdnRoot']
    manifest_data = _download(version['masterManifestUrl'])
    manifest = json.loads(manifest_data)
    if manifest['version'] != master_version:
        raise ValueError('The official master manifest version did not match.')
    entries = manifest['files']
    names = set()
    for entry in entries:
        name = entry['name']
        if (not re.fullmatch(r'Master[A-Za-z0-9_]+\.bin', name) or name in names
                or not re.fullmatch('[0-9a-f]{64}', entry['hash'])
                or type(entry['size']) is not int or entry['size'] < 0):
            raise ValueError('Invalid entry in the official master manifest.')
        names.add(name)
    backup_directory = None
    if backup_before_update and previous:
        _progress(on_progress, 'backing-up', 'Backing up the version, master data and local accounts')
        backup_directory = _backup_public_data(root, previous)
    cache = public / 'masterdata' / master_version
    cache.mkdir(parents=True, exist_ok=True)
    _progress(on_progress, 'downloading', 'Preparing public master data', 0, len(entries))

    def fetch(entry):
        encrypted = cache / entry['name']
        data = encrypted.read_bytes() if encrypted.is_file() else None
        if data is None or len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['hash']:
            data = _download(cdn_root + '/master/' + master_version + '/' + entry['name'])
        if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['hash']:
            raise ValueError('Official master-data verification failed: ' + entry['name'])
        decoded = json.loads(master(data, package['keys']))
        decoded_path = encrypted.with_suffix('.json')
        _atomic(encrypted, data)
        _atomic(decoded_path, _json_bytes(decoded))
        return {'name': entry['name'], 'bytes': len(data), 'sha256': entry['hash'],
                'rowCount': len(decoded['_allData'])}

    results = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        pending = [executor.submit(fetch, entry) for entry in entries]
        for future in as_completed(pending):
            results.append(future.result())
            _progress(on_progress, 'downloading', 'Preparing public master data', len(results), len(entries))
    from deathnote.game.live.rules import NativeLiveRules
    from deathnote.game.live.rating import NativeLiveRating
    NativeLiveRules(cache)
    NativeLiveRating(cache)
    report = {'recordedAt': datetime.now(timezone.utc).isoformat(),
              'masterVersion': master_version, 'directory': str(cache),
              'authenticationUsed': False, 'verifiedFiles': len(results),
              'downloadedBytes': sum(row['bytes'] for row in results),
              'files': sorted(results, key=lambda row: row['name'])}
    catalogs = _fetch_ios_catalogs(public, version, on_progress) if include_ios_catalogs else []
    _progress(on_progress, 'publishing', 'Publishing the public version', len(results), len(entries))
    if previous:
        history = public / 'version-history' / previous['masterVersion']
        history.mkdir(parents=True, exist_ok=True)
        for name in ('version.json', 'master-manifest.json'):
            if (public / name).is_file() and not (history / name).exists():
                shutil.copyfile(public / name, history / name)
    _atomic(cache / 'manifest.json', manifest_data)
    _atomic(public / 'master-cache.json', _json_bytes(report))
    _atomic(public / 'master-manifest.json', manifest_data)
    _atomic(public / 'version.json', _json_bytes(version))
    return {'version': version, 'verifiedFiles': len(results),
            'verifiedBytes': report['downloadedBytes'], 'recordedAt': report['recordedAt'],
            'backupDirectory': backup_directory, 'iosCatalogs': catalogs}

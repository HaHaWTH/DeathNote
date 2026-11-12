from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import struct

from .catalog import Catalog
from .locking import workspace_lock
from .chart_workspace import candidate_stage
from deathnote.game.live.rating import NativeLiveRating
from deathnote.game.live.rules import NativeLiveRules
from .public_update import _official_url


_VERSION_FIELDS = ('clientVersion', 'masterVersion', 'resourceVersion', 'region',
                   'apiRoot', 'cdnRoot', 'masterManifestUrl')
_LOCALE_SUFFIXES = ('', '_en', '_ko', '_zh-Hans', '_zh-Hant')
_METADATA = ('master-cache.json', 'master-manifest.json', 'version.json')


def _contained(root, *parts):
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'Public data path leaves the selected directory: {path.name}')
    return path


def _read_object(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object in the public snapshot.')
    return value


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')


def _reject_active(root, label):
    marker = root / '.native-server/chart-candidate-active.json'
    if marker.exists() or marker.is_symlink():
        raise ValueError(f'Revoke the active chart candidate in the {label} before importing.')


def _version(raw):
    source = _read_object(raw)
    if any(not isinstance(source.get(name), str) or not source[name] for name in _VERSION_FIELDS):
        raise ValueError('The public snapshot is missing its official version fields.')
    version = {name: source[name] for name in _VERSION_FIELDS}
    master, resource = version['masterVersion'], version['resourceVersion']
    if not re.fullmatch(r'[0-9a-f]{32}', master) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+\.[0-9a-f]{8}', resource):
        raise ValueError('The public snapshot has an invalid master or resource version.')
    for key in ('apiRoot', 'cdnRoot', 'masterManifestUrl'):
        _official_url(version[key])
    expected_url = version['cdnRoot'].rstrip('/') + f'/master/{master}/MasterManifest.json'
    if version['masterManifestUrl'] != expected_url:
        raise ValueError('The master manifest URL does not match the public version.')
    return version


def _stage_masters(source, stage, public, version):
    manifest_raw = _contained(source, 'master-manifest.json').read_bytes()
    manifest = _read_object(manifest_raw)
    entries = manifest.get('files')
    master = version['masterVersion']
    if manifest.get('version') != master or not isinstance(entries, list) or not entries:
        raise ValueError('The master manifest does not match the public version.')
    source_master = _contained(source, 'masterdata', master)
    if _read_object(_contained(source_master, 'manifest.json').read_bytes()) != manifest:
        raise ValueError('The versioned master manifest differs from the public manifest.')
    target_master = stage / 'masterdata' / master
    target_master.mkdir(parents=True)
    (target_master / 'manifest.json').write_bytes(manifest_raw)
    names, records = set(), []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Invalid master manifest entry.')
        name, length, digest = entry.get('name'), entry.get('size'), entry.get('hash')
        if (not isinstance(name, str) or not re.fullmatch(r'Master[A-Za-z0-9_]+\.bin', name)
                or name in names or type(length) is not int or length < 0
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('Invalid or duplicate master manifest entry.')
        names.add(name)
        raw = _contained(source_master, name).read_bytes()
        if len(raw) != length or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f'Master data failed its size or SHA-256 check: {name}')
        json_name = Path(name).with_suffix('.json').name
        decoded = _contained(source_master, json_name).read_bytes()
        rows = _read_object(decoded).get('_allData')
        if not isinstance(rows, list):
            raise ValueError(f'Master data has no decoded row list: {json_name}')
        (target_master / name).write_bytes(raw)
        (target_master / json_name).write_bytes(decoded)
        records.append({'name': name, 'bytes': length, 'sha256': digest, 'rowCount': len(rows)})
    NativeLiveRules(target_master)
    NativeLiveRating(target_master)
    (stage / 'master-manifest.json').write_bytes(manifest_raw)
    report = {'recordedAt': datetime.now(timezone.utc).isoformat(), 'masterVersion': master,
              'directory': str(public / 'masterdata' / master), 'authenticationUsed': False,
              'verifiedFiles': len(records), 'downloadedBytes': 0,
              'files': sorted(records, key=lambda row: row['name'])}
    (stage / 'master-cache.json').write_bytes(_json_bytes(report))
    return len(records)


def _validate_catalog(data, checksum, stem):
    if not re.fullmatch(rb'[0-9a-fA-F]{32}', checksum):
        raise ValueError(f'Invalid iOS catalog hash file: {stem}')
    try:
        locations = Catalog(data).parse()['locations']
        remotes = [row['internalId'] for row in locations if row['internalId'].startswith(('https://', 'http://'))]
    except (ValueError, struct.error) as error:
        raise ValueError(f'Invalid iOS catalog: {stem}') from error
    finally:
        Catalog.string.cache_clear()
        Catalog.type_info.cache_clear()
        Catalog.location.cache_clear()
    if not any('/asset/iOS/' in url for url in remotes) or any('/asset/Android/' in url for url in remotes):
        raise ValueError(f'Expected an iOS catalog: {stem}')


def _stage_catalogs(source, stage, resource):
    source_catalogs = _contained(source, 'resource-versions', resource)
    destination = stage / 'resource-versions' / resource
    pairs = 0
    for suffix in _LOCALE_SUFFIXES:
        stem = 'catalog_' + resource + suffix
        binary = _contained(source_catalogs, stem + '.bin')
        hash_file = _contained(source_catalogs, stem + '.hash')
        if not binary.exists() and not hash_file.exists():
            continue
        if not binary.is_file() or not hash_file.is_file():
            raise ValueError(f'The iOS catalog is missing its .bin or .hash: {stem}')
        data, checksum = binary.read_bytes(), hash_file.read_bytes()
        _validate_catalog(data, checksum, stem)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / binary.name).write_bytes(data)
        (destination / hash_file.name).write_bytes(checksum)
        pairs += 1
    return pairs


def _install_versioned_files(stage, public):
    files = []
    for root in (stage / 'masterdata', stage / 'resource-versions'):
        for staged in root.glob('*/*'):
            path = _contained(public, *staged.relative_to(stage).parts)
            if path.exists() and path.read_bytes() != staged.read_bytes():
                raise ValueError(f'The destination already has different bytes for this version: {path.name}')
            files.append((staged, path))
    for staged, path in files:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(path)


def _publish_metadata(stage, public):
    backups = {}
    installed = []
    for name in _METADATA:
        path = _contained(public, name)
        if path.exists():
            backup = stage / (name + '.previous')
            backup.write_bytes(path.read_bytes())
            backups[name] = backup
    try:
        for name in _METADATA:
            (stage / name).replace(public / name)
            installed.append(name)
    except OSError:
        for name in reversed(installed):
            path = public / name
            if name in backups:
                backups[name].replace(path)
            else:
                path.unlink()
        raise


def _backup_public_data(root, previous):
    public = root / '.native-server/public'
    recorded_at = datetime.now(timezone.utc)
    destination = root / '.native-server/backups' / (recorded_at.strftime('%Y%m%dT%H%M%S%fZ')
                                                    + '-before-master-import')
    backup_public = destination / 'public'
    try:
        backup_public.mkdir(parents=True, exist_ok=False)
        for name in _METADATA:
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
                        raise RuntimeError('The pre-import account backup failed its integrity check.')
        report = {'recordedAt': recorded_at.isoformat(), 'version': previous,
                  'accountBackedUp': account_backed_up}
        (destination / 'report.json').write_bytes(_json_bytes(report))
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError('The pre-import backup failed.') from error
    return destination.relative_to(root).as_posix()


def import_public_data(destination_root, source_root, *, backup_before_import=False):
    destination_root, source_root = Path(destination_root).resolve(), Path(source_root).resolve()
    if destination_root == source_root:
        raise ValueError('Choose a different project directory as the import source.')
    source = _contained(source_root, '.native-server', 'public').resolve()
    public = _contained(destination_root, '.native-server', 'public').resolve()
    if source == public:
        raise ValueError('The source and destination share the same public data directory.')
    _reject_active(source_root, 'source')
    source_version = _contained(source, 'version.json')
    raw_version = source_version.read_bytes()
    version = _version(raw_version)
    backup_directory = None
    with workspace_lock(destination_root):
        _reject_active(destination_root, 'destination')
        public.mkdir(parents=True, exist_ok=True)
        with candidate_stage(public) as stage:
            master_count = _stage_masters(source, stage, public, version)
            catalog_pairs = _stage_catalogs(source, stage, version['resourceVersion'])
            if source_version.read_bytes() != raw_version:
                raise ValueError('The source public version changed during import. Run the import again.')
            _reject_active(source_root, 'source')
            _reject_active(destination_root, 'destination')
            (stage / 'version.json').write_bytes(_json_bytes(version))
            current_version = _contained(public, 'version.json')
            if backup_before_import and current_version.is_file():
                previous = _version(current_version.read_bytes())
                backup_directory = _backup_public_data(destination_root, previous)
            _install_versioned_files(stage, public)
            _publish_metadata(stage, public)
    return {'version': version, 'verifiedFiles': master_count, 'catalogPairs': catalog_pairs,
            'backupDirectory': backup_directory}

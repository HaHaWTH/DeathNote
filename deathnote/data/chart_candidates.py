"""Validate, activate and revoke local chart snapshots without controlling services."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile


_VERSION_KEYS = ('clientVersion', 'masterVersion', 'resourceVersion', 'region')
_METADATA_NAMES = ('version.json', 'master-manifest.json', 'master-cache.json')
_CATALOG_SUFFIXES = ('', '_en', '_ko', '_zh-Hans', '_zh-Hant')
_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,79}\Z')
_MASTER = re.compile(r'[0-9a-f]{32}\Z')
_RESOURCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SEGMENT = re.compile(r'[A-Za-z0-9_.-]+\Z')
_MASTER_FILE = re.compile(r'Master[A-Za-z0-9_]+\.(bin|json)\Z')


class NativeChartCandidateError(Exception):
    def __init__(self, status, message, code):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _fail(status, message, code):
    raise NativeChartCandidateError(status, message, code)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf8')


def _read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        _fail(409, '候选或版本资料读取失败。', 'candidate_metadata_invalid')


def _atomic(path, data):
    stream = tempfile.NamedTemporaryFile(dir=path.parent, prefix='.chart-candidate-', suffix='.tmp', delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _version(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(name), str) or not value[name] for name in _VERSION_KEYS):
        _fail(409, '候选版本字段未齐全。', 'candidate_version_invalid')
    if not _MASTER.fullmatch(value['masterVersion']) or not _RESOURCE.fullmatch(value['resourceVersion']):
        _fail(409, '候选主数据或资源版本格式错误。', 'candidate_version_invalid')
    return {name: value[name] for name in _VERSION_KEYS}


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        _fail(400, '候选文件路径格式错误。', 'candidate_path_invalid')
    parts = value.split('/')
    if any(not _SEGMENT.fullmatch(part) or part in ('.', '..') for part in parts):
        _fail(400, '候选文件路径超出允许范围。', 'candidate_path_invalid')
    return PurePosixPath(value)


def _file_record(row):
    if (not isinstance(row, dict) or type(row.get('bytes')) is not int or row['bytes'] < 0
            or not isinstance(row.get('sha256'), str) or not _HASH.fullmatch(row['sha256'])):
        _fail(400, '候选文件清单格式错误。', 'candidate_files_invalid')
    relative = _relative(row.get('path'))
    return {'path': relative.as_posix(), 'bytes': row['bytes'], 'sha256': row['sha256']}


def _matches(path, record):
    return (path.is_file() and path.stat().st_size == record['bytes']
            and hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256'])


def _song_metadata(songs):
    result = []
    if not isinstance(songs, list):
        return result
    for song in songs:
        if not isinstance(song, dict):
            continue
        record = {name: song[name] for name in ('musicId', 'title', 'sourceBand', 'sourceSongId')
                  if type(song.get(name)) in (str, int)}
        record['charts'] = []
        for chart in song.get('charts', []) if isinstance(song.get('charts'), list) else []:
            if isinstance(chart, dict):
                record['charts'].append({name: chart[name] for name in (
                    'sourceDifficulty', 'targetDifficulty', 'scoreId', 'level', 'fullComboCount')
                    if type(chart.get(name)) in (str, int, float)})
        result.append(record)
    return result


class NativeChartCandidates:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = self.root / '.native-server'
        self.public = self.state / 'public'
        self.candidates = self.state / 'candidates'
        self.active_file = self.state / 'chart-candidate-active.json'

    def _contained(self, base, relative, *, required=False):
        path = base.joinpath(*_relative(relative).parts)
        resolved = path.resolve()
        if not base.resolve().is_relative_to(self.root) or not resolved.is_relative_to(base.resolve()):
            _fail(400, '候选文件路径超出允许范围。', 'candidate_path_invalid')
        if required and not resolved.is_file():
            _fail(409, '候选文件尚未齐全。', 'candidate_file_missing')
        return resolved

    def _candidate_dir(self, candidate_id):
        if not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id):
            _fail(400, '候选标识格式错误。', 'invalid_candidate_id')
        return self._contained(self.candidates, candidate_id)

    def _manifest(self, candidate_id):
        directory = self._candidate_dir(candidate_id)
        path = self._contained(directory, 'candidate.json')
        if not path.is_file():
            _fail(404, '本地候选不存在。', 'candidate_not_found')
        manifest = _read_json(path)
        if (not isinstance(manifest, dict) or manifest.get('schemaVersion') != 1
                or manifest.get('candidateId') != candidate_id
                or not isinstance(manifest.get('label'), str) or not manifest['label']):
            _fail(409, '候选说明格式错误。', 'candidate_manifest_invalid')
        _version(manifest.get('baseVersion'))
        _version(manifest.get('version'))
        return directory, manifest

    def _current_version(self):
        path = self._contained(self.public, 'version.json', required=True)
        value = _read_json(path)
        _version(value)
        return value

    def _active(self):
        if not self.active_file.is_file():
            return None
        path = self._contained(self.state, 'chart-candidate-active.json', required=True)
        active = _read_json(path)
        if (not isinstance(active, dict) or active.get('schemaVersion') != 1
                or not isinstance(active.get('label'), str)
                or not isinstance(active.get('backupDirectory'), str)):
            _fail(409, '已加载候选的记录无效。', 'candidate_state_invalid')
        self._candidate_dir(active.get('candidateId'))
        _version(active.get('baseVersion'))
        _version(active.get('activatedVersion'))
        return active

    @staticmethod
    def _active_metadata(active):
        return {'candidateId': active['candidateId'], 'label': active['label'],
                'version': _version(active['activatedVersion']), 'baseVersion': _version(active['baseVersion']),
                'backupDirectory': active['backupDirectory']}

    def status(self):
        candidates = []
        if self.candidates.is_dir():
            for directory in sorted(self.candidates.iterdir()):
                if not directory.is_dir() or not _ID.fullmatch(directory.name):
                    continue
                try:
                    _, manifest = self._manifest(directory.name)
                    candidates.append({'candidateId': manifest['candidateId'], 'label': manifest['label'],
                                       'version': _version(manifest['version']), 'baseVersion': _version(manifest['baseVersion']),
                                       'songs': _song_metadata(manifest.get('songs', []))})
                except NativeChartCandidateError as error:
                    candidates.append({'candidateId': directory.name, 'label': directory.name,
                                       'available': False, 'errorCode': error.code})
        active = self._active()
        return {'active': self._active_metadata(active) if active else None, 'candidates': candidates}

    def _validate(self, candidate_id):
        directory, manifest = self._manifest(candidate_id)
        base, version = manifest['baseVersion'], manifest['version']
        current = self._current_version()
        if _version(base) != _version(current):
            _fail(409, '候选基线与当前本地版本不同，请重新生成候选。', 'candidate_baseline_mismatch')
        if (version['clientVersion'] != base['clientVersion'] or version['region'] != base['region']
                or any(version.get(name) != current.get(name) for name in ('apiRoot', 'cdnRoot'))):
            _fail(409, '谱面候选须沿用原客户端、区服和服务地址。', 'candidate_version_invalid')
        if version['masterVersion'] == base['masterVersion'] or version['resourceVersion'] == base['resourceVersion']:
            _fail(409, '候选须使用独立的主数据和资源版本。', 'candidate_version_not_unique')
        if not isinstance(manifest.get('files'), list):
            _fail(400, '候选文件清单格式错误。', 'candidate_files_invalid')
        files = {}
        master_prefix = 'public/masterdata/' + version['masterVersion'] + '/'
        resource_prefix = 'public/resource-versions/' + version['resourceVersion'] + '/'
        for item in manifest['files']:
            record = _file_record(item)
            name = record['path']
            if name in files:
                _fail(400, '候选文件清单存在重复路径。', 'candidate_files_invalid')
            allowed = name in ('public/' + filename for filename in _METADATA_NAMES)
            if name.startswith(master_prefix):
                leaf = name[len(master_prefix):]
                allowed = leaf == 'manifest.json' or bool(_MASTER_FILE.fullmatch(leaf))
            elif name.startswith(resource_prefix):
                leaf = name[len(resource_prefix):]
                allowed = bool(re.fullmatch(r'catalog_' + re.escape(version['resourceVersion']) + r'(?:_[A-Za-z0-9_.-]+)?\.(bin|hash)', leaf))
            elif name.startswith('assets/asset/iOS/'):
                allowed = name.endswith('.bundle')
            if not allowed:
                _fail(400, '候选包含管理范围外的文件。', 'candidate_path_invalid')
            source = self._contained(directory, name, required=True)
            if not _matches(source, record):
                _fail(409, '候选文件校验失败。', 'candidate_checksum_mismatch')
            files[name] = {**record, 'source': source}
        required = {'public/' + name for name in _METADATA_NAMES}
        required.add(master_prefix + 'manifest.json')
        required.update(resource_prefix + 'catalog_' + version['resourceVersion'] + locale + '.' + suffix
                        for locale in _CATALOG_SUFFIXES for suffix in ('bin', 'hash'))
        if not required.issubset(files):
            _fail(409, '候选版本、主数据清单或资源目录尚未齐全。', 'candidate_file_missing')
        if _read_json(files['public/version.json']['source']) != version:
            _fail(409, '候选版本文件与说明不一致。', 'candidate_version_invalid')
        master_manifest = _read_json(files['public/master-manifest.json']['source'])
        if (not isinstance(master_manifest, dict) or master_manifest.get('version') != version['masterVersion']
                or not isinstance(master_manifest.get('files'), list) or not master_manifest['files']
                or _read_json(files[master_prefix + 'manifest.json']['source']) != master_manifest):
            _fail(409, '候选主数据清单与版本不一致。', 'candidate_master_invalid')
        master_names = set()
        for entry in master_manifest['files']:
            if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str)
                    or not re.fullmatch(r'Master[A-Za-z0-9_]+\.bin', entry['name'])
                    or entry['name'] in master_names or type(entry.get('size')) is not int or entry['size'] < 0
                    or not isinstance(entry.get('hash'), str) or not _HASH.fullmatch(entry['hash'])):
                _fail(409, '候选主数据条目格式错误。', 'candidate_master_invalid')
            master_names.add(entry['name'])
            binary = files.get(master_prefix + entry['name'])
            decoded = files.get(master_prefix + entry['name'][:-4] + '.json')
            if not binary or not decoded:
                _fail(409, '候选主数据文件尚未齐全。', 'candidate_file_missing')
            if binary['bytes'] != entry['size'] or binary['sha256'] != entry['hash']:
                _fail(409, '候选主数据与清单校验值不同。', 'candidate_master_invalid')
            table = _read_json(decoded['source'])
            if not isinstance(table, dict) or not isinstance(table.get('_allData'), list):
                _fail(409, '候选解码主数据格式错误。', 'candidate_master_invalid')
        cache = _read_json(files['public/master-cache.json']['source'])
        if not isinstance(cache, dict) or ('masterVersion' in cache and cache['masterVersion'] != version['masterVersion']):
            _fail(409, '候选主数据缓存报告与版本不同。', 'candidate_master_invalid')
        overrides = self._validate_overrides(directory, manifest.get('assetOverrides'), files=files)
        return directory, manifest, files, overrides

    def _validate_overrides(self, directory, overrides, *, files=None):
        if not isinstance(overrides, list) or not overrides:
            _fail(409, '候选谱面资源尚未齐全。', 'candidate_assets_invalid')
        result = {}
        for item in overrides:
            if not isinstance(item, dict):
                _fail(400, '候选资源映射格式错误。', 'candidate_assets_invalid')
            request = _relative(item.get('requestPath')).as_posix()
            record = _file_record({'path': item.get('file'), 'bytes': item.get('bytes'), 'sha256': item.get('sha256')})
            if (not request.startswith('asset/iOS/') or not request.endswith('.bundle')
                    or record['path'] != 'assets/' + request or request in result):
                _fail(400, '候选资源映射超出 iOS bundle 范围。', 'candidate_assets_invalid')
            if files is not None and (record['path'] not in files or any(files[record['path']][name] != record[name] for name in ('bytes', 'sha256'))):
                _fail(409, '候选资源映射与文件清单不同。', 'candidate_assets_invalid')
            source = self._contained(directory, record['path'], required=True)
            if not _matches(source, record):
                _fail(409, '候选资源校验失败。', 'candidate_checksum_mismatch')
            result[request] = {'requestPath': request, 'file': record['path'], 'bytes': record['bytes'], 'sha256': record['sha256']}
        return list(result.values())

    def _config(self):
        path = self._contained(self.state, 'config.json', required=True)
        config = _read_json(path)
        if not isinstance(config, dict):
            _fail(409, '本地服务配置格式错误。', 'candidate_config_invalid')
        return config

    def _apply_config_patch(self, patch, *, activate):
        config = self._config()
        if activate:
            config['resourceMirror'] = True
        elif patch['resourceMirror']['existed']:
            config['resourceMirror'] = patch['resourceMirror']['value']
        else:
            config.pop('resourceMirror', None)
        _atomic(self.state / 'config.json', _json_bytes(config))

    def _backup(self, candidate_id, current, config_patch):
        stamp = datetime.now(timezone.utc)
        relative = '.native-server/backups/' + stamp.strftime('%Y%m%dT%H%M%S%fZ') + '-chart-candidate-' + candidate_id
        directory = self._contained(self.root, relative)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / 'public').mkdir()
        metadata = []
        for name in _METADATA_NAMES:
            source = self._contained(self.public, name)
            record = {'name': name, 'existed': source.is_file()}
            if record['existed']:
                data = source.read_bytes()
                (directory / 'public' / name).write_bytes(data)
                record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            metadata.append(record)
        database = self._contained(self.state, 'players.sqlite3')
        account_backed_up = database.is_file()
        if account_backed_up:
            (directory / 'PRIVATE').mkdir()
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(directory / 'PRIVATE/players.sqlite3')) as target:
                    source.backup(target, pages=256)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        _fail(409, '候选加载前账号备份完整性检查失败。', 'candidate_backup_failed')
        report = {'schemaVersion': 1, 'candidateId': candidate_id, 'recordedAt': stamp.isoformat(),
                  'version': current, 'accountBackedUp': account_backed_up, 'publicMetadata': metadata,
                  'configPatch': config_patch}
        (directory / 'report.json').write_bytes(_json_bytes(report))
        return relative, metadata

    def _restore_inputs(self, active):
        relative = active.get('backupDirectory')
        if not isinstance(relative, str) or not relative.startswith('.native-server/backups/'):
            _fail(409, '候选恢复点路径无效。', 'candidate_recovery_invalid')
        directory = self._contained(self.root, relative)
        records = active.get('publicMetadata')
        if (not isinstance(records, list) or len(records) != len(_METADATA_NAMES)
                or {row.get('name') for row in records if isinstance(row, dict)} != set(_METADATA_NAMES)):
            _fail(409, '候选恢复点清单无效。', 'candidate_recovery_invalid')
        data = {}
        for record in records:
            name = record['name']
            if type(record.get('existed')) is not bool:
                _fail(409, '候选恢复点清单无效。', 'candidate_recovery_invalid')
            if not record['existed']:
                data[name] = None
                continue
            if (type(record.get('bytes')) is not int or record['bytes'] < 0
                    or not isinstance(record.get('sha256'), str) or not _HASH.fullmatch(record['sha256'])):
                _fail(409, '候选恢复点清单无效。', 'candidate_recovery_invalid')
            source = self._contained(directory, 'public/' + name, required=True)
            if not _matches(source, record):
                _fail(409, '候选恢复点校验失败。', 'candidate_recovery_invalid')
            data[name] = source.read_bytes()
        if data['version.json'] is None or _version(json.loads(data['version.json'])) != _version(active['baseVersion']):
            _fail(409, '候选恢复点与原版本不同。', 'candidate_recovery_invalid')
        patch = active.get('configPatch')
        if (not isinstance(patch, dict) or not isinstance(patch.get('resourceMirror'), dict)
                or type(patch['resourceMirror'].get('existed')) is not bool
                or (patch['resourceMirror']['existed'] and 'value' not in patch['resourceMirror'])):
            _fail(409, '候选配置恢复记录无效。', 'candidate_recovery_invalid')
        self._config()
        return data, patch

    def _restore_metadata(self, data):
        for name in ('master-manifest.json', 'master-cache.json', 'version.json'):
            path = self._contained(self.public, name)
            if data[name] is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, data[name])

    def activate(self, candidate_id):
        if self._active() is not None:
            _fail(409, '已有已加载候选，请先撤销。', 'candidate_already_active')
        _, manifest, files, overrides = self._validate(candidate_id)
        metadata_payloads = {}
        for name in _METADATA_NAMES:
            record = files['public/' + name]
            data = record['source'].read_bytes()
            if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                _fail(409, '候选版本资料在加载前发生变化。', 'candidate_checksum_mismatch')
            metadata_payloads[name] = data
        current, config = self._current_version(), self._config()
        config_patch = {'resourceMirror': {'existed': 'resourceMirror' in config,
                                           'value': config.get('resourceMirror')}}
        try:
            backup_directory, metadata = self._backup(candidate_id, current, config_patch)
        except (OSError, sqlite3.Error):
            _fail(409, '候选加载前备份失败，当前本地版本已保留。', 'candidate_backup_failed')
        active = {'schemaVersion': 1, 'candidateId': candidate_id, 'label': manifest['label'],
                  'baseVersion': current, 'activatedVersion': manifest['version'],
                  'activatedAt': datetime.now(timezone.utc).isoformat(), 'backupDirectory': backup_directory,
                  'publicMetadata': metadata, 'configPatch': config_patch, 'assetOverrides': overrides}
        for name, record in files.items():
            if not name.startswith(('public/masterdata/', 'public/resource-versions/')):
                continue
            target = self._contained(self.public, name.removeprefix('public/'))
            if target.is_file() and not _matches(target, record):
                _fail(409, '目标版本目录已有不同内容。', 'candidate_destination_conflict')
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                data = record['source'].read_bytes()
                if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                    _fail(409, '候选文件在加载前发生变化。', 'candidate_checksum_mismatch')
                _atomic(target, data)
        restore_data, _ = self._restore_inputs(active)
        try:
            self._apply_config_patch(config_patch, activate=True)
            for name in ('master-manifest.json', 'master-cache.json'):
                _atomic(self.public / name, metadata_payloads[name])
            _atomic(self.active_file, _json_bytes(active))
            _atomic(self.public / 'version.json', metadata_payloads['version.json'])
        except OSError:
            try:
                self._apply_config_patch(config_patch, activate=False)
                self._restore_metadata(restore_data)
                self.active_file.unlink(missing_ok=True)
            except OSError:
                _fail(500, '候选发布和恢复操作中断，恢复点已保留。', 'candidate_restore_failed')
            _fail(500, '候选发布失败，原版本资料已恢复。', 'candidate_publish_failed')
        return {'active': True, **self._active_metadata(active)}

    def revoke(self):
        active = self._active()
        if active is None:
            _fail(409, '当前没有已加载的候选。', 'candidate_inactive')
        current = _version(self._current_version())
        if current not in (_version(active['activatedVersion']), _version(active['baseVersion'])):
            _fail(409, '当前版本已改变，候选恢复点已保留。', 'candidate_version_changed')
        data, patch = self._restore_inputs(active)
        current_metadata = {name: (self.public / name).read_bytes() if (self.public / name).is_file() else None
                            for name in _METADATA_NAMES}
        config = self._config()
        current_patch = {'resourceMirror': {'existed': 'resourceMirror' in config,
                                            'value': config.get('resourceMirror')}}
        try:
            self._apply_config_patch(patch, activate=False)
            self._restore_metadata(data)
            self.active_file.unlink()
        except OSError:
            try:
                self._apply_config_patch(current_patch, activate=False)
                self._restore_metadata(current_metadata)
            except OSError:
                _fail(500, '候选撤销和恢复操作中断，恢复点已保留。', 'candidate_restore_failed')
            _fail(500, '候选撤销失败，加载状态已保留。', 'candidate_revoke_failed')
        return {'active': False, 'candidateId': active['candidateId'], 'label': active['label'],
                'version': _version(active['baseVersion']), 'backupDirectory': active['backupDirectory']}

    def active_asset_overrides(self, version):
        active = self._active()
        if active is None or _version(active['activatedVersion']) != _version(version):
            return {}
        directory = self._candidate_dir(active['candidateId'])
        overrides = self._validate_overrides(directory, active.get('assetOverrides'))
        return {row['requestPath']: self._contained(directory, row['file'], required=True) for row in overrides}

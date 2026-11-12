import json
from pathlib import Path
from urllib.parse import urlsplit


def read_version(path):
    version = json.loads(Path(path).read_text(encoding='utf8'))
    for name in ('masterVersion', 'resourceVersion', 'region', 'cdnRoot', 'apiRoot'):
        if not isinstance(version[name], str) or not version[name]:
            raise ValueError('Invalid local version field: ' + name)
    for name in ('cdnRoot', 'apiRoot'):
        if urlsplit(version[name]).scheme not in ('https', 'http') or not urlsplit(version[name]).hostname:
            raise ValueError('Invalid local version URL: ' + name)
    return version

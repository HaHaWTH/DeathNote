"""Read Unity Addressables v2 binary catalogs without Unity.

Layout follows official com.unity.addressables 2.9.1 Runtime sources:
ResourceLocators/ContentCatalogData.cs and ResourceManager/Util/BinaryStorageBuffer.cs.
The output preserves unresolved runtime property references in internal IDs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
import struct

NULL = 0xFFFFFFFF
MASK = 0x3FFFFFFF


class Catalog:
    def __init__(self, data: bytes):
        self.data = data
        self.header = dict(zip(
            ('magic', 'version', 'keysOffset', 'idOffset', 'instanceProvider',
             'sceneProvider', 'initObjectsArray', 'buildResultHash'),
            self.unpack('8I', 0)))
        if self.header['magic'] != 0x0DE38942 or self.header['version'] != 2:
            raise ValueError(f'Unsupported binary catalog header: {self.header}')

    def unpack(self, fmt, offset):
        return struct.unpack_from('<' + fmt, self.data, offset)

    def array(self, offset, fmt='I'):
        if offset == NULL:
            return []
        size, = self.unpack('I', offset - 4)
        if offset + size > len(self.data):
            raise ValueError(f'Array at {offset} extends beyond the file')
        return list(struct.iter_unpack('<' + fmt, self.data[offset:offset + size]))

    @lru_cache(maxsize=None)
    def string(self, offset, sep=''):
        if offset == NULL:
            return None
        if sep and offset & 0x40000000:
            parts = []
            while offset != NULL:
                part, offset = self.unpack('II', offset & MASK)
                parts.append(self.string(part))
            return sep.join(reversed(parts))
        encoding = 'utf-16-le' if offset & 0x80000000 else 'ascii'
        offset &= MASK
        size, = self.unpack('I', offset - 4)
        if offset + size > len(self.data):
            raise ValueError(f'String at {offset} extends beyond the file')
        return self.data[offset:offset + size].decode(encoding)

    @lru_cache(maxsize=None)
    def type_info(self, offset):
        assembly, cls = self.unpack('II', offset)
        return {'assembly': self.string(assembly, '.'), 'class': self.string(cls, '.')}

    def object(self, offset):
        if offset == NULL:
            return None
        type_id, obj_id = self.unpack('II', offset)
        t = self.type_info(type_id)
        cls = t['class']
        if obj_id == NULL:
            return None
        if cls == 'System.String':
            string_id, sep = self.unpack('IH', obj_id)
            return self.string(string_id, chr(sep) if sep else '')
        if cls == 'System.Int32':
            return self.unpack('i', obj_id)[0]
        if cls == 'System.Int64':
            return self.unpack('q', obj_id)[0]
        if cls == 'System.Boolean':
            return bool(self.unpack('?', obj_id)[0])
        if cls == 'UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions':
            hash_id, bundle_id, crc, bundle_size, common_id = self.unpack('5I', obj_id)
            timeout, redirect_limit, retry_count, flags = self.unpack('hBBi', common_id)
            return {'type': cls, 'bundleName': self.string(bundle_id, '_'),
                    'hashRawHex': self.data[hash_id:hash_id + 16].hex(),
                    'crc': crc, 'bundleSize': bundle_size, 'timeout': timeout,
                    'redirectLimit': redirect_limit, 'retryCount': retry_count, 'flags': flags}
        return {'type': cls, 'offset': obj_id}

    @lru_cache(maxsize=None)
    def location(self, offset):
        pk, iid, provider, deps, dep_hash, extra, type_id = self.unpack('4Ii2I', offset)
        return {'offset': offset, 'primaryKey': self.string(pk, '/'),
                'internalId': self.string(iid, '/'),
                'provider': self.string(provider, '.'),
                'resourceType': self.type_info(type_id),
                'dependencyOffsets': [x[0] for x in self.array(deps)],
                'dependencyHash': dep_hash, 'extraData': self.object(extra)}

    def parse(self):
        key_map = []
        seen = set()
        pending = []
        for key_offset, set_offset in self.array(self.header['keysOffset'], 'II'):
            offsets = [x[0] for x in self.array(set_offset)]
            key_map.append({'key': self.object(key_offset), 'locationOffsets': offsets})
            pending.extend(offsets)
        while pending:
            offset = pending.pop()
            if offset in seen:
                continue
            seen.add(offset)
            pending.extend(self.location(offset)['dependencyOffsets'])
        locations = [dict(self.location(offset)) for offset in sorted(seen)]
        by_offset = {l['offset']: l for l in locations}
        for loc in locations:
            loc['dependencyKeys'] = [by_offset[d]['primaryKey'] for d in loc['dependencyOffsets']]
            loc['dependencyInternalIds'] = [by_offset[d]['internalId'] for d in loc['dependencyOffsets']]
        return {'header': self.header, 'locatorId': self.string(self.header['idOffset']),
                'buildResultHash': self.string(self.header['buildResultHash']),
                'keys': key_map, 'locations': locations}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('catalog', type=Path)
    ap.add_argument('--output-dir', type=Path, required=True)
    args = ap.parse_args()
    raw = args.catalog.read_bytes()
    result = Catalog(raw).parse()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result['sourceFile'] = str(args.catalog.resolve())
    result['sha256'] = hashlib.sha256(raw).hexdigest()
    out = args.output_dir
    (out / 'catalog.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    locs = result['locations']
    bundles = [l for l in locs if l['extraData'] and l['extraData'].get('type', '').endswith('AssetBundleRequestOptions')]
    remote = [l for l in locs if l['internalId'].startswith(('http://', 'https://'))]
    summary = {'sourceFile': result['sourceFile'], 'sha256': result['sha256'],
               'fileSize': len(raw), 'version': result['header']['version'],
               'locatorId': result['locatorId'], 'buildResultHash': result['buildResultHash'],
               'keyCount': len(result['keys']), 'locationCount': len(locs),
               'bundleCount': len(bundles), 'bundleBytes': sum(l['extraData']['bundleSize'] for l in bundles),
               'remoteLocationCount': len(remote),
               'providers': dict(Counter(l['provider'] for l in locs)),
               'resourceTypes': dict(Counter(l['resourceType']['class'] for l in locs))}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'bundles.json').write_text(json.dumps(bundles, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'remote.json').write_text(json.dumps(remote, ensure_ascii=False, indent=2), encoding='utf-8')
    with (out / 'locations.tsv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['offset', 'primaryKey', 'internalId', 'provider', 'resourceType', 'dependencies'])
        for l in locs:
            w.writerow([l['offset'], l['primaryKey'], l['internalId'], l['provider'], l['resourceType']['class'], json.dumps(l['dependencyInternalIds'], ensure_ascii=False)])
    by_offset = {loc['offset']: loc for loc in locs}
    with (out / 'key-locations.tsv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['key', 'locationOffset', 'primaryKey', 'internalId'])
        for mapping in result['keys']:
            for offset in mapping['locationOffsets']:
                loc = by_offset[offset]
                w.writerow([mapping['key'], offset, loc['primaryKey'], loc['internalId']])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

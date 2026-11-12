"""Original anonymous announcement RPCs backed by an editable local directory."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from deathnote.protocol.rpc import NativeRpcError
from deathnote.game.live.single import int_value, parse_message
from deathnote.protocol.wire import integer_field, message_field, string_field


PREFIX = '/app.announcement.AnnouncementService/'
IMAGE_PREFIX = '/announcements/images/'
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}


def _image_resource(directory, reference, identifier, cache):
    if not reference:
        return '', None, None
    image = (directory / reference).resolve()
    if not image.is_relative_to(directory) or image.suffix.lower() not in IMAGE_TYPES:
        raise ValueError('Announcement image must be inside its announcement directory')
    if image not in cache:
        body = image.read_bytes()
        kind = IMAGE_TYPES[image.suffix.lower()]
        digest = hashlib.sha256(body).hexdigest()[:16]
        path = IMAGE_PREFIX + str(identifier) + '/' + digest + image.suffix.lower()
        cache[image] = path, body, kind
    return cache[image]


class NativeAnnouncement:
    methods = frozenset((PREFIX + 'GetList', PREFIX + 'Get'))

    def __init__(self, directory, *, image_base_url, clock=time.time):
        self.directory = Path(directory).resolve()
        self.image_base_url = image_base_url.rstrip('/')
        self.clock = clock

    def _entries(self):
        entries = {}
        for manifest in sorted(self.directory.glob('*/announcement.json')):
            data = json.loads(manifest.read_text('utf8'))
            identifier = data['id']
            if type(identifier) is not int or identifier < 1 or identifier in entries:
                raise ValueError('Announcement IDs must be unique positive integers')
            directory, cache = manifest.parent.resolve(), {}
            image_path, image_body, image_kind = image = _image_resource(directory, data.get('image'), identifier, cache)
            pickup_path, pickup_body, pickup_kind = (_image_resource(directory, data['pickupImage'], identifier, cache)
                                                      if data.get('pickupImage') else image)
            resources = {path: (body, kind) for path, body, kind in (image, (pickup_path, pickup_body, pickup_kind)) if path}
            entries[identifier] = {**data, 'imagePath': image_path, 'imageBody': image_body,
                                   'imageKind': image_kind, 'pickupImagePath': pickup_path,
                                   'imageResources': resources,
                                   'lastUpdatedAt': data.get('lastUpdatedAt', int(manifest.stat().st_mtime))}
        return entries

    def _visible(self, entry):
        return entry.get('startAt', 0) <= int(self.clock()) < entry.get('endAt', 4102444800)

    def _message(self, entry):
        image_url = self.image_base_url + entry['imagePath'] if entry['imagePath'] else ''
        pickup_url = self.image_base_url + entry['pickupImagePath'] if entry['pickupImagePath'] else ''
        result = (integer_field(1, entry['id']) + integer_field(2, entry.get('category', 1))
                  + string_field(3, entry['title']) + string_field(5, image_url)
                  + string_field(6, entry.get('body', '')) + integer_field(7, entry.get('startAt', 0))
                  + integer_field(8, entry.get('endAt', 4102444800))
                  + integer_field(9, entry.get('sortOrder', 0))
                  + integer_field(14, entry['lastUpdatedAt'])
                  + integer_field(16, entry.get('platform', 0)))
        if entry.get('pickup', False) and pickup_url:
            result += (string_field(11, pickup_url) + integer_field(12, entry.get('startAt', 0))
                       + integer_field(13, entry.get('endAt', 4102444800)))
        return result

    def home(self):
        return b''.join(message_field(1, self._message(entry)) for entry in self._entries().values()
                        if self._visible(entry))

    def dispatch(self, method, payload, headers):
        request = parse_message(payload)
        entries = self._entries()
        if method == PREFIX + 'GetList':
            selected = int_value(request, 1)
            if selected not in (0, 1, 2):
                raise NativeRpcError(3)
            values = [entry for entry in entries.values() if self._visible(entry)
                      and selected in entry.get('tabs', [0])]
            values.sort(key=lambda entry: (-entry.get('sortOrder', 0), -entry['lastUpdatedAt'], -entry['id']))
            return b''.join(message_field(1, self._message(entry)) for entry in values)
        if method == PREFIX + 'Get':
            entry = entries.get(int_value(request, 1))
            if entry is None or not self._visible(entry):
                raise NativeRpcError(5)
            return message_field(1, self._message(entry))
        raise NotImplementedError(method)

    def http_object(self, path):
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith(IMAGE_PREFIX):
            return None
        for entry in self._entries().values():
            if self._visible(entry) and parsed.path in entry['imageResources']:
                return entry['imageResources'][parsed.path]
        return None

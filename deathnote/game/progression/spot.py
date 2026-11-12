"""Original home-scene ownership and the complete favorite selection RPC."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import cached_property
import json
from pathlib import Path
import sqlite3
import time

from .band_rank import NativeBandRank
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import integer_field, message_field, varint as encode_varint
from deathnote.protocol.decode import fields, varint


UPDATE_SPOT_FAVORITES_METHOD = '/app.spot.SpotService/UpdateSpotFavorites'
SERVICE_TIMEZONE = timezone(timedelta(hours=9))


def spot_message(value):
    return integer_field(1, value['id']) + integer_field(2, value['got_at'])


def _favorites(payload):
    selected = []
    for number, value in fields(payload):
        if number < 1:
            raise ValueError('Protobuf field zero')
        if number != 1:
            continue
        if isinstance(value, bytes):
            position = 0
            while position < len(value):
                spot_id, position = varint(value, position)
                selected.append(spot_id)
        else:
            selected.append(value)
    if any(not 0 < spot_id <= 0x7fffffffffffffff for spot_id in selected):
        raise ValueError('Favorite spot ID is outside the positive int64 range')
    return list(dict.fromkeys(selected))


class NativeSpot:
    methods = frozenset((UPDATE_SPOT_FAVORITES_METHOD,))

    def __init__(self, login, master_directory, *, progress, clock=time.time):
        self.login = login
        self.master_directory = Path(master_directory)
        self.progress = progress
        self.clock = clock

        def rows(name):
            return json.loads((self.master_directory / (name + '.json')).read_text('utf8'))['_allData']

        self.spots = {row['_id']: row for row in rows('MasterHomeSpot')}
        parameters = {row['_id']: row['_value'] for row in rows('MasterParameter')}
        self.favorite_slots = int(parameters['spot_favorite_slots'])
        self.episode_ids = {(row['_chapterId'], row['_episodeNumber']): row['_id']
                            for row in rows('MasterStoryEpisode')}
        self.starts = {row['_id']: int(datetime.strptime(row['_startAt'], '%Y/%m/%d %H:%M:%S')
                       .replace(tzinfo=SERVICE_TIMEZONE).timestamp())
                       for row in self.spots.values() if row['_startAt']}

    @cached_property
    def band_rank(self):
        return NativeBandRank(self.master_directory)

    def unlock(self, state, *, now):
        owned = state.setdefault('spots', {})
        seen = state.get('story_records', {})
        gained = []
        for spot_id, master in self.spots.items():
            if str(spot_id) in owned or now < self.starts.get(spot_id, 0):
                continue
            if not master['_isInitialOwnership']:
                episode_number, rank = master['_releaseStoryEpisodeId'], master['_releaseBandRank']
                if not episode_number and not rank:
                    continue
                if episode_number:
                    episode_id = self.episode_ids[(master['_releaseStoryChapterId'], episode_number)]
                    if str(episode_id) not in seen:
                        continue
                if rank and self.band_rank.rank(state, master['_bandId']) < rank:
                    continue
            record = {'id': spot_id, 'got_at': now}
            owned[str(spot_id)] = record
            gained.append(record)
        return gained

    def player_fields(self, state, *, now):
        self.unlock(state, now=now)
        favorites = state.get('favorite_spot_ids', ())
        return ((message_field(56, b''.join(encode_varint(spot_id) for spot_id in favorites))
                 if favorites else b'')
                + b''.join(message_field(57, spot_message(state['spots'][key]))
                           for key in sorted(state['spots'], key=int)))

    def dispatch(self, method, payload, headers):
        if method != UPDATE_SPOT_FAVORITES_METHOD:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        try:
            selected = _favorites(payload)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'spot_favorites_wire'}) from None
        if len(selected) > self.favorite_slots:
            raise NativeRpcError(3, diagnostic={'code': 'spot_favorite_slots'})
        if any(spot_id not in self.spots for spot_id in selected):
            raise NativeRpcError(3, diagnostic={'code': 'spot_favorite_master'})
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, player['profile_id'])
            self.unlock(state, now=int(self.clock()))
            if any(str(spot_id) not in state['spots'] for spot_id in selected):
                raise NativeRpcError(9, diagnostic={'code': 'spot_favorite_not_owned'})
            state['favorite_spot_ids'] = selected
            self.progress.save(connection, player['profile_id'], state)
        return b''

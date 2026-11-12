"""1.1.0 Try RPCs in the official unopened state and audited music predicates.

The official snapshot has no TryMusic/Mission/LevelReward/TotalScoreReward
rows. These endpoints authenticate, validate their typed requests, and return
the ordinary unavailable-state status without creating a local activity.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from deathnote.protocol.rpc import NativeRpcError
from .single import int_value, parse_message
from deathnote.protocol.wire import integer_field, message_field

GET_TRY_EVENT_METHOD = '/app.event.EventService/GetTryEvent'
START_TRY_METHOD = '/app.live.LiveService/StartTryLive'
FINISH_TRY_METHOD = '/app.live.LiveService/FinishTryLive'
SKIP_TRY_METHOD = '/app.live.LiveService/SkipTryLive'

TRY_TABLES = ('MasterTryMusic', 'MasterTryMission', 'MasterTryLevelReward',
              'MasterTryMusicTotalScoreReward')


def matches_music_type(condition_type, target_type):
    """TryMissionMusicCondition.MatchesMusicType, 1.1.0 @0x5c23ea8."""
    if condition_type == 0:
        return True
    if condition_type == 99:
        return False
    return target_type == 99 or condition_type == target_type


def matches_music(condition, *, band_ids, music_type, is_try_music, difficulty, level):
    """TryMissionMusicCondition.Matches, 1.1.0 @0x5c23cac.

    Difficulty and level are lower bounds, including the native target-type
    99 wildcard. IsAssistDisallowed is a separate mission/result condition.
    """
    band_id = condition['_bandId']
    if band_id and band_id not in band_ids:
        return False
    if not matches_music_type(condition['_liveMusicType'], music_type):
        return False
    if condition['_isTryMusicOnly'] and not is_try_music:
        return False
    if condition['_useMusicDifficulty'] and difficulty < condition['_musicDifficulty']:
        return False
    return condition['_musicLevel'] == 0 or level >= condition['_musicLevel']


def progress_message(try_type, level):
    return integer_field(1, try_type) + integer_field(2, level)


def try_event_message(event_id, *, progresses=(), music_scores=()):
    result = integer_field(1, event_id)
    result += b''.join(message_field(2, progress_message(try_type, level)) for try_type, level in progresses)
    result += b''.join(message_field(3, integer_field(1, music_id) + integer_field(2, score))
                       for music_id, score in music_scores)
    return result


class NativeTryLive:
    methods = frozenset((GET_TRY_EVENT_METHOD, START_TRY_METHOD, FINISH_TRY_METHOD, SKIP_TRY_METHOD))

    def __init__(self, login, master_directory, *, clock=time.time):
        self.login, self.clock = login, clock
        directory = Path(master_directory)
        paths = {name: directory / (name + '.json') for name in TRY_TABLES}
        self.masters = ({name: json.loads(path.read_text('utf8'))['_allData'] for name, path in paths.items()}
                        if any(path.is_file() for path in paths.values()) else {name: [] for name in TRY_TABLES})
        music_events = {row['_eventId'] for row in self.masters['MasterTryMusic']}
        mission_events = {row['_eventId'] for row in self.masters['MasterTryMission']}
        self.configured_event_ids = music_events & mission_events

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        self.login.authenticate(headers)
        request = parse_message(payload)
        identifier = int_value(request, 1)
        if identifier < 0 or identifier >= 1 << 63:
            raise NativeRpcError(3, diagnostic={'code': 'try_identifier_wire'})
        if method in (START_TRY_METHOD, SKIP_TRY_METHOD):
            difficulty, boost, deck_id = (int_value(request, number) for number in (2, 3, 4))
            if difficulty not in range(1, 5) or boost not in range(11) or deck_id < 0:
                raise NativeRpcError(3, diagnostic={'code': 'try_start_wire'})
            if method == SKIP_TRY_METHOD:
                int_value(request, 5)
        elif method == FINISH_TRY_METHOD:
            live_finish = request.get(2)
            if not isinstance(live_finish, bytes):
                raise NativeRpcError(3, diagnostic={'code': 'try_finish_wire'})
            finish = parse_message(live_finish)
            clear_status, score, combo = (int_value(finish, number) for number in (1, 2, 3))
            unique_id = finish.get(11)
            if (clear_status not in range(1, 6) or score not in range(0x80000000) or combo < 0
                    or not isinstance(unique_id, bytes) or not unique_id):
                raise NativeRpcError(3, diagnostic={'code': 'try_finish_wire'})
            try:
                unique_id.decode('utf8')
            except UnicodeError:
                raise NativeRpcError(3, diagnostic={'code': 'try_finish_wire'}) from None
        raise NativeRpcError(9, diagnostic={'code': 'try_event_unavailable',
            'effectiveTryEvents': len(self.configured_event_ids),
            'masterRows': {name: len(rows) for name, rows in self.masters.items()}})

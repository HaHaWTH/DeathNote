"""Challenge LIVE consumption, transactional result rewards and local rankings."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import random
import sqlite3
import time

from deathnote.game.progression.event import deck_detail
from deathnote.game.players.inventory import member_cards
from .event import active
from .music import record_score
from deathnote.game.progression.login_bonus import posted_notification
from deathnote.protocol.rpc import NativeRpcError
from .single import int_value, parse_message, reward_resource
from deathnote.protocol.wire import integer_field, message_field, varint
from deathnote.protocol.decode import fields

START_CHALLENGE_METHOD = '/app.live.LiveService/StartChallengeLive'
FINISH_CHALLENGE_METHOD = '/app.live.LiveService/FinishChallengeLive'
SKIP_CHALLENGE_METHOD = '/app.live.LiveService/SkipChallengeLive'


class NativeChallengeLive:
    methods = frozenset((START_CHALLENGE_METHOD, FINISH_CHALLENGE_METHOD, SKIP_CHALLENGE_METHOD))

    def __init__(self, login, player_data, master_directory, *, single_live, event,
                 mission=None, season_pass=None, clock=time.time, rng=None):
        self.login, self.player_data, self.clock = login, player_data, clock
        self.single, self.event, self.mission, self.season_pass = single_live, event, mission, season_pass
        self.progress = player_data.progress
        self.rng = random.SystemRandom() if rng is None else rng
        rows = json.loads((Path(master_directory) / 'MasterChallengeMusicBoostBonus.json').read_text('utf8'))['_allData']
        self.rates = {row['_consumedChallengePointCount']: row for row in rows}
        with closing(sqlite3.connect(login.database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS challenge_live_active (
                    profile_id INTEGER PRIMARY KEY REFERENCES players(profile_id),
                    challenge_music_id INTEGER NOT NULL, difficulty INTEGER NOT NULL,
                    consumed_point INTEGER NOT NULL, deck_wire BLOB NOT NULL, started_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS challenge_live_results (
                    profile_id INTEGER NOT NULL REFERENCES players(profile_id),
                    live_unique_id TEXT NOT NULL, challenge_music_id INTEGER NOT NULL,
                    response BLOB NOT NULL, completed_at INTEGER NOT NULL,
                    PRIMARY KEY(profile_id, live_unique_id)
                );
            ''')

    def _selection(self, player, request):
        music_id, difficulty, consumed, deck_id = (int_value(request, number) for number in range(1, 5))
        master = self.event.challenge_music.get(music_id)
        if master is None or consumed not in self.rates:
            raise NativeRpcError(3, diagnostic={'code': 'challenge_selection'})
        event = self.event.events[master['_eventId']]
        if not active(event, int(self.clock())):
            raise NativeRpcError(9, diagnostic={'code': 'challenge_event_closed'})
        period = {'_startAt': master['_stratAt'], '_endAt': master['_endAt']}
        if not active(period, int(self.clock())):
            raise NativeRpcError(9, diagnostic={'code': 'challenge_music_closed'})
        try:
            self.single.rules.select_music(master['_liveMusicId'], difficulty)
        except ValueError:
            raise NativeRpcError(3, diagnostic={'code': 'challenge_difficulty'}) from None
        decks, _ = self.player_data.decks.load(player['profile_id'])
        deck = next((value for value in decks if int_value(parse_message(value), 1) == deck_id), None)
        if deck is None:
            raise NativeRpcError(9, diagnostic={'code': 'challenge_deck_missing'})
        members = self.single.deck_members(deck)
        if set(members) != set(range(5)) or len(set(members.values())) != 5:
            raise NativeRpcError(9, diagnostic={'code': 'challenge_deck_incomplete'})
        return master, difficulty, consumed, deck

    def _balance(self, state, player, master, consumed, deck, now):
        owned = member_cards(state, player['created_at'])
        if any(str(key) not in owned for key in self.single.deck_members(deck).values()):
            raise NativeRpcError(9, diagnostic={'code': 'challenge_deck_incomplete'})
        self.event.live_events.initialize_challenge(state, now=now)
        if state['challenge_lives'][str(master['_eventId'])]['point'] < consumed:
            raise NativeRpcError(9, diagnostic={'code': 'challenge_point_balance'})

    def _milestones(self, state, music_id, difficulty, rank, combo, clear_status):
        rules = self.single.rules
        score_key, combo_key = str(music_id), f'{music_id}:{difficulty}'
        score_claims = state['score_reward_ids'].setdefault(score_key, [])
        combo_claims = state['combo_reward_ids'].setdefault(combo_key, [])
        score_rewards = rules.score_rewards(music_id, rank, score_claims)
        combo_rewards = rules.combo_rewards(music_id, difficulty, combo, combo_claims, full_combo=clear_status >= 4)
        score_claims.extend(row['_id'] for row in score_rewards)
        combo_claims.extend(row['_id'] for row in combo_rewards)
        if score_rewards:
            state['score_reward_gained'][score_key] = max(state['score_reward_gained'].get(score_key, 0),
                max(row['_liveScoreRank'] for row in score_rewards))
        if combo_rewards:
            count = rules.select_music(music_id, difficulty)['score']['_fullComboCount']
            thresholds = (count // 4, count // 2, count * 3 // 4, count)
            state['combo_reward_gained'][combo_key] = max(state['combo_reward_gained'].get(combo_key, 0),
                max(thresholds[row['_comboRateType']] for row in combo_rewards))
        return [(row['_resourceType'], row['_resourceId'], row['_resourceCount']) for row in score_rewards + combo_rewards]

    def _settle(self, connection, player, state, master, difficulty, consumed, deck,
                *, now, rank, finish=None):
        music_id = master['_liveMusicId']
        before_rates = self.single.offline_bonus.capture_rates(state, now=now) if self.single.offline_bonus is not None else None
        detail = deck_detail(deck, state, player['created_at'])
        state['challenge_lives'][str(master['_eventId'])]['point'] -= consumed
        if self.season_pass is not None:
            self.season_pass.record_counter(connection, player['profile_id'], 129, 0, 0, consumed, now)
        rates = self.rates[consumed]
        ordinary = self.single.rules.draw_free_rewards(rank, self.rng)
        resources = [(row['_resourceType'], row['_resourceId'], row['_resourceCount'] * rates['_liveMusicRewardRate'])
                     for row in ordinary]
        event_wire, event_resources, posted = self.event.live_events.settle(state, deck, rank=rank,
            rates=rates, player_rank=self.single.rules.player_rank(state['rank_exp']), now=now,
            rng=self.rng, challenge_event_id=master['_eventId'])
        self.single.rewards.apply(state, resources + event_resources, now=now)
        characters = self.single._apply_exp(state, deck, player, self.single.rules.exp_base(rank), rates)
        event_record = state['events'][str(master['_eventId'])]
        event_record['deck'] = detail.hex()
        if finish is None:
            skip = state.get('live_skip', {'count': 0, 'last_skip': 0})
            offset = 9 * 3600 - self.single.rules.daily_reset_seconds
            count = skip['count'] if (skip['last_skip'] + offset) // 86400 == (now + offset) // 86400 else 0
            entitlements = self.single.rewards.entitlements
            entitlements._settle(state, now)
            bonus = max((entitlements.monthly_passes[value['monthly_pass_id']]['_addLiveSkip']
                         for value in state.get('monthly_passes', {}).values() if value['expire_at'] > now), default=0)
            if count >= self.single.skip_parameters['max_count'] + bonus:
                raise NativeRpcError(9, diagnostic={'code': 'skip_daily_limit'})
            state['live_skip'] = {'count': count + 1, 'last_skip': now}
            notification = posted_notification(posted)
            if self.single.offline_bonus is not None:
                notification += self.single.offline_bonus.stock_changed_rates(connection, player, state, before_rates, now=now)
            return (message_field(1, b''.join(varint(row['_id']) for row in ordinary))
                    + event_wire + message_field(3, notification))
        clear_status, score, combo = finish
        milestones = self._milestones(state, music_id, difficulty, rank, combo, clear_status)
        self.single.rewards.apply(state, milestones, now=now)
        previous_scores = list(state['scores'].values())
        record_score(state, music_id=music_id, difficulty=difficulty, mode=1, score=score,
                     deck=deck, created_at=player['created_at'])
        key = f'{music_id}:{difficulty}:1'
        previous = state['scores'].get(key, {})
        state['scores'][key] = {'music_id': music_id, 'difficulty': difficulty, 'mode': 1,
            'high_score': max(score, previous.get('high_score', 0)),
            'clear_status': max(clear_status, previous.get('clear_status', 0)),
            'max_combo': max(combo, previous.get('max_combo', 0))}
        ranking = state.setdefault('challenge_scores', {})
        previous = ranking.get(str(master['_id']))
        if previous is None or score > previous['score']:
            ranking[str(master['_id'])] = {'score': score, 'deck': detail.hex(), 'difficulty': difficulty}
        rating = self.single.rating.rewards(previous_scores, list(state['scores'].values()))
        self.single.rewards.apply(state, rating['resources'], now=now)
        stamp = state['live_stamp']
        state['live_stamp'] = self.single.rules.update_live_stamp(stamp['live_completed'], stamp['reward_gained'],
                                                               stamp['last_live_completed_at'], now)
        if self.season_pass is not None:
            self.season_pass.record_live(connection, player['profile_id'], music_id=music_id,
                                        cleared=clear_status >= 2, consumed_boost=0, now=now)
        if self.mission is not None:
            self.mission.record_live(connection, player['profile_id'], music_id=music_id,
                                     character_ids=characters, cleared=clear_status >= 2,
                                     difficulty=difficulty, score_rank=rank, mode=4, now=now)
        notification = posted_notification(posted)
        if self.single.offline_bonus is not None:
            notification += self.single.offline_bonus.stock_changed_rates(connection, player, state, before_rates, now=now)
        response = message_field(6, notification)
        response += b''.join(message_field(5, reward_resource(*value)) for value in resources + rating['resources'])
        if rating['total_reward_ids']:
            response += message_field(3, b''.join(varint(value) for value in rating['total_reward_ids']))
        for band_id, reward_ids in rating['band_rewards'].items():
            response += message_field(4, integer_field(1, band_id) + message_field(2, b''.join(varint(value) for value in reward_ids)))
        response += b''.join(message_field(7 if number == 4 else number, value) for number, value in fields(event_wire))
        return response

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        player = self.login.authenticate(headers)
        request = parse_message(payload)
        now, profile_id = int(self.clock()), player['profile_id']
        if method in (START_CHALLENGE_METHOD, SKIP_CHALLENGE_METHOD):
            master, difficulty, consumed, deck = self._selection(player, request)
            with closing(sqlite3.connect(self.login.database)) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                state = self.progress.load(connection, profile_id)
                self._balance(state, player, master, consumed, deck, now)
                if method == START_CHALLENGE_METHOD:
                    connection.execute('''INSERT INTO challenge_live_active VALUES (?,?,?,?,?,?)
                        ON CONFLICT(profile_id) DO UPDATE SET challenge_music_id=excluded.challenge_music_id,
                        difficulty=excluded.difficulty, consumed_point=excluded.consumed_point,
                        deck_wire=excluded.deck_wire, started_at=excluded.started_at''',
                        (profile_id, master['_id'], difficulty, consumed, deck, now))
                    response = b''
                else:
                    response = self._settle(connection, player, state, master, difficulty, consumed, deck,
                                            now=now, rank=self.single.skip_parameters['score_rank'])
                self.progress.save(connection, profile_id, state)
            return response
        event_id = int_value(request, 1)
        live_finish = request.get(2)
        if not isinstance(live_finish, bytes):
            raise NativeRpcError(3, diagnostic={'code': 'challenge_finish_wire'})
        result = parse_message(live_finish)
        clear_status, score, combo = (int_value(result, number) for number in range(1, 4))
        unique_id = result.get(11)
        if not isinstance(unique_id, bytes):
            raise NativeRpcError(3)
        try:
            unique_id = unique_id.decode('utf8')
        except UnicodeError:
            raise NativeRpcError(3) from None
        if not unique_id or clear_status not in range(1, 6) or score not in range(0x80000000) or combo < 0:
            raise NativeRpcError(3)
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            cached = connection.execute('SELECT response FROM challenge_live_results WHERE profile_id=? AND live_unique_id=?',
                                        (profile_id, unique_id)).fetchone()
            if cached is not None:
                return cached[0]
            live = connection.execute('SELECT challenge_music_id,difficulty,consumed_point,deck_wire FROM challenge_live_active WHERE profile_id=?',
                                      (profile_id,)).fetchone()
            if live is None:
                raise NativeRpcError(9, diagnostic={'code': 'challenge_not_started'})
            challenge_id, difficulty, consumed, deck = live
            master = self.event.challenge_music[challenge_id]
            if event_id != master['_eventId']:
                raise NativeRpcError(3, diagnostic={'code': 'challenge_finish_event'})
            note_count = self.single.rules.select_music(master['_liveMusicId'], difficulty)['score']['_fullComboCount']
            if combo > note_count:
                raise NativeRpcError(3)
            state = self.progress.load(connection, profile_id)
            self._balance(state, player, master, consumed, deck, now)
            response = self._settle(connection, player, state, master, difficulty, consumed, deck, now=now,
                rank=self.single.rules.score_rank(master['_liveMusicId'], score), finish=(clear_status, score, combo))
            self.progress.save(connection, profile_id, state)
            connection.execute('INSERT INTO challenge_live_results VALUES (?,?,?,?,?)',
                               (profile_id, unique_id, challenge_id, response, now))
            connection.execute('DELETE FROM challenge_live_active WHERE profile_id=?', (profile_id,))
        return response

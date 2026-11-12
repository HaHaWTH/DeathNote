"""TGW free gacha using the original ExecuteVip request and product counters.

Prize selection reuses NativeGacha's documented local weighted-draw policy.
The client applies returned prizes; Notification carries newly unlocked costumes.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from .gacha import _in_period, _packed, _request
from deathnote.game.players.inventory import member_cards
from deathnote.game.progression.costume import unlocked_costume_notification
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields


EXECUTE_VIP_METHOD = '/app.gacha.GachaService/ExecuteVip'


def _vip_request(payload):
    gacha_id, product_id, selected = _request(payload)
    draw_count = dict(fields(payload)).get(4, 0)
    if type(draw_count) is not int or not 1 <= draw_count <= 10:
        raise NativeRpcError(3)
    return gacha_id, product_id, selected, draw_count


class NativeVipGacha:
    methods = frozenset((EXECUTE_VIP_METHOD,))

    def __init__(self, gacha):
        self.gacha = gacha
        directory = gacha.rewards.master_directory

        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']

        self.ranks = sorted(rows('MasterVip'), key=lambda row: row['_point'])
        self.free_counts = {row['_vipRank']: row['_value'] for row in rows('MasterVipRankBonus')
                            if row['_vipBonusType'] == 8}

    def dispatch(self, method, payload, headers):
        gacha_service = self.gacha
        player = gacha_service.login.authenticate(headers)
        if method != EXECUTE_VIP_METHOD:
            raise NotImplementedError(method)
        gacha_id, product_id, selected, draw_count = _vip_request(payload)
        gacha = gacha_service.gachas.get(gacha_id)
        product = gacha_service.products.get(product_id)
        if (gacha is None or product is None or selected
                or product_id != gacha['_vipRankBonusProductId']):
            raise NativeRpcError(3)
        now = int(gacha_service.clock())
        if not _in_period(gacha, now):
            raise NativeRpcError(9)
        profile_id = player['profile_id']
        with closing(sqlite3.connect(gacha_service.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = gacha_service.progress.load(connection, profile_id)
            costume_ids = set(state.get('character_unlocked_costumes', {}))
            rank = max(row['_vipRank'] for row in self.ranks
                       if row['_point'] <= state.get('vip', {}).get('point', 0))
            consumed = gacha_service._consumed(connection, profile_id, gacha_id, product, now)
            if draw_count > max(0, self.free_counts.get(rank, 0) - consumed):
                raise NativeRpcError(9)
            normal = gacha_service._prize_weights(gacha, product, now, guaranteed=False)
            prize_ids = [gacha_service._choose(normal) for _ in range(draw_count)]
            seen = {card['master_id'] for card in member_cards(state, player['created_at']).values()}
            previous_rates = (gacha_service.offline_bonus.capture_rates(state, now=now)
                              if gacha_service.offline_bonus is not None else None)
            resources, history = [], []
            for prize_id in prize_ids:
                prize = gacha_service.prizes[prize_id]
                master_id = prize['_resourceId']
                converted = prize['_resourceType'] == 2 and master_id in seen
                if prize['_resourceType'] == 2:
                    seen.add(master_id)
                history.append({'prize_id': prize_id, 'converted': converted})
                resources.append((prize['_resourceType'], master_id, prize['_amount']))
            gacha_service.rewards.apply(state, resources, now=now)
            offline_notification = (gacha_service.offline_bonus.stock_changed_rates(connection, player, state, previous_rates, now=now)
                                    if gacha_service.offline_bonus is not None else b'')
            gacha_service.progress.save(connection, profile_id, state)
            connection.execute('''INSERT INTO player_gacha_products
                (profile_id, gacha_id, product_id, consumed_count, last_execute)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(profile_id, gacha_id, product_id)
                DO UPDATE SET consumed_count = excluded.consumed_count,
                              last_execute = excluded.last_execute''',
                (profile_id, gacha_id, product_id, consumed + draw_count, now))
            connection.execute('''INSERT INTO player_gacha_points(profile_id, gacha_id, point)
                VALUES (?, ?, ?) ON CONFLICT(profile_id, gacha_id)
                DO UPDATE SET point = point + excluded.point''',
                (profile_id, gacha_id, product['_gachaPoint'] * draw_count))
            connection.execute('''INSERT INTO player_gacha_history
                (profile_id, gacha_id, product_id, free_gem, paid_gem, ticket,
                 executed_at, prizes, bonus_lot_ids) VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?)''',
                (profile_id, gacha_id, product_id, now,
                 json.dumps(history, separators=(',', ':')), '[]'))
            if gacha_service.season_pass is not None:
                gacha_service.season_pass.record_counter(connection, profile_id, 3, 0, 0, draw_count, now)
        return (_packed(1, prize_ids)
                + message_field(3, unlocked_costume_notification(costume_ids, state) + offline_notification))

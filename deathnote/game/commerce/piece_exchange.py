"""Exchange only surplus member pieces and duplicate support cards.

The original StorageBoxDialogModel retains enough materials to reach max rank.
After a successful RPC it applies its own item/duplicate deltas; the response
therefore contains the present, empty Notification rather than a second award.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from deathnote.game.players.inventory import member_cards
from deathnote.game.players.progress import NativeLiveProgress
from deathnote.game.progression.rewards import NativeRewards
from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.wire import message_field
from deathnote.protocol.decode import fields


EXCHANGE_METHOD = '/app.pieceexchange.PieceExchangeService/Exchange'


def _selections(payload):
    selected = {1: Counter(), 2: Counter()}
    try:
        for number, data in fields(payload):
            if number == 0:
                raise ValueError('Invalid protobuf tag')
            if number not in selected:
                continue
            if type(data) is not bytes:
                raise ValueError('Exchange selections require messages')
            values = dict(fields(data))
            resource_id, count = values.get(1, 0), values.get(2, 0)
            if (0 in values or type(resource_id) is not int
                    or not 0 < resource_id <= 0x7fffffffffffffff
                    or type(count) is not int or not 0 < count <= 0x7fffffff):
                raise ValueError('Invalid exchange id or count')
            selected[number][resource_id] += count
        if not selected[1] and not selected[2]:
            raise ValueError('Empty exchange selection')
    except (TypeError, ValueError):
        raise NativeRpcError(3) from None
    return selected[1], selected[2]


class NativePieceExchange:
    methods = frozenset((EXCHANGE_METHOD,))

    def __init__(self, login, master_directory, *, progress=None, rewards=None, clock=time.time):
        self.login = login
        self.database = login.database
        self.clock = clock
        self.progress = NativeLiveProgress(login) if progress is None else progress
        self.rewards = NativeRewards(master_directory) if rewards is None else rewards
        directory = Path(master_directory)

        def rows(name):
            return json.loads((directory / (name + '.json')).read_text('utf8'))['_allData']

        self.member_masters = self.rewards.member_masters
        self.support_masters = self.rewards.support_masters
        self.piece_members = {row['_rankUpItemID']: row for row in self.member_masters.values()}
        self.piece_rules = {row['_cardRarity']: row for row in rows('MasterPieceExchange')
                            if row['_gameResourceType'] == 1 and row['_itemType'] == 9}
        self.support_rules = {row['_cardRarity']: row for row in rows('MasterPieceExchange')
                              if row['_gameResourceType'] == 3}
        self.member_ranks = defaultdict(dict)
        for row in rows('MasterMemberCardRank'):
            self.member_ranks[row['_group']][row['_rank']] = row['_requiredRankUpItemCount']
        self.support_max_rank = defaultdict(int)
        for row in rows('MasterSupportCardRank'):
            group = row['_group']
            self.support_max_rank[group] = max(self.support_max_rank[group], row['_rank'])

    def dispatch(self, method, payload, headers):
        player = self.login.authenticate(headers)
        if method != EXCHANGE_METHOD:
            raise NotImplementedError(method)
        pieces, supports = _selections(payload)
        if any(identifier not in self.piece_members for identifier in pieces):
            raise NativeRpcError(3)
        if any(identifier not in self.support_masters for identifier in supports):
            raise NativeRpcError(3)
        profile_id = player['profile_id']
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            state = self.progress.load(connection, profile_id)
            owned_members = {card['master_id']: card
                             for card in member_cards(state, player['created_at']).values()}
            awards = Counter()
            for identifier, count in pieces.items():
                master = self.piece_members[identifier]
                card = owned_members.get(master['_id'])
                if card is None:
                    raise NativeRpcError(9)
                ranks = self.member_ranks[master['_memberCardRankGroup']]
                reserve = sum(ranks[rank] for rank in range(card['card_rank'] + 1, max(ranks) + 1))
                available = max(0, state['items'].get(str(identifier), 0) - reserve)
                if count > available:
                    raise NativeRpcError(9)
                rule = self.piece_rules[master['_rarity']]
                awards[rule['_itemId']] += rule['_exchangeCount'] * count
            for identifier, count in supports.items():
                master = self.support_masters[identifier]
                card = state.get('support_cards', {}).get(str(identifier))
                if card is None:
                    raise NativeRpcError(9)
                reserve = self.support_max_rank[master['_supportCardRankGroup']] - card['card_rank']
                available = max(0, card['duplicate_count'] - reserve)
                if count > available:
                    raise NativeRpcError(9)
                rule = self.support_rules[master['_rarity']]
                awards[rule['_itemId']] += rule['_exchangeCount'] * count
            for identifier, count in pieces.items():
                state['items'][str(identifier)] -= count
            for identifier, count in supports.items():
                state['support_cards'][str(identifier)]['duplicate_count'] -= count
            self.rewards.apply(state, ((1, identifier, count) for identifier, count in awards.items()),
                               now=self.clock())
            self.progress.save(connection, profile_id, state)
        return message_field(1, b'')

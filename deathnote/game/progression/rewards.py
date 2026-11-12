"""Apply recovered resource awards inside the caller's player transaction."""
from __future__ import annotations

from functools import cached_property
import json
from pathlib import Path
import time

from deathnote.game.players.inventory import member_cards, new_member_card


class NativeRewards:
    def __init__(self, master_directory):
        self.master_directory = Path(master_directory)

    @cached_property
    def item_masters(self):
        data = json.loads((self.master_directory / 'MasterItem.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def support_masters(self):
        data = json.loads((self.master_directory / 'MasterSupportCard.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def member_masters(self):
        data = json.loads((self.master_directory / 'MasterMemberCard.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def piece_exchanges(self):
        data = json.loads((self.master_directory / 'MasterPieceExchange.json').read_text('utf8'))
        return {row['_cardRarity']: row for row in data['_allData'] if row['_gameResourceType'] == 2}

    @cached_property
    def music_masters(self):
        data = json.loads((self.master_directory / 'MasterLiveMusic.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def degree_masters(self):
        data = json.loads((self.master_directory / 'MasterDegree.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def background_masters(self):
        data = json.loads((self.master_directory / 'MasterBackground.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def home_spot_masters(self):
        data = json.loads((self.master_directory / 'MasterHomeSpot.json').read_text('utf8'))
        return {row['_id']: row for row in data['_allData']}

    @cached_property
    def entitlements(self):
        from .entitlements import NativeEntitlements
        return NativeEntitlements(self.master_directory)

    def apply(self, state, resources, *, now=None):
        acquired_at = int(time.time()) if now is None else int(now)
        for resource_type, resource_id, count in resources:
            key = str(resource_id)
            if resource_type == 1:
                state['items'][key] = min(self.item_masters[resource_id]['_max'],
                                          state['items'].get(key, 0) + count)
            elif resource_type == 2:
                master = self.member_masters[resource_id]
                cards = member_cards(state)
                already_owned = any(card['master_id'] == resource_id for card in cards.values())
                duplicates = count
                if not already_owned:
                    cards[key] = new_member_card(resource_id, gained_at=acquired_at)
                    duplicates -= 1
                self.entitlements.unlock_member_costumes(state, resource_id, now=acquired_at)
                if duplicates:
                    exchange = self.piece_exchanges[master['_rarity']]
                    self.apply(state, ((1, master['_rankUpItemID'],
                                        exchange['_exchangeCount'] * duplicates),), now=acquired_at)
            elif resource_type == 3:
                self.support_masters[resource_id]
                cards = state.setdefault('support_cards', {})
                if key in cards:
                    cards[key]['duplicate_count'] += count
                else:
                    cards[key] = {'id': resource_id, 'master_id': resource_id,
                                  'exp': 0, 'card_rank': 1, 'gain_at': acquired_at,
                                  'duplicate_count': count - 1}
            elif resource_type == 8:
                self.music_masters[resource_id]
                state.setdefault('music_unlocked', {})[key] = {
                    'music_id': resource_id, 'got_at': acquired_at}
            elif resource_type == 17:
                self.degree_masters[resource_id]
                degrees = state.setdefault('degrees', {})
                degree = degrees.setdefault(key, {'master_id': resource_id,
                                                  'amount': 0, 'acquired_at': acquired_at})
                degree['amount'] += count
            elif resource_type == 18:
                self.background_masters[resource_id]
                backgrounds = state.setdefault('background_ids', [])
                if resource_id not in backgrounds:
                    backgrounds.append(resource_id)
                    backgrounds.sort()
            elif resource_type == 19:
                self.home_spot_masters[resource_id]
                state.setdefault('spots', {}).setdefault(key, {'id': resource_id, 'got_at': acquired_at})
            elif resource_type in (6, 9, 10, 1001, 1002, 1003):
                followups = self.entitlements.apply(state, ((resource_type, resource_id, count),), now=acquired_at)
                self.apply(state, followups, now=acquired_at)
            else:
                raise NotImplementedError('Resource award type: ' + str(resource_type))

"""Recovered authenticated empty maintenance gates for available local services."""
from __future__ import annotations

from deathnote.protocol.rpc import NativeRpcError
from deathnote.protocol.decode import fields


SHOP_MAINTENANCE_METHOD = '/app.shop.ShopService/CheckMaintenanceShop'
CASUAL_MATCH_MAINTENANCE_METHOD = '/app.live.LiveService/CheckMaintenanceCasualMatch'
RANK_MATCH_MAINTENANCE_METHOD = '/app.live.LiveService/CheckMaintenanceRankMatch'
GACHA_MAINTENANCE_METHOD = '/app.gacha.GachaService/CheckMaintenanceGacha'
STORY_MAINTENANCE_METHOD = '/app.story.StoryService/CheckMaintenanceStory'


class NativeMaintenance:
    methods = frozenset((SHOP_MAINTENANCE_METHOD, CASUAL_MATCH_MAINTENANCE_METHOD,
                         RANK_MATCH_MAINTENANCE_METHOD,
                         GACHA_MAINTENANCE_METHOD, STORY_MAINTENANCE_METHOD))

    def __init__(self, login):
        self.login = login

    def dispatch(self, method, payload, headers):
        if method not in self.methods:
            raise NotImplementedError(method)
        self.login.authenticate(headers)
        try:
            if any(number < 1 for number, _ in fields(payload)):
                raise ValueError('Protobuf field zero')
        except ValueError:
            raise NativeRpcError(3) from None
        return b''

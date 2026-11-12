"""Initial local player data for the recovered APK 1.0.6 loading path."""
from __future__ import annotations
from contextlib import closing
import sqlite3
import time

from deathnote.protocol.wire import integer_field, message_field, string_field
from .decks import NativePlayerDecks
from .actions import NativePlayerActions
from .inventory import member_cards, member_card_message
from deathnote.game.social.stamp import stamp_favorite_name_fields
from deathnote.game.live.event import event_player_fields
from deathnote.game.progression.login_bonus import player_fields as login_bonus_fields, posted_notification
from deathnote.game.commerce.vip import vip_message
from .settings import user_setting_fields, favorite_member_card
from deathnote.game.live.music import _member_detail
from .progress import (NativeLiveProgress, gem_message, boost_message,
                                   stamp_message, skip_message, player_progress_fields)


PLAYER_DATA_METHOD = '/app.player.PlayerService/GetPlayerData'


class NativePlayerData:
    def __init__(self, login, *, season_pass=None, gacha=None, profile_card=None,
                 entitlements=None, exchange=None, mission=None, login_bonus=None,
                 band_item=None, vip=None, spot=None, music_ids=None, clock=time.time):
        self.login = login
        self.season_pass = season_pass
        self.gacha = gacha
        self.profile_card = profile_card
        self.entitlements = entitlements
        self.exchange = exchange
        self.mission = mission
        self.login_bonus = login_bonus
        self.band_item = band_item
        self.vip = vip
        self.spot = spot
        self.music_ids = music_ids
        self.friend = None
        self.event = None
        self.common_social = None
        self.clock = clock
        self.decks = NativePlayerDecks(login)
        self.progress = NativeLiveProgress(login)
        self.actions = NativePlayerActions(login)

    def get(self, headers: dict[str, str]) -> bytes:
        player = self.login.authenticate(headers)
        profile_id = player['profile_id']
        saved_profile = self.actions.get_profile(profile_id, '本地玩家' + str(profile_id))
        mission_counters = b''
        received, posted = [], []
        with closing(sqlite3.connect(self.login.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            progress = self.progress.load(connection, profile_id)
            if self.vip is not None:
                self.vip.refresh_login(progress, now=int(self.clock()))
            if self.login_bonus is not None:
                received, _, posted = self.login_bonus.update(progress, now=int(self.clock()))
            owned = member_cards(progress, player['created_at'])
            if self.season_pass is not None:
                mission_counters = self.season_pass.mission_counters(connection, profile_id)
            if self.mission is not None:
                mission_counters += self.mission.player_fields(connection, profile_id, state=progress)
            gacha_fields = self.gacha.player_fields(profile_id, connection=connection) if self.gacha is not None else b''
            profile_fields, simple_card = (self.profile_card.fields(connection, profile_id)
                                           if self.profile_card is not None else (b'', None))
            entitlement_fields = (self.entitlements.player_fields(progress, now=int(self.clock()))
                                  if self.entitlements is not None else b'')
            exchange_fields = self.exchange.player_fields(connection, profile_id) if self.exchange is not None else b''
            band_item_fields = self.band_item.player_fields(progress) if self.band_item is not None else b''
            spot_fields = self.spot.player_fields(progress, now=int(self.clock())) if self.spot is not None else b''
            friend_fields = self.friend.friend_data(connection, profile_id) if self.friend is not None else None
            extra_event_fields = self.event.player_fields(progress, now=int(self.clock())) if self.event is not None else b''
            invitation_fields = (self.common_social.invitation_data(connection, profile_id)
                                 if self.common_social is not None else b'')
            self.progress.save(connection, profile_id, progress)
        cards = [message_field(2, member_card_message(owned[key], progress))
                 for key in sorted(owned, key=int)]
        decks, main_deck = self.decks.load(profile_id)
        favorite = favorite_member_card(owned, progress)
        profile = (
            string_field(1, player['player_id'])
            + string_field(2, saved_profile['name'])
            + (integer_field(3, progress['rank_exp']) if progress['rank_exp'] else b'')
            + integer_field(4, max(saved_profile['updated_at'] or player['created_at'],
                                   progress.get('profile_updated_at', 0)))
            + message_field(5, _member_detail(favorite, progress))
            + integer_field(6, profile_id)
            + integer_field(7, favorite['master_id'])
            + (message_field(8, simple_card) if simple_card is not None else b'')
        )
        data = (
            b''.join(cards)
            + player_progress_fields(progress, music_ids=self.music_ids)
            + stamp_favorite_name_fields(progress)
            + event_player_fields(progress)
            + extra_event_fields
            + login_bonus_fields(received)
            + gacha_fields
            + profile_fields
            + entitlement_fields
            + exchange_fields
            + band_item_fields
            + spot_fields
            + user_setting_fields(progress)
            + b''.join(message_field(4, deck) for deck in decks)
            + integer_field(5, main_deck)
            + message_field(6, gem_message(progress))
            + message_field(7, boost_message(progress))
            + message_field(18, skip_message(progress))
            + message_field(24, mission_counters)
            + self.actions.shown_fields(profile_id)
            + message_field(34, profile)
            + message_field(35, stamp_message(progress))
            + message_field(50, vip_message(progress))
            + message_field(53, integer_field(1, player['created_at']))
        )
        return (
            message_field(1, data)
            + (message_field(2, posted_notification(posted)) if posted else b'')
            + message_field(3, b'')
            + (message_field(4, friend_fields) if friend_fields is not None else b'')
            + message_field(5, invitation_fields)
            + integer_field(6, profile_id)
        )

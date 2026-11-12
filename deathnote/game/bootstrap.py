"""Startup and local login routing for the original game client."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
from deathnote.protocol.wire import message_field as _message_field, string_field as _string_field
from deathnote.game.players.data import NativePlayerData, PLAYER_DATA_METHOD
from deathnote.game.players.decks import SAVE_DECKS_METHOD
from deathnote.game.players.progress import gem_message, boost_message
from deathnote.game.live.single import NativeSingleLive
from deathnote.game.live.battle import NativeBattleLive
from deathnote.game.live.boost import NativeLiveBoost
from .maintenance import NativeMaintenance
from deathnote.game.progression.season_pass import NativeSeasonPass
from deathnote.game.progression.offline_bonus import NativeOfflineBonus
from deathnote.game.progression.member_card import NativeMemberCard
from deathnote.game.social.queries import NativeSocialQueries
from deathnote.game.progression.story import NativeStory
from deathnote.game.commerce.gacha import NativeGacha
from deathnote.game.progression.support_card import NativeSupportCard
from deathnote.game.social.profile_card import NativeProfileCard
from deathnote.game.commerce.shop import NativeShop
from deathnote.game.commerce.exchange import NativeExchange
from deathnote.game.progression.mission import NativeMission
from deathnote.game.progression.login_bonus import NativeLoginBonus, sheet_message, posted_notification
from deathnote.game.progression.present import NativePresent
from deathnote.game.social.announcement import NativeAnnouncement
from deathnote.game.live.music import NativeLiveMusic
from deathnote.game.social.stamp import NativeStamp
from deathnote.game.progression.band_item import NativeBandItem
from deathnote.game.commerce.piece_exchange import NativePieceExchange
from deathnote.game.commerce.vip import NativeVip
from deathnote.game.commerce.vip_gacha import NativeVipGacha
from deathnote.game.progression.costume import NativeCostume
from deathnote.game.progression.spot import NativeSpot
from deathnote.game.social.friend import NativeFriend
from deathnote.game.players.settings import NativeUserSettings
from deathnote.game.players.gem_history import rpc_context
from deathnote.game.commerce.gacha_convert import NativeGachaConvert
from deathnote.game.progression.event import NativeEvent
from deathnote.game.live.challenge import NativeChallengeLive
from deathnote.game.social.services import NativeCommonSocial
from deathnote.game.players.session import NativePlayerSession
from deathnote.game.players.client_reports import NativeClientReports
from deathnote.game.commerce.payment_reservation import NativePaymentReservation
from deathnote.game.progression.event_visit import NativeEventVisit
from deathnote.game.live.try_live import NativeTryLive
from deathnote.game.live.arena import NativeArena
from deathnote.game.social.chat_settings import NativeChatSettings
from deathnote.game.social.chat_players import NativeChatPlayers
from deathnote.game.social.chat_messages import NativeChatMessages

BOOTSTRAP_HOST = '[CONFIGURE THIS ON YOUR OWN]' # TODO
SERVER_LIST_METHOD = '/app.playerlogin.PlayerLoginService/GetServerList'
VERSION_METHOD = '/app.masterdata.MasterdataService/Version'
FEATURE_SWITCH_METHOD = '/app.feature_switch.FeatureSwitchService/Get'
EXTERNAL_PAYMENTS_NOP_METHOD = '/app.external_payments.ExternalPaymentsService/Nop'
PRESENT_FETCH_METHOD = '/app.present.PresentService/Fetch'
PLAYER_SYNC_METHOD = '/app.playerext.PlayerExtService/SyncData'
HOME_GET_METHOD = '/app.home.HomeService/Get'
SEASON_PASS_FETCH_METHOD = '/app.seasonpass.SeasonPassService/Fetch'
ANNOUNCEMENT_LIST_METHOD = '/app.announcement.AnnouncementService/GetList'
SHOP_FETCH_METHOD = '/app.shop.ShopService/Fetch'


class NativeBootstrap:
    def __init__(self, version, *, login, api_port=443, api_root=None, live_master_directory=None,
                 server_display_name=None, cdn_root=None, clock=time.time,
                 battle_transport=None, battle_server_address=None, announcement_directory=None,
                 announcement_image_base_url=None, profile_card_image_base_url=None,
                 arena_point_adjust=None, arena_reset_scale=10000, chat_hub=None):
        self.version = version
        self.login = login
        self.clock = clock
        api_host = urlsplit(version['apiRoot']).hostname
        if api_root is None:
            api_root = 'https://' + api_host + (':' + str(api_port) if api_port != 443 else '')
        master_directory = (Path(live_master_directory) if live_master_directory is not None else
                            login.database.parent / 'public/masterdata' / version['masterVersion'])
        self.season_pass = NativeSeasonPass(login, master_directory, clock=clock)
        self.gacha = NativeGacha(login, master_directory, season_pass=self.season_pass, clock=clock)
        self.chat_settings = NativeChatSettings(login, master_directory,
                                               entitlements=self.gacha.rewards.entitlements)
        self.chat_messages = NativeChatMessages(login, hub=chat_hub, clock=clock)
        self.vip_gacha = NativeVipGacha(self.gacha)
        self.present = NativePresent(login, master_directory, rewards=self.gacha.rewards, clock=clock)
        self.login_bonus = NativeLoginBonus(login, master_directory, presents=self.present, clock=clock)
        self.mission = NativeMission(login, master_directory, rewards=self.gacha.rewards,
                                     season_pass=self.season_pass, clock=clock)
        self.band_item = NativeBandItem(login, master_directory, progress=self.mission.progress, clock=clock)
        self.vip = NativeVip(login, master_directory, progress=self.mission.progress,
                             rewards=self.gacha.rewards, clock=clock)
        self.spot = NativeSpot(login, master_directory, progress=self.mission.progress, clock=clock)
        self.user_settings = NativeUserSettings(login, master_directory,
                                                progress=self.mission.progress, clock=clock)
        self.exchange = NativeExchange(login, master_directory, clock=clock, rewards=self.gacha.rewards)
        self.profile_card = NativeProfileCard(login, master_directory,
            thumbnail_base_url=api_root if profile_card_image_base_url is None else profile_card_image_base_url,
            clock=clock)
        self.announcement = NativeAnnouncement(
            announcement_directory if announcement_directory is not None else login.database.parent / 'announcements',
            image_base_url=api_root if announcement_image_base_url is None else announcement_image_base_url, clock=clock)
        self.player_data = NativePlayerData(login, season_pass=self.season_pass, gacha=self.gacha,
                                            profile_card=self.profile_card,
                                            entitlements=self.gacha.rewards.entitlements,
                                            exchange=self.exchange, mission=self.mission,
                                            login_bonus=self.login_bonus, band_item=self.band_item,
                                            vip=self.vip, spot=self.spot,
                                            music_ids=self.mission.music_masters, clock=clock)
        self.chat_players = NativeChatPlayers(login, self.player_data, master_directory,
                                              chat_settings=self.chat_settings)
        self.single_live = NativeSingleLive(login, self.player_data, master_directory,
                                            season_pass=self.season_pass, mission=self.mission, clock=clock)
        self.battle_live = NativeBattleLive(login, self.player_data, master_directory,
                                            season_pass=self.season_pass, mission=self.mission, clock=clock,
                                            room_lookup=battle_transport.room_snapshot if battle_transport else None,
                                            room_reserve=battle_transport.reserve_room if battle_transport else None,
                                            room_complete=battle_transport.complete_room if battle_transport else None,
                                            room_release=battle_transport.release_room if battle_transport else None)
        self.live_music = NativeLiveMusic(login, self.player_data, master_directory, profile_card=self.profile_card)
        self.event = NativeEvent(login, self.player_data, master_directory,
                                 profile_builder=self.live_music._profile, clock=clock)
        self.event_visit = NativeEventVisit(self.event, master_directory, clock=clock)
        self.try_live = NativeTryLive(login, master_directory, clock=clock)
        self.player_data.event = self.event
        self.challenge_live = NativeChallengeLive(login, self.player_data, master_directory,
            single_live=self.single_live, event=self.event, mission=self.mission,
            season_pass=self.season_pass, clock=clock)
        self.common_social = NativeCommonSocial(login, master_directory,
            progress=self.player_data.progress, rewards=self.gacha.rewards,
            profile_builder=self.live_music._profile,
            room_lookup=battle_transport.room_snapshot if battle_transport else None,
            clock=clock)
        self.player_data.common_social = self.common_social
        self.friend = NativeFriend(login, self.player_data,
                                   profile_builder=self.live_music._profile, clock=clock)
        self.player_data.friend = self.friend
        self.stamp = NativeStamp(login, self.player_data.progress, self.gacha.rewards.entitlements)
        self.costume = NativeCostume(login, self.gacha.rewards.entitlements,
                                     self.player_data.progress)
        self.shop = NativeShop(login, master_directory, progress=self.player_data.progress,
                                rewards=self.single_live.rewards, clock=clock)
        self.payment_reservation = NativePaymentReservation(login, shop=self.shop, clock=clock)
        self.client_reports = NativeClientReports(login, clock=clock)
        self.player_session = NativePlayerSession(login, progress=self.player_data.progress,
            login_bonus=self.login_bonus, vip=self.vip, clock=clock)
        self.piece_exchange = NativePieceExchange(login, master_directory, progress=self.player_data.progress,
                                                  rewards=self.gacha.rewards, clock=clock)
        self.live_boost = NativeLiveBoost(login, master_directory, clock=clock)
        self.maintenance = NativeMaintenance(login)
        self.offline_bonus = NativeOfflineBonus(login, master_directory,
                                                progress=self.player_data.progress,
                                                rewards=self.single_live.rewards, clock=clock)
        self.single_live.offline_bonus = self.offline_bonus
        self.battle_live.single.offline_bonus = self.offline_bonus
        self.gacha.offline_bonus = self.offline_bonus
        self.vip.offline_bonus = self.offline_bonus
        self.band_item.offline_bonus = self.offline_bonus
        self.member_card = NativeMemberCard(login, master_directory, clock=clock,
                                            offline_bonus=self.offline_bonus)
        self.support_card = NativeSupportCard(login, master_directory, clock=clock,
                                              offline_bonus=self.offline_bonus)
        self.gacha_convert = NativeGachaConvert(login, master_directory,
            progress=self.player_data.progress, rewards=self.gacha.rewards, clock=clock)
        self.social_queries = NativeSocialQueries(login, master_directory)
        self.story = NativeStory(login, master_directory, progress=self.player_data.progress,
                                 rewards=self.single_live.rewards, spot=self.spot, clock=clock)
        self.arena = NativeArena(login, self.player_data, master_directory,
            season_pass=self.season_pass, mission=self.mission, clock=clock,
            profile_builder=self.live_music._profile,
            room_lookup=battle_transport.room_snapshot if battle_transport else None,
            room_complete=battle_transport.complete_room if battle_transport else None,
            room_release=battle_transport.release_room if battle_transport else None,
            point_adjust=arena_point_adjust, reset_scale=arena_reset_scale)
        self.arena.single.offline_bonus = self.offline_bonus
        display_name = version['region'] if server_display_name is None else server_display_name
        server = (
            display_name, version['cdnRoot'] if cdn_root is None else cdn_root, api_root,
            'wss://' + api_host + '/ws', api_root,
            battle_server_address or 'ssl:' + api_host + ':9001', display_name, '2', 'taiwan_game_rating',
        )
        self.server_list = _message_field(1, b''.join(
            _string_field(number, value) for number, value in enumerate(server, 1)))
        self.version_message = (_string_field(1, version['masterVersion']) +
                                _string_field(2, version['resourceVersion']))

    def dispatch(self, method, payload, headers):
        with rpc_context(method, payload, now=self.clock()):
            return self._dispatch(method, payload, headers)

    def _dispatch(self, method, payload, headers):
        if method == SERVER_LIST_METHOD:
            response = self.server_list
        elif method == VERSION_METHOD:
            response = self.version_message
        elif method in (FEATURE_SWITCH_METHOD, EXTERNAL_PAYMENTS_NOP_METHOD):
            response = b''
        elif method in self.announcement.methods:
            response = self.announcement.dispatch(method, payload, headers)
        elif method in self.login.methods:
            response = self.login.dispatch(method, payload)
        elif method in self.player_session.methods:
            response = self.player_session.dispatch(method, payload, headers)
        elif method in self.client_reports.methods:
            response = self.client_reports.dispatch(method, payload, headers)
        elif method in self.payment_reservation.methods:
            response = self.payment_reservation.dispatch(method, payload, headers)
        elif method == PLAYER_DATA_METHOD:
            response = self.player_data.get(headers)
        elif method in self.chat_settings.methods:
            response = self.chat_settings.dispatch(method, payload, headers)
        elif method in self.chat_players.methods:
            response = self.chat_players.dispatch(method, payload, headers)
        elif method in self.chat_messages.methods:
            response = self.chat_messages.dispatch(method, payload, headers)
        elif method == HOME_GET_METHOD:
            player = self.login.authenticate(headers)
            with closing(sqlite3.connect(self.login.database)) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                progress = self.player_data.progress.load(connection, player['profile_id'])
                now = int(self.clock())
                response, rewards = self.gacha.rewards.entitlements.home(progress, now=now)
                self.gacha.rewards.apply(progress, rewards, now=now)
                received, _, posted = self.login_bonus.update(progress, now=now)
                response += b''.join(_message_field(2, sheet_message(value)) for value in received)
                if posted:
                    response += _message_field(3, posted_notification(posted))
                response += self.announcement.home()
                response += _message_field(4, self.friend.friend_data(connection, player['profile_id']))
                response += self.common_social.home_fields(connection, player['profile_id'])
                self.player_data.progress.save(connection, player['profile_id'], progress)
        elif method in self.present.methods:
            response = self.present.dispatch(method, payload, headers)
        elif method == SAVE_DECKS_METHOD:
            response = self.player_data.decks.save(payload, headers)
        elif method in self.player_data.actions.methods:
            response = self.player_data.actions.dispatch(method, payload, headers)
        elif method == PLAYER_SYNC_METHOD:
            player = self.login.authenticate(headers)
            progress = self.player_data.progress.get(player['profile_id'])
            response = _message_field(3, gem_message(progress)) + _message_field(4, boost_message(progress))
        elif method in self.single_live.methods:
            response = self.single_live.dispatch(method, payload, headers)
        elif method in self.battle_live.methods:
            response = self.battle_live.dispatch(method, payload, headers)
        elif method in self.arena.methods:
            response = self.arena.dispatch(method, payload, headers)
        elif method in self.live_music.methods:
            response = self.live_music.dispatch(method, payload, headers)
        elif method in self.stamp.methods:
            response = self.stamp.dispatch(method, payload, headers)
        elif method in self.costume.methods:
            response = self.costume.dispatch(method, payload, headers)
        elif method in self.spot.methods:
            response = self.spot.dispatch(method, payload, headers)
        elif method in self.user_settings.methods:
            response = self.user_settings.dispatch(method, payload, headers)
        elif method in self.event.methods:
            response = self.event.dispatch(method, payload, headers)
        elif method in self.event_visit.methods:
            response = self.event_visit.dispatch(method, payload, headers)
        elif method in self.try_live.methods:
            response = self.try_live.dispatch(method, payload, headers)
        elif method in self.challenge_live.methods:
            response = self.challenge_live.dispatch(method, payload, headers)
        elif method in self.common_social.methods:
            response = self.common_social.dispatch(method, payload, headers)
        elif method in self.gacha_convert.methods:
            response = self.gacha_convert.dispatch(method, payload, headers)
        elif method in self.friend.methods:
            response = self.friend.dispatch(method, payload, headers)
        elif method in self.band_item.methods:
            response = self.band_item.dispatch(method, payload, headers)
        elif method in self.season_pass.methods:
            response = self.season_pass.dispatch(method, payload, headers)
        elif method in self.live_boost.methods:
            response = self.live_boost.dispatch(method, payload, headers)
        elif method in self.maintenance.methods:
            response = self.maintenance.dispatch(method, payload, headers)
        elif method in self.offline_bonus.methods:
            response = self.offline_bonus.dispatch(method, payload, headers)
        elif method in self.member_card.methods:
            response = self.member_card.dispatch(method, payload, headers)
        elif method in self.support_card.methods:
            response = self.support_card.dispatch(method, payload, headers)
        elif method in self.social_queries.methods:
            response = self.social_queries.dispatch(method, payload, headers)
        elif method in self.story.methods:
            response = self.story.dispatch(method, payload, headers)
        elif method in self.gacha.methods:
            response = self.gacha.dispatch(method, payload, headers)
        elif method in self.vip_gacha.methods:
            response = self.vip_gacha.dispatch(method, payload, headers)
        elif method in self.profile_card.methods:
            response = self.profile_card.dispatch(method, payload, headers)
        elif method in self.shop.methods:
            response = self.shop.dispatch(method, payload, headers)
        elif method in self.vip.methods:
            response = self.vip.dispatch(method, payload, headers)
        elif method in self.exchange.methods:
            response = self.exchange.dispatch(method, payload, headers)
        elif method in self.piece_exchange.methods:
            response = self.piece_exchange.dispatch(method, payload, headers)
        elif method in self.mission.methods:
            response = self.mission.dispatch(method, payload, headers)
        elif method in self.login_bonus.methods:
            response = self.login_bonus.dispatch(method, payload, headers)
        else:
            raise NotImplementedError(method)
        return response, {'x-asset-version': 'unknown',
                          'x-server-time': datetime.now(timezone.utc).isoformat()}

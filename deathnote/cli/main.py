"""DeathNote server and local administration."""
from __future__ import annotations

import argparse
import asyncio
from importlib import metadata
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from deathnote.data.version import read_version
from deathnote.paths import ROOT, STATE, VERSION
from deathnote.server import run


def local_status(root=ROOT, version_file=None):
    from deathnote.net.image_service import announcement_images_status
    root = Path(root)
    state = root / '.native-server'
    version_file = Path(version_file) if version_file is not None else state / 'public/version.json'
    config_path = state / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf8')) if config_path.is_file() else {}
    version = read_version(version_file) if version_file.is_file() else {}
    manifest_path = state / 'public/master-manifest.json'
    master_ready = False
    if version and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        cache = state / 'public/masterdata' / version['masterVersion']
        master_ready = manifest['version'] == version['masterVersion'] and bool(manifest['files']) and all(
            (cache / (Path(entry['name']).stem + '.json')).is_file() for entry in manifest['files'])
    hostnames = list(config.get('hostnames', []))
    resource_mirror = config.get('resourceMirror', False)
    if resource_mirror and version:
        hostnames = sorted(set(hostnames + [urlsplit(version['cdnRoot']).hostname]))
    return {
        'stateDirectory': str(state), 'versionFile': str(version_file),
        'versionAvailable': bool(version),
        'serverCertificateAvailable': (root / config.get('certFile', '.native-server/server.crt')).is_file(),
        'serverKeyAvailable': (root / config.get('keyFile', '.native-server/server.key')).is_file(),
        'configurationAvailable': config_path.is_file(),
        'lanIp': config.get('lanIp'), 'hostnames': hostnames,
        'resourceMirror': resource_mirror,
        'resourcePort': config.get('resourcePort', 8767) if resource_mirror else None,
        'announcementImages': announcement_images_status(root, config),
        'dnsUpstream': config.get('dnsUpstream', '1.1.1.1'),
        'battlePort': config.get('battlePort', 9001),
        'battleMode': 'solo-dummy', 'battleDummyCount': 4,
        'version': {key: version.get(key) for key in
                    ('clientVersion', 'masterVersion', 'resourceVersion', 'region')},
        'masterDataAvailable': master_ready,
        'dependencies': {name: metadata.version(name) for name in ('h2', 'cryptography')},
        'implementedRpc': ['PlayerLoginService/GetServerList', 'MasterdataService/Version',
                           'PlayerLoginService/PlayerPreLogin', 'PlayerLoginService/PlayerLogin',
                           'FeatureSwitchService/Get', 'PlayerService/GetPlayerData',
                           'ExternalPaymentsService/Nop', 'PresentService/Fetch',
                           'DeckService/SaveDecks', 'PlayerExtService/SyncData',
                           'HomeService/Get', 'SeasonPassService/Fetch',
                           'AnnouncementService/GetList', 'AnnouncementService/Get', 'ShopService/Fetch',
                           'LiveMusicService/GetRanking',
                           'LiveService/CheckMaintenanceFree', 'LiveService/StartFree',
                           'LiveService/FinishFree', 'LiveService/SkipFree', 'LiveService/GainStampReward',
                           'LiveService/StartBattleLive', 'LiveService/FinishBattleLive',
                           'LiveService/GetBattleLiveResult',
                           'LiveService/FetchBattleLiveInvitation', 'LiveService/GetBattleLiveSearchId',
                           'CharacterService/GainFriendshipRankReward',
                           'CarouselHelpService/Shown', 'PlayerService/EditProfile',
                           'PlayerService/SetCurrentCostumes',
                           'SpotService/UpdateSpotFavorites',
                           'FriendService/FindByProfileID', 'FriendService/FriendRequest',
                           'FriendService/FriendRequestWithdrawal', 'FriendService/FriendAnswer',
                           'FriendService/FriendUnlink',
                           'ContentUnlockService/Shown',
                           'ShopService/CheckMaintenanceShop',
                           'LiveService/CheckMaintenanceCasualMatch',
                           'LiveService/CheckMaintenanceRankMatch',
                           'GachaService/CheckMaintenanceGacha',
                           'StoryService/CheckMaintenanceStory',
                           'LiveBoostService/Recovery',
                           'SeasonPassService/ReceiveReward', 'SeasonPassService/ReceiveMission',
                           'OfflineBonusService/Status', 'OfflineBonusService/Detail',
                           'OfflineBonusService/Receive',
                           'MemberCardService/LevelUp',
                           'MemberCardService/Awake', 'MemberCardService/LiveSkillLevelUp',
                           'MemberCardService/PerformanceSkillLevelUp',
                           'PersonalNotificationService/Fetch',
                           'InvitationService/GenerateInvitationCode',
                           'EventService/GetChallengeMusicRanking',
                           'StoryService/ReadStoryEpisode', 'StoryService/ReadFriendshipEpisode',
                           'GachaService/Execute', 'GachaService/ExecuteVip',
                           'GachaService/Probability', 'GachaService/History',
                           'SupportCardService/LevelUp',
                           'PlayerService/GetPlayerFavoriteStatus', 'PlayerService/UpdatePlayerFavorite',
                           'ProfileCardService/SaveProfileCard', 'ProfileCardService/EditProfileCardSlot',
                           'ProfileCardService/SettingProfileCard', 'ProfileCardService/PutObjects',
                           'ProfileCardService/UpdateProfileCardDispOrder', 'ProfileCardService/RemoveProfileCard',
                           'ShopService/Purchase', 'ShopService/PurchaseCM', 'ShopService/PurchaseCash',
                           'OrderService/CreateOrder', 'VipService/Fetch', 'VipService/ReceivePoint',
                           'VipService/ReceiveReward', 'VipService/ReceiveRankupReward',
                           'ExchangeService/Exchange', 'PieceExchangeService/Exchange',
                           'MissionService/Fetch', 'MissionService/Receive',
                           'MissionService/ReceiveCharacter', 'LoginBonusService/Update',
                           'StampService/UpdateStampFavorites', 'StampService/UpdateStampFavoriteName',
                           'BandItemService/LevelUp',
                           'LiveService/SaveSetting', 'LiveMusicService/SaveFavorite',
                           'LiveMusicService/SavePuzzle', 'PlayerService/ChangeFavoriteMember',
                           'PlayerService/GetCharacterOrders', 'PlayerService/SaveCharacterOrders',
                           'PlayerService/SetNotificationSetting', 'MemberCardService/RankUp',
                           'MemberCardService/LeaderSkillLevelUp', 'MemberCardService/LinkSkillLevelUp',
                           'MemberCardService/GekisouSkillLevelUp', 'SupportCardService/RankUp',
                           'GachaService/ConvertPoint', 'OfflineBonusService/ReceiveStock',
                           'OfflineBonusService/Skip', 'MissionService/FetchEvent',
                           'MissionService/ReceiveEvent', 'MissionService/ReceiveLimited',
                           'EventService/ExecuteBoxGacha', 'EventService/ResetBoxGacha',
                           'EventService/GetDeck', 'EventService/GetRank',
                           'EventService/GetRankingList', 'LiveService/StartChallengeLive',
                           'LiveService/FinishChallengeLive', 'LiveService/SkipChallengeLive',
                           'GemService/History', 'ShopService/History',
                           'InvitationService/InputInvitationCode', 'InvitationService/UpdateInvitationView',
                           'SerialCodeService/RedeemSerialCode', 'LiveService/SendBattleLiveInvitation',
                           'LiveService/RemoveBattleLiveInvitation', 'FriendService/PlayerReport',
                           'PresentService/Open', 'PresentService/History',
                           'PlayerService/Login', 'PlayerExtService/ReportLog',
                           'ExternalPaymentsService/ReserveProvisionalPurchase',
                           'ExternalPaymentsService/ReleaseProvisionalPurchase',
                           'EventService/GetChallengeMusicTotalRanking', 'EventService/VisitTop',
                           'EventService/GetTryEvent', 'LiveService/StartTryLive',
                           'LiveService/FinishTryLive', 'LiveService/SkipTryLive',
                           'ArenaService/ArenaTop', 'ArenaService/ArenaMyRank',
                           'ArenaService/ArenaRanking', 'ArenaService/ArenaRewards',
                           'ArenaService/ConsecutiveWins', 'ArenaService/DeckTrend',
                           'ArenaService/MatchingHistories', 'ArenaService/SaveDedicatedDecks',
                           'ArenaService/ReportPenalty', 'ArenaService/ReceivePromotionReward',
                           'ArenaService/ReportSeasonResultShown', 'LiveService/StartArenaLive',
                           'LiveService/FinishArenaLive', 'LiveService/GetArenaLiveResult',
                           'ClientChatService/GetChatSettings', 'ClientChatService/SetChatSettings',
                           'ClientChatService/GetNotifications', 'ClientChatService/SyncMsg',
                           'ClientChatService/SendIMMsg',
                           'PlayerExtService/GetPlayerList'],
    }


def status(args):
    result = local_status(version_file=args.version_file)
    result['managedProcess'] = {'running': False, 'pid': None}
    if os.name == 'nt' and (STATE / 'service.pid').is_file():
        from .process import control_service
        result['managedProcess'] = control_service(ROOT, 'status')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def init(args):
    from deathnote.game.bootstrap import BOOTSTRAP_HOST
    from deathnote.net.setup import publish_setup
    from deathnote.net.tls import create_local_pki
    config_file = STATE / 'config.json'
    config = json.loads(config_file.read_text('utf8')) if config_file.is_file() else {}
    hosts = [BOOTSTRAP_HOST, *config.get('hostnames', []), *args.hostname]
    if args.version_file.is_file():
        hosts.append(urlsplit(read_version(args.version_file)['apiRoot']).hostname)
    hosts = sorted(set(hosts))
    result = create_local_pki(STATE, args.lan_ip, hosts)
    if result['status'] == 'incomplete':
        raise ValueError('Existing PKI is incomplete: ' + ', '.join(result['missing']))
    config.update(lanIp=args.lan_ip, hostnames=hosts)
    config.setdefault('serverDisplayName', 'DeathNote')
    config_file.write_text(json.dumps(config, indent=2) + '\n', encoding='utf8')
    publish_setup(STATE, STATE / 'setup')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def update(args):
    from deathnote.data.locking import workspace_lock
    from deathnote.data.public_update import check_public_data, update_public_data

    def progress(value):
        if value['totalFiles']:
            print(f"{value['message']} ({value['completedFiles']}/{value['totalFiles']})", file=sys.stderr)
        else:
            print(value['message'], file=sys.stderr)

    if args.check:
        result = check_public_data(ROOT, apk_file=args.apk, on_progress=progress)
    else:
        with workspace_lock(ROOT, 'service.lock'):
            result = update_public_data(ROOT, apk_file=args.apk, on_progress=progress,
                                        backup_before_update=args.backup, include_ios_catalogs=args.catalogs)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def candidates(args):
    from deathnote.data.locking import workspace_lock
    from deathnote.data.chart_candidates import NativeChartCandidates, NativeChartCandidateError
    manager = NativeChartCandidates(ROOT)
    try:
        if args.candidate_action == 'list':
            result = manager.status()
        else:
            with workspace_lock(ROOT, 'service.lock'), workspace_lock(ROOT):
                result = (manager.activate(args.candidate_id)
                          if args.candidate_action == 'activate' else manager.revoke())
    except NativeChartCandidateError as error:
        raise ValueError(f'{error.code}: {error.message}') from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


def logs(args):
    from collections import deque

    path = STATE / 'requests.jsonl'
    if path.is_file():
        with path.open(encoding='utf8') as stream:
            print(''.join(deque(stream, maxlen=args.lines)), end='')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='deathnote', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    state = commands.add_parser('status', help='Read local readiness; create no files')
    state.add_argument('--version-file', type=Path, default=VERSION)
    initialize = commands.add_parser('init', help='Explicitly create/reuse a private local CA and download page')
    initialize.add_argument('--lan-ip', required=True)
    initialize.add_argument('--hostname', action='append', default=[])
    initialize.add_argument('--version-file', type=Path, default=VERSION)
    sync = commands.add_parser('update', help='Fetch the public version and master data')
    sync.add_argument('--apk', type=Path, default=ROOT / 'client.apk')
    sync.add_argument('--check', action='store_true', help='Check the version without writing files')
    sync.add_argument('--backup', action='store_true', help='Back up existing local data before publishing')
    sync.add_argument('--catalogs', action='store_true', help='Also download the five iOS catalog pairs')
    importer = commands.add_parser('import-public', help='Import an existing public-data snapshot')
    importer.add_argument('source', type=Path, help='Root of the previous server installation')
    candidate = commands.add_parser('candidates', help='List or switch local chart candidates')
    candidate_commands = candidate.add_subparsers(dest='candidate_action', required=True)
    candidate_commands.add_parser('list')
    activate = candidate_commands.add_parser('activate')
    activate.add_argument('candidate_id')
    candidate_commands.add_parser('revoke')
    image_certificate = commands.add_parser('image-certificate', help='Create or reissue the LAN image certificate')
    image_certificate.add_argument('--lan-ip', required=True)
    image_certificate.add_argument('--coverage', choices=('single', 'subnet'), default='subnet')
    log_reader = commands.add_parser('logs', help='Print recent request log entries')
    log_reader.add_argument('--lines', type=int, default=30)
    for action in ('start', 'stop', 'restart'):
        commands.add_parser(action, help=f'{action.capitalize()} the managed Windows server')
    serve = commands.add_parser('run', help='Run the game server in the foreground')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=443)
    serve.add_argument('--cert-file', type=Path, help='Service certificate; overrides config.json certFile')
    serve.add_argument('--key-file', type=Path, help='Private key; overrides config.json keyFile')
    serve.add_argument('--chain-file', type=Path, help='Public CA chain appended to the service certificate')
    serve.add_argument('--version-file', type=Path, default=VERSION)
    serve.add_argument('--lan-ip')
    serve.add_argument('--api-root')
    serve.add_argument('--h2c-port', type=int)
    serve.add_argument('--h2c-only', action='store_true')
    serve.add_argument('--dns-upstream')
    serve.add_argument('--dns-port', type=int, default=53)
    serve.add_argument('--dns-passthrough', action='store_true')
    serve.add_argument('--setup-port', type=int)
    serve.add_argument('--tls-diagnostics', action='store_true')
    serve.add_argument('--console', action='store_true', help='Read interactive commands from the terminal')
    serve.add_argument('--launch-pid-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    from deathnote.data.locking import workspace_lock
    try:
        if args.command == 'status':
            status(args)
        elif args.command == 'init':
            with workspace_lock(ROOT, 'service.lock'):
                init(args)
        elif args.command == 'update':
            update(args)
        elif args.command == 'import-public':
            from deathnote.data.snapshot import import_public_data
            with workspace_lock(ROOT, 'service.lock'):
                print(json.dumps(import_public_data(ROOT, args.source), ensure_ascii=False, indent=2))
        elif args.command == 'candidates':
            candidates(args)
        elif args.command == 'image-certificate':
            from deathnote.net.image_certificate import rebuild_image_certificate
            with workspace_lock(ROOT, 'service.lock'):
                print(json.dumps(rebuild_image_certificate(ROOT, args.lan_ip, args.coverage), indent=2))
        elif args.command == 'logs':
            logs(args)
        elif args.command in ('start', 'stop', 'restart'):
            from .process import control_service
            print(json.dumps(control_service(ROOT, args.command), indent=2))
        else:
            with workspace_lock(ROOT, 'service.lock'):
                asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        parser.exit(1, f'DeathNote: {error}\n')
    return 0

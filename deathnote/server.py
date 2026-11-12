from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
import threading
from urllib.parse import urlsplit

from deathnote.data.version import read_version
from deathnote.paths import ROOT, STATE


async def run(args):
    from deathnote.game.bootstrap import BOOTSTRAP_HOST, NativeBootstrap
    from deathnote.game.players.login import NativePlayerLogin
    from deathnote.game.social.chat_hub import NativeChatHub
    from deathnote.net.battle_transport import NativeBattleTransport
    from deathnote.net.chat_realtime import NativeChatRealtime
    from deathnote.net.http2 import serve_native
    from deathnote.net.image_service import serve_announcement_images
    from deathnote.net.tls import DEFAULT_GAME_CERTIFICATE, DEFAULT_GAME_KEY, load_server_context

    if args.launch_pid_file is not None:
        temporary = args.launch_pid_file.with_suffix('.tmp')
        temporary.write_text(str(os.getpid()), 'ascii')
        temporary.replace(args.launch_pid_file)
    version = read_version(args.version_file)
    config = json.loads((STATE / 'config.json').read_text(encoding='utf8')) if (STATE / 'config.json').is_file() else {}
    lan_ip = args.lan_ip or config.get('lanIp')
    if args.dns_upstream and not lan_ip:
        raise ValueError('DNS needs --lan-ip or an explicitly initialized config.json')
    if args.h2c_only and args.h2c_port is None:
        raise ValueError('--h2c-only requires --h2c-port')
    context = None
    if not args.h2c_only:
        cert_file = args.cert_file or ROOT / config.get('certFile', DEFAULT_GAME_CERTIFICATE)
        key_file = args.key_file or ROOT / config.get('keyFile', DEFAULT_GAME_KEY)
        chain_file = args.chain_file or (ROOT / config['chainFile'] if config.get('chainFile') else None)
        context = load_server_context(cert_file, key_file, chain_file)
    api_root = args.api_root
    if api_root is None and args.h2c_port is not None:
        if not lan_ip:
            raise ValueError('h2c responses need --lan-ip or --api-root')
        api_root = f'http://{lan_ip}:{args.h2c_port}'
    display_name = config.get('serverDisplayName', version['region'])
    login = NativePlayerLogin(STATE, server_id='2', server_name=display_name)
    resource_mirror = None
    resource_port = config.get('resourcePort', 8767)
    resource_root = None
    if config.get('resourceMirror', False):
        from deathnote.data.resource_mirror import NativeResourceMirror, resource_download_root
        resource_mirror = NativeResourceMirror(ROOT, version)
        resource_root = resource_download_root(config, resource_mirror)

    def http_object(path):
        if resource_mirror is not None:
            resource = resource_mirror.http_object(path)
            if resource is not None:
                return resource
        announcement_image = bootstrap.announcement.http_object(path)
        if announcement_image is not None:
            return announcement_image
        return bootstrap.profile_card.http_object(path)
    STATE.mkdir(parents=True, exist_ok=True)

    def observe(summary):
        entry = {'time': datetime.now(timezone.utc).isoformat(), **summary}
        with (STATE / 'requests.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    battle_port = config.get('battlePort', 9001)
    battle = NativeBattleTransport(args.host, battle_port, ssl_context=context, dummy_count=4,
                                   logger=lambda event, details: observe({'event': event, **details}))
    chat_hub = NativeChatHub()
    chat_realtime = NativeChatRealtime(login, logger=observe, hub=chat_hub)
    battle_address = ('nossl:' if args.h2c_only else 'ssl:') + urlsplit(version['apiRoot']).hostname + ':' + str(battle_port)
    bootstrap = NativeBootstrap(version, login=login, api_port=args.port, api_root=api_root,
                                server_display_name=display_name, cdn_root=resource_root,
                                battle_transport=battle, battle_server_address=battle_address,
                                announcement_directory=ROOT / 'native-server/announcements',
                                announcement_image_base_url=config.get('announcementImageBaseUrl'),
                                profile_card_image_base_url=(config.get('announcementImageBaseUrl')
                                    if config.get('announcementImagesTls', {}).get('enabled', False) else None),
                                arena_point_adjust=config.get('arenaPointAdjust'),
                                arena_reset_scale=config.get('arenaResetScale', 10000),
                                chat_hub=chat_hub)

    diagnostics = nullcontext()
    if args.tls_diagnostics:
        from deathnote.net.tls_diagnostics import tls_diagnostics
        diagnostics = tls_diagnostics(context, observe)

    with diagnostics:
        listeners, dns, setup, resources = [], None, None, None
        announcement_image_port = None
        try:
            battle.start()
            if resource_mirror is not None:
                from deathnote.data.resource_mirror import resource_server
                resources = resource_server(args.host, resource_port, resource_mirror, observe)
                threading.Thread(target=resources.serve_forever, daemon=True).start()
            if not args.h2c_only:
                listeners.append(await serve_native(args.host, args.port, context, bootstrap.dispatch, observe,
                                                      http_get=http_object,
                                                      websocket_handler=chat_realtime.serve))
            if args.h2c_port is not None:
                listeners.append(await serve_native(args.host, args.h2c_port, None, bootstrap.dispatch, observe,
                                                      http_get=http_object,
                                                      websocket_handler=chat_realtime.serve))
            announcement_images = await serve_announcement_images(args.host, ROOT, config,
                bootstrap.announcement, observe, profile_card=bootstrap.profile_card,
                public_asset=resource_mirror.http_object if resource_mirror is not None else None)
            if announcement_images is not None:
                listeners.append(announcement_images)
                announcement_image_port = announcement_images.sockets[0].getsockname()[1]
            if args.dns_upstream:
                from deathnote.net.dns import serve_dns
                hosts = sorted(set(config.get('hostnames', []) + [BOOTSTRAP_HOST, urlsplit(version['apiRoot']).hostname]))
                if resource_mirror is not None:
                    hosts = sorted(set(hosts + [resource_mirror.host]))
                dns = await serve_dns(args.host, args.dns_port, lan_ip, hosts, args.dns_upstream, observe,
                                      passthrough=args.dns_passthrough)
            if args.setup_port is not None:
                from deathnote.net.setup import setup_server
                setup = setup_server(args.host, args.setup_port, STATE / 'setup')
                threading.Thread(target=setup.serve_forever, daemon=True).start()
            print(json.dumps({'event': 'listening', 'host': args.host,
                              'tlsPort': None if args.h2c_only else args.port,
                              'h2cPort': args.h2c_port, 'dnsPort': args.dns_port if dns else None,
                              'battlePort': battle_port, 'battleMode': 'solo-dummy', 'battleDummyCount': 4,
                              'resourcePort': resource_port if resources else None,
                              'announcementImagePort': announcement_image_port,
                              'setupPort': args.setup_port if setup else None}), flush=True)
            if args.console:
                from deathnote.cli.console import serve_console
                await serve_console(bootstrap, version)
            else:
                await asyncio.Event().wait()
        finally:
            await asyncio.to_thread(battle.stop)
            if dns:
                dns.close()
            if setup:
                await asyncio.to_thread(setup.shutdown)
                setup.server_close()
            if resources:
                await asyncio.to_thread(resources.shutdown)
                resources.server_close()
            for listener in listeners:
                listener.close()
                await listener.wait_closed()
            if resource_mirror is not None:
                resource_mirror.close()

from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import sys
from threading import Thread


def _players(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        rows = connection.execute('''
            SELECT players.profile_id, COALESCE(player_profile_edits.name, '')
            FROM players
            LEFT JOIN player_profile_edits USING (profile_id)
            ORDER BY players.profile_id
        ''').fetchall()
    return [{'profileId': profile_id, 'name': name} for profile_id, name in rows]


async def serve_console(bootstrap, version):
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def read_commands():
        try:
            with open(sys.stdin.fileno(), 'rb', buffering=0, closefd=False) as stdin:
                for line in stdin:
                    command = line.decode(sys.stdin.encoding or 'utf-8', errors='replace').strip().lower()
                    loop.call_soon_threadsafe(lines.put_nowait, command)
                    if command == 'stop':
                        return
            loop.call_soon_threadsafe(lines.put_nowait, 'stop')
        except RuntimeError:
            if not loop.is_closed():
                raise

    release = {key: version[key] for key in ('clientVersion', 'masterVersion', 'resourceVersion', 'region')}
    Thread(target=read_commands, name='DeathNote console', daemon=True).start()
    print("DeathNote console ready. Type 'help' for commands.", flush=True)
    while True:
        command = await lines.get()
        if command == 'stop':
            print('Stopping DeathNote.', flush=True)
            return
        elif command == 'help':
            print('help     Show console commands\n'
                  'status   Show service state and version\n'
                  'version  Show client and data versions\n'
                  'players  List local player profiles\n'
                  'stop     Shut down the server', flush=True)
        elif command == 'status':
            print(json.dumps({'running': True, 'version': release}), flush=True)
        elif command == 'version':
            print(json.dumps(release), flush=True)
        elif command == 'players':
            profiles = await asyncio.to_thread(_players, bootstrap.login.database)
            print(json.dumps({'players': profiles}, ensure_ascii=False), flush=True)
        elif command:
            print("Unknown command. Type 'help' for commands.", flush=True)

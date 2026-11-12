# DeathNote

A server reimplementation for a certain rhythm game.

## Compatibility

The implementation targets `TW/HK/MO` client, version **1.1.0**.

| Area | Availability |
| --- | --- |
| Login and player data | Server selection, local profiles, inventory, teams and progression |
| Live | Solo play, challenge and event flows, rankings, and Gekisou rooms with local simulated teammates |
| Progression | Cards, support cards, missions, gifts, season pass and story unlocks |
| Social and content | Friends, chat, announcements, profile cards and compatible custom chart candidates |
| Administration | Service commands, an interactive console, official version checks and master data backups |

Game functions follow the RPCs and rules implemented by DeathNote. Event availability depends on the installed master data.

## Quick start

Use Python 3.12. The included process controls target Windows.

```powershell
.\install.cmd
```

If Python is available through a standalone executable, pass its path to `install.cmd`.

Place your matching Android client package at `client.apk` in this directory. Set up the official master data, then initialize the instance with the server computer's LAN address:

```powershell
.\.venv\Scripts\python.exe -m deathnote update
.\.venv\Scripts\python.exe -m deathnote init --lan-ip 192.168.114.51
```

Replace the example address with the server computer's LAN address and configure game API TLS. Run `.\start.cmd` to open the foreground server console. Type `help` to see the commands and `stop` to shut down. See the [operations guide](docs/operations.md) for DNS, certificate setup, data imports and backups.

## Project layout

| Path | Contents |
| --- | --- |
| `deathnote/cli/` | Terminal commands and the interactive console |
| `deathnote/server.py` | Game service startup and listener setup |
| `deathnote/game/` | Player accounts, live play, progression, commerce and social features |
| `deathnote/net/` | HTTP/2, TLS, DNS, WebSocket and live room transport |
| `deathnote/protocol/` | RPC errors and protobuf encoding and decoding |
| `deathnote/data/` | Client input, version sync, resource mirroring and chart candidates |
| `scripts/` | Windows background service control |
| `native-server/protocol/` | Recovered protocol definitions |
| `docs/` | Setup and operations |
| `.native-server/` | Local instance data created by setup and use |

## Credit

DeathNote has no affiliation with the game publishers or developers. Game names, trademarks, client files and assets belong to their respective rights holders. The source tree contains the local server implementation and protocol definitions. Operators should supply the matching client and obtain game resources separately. Player accounts, credentials and service keys are specific to each deployment.

DeathNote is only for research purposes, **DO NOT USE IT FOR PIRACY**.

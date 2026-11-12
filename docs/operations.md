# Operating DeathNote

Run these commands from the DeathNote directory with the Python 3.12 environment created by `install.cmd`. In PowerShell, set a variable for the interpreter:

```powershell
$deathNotePython = ".\.venv\Scripts\python.exe"
& $deathNotePython -m deathnote status
```

## Versions and data

Place a matching APK at `client.apk` for official version queries and master decoding. On an existing instance, use `update --check` to compare versions and `update --backup` to save a recovery point before refreshing the master data.

```powershell
& $deathNotePython -m deathnote update --check
& $deathNotePython -m deathnote update --backup
```

Pass `--catalogs` to an update to fetch the matching iOS resource catalogs. These catalogs are used by the local resource mirror and custom chart candidates.

To seed DeathNote from an existing Native Server installation, import its official public snapshot into the new instance:

```powershell
& $deathNotePython -m deathnote import-public "D:\old-instance"
```

The import copies the selected official version metadata, manifest, master data and any matching iOS catalog pairs. It validates the raw master files against the manifest. The instance's configuration, accounts and TLS identity are managed separately.

## Configure the instance

`init --lan-ip 192.168.10.20` creates local setup files and a development CA. Replace the example address with the server computer's LAN address. The game API needs a server certificate covering the names routed to DeathNote and accepted by the client's TLS trust configuration. Set `certFile`, `keyFile` and, when applicable, `chainFile` in `.native-server/config.json` to the files for your deployment. Relative paths are resolved from the DeathNote directory.

Set the test device's Wi-Fi DNS to the server computer's LAN address when the local DNS listener is in use. Configure `hostnames` for additional game endpoints and `dnsUpstream` for forwarded DNS queries. The service includes the bootstrap and current API hostname in its DNS mapping. With `resourceMirror` enabled, it also includes the resource hostname.

| Listener | Default | Purpose |
| --- | --- | --- |
| Game API | TCP 443 | TLS, HTTP/2 gRPC and game web endpoints |
| LAN DNS | UDP 53 | Configured game host routing and upstream forwarding |
| Gekisou | TCP 9001 | Room transport |
| Resource mirror | TCP 8767 | Optional local resource endpoint |
| Image HTTPS | TCP 8768 | Optional announcement and profile-card image endpoint |
| Certificate download | TCP 8766 | Optional setup page for a development CA |

For a foreground process with DNS enabled, run:

```powershell
& $deathNotePython -m deathnote run --console --host 0.0.0.0 --dns-upstream 1.1.1.1
```

Type `help`, `status`, `version`, `players` or `stop` in the running console. The `players` command lists stored local profiles. Ctrl+C also stops the foreground server. The low ports may need elevated privileges on your platform. To serve the optional development CA download page on the LAN, append `--setup-port 8766`.

On Windows, the `start`, `stop` and `restart` subcommands control the background process. Run `python -m deathnote status` for readiness and tracked process status, or `python -m deathnote logs --lines 20` for recent request summaries. Stop the game service before changing versions or importing data.

Use `python -m deathnote candidates list` to inspect installed custom chart candidates. With the service stopped, `candidates activate CANDIDATE_ID` switches to a compatible candidate and `candidates revoke` restores the official version.

Use `python -m deathnote image-certificate --lan-ip 192.168.10.20 --coverage single` to reissue the announcement and profile-card image certificate for the specified LAN address.

## Local content and state

| Path | Data |
| --- | --- |
| `.native-server/config.json` | Instance configuration and TLS paths |
| `.native-server/public/` | Official version, master data and resource catalogs |
| `.native-server/players.sqlite3` | Local accounts and progress |
| `.native-server/requests.jsonl` | Request summaries with RPC paths and status codes |
| `.native-server/backups/` | Local update backups |
| `.native-server/candidates/` | Compatible custom chart candidates |
| `native-server/announcements/` | One subdirectory per announcement, with `announcement.json` and optional images |
| `native-server/serial-codes/` | Local redemption code definitions |

`update --backup` backs up the current public version and a consistent copy of the player database before updating. Keep instance files with the deployment when replacing the source tree. Recreate the Python virtual environment when moving the installation.

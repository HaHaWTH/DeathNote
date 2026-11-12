# Operating DeathNote

Run these commands from the DeathNote directory with the Python 3.12 environment created by `install.cmd`. In PowerShell, set a variable for the interpreter:

```powershell
$deathNotePython = ".\.venv\Scripts\python.exe"
& $deathNotePython -m deathnote status
```

## Versions and data

Import an official public snapshot from a compatible installation. Pass the directory that contains `.native-server/public/`.

```powershell
& $deathNotePython -m deathnote import-public "D:\snapshot-source"
```

The import verifies the version metadata, manifest and master data. It also copies matching iOS catalog pairs when present. Those catalogs are used by the local resource mirror and custom chart candidates. Each instance manages its own configuration, accounts and TLS identity.

Run `update --check` to compare with official data for the imported snapshot's client version and region. To install newer data, prepare the new snapshot in the source installation and import it again. Pass `--backup` to save the current version, master data and player database first.

```powershell
& $deathNotePython -m deathnote update --check
& $deathNotePython -m deathnote import-public "D:\snapshot-source" --backup
```

## Configure the instance

`init --lan-ip 192.168.10.20` writes the LAN settings and default game TLS paths to `.native-server/config.json`. Use the server computer's LAN address.

Supply a PEM certificate that covers the names routed to DeathNote and a matching PEM private key. Place them at `.native-server/server.crt` and `.native-server/server.key`. Set `certFile` and `keyFile` in the config to use different paths, and set `chainFile` if you supply the certificate chain in a separate PEM file. Relative config paths are resolved from the DeathNote directory. For a foreground run, `--cert-file`, `--key-file` and `--chain-file` override these paths.

Set the test device's Wi-Fi DNS to the server computer's LAN address when the local DNS listener is in use. Configure `hostnames` for additional game endpoints and `dnsUpstream` for forwarded DNS queries. The service includes the bootstrap and current API hostname in its DNS mapping. With `resourceMirror` enabled, it also includes the resource hostname.

| Listener | Default | Purpose |
| --- | --- | --- |
| Game API | TCP 443 | TLS, HTTP/2 gRPC and game web endpoints |
| LAN DNS | UDP 53 | Configured game host routing and upstream forwarding |
| Gekisou | TCP 9001 | Room transport |
| Resource mirror | TCP 8767 | Optional local resource endpoint |
| Image HTTPS | TCP 8768 | Optional announcement and profile-card image endpoint |
| Certificate download | TCP 8766 | Optional page for public CA downloads |

For a foreground process with DNS enabled, run:

```powershell
& $deathNotePython -m deathnote run --console --host 0.0.0.0 --dns-upstream 1.1.1.1
```

Type `help`, `status`, `version`, `players` or `stop` in the running console. The `players` command lists stored local profiles. Ctrl+C also stops the foreground server. The low ports may need elevated privileges on your platform. To serve public CA downloads from `.native-server/setup`, append `--setup-port 8766`. To prepare and serve files from a public CA PEM, run `python -m deathnote.net.setup --ca-file path/to/ca.pem`.

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

Keep instance files with the deployment when replacing the source tree. Recreate the Python virtual environment when moving the installation.

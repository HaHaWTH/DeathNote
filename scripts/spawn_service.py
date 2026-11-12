"""Launch this workspace's native service without inheriting control pipes."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


ROOT = Path(__file__).resolve().parents[1]


def spawn(dns_upstream: str) -> int:
    upstream = str(ipaddress.ip_address(dns_upstream))
    runtime = sys.executable
    entry = ROOT / 'native_server.py'
    state = ROOT / '.native-server'
    with TemporaryDirectory(dir=state, prefix='.launch-') as directory:
        pid_file = Path(directory) / 'pid'
        arguments = [runtime, '-E', '-s', '-B', '-X', 'utf8', str(entry),
                     'run', '--host', '0.0.0.0', '--dns-upstream', upstream,
                     '--tls-diagnostics', '--launch-pid-file', str(pid_file)]
        with (state / 'service.stdout.log').open('wb', buffering=0) as stdout, \
                (state / 'service.stderr.log').open('wb', buffering=0) as stderr:
            process = subprocess.Popen(arguments, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            if pid_file.is_file():
                return int(pid_file.read_text('ascii'))
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
            process.wait()
        raise RuntimeError('The service process did not start. See service.stderr.log.')


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dns-upstream', required=True)
    arguments = parser.parse_args(argv)
    print(json.dumps({'pid': spawn(arguments.dns_upstream)}), flush=True)


if __name__ == '__main__':
    main()

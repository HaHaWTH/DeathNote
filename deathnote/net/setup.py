"""Publish an explicitly prepared local CA and serve its download page."""
from __future__ import annotations

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil

from deathnote.paths import STATE

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeathNote Certificate Setup</title>
<style>body{font:17px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;
max-width:34rem;margin:3rem auto;padding:0 1.25rem;background:#f7f8fc;color:#202534}
a{display:block;margin:1rem 0;padding:1rem;border-radius:12px;background:#e6ecff;
color:#214bb4;text-decoration:none}h1{font-size:1.6rem}</style>
<h1>DeathNote Certificate Setup</h1>
<p>Open this page in Safari on the test device and download the CA profile.</p>
<a href="CA.mobileconfig">Download the iOS CA profile</a>
<a href="rootCA.der">Download the CA certificate</a>
<p>Install the profile in Settings, then open General → About → Certificate Trust Settings
and enable trust for the installed root.</p>
</html>'''.encode('utf8')
FILES = {'/CA.mobileconfig': ('CA.mobileconfig', 'application/x-apple-aspen-config'),
         '/rootCA.der': ('rootCA.der', 'application/pkix-cert')}


def publish_setup(state_dir, destination):
    state_dir, destination = Path(state_dir), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, _ in FILES.values():
        shutil.copyfile(state_dir / name, destination / name)
    (destination / 'index.html').write_bytes(PAGE)


class SetupHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, state_dir, **kwargs):
        self.state_dir = Path(state_dir)
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            body, kind = PAGE, 'text/html; charset=utf-8'
        elif self.path in FILES:
            name, kind = FILES[self.path]
            path = self.state_dir / name
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def setup_server(host, port, state_dir):
    return ThreadingHTTPServer((host, port), partial(SetupHandler, state_dir=state_dir))


def prepare_ca_download(ca_file, state_dir):
    """Package the supplied public CA chain for iOS; no signing or verification."""
    import plistlib
    import uuid
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    certificates = x509.load_pem_x509_certificates(Path(ca_file).read_bytes())
    payloads = []
    for index, certificate in enumerate(certificates, 1):
        payloads.append({
            'PayloadType': 'com.apple.security.root', 'PayloadVersion': 1,
            'PayloadIdentifier': f'local.deathnote.provided-ca.certificate.{index}',
            'PayloadUUID': str(uuid.uuid4()).upper(),
            'PayloadDisplayName': f'DeathNote CA {index}',
            'PayloadCertificateFileName': f'CA-{index}.cer',
            'PayloadContent': certificate.public_bytes(Encoding.DER),
        })
    profile = {
        'PayloadType': 'Configuration', 'PayloadVersion': 1,
        'PayloadIdentifier': 'local.deathnote.provided-ca',
        'PayloadUUID': str(uuid.uuid4()).upper(),
        'PayloadDisplayName': 'DeathNote CA',
        'PayloadRemovalDisallowed': False,
        'PayloadContent': payloads,
    }
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    (state / 'CA.mobileconfig').write_bytes(plistlib.dumps(profile))
    (state / 'rootCA.der').write_bytes(certificates[-1].public_bytes(Encoding.DER))


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description='Serve only public CA downloads for iOS.')
    parser.add_argument('--ca-file', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path,
                        default=STATE / 'setup')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    prepare_ca_download(args.ca_file, args.state_dir)
    with setup_server(args.host, args.port, args.state_dir) as server:
        print(json.dumps({'event': 'certificate_download_listening',
                          'host': args.host, 'port': args.port}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()

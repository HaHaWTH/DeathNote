"""Reissue the dedicated image leaf while retaining the installed local CA."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import tempfile

from .tls import create_local_pki

DISPLAY_NAME = 'Our Notes Announcement Images CA'
_PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in
                          ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
_REQUIRED_CA_FILES = ('rootCA.key', 'rootCA.pem', 'rootCA.der', 'server.key', 'CA.mobileconfig')


def certificate_options(lan_ip, coverage='subnet'):
    if not isinstance(lan_ip, str) or coverage not in ('single', 'subnet'):
        raise ValueError('请填写内网 IPv4，并选择单个 IP 或 /24 网段。')
    try:
        address = ipaddress.IPv4Address(lan_ip)
    except ipaddress.AddressValueError:
        raise ValueError('请填写有效的内网 IPv4 地址。') from None
    if not any(address in network for network in _PRIVATE_NETWORKS):
        raise ValueError('地址范围为 10/8、172.16/12 或 192.168/16 内网 IPv4。')
    network = ipaddress.IPv4Network(f'{address}/24', strict=False) if coverage == 'subnet' else None
    addresses = {address, *(network.hosts() if network is not None else ())}
    return str(address), sorted(addresses), {
        'mode': coverage, 'network': str(network) if network is not None else None,
        'addressCount': len(addresses),
    }


def _replace(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.image-certificate-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def rebuild_image_certificate(root, lan_ip, coverage='subnet'):
    address, ips, scope = certificate_options(lan_ip, coverage)
    root = Path(root)
    state = root / '.native-server'
    config_path = state / 'config.json'
    config = json.loads(config_path.read_text('utf8'))
    port = config.get('announcementImagesTls', {}).get('port', 8768)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('图片 HTTPS 端口须为 1～65535。')
    directory = state / 'announcement-tls'
    present = {name for name in (*_REQUIRED_CA_FILES, 'server.crt') if (directory / name).exists()}
    retained = bool(present)
    if retained:
        missing = set(_REQUIRED_CA_FILES) - present
        if missing:
            raise ValueError('Announcement PKI is incomplete: ' + ', '.join(sorted(missing)))
    else:
        create_local_pki(directory, address, [], display_name=DISPLAY_NAME)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca = x509.load_pem_x509_certificate((directory / 'rootCA.pem').read_bytes())
    ca_key = serialization.load_pem_private_key((directory / 'rootCA.key').read_bytes(), password=None)
    server_key = serialization.load_pem_private_key((directory / 'server.key').read_bytes(), password=None)
    if ca.public_key().public_numbers() != ca_key.public_key().public_numbers():
        raise ValueError('图片 CA 与签发密钥不匹配。')
    now = datetime.now(timezone.utc)
    expiry = min(now + timedelta(days=90), ca.not_valid_after_utc)
    if expiry <= now:
        raise ValueError('图片 CA 已到期，重建服务证书前须重新安装新的图片 CA。')
    certificate = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)]))
        .issuer_name(ca.subject).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expiry)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
            *(x509.IPAddress(value) for value in sorted({*ips, ipaddress.IPv4Address('127.0.0.1')}))]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    config['lanIp'] = address
    config['announcementImageBaseUrl'] = f'https://{address}:{port}'
    config['announcementImagesTls'] = {
        'enabled': True, 'port': port,
        'certFile': '.native-server/announcement-tls/server.crt',
        'keyFile': '.native-server/announcement-tls/server.key',
    }
    metadata = {**scope, 'lanIp': address, 'issuedAt': now.isoformat(), 'expiresAt': expiry.isoformat()}
    changes = {
        directory / 'server.crt': certificate.public_bytes(serialization.Encoding.PEM),
        directory / 'coverage.json': (json.dumps(metadata, ensure_ascii=False, indent=2) + '\n').encode('utf8'),
        config_path: (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode('utf8'),
    }
    previous = {path: path.read_bytes() if path.exists() else None for path in changes}
    published = []
    try:
        for path, data in changes.items():
            _replace(path, data)
            published.append(path)
    except OSError:
        for path in reversed(published):
            if previous[path] is None:
                path.unlink(missing_ok=True)
            else:
                _replace(path, previous[path])
        raise
    return {'status': 'reissued' if retained else 'created', 'lanIp': address,
            'coverage': scope, 'caRetained': retained,
            'imageBaseUrl': config['announcementImageBaseUrl']}

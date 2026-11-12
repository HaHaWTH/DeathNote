"""Load game TLS files and issue certificates for the separate image service."""
from __future__ import annotations

import ipaddress
import os
import plistlib
import ssl
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_GAME_CERTIFICATE = ".native-server/server.crt"
DEFAULT_GAME_KEY = ".native-server/server.key"

_FILES = {
    "ca_key": "rootCA.key",
    "ca_pem": "rootCA.pem",
    "ca_der": "rootCA.der",
    "server_cert": "server.crt",
    "server_key": "server.key",
    "mobileconfig": "CA.mobileconfig",
}


def _hostnames(hostnames: Iterable[str]) -> list[str]:
    names = sorted({name.rstrip(".").encode("idna").decode("ascii").lower() for name in hostnames})
    for name in names:
        if (not name or len(name) > 253 or "*" in name or
                any(not label or len(label) > 63 for label in name.split("."))):
            raise ValueError("Certificate hostnames must be explicit DNS names.")
    return names


def _write_new(path: Path, data: bytes, *, private: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def create_local_pki(state_dir: str | Path, lan_ip: str, hostnames: Iterable[str], *,
                     display_name: str = "DeathNote Local Server CA") -> dict:
    """Create or reuse a local CA and server certificate.

    Return flat path keys plus ``status`` (created/reused/incomplete),
    ``state_dir`` and ``missing``. An incomplete existing set is returned as-is.
    A complete set is reused after its server SAN covers the requested inputs.
    """
    state = Path(state_dir).resolve()
    address = ipaddress.IPv4Address(lan_ip)
    names = _hostnames(hostnames)
    paths = {key: state / filename for key, filename in _FILES.items()}
    existing = {key for key, path in paths.items() if path.exists()}
    result = {"state_dir": str(state), **{key: str(path) for key, path in paths.items()}}
    missing = [key for key in paths if key not in existing]
    if existing and missing:
        return {"status": "incomplete", **result, "missing": missing}

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    if existing:
        certificate = x509.load_pem_x509_certificate(paths["server_cert"].read_bytes())
        try:
            san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound as exc:
            raise ValueError("Existing server certificate has no SubjectAlternativeName.") from exc
        present_names = {name.lower() for name in san.get_values_for_type(x509.DNSName)}
        absent = set(names) - present_names
        if address not in san.get_values_for_type(x509.IPAddress) or absent:
            raise ValueError("Existing server certificate SAN does not cover the requested LAN IP and hostnames.")
        return {"status": "reused", **result, "missing": []}

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, display_name)])
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else str(address))])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                    key_encipherment=False, data_encipherment=False,
                                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                                    encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server = (
        x509.CertificateBuilder().subject_name(server_name).issuer_name(ca.subject)
        .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                    key_encipherment=True, data_encipherment=False,
                                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                                    encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([*(x509.DNSName(name) for name in names),
                                                   x509.IPAddress(address)]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    public_der = ca.public_bytes(serialization.Encoding.DER)
    profile_uuid, certificate_uuid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    profile = {
        "PayloadType": "Configuration", "PayloadVersion": 1, "PayloadUUID": profile_uuid,
        "PayloadIdentifier": "local.deathnote." + profile_uuid.lower(),
        "PayloadDisplayName": display_name, "PayloadOrganization": "DeathNote",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [{
            "PayloadType": "com.apple.security.root", "PayloadVersion": 1,
            "PayloadUUID": certificate_uuid,
            "PayloadIdentifier": "local.deathnote.ca." + certificate_uuid.lower(),
            "PayloadDisplayName": display_name,
            "PayloadCertificateFileName": _FILES["ca_der"], "PayloadContent": public_der,
        }],
    }
    content = {
        "ca_key": ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()),
        "ca_pem": ca.public_bytes(serialization.Encoding.PEM), "ca_der": public_der,
        "server_cert": server.public_bytes(serialization.Encoding.PEM),
        "server_key": server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()),
        "mobileconfig": plistlib.dumps(profile, fmt=plistlib.FMT_XML, sort_keys=False),
    }
    state.mkdir(parents=True, exist_ok=True)
    for key, data in content.items():
        _write_new(paths[key], data, private=key in ("ca_key", "server_key"))
    return {"status": "created", **result, "missing": []}


def load_server_context(cert_file: str | Path, key_file: str | Path,
                        chain_file: str | Path | None = None) -> ssl.SSLContext:
    """Load the operator-supplied game TLS files."""
    certificate = Path(cert_file)
    key = Path(key_file)
    chain = Path(chain_file) if chain_file is not None else None
    missing = [f"{label} ({path})" for label, path in (
        ("certificate", certificate), ("private key", key), ("certificate chain", chain)
    ) if path is not None and not path.is_file()]
    if missing:
        raise FileNotFoundError("Game TLS files are missing (" + ", ".join(missing) + ").")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["h2"])
    if chain is None:
        context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    else:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="deathnote-public-chain-") as directory:
            fullchain = Path(directory) / "server-chain.pem"
            fullchain.write_bytes(certificate.read_bytes() + b"\n" + chain.read_bytes())
            context.load_cert_chain(certfile=str(fullchain), keyfile=str(key))
    return context

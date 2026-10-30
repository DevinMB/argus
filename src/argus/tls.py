"""HTTPS by default (design decision 2), with nothing to set up.

On first start Argus makes a self-signed certificate in its data volume and
logs its SHA-256 fingerprint. Companions pin that fingerprint when they enroll,
so a self-signed certificate is safe for them; browsers show a warning once.

ARGUS_TLS=off serves plain HTTP (behind a proxy that does HTTPS), and
ARGUS_TLS_CERT plus ARGUS_TLS_KEY use your own certificate instead.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

YEARS = 10


def fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def ensure_self_signed(directory: Path) -> tuple[Path, Path]:
    """The certificate and key, made once. The key file is readable by its owner only."""
    cert_path, key_path = directory / "tls.crt", directory / "tls.key"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName(host), x509.DNSName("localhost"),
                                     x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Argus on {host}")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * YEARS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def files(data_dir: Path) -> tuple[Path, Path] | None:
    """Which certificate to serve, or None for plain HTTP."""
    if os.environ.get("ARGUS_TLS", "on").lower() in ("off", "0", "false", "no"):
        return None
    cert, key = os.environ.get("ARGUS_TLS_CERT"), os.environ.get("ARGUS_TLS_KEY")
    if cert and key:
        return Path(cert), Path(key)
    return ensure_self_signed(data_dir)

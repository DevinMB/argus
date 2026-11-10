"""Argus's own backup: one encrypted file with everything a new install needs to
carry on as this one (design decision 10).

The data key in the volume is what unlocks every secret; lose the volume and
the secrets are gone. The export holds the settings, the secrets (opened,
then sealed again under the export's passphrase), the enrolled hosts, the
paused checks, and Argus's TLS certificate and key, so companions that pinned
it keep talking to the restored install. Check history and sessions are left out.

Taking one needs the admin password again and a passphrase of 12 characters
or more; the file is AES-256-GCM under a scrypt key from that passphrase.
Importing replaces this install's settings, secrets and hosts.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from argus.settings import Settings

FORMAT = "argus-export-v1"
TAKEN = "export.taken_at"
N, R, P = 2**15, 8, 1
COMPANION_COLS = ("id", "name", "token_hash", "enrolled", "address", "read_only")
# Never carried over: what only made sense on the old install.
SKIP = {"github.manifest_state", "github.connect_error", TAKEN}


class BackupError(Exception):
    pass


def _key(passphrase: str, salt: bytes, n: int = N, r: int = R, p: int = P) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def export(settings: Settings, data_dir: Path, passphrase: str) -> bytes:
    db = settings.db
    plain = {
        "settings": {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM settings")
                     if k not in SKIP and not k.startswith(("patch.request.", "adopt.request."))},
        "secrets": {k: settings.secret(k) for (k,) in db.execute("SELECT key FROM secrets")},
        "companions": [dict(zip((*COMPANION_COLS, "ignored"), row, strict=True))
                       for row in db.execute(
                           f"SELECT {', '.join(COMPANION_COLS)}, ignored FROM companions")],
        "pauses": [dict(r) for r in db.execute("SELECT * FROM pauses")],
        "tls": {name: (data_dir / name).read_text() for name in ("tls.crt", "tls.key")
                if (data_dir / name).is_file()},
    }
    salt, nonce = os.urandom(16), os.urandom(12)
    sealed = AESGCM(_key(passphrase, salt)).encrypt(
        nonce, json.dumps(plain).encode(), FORMAT.encode())
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return json.dumps({"format": FORMAT, "kdf": {"name": "scrypt", "n": N, "r": R, "p": P,
                                                 "salt": b64(salt)},
                       "nonce": b64(nonce), "data": b64(sealed)}, indent=1).encode()


def restore(settings: Settings, data_dir: Path, blob: bytes, passphrase: str) -> dict:
    try:
        f = json.loads(blob)
        assert f["format"] == FORMAT and f["kdf"]["name"] == "scrypt"
        kdf = f["kdf"]
        key = _key(passphrase, base64.b64decode(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
        plain = json.loads(AESGCM(key).decrypt(base64.b64decode(f["nonce"]),
                                               base64.b64decode(f["data"]), FORMAT.encode()))
    except InvalidTag:
        raise BackupError("wrong passphrase, or the file was changed") from None
    except (ValueError, KeyError, TypeError, AssertionError):
        raise BackupError("this is not an Argus export") from None
    db = settings.db
    with db:
        db.execute("DELETE FROM settings")
        db.execute("DELETE FROM secrets")
        db.execute("DELETE FROM companions")
        db.execute("DELETE FROM sessions")
    for k, v in plain["settings"].items():
        settings.set(k, v)
    for k, v in plain["secrets"].items():
        settings.set_secret(k, v)
    with db:
        for c in plain["companions"]:
            db.execute(f"INSERT INTO companions ({', '.join(COMPANION_COLS)}) VALUES "
                       f"({', '.join('?' * len(COMPANION_COLS))})",
                       tuple(c[k] for k in COMPANION_COLS))
            if "ignored" in c:  # older exports have none
                db.execute("UPDATE companions SET ignored = ? WHERE id = ?",
                           (c["ignored"], c["id"]))
        # Older exports have none.
        if "pauses" in plain:
            db.execute("DELETE FROM pauses")
        for p in plain.get("pauses", []):
            db.execute("INSERT INTO pauses VALUES (?, ?, ?, ?, ?)",
                       (p["name"], p["reason"], p["since"], p["until"], p["seen_down"]))
    restart = False
    for name, text in plain["tls"].items():
        path = data_dir / name
        if not path.exists() or path.read_text() != text:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(text)
            restart = True
    return {"hosts": len(plain["companions"]), "secrets": len(plain["secrets"]),
            "restart": restart}

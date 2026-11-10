"""Settings made in the UI, in SQLite next to the state, and the secrets among them.

Secrets (a bot token, a dead-man URL) are encrypted with AES-256-GCM under a key
Argus makes on first start and keeps in the data volume as its own file, so a
copy of the database alone reveals nothing. Each secret is bound to its name, so
one cannot be swapped into another's slot. The API never returns a secret: only
whether it is set.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, sealed TEXT NOT NULL);
"""


def load_key(path: Path) -> bytes:
    """The data key: read it, or make it once. Only the owner can read the file."""
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = AESGCM.generate_key(bit_length=256)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process made it first
            return load_key(path)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    if len(key) != 32:
        raise ValueError(f"{path} is not an Argus key (expected 32 bytes)")
    return key


class Settings:
    def __init__(self, db: sqlite3.Connection, key: bytes) -> None:
        self.db = db
        self._aead = AESGCM(key)
        self.db.executescript(SCHEMA)

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                                (key, json.dumps(value)))

    def secret(self, key: str) -> str | None:
        row = self.db.execute("SELECT sealed FROM secrets WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        raw = base64.b64decode(row[0])
        return self._aead.decrypt(raw[:12], raw[12:], key.encode()).decode()

    def has_secret(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM secrets WHERE key = ?", (key,)).fetchone() is not None

    def set_secret(self, key: str, value: str | None) -> None:
        """Replace a secret, or forget it with None. There is no way to read one back
        through the API, so replacing is the only edit."""
        with self.db:
            if not value:
                self.db.execute("DELETE FROM secrets WHERE key = ?", (key,))
                return
            nonce = os.urandom(12)
            sealed = nonce + self._aead.encrypt(nonce, value.encode(), key.encode())
            self.db.execute("INSERT OR REPLACE INTO secrets VALUES (?, ?)",
                            (key, base64.b64encode(sealed).decode()))

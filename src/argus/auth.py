"""One admin account, for settings (and, with one switch, for viewing too).

- A fresh install has no password. The first visitor must type a setup code
  that Argus prints in its log, so nobody else on the network can claim it.
- Passwords are hashed with argon2id.
- A session is a random token in an HttpOnly, SameSite=Strict cookie; only its
  hash is stored. Each session has a CSRF token that every write must echo in
  the X-CSRF-Token header.
- Failed logins are rate-limited per address and overall.
- Forgot the password: `python -m argus reset-login` in the container clears it
  and every session; the next page load prints a new setup code.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from argus.settings import Settings

log = logging.getLogger("argus")

COOKIE = "argus_session"
SESSION_DAYS = 14
MIN_PASSWORD = 10
FAILS_PER_ADDRESS = 5  # in WINDOW, then 429
FAILS_OVERALL = 20
WINDOW = 15 * 60
HASH_KEY = "admin.password_hash"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL
);
"""


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf: str
    expires: float


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db: sqlite3.Connection, settings: Settings,
                 clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.hasher = PasswordHasher()  # argon2id, the library's (RFC 9106) defaults
        self.db.executescript(SCHEMA)
        self._code: str | None = None
        self._fails: dict[str, deque[float]] = {}
        self._all_fails: deque[float] = deque()

    # --- first start ---

    def needs_setup(self) -> bool:
        return self.settings.get(HASH_KEY) is None

    def setup_code(self) -> str | None:
        """The code that proves the first visitor can read the log. Made once per
        process while no password is set, and logged each time it is made."""
        if not self.needs_setup():
            self._code = None
            return None
        if self._code is None:
            raw = secrets.token_hex(6)
            self._code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
            log.warning("No admin password yet. Setup code: %s (open Argus in a browser"
                        " and enter it to set the password)", self._code)
        return self._code

    def setup(self, code: str, password: str, address: str) -> str:
        if not self.needs_setup():
            raise AuthError(409, "the admin password is already set")
        self._throttle(address)
        expected = self.setup_code() or ""
        if not hmac.compare_digest(code.strip().lower().encode(), expected.encode()):
            self._failed(address)
            raise AuthError(403, "wrong setup code; it is in the Argus log")
        self._set_password(password)
        self._code = None
        log.info("admin password set")
        return self._new_session()

    # --- sign in and out ---

    def login(self, password: str, address: str) -> str:
        self._throttle(address)
        stored = self.settings.get(HASH_KEY)
        if stored is None:
            raise AuthError(409, "no admin password yet: set one with the setup code")
        try:
            self.hasher.verify(stored, password)
        except (VerificationError, InvalidHashError):
            self._failed(address)
            raise AuthError(403, "wrong password") from None
        if self.hasher.check_needs_rehash(stored):
            self.settings.set(HASH_KEY, self.hasher.hash(password))
        return self._new_session()

    def session(self, token: str | None) -> Session | None:
        if not token:
            return None
        row = self.db.execute("SELECT token_hash, csrf, expires FROM sessions"
                              " WHERE token_hash = ?", (_digest(token),)).fetchone()
        if row is None or row[2] < self.clock():
            return None
        return Session(*row)

    def logout(self, token: str | None) -> None:
        if token:
            with self.db:
                self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))

    def change_password(self, token: str, current: str, new: str, address: str) -> str:
        """Signs out every other session; returns a fresh one for this browser."""
        self._throttle(address)
        try:
            self.hasher.verify(self.settings.get(HASH_KEY), current)
        except (VerificationError, InvalidHashError):
            self._failed(address)
            raise AuthError(403, "the current password is wrong") from None
        self._set_password(new)
        with self.db:
            self.db.execute("DELETE FROM sessions")
        return self._new_session()

    def reset(self) -> None:
        """Forget the password and every session (the CLI's reset-login)."""
        self.settings.set(HASH_KEY, None)
        with self.db:
            self.db.execute("DELETE FROM sessions")

    # --- internals ---

    def _set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AuthError(422, f"use at least {MIN_PASSWORD} characters")
        if len(password) > 1024:
            raise AuthError(422, "that password is too long")
        self.settings.set(HASH_KEY, self.hasher.hash(password))

    def _new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                            (_digest(token), secrets.token_urlsafe(24), now,
                             now + SESSION_DAYS * 86400))
        return token

    def _recent(self, q: deque[float]) -> deque[float]:
        while q and q[0] < self.clock() - WINDOW:
            q.popleft()
        return q

    def _throttle(self, address: str) -> None:
        if (len(self._recent(self._fails.get(address, deque()))) >= FAILS_PER_ADDRESS
                or len(self._recent(self._all_fails)) >= FAILS_OVERALL):
            raise AuthError(429, "too many failed attempts; try again in 15 minutes")

    def _failed(self, address: str) -> None:
        now = self.clock()
        self._fails.setdefault(address, deque()).append(now)
        self._all_fails.append(now)
        if len(self._fails) > 10_000:  # bound memory under a scan
            self._fails.clear()

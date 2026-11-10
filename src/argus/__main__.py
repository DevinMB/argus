"""python -m argus [serve | reset-login | healthcheck]"""

import logging
import os
import sqlite3
import ssl
import sys
import urllib.request
from pathlib import Path

DB = Path(os.environ.get("ARGUS_DB", "/data/argus.db"))
PORT = int(os.environ.get("ARGUS_PORT", "8080"))


def serve() -> None:
    import uvicorn

    from argus import tls
    from argus.app import create_app

    # The database holds the password hash and sealed secrets: owner-only files.
    os.umask(0o077)
    log = logging.getLogger("argus")
    cert = tls.files(DB.parent)
    pin = cert and tls.spki_pin(cert[0])
    if cert:
        log.info("HTTPS on port %d; certificate fingerprint (SHA-256) %s; key pin %s",
                 PORT, tls.fingerprint(cert[0]), pin)
    else:
        log.info("plain HTTP on port %d (ARGUS_TLS=off)", PORT)
    uvicorn.run(
        create_app(tls_pin=pin or None),
        host="0.0.0.0",  # noqa: S104 (a container's port, published by its owner)
        port=PORT,
        access_log=False,
        log_config=None,
        ssl_certfile=cert and str(cert[0]),
        ssl_keyfile=cert and str(cert[1]),
    )


def reset_login() -> None:
    """Forget the admin password and sign everyone out. Argus prints a new setup
    code the next time the page is opened."""
    from argus.auth import HASH_KEY

    db = sqlite3.connect(DB)
    with db:
        db.execute("DELETE FROM settings WHERE key = ?", (HASH_KEY,))
        db.execute("DELETE FROM sessions")
    print("Login reset. Open Argus and use the setup code from its log to set a new password.")


def healthcheck() -> None:
    """For the container's HEALTHCHECK: the local /healthz, over HTTPS when it is on."""
    tls_on = os.environ.get("ARGUS_TLS", "on").lower() not in ("off", "0", "false", "no")
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # our own, on loopback
    scheme = "https" if tls_on else "http"
    urllib.request.urlopen(f"{scheme}://127.0.0.1:{PORT}/healthz", timeout=4,  # noqa: S310
                           context=ctx if tls_on else None)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs URLs, and some hold tokens
COMMANDS = {"serve": serve, "reset-login": reset_login, "healthcheck": healthcheck}
command = sys.argv[1] if len(sys.argv) > 1 else "serve"
if command not in COMMANDS:
    sys.exit(f"unknown command {command}; use one of {', '.join(COMMANDS)}")
COMMANDS[command]()

import logging
import os

import uvicorn

from argus.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
uvicorn.run(
    create_app(),
    host="0.0.0.0",
    port=int(os.environ.get("ARGUS_PORT", "8080")),
    access_log=False,
    log_config=None,
)

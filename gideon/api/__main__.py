"""Run the GIDEON API service with plain uvicorn."""

import logging
from typing import Final

import uvicorn

from .app import create_app
from .settings import load_settings

# exempt: graceful-shutdown bound below Compose's ten-second stop grace period.
UVICORN_GRACEFUL_SHUTDOWN_SECONDS: Final[int] = 5


def main() -> None:
    """Load startup settings and serve on every container interface."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        access_log=False,
        timeout_graceful_shutdown=UVICORN_GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()

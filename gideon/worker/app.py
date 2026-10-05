"""Procrastinate application and explicit worker options."""

from typing import cast

import procrastinate
from procrastinate.app import WorkerOptions

from .settings import Settings, connection_kwargs
from .tasks import STALLED_SECONDS, register_tasks


def build_app(settings: Settings) -> procrastinate.App:
    """Create the queue application with credentials resolved per connection."""

    pool_size = settings.concurrency + 1
    connector = procrastinate.PsycopgConnector(
        kwargs=lambda: connection_kwargs(settings),
        min_size=pool_size,
        max_size=pool_size,
    )
    # exempt: the heartbeat and stall window use the queue's documented pair.
    options = cast("WorkerOptions", {
        "queues": None,
        "concurrency": settings.concurrency,
        "wait": True,
        "listen_notify": True,
        "delete_jobs": "never",
        "update_heartbeat_interval": 10.0,
        "stalled_worker_timeout": float(STALLED_SECONDS),
        "shutdown_graceful_timeout": None,
    })
    app = procrastinate.App(
        connector=connector,
        worker_defaults=options,
    )
    register_tasks(app)
    return app

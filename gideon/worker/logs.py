"""Content-free logging for queue events."""

import logging
import math
import re
from collections.abc import Mapping
from typing import TextIO

_SAFE_NAME = re.compile(r"[A-Za-z0-9_.:-]+\Z")


def _name(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_NAME.fullmatch(value):
        return value
    return None


class QueueLogFilter(logging.Filter):
    """Replace queue messages before the stream handler formats them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "procrastinate" and not record.name.startswith("procrastinate."):
            return True
        # The queue logs a job's outcome before it records the job finished, so
        # a filter that raised there would leave the job doing for ever.
        try:
            message = self._content_free(record)
        except Exception:  # noqa: BLE001 - a log line never stops a job.
            message = "queue_event"
        record.msg = message
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True

    def _content_free(self, record: logging.LogRecord) -> str:
        parts: list[str] = []
        action = _name(getattr(record, "action", None))
        if action is not None:
            parts.append(f"action={action}")

        job = getattr(record, "job", None)
        worker = getattr(record, "worker", None)
        job_id = job.get("id") if isinstance(job, Mapping) else None
        if job_id is None and isinstance(worker, Mapping):
            job_id = worker.get("job_id")
        if isinstance(job_id, int) and not isinstance(job_id, bool):
            parts.append(f"job_id={job_id}")

        if isinstance(job, Mapping):
            for field in ("task_name", "queue", "status"):
                value = _name(job.get(field))
                if value is not None:
                    parts.append(f"{field}={value}")
        status = _name(getattr(record, "status", None))
        if status is not None and not isinstance(job, Mapping):
            parts.append(f"status={status}")

        duration = getattr(record, "duration", None)
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and duration >= 0
        ):
            parts.append(f"duration={duration:.3f}")

        # The queue passes exc_info=False on a job without an exception.
        exc_info = record.exc_info
        if isinstance(exc_info, tuple) and exc_info[1] is not None:
            parts.append(f"exception={type(exc_info[1]).__name__}")

        return " ".join(parts) or "queue_event"


def install_handler(stream: TextIO | None = None) -> logging.Handler:
    """Install one root stream handler with filtering on that handler."""

    handler = logging.StreamHandler(stream)
    handler.addFilter(QueueLogFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    return handler

"""Read Grafana's page alerts and format the needs-attention block."""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from gideon.host import grafana, stack
from gideon.host.report import Problem, one_line
from gideon.status.glance import age_text

PAGE_CLASS: Final[str] = "page"
HEARTBEAT_LABEL: Final[str] = "heartbeat"
_GRAFANA_FIX: Final[str] = stack.logs_fix("/etc/gideon/rendered", "grafana")
_LONG_FRACTION: Final[re.Pattern[str]] = re.compile(r"(\.\d{6})\d+(?=Z|[+-]\d{2}:\d{2}$)")


@dataclass(frozen=True, slots=True)
class Page:
    """One firing page and the operator-facing context attached to it."""

    title: str
    summary: str
    runbook: str
    started_at: datetime | None
    suppressed: bool


def _started_at(value: str) -> datetime | None:
    normalized = _LONG_FRACTION.sub(r"\1", value)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def pages(alerts: Iterable[grafana.Alert]) -> tuple[Page, ...]:
    """Select page alerts, excluding the heartbeat and ordering them stably."""

    result = tuple(
        Page(
            title=alert.labels.get("alertname", ""),
            summary=alert.annotations.get("summary", ""),
            runbook=alert.annotations.get("runbook", ""),
            started_at=_started_at(alert.starts_at),
            suppressed=alert.state == "suppressed",
        )
        for alert in alerts
        if alert.labels.get("class") == PAGE_CLASS
        and alert.labels.get(HEARTBEAT_LABEL, "").lower() != "true"
    )
    return tuple(
        sorted(
            result,
            key=lambda page: (
                page.started_at is None,
                page.started_at or datetime.max.replace(tzinfo=UTC),
                page.title,
            ),
        )
    )


def page_line(page: Page, now: datetime) -> str:
    """Render a page with its summary, runbook, and firing age."""

    parts = [part for part in (page.title, page.summary) if part]
    detail = ": ".join(parts)
    if page.runbook:
        detail += f" — {page.runbook}"
    age = "unknown" if page.started_at is None else age_text(now, page.started_at)
    marker = "silenced, " if page.suppressed else ""
    return one_line(f"{detail} ({marker}firing {age})")


def read_pages(connect: Callable[[], grafana.Client]) -> tuple[Page, ...] | Problem:
    """Read and select pages, converting Grafana and transport failures."""

    try:
        alerts = connect().get_alerts()
    except grafana.GrafanaError as exc:
        return Problem(exc.problem, _GRAFANA_FIX)
    except OSError:
        return Problem("Grafana request failed.", _GRAFANA_FIX)
    return pages(alerts)

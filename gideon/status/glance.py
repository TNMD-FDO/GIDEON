"""Read and format the independent facts in the at-a-glance block."""

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import gideon
from gideon.evaluation import record
from gideon.host import backupset, report, stack, tls
from gideon.host.checks import format_gb
from gideon.host.checks.capacity import DATA_DF_ARGV, parse_size_and_available
from gideon.host.report import Problem, one_line
from gideon.host.sysio import Host, PathLike
from gideon.improvement import sections


def _retry_fix(path: str) -> str:
    return f"Run {report.command(path)}, then retry."


def _data_fix() -> str:
    return f"Ensure /data is mounted and readable, then run {report.command('status')}."


_RECORDS_SQL: Final[str] = """\
SELECT DISTINCT ON (kind)
    kind,
    to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    CASE
        WHEN kind = 'backup_push' THEN detail->>'newest_set'
        WHEN kind = 'backup_drill' THEN detail->>'result'
    END
FROM audit_log
WHERE kind IN ('backup_push', 'backup_drill')
ORDER BY kind, at DESC
"""


@dataclass(frozen=True, slots=True)
class Fact:
    """One at-a-glance label, its detail, and an optional corrective command."""

    name: str
    detail: str
    fix: str


def age_text(now: datetime, then: datetime) -> str:
    """Render a non-negative age as minutes, hours, or days and hours."""

    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    days, remaining = divmod(seconds, 86_400)
    return f"{days}d {remaining // 3600}h"


def failure(name: str, problem: str, fix: str) -> Fact:
    """Render a reader's failure as one could-not-read fact with its fix."""

    return Fact(name, f"could not read — {one_line(problem)}", fix)


def version_fact() -> Fact:
    """Report the version of the checkout running this command."""

    return Fact("version", gideon.__version__, "")


def service_counts(
    host: Host, rendered_dir: PathLike
) -> tuple[tuple[str, ...], int, tuple[str, ...]] | Problem:
    """Count declared services that are running and name those missing."""

    declared = stack.declared_services(host, rendered_dir)
    if isinstance(declared, Problem):
        return declared
    running = stack.running_services(host, rendered_dir)
    if running is None:
        return Problem("Compose service status is unavailable.", _retry_fix("apply"))
    active = set(running)
    missing = tuple(service for service in declared if service not in active)
    return declared, len(declared) - len(missing), missing


def services_fact(host: Host, rendered_dir: PathLike) -> Fact:
    """Compare running Compose services with those declared in rendered state."""

    counts = service_counts(host, rendered_dir)
    if isinstance(counts, Problem):
        return failure("services", counts.problem, counts.fix)
    declared, count, missing = counts
    detail = f"{count} of {len(declared)} up"
    if missing:
        detail += f"; not running: {', '.join(missing)}"
    return Fact("services", detail, "")


def backup_set_fact(host: Host, staging: PathLike, now: datetime) -> Fact:
    """Report the age and label of the newest complete local backup set."""

    try:
        refs = backupset.list_sets(host, staging)
    except (OSError, subprocess.SubprocessError) as exc:
        return failure("backup set", str(exc), _retry_fix("backup run"))
    selected = backupset.select_set(refs)
    if isinstance(selected, Problem):
        return Fact("backup set", "none yet", _retry_fix("backup run"))
    if selected.finished is None:
        return Fact("backup set", "none yet", _retry_fix("backup run"))
    return Fact(
        "backup set",
        f"{selected.label}, {age_text(now, selected.finished)} ago",
        "",
    )


def _record_time(value: str) -> datetime | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _record_fact(
    name: str,
    kind: str,
    row: tuple[datetime, str] | None,
    now: datetime,
    fix: str,
) -> Fact:
    if row is None:
        return Fact(name, "none yet", fix)
    recorded_at, detail = row
    age = f"{age_text(now, recorded_at)} ago"
    if not detail:
        return Fact(name, age, "")
    if kind == "backup_push":
        return Fact(name, f"{detail} pushed {age}", "")
    return Fact(name, f"{detail}, {age}", "")


def record_facts(
    host: Host, rendered_dir: PathLike, now: datetime
) -> tuple[Fact, Fact]:
    """Read the newest verified push and drill rows in one metrics query."""

    def failed(problem: str) -> tuple[Fact, Fact]:
        fix = stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE)
        return failure("off-box push", problem, fix), failure("drill", problem, fix)

    result = sections.read_rows(host, rendered_dir, _RECORDS_SQL)
    if isinstance(result, Problem):
        return failed(result.problem)
    push: tuple[datetime, str] | None = None
    drill: tuple[datetime, str] | None = None
    for line in result:
        fields = line.split("|", 2)
        recorded_at = _record_time(fields[1]) if len(fields) == 3 else None
        if recorded_at is None or fields[0] not in {"backup_push", "backup_drill"}:
            return failed("metrics reader returned an invalid audit row.")
        row = (recorded_at, fields[2])
        if fields[0] == "backup_push":
            push = row
        else:
            drill = row
    return (
        _record_fact("off-box push", "backup_push", push, now, _retry_fix("backup push")),
        _record_fact("drill", "backup_drill", drill, now, _retry_fix("backup drill")),
    )


def tls_fact(host: Host, hostname: str, now: datetime) -> Fact:
    """Report the served ingress certificate's remaining days and expiry date."""

    try:
        expiry = tls.served_expiry(
            host,
            connect="127.0.0.1:443",
            hostname=hostname,
            cafile=tls.CA_PATH,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return failure(
            "TLS certificate",
            str(exc),
            stack.logs_fix("/etc/gideon/rendered", "caddy"),
        )
    if isinstance(expiry, Problem):
        return failure("TLS certificate", expiry.problem, expiry.fix)
    return Fact(
        "TLS certificate",
        f"{(expiry - now).days} days left, expires {expiry.date().isoformat()}",
        "",
    )


def data_fact(host: Host) -> Fact:
    """Report /data's free bytes and percentage using the preflight reader."""

    try:
        result = host.run(DATA_DF_ARGV)
    except (OSError, subprocess.SubprocessError) as exc:
        return failure("/data", str(exc), _data_fix())
    if result.returncode != 0:
        return failure("/data", "df could not measure the data volume.", _data_fix())
    measurement = parse_size_and_available(result.stdout)
    if measurement is None:
        return failure("/data", "df returned invalid size and free-space values.", _data_fix())
    size, available = measurement
    if size == 0:
        return failure("/data", "df returned a zero-sized data volume.", _data_fix())
    percent = available * 100 / size
    return Fact(
        "/data", f"{format_gb(available)} free of {format_gb(size)} ({percent:.1f}%)", ""
    )


def fact_line(fact: Fact) -> str:
    """Render one fact, appending its fix only when one is available."""

    line = f"{fact.name}: {fact.detail}"
    if fact.fix:
        line += f" Fix: {fact.fix}"
    return one_line(line)

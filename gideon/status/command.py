"""Assemble and print the read-only box status report."""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon.host import backupset, grafana, nogpu, secrets, site, tls
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import Problem, one_line, refusal
from gideon.host.sysio import Host, PathLike, RealHost
from gideon.improvement import proposals, triggers
from gideon.improvement import sections as improvement_sections
from gideon.status import attention, glance

_SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")
_RENDERED_DIR: Final[Path] = Path("/etc/gideon/rendered")
_STAGING: Final[str] = backupset.STAGING
_ROOT_FIX: Final[str] = "Run sudo python3 -m gideon status."
_SITE_FIX: Final[str] = "Correct the site file, then run sudo python3 -m gideon status."
_APPLY_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
_REGISTRY_FIX: Final[str] = (
    "Restore config/triggers.yaml from the release checkout, then retry."
)
_HEADERS: Final[tuple[str, str, str]] = (
    "needs attention",
    "waiting on you",
    "at a glance",
)
_CLEAR_LINE: Final[str] = "status: nothing needs attention"
_ATTENTION_LINE: Final[str] = "status: {count} need attention"
_UNKNOWN_LINE: Final[str] = "status: could not check"
_REGISTRY_PROBLEM: Final[str] = "trigger registry could not be loaded"


def _print_line(value: object) -> None:
    print(one_line(value))


def _waiting_lines(
    host: Host,
    *,
    checkout_root: Path,
    rendered_dir: Path,
    triggers_path: PathLike | None,
    registered: Sequence[improvement_sections.Section] | None,
) -> tuple[str, ...]:
    registry_path = (
        checkout_root / "config/triggers.yaml"
        if triggers_path is None
        else triggers_path
    )
    loaded = triggers.load_trigger_registry(registry_path, host=host)
    if loaded.errors or loaded.registry is None:
        fix = loaded.errors[0].fix if loaded.errors else _REGISTRY_FIX
        return (f"could not check — {_REGISTRY_PROBLEM} Fix: {fix}",)

    context = improvement_sections.Context(
        host=host,
        checkout_root=checkout_root,
        rendered_dir=rendered_dir,
        registry=loaded.registry,
        build_box=nogpu.is_build_box(host),
        query=lambda sql: improvement_sections.read_rows(host, rendered_dir, sql),
    )
    selected = proposals.SECTIONS if registered is None else tuple(registered)
    lines: list[str] = []
    for section in selected:
        if section.scope != "office":
            continue
        result = section.render(context)
        if isinstance(result, Problem):
            lines.append(f"{section.name}: {result.problem} Fix: {result.fix}")
            continue
        lines.extend(
            f"{row.name}: {row.detail}"
            for row in result.rows
            if row.state == "fired"
        )
    return tuple(lines) if lines else ("none",)


def _attention_result(
    host: Host,
    hostname: str,
    client_factory: Callable[..., grafana.Client] | None,
) -> tuple[attention.Page, ...] | Problem:
    secret = secrets.read_secret(host, "grafana_admin_password")
    if not secret.ok or secret.value is None:
        return Problem(
            secret.problem or "Grafana administrator secret is unavailable.",
            _APPLY_FIX,
        )
    make_client = client_factory or grafana.ingress_client_factory(
        hostname, ca_path=tls.CA_PATH
    )
    credential = (GRAFANA_ADMIN_USER, secret.value)
    return attention.read_pages(lambda: make_client(credential=credential))


def run_status(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    site_path: PathLike = _SITE_PATH,
    rendered_dir: PathLike = _RENDERED_DIR,
    staging: PathLike = _STAGING,
    checkout_root: PathLike | None = None,
    triggers_path: PathLike | None = None,
    client_factory: Callable[..., grafana.Client] | None = None,
    sections: Sequence[improvement_sections.Section] | None = None,
    now: datetime | None = None,
) -> int:
    """Print status blocks and return the attention/read-failure exit code."""

    del args
    io = RealHost() if host is None else host
    if io.geteuid() != 0:
        print(refusal("status", "root privileges are required.", _ROOT_FIX), file=sys.stderr)
        return 2

    loaded_site = site.load_site(Path(site_path), host=io)
    if loaded_site.errors or loaded_site.config is None:
        problem = site.render_errors(loaded_site.errors) or "site file could not be loaded."
        print(refusal("status", problem, _SITE_FIX), file=sys.stderr)
        return 2
    config = loaded_site.config
    rendered = Path(rendered_dir)
    checkout = (
        Path(__file__).parents[2]
        if checkout_root is None
        else Path(checkout_root)
    )
    current = datetime.now(UTC) if now is None else now

    _print_line(_HEADERS[0])
    alert_result = _attention_result(io, config.hostname, client_factory)
    attention_failed = isinstance(alert_result, Problem)
    if isinstance(alert_result, Problem):
        _print_line(f"could not check — {alert_result.problem} Fix: {alert_result.fix}")
        firing = 0
    elif alert_result:
        for page in alert_result:
            _print_line(attention.page_line(page, current))
        firing = sum(not page.suppressed for page in alert_result)
    else:
        _print_line("none")
        firing = 0

    _print_line(_HEADERS[1])
    for line in _waiting_lines(
        io,
        checkout_root=checkout,
        rendered_dir=rendered,
        triggers_path=triggers_path,
        registered=sections,
    ):
        _print_line(line)

    _print_line(_HEADERS[2])
    facts = (
        glance.version_fact(),
        glance.services_fact(io, rendered),
        glance.backup_set_fact(io, staging, current),
        *glance.record_facts(io, rendered, current),
        glance.tls_fact(io, config.hostname, current),
        glance.data_fact(io),
    )
    for fact in facts:
        _print_line(glance.fact_line(fact))

    if attention_failed:
        _print_line(_UNKNOWN_LINE)
        return 2
    if firing:
        _print_line(_ATTENTION_LINE.format(count=firing))
        return 1
    _print_line(_CLEAR_LINE)
    return 0

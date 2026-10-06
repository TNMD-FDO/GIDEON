"""Assemble and print the read-only box status report."""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon.host import backupset, grafana, nogpu, owui, report, secrets, site, tls
from gideon.host.render.ci import CI_ROOT
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import Problem, one_line, refusal
from gideon.host.sysio import Host, PathLike, RealHost
from gideon.improvement import owuifeedback, proposals, triggers
from gideon.improvement import sections as improvement_sections
from gideon.status import attention, developer, fired, glance

_SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")
_RENDERED_DIR: Final[Path] = Path("/etc/gideon/rendered")
_STAGING: Final[str] = backupset.STAGING
_REGISTRY_FIX: Final[str] = (
    "Restore config/triggers.yaml from the release checkout, then retry."
)
_HEADERS: Final[tuple[str, str, str, str]] = (
    "needs attention",
    "waiting on you",
    "at a glance",
    "developer",
)
_CLEAR_LINE: Final[str] = "status: nothing needs attention"
_ATTENTION_LINE: Final[str] = "status: {count} need attention"
_UNKNOWN_LINE: Final[str] = "status: could not check"
_REGISTRY_PROBLEM: Final[str] = "trigger registry could not be loaded"


def _print_line(value: object) -> None:
    print(one_line(value))


def _status_context(
    host: Host,
    *,
    checkout_root: Path,
    rendered_dir: Path,
    triggers_path: PathLike | None,
    site_path: PathLike,
    owui_client_factory: Callable[..., owui.Client] | None,
    now: Callable[[], float],
    build_box: bool,
) -> improvement_sections.Context | str:
    registry_path = (
        checkout_root / "config/triggers.yaml"
        if triggers_path is None
        else triggers_path
    )
    loaded = triggers.load_trigger_registry(registry_path, host=host)
    if loaded.errors or loaded.registry is None:
        fix = loaded.errors[0].fix if loaded.errors else _REGISTRY_FIX
        return f"could not check — {_REGISTRY_PROBLEM} Fix: {fix}"

    return improvement_sections.Context(
        host=host,
        checkout_root=checkout_root,
        rendered_dir=rendered_dir,
        registry=loaded.registry,
        build_box=build_box,
        query=lambda sql: improvement_sections.read_rows(host, rendered_dir, sql),
        feedback=improvement_sections.once(
            owuifeedback.source(
                host,
                site_path,
                owui_client_factory,
            ).read
        ),
        now=now,
    )


def _attention_result(
    host: Host,
    hostname: str,
    client_factory: Callable[..., grafana.Client] | None,
) -> tuple[attention.Page, ...] | Problem:
    secret = secrets.read_secret(host, "grafana_admin_password")
    if not secret.ok or secret.value is None:
        return Problem(
            secret.problem or "Grafana administrator secret is unavailable.",
            f"Run {report.command('apply')}, then retry.",
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
    ci_root: PathLike = CI_ROOT,
    staging: PathLike = _STAGING,
    checkout_root: PathLike | None = None,
    triggers_path: PathLike | None = None,
    client_factory: Callable[..., grafana.Client] | None = None,
    owui_client_factory: Callable[..., owui.Client] | None = None,
    sections: Sequence[improvement_sections.Section] | None = None,
    now: datetime | None = None,
) -> int:
    """Print status blocks and return the attention/read-failure exit code."""

    del args
    io = RealHost() if host is None else host
    if io.geteuid() != 0:
        print(refusal("status", "root privileges are required.", f"Run {report.command('status')}."), file=sys.stderr)
        return 2

    loaded_site = site.load_site(Path(site_path), host=io)
    if loaded_site.errors or loaded_site.config is None:
        problem = site.render_errors(loaded_site.errors) or "site file could not be loaded."
        print(refusal("status", problem, f"Correct the site file, then run {report.command('status')}."), file=sys.stderr)
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

    build_box = nogpu.is_build_box(io)
    registered = proposals.SECTIONS if sections is None else tuple(sections)
    context = _status_context(
        io,
        checkout_root=checkout,
        rendered_dir=rendered,
        triggers_path=triggers_path,
        site_path=site_path,
        owui_client_factory=owui_client_factory,
        now=lambda: current.timestamp(),
        build_box=build_box,
    )
    _print_line(_HEADERS[1])
    waiting = (
        (context,)
        if isinstance(context, str)
        else fired.lines(
            context,
            registered,
            scope="office",
            failure="{section}: {problem} Fix: {fix}",
            row="{name}: {detail}",
            empty="none",
        )
    )
    for line in waiting:
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

    if build_box:
        _print_line(_HEADERS[3])
        _print_line(glance.fact_line(developer.sibling_fact(io, ci_root)))
        _print_line(glance.fact_line(developer.eval_run_fact(io, rendered, current)))
        lines = (context,) if isinstance(context, str) else developer.proposal_lines(context, registered)
        for line in lines:
            _print_line(line)

    if attention_failed:
        _print_line(_UNKNOWN_LINE)
        return 2
    if firing:
        _print_line(_ATTENTION_LINE.format(count=firing))
        return 1
    _print_line(_CLEAR_LINE)
    return 0

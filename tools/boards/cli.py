"""Command entry and stage rows for the Grafana browser board check."""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from gideon.evaluation.turns import chromium
from gideon.host import grafana, secrets, site, tls
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import Problem, StageResult, print_stage
from gideon.host.sysio import Host, RealHost
from tools.boards import inventory, page
from tools.ownership import restore_ownership, sudo_ids

DEFAULT_SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")
_OUT_FIX: Final = "Pass --out with a new or empty directory, then retry."
_ROOT_FIX: Final = "Run sudo .venv/bin/python -m tools.boards --out <dir>, then retry."
_SITE_FIX: Final = "Correct /etc/gideon/site.yaml, then retry."
_RENDER_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_LAUNCH_FIX: Final = (
    "Run .venv/bin/playwright install-deps chromium for the shared libraries and "
    f"check {chromium.BROWSERS_DIR}, then retry."
)
_BOARD_READ_FIX: Final = "Correct the rendered dashboard file, run apply, then retry."
_BOARD_OPEN_FIX: Final = (
    "Check the Grafana ingress and browser request log, then retry."
)
_BOARD_PANELS_FIX: Final = "Inspect the failing panel rows and screenshot, then correct the board or datasource."
_CLIENT_FIX: Final = "Check Grafana availability and the office CA, then retry."
_PANEL_ERROR_FIX: Final = (
    "Correct the panel query or datasource, then rerun the board check."
)
_PANEL_LOADING_FIX: Final = (
    "Check the panel query and Grafana service, then rerun the board check."
)
_PANEL_MISSING_FIX: Final = (
    "Check that the rendered panel appears on the Grafana board, then retry."
)
_RECORD_FIX: Final = "Make --out writable with free space, then retry."
_INTERNAL_FIX: Final = "Check the board check's inputs and browser setup, then retry."
_SECRET_FIX: Final = "Correct the Grafana break-glass secret file, then retry."
_SUMMARY_FIX: Final = "Correct the failing rows, then rerun the board check."

type PageFactory = Callable[
    [str, chromium.RequestLog], tuple[page.BoardPage, Callable[[], None]]
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.boards")
    parser.add_argument("uids", nargs="*")
    parser.add_argument("--out", type=Path, metavar="DIR")
    parser.add_argument("--trust-ca", action="store_true")
    return parser


def _launch_page(
    hostname: str, request_log: chromium.RequestLog
) -> tuple[page.BoardPage, Callable[[], None]]:
    return chromium.launch(
        hostname,
        browsers_dir=chromium.BROWSERS_DIR,
        browser_home=chromium.BROWSER_HOME,
        request_log=request_log,
        page_timeout=chromium.PAGE_TIMEOUT_SECONDS,
        locale="en-US",
    )


def _refuse(name: str, problem: Problem) -> None:
    print_stage(StageResult(name, False, problem.problem, problem.fix))


def _out_refusal(host: Host, output: Path) -> bool:
    if not host.exists(output):
        return False
    try:
        occupied = bool(host.listdir(output))
    except OSError:
        print(
            f"tools.boards: --out {output} is not a usable directory. Fix: {_OUT_FIX}",
            file=sys.stderr,
        )
        return True
    if occupied:
        print(
            f"tools.boards: --out {output} exists and is not empty. Fix: {_OUT_FIX}",
            file=sys.stderr,
        )
        return True
    return False


def _selected(
    requested: Sequence[str],
    available: tuple[inventory.Board | inventory.Unreadable, ...],
) -> tuple[inventory.Board | inventory.Unreadable | str, ...]:
    if not requested:
        return available
    by_uid = {
        board.uid: board for board in available if isinstance(board, inventory.Board)
    }
    return tuple(by_uid.get(uid, uid) for uid in requested)


def _panel_row(uid: str, reading: page.PanelReading) -> StageResult:
    fix = {
        "errored": _PANEL_ERROR_FIX,
        "loading": _PANEL_LOADING_FIX,
        "missing": _PANEL_MISSING_FIX,
    }.get(reading.state, "")
    detail = f"{uid}: {reading.title}: {reading.state}"
    if reading.detail:
        detail += f"; {reading.detail}"
    return StageResult("panel", reading.state not in page.FAILING_STATES, detail, fix)


def _board_row(
    board: inventory.Board, readings: tuple[page.PanelReading, ...]
) -> StageResult:
    counts = Counter(reading.state for reading in readings)
    opened = bool(readings) and counts["missing"] < len(readings)
    ok = opened and not any(
        reading.state in page.FAILING_STATES for reading in readings
    )
    count_text = ", ".join(
        f"{state}={counts[state]}"
        for state in ("drawn", "empty", "errored", "loading", "missing")
    )
    detail = f"{board.uid}: {board.title}; {count_text}; screenshot {board.uid}.png"
    if not opened:
        detail = f"{board.uid}: {board.title}; no expected panel mounted; {count_text}; screenshot {board.uid}.png"
    return StageResult("board", ok, detail, "" if ok else _BOARD_PANELS_FIX)


def _request_row(
    host: Host, output: Path, log: chromium.RequestLog, hostname: str
) -> bool:
    counts = log.counts()
    detail = (
        f"{counts.on_host} requests to {hostname}; {counts.off_host} off-host aborted"
    )
    if counts.off_hostnames:
        detail += f" ({', '.join(counts.off_hostnames)})"
    try:
        host.write_text(output / "requests.log", log.render() + "\n")
    except OSError:
        print_stage(
            StageResult(
                "requests", False, f"{detail}; requests.log not written", _RECORD_FIX
            )
        )
        return False
    print_stage(StageResult("requests", True, detail, ""))
    return True


def _record(
    host: Host,
    output: Path,
    *,
    checkout: Path,
    screenshots: Sequence[str],
    requests_written: bool,
) -> StageResult:
    files = (["requests.log"] if requests_written else []) + [
        f"{uid}.png" for uid in screenshots
    ]
    try:
        owner = sudo_ids()
        if owner is not None:
            restore_ownership(host, output, owner, checkout=checkout)
    except OSError:
        return StageResult("record", False, "ownership hand-back failed", _RECORD_FIX)
    return StageResult(
        "record", True, f"{output}: {', '.join(files) or 'no files'}", ""
    )


def _run(
    options: argparse.Namespace,
    *,
    host: Host,
    page_factory: PageFactory | None,
    client_factory: Callable[..., inventory.BoardClient] | None,
    site_path: Path,
    rendered_root: Path,
    checkout: Path,
    monotonic: Callable[[], float],
) -> int:
    if options.out is None:
        print(f"tools.boards: --out is required. Fix: {_OUT_FIX}", file=sys.stderr)
        return 1
    output: Path = options.out.resolve()
    if _out_refusal(host, output):
        return 1
    if host.geteuid() != 0:
        _refuse("preconditions", Problem("root is required", _ROOT_FIX))
        return 1
    loaded_site = site.load_site(site_path, host=host)
    if loaded_site.errors or loaded_site.config is None:
        detail = (
            "; ".join(error.problem for error in loaded_site.errors)
            or "site file is unavailable"
        )
        _refuse("preconditions", Problem(detail, _SITE_FIX))
        return 1
    for problem in (
        chromium.playwright_problem(),
        chromium.browser_problem(host, chromium.BROWSERS_DIR),
    ):
        if problem is not None:
            _refuse("preconditions", problem)
            return 1
    secret = secrets.read_secret(host, "grafana_admin_password")
    if secret.problem is not None:
        _refuse("preconditions", Problem(secret.problem, secret.fix))
        return 1
    password = secret.value
    del secret
    if not password:
        _refuse(
            "preconditions",
            Problem("Grafana break-glass password is empty", _SECRET_FIX),
        )
        return 1
    available = inventory.provisioned_boards(host, rendered_root=rendered_root)
    if not options.uids and not available:
        _refuse(
            "preconditions", Problem("no provisioned boards are rendered", _RENDER_FIX)
        )
        return 1
    selected = _selected(options.uids, available)
    try:
        host.mkdir(output, parents=True, exist_ok=True)
    except OSError:
        _refuse(
            "preconditions", Problem("output directory could not be created", _OUT_FIX)
        )
        return 1
    hostname = loaded_site.config.hostname
    print_stage(
        StageResult(
            "preconditions", True, f"{len(selected)} boards; output {output}", ""
        )
    )
    if options.trust_ca:
        try:
            detail, problem = chromium.trust_ca(host)
        except OSError:
            _refuse(
                "trust-ca",
                Problem("browser CA trust could not be changed", _LAUNCH_FIX),
            )
            return 1
        if problem is not None:
            _refuse("trust-ca", problem)
            return 1
        print_stage(StageResult("trust-ca", True, detail, ""))

    request_log = chromium.RequestLog(monotonic)
    try:
        browser_page, close = (page_factory or _launch_page)(hostname, request_log)
    except Exception:  # noqa: BLE001 - a browser launch failure must be a safe row.
        _refuse("browser", Problem("the browser could not be launched", _LAUNCH_FIX))
        return 1
    print_stage(
        StageResult(
            "browser",
            True,
            f"playwright {chromium.PLAYWRIGHT_VERSION}; chromium headless shell under "
            f"{chromium.BROWSERS_DIR}; {hostname} mapped to loopback",
            "",
        )
    )

    counts: Counter[str] = Counter()
    screenshots: list[str] = []
    opened = 0
    boards_ok = True
    signin_ok = False
    try:
        try:
            signin_problem = page.sign_in(
                browser_page,
                GRAFANA_ADMIN_USER,
                password,
                timeout=chromium.PAGE_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - no page exception text may reach a row.
            signin_problem = Problem(
                "Grafana sign-in page could not be read", _BOARD_OPEN_FIX
            )
        if signin_problem is not None:
            _refuse("signin", signin_problem)
        else:
            signin_ok = True
            print_stage(
                StageResult("signin", True, f"signed in as {GRAFANA_ADMIN_USER}", "")
            )
            known_uids = sorted(
                board.uid for board in available if isinstance(board, inventory.Board)
            )
            unreadable = ", ".join(
                board.name
                for board in available
                if isinstance(board, inventory.Unreadable)
            )
            remote_client: inventory.BoardClient | Problem | None = None
            for item in selected:
                if isinstance(item, str) and unreadable:
                    # The uid may be the unreadable file's own, and Grafana's stored
                    # copy would then stand in for a rendered tree that is broken.
                    _refuse(
                        "board",
                        Problem(
                            f"{item}: not among the readable rendered boards while "
                            f"{unreadable} cannot be read",
                            _BOARD_READ_FIX,
                        ),
                    )
                    boards_ok = False
                    continue
                if isinstance(item, str):
                    if remote_client is None:
                        try:
                            make_client = (
                                client_factory
                                or grafana.ingress_client_factory(
                                    hostname, ca_path=tls.CA_PATH
                                )
                            )
                            remote_client = make_client(
                                credential=(GRAFANA_ADMIN_USER, password)
                            )
                        except grafana.GrafanaError as exc:
                            remote_client = Problem(exc.problem, exc.fix)
                        except Exception:  # noqa: BLE001 - a client failure is one safe board row.
                            remote_client = Problem(
                                "Grafana client could not be opened", _CLIENT_FIX
                            )
                    if isinstance(remote_client, Problem):
                        _refuse(
                            "board",
                            Problem(
                                f"{item}: {remote_client.problem}", remote_client.fix
                            ),
                        )
                        boards_ok = False
                        continue
                    try:
                        named = inventory.named_board(
                            remote_client, item, provisioned_uids=known_uids
                        )
                    except Exception:  # noqa: BLE001 - no client exception text reaches a row.
                        named = Problem(
                            f"{item}: Grafana dashboard could not be read", _CLIENT_FIX
                        )
                    if isinstance(named, Problem):
                        _refuse("board", named)
                        boards_ok = False
                        continue
                    item = named
                if isinstance(item, inventory.Unreadable):
                    _refuse(
                        "board", Problem(f"{item.name}: {item.reason}", _BOARD_READ_FIX)
                    )
                    boards_ok = False
                    continue
                try:
                    result = page.check_board(
                        browser_page,
                        item,
                        output / f"{item.uid}.png",
                        timeout=chromium.PAGE_TIMEOUT_SECONDS,
                    )
                except Exception:  # noqa: BLE001 - a failed board must not stop its peers.
                    result = Problem("Grafana board could not be read", _BOARD_OPEN_FIX)
                if isinstance(result, Problem):
                    _refuse(
                        "board",
                        Problem(f"{item.uid}: {result.problem}", _BOARD_OPEN_FIX),
                    )
                    boards_ok = False
                    continue
                screenshots.append(item.uid)
                for reading in result:
                    counts[reading.state] += 1
                    panel_row = _panel_row(item.uid, reading)
                    print_stage(panel_row)
                board_row = _board_row(item, result)
                print_stage(board_row)
                opened += int(any(reading.state != "missing" for reading in result))
                boards_ok &= board_row.ok
    finally:
        try:
            close()
        except Exception:  # noqa: BLE001 - teardown still yields the remaining rows.
            _refuse("browser", Problem("the browser could not be closed", _LAUNCH_FIX))
            boards_ok = False

    requests_ok = _request_row(host, output, request_log, hostname)
    run_ok = signin_ok and boards_ok and requests_ok and opened == len(selected)
    record_row = _record(
        host,
        output,
        checkout=checkout,
        screenshots=screenshots,
        requests_written=requests_ok,
    )
    run_ok &= record_row.ok
    summary = f"{opened}/{len(selected)} boards opened; " + ", ".join(
        f"{state}={counts[state]}"
        for state in ("drawn", "empty", "errored", "loading", "missing")
    )
    print_stage(StageResult("summary", run_ok, summary, "" if run_ok else _SUMMARY_FIX))
    print_stage(record_row)
    return int(not run_ok)


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    page_factory: PageFactory | None = None,
    client_factory: Callable[..., inventory.BoardClient] | None = None,
    site_path: Path = DEFAULT_SITE_PATH,
    rendered_root: Path = inventory.RENDERED_ROOT,
    checkout: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run the browser check with injectable host and page boundaries."""

    try:
        options = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return _run(
            options,
            host=host or RealHost(),
            page_factory=page_factory,
            client_factory=client_factory,
            site_path=site_path,
            rendered_root=rendered_root,
            checkout=checkout or Path(__file__).resolve().parents[2],
            monotonic=monotonic,
        )
    except Exception:  # noqa: BLE001 - the CLI never prints an unexpected exception.
        _refuse("summary", Problem("board check could not complete", _INTERNAL_FIX))
        return 1

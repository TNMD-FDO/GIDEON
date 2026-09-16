"""CLI surface and preconditions for the turn harness."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from gideon.host import models, owui, secrets, site, tls
from gideon.host.render.owui import EVAL_PASSWORD_SECRET, GENERAL_PRESET_ID
from gideon.host.report import Problem, StageResult, print_stage
from gideon.host.sysio import Host, PathLike, RealHost
from tools.turns import browser, cases, chromium, classify
from tools.turns.run import (
    BROWSER_ENGINE_CALLS_PER_TURN,
    SMOKE_TURNS,
    TURN_TIMEOUT_SECONDS,
    BrowserSetup,
    RunSpec,
    new_sentinel,
    run,
)

DEFAULT_SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")
_SITE_FIX: Final[str] = "Correct /etc/gideon/site.yaml, then retry."
_ROOT_FIX: Final[str] = "Run sudo python3 -m tools.turns <cases>, then retry."
_BROWSER_ROOT_FIX: Final[str] = (
    "Run sudo .venv/bin/python -m tools.turns --browser <cases>, then retry."
)
_PASSWORD_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
_MODELS_FIX: Final[str] = (
    "Correct models.lock so the site's hardware_profile names a profile with a generator pin, then retry."
)
_OUT_FIX: Final[str] = "Name a new or empty directory for the run's transcripts."
_BROWSER_OUT_FIX: Final[str] = "Pass --out <dir> when using --browser, then retry."
_BROWSER_STREAM_FIX: Final[str] = (
    "Use --probe-inlet for the users-seat probe; browser mode captures the live screen."
)
_BROWSER_CONCURRENT_FIX: Final[str] = (
    "Run the API mode with --concurrent, and one --browser case beside it for the "
    "screen under load, then retry."
)
_BROWSER_SEARCH_FIX: Final[str] = (
    "Run the API mode over this file, or remove its search cases for a browser run, "
    "then retry."
)
_BROWSER_ONLY_FIX: Final[str] = "Pass --browser with this flag, then retry."
_LAUNCH_FIX: Final[str] = (
    "Run .venv/bin/playwright install-deps chromium for the shared libraries and "
    f"check {chromium.BROWSERS_DIR}, then retry."
)
# The directory account is a person-shaped users-group seat, not a product secret.
TEST_ACCOUNT: Final[str] = "gideon-test-user"

# The browser mode's launcher seam: the hostname and the request log in, the
# page, its teardown, and the browser row's detail out.
type PageFactory = Callable[
    [str, chromium.RequestLog], tuple[browser.Page, Callable[[], None], str]
]


def _positive(value: str, name: str = "repeat") -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be positive") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.turns")
    parser.add_argument("cases", type=Path)
    parser.add_argument("--repeat", type=_positive, default=1, metavar="N")
    parser.add_argument(
        "--concurrent",
        type=lambda value: _positive(value, "concurrent"),
        default=1,
        metavar="N",
    )
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--probe-inlet", action="store_true")
    parser.add_argument("--trust-ca", action="store_true")
    parser.add_argument("--out", type=Path, metavar="DIR")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _window_judgement(clock: datetime, timezone_name: str) -> tuple[str, bool]:
    """Return the quiet-window report and whether the clock is office hours."""

    if clock.tzinfo is None or clock.utcoffset() is None:
        clock = clock.replace(tzinfo=UTC)
    local = clock.astimezone(ZoneInfo(timezone_name))
    weekday = local.strftime("%A")
    clock_text = local.strftime("%H:%M")
    if weekday in {"Saturday", "Sunday"}:
        return f"weekend ({weekday})", False
    if local.hour >= 19 or local.hour < 6:
        return f"inside the quiet window ({clock_text} {timezone_name}, {weekday})", False
    return f"office hours ({clock_text} {timezone_name}, {weekday})", True


def _out_refusal(io: Host, output: Path) -> int | None:
    if not io.exists(output):
        return None
    try:
        occupied = bool(io.listdir(output))
    except OSError as exc:
        print(
            f"tools.turns: --out {output} is not a usable directory ({exc}). "
            f"Fix: {_OUT_FIX}",
            file=sys.stderr,
        )
        return 1
    if occupied:
        print(
            f"tools.turns: --out {output} exists and is not empty. Fix: {_OUT_FIX}",
            file=sys.stderr,
        )
        return 1
    return None


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    client_factory: Callable[..., owui.Client] | None = None,
    page_factory: PageFactory | None = None,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    checkout: Path | None = None,
    site_path: PathLike = DEFAULT_SITE_PATH,
) -> int:
    """Parse the command, judge the preconditions as one row, then run the cases."""

    options = _parser().parse_args(argv)
    io = host or RealHost()
    root = checkout or Path(__file__).resolve().parents[2]
    output = options.out.resolve() if options.out is not None else None
    if options.browser and output is None:
        print_stage(StageResult("preconditions", False, "--out is required with --browser", _BROWSER_OUT_FIX))
        return 1
    if options.browser and options.stream:
        print_stage(StageResult("preconditions", False, "--stream is not available with --browser", _BROWSER_STREAM_FIX))
        return 1
    if options.browser and options.concurrent > 1:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "--concurrent is not available with --browser",
                _BROWSER_CONCURRENT_FIX,
            )
        )
        return 1
    if options.probe_inlet and not options.browser:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "--probe-inlet requires --browser",
                _BROWSER_ONLY_FIX,
            )
        )
        return 1
    if options.trust_ca and not options.browser:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "--trust-ca requires --browser",
                _BROWSER_ONLY_FIX,
            )
        )
        return 1
    if output is not None:
        refused = _out_refusal(io, output)
        if refused is not None:
            return refused

    if io.geteuid() != 0:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "root is required",
                _BROWSER_ROOT_FIX if options.browser else _ROOT_FIX,
            )
        )
        return 1

    loaded_site = site.load_site(Path(site_path), host=io)
    if loaded_site.errors or loaded_site.config is None:
        detail = "; ".join(error.problem for error in loaded_site.errors)
        print_stage(StageResult("preconditions", False, detail, _SITE_FIX))
        return 1

    base_model: str | None = None
    if options.probe_inlet:
        models_result = models.load_models_lock(root / "models.lock", host=io)
        if models_result.errors or models_result.lock is None:
            fix = models_result.errors[0].fix if models_result.errors else _MODELS_FIX
            print_stage(
                StageResult(
                    "preconditions",
                    False,
                    models.render_errors(models_result.errors),
                    fix,
                )
            )
            return 1
        selected = models.select_profile(
            models_result.lock, loaded_site.config.hardware_profile
        )
        if isinstance(selected, Problem):
            print_stage(
                StageResult("preconditions", False, selected.problem, selected.fix)
            )
            return 1
        generator = selected.model("generator")
        if generator is None:
            print_stage(
                StageResult(
                    "preconditions",
                    False,
                    f"models.lock profile '{selected.name}' has no generator pin",
                    _MODELS_FIX,
                )
            )
            return 1
        base_model = generator.serve.served_name

    if options.browser:
        password_problem = chromium.password_file_problem(io)
        if password_problem is not None:
            print_stage(
                StageResult(
                    "preconditions",
                    False,
                    password_problem.problem,
                    password_problem.fix,
                )
            )
            return 1
        password_value = chromium.read_password(io)
        browser_checks = (
            chromium.playwright_problem(),
            chromium.browser_problem(io, chromium.BROWSERS_DIR),
        )
        for problem in browser_checks:
            if problem is not None:
                print_stage(StageResult("preconditions", False, problem.problem, problem.fix))
                return 1
    else:
        password = secrets.read_secret(io, EVAL_PASSWORD_SECRET)
        if not password.ok or password.value is None:
            fix = _PASSWORD_FIX if password.missing else password.fix or _PASSWORD_FIX
            print_stage(
                StageResult(
                    "preconditions",
                    False,
                    password.problem or "the evaluation password is unavailable",
                    fix,
                )
            )
            return 1
        password_value = password.value

    filter_path = root / "compose/open-webui/functions/arithmetic_guardrail.py"
    try:
        guardrail = classify.load_guardrail(root)
    except Exception as exc:  # noqa: BLE001 - the precondition owns import failures.
        print_stage(
            StageResult(
                "preconditions",
                False,
                f"guardrail could not be loaded from {filter_path}: {type(exc).__name__}: {exc}",
                f"Correct {filter_path}, then retry.",
            )
        )
        return 1

    loaded_cases = cases.load_cases(options.cases)
    if not isinstance(loaded_cases, cases.CaseSet):
        print_stage(
            StageResult("preconditions", False, loaded_cases.problem, loaded_cases.fix)
        )
        return 1
    if options.browser and loaded_cases.searched:
        print_stage(
            StageResult(
                "preconditions",
                False,
                "search cases run in the API mode",
                _BROWSER_SEARCH_FIX,
            )
        )
        return 1

    case_values = loaded_cases.cases
    selected_now = now or _utc_now
    case_turns = (
        len(case_values)
        * options.repeat
        * options.concurrent
        * (2 if options.stream else 1)
    )
    turns = case_turns + (1 if options.probe_inlet else 0)
    if options.browser:
        engine_calls = case_turns * BROWSER_ENGINE_CALLS_PER_TURN + (
            1 if options.probe_inlet else 0
        )
    else:
        engine_calls = (
            case_turns
            + loaded_cases.searched * options.repeat * options.concurrent
            + (1 if options.probe_inlet else 0)
        )
    engine_call_fragment = ""
    if options.browser:
        engine_call_fragment = f" ({BROWSER_ENGINE_CALLS_PER_TURN} per browser turn"
        if options.probe_inlet:
            engine_call_fragment += ", the probe 1"
        engine_call_fragment += ")"
    elif loaded_cases.searched:
        engine_call_fragment = " (2 per searched case)"
    engine_call_detail = f"{engine_calls} engine calls{engine_call_fragment}"
    # The engine-call count is named only when it differs from the turns.
    shows_calls = options.browser or bool(loaded_cases.searched)
    turns_detail = f"{turns} turns" + (
        f" ({options.concurrent} sessions)" if options.concurrent > 1 else ""
    )
    current = selected_now()
    timezone_name = loaded_site.config.office.timezone
    window_judgement, office_hours = _window_judgement(current, timezone_name)
    if engine_calls > SMOKE_TURNS and office_hours and not options.force:
        local_time = current.astimezone(ZoneInfo(timezone_name))
        detail = (
            turns_detail
            + (f", {engine_call_detail}" if shows_calls else "")
            + f" during office hours ({local_time:%H:%M} {timezone_name})"
        )
        print_stage(
            StageResult(
                "preconditions",
                False,
                detail,
                f"Run between 19:00 and 06:00 {timezone_name} or on a weekend, "
                "or pass --force for a deliberate daytime run.",
            )
        )
        return 1
    window_detail = window_judgement
    if options.force and office_hours:
        window_detail += "; window overridden by --force"
    print_stage(
        StageResult(
            "preconditions",
            True,
            f"loaded {loaded_cases.origin}; {turns_detail}; "
            + (f"{engine_call_detail}; " if shows_calls else "")
            + window_detail,
            "",
        )
    )

    if options.trust_ca:
        trust_detail, trust_problem = chromium.trust_ca(io)
        if trust_problem is not None:
            print_stage(
                StageResult(
                    "trust-ca",
                    False,
                    trust_problem.problem,
                    trust_problem.fix,
                )
            )
            return 1
        print_stage(StageResult("trust-ca", True, trust_detail, ""))

    if options.dry_run:
        print("Turn harness dry run:")
        streamed = ", streamed" if options.stream else ""
        probed = " + 1 probe" if options.probe_inlet else ""
        print(f"cases: {loaded_cases.origin}")
        sessions = f"{options.concurrent} sessions × " if options.concurrent > 1 else ""
        print(
            f"turns: {turns} ({sessions}{options.repeat} × {len(case_values)}"
            f"{streamed}{probed})"
        )
        if options.concurrent > 1:
            print(f"sessions: {options.concurrent} in flight")
        if shows_calls:
            print(f"engine calls: {engine_calls}{engine_call_fragment}")
        print(f"window: {window_detail}")
        print(f"model: {GENERAL_PRESET_ID}")
        print(f"output directory: {output if output is not None else 'none'}")
        if options.probe_inlet:
            print(f"probe: inlet gate ({base_model})")
        if options.browser:
            print("mode: browser")
            print(f"harness home: {chromium.HARNESS_HOME}")
            print(f"password file: {chromium.PASSWORD_FILE}")
            print(f"browsers directory: {chromium.BROWSERS_DIR}")
            print(f"browser home: {chromium.BROWSER_HOME}")
            print(f"playwright: {chromium.PLAYWRIGHT_VERSION}")
        return 0

    chosen_factory = client_factory or owui.ingress_client_factory(
        loaded_site.config.hostname,
        ca_path=tls.CA_PATH,
        timeout=TURN_TIMEOUT_SECONDS,
    )
    browser_setup: BrowserSetup | None = None
    if options.browser:
        request_log = chromium.RequestLog(monotonic)
        try:
            page, close, detail = (page_factory or _launch_page)(
                loaded_site.config.hostname, request_log
            )
        except Exception as exc:  # noqa: BLE001 - a launch that fails is a row, never a traceback.
            print_stage(
                StageResult(
                    "browser",
                    False,
                    f"the browser could not be launched: {type(exc).__name__}",
                    _LAUNCH_FIX,
                )
            )
            return 1
        browser_setup = BrowserSetup(
            page=page,
            close=close,
            detail=detail,
            hostname=loaded_site.config.hostname,
            account=TEST_ACCOUNT,
            request_log=request_log,
            page_timeout=chromium.PAGE_TIMEOUT_SECONDS,
        )
    spec = RunSpec(
        cases=options.cases,
        repeat=options.repeat,
        stream=options.stream,
        out=output,
        force=options.force,
        dry_run=options.dry_run,
        sentinel=new_sentinel(),
        concurrent=options.concurrent,
        browser=options.browser,
        probe_inlet=options.probe_inlet,
        trust_ca=options.trust_ca,
        base_model=base_model,
    )
    selected_now = now or _utc_now
    return run(
        spec,
        cases=case_values,
        password=password_value,
        guardrail=guardrail,
        client_factory=chosen_factory,
        now=selected_now,
        monotonic=monotonic,
        host=io,
        checkout=root,
        origin=loaded_cases.origin,
        window=window_detail,
        browser_setup=browser_setup,
    )


def _launch_page(
    hostname: str, request_log: chromium.RequestLog
) -> tuple[browser.Page, Callable[[], None], str]:
    """The production page factory: the pinned headless shell in the footprint."""

    page, close = chromium.launch(
        hostname,
        browsers_dir=chromium.BROWSERS_DIR,
        browser_home=chromium.BROWSER_HOME,
        request_log=request_log,
        page_timeout=chromium.PAGE_TIMEOUT_SECONDS,
    )
    detail = (
        f"playwright {chromium.PLAYWRIGHT_VERSION}; chromium headless shell under "
        f"{chromium.BROWSERS_DIR}; {hostname} mapped to loopback; trust store "
        f"{chromium.TRUST_STORE_PATH}"
    )
    return page, close, detail

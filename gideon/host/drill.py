"""The isolated backup restore drill (§19.2 and the [22] ruling)."""

import base64
import binascii
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import gideon
from gideon.host import apply, audit, backupset, owui, pgbackrest, site, stack
from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock
from gideon.host.models import load_models_lock, select_profile
from gideon.host.render import RenderInputs
from gideon.host.render import command as render_command
from gideon.host.render.drill import DRILL_PORT, DRILL_ROOT, drill_compose_document
from gideon.host.render.facts import HostFacts
from gideon.host.render.yamlout import dump
from gideon.host.report import (
    Problem,
    StageResult,
    command_detail,
    print_stage,
    refusal,
)
from gideon.host.site import SiteConfig
from gideon.host.stages import IDENTIFIER, aware_now, psql_argv, run_stage, table_parts
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_ROOT_FIX: Final = "Run sudo python3 -m gideon backup drill as root, then retry."
_APPLY_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_TOOLS_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only host-tools, then retry."
)
_SET_FIX: Final = "Run sudo python3 -m gideon backup run, then retry."
_PREPARE_FIX: Final = "Repair the drill directory and render inputs, then retry."
_TARBALL_FIX: Final = "Repair the backup set's secrets tarball, then retry."
_VERIFY_FIX: Final = "Repair the pgBackRest repository, then retry."
_COUNTS_FIX: Final = "Repair the restored database, then retry."
_DRILL_TIMEOUT: Final = 18000.0
_AGE_MAGIC: Final = b"age-encryption.org/v1"
_AGE_FIX: Final = "Run sudo python3 -m gideon backup run, then retry."
_CLEANUP_NAMES: Final = ("postgres", "openwebui", "compose.yaml")


@dataclass(frozen=True, slots=True)
class _DrillContext:
    config: SiteConfig
    checkout: str
    local_set: backupset.SetRef
    inputs: RenderInputs


def _refuse(problem: str, fix: str) -> int:
    print(refusal("backup drill", problem, fix), file=sys.stderr)
    return 1


def _load_error_rows(errors: Sequence[object]) -> None:
    for error in errors:
        problem = getattr(error, "problem", "render input is invalid")
        fix = getattr(error, "fix", _PREPARE_FIX)
        _refuse(str(problem), str(fix))


def _context(
    io: Host,
    *,
    rendered_dir: PathLike,
    site_path: PathLike,
    checkout: Path,
) -> _DrillContext | None:
    loaded_site = site.load_site(Path(site_path), host=io)
    if loaded_site.errors or loaded_site.config is None:
        _load_error_rows(loaded_site.errors)
        return None

    try:
        has_bash = io.exists("/usr/bin/bash")
    except OSError as exc:
        _refuse(f"cannot inspect drill prerequisites: {exc}.", _TOOLS_FIX)
        return None
    if not has_bash:
        _refuse("backup drill tool is missing: /usr/bin/bash.", _TOOLS_FIX)
        return None

    compose_path = Path(rendered_dir) / "compose.yaml"
    try:
        has_compose = io.exists(compose_path)
    except OSError as exc:
        _refuse(f"cannot inspect rendered Compose state: {exc}.", _APPLY_FIX)
        return None
    if not has_compose:
        _refuse(f"rendered Compose file is missing: {compose_path}.", _APPLY_FIX)
        return None

    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        _refuse(f"cannot list local backup sets: {exc}.", _SET_FIX)
        return None
    local_set = next(
        (ref for ref in sets if ref.complete and ref.manifest is not None), None
    )
    if local_set is None:
        _refuse("no complete local backup set is available.", _SET_FIX)
        return None

    lock_result = load_host_lock(checkout / "host.lock", host=io)
    image_result = load_image_lock(checkout / "images.lock", host=io)
    models_result = load_models_lock(checkout / "models.lock", host=io)
    if lock_result.errors:
        _load_error_rows(lock_result.errors)
    if image_result.errors:
        _load_error_rows(image_result.errors)
    if models_result.errors:
        _load_error_rows(models_result.errors)
    if (
        lock_result.lock is None
        or image_result.lock is None
        or models_result.lock is None
    ):
        return None

    profile = select_profile(models_result.lock, loaded_site.config.hardware_profile)
    if isinstance(profile, Problem):
        _refuse(profile.problem, profile.fix)
        return None

    try:
        templates = render_command.load_templates(io, checkout)
    except (OSError, UnicodeError, ValueError) as exc:
        _refuse(f"drill render template loading failed: {exc}.", _PREPARE_FIX)
        return None

    inputs = RenderInputs(
        site=loaded_site.config,
        lock=lock_result.lock,
        images=image_result.lock,
        facts=HostFacts((), service_gid=0),
        profile=profile,
        templates=templates,
        release=gideon.__version__,
        secrets={},
        checkout=os.fspath(checkout),
    )
    audit_problem = audit.probe(io, rendered_dir)
    if audit_problem is not None:
        _refuse(
            f"audit writer is unavailable: {audit_problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
        return None
    return _DrillContext(
        loaded_site.config,
        os.fspath(checkout),
        local_set,
        inputs,
    )


def _drill_path(name: str) -> str:
    path = os.path.join(DRILL_ROOT, name)
    if not path.startswith(DRILL_ROOT + os.sep):
        raise ValueError(f"unsafe drill path: {path}")
    return path


def _cleanup(
    io: Host,
    *,
    stage: str,
    fix: str,
) -> StageResult | None:
    first_failure: StageResult | None = None
    for name in _CLEANUP_NAMES:
        path = _drill_path(name)
        result = run_stage(
            io,
            stage,
            ["rm", "-rf", path],
            f"removed {path}",
            fix,
        )
        if not result.ok and first_failure is None:
            first_failure = result
    return first_failure


def _teardown_before(io: Host) -> StageResult:
    compose_path = _drill_path("compose.yaml")
    try:
        has_compose = io.exists(compose_path)
    except OSError as exc:
        return StageResult(
            "teardown-before",
            False,
            f"cannot inspect the previous drill: {exc}",
            _PREPARE_FIX,
        )

    first_failure: StageResult | None = None
    if has_compose:
        down = run_stage(
            io,
            "teardown-before",
            stack.compose_argv(DRILL_ROOT, "down", "-v", "--remove-orphans"),
            "tore down the previous drill project",
            _PREPARE_FIX,
        )
        if not down.ok:
            first_failure = down
    cleanup_failure = _cleanup(
        io,
        stage="teardown-before",
        fix=_PREPARE_FIX,
    )
    if first_failure is not None:
        return first_failure
    if cleanup_failure is not None:
        return cleanup_failure
    detail = "removed the previous drill project" if has_compose else "no previous drill project"
    return StageResult("teardown-before", True, detail, "")


def _prepare_stage(io: Host, context: _DrillContext, *, rendered_dir: PathLike) -> StageResult:
    compose_path = _drill_path("compose.yaml")
    postgres_path = _drill_path("postgres")
    openwebui_path = _drill_path("openwebui")
    try:
        document = dump(drill_compose_document(context.inputs))
        io.write_text(compose_path, document, mode=0o644)
    except (OSError, TypeError, ValueError) as exc:
        return StageResult(
            "prepare",
            False,
            f"drill Compose document could not be written: {exc}",
            _PREPARE_FIX,
        )

    # The drill runs the same image as production, so the running production
    # service answers for the ids before the drill project exists.
    identity, failure = pgbackrest.container_identity(io, rendered_dir)
    if failure is not None or identity is None:
        return StageResult(
            "prepare",
            False,
            (
                failure.problem
                if failure is not None and failure.problem is not None
                else "Postgres identity lookup failed"
            ),
            (failure.fix if failure is not None and failure.fix else _PREPARE_FIX),
        )
    uid, gid = identity
    try:
        io.mkdir(postgres_path, mode=0o750, parents=True, exist_ok=True)
        io.chown(postgres_path, uid, gid)
        io.mkdir(openwebui_path, mode=0o755, parents=True, exist_ok=True)
    except OSError as exc:
        return StageResult(
            "prepare",
            False,
            f"drill data directories could not be prepared: {exc}",
            _PREPARE_FIX,
        )

    source = os.path.join(
        context.local_set.path,
        backupset.FILES_DIR,
        "data-bulk-openwebui",
    )
    return run_stage(
        io,
        "prepare",
        ["rsync", "-a", source.rstrip("/") + "/", openwebui_path.rstrip("/") + "/"],
        "prepared the drill data directories and uploads",
        _PREPARE_FIX,
    )


def parse_age_header(data: bytes, recipients: int) -> Problem | None:
    """Validate the structural age header without inspecting its ciphertext."""

    if not isinstance(data, bytes):
        return Problem("Age ciphertext is not bytes.", _AGE_FIX)
    lines = data.splitlines()
    if not lines or lines[0] != _AGE_MAGIC:
        return Problem("Age ciphertext has the wrong header.", _AGE_FIX)
    mac = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.startswith(b"---")),
        None,
    )
    if mac is None:
        return Problem("Age ciphertext has no header MAC line.", _AGE_FIX)
    stanza_count = sum(line.startswith(b"-> X25519 ") for line in lines[1:mac])
    if stanza_count == 0:
        return Problem("Age ciphertext has no X25519 recipient stanza.", _AGE_FIX)
    if stanza_count != recipients:
        return Problem(
            f"Age ciphertext has {stanza_count} X25519 recipient stanza(s); "
            f"the manifest names {recipients}.",
            _AGE_FIX,
        )
    return None


def _tarball_stage(
    io: Host,
    context: _DrillContext,
) -> StageResult:
    assert context.local_set.manifest is not None
    tarball = os.path.join(context.local_set.path, backupset.TARBALL_NAME)
    try:
        hashed = io.run(["sha256sum", tarball])
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("tarball", False, f"tarball hash failed: {exc}", _TARBALL_FIX)
    if hashed.returncode != 0:
        return StageResult(
            "tarball",
            False,
            f"tarball hash failed: {command_detail(hashed)}",
            _TARBALL_FIX,
        )
    try:
        hashes = backupset.parse_sha256sum(hashed.stdout)
    except ValueError as exc:
        return StageResult("tarball", False, f"tarball hash was malformed: {exc}", _TARBALL_FIX)
    if hashes.get(tarball, "").casefold() != context.local_set.manifest.tarball_sha256.casefold():
        return StageResult("tarball", False, "tarball hash does not match the manifest", _TARBALL_FIX)

    script = (
        "set -o pipefail; head -c 4096 -- "
        + shlex.quote(tarball)
        + " | base64 -w0"
    )
    try:
        inspected = io.run(["bash", "-c", script])
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("tarball", False, f"age header probe failed: {exc}", _TARBALL_FIX)
    if inspected.returncode != 0:
        return StageResult(
            "tarball",
            False,
            f"age header probe failed: {command_detail(inspected)}",
            _TARBALL_FIX,
        )
    try:
        decoded = base64.b64decode(inspected.stdout, validate=True)
    except (ValueError, binascii.Error) as exc:
        return StageResult("tarball", False, f"age header was not base64: {exc}", _TARBALL_FIX)
    recipient_count = len(context.local_set.manifest.recipients)
    problem = parse_age_header(decoded, recipient_count)
    if problem is not None:
        return StageResult("tarball", False, problem.problem, problem.fix)
    return StageResult(
        "tarball",
        True,
        f"the secrets tarball hash and age header passed for {recipient_count} recipient stanza(s)",
        "",
    )


def _verify_stage(io: Host) -> StageResult:
    problem = pgbackrest.verify(io, pgbackrest.verify_argv(DRILL_ROOT))
    if problem is not None:
        return StageResult("verify", False, problem, _VERIFY_FIX)
    return StageResult("verify", True, "pgBackRest repository verification passed", "")


def _postgres_stage(
    io: Host,
    *,
    sleep: Callable[[float], None],
    manifest: backupset.Manifest,
) -> StageResult:
    postgres_fix = stack.logs_fix(DRILL_ROOT, "postgres")
    restored = run_stage(
        io,
        "postgres",
        pgbackrest.run_argv(
            DRILL_ROOT,
            "restore",
            "--delta",
            f"--set={manifest.pgbackrest_label}",
            "--type=immediate",
            "--archive-mode=off",
        ),
        "restored the drill Postgres database",
        postgres_fix,
        timeout=_DRILL_TIMEOUT,
    )
    if not restored.ok:
        return restored
    started = run_stage(
        io,
        "postgres",
        stack.compose_argv(DRILL_ROOT, "up", "-d", "postgres"),
        "started drill Postgres",
        postgres_fix,
    )
    if not started.ok:
        return started
    ready, detail, _ = apply.wait_for_services(
        io,
        DRILL_ROOT,
        ("postgres",),
        sleep,
        exact=False,
        require_healthy=True,
    )
    if not ready:
        return StageResult(
            "postgres",
            False,
            detail,
            stack.logs_fix(DRILL_ROOT, "postgres"),
        )
    return StageResult("postgres", True, "drill Postgres is healthy", "")


def _counts_stage(
    io: Host,
    *,
    manifest: backupset.Manifest,
) -> StageResult:
    for database, expected_mapping in manifest.row_counts.items():
        if IDENTIFIER.fullmatch(database) is None:
            return StageResult("counts", False, f"invalid database name {database}", _COUNTS_FIX)
        expected = dict(expected_mapping)
        statements: list[str] = []
        for table in expected:
            parts = table_parts(table)
            if parts is None:
                return StageResult("counts", False, f"invalid table name {table}", _COUNTS_FIX)
            schema, relation = parts
            statements.append(f"SELECT '{table}', count(*) FROM {schema}.{relation};")
        argv = psql_argv(DRILL_ROOT, database)
        try:
            result = io.run(
                argv,
                input="\n".join(statements) + ("\n" if statements else ""),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return StageResult("counts", False, f"row count query failed for {database}: {exc}", _COUNTS_FIX)
        if result.returncode != 0:
            return StageResult(
                "counts",
                False,
                f"row count query failed for {database}: {command_detail(result)}",
                _COUNTS_FIX,
            )
        actual: dict[str, int] = {}
        for line in result.stdout.splitlines():
            name, separator, count_text = line.partition("|")
            if not separator or not count_text.isdecimal():
                return StageResult("counts", False, f"invalid row count output for {database}", _COUNTS_FIX)
            actual[name] = int(count_text)
        mismatches = sorted(
            name
            for name in set(expected) | set(actual)
            if expected.get(name) != actual.get(name)
        )
        if mismatches:
            return StageResult(
                "counts",
                False,
                "row counts differ for: " + ", ".join(mismatches),
                _COUNTS_FIX,
            )
    return StageResult("counts", True, "restored row counts match the manifest", "")


def _frontend_stage(
    io: Host,
    *,
    sleep: Callable[[float], None],
    client_factory: Callable[[], owui.Client] | None,
) -> StageResult:
    started = run_stage(
        io,
        "frontend",
        stack.compose_argv(DRILL_ROOT, "up", "-d", "open-webui"),
        "started the drill frontend",
        stack.logs_fix(DRILL_ROOT, "open-webui"),
    )
    if not started.ok:
        return started
    try:
        client = (
            client_factory()
            if client_factory is not None
            else owui.Client(f"http://127.0.0.1:{DRILL_PORT}")
        )
        ready = owui.wait_ready(client, attempts=60, sleep=sleep)
    except (owui.OwuiError, OSError, subprocess.SubprocessError) as exc:
        return StageResult(
            "frontend",
            False,
            f"drill frontend readiness failed: {exc}",
            stack.logs_fix(DRILL_ROOT, "open-webui"),
        )
    if not ready.ok:
        return StageResult(
            "frontend",
            False,
            ready.problem or "drill frontend is not ready",
            stack.logs_fix(DRILL_ROOT, "open-webui"),
        )
    return StageResult("frontend", True, "drill frontend is healthy after migrations", "")


def _audit_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    context: _DrillContext,
    started: datetime,
    finished: datetime,
    checks: Mapping[str, str],
    result: str,
) -> StageResult:
    assert context.local_set.manifest is not None
    row = audit.AuditRow(
        str(uuid.uuid4()),
        "backup_drill",
        None,
        None,
        None,
        (),
        {
            "set_label": context.local_set.label,
            "pgbackrest_label": context.local_set.manifest.pgbackrest_label,
            "duration_s": max(0.0, (finished - started).total_seconds()),
            "checks": dict(checks),
            "result": result,
        },
    )
    problem = audit.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(
            "audit",
            False,
            f"backup drill audit write failed: {problem}",
            stack.logs_fix(rendered_dir, "postgres"),
        )
    return StageResult("audit", True, f"backup drill {result}", "")


def run_backup_drill(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    root: PathLike | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    client_factory: Callable[[], owui.Client] | None = None,
) -> int:
    """Run the throwaway restore drill and always tear it down after prepare."""

    del args
    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _ROOT_FIX)
    effective_now = aware_now(now)
    if effective_now is None:
        return _refuse(
            "backup drill's clock value must be timezone-aware.",
            "Use an aware UTC time, then retry.",
        )
    checkout = Path(__file__).parents[2] if root is None else Path(root)
    context = _context(
        io,
        rendered_dir=rendered_dir,
        site_path=site_path,
        checkout=checkout,
    )
    if context is None:
        return 1

    before = _teardown_before(io)
    print_stage(before)
    if not before.ok:
        return 1

    assert context.local_set.manifest is not None
    checks: dict[str, str] = {
        "tarball": "skipped",
        "verify": "skipped",
        "postgres": "skipped",
        "counts": "skipped",
        "frontend": "skipped",
        "cas_walk": "skipped",
        "retrieval_read": "skipped",
    }
    failed = False
    teardown: StageResult | None = None
    try:
        prepare = _prepare_stage(io, context, rendered_dir=rendered_dir)
        print_stage(prepare)
        failed = not prepare.ok

        if not failed:
            tarball = _tarball_stage(io, context)
            checks["tarball"] = "pass" if tarball.ok else "failed"
            print_stage(tarball)
            failed = not tarball.ok

        if not failed:
            verify = _verify_stage(io)
            checks["verify"] = "pass" if verify.ok else "failed"
            print_stage(verify)
            failed = not verify.ok

        if not failed:
            postgres = _postgres_stage(
                io,
                sleep=sleep,
                manifest=context.local_set.manifest,
            )
            checks["postgres"] = "pass" if postgres.ok else "failed"
            print_stage(postgres)
            failed = not postgres.ok

        if not failed:
            counts = _counts_stage(io, manifest=context.local_set.manifest)
            checks["counts"] = "pass" if counts.ok else "failed"
            print_stage(counts)
            failed = not counts.ok

        if not failed:
            frontend = _frontend_stage(
                io,
                sleep=sleep,
                client_factory=client_factory,
            )
            checks["frontend"] = "pass" if frontend.ok else "failed"
            print_stage(frontend)
            failed = not frontend.ok

        inert_cas = StageResult(
            "cas-walk",
            True,
            "inert (no content-addressed store until slice 3)",
            "",
        )
        inert_retrieval = StageResult(
            "retrieval-read",
            True,
            "inert (no index until slice 3)",
            "",
        )
        checks["cas_walk"] = "inert"
        checks["retrieval_read"] = "inert"
        print_stage(inert_cas)
        print_stage(inert_retrieval)
    finally:
        teardown = _teardown_after(io)
        print_stage(teardown)

    assert teardown is not None
    result = "pass" if not failed and teardown.ok else "failed"
    audit_result = _audit_stage(
        io,
        rendered_dir,
        context=context,
        started=effective_now,
        finished=effective_now if now is not None else datetime.now(UTC),
        checks=checks,
        result=result,
    )
    print_stage(audit_result)
    return int(result != "pass" or not audit_result.ok)


def _teardown_after(io: Host) -> StageResult:
    down = run_stage(
        io,
        "teardown",
        stack.compose_argv(DRILL_ROOT, "down", "-v", "--remove-orphans"),
        "tore down the drill project",
        _PREPARE_FIX,
    )
    cleanup_failure = _cleanup(io, stage="teardown", fix=_PREPARE_FIX)
    if not down.ok:
        return down
    if cleanup_failure is not None:
        return cleanup_failure
    return StageResult("teardown", True, "removed the drill project", "")

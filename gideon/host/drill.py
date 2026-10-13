"""The isolated backup restore drill, its store hash walk, and a verified chunk read."""

import base64
import binascii
import hashlib
import os
import re
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
from gideon.host import (
    apply,
    audit,
    backuproots,
    backupset,
    cas,
    owui,
    pgbackrest,
    report,
    site,
    stack,
)
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
from gideon.host.sysio import Host, PathLike, ReadBytesHost, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_PREPARE_FIX: Final = "Repair the drill directory and render inputs, then retry."
_TARBALL_FIX: Final = "Repair the backup set's secrets tarball, then retry."
_VERIFY_FIX: Final = "Repair the pgBackRest repository, then retry."
_COUNTS_FIX: Final = "Repair the restored database, then retry."
_DRILL_TIMEOUT: Final = 18000.0
_AGE_MAGIC: Final = b"age-encryption.org/v1"
_CLEANUP_NAMES: Final = ("postgres", "openwebui", "compose.yaml", "walk")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def _root_fix() -> str:
    return f"Run {report.command('backup drill')} as root, then retry."


def _apply_fix() -> str:
    return f"Run {report.command('apply')}, then retry."


def _tools_fix() -> str:
    return f"Run {report.command('host provision --only host-tools')}, then retry."


def _set_fix() -> str:
    return f"Run {report.command('backup run')}, then retry."


def _age_fix() -> str:
    return f"Run {report.command('backup run')}, then retry."


def _walk_fix() -> str:
    return (
        "Remove the named object's path from the set's copy; where the live store's read refuses it, "
        "move that path aside and write its bytes again from their source "
        "(docs/runbooks/backup-restore.md §4); then run "
        f"{report.command('backup run')}, then retry."
    )


def _read_fix() -> str:
    return f"Run {report.command('backup run')}, then retry."


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
        _refuse(f"cannot inspect drill prerequisites: {exc}.", _tools_fix())
        return None
    if not has_bash:
        _refuse("backup drill tool is missing: /usr/bin/bash.", _tools_fix())
        return None

    compose_path = Path(rendered_dir) / "compose.yaml"
    try:
        has_compose = io.exists(compose_path)
    except OSError as exc:
        _refuse(f"cannot inspect rendered Compose state: {exc}.", _apply_fix())
        return None
    if not has_compose:
        _refuse(f"rendered Compose file is missing: {compose_path}.", _apply_fix())
        return None

    try:
        sets = backupset.list_sets(io)
    except OSError as exc:
        _refuse(f"cannot list local backup sets: {exc}.", _set_fix())
        return None
    local_set = next(
        (ref for ref in sets if ref.complete and ref.manifest is not None), None
    )
    if local_set is None:
        _refuse("no complete local backup set is available.", _set_fix())
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

    assert context.local_set.manifest is not None
    source = backuproots.carrying(
        backuproots.held(
            io,
            backupset.inventory_roots(context.local_set.manifest.checkout),
            context.local_set,
        ),
        backupset.FRONTEND_UPLOADS,
    ).copy
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
        return Problem("Age ciphertext is not bytes.", _age_fix())
    lines = data.splitlines()
    if not lines or lines[0] != _AGE_MAGIC:
        return Problem("Age ciphertext has the wrong header.", _age_fix())
    mac = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.startswith(b"---")),
        None,
    )
    if mac is None:
        return Problem("Age ciphertext has no header MAC line.", _age_fix())
    stanza_count = sum(line.startswith(b"-> X25519 ") for line in lines[1:mac])
    if stanza_count == 0:
        return Problem("Age ciphertext has no X25519 recipient stanza.", _age_fix())
    if stanza_count != recipients:
        return Problem(
            f"Age ciphertext has {stanza_count} X25519 recipient stanza(s); "
            f"the manifest names {recipients}.",
            _age_fix(),
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
    drifted: list[tuple[str, int]] = []
    missing: list[str] = []
    emptied: list[str] = []
    tables = 0
    listing_sql = (
        "SELECT schemaname||'.'||relname FROM pg_stat_user_tables "
        "ORDER BY schemaname, relname;\n"
    )
    for database, expected_mapping in manifest.row_counts.items():
        if IDENTIFIER.fullmatch(database) is None:
            return StageResult("counts", False, f"invalid database name {database}", _COUNTS_FIX)
        expected = dict(expected_mapping)
        tables += len(expected)
        if any(table_parts(table) is None for table in expected):
            return StageResult("counts", False, f"invalid table name for {database}", _COUNTS_FIX)
        argv = psql_argv(DRILL_ROOT, database, on_error_stop=True)
        try:
            listing = io.run(argv, input=listing_sql)
        except (OSError, subprocess.SubprocessError) as exc:
            return StageResult("counts", False, f"table listing failed for {database}: {exc}", _COUNTS_FIX)
        if listing.returncode != 0:
            return StageResult(
                "counts", False, f"table listing failed for {database}: {command_detail(listing)}", _COUNTS_FIX
            )
        # backup run lists the same way and counts only these names, so the manifest holds no other.
        inventory = {line.strip() for line in listing.stdout.splitlines() if table_parts(line.strip()) is not None}
        missing.extend(set(expected) - inventory)
        present = set(expected) & inventory
        if not present:
            continue
        statements: list[str] = []
        for table in sorted(present):
            parts = table_parts(table)
            assert parts is not None
            schema, relation = parts
            statements.append(f"SELECT '{table}', count(*) FROM {schema}.{relation};")
        try:
            result = io.run(
                argv,
                input="\n".join(statements) + "\n",
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
            if (
                not separator or not count_text.isascii() or not count_text.isdecimal()
                or name not in present or name in actual
            ):
                return StageResult("counts", False, f"invalid row count output for {database}", _COUNTS_FIX)
            actual[name] = int(count_text)
        if set(actual) != present:
            return StageResult(
                "counts", False, f"row counts were missing for {database}: {', '.join(sorted(present - set(actual)))}", _COUNTS_FIX
            )
        for name, count in actual.items():
            if expected[name] > 0 and count == 0:
                emptied.append(name)
            elif count != expected[name]:
                drifted.append((name, count - expected[name]))
    clauses: list[str] = []
    if drifted:
        differences = ", ".join(f"{name} {difference:+d}" for name, difference in sorted(drifted))
        clauses.append(f"{len(drifted)} table(s) drift from the manifest: {differences} (the writer's rows since the boundary)")
    if missing:
        clauses.append("absent from the restored database: " + ", ".join(sorted(missing)))
    if emptied:
        clauses.append("restored empty: " + ", ".join(sorted(emptied)))
    detail = "; ".join(clauses) if clauses else f"restored row counts match the manifest ({tables} tables)"
    return StageResult("counts", True, detail, "")


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


def _store_root(io: Host, context: _DrillContext) -> backuproots.HeldRoot:
    """Bind the set's copy of the content-addressed store."""

    assert context.local_set.manifest is not None
    return backuproots.carrying(
        backuproots.held(
            io, backupset.inventory_roots(context.local_set.manifest.checkout), context.local_set
        ),
        os.fspath(cas.ROOT),
    )


def _cas_walk_stage(
    io: Host,
    context: _DrillContext,
    measured: bool,
    clock: Callable[[], float],
) -> tuple[StageResult, int | None, float]:
    """Hash the set's store copy and check the restored documents' object references."""

    started = clock() if measured else 0.0
    objects: int | None = None

    def result(ok: bool, detail: str, fix: str = "") -> tuple[StageResult, int | None, float]:
        seconds = max(0.0, clock() - started) if measured else 0.0
        if ok:
            detail += f"; {seconds:.1f} s"
        return StageResult("cas-walk", ok, detail, "" if ok else fix or _walk_fix()), objects, seconds

    root = _store_root(io, context)
    problem = root.verify()
    if problem is not None:
        return result(False, problem.problem)
    scratch_dir = _drill_path("walk")
    try:
        io.mkdir(scratch_dir, mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return result(False, f"store walk directory could not be prepared: {exc}")
    walk = root.walk(scratch_dir)
    if isinstance(walk, Problem):
        return result(False, walk.problem, walk.fix)
    objects = walk.objects
    if walk.failed:
        return result(
            False,
            f"{len(walk.failed)} object(s) do not hash to their names: {', '.join(walk.failed[:10])}",
        )
    names = frozenset(walk.names)
    statement = (
        "SELECT doc_id, coalesce(sha256, ''), coalesce(canonical_text_sha256, '') "
        "FROM documents WHERE sha256 IS NOT NULL OR canonical_text_sha256 IS NOT NULL;\n"
    )
    try:
        queried = io.run(psql_argv(DRILL_ROOT, "gideon", on_error_stop=True), input=statement)
    except (OSError, subprocess.SubprocessError) as exc:
        return result(False, f"document object query failed: {exc}")
    if queried.returncode != 0:
        return result(False, f"document object query failed: {command_detail(queried)}")
    documents = 0
    missing = 0
    missing_ids: list[str] = []
    for line in queried.stdout.splitlines():
        fields = line.split("|")
        if (
            len(fields) != 3
            or _SHA256.fullmatch(fields[0]) is None
            or any(name and _SHA256.fullmatch(name) is None for name in fields[1:])
        ):
            return result(False, "document object query returned a malformed line")
        documents += 1
        if any(name and name not in names for name in fields[1:]):
            missing += 1
            if len(missing_ids) < 10:
                missing_ids.append(fields[0])
    if missing:
        return result(False, f"{missing} document row(s) name objects the set lacks: {', '.join(missing_ids)}")
    return result(
        True,
        f"walked {walk.objects} object(s) ({walk.bytes} bytes), every object hashes to its name; "
        f"{documents} document row(s) name objects the set holds",
    )


@dataclass(frozen=True, slots=True)
class _ChunkRow:
    """One restored chunk's identifiers, object, code-point offsets, hash, and tokens."""

    chunk_id: str
    doc_id: str
    object_name: str
    char_start: int
    char_end: int
    content_hash: str
    token_count: int


def _retrieval_read_stage(
    io: ReadBytesHost,
    context: _DrillContext,
    measured: bool,
    clock: Callable[[], float],
) -> tuple[StageResult, float]:
    """Read one restored chunk from its verified object without exposing its text."""

    started = clock() if measured else 0.0

    def result(ok: bool, detail: str, *, show_seconds: bool = True) -> tuple[StageResult, float]:
        seconds = max(0.0, clock() - started) if measured else 0.0
        if ok and show_seconds:
            detail += f"; {seconds:.1f} s"
        return StageResult("retrieval-read", ok, detail, "" if ok else _read_fix()), seconds

    statement = (
        "SELECT c.chunk_id, c.doc_id, d.canonical_text_sha256, c.char_start, c.char_end, "
        "c.content_hash, c.token_count FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
        "ORDER BY c.chunk_id LIMIT 1;\n"
    )
    try:
        queried = io.run(psql_argv(DRILL_ROOT, "gideon", on_error_stop=True), input=statement)
    except (OSError, subprocess.SubprocessError) as exc:
        return result(False, f"chunk query failed: {exc}")
    if queried.returncode != 0:
        return result(False, f"chunk query failed: {command_detail(queried)}")
    if not queried.stdout:
        return result(True, "the restored record holds no chunk; nothing to read", show_seconds=False)
    lines = queried.stdout.splitlines()
    fields = lines[0].split("|")
    if (
        len(lines) != 1 or len(fields) != 7
        or any(_SHA256.fullmatch(fields[index]) is None for index in (0, 1, 2, 5))
        or any(not fields[index].isascii() or not fields[index].isdecimal() for index in (3, 4, 6))
    ):
        return result(False, "chunk query returned a malformed row")
    chunk = _ChunkRow(fields[0], fields[1], fields[2], int(fields[3]), int(fields[4]), fields[5], int(fields[6]))
    if chunk.char_start >= chunk.char_end or chunk.token_count <= 0:
        return result(False, "chunk query returned a malformed row")
    data = cas.get(io, chunk.object_name, root=_store_root(io, context).copy)
    if isinstance(data, Problem):
        return result(False, data.problem)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return result(False, f"object {chunk.object_name} is not UTF-8")
    content = text[chunk.char_start:chunk.char_end]
    if not content or hashlib.sha256(content.encode("utf-8")).hexdigest() != chunk.content_hash:
        return result(False, f"chunk {chunk.chunk_id} text does not hash to its content hash")
    return result(
        True,
        f"read chunk {chunk.chunk_id} of document {chunk.doc_id}: {len(content)} character(s), "
        f"{chunk.token_count} token(s), from object {chunk.object_name}",
    )


def _audit_stage(
    io: Host,
    rendered_dir: PathLike,
    *,
    context: _DrillContext,
    started: datetime,
    finished: datetime,
    checks: Mapping[str, str],
    objects: int | None,
    seconds: Mapping[str, float],
    result: str,
) -> StageResult:
    assert context.local_set.manifest is not None
    detail: dict[str, object] = {
        "set_label": context.local_set.label,
        "pgbackrest_label": context.local_set.manifest.pgbackrest_label,
        "duration_s": max(0.0, (finished - started).total_seconds()),
        "checks": dict(checks),
        "seconds": dict(seconds),
        "result": result,
    }
    if objects is not None:
        detail["objects"] = objects
    row = audit.AuditRow(
        str(uuid.uuid4()),
        "backup_drill",
        None,
        None,
        None,
        (),
        detail,
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
    host: ReadBytesHost | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    root: PathLike | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    client_factory: Callable[[], owui.Client] | None = None,
) -> int:
    """Restore, walk the store, verify a chunk's text, and tear down the drill."""

    del args
    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _root_fix())
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
    measured = now is None
    seconds = {"cas_walk": 0.0, "retrieval_read": 0.0}
    objects: int | None = None
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

        if not failed:
            walk, objects, seconds["cas_walk"] = _cas_walk_stage(io, context, measured, clock)
            checks["cas_walk"] = "pass" if walk.ok else "failed"
            print_stage(walk)
            failed = not walk.ok

        if not failed:
            read, seconds["retrieval_read"] = _retrieval_read_stage(io, context, measured, clock)
            checks["retrieval_read"] = "pass" if read.ok else "failed"
            print_stage(read)
            failed = not read.ok
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
        objects=objects,
        seconds=seconds,
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

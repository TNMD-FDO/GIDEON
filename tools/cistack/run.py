"""Run stages against the CI sibling whose containers mount the last converged checkout."""

from __future__ import annotations

import json
import subprocess
import time
from argparse import Namespace
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from gideon.evaluation import command as evaluation_command
from gideon.host import (
    apply,
    backuplock,
    nogpu,
    owui,
    render,
    secrets,
    site,
    stack,
    stages,
    stores,
)
from gideon.host.images import load_image_lock
from gideon.host.index.lexical import live_index_job
from gideon.host.lock import load_host_lock
from gideon.host.models import load_models_lock
from gideon.host.render import ARTIFACTS
from gideon.host.render.api import (
    API_HEALTH_PATH,
    API_INSTRUCTION_PATH,
    API_SERVICE_NAME,
)
from gideon.host.render.ci import (
    CI_OPENSEARCH_DATA_ROOT,
    CI_OPENSEARCH_RENDERED_DIR,
    CI_PROJECT,
    CI_QDRANT_DATA_ROOT,
    CI_ROOT,
    CI_SECRET_NAMES,
    CI_SKIPPED_SECRETS,
    CI_STACK,
    CI_WIPE_PATHS,
    CI_WORK_ROOT,
    EMBED_RELAY_SERVICE_NAME,
    RECORD_RELAY_SERVICE_NAME,
    RELAY_SERVICE_NAME,
    ci_compose_document,
    ci_env_file,
    ci_instruction,
    ci_manifest,
)
from gideon.host.render.command import load_render_inputs
from gideon.host.render.compose import (
    INTERNAL_NETWORK_NAME,
    container_name,
    network_name,
)
from gideon.host.render.engine import ENGINE_PORT, INTEGRATION_NETWORK_NAME
from gideon.host.render.opensearch import (
    OPENSEARCH_GID,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_UID,
)
from gideon.host.render.qdrant import QDRANT_SERVICE_NAME
from gideon.host.render.worker import DIR_MODE, WORKER_SERVICE_NAME
from gideon.host.render.yamlout import dump
from gideon.host.report import (
    Problem,
    StageResult,
    command,
    command_detail,
    print_stage,
)
from gideon.host.sysio import Host, LockingHost, PathLike

CI_SERVICES: Final[tuple[str, ...]] = (
    "postgres",
    "open-webui",
    QDRANT_SERVICE_NAME,
    OPENSEARCH_SERVICE_NAME,
    API_SERVICE_NAME,
    WORKER_SERVICE_NAME,
    RELAY_SERVICE_NAME,
    RECORD_RELAY_SERVICE_NAME,
    EMBED_RELAY_SERVICE_NAME,
)
_ROOT_FIX: Final = "Run sudo python3 -m tools.cistack <up|smoke|down|status>, then retry."
_UP_ROOT_FIX: Final = "Run sudo python3 -m tools.cistack up, then retry."
_DOWN_ROOT_FIX: Final = "Run sudo python3 -m tools.cistack down, then retry."
_SMOKE_ROOT_FIX: Final = "Run sudo python3 -B -m tools.cistack smoke, then retry."
_SMOKE_FAILED_FIX: Final = "Read the eval run rows above, the gate's first, then retry."
_DATA_FIX: Final = "Run sudo python3 -m gideon host provision, then retry."
_NETWORK_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_NETWORK_ABSENT_MARK: Final = "network {} not found"
_BACKUP_LOCK_READ_FIX: Final = "Repair access to the backup lock and the kernel lock table, then retry."
_NOGPU_FIX: Final = (
    f"Remove {nogpu.NO_GPU_PATH} on a GPU host, then retry."
)
_RENDER_FIX: Final = "Correct the CI render inputs, then retry."
_FOREIGN_FIX: Final = "Remove or move the foreign file(s), then retry."
_SECRET_FIX: Final = "Correct the CI secrets directory, then retry."
_WIPE_FIX: Final = f"Correct ownership and mode under {CI_ROOT}, then retry."
# Production's `compose down` leaves both shared networks in use by the relays.
_RELAY_REMINDER: Final = (
    "; the relays keep the integration and internal networks in use, so run sudo python3 -m tools.cistack down before a restore or upgrade --rollback"
)
_HEALTH_SCRIPT: Final = (
    "import urllib.request; "
    f"raise SystemExit(urllib.request.urlopen('http://127.0.0.1:{ENGINE_PORT}{API_HEALTH_PATH}', "
    "timeout=4).status != 200)"
)
_OPENSEARCH_ARTIFACTS: Final = tuple(
    artifact for artifact in ARTIFACTS if OPENSEARCH_SERVICE_NAME in artifact.owners
)
_OPENSEARCH_PATHS: Final = tuple(
    PurePosixPath(artifact.relative_path) for artifact in _OPENSEARCH_ARTIFACTS
)
_OPENSEARCH_DIRECTORIES: Final = tuple(
    sorted({path.parent for path in _OPENSEARCH_PATHS}, key=str)
)
_RENDERED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "compose.yaml",
        "open-webui",
        API_SERVICE_NAME,
        PurePosixPath(CI_OPENSEARCH_RENDERED_DIR).name,
    }
)
_PERSISTENT_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "postgres",
        "openwebui",
        "secrets",
        PurePosixPath(CI_WORK_ROOT).name,
        PurePosixPath(CI_QDRANT_DATA_ROOT).name,
        PurePosixPath(CI_OPENSEARCH_DATA_ROOT).name,
    }
)
# Each rendered directory and the names the render stage writes inside it.
_RENDERED_DIRECTORY_NAMES: Final[Mapping[str, frozenset[str]]] = {
    "open-webui": frozenset({"env", "manifest.yaml"}),
    API_SERVICE_NAME: frozenset({PurePosixPath(API_INSTRUCTION_PATH).name}),
    **{
        str(directory): frozenset(
            path.relative_to(directory).parts[0]
            for path in _OPENSEARCH_PATHS
            if path.is_relative_to(directory)
        )
        for directory in _OPENSEARCH_DIRECTORIES
    },
}
_LOCK_ACCESS_FIX: Final = "Repair access to the engine lock, then retry."


@dataclass(frozen=True, slots=True)
class CiStack:
    """The fixed CI project paths and the checkout that supplies its image code."""

    project_dir: Path
    secrets_dir: Path
    base_url: str
    checkout: Path


@dataclass(frozen=True, slots=True)
class _NetworkReading:
    """The production network refusal and any network Docker reports absent."""

    refusal: StageResult | None
    absent: str | None


def _error_row(name: str, errors: Sequence[object], fallback: str) -> StageResult:
    error = errors[0] if errors else None
    problem = getattr(error, "problem", None) or fallback
    fix = getattr(error, "fix", None) or _RENDER_FIX
    return StageResult(name, False, str(problem), str(fix))


def _root_row(io: Host, command: str) -> StageResult | None:
    if io.geteuid() == 0:
        return None
    fix = {"up": _UP_ROOT_FIX, "smoke": _SMOKE_ROOT_FIX}.get(command, _DOWN_ROOT_FIX)
    return StageResult("preconditions", False, "root is required", fix)


def _inspect_networks(io: Host) -> _NetworkReading:
    """Inspect production's networks, distinguishing absence from other failures."""

    for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME):
        try:
            network = io.run(["docker", "network", "inspect", network_name(key)])
        except (OSError, subprocess.SubprocessError) as exc:
            return _NetworkReading(
                StageResult(
                    "preconditions", False,
                    f"{key} Docker network could not be inspected: {exc}", _NETWORK_FIX,
                ),
                None,
            )
        if network.returncode != 0:
            detail = command_detail(network)
            return _NetworkReading(
                StageResult(
                    "preconditions", False,
                    f"{key} Docker network is unavailable: {detail}", _NETWORK_FIX,
                ),
                key if _NETWORK_ABSENT_MARK.format(network_name(key)) in detail else None,
            )
    return _NetworkReading(None, None)


def _preconditions(
    ci_stack: CiStack,
    io: Host,
    *,
    site_path: PathLike,
    lock_detail: str = "",
    inspect_networks: bool = True,
) -> StageResult:
    root = _root_row(io, "up")
    if root is not None:
        return root
    try:
        if nogpu.is_no_gpu_host(io):
            return StageResult(
                "preconditions",
                False,
                "the CI stack needs the production engine on a GPU host",
                _NOGPU_FIX,
            )
        if not io.exists(ci_stack.project_dir):
            return StageResult(
                "preconditions",
                False,
                f"CI data root is missing: {ci_stack.project_dir}",
                _DATA_FIX,
            )
    except OSError as exc:
        return StageResult(
            "preconditions",
            False,
            f"CI host state could not be inspected: {exc}",
            _DATA_FIX,
        )

    loaded_site = site.load_site(Path(site_path), host=io)
    if loaded_site.errors or loaded_site.config is None:
        return _error_row(
            "preconditions",
            loaded_site.errors,
            f"site file is unavailable: {site_path}",
        )
    lock_result = load_host_lock(ci_stack.checkout / "host.lock", host=io)
    if lock_result.errors or lock_result.lock is None:
        return _error_row("preconditions", lock_result.errors, "host.lock is unavailable")
    image_result = load_image_lock(ci_stack.checkout / "images.lock", host=io)
    if image_result.errors or image_result.lock is None:
        return _error_row("preconditions", image_result.errors, "images.lock is unavailable")
    models_result = load_models_lock(ci_stack.checkout / "models.lock", host=io)
    if models_result.errors or models_result.lock is None:
        return _error_row("preconditions", models_result.errors, "models.lock is unavailable")

    if inspect_networks:
        reading = _inspect_networks(io)
        if reading.refusal is not None:
            return reading.refusal
    return StageResult(
        "preconditions",
        True,
        "root, render inputs, CI data root, integration network, and internal network are available"
        + (f"; {lock_detail}" if lock_detail else ""),
        "",
    )


def _engine_lock(
    io: LockingHost, command: str
) -> tuple[backuplock.Outcome | None, StageResult]:
    try:
        outcome = backuplock.take(
            io,
            command=command,
            now=datetime.now(UTC),
            lock=backuplock.ENGINE_LOCK,
        )
    except OSError as exc:
        return None, StageResult(
            "preconditions", False, f"engine lock could not be taken: {exc}", _LOCK_ACCESS_FIX
        )
    if outcome.state is backuplock.State.REFUSED:
        problem = outcome.problem
        return outcome, StageResult(
            "preconditions",
            False,
            problem.problem if problem is not None else "engine lock is held",
            problem.fix if problem is not None else _LOCK_ACCESS_FIX,
        )
    detail = (
        "engine lock taken"
        if outcome.state is backuplock.State.HELD
        else "engine lock held by this process"
    )
    return outcome, StageResult("preconditions", True, detail, "")


def _release_engine_lock(io: LockingHost, outcome: backuplock.Outcome | None) -> None:
    if outcome is not None and outcome.state is backuplock.State.HELD:
        backuplock.release(io, lock=backuplock.ENGINE_LOCK)


def _backup_lock_skip(io: Host) -> StageResult | None:
    """Describe a backup hold's skip or a failure to read the holder."""

    try:
        holding = backuplock.held_by(io, lock=backuplock.BACKUP_LOCK)
    except OSError as exc:
        return StageResult(
            "preconditions", False,
            f"the backup lock could not be read: {exc}", _BACKUP_LOCK_READ_FIX,
        )
    if not holding.held:
        return None
    record = holding.record
    detail = (
        "the backup lock is held by another gideon command, its record unreadable"
        if record is None
        else f"the backup lock is held by {record.command} since {record.started.isoformat()} "
        f"(pid {record.pid})"
    )
    return StageResult(
        "skipped", True,
        f"{detail}; the box is held, the sibling untouched, nothing evaluated, nothing recorded",
        "",
    )


def _live_index_job(ci_stack: CiStack, io: Host, verb: str) -> int | None | StageResult:
    """Read the sibling's index queue; a read failure is the refusing row."""

    job = live_index_job(io, ci_stack.project_dir, f"sudo python3 -m tools.cistack {verb}")
    if isinstance(job, Problem):
        return StageResult("preconditions", False, job.problem, job.fix)
    return job


def _secrets_stage(
    ci_stack: CiStack,
    io: Host,
    *,
    site_path: PathLike,
) -> tuple[StageResult, render.RenderInputs | None]:
    secrets.select_directory(ci_stack.secrets_dir)
    try:
        gid = secrets.service_group_gid(io)
        if gid is None:
            return (
                StageResult("secrets", False, secrets.SERVICE_GROUP_PROBLEM, _DATA_FIX),
                None,
            )
        io.mkdir(ci_stack.secrets_dir, mode=0o750, parents=True, exist_ok=True)
        io.chown(ci_stack.secrets_dir, 0, gid)
    except OSError as exc:
        return (
            StageResult(
                "secrets",
                False,
                f"CI secrets directory could not be prepared: {exc}",
                _SECRET_FIX,
            ),
            None,
        )
    try:
        ensured = secrets.ensure_generated(io, skip=CI_SKIPPED_SECRETS)
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("secrets", False, f"generated secrets could not be ensured: {exc}", _SECRET_FIX),
            None,
        )
    if not ensured.ok:
        return (
            StageResult(
                "secrets",
                False,
                ensured.problem or "generated secrets could not be ensured",
                ensured.fix or _SECRET_FIX,
            ),
            None,
        )

    loaded = load_render_inputs(
        io,
        site_path=site_path,
        lock_path=ci_stack.checkout / "host.lock",
        images_path=ci_stack.checkout / "images.lock",
        models_path=ci_stack.checkout / "models.lock",
        root=ci_stack.checkout,
        command="tools.cistack up",
        secret_names=CI_SECRET_NAMES,
    )
    if loaded is None:
        return (
            StageResult("secrets", False, "render inputs could not be loaded", _RENDER_FIX),
            None,
        )
    inputs = loaded[0]
    detail = (
        f"created generated secrets: {', '.join(ensured.created)}"
        if ensured.created
        else "generated secrets are current"
    )
    return StageResult("secrets", True, detail, ""), inputs


def _directory_files(io: Host, path: Path) -> tuple[str, ...]:
    if not io.exists(path):
        return ()
    return tuple(io.listdir(path))


def _foreign_files(io: Host, project_dir: Path) -> tuple[str, ...]:
    foreign: list[str] = []
    for name in io.listdir(project_dir):
        if name in _RENDERED_NAMES or name in _PERSISTENT_DIRECTORY_NAMES:
            continue
        foreign.append(str(project_dir / name))
    for directory, names in _RENDERED_DIRECTORY_NAMES.items():
        for name in _directory_files(io, project_dir / directory):
            if name not in names:
                foreign.append(str(project_dir / directory / name))
    return tuple(sorted(foreign))


def _render_stage(
    ci_stack: CiStack,
    io: Host,
    inputs: render.RenderInputs,
) -> StageResult:
    try:
        foreign = _foreign_files(io, ci_stack.project_dir)
    except OSError as exc:
        return StageResult("render", False, f"CI render directory is unreadable: {exc}", _RENDER_FIX)
    if foreign:
        return StageResult(
            "render",
            False,
            "foreign file(s) under CI data root: " + ", ".join(foreign),
            _FOREIGN_FIX,
        )
    try:
        compose_text = dump(ci_compose_document(inputs))
        env_text = ci_env_file(inputs)
        manifest_text = ci_manifest(inputs)
        instruction_text = ci_instruction(inputs)
        open_webui = ci_stack.project_dir / "open-webui"
        api_directory = ci_stack.project_dir / API_SERVICE_NAME
        io.mkdir(open_webui, mode=0o755, parents=True, exist_ok=True)
        io.mkdir(api_directory, mode=0o755, parents=True, exist_ok=True)
        io.write_text(ci_stack.project_dir / "compose.yaml", compose_text, mode=0o644)
        io.write_text(open_webui / "env", env_text, mode=0o600)
        io.write_text(open_webui / "manifest.yaml", manifest_text, mode=0o644)
        io.write_text(ci_stack.project_dir / API_INSTRUCTION_PATH, instruction_text, mode=0o644)
        for directory in _OPENSEARCH_DIRECTORIES:
            io.mkdir(ci_stack.project_dir / directory, mode=0o755, parents=True, exist_ok=True)
        for artifact in _OPENSEARCH_ARTIFACTS:
            io.write_text(
                ci_stack.project_dir / artifact.relative_path,
                artifact.emit(inputs),
                mode=artifact.mode,
            )
        qdrant_root = ci_stack.project_dir / PurePosixPath(CI_QDRANT_DATA_ROOT).name
        if not io.exists(qdrant_root):
            gid = secrets.service_group_gid(io)
            if gid is None:
                return StageResult("render", False, secrets.SERVICE_GROUP_PROBLEM, _DATA_FIX)
            io.mkdir(qdrant_root, mode=0o750)
            io.chown(qdrant_root, 0, gid)
        opensearch_root = ci_stack.project_dir / PurePosixPath(CI_OPENSEARCH_DATA_ROOT).name
        if not io.exists(opensearch_root):
            io.mkdir(opensearch_root, mode=0o700)
            io.chown(opensearch_root, OPENSEARCH_UID, OPENSEARCH_GID)
        work_root = ci_stack.project_dir / PurePosixPath(CI_WORK_ROOT).name
        if not io.exists(work_root):
            gid = secrets.service_group_gid(io)
            if gid is None:
                return StageResult("render", False, secrets.SERVICE_GROUP_PROBLEM, _DATA_FIX)
            io.mkdir(work_root, mode=DIR_MODE)
            io.chown(work_root, 0, gid)
            io.chmod(work_root, DIR_MODE)
    except (OSError, TypeError, ValueError) as exc:
        return StageResult("render", False, f"CI render files could not be written: {exc}", _RENDER_FIX)
    return StageResult(
        "render",
        True,
        "wrote the CI Compose document, Open WebUI env file and manifest, API instruction, "
        "and OpenSearch settings and security files; prepared Qdrant and OpenSearch data roots and worker work root",
        "",
    )


def _stores_stage(
    ci_stack: CiStack,
    io: Host,
    *,
    sleep: Callable[[float], None],
) -> StageResult:
    started = stages.run_stage(
        io,
        "stores",
        stack.compose_argv(ci_stack.project_dir, "up", "-d", "postgres"),
        "started CI Postgres",
        stack.logs_fix(ci_stack.project_dir, "postgres"),
    )
    if not started.ok:
        return started
    ready, detail, service = apply.wait_for_services(
        io,
        ci_stack.project_dir,
        ("postgres",),
        sleep,
        exact=False,
        require_healthy=True,
    )
    if not ready:
        return StageResult("stores", False, detail, stack.logs_fix(ci_stack.project_dir, service))
    try:
        converged = stores.converge(io, ci_stack.project_dir, root=ci_stack.checkout)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("stores", False, f"store convergence failed: {exc}", stack.logs_fix(ci_stack.project_dir, "postgres"))
    if not converged.ok:
        return StageResult(
            "stores",
            False,
            converged.problem or "store convergence failed",
            converged.fix or stack.logs_fix(ci_stack.project_dir, "postgres"),
        )
    return StageResult(
        "stores",
        True,
        "CI roles, databases, and migrations are current",
        "",
    )


def _start_stage(ci_stack: CiStack, io: Host) -> StageResult:
    return stages.run_stage(
        io,
        "start",
        stack.compose_argv(ci_stack.project_dir, "up", "-d", "--remove-orphans"),
        "started the CI stack",
        stack.logs_fix(ci_stack.project_dir, RELAY_SERVICE_NAME),
    )


def _manifest_stage(
    ci_stack: CiStack,
    io: Host,
    *,
    client_factory: Callable[..., owui.Client],
    sleep: Callable[[float], None],
) -> StageResult:
    try:
        ready = owui.wait_ready(client_factory(), attempts=60, sleep=sleep)
    except (owui.OwuiError, OSError, subprocess.SubprocessError) as exc:
        return StageResult("apply-manifest", False, f"Open WebUI readiness failed: {exc}", stack.logs_fix(ci_stack.project_dir, "open-webui"))
    if not ready.ok:
        return StageResult(
            "apply-manifest",
            False,
            ready.problem or "Open WebUI is not ready",
            ready.fix or stack.logs_fix(ci_stack.project_dir, "open-webui"),
        )
    try:
        manifest = owui.load_manifest(io, ci_stack.project_dir / "open-webui" / "manifest.yaml")
        result = owui.bootstrap(
            io,
            client_factory,
            manifest,
            rendered_dir=ci_stack.project_dir,
        )
    except (OSError, ValueError, owui.OwuiError) as exc:
        return StageResult("apply-manifest", False, f"Open WebUI bootstrap failed: {exc}", stack.logs_fix(ci_stack.project_dir, "open-webui"))
    if not result.ok:
        return StageResult(
            "apply-manifest",
            False,
            result.problem or "Open WebUI bootstrap failed",
            result.fix or stack.logs_fix(ci_stack.project_dir, "open-webui"),
        )
    changes = (*result.created_groups, *result.updated_groups, *result.minted)
    detail = "frontend state matches the CI manifest"
    if changes:
        detail = "frontend manifest changes: " + ", ".join(changes)
    return StageResult("apply-manifest", True, detail, "")


def _verify_stage(
    ci_stack: CiStack,
    io: Host,
    *,
    client_factory: Callable[..., owui.Client],
    sleep: Callable[[float], None],
) -> StageResult:
    ready, detail, service = apply.wait_for_services(
        io,
        ci_stack.project_dir,
        CI_SERVICES,
        sleep,
        exact=True,
        require_healthy=True,
    )
    if not ready:
        return StageResult("verify", False, detail, stack.logs_fix(ci_stack.project_dir, service))
    try:
        frontend_ready = client_factory().ready()
    except (owui.OwuiError, OSError, subprocess.SubprocessError) as exc:
        return StageResult("verify", False, f"Open WebUI readiness failed: {exc}", stack.logs_fix(ci_stack.project_dir, "open-webui"))
    if not frontend_ready:
        return StageResult("verify", False, "Open WebUI /ready did not report status true", stack.logs_fix(ci_stack.project_dir, "open-webui"))
    try:
        api_health = io.run(
            stack.exec_argv(
                ci_stack.project_dir,
                API_SERVICE_NAME,
                "python3",
                "-c",
                _HEALTH_SCRIPT,
            )
        )
    except OSError as exc:
        return StageResult("verify", False, f"gideon-api health probe failed: {exc}", stack.logs_fix(ci_stack.project_dir, API_SERVICE_NAME))
    if api_health.returncode != 0:
        return StageResult(
            "verify",
            False,
            f"gideon-api health probe failed: {command_detail(api_health)}",
            stack.logs_fix(ci_stack.project_dir, API_SERVICE_NAME),
        )
    return StageResult(
        "verify",
        True,
        "CI services are running and healthy; frontend and gideon-api are ready",
        "",
    )


def _up_stages(
    ci_stack: CiStack,
    io: Host,
    *,
    site_path: PathLike,
    client_factory: Callable[..., owui.Client] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    lock_detail: str = "",
    inspect_networks: bool = True,
) -> int:
    """Run the CI stack's ordered convergence and verification stages."""

    preconditions = _preconditions(
        ci_stack, io, site_path=site_path, lock_detail=lock_detail,
        inspect_networks=inspect_networks,
    )
    print_stage(preconditions)
    if not preconditions.ok:
        return 1
    factory = client_factory or owui.loopback_client_factory(ci_stack.base_url)
    secret_result, inputs = _secrets_stage(ci_stack, io, site_path=site_path)
    print_stage(secret_result)
    if not secret_result.ok or inputs is None:
        return 1
    render_result = _render_stage(ci_stack, io, inputs)
    print_stage(render_result)
    if not render_result.ok:
        return 1
    stores_result = _stores_stage(ci_stack, io, sleep=sleep)
    print_stage(stores_result)
    if not stores_result.ok:
        return 1
    start_result = _start_stage(ci_stack, io)
    print_stage(start_result)
    if not start_result.ok:
        return 1
    manifest_result = _manifest_stage(
        ci_stack,
        io,
        client_factory=factory,
        sleep=sleep,
    )
    print_stage(manifest_result)
    if not manifest_result.ok:
        return 1
    verify_result = _verify_stage(
        ci_stack,
        io,
        client_factory=factory,
        sleep=sleep,
    )
    print_stage(verify_result)
    return int(not verify_result.ok)


def up(
    ci_stack: CiStack,
    io: LockingHost,
    *,
    site_path: PathLike,
    client_factory: Callable[..., owui.Client] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Converge the sibling under the engine lock.

    A convergence recreates the containers a sibling run is driving, so it
    refuses while another process holds the lock.
    """

    root = _root_row(io, "up")
    if root is not None:
        print_stage(root)
        return 1
    claim, lock_row = _engine_lock(io, "tools.cistack up")
    if not lock_row.ok:
        print_stage(lock_row)
        return 1
    try:
        job = _live_index_job(ci_stack, io, "up")
        if isinstance(job, StageResult):
            print_stage(job)
            return 1
        if job is not None:
            print_stage(StageResult(
                "preconditions", False,
                f"a sample build is running on the sibling (job {job})",
                "Wait for the build's job or rejoin it with "
                f"{command('index build caselaw --sample')}, then retry.",
            ))
            return 1
        return _up_stages(
            ci_stack,
            io,
            site_path=site_path,
            client_factory=client_factory,
            sleep=sleep,
            lock_detail=lock_row.detail,
        )
    finally:
        _release_engine_lock(io, claim)


def smoke(
    ci_stack: CiStack,
    io: LockingHost,
    *,
    site_path: PathLike,
    client_factory: Callable[..., owui.Client] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    run_eval: Callable[..., int] | None = None,
) -> int:
    """Converge the sibling from the selected tree and run the push smoke on it.

    The engine lock is taken before anything of the sibling is touched and held
    until the run ends. Four conditions skip in order: the engine lock held,
    the backup lock held, a live sample job, and the production stack down.
    Each prints two ok rows and exits 0, the sibling untouched. The evaluation
    runs in-process under the same lock, which it passes as nested, and reads
    its cases, reference, and provenance from the selected checkout.
    """

    root = _root_row(io, "smoke")
    if root is not None:
        print_stage(root)
        return 1
    claim, lock_row = _engine_lock(io, "tools.cistack smoke")
    if not lock_row.ok:
        if claim is None:
            print_stage(lock_row)
            return 1
        print_stage(StageResult("preconditions", True, lock_row.detail, ""))
        print_stage(
            StageResult(
                "skipped",
                True,
                "engine busy; the sibling untouched, nothing evaluated, nothing recorded",
                "",
            )
        )
        return 0
    try:
        backup_skip = _backup_lock_skip(io)
        if backup_skip is not None and not backup_skip.ok:
            print_stage(backup_skip)
            return 1
        if backup_skip is not None:
            print_stage(StageResult("preconditions", True, lock_row.detail, ""))
            print_stage(backup_skip)
            return 0
        job = _live_index_job(ci_stack, io, "smoke")
        if isinstance(job, StageResult):
            print_stage(job)
            return 1
        if job is not None:
            print_stage(StageResult("preconditions", True, lock_row.detail, ""))
            print_stage(StageResult(
                "skipped", True,
                f"a sample build is running on the sibling (job {job}); "
                "the sibling untouched, nothing evaluated, nothing recorded",
                "",
            ))
            return 0
        networks = _inspect_networks(io)
        if networks.absent is not None:
            print_stage(StageResult("preconditions", True, lock_row.detail, ""))
            print_stage(StageResult(
                "skipped", True,
                f"the production stack is down: the {networks.absent} Docker network is absent; "
                "the sibling untouched, nothing evaluated, nothing recorded",
                "",
            ))
            return 0
        if networks.refusal is not None:
            print_stage(networks.refusal)
            return 1
        result = _up_stages(
            ci_stack,
            io,
            site_path=site_path,
            client_factory=client_factory,
            sleep=sleep,
            lock_detail=lock_row.detail,
            inspect_networks=False,
        )
        if result != 0:
            return result
        evaluator = evaluation_command.run_eval if run_eval is None else run_eval
        code = evaluator(
            Namespace(
                slice="smoke",
                stack=CI_STACK,
                kind="smoke",
                set=None,
                ranked=None,
                decision=False,
                force=False,
            ),
            host=io,
            checkout_root=ci_stack.checkout,
        )
        print_stage(
            StageResult(
                "smoke",
                code == 0,
                f"eval run --slice smoke --stack ci --kind smoke exited {code}",
                "" if code == 0 else _SMOKE_FAILED_FIX,
            )
        )
        return code
    finally:
        _release_engine_lock(io, claim)


def _wipe(ci_stack: CiStack, io: Host) -> StageResult:
    results = [
        stages.run_stage(
            io,
            "wipe",
            ["rm", "-rf", path],
            f"removed {path}",
            _WIPE_FIX,
        )
        for path in CI_WIPE_PATHS
    ]
    failure = next((result for result in results if not result.ok), None)
    if failure is not None:
        return failure
    return StageResult("wipe", True, "removed: " + ", ".join(CI_WIPE_PATHS), "")


def down(ci_stack: CiStack, io: LockingHost, *, wipe: bool = False) -> int:
    """Stop the CI project and optionally remove only its named wipe paths.

    A teardown stops the containers a sibling run is driving, so it runs under
    the engine lock, held through the wipe, and refuses while another process
    holds it.
    """

    root = _root_row(io, "down")
    if root is not None:
        print_stage(root)
        return 1
    claim, lock_row = _engine_lock(io, "tools.cistack down")
    if not lock_row.ok:
        print_stage(lock_row)
        return 1
    try:
        return _down_stages(ci_stack, io, wipe=wipe, lock_detail=lock_row.detail)
    finally:
        _release_engine_lock(io, claim)


def _down_stages(ci_stack: CiStack, io: Host, *, wipe: bool, lock_detail: str) -> int:
    try:
        present = io.exists(ci_stack.project_dir / "compose.yaml")
    except OSError as exc:
        result = StageResult("preconditions", False, f"CI project could not be inspected: {exc}", _DOWN_ROOT_FIX)
        print_stage(result)
        return 1
    preconditions = StageResult(
        "preconditions",
        True,
        ("CI project is present" if present else "CI project is already absent")
        + f"; {lock_detail}",
        "",
    )
    print_stage(preconditions)
    if present:
        stopped = stages.run_stage(
            io,
            "down",
            stack.compose_argv(ci_stack.project_dir, "down", "--remove-orphans"),
            "stopped the CI stack",
            stack.logs_fix(ci_stack.project_dir, RELAY_SERVICE_NAME),
        )
    else:
        stopped = StageResult("down", True, "CI stack is already down", "")
    print_stage(stopped)
    if not stopped.ok:
        return 1
    if not wipe:
        return 0
    wiped = _wipe(ci_stack, io)
    print_stage(wiped)
    return int(not wiped.ok)


def _relay_attached(io: Host, service: str, network: str) -> tuple[bool, str] | str:
    try:
        result = io.run(["docker", "network", "inspect", network_name(network)])
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{network} network inspection failed: {exc}"
    if result.returncode != 0:
        return f"{network} network inspection failed: {command_detail(result)}"
    try:
        documents = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"{network} network inspection returned invalid JSON"
    if not isinstance(documents, list) or not documents or not isinstance(documents[0], Mapping):
        return f"{network} network inspection returned no network"
    containers = documents[0].get("Containers", {})
    if not isinstance(containers, Mapping):
        return False, f"{service} is absent from the {network} network"
    for value in containers.values():
        if not isinstance(value, Mapping):
            continue
        name = value.get("Name")
        labels = value.get("Labels")
        if name == container_name(service, project=CI_PROJECT) or (
            isinstance(labels, Mapping)
            and labels.get("com.docker.compose.project") == CI_PROJECT
            and labels.get("com.docker.compose.service") == service
        ):
            return True, f"{service} is attached to the {network} network"
    return False, f"{service} is absent from the {network} network"


def status(ci_stack: CiStack, io: Host) -> int:
    """Report running CI services and their relays on production's networks."""

    root = _root_row(io, "status")
    if root is not None:
        root = StageResult("preconditions", False, root.detail, _ROOT_FIX)
        print_stage(root)
        return 1
    try:
        present = io.exists(ci_stack.project_dir / "compose.yaml")
    except OSError as exc:
        result = StageResult("preconditions", False, f"CI project could not be inspected: {exc}", _ROOT_FIX)
        print_stage(result)
        return 1
    preconditions = StageResult("preconditions", True, "CI project is available" if present else "CI project is absent", "")
    print_stage(preconditions)
    if not present:
        services_result = StageResult("services", True, "CI stack is down", "")
    else:
        running = stack.running_services(io, ci_stack.project_dir)
        if running is None:
            services_result = StageResult(
                "services",
                False,
                "CI service state could not be read",
                stack.logs_fix(ci_stack.project_dir, RELAY_SERVICE_NAME),
            )
        else:
            services_result = StageResult(
                "services",
                True,
                "running services: " + (", ".join(running) if running else "none"),
                "",
            )
    print_stage(services_result)
    if not services_result.ok:
        return 1
    details: list[str] = []
    any_attached = False
    for service, network in (
        (RELAY_SERVICE_NAME, INTEGRATION_NETWORK_NAME),
        (RECORD_RELAY_SERVICE_NAME, INTERNAL_NETWORK_NAME),
        (EMBED_RELAY_SERVICE_NAME, INTERNAL_NETWORK_NAME),
    ):
        attached = _relay_attached(io, service, network)
        if isinstance(attached, str):
            print_stage(StageResult("relay", False, attached, _NETWORK_FIX))
            return 1
        any_attached = any_attached or attached[0]
        details.append(attached[1])
    relay_result = StageResult(
        "relay", True, "; ".join(details) + (_RELAY_REMINDER if any_attached else ""), "",
    )
    print_stage(relay_result)
    return int(not relay_result.ok)

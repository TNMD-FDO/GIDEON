"""Run the CI sibling stack's ordered up, down, and status stages."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import apply, nogpu, owui, render, secrets, site, stack, stages, stores
from gideon.host.images import load_image_lock
from gideon.host.lock import load_host_lock
from gideon.host.models import load_models_lock
from gideon.host.render.api import API_HEALTH_PATH, API_SERVICE_NAME
from gideon.host.render.ci import (
    CI_PROJECT,
    CI_ROOT,
    CI_SECRET_NAMES,
    CI_SKIPPED_SECRETS,
    CI_WIPE_PATHS,
    RELAY_SERVICE_NAME,
    ci_compose_document,
    ci_env_file,
    ci_manifest,
    production_network_name,
)
from gideon.host.render.command import load_render_inputs
from gideon.host.render.engine import ENGINE_PORT
from gideon.host.render.yamlout import dump
from gideon.host.report import StageResult, command_detail, print_stage
from gideon.host.sysio import Host, PathLike

CI_SERVICES: Final[tuple[str, ...]] = (
    "postgres",
    "open-webui",
    API_SERVICE_NAME,
    RELAY_SERVICE_NAME,
)
_ROOT_FIX: Final = "Run sudo python3 -m tools.cistack <up|down|status>, then retry."
_UP_ROOT_FIX: Final = "Run sudo python3 -m tools.cistack up, then retry."
_DOWN_ROOT_FIX: Final = "Run sudo python3 -m tools.cistack down, then retry."
_DATA_FIX: Final = "Run sudo python3 -m gideon host provision, then retry."
_NETWORK_FIX: Final = "Run sudo python3 -m gideon apply, then retry."
_NOGPU_FIX: Final = (
    f"Remove {nogpu.NO_GPU_PATH} on a GPU host, then retry."
)
_RENDER_FIX: Final = "Correct the CI render inputs, then retry."
_FOREIGN_FIX: Final = "Remove or move the foreign file(s), then retry."
_SECRET_FIX: Final = "Correct the CI secrets directory, then retry."
_WIPE_FIX: Final = f"Correct ownership and mode under {CI_ROOT}, then retry."
# Production's `compose down` cannot remove a network a container still joins.
_RELAY_REMINDER: Final = (
    "; run sudo python3 -m tools.cistack down before a restore or upgrade --rollback"
)
_HEALTH_SCRIPT: Final = (
    "import urllib.request; "
    f"raise SystemExit(urllib.request.urlopen('http://127.0.0.1:{ENGINE_PORT}{API_HEALTH_PATH}', "
    "timeout=4).status != 200)"
)
_RENDERED_NAMES: Final[frozenset[str]] = frozenset(
    {"compose.yaml", "open-webui"}
)
_OPEN_WEBUI_NAMES: Final[frozenset[str]] = frozenset(
    {"env", "manifest.yaml"}
)


@dataclass(frozen=True, slots=True)
class CiStack:
    """The fixed CI project paths and the checkout that supplies its image code."""

    project_dir: Path
    secrets_dir: Path
    base_url: str
    checkout: Path


def _error_row(name: str, errors: Sequence[object], fallback: str) -> StageResult:
    error = errors[0] if errors else None
    problem = getattr(error, "problem", None) or fallback
    fix = getattr(error, "fix", None) or _RENDER_FIX
    return StageResult(name, False, str(problem), str(fix))


def _root_row(io: Host, command: str) -> StageResult | None:
    if io.geteuid() == 0:
        return None
    fix = _UP_ROOT_FIX if command == "up" else _DOWN_ROOT_FIX
    return StageResult("preconditions", False, "root is required", fix)


def _preconditions(
    ci_stack: CiStack,
    io: Host,
    *,
    site_path: PathLike,
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

    try:
        network = io.run(["docker", "network", "inspect", production_network_name()])
    except OSError as exc:
        return StageResult(
            "preconditions",
            False,
            f"production Docker network could not be inspected: {exc}",
            _NETWORK_FIX,
        )
    if network.returncode != 0:
        return StageResult(
            "preconditions",
            False,
            f"production Docker network is unavailable: {command_detail(network)}",
            _NETWORK_FIX,
        )
    return StageResult(
        "preconditions",
        True,
        "root, render inputs, CI data root, and production network are available",
        "",
    )


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


def _open_webui_files(io: Host, path: Path) -> tuple[str, ...]:
    if not io.exists(path):
        return ()
    return tuple(io.listdir(path))


def _foreign_files(io: Host, project_dir: Path) -> tuple[str, ...]:
    foreign: list[str] = []
    for name in io.listdir(project_dir):
        if name in _RENDERED_NAMES or name in {"postgres", "openwebui", "secrets"}:
            continue
        foreign.append(str(project_dir / name))
    for name in _open_webui_files(io, project_dir / "open-webui"):
        if name not in _OPEN_WEBUI_NAMES:
            foreign.append(str(project_dir / "open-webui" / name))
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
        open_webui = ci_stack.project_dir / "open-webui"
        io.mkdir(open_webui, mode=0o755, parents=True, exist_ok=True)
        io.write_text(ci_stack.project_dir / "compose.yaml", compose_text, mode=0o644)
        io.write_text(open_webui / "env", env_text, mode=0o600)
        io.write_text(open_webui / "manifest.yaml", manifest_text, mode=0o644)
    except (OSError, TypeError, ValueError) as exc:
        return StageResult("render", False, f"CI render files could not be written: {exc}", _RENDER_FIX)
    return StageResult(
        "render",
        True,
        "wrote the CI Compose document, Open WebUI env file, and manifest",
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


def up(
    ci_stack: CiStack,
    io: Host,
    *,
    site_path: PathLike,
    client_factory: Callable[..., owui.Client] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the CI stack's ordered convergence and verification stages."""

    preconditions = _preconditions(ci_stack, io, site_path=site_path)
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


def down(ci_stack: CiStack, io: Host, *, wipe: bool = False) -> int:
    """Stop the CI project and optionally remove only its named wipe paths."""

    root = _root_row(io, "down")
    if root is not None:
        print_stage(root)
        return 1
    try:
        present = io.exists(ci_stack.project_dir / "compose.yaml")
    except OSError as exc:
        result = StageResult("preconditions", False, f"CI project could not be inspected: {exc}", _DOWN_ROOT_FIX)
        print_stage(result)
        return 1
    preconditions = StageResult(
        "preconditions",
        True,
        "CI project is present" if present else "CI project is already absent",
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


def _relay_attached(io: Host) -> tuple[bool, str] | str:
    try:
        result = io.run(["docker", "network", "inspect", production_network_name()])
    except OSError as exc:
        return f"production network inspection failed: {exc}"
    if result.returncode != 0:
        return f"production network inspection failed: {command_detail(result)}"
    try:
        documents = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "production network inspection returned invalid JSON"
    if not isinstance(documents, list) or not documents or not isinstance(documents[0], Mapping):
        return "production network inspection returned no network"
    containers = documents[0].get("Containers", {})
    if not isinstance(containers, Mapping):
        return False, "engine relay is absent from production's network"
    for value in containers.values():
        if not isinstance(value, Mapping):
            continue
        name = value.get("Name")
        labels = value.get("Labels")
        if name == f"{CI_PROJECT}-{RELAY_SERVICE_NAME}-1" or (
            isinstance(labels, Mapping)
            and labels.get("com.docker.compose.project") == CI_PROJECT
            and labels.get("com.docker.compose.service") == RELAY_SERVICE_NAME
        ):
            return True, "engine relay is attached to production's network" + _RELAY_REMINDER
    return False, "engine relay is absent from production's network"


def status(ci_stack: CiStack, io: Host) -> int:
    """Report running CI services and the relay's production-network attachment."""

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
    attached = _relay_attached(io)
    if isinstance(attached, str):
        relay_result = StageResult("relay", False, attached, _NETWORK_FIX)
    else:
        relay_result = StageResult("relay", True, attached[1], "")
    print_stage(relay_result)
    return int(not relay_result.ok)

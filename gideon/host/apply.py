"""Render, pull, start, and verify the host's Compose project."""

import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import (
    egress,
    grafana,
    owui,
    pgbackrest,
    secrets,
    stack,
    stores,
    tls,
    weights,
)
from gideon.host.egress import EgressAllowlist
from gideon.host.images import (
    RegistryTarget,
    parse_registry,
    proxy_environment,
)
from gideon.host.models import HardwareProfile
from gideon.host.render import RenderedSet, RenderInputs
from gideon.host.render.command import (
    AppliedManifest,
    RecreateJudgment,
    compose_digests,
    manifest_document,
    read_applied_manifest,
    recreate_judgment,
    render_to_disk,
)
from gideon.host.render.compose import (
    ENGINE_READY_SECONDS,
    STORE_SERVICES,
    service_images,
    service_names,
)
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.render.owui import BREAK_GLASS
from gideon.host.report import StageResult, command_detail, print_stage, refusal
from gideon.host.site import SiteConfig
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_ROOT_FIX: Final = "Run gideon apply as root, for example with sudo."
_DOCKER_FIX: Final = (
    "Run gideon host provision --only docker-engine, then re-run apply."
)
_REGISTRY_FIX: Final = (
    "Run python3 -m gideon registry mirror on the box, then re-run apply."
)
_PULL_FIX: Final = (
    "Check the registry and the images.lock digests "
    "(docker compose -f /etc/gideon/rendered/compose.yaml pull), then re-run apply."
)
_RENDER_FIX: Final = "Correct the render inputs, then re-run apply."
_MANIFEST_FIX: Final = (
    "Correct /etc/gideon/rendered/open-webui/manifest.yaml, then re-run apply."
)
_PS_FORMAT: Final = "json"
# Exempt operational constants: Compose may need time to start every service.
_VERIFY_ATTEMPTS: Final = 30
_VERIFY_SLEEP_SECONDS: Final = 2.0
_READY_ATTEMPTS: Final = 60


@dataclass(slots=True)
class _ApplyContext:
    rendered: RenderedSet
    inputs: RenderInputs
    target: RegistryTarget
    references: tuple[str, ...]
    judgment: RecreateJudgment
    applied: AppliedManifest | None


def preconditions(io: Host) -> StageResult:
    try:
        result = io.run(["docker", "compose", "version"])
    except OSError as exc:
        return StageResult(
            "preconditions",
            False,
            f"Docker Compose is unavailable: {exc}",
            _DOCKER_FIX,
        )
    if result.returncode == 127:
        return StageResult(
            "preconditions", False, "Docker Compose is unavailable", _DOCKER_FIX
        )
    if result.returncode != 0:
        return StageResult(
            "preconditions",
            False,
            f"Docker Compose version check failed: {command_detail(result)}",
            _DOCKER_FIX,
        )
    return StageResult("preconditions", True, "Docker Compose is available", "")


def _secrets_stage(io: Host) -> tuple[StageResult, secrets.EnsureResult | None]:
    try:
        result = secrets.ensure_generated(io)
    except OSError as exc:
        return (
            StageResult(
                "secrets",
                False,
                f"generated secrets could not be ensured: {exc}",
                "Correct ownership and mode for /etc/gideon/secrets, then re-run apply.",
            ),
            None,
        )
    if not result.ok:
        return (
            StageResult(
                "secrets",
                False,
                result.problem or "generated secrets could not be ensured",
                result.fix,
            ),
            result,
        )
    detail = (
        f"created generated secrets: {', '.join(result.created)}"
        if result.created
        else "no generated secret was missing"
    )
    return StageResult("secrets", True, detail, ""), result


# The sign-in name each printed-once password belongs to; the registry carries
# the consumer text, the render modules the usernames.
_PRINTED_LOGINS: Final[Mapping[str, str]] = {
    "gideon_admin_password": BREAK_GLASS.username,
    "grafana_admin_password": GRAFANA_ADMIN_USER,
}
PRINT_ONCE_SUFFIX: Final = " — into the office password manager now (§1.7)."
PRINT_ONCE_LINE: Final = re.compile(
    r"^[^()\n]+ \([^)]*\): (?P<value>.+?)"
    + re.escape(PRINT_ONCE_SUFFIX)
    + r"$"
)


def _print_generated_secrets(result: secrets.EnsureResult) -> None:
    consumers = {secret.name: secret.consumer for secret in secrets.SECRET_REGISTRY}
    for name, value in result.printed.items():
        who = consumers.get(name, name)
        login = _PRINTED_LOGINS.get(name)
        if login is not None:
            who = f"{who} {login}"
        print(f"{name} ({who}): {value}{PRINT_ONCE_SUFFIX}")


def _render_stage(
    io: Host,
    *,
    rendered_dir: PathLike,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    root: Path,
) -> tuple[StageResult, _ApplyContext | None]:
    try:
        outcome = render_to_disk(
            io,
            rendered_dir=rendered_dir,
            site_path=site_path,
            lock_path=lock_path,
            images_path=images_path,
            models_path=models_path,
            root=root,
            diff=False,
            command="apply",
        )
    except Exception as exc:  # noqa: BLE001  # command boundary must not traceback
        return (
            StageResult(
                "render",
                False,
                f"render failed: {type(exc).__name__}: {exc}",
                _RENDER_FIX,
            ),
            None,
        )
    if outcome.exit_code != 0 or outcome.rendered is None or outcome.inputs is None:
        return (
            StageResult("render", False, "render inputs were refused", _RENDER_FIX),
            None,
        )

    rendered_dir_path = Path(rendered_dir)
    try:
        applied = read_applied_manifest(io, rendered_dir_path / "applied.yaml")
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        return (
            StageResult(
                "render",
                False,
                f"applied manifest is unreadable: {exc}",
                "Repair the applied manifest, then re-run apply.",
            ),
            None,
        )

    target = parse_registry(outcome.inputs.site.registry)
    if target is None:
        return (
            StageResult(
                "render",
                False,
                f"registry is not usable: {outcome.inputs.site.registry}",
                "Correct registry in /etc/gideon/site.yaml, then re-run apply.",
            ),
            None,
        )
    current_services = service_names(outcome.inputs)
    digests = compose_digests(outcome.inputs)
    judgment = recreate_judgment(outcome.rendered, applied, current_services, digests)
    # The registry and pull stages judge the images the rendered stack names,
    # not every lock pin: a service the mode pins out is never pulled.
    references = service_images(outcome.inputs)
    return (
        StageResult(
            "render",
            True,
            f"rendered {len(outcome.rendered.files)} file(s)",
            "",
        ),
        _ApplyContext(
            rendered=outcome.rendered,
            inputs=outcome.inputs,
            target=target,
            references=references,
            judgment=judgment,
            applied=applied,
        ),
    )


def _registry_stage(io: Host, context: _ApplyContext) -> StageResult:
    environment: Mapping[str, str] | None = None
    if context.inputs.site.egress_proxy:
        proxy = proxy_environment(context.inputs.site, io)
        if not proxy.ok:
            return StageResult(
                "registry",
                False,
                proxy.problem or "proxy is unusable",
                proxy.fix,
            )
        environment = proxy.variables

    missing: list[str] = []
    failures: list[str] = []
    for image_reference in context.references:
        argv = ["docker", "manifest", "inspect"]
        if context.target.scheme == "http":
            argv.append("--insecure")
        argv.append(image_reference)
        try:
            result = io.run(argv, env=environment)
        except OSError as exc:
            failures.append(f"{image_reference} ({exc})")
        else:
            if result.returncode != 0:
                missing.append(image_reference)
    if missing or failures:
        names = missing + failures
        return StageResult(
            "registry",
            False,
            "missing image digest(s): " + ", ".join(names),
            _REGISTRY_FIX,
        )
    return StageResult("registry", True, "all locked image digests are present", "")


def _repo_digests(stdout: str) -> tuple[str, ...] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _pull_stage(
    io: Host, rendered_dir: PathLike, context: _ApplyContext
) -> StageResult:
    try:
        pulled = io.run(stack.compose_argv(rendered_dir, "pull"))
    except OSError as exc:
        return StageResult("pull", False, f"Compose pull failed: {exc}", _PULL_FIX)
    if pulled.returncode != 0:
        return StageResult(
            "pull", False, f"Compose pull failed: {command_detail(pulled)}", _PULL_FIX
        )

    mismatches: list[str] = []
    for image_reference in context.references:
        try:
            inspected = io.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    image_reference,
                ]
            )
        except OSError as exc:
            mismatches.append(f"{image_reference} ({exc})")
            continue
        digests = _repo_digests(inspected.stdout) if inspected.returncode == 0 else None
        if digests is None or image_reference not in digests:
            mismatches.append(image_reference)
    if mismatches:
        return StageResult(
            "pull",
            False,
            "image digest verification failed for: " + ", ".join(mismatches),
            _PULL_FIX,
        )
    return StageResult("pull", True, "all locked images are pulled by digest", "")


# The weights pull apply runs between the image pull and the recreate; injectable
# so the apply tests prove the ordering and the tests of weights prove the pull.
PullModels = Callable[[Host, SiteConfig, HardwareProfile, EgressAllowlist], weights.PullOutcome]


def _models_stage(
    io: Host,
    context: _ApplyContext,
    egress_path: PathLike,
    pull_models: PullModels,
) -> StageResult:
    """Converge the weights tree to the profile before any service restarts (§5.4, §3.6)."""

    allowlist_result = egress.load_egress_allowlist(egress_path, host=io)
    if not allowlist_result.ok or allowlist_result.allowlist is None:
        return StageResult(
            "models",
            False,
            egress.render_errors(allowlist_result.errors),
            "Correct config/egress.yaml, then re-run apply.",
        )

    try:
        outcome = pull_models(
            io, context.inputs.site, context.inputs.profile, allowlist_result.allowlist
        )
    except Exception as exc:  # noqa: BLE001  # command boundary must not traceback
        return StageResult(
            "models",
            False,
            f"weights pull failed: {type(exc).__name__}: {exc}",
            "Inspect /data/models and the lines above, then re-run apply.",
        )
    if not outcome.ok:
        return StageResult("models", False, outcome.problem, outcome.fix)

    present = sum(model.kind == "present" for model in outcome.models)
    fetched = sum(model.kind == "fetched" for model in outcome.models)
    detail = (
        f"{fetched} model(s) fetched, {present} present"
        if fetched
        else f"{present} model(s) present"
    )
    if outcome.uncovered:
        detail += "; uncovered: " + ", ".join(outcome.uncovered)
    return StageResult("models", True, detail, "")


_logs_fix = stack.logs_fix


def force_recreate_services(
    io: Host,
    rendered_dir: PathLike,
    services: tuple[str, ...],
    stage: str,
) -> StageResult | None:
    for service in services:
        try:
            result = stack.force_recreate(io, rendered_dir, service)
        except OSError as exc:
            return StageResult(
                stage,
                False,
                f"recreate failed for {service}: {exc}",
                _logs_fix(rendered_dir, service),
            )
        if result.returncode != 0:
            return StageResult(
                stage,
                False,
                f"recreate failed for {service}: {command_detail(result)}",
                _logs_fix(rendered_dir, service),
            )
    return None


def _recreate_detail(
    services: tuple[str, ...], judgment: RecreateJudgment, none_text: str
) -> str:
    """The stage's row: its services, then the reasons grouped in a fixed order.

    One reason stands bare; several are joined by ``; ``, each naming the
    stage's services it covers, a service under two reasons appearing in both.
    """

    if not services:
        return none_text
    if judgment.first_apply:
        return f"recreated {', '.join(services)}: first apply"
    clauses = [
        (label, [service for service in services if service in covered])
        for label, covered in (
            ("changed rendered files", judgment.files),
            ("changed compose block", judgment.block),
            ("changed compose top-level", judgment.top_level),
        )
    ]
    clauses = [(label, members) for label, members in clauses if members]
    if len(clauses) == 1:
        reason = clauses[0][0]
    else:
        reason = "; ".join(f"{label} ({', '.join(members)})" for label, members in clauses)
    return f"recreated {', '.join(services)}: {reason}"


def _recreate_stage(
    io: Host, rendered_dir: PathLike, context: _ApplyContext
) -> StageResult:
    store_recreate = tuple(
        service for service in context.judgment.services if service in STORE_SERVICES
    )
    failure = force_recreate_services(io, rendered_dir, store_recreate, "recreate")
    if failure is not None:
        return failure

    try:
        started = io.run(
            stack.compose_argv(rendered_dir, "up", "-d", *STORE_SERVICES)
        )
    except OSError as exc:
        return StageResult(
            "recreate",
            False,
            f"Compose store-tier start failed: {exc}",
            _logs_fix(rendered_dir, STORE_SERVICES[0]),
        )
    if started.returncode != 0:
        return StageResult(
            "recreate",
            False,
            f"Compose store-tier start failed: {command_detail(started)}",
            _logs_fix(rendered_dir, STORE_SERVICES[0]),
        )
    detail = _recreate_detail(
        store_recreate,
        context.judgment,
        "no store-tier rendered file or compose block changed",
    )
    return StageResult("recreate", True, detail, "")


def _start_stage(
    io: Host, rendered_dir: PathLike, context: _ApplyContext
) -> StageResult:
    other_recreate = tuple(
        service for service in context.judgment.services if service not in STORE_SERVICES
    )
    failure = force_recreate_services(io, rendered_dir, other_recreate, "start")
    if failure is not None:
        return failure

    try:
        started = io.run(
            stack.compose_argv(rendered_dir, "up", "-d", "--remove-orphans")
        )
    except OSError as exc:
        service = other_recreate[0] if other_recreate else service_names(context.inputs)[0]
        return StageResult(
            "start",
            False,
            f"Compose start failed: {exc}",
            _logs_fix(rendered_dir, service),
        )
    if started.returncode != 0:
        service = other_recreate[0] if other_recreate else service_names(context.inputs)[0]
        return StageResult(
            "start",
            False,
            f"Compose start failed: {command_detail(started)}",
            _logs_fix(rendered_dir, service),
        )
    detail = _recreate_detail(
        other_recreate,
        context.judgment,
        "no other rendered file or compose block changed",
    )
    return StageResult("start", True, detail, "")


def _service_state(
    rows: tuple[Mapping[str, object], ...],
    expected: tuple[str, ...],
    *,
    exact: bool,
    require_healthy: bool,
) -> tuple[bool, str, str]:
    actual: set[str] = set()
    for row in rows:
        service = row.get("Service")
        if isinstance(service, str):
            actual.add(service)
    expected_set = set(expected)
    compared = actual if exact else actual.intersection(expected_set)
    if compared != expected_set:
        missing = sorted(expected_set - actual)
        extra = sorted(actual - expected_set) if exact else []
        details: list[str] = []
        if missing:
            details.append("missing service(s): " + ", ".join(missing))
        if extra:
            details.append("unexpected service(s): " + ", ".join(extra))
        service = missing[0] if missing else (extra[0] if extra else expected[0])
        return False, "; ".join(details), service
    for row in rows:
        service = row.get("Service")
        if not isinstance(service, str) or service not in expected_set:
            continue
        state = row.get("State", "")
        health = row.get("Health", "")
        if state != "running":
            return False, f"service {service} is not running (state: {state})", service
        if (require_healthy and health != "healthy") or (
            not require_healthy and health not in ("", "healthy")
        ):
            return False, f"service {service} is not healthy (health: {health})", service
    return True, "", expected[0]


def wait_for_services(
    io: Host,
    rendered_dir: PathLike,
    expected: tuple[str, ...],
    sleep: Callable[[float], None],
    *,
    exact: bool,
    require_healthy: bool,
    attempts: int = _VERIFY_ATTEMPTS,
) -> tuple[bool, str, str]:
    """Poll ``compose ps`` until *expected* services are running (and healthy).

    ``exact`` demands the running set equal *expected* (verify's contract);
    otherwise other services are ignored (the store tier is judged alone).
    ``require_healthy`` refuses a service without a healthcheck verdict.
    """

    if not expected:
        return False, "no services are expected", ""
    last_detail = "service state could not be read"
    last_service = expected[0]
    for attempt in range(attempts):
        try:
            result = io.run(
                stack.compose_argv(
                    rendered_dir, "ps", "--all", "--format", _PS_FORMAT
                )
            )
        except OSError as exc:
            ready = False
            detail = f"Compose service check failed: {exc}"
            service = expected[0]
        else:
            if result.returncode != 0:
                ready = False
                detail = f"Compose service check failed: {command_detail(result)}"
                service = expected[0]
            else:
                rows = stack.parse_ps(result.stdout)
                if rows is None:
                    ready = False
                    detail = "Compose service check returned invalid JSON"
                    service = expected[0]
                else:
                    ready, detail, service = _service_state(
                        rows,
                        expected,
                        exact=exact,
                        require_healthy=require_healthy,
                    )
        if ready:
            return True, "", service
        last_detail, last_service = detail, service
        if attempt + 1 < attempts:
            sleep(_VERIFY_SLEEP_SECONDS)
    return False, last_detail, last_service


def _stores_stage(
    io: Host,
    rendered_dir: PathLike,
    context: _ApplyContext,
    sleep: Callable[[float], None],
) -> StageResult:
    ready, detail, service = wait_for_services(
        io, rendered_dir, STORE_SERVICES, sleep, exact=False, require_healthy=True
    )
    if not ready:
        return StageResult("stores", False, detail, _logs_fix(rendered_dir, service))
    try:
        result = stores.converge(
            io, rendered_dir, root=context.inputs.checkout
        )
    except OSError as exc:
        return StageResult(
            "stores",
            False,
            f"store convergence failed: {exc}",
            _logs_fix(rendered_dir, STORE_SERVICES[0]),
        )
    if not result.ok:
        return StageResult(
            "stores",
            False,
            result.problem or "store convergence failed",
            result.fix,
        )
    for operation in (
        pgbackrest.ensure_repository,
        pgbackrest.ensure_stanza,
        pgbackrest.check,
    ):
        operation_result = operation(io, rendered_dir)
        if not operation_result.ok:
            return StageResult(
                "stores",
                False,
                operation_result.problem or "pgBackRest convergence failed",
                operation_result.fix,
            )
    changes: list[str] = []
    if result.created_roles:
        changes.append(f"created roles: {', '.join(result.created_roles)}")
    if result.created_databases:
        changes.append(
            f"created databases: {', '.join(result.created_databases)}"
        )
    if result.applied_migrations:
        changes.append(
            f"applied migrations: {', '.join(result.applied_migrations)}"
        )
    changes.append(f"pgBackRest stanza {pgbackrest.STANZA} current; archiving verified")
    return StageResult(
        "stores",
        True,
        "; ".join(changes) or "roles, databases, and migrations are current",
        "",
    )


def _manifest_detail(result: owui.BootstrapReport) -> str:
    changes: list[str] = []
    if result.created_groups:
        changes.append(f"created groups: {', '.join(result.created_groups)}")
    if result.updated_groups:
        changes.append(f"updated groups: {', '.join(result.updated_groups)}")
    if result.removed_groups:
        changes.append(f"removed groups: {', '.join(result.removed_groups)}")
    if result.removed_functions:
        changes.append(
            f"removed functions: {', '.join(result.removed_functions)}"
        )
    if result.removed_models:
        changes.append(f"removed models: {', '.join(result.removed_models)}")
    if result.minted:
        changes.append(f"minted secrets: {', '.join(result.minted)}")
    return "; ".join(changes) or "frontend state matches the manifest"


def _apply_manifest_stage(
    io: Host,
    rendered_dir: PathLike,
    context: _ApplyContext,
    sleep: Callable[[float], None],
    client_factory: Callable[..., owui.Client],
) -> StageResult:
    try:
        ready = owui.wait_ready(
            client_factory(), attempts=_READY_ATTEMPTS, sleep=sleep
        )
    except owui.OwuiError as exc:
        return StageResult("apply-manifest", False, exc.problem, exc.fix)
    if not ready.ok:
        return StageResult(
            "apply-manifest",
            False,
            ready.problem or "Open WebUI is not ready",
            ready.fix,
        )

    manifest_path = Path(rendered_dir) / "open-webui" / "manifest.yaml"
    try:
        manifest = owui.load_manifest(io, manifest_path)
    except ValueError as exc:
        return StageResult(
            "apply-manifest",
            False,
            f"manifest is unreadable: {manifest_path} ({exc})",
            _MANIFEST_FIX,
        )
    try:
        result = owui.bootstrap(
            io, client_factory, manifest, rendered_dir=rendered_dir
        )
    except owui.OwuiError as exc:
        return StageResult("apply-manifest", False, exc.problem, exc.fix)
    if not result.ok:
        return StageResult(
            "apply-manifest",
            False,
            result.problem or "Open WebUI bootstrap failed",
            result.fix,
        )
    return StageResult("apply-manifest", True, _manifest_detail(result), "")


def _verify_stage(
    io: Host,
    rendered_dir: PathLike,
    context: _ApplyContext,
    sleep: Callable[[float], None],
    client_factory: Callable[..., owui.Client],
    grafana_client_factory: Callable[..., grafana.Client],
    *,
    start_time: float,
    clock: Callable[[], float],
) -> StageResult:
    expected = service_names(context.inputs)
    engine_healthy_seconds: int | None = None
    # The engine alone gets the long wait, counted from the start stage on
    # the same clock as its healthcheck's start_period; every other service
    # keeps the default bound, so an unrelated failure is reported within a
    # minute with its own logs, never after the engine's fifteen.
    if ENGINE_SERVICE_NAME in expected:
        without_engine = tuple(
            service for service in expected if service != ENGINE_SERVICE_NAME
        )
        if without_engine:
            ready, detail, service = wait_for_services(
                io,
                rendered_dir,
                without_engine,
                sleep,
                exact=False,
                require_healthy=False,
            )
            if not ready:
                return StageResult(
                    "verify", False, detail, _logs_fix(rendered_dir, service)
                )

        remaining_seconds = ENGINE_READY_SECONDS - (clock() - start_time)
        engine_attempts = max(
            1, math.floor(remaining_seconds / _VERIFY_SLEEP_SECONDS)
        )
        ready, detail, service = wait_for_services(
            io,
            rendered_dir,
            (ENGINE_SERVICE_NAME,),
            sleep,
            exact=False,
            require_healthy=True,
            attempts=engine_attempts,
        )
        if not ready:
            return StageResult(
                "verify", False, detail, _logs_fix(rendered_dir, service)
            )
        engine_healthy_seconds = math.floor(clock() - start_time)

        ready, detail, service = wait_for_services(
            io,
            rendered_dir,
            expected,
            sleep,
            exact=True,
            require_healthy=False,
            attempts=1,
        )
        if not ready:
            return StageResult(
                "verify", False, detail, _logs_fix(rendered_dir, service)
            )
    else:
        ready, detail, service = wait_for_services(
            io, rendered_dir, expected, sleep, exact=True, require_healthy=False
        )
        if not ready:
            return StageResult(
                "verify", False, detail, _logs_fix(rendered_dir, service)
            )

    try:
        probe = tls.probe_ingress(io, context.inputs.site.hostname, sleep=sleep)
    except Exception as exc:  # noqa: BLE001  # command boundary must not traceback
        return StageResult(
            "verify",
            False,
            f"ingress probe failed: {type(exc).__name__}: {exc}",
            _logs_fix(rendered_dir, expected[0]),
        )
    if not probe.ok:
        return StageResult(
            "verify", False, probe.detail, _logs_fix(rendered_dir, expected[0])
        )
    try:
        frontend_ready = client_factory().ready()
    except owui.OwuiError as exc:
        return StageResult(
            "verify", False, exc.problem, _logs_fix(rendered_dir, "open-webui")
        )
    if not frontend_ready:
        return StageResult(
            "verify",
            False,
            "Open WebUI /ready did not report status true",
            _logs_fix(rendered_dir, "open-webui"),
        )
    # Grafana's health endpoint needs no credential; the break-glass
    # administrator is for `alerts test`, not for proving the door answers.
    try:
        grafana_ready = grafana.wait_ready(
            grafana_client_factory(), attempts=_READY_ATTEMPTS, sleep=sleep
        )
    except grafana.GrafanaError as exc:
        return StageResult(
            "verify", False, exc.problem, _logs_fix(rendered_dir, "grafana")
        )
    if not grafana_ready.ok:
        return StageResult(
            "verify",
            False,
            grafana_ready.problem or "Grafana is not ready",
            _logs_fix(rendered_dir, "grafana"),
        )
    verify_detail = (
        "Compose services are running, ingress is verified, and Open WebUI and Grafana are ready"
    )
    if engine_healthy_seconds is not None:
        verify_detail += f", engine healthy {engine_healthy_seconds} s after start"
    return StageResult(
        "verify",
        True,
        verify_detail,
        "",
    )


def _rendered_units(context: _ApplyContext, suffix: str) -> tuple[str, ...]:
    return tuple(
        rendered_file.relative_path
        for rendered_file in context.rendered.files
        if rendered_file.relative_path.startswith("systemd/")
        and rendered_file.relative_path.endswith(suffix)
    )


def _applied_units(context: _ApplyContext, suffix: str) -> set[str]:
    return {
        Path(path).name
        for path in (context.applied.files if context.applied is not None else {})
        if path.startswith("systemd/") and path.endswith(suffix)
    }


def _timers_stage(
    io: Host, rendered_dir: PathLike, context: _ApplyContext
) -> StageResult:
    """Link the rendered service units, then enable and start the rendered timers.

    A timer refuses to start unless the service it triggers is a loaded unit,
    so services are linked (``systemctl link`` is idempotent) before any timer
    is enabled; units the previous apply rendered and this one does not are
    disabled first.  An absolute path to ``enable`` links and enables in one
    idempotent call.
    """

    service_paths = _rendered_units(context, ".service")
    timer_paths = _rendered_units(context, ".timer")
    stale_timers = tuple(sorted(_applied_units(context, ".timer") - {Path(p).name for p in timer_paths}))
    stale_services = tuple(sorted(_applied_units(context, ".service") - {Path(p).name for p in service_paths}))
    if not (service_paths or timer_paths or stale_timers or stale_services):
        return StageResult("timers", True, "timers are current", "")

    def run_systemctl(argv: list[str], unit: str, action: str, fix: str) -> StageResult | None:
        try:
            result = io.run(argv)
        except OSError as exc:
            return StageResult("timers", False, f"systemctl {action} failed for {unit}: {exc}", fix)
        if result.returncode != 0:
            return StageResult(
                "timers", False, f"systemctl {action} failed for {unit}: {command_detail(result)}", fix
            )
        return None

    failure = run_systemctl(["systemctl", "daemon-reload"], "systemd", "daemon-reload", "journalctl -xe")
    if failure is not None:
        return failure
    for unit in stale_timers + stale_services:
        failure = run_systemctl(
            ["systemctl", "disable", "--now", unit], unit, "disable", f"journalctl -u {unit}"
        )
        if failure is not None:
            return failure
    rendered_root = Path(rendered_dir)
    for relative_path in service_paths:
        unit = Path(relative_path).name
        failure = run_systemctl(
            ["systemctl", "link", os.fspath(rendered_root / relative_path)], unit, "link", f"journalctl -u {unit}"
        )
        if failure is not None:
            return failure
    enabled: list[str] = []
    for relative_path in timer_paths:
        unit = Path(relative_path).name
        failure = run_systemctl(
            ["systemctl", "enable", "--now", os.fspath(rendered_root / relative_path)],
            unit,
            "enable",
            f"journalctl -u {unit}",
        )
        if failure is not None:
            return failure
        failure = run_systemctl(["systemctl", "is-active", unit], unit, "is-active", f"journalctl -u {unit}")
        if failure is not None:
            return failure
        enabled.append(unit)
    detail = f"enabled timers: {', '.join(enabled)}" if enabled else "timers are current"
    return StageResult("timers", True, detail, "")


def _record_stage(
    io: Host,
    rendered_dir: PathLike,
    site_path: PathLike,
    lock_path: PathLike,
    models_path: PathLike,
    context: _ApplyContext,
) -> StageResult:
    try:
        site_text = io.read_text(site_path)
        lock_text = io.read_text(lock_path)
        models_lock_text = io.read_text(models_path)
        manifest = manifest_document(
            context.rendered,
            context.inputs,
            site_text=site_text,
            lock_text=lock_text,
            models_lock_text=models_lock_text,
        )
        io.write_text(Path(rendered_dir) / "applied.yaml", manifest, mode=0o644)
    except (OSError, UnicodeError, ValueError) as exc:
        return StageResult(
            "record",
            False,
            f"could not record applied manifest: {exc}",
            "Repair the rendered output, then re-run apply.",
        )
    return StageResult("record", True, "applied manifest recorded", "")


def converge(
    io: Host,
    *,
    rendered_dir: PathLike,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    egress_path: PathLike,
    pull_models: PullModels,
    root: Path,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    client_factory: Callable[..., owui.Client] | None,
    grafana_client_factory: Callable[..., grafana.Client] | None,
) -> int:
    """Run apply's convergence stages after the command preconditions row."""

    secrets_result, ensured = _secrets_stage(io)
    print_stage(secrets_result)
    if not secrets_result.ok:
        return 1
    if ensured is not None:
        _print_generated_secrets(ensured)

    rendered_result, context = _render_stage(
        io,
        rendered_dir=rendered_dir,
        site_path=site_path,
        lock_path=lock_path,
        images_path=images_path,
        models_path=models_path,
        root=root,
    )
    print_stage(rendered_result)
    if not rendered_result.ok or context is None:
        return 1

    registry_result = _registry_stage(io, context)
    print_stage(registry_result)
    if not registry_result.ok:
        return 1

    pull_result = _pull_stage(io, rendered_dir, context)
    print_stage(pull_result)
    if not pull_result.ok:
        return 1

    models_result = _models_stage(io, context, egress_path, pull_models)
    print_stage(models_result)
    if not models_result.ok:
        return 1

    recreate_result = _recreate_stage(io, rendered_dir, context)
    print_stage(recreate_result)
    if not recreate_result.ok:
        return 1

    stores_result = _stores_stage(io, rendered_dir, context, sleep)
    print_stage(stores_result)
    if not stores_result.ok:
        return 1

    start_result = _start_stage(io, rendered_dir, context)
    print_stage(start_result)
    if not start_result.ok:
        return 1
    start_time = clock()

    factory = client_factory or owui.ingress_client_factory(
        context.inputs.site.hostname, ca_path=tls.CA_PATH
    )
    grafana_factory = grafana_client_factory or grafana.ingress_client_factory(
        context.inputs.site.hostname, ca_path=tls.CA_PATH
    )
    apply_manifest_result = _apply_manifest_stage(
        io, rendered_dir, context, sleep, factory
    )
    print_stage(apply_manifest_result)
    if not apply_manifest_result.ok:
        return 1

    verify_result = _verify_stage(
        io,
        rendered_dir,
        context,
        sleep,
        factory,
        grafana_factory,
        start_time=start_time,
        clock=clock,
    )
    print_stage(verify_result)
    if not verify_result.ok:
        return 1

    timers_result = _timers_stage(io, rendered_dir, context)
    print_stage(timers_result)
    if not timers_result.ok:
        return 1

    record_result = _record_stage(
        io, rendered_dir, site_path, lock_path, models_path, context
    )
    print_stage(record_result)
    return int(not record_result.ok)


def run_apply(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    lock_path: PathLike | None = None,
    images_path: PathLike | None = None,
    models_path: PathLike | None = None,
    egress_path: PathLike | None = None,
    pull_models: PullModels | None = None,
    root: PathLike | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    client_factory: Callable[..., owui.Client] | None = None,
    grafana_client_factory: Callable[..., grafana.Client] | None = None,
) -> int:
    """Run the ordered render/apply/verify stages."""

    del args
    io = host or RealHost()
    if io.geteuid() != 0:
        print(refusal("apply", "root is required.", _ROOT_FIX), file=sys.stderr)
        return 1
    preconditions_result = preconditions(io)
    print_stage(preconditions_result)
    if not preconditions_result.ok:
        return 1

    checkout = Path(__file__).parents[2] if root is None else Path(root)
    actual_lock = checkout / "host.lock" if lock_path is None else lock_path
    actual_images = checkout / "images.lock" if images_path is None else images_path
    actual_models = checkout / "models.lock" if models_path is None else models_path
    actual_egress = (
        checkout / "config" / "egress.yaml" if egress_path is None else egress_path
    )
    return converge(
        io,
        rendered_dir=rendered_dir,
        site_path=site_path,
        lock_path=actual_lock,
        images_path=actual_images,
        models_path=actual_models,
        egress_path=actual_egress,
        pull_models=pull_models or weights.pull_profile,
        root=checkout,
        sleep=sleep,
        clock=clock,
        client_factory=client_factory,
        grafana_client_factory=grafana_client_factory,
    )

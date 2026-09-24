"""The ordered host command for rotating one generated secret.

``secrets rotate <name>`` regenerates one rotatable secret and recreates exactly
the services that loaded the old value, then converges the rest of the stack
the way ``apply`` does.  Every other registry name refuses before any change,
naming the class of its second home or the office's replacement path.
"""

import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import apply, grafana, owui, secrets, weights
from gideon.host.render import RenderedSet, RenderInputs, render_all
from gideon.host.render import command as render_command
from gideon.host.render.compose import ENGINE_READY_SECONDS, service_names
from gideon.host.render.consumers import SecretConsumers, consumers_of
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.render.owui import BREAK_GLASS, EVAL_IDENTITY
from gideon.host.report import StageResult, print_stage, refusal
from gideon.host.secrets import registry_entry, secret_path
from gideon.host.stores import ROLE_SPECS
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_APPLY_COMMAND: Final = "sudo python3 -m gideon apply"
_ROTATE_COMMAND: Final = "sudo python3 -m gideon secrets rotate"
_RENDER_DIFF_COMMAND: Final = "sudo python3 -m gideon render --diff"
_COMPOSE_FILE: Final = f"{_RENDERED_DIR}/compose.yaml"
_PENDING_FIX: Final = f"Run {_RENDER_DIFF_COMMAND}, then {_APPLY_COMMAND}, then retry."
_APPLY_FIX: Final = f"Run {_APPLY_COMMAND}, then retry."
_INPUTS_FIX: Final = "Correct the render inputs named above, then retry."
# The command rotates only a secret whose file is the value's only home; a
# second home is changed through its consumer's own route first (the ticket's
# ruling b), and that route is a ticket of its own.
_ONLY_HOME_RULE: Final = "this command rotates only a secret whose file is its only home"
_ROTATABLE_CLASSES: Final[frozenset[str]] = frozenset({"rewrite", "remint"})

# Release identities the refusals name, never a value: the superuser is the
# image's fixed role, the application roles are the store convergence's own
# registry, and the two logins are the frontend's machine identities.
_ROLE_NAMES: Final[Mapping[str, str]] = {
    "postgres_superuser_password": "postgres",
    **{spec.secret_name: spec.name for spec in ROLE_SPECS},
}
_ACCOUNT_LOGINS: Final[Mapping[str, str]] = {
    "gideon_admin_password": BREAK_GLASS.username,
    "gideon_eval_password": EVAL_IDENTITY.username,
}


@dataclass(frozen=True, slots=True)
class _RotationContext:
    inputs: RenderInputs
    rendered: RenderedSet
    consumers: SecretConsumers


def _refuse(problem: object, fix: str) -> int:
    print(refusal("secrets rotate", problem, fix), file=sys.stderr)
    return 1


def _role_fix(name: str, role: str) -> str:
    return (
        f"Change it in the role first (ALTER ROLE {role} PASSWORD over psql on stdin: "
        f"sudo docker compose -f {_COMPOSE_FILE} exec -T postgres psql -U postgres "
        f"-d postgres -f -), rewrite {secret_path(name)} in place, recreate every service "
        f"that mounts or carries it by hand (sudo docker compose -f {_COMPOSE_FILE} up -d "
        f"--no-deps --force-recreate <service>), then {_APPLY_COMMAND}; a ticket of its "
        f"own — {_ONLY_HOME_RULE}."
    )


def _account_fix(name: str, login: str) -> str:
    return (
        f"Change {login}'s password in the frontend first (the account's own password "
        f"route, signed in as {login}), rewrite {secret_path(name)} in place, then "
        f"{_APPLY_COMMAND}; a ticket of its own — {_ONLY_HOME_RULE}."
    )


def _seeded_fix(name: str) -> str:
    return (
        f"Change it in Grafana first: PUT /grafana/api/user/password through the ingress "
        f"as {GRAFANA_ADMIN_USER} with the old and new value in the request body carried "
        f"on stdin (curl -K -), then rewrite {secret_path(name)} in place — the install "
        f"runbook's rotation step; a ticket of its own — {_ONLY_HOME_RULE}."
    )


def _pre_run_refusal(name: str) -> int | None:
    """The refusal for a name the command will not rotate, or None for a rotatable one."""

    entry = registry_entry(name)
    if entry is None:
        supplied = next(
            (candidate for candidate in secrets.SUPPLIED_REGISTRY if candidate.name == name),
            None,
        )
        if supplied is not None:
            return _refuse(
                f"{name} is a supplied secret, which the office replaces.",
                supplied.replaced_by,
            )
        rotatable = ", ".join(sorted(secrets.ROTATABLE_NAMES))
        generated = ", ".join(sorted(secrets.GENERATED_NAMES))
        supplied_names = ", ".join(sorted(secrets.SUPPLIED_NAMES))
        return _refuse(
            f"unknown secret name {name}.",
            f"Run {_ROTATE_COMMAND} <name> with a rotatable name ({rotatable}); every other "
            f"registry name refuses naming its path (generated: {generated}; supplied: "
            f"{supplied_names}).",
        )
    if entry.rotation in _ROTATABLE_CLASSES:
        return None
    if entry.rotation == "role":
        role = _ROLE_NAMES[name]
        return _refuse(
            f"{name} also lives in the Postgres role {role}, which apply never alters.",
            _role_fix(name, role),
        )
    if entry.rotation == "account":
        login = _ACCOUNT_LOGINS[name]
        return _refuse(
            f"{name} also lives in the frontend account {login}.",
            _account_fix(name, login),
        )
    return _refuse(
        f"{name} also lives in Grafana's stored admin user, seeded from the file at "
        "first start only.",
        _seeded_fix(name),
    )


def _pending_reason(judgment: render_command.RecreateJudgment) -> str:
    reasons: list[str] = []
    if judgment.first_apply:
        reasons.append("first apply")
    for label, services in (
        ("changed rendered files", judgment.files),
        ("changed compose block", judgment.block),
        ("changed compose top-level", judgment.top_level),
    ):
        if services:
            reasons.append(f"{label}: {', '.join(services)}")
    return "; ".join(reasons) or "pending change"


def _preconditions(
    io: Host,
    *,
    rendered_dir: PathLike,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    root: Path,
    name: str,
) -> tuple[StageResult, _RotationContext | None]:
    """Docker Compose, the rendered tree, the applied record, and no pending change."""

    result = apply.preconditions(io)
    if not result.ok:
        return result, None

    rendered = Path(rendered_dir)
    compose_path = rendered / "compose.yaml"
    if not io.exists(compose_path):
        return (
            StageResult(
                "preconditions", False, f"rendered Compose file is missing: {compose_path}", _APPLY_FIX
            ),
            None,
        )
    applied_path = rendered / "applied.yaml"
    if not io.exists(applied_path):
        return (
            StageResult(
                "preconditions", False, f"no verified apply is recorded: {applied_path}", _APPLY_FIX
            ),
            None,
        )

    loaded = render_command.load_render_inputs(
        io,
        site_path=site_path,
        lock_path=lock_path,
        images_path=images_path,
        models_path=models_path,
        root=root,
        command="secrets rotate",
    )
    if loaded is None:
        return (
            StageResult("preconditions", False, "render inputs were refused", _INPUTS_FIX),
            None,
        )
    inputs, _, _, _ = loaded
    try:
        rendered_set = render_all(inputs)
        digests = render_command.compose_digests(inputs)
        applied = render_command.read_applied_manifest(io, applied_path)
        judgment = render_command.recreate_judgment(
            rendered_set, applied, service_names(inputs), digests
        )
    except Exception as exc:  # noqa: BLE001  # command boundary must not traceback
        return (
            StageResult(
                "preconditions", False, f"rendered state is unreadable: {exc}", _APPLY_FIX
            ),
            None,
        )
    if judgment.services:
        return (
            StageResult(
                "preconditions",
                False,
                f"apply has pending changes ({_pending_reason(judgment)}); a rotation "
                "recreates exactly the secret's consumers.",
                _PENDING_FIX,
            ),
            None,
        )
    return (
        StageResult(
            "preconditions",
            True,
            "Docker Compose is available, a verified apply is recorded, and no change is pending",
            "",
        ),
        _RotationContext(inputs, rendered_set, consumers_of(inputs, name)),
    )


def _plan(name: str, entry: secrets.GeneratedSecret, consumers: SecretConsumers) -> StageResult:
    """What the run will do: the class, the consumers, and each consumer's unavailability."""

    if entry.rotation == "remint":
        return StageResult(
            "plan",
            True,
            f"{name}: remint — the file removed and re-minted by the apply-manifest stage; "
            "no service recreated",
            "",
        )
    if not consumers.mounts and not consumers.carried:
        return StageResult(
            "plan", True, f"no service on this host consumes {name}; nothing rotated", ""
        )
    parts: list[str] = []
    if consumers.mounts:
        mounts = ", ".join(
            f"{service} (cold start; apply waits up to {ENGINE_READY_SECONDS} s)"
            if service == ENGINE_SERVICE_NAME
            else f"{service} (seconds)"
            for service in consumers.mounts
        )
        parts.append(f"recreated by mount: {mounts}")
    if consumers.carried:
        parts.append(
            "recreated by the converge for changed rendered files: "
            + ", ".join(consumers.carried)
        )
    return StageResult("plan", True, f"{name}: rewrite — " + "; ".join(parts), "")


def _rotate(io: Host, name: str, entry: secrets.GeneratedSecret) -> StageResult:
    """Write the new value, or remove a minted key's file; never the value in the row."""

    path = secret_path(name)
    if entry.rotation == "rewrite":
        rotation = secrets.rotate_generated(io, name)
        if not rotation.ok:
            return StageResult("rotate", False, rotation.problem or "rotation failed", rotation.fix)
        return StageResult(
            "rotate", True, f"{name}: new value written to {path} (root:gideon 0440)", ""
        )
    try:
        io.unlink(path, missing_ok=True)
    except OSError as exc:
        return StageResult(
            "rotate",
            False,
            f"could not remove {path}: {exc}",
            f"Correct {path}, then {_APPLY_COMMAND}, then retry.",
        )
    return StageResult("rotate", True, f"{name}: file removed; re-minted below", "")


def _recreate(
    io: Host, rendered_dir: PathLike, name: str, mounts: tuple[str, ...]
) -> StageResult:
    """Force-recreate each service that mounts the secret, once, in service order."""

    failure = apply.force_recreate_services(io, rendered_dir, mounts, "recreate")
    if failure is not None:
        # The value is written and this consumer still holds the old one: apply
        # converges the carriers and clears the precondition, then a fresh
        # rotation reaches every consumer.
        return StageResult(
            "recreate",
            False,
            failure.detail,
            f"sudo {failure.fix}, then {_APPLY_COMMAND}, then {_ROTATE_COMMAND} {name} again.",
        )
    if not mounts:
        return StageResult("recreate", True, f"no service mounts {name}", "")
    return StageResult("recreate", True, f"recreated {', '.join(mounts)}: mount {name}", "")


def run_secrets_rotate(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    lock_path: PathLike | None = None,
    images_path: PathLike | None = None,
    models_path: PathLike | None = None,
    egress_path: PathLike | None = None,
    pull_models: apply.PullModels | None = None,
    root: PathLike | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    client_factory: Callable[..., owui.Client] | None = None,
    grafana_client_factory: Callable[..., grafana.Client] | None = None,
) -> int:
    """Rotate one generated secret, recreate its mount consumers, and converge the rest."""

    name = str(getattr(args, "name", ""))
    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", f"Run {_ROTATE_COMMAND} {name}.")
    refusal_code = _pre_run_refusal(name)
    if refusal_code is not None:
        return refusal_code
    entry = registry_entry(name)
    assert entry is not None

    checkout = Path(__file__).parents[2] if root is None else Path(root)
    actual_lock = checkout / "host.lock" if lock_path is None else lock_path
    actual_images = checkout / "images.lock" if images_path is None else images_path
    actual_models = checkout / "models.lock" if models_path is None else models_path
    actual_egress = (
        checkout / "config" / "egress.yaml" if egress_path is None else egress_path
    )
    preconditions, context = _preconditions(
        io,
        rendered_dir=rendered_dir,
        site_path=site_path,
        lock_path=actual_lock,
        images_path=actual_images,
        models_path=actual_models,
        root=checkout,
        name=name,
    )
    print_stage(preconditions)
    if not preconditions.ok or context is None:
        return 1

    consumers = context.consumers
    print_stage(_plan(name, entry, consumers))
    if entry.rotation == "rewrite" and not consumers.mounts and not consumers.carried:
        return 0

    rotate_result = _rotate(io, name, entry)
    print_stage(rotate_result)
    if not rotate_result.ok:
        return 1

    recreate_result = _recreate(io, rendered_dir, name, consumers.mounts)
    print_stage(recreate_result)
    if not recreate_result.ok:
        return 1

    return apply.converge(
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

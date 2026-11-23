"""Hold seeded thinking-mode sampling to the generator snapshot and derive retry sets from held sets."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from gideon.host import models, report, stack, weights
from gideon.host.render.engine import GENERATOR
from gideon.host.sysio import Host, PathLike

# exempt: the pinned model card's thinking-mode settings; correct at a generator bump.
THINKING_SAMPLING: Final[Mapping[str, int | float]] = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0,
}
# exempt: arbitrary default; override with --seed.
DEFAULT_SEED: Final[int] = 0
# exempt: the upper bound of a signed 64-bit seed.
MAX_SEED: Final[int] = 2**63 - 1


def next_seed(seed: int) -> int:
    """Advance a seed, wrapping to zero past the upper bound."""

    return (seed + 1) % (MAX_SEED + 1)


@dataclass(frozen=True, slots=True)
class HeldSet:
    """Sampling held to the generator snapshot, with a seed from which to derive a retry set."""

    seed: int

    @property
    def request_fields(self) -> dict[str, int | float]:
        return {**THINKING_SAMPLING, "seed": self.seed}

    def for_retry(self) -> HeldSet:
        """Derive a held set with the next seed since the snapshot check is seed-independent."""

        return HeldSet(next_seed(self.seed))


def sampling_differences(config: Mapping[str, object]) -> tuple[str, ...]:
    """Return differing sampling keys in request order. The snapshot may omit min_p."""

    differences: list[str] = []
    for key, expected in THINKING_SAMPLING.items():
        if key == "min_p" and key not in config:
            continue
        value = config.get(key)
        if type(value) not in (int, float) or value != expected:
            differences.append(key)
    return tuple(differences)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("repeated JSON key")
    return value


def hold(
    io: Host, repository: str, revision: str, seed: int, *, models_root: PathLike = weights.MODELS_ROOT,
) -> HeldSet | report.Problem:
    """Hold the thinking-mode set to a checked repository and revision."""

    path = weights.snapshot_path(repository, revision, "generation_config.json", root=models_root)
    try:
        config = json.loads(io.read_text(path), object_pairs_hook=_unique_object)
        if not isinstance(config, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError, RecursionError):
        return report.Problem(
            "the pinned generation_config.json cannot be read as a JSON object",
            f"Run {report.command('models pull')} to restore the pinned snapshot, then retry.",
        )
    if differences := sampling_differences(config):
        return report.Problem(
            f"sampling keys differ from the pinned generation_config.json: {', '.join(differences)}",
            "Run on a box serving the reference profile's generator, or after a generator bump correct the sampling "
            "constants in gideon/evaluation/sampling.py to the new model card's thinking-mode settings, then retry.",
        )
    return HeldSet(seed)


def pin_fix(tool: str) -> str:
    """Tell an operator how to align a checkout with the running generator."""

    return (
        f"Run {report.command('apply')} from the checkout holding the intended model pin, "
        f"or run {tool} from the checkout the box runs."
    )


def _engine_command(io: Host, argv: Sequence[str], label: str, fix: str) -> str | report.Problem:
    try:
        result = io.run(argv)
    except (OSError, subprocess.SubprocessError):
        return report.Problem(f"{label} could not run", fix)
    if result.returncode != 0:
        return report.Problem(f"{label} exited {result.returncode}", fix)
    return result.stdout


def running_pin(io: Host, rendered_dir: PathLike, fix: str) -> tuple[str, str] | report.Problem:
    """Read the repository and revision from the running generator's start command."""

    locate = stack.compose_argv(rendered_dir, "ps", "--status", "running", "--quiet", GENERATOR.service_name)
    located = _engine_command(io, locate, "generator container lookup", fix)
    if isinstance(located, report.Problem):
        return located
    containers = located.split()
    if len(containers) != 1:
        return report.Problem("expected one running generator container", fix)
    # Compose names the service's running container; its command is what the
    # container was started with, whatever the checkout's lock now says.
    inspect = ["docker", "container", "inspect", "--format", "{{json .Config.Cmd}}", "--", containers[0]]
    raw_command = _engine_command(io, inspect, "generator container inspection", fix)
    if isinstance(raw_command, report.Problem):
        return raw_command
    try:
        command = json.loads(raw_command)
    except (ValueError, RecursionError):
        return report.Problem("the running generator command cannot be read", fix)
    if (
        not isinstance(command, list) or len(command) < 3
        or not all(isinstance(arg, str) for arg in command)
        or command[1] != "--revision"
        or models.HF_REPO.fullmatch(command[0]) is None
        or models.REVISION.fullmatch(command[2]) is None
        or any(arg == "--revision" or arg.startswith("--revision=") for arg in command[3:])
    ):
        return report.Problem("the running generator command has no usable repository and revision", fix)
    return command[0], command[2]


def hold_running(
    io: Host, rendered_dir: PathLike, models_path: PathLike, profile_name: str, seed: int,
    *, models_root: PathLike = weights.MODELS_ROOT,
) -> HeldSet | report.Problem:
    """Match the running pin to the selected profile and hold it under the caller's engine lock."""

    fix = pin_fix("this command")
    running = running_pin(io, rendered_dir, fix)
    if isinstance(running, report.Problem):
        return running
    lock = models.load_models_lock(models_path, host=io).lock
    profile = None if lock is None else lock.profile(profile_name)
    pin = None if profile is None else profile.model(GENERATOR.role)
    if pin is None:
        return report.Problem("the generator pin cannot be read", "Restore the selected profile's generator pin, then retry.")
    if running != (pin.repo, pin.revision):
        return report.Problem("the running generator repository or revision differs from the checkout's pin", fix)
    return hold(io, *running, seed, models_root=models_root)

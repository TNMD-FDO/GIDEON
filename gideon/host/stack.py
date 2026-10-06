"""Shared Docker Compose command construction for host operations."""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.host import report
from gideon.host.render.api import API_MOUNT_TARGET, API_WORKING_DIRECTORY
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike


def _apply_fix() -> str:
    return f"Run {report.command('apply')}, then retry."


def compose_argv(rendered_dir: PathLike, *args: str) -> list[str]:
    """Build the one canonical Compose invocation for rendered state."""

    directory = os.fspath(rendered_dir)
    compose_file = os.fspath(Path(directory) / "compose.yaml")
    return [
        "docker",
        "compose",
        "--project-directory",
        directory,
        "-f",
        compose_file,
        *args,
    ]


def exec_argv(
    rendered_dir: PathLike,
    service: str,
    *args: str,
    user: str | None = None,
) -> list[str]:
    """Build the canonical non-interactive Compose exec invocation."""

    user_args = [] if user is None else ["-u", user]
    return compose_argv(rendered_dir, "exec", "-T", *user_args, service, *args)


def image_run_argv(
    reference: str, checkout: PathLike, *args: str, name: str
) -> list[str]:
    """Run mounted checkout code in one isolated, named container of a pinned image.

    The name lets a caller whose client timed out remove the container.
    """

    source = Path(checkout) / "gideon"
    return [
        "docker", "run", "--rm", "-i", "--name", name, "--network", "none",
        "--log-driver", "none", "-v", f"{source}:{API_MOUNT_TARGET}:ro",
        "-w", API_WORKING_DIRECTORY, reference, *args,
    ]


def container_remove_argv(name: str) -> list[str]:
    """Force-remove one named container, running or not."""

    return ["docker", "rm", "-f", name]


def image_present_argv(reference: str) -> list[str]:
    """Probe whether the pinned image is present on the Docker daemon."""

    return ["docker", "image", "inspect", reference]


def logs_fix(rendered_dir: PathLike, service: str) -> str:
    """The operator's next command when *service* misbehaves: its Compose logs."""

    compose_file = Path(rendered_dir) / "compose.yaml"
    return f"docker compose -f {compose_file} logs {service}"


def force_recreate(
    host: Host, rendered_dir: PathLike, service: str
) -> subprocess.CompletedProcess[str]:
    """Force-recreate one service through the host I/O seam."""

    return host.run(
        compose_argv(
            rendered_dir,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            service,
        )
    )


def parse_ps(stdout: str) -> tuple[Mapping[str, object], ...] | None:
    """Rows of ``compose ps --format json`` (one document or one per line), or None."""

    text = stdout.strip()
    if not text:
        return ()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        rows: list[object] = []
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return None
        if not all(isinstance(row, Mapping) for row in rows):
            return None
        return tuple(row for row in rows if isinstance(row, Mapping))
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, list) and all(isinstance(row, Mapping) for row in value):
        return tuple(row for row in value if isinstance(row, Mapping))
    return None


def running_services(host: Host, rendered_dir: PathLike) -> tuple[str, ...] | None:
    """The project's running service names, or None when Compose cannot say."""

    try:
        result = host.run(compose_argv(rendered_dir, "ps", "--all", "--format", "json"))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    rows = parse_ps(result.stdout)
    if rows is None:
        return None
    return tuple(
        str(row["Service"])
        for row in rows
        if isinstance(row.get("Service"), str) and row.get("State") == "running"
    )


def declared_services(host: Host, rendered_dir: PathLike) -> tuple[str, ...] | Problem:
    """Read service names in order from the rendered Compose file."""

    compose_path = Path(rendered_dir) / "compose.yaml"
    try:
        document = yaml.safe_load(host.read_text(compose_path))
    except (OSError, UnicodeDecodeError) as exc:
        return Problem(f"rendered Compose file is unavailable: {exc}.", _apply_fix())
    except yaml.YAMLError:
        return Problem("rendered Compose file is invalid.", _apply_fix())
    if not isinstance(document, Mapping) or not isinstance(document.get("services"), Mapping):
        return Problem("rendered Compose file has no services map.", _apply_fix())
    services = document["services"]
    if not all(isinstance(name, str) for name in services):
        return Problem("rendered Compose file has an invalid services map.", _apply_fix())
    return tuple(services)

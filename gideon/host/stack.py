"""Shared Docker Compose command construction for host operations."""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from gideon.host.sysio import Host, PathLike


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

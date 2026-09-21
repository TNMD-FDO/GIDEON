"""Compare an interpreter's installed development environment with exact pins.

The comparison reads ``requirements-dev.txt`` from the current working tree,
walks the installed distributions' declared requirements, and asks the same
interpreter to run ``pip check``.  The dependency walk deliberately follows
markers without evaluating them, except for requirements conditioned on an
extra, which are not part of the base environment.  This over-approximates a
platform-specific closure so a distribution needed on another platform is
tolerated.  The module uses only the standard library and can run by path
under ``-P`` or as ``python -m tools.environment``.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

PIN_FILE: Final[str] = "requirements-dev.txt"
INSTALLER_PACKAGES: Final[frozenset[str]] = frozenset(
    {"pip", "setuptools", "wheel"}
)
PIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s#=;]+)$"
)
REQUIREMENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)"
)

LINK_FIX: Final[str] = (
    "Replace the .venv link with a venv built from this tree's "
    "requirements-dev.txt, or bring this tree's requirements-dev.txt level "
    "with the linked checkout; never install through the link, then re-run."
)
OWN_FIX: Final[str] = (
    "Build a fresh venv from this tree's requirements-dev.txt, then re-run."
)
PIN_FIX: Final[str] = (
    "Repair requirements-dev.txt so every non-comment line is one exact "
    "name==version pin with no duplicate names, then re-run."
)


@dataclass(frozen=True, slots=True)
class InstalledDistribution:
    """The metadata needed to compare one installed distribution."""

    name: str
    version: str
    requires: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Pin:
    """One exact development-tool pin and its source line."""

    name: str
    version: str
    line: int


class DistributionMetadata(Protocol):
    """The metadata attributes accepted by the distribution seam."""

    name: str
    version: str
    requires: Iterable[str] | None


def normalize_name(name: str) -> str:
    """Return the PEP 503 spelling used for package-name comparisons."""

    return re.sub(r"[-_.]+", "-", name).lower()


def read_distributions() -> tuple[InstalledDistribution, ...]:
    """Read installed distribution metadata from this interpreter's paths."""

    return tuple(
        InstalledDistribution(
            name=str(distribution.name),
            version=str(distribution.version),
            requires=tuple(distribution.requires or ()),
        )
        for distribution in importlib.metadata.distributions()
    )


def run_pip_check() -> tuple[int, str]:
    """Run this interpreter's ``pip check`` without allowing it to install."""

    try:
        result = subprocess.run(
            (sys.executable, "-P", "-m", "pip", "check"),
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        return 127, str(error)
    output = result.stdout
    if result.stderr:
        if output and not output.endswith("\n"):
            output += "\n"
        output += result.stderr
    return result.returncode, output


def _read_pins(path: Path) -> tuple[tuple[Pin, ...], tuple[str, ...]]:
    """Read exact pins and collect every input error before returning."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return (), (f"cannot read {PIN_FILE}: {error}",)

    pins: list[Pin] = []
    errors: list[str] = []
    names: set[str] = set()
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PIN_PATTERN.fullmatch(stripped)
        if match is None:
            errors.append(f"line {number}: expected an exact name==version pin")
            continue
        name = normalize_name(match.group("name"))
        if name in names:
            errors.append(f"line {number}: package {name} is pinned more than once")
            continue
        names.add(name)
        pins.append(Pin(name=name, version=match.group("version"), line=number))
    return tuple(pins), tuple(errors)


def _as_installed(distribution: object) -> InstalledDistribution:
    """Convert an injected metadata object to the comparison's small record."""

    if isinstance(distribution, InstalledDistribution):
        return distribution
    metadata = cast(DistributionMetadata, distribution)
    name = metadata.name
    version = metadata.version
    return InstalledDistribution(
        name=str(name),
        version=str(version),
        requires=tuple(metadata.requires or ()),
    )


def _requirement_name(requirement: str) -> str | None:
    """Return a requirement's normalized name unless it is extra-only."""

    requirement_parts = requirement.split(";", 1)
    if len(requirement_parts) == 2 and re.search(r"\bextra\b", requirement_parts[1]):
        return None
    match = REQUIREMENT_NAME_PATTERN.match(requirement_parts[0])
    if match is None:
        return None
    return normalize_name(match.group(1))


def _closure(
    pins: Sequence[Pin], installed: dict[str, tuple[InstalledDistribution, ...]]
) -> set[str]:
    """Return the fixed-point name closure rooted at the readable pins."""

    allowed: set[str] = set()
    pending = [pin.name for pin in pins]
    while pending:
        name = pending.pop()
        if name in allowed:
            continue
        allowed.add(name)
        for distribution in installed.get(name, ()):
            for requirement in distribution.requires:
                required_name = _requirement_name(requirement)
                if required_name is not None and required_name not in allowed:
                    pending.append(required_name)
    return allowed


def _pip_findings(output: str, code: int) -> list[str]:
    """Pass every non-empty ``pip check`` line through; each names its package."""

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return [f"pip check exited {code} without naming a package"]
    return lines


def _fix_text() -> str:
    """Choose the repair for the environment holding this interpreter."""

    return LINK_FIX if Path(sys.prefix).is_symlink() else OWN_FIX


def _findings(
    pins: Sequence[Pin],
    distributions: Sequence[InstalledDistribution],
    pip_check: Callable[[], tuple[int, str]],
) -> tuple[list[str], bool]:
    """Collect all environment differences and whether pip check ran."""

    grouped: dict[str, list[InstalledDistribution]] = {}
    for distribution in distributions:
        grouped.setdefault(normalize_name(distribution.name), []).append(distribution)
    installed_groups = {name: tuple(values) for name, values in grouped.items()}
    allowed = _closure(pins, installed_groups) | INSTALLER_PACKAGES
    findings: list[str] = []

    for name in sorted(installed_groups):
        if name not in allowed:
            for distribution in sorted(
                installed_groups[name], key=lambda item: item.version
            ):
                findings.append(
                    f"package {name}=={distribution.version} is outside the pins' "
                    "dependency closure"
                )

    for pin in pins:
        matches = installed_groups.get(pin.name, ())
        if not matches:
            findings.append(f"package {pin.name}=={pin.version} is not installed")
        elif not any(distribution.version == pin.version for distribution in matches):
            versions = ", ".join(
                distribution.version
                for distribution in sorted(matches, key=lambda item: item.version)
            )
            findings.append(
                f"package {pin.name} is installed at {versions}, expected {pin.version}"
            )

    for name in sorted(installed_groups):
        matches = installed_groups[name]
        if len(matches) > 1:
            versions = ", ".join(
                distribution.version
                for distribution in sorted(matches, key=lambda item: item.version)
            )
            findings.append(
                f"package {name} is installed more than once: {versions}"
            )

    pip_name = normalize_name("pip")
    if pip_name not in installed_groups:
        return findings, False
    try:
        code, output = pip_check()
    except (OSError, subprocess.SubprocessError) as error:
        findings.append(f"package pip: pip check could not run: {error}")
        return findings, True
    if code != 0:
        findings.extend(_pip_findings(output, code))
    return findings, True


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    distributions: Callable[[], Iterable[object]] | None = None,
    pip_check: Callable[[], tuple[int, str]] | None = None,
) -> int:
    """Compare the current environment and return 0, 1, or 2."""

    parser = argparse.ArgumentParser(
        prog="python3 -m tools.environment",
        description=(
            "read exact pins, compare the installed environment, then run pip check"
        ),
    )
    parser.add_argument(
        "--pins-only",
        action="store_true",
        help="validate requirements-dev.txt without reading the environment",
    )
    options = parser.parse_args(argv)
    checkout = Path.cwd() if root is None else root
    pins, errors = _read_pins(checkout / PIN_FILE)
    if errors:
        for error in errors:
            print(f"environment: {error}", file=sys.stderr)
        print(f"environment: Fix: {PIN_FIX}", file=sys.stderr)
        return 2
    if options.pins_only:
        print(f"environment: {PIN_FILE} has valid exact pins")
        return 0

    distribution_reader = read_distributions if distributions is None else distributions
    pip_checker = run_pip_check if pip_check is None else pip_check
    try:
        installed = tuple(_as_installed(item) for item in distribution_reader())
    except (OSError, ValueError, AttributeError) as error:
        print(f"environment: cannot read installed distributions: {error}", file=sys.stderr)
        print(f"environment: Fix: {_fix_text()}", file=sys.stderr)
        return 1
    findings, pip_ran = _findings(pins, installed, pip_checker)
    if findings:
        for finding in findings:
            print(f"environment: {finding}", file=sys.stderr)
        print(f"environment: Fix: {_fix_text()}", file=sys.stderr)
        return 1
    if pip_ran:
        print(f"environment: equal to {PIN_FILE}; pip check passed")
    else:
        print(f"environment: equal to {PIN_FILE}; pip check did not run")
    return 0


if __name__ == "__main__":
    sys.exit(main())

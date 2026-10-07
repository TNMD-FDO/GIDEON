"""Periodic apt triggers for unattended maintenance."""

import re
from dataclasses import dataclass
from pathlib import Path

from gideon.host.steps import (
    BOX_WIDE_SHORTFALL_FIX,
    BoxWideSetting,
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
    apt_install,
    box_wide_shortfall,
    package_installed,
    stderr_first_line,
)

_DROP_IN = Path("/etc/apt/apt.conf.d/52gideon-auto-upgrades")
_PERIODIC = ("apt-config", "dump", "APT::Periodic")
_KEYS = ("Update-Package-Lists", "Unattended-Upgrade")
_PERIODIC_LINE = re.compile(r'^APT::Periodic::([A-Za-z-]+) "([^"]*)";$')
_PERIODIC_READ_FIX = (
    "Repair apt's configuration (apt-config dump names the error), then re-run provision."
)
_DROP_IN_READ_FIX = f"Repair access to {_DROP_IN}, then re-run provision."
_DROP_IN_WRITE_FIX = f"Write {_DROP_IN}, then re-run provision."


@dataclass(frozen=True)
class _PeriodicReading:
    values: tuple[str | None, ...]
    own: frozenset[str]
    stale: bool


def _drop_in_text(keys: set[str] | frozenset[str]) -> str:
    return "".join(
        f'APT::Periodic::{key} "1";\n' for key in _KEYS if key in keys
    )


def _drop_in_keys(text: str) -> tuple[frozenset[str], bool]:
    own: set[str] = set()
    stale = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        match = _PERIODIC_LINE.fullmatch(stripped)
        if match is None or match.group(1) not in _KEYS:
            stale = True
            continue
        key, value = match.groups()
        if key in own or value != "1":
            stale = True
        own.add(key)
    return frozenset(own), stale


def _periodic_read(context: ProvisionContext) -> _PeriodicReading | CheckResult:
    try:
        result = context.host.run(_PERIODIC)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"apt-config could not report APT::Periodic: {exc}",
            _PERIODIC_READ_FIX,
        )
    if result.returncode != 0:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"apt-config could not report APT::Periodic: {stderr_first_line(result.stderr)}",
            _PERIODIC_READ_FIX,
        )
    values: dict[str, str | None] = dict.fromkeys(_KEYS)
    for line in result.stdout.splitlines():
        match = _PERIODIC_LINE.fullmatch(line.strip())
        if match is not None and match.group(1) in values:
            values[match.group(1)] = match.group(2)
    own: frozenset[str] = frozenset()
    stale = False
    if context.host.exists(_DROP_IN):
        try:
            own, stale = _drop_in_keys(context.host.read_text(_DROP_IN))
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_DROP_IN}: {exc}",
                _DROP_IN_READ_FIX,
            )
    return _PeriodicReading(tuple(values[key] for key in _KEYS), own, stale)


def _periodic_check(reading: _PeriodicReading, setting: BoxWideSetting) -> CheckResult:
    short = next(
        (key for key, value in zip(_KEYS, reading.values, strict=True) if value == "0"),
        None,
    )
    if short is not None:
        if reading.stale:
            return CheckResult(
                Disposition.DRIFT,
                f"{_DROP_IN} holds a line GIDEON does not write",
                _DROP_IN_WRITE_FIX,
            )
        return CheckResult(
            Disposition.UNFIXABLE,
            box_wide_shortfall(
                setting, f"APT::Periodic::{short} 0", "a value other than 0"
            ),
            BOX_WIDE_SHORTFALL_FIX,
        )
    unset = next(
        (key for key, value in zip(_KEYS, reading.values, strict=True) if value is None),
        None,
    )
    if unset is not None:
        return CheckResult(
            Disposition.DRIFT,
            f"APT::Periodic::{unset} is unset; GIDEON writes {_DROP_IN}",
            _DROP_IN_WRITE_FIX,
        )
    if reading.stale:
        return CheckResult(
            Disposition.DRIFT,
            f"{_DROP_IN} holds a line GIDEON does not write",
            _DROP_IN_WRITE_FIX,
        )
    return CheckResult(
        Disposition.CONVERGED,
        "unattended upgrades are on ("
        + ", ".join(
            f"{key} {value}"
            for key, value in zip(_KEYS, reading.values, strict=True)
        )
        + ")",
        "",
    )


class UnattendedUpgradesStep(Step):
    """Own only apt's periodic triggers, set each while unset, and accept any on value.

    Ubuntu's stock 50unattended-upgrades origins stay security-only; a trigger
    another file set to a value other than 0 is met and never restated, since
    a later drop-in would override it.
    """

    name = "unattended-upgrades"
    summary = "ensure periodic package lists and unattended security upgrades"
    settings = (BoxWideSetting("apt periodic triggers"),)

    def check(self, context: ProvisionContext) -> CheckResult:
        if not package_installed(context, "unattended-upgrades"):
            return CheckResult(
                Disposition.DRIFT,
                "unattended-upgrades is not installed",
                "Install unattended-upgrades, then re-run provision.",
            )
        reading = _periodic_read(context)
        if isinstance(reading, CheckResult):
            return reading
        return _periodic_check(reading, self.settings[0])

    def apply(self, context: ProvisionContext) -> None:
        if not package_installed(context, "unattended-upgrades"):
            apt_install(context, ["unattended-upgrades"])
        reading = _periodic_read(context)
        if isinstance(reading, CheckResult):
            raise StepFailure(reading.detail, reading.fix)
        checked = _periodic_check(reading, self.settings[0])
        if checked.disposition is Disposition.UNFIXABLE:
            raise StepFailure(checked.detail, checked.fix)
        if reading.stale or any(value is None for value in reading.values):
            keys = set(reading.own)
            keys.update(
                key for key, value in zip(_KEYS, reading.values, strict=True)
                if value is None
            )
            context.host.write_text(_DROP_IN, _drop_in_text(keys))

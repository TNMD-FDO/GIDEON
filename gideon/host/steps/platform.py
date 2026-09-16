"""The supported bare-host platform check."""

import yaml  # type: ignore[import-untyped]

from gideon.host.steps import CheckResult, Disposition, ProvisionContext, Step

_OS_RELEASE = "/etc/os-release"
_REINSTALL_FIX = "Reinstall Ubuntu 26.04 per the §1.9 runbook, then re-run provision."


def _release_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID"}:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _failure(detail: str) -> CheckResult:
    return CheckResult(Disposition.UNFIXABLE, detail, _REINSTALL_FIX, halts_run=True)


class PlatformStep(Step):
    """Refuse to run provisioning on an unsupported platform."""

    name = "platform"
    summary = "verify Ubuntu 26.04, x86-64, and YAML support"

    def check(self, context: ProvisionContext) -> CheckResult:
        try:
            release = _release_values(context.host.read_text(_OS_RELEASE))
        except (OSError, UnicodeError) as exc:
            return _failure(f"cannot read {_OS_RELEASE}: {exc}")

        if release.get("ID") != "ubuntu":
            return _failure(
                f"unsupported operating system ID {release.get('ID', '(missing)')!r}"
            )
        if release.get("VERSION_ID") != context.lock.os_lts:
            return _failure(
                f"Ubuntu VERSION_ID is {release.get('VERSION_ID', '(missing)')!r}; "
                f"host.lock requires {context.lock.os_lts!r}"
            )

        try:
            architecture = context.host.run(["uname", "-m"])
        except OSError as exc:
            return _failure(f"cannot determine machine architecture: {exc}")
        if architecture.returncode != 0:
            return _failure("uname -m could not determine the machine architecture")
        if architecture.stdout.strip() != "x86_64":
            return _failure(
                f"machine architecture is {architecture.stdout.strip()!r}; x86_64 is required"
            )

        # Importing yaml is part of the bare-host module contract; this check
        # makes that prerequisite explicit without touching the filesystem.
        if yaml is None:  # pragma: no cover - import failure stops module load
            return _failure("the yaml module is not importable")
        return CheckResult(Disposition.CONVERGED, "supported platform", "")

    def apply(self, context: ProvisionContext) -> None:
        del context
        # The runner cannot reach apply for this check-only step.
        raise NotImplementedError("platform is check-only")

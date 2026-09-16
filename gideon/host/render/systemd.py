"""Pure systemd service and timer artifacts."""

from collections.abc import Mapping
from string import Template
from typing import Final

from gideon.host.render import Artifact, RenderInputs

_SERVICE_TEMPLATE = "systemd/gideon-users-reconcile.service.tmpl"
_TIMER_TEMPLATE = "systemd/gideon-users-reconcile.timer.tmpl"
_BACKUP_SERVICE_TEMPLATE = "systemd/gideon-backup.service.tmpl"
_BACKUP_TIMER_TEMPLATE = "systemd/gideon-backup.timer.tmpl"
_DRILL_SERVICE_TEMPLATE = "systemd/gideon-backup-drill.service.tmpl"
_DRILL_TIMER_TEMPLATE = "systemd/gideon-backup-drill.timer.tmpl"
_VERIFY_SERVICE_TEMPLATE = "systemd/gideon-backup-verify.service.tmpl"
_VERIFY_TIMER_TEMPLATE = "systemd/gideon-backup-verify.timer.tmpl"

RECONCILE_CALENDAR: Final = "*-*-* 03:00:00"
BACKUP_CALENDAR: Final = "*-*-* 01:00:00"
VERIFY_ALL_CALENDAR: Final = "Sat *-01,04,07,10-8..14 04:00:00"

DRILL_CALENDAR: Final[Mapping[str, str]] = {
    "1w": "Sat *-*-* 04:00:00",
    "2w": "Sat *-*-1..7,15..21 04:00:00",
    "1m": "Sat *-*-1..7 04:00:00",
    "3m": "Sat *-01,04,07,10-1..7 04:00:00",
    "6m": "Sat *-01,07-1..7 04:00:00",
    "1y": "Sat *-01-1..7 04:00:00",
}


def _calendar_with_zone(calendar: str, inputs: RenderInputs) -> str:
    return f"{calendar} {inputs.site.office.timezone}"


class _SystemdArtifact(Artifact):
    """Shared template expansion for a rendered systemd unit."""

    def _substitutions(self, inputs: RenderInputs) -> Mapping[str, str]:
        if not inputs.checkout:
            raise ValueError(
                "Render input checkout is empty; the systemd unit needs the "
                "release checkout's absolute path. Re-run render with a checkout."
            )
        return {"checkout": inputs.checkout}

    def emit(self, inputs: RenderInputs) -> str:
        try:
            source = inputs.templates[self.template_paths[0]]
        except KeyError as exc:
            raise ValueError(
                f"Render template {self.template_paths[0]} is missing."
            ) from exc
        try:
            return Template(source).substitute(self._substitutions(inputs))
        except KeyError as exc:
            placeholder = exc.args[0] if exc.args else "unknown"
            raise ValueError(
                f"Render template {self.template_paths[0]} has an unfilled "
                f"placeholder: {placeholder}."
            ) from exc


class ReconcileServiceArtifact(_SystemdArtifact):
    """Render the oneshot service used by the nightly timer."""

    name = "gideon-users-reconcile-service"
    relative_path = "systemd/gideon-users-reconcile.service"
    template_paths = (_SERVICE_TEMPLATE,)


class ReconcileTimerArtifact(_SystemdArtifact):
    """Render the persistent 03:00 reconcile timer."""

    name = "gideon-users-reconcile-timer"
    relative_path = "systemd/gideon-users-reconcile.timer"
    template_paths = (_TIMER_TEMPLATE,)

    def _substitutions(self, inputs: RenderInputs) -> Mapping[str, str]:
        substitutions = dict(super()._substitutions(inputs))
        substitutions["calendar"] = _calendar_with_zone(RECONCILE_CALENDAR, inputs)
        return substitutions


class BackupServiceArtifact(_SystemdArtifact):
    """Render the daily backup and off-box push service."""

    name = "gideon-backup-service"
    relative_path = "systemd/gideon-backup.service"
    template_paths = (_BACKUP_SERVICE_TEMPLATE,)


class BackupTimerArtifact(_SystemdArtifact):
    """Render the persistent daily backup timer."""

    name = "gideon-backup-timer"
    relative_path = "systemd/gideon-backup.timer"
    template_paths = (_BACKUP_TIMER_TEMPLATE,)

    def _substitutions(self, inputs: RenderInputs) -> Mapping[str, str]:
        substitutions = dict(super()._substitutions(inputs))
        substitutions["calendar"] = _calendar_with_zone(BACKUP_CALENDAR, inputs)
        return substitutions


class DrillServiceArtifact(_SystemdArtifact):
    """Render the backup restore-drill service."""

    name = "gideon-backup-drill-service"
    relative_path = "systemd/gideon-backup-drill.service"
    template_paths = (_DRILL_SERVICE_TEMPLATE,)


class DrillTimerArtifact(_SystemdArtifact):
    """Render the persistent restore-drill timer for the site's cadence."""

    name = "gideon-backup-drill-timer"
    relative_path = "systemd/gideon-backup-drill.timer"
    template_paths = (_DRILL_TIMER_TEMPLATE,)

    def _substitutions(self, inputs: RenderInputs) -> Mapping[str, str]:
        substitutions = dict(super()._substitutions(inputs))
        try:
            calendar = DRILL_CALENDAR[inputs.site.backup.drill_interval]
        except KeyError as exc:
            raise ValueError(
                "Render input backup.drill_interval is unsupported. "
                "Correct backup.drill_interval, then re-run render."
            ) from exc
        substitutions["calendar"] = _calendar_with_zone(calendar, inputs)
        return substitutions


class VerifyServiceArtifact(_SystemdArtifact):
    """Render the quarterly full-backup verification service."""

    name = "gideon-backup-verify-service"
    relative_path = "systemd/gideon-backup-verify.service"
    template_paths = (_VERIFY_SERVICE_TEMPLATE,)


class VerifyTimerArtifact(_SystemdArtifact):
    """Render the persistent quarterly full-backup verification timer."""

    name = "gideon-backup-verify-timer"
    relative_path = "systemd/gideon-backup-verify.timer"
    template_paths = (_VERIFY_TIMER_TEMPLATE,)

    def _substitutions(self, inputs: RenderInputs) -> Mapping[str, str]:
        substitutions = dict(super()._substitutions(inputs))
        substitutions["calendar"] = _calendar_with_zone(VERIFY_ALL_CALENDAR, inputs)
        return substitutions

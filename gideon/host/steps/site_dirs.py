"""Provision-owned directories and age backup keys.

The office recipient is kept and its identity printed once; the box identity is
kept and its recipient derived at backup time.
"""

import re
import stat
from pathlib import Path
from typing import Final

from gideon.host.secrets import SERVICE_GROUP_PROBLEM, service_group_gid
from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    passwd_entry,
)

_ROOT = Path("/etc/gideon")
_SECRETS = _ROOT / "secrets"
_DIR_MODES = {
    _ROOT: 0o755,
    _ROOT / "tls": 0o755,
    _ROOT / "rendered": 0o755,
    _SECRETS: 0o700,
}
_BACKUP_KEY = _SECRETS / "backup_ssh_key"
_BACKUP_PUBLIC = Path(f"{_BACKUP_KEY}.pub")
_FILE_MODES = {
    "backup_ssh_key": 0o400,
    "backup_ssh_key.pub": 0o440,
}
_KEYPAIR_FIX = "Generate the gideon backup keypair with the required ownership and modes, then re-run provision."
_SERVICE_GROUP_FIX = (
    "Run gideon host provision --only service-user, then re-run provision."
)
AGE_RECIPIENT_PATH: Final = Path("/etc/gideon/backup_age_recipient")
AGE_RECIPIENT: Final = re.compile(
    r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}"
)
AGE_IDENTITY_PREFIX: Final = (
    "age identity (store it in the office password manager now): "
)
AGE_SECRET_PREFIX: Final = "AGE-SECRET-KEY-1"
AGE_IDENTITY_PATH: Final = Path("/etc/gideon/backup_age_identity")
# age-keygen writes the secret key upper-cased and the recipient lower-cased.
AGE_IDENTITY: Final = re.compile(
    re.escape(AGE_SECRET_PREFIX) + r"[023456789ACDEFGHJKLMNPQRSTUVWXYZ]{58}"
)
AGE_IDENTITY_MODE: Final = 0o400
AGE_IDENTITY_LINE: Final = re.compile(
    r"^" + re.escape(AGE_IDENTITY_PREFIX) + r"(?P<value>[^\n]+)$"
)
_AGE_RECIPIENT_FIX = (
    "Move /etc/gideon/backup_age_recipient aside to generate a new identity — "
    "every earlier tarball then needs the earlier identity from the office password manager."
)
_AGE_IDENTITY_STEP_FIX = (
    "Run sudo python3 -m gideon host provision --only age-identity."
)
AGE_IDENTITY_FIX: Final = (
    "Delete /etc/gideon/backup_age_identity, keeping no copy under /etc/gideon "
    "(only that exact path is excluded from a backup set), and run "
    "sudo python3 -m gideon host provision --only age-identity to mint a new one; "
    "every earlier set then opens on this box with the office identity alone."
)


def _mode(path: Path, context: ProvisionContext) -> int | None:
    try:
        return stat.S_IMODE(context.host.stat(path).st_mode)
    except OSError:
        return None


class SecretsDirsStep(Step):
    """Create the provision-owned directory tree and normalize secret modes."""

    name = "secrets-dirs"
    summary = "create GIDEON directories and normalize secret file modes"
    requires = ("service-user",)

    def check(self, context: ProvisionContext) -> CheckResult:
        for path, expected in _DIR_MODES.items():
            if not context.host.exists(path):
                return CheckResult(Disposition.DRIFT, f"{path} is missing", f"Create {path} with mode {expected:04o}, then re-run provision.")
            actual = _mode(path, context)
            if actual is None:
                return CheckResult(Disposition.UNFIXABLE, f"cannot stat {path}", f"Repair access to {path}, then re-run provision.")
            if actual != expected:
                return CheckResult(Disposition.DRIFT, f"{path} mode is {actual:04o}, not {expected:04o}", f"Set {path} to mode {expected:04o}, then re-run provision.")
        service_gid = service_group_gid(context.host)
        if service_gid is None:
            return CheckResult(Disposition.DRIFT, SERVICE_GROUP_PROBLEM, _SERVICE_GROUP_FIX)
        try:
            names = context.host.listdir(_SECRETS)
        except OSError as exc:
            return CheckResult(Disposition.UNFIXABLE, f"cannot list {_SECRETS}: {exc}", "Repair access to /etc/gideon/secrets, then re-run provision.")
        for name in sorted(names):
            path = _SECRETS / name
            try:
                details = context.host.stat(path)
            except OSError as exc:
                return CheckResult(Disposition.UNFIXABLE, f"cannot stat {path}: {exc}", "Repair access to /etc/gideon/secrets, then re-run provision.")
            if not stat.S_ISREG(details.st_mode):
                continue
            expected = _FILE_MODES.get(name, 0o440)
            actual = stat.S_IMODE(details.st_mode)
            if name in _FILE_MODES:
                if actual != expected:
                    return CheckResult(Disposition.DRIFT, f"{path} mode is {actual:04o}, not {expected:04o}", f"Set {path} to mode {expected:04o}, then re-run provision.")
                continue
            if actual != 0o440 or details.st_uid != 0 or details.st_gid != service_gid:
                return CheckResult(
                    Disposition.DRIFT,
                    f"{path} ownership or mode is wrong",
                    f"Set {path} to root:gideon 0440 (gid {service_gid}), then re-run provision.",
                )
        return CheckResult(Disposition.CONVERGED, "GIDEON directories and secret modes are current", "")

    def apply(self, context: ProvisionContext) -> None:
        for path, expected in _DIR_MODES.items():
            context.host.mkdir(path, mode=expected, parents=True, exist_ok=True)
            context.host.chmod(path, expected)
        service_gid = service_group_gid(context.host)
        if service_gid is None:
            raise RuntimeError(_SERVICE_GROUP_FIX)
        for name in context.host.listdir(_SECRETS):
            path = _SECRETS / name
            try:
                details = context.host.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(details.st_mode):
                context.host.chmod(path, _FILE_MODES.get(name, 0o440))
                if name not in _FILE_MODES:
                    context.host.chown(path, 0, service_gid)


def _public_detail(context: ProvisionContext) -> str:
    if not context.host.exists(_BACKUP_PUBLIC):
        return "public key is missing"
    try:
        public = context.host.read_text(_BACKUP_PUBLIC).strip()
    except (OSError, UnicodeError) as exc:
        return f"public key cannot be read: {exc}"
    return f"public key: {public or '(empty)'}"


class BackupKeypairStep(Step):
    """Generate and own the backup SSH keypair."""

    name = "backup-keypair"
    summary = "generate the gideon backup SSH keypair"
    requires = ("secrets-dirs", "service-user")

    def check(self, context: ProvisionContext) -> CheckResult:
        public_detail = _public_detail(context)
        entry = passwd_entry(context, "gideon")
        if entry is None:
            # The run order resolves this (service-user applies first); on a
            # fresh box it is drift-in-progress, not an unfixable state.
            return CheckResult(
                Disposition.DRIFT,
                f"the gideon service user is missing; {public_detail}",
                "Converge the service-user step, then re-run provision.",
            )
        uid, gid = entry.uid, entry.gid
        for path, expected in ((_BACKUP_KEY, 0o400), (_BACKUP_PUBLIC, 0o440)):
            if not context.host.exists(path):
                return CheckResult(Disposition.DRIFT, f"{path} is missing; {public_detail}", _KEYPAIR_FIX)
            try:
                details = context.host.stat(path)
            except OSError as exc:
                return CheckResult(Disposition.UNFIXABLE, f"cannot stat {path}: {exc}; {public_detail}", _KEYPAIR_FIX)
            actual_mode = stat.S_IMODE(details.st_mode)
            if actual_mode != expected or details.st_uid != uid or details.st_gid != gid:
                return CheckResult(Disposition.DRIFT, f"{path} ownership or mode is wrong; {public_detail}", _KEYPAIR_FIX)
        return CheckResult(Disposition.CONVERGED, f"backup keypair is current; {public_detail}", "")

    def apply(self, context: ProvisionContext) -> None:
        # Never regenerate an existing private key: the public half may already
        # be authorized on the backup target, and ssh-keygen would prompt.
        if not context.host.exists(_BACKUP_KEY):
            context.host.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "gideon-backup",
                    "-f",
                    str(_BACKUP_KEY),
                ],
                check=True,
            )
        elif not context.host.exists(_BACKUP_PUBLIC):
            derived = context.host.run(
                ["ssh-keygen", "-y", "-f", str(_BACKUP_KEY)], check=True
            )
            context.host.write_text(_BACKUP_PUBLIC, derived.stdout)
        entry = passwd_entry(context, "gideon")
        if entry is None:
            raise RuntimeError("cannot resolve gideon ownership")
        uid, gid = entry.uid, entry.gid
        for path, mode in ((_BACKUP_KEY, 0o400), (_BACKUP_PUBLIC, 0o440)):
            context.host.chmod(path, mode)
            context.host.chown(path, uid, gid)


class AgeRecipientStep(Step):
    """Generate the backup recipient, retaining only its public half."""

    name = "age-recipient"
    summary = "generate the age recipient for encrypted backup secrets"
    requires = ("host-tools", "secrets-dirs")

    def check(self, context: ProvisionContext) -> CheckResult:
        if not context.host.exists(AGE_RECIPIENT_PATH):
            return CheckResult(
                Disposition.DRIFT,
                f"{AGE_RECIPIENT_PATH} is missing",
                "Run provision --only age-recipient to generate the backup identity.",
            )
        try:
            recipient = context.host.read_text(AGE_RECIPIENT_PATH).strip()
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {AGE_RECIPIENT_PATH}: {exc}",
                _AGE_RECIPIENT_FIX,
            )
        if AGE_RECIPIENT.fullmatch(recipient) is None:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"{AGE_RECIPIENT_PATH} is not a valid age recipient",
                _AGE_RECIPIENT_FIX,
            )
        return CheckResult(
            Disposition.CONVERGED,
            f"age recipient is current: {recipient}",
            "",
        )

    def apply(self, context: ProvisionContext) -> str | None:
        # The runner only calls apply after a drift check. Keep this guard as
        # well so a direct caller cannot rotate the identity by accident.
        if context.host.exists(AGE_RECIPIENT_PATH):
            return None

        result = context.host.run(["age-keygen"], check=True)
        public_key: str | None = None
        identity: str | None = None
        for line in result.stdout.splitlines():
            if line.startswith("# public key: "):
                candidate = line.removeprefix("# public key: ").strip()
                if AGE_RECIPIENT.fullmatch(candidate) is not None:
                    public_key = candidate
            elif line.startswith(AGE_SECRET_PREFIX):
                identity = line.strip()
        if public_key is None or identity is None:
            raise RuntimeError("age-keygen did not return a valid recipient and identity")

        context.host.write_text(AGE_RECIPIENT_PATH, f"{public_key}\n", mode=0o644)
        return (
            f"{AGE_IDENTITY_PREFIX}{identity}"
        )


class AgeIdentityStep(Step):
    """Generate and retain the box's own age identity for backup sets."""

    name = "age-identity"
    summary = "generate the box's own age identity for encrypted backup sets"
    requires = ("host-tools", "secrets-dirs")

    def check(self, context: ProvisionContext) -> CheckResult:
        if not context.host.exists(AGE_IDENTITY_PATH):
            return CheckResult(
                Disposition.DRIFT,
                f"{AGE_IDENTITY_PATH} is missing",
                _AGE_IDENTITY_STEP_FIX,
            )
        try:
            identity = context.host.read_text(AGE_IDENTITY_PATH).strip()
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {AGE_IDENTITY_PATH}: {exc}",
                AGE_IDENTITY_FIX,
            )
        if AGE_IDENTITY.fullmatch(identity) is None:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"{AGE_IDENTITY_PATH} is not a valid age identity",
                AGE_IDENTITY_FIX,
            )
        try:
            details = context.host.stat(AGE_IDENTITY_PATH)
        except OSError as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot stat {AGE_IDENTITY_PATH}: {exc}",
                AGE_IDENTITY_FIX,
            )
        actual_mode = stat.S_IMODE(details.st_mode)
        if actual_mode != AGE_IDENTITY_MODE:
            return CheckResult(
                Disposition.DRIFT,
                f"{AGE_IDENTITY_PATH} mode is {actual_mode:04o}, not {AGE_IDENTITY_MODE:04o}",
                _AGE_IDENTITY_STEP_FIX,
            )
        if details.st_uid != 0 or details.st_gid != 0:
            return CheckResult(
                Disposition.DRIFT,
                f"{AGE_IDENTITY_PATH} owner is {details.st_uid}:{details.st_gid}, not 0:0",
                _AGE_IDENTITY_STEP_FIX,
            )
        return CheckResult(
            Disposition.CONVERGED,
            f"box identity is current: {AGE_IDENTITY_PATH}",
            "",
        )

    def apply(self, context: ProvisionContext) -> None:
        # A present identity is only ever repaired, never regenerated: every
        # set sealed to it opens on this box with it alone.
        if context.host.exists(AGE_IDENTITY_PATH):
            context.host.chmod(AGE_IDENTITY_PATH, AGE_IDENTITY_MODE)
            context.host.chown(AGE_IDENTITY_PATH, 0, 0)
            return

        # Only the secret line is kept; the public-key comment is discarded,
        # since backup run derives the recipient from the file.
        result = context.host.run(["age-keygen"], check=True)
        identities = [
            line.strip()
            for line in result.stdout.splitlines()
            if AGE_IDENTITY.fullmatch(line.strip()) is not None
        ]
        if len(identities) != 1:
            raise RuntimeError("age-keygen did not return one valid box identity")

        context.host.write_text(
            AGE_IDENTITY_PATH,
            f"{identities[0]}\n",
            mode=AGE_IDENTITY_MODE,
        )
        context.host.chown(AGE_IDENTITY_PATH, 0, 0)

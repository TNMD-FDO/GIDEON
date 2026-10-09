"""The host service account and CSA SSH policy steps."""

import subprocess
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
    box_wide_shortfall,
    stderr_first_line,
)

_NOLOGIN_SHELLS = frozenset({"/usr/sbin/nologin", "/sbin/nologin"})
_CSA_FIX = (
    "Create at least two CSA accounts, grant each sudo access, install each "
    "CSA public key in ~/.ssh/authorized_keys, then re-run provision."
)
# sshd keeps the first value it reads; the installer's 50-cloud-init.conf
# restates the default, so this name must sort ahead of it.
SSHD_POLICY = Path("/etc/ssh/sshd_config.d/00-gideon-key-only.conf")
_SSHD_POLICY_RETIRED = Path("/etc/ssh/sshd_config.d/99-gideon-key-only.conf")
_SSHD_POLICY_TEXT = "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
_SSHD_EFFECTIVE = ("sshd", "-T")
_SSHD_READ_FIX = "Repair the sshd configuration (sshd -t names the error), then re-run provision."


@dataclass(frozen=True, slots=True)
class _SshLoginRule:
    password: str
    keyboard: str


def _ssh_login_rule(context: ProvisionContext) -> _SshLoginRule | CheckResult:
    try:
        result = context.host.run(_SSHD_EFFECTIVE)
    except OSError as exc:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"sshd -T could not report the login rule: {exc}",
            _SSHD_READ_FIX,
        )
    if result.returncode != 0:
        return CheckResult(
            Disposition.UNFIXABLE,
            f"sshd -T could not report the login rule: {stderr_first_line(result.stderr)}",
            _SSHD_READ_FIX,
        )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0] in {
            "passwordauthentication", "kbdinteractiveauthentication"
        }:
            values[fields[0]] = fields[1]
    password = values.get("passwordauthentication")
    keyboard = values.get("kbdinteractiveauthentication")
    if password not in {"yes", "no"} or keyboard not in {"yes", "no"}:
        return CheckResult(
            Disposition.UNFIXABLE,
            "sshd -T could not report the complete login rule",
            _SSHD_READ_FIX,
        )
    return _SshLoginRule(password, keyboard)


def _command_failed(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode != 0


def _passwd_record(output: str, account: str) -> tuple[str, int, str] | None:
    for line in output.splitlines():
        fields = line.split(":")
        if len(fields) >= 7 and fields[0] == account:
            try:
                uid = int(fields[2])
            except ValueError:
                return None
            return fields[5], uid, fields[6]
    return None


class ServiceUserStep(Step):
    """Ensure the system-owned gideon service account exists."""

    name = "service-user"
    summary = "ensure the gideon system user uses nologin"

    def check(self, context: ProvisionContext) -> CheckResult:
        result = context.host.run(["getent", "passwd", "gideon"])
        if _command_failed(result):
            return CheckResult(
                Disposition.DRIFT,
                "the gideon user does not exist",
                "Create the gideon system user, then re-run provision.",
            )
        record = _passwd_record(result.stdout, "gideon")
        if record is None:
            return CheckResult(
                Disposition.UNFIXABLE,
                "getent returned an invalid gideon passwd record",
                "Repair the gideon passwd record as a system user, then re-run provision.",
            )
        _, uid, shell = record
        if uid >= 1000 or shell not in _NOLOGIN_SHELLS:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"gideon is not a system nologin user (uid {uid}, shell {shell!r})",
                "Recreate gideon as a system user with /usr/sbin/nologin, then re-run provision.",
            )
        return CheckResult(Disposition.CONVERGED, "gideon system user is current", "")

    def apply(self, context: ProvisionContext) -> None:
        context.host.run(
            [
                "useradd",
                "--system",
                "--shell",
                "/usr/sbin/nologin",
                "gideon",
            ],
            check=True,
        )


class CsaAccountsStep(Step):
    """Check CSA access and converge the SSH key-only policy."""

    name = "csa-accounts"
    summary = "verify two CSA sudo accounts and enforce SSH key-only login"
    settings = (BoxWideSetting("SSH login rule"),)

    def _csa_accounts(self, context: ProvisionContext) -> tuple[bool, str]:
        group = context.host.run(["getent", "group", "sudo"])
        if _command_failed(group):
            return False, "the sudo group could not be read"
        members: list[str] = []
        for line in group.stdout.splitlines():
            fields = line.split(":")
            if len(fields) >= 4 and fields[0] == "sudo":
                members.extend(account for account in fields[3].split(",") if account)
        valid = 0
        for account in members:
            passwd = context.host.run(["getent", "passwd", account])
            if _command_failed(passwd):
                continue
            record = _passwd_record(passwd.stdout, account)
            if record is None:
                continue
            home, _, _ = record
            try:
                keys = context.host.read_text(Path(home) / ".ssh" / "authorized_keys")
            except (OSError, UnicodeError):
                continue
            if keys.strip():
                valid += 1
        if valid < 2:
            return False, f"only {valid} sudo account(s) have readable authorized_keys"
        return True, f"{valid} sudo accounts have readable authorized_keys"

    def check(self, context: ProvisionContext) -> CheckResult:
        accounts_ok, detail = self._csa_accounts(context)
        if not accounts_ok:
            return CheckResult(Disposition.UNFIXABLE, detail, _CSA_FIX)

        rule = _ssh_login_rule(context)
        if isinstance(rule, CheckResult):
            return rule
        present = context.host.exists(SSHD_POLICY)
        retired = context.host.exists(_SSHD_POLICY_RETIRED)
        current = None
        if present:
            try:
                current = context.host.read_text(SSHD_POLICY)
            except (OSError, UnicodeError) as exc:
                return CheckResult(
                    Disposition.UNFIXABLE,
                    f"cannot read {SSHD_POLICY}: {exc}",
                    f"Correct access to {SSHD_POLICY}, then re-run provision.",
                )
        if rule.password == "no" and rule.keyboard == "no":
            if current == _SSHD_POLICY_TEXT and not retired:
                return CheckResult(
                    Disposition.CONVERGED,
                    f"{SSHD_POLICY} is current and key-only login is in effect; {detail}",
                    "",
                )
            if not present and not retired:
                return CheckResult(
                    Disposition.CONVERGED,
                    f"key-only login is in effect, set outside GIDEON's drop-in; {detail}",
                    "",
                )
            if not present:
                state = "at its retired name"
            elif current != _SSHD_POLICY_TEXT:
                state = "stale"
            else:
                state = "current beside its retired file"
            return CheckResult(
                Disposition.DRIFT,
                f"GIDEON's drop-in is {state}; key-only login is in effect; {detail}",
                f"Run provision, which writes {SSHD_POLICY}.",
            )
        if current == _SSHD_POLICY_TEXT:
            found = " and ".join(
                name for name, value in (
                    ("PasswordAuthentication yes", rule.password),
                    ("KbdInteractiveAuthentication yes", rule.keyboard),
                ) if value == "yes"
            )
            return CheckResult(
                Disposition.UNFIXABLE,
                box_wide_shortfall(
                    self.settings[0], found, "no for both", source=f"a file ahead of {SSHD_POLICY}"
                ),
                BOX_WIDE_SHORTFALL_FIX,
            )
        return CheckResult(
            Disposition.DRIFT,
            f"password login is allowed, the installed default; GIDEON sets key-only login; {detail}",
            f"Run provision, which writes {SSHD_POLICY} and reloads ssh.",
        )

    def apply(self, context: ProvisionContext) -> None:
        reading = self.check(context)
        if reading.disposition is Disposition.UNFIXABLE:
            raise StepFailure(reading.detail, reading.fix)
        if reading.disposition is Disposition.CONVERGED:
            return
        context.host.write_text(SSHD_POLICY, _SSHD_POLICY_TEXT)
        if context.host.exists(_SSHD_POLICY_RETIRED):
            context.host.unlink(_SSHD_POLICY_RETIRED, missing_ok=True)
        # Ubuntu's unit is ssh; the sshd alias only resolves while enabled.
        context.host.run(["systemctl", "reload", "ssh"], check=True)

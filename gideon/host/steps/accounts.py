"""The host service account and CSA SSH policy steps."""

import subprocess
from pathlib import Path

from gideon.host.steps import CheckResult, Disposition, ProvisionContext, Step

_NOLOGIN_SHELLS = frozenset({"/usr/sbin/nologin", "/sbin/nologin"})
_CSA_FIX = (
    "Create at least two CSA accounts, grant each sudo access, install each "
    "CSA public key in ~/.ssh/authorized_keys, then re-run provision."
)
_SSHD_POLICY = Path("/etc/ssh/sshd_config.d/99-gideon-key-only.conf")
_SSHD_POLICY_TEXT = "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"


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

        if not context.host.exists(_SSHD_POLICY):
            return CheckResult(
                Disposition.DRIFT,
                f"{_SSHD_POLICY} is missing; {detail}",
                f"Write the key-only SSH policy to {_SSHD_POLICY}.",
            )
        try:
            current = context.host.read_text(_SSHD_POLICY)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_SSHD_POLICY}: {exc}",
                f"Correct access to {_SSHD_POLICY}, then re-run provision.",
            )
        if current != _SSHD_POLICY_TEXT:
            return CheckResult(
                Disposition.DRIFT,
                f"{_SSHD_POLICY} does not enforce key-only SSH; {detail}",
                f"Rewrite {_SSHD_POLICY} with the key-only SSH policy.",
            )
        return CheckResult(Disposition.CONVERGED, f"{_SSHD_POLICY} is current; {detail}", "")

    def apply(self, context: ProvisionContext) -> None:
        context.host.write_text(_SSHD_POLICY, _SSHD_POLICY_TEXT)
        # Ubuntu's unit is ssh; the sshd alias only resolves while enabled.
        context.host.run(["systemctl", "reload", "ssh"], check=True)

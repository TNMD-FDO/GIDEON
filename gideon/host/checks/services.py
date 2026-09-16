"""Checks for the office services used during installation."""

import shlex
import smtplib
import ssl
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from pathlib import Path

from gideon.host.checks import (
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
    read_secret_file,
)
from gideon.host.ldap import (
    LDAP_PASSWORD,
    LDAP_TIMEOUT_SECONDS,
    NO_SUCH_OBJECT,
    filter_value,
    ldapsearch_argv,
    read_ldif,
)
from gideon.host.sshtarget import BACKUP_PROBE_SHA256, remote_script, ssh_argv

SMTP_PASSWORD = Path("/etc/gideon/secrets/smtp_password")
SERVICE_TIMEOUT_SECONDS = 10

_LDAP_BIND_FIX = "Fix the LDAP bind account in the §3.6 step 0 checklist, then re-run preflight."
_BACKUP_AUTH_FIX = "Authorize the printed public key on the backup target per §1.9 step 3, then re-run preflight."
_BACKUP_PATH_FIX = "Correct the backup target path and its permissions as recorded in §3.7, then re-run preflight."
_SMTP_FIX = "Permit this host and sender through the office SMTP relay, then re-run preflight."


def _ldap_group_fix(leaf: str) -> str:
    return (
        f"set auth.ldap.{leaf} to the group's distinguished name "
        "(get-ADGroup … | select DistinguishedName), then re-run preflight."
    )


def _ldap_member_of_fix(bind_user: str) -> str:
    return (
        f"Grant {bind_user} read of memberOf on user objects (add it to AD's "
        "'Pre-Windows 2000 Compatible Access' group, or delegate Read memberOf "
        "on the users OU), then re-run preflight."
    )


def _ldap_incomplete_member_of_fix(bind_user: str) -> str:
    return (
        f"Grant {bind_user} read of memberOf on those objects' OUs (or delegate it "
        "on the users OU), or move them under auth.ldap.search_base — the frontend "
        "searches only there — then re-run preflight."
    )


class LdapCheck(PreflightCheck):
    """Bind to the office directory and resolve every configured group by DN."""

    name = "ldap"
    summary = "bind to LDAPS and resolve the GIDEON LDAP groups"

    def run(self, context: PreflightContext) -> CheckReport:
        password = read_secret_file(context.host, LDAP_PASSWORD.name)
        if not password.ok:
            return CheckReport(
                Severity.REFUSE,
                password.problem or "LDAP bind password is unavailable",
                password.fix,
            )

        ldap = context.site.auth.ldap
        groups: list[tuple[str, str]] = [
            ("users_group", ldap.users_group_dn),
            ("admins_group", ldap.admins_group_dn),
        ]
        groups.extend(
            (f"mirror_groups[{index}]", group_dn)
            for index, group_dn in enumerate(ldap.mirror_group_dns)
        )
        missing: list[tuple[str, str]] = []
        users_members: tuple[str, ...] = ()
        for index, (leaf, group_dn) in enumerate(groups):
            result = context.host.run(
                ldapsearch_argv(
                    ldap.host,
                    ldap.port,
                    ldap.bind_user,
                    group_dn,
                    "(objectClass=group)",
                    ("dn", "member"),
                    scope="base",
                ),
                timeout=LDAP_TIMEOUT_SECONDS,
            )
            # A base-scope search at an absent DN is the directory saying
            # "no such object" (exit 32), not a bind or transport failure.
            entries = read_ldif(result.stdout) if result.returncode == 0 else ()
            if result.returncode == NO_SUCH_OBJECT or (result.returncode == 0 and not entries):
                missing.append((leaf, group_dn))
                continue
            if result.returncode != 0:
                if index == 0:
                    return CheckReport(
                        Severity.REFUSE,
                        "LDAP bind or group search failed",
                        _LDAP_BIND_FIX,
                    )
                return CheckReport(
                    Severity.REFUSE,
                    f"LDAP search failed while resolving auth.ldap.{leaf} ({group_dn})",
                    _ldap_group_fix(leaf),
                )
            if leaf == "users_group":
                users_members = entries[0].attributes.get("member", ())
        if missing:
            rendered = ", ".join(f"auth.ldap.{leaf} ({dn})" for leaf, dn in missing)
            return CheckReport(
                Severity.REFUSE,
                f"LDAP group(s) not found: {rendered}",
                _ldap_group_fix(missing[0][0]),
            )

        # The login filter and the reconcile both select users by memberOf,
        # a back-link the bind account must be allowed to read: a group with
        # members that the memberOf search cannot find is that permission gap.
        if not users_members:
            return CheckReport(
                Severity.WARN,
                f"the users group has no members yet ({ldap.users_group_dn}); nobody can sign in",
            )
        visible = context.host.run(
            ldapsearch_argv(
                ldap.host,
                ldap.port,
                ldap.bind_user,
                ldap.search_base,
                f"(memberOf={filter_value(ldap.users_group_dn)})",
                ("sAMAccountName", "userPrincipalName"),
            ),
            timeout=LDAP_TIMEOUT_SECONDS,
        )
        visible_entries = read_ldif(visible.stdout) if visible.returncode == 0 else ()
        if not visible_entries:
            return CheckReport(
                Severity.REFUSE,
                f"the bind account cannot see the users group's {len(users_members)} member(s) through memberOf",
                _ldap_member_of_fix(ldap.bind_user),
            )

        # "Some result" is not "every member": a per-OU memberOf delegation or a
        # member outside the search base leaves a partial view, and the members
        # it hides are exactly the ones the UPN judgement below could not see.
        expected_dns = {member.strip().casefold() for member in users_members}
        visible_dns = {entry.dn.strip().casefold() for entry in visible_entries}
        invisible_dns = tuple(
            dict.fromkeys(
                member.strip()
                for member in users_members
                if member.strip().casefold() not in visible_dns
            )
        )
        if invisible_dns:
            return CheckReport(
                Severity.REFUSE,
                (
                    f"the bind account cannot see {len(invisible_dns)} of "
                    f"{len(expected_dns)} users-group member(s) through memberOf: "
                    f"{', '.join(invisible_dns)}"
                ),
                _ldap_incomplete_member_of_fix(ldap.bind_user),
            )

        missing_upn: list[str] = []
        for entry in visible_entries:
            if entry.dn.strip().casefold() not in expected_dns:
                continue
            upn = entry.attributes.get("userprincipalname", ())
            if upn and upn[0].strip():
                continue
            accounts = entry.attributes.get("samaccountname", ())
            missing_upn.append(
                accounts[0] if accounts and accounts[0].strip() else entry.dn
            )
        if missing_upn:
            return CheckReport(
                Severity.REFUSE,
                (
                    "users-group member(s) without a userPrincipalName: "
                    + ", ".join(missing_upn)
                ),
                (
                    "Set the User logon name (userPrincipalName) on each named account "
                    "in ADUC (Account tab), or remove a non-user member from the group "
                    "(direct membership only), then re-run preflight."
                ),
            )
        return CheckReport(
            Severity.PASS,
            f"LDAP bind, all configured GIDEON groups, and {len(users_members)} users-group member(s) resolved, each with a userPrincipalName",
        )


class BackupSshCheck(PreflightCheck):
    """Check backup-target authentication and writability."""

    name = "backup-ssh"
    summary = "connect to the backup target, write a probe file, and verify its tools"

    def run(self, context: PreflightContext) -> CheckReport:
        target = context.site.backup.target
        connected = context.host.run(ssh_argv(context.site, "true"))
        if connected.returncode != 0:
            return CheckReport(
                Severity.REFUSE,
                f"cannot connect or authenticate to {target.user}@{target.host}",
                _BACKUP_AUTH_FIX,
            )

        script = (
            "probe_dir="
            + shlex.quote(target.path)
            + '; probe="$probe_dir/.gideon-preflight.$$"; '
            + '(umask 077; : > "$probe") && rm -f -- "$probe"'
        )
        # ssh joins remote argv words with spaces for the remote shell to
        # re-parse, so the script must travel as one quoted word.
        writable = context.host.run(
            ssh_argv(context.site, remote_script(script))
        )
        if writable.returncode != 0:
            return CheckReport(
                Severity.REFUSE,
                f"backup target path is not writable: {target.path}",
                _BACKUP_PATH_FIX,
            )
        # The target must receive (rsync) and verify (sha256sum -c over a
        # list on stdin); the probe hashes a known file it writes beside the
        # writability probe, since stdin is the list, not the data.
        tools_script = (
            "probe_dir="
            + shlex.quote(target.path)
            + '; probe="$probe_dir/.gideon-preflight-tools.$$"; '
            + "command -v rsync sha256sum >/dev/null "
            + "&& printf 'gideon\\n' > \"$probe\" "
            + f"&& printf '%s  %s\\n' {BACKUP_PROBE_SHA256} \"$probe\" | sha256sum -c - >/dev/null; "
            + 'rc=$?; rm -f -- "$probe"; exit $rc'
        )
        tools = context.host.run(
            ssh_argv(context.site, remote_script(tools_script))
        )
        if tools.returncode != 0:
            return CheckReport(
                Severity.REFUSE,
                "backup target lacks rsync or a working sha256sum -c",
                "Install rsync and coreutils sha256sum on the backup target per the office-services runbook (§3.7), then re-run preflight.",
            )
        return CheckReport(Severity.PASS, f"backup target path is writable: {target.path}")


SmtpTransport = Callable[[str, int, str, Sequence[str], str, str | None, str | None], None]


def smtp_transport(
    host: str,
    port: int,
    sender: str,
    recipients: Sequence[str],
    subject: str,
    user: str | None,
    password: str | None,
) -> None:
    """Send one message, using STARTTLS and AUTH when requested."""

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content("GIDEON preflight SMTP transport check.")
    with smtplib.SMTP(host, port, timeout=SERVICE_TIMEOUT_SECONDS) as connection:
        connection.ehlo()
        offers_tls = connection.has_extn("starttls")
        if user is not None and not offers_tls:
            raise RuntimeError(
                "relay does not offer STARTTLS; refusing to send credentials in the clear"
            )
        if offers_tls:
            tls = ssl.create_default_context()
            if Path("/etc/gideon/ca.pem").exists():
                tls.load_verify_locations("/etc/gideon/ca.pem")
            connection.starttls(context=tls)
            connection.ehlo()
        if user is not None:
            connection.login(user, password or "")
        connection.send_message(message)


class SmtpCheck(PreflightCheck):
    """Send one test message through the configured office relay."""

    name = "smtp"
    summary = "send one test message through the office SMTP relay"

    def __init__(self, transport: SmtpTransport = smtp_transport) -> None:
        self.transport = transport

    def run(self, context: PreflightContext) -> CheckReport:
        password: str | None = None
        if context.host.exists(SMTP_PASSWORD):
            result = read_secret_file(context.host, SMTP_PASSWORD.name)
            if not result.ok:
                return CheckReport(
                    Severity.REFUSE,
                    result.problem or "SMTP password is unavailable",
                    result.fix,
                )
            password = result.value or ""
            if not context.site.alerts.smtp.user:
                return CheckReport(
                    Severity.REFUSE,
                    "smtp_password is present but alerts.smtp.user is unset",
                    "Set the alerts.smtp.user key in /etc/gideon/site.yaml, then re-run preflight.",
                )

        smtp = context.site.alerts.smtp
        subject = f"GIDEON preflight — {context.site.hostname}"
        try:
            self.transport(
                smtp.host,
                smtp.port,
                smtp.from_,
                context.site.alerts.recipients,
                subject,
                smtp.user if password is not None else None,
                password,
            )
        except Exception as exc:  # noqa: BLE001  # service boundary
            return CheckReport(
                Severity.REFUSE,
                f"SMTP test message failed: {type(exc).__name__}: {exc}",
                _SMTP_FIX,
            )
        return CheckReport(Severity.PASS, "SMTP test message accepted by relay")

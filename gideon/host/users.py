"""LDAP-backed Open WebUI role reconciliation.

The directory is the source of membership, while Open WebUI remains the
source of user and knowledge-base identifiers.  Host commands and audit SQL
use the host seam; no credential is included in a report line.
"""

import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import audit, ldap, owui, secrets, site, stack, tls
from gideon.host.render.owui import BREAK_GLASS, MACHINE_IDENTITIES
from gideon.host.report import refusal
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final[str] = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final[str] = "/etc/gideon/rendered"
_ROOT_FIX: Final[str] = "Run gideon users reconcile as root, for example with sudo."
_ADMIN_KEY_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry"
_LDAP_FIX: Final[str] = (
    "Fix the LDAP bind account and group distinguished names per "
    "docs/runbooks/office-services-setup.md §1, then retry."
)
_AUDIT_FIX: Final[str] = stack.logs_fix(_RENDERED_DIR, "postgres")
_FRONTEND_FIX: Final[str] = stack.logs_fix(_RENDERED_DIR, "open-webui")
# Exempt operational constant: how long a reboot catch-up run waits for the
# frontend (attempts × the readiness wait's 5 s) before refusing.
_READY_ATTEMPTS: Final[int] = 12


@dataclass(frozen=True, slots=True)
class Correction:
    """One frontend role change required by directory membership."""

    user_id: str
    from_role: str
    to_role: str
    reason: str


@dataclass(frozen=True, slots=True)
class Orphan:
    """A knowledge base whose owner is pending or no longer present."""

    kb_id: str
    owner_id: str
    owner_role: str


@dataclass(frozen=True, slots=True)
class SnapshotMember:
    """A directory account, its userPrincipalName, and its Open WebUI user id, if any."""

    account: str
    upn: str | None
    user_id: str | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The direct directory membership observed for one configured group."""

    group: str
    members: tuple[SnapshotMember, ...]


@dataclass(frozen=True, slots=True)
class _DirectoryMember:
    account: str
    upn: str | None


@dataclass(frozen=True, slots=True)
class _DirectoryGroup:
    leaf: str
    configured_value: str
    members: tuple[_DirectoryMember, ...]


@dataclass(frozen=True, slots=True)
class _ConfiguredGroup:
    leaf: str
    configured_value: str
    group_dn: str


@dataclass(frozen=True, slots=True)
class _DirectoryFailure:
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class _MatchedMember:
    member: _DirectoryMember
    user_id: str | None


def _refuse(problem: str, fix: str) -> int:
    print(refusal("users reconcile", problem, fix), file=sys.stderr)
    return 1


def _frontend_failure(error: Exception) -> tuple[str, str]:
    if isinstance(error, owui.OwuiError):
        return error.problem, error.fix or _FRONTEND_FIX
    return "Open WebUI request failed.", _FRONTEND_FIX


def _read_directory_group(
    io: Host,
    config: site.Ldap,
    *,
    leaf: str,
    configured_value: str,
    group_dn: str,
) -> _DirectoryGroup | _DirectoryFailure:
    filter_expression = f"(&(objectClass=user)(memberOf={ldap.filter_value(group_dn)}))"
    argv = ldap.ldapsearch_argv(
        config.host,
        config.port,
        config.bind_user,
        config.search_base,
        filter_expression,
        ("sAMAccountName", "userPrincipalName"),
    )
    try:
        result = io.run(argv, timeout=ldap.LDAP_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return _DirectoryFailure(
            f"LDAP membership search failed or timed out for auth.ldap.{leaf}.", _LDAP_FIX
        )
    if result.returncode != 0:
        return _DirectoryFailure(
            (
                f"LDAP membership search failed for auth.ldap.{leaf} "
                f"(exit {result.returncode})."
            ),
            _LDAP_FIX,
        )
    try:
        entries = ldap.read_ldif(result.stdout)
    except (ValueError, UnicodeError):
        return _DirectoryFailure(
            f"LDAP membership response for auth.ldap.{leaf} is invalid.", _LDAP_FIX
        )
    members: list[_DirectoryMember] = []
    for entry in entries:
        accounts = entry.attributes.get("samaccountname", ())
        if not accounts or not accounts[0]:
            continue
        upn_values = entry.attributes.get("userprincipalname", ())
        upn = upn_values[0].casefold() if upn_values and upn_values[0] else None
        members.append(_DirectoryMember(accounts[0], upn))
    return _DirectoryGroup(leaf, configured_value, tuple(members))


def _directory_groups(
    io: Host, config: site.Ldap
) -> tuple[_DirectoryGroup, ...] | _DirectoryFailure:
    configured: list[_ConfiguredGroup] = [
        _ConfiguredGroup("users_group", config.users_group, config.users_group_dn),
        _ConfiguredGroup("admins_group", config.admins_group, config.admins_group_dn),
    ]
    configured.extend(
        _ConfiguredGroup(f"mirror_groups[{index}]", value, group_dn)
        for index, (value, group_dn) in enumerate(
            zip(config.mirror_groups, config.mirror_group_dns, strict=True)
        )
    )
    groups: list[_DirectoryGroup] = []
    for group in configured:
        result = _read_directory_group(
            io,
            config,
            leaf=group.leaf,
            configured_value=group.configured_value,
            group_dn=group.group_dn,
        )
        if isinstance(result, _DirectoryFailure):
            return result
        groups.append(result)
    return tuple(groups)


def _user_match(
    member: _DirectoryMember,
    *,
    users_by_upn: Mapping[str, owui.User],
) -> owui.User | None:
    if member.upn is None:
        return None
    return users_by_upn.get(member.upn)


def _matched_members(
    group: _DirectoryGroup,
    *,
    users_by_upn: Mapping[str, owui.User],
) -> tuple[_MatchedMember, ...]:
    matched: list[_MatchedMember] = []
    for member in group.members:
        user = _user_match(member, users_by_upn=users_by_upn)
        matched.append(_MatchedMember(member, None if user is None else user.id))
    return tuple(matched)


def _snapshot(
    group: _DirectoryGroup,
    *,
    users_by_upn: Mapping[str, owui.User],
) -> Snapshot:
    return Snapshot(
        group=site.group_name(group.configured_value),
        members=tuple(
            SnapshotMember(matched.member.account, matched.member.upn, matched.user_id)
            for matched in _matched_members(
                group,
                users_by_upn=users_by_upn,
            )
        ),
    )


def _audit_row(
    run_id: str,
    kind: str,
    actor_user_id: str,
    *,
    user_id: str | None = None,
    detail: Mapping[str, object] | None = None,
) -> audit.AuditRow:
    return audit.AuditRow(
        run_id=run_id,
        kind=kind,
        actor_user_id=actor_user_id,
        user_id=user_id,
        chat_id=None,
        kb_ids=(),
        detail={} if detail is None else detail,
    )


def _snapshot_rows(
    run_id: str, actor_user_id: str, snapshots: tuple[Snapshot, ...]
) -> tuple[audit.AuditRow, ...]:
    return tuple(
        _audit_row(
            run_id,
            "membership_snapshot",
            actor_user_id,
            detail={
                "group": snapshot.group,
                "members": [
                    {
                        "account": member.account,
                        "upn": member.upn,
                        "user_id": member.user_id,
                    }
                    for member in snapshot.members
                ],
            },
        )
        for snapshot in snapshots
    )


def _correction_line(correction: Correction) -> str:
    return f"{correction.user_id}: {correction.from_role} → {correction.to_role} ({correction.reason})"


def _orphan_line(orphan: Orphan) -> str:
    return f"orphaned knowledge base {orphan.kb_id}: owner {orphan.owner_id} is {orphan.owner_role}"


def _summary(corrections: int, orphans: int, snapshots: int) -> str:
    return (
        f"Summary: {corrections} correction(s), {orphans} orphaned knowledge base(s), "
        f"{snapshots} group(s) snapshotted."
    )


def _write_audit_rows(
    io: Host,
    rendered_dir: PathLike,
    rows: tuple[audit.AuditRow, ...],
    *,
    description: str,
) -> tuple[str, str] | None:
    problem = audit.write_rows(io, rendered_dir, rows)
    if problem is None:
        return None
    return (f"{description}: {problem}", _AUDIT_FIX)


def run_reconcile(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    client_factory: Callable[..., owui.Client] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Report or enforce LDAP membership's role truth for Open WebUI."""

    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _ROOT_FIX)

    site_result = site.load_site(Path(site_path), host=io)
    if site_result.errors or site_result.config is None:
        return _refuse(
            site.render_errors(site_result.errors),
            "Correct the site file, then retry.",
        )
    config = site_result.config
    make_client = client_factory or owui.ingress_client_factory(config.hostname, ca_path=tls.CA_PATH)

    admin_secret = secrets.read_secret(io, "gideon_admin_api_key")
    if not admin_secret.ok or admin_secret.value is None:
        if admin_secret.missing:
            return _refuse("the break-glass API key is missing.", _ADMIN_KEY_FIX)
        return _refuse(
            admin_secret.problem or "the break-glass API key is unavailable.",
            admin_secret.fix or _ADMIN_KEY_FIX,
        )

    try:
        ready = owui.wait_ready(make_client(), attempts=_READY_ATTEMPTS, sleep=sleep)
    except (owui.OwuiError, OSError) as exc:
        problem, fix = _frontend_failure(exc)
        return _refuse(problem, fix)
    if not ready.ok:
        return _refuse(ready.problem or "Open WebUI is not ready.", _FRONTEND_FIX)

    enforce = bool(getattr(args, "now", False))
    if enforce:
        audit_problem = audit.probe(io, rendered_dir)
        if audit_problem is not None:
            return _refuse(f"audit writer is unavailable: {audit_problem}", _AUDIT_FIX)

    directory_groups = _directory_groups(io, config.auth.ldap)
    if isinstance(directory_groups, _DirectoryFailure):
        return _refuse(directory_groups.problem, directory_groups.fix)

    try:
        admin_client = make_client(api_key=admin_secret.value)
        frontend_users = admin_client.users_all()
    except (owui.OwuiError, OSError) as exc:
        problem, fix = _frontend_failure(exc)
        return _refuse(problem, fix)

    machine_emails = frozenset(
        identity.email.casefold() for identity in MACHINE_IDENTITIES
    )
    humans = tuple(user for user in frontend_users if user.email.casefold() not in machine_emails)
    users_by_upn = {
        user.email.casefold(): user for user in humans if user.email
    }

    users_group, admins_group = directory_groups[:2]
    matched_users = _matched_members(
        users_group,
        users_by_upn=users_by_upn,
    )
    matched_admins = _matched_members(
        admins_group,
        users_by_upn=users_by_upn,
    )
    users_ids = frozenset(
        matched.user_id for matched in matched_users if matched.user_id is not None
    )
    admins_ids = frozenset(
        matched.user_id for matched in matched_admins if matched.user_id is not None
    )
    users_group_name = site.group_name(config.auth.ldap.users_group)
    admins_group_name = site.group_name(config.auth.ldap.admins_group)

    corrections: list[Correction] = []
    for user in humans:
        if user.id in users_ids:
            expected = "admin" if user.id in admins_ids else "user"
            if user.id in admins_ids:
                reason = f"in {admins_group_name}"
            elif user.role == "admin":
                reason = f"not in {admins_group_name}"
            else:
                reason = f"in {users_group_name}"
        else:
            expected = "pending"
            reason = f"not in {users_group_name}"
        if user.role != expected:
            corrections.append(Correction(user.id, user.role, expected, reason))

    users_accounts = {
        matched_member.member.account.casefold() for matched_member in matched_users
    }
    inconsistencies = (
        tuple(
            f"inconsistency: {matched.member.account} is in {admins_group_name} but not "
            f"{users_group_name}"
            for matched in matched_admins
            if matched.member.account.casefold() not in users_accounts
        )
        + tuple(
            f"inconsistency: {member.account} has no userPrincipalName"
            for member in users_group.members
            if member.upn is None
        )
    )

    try:
        knowledge = admin_client.knowledge_all()
    except (owui.OwuiError, OSError) as exc:
        problem, fix = _frontend_failure(exc)
        return _refuse(problem, fix)
    # An orphan's owner is gone or pending once this run's corrections land:
    # judged on the post-reconcile role, so a restored owner is not an orphan
    # and one being set pending is reported now.
    users_by_id = {user.id: user for user in frontend_users}
    final_role = {user.id: user.role for user in frontend_users}
    final_role.update((correction.user_id, correction.to_role) for correction in corrections)
    orphans = tuple(
        Orphan(
            knowledge_base.id,
            knowledge_base.user_id,
            "pending" if knowledge_base.user_id in users_by_id else "missing",
        )
        for knowledge_base in knowledge
        if final_role.get(knowledge_base.user_id, "missing") in ("pending", "missing")
    )

    snapshots = tuple(
        _snapshot(
            group,
            users_by_upn=users_by_upn,
        )
        for group in directory_groups
    )
    if not enforce:
        for line in (
            *(_correction_line(correction) for correction in corrections),
            *inconsistencies,
            *(_orphan_line(orphan) for orphan in orphans),
            f"snapshot: {len(snapshots)} groups recorded",
            _summary(len(corrections), len(orphans), len(snapshots)),
        ):
            print(f"would {line}")
        return 0

    break_glass = next(
        (user for user in frontend_users if user.email.casefold() == BREAK_GLASS.email.casefold()),
        None,
    )
    if break_glass is None:
        return _refuse(
            "the break-glass administrator is missing.",
            _ADMIN_KEY_FIX,
        )
    actor_user_id = break_glass.id
    run_id = str(uuid.uuid4())
    snapshot_problem = _write_audit_rows(
        io,
        rendered_dir,
        _snapshot_rows(run_id, actor_user_id, snapshots),
        description="membership snapshot audit write failed",
    )
    if snapshot_problem is not None:
        problem, fix = snapshot_problem
        return _refuse(problem, fix)
    print(f"snapshot: {len(snapshots)} groups recorded")

    # Each correction is announced only once its applied row exists, so stdout
    # never claims work an interrupted run did not finish.
    applied_count = 0
    for correction in corrections:
        detail = {
            "from": correction.from_role,
            "to": correction.to_role,
            "reason": correction.reason,
        }
        intent_problem = _write_audit_rows(
            io,
            rendered_dir,
            (
                _audit_row(
                    run_id,
                    "reconcile.role.intent",
                    actor_user_id,
                    user_id=correction.user_id,
                    detail=detail,
                ),
            ),
            description=f"role intent audit write failed for {correction.user_id}",
        )
        if intent_problem is not None:
            problem, fix = intent_problem
            return _refuse(problem, fix)
        try:
            admin_client.update_role(correction.user_id, correction.to_role)
        except (owui.OwuiError, OSError) as exc:
            problem, fix = _frontend_failure(exc)
            return _refuse(f"role update failed for {correction.user_id}: {problem}", fix)
        applied_problem = _write_audit_rows(
            io,
            rendered_dir,
            (
                _audit_row(
                    run_id,
                    "reconcile.role.applied",
                    actor_user_id,
                    user_id=correction.user_id,
                    detail=detail,
                ),
            ),
            description=f"role applied audit write failed for {correction.user_id}",
        )
        if applied_problem is not None:
            problem, fix = applied_problem
            return _refuse(problem, fix)
        print(_correction_line(correction))
        applied_count += 1
    for line in inconsistencies:
        print(line)
    for orphan in orphans:
        print(_orphan_line(orphan))
    print(_summary(applied_count, len(orphans), len(snapshots)))
    return 0

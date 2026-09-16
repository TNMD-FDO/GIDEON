"""The acceptance VM's upgrade/rollback rehearsal and final verification.

The temporary release is made from the harness's root-owned mirror.  The
box checkout is deliberately not involved: the VM receives the tag through
its own mirror, and all four upgrade legs run against the checkout already
installed in the VM.
"""

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import yaml  # type: ignore[import-untyped]

from gideon.host.report import Problem, StageResult, command_detail
from gideon.host.sysio import Host
from tools.acceptance import vm
from tools.acceptance.context import HarnessContext, authenticated_messages

REHEARSAL_TIMEOUT_SECONDS: Final = 1800
VERIFY_PREFLIGHT_TIMEOUT_SECONDS: Final = 900
MIRROR_NAME: Final = "GIDEON.git"
WORKTREE_NAME: Final = "rehearsal"
APPLIED_RECORD: Final = "/etc/gideon/rendered/applied.yaml"
REHEARSAL_FIX: Final = (
    "Inspect the rehearsal transcripts and the acceptance VM, then retry acceptance."
)
RESTORE_FIX: Final = (
    "Inspect the restore transcripts and the acceptance VM, then retry acceptance."
)
VERIFY_FIX: Final = (
    "Inspect the verification transcript and the acceptance VM, then retry acceptance."
)
_VERSION_LINE: Final = re.compile(
    r"(?m)^(?P<prefix>\s*__version__\s*=\s*['\"])(?P<value>[^'\"]+)(?P<suffix>['\"]\s*)$"
)
_SEMVER: Final = re.compile(r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)$")


def _problem(problem: str, fix: str = REHEARSAL_FIX) -> Problem:
    return Problem(problem, fix)


def _git(
    host: Host,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a non-interactive git command through the host seam."""

    try:
        return host.run(argv, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(list(argv), 1, "", str(exc))


def base_version(ctx: HarnessContext) -> str | Problem:
    """Read ``__version__`` from the resolved commit in the run's mirror."""

    mirror = ctx.spec.run_dir / MIRROR_NAME
    argv = [
        "git",
        "-C",
        str(mirror),
        "show",
        f"{ctx.spec.resolved_commit}:gideon/__init__.py",
    ]
    result = _git(ctx.host, argv)
    if result.returncode != 0:
        return _problem(
            f"could not read the release version from {mirror}",
            "Inspect the acceptance mirror with git, then retry acceptance.",
        )
    matches = list(_VERSION_LINE.finditer(result.stdout))
    if len(matches) != 1:
        return _problem(
            f"{mirror} does not contain exactly one __version__ pin",
            "Repair gideon/__init__.py in the ref, then retry acceptance.",
        )
    return matches[0].group("value")


def rc_tag(version: str) -> str:
    """Return the rc tag immediately after the release's patch version."""

    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported release version: {version}")
    return (
        f"v{match.group('major')}.{match.group('minor')}."
        f"{int(match.group('patch')) + 1}-rc.1"
    )


def _replace_version_line(
    text: str, old_version: str, new_version: str
) -> str | Problem:
    matches = list(_VERSION_LINE.finditer(text))
    if len(matches) != 1 or matches[0].group("value") != old_version:
        return _problem(
            "the rehearsal worktree does not contain exactly one __version__ pin",
            "Repair gideon/__init__.py in the ref, then retry acceptance.",
        )
    match = matches[0]
    return text[: match.start("value")] + new_version + text[match.end("value") :]


def _git_failure(action: str, result: subprocess.CompletedProcess[str]) -> StageResult:
    return StageResult(
        "rehearse",
        False,
        f"{action}: {command_detail(result)}",
        REHEARSAL_FIX,
    )


def _build_rc(ctx: HarnessContext, worktree: Path, version: str, tag: str) -> StageResult | None:
    """In an existing worktree: move the version pin, commit, tag, push into the VM."""

    version_path = worktree / "gideon/__init__.py"
    rc_version = tag.removeprefix("v")
    try:
        rewritten_version = _replace_version_line(ctx.host.read_text(version_path), version, rc_version)
        if isinstance(rewritten_version, Problem):
            return StageResult("rehearse", False, rewritten_version.problem, rewritten_version.fix)
        ctx.host.write_text(version_path, rewritten_version, mode=0o644)
    except (OSError, UnicodeError) as exc:
        return StageResult("rehearse", False, f"could not rewrite the rehearsal worktree: {exc}", REHEARSAL_FIX)

    commit = _git(
        ctx.host,
        [
            "git", "-C", str(worktree),
            "-c", "user.name=gideon-acceptance",
            "-c", "user.email=acceptance@gideon.invalid",
            "commit", "-qam", f"acceptance: rehearse {tag}",
        ],
    )
    if commit.returncode != 0:
        return _git_failure("could not commit the rehearsal tag", commit)
    tagged = _git(ctx.host, ["git", "-C", str(worktree), "tag", tag])
    if tagged.returncode != 0:
        return _git_failure("could not tag the rehearsal worktree", tagged)
    if ctx.address is None:
        return StageResult("rehearse", False, "the VM has no address for the rehearsal push", REHEARSAL_FIX)
    # The tag reaches the VM's own mirror over SSH as the CSA account; the key
    # and known-hosts file are the run's, named in the environment, never argv.
    env = {
        "GIT_SSH_COMMAND": (
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={ctx.spec.run_dir / 'known_hosts'} "
            f"-i {ctx.spec.run_dir / 'id_ed25519'}"
        )
    }
    pushed = _git(
        ctx.host,
        ["git", "-C", str(worktree), "push", f"ssh://{vm.CSA_ACCOUNT}@{ctx.address}/srv/GIDEON.git", f"refs/tags/{tag}"],
        env=env,
    )
    if pushed.returncode != 0:
        return _git_failure("could not push the rehearsal tag", pushed)
    return None


def _make_tag(ctx: HarnessContext, version: str, tag: str) -> StageResult | None:
    """Make the throwaway rc tag in a worktree of the mirror; the worktree goes on every path.

    The checkout on the box is never touched: the worktree hangs off the run's
    root-owned mirror.
    """

    mirror = ctx.spec.run_dir / MIRROR_NAME
    worktree = ctx.spec.run_dir / WORKTREE_NAME
    add = _git(
        ctx.host,
        ["git", "-C", str(mirror), "worktree", "add", "--detach", str(worktree), ctx.spec.resolved_commit],
    )
    if add.returncode != 0:
        return _git_failure("could not create the rehearsal worktree", add)
    try:
        outcome = _build_rc(ctx, worktree, version, tag)
    finally:
        removed = _git(ctx.host, ["git", "-C", str(mirror), "worktree", "remove", "--force", str(worktree)])
    if outcome is None and removed.returncode != 0:
        return _git_failure("could not remove the rehearsal worktree", removed)
    return outcome


def rehearse(ctx: HarnessContext) -> StageResult:
    """Push a temporary rc tag and exercise upgrade and rollback four times."""

    parsed = base_version(ctx)
    if isinstance(parsed, Problem):
        return StageResult("rehearse", False, parsed.problem, parsed.fix)
    try:
        tag = rc_tag(parsed)
    except ValueError as exc:
        return StageResult("rehearse", False, str(exc), REHEARSAL_FIX)
    made = _make_tag(ctx, parsed, tag)
    if made is not None:
        return made
    ctx.base_version = parsed
    ctx.rc_tag = tag
    legs = (
        ("rehearse-1-upgrade.txt", ("./upgrade.sh", tag)),
        ("rehearse-2-rollback.txt", ("./upgrade.sh", "--rollback")),
        ("rehearse-3-upgrade.txt", ("./upgrade.sh", tag)),
        ("rehearse-4-rollback.txt", ("./upgrade.sh", "--rollback")),
    )
    paths = [ctx.spec.out / f"{ctx.stage_index:02d}-{name}" for name, _ in legs]
    for (transcript_name, command), path in zip(legs, paths, strict=True):
        result, text = vm.run_product(
            ctx,
            "rehearse",
            transcript_name,
            command,
            timeout=REHEARSAL_TIMEOUT_SECONDS,
        )
        if not result.ok:
            return StageResult("rehearse", False, result.detail, result.fix)
        if "refuse" in vm.row_outcomes(text).values():
            return StageResult(
                "rehearse",
                False,
                f"{transcript_name} contains a refusal row; transcript {path}",
                f"Inspect {path}, then retry acceptance.",
            )
    transcript_text = ", ".join(str(path) for path in paths)
    return StageResult(
        "rehearse",
        True,
        f"rehearsed {tag}; transcripts {transcript_text}",
        "",
    )


def restore(ctx: HarnessContext) -> StageResult:
    """Push, restore the newest target snapshot, and apply it in the VM."""

    commands = (
        ("restore-1-push.txt", ("python3", "-m", "gideon", "backup", "push")),
        ("restore-2-restore.txt", ("python3", "-m", "gideon", "restore", "--from", "target")),
        ("restore-3-apply.txt", ("python3", "-m", "gideon", "apply")),
    )
    paths = [ctx.spec.out / f"{ctx.stage_index:02d}-{name}" for name, _ in commands]
    for transcript_name, command in commands:
        result, _text = vm.run_product(
            ctx,
            "restore",
            transcript_name,
            command,
            timeout=REHEARSAL_TIMEOUT_SECONDS,
        )
        if not result.ok:
            return StageResult("restore", False, result.detail, result.fix)
    return StageResult(
        "restore",
        True,
        f"restored the newest target snapshot; transcripts {', '.join(str(path) for path in paths)}",
        "",
    )


def verify(ctx: HarnessContext) -> StageResult:
    """Re-run preflight and verify the release, sink, and every transcript."""

    result, _text = vm.run_product(
        ctx,
        "verify",
        "verify-preflight.txt",
        ("./preflight.sh",),
        timeout=VERIFY_PREFLIGHT_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return StageResult("verify", False, result.detail, result.fix)

    read_result, applied_text = vm.read_file(ctx, APPLIED_RECORD)
    if not read_result.ok or applied_text is None:
        return StageResult("verify", False, read_result.detail, VERIFY_FIX)
    try:
        applied = yaml.safe_load(applied_text)
    except yaml.YAMLError as exc:
        return StageResult("verify", False, f"could not parse {APPLIED_RECORD}: {exc}", VERIFY_FIX)
    if not isinstance(applied, dict) or not isinstance(applied.get("release"), str):
        return StageResult(
            "verify",
            False,
            f"{APPLIED_RECORD} has no release",
            VERIFY_FIX,
        )
    expected = ctx.base_version
    if expected is None:
        parsed = base_version(ctx)
        if isinstance(parsed, Problem):
            return StageResult("verify", False, parsed.problem, parsed.fix)
        expected = parsed
    actual = applied["release"]
    if actual != expected:
        return StageResult(
            "verify",
            False,
            f"applied release {actual} does not match base version {expected}",
            VERIFY_FIX,
        )
    # The tree itself, not only its record: the rollback must have left the
    # checkout at the release too.
    reported = vm.run_as_root(ctx, f"sh -c 'cd {vm.VM_CHECKOUT} && python3 -m gideon --version'")
    tree_version = reported.stdout.strip().removeprefix("gideon ").strip()
    if reported.returncode != 0 or tree_version != expected:
        return StageResult(
            "verify",
            False,
            f"the checkout reports {tree_version or command_detail(reported)!r}, not {expected}",
            VERIFY_FIX,
        )
    messages = len(authenticated_messages(ctx))
    if messages < 2:
        return StageResult(
            "verify",
            False,
            f"sink holds {messages} authenticated TLS message(s), need at least 2",
            VERIFY_FIX,
        )
    missing = [path for path in ctx.transcripts if not ctx.host.exists(path)]
    if missing:
        return StageResult(
            "verify",
            False,
            f"transcript is missing: {missing[0]}",
            VERIFY_FIX,
        )
    return StageResult(
        "verify",
        True,
        f"verified release {expected} (the applied record and the checkout's --version), "
        f"{messages} authenticated TLS message(s), and {len(ctx.transcripts)} transcripts",
        "",
    )

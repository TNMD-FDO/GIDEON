"""The SSH driver: boot, the product commands as ``sudo … 2>&1 | tee``, reboot.

Every product command runs inside the VM the way the box rehearsal showed
matters — no pty, a pipe, the terminal never the child's stdin — through
``bash -o pipefail -c``, so the stage carries the product's exit status and
never ``tee``'s. The pipeline's stdout is captured host-side and written under
``--out`` through :func:`redact` with the VM's site values masked.
"""

import re
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Final

from gideon.host.report import StageResult, command_detail
from gideon.host.sysio import Host, PathLike
from tools.acceptance import domain
from tools.acceptance.context import HARNESS_ROOT, HarnessContext
from tools.redact.core import redact

LEASE_TIMEOUT_SECONDS: Final = 300
CLOUD_INIT_TIMEOUT_SECONDS: Final = 900
PROVISION_TIMEOUT_SECONDS: Final = 1800
RECONNECT_TIMEOUT_SECONDS: Final = 300
POLL_INTERVAL_SECONDS: Final = 1
SSH_DISCONNECTED: Final = 255
# The seam's own timeout, distinct from ssh's 255: never retried as a disconnect.
SSH_TIMED_OUT: Final = 124
CSA_ACCOUNT: Final = "csa1"
VM_CHECKOUT: Final = "/opt/gideon"
VM_TRANSCRIPTS: Final = "~/acceptance"
# A reboot scheduled two seconds out: the SSH session returns instead of
# hanging until the connection drops.
REBOOT_SCRIPT: Final = "sudo systemd-run --quiet --on-active=2 systemctl reboot"
_ROW: Final = re.compile(r"^(?P<step>[A-Za-z0-9_-]+): (?P<outcome>[a-z-]+) — ")
VM_FIX: Final = "Inspect the acceptance VM and its console log, then retry acceptance."


def _attempts(timeout: float) -> int:
    return max(1, int(timeout / POLL_INTERVAL_SECONDS) + 1)


def ssh_argv(ctx: HarnessContext, script: str) -> list[str]:
    """The non-interactive, run-scoped SSH command running *script* in bash.

    ssh joins its remote words with spaces for the remote shell to re-parse,
    so the script travels as one quoted word (the backup target's
    ``remote_script`` does the same).
    """

    if ctx.address is None:
        raise ValueError("the VM has no address")
    known_hosts = ctx.spec.run_dir / "known_hosts"
    private_key = ctx.spec.run_dir / "id_ed25519"
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(private_key),
        f"{CSA_ACCOUNT}@{ctx.address}",
        "--",
        "bash",
        "-o",
        "pipefail",
        "-c",
        shlex.quote(script),
    ]


def _row_failure(stage: str, detail: str, fix: str = VM_FIX) -> StageResult:
    return StageResult(stage, False, detail, fix)


def _ssh(
    ctx: HarnessContext,
    script: str,
    *,
    input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return ctx.host.run(ssh_argv(ctx, script), input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess([], SSH_TIMED_OUT, "", f"timed out after {exc.timeout} s")
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([], SSH_DISCONNECTED, "", str(exc))


def wait_for_address(
    io: Host,
    name: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = LEASE_TIMEOUT_SECONDS,
    console_path: PathLike = "console.log",
) -> tuple[StageResult, str | None]:
    """Poll the libvirt lease, bounded by *timeout*; a miss names the console log."""

    attempts = _attempts(timeout)
    for attempt in range(attempts):
        address = domain.address(io, name)
        if address is not None:
            return StageResult("boot", True, f"VM address is {address}", ""), address
        if attempt + 1 < attempts:
            sleep(POLL_INTERVAL_SECONDS)
    return (
        _row_failure(
            "boot",
            f"VM did not acquire a lease; inspect {console_path}",
            f"Inspect {console_path}, then retry acceptance.",
        ),
        None,
    )


def wait_for_cloud_init(ctx: HarnessContext) -> StageResult:
    """Wait for cloud-init to finish; degraded (exit 2) is reported, not fatal.

    sshd answers before cloud-init is done, and may not answer at all for the
    first seconds after the lease, so a dropped connection is retried within
    the reconnect bound; the wait itself is one bounded call.
    """

    deadline = ctx.clock() + RECONNECT_TIMEOUT_SECONDS
    while True:
        result = _ssh(ctx, "cloud-init status --wait", timeout=CLOUD_INIT_TIMEOUT_SECONDS)
        if result.returncode == 0:
            return StageResult("boot", True, "cloud-init is done", "")
        if result.returncode == 2:
            return StageResult("boot", True, "cloud-init is degraded; continuing", "")
        if result.returncode == SSH_TIMED_OUT:
            return _row_failure("boot", f"cloud-init did not finish within {CLOUD_INIT_TIMEOUT_SECONDS} s")
        if result.returncode != SSH_DISCONNECTED:
            return _row_failure("boot", f"cloud-init did not finish: {command_detail(result)}")
        if ctx.clock() >= deadline:
            return _row_failure("boot", f"the VM did not answer over SSH within {RECONNECT_TIMEOUT_SECONDS} s")
        ctx.sleep(POLL_INTERVAL_SECONDS)


def run_product(
    ctx: HarnessContext,
    stage: str,
    transcript_name: str,
    command: Sequence[str],
    *,
    cwd: str = VM_CHECKOUT,
    timeout: float | None = None,
) -> tuple[StageResult, str]:
    """Run one product command in the VM and keep its transcript on both sides.

    Returns the row and the transcript text as written under ``--out``.
    """

    transcript = f"{VM_TRANSCRIPTS}/{transcript_name}"
    script = (
        f"cd {shlex.quote(cwd)} && sudo {shlex.join(command)} 2>&1 | tee {transcript}"
    )
    result = _ssh(ctx, script, timeout=timeout)
    text = redact(result.stdout, ctx.vm_site)
    host_transcript = ctx.spec.out / f"{ctx.stage_index:02d}-{transcript_name}"
    ctx.host.write_text(host_transcript, text, mode=0o644)
    ctx.transcripts.append(host_transcript)
    if result.returncode != 0:
        return (
            _row_failure(
                stage,
                f"{shlex.join(command)} exited {result.returncode}; transcript {host_transcript}",
                f"Inspect {host_transcript}, then retry acceptance.",
            ),
            text,
        )
    return StageResult(stage, True, f"{shlex.join(command)} passed; transcript {host_transcript}", ""), text


def run_as_root(
    ctx: HarnessContext, script: str, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """One non-interactive root command in the VM."""

    return _ssh(ctx, f"sudo {script}", timeout=timeout)


def copy_in(
    ctx: HarnessContext,
    path: PathLike,
    text: str,
    mode: int,
    *,
    as_root: bool = False,
    owner: str = "root:root",
    timeout: float | None = None,
) -> StageResult:
    """Write *text* into the VM at *path* through stdin — never argv.

    The root form keeps the secret's bytes on stdin while doing the mode and
    ownership change inside one privileged shell.  The ordinary form remains
    the CSA-owned home-file path used by the earlier stages.
    """

    target = shlex.quote(str(path))
    if as_root:
        inner = (
            f'umask 077 && cat > "$1" && chmod {mode:04o} "$1" && '
            f'chown {shlex.quote(owner)} "$1"'
        )
        script = f"sudo sh -c {shlex.quote(inner)} sh {target}"
    else:
        script = f"cat > {target} && chmod {mode:o} {target}"
    result = _ssh(ctx, script, input=text, timeout=timeout)
    if result.returncode != 0:
        return _row_failure("copy", f"could not copy {path}: {command_detail(result)}")
    return StageResult("copy", True, f"copied {path}", "")


def read_file(
    ctx: HarnessContext, path: PathLike, *, timeout: float | None = None
) -> tuple[StageResult, str | None]:
    """Read a small VM file back over SSH."""

    result = _ssh(ctx, f"cat {shlex.quote(str(path))}", timeout=timeout)
    if result.returncode != 0:
        return _row_failure("read", f"could not read {path}: {command_detail(result)}"), None
    return StageResult("read", True, f"read {path}", ""), result.stdout


def reboot_and_wait(ctx: HarnessContext) -> StageResult:
    """Reboot the VM, wait for it to go down and come back, then for cloud-init."""

    requested = _ssh(ctx, REBOOT_SCRIPT, timeout=30)
    if requested.returncode not in (0, SSH_DISCONNECTED):
        return _row_failure("reboot", f"reboot request failed: {command_detail(requested)}")
    down = False
    deadline = ctx.clock() + RECONNECT_TIMEOUT_SECONDS
    while True:
        probe = _ssh(ctx, "true", timeout=30)
        if probe.returncode != 0:
            # Refused, dropped, or timed out: the VM is (still) going down.
            down = True
        elif down:
            cloud = wait_for_cloud_init(ctx)
            if not cloud.ok:
                return cloud
            return StageResult("reboot", True, "rebooted; cloud-init is ready", "")
        if ctx.clock() >= deadline:
            break
        ctx.sleep(POLL_INTERVAL_SECONDS)
    console = ctx.spec.out / "console.log"
    return _row_failure(
        "reboot",
        f"VM did not come back after the reboot; inspect {console}",
        f"Inspect {console}, then retry acceptance.",
    )


def boot(ctx: HarnessContext) -> StageResult:
    """Define and start the VM, wait for its lease, then for cloud-init."""

    ctx.run_dir_created = True
    defined = domain.define(
        ctx.host,
        name=ctx.spec.vm_name,
        run_dir=ctx.spec.run_dir,
        harness_root=HARNESS_ROOT,
        seed_path=ctx.spec.run_dir / "seed.iso",
        out=ctx.spec.out,
        run_id=ctx.run_id,
        template_path=ctx.template_path,
    )
    if not defined.ok:
        return StageResult("boot", False, defined.detail, defined.fix)
    ctx.domain_defined = True
    started = domain.start(ctx.host, ctx.spec.vm_name)
    if not started.ok:
        return StageResult("boot", False, started.detail, started.fix)
    lease, address = wait_for_address(
        ctx.host,
        ctx.spec.vm_name,
        sleep=ctx.sleep,
        console_path=ctx.spec.out / "console.log",
    )
    if not lease.ok or address is None:
        return lease
    ctx.address = address
    cloud = wait_for_cloud_init(ctx)
    if not cloud.ok:
        return cloud
    return StageResult("boot", True, f"VM booted at {address}; cloud-init is ready", "")


def clone(ctx: HarnessContext) -> StageResult:
    """Clone the in-VM mirror to the checkout as the owning CSA account."""

    if not ctx.spec.clone_ref:
        return _row_failure("clone", "the image stage did not select a clone ref")
    created = run_as_root(
        ctx,
        f"install -d -o {CSA_ACCOUNT} -g {CSA_ACCOUNT} {VM_CHECKOUT} /home/{CSA_ACCOUNT}/acceptance",
    )
    if created.returncode != 0:
        return _row_failure("clone", f"could not create the VM checkout: {command_detail(created)}")
    cloned = _ssh(
        ctx,
        f"git clone --branch {shlex.quote(ctx.spec.clone_ref)} /srv/GIDEON.git {VM_CHECKOUT}",
        timeout=300,
    )
    if cloned.returncode != 0:
        return _row_failure("clone", f"could not clone the VM checkout: {command_detail(cloned)}")
    return StageResult("clone", True, f"cloned {ctx.spec.clone_ref} into {VM_CHECKOUT}", "")


def row_outcomes(text: str) -> dict[str, str]:
    """The name → outcome map of a transcript's rows (provision steps, command stages)."""

    outcomes: dict[str, str] = {}
    for line in text.splitlines():
        match = _ROW.match(line)
        if match is not None:
            outcomes[match.group("step")] = match.group("outcome")
    return outcomes


def provision(ctx: HarnessContext, *, allow_blocked: bool) -> StageResult:
    """``host provision --no-gpu`` in the VM, honouring ``reboot-required`` at most twice.

    Before the site file the site-dependent steps are ``blocked`` by design
    (the runbook's second provision run converges them); after it none may be.
    """

    for attempt in range(3):
        suffix = "" if attempt == 0 else f"-{attempt + 1}"
        result, text = run_product(
            ctx,
            "provision",
            f"provision{suffix}.txt",
            ["python3", "-m", "gideon", "host", "provision", "--no-gpu"],
            timeout=PROVISION_TIMEOUT_SECONDS,
        )
        if not result.ok:
            return result
        outcomes = row_outcomes(text)
        blocked = sorted(step for step, outcome in outcomes.items() if outcome == "blocked")
        if blocked and not allow_blocked:
            return _row_failure(
                "provision",
                f"provision left {', '.join(blocked)} blocked after the site file",
                f"{result.fix or 'Inspect the transcript, then retry acceptance.'}",
            )
        if "reboot-required" not in outcomes.values():
            return result
        if attempt == 2:
            return _row_failure("provision", "provision still requires a reboot after two reboots")
        rebooted = reboot_and_wait(ctx)
        if not rebooted.ok:
            return StageResult("provision", False, rebooted.detail, rebooted.fix)
    return _row_failure("provision", "provision retry limit reached")


def reboot(ctx: HarnessContext) -> StageResult:
    """The unconditional reboot after the first provision: the fresh boot proves
    ``wait-online`` and the LVM fstab hold."""

    return reboot_and_wait(ctx)

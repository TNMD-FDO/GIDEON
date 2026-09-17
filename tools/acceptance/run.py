"""The ordered acceptance stages and their box-side runner."""

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from gideon.host import install, site, tls
from gideon.host.lock import load_host_lock
from gideon.host.lock import render_errors as render_lock_errors
from gideon.host.report import StageResult, print_stage, stage_line
from gideon.host.stages import site_problem
from gideon.host.steps.services import acceptance_image_path
from gideon.host.sysio import Host
from tools.acceptance import domain, fullrestore, image, rehearsal, seed, services, vm
from tools.acceptance.context import (
    ACCEPTANCE_REGISTRY_AUTHORITY,
    HARNESS_ROOT,
    HarnessContext,
    RunSpec,
    SinkFactory,
    Stage,
    authenticated_messages,
    checkout_git,
)
from tools.ownership import restore_ownership, sudo_ids
from tools.pinwatch.fetch import Fetcher, FetchError

_TOOLS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("virsh", ("virsh", "--version")),
    ("qemu-img", ("qemu-img", "--version")),
    ("guestfish", ("guestfish", "--version")),
    ("virt-customize", ("virt-customize", "--version")),
    ("cloud-localds", ("cloud-localds", "--version")),
    ("openssl", ("openssl", "version")),
    ("ssh", ("ssh", "-V")),
    ("git", ("git", "--version")),
)
_ROOT_FIX = "Run sudo python3 -m tools.acceptance <ref>, then retry."
_KVM_FIX = "Run sudo python3 -m gideon host provision --only kvm, then retry acceptance."
_IMAGE_FIX = _KVM_FIX
_NETWORK_FIX = "Start and autostart libvirt's default network, then retry acceptance."
_REGISTRY_FIX = "Start the provisioned registry on the libvirt bridge, then retry acceptance."
_SITE_FIX = "Correct /etc/gideon/site.yaml, then retry acceptance."
_REF_FIX = "Correct the checkout ref, then retry python3 -m tools.acceptance <ref>."
_TEARDOWN_FIX = "Remove only the harness-owned VM, then retry acceptance."
_SINK_WAIT_SECONDS: Final = 60
_SINK_POLL_SECONDS: Final = 1
_PREFLIGHT_TIMEOUT_SECONDS: Final = 900
_INSTALL_TIMEOUT_SECONDS: Final = 3600
# alerts test waits for Grafana's readiness itself before sending.
_ALERTS_TIMEOUT_SECONDS: Final = 300
_PROBE_FIX = "Inspect openssl s_client and the VM's console.log, then retry acceptance."


def _stage_refusal(detail: str, fix: str) -> StageResult:
    return StageResult("preconditions", False, detail, fix)


def _sha256_matches(io: Host, path: Path, expected: str) -> bool:
    result = io.run(["sha256sum", str(path)])
    if result.returncode != 0:
        return False
    fields = result.stdout.split()
    return bool(fields) and fields[0].casefold() == expected.casefold()


def _network_active(text: str) -> bool:
    return any(
        key.strip() == "Active" and value.strip().casefold() == "yes"
        for line in text.splitlines()
        for key, separator, value in (line.partition(":"),)
        if separator
    )


def _resolve_ref(ctx: HarnessContext) -> StageResult | None:
    result = ctx.host.run(
        checkout_git(ctx.checkout, "rev-parse", "--verify", f"{ctx.spec.ref}^{{commit}}"),
        cwd=ctx.checkout,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return _stage_refusal(
            f"ref does not resolve in the checkout: {ctx.spec.ref}", _REF_FIX
        )
    ctx.spec = replace(ctx.spec, resolved_commit=result.stdout.strip())
    ctx.run_id = f"{ctx.spec.vm_name}-{ctx.spec.resolved_commit[:12]}"
    return None


def preconditions(ctx: HarnessContext) -> StageResult:
    """Check root, host tools, pins, networking, site, ref, and old VMs."""

    if ctx.host.geteuid() != 0:
        return _stage_refusal("root is required", _ROOT_FIX)
    ctx.host.mkdir(ctx.spec.out, mode=0o755, parents=True, exist_ok=True)

    try:
        lock_result = load_host_lock(ctx.checkout / "host.lock", host=ctx.host)
    except (OSError, UnicodeError) as exc:
        return _stage_refusal(f"host.lock could not be loaded: {exc}", _KVM_FIX)
    if lock_result.errors or lock_result.lock is None:
        return _stage_refusal(render_lock_errors(lock_result.errors), _KVM_FIX)
    ctx.lock = lock_result.lock

    # Only the seam's 127 means absent: a tool that answers its version probe
    # with any other status is installed.
    for name, argv in _TOOLS:
        try:
            result = ctx.host.run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return _stage_refusal(f"required tool is unavailable: {name} ({exc})", _KVM_FIX)
        if result.returncode == 127:
            return _stage_refusal(f"required tool is not installed: {name}", _KVM_FIX)

    image = acceptance_image_path(ctx.lock)
    if not ctx.host.exists(image):
        return _stage_refusal(f"pinned acceptance image is missing: {image}", _IMAGE_FIX)
    if not _sha256_matches(ctx.host, image, ctx.lock.acceptance_vm_image.sha256):
        return _stage_refusal(
            f"pinned acceptance image checksum mismatch: {image}", _IMAGE_FIX
        )

    network = ctx.host.run(["virsh", "net-info", "default"])
    if network.returncode != 0 or not _network_active(network.stdout):
        return _stage_refusal("libvirt default network is not active", _NETWORK_FIX)

    try:
        response = ctx.fetcher.get(
            f"http://{ACCEPTANCE_REGISTRY_AUTHORITY}/v2/"
        )
    except FetchError as exc:
        return _stage_refusal(f"acceptance registry is unreachable: {exc}", _REGISTRY_FIX)
    del response  # Any HTTP response, including an error status, proves liveness.

    loaded = site.load_site(ctx.site_path, host=ctx.host)
    if loaded.errors or loaded.config is None:
        return _stage_refusal(site_problem(loaded) or "site file is invalid", _SITE_FIX)
    ctx.site = loaded.config

    unresolved = _resolve_ref(ctx)
    if unresolved is not None:
        return unresolved

    stale_rule = services.sweep_ufw(ctx)
    if stale_rule is not None:
        return stale_rule

    existing = domain.sweep_leftover(
        ctx.host,
        name=ctx.spec.vm_name,
        harness_root=HARNESS_ROOT,
        run_dir=ctx.spec.run_dir,
    )
    if not existing.ok:
        return existing
    return StageResult("preconditions", True, "acceptance preconditions passed", "")


def _allocated_size(io: Host, run_dir: Path) -> str:
    # Allocated blocks, never the apparent size: the data disk is sparse.
    # The figure is a record, so a failed read never fails the teardown.
    try:
        result = io.run(["du", "-s", "-B1", str(run_dir)])
    except (OSError, subprocess.SubprocessError):
        return "run directory size unknown"
    fields = result.stdout.split()
    if result.returncode != 0 or not fields or not fields[0].isdigit():
        return "run directory size unknown"
    return f"run directory allocated {fields[0]} bytes"


def _teardown(ctx: HarnessContext, *, remove_vm: bool = True) -> StageResult:
    """Stop run services and, unless kept, remove only the owned VM/files."""

    failures: list[StageResult] = []
    details: list[str] = []
    had_sink = ctx.sink is not None
    had_rule = ctx.ufw_rule is not None
    stopped = services.stop_sink(ctx)
    if stopped is not None:
        failures.append(stopped)
    elif had_sink:
        details.append("SMTP sink stopped")
    removed_rule = services.remove_rule(ctx)
    if removed_rule is not None:
        failures.append(removed_rule)
    elif had_rule:
        details.append("acceptance UFW rule removed")

    if remove_vm:
        if ctx.run_dir_created or ctx.domain_defined:
            details.append(_allocated_size(ctx.host, ctx.spec.run_dir))
            vm_result = domain.teardown(
                ctx.host,
                name=ctx.spec.vm_name,
                run_dir=ctx.spec.run_dir,
                harness_root=HARNESS_ROOT,
                defined=ctx.domain_defined,
            )
        else:
            vm_result = StageResult("teardown", True, "nothing to tear down", "")
        if not vm_result.ok:
            failures.append(vm_result)
        else:
            details.insert(0, vm_result.detail)
    if failures:
        first = failures[0]
        return StageResult("teardown", False, first.detail, first.fix)
    return StageResult("teardown", True, "; ".join(details), "")


def _services_stage(ctx: HarnessContext) -> StageResult:
    return services.prepare(ctx)


def _first_provision(ctx: HarnessContext) -> StageResult:
    # Before the site file the site-dependent steps are blocked by design.
    return vm.provision(ctx, allow_blocked=True)


def _second_provision(ctx: HarnessContext) -> StageResult:
    return vm.provision(ctx, allow_blocked=False)


def _site_stage(ctx: HarnessContext) -> StageResult:
    return services.install_site(ctx)


def _authorize_stage(ctx: HarnessContext) -> StageResult:
    return services.authorize(ctx)


def _wait_for_messages(
    ctx: HarnessContext,
    before: int,
) -> tuple[bool, int]:
    attempts = max(1, int(_SINK_WAIT_SECONDS / _SINK_POLL_SECONDS) + 1)
    for attempt in range(attempts):
        count = len(authenticated_messages(ctx))
        if count > before:
            return True, count
        if attempt + 1 < attempts:
            ctx.sleep(_SINK_POLL_SECONDS)
    return False, len(authenticated_messages(ctx))


def _preflight_stage(ctx: HarnessContext) -> StageResult:
    # The second run finds the first run's message in the sink already.
    before = len(authenticated_messages(ctx))
    transcript = ctx.spec.out / f"{ctx.stage_index:02d}-preflight.txt"
    result, _text = vm.run_product(
        ctx,
        "preflight",
        "preflight.txt",
        ["./preflight.sh"],
        timeout=_PREFLIGHT_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return result
    found, authenticated = _wait_for_messages(ctx, before)
    if not found:
        return StageResult(
            "preflight",
            False,
            f"preflight produced no authenticated TLS SMTP message; transcript {transcript}",
            f"Inspect {transcript}, then retry acceptance.",
        )
    return StageResult(
        "preflight",
        True,
        f"preflight passed; sink holds {authenticated} authenticated TLS message(s)",
        "",
    )


def _install_stage(ctx: HarnessContext) -> StageResult:
    transcript = ctx.spec.out / f"{ctx.stage_index:02d}-install.txt"
    result, text = vm.run_product(
        ctx,
        "install",
        "install.txt",
        ["./install.sh"],
        timeout=_INSTALL_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return result
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].startswith(("http://", "https://")):
        return StageResult(
            "install",
            False,
            f"install did not finish with a URL; transcript {transcript}",
            f"Inspect {transcript}, then retry acceptance.",
        )
    # The VM is a no-GPU host (ADR-0035), so the only row a correct install prints
    # for the gate is the skip; the old inert row or any other detail is red here.
    expected_engine_verify = stage_line(
        StageResult("engine-verify", True, install.ENGINE_VERIFY_SKIPPED_DETAIL, "")
    )
    if expected_engine_verify not in lines:
        return StageResult(
            "install",
            False,
            f"install transcript lacks the required engine-verify row; transcript {transcript}",
            f"Inspect {transcript}, then retry acceptance.",
        )
    ctx.install_url = lines[-1]
    return StageResult(
        "install",
        True,
        f"installed at {ctx.install_url}; {install.ENGINE_VERIFY_SKIPPED_DETAIL}",
        "",
    )


def _alerts_stage(ctx: HarnessContext) -> StageResult:
    before = len(authenticated_messages(ctx))
    transcript = ctx.spec.out / f"{ctx.stage_index:02d}-alerts.txt"
    result, _text = vm.run_product(
        ctx,
        "alerts",
        "alerts.txt",
        ["python3", "-m", "gideon", "alerts", "test"],
        timeout=_ALERTS_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return result
    found, count = _wait_for_messages(ctx, before)
    if not found:
        return StageResult(
            "alerts",
            False,
            f"sink did not receive a new authenticated TLS alert; transcript {transcript}",
            f"Inspect {transcript} and the sink, then retry acceptance.",
        )
    return StageResult("alerts", True, f"alerts test delivered; sink holds {count} authenticated TLS message(s)", "")


def _probe_stage(ctx: HarnessContext) -> StageResult:
    material = ctx.services
    if material is None or ctx.address is None:
        return StageResult("probe", False, "probe inputs were not prepared", vm.VM_FIX)
    hostname = seed.hostname_for(ctx.spec.vm_name)
    served = tls.served_fingerprint(
        ctx.host,
        connect=f"{ctx.address}:443",
        hostname=hostname,
        cafile=ctx.spec.run_dir / "ca" / "bundle.pem",
    )
    if isinstance(served, tls.Problem):
        return StageResult("probe", False, served.problem, served.fix)
    placed = tls.file_fingerprint(ctx.host, ctx.spec.run_dir / "ca" / "vm.pem")
    if isinstance(placed, tls.Problem):
        return StageResult("probe", False, placed.problem, placed.fix)
    if served != placed:
        return StageResult(
            "probe",
            False,
            f"served certificate fingerprint {served} does not match VM leaf {placed}",
            _PROBE_FIX,
        )
    return StageResult("probe", True, "VM serves the acceptance leaf certificate", "")


STAGES: Final[tuple[Stage, ...]] = (
    Stage("preconditions", preconditions, _KVM_FIX),
    Stage("image", image.build, image.IMAGE_FIX),
    Stage("seed", seed.build, seed.SEED_FIX),
    Stage("services", _services_stage, services.SERVICE_FIX),
    Stage("boot", vm.boot, vm.VM_FIX),
    Stage("clone", vm.clone, vm.VM_FIX),
    Stage("provision", _first_provision, vm.VM_FIX),
    Stage("reboot", vm.reboot, vm.VM_FIX),
    Stage("site", _site_stage, services.SERVICE_FIX),
    Stage("provision", _second_provision, vm.VM_FIX, key="provision-2"),
    Stage("authorize", _authorize_stage, services.SERVICE_FIX),
    Stage("preflight", _preflight_stage, vm.VM_FIX),
    Stage("install", _install_stage, vm.VM_FIX),
    Stage("alerts", _alerts_stage, vm.VM_FIX),
    Stage("probe", _probe_stage, vm.VM_FIX),
    Stage("rehearse", rehearsal.rehearse, rehearsal.REHEARSAL_FIX),
    Stage("restore", rehearsal.restore, rehearsal.RESTORE_FIX),
    Stage("verify", rehearsal.verify, rehearsal.VERIFY_FIX),
    Stage("teardown", _teardown, _TEARDOWN_FIX),
)
STAGE_IDENTIFIERS: Final[tuple[str, ...]] = tuple(stage.identifier for stage in STAGES)
# The receiving-office stages the full restore shares, image through preflight.
_SHARED: Final = STAGES[
    STAGE_IDENTIFIERS.index("image") : STAGE_IDENTIFIERS.index("preflight") + 1
]
FULL_RESTORE_STAGES: Final[tuple[Stage, ...]] = (
    STAGES[0],
    # Second, so a set the box cannot open boots nothing.
    Stage("set", fullrestore.select_set, fullrestore.SET_FIX),
    *_SHARED,
    Stage("apply", fullrestore.apply_fresh, fullrestore.FULL_RESTORE_FIX),
    Stage("snapshot", fullrestore.snapshot, fullrestore.FULL_RESTORE_FIX),
    Stage("stop", fullrestore.stop_stack, fullrestore.FULL_RESTORE_FIX),
    Stage("restore", fullrestore.restore_target, fullrestore.FULL_RESTORE_FIX),
    Stage("counts", fullrestore.counts, fullrestore.FULL_RESTORE_FIX),
    Stage("decrypt", fullrestore.decrypt, fullrestore.FULL_RESTORE_FIX),
    Stage("reinstall", fullrestore.reinstall, fullrestore.FULL_RESTORE_FIX),
    # restore re-owns by the box's numeric ids, which a rebuilt host's gideon
    # need not share; provision re-owns the managed /data directories (the
    # fix preflight names), as the runbook's §5 does after the restore.
    Stage("provision", _second_provision, vm.VM_FIX, key="provision-3"),
    Stage("apply", fullrestore.apply_again, fullrestore.FULL_RESTORE_FIX, key="apply-2"),
    Stage("health", fullrestore.health, fullrestore.FULL_RESTORE_FIX),
    Stage("preflight", _preflight_stage, vm.VM_FIX, key="preflight-2"),
    Stage("mode", fullrestore.mode, fullrestore.FULL_RESTORE_FIX),
    Stage("users", fullrestore.users, fullrestore.FULL_RESTORE_FIX),
    Stage("backup", fullrestore.backup, fullrestore.FULL_RESTORE_FIX),
    Stage("audit", fullrestore.audit, fullrestore.FULL_RESTORE_FIX),
    Stage("drill", fullrestore.drill, fullrestore.FULL_RESTORE_FIX),
    STAGES[-1],
)
FULL_RESTORE_IDENTIFIERS: Final[tuple[str, ...]] = tuple(
    stage.identifier for stage in FULL_RESTORE_STAGES
)


def run(
    spec: RunSpec,
    *,
    host: Host,
    fetcher: Fetcher,
    checkout: Path,
    site_path: Path,
    template_path: Path,
    sleep: Callable[[float], None] = time.sleep,
    sink_factory: SinkFactory | None = None,
    stages: Sequence[Stage] = STAGES,
) -> int:
    """Run stages through the requested point, always cleaning up if allowed."""

    ctx = HarnessContext(
        host,
        fetcher,
        checkout,
        site_path,
        template_path,
        spec,
        sleep=sleep,
        sink_factory=sink_factory,
    )
    successful = True
    started = time.monotonic()
    until = spec.until
    try:
        for index, stage in enumerate(stages, start=1):
            if stage.identifier == "teardown":
                break
            ctx.stage_index = index
            try:
                result = stage.callable(ctx)
            except Exception as exc:  # noqa: BLE001  # the command boundary: a traceback is a bug
                result = StageResult(
                    stage.name, False, f"internal error: {type(exc).__name__}: {exc}", stage.fix
                )
            print_stage(result)
            if not result.ok:
                successful = False
                break
            if until == stage.identifier:
                break
    finally:
        if spec.keep:
            cleanup = _teardown(ctx, remove_vm=False)
            if not cleanup.ok:
                print_stage(cleanup)
                successful = False
            if ctx.address is not None:
                print(f"VM {spec.vm_name} kept at {ctx.address}")
        else:
            teardown_result = _teardown(ctx)
            print_stage(teardown_result)
            successful = successful and teardown_result.ok
        print(f"elapsed: {round(time.monotonic() - started)} s")
        # The transcripts belong to whoever ran sudo, as the build tool's
        # record does; run directly by root there is nothing to restore. Only
        # the harness's own tree is re-owned — never recursively, so an --out
        # naming a directory the harness did not fill cannot be handed over.
        # The bytecode caches root left under the checkout go back the same way.
        owner = sudo_ids()
        if owner is not None:
            restore_ownership(host, spec.out, owner, checkout=checkout)
    return int(not successful)

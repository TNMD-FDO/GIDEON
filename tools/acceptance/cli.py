"""CLI surface for the clean-VM acceptance harness."""

import argparse
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from gideon.host.sysio import Host, PathLike, RealHost
from tools.acceptance import smtpsink
from tools.acceptance.context import (
    DEFAULT_COTENANT_VM_NAMES,
    DEFAULT_RESTORE_VM_NAME,
    DEFAULT_SITE_PATH,
    DEFAULT_VM_NAME,
    HARNESS_ROOT,
    RunSpec,
    SinkFactory,
)
from tools.acceptance.run import (
    COTENANT_AFTER_IDENTIFIERS,
    COTENANT_BEFORE_IDENTIFIERS,
    FULL_RESTORE_IDENTIFIERS,
    STAGE_IDENTIFIERS,
    form_stages,
    run,
)
from tools.pinwatch.fetch import Fetcher, UrllibFetcher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.acceptance",
        description="Build and drive the GIDEON clean-VM acceptance run.",
    )
    parser.add_argument("ref")
    form = parser.add_mutually_exclusive_group()
    form.add_argument(
        "--full-restore",
        action="store_true",
        help="restore the box's newest set into a clean VM",
    )
    form.add_argument(
        "--cotenant",
        choices=("before", "after"),
        help="run with a synthetic co-tenant arriving before or after GIDEON",
    )
    parser.add_argument("--name", default=None, metavar="VM")
    parser.add_argument("--out", metavar="DIR")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--until",
        choices=tuple(
            dict.fromkeys(
                (
                    *STAGE_IDENTIFIERS,
                    *FULL_RESTORE_IDENTIFIERS,
                    *COTENANT_BEFORE_IDENTIFIERS,
                    *COTENANT_AFTER_IDENTIFIERS,
                )
            )
        ),
        help="stop after this stage (use provision-2 for the second provision)",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _safe_ref(ref: str) -> str:
    """Make a ref suitable for the default output directory."""

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", ref).strip("-.")
    return safe or "ref"


def default_out(ref: str, name: str) -> Path:
    """./acceptance-out/<ref>/, with the VM name appended for a non-default run."""

    leaf = _safe_ref(ref) if name == DEFAULT_VM_NAME else f"{_safe_ref(ref)}-{_safe_ref(name)}"
    return Path.cwd() / "acceptance-out" / leaf


def _plan(spec: RunSpec) -> None:
    stages = form_stages(spec)
    identifiers = ", ".join(stage.identifier for stage in stages)
    if spec.full_restore:
        form = " (full restore)"
    elif spec.cotenant:
        form = f" (co-tenant {spec.cotenant})"
    else:
        form = ""
    print(f"Acceptance dry run{form}:")
    print(f"stages: {identifiers}")
    print(f"run directory: {spec.run_dir}")
    print(f"output directory: {spec.out}")
    print(
        "VM: 8 vCPUs, 16 GiB RAM, machine pc, "
        "/usr/bin/qemu-system-x86_64, default libvirt network"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    fetcher: Fetcher | None = None,
    root: Path | None = None,
    site_path: PathLike = DEFAULT_SITE_PATH,
    sleep: Callable[[float], None] = time.sleep,
    sink_factory: SinkFactory | None = None,
) -> int:
    """Parse and run the acceptance harness over injectable box seams."""

    options = _parser().parse_args(argv)
    if options.full_restore:
        selected_identifiers = FULL_RESTORE_IDENTIFIERS
    elif options.cotenant == "before":
        selected_identifiers = COTENANT_BEFORE_IDENTIFIERS
    elif options.cotenant == "after":
        selected_identifiers = COTENANT_AFTER_IDENTIFIERS
    else:
        selected_identifiers = STAGE_IDENTIFIERS
    if options.until is not None and options.until not in selected_identifiers:
        if options.until in STAGE_IDENTIFIERS:
            other_flag = "omit --full-restore" if options.full_restore else "omit --cotenant"
        elif options.until in FULL_RESTORE_IDENTIFIERS:
            other_flag = "--full-restore"
        elif options.until in COTENANT_BEFORE_IDENTIFIERS:
            other_flag = "--cotenant before"
        else:
            other_flag = "--cotenant after"
        print(
            f"tools.acceptance: --until {options.until} is not valid for this form. "
            f"Fix: {other_flag} to use that stage.",
            file=sys.stderr,
        )
        return 1
    checkout = root or Path(__file__).resolve().parents[2]
    io = host or RealHost()
    if options.name is not None:
        vm_name = options.name
    elif options.full_restore:
        vm_name = DEFAULT_RESTORE_VM_NAME
    elif options.cotenant:
        vm_name = DEFAULT_COTENANT_VM_NAMES[options.cotenant]
    else:
        vm_name = DEFAULT_VM_NAME
    # Absolute whatever the caller typed: libvirt opens the console log by path
    # from its own working directory, and the workflow names --out relative to
    # the runner's workspace (the v0.1.0 tag run refused at boot on it).
    output = (
        Path(options.out).resolve()
        if options.out is not None
        else default_out(options.ref, vm_name)
    )
    # --out is the harness's own directory: its ownership is handed to the sudo
    # caller at the end, so it must be new or empty, never a directory that
    # already holds someone's files.
    if io.exists(output):
        try:
            occupied = bool(io.listdir(output))
        except OSError as exc:
            print(
                f"tools.acceptance: --out {output} is not a usable directory ({exc}). "
                "Fix: name a new or empty directory for the run's transcripts.",
                file=sys.stderr,
            )
            return 1
        if occupied:
            print(
                f"tools.acceptance: --out {output} exists and is not empty. "
                "Fix: name a new or empty directory for the run's transcripts.",
                file=sys.stderr,
            )
            return 1
    spec = RunSpec(
        ref=options.ref,
        resolved_commit="",
        vm_name=vm_name,
        run_dir=HARNESS_ROOT / vm_name,
        out=output,
        keep=options.keep,
        until=options.until,
        full_restore=options.full_restore,
        cotenant=options.cotenant or "",
    )
    if options.dry_run:
        _plan(spec)
        return 0
    chosen_sink_factory = cast(SinkFactory, sink_factory or smtpsink.SmtpSink)
    return run(
        spec,
        host=io,
        fetcher=fetcher or UrllibFetcher(),
        checkout=checkout,
        site_path=Path(site_path),
        template_path=checkout / "tools/acceptance/domain.xml.tmpl",
        sleep=sleep,
        sink_factory=chosen_sink_factory,
        stages=form_stages(spec),
    )

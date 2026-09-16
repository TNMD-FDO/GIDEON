"""Libvirt domain definition and ownership-guarded lifecycle for acceptance VMs."""

import hashlib
import ipaddress
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from string import Template
from typing import Final
from xml.sax.saxutils import escape

from gideon.host.report import StageResult, command_detail
from gideon.host.sysio import Host, PathLike

DOMAIN_TEMPLATE: Final = Path(__file__).with_name("domain.xml.tmpl")
HARNESS_NAMESPACE: Final = "https://gideon.invalid/acceptance"
_GIDEON_RUN = f"{{{HARNESS_NAMESPACE}}}run"
_IPV4 = re.compile(r"(?<![0-9])(?P<address>[0-9]{1,3}(?:\.[0-9]{1,3}){3})/[0-9]{1,2}(?![0-9])")
_DOMAIN_FIX = "Remove the foreign VM or use a different acceptance --name, then retry."
_COMMAND_FIX = "Repair libvirt and retry the acceptance run."


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return path.resolve() != parent.resolve()


def disk_wwns(run_id: str) -> tuple[str, str]:
    """Return two deterministic NAA-64 WWNs derived from *run_id*."""

    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"0x5{digest[:15]}", f"0x5{digest[15:30]}"


def domain_xml(
    io: Host,
    *,
    template_path: PathLike = DOMAIN_TEMPLATE,
    name: str,
    os_path: PathLike,
    data_path: PathLike,
    seed_path: PathLike,
    console_path: PathLike,
    run_id: str,
    network: str = "default",
    vcpus: int = 8,
    memory_mib: int = 16 * 1024,
    machine: str = "pc",
    emulator: str = "/usr/bin/qemu-system-x86_64",
) -> str:
    """Render the checked-in libvirt template using files read through *io*."""

    template = Template(io.read_text(template_path))
    os_wwn, data_wwn = disk_wwns(run_id)
    values = {
        "name": escape(name),
        "memory_mib": str(memory_mib),
        "vcpus": str(vcpus),
        "machine": escape(machine),
        "emulator": escape(emulator),
        "os_path": escape(os.fspath(os_path)),
        "data_path": escape(os.fspath(data_path)),
        "seed_path": escape(os.fspath(seed_path)),
        "console_path": escape(os.fspath(console_path)),
        "network": escape(network),
        "os_wwn": os_wwn,
        "data_wwn": data_wwn,
        "run_id": escape(run_id),
    }
    return template.substitute(values)


def _result(name: str, result: subprocess.CompletedProcess[str], detail: str) -> StageResult:
    if result.returncode == 0:
        return StageResult(name, True, detail, "")
    return StageResult(name, False, f"{detail}: {command_detail(result)}", _COMMAND_FIX)


def _domain_xml(io: Host, name: str) -> tuple[StageResult | None, str | None]:
    result = io.run(["virsh", "dumpxml", name])
    if result.returncode != 0:
        return (
            StageResult(
                "preconditions",
                False,
                f"could not inspect existing VM {name}: {command_detail(result)}",
                _DOMAIN_FIX,
            ),
            None,
        )
    return None, result.stdout


def _owned(xml: str, harness_root: Path) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    run = root.find(f".//{_GIDEON_RUN}")
    if run is None or not run.get("id"):
        return False
    for source in root.findall(".//disk/source"):
        file_path = source.get("file")
        if file_path is None or not _under(Path(file_path), harness_root):
            return False
    return True


def _remove_run_dir(io: Host, run_dir: Path, harness_root: Path) -> StageResult:
    if not _under(run_dir, harness_root):
        return StageResult(
            "teardown",
            False,
            f"refused unsafe acceptance run directory: {run_dir}",
            _DOMAIN_FIX,
        )
    result = io.run(["rm", "-rf", str(run_dir)])
    return _result("teardown", result, f"removed acceptance run directory {run_dir}")


def _existing(io: Host, name: str) -> tuple[StageResult | None, str | None]:
    result = io.run(["virsh", "dominfo", name])
    if result.returncode == 1:
        return None, None
    if result.returncode != 0:
        return (
            StageResult(
                "preconditions",
                False,
                f"could not inspect VM {name}: {command_detail(result)}",
                _DOMAIN_FIX,
            ),
            None,
        )
    return _domain_xml(io, name)


def sweep_leftover(
    io: Host,
    *,
    name: str,
    harness_root: PathLike,
    run_dir: PathLike,
) -> StageResult:
    """Destroy only an owned leftover whose disks are inside *harness_root*."""

    root = Path(harness_root)
    current = Path(run_dir)
    failure, xml = _existing(io, name)
    if failure is not None:
        return failure
    if xml is None:
        return StageResult("preconditions", True, f"no existing VM named {name}", "")
    if not _owned(xml, root):
        return StageResult(
            "preconditions",
            False,
            f"existing VM {name} is not an acceptance VM owned by this harness",
            _DOMAIN_FIX,
        )
    destroyed = destroy(io, name)
    if not destroyed.ok:
        return StageResult("preconditions", False, destroyed.detail, destroyed.fix)
    undefined = _result(
        "preconditions",
        io.run(["virsh", "undefine", name, "--remove-all-storage"]),
        f"undefined leftover acceptance VM {name}",
    )
    if not undefined.ok:
        return undefined
    return _remove_run_dir(io, current, root)


def define(
    io: Host,
    *,
    name: str,
    run_dir: PathLike,
    harness_root: PathLike,
    seed_path: PathLike,
    out: PathLike,
    run_id: str,
    template_path: PathLike = DOMAIN_TEMPLATE,
) -> StageResult:
    """Write and define an owned domain, keeping its XML under the run directory."""

    run_path = Path(run_dir)
    root = Path(harness_root)
    if not _under(run_path, root):
        return StageResult("domain", False, f"refused unsafe acceptance run directory: {run_path}", _DOMAIN_FIX)
    io.mkdir(run_path, mode=0o755, parents=True, exist_ok=True)
    xml_path = run_path / "domain.xml"
    xml = domain_xml(
        io,
        template_path=template_path,
        name=name,
        os_path=run_path / "os.qcow2",
        data_path=run_path / "data.qcow2",
        seed_path=seed_path,
        console_path=Path(out) / "console.log",
        run_id=run_id,
    )
    io.write_text(xml_path, xml, mode=0o644)
    result = io.run(["virsh", "define", str(xml_path)])
    return _result("domain", result, f"defined acceptance VM {name}")


def start(io: Host, name: str) -> StageResult:
    """Start an acceptance VM by name."""

    return _result("boot", io.run(["virsh", "start", name]), f"started acceptance VM {name}")


def address(io: Host, name: str) -> str | None:
    """Return the first valid IPv4 lease reported for *name*."""

    result = io.run(["virsh", "domifaddr", name, "--source", "lease"])
    if result.returncode != 0:
        return None
    for match in _IPV4.finditer(result.stdout):
        candidate = match.group("address")
        try:
            if isinstance(ipaddress.ip_address(candidate), ipaddress.IPv4Address):
                return candidate
        except ValueError:
            pass
    return None


def running(io: Host, name: str) -> bool:
    """Whether libvirt reports the domain as running or paused."""

    result = io.run(["virsh", "domstate", name])
    return result.returncode == 0 and result.stdout.strip() in {"running", "paused"}


def destroy(io: Host, name: str) -> StageResult:
    """Stop a defined acceptance VM; one already shut off is left as it is."""

    if not running(io, name):
        return StageResult("teardown", True, f"acceptance VM {name} is not running", "")
    return _result("teardown", io.run(["virsh", "destroy", name]), f"destroyed acceptance VM {name}")


def undefine(io: Host, name: str) -> StageResult:
    """Undefine an acceptance VM and remove its storage."""

    return _result(
        "teardown",
        io.run(["virsh", "undefine", name, "--remove-all-storage"]),
        f"undefined acceptance VM {name}",
    )


def teardown(
    io: Host,
    *,
    name: str,
    run_dir: PathLike,
    harness_root: PathLike,
    defined: bool,
) -> StageResult:
    """Guard, destroy, undefine, and remove only this harness's VM and files."""

    root = Path(harness_root)
    run_path = Path(run_dir)
    if not _under(run_path, root):
        return StageResult("teardown", False, f"refused unsafe acceptance run directory: {run_path}", _DOMAIN_FIX)
    if defined:
        failure, xml = _domain_xml(io, name)
        if failure is not None:
            return StageResult("teardown", False, failure.detail, failure.fix)
        if xml is None or not _owned(xml, root):
            return StageResult("teardown", False, f"refused to tear down foreign VM {name}", _DOMAIN_FIX)
        result = destroy(io, name)
        if not result.ok:
            return result
        result = undefine(io, name)
        if not result.ok:
            return result
    return _remove_run_dir(io, run_path, root)

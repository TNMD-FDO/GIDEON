"""Build the NoCloud seed for one acceptance VM."""

import subprocess
from typing import Final

import yaml  # type: ignore[import-untyped]

from gideon.host.report import StageResult, command_detail
from tools.acceptance.context import HarnessContext

# A reserved TLD (RFC 2606): the VM's name resolves through manage_etc_hosts
# and the site file names it; nothing outside the VM ever resolves it.
ACCEPTANCE_DOMAIN: Final = "acceptance.invalid"
SEED_FIX: Final = "Inspect ssh-keygen and cloud-localds output, then retry acceptance."


def hostname_for(vm_name: str) -> str:
    """Return the VM hostname in the reserved acceptance namespace."""

    return f"{vm_name}.{ACCEPTANCE_DOMAIN}"


def _failure(detail: str, result: subprocess.CompletedProcess[str]) -> StageResult:
    return StageResult("seed", False, f"{detail}: {command_detail(result)}", SEED_FIX)


def _user_data(hostname: str, public_key: str) -> str:
    document = {
        "hostname": hostname.split(".", 1)[0],
        "fqdn": hostname,
        "manage_etc_hosts": True,
        "ssh_pwauth": False,
        "users": [
            {
                "name": "csa1",
                "groups": ["sudo"],
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "ssh_authorized_keys": [public_key],
            },
            {
                "name": "csa2",
                "groups": ["sudo"],
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "ssh_authorized_keys": [public_key],
            },
            {
                "name": "gideon-backup",
                "shell": "/bin/sh",
                "homedir": "/srv/gideon-backup",
                "lock_passwd": True,
            },
        ],
        "runcmd": ["chown -R csa1:csa1 /srv/GIDEON.git"],
    }
    # The first line is cloud-init's format marker, not YAML.
    return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)


def _meta_data(run_id: str, hostname: str) -> str:
    return yaml.safe_dump(
        {"instance-id": run_id, "local-hostname": hostname}, sort_keys=False
    )


def build(ctx: HarnessContext) -> StageResult:
    """Generate the run keypair and the two NoCloud metadata files."""

    run_dir = ctx.spec.run_dir
    private_key = run_dir / "id_ed25519"
    public_key = run_dir / "id_ed25519.pub"
    result = ctx.host.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(private_key),
            "-C",
            ctx.run_id,
        ]
    )
    if result.returncode != 0:
        return _failure("could not create the acceptance SSH key", result)
    try:
        key = ctx.host.read_text(public_key).strip()
    except (OSError, UnicodeError) as exc:
        return StageResult("seed", False, f"could not read {public_key}: {exc}", SEED_FIX)
    if not key:
        return StageResult("seed", False, f"{public_key} is empty", SEED_FIX)

    hostname = hostname_for(ctx.spec.vm_name)
    user_data = run_dir / "user-data"
    meta_data = run_dir / "meta-data"
    ctx.host.write_text(user_data, _user_data(hostname, key), mode=0o644)
    ctx.host.write_text(meta_data, _meta_data(ctx.run_id, hostname), mode=0o644)
    result = ctx.host.run(
        ["cloud-localds", str(run_dir / "seed.iso"), str(user_data), str(meta_data)]
    )
    if result.returncode != 0:
        return _failure("could not create the NoCloud seed", result)
    return StageResult("seed", True, f"created NoCloud seed for {hostname}", "")

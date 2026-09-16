"""Create the acceptance VM's disks from the pinned cloud image.

The image conversion was settled on the build box before this stage was
implemented.  The source image is GPT: partition 1 is the 2.4 GiB ext4
``cloudimg-rootfs`` root, partition 13 is the 1023 MiB ext4 ``BOOT``
partition, partition 14 is the 4 MiB BIOS-boot partition, and partition 15
is the 106 MiB vfat ``UEFI`` ESP.  Its ``/etc/fstab`` mounts ``/``,
``/boot``, and ``/boot/efi`` by label.  The box has lvm2 2.03.31, grub-pc and
grub-pc-bin, and uses dracut 110 rather than initramfs-tools; the tested
kernel is the one recorded in ``host.lock``.  Its grub command line is
``console=tty1 console=ttyS0`` so the domain's serial log carries boot output.

The target OS disk is a sparse 40 GiB qcow2.  One guestfish session creates
the GPT/LVM layout, copies the source ESP and /boot partitions read-only, and
copies the source root filesystem into the new root LV.  A single
``virt-customize --no-network`` invocation then copies the checkout mirror
and runs ``grub-install --target=i386-pc /dev/sda``, ``update-grub``, and
``dracut --force --no-hostonly --regenerate-all``.  The data disk is a sparse
2 TiB qcow2, larger than the OS disk and the disk-layout reserve.
"""

import shlex
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Final

from gideon.host.report import StageResult, command_detail
from gideon.host.steps.services import acceptance_image_path
from gideon.host.sysio import Host
from tools.acceptance.context import HarnessContext, checkout_git

OS_DISK_SIZE: Final = "40G"
DATA_DISK_SIZE: Final = "2T"
SECTOR_SIZE: Final = 512
SECTORS_PER_MIB: Final = 1024 * 1024 // SECTOR_SIZE
PARTITION_ALIGNMENT_SECTORS: Final = 2048
GPT_TRAILER_SECTORS: Final = 34
BIOS_BOOT_MIB: Final = 4
ESP_MIB: Final = 128
BOOT_MIB: Final = 1024
BIOS_BOOT_TYPE: Final = "21686148-6449-6E6F-744E-656564454649"
ESP_TYPE: Final = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LVM_TYPE: Final = "E6D6D379-F507-44C2-A23C-238F2A3DF928"
ROOT_LV_MIB: Final = 24 * 1024
DOCKER_LV_MIB: Final = 12 * 1024
SWAP_LV_MIB: Final = 2 * 1024
IMAGE_FIX: Final = "Inspect qemu-img, guestfish, and virt-customize output, then retry acceptance."


def _partition_sectors() -> tuple[tuple[int, int], ...]:
    """Return aligned GPT ranges for the fixed 40 GiB target disk."""

    total_sectors = 40 * 1024 * 1024 * 1024 // SECTOR_SIZE
    next_sector = PARTITION_ALIGNMENT_SECTORS

    def fixed_partition(size_mib: int) -> tuple[int, int]:
        nonlocal next_sector
        start = next_sector
        end = start + size_mib * SECTORS_PER_MIB - 1
        next_sector = end + 1
        return start, end

    bios = fixed_partition(BIOS_BOOT_MIB)
    esp = fixed_partition(ESP_MIB)
    boot = fixed_partition(BOOT_MIB)
    lvm = (next_sector, total_sectors - GPT_TRAILER_SECTORS)
    return bios, esp, boot, lvm


def _guestfish_script(os_path: Path, source_path: Path) -> str:
    """Return the one guestfish script that lays out the new OS disk."""

    bios, esp, boot, lvm = _partition_sectors()
    quote = shlex.quote
    fstab_lines = (
        "/dev/ubuntu-vg/root / ext4 defaults 0 1",
        "/dev/ubuntu-vg/docker /var/lib/docker ext4 defaults 0 2",
        "/dev/ubuntu-vg/swap none swap sw 0 0",
        "LABEL=BOOT /boot ext4 defaults 0 2",
        "LABEL=UEFI /boot/efi vfat defaults 0 1",
    )

    def guestfish_text(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'

    commands = [
        f"add-drive {quote(str(os_path))}",
        f"add-drive-ro {quote(str(source_path))}",
        "run",
        "part-init /dev/sda gpt",
        f"part-add /dev/sda p {bios[0]} {bios[1]}",
        f"part-set-gpt-type /dev/sda 1 {BIOS_BOOT_TYPE}",
        f"part-add /dev/sda p {esp[0]} {esp[1]}",
        f"part-set-gpt-type /dev/sda 2 {ESP_TYPE}",
        f"part-add /dev/sda p {boot[0]} {boot[1]}",
        f"part-add /dev/sda p {lvm[0]} {lvm[1]}",
        f"part-set-gpt-type /dev/sda 4 {LVM_TYPE}",
        "copy-device-to-device /dev/sdb15 /dev/sda2",
        "copy-device-to-device /dev/sdb13 /dev/sda3",
        "pvcreate /dev/sda4",
        # guestfish: lvcreate <logvol> <volgroup> <mbytes>.
        "vgcreate ubuntu-vg /dev/sda4",
        f"lvcreate root ubuntu-vg {ROOT_LV_MIB}",
        f"lvcreate docker ubuntu-vg {DOCKER_LV_MIB}",
        f"lvcreate swap ubuntu-vg {SWAP_LV_MIB}",
        "copy-device-to-device /dev/sdb1 /dev/ubuntu-vg/root",
        "e2fsck-f /dev/ubuntu-vg/root",
        "resize2fs /dev/ubuntu-vg/root",
        "mkfs ext4 /dev/ubuntu-vg/docker",
        "mkswap /dev/ubuntu-vg/swap",
        "mount /dev/ubuntu-vg/root /",
        "mkdir-p /var/lib/docker",
        f"write /etc/fstab {guestfish_text(fstab_lines[0] + chr(10))}",
        *(
            f"write-append /etc/fstab {guestfish_text(line + chr(10))}"
            for line in fstab_lines[1:]
        ),
        "umount-all",
    ]
    return "\n".join(commands) + "\n"


def _failed(name: str, detail: str, result: subprocess.CompletedProcess[str]) -> StageResult:
    return StageResult(
        name,
        False,
        f"{detail}: {command_detail(result)}",
        IMAGE_FIX,
    )


def _run(
    io: Host,
    argv: list[str],
    *,
    input: str | None = None,
    detail: str,
) -> StageResult | None:
    try:
        result = io.run(argv, input=input)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("image", False, f"{detail}: {exc}", IMAGE_FIX)
    if result.returncode != 0:
        return _failed("image", detail, result)
    return None


def _mirror(ctx: HarnessContext, mirror: Path) -> StageResult | None:
    result = _run(
        ctx.host,
        checkout_git(
            ctx.checkout,
            "clone",
            "--mirror",
            str(ctx.checkout),
            str(mirror),
        ),
        detail=f"cloned the checkout mirror into {mirror}",
    )
    if result is not None:
        return result

    tag_probe = ctx.host.run(
        checkout_git(
            ctx.checkout,
            "rev-parse",
            "--verify",
            f"refs/tags/{ctx.spec.ref}",
        ),
        cwd=ctx.checkout,
    )
    if tag_probe.returncode == 0:
        ctx.spec = replace(ctx.spec, clone_ref=ctx.spec.ref)
        return None

    tag = f"acceptance-{ctx.spec.resolved_commit[:12]}"
    result = _run(
        ctx.host,
        ["git", "-C", str(mirror), "tag", tag, ctx.spec.resolved_commit],
        detail=f"tagged the mirror {tag}",
    )
    if result is not None:
        return result
    ctx.spec = replace(ctx.spec, clone_ref=tag)
    return None


def build(ctx: HarnessContext) -> StageResult:
    """Create the OS/data disks, mirror the checkout, and customize the OS."""

    if ctx.lock is None or not ctx.run_id:
        return StageResult("image", False, "image inputs were not resolved", IMAGE_FIX)
    run_dir = ctx.spec.run_dir
    ctx.host.mkdir(run_dir, mode=0o755, parents=True, exist_ok=True)
    ctx.run_dir_created = True
    os_disk = run_dir / "os.qcow2"
    data_disk = run_dir / "data.qcow2"
    source = acceptance_image_path(ctx.lock)

    for disk, size in ((os_disk, OS_DISK_SIZE), (data_disk, DATA_DISK_SIZE)):
        result = _run(
            ctx.host,
            ["qemu-img", "create", "-f", "qcow2", str(disk), size],
            detail=f"created {disk.name} ({size})",
        )
        if result is not None:
            return result

    guestfish = _run(
        ctx.host,
        ["guestfish"],
        input=_guestfish_script(os_disk, source),
        detail="converted the pinned image with guestfish",
    )
    if guestfish is not None:
        return guestfish

    mirror = run_dir / "GIDEON.git"
    mirrored = _mirror(ctx, mirror)
    if mirrored is not None:
        return mirrored

    customized = _run(
        ctx.host,
        [
            "virt-customize",
            "-a",
            str(os_disk),
            "--no-network",
            "--copy-in",
            f"{mirror}:/srv",
            "--run-command",
            "grub-install --target=i386-pc /dev/sda",
            "--run-command",
            "update-grub",
            "--run-command",
            "dracut --force --no-hostonly --regenerate-all",
        ],
        detail="customized the acceptance OS image",
    )
    if customized is not None:
        return customized
    return StageResult(
        "image",
        True,
        f"created OS disk {OS_DISK_SIZE} and data disk {DATA_DISK_SIZE}",
        "",
    )

"""The real co-tenant's guide, arrival stages, and host-side checks."""

import math
import re
import secrets
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml  # type: ignore[import-untyped]

from gideon.host import images as host_images
from gideon.host import stack, tls
from gideon.host.render import compose as host_compose
from gideon.host.report import StageResult, command_detail
from gideon.host.steps import docker as docker_step
from tools.acceptance import cotenant, fullrestore, vm
from tools.acceptance.context import HarnessContext
from tools.pinwatch.fetch import FetchError

REPOSITORY: Final = "https://github.com/TNMD-FDO/gideon-transcribe.git"
RAW_BASE: Final = "https://raw.githubusercontent.com/TNMD-FDO/gideon-transcribe/{tag}"
INSTALL_HOME: Final = "/opt/gideon-transcribe"
DATA_DIR: Final = "/data/transcribe"
APP_USER: Final = "transcribe"
HTTPS_PORT: Final = 8443
# Transcribe's own address, distinct from GIDEON's as on a shared box, and
# short: its self-signed root's subject wraps the name in 40 more characters,
# and OpenSSL refuses a common name past 64.
HOSTNAME: Final = "tx.acceptance.invalid"
PROJECT: Final = "transcribe"
TIMER_UNITS: Final = (
    "transcribe-backup.service",
    "transcribe-backup.timer",
    "transcribe-backup-weekly.service",
    "transcribe-backup-weekly.timer",
    "transcribe-backup-drill.service",
    "transcribe-backup-drill.timer",
)
FIXTURE_DIR: Final = Path(__file__).with_suffix("")
# Transcribe's media worker carries a fixed 40-CPU limit, and Docker refuses
# a limit above the host's CPU count, so the pair VM has at least that many.
PAIR_VCPUS: Final = 40
# Set from the two forms' measure (about 5.1 GiB used with both stacks up) by
# pair_size: twice the use rounds up to 12 GiB, under the 16 GiB floor.
PAIR_MEMORY_MIB: Final = 16 * 1024
TAG_TIMEOUT_SECONDS: Final = 300
PREPARE_TIMEOUT_SECONDS: Final = 1800
DATA_MOUNT_TIMEOUT_SECONDS: Final = 300
CLONE_TIMEOUT_SECONDS: Final = 600
IMAGES_TIMEOUT_SECONDS: Final = 1800
INSTALL_TIMEOUT_SECONDS: Final = 1800
UP_TIMEOUT_SECONDS: Final = 900
HEALTH_TIMEOUT_SECONDS: Final = 300
HEALTH_POLL_SECONDS: Final = 5
PUSH_TIMEOUT_SECONDS: Final = 3600
STORE_MOVE_TIMEOUT_SECONDS: Final = 1800
MEASURE_TIMEOUT_SECONDS: Final = 300
DATA_DISK: Final = "/dev/sdb"
MOUNT_DATA_SCRIPT: Final = "/tmp/gideon-acceptance-transcribe-mount-data.sh"
SELECT_FIX: Final = "Check the Transcribe repository and acceptance box, then retry acceptance."
DOCKER_FIX: Final = "Repair Docker on the acceptance VM, then retry acceptance."
MOUNT_FIX: Final = "Inspect the acceptance VM's data disk and /data mount, then retry acceptance."
PREPARE_FIX: Final = "Inspect the Transcribe preparation transcript and acceptance VM, then retry acceptance."
CLONE_FIX: Final = "Inspect the Transcribe clone transcript and acceptance VM, then retry acceptance."
IMAGES_FIX: Final = "Restore the selected Transcribe images on the acceptance VM, then retry acceptance."
INSTALL_FIX: Final = "Inspect the Transcribe install transcript and VM .env, then retry acceptance."
UP_FIX: Final = "Inspect the Transcribe Compose stack and create the Local admin in a terminal, then retry acceptance."
PUSH_FIX: Final = "Inspect the backup push transcript and the VM's loopback target, then retry acceptance."
HEALTH_FIX: Final = "Inspect the Transcribe and GIDEON health transcripts and VM, then retry acceptance."
STORE_MOVE_FIX: Final = "Inspect the containerd store-move transcript and VM; preserve both roots until the move is accepted."
MEASURE_FIX: Final = "Inspect the measure transcript and VM resource readings, then retry acceptance."
STORE_MOVE_SCRIPT: Final = "/tmp/gideon-acceptance-transcribe-store-move.sh"
STORE_COUNTS: Final = re.compile(
    r"^counts inode=(\d+)/(\d+) bytes=(\d+)/(\d+) whiteouts=(\d+)/(\d+) opaque=(\d+)/(\d+)$",
    re.MULTILINE,
)
_MEM_TOTAL: Final = re.compile(r"^MemTotal:\s+(\d+)\s+kB$", re.MULTILINE)
_MEM_AVAILABLE: Final = re.compile(r"^MemAvailable:\s+(\d+)\s+kB$", re.MULTILINE)
_LOAD_AVERAGE: Final = re.compile(r"^== loadavg ==\n([0-9]+(?:\.[0-9]+)?)\s", re.MULTILINE)
_CONTAINER_COUNT: Final = re.compile(r"^container-count (\d+)$", re.MULTILINE)
_RELEASE_TAG: Final = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)$"
)
_VARIABLE: Final = re.compile(r"\$\{[^}]*\}")
_RELEASE_VARIABLE: Final = "${RELEASE_TAG:-dev}"


@dataclass(frozen=True, slots=True)
class GuideBlock:
    """A guide block and whether the no-GPU acceptance VM runs it."""

    name: str
    commands: tuple[str, ...]
    run: bool


GUIDE_STEP_BLOCKS: Final[tuple[GuideBlock, ...]] = (
    GuideBlock(
        "Step 0 Docker keyring",
        (
            "sudo apt update && sudo apt install -y ca-certificates curl && sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && sudo chmod a+r /etc/apt/keyrings/docker.asc",
        ),
        True,
    ),
    GuideBlock(
        "Step 0 Docker source",
        (
            "sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF\nTypes: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: $(. /etc/os-release && echo \"${UBUNTU_CODENAME:-$VERSION_CODENAME}\")\nComponents: stable\nArchitectures: $(dpkg --print-architecture)\nSigned-By: /etc/apt/keyrings/docker.asc\nEOF",
        ),
        True,
    ),
    GuideBlock(
        "Step 0 Docker packages",
        (
            "sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
        ),
        True,
    ),
    GuideBlock("Step 0 Docker group", ("sudo usermod -aG docker $USER",), True),
    GuideBlock(
        "Step 0 NVIDIA driver",
        (
            "sudo ubuntu-drivers install --gpgpu",
        ),
        False,
    ),
    GuideBlock(
        "Step 0 NVIDIA container toolkit",
        (
            "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null",
            "sudo apt update && sudo apt install -y nvidia-container-toolkit",
            "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker",
        ),
        False,
    ),
    GuideBlock(
        "Step 0 NVIDIA CDI",
        ("sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml",),
        False,
    ),
    GuideBlock(
        "Step 1 transcribe account",
        (
            "sudo useradd --system --no-create-home --home-dir <App data folder> --shell /usr/sbin/nologin --user-group transcribe",
        ),
        True,
    ),
    GuideBlock(
        "Step 1 data folder",
        ("sudo install -d -o transcribe -g transcribe -m 0750 <App data folder>",),
        True,
    ),
    GuideBlock(
        "Step 1 install home",
        ("sudo install -d -o $USER -g $USER -m 0755 <Install home>",),
        True,
    ),
    GuideBlock(
        "Step 1 account and data check",
        ("id transcribe && sudo ls -la <App data folder>",),
        True,
    ),
)


def _failure(stage: str, detail: str, fix: str) -> StageResult:
    return StageResult(stage, False, detail, fix)


def _tag_key(tag: str) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.fullmatch(tag)
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _latest_tag(output: str) -> str | None:
    releases: list[tuple[tuple[int, int, int], str]] = []
    for line in output.splitlines():
        _object, separator, ref = line.partition("\t")
        if not separator or not ref.startswith("refs/tags/") or ref.endswith("^{}"):
            continue
        tag = ref.removeprefix("refs/tags/")
        version = _tag_key(tag)
        if version is not None:
            releases.append((version, tag))
    if not releases:
        return None
    return max(releases)[1]


def _fetch_text(ctx: HarnessContext, tag: str, path: str) -> tuple[str | None, str | None]:
    url = f"{RAW_BASE.format(tag=tag)}/{path}"
    try:
        response = ctx.fetcher.get(url)
    except FetchError as exc:
        return None, str(exc)
    if response.status != 200:
        return None, f"{url} returned HTTP {response.status}"
    try:
        return response.body.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"{url} is not UTF-8: {exc}"


def _compose_images(text: str, tag: str) -> tuple[dict[str, str] | None, str | None]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"compose.yaml is invalid YAML: {exc}"
    if not isinstance(document, Mapping):
        return None, "compose.yaml is not a mapping"
    services = document.get("services")
    if not isinstance(services, Mapping):
        return None, "compose.yaml has no services mapping"
    images: dict[str, str] = {}
    for service, definition in services.items():
        if not isinstance(service, str) or not isinstance(definition, Mapping):
            return None, "compose.yaml has an invalid service entry"
        if "profiles" in definition:
            continue
        reference = definition.get("image")
        if not isinstance(reference, str) or not reference:
            return None, f"service {service} has no image reference"
        reference = reference.replace(_RELEASE_VARIABLE, tag)
        variable = _VARIABLE.search(reference)
        if variable is not None:
            return None, f"service {service} image uses unsupported variable {variable.group(0)}"
        images[service] = reference
    if not images:
        return None, "compose.yaml declares no unprofiled service images"
    return images, None


def select(ctx: HarnessContext) -> StageResult:
    """Select a release, compare its recorded guide, and check box images."""

    tag = ctx.spec.transcribe_tag
    if tag:
        if _tag_key(tag) is None:
            return _failure("select", f"Transcribe tag {tag!r} is not a vMAJOR.MINOR.PATCH release", SELECT_FIX)
    else:
        try:
            listed = ctx.host.run(["git", "ls-remote", "--tags", REPOSITORY], timeout=TAG_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return _failure("select", f"could not run git ls-remote for {REPOSITORY}: {exc}", SELECT_FIX)
        if listed.returncode != 0:
            return _failure("select", f"git ls-remote failed: {command_detail(listed)}", SELECT_FIX)
        tag = _latest_tag(listed.stdout) or ""
        if not tag:
            return _failure("select", f"{REPOSITORY} has no vMAJOR.MINOR.PATCH release tags", SELECT_FIX)

    compose, problem = _fetch_text(ctx, tag, "compose.yaml")
    if problem is not None or compose is None:
        return _failure("select", f"could not read compose.yaml at {tag}: {problem or 'no text returned'}", SELECT_FIX)
    images, problem = _compose_images(compose, tag)
    if problem is not None or images is None:
        return _failure("select", problem or f"could not read image references at {tag}", SELECT_FIX)

    guide, problem = _fetch_text(ctx, tag, "docs/install.md")
    if problem is not None or guide is None:
        return _failure("select", f"could not read docs/install.md at {tag}: {problem or 'no text returned'}", SELECT_FIX)
    moved = tuple(
        block.name
        for block in GUIDE_STEP_BLOCKS
        if any(command not in guide for command in block.commands)
    )
    if moved:
        names = ", ".join(moved)
        return _failure(
            "select",
            f"the {tag} install guide moved recorded blocks: {names}",
            f"Update GUIDE_STEP_BLOCKS for the moved guide blocks ({names}), then retry acceptance.",
        )

    for reference in dict.fromkeys(images.values()):
        try:
            inspected = ctx.host.run(["docker", "image", "inspect", reference], timeout=TAG_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return _failure("select", f"could not run docker image inspect for {reference}: {exc}", DOCKER_FIX)
        if inspected.returncode == 0:
            continue
        if "No such image" in inspected.stderr or "No such image" in inspected.stdout:
            return _failure(
                "select",
                f"the box has no image {reference} required by Transcribe {tag}",
                f"Upgrade Transcribe on the box to {tag} so its image is present, or pass --transcribe-tag <box tag> and retry acceptance.",
            )
        return _failure(
            "select",
            f"could not inspect image {reference}: {command_detail(inspected)}",
            DOCKER_FIX,
        )

    ctx.transcribe_tag = tag
    ctx.transcribe_images = images
    return StageResult(
        "select",
        True,
        f"selected Transcribe {tag}; images {', '.join(f'{name}={image}' for name, image in images.items())}",
        "",
    )


def mount_data(ctx: HarnessContext) -> StageResult:
    """Format the VM's blank data disk and mount it at /data."""

    if ctx.spec.pair != "before":
        return _failure("data-mount", "the data-mount stage is only for the before form", MOUNT_FIX)
    try:
        script = ctx.host.read_text(FIXTURE_DIR / "mount-data.sh")
    except (OSError, UnicodeError) as exc:
        return _failure("data-mount", f"could not read the mount script: {exc}", MOUNT_FIX)
    copied = vm.copy_in(ctx, MOUNT_DATA_SCRIPT, script, 0o755, as_root=True)
    if not copied.ok:
        return StageResult("data-mount", False, copied.detail, MOUNT_FIX)
    mounted = vm.run_as_root(
        ctx,
        f"bash {shlex.quote(MOUNT_DATA_SCRIPT)} {shlex.quote(DATA_DISK)}",
        timeout=DATA_MOUNT_TIMEOUT_SECONDS,
    )
    if mounted.returncode != 0:
        return _failure("data-mount", f"data disk mount failed: {command_detail(mounted)}", MOUNT_FIX)
    reading = vm.run_as_root(ctx, "findmnt -rn -o TARGET --mountpoint /data")
    if reading.returncode != 0 or reading.stdout.strip() != "/data":
        return _failure(
            "data-mount",
            f"findmnt did not confirm /data (exit {reading.returncode}; target {reading.stdout.strip() or 'none'})",
            MOUNT_FIX,
        )
    return StageResult("data-mount", True, f"mounted {DATA_DISK} at /data", "")


def _docker_reading(
    ctx: HarnessContext,
    *,
    allow_absent: bool,
    stage: str = "transcribe-prepare",
) -> tuple[tuple[str, str] | None, StageResult | None]:
    # A shell builtin, so never behind sudo, which would find no such program.
    present = vm.ssh(ctx, "command -v docker")
    if present.returncode == 1 and allow_absent:
        return None, None
    if present.returncode != 0 or not present.stdout.strip():
        return None, _failure(
            stage,
            f"could not determine whether Docker is installed: {command_detail(present)}",
            DOCKER_FIX,
        )
    version = vm.run_as_root(ctx, "docker --version")
    if version.returncode != 0 or not version.stdout.strip():
        return None, _failure(stage, f"could not read Docker version: {command_detail(version)}", DOCKER_FIX)
    started = vm.run_as_root(
        ctx,
        "systemctl show docker.service --property=ActiveEnterTimestamp --value",
    )
    if started.returncode != 0 or not started.stdout.strip():
        return None, _failure(
            stage,
            f"could not read Docker ActiveEnterTimestamp: {command_detail(started)}",
            DOCKER_FIX,
        )
    return (version.stdout.strip(), started.stdout.strip()), None


def _substitute(command: str) -> str:
    return (
        command.replace("<App data folder>", DATA_DIR)
        .replace("<Install home>", INSTALL_HOME)
    )


def prepare(ctx: HarnessContext) -> StageResult:
    """Run the runnable Docker and server blocks from Transcribe's guide."""

    before, failure = _docker_reading(ctx, allow_absent=True)
    if failure is not None:
        return failure
    if before is not None:
        ctx.docker_reading = before
    transcripts_before = len(ctx.transcripts)
    for block in GUIDE_STEP_BLOCKS:
        if not block.run:
            continue
        for number, raw in enumerate(block.commands, start=1):
            command = _substitute(raw)
            result, _text = vm.run_product(
                ctx,
                "transcribe-prepare",
                f"transcribe-prepare-{re.sub(r'[^a-z0-9]+', '-', block.name.lower()).strip('-')}-{number}.txt",
                ["bash", "-o", "pipefail", "-c", command],
                timeout=PREPARE_TIMEOUT_SECONDS,
                sudo=False,
            )
            if not result.ok:
                return StageResult("transcribe-prepare", False, result.detail, PREPARE_FIX)
    compose, _text = vm.run_product(
        ctx,
        "transcribe-prepare",
        "transcribe-compose-version.txt",
        ["docker", "compose", "version"],
        timeout=TAG_TIMEOUT_SECONDS,
        sudo=False,
    )
    if not compose.ok:
        return StageResult("transcribe-prepare", False, compose.detail, PREPARE_FIX)
    after, failure = _docker_reading(ctx, allow_absent=False)
    if failure is not None or after is None:
        return failure or _failure("transcribe-prepare", "Docker reading is unavailable", DOCKER_FIX)
    ctx.docker_reading = after
    changes: list[str] = []
    if before is None:
        changes.append("docker installed")
    elif before[0] != after[0]:
        changes.append(f"docker upgraded from {before[0]} to {after[0]}")
    if before is not None and before[1] != after[1]:
        changes.append("docker restarted")
    change_detail = "; ".join(changes) if changes else "Docker version and start time unchanged"
    transcripts = ", ".join(str(path) for path in ctx.transcripts[transcripts_before:])
    return StageResult(
        "transcribe-prepare",
        True,
        f"ran the recorded Docker and server blocks; docker compose version passed; {change_detail}; transcripts {transcripts}",
        "",
    )


def clone(ctx: HarnessContext) -> StageResult:
    """Clone the selected Transcribe release into its install home."""

    tag = ctx.transcribe_tag
    if tag is None:
        return _failure("transcribe-clone", "the select stage did not choose a Transcribe tag", CLONE_FIX)
    script = (
        f"cd {shlex.quote(INSTALL_HOME)} && git clone --branch {shlex.quote(tag)} "
        f"{REPOSITORY} ."
    )
    result, _text = vm.run_product(
        ctx,
        "transcribe-clone",
        "transcribe-clone.txt",
        ["bash", "-o", "pipefail", "-c", script],
        timeout=CLONE_TIMEOUT_SECONDS,
        sudo=False,
    )
    if not result.ok:
        return StageResult("transcribe-clone", False, result.detail, CLONE_FIX)
    return StageResult("transcribe-clone", True, f"cloned Transcribe {tag} into {INSTALL_HOME}", "")


def images(ctx: HarnessContext) -> StageResult:
    """Stream the selected images from the box daemon into the VM daemon."""

    selected = ctx.transcribe_images
    if not selected:
        return _failure("transcribe-images", "the select stage did not choose Transcribe images", IMAGES_FIX)
    references = tuple(dict.fromkeys(selected.values()))
    save = shlex.join(("docker", "save", *references))
    load = shlex.join(vm.ssh_argv(ctx, "sudo docker load"))
    try:
        streamed = ctx.host.run(
            ["bash", "-o", "pipefail", "-c", f"{save} | {load}"],
            timeout=IMAGES_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _failure(
            "transcribe-images",
            f"could not run the docker save | ssh docker load stream ({type(exc).__name__})",
            IMAGES_FIX,
        )
    if streamed.returncode != 0:
        return _failure(
            "transcribe-images",
            f"docker save | ssh docker load failed: {command_detail(streamed)}",
            IMAGES_FIX,
        )

    pulled: list[str] = []
    for reference in references:
        inspected = vm.run_as_root(ctx, f"docker image inspect {shlex.quote(reference)}", timeout=TAG_TIMEOUT_SECONDS)
        if inspected.returncode == 0:
            continue
        absent = "No such image" in inspected.stderr or "No such image" in inspected.stdout
        if not absent or "@sha256:" not in reference or reference.startswith("ghcr.io/"):
            return _failure(
                "transcribe-images",
                f"image reference {reference} did not resolve in the VM: exit {inspected.returncode}",
                IMAGES_FIX,
            )
        pull = vm.run_as_root(ctx, f"docker pull {shlex.quote(reference)}", timeout=IMAGES_TIMEOUT_SECONDS)
        if pull.returncode != 0:
            return _failure(
                "transcribe-images",
                f"image reference {reference} was absent and docker pull exited {pull.returncode}",
                IMAGES_FIX,
            )
        verified = vm.run_as_root(ctx, f"docker image inspect {shlex.quote(reference)}", timeout=TAG_TIMEOUT_SECONDS)
        if verified.returncode != 0:
            return _failure(
                "transcribe-images",
                f"image reference {reference} did not resolve after docker pull (exit {verified.returncode})",
                IMAGES_FIX,
            )
        pulled.append(reference)
    detail = f"streamed and verified {len(references)} Transcribe image references"
    if pulled:
        detail += f"; pulled {', '.join(pulled)}"
    return StageResult("transcribe-images", True, detail, "")


def _install_answers(ctx: HarnessContext) -> tuple[str, ...]:
    """The no-card answers in the order the tagged installer asks them."""

    if ctx.address is None or ctx.vm_site is None:
        raise ValueError("the VM address and site must be loaded before Transcribe install")
    return (
        HOSTNAME,
        str(HTTPS_PORT),
        ctx.address,
        host_images.LIBVIRT_BRIDGE_CIDR,
        DATA_DIR,
        ctx.vm_site.office.timezone,
        "self-signed",
        "no",
        "none",
        "",
        "",
        "",
        "no",
    )


_INSTALL_GAPS_SCRIPT: Final = (
    f"cd {INSTALL_HOME} && "
    "if ! sudo test -e secrets/ldap_bind_password; then "
    f"sudo install -o {APP_USER} -g {APP_USER} -m 0400 /dev/null secrets/ldap_bind_password && "
    "printf 'wrote an empty secrets/ldap_bind_password; '; fi && "
    "mkdir -p ca && if ! test -e ca/office-root.pem; then : > ca/office-root.pem && "
    "printf 'wrote an empty ca/office-root.pem; '; fi && "
    # The self-signed path leaves the key its maker's at 0600, and Caddy runs
    # as the app's account, so the key is handed over as a placed one is.
    f"sudo chown {APP_USER}:{APP_USER} tls/key.pem && sudo chmod 0400 tls/key.pem && "
    f"printf 'gave tls/key.pem to {APP_USER} at 0400'"
)


def _set_env_line(ctx: HarnessContext, name: str, key: str, value: str) -> str | None:
    """Set one KEY=value line of a file in the install home; the value travels on stdin."""

    script = (
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "key = sys.argv[2]\n"
        "value = sys.stdin.readline().rstrip('\\n')\n"
        "lines = path.read_text().splitlines()\n"
        "replacement = key + '=' + value\n"
        "for index, line in enumerate(lines):\n"
        "    if line.startswith(key + '='):\n"
        "        lines[index] = replacement\n"
        "        break\n"
        "else:\n"
        "    lines.append(replacement)\n"
        "path.write_text('\\n'.join(lines) + '\\n')\n"
    )
    written = vm.ssh(
        ctx,
        f"cd {shlex.quote(INSTALL_HOME)} && python3 -c {shlex.quote(script)} {shlex.quote(name)} {shlex.quote(key)}",
        input=value + "\n",
        timeout=TAG_TIMEOUT_SECONDS,
    )
    if written.returncode != 0:
        return f"writing {key} into {name} failed: {command_detail(written)}"
    return None


def install(ctx: HarnessContext) -> StageResult:
    """Run Transcribe's guide installer, record its selected release, and add timers."""

    tag = ctx.transcribe_tag
    if tag is None:
        return _failure("transcribe-install", "the select stage did not choose a Transcribe tag", INSTALL_FIX)
    try:
        answers = _install_answers(ctx)
    except ValueError as exc:
        return _failure("transcribe-install", str(exc), INSTALL_FIX)
    # The tag's no-card install leaves its example file's `transcription`
    # profile on and then exits at the engine question reading an absent card,
    # so the profile is blanked first, as the no-card path should leave it.
    blanked = _set_env_line(ctx, ".env.example", "COMPOSE_PROFILES", "")
    if blanked is not None:
        return _failure("transcribe-install", blanked, INSTALL_FIX)
    result, _text = vm.run_product(
        ctx,
        "transcribe-install",
        "transcribe-install.txt",
        ["./transcribe", "install"],
        cwd=INSTALL_HOME,
        timeout=INSTALL_TIMEOUT_SECONDS,
        sudo=False,
        input="\n".join(answers) + "\n",
    )
    if not result.ok:
        return StageResult("transcribe-install", False, result.detail, INSTALL_FIX)
    env = vm.ssh(ctx, f"test -f {shlex.quote(INSTALL_HOME + '/.env')}")
    if env.returncode != 0:
        return _failure("transcribe-install", f"Transcribe install did not leave {INSTALL_HOME}/.env", INSTALL_FIX)

    recorded = _set_env_line(ctx, ".env", "RELEASE_TAG", tag)
    if recorded is not None:
        return _failure("transcribe-install", recorded, INSTALL_FIX)
    # With directory sign-in off the tag writes neither file its Compose
    # secrets name, and a secret whose file is missing stops the whole stack;
    # both are written empty, as the install writes its mail and backup ones.
    # The same script gives the self-signed key to the account Caddy runs as.
    placed = vm.ssh(ctx, _INSTALL_GAPS_SCRIPT, timeout=TAG_TIMEOUT_SECONDS)
    if placed.returncode != 0:
        return _failure(
            "transcribe-install", f"writing the directory secrets and key owner failed: {command_detail(placed)}", INSTALL_FIX,
        )
    timers, _text = vm.run_product(
        ctx,
        "transcribe-install",
        "transcribe-install-timers.txt",
        ["./transcribe", "install-timers"],
        cwd=INSTALL_HOME,
        timeout=INSTALL_TIMEOUT_SECONDS,
        sudo=False,
    )
    if not timers.ok:
        return StageResult("transcribe-install", False, timers.detail, INSTALL_FIX)
    return StageResult(
        "transcribe-install",
        True,
        f"blanked COMPOSE_PROFILES in .env.example; ./transcribe install passed; "
        f"wrote RELEASE_TAG={tag} to .env; {placed.stdout.strip() or 'no directory secret written'}; "
        "./transcribe install-timers passed",
        "",
    )


def up(ctx: HarnessContext) -> StageResult:
    """Start Transcribe, compare Compose's running images, then try its Local admin command."""

    selected = ctx.transcribe_images
    if not selected:
        return _failure("transcribe-up", "the select stage did not choose Transcribe images", UP_FIX)
    started, _text = vm.run_product(
        ctx,
        "transcribe-up",
        "transcribe-up.txt",
        ["docker", "compose", "up", "-d"],
        cwd=INSTALL_HOME,
        timeout=UP_TIMEOUT_SECONDS,
        sudo=False,
    )
    if not started.ok:
        return StageResult("transcribe-up", False, started.detail, UP_FIX)
    listing = vm.ssh(ctx, f"cd {shlex.quote(INSTALL_HOME)} && docker compose ps --all --format json", timeout=TAG_TIMEOUT_SECONDS)
    if listing.returncode != 0:
        return _failure("transcribe-up", f"docker compose ps exited {listing.returncode}", UP_FIX)
    rows = stack.parse_ps(listing.stdout)
    if rows is None:
        return _failure("transcribe-up", "docker compose ps did not return parseable JSON", UP_FIX)
    project_rows = tuple(row for row in rows if row.get("Project") == PROJECT)
    observed_services: set[str] = set()
    for row in project_rows:
        service = str(row.get("Service", "missing"))
        reference = selected.get(service)
        if reference is None:
            return _failure("transcribe-up", f"service {service} in project {PROJECT} has no selected image", UP_FIX)
        observed_services.add(service)
        if str(row.get("State", "")).lower() != "running":
            return _failure("transcribe-up", f"service {service} is not running (state {row.get('State', 'missing')})", UP_FIX)
        if row.get("Image") != reference:
            return _failure(
                "transcribe-up",
                f"service {service} uses image {row.get('Image', 'missing')}, expected {reference}",
                UP_FIX,
            )
    for service in selected:
        if service not in observed_services:
            return _failure("transcribe-up", f"service {service} is missing from project {PROJECT}", UP_FIX)

    password = secrets.token_urlsafe(32)
    admin = vm.ssh(
        ctx,
        f"cd {shlex.quote(INSTALL_HOME)} && docker compose run --rm -T app create-local-admin",
        input=f"{password}\n{password}\n",
        timeout=UP_TIMEOUT_SECONDS,
    )
    if admin.returncode != 0:
        output = f"{admin.stdout}\n{admin.stderr}"
        if "needs a terminal" not in output:
            return _failure(
                "transcribe-up",
                f"create-local-admin exited {admin.returncode} and did not report its stdin/terminal refusal",
                UP_FIX,
            )
        admin_detail = "create-local-admin requires a terminal and refused stdin; no Local admin was created"
    else:
        admin_detail = "create-local-admin accepted the stdin password"
    return StageResult(
        "transcribe-up",
        True,
        f"docker compose up passed; all {len(selected)} services in project {PROJECT} are running with selected images; {admin_detail}",
        "",
    )


def _poll(
    ctx: HarnessContext,
    transcript_name: str,
    command: list[str],
    *,
    retry: Callable[[StageResult], bool],
    cwd: str = vm.VM_CHECKOUT,
    sudo: bool = True,
) -> tuple[bool, StageResult | None, str, Path]:
    """Run *command* until it passes or HEALTH_TIMEOUT_SECONDS runs out.

    One transcript per stage, the last attempt's: a poll of hundreds of
    attempts would otherwise leave hundreds of files. Returns whether it
    passed, the failure that stopped retrying early, the last text, and the
    transcript's path.
    """

    deadline = ctx.clock() + HEALTH_TIMEOUT_SECONDS
    transcript_path = ctx.spec.out / f"{ctx.stage_index:02d}-{transcript_name}"
    last_text = ""
    while True:
        remaining = deadline - ctx.clock()
        if remaining <= 0:
            return False, None, last_text, transcript_path
        if transcript_path in ctx.transcripts:
            ctx.transcripts[:] = [path for path in ctx.transcripts if path != transcript_path]
        result, last_text = vm.run_product(
            ctx, "health", transcript_name, command, cwd=cwd, timeout=remaining, sudo=sudo,
        )
        if result.ok:
            return True, None, last_text, transcript_path
        if not retry(result):
            return False, _failure("health", result.detail, HEALTH_FIX), last_text, transcript_path
        remaining = deadline - ctx.clock()
        if remaining > 0:
            ctx.sleep(min(HEALTH_POLL_SECONDS, remaining))


def _transcribe_check(ctx: HarnessContext) -> tuple[str | None, StageResult | None]:
    """Poll Transcribe's check while Compose services return after a restart."""

    passed, stopped, last_text, transcript_path = _poll(
        ctx, "transcribe-check.txt", ["./transcribe", "check"], cwd=INSTALL_HOME, sudo=False,
        # A timeout, an unrunnable command, or a dropped SSH session is no
        # service still starting.
        retry=lambda result: not any(f"exited {code};" in result.detail for code in (124, 126, 127, 255)),
    )
    if passed:
        return "./transcribe check passed", None
    if stopped is not None:
        return None, stopped
    failed = tuple(line.strip() for line in last_text.splitlines() if line.strip().startswith("FAIL"))
    detail = f"./transcribe check did not exit 0 within {HEALTH_TIMEOUT_SECONDS} s"
    if failed:
        detail += "; last attempt: " + " | ".join(failed)
    detail += f"; transcript {transcript_path}"
    return None, _failure("health", detail, HEALTH_FIX)


def _transcribe_tls(ctx: HarnessContext) -> tuple[str | None, StageResult | None]:
    """Compare the certificate served on Transcribe's port with its VM leaf."""

    if ctx.address is None:
        return None, _failure("health", "the VM address is unavailable for the Transcribe TLS probe", HEALTH_FIX)
    tls_root = Path(INSTALL_HOME) / "tls"
    root_source = tls_root / "self-signed-root.pem"
    cert_source = tls_root / "cert.pem"
    local_root = ctx.spec.run_dir / "transcribe" / "tls" / "self-signed-root.pem"
    local_cert = ctx.spec.run_dir / "transcribe" / "tls" / "cert.pem"
    try:
        ctx.host.mkdir(local_root.parent, mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return None, _failure("health", f"could not create Transcribe TLS directory {local_root.parent}: {exc}", HEALTH_FIX)
    for source, destination in ((root_source, local_root), (cert_source, local_cert)):
        read, contents = vm.read_file(ctx, source, timeout=TAG_TIMEOUT_SECONDS)
        if not read.ok or contents is None:
            return None, _failure("health", f"could not read Transcribe TLS file {source}: {read.detail}", HEALTH_FIX)
        try:
            ctx.host.write_text(destination, contents, mode=0o600)
        except OSError as exc:
            return None, _failure("health", f"could not write Transcribe TLS file {destination}: {exc}", HEALTH_FIX)
    ctx.transcribe_root = local_root
    served = tls.served_fingerprint(
        ctx.host,
        connect=f"{ctx.address}:{HTTPS_PORT}",
        hostname=HOSTNAME,
        cafile=local_root,
    )
    if isinstance(served, tls.Problem):
        return None, _failure("health", served.problem, served.fix)
    placed = tls.file_fingerprint(ctx.host, local_cert)
    if isinstance(placed, tls.Problem):
        return None, _failure("health", placed.problem, placed.fix)
    if served != placed:
        return None, _failure(
            "health",
            f"Transcribe serves certificate fingerprint {served}, not its VM leaf {placed}",
            HEALTH_FIX,
        )
    return "Transcribe serves its VM leaf certificate on port 8443", None


def _transcribe_paths(ctx: HarnessContext) -> tuple[str | None, StageResult | None]:
    """Read the data directories and timer units, including data ownership."""

    directories = tuple(Path(DATA_DIR) / name for name in ("", "scratch", "cases", "uploads"))
    units = tuple(Path("/etc/systemd/system") / name for name in TIMER_UNITS)
    expected = {str(path): "directory" for path in directories}
    expected.update({str(path): "file" for path in units})
    arguments = " ".join(shlex.quote(path) for path in expected)
    script = (
        f"for path in {arguments}; do "
        "if test -d \"$path\"; then "
        "owner=$(stat -c %U -- \"$path\") || exit; printf 'directory %s %s\\n' \"$path\" \"$owner\"; "
        "elif test -f \"$path\"; then printf 'file %s\\n' \"$path\"; "
        "else printf 'missing %s\\n' \"$path\"; fi; done"
    )
    reading = vm.run_as_root(ctx, f"sh -c {shlex.quote(script)}", timeout=TAG_TIMEOUT_SECONDS)
    if reading.returncode != 0:
        return None, _failure("health", f"Transcribe path read failed: {command_detail(reading)}", HEALTH_FIX)
    observed: dict[str, tuple[str, str | None]] = {}
    for line in reading.stdout.splitlines():
        fields = line.split(" ")
        if fields[0] == "directory" and len(fields) == 3:
            observed[fields[1]] = (fields[0], fields[2])
        elif fields[0] in ("file", "missing") and len(fields) == 2:
            observed[fields[1]] = (fields[0], None)
        else:
            return None, _failure("health", f"Transcribe path read returned an invalid row: {line}", HEALTH_FIX)
    if set(observed) != set(expected):
        return None, _failure("health", "Transcribe path read did not name every expected path", HEALTH_FIX)
    for path, kind in expected.items():
        actual_kind, owner = observed[path]
        if actual_kind != kind:
            return None, _failure("health", f"Transcribe path {path} is missing or has the wrong type", HEALTH_FIX)
        if kind == "directory" and owner != APP_USER:
            return None, _failure("health", f"Transcribe directory {path} is owned by {owner}, expected {APP_USER}", HEALTH_FIX)
    return f"{len(directories)} Transcribe data directories and {len(units)} timer units are present", None


def _gideon_status(ctx: HarnessContext) -> tuple[str | None, StageResult | None]:
    """Poll GIDEON's status to the bound: a fresh install's alerts are evaluated
    before their first scrape and clear once it lands."""

    passed, stopped, last_text, transcript_path = _poll(
        ctx, "gideon-status.txt", ["python3", "-m", "gideon", "status"],
        # Exit 1 is a firing page, which a later scrape may clear; any other
        # exit is the command's own failure.
        retry=lambda result: "exited 1;" in result.detail,
    )
    if passed:
        return "GIDEON status passed", None
    if stopped is not None:
        return None, stopped
    firing = tuple(line.split(" — ")[0] for line in last_text.splitlines() if "(firing " in line)
    detail = f"GIDEON status did not exit 0 within {HEALTH_TIMEOUT_SECONDS} s"
    if firing:
        detail += "; firing: " + " | ".join(firing)
    detail += f"; transcript {transcript_path}"
    return None, _failure("health", detail, HEALTH_FIX)


def push(ctx: HarnessContext) -> StageResult:
    """Push the install's backup set to the VM's loopback target, as an office's first night does.

    Without it the push-overdue page fires by construction on a VM no form
    has pushed from yet, and the health read would judge the VM, not the pair.
    """

    result, _text = vm.run_product(
        ctx,
        "push",
        "push.txt",
        ["python3", "-m", "gideon", "backup", "push"],
        timeout=PUSH_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return StageResult("push", False, result.detail, PUSH_FIX)
    return StageResult("push", True, result.detail, "")


def health(ctx: HarnessContext) -> StageResult:
    """Read Transcribe health and, while installed, GIDEON health."""

    check, failure = _transcribe_check(ctx)
    if failure is not None or check is None:
        return failure or _failure("health", "Transcribe check returned no result", HEALTH_FIX)
    certificate, failure = _transcribe_tls(ctx)
    if failure is not None or certificate is None:
        return failure or _failure("health", "Transcribe TLS probe returned no result", HEALTH_FIX)
    paths, failure = _transcribe_paths(ctx)
    if failure is not None or paths is None:
        return failure or _failure("health", "Transcribe path read returned no result", HEALTH_FIX)

    docker, failure = _docker_reading(ctx, allow_absent=False, stage="health")
    if failure is not None or docker is None:
        return failure or _failure("health", "Docker reading is unavailable", DOCKER_FIX)
    previous = ctx.docker_reading
    ctx.docker_reading = docker
    changes: list[str] = []
    if previous is not None and previous[0] != docker[0]:
        changes.append(f"Docker version changed from {previous[0]} to {docker[0]}")
    if previous is not None and previous[1] != docker[1]:
        changes.append("Docker restarted since its previous reading")
    docker_detail = "; ".join(changes) if changes else "Docker version and start time unchanged"

    findings = [check, certificate, paths]
    if ctx.install_url is not None:
        status, failure = _gideon_status(ctx)
        if failure is not None or status is None:
            return failure or _failure("health", "GIDEON status returned no result", HEALTH_FIX)
        services = fullrestore.services_health(ctx, "health")
        if not services.ok:
            return services
        findings.extend((status, services.detail))
    findings.append(docker_detail)
    return StageResult("health", True, "; ".join(findings), "")


def first_provision(ctx: HarnessContext) -> StageResult:
    """Provision GIDEON and recognize only its populated default-store refusal."""

    result = vm.provision(ctx, allow_blocked=True, acknowledge=True)
    if result.ok:
        return result
    if not ctx.transcripts:
        return result
    transcript = ctx.transcripts[-1]
    try:
        text = ctx.host.read_text(transcript)
    except (OSError, UnicodeError):
        return result
    rows = vm.row_details(text)
    failures = tuple((name, detail) for name, (outcome, detail) in rows.items() if outcome == "failed")
    expected = (
        f"containerd store is populated under {docker_step._CONTAINERD_DEFAULT_ROOT} "
        f"but empty under {docker_step._CONTAINERD_ROOT}"
    )
    if len(failures) != 1 or failures[0][0] != "docker-engine" or expected not in failures[0][1]:
        return result
    ctx.store_refusal = failures[0][1]
    return StageResult(
        "provision",
        True,
        f"accepted the first-provision finding: {ctx.store_refusal}; following the containerd store move",
        "",
    )


def store_move(ctx: HarnessContext) -> StageResult:
    """Follow the one-VM containerd store move when provision found its default root populated."""

    if ctx.store_refusal is None:
        return StageResult("store-move", True, "nothing to move", "")
    try:
        script = ctx.host.read_text(FIXTURE_DIR / "store-move.sh")
    except (OSError, UnicodeError) as exc:
        return _failure("store-move", f"could not read the store-move script: {exc}", STORE_MOVE_FIX)
    copied = vm.copy_in(ctx, STORE_MOVE_SCRIPT, script, 0o700, as_root=True)
    if not copied.ok:
        return StageResult("store-move", False, copied.detail, STORE_MOVE_FIX)
    moved, text = vm.run_product(
        ctx,
        "store-move",
        "store-move.txt",
        ["bash", STORE_MOVE_SCRIPT],
        cwd=vm.VM_CHECKOUT,
        timeout=STORE_MOVE_TIMEOUT_SECONDS,
    )
    if not moved.ok:
        return StageResult("store-move", False, moved.detail, STORE_MOVE_FIX)
    counts = STORE_COUNTS.search(text)
    if counts is None or "lists equal" not in text.splitlines():
        return _failure("store-move", "the store-move transcript lacks matching counts or equal lists", STORE_MOVE_FIX)
    return StageResult(
        "store-move",
        True,
        f"finding: {ctx.store_refusal}; counts inode={counts.group(1)}/{counts.group(2)} "
        f"bytes={counts.group(3)}/{counts.group(4)} whiteouts={counts.group(5)}/{counts.group(6)} "
        f"opaque={counts.group(7)}/{counts.group(8)}; image and container lists read equal",
        "",
    )


def provision_after_move(ctx: HarnessContext) -> StageResult:
    """Repeat GIDEON's provision only after the recorded containerd store move."""

    if ctx.store_refusal is None:
        return StageResult("provision-moved", True, "not needed", "")
    result = vm.provision(ctx, allow_blocked=False, acknowledge=True)
    if not result.ok:
        return StageResult("provision-moved", False, result.detail, result.fix or STORE_MOVE_FIX)
    return StageResult("provision-moved", True, "GIDEON provision passed after the containerd store move", "")


def pair_size(used_bytes: int, load_one: float) -> tuple[int, int]:
    """Return a starting size: memory rounds 2x use to 4 GiB; heavy load gets 2x rounded-up load vCPUs."""

    if used_bytes < 0 or not math.isfinite(load_one) or load_one < 0:
        raise ValueError("memory use and one-minute load must be finite and non-negative")
    four_gib = 4 * 1024**3
    memory_mib = max(
        16 * 1024,
        ((2 * used_bytes + four_gib - 1) // four_gib) * 4 * 1024,
    )
    vcpus = (
        max(PAIR_VCPUS, math.ceil(load_one) * 2)
        if load_one > PAIR_VCPUS / 2
        else PAIR_VCPUS
    )
    return vcpus, memory_mib


def measure(ctx: HarnessContext) -> StageResult:
    """Record both projects' VM usage and propose the pair VM's starting size."""

    script = "\n".join(
        (
            "set -euo pipefail",
            "printf '%s\\n' '== free -b =='",
            "free -b",
            "printf '%s\\n' '== meminfo =='",
            "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo",
            "printf '%s\\n' '== loadavg =='",
            "cat /proc/loadavg",
            "printf '%s\\n' '== nproc =='",
            "nproc",
            "printf '%s\\n' '== docker stats =='",
            f"gideon_ids=$(docker ps -q --filter label=com.docker.compose.project={shlex.quote(host_compose.PROJECT_NAME)})",
            f"transcribe_ids=$(docker ps -q --filter label=com.docker.compose.project={shlex.quote(PROJECT)})",
            "containers=()",
            "while IFS= read -r container; do [[ -z \"$container\" ]] || containers+=(\"$container\"); done <<< \"$gideon_ids\"",
            "while IFS= read -r container; do [[ -z \"$container\" ]] || containers+=(\"$container\"); done <<< \"$transcribe_ids\"",
            "printf 'container-count %s\\n' \"${#containers[@]}\"",
            "if (( ${#containers[@]} )); then docker stats --no-stream \"${containers[@]}\"; fi",
            "printf '%s\\n' '== disk usage =='",
            "df -B1 / /data /var/lib/docker",
        )
    )
    result, text = vm.run_product(
        ctx,
        "measure",
        "measure.txt",
        ["bash", "-o", "pipefail", "-c", script],
        timeout=MEASURE_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return StageResult("measure", False, result.detail, result.fix or MEASURE_FIX)
    total_match = _MEM_TOTAL.search(text)
    available_match = _MEM_AVAILABLE.search(text)
    load_match = _LOAD_AVERAGE.search(text)
    containers_match = _CONTAINER_COUNT.search(text)
    if total_match is None or available_match is None or load_match is None or containers_match is None:
        return _failure("measure", f"measure transcript lacks memory, load, or container readings: {result.detail}", MEASURE_FIX)
    total_bytes = int(total_match.group(1)) * 1024
    available_bytes = int(available_match.group(1)) * 1024
    if available_bytes > total_bytes:
        return _failure("measure", "MemAvailable exceeds MemTotal in the measure transcript", MEASURE_FIX)
    used_bytes = total_bytes - available_bytes
    load_one = float(load_match.group(1))
    container_count = int(containers_match.group(1))
    try:
        proposed_vcpus, proposed_memory_mib = pair_size(used_bytes, load_one)
    except ValueError as exc:
        return _failure("measure", f"measure values cannot size the pair VM: {exc}", MEASURE_FIX)
    current_memory = f"{ctx.spec.memory_mib / 1024:g} GiB"
    proposed_memory = f"{proposed_memory_mib / 1024:g} GiB"
    return StageResult(
        "measure",
        True,
        f"used {used_bytes} bytes; available {available_bytes} bytes; total {total_bytes} bytes; "
        f"one-minute load {load_one:g}; {container_count} containers; VM size "
        f"{ctx.spec.vcpus} vCPUs, {current_memory}; size rule proposes "
        f"{proposed_vcpus} vCPUs, {proposed_memory}; transcript {ctx.spec.out / f'{ctx.stage_index:02d}-measure.txt'}",
        "",
    )


def uninstall(ctx: HarnessContext) -> StageResult:
    """Run the plain GIDEON uninstall while preserving Transcribe's data path."""

    result = cotenant.uninstall_stage(ctx, purge=False, cotenant_data=DATA_DIR)
    if result.ok:
        ctx.install_url = None
    return result


def purge(ctx: HarnessContext) -> StageResult:
    """Run GIDEON's purge while preserving Transcribe's data path."""

    return cotenant.uninstall_stage(ctx, purge=True, cotenant_data=DATA_DIR)

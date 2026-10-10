"""Synthetic co-tenant acceptance stages and their VM state checks."""

import json
import shlex
from pathlib import Path
from typing import Final

import gideon.host.uninstall
from gideon.host import cotenants
from gideon.host.report import Problem, StageResult
from gideon.host.steps import disk, docker, network
from gideon.host.steps.command import COMMAND_PATH
from gideon.host.steps.site_dirs import ETC_GIDEON
from tools.acceptance import rehearsal, vm
from tools.acceptance.context import HarnessContext

NAME: Final = "cotenant"
CONTAINER: Final = "cotenant-app"
INSTALL_HOME: Final = "/opt/cotenant"
DATA_DIR: Final = "/data/cotenant"
MARKER_TEXT: Final = "cotenant data"
KEY: Final = "max-concurrent-downloads"
KEY_VALUE: Final = 3
RULE_COMMENT: Final = "cotenant"
RULE_CIDR: Final = "192.0.2.0/24"
RULE_UNIT: Final = "cotenant-rule.service"
TIMER: Final = "cotenant-tick.timer"
FIXTURE_DIR: Final = Path(__file__).with_suffix("")
STAGING: Final = f"{INSTALL_HOME}/fixture"
DATA_DISK: Final = "/dev/sdb"
FOREIGN_SOURCE: Final = "/etc/apt/sources.list.d/cotenant-docker.list"
CONTAINER_WAIT_SECONDS: Final = 300
ARRIVAL_TIMEOUT_SECONDS: Final = 1800
COTENANT_FIX: Final = "Inspect the cotenant arrival transcript and VM, then retry acceptance."
INTACT_FIX: Final = "Inspect the cotenant state in the VM, then retry acceptance."
REFUSAL_FIX: Final = "Inspect the refusal transcript and VM, then retry acceptance."
UPGRADE_FIX: Final = "Inspect the upgrade transcript and VM, then retry acceptance."
UNINSTALL_FIX: Final = "Inspect the uninstall transcript and VM, then retry acceptance."
UNINSTALL_TIMEOUT_SECONDS: Final = 1800

_DATA_PATHS: Final = tuple(disk.DATA_MOUNT / name for name in disk.data_directories())
_DROPIN_PATHS: Final = (
    *(item.path for item in gideon.host.uninstall.KEPT_DROP_INS),
    *(item.path for item in gideon.host.uninstall.PURGED_DROP_INS),
)
_KEPT_AFTER_PLAIN: Final = (ETC_GIDEON, *_DATA_PATHS)
_ABSENT_AFTER_PLAIN: Final = (COMMAND_PATH,)
_ABSENT_AFTER_PURGE: Final = (
    *_KEPT_AFTER_PLAIN, COMMAND_PATH, *(item.path for item in gideon.host.uninstall.PURGED_DROP_INS),
)
_KEPT_AFTER_PURGE: Final = (disk.DATA_MOUNT, Path(DATA_DIR))


def _red(stage: str, detail: str, fix: str) -> StageResult:
    return StageResult(stage, False, detail, fix)


def _root_read(
    ctx: HarnessContext, stage: str, script: str, label: str, fix: str,
) -> tuple[str | None, StageResult | None]:
    result = vm.run_as_root(ctx, script)
    if result.returncode != 0:
        if result.returncode in (126, 127):
            tool = shlex.split(script)[0]
            return None, _red(
                stage, f"{label} could not run (exit {result.returncode})",
                f"Repair {tool} in the VM, then retry acceptance.",
            )
        return None, _red(stage, f"{label} exited {result.returncode}", fix)
    return result.stdout, None


def _intact(ctx: HarnessContext) -> StageResult:
    """Read and judge the container, key, data, rule, timer, and mount."""

    if not ctx.cotenant_arrived:
        return StageResult("intact", False, "cotenant arrival has not run", INTACT_FIX)

    # After a reboot or an acknowledged Docker restart the daemon, then the
    # restart policy, bring the container back: an unanswered inspect is polled
    # within the bound like a status that is not yet running.
    deadline = ctx.clock() + CONTAINER_WAIT_SECONDS
    while True:
        container = vm.run_as_root(
            ctx, f"docker inspect -f '{{{{.State.Status}}}}' {shlex.quote(CONTAINER)}"
        )
        status = (
            container.stdout.strip() or "unreported"
            if container.returncode == 0
            else f"not inspectable (exit {container.returncode})"
        )
        if status == "running":
            break
        if ctx.clock() >= deadline:
            return StageResult(
                "intact", False,
                f"{CONTAINER} is {status} after {CONTAINER_WAIT_SECONDS} s", INTACT_FIX,
            )
        ctx.sleep(vm.POLL_INTERVAL_SECONDS)

    settings = vm.run_as_root(ctx, "cat /etc/docker/daemon.json")
    if settings.returncode != 0:
        return StageResult("intact", False, "could not read /etc/docker/daemon.json", INTACT_FIX)
    try:
        values = json.loads(settings.stdout)
    except json.JSONDecodeError:
        return StageResult("intact", False, "/etc/docker/daemon.json is not valid JSON", INTACT_FIX)
    if not isinstance(values, dict):
        return StageResult("intact", False, "/etc/docker/daemon.json is not an object", INTACT_FIX)
    if KEY not in values:
        return StageResult("intact", False, f"Docker key {KEY} is absent", INTACT_FIX)
    # A bool equals 1 or 0 in Python, so the type is read before the value.
    if type(values[KEY]) is not int or values[KEY] != KEY_VALUE:
        return StageResult(
            "intact", False, f"Docker key {KEY} is {values[KEY]!r}, not {KEY_VALUE}", INTACT_FIX
        )

    marker = vm.run_as_root(ctx, f"cat {shlex.quote(DATA_DIR)}/marker")
    if marker.returncode != 0:
        return StageResult("intact", False, f"could not read {DATA_DIR}/marker", INTACT_FIX)
    if marker.stdout != f"{MARKER_TEXT}\n":
        return StageResult("intact", False, f"{DATA_DIR}/marker changed", INTACT_FIX)

    rules = vm.run_as_root(ctx, "iptables -w -S DOCKER-USER")
    if rules.returncode != 0:
        return StageResult("intact", False, "could not read DOCKER-USER", INTACT_FIX)
    if not any(
        line.startswith("-A DOCKER-USER ")
        and f"-s {RULE_CIDR}" in line
        and "-m comment" in line
        and f"--comment {RULE_COMMENT}" in line
        and "-j RETURN" in line
        for line in rules.stdout.splitlines()
    ):
        return StageResult("intact", False, f"DOCKER-USER lacks the {RULE_COMMENT} rule", INTACT_FIX)

    for verb, expected in (("is-enabled", "enabled"), ("is-active", "active")):
        timer = vm.run_as_root(ctx, f"systemctl {verb} {shlex.quote(TIMER)}")
        if timer.returncode != 0 or timer.stdout.strip() != expected:
            return StageResult(
                "intact", False, f"{TIMER} {verb} is {timer.stdout.strip() or 'unreported'}", INTACT_FIX
            )

    mount = vm.run_as_root(ctx, "findmnt -rn -o TARGET --mountpoint /data")
    if mount.returncode != 0 or mount.stdout.strip() != "/data":
        return StageResult(
            "intact", False,
            f"/data is not mounted (findmnt exit {mount.returncode})", INTACT_FIX,
        )

    return StageResult(
        "intact", True,
        f"{CONTAINER} running; {KEY}={KEY_VALUE}; {DATA_DIR}/marker intact; "
        f"{RULE_COMMENT} rule present; {TIMER} enabled and active; /data mounted",
        "",
    )


def arrive(ctx: HarnessContext) -> StageResult:
    """Stage and run the fixture, then read its state once."""

    if ctx.spec.cotenant not in ("before", "after"):
        return StageResult("cotenant", False, "co-tenant form is not selected", COTENANT_FIX)
    created = vm.run_as_root(ctx, f"install -d {shlex.quote(STAGING)}")
    if created.returncode != 0:
        return StageResult("cotenant", False, f"could not create {STAGING}", COTENANT_FIX)
    for filename in (
        "arrive.sh", "compose.yaml", "cotenant-rule.service",
        "cotenant-tick.service", "cotenant-tick.timer",
    ):
        try:
            content = ctx.host.read_text(FIXTURE_DIR / filename)
        except (OSError, UnicodeError) as exc:
            return StageResult("cotenant", False, f"could not read fixture {filename}: {exc}", COTENANT_FIX)
        copied = vm.copy_in(
            ctx, f"{STAGING}/{filename}", content,
            0o755 if filename == "arrive.sh" else 0o644, as_root=True,
        )
        if not copied.ok:
            return StageResult("cotenant", False, copied.detail, COTENANT_FIX)

    verb = "first" if ctx.spec.cotenant == "before" else "later"
    argv = ["bash", "arrive.sh", verb]
    if verb == "first":
        argv.append(DATA_DISK)
    ran, _text = vm.run_product(
        ctx, "cotenant", "cotenant-arrive.txt", argv, cwd=STAGING,
        timeout=ARRIVAL_TIMEOUT_SECONDS,
    )
    if not ran.ok:
        return ran
    ctx.cotenant_arrived = True
    checked = _intact(ctx)
    if not checked.ok:
        return StageResult("cotenant", False, checked.detail, COTENANT_FIX)
    version = vm.run_as_root(ctx, "docker --version")
    if version.returncode != 0 or not version.stdout.strip():
        return StageResult("cotenant", False, "could not read Docker version", COTENANT_FIX)
    return StageResult(
        "cotenant", True,
        f"{verb} arrival; {version.stdout.strip()}; {checked.detail}", "",
    )


def intact(ctx: HarnessContext) -> StageResult:
    """Report whether the arrived co-tenant still works."""

    return _intact(ctx)


def _failed_step(
    ctx: HarnessContext, stage: str, step: str, transcript: str,
    required: tuple[str, ...],
) -> StageResult:
    argv = ["python3", "-m", "gideon", "host", "provision", "--no-gpu", "--only", step]
    ran, text = vm.run_product(
        ctx, stage, transcript, argv, timeout=vm.PROVISION_TIMEOUT_SECONDS,
    )
    path = ctx.spec.out / f"{ctx.stage_index:02d}-{transcript}"
    row = vm.row_details(text).get(step)
    if ran.ok or (row is not None and row[0] in ("ok", "applied", "would-apply")):
        read = row[0] if row is not None else "absent"
        return _red(stage, f"provision did not refuse at {step}; row {read}; transcript {path}", REFUSAL_FIX)
    if "exited 1;" not in ran.detail:
        return _red(stage, f"provision did not exit 1 at {step}: {ran.detail}", REFUSAL_FIX)
    if row is None or row[0] != "failed":
        status = row[0] if row is not None else "absent"
        return _red(stage, f"{step} row is {status}, not failed; transcript {path}", REFUSAL_FIX)
    missing = tuple(name for name in required if name not in row[1])
    if missing:
        return _red(stage, f"{step} failed row omits {', '.join(missing)}; transcript {path}", REFUSAL_FIX)
    return StageResult(stage, True, f"{step} failed as required; transcript {path}", "")


def _data_refusal(ctx: HarnessContext) -> StageResult:
    stage = "refuse-data"
    before, failure = _root_read(ctx, stage, "pvs --noheadings -o pv_name", "pvs", REFUSAL_FIX)
    if failure is not None or before is None:
        return failure or _red(stage, "pvs returned no reading", REFUSAL_FIX)
    if any(name.startswith(DATA_DISK) for name in before.split()):
        return _red(stage, f"pvs already names {DATA_DISK}", REFUSAL_FIX)

    refused = _failed_step(ctx, stage, "disk-layout", "refuse-data.txt", ("1 entry", NAME))
    if not refused.ok:
        return refused

    after, failure = _root_read(ctx, stage, "pvs --noheadings -o pv_name", "pvs", REFUSAL_FIX)
    if failure is not None or after is None:
        return failure or _red(stage, "pvs returned no reading", REFUSAL_FIX)
    if after != before or any(name.startswith(DATA_DISK) for name in after.split()):
        return _red(
            stage, f"pvs changed from {before.split()} to {after.split()} or names {DATA_DISK}", REFUSAL_FIX,
        )

    fstab, failure = _root_read(
        ctx, stage, f"cat {shlex.quote(str(disk._FSTAB))}", str(disk._FSTAB), REFUSAL_FIX,
    )
    if failure is not None or fstab is None:
        return failure or _red(stage, "fstab returned no reading", REFUSAL_FIX)
    if disk._BEGIN in fstab or disk._END in fstab:
        return _red(stage, f"{disk._FSTAB} gained the GIDEON disk block", REFUSAL_FIX)

    mount = vm.run_as_root(ctx, f"findmnt -rn -o TARGET --mountpoint {shlex.quote(str(disk.DATA_MOUNT))}")
    if mount.returncode != 1:
        fix = (
            "Repair findmnt in the VM, then retry acceptance."
            if mount.returncode in (126, 127) else REFUSAL_FIX
        )
        return _red(
            stage,
            f"{disk.DATA_MOUNT} mount read exited {mount.returncode}; target {mount.stdout.strip() or 'none'}",
            fix,
        )

    listing, failure = _root_read(
        ctx, stage, f"lsblk -J -o NAME,FSTYPE,TYPE {shlex.quote(DATA_DISK)}",
        f"lsblk {DATA_DISK}", REFUSAL_FIX,
    )
    if failure is not None or listing is None:
        return failure or _red(stage, f"lsblk {DATA_DISK} returned no reading", REFUSAL_FIX)
    try:
        parsed = json.loads(listing)
    except json.JSONDecodeError:
        return _red(stage, f"lsblk {DATA_DISK} returned invalid JSON", REFUSAL_FIX)
    devices = parsed.get("blockdevices") if isinstance(parsed, dict) else None
    if not isinstance(devices, list) or len(devices) != 1 or not isinstance(devices[0], dict):
        return _red(stage, f"lsblk {DATA_DISK} did not name one disk", REFUSAL_FIX)
    disk_row = devices[0]
    if (
        disk_row.get("name") != Path(DATA_DISK).name
        or disk_row.get("type") != "disk"
        or disk_row.get("fstype")
        or disk_row.get("children")
    ):
        return _red(
            stage, f"lsblk {DATA_DISK} read name {disk_row.get('name')}, type {disk_row.get('type')}, "
            f"filesystem {disk_row.get('fstype')}, children {bool(disk_row.get('children'))}", REFUSAL_FIX,
        )
    return StageResult(stage, True, f"{refused.detail}; pvs, fstab, mount, and {DATA_DISK} unchanged", "")


def refuse_data(ctx: HarnessContext) -> StageResult:
    """Require disk layout to reject the unmounted co-tenant directory."""

    stage = "refuse-data"
    prerequisite, text = vm.run_product(
        ctx, stage, "refuse-data-service-user.txt",
        ["python3", "-m", "gideon", "host", "provision", "--no-gpu", "--only", "service-user"],
        timeout=vm.PROVISION_TIMEOUT_SECONDS,
    )
    if not prerequisite.ok:
        return _red(stage, prerequisite.detail, prerequisite.fix)
    service_row = vm.row_details(text).get("service-user")
    if service_row is None or service_row[0] not in ("ok", "applied"):
        read = service_row[0] if service_row is not None else "absent"
        return _red(stage, f"service-user row is {read}", REFUSAL_FIX)
    made = vm.run_as_root(ctx, f"install -d {shlex.quote(DATA_DIR)}")
    if made.returncode != 0:
        return _red(stage, f"install -d {DATA_DIR} exited {made.returncode}", REFUSAL_FIX)
    try:
        copied = vm.copy_in(ctx, f"{DATA_DIR}/marker", f"{MARKER_TEXT}\n", 0o644, as_root=True)
        outcome = _data_refusal(ctx) if copied.ok else _red(stage, copied.detail, REFUSAL_FIX)
    finally:
        cleanup = vm.run_as_root(ctx, f"rm -rf -- {shlex.quote(DATA_DIR)}")
    if cleanup.returncode != 0:
        return _red(stage, f"cleanup of {DATA_DIR} exited {cleanup.returncode}; {outcome.detail}", REFUSAL_FIX)
    return outcome


def _source_refusal(ctx: HarnessContext) -> StageResult:
    stage = "refuse-source"
    hashes_command = f"sha256sum {shlex.quote(str(docker._SOURCE))} {shlex.quote(str(docker._KEYRING))}"
    before_hashes, failure = _root_read(ctx, stage, hashes_command, "Docker recipe hashes", REFUSAL_FIX)
    if failure is not None or before_hashes is None:
        return failure or _red(stage, "Docker recipe hashes returned no reading", REFUSAL_FIX)
    before_start, failure = _root_read(
        ctx, stage, "systemctl show -p ActiveEnterTimestamp docker", "Docker active timestamp", REFUSAL_FIX,
    )
    if failure is not None or before_start is None:
        return failure or _red(stage, "Docker active timestamp returned no reading", REFUSAL_FIX)

    refused = _failed_step(
        ctx, stage, "docker-engine", "refuse-source.txt",
        (str(docker._SOURCE), FOREIGN_SOURCE),
    )
    if not refused.ok:
        return refused
    after_hashes, failure = _root_read(ctx, stage, hashes_command, "Docker recipe hashes", REFUSAL_FIX)
    if failure is not None or after_hashes is None:
        return failure or _red(stage, "Docker recipe hashes returned no reading", REFUSAL_FIX)
    if after_hashes != before_hashes:
        return _red(stage, f"{docker._SOURCE} or {docker._KEYRING} changed", REFUSAL_FIX)
    after_start, failure = _root_read(
        ctx, stage, "systemctl show -p ActiveEnterTimestamp docker", "Docker active timestamp", REFUSAL_FIX,
    )
    if failure is not None or after_start is None:
        return failure or _red(stage, "Docker active timestamp returned no reading", REFUSAL_FIX)
    if after_start != before_start:
        return _red(
            stage, f"Docker ActiveEnterTimestamp changed from {before_start.strip()} "
            f"to {after_start.strip()}", REFUSAL_FIX,
        )
    survived = _intact(ctx)
    if not survived.ok:
        return _red(stage, survived.detail, REFUSAL_FIX)
    return StageResult(stage, True, f"{refused.detail}; Docker recipe, start time, and {CONTAINER} unchanged", "")


def refuse_source(ctx: HarnessContext) -> StageResult:
    """Require a second Docker repository entry to refuse without change."""

    stage = "refuse-source"
    codename, failure = _root_read(
        ctx, stage, "sh -c '. /etc/os-release; printf %s \"$VERSION_CODENAME\"'",
        "Ubuntu codename", REFUSAL_FIX,
    )
    if failure is not None or codename is None:
        return failure or _red(stage, "Ubuntu codename returned no reading", REFUSAL_FIX)
    if not codename.strip():
        return _red(stage, "Ubuntu codename is empty", REFUSAL_FIX)
    entry = (
        f"deb [signed-by={docker._KEYRING}] {docker._REPOSITORY} "
        f"{codename.strip()} stable\n"
    )
    try:
        copied = vm.copy_in(ctx, FOREIGN_SOURCE, entry, 0o644, as_root=True)
        outcome = _source_refusal(ctx) if copied.ok else _red(stage, copied.detail, REFUSAL_FIX)
    finally:
        cleanup = vm.run_as_root(ctx, f"rm -f -- {shlex.quote(FOREIGN_SOURCE)}")
    if cleanup.returncode != 0:
        return _red(stage, f"cleanup of {FOREIGN_SOURCE} exited {cleanup.returncode}; {outcome.detail}", REFUSAL_FIX)
    return outcome


def upgrade(ctx: HarnessContext) -> StageResult:
    """Push one temporary tag and run one unacknowledged upgrade leg."""

    parsed = rehearsal.base_version(ctx)
    if isinstance(parsed, Problem):
        return _red("upgrade", parsed.problem, parsed.fix)
    try:
        tag = rehearsal.rc_tag(parsed)
    except ValueError as exc:
        return _red("upgrade", str(exc), UPGRADE_FIX)
    made = rehearsal.make_tag(ctx, parsed, tag)
    if made is not None:
        return _red("upgrade", made.detail, made.fix)
    ctx.base_version = parsed
    ctx.rc_tag = tag
    ran, text = vm.run_product(
        ctx, "upgrade", "cotenant-upgrade.txt", ["./upgrade.sh", tag],
        timeout=rehearsal.REHEARSAL_TIMEOUT_SECONDS,
    )
    if not ran.ok:
        return ran
    path = ctx.spec.out / f"{ctx.stage_index:02d}-cotenant-upgrade.txt"
    if "refuse" in vm.row_outcomes(text).values():
        return _red("upgrade", f"upgrade has a refusal row; transcript {path}", UPGRADE_FIX)
    return StageResult("upgrade", True, f"upgraded to {tag}; transcript {path}", "")


def _marked_volumes(
    ctx: HarnessContext, stage: str,
) -> tuple[tuple[str, ...] | None, StageResult | None]:
    """GIDEON's volumes by their Compose project label, as uninstall finds them."""

    text, failure = _root_read(
        ctx, stage, shlex.join(cotenants.volume_ls_argv()), "Docker volumes", UNINSTALL_FIX,
    )
    if failure is not None or text is None:
        return None, failure or _red(stage, "Docker volumes returned no reading", UNINSTALL_FIX)
    return tuple(sorted(row.name for row in cotenants.parse_rows(text) if cotenants.is_marked(row))), None


def _presence(
    ctx: HarnessContext, stage: str, paths: tuple[Path, ...],
) -> tuple[dict[str, bool] | None, StageResult | None]:
    """Read path presence in one VM shell loop, with one named line per path."""

    if not paths:
        return {}, None
    arguments = " ".join(shlex.quote(str(path)) for path in paths)
    script = (
        f"for path in {arguments}; do "
        "if test -e \"$path\"; then printf 'present %s\\n' \"$path\"; "
        "else printf 'absent %s\\n' \"$path\"; fi; done"
    )
    text, failure = _root_read(ctx, stage, f"sh -c {shlex.quote(script)}", "path presence", UNINSTALL_FIX)
    if failure is not None or text is None:
        return None, failure or _red(stage, "path presence returned no reading", UNINSTALL_FIX)
    found: dict[str, bool] = {}
    for line in text.splitlines():
        state, separator, path = line.partition(" ")
        if not separator or state not in ("present", "absent") or path in found:
            return None, _red(stage, f"path presence has an invalid row: {state or 'empty'}", UNINSTALL_FIX)
        found[path] = state == "present"
    expected = {str(path) for path in paths}
    if set(found) != expected:
        return None, _red(stage, f"path presence named {len(found)} of {len(expected)} paths", UNINSTALL_FIX)
    return found, None


def _uninstall_rows(stage: str, text: str) -> StageResult | None:
    # The closing line prints only after every stage's row is ok, so the rows
    # and the line together prove the whole run without restating its stages.
    rows = vm.row_outcomes(text)
    if not rows:
        return _red(stage, "uninstall printed no rows", UNINSTALL_FIX)
    for name, outcome in rows.items():
        if outcome != "ok":
            return _red(stage, f"uninstall row {name} is {outcome}, not ok", UNINSTALL_FIX)
    if "GIDEON is removed from this box." not in text.splitlines():
        return _red(stage, "uninstall closing line is absent", UNINSTALL_FIX)
    return None


def _state_after(
    ctx: HarnessContext, stage: str, *, purge: bool, volumes: tuple[str, ...],
) -> StageResult:
    before = ctx.dropins_before
    if before is None:
        return _red(stage, "drop-in presence before uninstall was not recorded", UNINSTALL_FIX)
    kept = _KEPT_AFTER_PURGE if purge else _KEPT_AFTER_PLAIN
    absent = _ABSENT_AFTER_PURGE if purge else _ABSENT_AFTER_PLAIN
    paths = tuple(dict.fromkeys((*kept, *absent, *_DROPIN_PATHS)))
    present, failure = _presence(ctx, stage, paths)
    if failure is not None or present is None:
        return failure or _red(stage, "path presence returned no reading", UNINSTALL_FIX)
    for path in kept:
        if not present[str(path)]:
            return _red(stage, f"{path} read absent, expected present", UNINSTALL_FIX)
    for path in absent:
        if present[str(path)]:
            return _red(stage, f"{path} read present, expected absent", UNINSTALL_FIX)
    for kept_item in gideon.host.uninstall.KEPT_DROP_INS:
        if present[str(kept_item.path)] != before[str(kept_item.path)]:
            return _red(
                stage, f"{kept_item.path} read {'present' if present[str(kept_item.path)] else 'absent'}, "
                f"was {'present' if before[str(kept_item.path)] else 'absent'}", UNINSTALL_FIX,
            )
    if not purge:
        for purged_item in gideon.host.uninstall.PURGED_DROP_INS:
            if present[str(purged_item.path)] != before[str(purged_item.path)]:
                return _red(
                    stage, f"{purged_item.path} read {'present' if present[str(purged_item.path)] else 'absent'}, "
                    f"was {'present' if before[str(purged_item.path)] else 'absent'}", UNINSTALL_FIX,
                )

    # Unfiltered and judged here: a pattern matching nothing is not an error
    # every systemd version reports the same way.
    units, failure = _root_read(
        ctx, stage, "systemctl list-unit-files --no-legend --no-pager", "units", UNINSTALL_FIX,
    )
    if failure is not None or units is None:
        return failure or _red(stage, "GIDEON units returned no reading", UNINSTALL_FIX)
    remaining_units = tuple(
        line.split()[0] for line in units.splitlines()
        if line.split() and cotenants.marked_name(line.split()[0])
    )
    if remaining_units:
        return _red(stage, f"GIDEON unit still present: {remaining_units[0]}", UNINSTALL_FIX)

    projects, failure = _root_read(
        ctx, stage, "docker compose ls --all --format json", "Compose projects", UNINSTALL_FIX,
    )
    if failure is not None or projects is None:
        return failure or _red(stage, "Compose projects returned no reading", UNINSTALL_FIX)
    project_names = cotenants.parse_projects(projects)
    if project_names is None:
        return _red(stage, "Compose projects listing is malformed", UNINSTALL_FIX)
    marked_projects = tuple(name for name in project_names if cotenants.marked_name(name))
    if marked_projects:
        return _red(stage, f"GIDEON project still present: {marked_projects[0]}", UNINSTALL_FIX)

    networks, failure = _root_read(
        ctx, stage, "docker network ls --format '{{.Name}}'", "Docker networks", UNINSTALL_FIX,
    )
    if failure is not None or networks is None:
        return failure or _red(stage, "Docker networks returned no reading", UNINSTALL_FIX)
    marked_networks = tuple(
        name for name in cotenants.parse_names(networks) if cotenants.marked_name(name)
    )
    if marked_networks:
        return _red(stage, f"GIDEON network still present: {marked_networks[0]}", UNINSTALL_FIX)

    marked_volumes, failure = _marked_volumes(ctx, stage)
    if failure is not None or marked_volumes is None:
        return failure or _red(stage, "Docker volumes returned no reading", UNINSTALL_FIX)
    expected_volumes = () if purge else volumes
    if marked_volumes != expected_volumes:
        return _red(
            stage, f"GIDEON volumes read {', '.join(marked_volumes) or 'none'}, "
            f"expected {', '.join(expected_volumes) or 'none'}", UNINSTALL_FIX,
        )

    chain = vm.run_as_root(ctx, f"iptables -w -S {shlex.quote(network.CHAIN)}")
    if chain.returncode != 1:
        fix = (
            "Repair iptables in the VM, then retry acceptance."
            if chain.returncode in (126, 127) else UNINSTALL_FIX
        )
        return _red(stage, f"{network.CHAIN} read exit {chain.returncode}, expected absent", fix)
    shared, failure = _root_read(
        ctx, stage, f"iptables -w -S {shlex.quote(network.SHARED_CHAIN)}",
        network.SHARED_CHAIN, UNINSTALL_FIX,
    )
    if failure is not None or shared is None:
        return failure or _red(stage, f"{network.SHARED_CHAIN} returned no reading", UNINSTALL_FIX)
    if any(network.tagged(line) for line in shared.splitlines()):
        return _red(stage, f"{network.SHARED_CHAIN} still has the {network.UFW_TAG} jump", UNINSTALL_FIX)

    if purge:
        mount = vm.run_as_root(
            ctx, f"findmnt -rn -o TARGET --mountpoint {shlex.quote(str(disk.DATA_MOUNT))}",
        )
        if mount.returncode != 0 or mount.stdout.strip() != str(disk.DATA_MOUNT):
            fix = (
                "Repair findmnt in the VM, then retry acceptance."
                if mount.returncode in (126, 127) else UNINSTALL_FIX
            )
            return _red(
                stage, f"{disk.DATA_MOUNT} mount read {mount.stdout.strip() or 'none'} "
                f"(exit {mount.returncode})", fix,
            )
    kept_volumes = "none" if purge else ", ".join(volumes)
    return StageResult(
        stage, True,
        f"{len(kept)} paths kept, {len(absent)} absent, drop-ins as before; "
        f"GIDEON volumes {kept_volumes}; no GIDEON unit, project, network, chain, or jump",
        "",
    )


def _uninstall_stage(ctx: HarnessContext, *, purge: bool) -> StageResult:
    stage = "purge" if purge else "uninstall"
    if not purge:
        present, failure = _presence(ctx, stage, _DROPIN_PATHS)
        if failure is not None or present is None:
            return failure or _red(stage, "drop-in presence returned no reading", UNINSTALL_FIX)
        volumes, failure = _marked_volumes(ctx, stage)
        if failure is not None or volumes is None:
            return failure or _red(stage, "Docker volumes returned no reading", UNINSTALL_FIX)
        if not volumes:
            return _red(stage, "no GIDEON volume before uninstall", UNINSTALL_FIX)
        ctx.dropins_before = present
        ctx.volumes_before = volumes
    if ctx.dropins_before is None or ctx.volumes_before is None:
        return _red(stage, "plain uninstall did not record its readings", UNINSTALL_FIX)
    argv = ["python3", "-m", "gideon", "uninstall", *(["--purge"] if purge else [])]
    ran, text = vm.run_product(
        ctx, stage, f"cotenant-{stage}.txt", argv, timeout=UNINSTALL_TIMEOUT_SECONDS,
    )
    if not ran.ok:
        return ran
    failed = _uninstall_rows(stage, text)
    if failed is not None:
        return failed
    checked = _state_after(ctx, stage, purge=purge, volumes=ctx.volumes_before)
    if not checked.ok:
        return checked
    transcript = ctx.spec.out / f"{ctx.stage_index:02d}-cotenant-{stage}.txt"
    return StageResult(stage, True, f"{checked.detail}; transcript {transcript}", "")


def uninstall(ctx: HarnessContext) -> StageResult:
    """Remove GIDEON's marked state and verify the plain tier's kept state."""

    return _uninstall_stage(ctx, purge=False)


def purge(ctx: HarnessContext) -> StageResult:
    """Purge GIDEON's data and verify the co-tenant and shared mount remain."""

    return _uninstall_stage(ctx, purge=True)

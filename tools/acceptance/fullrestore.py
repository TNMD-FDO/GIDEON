"""The clean-VM full-restore stages that run inside the acceptance VM."""

import shlex
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

import yaml  # type: ignore[import-untyped]

from gideon.host import backupset, nogpu, restore, stack, stages
from gideon.host.backup import parse_rsync_stats
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.report import Problem, StageResult, command_detail
from gideon.host.steps.site_dirs import AGE_IDENTITY_PATH
from gideon.host.sysio import Host
from tools.acceptance import rehearsal, seed, services, vm
from tools.acceptance.context import HARNESS_ROOT, HarnessContext

SET_FIX: Final = "Inspect the box's backup staging tree and identity, then retry acceptance."
_IDENTITY_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only age-identity, then retry acceptance."
)
FULL_RESTORE_FIX: Final = (
    "Inspect the full-restore transcripts and the acceptance VM, then retry acceptance."
)
# The restored rendered tree is the build box's, whose images are named on
# its loopback registry; the restore's one-off and store-tier containers pull
# them there, so the VM's loopback port reaches the same registry (the one
# the VM's own site names on the bridge) over the restore's SSH session.
REGISTRY_FORWARD: Final = "127.0.0.1:5000:127.0.0.1:5000"
COPY_TIMEOUT_SECONDS: Final = 3600
RESTORE_TIMEOUT_SECONDS: Final = 3600
APPLY_TIMEOUT_SECONDS: Final = 3600
BACKUP_TIMEOUT_SECONDS: Final = 3600
DRILL_TIMEOUT_SECONDS: Final = 3600
_COUNTS_SQL: Final = (
    "SELECT schemaname||'.'||relname FROM pg_stat_user_tables "
    "ORDER BY schemaname, relname;\n"
)


def _failure(detail: str, fix: str = SET_FIX) -> StageResult:
    return StageResult("set", False, detail, fix)


def _stage_failure(stage: str, detail: str, fix: str = FULL_RESTORE_FIX) -> StageResult:
    return StageResult(stage, False, detail, fix)


def _set_size(ref: backupset.SetRef) -> int:
    manifest = ref.manifest
    if manifest is None:
        return 0
    return sum(entry.size for entries in manifest.inventory.values() for entry in entries)


def _available(host: Host) -> tuple[int | None, str | None]:
    argv = ["df", "-B1", "--output=avail", str(HARNESS_ROOT)]
    try:
        result = host.run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not inspect free space under {HARNESS_ROOT}: {exc}"
    if result.returncode != 0:
        return None, f"could not inspect free space under {HARNESS_ROOT}: {command_detail(result)}"
    values = [value for value in result.stdout.split() if value.isdigit()]
    if not values:
        return None, f"could not parse free space under {HARNESS_ROOT}"
    return int(values[-1]), None


def select_set(ctx: HarnessContext) -> StageResult:
    """Select the newest complete box set and verify its restore prerequisites."""

    refs = backupset.list_sets(ctx.host)
    selected = backupset.select_set(refs)
    if isinstance(selected, Problem):
        return _failure(selected.problem, selected.fix)
    manifest = selected.manifest
    if manifest is None:
        return _failure(f"selected set {selected.label} has no manifest")

    if not ctx.host.exists(AGE_IDENTITY_PATH):
        return _failure(
            f"the box identity is missing: {AGE_IDENTITY_PATH}",
            _IDENTITY_FIX,
        )
    derive_argv = ["age-keygen", "-y", str(AGE_IDENTITY_PATH)]
    try:
        derived = ctx.host.run(derive_argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return _failure(
            f"the box identity could not be read: {AGE_IDENTITY_PATH} ({exc})",
            _IDENTITY_FIX,
        )
    if derived.returncode != 0:
        return _failure(
            f"the box identity could not be read: {AGE_IDENTITY_PATH} ({command_detail(derived)})",
            _IDENTITY_FIX,
        )
    recipient = derived.stdout.strip()
    if recipient not in manifest.recipients:
        return _failure(
            f"set {manifest.label} is not sealed to the box identity recipient",
            "Run sudo python3 -m gideon backup run --full so the newest set is sealed "
            "to the box identity, then retry acceptance.",
        )

    size = _set_size(selected)
    free_space, problem = _available(ctx.host)
    if problem is not None or free_space is None:
        return _failure(
            problem or f"could not inspect free space under {HARNESS_ROOT}",
            f"Ensure {HARNESS_ROOT} is mounted and has available space, then retry acceptance.",
        )
    required = size * 4
    if free_space < required:
        return _failure(
            f"{HARNESS_ROOT} has {free_space} bytes free, but set {manifest.label} needs "
            f"at least {required} bytes for four copies",
            f"Free space under {HARNESS_ROOT}, then retry acceptance.",
        )

    ctx.restore_set = selected
    ctx.box_recipient = recipient
    return StageResult(
        "set",
        True,
        f"selected {manifest.label} (release {manifest.release}, {len(manifest.recipients)} "
        f"recipients, {size} bytes, {free_space} bytes free)",
        "",
    )


def _now() -> datetime:
    """Return the injectable boundary for the snapshot's wall clock."""

    return datetime.now(UTC)


def _selected_manifest(ctx: HarnessContext) -> tuple[backupset.SetRef, backupset.Manifest] | None:
    selected = ctx.restore_set
    if selected is None or selected.manifest is None:
        return None
    return selected, selected.manifest


def _product_stage(
    ctx: HarnessContext,
    stage: str,
    transcript_name: str,
    command: list[str],
    *,
    timeout: float,
) -> tuple[StageResult, str]:
    result, text = vm.run_product(
        ctx,
        stage,
        transcript_name,
        command,
        timeout=timeout,
    )
    if not result.ok:
        return result, text
    refused = sorted(
        name for name, outcome in vm.row_outcomes(text).items() if outcome == "refuse"
    )
    if refused:
        transcript = ctx.spec.out / f"{ctx.stage_index:02d}-{transcript_name}"
        return _stage_failure(
            stage,
            f"{stage} transcript contains refusal row(s): {', '.join(refused)}; transcript {transcript}",
        ), text
    return result, text


def _apply(ctx: HarnessContext, transcript_name: str) -> StageResult:
    result, _text = _product_stage(
        ctx,
        "apply",
        transcript_name,
        ["python3", "-m", "gideon", "apply"],
        timeout=APPLY_TIMEOUT_SECONDS,
    )
    return result


def apply_fresh(ctx: HarnessContext) -> StageResult:
    """Apply the fresh no-GPU stack in the clean VM."""

    return _apply(ctx, "apply.txt")


def snapshot(ctx: HarnessContext) -> StageResult:
    """Place the selected box set beside the VM's own target snapshot."""

    selected = _selected_manifest(ctx)
    if selected is None:
        return _stage_failure("snapshot", "the set stage did not select a manifest")
    ref, manifest = selected
    if ctx.address is None:
        return _stage_failure("snapshot", "the VM has no address for the snapshot copy")
    now = _now()
    label = backupset.nightly_label(now)
    partial = f"{services.ACCEPTANCE_TARGET_PATH}/{label}{backupset.PARTIAL_SUFFIX}"
    final = f"{services.ACCEPTANCE_TARGET_PATH}/{label}"
    created = vm.run_as_root(
        ctx,
        f"install -d -m 0750 {shlex.quote(partial)}",
        timeout=COPY_TIMEOUT_SECONDS,
    )
    if created.returncode != 0:
        return _stage_failure(
            "snapshot",
            f"could not create partial snapshot directory: {command_detail(created)}",
        )
    destination = (
        f"{vm.CSA_ACCOUNT}@{ctx.address}:{partial}/"
    )
    rsync_argv = [
        "rsync",
        "-aH",
        "--no-owner",
        "--no-group",
        "--relative",
        "--stats",
        "-e",
        vm.ssh_option_string(ctx),
        "--rsync-path",
        "sudo rsync",
        f"{backupset.STAGING}/./sets/{ref.label}",
        f"{backupset.STAGING}/./pgbackrest",
        destination,
    ]
    try:
        copied = ctx.host.run(rsync_argv, timeout=COPY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return _stage_failure("snapshot", f"rsync snapshot copy failed: {exc}")
    if copied.returncode != 0:
        return _stage_failure(
            "snapshot", f"rsync snapshot copy failed: {command_detail(copied)}"
        )
    stats = parse_rsync_stats(copied.stdout)
    if stats is None:
        return _stage_failure(
            "snapshot",
            "rsync --stats output was missing transferred file size",
        )
    record = backupset.PushRecord(label, now, ref.label, manifest.archive_through)
    record_path = f"{partial}/{backupset.PUSH_RECORD_NAME}"
    written = vm.copy_in(
        ctx,
        record_path,
        record.to_json(),
        0o640,
        as_root=True,
        owner=f"{services.ACCEPTANCE_TARGET_USER}:{services.ACCEPTANCE_TARGET_USER}",
        timeout=COPY_TIMEOUT_SECONDS,
    )
    if not written.ok:
        return _stage_failure("snapshot", written.detail)
    # Two root commands, never one `a && b` string: run_as_root prefixes sudo
    # to the first command alone, so a chained rename ran as the CSA account.
    target_owner = f"{services.ACCEPTANCE_TARGET_USER}:{services.ACCEPTANCE_TARGET_USER}"
    for step in (
        f"chown -R {shlex.quote(target_owner)} {shlex.quote(partial)}",
        f"mv -- {shlex.quote(partial)} {shlex.quote(final)}",
    ):
        finalized = vm.run_as_root(ctx, step, timeout=COPY_TIMEOUT_SECONDS)
        if finalized.returncode != 0:
            return _stage_failure(
                "snapshot",
                f"could not finalize snapshot {label}: {command_detail(finalized)}",
            )
    ctx.snapshot_label = label
    return StageResult(
        "snapshot",
        True,
        f"snapshot {label} copied set {ref.label}; transferred {stats.transferred_bytes} bytes",
        "",
    )


def _restore_failure(ctx: HarnessContext, issue: str) -> StageResult:
    transcript = ctx.spec.out / f"{ctx.stage_index:02d}-restore.txt"
    return _stage_failure("restore", f"{issue}; transcript {transcript}")


def restore_target(ctx: HarnessContext) -> StageResult:
    """Restore the snapshot and require the rebuilt-box product rows."""

    selected = _selected_manifest(ctx)
    if selected is None or ctx.snapshot_label is None:
        return _restore_failure(ctx, "snapshot or selected set is missing")
    ref, _manifest = selected
    result, text = vm.run_product(
        ctx,
        "restore",
        "restore.txt",
        ["python3", "-m", "gideon", "restore", "--from", "target"],
        timeout=RESTORE_TIMEOUT_SECONDS,
        forwards=(REGISTRY_FORWARD,),
    )
    if not result.ok:
        return result
    details = vm.row_details(text)
    refused = sorted(name for name, (outcome, _detail) in details.items() if outcome == "refuse")
    if refused:
        return _restore_failure(ctx, f"restore transcript contains refusal row(s): {', '.join(refused)}")
    select = details.get("select")
    if select is None or f"snapshot={ctx.snapshot_label}" not in select[1]:
        return _restore_failure(ctx, f"select row does not name snapshot={ctx.snapshot_label}")
    pre_restore = details.get("pre-restore")
    if pre_restore is None or pre_restore[1] != restore.FRESH_STACK_SKIPPED_DETAIL:
        return _restore_failure(
            ctx,
            f"pre-restore row was not {restore.FRESH_STACK_SKIPPED_DETAIL}",
        )
    fetch = details.get("fetch")
    if fetch is None or f"selected set {ref.label}" not in fetch[1]:
        return _restore_failure(ctx, f"fetch row does not name selected set {ref.label}")
    next_row = details.get("next")
    if next_row is None or next_row[0] != "ok":
        return _restore_failure(ctx, "next row is not ok")
    if not any(
        line.startswith("Secrets:") and "a rebuilt box" in line
        for line in text.splitlines()
    ):
        return _restore_failure(ctx, "Secrets line does not describe a rebuilt box")
    return StageResult(
        "restore",
        True,
        f"restored snapshot {ctx.snapshot_label}, set {ref.label}; "
        f"pre-restore {restore.FRESH_STACK_SKIPPED_DETAIL}; next: a rebuilt box",
        "",
    )


def _parse_counts(text: str, wanted: set[str]) -> tuple[dict[str, int], str | None]:
    parsed: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, value = line.partition("|")
        name = name.strip()
        value = value.strip()
        if not separator or name not in wanted:
            continue
        try:
            number = int(value, 10)
        except ValueError:
            return {}, name
        if number < 0:
            return {}, name
        parsed[name] = number
    return parsed, None


def _counts_detail(
    expected: Mapping[str, Mapping[str, int]],
    observed: Mapping[str, Mapping[str, int]],
    mismatches: tuple[str, ...],
) -> str:
    databases = []
    for database in sorted(set(expected) | set(observed)):
        values = observed.get(database, {})
        tables = ", ".join(f"{name}={values[name]}" for name in sorted(values))
        databases.append(f"{database}: {tables or '-'}")
    if not mismatches:
        return "; ".join(databases)
    differences: list[str] = []
    for name in mismatches:
        if "." not in name:
            differences.append(name)
            continue
        database, table = name.split(".", 1)
        differences.append(
            f"{name} {expected.get(database, {}).get(table, '-')} -> "
            f"{observed.get(database, {}).get(table, '-')}"
        )
    return f"{'; '.join(databases)}; mismatches: {', '.join(differences)}"


def counts(ctx: HarnessContext) -> StageResult:
    """Count the current database tables and compare them with the set."""

    selected = _selected_manifest(ctx)
    if selected is None:
        return _stage_failure("counts", "the set stage did not select a manifest")
    _ref, manifest = selected
    observed: dict[str, Mapping[str, int]] = {}
    for database in sorted(manifest.row_counts):
        listing = vm.run_sql(ctx, database, _COUNTS_SQL)
        if listing.returncode != 0:
            return _stage_failure(
                "counts",
                f"table listing failed for {database}: {command_detail(listing)}",
            )
        tables = tuple(
            line.strip()
            for line in listing.stdout.splitlines()
            if line.strip() and stages.table_parts(line.strip()) is not None
        )
        count_sql = "".join(
            f"SELECT '{table}', count(*) FROM {table};\n" for table in tables
        )
        counted = vm.run_sql(ctx, database, count_sql)
        if counted.returncode != 0:
            return _stage_failure(
                "counts",
                f"table counts failed for {database}: {command_detail(counted)}",
            )
        parsed, invalid = _parse_counts(counted.stdout, set(tables))
        if invalid is not None:
            return _stage_failure("counts", f"invalid row count for {database}.{invalid}")
        missing = sorted(set(tables) - set(parsed))
        if missing:
            return _stage_failure(
                "counts",
                f"row counts were missing for {database}: {', '.join(missing)}",
            )
        observed[database] = {name: parsed[name] for name in tables}
    mismatches = compare_counts(manifest.row_counts, observed)
    return StageResult(
        "counts",
        not mismatches,
        _counts_detail(manifest.row_counts, observed, mismatches),
        "" if not mismatches else FULL_RESTORE_FIX,
    )


_AUDIT_TABLE: Final = "public.audit_log"
_AUDIT_PARTITION_PREFIX: Final = f"{_AUDIT_TABLE}_"


def _audit_partition(name: str) -> bool:
    return name.startswith(_AUDIT_PARTITION_PREFIX)


def _count_mismatches(
    expected: Mapping[str, int], observed: Mapping[str, int], database: str
) -> list[str]:
    names = set(expected) | set(observed)
    mismatches: list[str] = []
    audit_parent_expected = expected.get(_AUDIT_TABLE)
    audit_parent_observed = observed.get(_AUDIT_TABLE)
    audit_present = (
        audit_parent_expected is not None
        or audit_parent_observed is not None
        or any(_audit_partition(name) for name in expected)
        or any(_audit_partition(name) for name in observed)
    )
    if audit_present and (
        audit_parent_expected is None
        or audit_parent_observed != audit_parent_expected + 1
    ):
        mismatches.append(f"{database}.{_AUDIT_TABLE}")

    changed_partitions = 0
    extra_partitions = 0
    for name in sorted(names):
        if name == _AUDIT_TABLE or _audit_partition(name):
            continue
        if expected.get(name) != observed.get(name):
            mismatches.append(f"{database}.{name}")

    expected_partitions = {name for name in expected if _audit_partition(name)}
    observed_partitions = {name for name in observed if _audit_partition(name)}
    for name in sorted(expected_partitions & observed_partitions):
        if observed[name] == expected[name] + 1:
            changed_partitions += 1
        elif observed[name] != expected[name]:
            mismatches.append(f"{database}.{name}")
    for name in sorted(expected_partitions - observed_partitions):
        mismatches.append(f"{database}.{name}")
    for name in sorted(observed_partitions - expected_partitions):
        if observed[name] == 1:
            extra_partitions += 1
        else:
            mismatches.append(f"{database}.{name}")
    if audit_present and changed_partitions + extra_partitions != 1:
        mismatches.append(f"{database}.{_AUDIT_TABLE}")
    return mismatches


def compare_counts(
    expected: Mapping[str, Mapping[str, int]],
    observed: Mapping[str, Mapping[str, int]],
) -> tuple[str, ...]:
    """Return table names whose restored counts violate the audit-row rule."""

    mismatches: list[str] = []
    for database in sorted(set(expected) | set(observed)):
        if database not in expected or database not in observed:
            mismatches.append(database)
            continue
        mismatches.extend(_count_mismatches(expected[database], observed[database], database))
    return tuple(dict.fromkeys(mismatches))


def _decrypt_fix(label: str) -> str:
    return f"Run age -d for set {label} with the box identity, then retry acceptance."


def decrypt(ctx: HarnessContext) -> StageResult:
    """Decrypt the set's secret tarball directly into the VM."""

    selected = _selected_manifest(ctx)
    if selected is None:
        return _stage_failure("decrypt", "the set stage did not select a manifest")
    ref, _manifest = selected
    tarball = f"{ref.path}/{backupset.TARBALL_NAME}"
    age = shlex.join(["age", "-d", "-i", str(AGE_IDENTITY_PATH), tarball])
    extract = shlex.join(vm.ssh_argv(ctx, "sudo tar -xv -C /etc/gideon"))
    pipeline = f"{age} | {extract}"
    argv = ["bash", "-o", "pipefail", "-c", pipeline]
    # The identity reaches age by path and neither stream is echoed, since a
    # decoder's diagnostic may quote its input (backup run's rule).
    try:
        result = ctx.host.run(argv, timeout=COPY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return _stage_failure(
            "decrypt", f"decrypt did not run ({type(exc).__name__})", _decrypt_fix(ref.label)
        )
    if result.returncode != 0:
        return _stage_failure(
            "decrypt", f"decrypt exited {result.returncode}", _decrypt_fix(ref.label)
        )
    extracted = len([line for line in result.stdout.splitlines() if line.strip()])
    return StageResult("decrypt", True, f"extracted {extracted} name(s)", "")


def reinstall(ctx: HarnessContext) -> StageResult:
    """Reinstall the VM's site material and backup authorization."""

    installed = services.install_site(ctx)
    if not installed.ok:
        return StageResult("reinstall", False, installed.detail, installed.fix)
    authorized = services.authorize(ctx)
    if not authorized.ok:
        return StageResult("reinstall", False, authorized.detail, authorized.fix)
    return StageResult("reinstall", True, "site files and backup key re-installed", "")


def apply_again(ctx: HarnessContext) -> StageResult:
    """Apply again after restoring and reinstalling the box material."""

    return _apply(ctx, "apply-2.txt")


def _expected_release(ctx: HarnessContext) -> tuple[str | None, StageResult | None]:
    if ctx.base_version is not None:
        return ctx.base_version, None
    version = rehearsal.base_version(ctx)
    if isinstance(version, Problem):
        return None, _stage_failure("health", version.problem, version.fix)
    return version, None


def health(ctx: HarnessContext) -> StageResult:
    """Check the applied release and every declared Compose service."""

    applied = vm.run_as_root(ctx, f"cat {shlex.quote(rehearsal.APPLIED_RECORD)}")
    if applied.returncode != 0:
        return _stage_failure("health", f"could not read {rehearsal.APPLIED_RECORD}: {command_detail(applied)}")
    try:
        document = yaml.safe_load(applied.stdout)
    except yaml.YAMLError as exc:
        return _stage_failure("health", f"could not parse {rehearsal.APPLIED_RECORD}: {exc}")
    if not isinstance(document, Mapping):
        return _stage_failure("health", f"{rehearsal.APPLIED_RECORD} is not a mapping")
    expected, failure = _expected_release(ctx)
    if failure is not None or expected is None:
        return failure or _stage_failure("health", "the expected release is unavailable")
    if document.get("release") != expected:
        return _stage_failure(
            "health",
            f"applied release {document.get('release', '-')!r} does not match {expected}",
        )
    declared = document.get("services")
    if not isinstance(declared, Mapping) or not all(isinstance(name, str) for name in declared):
        return _stage_failure("health", f"{rehearsal.APPLIED_RECORD} has no service mapping")
    listing = vm.run_as_root(
        ctx,
        shlex.join(stack.compose_argv("/etc/gideon/rendered", "ps", "--all", "--format", "json")),
    )
    if listing.returncode != 0:
        return _stage_failure("health", f"Compose service check failed: {command_detail(listing)}")
    rows = stack.parse_ps(listing.stdout)
    if rows is None:
        return _stage_failure("health", "Compose service check returned invalid JSON")
    missing: list[str] = []
    for name in declared:
        healthy = any(
            row.get("Service") == name
            and row.get("State") == "running"
            and (not row.get("Health") or row.get("Health") == "healthy")
            for row in rows
        )
        if not healthy:
            missing.append(name)
    if missing:
        return _stage_failure("health", f"services are not running and healthy: {', '.join(missing)}")
    ctx.services_listing = listing.stdout
    ctx.applied_record = document
    return StageResult("health", True, f"{len(declared)} services healthy at release {expected}", "")


def mode(ctx: HarnessContext) -> StageResult:
    """Verify the restored VM remains in no-GPU mode."""

    applied = ctx.applied_record
    if applied is None:
        return _stage_failure("mode", "the health stage did not retain the applied record")
    marker = vm.run_as_root(ctx, f"test -e {shlex.quote(str(nogpu.NO_GPU_PATH))}")
    if marker.returncode != 0:
        return _stage_failure("mode", f"no-GPU marker is missing: {nogpu.NO_GPU_PATH}")
    inputs = applied.get("inputs")
    declared = applied.get("services")
    if not isinstance(inputs, Mapping) or inputs.get("no_gpu") is not True:
        return _stage_failure("mode", "the applied record does not declare no-GPU mode")
    if not isinstance(declared, Mapping) or ENGINE_SERVICE_NAME in declared:
        return _stage_failure("mode", f"the applied record declares {ENGINE_SERVICE_NAME}")
    if ctx.services_listing is None:
        return _stage_failure("mode", "the health stage did not retain the service listing")
    rows = stack.parse_ps(ctx.services_listing)
    if rows is None:
        return _stage_failure("mode", "the retained Compose service listing is invalid")
    if any(row.get("Service") == ENGINE_SERVICE_NAME for row in rows):
        return _stage_failure("mode", f"the running project contains {ENGINE_SERVICE_NAME}")
    return StageResult("mode", True, "no-GPU mode is active", "")


def users(ctx: HarnessContext) -> StageResult:
    """Count Open WebUI users through the product's frontend client."""

    selected = _selected_manifest(ctx)
    if selected is None:
        return _stage_failure("users", "the set stage did not select a manifest")
    _ref, manifest = selected
    expected = manifest.row_counts.get("openwebui", {}).get("public.user")
    if not isinstance(expected, int):
        return _stage_failure("users", "the manifest has no Open WebUI user count")
    hostname = seed.hostname_for(ctx.spec.vm_name)
    program = (
        "from gideon.host import owui, secrets, tls\n"
        "from gideon.host.sysio import RealHost\n"
        f"factory = owui.ingress_client_factory({hostname!r}, ca_path=tls.CA_PATH)\n"
        "secret = secrets.read_secret(RealHost(), 'gideon_admin_api_key')\n"
        "print(len(factory(api_key=secret.value).users_all()))\n"
    )
    result = vm.ssh(
        ctx,
        f"cd {shlex.quote(vm.VM_CHECKOUT)} && sudo python3 -c {shlex.quote(program)}",
    )
    if result.returncode != 0:
        return _stage_failure("users", f"user-count program exited {result.returncode}")
    value = result.stdout.strip()
    if not value.isdigit():
        return _stage_failure("users", "user-count program did not print one integer")
    observed = int(value, 10)
    if observed != expected:
        return _stage_failure("users", f"users {observed}, manifest {expected}")
    return StageResult("users", True, f"users {observed}, manifest {expected}", "")


def backup(ctx: HarnessContext) -> StageResult:
    """Create a new full backup and verify its recipient coverage."""

    selected = _selected_manifest(ctx)
    if selected is None:
        return _stage_failure("backup", "the set stage did not select a manifest")
    ref, manifest = selected
    result, _text = _product_stage(
        ctx,
        "backup",
        "backup.txt",
        ["python3", "-m", "gideon", "backup", "run", "--full"],
        timeout=BACKUP_TIMEOUT_SECONDS,
    )
    if not result.ok:
        return result
    listed = vm.run_as_root(ctx, f"ls -1 {shlex.quote(backupset.SETS_DIR)}")
    if listed.returncode != 0:
        return _stage_failure("backup", f"could not list backup sets: {command_detail(listed)}")
    labels = tuple(
        line.strip()
        for line in listed.stdout.splitlines()
        if backupset.kind_of(line.strip()) is backupset.Kind.NIGHTLY
        and line.strip() != ref.label
    )
    if not labels:
        return _stage_failure("backup", f"no new nightly set appeared after {ref.label}")
    label = max(labels)
    manifest_result = vm.run_as_root(
        ctx,
        f"cat {shlex.quote(f'{backupset.set_dir(label)}/{backupset.MANIFEST_NAME}')}",
    )
    if manifest_result.returncode != 0:
        return _stage_failure(
            "backup",
            f"could not read manifest for {label}: {command_detail(manifest_result)}",
        )
    parsed = backupset.parse_manifest(manifest_result.stdout)
    if isinstance(parsed, Problem):
        return _stage_failure("backup", parsed.problem, parsed.fix)
    if len(parsed.recipients) != 2:
        return _stage_failure("backup", f"new set {label} has {len(parsed.recipients)} recipients, not 2")
    if parsed.recipients[0] != manifest.recipients[0]:
        return _stage_failure("backup", f"new set {label} has the wrong first recipient")
    return StageResult("backup", True, f"new set {label}; {len(parsed.recipients)} recipients", "")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _count_value(text: str) -> int | None:
    value = text.strip()
    return int(value, 10) if value.isdigit() else None


def audit(ctx: HarnessContext) -> StageResult:
    """Verify the restore and backup-run audit rows."""

    selected = _selected_manifest(ctx)
    if selected is None:
        return _stage_failure("audit", "the set stage did not select a manifest")
    ref, _manifest = selected
    label_problem = backupset.validate_label(ref.label)
    if label_problem is not None:
        return _stage_failure("audit", label_problem.problem, label_problem.fix)
    restore_rows = (
        "FROM audit_log WHERE kind = 'restore' "
        f"AND detail->>'set_label' = {_sql_literal(ref.label)}"
    )
    restore_sql = f"SELECT count(*) {restore_rows};\n"
    restored = vm.run_sql(ctx, "gideon", restore_sql)
    if restored.returncode != 0:
        return _stage_failure("audit", f"restore audit query failed: {command_detail(restored)}")
    restore_count = _count_value(restored.stdout)
    if restore_count is None:
        return _stage_failure("audit", "restore audit query did not print one integer")
    backup_rows = vm.run_sql(
        ctx,
        "gideon",
        # The restored table carries the box's own rows; only the VM's count.
        "SELECT count(*) FROM audit_log WHERE kind = 'backup_run' "
        f"AND at > (SELECT max(at) {restore_rows});\n",
    )
    if backup_rows.returncode != 0:
        return _stage_failure("audit", f"backup audit query failed: {command_detail(backup_rows)}")
    backup_count = _count_value(backup_rows.stdout)
    if backup_count is None:
        return _stage_failure("audit", "backup audit query did not print one integer")
    if restore_count != 1 or backup_count < 2:
        return _stage_failure("audit", f"restore {restore_count}, backup_run {backup_count}")
    return StageResult("audit", True, f"restore {restore_count}, backup_run {backup_count}", "")


def drill(ctx: HarnessContext) -> StageResult:
    """Run the product's backup drill and reject any refusal row."""

    result, _text = _product_stage(
        ctx,
        "drill",
        "drill.txt",
        ["python3", "-m", "gideon", "backup", "drill"],
        timeout=DRILL_TIMEOUT_SECONDS,
    )
    return result

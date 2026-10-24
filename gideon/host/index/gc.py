"""Drop retired generations from both derived stores, holding only promoted ones."""

import argparse
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from gideon.host import backuplock, derivedstores, report, secrets, stack
from gideon.host.index import aliases, generations, lexical, record
from gideon.host.render.ci import CI_ROOT, CI_SECRETS_DIR, CI_STACK
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, QDRANT_SERVICE_NAME
from gideon.host.render.worker import GENERATION_PATTERN
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

COMMAND_PATH = "index gc"
# exempt: a retired generation answers turns that read it before the flip for a week.
RETIRED_HOLD_DAYS: Final = 7


@dataclass(frozen=True, slots=True)
class Candidates:
    """The droppable, held, and live generations in the record."""

    drop: tuple[record.GenerationRow, ...]
    held: tuple[tuple[record.GenerationRow, int], ...]
    live: tuple[record.GenerationRow, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    """One generation's completed deletes, with an unvisited store left unset."""

    name: str
    lexical: Literal["dropped", "absent"] | None = None
    dense: Literal["dropped", "absent"] | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(stage: str, problem: Problem, command_path: str) -> int:
    if report.command(command_path) not in problem.fix:
        problem = Problem(problem.problem, f"{problem.fix} Then run {report.command(command_path)} again.")
    report.print_stage(StageResult(stage, False, problem.problem, problem.fix))
    return 1


def _candidates(
    rows: tuple[record.GenerationRow, ...], now: datetime,
    rendered_dir: PathLike, command_path: str,
) -> Candidates | Problem:
    cutoff = now - timedelta(days=RETIRED_HOLD_DAYS)
    drop: list[record.GenerationRow] = []
    held: list[tuple[record.GenerationRow, int]] = []
    live: list[record.GenerationRow] = []
    for row in rows:
        if row.state == "live":
            live.append(row)
        elif row.state == "retired":
            try:
                retired_at = datetime.fromisoformat(row.retired_at or "")
            except ValueError:
                return Problem("generation record retired_at is invalid", record.database_fix(rendered_dir, command_path))
            if retired_at.tzinfo is None or retired_at.utcoffset() is None:
                return Problem("generation record retired_at is invalid", record.database_fix(rendered_dir, command_path))
            if row.promoted_at is not None and retired_at > cutoff:
                held.append((row, -((cutoff - retired_at) // timedelta(days=1))))
                continue
            if re.fullmatch(GENERATION_PATTERN, row.gen_id) is None or len(row.gen_id.encode()) >= 255:
                return Problem("generation record name is invalid", record.database_fix(rendered_dir, command_path))
            drop.append(row)
    return Candidates(
        tuple(sorted(drop, key=lambda row: row.gen_id)),
        tuple(sorted(held, key=lambda item: item[0].gen_id)),
        tuple(sorted(live, key=lambda row: row.gen_id)),
    )


def _drop_reason(row: record.GenerationRow) -> str:
    return "never promoted" if row.promoted_at is None else "hold passed"


def _drop(
    host: Host, rendered_dir: PathLike, name: str,
    store: Literal["lexical", "dense"], command_path: str,
) -> Literal["dropped", "absent"] | Problem:
    service = OPENSEARCH_SERVICE_NAME if store == "lexical" else QDRANT_SERVICE_NAME
    secret_name = OPENSEARCH_PASSWORD_SECRET_NAME if store == "lexical" else QDRANT_SECRET_NAME
    credential = derivedstores.store_secret(host, secret_name)
    if isinstance(credential, Problem):
        return credential
    if store == "lexical":
        reply = derivedstores.opensearch_request(
            host, rendered_dir, credential, "DELETE", f"/{name}",
            accepted_statuses=frozenset({200, 404}),
        )
        field = "acknowledged"
    else:
        # Qdrant answers a missing collection 200 with result false, never 404.
        reply = derivedstores.qdrant_request(
            host, rendered_dir, credential, "DELETE", f"/collections/{name}",
        )
        field = "result"
    if isinstance(reply, Problem):
        return reply
    if reply.status == 404 or (store == "dense" and isinstance(reply.data, Mapping) and reply.data.get(field) is False):
        return "absent"
    if not isinstance(reply.data, Mapping) or reply.data.get(field) is not True:
        return Problem(
            f"{service} returned an invalid delete reply",
            f"Run {stack.logs_fix(rendered_dir, service)}, "
            f"then run {report.command(command_path)} again.",
        )
    return "dropped"


def run_index_gc(
    args: argparse.Namespace, *, host: LockingHost | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered", clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Sweep the stack's eligible retired generations without changing their record."""

    io = RealHost() if host is None else host
    stack_name = args.stack
    command_path = f"{COMMAND_PATH} --stack ci" if stack_name == CI_STACK else COMMAND_PATH
    active_stage = "preconditions"
    claim: backuplock.Claim | None = None
    engine_lock: backuplock.Outcome | None = None
    backup_lock: backuplock.Outcome | None = None
    try:
        if io.geteuid() != 0:
            problem = Problem("root privileges are required.", f"Run {report.command(command_path)}.")
            print(report.refusal(COMMAND_PATH, problem.problem, problem.fix), file=sys.stderr)
            return 2
        if stack_name == CI_STACK:
            rendered_dir = CI_ROOT
            secrets.select_directory(Path(CI_SECRETS_DIR))
            try:
                engine_lock = backuplock.take(
                    io, command=report.command_name(command_path), now=clock(), lock=backuplock.ENGINE_LOCK,
                )
            except OSError:
                return _refuse(active_stage, Problem(
                    "engine lock could not be taken",
                    f"Repair access to the engine lock, then run {report.command(command_path)} again.",
                ), command_path)
            if engine_lock.state is backuplock.State.REFUSED:
                assert engine_lock.problem is not None
                return _refuse(active_stage, engine_lock.problem, command_path)
            lock_detail = "engine lock taken" if engine_lock.state is backuplock.State.HELD else "engine lock held by this process"
            job = lexical.live_index_job(io, rendered_dir, report.command(command_path))
            if isinstance(job, Problem):
                return _refuse(active_stage, job, command_path)
            if job is not None:
                return _refuse(active_stage, Problem(
                    f"a sample build is running on the sibling (job {job})",
                    f"Wait for the job, then run {report.command(command_path)} again.",
                ), command_path)
        else:
            claim = backuplock.claim(
                io, command=report.command_name(command_path), now=clock(), lock=backuplock.CORPUS_LOCK,
            )
            if claim.refusal is not None:
                return _refuse(active_stage, Problem(claim.refusal.detail, claim.refusal.fix), command_path)
            if not claim.taken:
                return _refuse(active_stage, Problem(
                    "a corpus command is already running in this process",
                    f"Wait, then run {report.command(command_path)} again.",
                ), command_path)
            try:
                backup_lock = backuplock.take(
                    io, command=report.command_name(command_path), now=clock(), lock=backuplock.BACKUP_LOCK,
                )
            except OSError:
                return _refuse(active_stage, Problem(
                    "backup lock could not be taken",
                    f"Repair access to the backup lock, then run {report.command(command_path)} again.",
                ), command_path)
            if backup_lock.state is backuplock.State.REFUSED:
                assert backup_lock.problem is not None
                return _refuse(active_stage, backup_lock.problem, command_path)
            backup_detail = "backup lock taken" if backup_lock.state is backuplock.State.HELD else "backup lock held by this process"
            lock_detail = f"{claim.detail}; {backup_detail}"
        report.print_stage(StageResult(active_stage, True, f"{stack_name}; {lock_detail}", ""))

        active_stage = "candidates"
        rows = record.read_generations(io, rendered_dir, command_path=command_path)
        if isinstance(rows, Problem):
            return _refuse(active_stage, rows, command_path)
        candidates = _candidates(rows, clock(), rendered_dir, command_path)
        if isinstance(candidates, Problem):
            return _refuse(active_stage, candidates, command_path)
        for row in candidates.drop:
            for prefix, subject in lexical.JOB_SUBJECTS.items():
                job = lexical.live_job(
                    io, rendered_dir, f"{prefix}{row.gen_id}", prefix=prefix, command_path=command_path,
                )
                if isinstance(job, Problem):
                    return _refuse(active_stage, job, command_path)
                if job is not None:
                    return _refuse(active_stage, Problem(
                        f"{row.gen_id}: {subject} job {job} is queued or running",
                        f"Wait for the job, then run {report.command(command_path)} again.",
                    ), command_path)
        drop_names = ", ".join(f"{row.gen_id} ({_drop_reason(row)})" for row in candidates.drop) or "none (nothing to drop)"
        held_names = ", ".join(f"{row.gen_id} ({days} days left)" for row, days in candidates.held) or "none"
        live_names = ", ".join(row.gen_id for row in candidates.live) or "none"
        skipped = "; ".join(f"{state} {sum(row.state == state for row in rows)}" for state in ("building", "built", "failed"))
        report.print_stage(StageResult(
            active_stage, True,
            f"drop {len(candidates.drop)}: {drop_names}; held {len(candidates.held)}: {held_names}; "
            f"live: {live_names} (never dropped); {skipped} "
            f"(retire one with {report.command('index retire <generation>')})", "",
        ))

        active_stage = "aliases"
        if not candidates.drop:
            report.print_stage(StageResult(active_stage, True, "nothing to drop", ""))
        else:
            details: list[str] = []
            names = {row.gen_id for row in candidates.drop}
            for collection in generations.COLLECTIONS:
                targets = aliases.read_aliases(io, rendered_dir, collection.name, command_path=command_path)
                if isinstance(targets, Problem):
                    return _refuse(active_stage, targets, command_path)
                serving = names.intersection((*targets.lexical, *targets.dense))
                if serving:
                    live = next((row for row in candidates.live if row.collection == collection.name), None)
                    fix_command = f"index promote {live.gen_id}" if live is not None else "index report"
                    action = "converge the aliases" if live is not None else "inspect the record"
                    return _refuse(active_stage, Problem(
                        f"retired generation still served by {collection.name}'s alias: {', '.join(sorted(serving))}",
                        f"Run {report.command(fix_command)} to {action}, "
                        f"then run {report.command(command_path)} again.",
                    ), command_path)
                details.append(
                    f"{collection.name}: lexical {', '.join(targets.lexical) or 'none'}; "
                    f"dense {', '.join(targets.dense) or 'none'}"
                )
            report.print_stage(StageResult(active_stage, True, "; ".join(details), ""))

        verdicts = [Verdict(row.gen_id) for row in candidates.drop]
        stores: tuple[Literal["lexical", "dense"], ...] = ("lexical", "dense")
        for store in stores:
            active_stage = store
            if not verdicts:
                report.print_stage(StageResult(active_stage, True, "nothing to drop", ""))
                continue
            dropped: list[str] = []
            absent: list[str] = []
            for index, verdict in enumerate(verdicts):
                outcome = _drop(io, rendered_dir, verdict.name, store, command_path)
                if isinstance(outcome, Problem):
                    return _refuse(active_stage, outcome, command_path)
                verdicts[index] = replace(verdict, lexical=outcome) if store == "lexical" else replace(verdict, dense=outcome)
                (dropped if outcome == "dropped" else absent).append(verdict.name)
            report.print_stage(StageResult(
                active_stage, True,
                f"dropped: {', '.join(dropped) or 'none'}; absent: {', '.join(absent) or 'none'}", "",
            ))

        active_stage = "summary"
        counts = "; ".join(
            f"{store} dropped {sum(getattr(verdict, store) == 'dropped' for verdict in verdicts)}, "
            f"absent {sum(getattr(verdict, store) == 'absent' for verdict in verdicts)}"
            for store in stores
        )
        report.print_stage(StageResult(
            active_stage, True, f"{counts}; held {len(candidates.held)}; live {len(candidates.live)}", "",
        ))
        return 0
    except KeyboardInterrupt:
        if active_stage in {"lexical", "dense", "summary"}:
            detail = "interrupted; the record is unchanged; rerun to finish the store deletes, completed deletes read absent"
        else:
            detail = "interrupted; nothing was dropped and the record is unchanged"
        return _refuse(active_stage, Problem(
            detail,
            f"Run {report.command(command_path)} again.",
        ), command_path)
    finally:
        if backup_lock is not None and backup_lock.state is backuplock.State.HELD:
            backuplock.release(io, lock=backuplock.BACKUP_LOCK)
        if claim is not None:
            backuplock.release_claim(io, claim, lock=backuplock.CORPUS_LOCK)
        if engine_lock is not None and engine_lock.state is backuplock.State.HELD:
            backuplock.release(io, lock=backuplock.ENGINE_LOCK)

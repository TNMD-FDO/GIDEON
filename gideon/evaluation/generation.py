"""Resolve the index generation an evaluation reads on its selected stack."""

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Final

from gideon.evaluation import record
from gideon.host import report, stack
from gideon.host.corpus.lockfile import LABEL
from gideon.host.index import generations
from gideon.host.render.ci import CI_STACK
from gideon.host.render.worker import GENERATION_PATTERN, GENERATION_STATES
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.host.worker import bind

COLLECTION: Final[str] = generations.COLLECTIONS[0].name
_READABLE_STATES: Final = frozenset({"built", "live"})
_ROW_KEYS: Final = frozenset({"gen_id", "corpus_lockfile", "state"})


@dataclass(frozen=True, slots=True)
class GenerationAccess:
    """The recorded identity, corpus lockfile, and state of a resolved generation."""

    name: str
    corpus_lockfile: str
    state: str


def _select_sql(name: str | None) -> str:
    """One metrics-role statement over the named row or the collection's live rows."""

    if name is None:
        sql = bind("v_collection", COLLECTION) + "\n"
        where = "collection = :'v_collection' AND state = 'live'"
    else:
        sql = bind("v_name", name) + "\n"
        where = "gen_id = :'v_name'"
    return sql + (
        "SELECT json_build_object('gen_id', gen_id, 'corpus_lockfile', corpus_lockfile, "
        "'state', state) FROM public.index_generations WHERE " + where + " ORDER BY gen_id;\n"
    )


def _parse_row(line: str) -> GenerationAccess | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or set(value) != _ROW_KEYS:
        return None
    gen_id, lockfile, state = value["gen_id"], value["corpus_lockfile"], value["state"]
    if (
        not isinstance(gen_id, str) or re.fullmatch(GENERATION_PATTERN, gen_id) is None
        or not isinstance(lockfile, str) or LABEL.fullmatch(lockfile) is None
        or not isinstance(state, str) or state not in GENERATION_STATES
    ):
        return None
    return GenerationAccess(gen_id, lockfile, state)


def resolve_generation(
    host: Host, stack_dir: PathLike, name: str | None, *, stack_name: str, retry_command: str,
) -> GenerationAccess | Problem:
    """Read one named generation, or the stack's one live collection generation.

    Only a ``built`` or ``live`` generation resolves, so a partial manifest never
    reaches a gate; every fix reprints ``retry_command``, the run's own flags.
    """

    selection_fix = (
        f"Name a built generation with {retry_command} --generation <name> "
        f"({report.command('index report')} lists production's; "
        "the sibling's sample is <production name>__sample), then retry."
    )
    if name is not None and re.fullmatch(GENERATION_PATTERN, name) is None:
        return Problem("not a generation name", selection_fix)
    logs_fix = (
        f"Run {stack.logs_fix(stack_dir, record.POSTGRES_SERVICE)}, "
        f"then run {retry_command} again."
    )
    try:
        result = host.run(record.reader_argv(stack_dir), input=_select_sql(name))
    except (OSError, subprocess.SubprocessError):
        return Problem("generation record read could not run", logs_fix)
    if result.returncode != 0:
        return Problem(f"generation record read failed (exit {result.returncode})", logs_fix)
    rows: list[GenerationAccess] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        row = _parse_row(line)
        if row is None or (name is not None and row.name != name):
            return Problem("generation record row is invalid", logs_fix)
        rows.append(row)
    if not rows:
        detail = (
            f"no live {COLLECTION} generation on the {stack_name} stack"
            if name is None
            else f"generation {name} is not recorded on the {stack_name} stack"
        )
        return Problem(detail, selection_fix)
    if len(rows) > 1:
        if name is None:
            return Problem(
                f"more than one live {COLLECTION} generation on the {stack_name} stack",
                selection_fix,
            )
        return Problem("generation record row is invalid", logs_fix)
    resolved = rows[0]
    if resolved.state not in _READABLE_STATES:
        build = f"index build {COLLECTION}" + (" --sample" if stack_name == CI_STACK else "")
        return Problem(
            f"generation {resolved.name} is {resolved.state}",
            f"Finish its build ({report.command(build)}) or name a built one with "
            f"{retry_command} --generation <name>, then retry.",
        )
    return resolved

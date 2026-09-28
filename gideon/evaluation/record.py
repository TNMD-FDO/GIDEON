"""Record evaluation runs and case results in the append-only Postgres tables."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from gideon.host import stack
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

EVAL_ROLE: Final[str] = "gideon_eval"
METRICS_ROLE: Final[str] = "gideon_ro_metrics"
EVAL_DATABASE: Final[str] = "gideon"
POSTGRES_SERVICE: Final[str] = "postgres"
_PSQL_FLAGS: Final[tuple[str, ...]] = (
    "-v",
    "ON_ERROR_STOP=1",
    "--single-transaction",
    "-f",
    "-",
)
_READER_FLAGS: Final[tuple[str, ...]] = (
    "-v",
    "ON_ERROR_STOP=1",
    "-tA",
    "-f",
    "-",
)


@dataclass(frozen=True, slots=True)
class RunRow:
    """One row for a completed evaluation run."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    product_version: str
    corpus_lockfile: str | None
    eval_set_version: str
    hardware_profile: str
    stack: str
    generation_id: str | None
    kind: str
    slice: str | None
    overrides: Mapping[str, object]
    repeats: int
    git_sha: str | None
    git_dirty: bool | None
    set_digest: str
    verdict: str
    forced: bool
    partial: bool
    decision: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One row for one evaluated case and repeat."""

    run_id: str
    run_started_at: datetime
    case_id: str
    repeat: int
    verdict: str
    metrics: Mapping[str, object]
    judge: Mapping[str, object] | None
    provenance_ref: str | None
    latency_ms: float | None

    def __repr__(self) -> str:
        """Keep the judge's reason out of diagnostic representations.

        Every other column is content-free; ``judge`` carries the one
        model-written text a result row holds.
        """

        judge = None if self.judge is None else "<redacted>"
        return (
            "ResultRow("
            f"run_id={self.run_id!r}, run_started_at={self.run_started_at!r}, "
            f"case_id={self.case_id!r}, repeat={self.repeat!r}, "
            f"verdict={self.verdict!r}, metrics={self.metrics!r}, "
            f"judge={judge}, provenance_ref={self.provenance_ref!r}, "
            f"latency_ms={self.latency_ms!r})"
        )


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """The run identity and verdict rows needed to write a reference file."""

    run_id: str
    product_version: str
    corpus_lockfile: str | None
    eval_set_version: str
    hardware_profile: str
    slice: str | None
    overrides: Mapping[str, object]
    repeats: int
    git_sha: str | None
    git_dirty: bool | None
    set_digest: str
    verdict: str
    forced: bool
    partial: bool
    decision: Mapping[str, object] | None
    results: tuple[tuple[str, int, str], ...]


@dataclass(frozen=True, slots=True)
class ComparandRun:
    """The content-free measurements needed to compare a recorded run."""

    run_id: str
    slice: str | None
    eval_set_version: str
    set_digest: str
    repeats: int
    kind: str
    partial: bool
    results: tuple[tuple[str, int, Mapping[str, object]], ...]


def _contains_forbidden_text(value: object) -> bool:
    if isinstance(value, str):
        return any(character in value for character in "\r\n\x00")
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden_text(key) or _contains_forbidden_text(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_contains_forbidden_text(item) for item in value)
    return False


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _set_variable(name: str, value: str, lines: list[str]) -> str:
    lines.append(f"\\set {name} '{_escape(value)}'")
    return f":'{name}'"


def _json_variable(name: str, value: Mapping[str, object], lines: list[str]) -> str:
    try:
        encoded = json.dumps(dict(value), sort_keys=True, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not JSON-serializable") from exc
    return _set_variable(name, encoded, lines)


def _text_variable(name: str, value: str | None, lines: list[str], cast: str = "") -> str:
    if value is None:
        return "NULL"
    return f"{_set_variable(name, value, lines)}{cast}"


def _bool_variable(name: str, value: bool | None, lines: list[str]) -> str:
    if value is None:
        return "NULL"
    return _set_variable(name, "true" if value else "false", lines) + "::boolean"


def _number_variable(name: str, value: int | float, lines: list[str], cast: str) -> str:
    return _set_variable(name, str(value), lines) + cast


def _validate_row(value: RunRow | ResultRow) -> None:
    fields: tuple[object, ...]
    if isinstance(value, RunRow):
        fields = (
            value.run_id,
            value.product_version,
            value.corpus_lockfile,
            value.eval_set_version,
            value.hardware_profile,
            value.stack,
            value.generation_id,
            value.kind,
            value.slice,
            value.overrides,
            value.git_sha,
            value.set_digest,
            value.verdict,
            value.decision,
        )
    else:
        fields = (value.run_id, value.case_id, value.verdict, value.metrics, value.judge, value.provenance_ref)
    if any(_contains_forbidden_text(field) for field in fields):
        raise ValueError("a row value contains CR, LF, or NUL")


def _run_sql(row: RunRow, lines: list[str]) -> str:
    _validate_row(row)
    values = [
        _text_variable("run_id", row.run_id, lines, "::uuid"),
        ":'run_started_at'::timestamptz",
        _set_variable("finished_at", row.finished_at.isoformat(), lines) + "::timestamptz",
        _set_variable("product_version", row.product_version, lines),
        _text_variable("corpus_lockfile", row.corpus_lockfile, lines),
        _set_variable("eval_set_version", row.eval_set_version, lines),
        _set_variable("hardware_profile", row.hardware_profile, lines),
        _set_variable("run_stack", row.stack, lines),
        _text_variable("generation_id", row.generation_id, lines),
        _set_variable("kind", row.kind, lines),
        _text_variable("slice", row.slice, lines),
        _json_variable("overrides", row.overrides, lines) + "::jsonb",
        _number_variable("repeats", row.repeats, lines, "::integer"),
        _text_variable("git_sha", row.git_sha, lines),
        _bool_variable("git_dirty", row.git_dirty, lines),
        _set_variable("set_digest", row.set_digest, lines),
        _set_variable("run_verdict", row.verdict, lines),
        _bool_variable("forced", row.forced, lines),
        _bool_variable("partial", row.partial, lines),
        "NULL"
        if row.decision is None
        else _json_variable("decision", row.decision, lines) + "::jsonb",
    ]
    return (
        "INSERT INTO eval_runs ("
        "run_id, started_at, finished_at, product_version, corpus_lockfile, "
        "eval_set_version, hardware_profile, stack, generation_id, kind, slice, "
        "overrides, repeats, git_sha, git_dirty, set_digest, verdict, forced, partial, decision) VALUES ("
        + ", ".join(values)
        + ");"
    )


def _result_sql(row: ResultRow, lines: list[str], index: int) -> str:
    _validate_row(row)
    prefix = f"result_{index}_"
    values = [
        _text_variable(prefix + "run_id", row.run_id, lines, "::uuid"),
        _set_variable(prefix + "started_at", row.run_started_at.isoformat(), lines) + "::timestamptz",
        _set_variable(prefix + "case_id", row.case_id, lines),
        _number_variable(prefix + "repeat", row.repeat, lines, "::integer"),
        _set_variable(prefix + "verdict", row.verdict, lines),
        _json_variable(prefix + "metrics", row.metrics, lines) + "::jsonb",
        "NULL" if row.judge is None else _json_variable(prefix + "judge", row.judge, lines) + "::jsonb",
        _text_variable(prefix + "provenance_ref", row.provenance_ref, lines),
        "NULL"
        if row.latency_ms is None
        else _number_variable(prefix + "latency_ms", row.latency_ms, lines, "::double precision"),
    ]
    return (
        "INSERT INTO eval_results ("
        "run_id, run_started_at, case_id, repeat, verdict, metrics, judge, "
        "provenance_ref, latency_ms) VALUES ("
        + ", ".join(values)
        + ");"
    )


def _argv(rendered_dir: PathLike) -> list[str]:
    return stack.exec_argv(
        rendered_dir,
        POSTGRES_SERVICE,
        "psql",
        "-U",
        EVAL_ROLE,
        "-d",
        EVAL_DATABASE,
        *_PSQL_FLAGS,
    )


def _reader_argv(rendered_dir: PathLike) -> list[str]:
    return stack.exec_argv(
        rendered_dir,
        POSTGRES_SERVICE,
        "psql",
        "-U",
        METRICS_ROLE,
        "-d",
        EVAL_DATABASE,
        *_READER_FLAGS,
    )


def _reader_fix(run_id: str) -> str:
    return (
        f"Run sudo python3 -m gideon eval reference --run {run_id} "
        "as root with the stack up, then retry."
    )


def _read_sql(run_id: str) -> str:
    lines: list[str] = []
    bound_id = _text_variable("run_id", run_id, lines, "::uuid")
    lines.append(
        f"""WITH selected_run AS (
    SELECT
        run_id,
        started_at,
        product_version,
        corpus_lockfile,
        eval_set_version,
        hardware_profile,
        slice,
        overrides,
        repeats,
        forced,
        partial,
        decision,
        git_sha,
        git_dirty,
        set_digest,
        verdict
    FROM eval_runs
    WHERE run_id = {bound_id}
    ORDER BY started_at DESC
    LIMIT 1
)
SELECT json_build_object(
    'run', (
        SELECT json_build_object(
            'run_id', run_id::text,
            'product_version', product_version,
            'corpus_lockfile', corpus_lockfile,
            'eval_set_version', eval_set_version,
            'hardware_profile', hardware_profile,
            'slice', slice,
            'overrides', overrides,
            'repeats', repeats,
            'forced', forced,
            'partial', partial,
            'decision', decision,
            'git_sha', git_sha,
            'git_dirty', git_dirty,
            'set_digest', set_digest,
            'verdict', verdict
        )
        FROM selected_run
    ),
    'results', COALESCE(
        (
            SELECT json_agg(
                json_build_object(
                    'case_id', result.case_id,
                    'repeat', result.repeat,
                    'verdict', result.verdict
                )
                ORDER BY result.case_id, result.repeat
            )
            FROM eval_results AS result
            JOIN selected_run AS run
              ON run.run_id = result.run_id
             AND run.started_at = result.run_started_at
        ),
        '[]'::json
    )
);"""
    )
    return "\n".join(lines) + "\n"


class _ReadRefusal(Exception):
    """One refusal raised while decoding the reader's document.

    The read yields a single problem-and-fix pair, not a collected list, so a
    field refuses where it is checked instead of being threaded back through
    every caller as a union.
    """

    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.problem)
        self.problem = problem


def _invalid(name: str, fix: str) -> _ReadRefusal:
    return _ReadRefusal(Problem(f"recorded run has an invalid {name}", fix))


def _text_field(value: object, name: str, fix: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise _invalid(name, fix)


def _optional_text_field(value: object, name: str, fix: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise _invalid(name, fix)


def _int_field(value: object, name: str, fix: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise _invalid(name, fix)


def _bool_field(value: object, name: str, fix: str) -> bool:
    if isinstance(value, bool):
        return value
    raise _invalid(name, fix)


def _mapping_field(value: object, name: str, fix: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    raise _invalid(name, fix)


def _reader_payload(
    stdout: str, run_id: str, fix: str
) -> tuple[Mapping[str, object], tuple[Mapping[str, object], ...]]:
    """Decode the reader's document into its run object and result objects.

    *fix* answers a malformed document; an absent run and a run with no
    results answer with the id to use instead, whichever command read it.
    """

    try:
        document = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _ReadRefusal(Problem("eval reader returned invalid JSON", fix)) from exc
    if not isinstance(document, Mapping):
        raise _ReadRefusal(Problem("eval reader returned an invalid document", fix))

    run = document.get("run")
    if run is None:
        raise _ReadRefusal(
            Problem(
                f"no evaluation run exists for {run_id}",
                "Use the id of a recorded evaluation run, then retry.",
            )
        )
    if not isinstance(run, Mapping):
        raise _ReadRefusal(Problem("eval reader returned an invalid run", fix))

    raw_results = document.get("results")
    if not isinstance(raw_results, list):
        raise _ReadRefusal(Problem("eval reader returned invalid results", fix))
    if not raw_results:
        raise _ReadRefusal(
            Problem(
                f"evaluation run {run_id} has no results",
                "Use a completed evaluation run with result rows, then retry.",
            )
        )
    rows = tuple(row for row in raw_results if isinstance(row, Mapping))
    if len(rows) != len(raw_results):
        raise _ReadRefusal(Problem("eval reader returned an invalid result", fix))
    return run, rows


def _decoded_run(stdout: str, run_id: str) -> RecordedRun:
    fix = _reader_fix(run_id)
    run, rows = _reader_payload(stdout, run_id, fix)

    overrides = run.get("overrides")
    if not isinstance(overrides, Mapping):
        raise _ReadRefusal(Problem("recorded run has invalid overrides", fix))
    git_dirty = run.get("git_dirty")
    if git_dirty is not None and not isinstance(git_dirty, bool):
        raise _ReadRefusal(Problem("recorded run has an invalid git dirty state", fix))
    decision = run.get("decision")
    if decision is not None and not isinstance(decision, Mapping):
        raise _invalid("decision", fix)

    return RecordedRun(
        run_id=_text_field(run.get("run_id"), "run id", fix),
        product_version=_text_field(run.get("product_version"), "product version", fix),
        corpus_lockfile=_optional_text_field(run.get("corpus_lockfile"), "corpus lockfile", fix),
        eval_set_version=_text_field(run.get("eval_set_version"), "eval-set version", fix),
        hardware_profile=_text_field(run.get("hardware_profile"), "hardware profile", fix),
        slice=_optional_text_field(run.get("slice"), "slice", fix),
        overrides=overrides,
        repeats=_int_field(run.get("repeats"), "repeats", fix),
        git_sha=_optional_text_field(run.get("git_sha"), "git sha", fix),
        git_dirty=git_dirty,
        set_digest=_text_field(run.get("set_digest"), "set digest", fix),
        verdict=_text_field(run.get("verdict"), "verdict", fix),
        forced=_bool_field(run.get("forced"), "forced state", fix),
        partial=_bool_field(run.get("partial"), "partial state", fix),
        decision=decision,
        results=tuple(
            (
                _text_field(row.get("case_id"), "case id", fix),
                _int_field(row.get("repeat"), "repeat", fix),
                _text_field(row.get("verdict"), "result verdict", fix),
            )
            for row in rows
        ),
    )


def _metrics_read_sql(run_id: str) -> str:
    lines: list[str] = []
    bound_id = _text_variable("run_id", run_id, lines, "::uuid")
    lines.append(
        f"""WITH selected_run AS (
    SELECT run_id, started_at, slice, eval_set_version, set_digest, repeats, kind, partial
    FROM eval_runs
    WHERE run_id = {bound_id}
    ORDER BY started_at DESC
    LIMIT 1
)
SELECT json_build_object(
    'run', (
        SELECT json_build_object(
            'run_id', run_id::text,
            'slice', slice,
            'eval_set_version', eval_set_version,
            'set_digest', set_digest,
            'repeats', repeats,
            'kind', kind,
            'partial', partial
        )
        FROM selected_run
    ),
    'results', COALESCE(
        (
            SELECT json_agg(
                json_build_object(
                    'case_id', result.case_id,
                    'repeat', result.repeat,
                    'metrics', result.metrics
                )
                ORDER BY result.case_id, result.repeat
            )
            FROM eval_results AS result
            JOIN selected_run AS run
              ON run.run_id = result.run_id
             AND run.started_at = result.run_started_at
        ),
        '[]'::json
    )
);"""
    )
    return "\n".join(lines) + "\n"


def _decoded_comparand(stdout: str, run_id: str, fix: str) -> ComparandRun:
    run, rows = _reader_payload(stdout, run_id, fix)
    return ComparandRun(
        run_id=_text_field(run.get("run_id"), "run id", fix),
        slice=_optional_text_field(run.get("slice"), "slice", fix),
        eval_set_version=_text_field(run.get("eval_set_version"), "eval-set version", fix),
        set_digest=_text_field(run.get("set_digest"), "set digest", fix),
        repeats=_int_field(run.get("repeats"), "repeats", fix),
        kind=_text_field(run.get("kind"), "kind", fix),
        partial=_bool_field(run.get("partial"), "partial state", fix),
        results=tuple(
            (
                _text_field(row.get("case_id"), "case id", fix),
                _int_field(row.get("repeat"), "repeat", fix),
                _mapping_field(row.get("metrics"), "result metrics", fix),
            )
            for row in rows
        ),
    )


def _read_document(
    io: Host, rendered_dir: PathLike, run_id: str, sql: str, fix: str
) -> str | Problem:
    """Run one reader statement as the metrics role and return its stdout."""

    try:
        UUID(run_id)
    except (AttributeError, TypeError, ValueError):
        return Problem(
            f"run id is not a UUID: {run_id!r}",
            "Supply the id of a recorded evaluation run, then retry.",
        )
    try:
        result = io.run(_reader_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("eval reader failed: command could not run", fix)
    if result.returncode != 0:
        return Problem(f"eval reader failed: exit {result.returncode}", fix)
    return result.stdout


def read_run(
    io: Host, rendered_dir: PathLike, run_id: str
) -> tuple[RecordedRun | None, Problem | None]:
    """Read one recorded run and its content-free verdicts, or return a refusal."""

    stdout = _read_document(io, rendered_dir, run_id, _read_sql(run_id), _reader_fix(run_id))
    if isinstance(stdout, Problem):
        return None, stdout
    try:
        return _decoded_run(stdout, run_id), None
    except _ReadRefusal as refused:
        return None, refused.problem


def read_run_metrics(
    io: Host, rendered_dir: PathLike, run_id: str
) -> tuple[ComparandRun | None, Problem | None]:
    """Read a recorded run's code-computed metrics, never its judge mapping.

    A decision pairs against these per-case metrics; the statement selects each
    result's case id, repeat, and ``metrics`` alone, so no judge-derived value
    can reach a paired figure. A reader that cannot run answers with the
    database's logs, since the id itself was well formed.
    """

    fix = stack.logs_fix(rendered_dir, POSTGRES_SERVICE)
    stdout = _read_document(io, rendered_dir, run_id, _metrics_read_sql(run_id), fix)
    if isinstance(stdout, Problem):
        return None, stdout
    try:
        return _decoded_comparand(stdout, run_id, fix), None
    except _ReadRefusal as refused:
        return None, refused.problem


def _run(io: Host, rendered_dir: PathLike, sql: str) -> str | None:
    try:
        result = io.run(_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return "eval writer failed: command could not run"
    if result.returncode != 0:
        return f"eval writer failed: exit {result.returncode}"
    return None


def probe(io: Host, rendered_dir: PathLike) -> str | None:
    """Check that the evaluation writer can execute a database statement."""

    return _run(io, rendered_dir, "SELECT 1;\n")


def write_rows(
    io: Host,
    rendered_dir: PathLike,
    run: RunRow,
    results: Sequence[ResultRow],
) -> str | None:
    """Write one run and its results in one transaction, or return a refusal."""

    lines = [
        "SELECT eval_runs_ensure_partition(:'run_started_at'::timestamptz);",
        "SELECT eval_results_ensure_partition(:'run_started_at'::timestamptz);",
    ]
    try:
        run_sql_lines: list[str] = []
        run_sql = _run_sql(run, run_sql_lines)
        result_sql_lines: list[str] = []
        result_sql = [
            _result_sql(row, result_sql_lines, index)
            for index, row in enumerate(sorted(results, key=lambda value: value.case_id))
        ]
    except ValueError as exc:
        return f"eval row refused: {exc}"

    # The run's timestamp variable is emitted once before the two ensure calls;
    # all following values are likewise psql variables, never SQL literals.
    variable_lines = [f"\\set run_started_at '{_escape(run.started_at.isoformat())}'"]
    lines = variable_lines + lines + run_sql_lines + result_sql_lines
    lines.extend((run_sql, *result_sql))
    return _run(io, rendered_dir, "\n".join(lines) + "\n")

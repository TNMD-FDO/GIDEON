"""Read passing smoke evidence for a production build's sample generation."""

import json
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from gideon.host import report, stack, worker
from gideon.host.index import generations
from gideon.host.index.generations import GenerationInputs
from gideon.host.index.sample import sample_name
from gideon.host.render.worker import WORKER_DATABASE_NAME
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

METRICS_ROLE: Final[str] = "gideon_ro_metrics"
_READER_FLAGS: Final[tuple[str, ...]] = ("-v", "ON_ERROR_STOP=1", "-tA", "-f", "-")
_RESTORE_FIX: Final[str] = "Restore the reference file from the release checkout, then retry."
_REFERENCE_FIELDS: Final[tuple[str, ...]] = (
    "slice", "corpus_lockfile", "generation_id", "verdict", "tag",
)
_RECORD_FIELDS: Final[frozenset[str]] = frozenset({"run_id", "stack", "kind", "finished_at"})
_KINDS: Final[frozenset[str]] = frozenset({
    "smoke", "nightly", "weekly-off", "decision", "candidate", "engine-verify", "manual",
})


@dataclass(frozen=True, slots=True)
class SmokeEvidence:
    """One matching source, with input metadata filled by smoke_evidence."""

    source: Literal["record", "reference"]
    label: str
    pipeline: str
    embedding_space: str
    sample: str
    detail: str


def expected_sample(inputs: GenerationInputs) -> str:
    """Derive the requested label's sample even when its production row converged."""

    return sample_name(generations.generation_name(
        inputs.collection, inputs.label, inputs.embedding_space,
    ))


def read_reference_evidence(
    host: Host, checkout: PathLike, *, label: str, sample: str,
) -> SmokeEvidence | None | Problem:
    """Read only the reference headers needed to identify a passing sample run."""

    directory = Path(checkout) / "eval/reference/smoke"
    try:
        names = sorted(host.listdir(directory))
    except FileNotFoundError:
        return None
    except OSError:
        return Problem(f"could not list smoke references in {directory}", _RESTORE_FIX)
    evidence = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = directory / name
        try:
            document = json.loads(host.read_text(path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return Problem(f"could not read smoke reference {path}", _RESTORE_FIX)
        if (
            not isinstance(document, dict)
            or type(document.get("format")) is not int
            or document["format"] not in {1, 2}
        ):
            return Problem(f"invalid smoke reference {path}", _RESTORE_FIX)
        if document["format"] == 1:
            continue
        if any(not isinstance(document.get(key), str) for key in _REFERENCE_FIELDS):
            return Problem(f"invalid smoke reference {path}", _RESTORE_FIX)
        if (
            evidence is None
            and document["slice"] == "smoke" and document["verdict"] == "pass"
            and document["corpus_lockfile"] == label and document["generation_id"] == sample
        ):
            evidence = SmokeEvidence(
                "reference", label, "", "", sample, f"reference {document['tag']} ({name})",
            )
    return evidence


def reader_argv(rendered_dir: PathLike) -> list[str]:
    """Build the metrics role's stdin-fed psql command without importing eval."""

    return stack.exec_argv(
        rendered_dir, "postgres", "psql", "-U", METRICS_ROLE,
        "-d", WORKER_DATABASE_NAME, *_READER_FLAGS,
    )


def read_record_evidence(
    host: Host, rendered_dir: PathLike, *, label: str, sample: str,
) -> SmokeEvidence | None | Problem:
    """Read the newest complete passing smoke run naming the requested sample."""

    sql = "\n".join((
        worker.bind("v_label", label), worker.bind("v_sample", sample),
        """SELECT json_build_object(
    'run_id', run_id, 'stack', stack, 'kind', kind,
    'finished_at', to_char(
        finished_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
    )
)
FROM public.eval_runs
WHERE slice = 'smoke' AND verdict = 'pass' AND partial = false
    AND corpus_lockfile = :'v_label' AND generation_id = :'v_sample'
ORDER BY started_at DESC
LIMIT 1;
""",
    ))
    fix = f"Run {stack.logs_fix(rendered_dir, 'postgres')}, then retry."
    try:
        result = host.run(reader_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("smoke reader failed: command could not run", fix)
    if result.returncode != 0:
        return Problem(f"smoke reader failed: exit {result.returncode}", fix)
    if not result.stdout.strip():
        return None
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Problem("smoke reader returned invalid JSON", fix)
    if (
        not isinstance(document, dict) or set(document) != _RECORD_FIELDS
        or any(not isinstance(value, str) for value in document.values())
        or document["stack"] not in {"ci", "production"} or document["kind"] not in _KINDS
    ):
        return Problem("smoke reader returned an invalid run row", fix)
    try:
        UUID(document["run_id"])
        finished = datetime.fromisoformat(document["finished_at"])
    except ValueError:
        return Problem("smoke reader returned an invalid run id or finished time", fix)
    if finished.tzinfo is None or finished.utcoffset() is None:
        return Problem("smoke reader returned a finished time without a timezone", fix)
    minute = finished.astimezone(UTC).isoformat(timespec="minutes").replace("+00:00", "Z")
    return SmokeEvidence(
        "record", label, "", "", sample,
        f"run {document['run_id']} ({document['stack']}, {document['kind']}, finished {minute})",
    )


def smoke_evidence(
    host: Host, rendered_dir: PathLike, checkout: PathLike,
    inputs: GenerationInputs, command_path: str,
) -> SmokeEvidence | Problem:
    """Prefer committed evidence, then the record, or name the commands that supply it."""

    sample = expected_sample(inputs)
    evidence = read_reference_evidence(host, checkout, label=inputs.label, sample=sample)
    if evidence is None:
        evidence = read_record_evidence(host, rendered_dir, label=inputs.label, sample=sample)
    if isinstance(evidence, Problem):
        return evidence
    if evidence is not None:
        return replace(evidence, pipeline=inputs.pipeline, embedding_space=inputs.embedding_space)
    build = report.command(f"index build caselaw --sample --lockfile {inputs.label}")
    smoke = report.command(f"eval run --slice smoke --stack ci --kind smoke --generation {sample}")
    return Problem(
        f"no passing smoke run for lockfile {inputs.label}, pipeline {inputs.pipeline}, "
        f"embedding space {inputs.embedding_space}: none names generation {sample} "
        "among eval/reference/smoke or the record's runs",
        f"Run {build} on the sibling, then {smoke}, then retry {report.command(command_path)}.",
    )


def stage_detail(evidence: SmokeEvidence) -> str:
    """Format the evidence as the production command's smoke row detail."""

    return (
        f"lockfile {evidence.label}, pipeline {evidence.pipeline}, "
        f"embedding space {evidence.embedding_space}: {evidence.detail}"
    )

"""Gate gold-document drops against a generation's recorded canonical-text hashes."""

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from gideon.evaluation import bound, record, sample
from gideon.evaluation.evalset import SHAPE_REGISTRY, LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.host import report, stack, staging
from gideon.host.index.sample import (
    SAMPLE_SUFFIX,
    SOURCE_ID_MAX_LENGTH,
    SOURCE_ID_PATTERN,
)
from gideon.host.report import Problem
from gideon.host.worker import bind

# exempt: the gold-drop ceiling is a release decision, never moved to pass a generation.
GOLD_DROP_CEILING_PERCENT: Final[int] = 5
PROBLEMS: Final[frozenset[str]] = frozenset({"record-unreachable", "generation-unresolved"})
MANIFEST_SQL: Final[str] = """SELECT row_to_json(manifest) FROM (
    SELECT source_id, canonical_text_sha256 FROM public.generation_documents
    WHERE gen_id = :'v_name' AND source_id IN
        (SELECT jsonb_array_elements_text(:'v_source_ids'::jsonb))
    ORDER BY source_id
) AS manifest;
"""
GIVEN_SQL: Final[str] = """SELECT row_to_json(given) FROM (
    SELECT source_id FROM public.generation_gold
    WHERE gen_id = :'v_name' AND source_id IN
        (SELECT jsonb_array_elements_text(:'v_source_ids'::jsonb))
    ORDER BY source_id
) AS given;
"""


@dataclass(frozen=True, slots=True)
class DropVerdict:
    """The absent and changed gold source ids, each sorted and counted once."""

    absent: tuple[str, ...]
    changed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Whether the zero-tolerance and gold-document drop bounds hold."""

    ok: bool
    reasons: tuple[str, ...]


def dropped_documents(
    gold: Mapping[str, frozenset[str]], manifest: Mapping[str, str],
) -> DropVerdict:
    """Drop an absent id or one whose manifest hash differs from any judged hash."""

    absent = tuple(sorted(identifier for identifier in gold if identifier not in manifest))
    changed = tuple(sorted(
        identifier for identifier, hashes in gold.items()
        if identifier in manifest and any(sha256 != manifest[identifier] for sha256 in hashes)
    ))
    return DropVerdict(absent, changed)


def slice_gate(zero_tolerance_dropped: int, dropped: int, total: int) -> GateVerdict:
    """Apply both drop bounds with exact integers, including the zero-gold case."""

    reasons: list[str] = []
    if zero_tolerance_dropped:
        reasons.append("zero-tolerance gold cases dropped")
    if dropped * 100 > GOLD_DROP_CEILING_PERCENT * total:
        reasons.append("gold documents dropped above the ceiling")
    return GateVerdict(not reasons, tuple(reasons))


def _read_manifest(
    name: str, source_ids: tuple[str, ...], context: RunContext,
) -> Mapping[str, str] | Problem:
    """Read requested gold hashes in one metrics-role statement on the selected stack."""

    sql = (
        bind("v_name", name) + "\n"
        + bind("v_source_ids", json.dumps(source_ids, separators=(",", ":")))
        + "\n" + MANIFEST_SQL
    )
    fix = f"Run {stack.logs_fix(context.turns_dir, record.POSTGRES_SERVICE)}, then retry."
    try:
        result = context.host.run(record.reader_argv(context.turns_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("generation manifest read could not run", fix)
    if result.returncode != 0:
        return Problem(f"generation manifest read failed (exit {result.returncode})", fix)
    requested = frozenset(source_ids)
    hashes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return Problem("generation manifest row is invalid", fix)
        if (
            not isinstance(value, dict) or set(value) != {"source_id", "canonical_text_sha256"}
            or not isinstance(value["source_id"], str)
            or not 1 <= len(value["source_id"]) <= SOURCE_ID_MAX_LENGTH
            or re.fullmatch(SOURCE_ID_PATTERN, value["source_id"]) is None
            or value["source_id"] not in requested or value["source_id"] in hashes
            or not isinstance(value["canonical_text_sha256"], str)
            or staging.DIGEST_PATTERN.fullmatch(value["canonical_text_sha256"]) is None
        ):
            return Problem("generation manifest row is invalid", fix)
        hashes[value["source_id"]] = value["canonical_text_sha256"]
    return hashes


def _read_given(
    name: str, source_ids: tuple[str, ...], context: RunContext,
) -> frozenset[str] | Problem:
    """Read requested given gold in one metrics-role statement on the selected stack."""

    sql = (
        bind("v_name", name) + "\n"
        + bind("v_source_ids", json.dumps(source_ids, separators=(",", ":")))
        + "\n" + GIVEN_SQL
    )
    fix = f"Run {stack.logs_fix(context.turns_dir, record.POSTGRES_SERVICE)}, then retry."
    try:
        result = context.host.run(record.reader_argv(context.turns_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("generation gold read could not run", fix)
    if result.returncode != 0:
        return Problem(f"generation gold read failed (exit {result.returncode})", fix)
    requested = frozenset(source_ids)
    given: set[str] = set()
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return Problem("generation gold row is invalid", fix)
        if (
            not isinstance(value, dict) or set(value) != {"source_id"}
            or not isinstance(value["source_id"], str)
            or not 1 <= len(value["source_id"]) <= SOURCE_ID_MAX_LENGTH
            or re.fullmatch(SOURCE_ID_PATTERN, value["source_id"]) is None
            or value["source_id"] not in requested or value["source_id"] in given
        ):
            return Problem("generation gold row is invalid", fix)
        given.add(value["source_id"])
    return frozenset(given)


def _failed(
    counted: tuple[str, ...], unsigned: int, code: str, problem: Problem,
) -> SliceResult:
    """Fail every counted case and render the content-free problem and its fix."""

    results = tuple(CaseResult(case_id, 1, "fail", {"problem": code}) for case_id in counted)
    lines = (
        f"{len(counted)} counted, {unsigned} unsigned excluded",
        *report.failure_lines(problem.fix, f"{code}: {problem.problem}"),
        "gold-drop: fail",
    )
    return SliceResult(False, "\n".join(lines) + "\n", results)


def run_gold_drop(loaded: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Measure signed cases and every gold hash against the resolved generation."""

    selection = select_cases(loaded, slice_name)
    generation = context.generation
    if generation is None:
        return _failed(selection.counted, len(selection.unsigned), "generation-unresolved", Problem(
            "the stores generation was not resolved",
            f"Run {report.command(f'eval run --slice {slice_name} --generation <name>')} "
            "through the command's stores preconditions, then retry.",
        ))
    context.checkpoint()
    gold = sample.gold_hashes(loaded)
    manifest = _read_manifest(generation.name, tuple(gold), context)
    if isinstance(manifest, Problem):
        return _failed(selection.counted, len(selection.unsigned), "record-unreachable", manifest)

    sampled = generation.name.endswith(f"__{SAMPLE_SUFFIX}")
    unbuilt: tuple[str, ...] = ()
    if sampled:
        given = _read_given(generation.name, tuple(gold), context)
        if isinstance(given, Problem):
            return _failed(selection.counted, len(selection.unsigned), "record-unreachable", given)
        # The recorded given list decides which absent gold was never built; with none recorded,
        # every absent id is dropped until the sample is refilled.
        if given:
            unbuilt = tuple(identifier for identifier in gold if identifier not in manifest and identifier not in given)
            gold = {identifier: hashes for identifier, hashes in gold.items() if identifier in manifest or identifier in given}
    drops = dropped_documents(gold, manifest)
    dropped = set(drops.absent) | set(drops.changed)
    signoffs = {signoff.case_id: signoff for signoff in loaded.signoffs}
    results: list[CaseResult] = []
    dropped_cases: list[str] = []
    zero_tolerance_cases: list[str] = []
    gated_cases = 0
    for case_id in selection.counted:
        context.checkpoint()
        must_cite = signoffs[case_id].must_cite
        case_dropped = sorted(set(must_cite) & dropped)
        case = loaded.cases_by_id[case_id]
        category = (cast(str, case["suite"]), cast(str, case["category"]))
        zero_tolerance = SHAPE_REGISTRY[category].zero_tolerance
        gated_cases += int(zero_tolerance)
        if case_dropped:
            dropped_cases.append(case_id)
            if zero_tolerance:
                zero_tolerance_cases.append(case_id)
        results.append(CaseResult(case_id, 1, "fail" if case_dropped else "pass", {
            "must_cite": len(must_cite), "dropped": list(case_dropped),
            "zero_tolerance": zero_tolerance,
        }))

    lost_coordinates: dict[str, set[str]] = {}
    for judgment in loaded.judgments:
        if judgment.source_id in gold and manifest.get(judgment.source_id) != judgment.sha256:
            lost_coordinates.setdefault(judgment.query_id, set()).add(
                f"{judgment.source_id}@{judgment.sha256}"
            )
    gate = slice_gate(len(zero_tolerance_cases), len(dropped), len(gold))
    lines = [
        f"generation {generation.name} ({generation.corpus_lockfile})",
        f"{len(selection.counted)} counted, {len(selection.unsigned)} unsigned excluded",
        *((f"unbuilt gold documents {len(unbuilt)} (not given to the sample's build, not counted): "
           + (" ".join(unbuilt) or "none"),) if sampled else ()),
        f"gold documents {len(gold)}: dropped {len(dropped)} "
        f"(absent {len(drops.absent)}, hash changed {len(drops.changed)}), "
        f"ceiling {GOLD_DROP_CEILING_PERCENT} %",
        f"absent source ids: {' '.join(drops.absent) or 'none'}",
        f"hash changed source ids: {' '.join(drops.changed) or 'none'}",
        f"zero-tolerance cases dropped {len(zero_tolerance_cases)}: "
        + (" ".join(zero_tolerance_cases) or "none"),
        bound.bound_text(gated_cases, gate.ok),
        f"cases dropped (reported) {len(dropped_cases)}: " + (" ".join(dropped_cases) or "none"),
        f"judged queries with dropped coordinates {len(lost_coordinates)}:",
    ]
    for query_id, coordinates in sorted(lost_coordinates.items()):
        lines.append(f"{query_id}: {' '.join(sorted(coordinates))}")
    lines.extend(f"gate: {reason}" for reason in gate.reasons)
    lines.append(f"gold-drop: {'pass' if gate.ok else 'fail'}")
    return SliceResult(gate.ok, "\n".join(lines) + "\n", tuple(results), gated_cases=gated_cases)

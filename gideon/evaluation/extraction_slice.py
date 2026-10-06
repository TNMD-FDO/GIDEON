"""Run both exact-object extractors over the frozen extraction slice."""

import contextlib
import json
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from typing import Final, cast

from gideon.evaluation.evalset import Case, LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.extraction import ExactObject
from gideon.extraction.contract import (
    EYECITE_TYPES,
    combine_extractions,
    object_from_wire,
    span_violations,
)
from gideon.extraction.grammar import extract, registry_types
from gideon.extraction.scoring import SetScore, build_report, score
from gideon.host import stack
from gideon.host.report import failure_lines

IMAGE_TIMEOUT_SECONDS: Final[int] = 120
REMOVE_TIMEOUT_SECONDS: Final[int] = 30
CONTAINER_PREFIX: Final[str] = "gideon-casecite-"
# The closed problem codes of a failed row: a citation leg that could not
# supply trusted objects fails every counted case, so a reference comparison
# reads the cases regressed rather than dropped.
IMAGE_LEG_FAILED: Final[str] = "image-leg-failed"
PROBLEMS: Final[frozenset[str]] = frozenset({IMAGE_LEG_FAILED})


def _image_objects(
    cases: tuple[Case, ...], context: RunContext
) -> tuple[dict[str, tuple[ExactObject, ...]] | None, str, float]:
    """Read one image run's complete, validated reply or a content-free problem."""

    image = context.image
    assert image is not None
    questions = {cast(str, case["id"]): cast(str, case["question"]) for case in cases}
    request = "".join(
        json.dumps({"id": case_id, "text": question}) + "\n"
        for case_id, question in questions.items()
    )
    name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:12]}"
    started = time.monotonic()
    try:
        result = context.host.run(
            stack.image_run_argv(
                image.reference, image.checkout, "python", "-m", "gideon.casecite",
                name=name,
            ),
            input=request,
            timeout=IMAGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # The timeout kills the Docker client alone; --rm removes the
        # container only once it exits, so a stuck extractor is removed here.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            context.host.run(stack.container_remove_argv(name), timeout=REMOVE_TIMEOUT_SECONDS)
        return None, "container timed out", time.monotonic() - started
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None, "container could not run", time.monotonic() - started
    elapsed = time.monotonic() - started
    lines = result.stdout.splitlines()
    if result.returncode != 0:
        return None, f"container exited {result.returncode}; {len(lines)} reply lines", elapsed

    replies: dict[str, tuple[ExactObject, ...]] = {}
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None, f"unreadable reply line {line_number} of {len(lines)}", elapsed
        if not isinstance(value, dict) or set(value) != {"id", "objects"}:
            return None, f"invalid reply line {line_number} of {len(lines)}", elapsed
        case_id = value["id"]
        if not isinstance(case_id, str) or case_id not in questions:
            return None, "reply for unknown id", elapsed
        if case_id in replies:
            return None, "duplicate reply id", elapsed
        raw_objects = value["objects"]
        if not isinstance(raw_objects, list):
            return None, "invalid reply objects", elapsed
        try:
            objects = tuple(object_from_wire(item) for item in raw_objects)
        except ValueError:
            return None, "invalid reply object", elapsed
        if any(obj.type not in EYECITE_TYPES for obj in objects):
            return None, "reply type outside citation extractor", elapsed
        if span_violations(questions[case_id], objects):
            return None, "reply span does not match question", elapsed
        replies[case_id] = objects
    if len(replies) != len(questions):
        return None, f"{len(replies)} replies for {len(questions)} counted cases", elapsed
    return replies, "", elapsed


def _objects(case: Case) -> tuple[Mapping[str, object], ...]:
    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        return ()
    values = expected.get("objects")
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, Mapping))


def _type_counts(
    case: Case,
    extracted: tuple[ExactObject, ...],
    result: SetScore,
    landed: frozenset[str],
) -> tuple[Mapping[str, Mapping[str, int]], str]:
    case_id = cast(str, case["id"])
    labels: Counter[str] = Counter()
    for value in _objects(case):
        object_type = value.get("type")
        if isinstance(object_type, str):
            labels[object_type] += 1
    misses: Counter[str] = Counter(
        finding.type for finding in result.misses if finding.case_id == case_id
    )
    false_hits: Counter[str] = Counter(
        finding.type for finding in result.false_hits if finding.case_id == case_id
    )
    types = sorted(set(labels) | {value.type for value in extracted})
    metrics: dict[str, Mapping[str, int]] = {}
    for object_type in types:
        miss_count = misses[object_type]
        metrics[object_type] = {
            "hits": labels[object_type] - miss_count,
            "false_hits": false_hits[object_type],
            "misses": miss_count,
        }
    failed = any(
        metrics[object_type]["misses"] or metrics[object_type]["false_hits"]
        for object_type in landed
        if object_type in metrics
    )
    return metrics, "fail" if failed else "pass"


def run_extraction(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Run the extraction measure for *slice_name* in deterministic id order."""

    selected = select_cases(eval_set, slice_name)
    cases = tuple(eval_set.cases_by_id[case_id] for case_id in selected.counted)
    extracted: dict[str, tuple[ExactObject, ...]] = {}
    latencies: dict[str, float] = {}
    for case in cases:
        case_id = cast(str, case["id"])
        question = case.get("question")
        source = question if isinstance(question, str) else ""
        started = time.monotonic_ns()
        extracted[case_id] = extract(source)
        latencies[case_id] = (time.monotonic_ns() - started) / 1_000_000

    leg_line = ""
    if context.image is not None:
        citations, problem, elapsed = _image_objects(cases, context)
        leg_line = f"image leg elapsed {elapsed:.3f} s\n"
        if citations is None:
            registry = context.image.reference.rsplit("/gideon@", 1)[0]
            fix = (
                "Run sudo python3 -m tools.imagebuild gideon "
                f"--to {registry} --check, then retry."
            )
            lines = failure_lines(
                fix,
                f"image leg failed: {problem}; {len(cases)} counted cases failed",
            )
            failed_rows = tuple(
                CaseResult(
                    cast(str, case["id"]), 1, "fail", {"problem": IMAGE_LEG_FAILED},
                    latency_ms=latencies[cast(str, case["id"])],
                )
                for case in cases
            )
            return SliceResult(False, "\n".join(lines) + "\n" + leg_line, failed_rows)
        extracted = {
            case_id: combine_extractions(extracted[case_id], citations[case_id])
            for case_id in extracted
        }

    landed = frozenset((*registry_types(), *(EYECITE_TYPES if context.image is not None else ())))
    result = score((cases,), extracted, landed)
    case_results: list[CaseResult] = []
    for case in cases:
        case_id = cast(str, case["id"])
        metrics, verdict = _type_counts(case, extracted[case_id], result, landed)
        case_results.append(
            CaseResult(case_id, 1, verdict, metrics, judge=None, latency_ms=latencies[case_id])
        )
    return SliceResult(result.verdict, build_report(result) + leg_line, tuple(case_results))

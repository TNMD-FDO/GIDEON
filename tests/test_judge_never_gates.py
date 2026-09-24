"""Independent invariants that keep judge scores out of the evaluation gate."""

import ast
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import cast

from test_guardrails_slice import _DECLINE, SAMPLE_IDS, JudgingDoorHost, _fixture_turns
from test_judge import false_refusal_content, valid_content

from gideon import guardrail
from gideon.evaluation import command, judge
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set
from gideon.evaluation.results import RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.evaluation.turns import classify
from gideon.host import courts
from gideon.host.sysio import Host, PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
GATE_ATTRIBUTES: frozenset[str] = frozenset(
    {"verdict", "gate_pass", "gate_fail", "gate_fix"}
)
REFERENCE_GATE_ATTRIBUTES: frozenset[str] = GATE_ATTRIBUTES | frozenset(
    {
        # the comparison, folded from (case id, repeat, verdict) alone
        "Comparison",
        "outcome",
        "regressed",
        "tag",
        # the reference module's fix texts, and the list the fixes join from
        "REGRESSION_FIX",
        "SLICE_REPAIR_FIX",
        "append",
        "join",
    }
)
GATE_FUNCTIONS: tuple[tuple[ModuleType, str, frozenset[str]], ...] = (
    (command, "_gate", GATE_ATTRIBUTES),
    (command, "_reference_gate", REFERENCE_GATE_ATTRIBUTES),
)
"""Every function that prints a gate row, with the attributes it may read. A
ticket whose gate reads something new adds its function and its names here."""
FORBIDDEN_METRIC_NAMES: frozenset[str] = frozenset(
    {"judge", "score", "band", "failure_mode"}
)
"""Tokens a metrics key may not CONTAIN, so ``failure_modes``, ``in_band``, and
``raw_score`` are caught as surely as the bare words are."""



VERDICT_PAIRS: Mapping[str, tuple[tuple[str, int, int, float], tuple[str, int, int, float]]] = {
    "synthesis_verdict": (
        (
            valid_content(
                score=3,
                reason="A visibly fictitious complete ruling.",
                modes=(),
            ),
            17,
            9,
            2.5,
        ),
        (
            valid_content(
                score=0,
                reason="A visibly fictitious contradictory ruling.",
                modes=("contradicts-reference",),
            ),
            17,
            9,
            2.5,
        ),
    ),
    "false_refusal_verdict": (
        (
            false_refusal_content(
                withheld=True,
                reason="A visibly fictitious withheld reading.",
            ),
            19,
            11,
            3.5,
        ),
        (
            false_refusal_content(
                withheld=False,
                reason="A visibly fictitious answered reading.",
            ),
            37,
            29,
            6.5,
        ),
    ),
}


def _loaded() -> LoadedSet:
    court_result = courts.load_court_map(ROOT / "courts.yaml")
    assert court_result.court_map is not None
    result = load_set(ROOT / SET_ROOT, court_result.court_map.courts)
    assert result.loaded is not None, result.findings
    return result.loaded


def _run_registered(
    *, variation: int
) -> tuple[LoadedSet, Mapping[str, SliceResult]]:
    loaded = _loaded()
    results: dict[str, SliceResult] = {}
    for slice_name, spec in SLICE_RUNNERS.items():
        if slice_name not in loaded.slices:
            # A registered slice the set lacks is the export boundary's doing
            # and nothing else's: in the development tree every entry is driven,
            # and an omission here must be one the boundary itself reports.
            assert absent_from_export(SET_ROOT / "slices" / slice_name, ROOT), (
                f"slice {slice_name} is registered but the set has no id list "
                "for it, and the export boundary does not omit its directory"
            )
            continue
        prompt = (
            None
            if spec.judge_prompt is None
            else judge.PROMPT_REGISTRY[spec.judge_prompt]
        )
        schema_name = "synthesis_verdict" if prompt is None else prompt.schema_name
        pair = VERDICT_PAIRS[schema_name]
        default_reading = pair[variation]
        host = JudgingDoorHost(default=default_reading)
        turns = None
        rendered_dir: PathLike = "/rendered"
        served_model = "fixture-model" if spec.reaches_engine else None
        if spec.drives_turns and spec.judge_prompt is not None:
            assert classify.DECLINE_FORM.match(_DECLINE)
            assert len(_DECLINE) <= classify.DECLINE_MAX_CHARS
            active = set(loaded.active_ids)
            selected_ids = tuple(
                case_id
                for case_id in loaded.slices[slice_name]
                if case_id in active
            )
            _host, _frontend, turn_context = _fixture_turns(
                replace(loaded, active_ids=selected_ids), host=host
            )
            rendered_dir = turn_context.rendered_dir
            served_model = turn_context.served_model_name
            for case_id in loaded.slices[slice_name]:
                if case_id not in active:
                    continue
                case = loaded.cases_by_id[case_id]
                role = cast(list[str], case["labels"])[1]
                host.answers[case_id] = (
                    guardrail.DEADLINE_REFUSAL if role == "positive" else _DECLINE
                )
            turns = turn_context.turns
        progress: list[str] = []
        context = RunContext(
            cast(Host, host),
            rendered_dir,
            served_model,
            spec.judge_prompt,
            spec.repeats,
            progress.append,
            turns=turns,
        )
        results[slice_name] = spec.runner(loaded, slice_name, context)
    return loaded, results


def _metric_offenders(value: object) -> tuple[str, ...]:
    offenders: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and any(
                token in key.lower() for token in FORBIDDEN_METRIC_NAMES
            ):
                offenders.append(key)
            offenders.extend(_metric_offenders(item))
    elif isinstance(value, list):
        for item in value:
            offenders.extend(_metric_offenders(item))
    return tuple(offenders)


class Invariance(unittest.TestCase):
    """Every registered runner keeps judge-derived variation inside judge."""

    def test_every_registered_slice_is_driven_and_never_gates_on_verdict_fields(self) -> None:
        first_loaded, first = _run_registered(
            variation=0,
        )
        second_loaded, second = _run_registered(
            variation=1,
        )
        # A prompt registered later must bring its canned pair, or the
        # invariance would be proven over no judge fields of its schema.
        registered_schemas = {prompt.schema_name for prompt in judge.PROMPT_REGISTRY.values()}
        self.assertEqual(registered_schemas, set(VERDICT_PAIRS))
        self.assertEqual(first_loaded.slices, second_loaded.slices)
        self.assertEqual(set(first), set(second))
        for slice_name in first:
            spec = SLICE_RUNNERS[slice_name]
            with self.subTest(slice_name=slice_name):
                first_result = first[slice_name]
                second_result = second[slice_name]
                self.assertEqual(first_result.verdict, second_result.verdict)
                self.assertEqual(
                    tuple((result.verdict, result.metrics) for result in first_result.results),
                    tuple((result.verdict, result.metrics) for result in second_result.results),
                )
                active = set(first_loaded.active_ids)
                expected_ids = tuple(
                    case_id
                    for case_id in first_loaded.slices[slice_name]
                    if case_id in active
                )
                self.assertEqual(
                    {result.case_id for result in first_result.results},
                    set(expected_ids),
                )
                self.assertEqual(
                    len(first_result.results), len(expected_ids) * spec.repeats
                )
                if spec.drives_turns and spec.judge_prompt is not None:
                    controls = tuple(
                        result
                        for result in first_result.results
                        if result.metrics["role"] == "control"
                    )
                    self.assertTrue(controls)
                    self.assertTrue(
                        all(
                            result.metrics["class"] == "declined"
                            and result.judge is not None
                            for result in controls
                        )
                    )
                    self.assertTrue(
                        all(
                            result.judge is None
                            for result in first_result.results
                            if result.metrics["role"] == "positive"
                        )
                    )
                    self.assertTrue(
                        all(
                            result.metrics["class"] == "replaced"
                            for result in first_result.results
                            if result.metrics["role"] == "positive"
                        )
                    )
                    frontend_rows = tuple(
                        result
                        for result in first_result.results
                        if result.case_id in SAMPLE_IDS
                    )
                    self.assertEqual(len(frontend_rows), len(SAMPLE_IDS))
                    self.assertTrue(
                        all("frontend" in result.metrics for result in frontend_rows)
                    )

        # The invariance above is only meaningful if the two runs really did
        # differ, so every judging slice in the registry — not one named here —
        # must show the variation inside its judge field. A slice registered by
        # a later ticket is held to this on registration.
        judging = tuple(
            name for name in first if SLICE_RUNNERS[name].judge_prompt is not None
        )
        self.assertTrue(judging, "no judging slice is registered")
        for slice_name in judging:
            with self.subTest(slice_name=slice_name):
                first_judge = next(
                    (result.judge for result in first[slice_name].results if result.judge is not None),
                    None,
                )
                second_judge = next(
                    (result.judge for result in second[slice_name].results if result.judge is not None),
                    None,
                )
                assert first_judge is not None
                assert second_judge is not None
                self.assertNotEqual(first_judge, second_judge)


class MetricNames(unittest.TestCase):
    """Metrics cannot acquire names that expose judge-derived values."""

    def test_every_nested_metric_mapping_and_list_is_content_free(self) -> None:
        _loaded_set, results = _run_registered(
            variation=0,
        )
        for slice_name, slice_result in results.items():
            for result in slice_result.results:
                with self.subTest(slice_name=slice_name, case_id=result.case_id):
                    self.assertEqual(_metric_offenders(result.metrics), ())
        # Positive controls: the walker must reach into nested lists, and must
        # catch a compound or plural key, not only the four bare words.
        self.assertEqual(_metric_offenders({"outer": [{"safe": 1}]}), ())
        self.assertEqual(_metric_offenders({"outer": [{"score": 1}]}), ("score",))
        self.assertEqual(_metric_offenders({"failure_modes": []}), ("failure_modes",))
        self.assertEqual(_metric_offenders({"in_band": True}), ("in_band",))
        self.assertEqual(_metric_offenders({"a": {"b": [{"raw_score": 2}]}}), ("raw_score",))


class GateAST(unittest.TestCase):
    """Each gate function reads only the attributes registered for it."""

    def test_gate_functions_have_no_other_attribute_reads(self) -> None:
        for module, function_name, allowed in GATE_FUNCTIONS:
            with self.subTest(module=module.__name__, function=function_name):
                source_path = Path(cast(str, module.__file__))
                tree = ast.parse(source_path.read_text(encoding="utf-8"))
                functions = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == function_name
                ]
                self.assertEqual(len(functions), 1)
                attributes = {
                    node.attr
                    for node in ast.walk(functions[0])
                    if isinstance(node, ast.Attribute)
                }
                self.assertTrue(attributes <= allowed, attributes - allowed)

    def test_every_gate_row_is_printed_by_a_walked_function(self) -> None:
        """A gate row printed elsewhere would be a gate no whitelist holds."""

        tree = ast.parse(Path(cast(str, command.__file__)).read_text(encoding="utf-8"))
        printers = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "StageResult"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "gate"
        }
        walked = {name for module, name, _allowed in GATE_FUNCTIONS if module is command}
        self.assertEqual(printers, walked)


if __name__ == "__main__":
    unittest.main()

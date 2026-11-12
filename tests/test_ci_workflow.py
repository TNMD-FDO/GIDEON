"""Check CI runner routing, box label equality, concurrency, guarded steps, and gate flags."""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from gideon.host.steps.services import RUNNER_INSTANCES

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
_PRIVATE_PUSH = re.compile(
    r"github\.event_name\s*==\s*'push'\s*&&\s*github\.event\.repository\.private"
)
_FROM_JSON = re.compile(r"fromJSON\('([^']+)'\)")
_HOSTED_FALLBACK = re.compile(r"\|\|\s*'ubuntu-latest'\s*}}$")
_BOX_LABEL = re.compile(r"['\"]self-hosted['\"]")
_STEP_GUARD = re.compile(
    r"runner\.environment\s*==\s*'(?P<environment>github-hosted|self-hosted)'"
    r"(?:\s*&&\s*github\.event_name\s*(?P<event>==|!=)\s*'pull_request')?"
)
_PULL_REQUEST_GATE = "runner.environment == 'github-hosted' && github.event_name == 'pull_request'"
_HOSTED_GATE = "runner.environment == 'github-hosted' && github.event_name != 'pull_request'"
_BOX_GATE = "runner.environment == 'self-hosted'"


def _can_name_box(runner: object) -> bool:
    if isinstance(runner, list):
        return "self-hosted" in runner
    if isinstance(runner, str):
        return runner == "self-hosted" or _BOX_LABEL.search(runner) is not None
    return False


def _label_set(runner: object) -> set[str] | None:
    if not isinstance(runner, list) or not all(isinstance(label, str) for label in runner):
        return None
    return {label.casefold() for label in runner}


def workflow_findings(workflow: dict[str, Any]) -> list[str]:
    """Name each workflow, runner, or step contract the parsed workflow violates."""

    findings: list[str] = []
    jobs = workflow["jobs"]
    checks = jobs["checks"]
    if "concurrency" in workflow:
        findings.append("workflow: concurrency")
    for name, job in jobs.items():
        if "concurrency" in job:
            findings.append(f"{name}: concurrency")
        runner = job["runs-on"]
        guarded = _PRIVATE_PUSH.search(str(job.get("if", ""))) or _PRIVATE_PUSH.search(
            str(runner)
        )
        if _can_name_box(runner) and not guarded:
            findings.append(f"{name}: private push guard")

    runner = checks["runs-on"]
    if not isinstance(runner, str) or _HOSTED_FALLBACK.search(runner) is None:
        findings.append("checks: hosted fallback")
    label_match = _FROM_JSON.search(runner) if isinstance(runner, str) else None
    try:
        labels = json.loads(label_match.group(1)) if label_match else None
    except json.JSONDecodeError:
        labels = None
    checks_labels = _label_set(labels)
    box_labels = {
        name: _label_set(job["runs-on"])
        for name, job in jobs.items()
        if name != "checks" and _can_name_box(job["runs-on"])
    }
    if box_labels:
        label_sets = list(box_labels.values())
        common_labels = max(label_sets, key=label_sets.count)
        for name, job_labels in box_labels.items():
            if job_labels != common_labels:
                findings.append(f"{name}: box label set")
    if (
        not box_labels
        or checks_labels is None
        or "self-hosted" not in checks_labels
        or any(
            job_labels is None
            or "self-hosted" not in job_labels
            or not checks_labels - job_labels
            or not job_labels - checks_labels
            for job_labels in box_labels.values()
        )
    ):
        findings.append("checks: box labels")
    first_labels = {label.casefold() for label in RUNNER_INSTANCES[0].labels}
    checks_instance_labels = {label.casefold() for label in RUNNER_INSTANCES[-1].labels}
    if checks_labels is None or not checks_labels <= checks_instance_labels or checks_labels <= first_labels:
        findings.append("checks: routing")
    for name, job_labels in box_labels.items():
        if job_labels is None or not job_labels <= first_labels or job_labels <= checks_instance_labels:
            findings.append(f"{name}: routing")
    if not isinstance(checks.get("timeout-minutes"), int) or checks["timeout-minutes"] <= 0:
        findings.append("checks: timeout")

    steps = checks["steps"]
    hosted: dict[str | None, list[str]] = {"==": [], "!=": [], None: []}
    box: list[str] = []
    for index, step in enumerate(steps[1:], 1):
        guard = _STEP_GUARD.fullmatch(str(step.get("if", "")).strip())
        if guard is None or (
            guard.group("environment") == "self-hosted" and guard.group("event") is not None
        ):
            findings.append(f"checks: step {index} environment guard")
            continue
        run = step.get("run", "")
        if "-m tools.gate" not in run:
            continue
        if guard.group("environment") == "self-hosted":
            box.append(run)
        else:
            hosted[guard.group("event")].append(run)

    if hosted[None]:
        findings.append("checks: hosted gate events")
    for event, required, forbidden, finding in (
        ("==", {"--all", "--pin-selection"}, {"--masked"}, "checks: pull request gate flags"),
        ("!=", {"--all"}, {"--masked", "--pin-selection"}, "checks: hosted gate flags"),
    ):
        commands = hosted[event]
        if len(commands) != 1 or not required.issubset(commands[0].split()) or forbidden.intersection(commands[0].split()):
            findings.append(finding)
    if len(box) != 1 or not {"--all", "--masked"}.issubset(box[0].split()) or "--pin-selection" in box[0].split():
        findings.append("checks: box gate flags")
    return findings


class CiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def seeded_findings(self, workflow: dict[str, Any]) -> list[str]:
        return workflow_findings(yaml.safe_load(yaml.safe_dump(workflow)))

    def gate_step(self, workflow: dict[str, Any], condition: str) -> dict[str, Any]:
        steps = [
            step for step in workflow["jobs"]["checks"]["steps"]
            if step.get("if") == condition and "-m tools.gate" in step.get("run", "")
        ]
        self.assertEqual(len(steps), 1, f"expected one gate step with {condition}")
        return steps[0]

    def test_checked_in_workflow_follows_runner_contract(self) -> None:
        self.assertEqual(workflow_findings(self.workflow), [])

    def test_seeded_workflow_concurrency_is_rejected(self) -> None:
        seed = copy.deepcopy(self.workflow)
        seed["concurrency"] = {"group": "seeded", "cancel-in-progress": False}
        self.assertIn("workflow: concurrency", self.seeded_findings(seed))

    def test_seeded_box_job_concurrency_is_rejected(self) -> None:
        seed = copy.deepcopy(self.workflow)
        seed["jobs"]["smoke"]["concurrency"] = "seeded"
        self.assertIn("smoke: concurrency", self.seeded_findings(seed))

    def test_seeded_box_job_label_drop_fails_equality_alone(self) -> None:
        seed = copy.deepcopy(self.workflow)
        label_to_drop = next(
            label for label in RUNNER_INSTANCES[0].labels if label.casefold() != "self-hosted"
        )
        seed["jobs"]["smoke"]["runs-on"].remove(label_to_drop)

        findings = self.seeded_findings(seed)

        self.assertIn("smoke: box label set", findings)
        self.assertNotIn("smoke: routing", findings)
        self.assertNotIn("checks: routing", findings)
        self.assertNotIn("checks: box labels", findings)

    def test_seeded_box_job_requires_private_push(self) -> None:
        seed = copy.deepcopy(self.workflow)
        seed["jobs"]["sample-box-job"] = {"runs-on": ["self-hosted", "test-label"]}
        self.assertIn("sample-box-job: private push guard", self.seeded_findings(seed))

    def test_seeded_checks_expression_requires_private_push(self) -> None:
        seed = copy.deepcopy(self.workflow)
        checks = seed["jobs"]["checks"]
        match = _PRIVATE_PUSH.search(checks["runs-on"])
        self.assertIsNotNone(match)
        assert match is not None
        checks["runs-on"] = checks["runs-on"].replace(match.group(0), "true")
        self.assertIn("checks: private push guard", self.seeded_findings(seed))

    def test_seeded_checks_runner_requires_hosted_fallback_and_instance_routing(self) -> None:
        for case, finding in (
            ("fallback", "checks: hosted fallback"),
            ("labels", "checks: routing"),
        ):
            with self.subTest(case=case):
                seed = copy.deepcopy(self.workflow)
                checks = seed["jobs"]["checks"]
                runner = checks["runs-on"]
                if case == "fallback":
                    checks["runs-on"] = runner.replace("'ubuntu-latest'", "'test-runner'")
                else:
                    match = _FROM_JSON.search(runner)
                    self.assertIsNotNone(match)
                    assert match is not None
                    labels = json.loads(match.group(1))
                    labels.append("test-only-label")
                    checks["runs-on"] = runner.replace(match.group(1), json.dumps(labels))
                findings = self.seeded_findings(seed)
                self.assertIn(finding, findings)
                if case == "labels":
                    self.assertNotIn("checks: box labels", findings)

    def test_seeded_checks_shared_labels_fail_separation_and_routing(self) -> None:
        seed = copy.deepcopy(self.workflow)
        runner = seed["jobs"]["checks"]["runs-on"]
        match = _FROM_JSON.search(runner)
        self.assertIsNotNone(match)
        assert match is not None
        seed["jobs"]["checks"]["runs-on"] = runner.replace(
            match.group(1), json.dumps(["self-hosted", "linux", "x64"])
        )

        findings = self.seeded_findings(seed)

        self.assertIn("checks: box labels", findings)
        self.assertIn("checks: routing", findings)

    def test_seeded_box_job_shared_labels_fail_separation_and_routing(self) -> None:
        for name in self.workflow["jobs"]:
            if name == "checks":
                continue
            with self.subTest(job=name):
                seed = copy.deepcopy(self.workflow)
                seed["jobs"][name]["runs-on"] = ["self-hosted", "linux", "x64"]

                findings = self.seeded_findings(seed)

                self.assertIn("checks: box labels", findings)
                self.assertIn(f"{name}: routing", findings)

    def test_seeded_checks_requires_timeout(self) -> None:
        seed = copy.deepcopy(self.workflow)
        del seed["jobs"]["checks"]["timeout-minutes"]
        self.assertIn("checks: timeout", self.seeded_findings(seed))

    def test_seeded_checks_step_requires_environment_guard(self) -> None:
        seed = copy.deepcopy(self.workflow)
        del seed["jobs"]["checks"]["steps"][1]["if"]
        self.assertIn("checks: step 1 environment guard", self.seeded_findings(seed))

    def test_seeded_gate_flags_are_checked_for_each_runner(self) -> None:
        for condition, command, finding in (
            (_PULL_REQUEST_GATE, "python -m tools.gate --all", "checks: pull request gate flags"),
            (_PULL_REQUEST_GATE, "python -m tools.gate --pin-selection", "checks: pull request gate flags"),
            (_PULL_REQUEST_GATE, "python -m tools.gate --all --pin-selection --masked", "checks: pull request gate flags"),
            (_HOSTED_GATE, "python -m tools.gate --masked", "checks: hosted gate flags"),
            (_HOSTED_GATE, "python -m tools.gate --all --masked", "checks: hosted gate flags"),
            (_HOSTED_GATE, "python -m tools.gate --all --pin-selection", "checks: hosted gate flags"),
            (_BOX_GATE, ".venv/bin/python -m tools.gate --masked", "checks: box gate flags"),
            (_BOX_GATE, ".venv/bin/python -m tools.gate --all", "checks: box gate flags"),
            (_BOX_GATE, ".venv/bin/python -m tools.gate --all --masked --pin-selection", "checks: box gate flags"),
        ):
            with self.subTest(condition=condition, command=command):
                seed = copy.deepcopy(self.workflow)
                self.gate_step(seed, condition)["run"] = command
                self.assertIn(finding, self.seeded_findings(seed))

    def test_seeded_pull_request_gate_is_required(self) -> None:
        seed = copy.deepcopy(self.workflow)
        step = self.gate_step(seed, _PULL_REQUEST_GATE)
        seed["jobs"]["checks"]["steps"].remove(step)
        self.assertIn("checks: pull request gate flags", self.seeded_findings(seed))

    def test_seeded_hosted_gate_events_must_be_complementary(self) -> None:
        for condition, replacement in (
            (_PULL_REQUEST_GATE, _HOSTED_GATE),
            (_HOSTED_GATE, _PULL_REQUEST_GATE),
        ):
            with self.subTest(replacement=replacement):
                seed = copy.deepcopy(self.workflow)
                self.gate_step(seed, condition)["if"] = replacement
                findings = self.seeded_findings(seed)
                self.assertIn("checks: pull request gate flags", findings)
                self.assertIn("checks: hosted gate flags", findings)

    def test_seeded_hosted_gate_without_event_is_rejected(self) -> None:
        for condition in (_PULL_REQUEST_GATE, _HOSTED_GATE):
            with self.subTest(condition=condition):
                seed = copy.deepcopy(self.workflow)
                self.gate_step(seed, condition)["if"] = "runner.environment == 'github-hosted'"
                self.assertIn("checks: hosted gate events", self.seeded_findings(seed))

    def test_seeded_step_guards_reject_extra_or_wrong_event_clauses(self) -> None:
        for condition, replacement in (
            (_PULL_REQUEST_GATE, _PULL_REQUEST_GATE + " && always()"),
            (_HOSTED_GATE, "runner.environment == 'github-hosted' && github.event_name == 'push'"),
            (_BOX_GATE, _BOX_GATE + " && github.event_name == 'pull_request'"),
            (_BOX_GATE, _BOX_GATE + " && github.event_name != 'pull_request'"),
        ):
            with self.subTest(replacement=replacement):
                seed = copy.deepcopy(self.workflow)
                step = self.gate_step(seed, condition)
                index = seed["jobs"]["checks"]["steps"].index(step)
                step["if"] = replacement
                self.assertIn(f"checks: step {index} environment guard", self.seeded_findings(seed))

    def test_step_guards_allow_whitespace_around_clauses(self) -> None:
        seed = copy.deepcopy(self.workflow)
        for condition in (_PULL_REQUEST_GATE, _HOSTED_GATE, _BOX_GATE):
            step = self.gate_step(seed, condition)
            step["if"] = "  " + condition.replace(" && ", "  &&  ").replace(" == ", "  ==  ") + "  "
        self.assertEqual(self.seeded_findings(seed), [])

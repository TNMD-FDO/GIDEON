"""Contract checks for CI runner selection and guarded workflow steps."""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml"
_PRIVATE_PUSH = re.compile(
    r"github\.event_name\s*==\s*'push'\s*&&\s*github\.event\.repository\.private"
)
_FROM_JSON = re.compile(r"fromJSON\('([^']+)'\)")
_HOSTED_FALLBACK = re.compile(r"\|\|\s*'ubuntu-latest'\s*}}$")
_BOX_LABEL = re.compile(r"['\"]self-hosted['\"]")
_ENVIRONMENTS = {
    "runner.environment == 'github-hosted'",
    "runner.environment == 'self-hosted'",
}


def _can_name_box(runner: object) -> bool:
    if isinstance(runner, list):
        return "self-hosted" in runner
    if isinstance(runner, str):
        return runner == "self-hosted" or _BOX_LABEL.search(runner) is not None
    return False


def workflow_findings(workflow: dict[str, Any]) -> list[str]:
    """Name each runner or step contract the parsed workflow violates."""

    findings: list[str] = []
    jobs = workflow["jobs"]
    checks = jobs["checks"]
    for name, job in jobs.items():
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
    box_labels = [
        job["runs-on"]
        for name, job in jobs.items()
        if name != "checks"
        and isinstance(job["runs-on"], list)
        and "self-hosted" in job["runs-on"]
    ]
    if not box_labels or not isinstance(labels, list) or any(
        labels != box_runner for box_runner in box_labels
    ):
        findings.append("checks: box labels")
    if not isinstance(checks.get("timeout-minutes"), int) or checks["timeout-minutes"] <= 0:
        findings.append("checks: timeout")

    steps = checks["steps"]
    for index, step in enumerate(steps[1:], 1):
        if step.get("if") not in _ENVIRONMENTS:
            findings.append(f"checks: step {index} environment guard")

    gates: dict[str, list[str]] = {environment: [] for environment in _ENVIRONMENTS}
    for step in steps[1:]:
        run = step.get("run", "")
        if "-m tools.gate" in run and step.get("if") in gates:
            gates[step["if"]].append(run)
    hosted = gates["runner.environment == 'github-hosted'"]
    box = gates["runner.environment == 'self-hosted'"]
    if len(hosted) != 1 or "--all" not in hosted[0].split() or "--masked" in hosted[0].split():
        findings.append("checks: hosted gate flags")
    if len(box) != 1 or not {"--all", "--masked"}.issubset(box[0].split()):
        findings.append("checks: box gate flags")
    return findings


class CiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def seeded_findings(self, workflow: dict[str, Any]) -> list[str]:
        return workflow_findings(yaml.safe_load(yaml.safe_dump(workflow)))

    def test_checked_in_workflow_follows_runner_contract(self) -> None:
        self.assertEqual(workflow_findings(self.workflow), [])

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

    def test_seeded_checks_runner_requires_hosted_fallback_and_box_labels(self) -> None:
        for case, finding in (
            ("fallback", "checks: hosted fallback"),
            ("labels", "checks: box labels"),
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
                self.assertIn(finding, self.seeded_findings(seed))

    def test_seeded_checks_requires_timeout(self) -> None:
        seed = copy.deepcopy(self.workflow)
        del seed["jobs"]["checks"]["timeout-minutes"]
        self.assertIn("checks: timeout", self.seeded_findings(seed))

    def test_seeded_checks_step_requires_environment_guard(self) -> None:
        seed = copy.deepcopy(self.workflow)
        del seed["jobs"]["checks"]["steps"][1]["if"]
        self.assertIn("checks: step 1 environment guard", self.seeded_findings(seed))

    def test_seeded_gate_flags_are_checked_for_each_runner(self) -> None:
        for environment, command, finding in (
            ("github-hosted", "python -m tools.gate --masked", "checks: hosted gate flags"),
            ("github-hosted", "python -m tools.gate --all --masked", "checks: hosted gate flags"),
            ("self-hosted", ".venv/bin/python -m tools.gate --masked", "checks: box gate flags"),
            ("self-hosted", ".venv/bin/python -m tools.gate --all", "checks: box gate flags"),
        ):
            with self.subTest(environment=environment):
                seed = copy.deepcopy(self.workflow)
                for step in seed["jobs"]["checks"]["steps"]:
                    if step.get("if") == f"runner.environment == '{environment}'" and "-m tools.gate" in step.get("run", ""):
                        step["run"] = command
                        break
                else:
                    self.fail(f"no gate step for {environment}")
                self.assertIn(finding, self.seeded_findings(seed))

"""Subprocess contracts for the development lifecycle launcher."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.exportboundary import in_export_tree

ROOT = Path(__file__).resolve().parent.parent
PHASES = ("plan", "implement", "release")
_PAIRING = re.compile(
    r"^(?P<key>PLAN_MODEL|PLAN_EFFORT|IMPLEMENT_MODEL|IMPLEMENT_EFFORT|"
    r"RELEASE_MODEL|RELEASE_EFFORT)=(?P<value>\S+)$",
    re.MULTILINE,
)
_PAIRING_KEYS = tuple(f"{phase.upper()}_{kind}" for phase in PHASES for kind in ("MODEL", "EFFORT"))


def _pairings() -> dict[str, tuple[str, str]]:
    """The table as bin/trip states it: the six assignments at its top."""

    matches = list(_PAIRING.finditer((ROOT / "bin" / "trip").read_text()))
    if tuple(match.group("key") for match in matches) != _PAIRING_KEYS:
        raise AssertionError("bin/trip does not contain its six pairing assignments")
    values = {match.group("key"): match.group("value") for match in matches}
    return {
        phase: (values[f"{phase.upper()}_MODEL"], values[f"{phase.upper()}_EFFORT"])
        for phase in PHASES
    }


def _prompt(phase: str, path: str | Path, ask: bool = False) -> str:
    number = PHASES.index(phase) + 1
    return f"/TRIP-{number}-{phase} {path}{' --ask' if ask else ''}"


class TripLauncher(unittest.TestCase):
    """Exercise the launcher from a synthetic primary checkout."""

    def setUp(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("bin/ is excluded from the public export")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.root = root
        self.primary = root / "primary"
        self.launcher = self.primary / "bin" / "trip"
        self.launcher.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "bin" / "trip", self.launcher)

        self.ticket = Path(".scratch", "effort", "issues", "27-a-model-and-effort-per-phase.md")
        slug = self.ticket.name.removesuffix(".md")[3:]
        self.plan = Path("docs", "1-plans", f"{slug}.plan.md")
        ticket_path = self.primary / self.ticket
        ticket_path.parent.mkdir(parents=True)
        ticket_path.write_text("ticket\n", encoding="utf-8")
        plan_path = self.primary / ".claude" / "worktrees" / slug / self.plan
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text("plan\n", encoding="utf-8")

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        self.log = root / "claude.log"
        fake_claude = fake_bin / "claude"
        fake_claude.write_text(
            """#!/usr/bin/env bash
set -u
printf 'ARGV:%s\\n' "$*" >> "$FAKE_LOG"
printf 'HANDOFF:%s\\n' "${TRIP_HANDOFF-}" >> "$FAKE_LOG"
printf 'CONFIG:%s\\n' "${CLAUDE_CONFIG_DIR-<unset>}" >> "$FAKE_LOG"
printf 'CWD:%s\\n' "$PWD" >> "$FAKE_LOG"
prompt=$5
record=
status=0
case "$prompt" in
    /TRIP-1-plan*) record=${FAKE_PLAN_RECORD-}; status=${FAKE_PLAN_STATUS:-0} ;;
    /TRIP-2-implement*) record=${FAKE_IMPLEMENT_RECORD-}; status=${FAKE_IMPLEMENT_STATUS:-0} ;;
    /TRIP-3-release*) record=${FAKE_RELEASE_RECORD-}; status=${FAKE_RELEASE_STATUS:-0} ;;
    *) exit 91 ;;
esac
if [[ -n "$record" ]]; then
    printf 'RECORD:%s\\n' "$record" >> "$FAKE_LOG"
    printf '%s\\n' "$record" > "$TRIP_HANDOFF"
fi
exit "$status"
""",
            encoding="utf-8",
        )
        fake_claude.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment["PATH"]
        self.environment["FAKE_LOG"] = str(self.log)
        self.environment.pop("CLAUDE_CONFIG_DIR", None)

    def _run(self, arguments: tuple[str, ...], **environment: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("", encoding="utf-8")
        child_environment = self.environment.copy()
        child_environment.update(environment)
        child_environment["HOME"] = str(self.root)
        return subprocess.run(
            [str(self.launcher), *arguments],
            cwd=self.primary,
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def _logged(self, prefix: str) -> list[str]:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]

    def _assert_dry_phase(
        self,
        lines: list[str],
        phase: str,
        model: str,
        effort: str,
        path: str | Path,
        ask: bool = False,
    ) -> None:
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], f"trip {phase}: {model} {effort}")
        self.assertEqual(lines[1], f"claude --model {model} --effort {effort} '{_prompt(phase, path, ask)}'")
        self.assertRegex(lines[2], r"^handoff: /.+")

    def _assert_dry_cycle(
        self,
        result: subprocess.CompletedProcess[str],
        pairings: dict[str, tuple[str, str]],
        ask: bool = False,
    ) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 9)
        for index, phase in enumerate(PHASES):
            model, effort = pairings[phase]
            path: str | Path = self.ticket if phase == "plan" else "<plan path from the handoff>"
            self._assert_dry_phase(lines[index * 3 : index * 3 + 3], phase, model, effort, path, ask)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "")

    def test_dry_run_single_phases_use_the_table(self) -> None:
        pairings = _pairings()
        for phase, path in (("plan", self.ticket), ("implement", self.plan), ("release", self.plan)):
            with self.subTest(phase=phase):
                result = self._run((phase, str(path), "--dry-run"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                model, effort = pairings[phase]
                self._assert_dry_phase(result.stdout.splitlines(), phase, model, effort, path)
                self.assertEqual(self.log.read_text(encoding="utf-8"), "")

    def test_dry_run_cycle_prints_three_phases_from_one_table(self) -> None:
        result = self._run(("cycle", str(self.ticket), "--dry-run"))
        self._assert_dry_cycle(result, _pairings())

    def test_overrides_and_ask_reach_every_phase(self) -> None:
        result = self._run(("cycle", str(self.ticket), "--dry-run", "--ask", "--effort", "probe-effort"))
        pairings = {phase: (model, "probe-effort") for phase, (model, _effort) in _pairings().items()}
        self._assert_dry_cycle(result, pairings, ask=True)

        result = self._run(("plan", str(self.ticket), "--dry-run", "--model", "probe-model"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_dry_phase(
            result.stdout.splitlines(), "plan", "probe-model", _pairings()["plan"][1], self.ticket
        )

    def test_cycle_chains_records_and_passes_the_environment_through(self) -> None:
        result = self._run(
            ("cycle", str(self.ticket), "--ask"),
            FAKE_PLAN_RECORD=f"implement {self.plan}",
            FAKE_IMPLEMENT_RECORD=f"release {self.plan}",
            CLAUDE_CONFIG_DIR="/inherited",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._logged("RECORD:"), [f"implement {self.plan}", f"release {self.plan}"]
        )
        handoffs = self._logged("HANDOFF:")
        self.assertEqual(len(handoffs), 3)
        self.assertTrue(all(handoffs))
        self.assertEqual(len(set(handoffs)), 1)
        self.assertFalse(Path(handoffs[0]).exists())
        self.assertEqual(self._logged("CWD:"), [str(self.primary)] * 3)
        self.assertEqual(self._logged("CONFIG:"), ["/inherited"] * 3)
        pairings = _pairings()
        self.assertEqual(
            self._logged("ARGV:"),
            [
                f"--model {pairings[phase][0]} --effort {pairings[phase][1]} {_prompt(phase, path, True)}"
                for phase, path in (("plan", self.ticket), ("implement", self.plan), ("release", self.plan))
            ],
        )

    def test_single_phase_leaves_an_unset_configuration_unset(self) -> None:
        result = self._run(("plan", str(self.ticket)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._logged("CONFIG:"), ["<unset>"])
        model, effort = _pairings()["plan"]
        self.assertEqual(self._logged("ARGV:"), [f"--model {model} --effort {effort} {_prompt('plan', self.ticket)}"])

    def test_cycle_stops_on_empty_wrong_missing_and_nonzero_plan(self) -> None:
        cases: tuple[tuple[str, dict[str, str]], ...] = (
            ("empty record", {}),
            ("wrong phase", {"FAKE_PLAN_RECORD": f"release {self.plan}"}),
            ("missing plan", {"FAKE_PLAN_RECORD": "implement docs/1-plans/missing.md"}),
            ("non-zero exit", {"FAKE_PLAN_STATUS": "7"}),
        )
        for name, environment in cases:
            with self.subTest(case=name):
                result = self._run(("cycle", str(self.ticket)), **environment)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"trip cycle: plan ended; resume with bin/trip plan {self.ticket}", result.stderr
                )
                self.assertNotIn("TRIP-2-implement", self.log.read_text(encoding="utf-8"))

    def test_cycle_stops_after_implement_naming_the_plan(self) -> None:
        plan_record = {"FAKE_PLAN_RECORD": f"implement {self.plan}"}
        cases: tuple[tuple[str, dict[str, str]], ...] = (
            ("wrong phase", {"FAKE_IMPLEMENT_RECORD": f"implement {self.plan}"}),
            ("non-zero exit", {"FAKE_IMPLEMENT_STATUS": "5"}),
        )
        for name, environment in cases:
            with self.subTest(case=name):
                result = self._run(("cycle", str(self.ticket), "--ask"), **plan_record, **environment)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"trip cycle: implement ended; resume with bin/trip implement {self.plan} --ask",
                    result.stderr,
                )
                self.assertNotIn("TRIP-3-release", self.log.read_text(encoding="utf-8"))

    def test_worktree_copy_is_refused(self) -> None:
        if "/.claude/worktrees/" in ROOT.as_posix():
            launcher = ROOT / "bin" / "trip"
        else:
            directory = self.root / "x" / ".claude" / "worktrees" / "y" / "bin"
            directory.mkdir(parents=True)
            launcher = directory / "trip"
            shutil.copy2(ROOT / "bin" / "trip", launcher)
        result = subprocess.run(
            [str(launcher), "plan", "anything", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("trip: the launcher is in a worktree. Fix: run the primary bin/trip", result.stderr)

    def test_missing_path_help_and_bad_argument_refuse(self) -> None:
        missing = self._run(("plan", "missing.md"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn(
            "trip: path does not exist in the primary checkout or a worktree: missing.md. "
            "Fix: provide an existing path",
            missing.stderr,
        )

        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.primary / "linked.md"
        link.symlink_to(outside)
        for name, candidate in (
            ("traversal", "../outside.md"),
            ("symlink out", "linked.md"),
            ("absolute outside", str(outside)),
        ):
            with self.subTest(case=name):
                result = self._run(("plan", candidate, "--dry-run"))
                self.assertEqual(result.returncode, 1)
                self.assertIn("path does not exist in the primary checkout or a worktree", result.stderr)
                self.assertEqual(result.stdout, "")

        usage = "Usage: bin/trip <plan|implement|release|cycle> <path>"
        usage_cases: tuple[tuple[str, ...], ...] = (
            (), ("--help",), ("-h",), ("plan",), ("plan", str(self.ticket), "--model"),
        )
        for arguments in usage_cases:
            with self.subTest(usage_arguments=arguments):
                result = self._run(arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn(usage, result.stderr)

        for arguments, word in ((("unknown", "path"), "unknown"), (("4", "cycle", str(self.ticket)), "4")):
            with self.subTest(arguments=arguments):
                result = self._run(arguments)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"trip: unknown phase: {word}. Fix: type plan, implement, release, or cycle",
                    result.stderr,
                )
                self.assertEqual(self.log.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()

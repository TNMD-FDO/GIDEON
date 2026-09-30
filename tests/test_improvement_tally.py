"""Content-free proposal tallies and their git provenance."""

import json
import subprocess
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from gideon.host.sysio import Command, Host, PathLike
from gideon.improvement import proposals, ratings, tally, triggers, watch
from gideon.improvement.sections import Row, SectionReport

CHECKOUT = Path("/tmp/fictional-checkout")


class GitHost:
    """Only the git operations needed to read a fictional checkout."""

    def __init__(
        self, *, git_present: bool = True, returncode: int = 0,
        stdout: str = "", error: OSError | None = None,
    ) -> None:
        self.git_present = git_present
        self.returncode = returncode
        self.stdout = stdout
        self.error = error
        self.calls: list[tuple[str, ...]] = []

    def exists(self, path: PathLike) -> bool:
        return self.git_present and Path(path) == CHECKOUT / ".git"

    def run(self, argv: Command, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if kwargs:
            raise AssertionError(f"unexpected options: {kwargs}")
        self.calls.append(tuple(argv))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, "")


class TallyCase(unittest.TestCase):
    """A tally carries the report's counts and trigger figures alone."""

    def test_detail_has_only_six_keys_and_the_trigger_section_rows(self) -> None:
        says = "Fictional prose that must not enter the audit row."
        trigger = triggers.Trigger(
            id="fictional-trigger",
            reopens="§1.1",
            register=None,
            measure="unavailable",
            state="watching",
            condition=triggers.Condition((triggers.Clause("fictional_figure", "above", 1),)),
            says=says,
            ruled=None,
            baseline=None,
        )
        row = watch.render_row(
            watch.evaluate_trigger(trigger, {"fictional_figure": (2,)})
        )
        walk = proposals.WalkResult(
            outcomes=(
                proposals.SectionOutcome(
                    watch.TRIGGERS_SECTION, SectionReport("fictional figures", (row,))
                ),
                proposals.SectionOutcome(
                    ratings.FEEDBACK_SECTION,
                    SectionReport("fictional ratings", (Row("fictional-office", "rated", "3"),)),
                ),
            ),
            fired=1,
            skipped=0,
            refused=0,
        )
        detail = tally.tally_detail(walk, False)
        self.assertEqual(set(detail), set(tally.DETAIL_KEYS))
        self.assertEqual(detail["fired"], 1)
        self.assertEqual(detail["sections"], len(walk.outcomes))
        self.assertEqual(detail["skipped"], 0)
        self.assertEqual(detail["refused"], 0)
        self.assertIs(detail["git_dirty"], False)
        self.assertEqual(detail["triggers"], [
            {"id": row.name, "state": row.state, "detail": row.detail}
        ])
        encoded = json.dumps(detail)
        self.assertNotIn(says, encoded)
        self.assertNotIn("fictional-office", encoded)

        def strings(value: object) -> Sequence[str]:
            if isinstance(value, str):
                return (value,)
            if isinstance(value, dict):
                return tuple(
                    text for key, item in value.items()
                    for text in (*strings(key), *strings(item))
                )
            if isinstance(value, list):
                return tuple(text for item in value for text in strings(item))
            return ()

        for value in strings(detail):
            self.assertFalse(any(character in value for character in "\r\n\x00"))

    def test_skipped_product_section_has_no_trigger_rows(self) -> None:
        walk = proposals.WalkResult(
            (proposals.SectionOutcome(watch.TRIGGERS_SECTION, None),),
            fired=0,
            skipped=1,
            refused=0,
        )
        detail = tally.tally_detail(walk, None)
        self.assertEqual(detail["triggers"], [])
        self.assertEqual(detail["sections"], 1)
        self.assertEqual(detail["skipped"], 1)

    def test_git_dirty_reads_clean_dirty_and_unknown(self) -> None:
        cases = (
            ("clean", GitHost(), False),
            ("dirty", GitHost(stdout=" M fictional.txt\n"), True),
            ("no-git", GitHost(git_present=False), None),
            ("git-failed", GitHost(returncode=7), None),
            ("git-missing", GitHost(error=FileNotFoundError("fictional git")), None),
        )
        for name, host, expected in cases:
            with self.subTest(name=name):
                self.assertIs(tally.git_dirty(cast(Host, host), CHECKOUT), expected)
                self.assertEqual(
                    host.calls,
                    [] if name == "no-git" else [(
                        "git", "-c", f"safe.directory={CHECKOUT}", "-C", str(CHECKOUT),
                        "status", "--porcelain",
                    )],
                )

    def test_writer_rejects_a_line_break_before_running_psql(self) -> None:
        host = GitHost(git_present=False)
        walk = proposals.WalkResult(
            (
                proposals.SectionOutcome(
                    watch.TRIGGERS_SECTION,
                    SectionReport("fictional figures", (Row("fictional", "fired", "bad\nfigure"),)),
                ),
            ),
            fired=1,
            skipped=0,
            refused=0,
        )
        problem = tally.write_tally(cast(Host, host), "/tmp/fictional-rendered", CHECKOUT, walk)
        self.assertEqual(problem, "audit row refused: detail contains CR, LF, or NUL")
        self.assertEqual(host.calls, [])


if __name__ == "__main__":
    unittest.main()

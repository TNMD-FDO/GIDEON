"""The vendored-skills tripwire (ADR-0033, ADR-0041; docs/agents/tooling.md §5).

A standard-library-only contract around the folder-hash algorithm read from
the ``skills`` CLI v1.5.23: every entry of the committed lock has its
directory and its folder hash, no vendored skill carries a placeholder, a
conflict marker, or a non-executable script, no two skill directories differ
only by case, and the clone checker reports its statuses.
"""

import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from tools.exportboundary import in_export_tree
from tools.pinwatch import skills

ROOT = Path(__file__).resolve().parent.parent
_REFRESH = "then re-run docs/agents/tooling.md §3."


def _skip_in_export_tree(case: unittest.TestCase) -> None:
    """Skip checkout-copying contracts because skills are excluded from exports."""

    if in_export_tree(ROOT):
        case.skipTest("vendored skills are excluded from the public export")


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable tripwire finding."""

    path: Path
    problem: str
    fix: str


def _lock(root: Path) -> skills.SkillsLock:
    return skills.load_skills_lock(
        (root / skills.SKILLS_LOCK_PATH).read_text(encoding="utf-8")
    )


def _skill_directory(root: Path, entry: skills.SkillEntry) -> Path:
    return root / skills.SKILLS_ROOT / entry.name


def _skill_files(root: Path, entries: Iterable[skills.SkillEntry]) -> Iterator[Path]:
    for entry in entries:
        directory = _skill_directory(root, entry)
        if directory.is_dir():
            yield from (path for path, _relative in skills.walk_files(directory))


def _finding(root: Path, path: Path, problem: str, fix: str) -> Finding:
    return Finding(path.relative_to(root), problem, fix)


def rule_directories(root: Path, lock: skills.SkillsLock) -> list[Finding]:
    return [
        _finding(
            root,
            _skill_directory(root, entry),
            f"vendored skill directory for {entry.name!r} is missing",
            f"Restore the directory from the vendor commit, {_REFRESH}",
        )
        for entry in lock.entries
        if not _skill_directory(root, entry).is_dir()
    ]


def rule_hashes(root: Path, lock: skills.SkillsLock) -> list[Finding]:
    findings: list[Finding] = []
    for entry in lock.entries:
        directory = _skill_directory(root, entry)
        if directory.is_dir() and skills.folder_hash(directory) != entry.computed_hash:
            findings.append(
                _finding(
                    root,
                    directory,
                    "folder hash differs from skills-lock.json",
                    "Restore the file or the lock's hash from the vendor commit "
                    f"(a vendored skill is never edited), {_REFRESH}",
                )
            )
    return findings


def rule_placeholders(root: Path, lock: skills.SkillsLock) -> list[Finding]:
    return [
        _finding(
            root,
            path,
            "vendored skill contains an unresolved project placeholder",
            f"Remove the placeholder from the vendored skill, {_REFRESH}",
        )
        for path in _skill_files(root, lock.entries)
        if any(
            token in path.read_text(encoding="utf-8")
            for token in ("[ADAPT_TO_PROJECT", "[PROJECT_NAME]")
        )
    ]


def rule_conflicts(root: Path, lock: skills.SkillsLock) -> list[Finding]:
    findings: list[Finding] = []
    for path in _skill_files(root, lock.entries):
        active = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("<<<<<<< "):
                active = True
                problem = "vendored skill contains a conflict marker"
            elif line == "=======" and active:
                problem = "vendored skill contains a conflict separator"
            elif line.startswith(">>>>>>> "):
                active = False
                problem = "vendored skill contains a conflict marker"
            else:
                continue
            findings.append(
                _finding(root, path, problem, f"Resolve the conflict marker, {_REFRESH}")
            )
    return findings


def rule_exec_bits(root: Path, lock: skills.SkillsLock) -> list[Finding]:
    return [
        _finding(
            root,
            path,
            "script does not have an executable mode",
            f"Set the mode to executable, {_REFRESH}",
        )
        for path in _skill_files(root, lock.entries)
        if path.parent.name == "scripts"
        and path.suffix == ".sh"
        and not path.name.endswith(".template.sh")
        and not os.stat(path).st_mode & 0o111
    ]


def rule_case_duplicates(root: Path) -> list[Finding]:
    skills_root = root / skills.SKILLS_ROOT
    if not skills_root.is_dir():
        return []
    groups: dict[str, list[Path]] = {}
    for path in skills_root.iterdir():
        if path.is_dir():
            groups.setdefault(path.name.casefold(), []).append(path)
    return [
        _finding(
            root,
            skills_root,
            "skill directories differ only by case: "
            + ", ".join(sorted(path.name for path in paths)),
            "Remove the lowercased directory the CLI left and keep the canonical directory.",
        )
        for paths in groups.values()
        if len(paths) > 1
    ]


def findings(root: Path) -> list[Finding]:
    """Return every finding in rule order, without stopping at the first one."""

    try:
        lock = _lock(root)
    except skills.SkillsRecordError as error:
        return [_finding(root, root / skills.SKILLS_LOCK_PATH, error.problem, error.fix)]
    return [
        *rule_directories(root, lock),
        *rule_hashes(root, lock),
        *rule_placeholders(root, lock),
        *rule_conflicts(root, lock),
        *rule_exec_bits(root, lock),
        *rule_case_duplicates(root),
    ]


def render_findings(items: list[Finding]) -> str:
    """Render every finding in the tripwire's one-line failure shape."""

    return "\n".join(f"{item.path}: {item.problem}. Fix: {item.fix}" for item in items)


@contextmanager
def _temporary_checkout() -> Iterator[Path]:
    """A copy of the two inputs the rules read: the skills and the lock.

    Gitignored Codex thread state under ``state/`` is left behind, so the copy
    is a few megabytes of committed text.
    """

    def ignore(directory: str, names: list[str]) -> set[str]:
        if Path(directory).name == "state":
            return {name for name in names if name != ".gitignore"}
        return {name for name in names if name == "__pycache__"}

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "checkout"
        shutil.copytree(ROOT / skills.SKILLS_ROOT, target / skills.SKILLS_ROOT, ignore=ignore)
        shutil.copy2(ROOT / skills.SKILLS_LOCK_PATH, target / skills.SKILLS_LOCK_PATH)
        yield target


def _first_entry(root: Path) -> skills.SkillEntry:
    return _lock(root).entries[0]


def _entry_skill_file(root: Path, entry: skills.SkillEntry) -> Path:
    return _skill_directory(root, entry) / Path(entry.skill_path).name


class RecordContracts(unittest.TestCase):
    def test_committed_records_load_and_parse(self) -> None:
        lock = _lock(ROOT)
        provenance = skills.parse_provenance(
            (ROOT / skills.PROVENANCE_PATH).read_text(encoding="utf-8")
        )
        self.assertTrue(lock.entries)
        self.assertTrue(provenance.matt_commit)
        self.assertTrue(provenance.matt_date)


class TripwireContracts(unittest.TestCase):
    def test_committed_checkout_is_clean(self) -> None:
        _skip_in_export_tree(self)
        items = findings(ROOT)
        if items:
            self.fail(render_findings(items))

    def test_failure_renders_one_line_per_finding(self) -> None:
        items = [
            Finding(Path("one"), "first problem", "first fix"),
            Finding(Path("two"), "second problem", "second fix"),
        ]
        self.assertEqual(
            render_findings(items),
            "one: first problem. Fix: first fix\ntwo: second problem. Fix: second fix",
        )

    def test_missing_directory_is_a_finding(self) -> None:
        _skip_in_export_tree(self)
        with _temporary_checkout() as root:
            shutil.rmtree(_skill_directory(root, _first_entry(root)))
            self.assertTrue(any("directory" in item.problem for item in findings(root)))

    def test_edited_skill_and_rewritten_lock_hash_are_findings(self) -> None:
        _skip_in_export_tree(self)
        for mutation in ("edited skill", "rewritten lock hash"):
            with self.subTest(mutation=mutation), _temporary_checkout() as root:
                entry = _first_entry(root)
                if mutation == "edited skill":
                    path = _entry_skill_file(root, entry)
                    path.write_bytes(path.read_bytes() + b"\ntripwire mutation\n")
                else:
                    lock_path = root / skills.SKILLS_LOCK_PATH
                    document = json.loads(lock_path.read_text(encoding="utf-8"))
                    document["skills"][entry.name]["computedHash"] = "0" * 64
                    lock_path.write_text(
                        json.dumps(document, indent=2) + "\n", encoding="utf-8"
                    )
                expected_path = _skill_directory(root, entry).relative_to(root)
                self.assertTrue(
                    any(
                        item.path == expected_path and "folder hash" in item.problem
                        for item in findings(root)
                    )
                )

    def test_placeholder_is_a_finding(self) -> None:
        _skip_in_export_tree(self)
        with _temporary_checkout() as root:
            path = _entry_skill_file(root, _first_entry(root))
            path.write_text(
                path.read_text(encoding="utf-8") + "\n[PROJECT_NAME]\n", encoding="utf-8"
            )
            self.assertTrue(any("placeholder" in item.problem for item in findings(root)))

    def test_conflict_marker_is_a_finding(self) -> None:
        _skip_in_export_tree(self)
        with _temporary_checkout() as root:
            path = _entry_skill_file(root, _first_entry(root))
            path.write_text(
                path.read_text(encoding="utf-8") + "\n<<<<<<< ours\n", encoding="utf-8"
            )
            self.assertTrue(any("conflict" in item.problem for item in findings(root)))

    def test_scripts_need_exec_bits_except_templates(self) -> None:
        _skip_in_export_tree(self)
        with _temporary_checkout() as root:
            scripts = _skill_directory(root, _first_entry(root)) / "scripts"
            scripts.mkdir()
            script = scripts / "x.sh"
            template = scripts / "x.template.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            template.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o644)
            template.chmod(0o644)
            script_findings = [item for item in findings(root) if "script" in item.problem]
            self.assertTrue(any(item.path.name == "x.sh" for item in script_findings))
            self.assertFalse(
                any(item.path.name == "x.template.sh" for item in script_findings)
            )

    def test_case_duplicate_directory_is_a_finding(self) -> None:
        _skip_in_export_tree(self)
        with _temporary_checkout() as root:
            (root / skills.SKILLS_ROOT / "Synthetic-Skill").mkdir()
            (root / skills.SKILLS_ROOT / "synthetic-skill").mkdir()
            self.assertTrue(any("only by case" in item.problem for item in findings(root)))

    def test_unreadable_lock_is_one_finding(self) -> None:
        _skip_in_export_tree(self)
        with _temporary_checkout() as root:
            (root / skills.SKILLS_LOCK_PATH).write_text("{}\n", encoding="utf-8")
            items = findings(root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].path, Path(skills.SKILLS_LOCK_PATH))


class HashProperties(unittest.TestCase):
    def test_folder_hash_is_path_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "a.txt"
            original.write_bytes(b"same\n")
            before = skills.folder_hash(root)
            original.rename(root / "b.txt")
            self.assertNotEqual(skills.folder_hash(root), before)

    def test_collation_key_truth_table(self) -> None:
        self.assertEqual(
            min(("scripts/start.sh", "SKILL.md"), key=skills.collation_key),
            "scripts/start.sh",
        )
        self.assertEqual(
            min(("_common.sh", "key.sh"), key=skills.collation_key),
            "_common.sh",
        )
        self.assertEqual(
            min((".gitignore", "key.sh"), key=skills.collation_key),
            ".gitignore",
        )
        self.assertEqual(
            sorted(("same", "SAME"), key=skills.collation_key),
            ["same", "SAME"],
        )


class CloneCheckerContracts(unittest.TestCase):
    SOURCE = "example/source"

    def _tree(self, root: Path) -> tuple[skills.SkillsLock, Path, Path]:
        clone = root / "clone"
        installed = root / "installed"
        contents = {"alpha": "alpha\n", "beta": "beta\n"}
        entries: list[skills.SkillEntry] = []
        for name, content in contents.items():
            clone_file = clone / "skills" / name / "SKILL.md"
            installed_file = installed / name / "SKILL.md"
            clone_file.parent.mkdir(parents=True, exist_ok=True)
            installed_file.parent.mkdir(parents=True, exist_ok=True)
            clone_file.write_text(content, encoding="utf-8")
            installed_file.write_text(content, encoding="utf-8")
            entries.append(
                skills.SkillEntry(
                    name,
                    self.SOURCE,
                    None,
                    f"skills/{name}/SKILL.md",
                    skills.folder_hash(clone_file.parent),
                )
            )
        return skills.SkillsLock(tuple(entries)), clone, installed

    def test_clone_findings_reports_match_differences_and_absence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock, clone, installed = self._tree(root)
            self.assertEqual(
                skills.clone_findings(lock, self.SOURCE, clone, installed),
                (("alpha", "match"), ("beta", "match")),
            )

            (installed / "alpha" / "SKILL.md").write_text(
                "installed mutation\n", encoding="utf-8"
            )
            rows = skills.clone_findings(lock, self.SOURCE, clone, installed)
            self.assertEqual(rows[0], ("alpha", "installed differs from clone"))

            (installed / "alpha" / "SKILL.md").write_text("alpha\n", encoding="utf-8")
            lock_difference = skills.SkillsLock(
                (
                    skills.SkillEntry(
                        lock.entries[0].name,
                        lock.entries[0].source,
                        lock.entries[0].ref,
                        lock.entries[0].skill_path,
                        "0" * 64,
                    ),
                    lock.entries[1],
                )
            )
            rows = skills.clone_findings(lock_difference, self.SOURCE, clone, installed)
            self.assertEqual(rows[0], ("alpha", "lock differs from clone"))

            (installed / "alpha" / "SKILL.md").write_text(
                "installed mutation\n", encoding="utf-8"
            )
            rows = skills.clone_findings(lock_difference, self.SOURCE, clone, installed)
            self.assertEqual(rows[0], ("alpha", "installed and lock differ from clone"))
            (installed / "alpha" / "SKILL.md").write_text("alpha\n", encoding="utf-8")

            shutil.rmtree(clone / "skills" / "beta")
            rows = skills.clone_findings(lock, self.SOURCE, clone, installed)
            self.assertEqual(rows[1], ("beta", "absent from clone"))

            shutil.rmtree(clone / "skills" / "alpha")
            shutil.rmtree(installed / "alpha")
            rows = skills.clone_findings(lock, self.SOURCE, clone, installed)
            self.assertEqual(rows[0], ("alpha", "absent from clone and installed"))

    def test_checker_main_prints_rows_and_returns_match_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            lock, clone, installed = self._tree(root.parent)
            lock_text = json.dumps(
                {
                    "version": 1,
                    "skills": {
                        entry.name: {
                            "source": entry.source,
                            "sourceType": "github",
                            "skillPath": entry.skill_path,
                            "computedHash": entry.computed_hash,
                        }
                        for entry in lock.entries
                    },
                },
                indent=2,
            ) + "\n"
            (root / skills.SKILLS_LOCK_PATH).write_text(lock_text, encoding="utf-8")
            shutil.copytree(installed, root / skills.SKILLS_ROOT)

            output = StringIO()
            with redirect_stdout(output):
                result = skills.main(
                    ["--source", self.SOURCE, str(clone)], checkout_root=root
                )
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), "alpha: match\nbeta: match\n")

            (root / skills.SKILLS_ROOT / "beta" / "SKILL.md").write_text(
                "changed\n", encoding="utf-8"
            )
            output = StringIO()
            with redirect_stdout(output):
                result = skills.main(
                    ["--source", self.SOURCE, str(clone)], checkout_root=root
                )
            self.assertEqual(result, 1)
            self.assertEqual(
                output.getvalue(), "alpha: match\nbeta: installed differs from clone\n"
            )

            # A mistyped source is a refusal naming the known ones, never a green run.
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = skills.main(
                    ["--source", "example/typo", str(clone)], checkout_root=root
                )
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("no lock entry has source 'example/typo'", errors.getvalue())
            self.assertIn(self.SOURCE, errors.getvalue())


if __name__ == "__main__":
    unittest.main()

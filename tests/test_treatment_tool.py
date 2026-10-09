"""Contracts for the pinned sentence reader, finder, and baseline command."""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import re
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request

import tools.treatment as treatment
import tools.treatment.__main__ as treatment_command
from gideon.host.egress import load_egress_allowlist
from gideon.worker.treatment import RULES, RULES_1, PatternRules
from tools.treatment.arms import ARMS, SHIPPED, arm, compose, shipped

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "cobaltquill"


def _tsv(records: tuple[tuple[str, str, str], ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    writer.writerow(("index", "answer", "text"))
    writer.writerows(records)
    return stream.getvalue().encode()


def _sample_files() -> tuple[bytes, bytes]:
    train = _tsv((
        ("0", "Yes", "we overruled alden v. brix, 1 u.s. 2 and coda v. dell, "
         f"3 u.s. 4. {SENTINEL}"),
        ("1", "No", f"we overruled alden v. brix, 1 u.s. 2. {SENTINEL}"),
        ("2", "Yes", f"we overruled without a case citation. {SENTINEL}"),
    ))
    test = _tsv((
        ("0", "Yes", f"alden v. brix, 1 u.s. 2 was reversed. {SENTINEL}"),
        ("1", "No", f"alden v. brix, 1 u.s. 2 was never overruled. {SENTINEL}"),
    ))
    return train, test


def _fixture_dataset(train: bytes, test: bytes) -> treatment.Dataset:
    files = tuple(
        treatment.PinnedFile(path, len(content), hashlib.sha256(content).hexdigest())
        for path, content in (
            ("data/fables/train.tsv", train),
            ("data/fables/test.tsv", test),
        )
    )
    return treatment.Dataset(
        treatment.DATASET.host, "datasets/example/fables", "a" * 40,
        files, "CC0-1.0", "Fictional authors",
    )


def _write_files(
    root: Path, dataset: treatment.Dataset, contents: tuple[bytes, bytes],
) -> None:
    for pinned, content in zip(dataset.files, contents, strict=True):
        path = root / pinned.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


@contextmanager
def _pinned(dataset: treatment.Dataset) -> Iterator[None]:
    with (
        patch.object(treatment, "DATASET", dataset),
        patch.object(treatment_command, "DATASET", dataset),
    ):
        yield


class _Response:
    def __init__(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._stream = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()


class Pin(unittest.TestCase):
    def test_grammar_and_allowed_host(self) -> None:
        dataset = treatment.DATASET
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40}", dataset.revision))
        self.assertEqual(len(dataset.files), 2)
        for pinned in dataset.files:
            with self.subTest(path=pinned.path):
                self.assertGreater(pinned.size, 0)
                self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{64}", pinned.sha256))
                self.assertEqual(
                    treatment.resolve_url(pinned),
                    f"https://{dataset.host}/{dataset.repository}/resolve/"
                    f"{dataset.revision}/{pinned.path}",
                )
        loaded = load_egress_allowlist(ROOT / "config/egress.yaml")
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.allowlist is not None
        group = loaded.allowlist.group("install-upgrade")
        self.assertIsNotNone(group)
        assert group is not None
        self.assertIn(dataset.host, {entry.host for entry in group.hosts})


class Reader(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_split_ids_and_labels(self) -> None:
        train = _tsv((("7", "Yes", "alden was overruled"),))
        test = _tsv((("7", "No", "brix remains"),))
        dataset = _fixture_dataset(train, test)
        _write_files(self.root, dataset, (train, test))
        with _pinned(dataset):
            self.assertEqual(treatment.read(self.root), (
                treatment.Sentence("train-7", True, "alden was overruled"),
                treatment.Sentence("test-7", False, "brix remains"),
            ))

    def test_changed_header_and_unknown_label_refuse(self) -> None:
        cases = (
            (_tsv((("0", "Yes", "alden"),)).replace(b"index", b"number"), "header"),
            (_tsv((("0", "Maybe", "brix"),)), "label"),
        )
        for train, problem in cases:
            with self.subTest(problem=problem):
                test = _tsv((("0", "No", "brix"),))
                dataset = _fixture_dataset(train, test)
                _write_files(self.root, dataset, (train, test))
                with _pinned(dataset), self.assertRaisesRegex(ValueError, problem):
                    treatment.read(self.root)


class Finder(unittest.TestCase):
    def test_case_names_and_reporter_forms(self) -> None:
        for reporter in (
            "1 u.s. 2", "2 f.3d 4", "3 f. supp. 2d 5",
            "4 s. ct. 6", "5 n.e.2d 7",
        ):
            with self.subTest(reporter=reporter):
                text = f"alden v. brix; cited {reporter}."
                spans = treatment.find(text)
                self.assertIn("alden v. brix", (text[start:end] for start, end in spans))
                self.assertIn(reporter, (text[start:end] for start, end in spans))

    def test_plain_prose_and_treatment_verb_boundary(self) -> None:
        self.assertEqual(treatment.find("the court counted 23 motions in 2024"), ())
        text = "we overruled alden v. brix, 1 u.s. 2"
        spans = treatment.find(text)
        self.assertEqual(len(spans), 2)
        self.assertTrue(all("overruled" not in text[start:end] for start, end in spans))
        self.assertEqual(text[spans[0][0]:spans[0][1]], "alden v. brix")

    def test_rows_place_anchors_in_text_order(self) -> None:
        text = "alden v. brix cited 1 u.s. 2 and coda v. dell cited 3 f.3d 4"
        sentence = treatment.Sentence("fiction-0", True, text)
        section, citations = treatment.rows(sentence)
        self.assertEqual(
            (section.section_type, section.char_start, section.char_end),
            ("majority", 0, len(text)),
        )
        self.assertEqual(tuple(row.ordinal for row in citations), tuple(range(len(citations))))
        self.assertEqual(len(citations), 4)
        self.assertEqual(tuple(row.char_start for row in citations), tuple(sorted(
            row.char_start for row in citations
        )))
        for row in citations:
            self.assertEqual(row.cite_type, "case_cite")
            self.assertEqual(row.cite_form, "full")
            self.assertEqual(row.section_id, section.section_id)
            self.assertEqual(row.raw_cite, text[row.char_start:row.char_end])
            self.assertIsNone(row.reporter_cite)
            self.assertIsNone(row.to_cluster)


class Measurement(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contents = _sample_files()
        self.dataset = _fixture_dataset(*self.contents)
        _write_files(self.root, self.dataset, self.contents)

    def test_both_units_ids_coverage_and_lineage(self) -> None:
        with _pinned(self.dataset):
            result = treatment.measure(treatment.read(self.root), RULES_1)
        self.assertEqual(
            (result.edge.predicted, result.edge.true_positives,
             result.edge.false_positives, result.edge.false_negatives),
            (2, 1, 1, 2),
        )
        self.assertEqual(result.edge.false_positive_ids, ("train-1",))
        self.assertEqual(result.edge.false_negative_ids, ("train-2", "test-0"))
        self.assertAlmostEqual(result.edge.precision, 1 / 2)
        self.assertAlmostEqual(result.edge.recall, 1 / 3)
        self.assertEqual(
            (result.sentence.predicted, result.sentence.true_positives,
             result.sentence.false_positives, result.sentence.false_negatives),
            (3, 2, 1, 1),
        )
        self.assertEqual(result.sentence.false_positive_ids, ("train-1",))
        self.assertEqual(result.sentence.false_negative_ids, ("test-0",))
        self.assertAlmostEqual(result.sentence.precision, 2 / 3)
        self.assertAlmostEqual(result.sentence.recall, 2 / 3)
        self.assertEqual((result.anchored_positive, result.anchored_negative), (2, 2))
        self.assertEqual(result.lineage_only, 1)
        self.assertEqual(result.per_verb, (("overruled", 2),))


class Arms(unittest.TestCase):
    def test_registry_changes_one_field_per_arm(self) -> None:
        names = [item.name for item in ARMS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(arm("baseline").rules, RULES_1)
        diagnostics = {"no-direction": "direction", "nearest-citation": "attribution"}
        for item in ARMS:
            changed = {
                field.name for field in fields(PatternRules)
                if getattr(item.rules, field.name) != getattr(RULES_1, field.name)
            }
            with self.subTest(name=item.name):
                if item.name == "baseline":
                    self.assertEqual(changed, set())
                else:
                    self.assertEqual(len(changed), 1)
                if item.name in diagnostics:
                    self.assertEqual(changed, {diagnostics[item.name]})
                    self.assertFalse(item.selectable)
                else:
                    self.assertFalse(changed & {"direction", "attribution"})
                    self.assertTrue(item.selectable)
        self.assertTrue(set(SHIPPED.split(",")) <= set(names))
        self.assertEqual(shipped().name, SHIPPED)
        self.assertTrue(shipped().selectable)
        self.assertEqual(shipped().rules, RULES)

    def test_lookup_refuses_unknown_name_with_choices(self) -> None:
        with self.assertRaises(ValueError) as caught:
            arm("missing")
        self.assertIn("missing", str(caught.exception))
        for item in ARMS:
            self.assertIn(item.name, str(caught.exception))

    def test_composition_carries_distinct_changes_and_refuses_conflicts(self) -> None:
        combined = compose(("hyphenated", "negator-5"))
        self.assertEqual(combined.name, "hyphenated,negator-5")
        self.assertTrue(combined.selectable)
        self.assertTrue(combined.rules.hyphenated)
        self.assertEqual(combined.rules.negator_reach, arm("negator-5").rules.negator_reach)
        for field in fields(PatternRules):
            if field.name not in {"hyphenated", "negator_reach"}:
                self.assertEqual(getattr(combined.rules, field.name),
                                 getattr(RULES_1, field.name))
        for names in (
            ("hyphenated", "hyphenated"),
            ("window-150", "window-600"),
            ("no-direction", "hyphenated"),
        ):
            with self.subTest(names=names), self.assertRaises(ValueError):
                compose(names)


class Command(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.contents = _sample_files()
        self.dataset = _fixture_dataset(*self.contents)

    def _run(
        self, argv: list[str], opener: Callable[[Request], _Response] | None = None,
    ) -> tuple[int, str, str]:
        output = io.StringIO()
        error = io.StringIO()
        with _pinned(self.dataset):
            code = treatment_command.main(
                argv, opener=opener, stdout=output, stderr=error, root=ROOT,
            )
        self.assertNotIn(SENTINEL, output.getvalue() + error.getvalue())
        return code, output.getvalue(), error.getvalue()

    def test_verified_files_skip_opener_and_print_licence_once(self) -> None:
        _write_files(self.root, self.dataset, self.contents)

        def unopened(request: Request) -> _Response:
            self.fail(f"unexpected request: {request.full_url}")

        code, output, error = self._run(["fetch", "--to", str(self.root)], unopened)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.count("Licence:"), 1)
        self.assertIn(self.dataset.licence, output)
        self.assertIn(self.dataset.attribution, output)
        self.assertIn("LegalBench states", output)
        self.assertEqual(output.count(": verified"), len(self.dataset.files))

    def test_digest_mismatch_deletes_partial_and_refuses(self) -> None:
        def bad_body(request: Request) -> _Response:
            return _Response(200, b"?" * len(self.contents[0]))

        code, _, error = self._run(["fetch", "--to", str(self.root)], bad_body)
        first = self.root / self.dataset.files[0].path
        self.assertEqual(code, 1)
        self.assertIn(self.dataset.files[0].path, error)
        self.assertIn("sha256", error)
        self.assertIn("Fix:", error)
        self.assertFalse(first.exists())
        self.assertFalse(first.with_name(first.name + ".partial").exists())

    def test_redirect_outside_group_refuses_before_second_request(self) -> None:
        calls: list[str] = []
        outside = "outside.example"

        def redirected(request: Request) -> _Response:
            calls.append(request.full_url)
            return _Response(302, headers={"Location": f"https://{outside}/fiction"})

        code, _, error = self._run(["fetch", "--to", str(self.root)], redirected)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn(outside, error)
        self.assertIn("Fix:", error)
        self.assertFalse((self.root / self.dataset.files[0].path).exists())

    def test_allowed_redirect_is_followed(self) -> None:
        loaded = load_egress_allowlist(ROOT / "config/egress.yaml")
        assert loaded.allowlist is not None
        group = loaded.allowlist.group("install-upgrade")
        assert group is not None
        next_host = next(entry.host for entry in group.hosts if entry.host != self.dataset.host)
        second = self.root / self.dataset.files[1].path
        second.parent.mkdir(parents=True, exist_ok=True)
        second.write_bytes(self.contents[1])
        calls: list[str] = []
        responses = [
            _Response(302, headers={"Location": f"https://{next_host}/fiction"}),
            _Response(200, self.contents[0]),
        ]

        def redirected(request: Request) -> _Response:
            calls.append(request.full_url)
            return responses.pop(0)

        code, output, error = self._run(["fetch", "--to", str(self.root)], redirected)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(len(calls), 2)
        self.assertEqual(urlsplit(calls[1]).hostname, next_host)
        self.assertEqual((self.root / self.dataset.files[0].path).read_bytes(),
                         self.contents[0])
        self.assertEqual(output.count("Licence:"), 1)

    def test_measure_missing_file_refuses_with_fetch_command(self) -> None:
        first = self.dataset.files[0]
        path = self.root / first.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.contents[0])
        code, output, error = self._run(["measure", "--data", str(self.root)])
        self.assertEqual((code, output), (1, ""))
        self.assertIn(self.dataset.files[1].path, error)
        self.assertIn(f"python3 -m tools.treatment fetch --to {self.root}", error)

    def test_measure_table_ids_and_no_sentence_text(self) -> None:
        _write_files(self.root, self.dataset, self.contents)
        code, output, error = self._run(["measure", "--data", str(self.root), "--ids"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("arm unit predicted tp fp fn precision recall", output)
        self.assertIn("baseline edge 2 1 1 2 0.500 0.333", output)
        self.assertIn("baseline sentence 3 2 1 1 0.667 0.667", output)
        self.assertIn("anchored coverage: 4 (positive 2, negative 2)", output)
        self.assertIn("lineage-only: 1", output)
        self.assertIn("baseline per verb: overruled=2", output)
        self.assertIn("baseline edge fp ids: train-1", output)
        self.assertIn("baseline edge fn ids: train-2, test-0", output)
        self.assertIn("baseline sentence fp ids: train-1", output)
        self.assertIn("baseline sentence fn ids: test-0", output)
        for item in ARMS:
            self.assertIn(f"\n{item.name} edge ", output)
        self.assertIn(f"\n{SHIPPED} edge ", output)
        self.assertNotIn(SENTINEL, output + error)

    def test_measure_selected_diagnostic_and_composed_arm(self) -> None:
        _write_files(self.root, self.dataset, self.contents)
        code, output, error = self._run([
            "measure", "--data", str(self.root),
            "--arm", "baseline", "--arm", "no-direction",
            "--compose", "window-150,hyphenated",
        ])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("baseline edge ", output)
        self.assertIn("no-direction edge ", output)
        self.assertIn("no-direction sentence ", output)
        self.assertIn(" diagnostic\n", output)
        self.assertIn("window-150,hyphenated edge ", output)
        self.assertIn("window-150,hyphenated sentence ", output)
        self.assertNotIn("window-600 edge ", output)
        self.assertEqual(output.count("anchored coverage:"), 1)

    def test_measure_refuses_unknown_arm_and_bad_composition(self) -> None:
        _write_files(self.root, self.dataset, self.contents)
        for choice, value, reason in (
            ("--arm", "missing", "unknown arm"),
            ("--compose", "window-150,window-600", "window"),
            ("--compose", "no-direction,hyphenated", "diagnostic"),
        ):
            with self.subTest(choice=choice, value=value):
                code, output, error = self._run([
                    "measure", "--data", str(self.root), choice, value,
                ])
                self.assertEqual((code, output), (2, ""))
                self.assertIn(reason, error)


class Imports(unittest.TestCase):
    def test_package_imports_stay_within_the_tool_and_pattern_modules(self) -> None:
        allowed = {
            "gideon.worker.treatment", "gideon.worker.citations", "gideon.host.egress",
        }
        for path in (ROOT / "tools/treatment").glob("*.py"):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                modules: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        modules.append(node.module or "")
                for module in modules:
                    self.assertTrue(
                        module.split(".")[0] in sys.stdlib_module_names
                        or module == "tools.treatment"
                        or module.startswith("tools.treatment.")
                        or module in allowed,
                        f"{path.name}: {module}",
                    )

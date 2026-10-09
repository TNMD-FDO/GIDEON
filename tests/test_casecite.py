"""The image-only case citation adapter and its content-free JSON-lines entry."""

import ast
import importlib
import io
import json
import logging
import re
import sys
import unittest
import warnings
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from gideon.casecite.__main__ import main
from gideon.casecite.found import EDGE_PATTERN_ID, FoundCitation
from gideon.extraction.contract import ExactObject, object_from_wire

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "gideon" / "casecite"

try:
    importlib.import_module("eyecite")
except ImportError:
    EYECITE_AVAILABLE = False
else:
    EYECITE_AVAILABLE = True


class Package(unittest.TestCase):
    """The adapter stays behind the image boundary."""

    def test_imports_stay_within_the_image_adapter_boundary(self) -> None:
        standard_library = set(sys.stdlib_module_names)
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names = (node.module or "",)
                else:
                    continue
                for name in names:
                    self.assertTrue(
                        name.split(".", 1)[0] in standard_library
                        or name == "eyecite"
                        or name.startswith("eyecite.")
                        or name.startswith("gideon.extraction.")
                        or name.startswith("gideon.casecite."),
                        f"{path}: import outside the adapter boundary: {name}",
                    )

    def test_pattern_ids_are_versioned_without_importing_eyecite(self) -> None:
        for path, name in (("adapter.py", "PATTERN_ID"), ("found.py", "EDGE_PATTERN_ID")):
            tree = ast.parse((PACKAGE / path).read_text(encoding="utf-8"))
            pattern_ids = [
                node.value.value
                for node in tree.body
                if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ]
            self.assertEqual(len(pattern_ids), 1)
            self.assertIsNotNone(
                re.fullmatch(r"eyecite/[a-z-]+@[1-9][0-9]*", pattern_ids[0])
            )
        self.assertEqual(EDGE_PATTERN_ID, pattern_ids[0])


class Entry(unittest.TestCase):
    """The JSON-lines entry runs in a dev venv with no eyecite installed."""

    @unittest.skipIf(EYECITE_AVAILABLE, "the adapter is available in this environment")
    def test_missing_eyecite_refuses_before_reading_input(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "stdin", io.StringIO("PRIVATE MATTER TEXT\n")),
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
        ):
            self.assertEqual(main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("adapter unavailable", stderr.getvalue())
        self.assertIn("Fix:", stderr.getvalue())
        self.assertNotIn("PRIVATE MATTER TEXT", stderr.getvalue())

    def test_wire_round_trip_and_no_warning_or_log_text(self) -> None:
        source = "[FICTIONAL TEST ONLY] 503 F. App’x. 345"
        citation = "503 F. App’x. 345"
        expected = ExactObject(
            "case_cite",
            source.index(citation),
            source.index(citation) + len(citation),
            citation,
            pattern_id="eyecite/full-case@1",
        )

        def fake_extract(text: str) -> tuple[ExactObject, ...]:
            warnings.warn(f"private warning: {text}", stacklevel=2)
            logging.getLogger("eyecite").error("private log: %s", text)
            return (expected,) if text == source else ()

        fake_adapter = ModuleType("gideon.casecite.adapter")
        fake_adapter.__dict__["extract_case_cites"] = fake_extract
        requests = (
            json.dumps({"id": "fiction-1", "text": source})
            + "\n"
            + json.dumps({"id": "fiction-2", "text": "[FICTIONAL TEST ONLY] no cite"})
            + "\n"
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        original_logging_level = logging.root.manager.disable
        with (
            patch.dict(sys.modules, {"gideon.casecite.adapter": fake_adapter}),
            patch.object(sys, "stdin", io.StringIO(requests)),
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
        ):
            self.assertEqual(main(), 0)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(tuple(row["id"] for row in rows), ("fiction-1", "fiction-2"))
        self.assertEqual(tuple(object_from_wire(obj) for obj in rows[0]["objects"]), (expected,))
        self.assertEqual(rows[1]["objects"], [])
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(logging.root.manager.disable, original_logging_level)

    def test_malformed_line_refuses_by_number_without_echoing_text(self) -> None:
        fake_adapter = ModuleType("gideon.casecite.adapter")
        fake_adapter.__dict__["extract_case_cites"] = lambda _text: ()
        for malformed in (
            "PRIVATE MATTER TEXT",
            json.dumps({"id": "PRIVATE MATTER TEXT"}),
            json.dumps({"id": "fiction-2", "text": 5}),
        ):
            with self.subTest(malformed=malformed):
                stdin = io.StringIO(
                    json.dumps({"id": "fiction-1", "text": "[FICTIONAL TEST ONLY]"})
                    + "\n"
                    + malformed
                    + "\n"
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.dict(sys.modules, {"gideon.casecite.adapter": fake_adapter}),
                    patch.object(sys, "stdin", stdin),
                    patch.object(sys, "stdout", stdout),
                    patch.object(sys, "stderr", stderr),
                ):
                    self.assertEqual(main(), 2)
                self.assertEqual(len(stdout.getvalue().splitlines()), 1)
                self.assertIn("line 2: invalid request", stderr.getvalue())
                self.assertIn("Fix:", stderr.getvalue())
                self.assertNotIn("PRIVATE MATTER TEXT", stderr.getvalue())

    def test_adapter_failure_does_not_echo_the_source_or_exception(self) -> None:
        source = "[FICTIONAL TEST ONLY] PRIVATE MATTER TEXT"

        def fail(text: str) -> tuple[ExactObject, ...]:
            raise RuntimeError(text)

        fake_adapter = ModuleType("gideon.casecite.adapter")
        fake_adapter.__dict__["extract_case_cites"] = fail
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(sys.modules, {"gideon.casecite.adapter": fake_adapter}),
            patch.object(sys, "stdin", io.StringIO(json.dumps({"id": "fiction-1", "text": source}) + "\n")),
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
        ):
            self.assertEqual(main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("line 1: extraction failed", stderr.getvalue())
        self.assertIn("Fix:", stderr.getvalue())
        self.assertNotIn("PRIVATE MATTER TEXT", stderr.getvalue())


@unittest.skipUnless(EYECITE_AVAILABLE, "eyecite runs only inside the pinned image")
class EyeciteAdapter(unittest.TestCase):
    """The pinned library's citation kinds and spans determine the objects."""

    def test_full_case_spans_and_determinism(self) -> None:
        from gideon.casecite.adapter import PATTERN_ID, extract_case_cites

        cases = (
            ("What can you tell me about 503 F. App’x. 345", "503 F. App’x. 345"),
            ("[FICTIONAL TEST ONLY] 503 F. App'x. 345", "503 F. App'x. 345"),
            (
                "see Strickland v. Washington, 466 U.S. 668, 687 (1984)",
                "466 U.S. 668",
            ),
            ("United States v. Booker, 543 U.S. 220 (2005)", "543 U.S. 220"),
            ("Bruton v. United States, 391 U.S. 123, 126 (1968)", "391 U.S. 123"),
            ("Shepard v. United States, 544 U.S. 13 (2005)", "544 U.S. 13"),
        )
        for source, citation in cases:
            with self.subTest(citation=citation):
                start = source.index(citation)
                expected = ExactObject(
                    "case_cite", start, start + len(citation), citation,
                    pattern_id=PATTERN_ID,
                )
                self.assertEqual(extract_case_cites(source), (expected,))
                self.assertEqual(extract_case_cites(source), (expected,))

    def test_non_full_citations_and_party_reference_are_not_objects(self) -> None:
        from gideon.casecite.adapter import extract_case_cites

        for source in (
            "18 U.S.C. § 3663A",
            "Tenn. Code Ann. § 39-17-417",
            "466 U.S. at 690",
            "Id. at 5",
            "Strickland, supra, at 690",
            "Strickland at 691",
        ):
            with self.subTest(source=source):
                self.assertEqual(extract_case_cites(source), ())

        source = "Strickland v. Washington, 466 U.S. 668 (1984). Strickland at 691."
        objects = extract_case_cites(source)
        self.assertEqual(tuple(obj.text for obj in objects), ("466 U.S. 668",))

    def test_parallel_citations_are_disjoint_and_ordered(self) -> None:
        from gideon.casecite.adapter import extract_case_cites

        source = "[FICTIONAL TEST ONLY] 466 U.S. 668, 104 S. Ct. 2052."
        self.assertEqual(
            tuple(obj.text for obj in extract_case_cites(source)),
            ("466 U.S. 668", "104 S. Ct. 2052"),
        )

    def test_find_citations_over_fictitious_opinion(self) -> None:
        from gideon.casecite.adapter import find_citations

        source = (
            "[FICTIONAL TEST OPINION] Strickland v. Washington, "
            "466 U.S. 668, 687 (1984). Id. at 690. "
            "Strickland, 466 U.S. at 691. Strickland, supra, at 692. "
            "466 U.S. 668. 104 S. Ct. 2052. "
            "18 U.S.C. § 922(g)(1). Id. at 5. § 3553."
        )
        found = find_citations(source)
        self.assertEqual(found, find_citations(source))
        self.assertTrue(all(isinstance(cite, FoundCitation) for cite in found))
        self.assertTrue(all(source[cite.start:cite.end] for cite in found))
        self.assertEqual(tuple(cite.start for cite in found), tuple(sorted(cite.start for cite in found)))

        def citation(fragment: str, occurrence: int = 0) -> FoundCitation:
            start = source.index(fragment)
            if occurrence:
                start = source.index(fragment, start + len(fragment))
            end = start + len(fragment)
            return next(cite for cite in found if cite.start < end and start < cite.end)

        full = citation("466 U.S. 668")
        self.assertEqual((full.form, full.kind), ("full", "case"))
        self.assertEqual(full.resource, found.index(full))
        self.assertEqual((full.volume, full.reporter, full.page), ("466", "U.S.", "668"))
        self.assertEqual(full.reporter_cite, "466 U.S. 668")
        self.assertEqual(full.pincite, "687")

        for fragment, form in (
            ("Id. at 690", "id"),
            ("466 U.S. at 691", "short"),
            ("supra, at 692", "supra"),
        ):
            with self.subTest(fragment=fragment):
                cite = citation(fragment)
                self.assertEqual((cite.form, cite.kind), (form, "case"))
                self.assertEqual(cite.resource, full.resource)
                self.assertEqual(
                    (cite.volume, cite.reporter, cite.page),
                    (full.volume, full.reporter, full.page),
                )

        repeated = citation("466 U.S. 668", 1)
        parallel = citation("104 S. Ct. 2052")
        self.assertEqual(repeated.resource, found.index(repeated))
        self.assertEqual(parallel.resource, found.index(parallel))
        self.assertNotEqual(full.resource, repeated.resource)
        self.assertNotEqual(parallel.resource, full.resource)
        law = citation("18 U.S.C. § 922(g)(1)")
        self.assertEqual((law.form, law.kind), ("full", "law"))
        self.assertIsNone(law.reporter_cite)
        law_id = citation("Id. at 5")
        self.assertEqual((law_id.form, law_id.kind), ("id", "law"))
        self.assertEqual(law_id.resource, law.resource)
        bare = source.index("§ 3553")
        self.assertFalse(any(cite.start <= bare < cite.end for cite in found))

    def test_unresolved_short_order_reference_and_canned_reply(self) -> None:
        from gideon.casecite.adapter import find_citations

        after_unknown = find_citations("[FICTIONAL TEST ONLY] § 3553. 466 U.S. 668")
        self.assertEqual(len(after_unknown), 1)
        self.assertEqual(after_unknown[0].resource, 0)

        short = find_citations("[FICTIONAL TEST ONLY] 466 U.S. at 690")
        self.assertEqual(len(short), 1)
        self.assertEqual((short[0].form, short[0].kind, short[0].resource),
                         ("short", "case", None))
        self.assertEqual((short[0].volume, short[0].reporter, short[0].page),
                         (None, None, None))
        self.assertIsNotNone(short[0].reporter_cite)

        # The library lists the bare section sign after the full cite; read
        # out of order, the sign would leave the Id. unresolved.
        ordered = find_citations("[FICTIONAL TEST ONLY] § 924(e)(1). 466 U.S. 668. Id.")
        self.assertEqual(tuple((cite.form, cite.kind, cite.resource) for cite in ordered),
                         (("full", "case", 0), ("id", "case", 0)))

        reference_text = "Wong Sun v. United States, 371 U.S. 471 (1963); Wong Sun at 485."
        reference = find_citations(reference_text)
        self.assertEqual(
            tuple((reference_text[cite.start:cite.end], cite.form, cite.kind, cite.resource)
                  for cite in reference),
            (("371 U.S. 471", "full", "case", 0),
             ("Wong Sun at 485", "reference", "case", 0)),
        )
        self.assertEqual(reference[1].reporter_cite, "371 U.S. 471")

        # The canned reply to the library's own name spans past the text.
        self.assertEqual(find_citations("eyecite"), ())

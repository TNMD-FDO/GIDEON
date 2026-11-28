"""The court file, the names a turn reads from it, and the stated scope they resolve to."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from regenerate_render_fixtures import fixture_lockfile_courts
from test_turn_legs import FIXTURES, turn_fixture

from gideon.extraction import extract
from gideon.extraction.contract import ExactObject, ObjectType
from gideon.host.courts import (
    LEVELS,
    STATE_CODES,
    CourtMap,
    load_court_map,
    render_errors,
)
from gideon.host.render.courts import api_courts_text
from gideon.turn import scope
from gideon.turn.scope import (
    CourtFile,
    CourtFileError,
    LockfileOutsideRelease,
    NamedCourt,
    StatedScope,
    read_court_file,
)

_FIRST_LOCKFILE = "corpus-2099-01-01"
_SECOND_LOCKFILE = "corpus-2099-01-02"
_MISSING_LOCKFILE = "corpus-2099-01-03"
_COURTS = """{
  "format": 1,
  "courts": {
    "top": {"level": "scotus", "circuit": null, "state": null,
      "name": "Supreme Court of the United States"},
    "ca6": {"level": "circuit", "circuit": "ca6", "state": null,
      "name": "Court of Appeals for the Sixth Circuit"},
    "ca9": {"level": "circuit", "circuit": "ca9", "state": null,
      "name": "Court of Appeals for the Ninth Circuit"},
    "cadc": {"level": "circuit", "circuit": "cadc", "state": null,
      "name": "Court of Appeals for the D.C. Circuit"},
    "cafc": {"level": "circuit", "circuit": "cafc", "state": null,
      "name": "Court of Appeals for the Federal Circuit"},
    "cax": {"level": "circuit", "circuit": "cax", "state": null,
      "name": "Circuit Court of Example"},
    "tnmd": {"level": "district", "circuit": "ca6", "state": "TN",
      "name": "District Court, M.D. Tennessee"},
    "tnwd": {"level": "district", "circuit": "ca6", "state": "TN",
      "name": "District Court of the Sixth Circuit, W.D. Tennessee"},
    "ord": {"level": "district", "circuit": "ca9", "state": "OR",
      "name": "District Court, D. Oregon"},
    "tenn": {"level": "state_supreme", "circuit": "ca6", "state": "TN",
      "name": "Supreme Court of Tennessee"},
    "tennx": {"level": "state_supreme", "circuit": "ca6", "state": "TN",
      "name": "High Court of Example"},
    "tennapp": {"level": "state_appellate", "circuit": "ca6", "state": "TN",
      "name": "Sixth Circuit Court of Tennessee"},
    "bap6": {"level": "other", "circuit": null, "state": null,
      "name": "Bankruptcy Appellate Panel of the Sixth Circuit"},
    "appealspanel": {"level": "other", "circuit": null, "state": null,
      "name": "Circuit Court of Appeals for the Ninth Circuit"},
    "shared": {"level": "other", "circuit": null, "state": null,
      "name": "  Ninth   CIR  "}
  },
  "lockfiles": {
    "corpus-2099-01-01": ["ca6", "ord", "top"],
    "corpus-2099-01-02": ["ca9", "tnmd"]
  }
}"""


class TurnScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "courts.json"
        self.file = self.read(_COURTS)

    def read(self, text: str) -> CourtFile:
        self.path.write_text(text, encoding="utf-8")
        return read_court_file(self.path)

    def test_court_levels_match_the_host_map_vocabulary(self) -> None:
        self.assertEqual(scope._LEVELS, LEVELS)

    def test_court_name_preserves_the_name_from_the_court_file(self) -> None:
        fixture = turn_fixture("courts.json")
        court_file = read_court_file(FIXTURES / "courts.json")
        self.assertEqual(court_file.court_name("fx-sup-al"), fixture["courts"]["fx-sup-al"]["name"])

    def test_strict_reader_refusals_name_faults_without_quoting_values(self) -> None:
        for old, new, fault in (
            ('"format": 1', '"format": true', "format"),
            ('"format": 1', '"format": 1.0', "format"),
            ('"format": 1', '"format": 2', "format"),
            ('"format": 1', '"sentinel": 1', "file keys"),
            ('"format": 1', '"format": 1, "sentinel": null', "file keys"),
            ('"format": 1', '"sentinel": 1, "sentinel": 1', "duplicate JSON key"),
            ('"format": 1', '"format": NaN', "JSON constant"),
            ('"format": 1', '"format": "sentinel', "JSON"),
            ('"name": "District Court, D. Oregon"', '"sentinel": "example"', "court keys"),
            ('"level": "district"', '"level": "sentinel"', "court level"),
            ('"name": "District Court, D. Oregon"', '"name": " "', "court name"),
            ('"state": "OR"', '"state": "sentinel"', "state code"),
            ('"ord": {', '"sentinel court id": {', "court id"),
            ('"circuit": "ca6"', '"circuit": "sentinel circuit id"', "circuit id"),
            (f'"{_FIRST_LOCKFILE}"', '"sentinel"', "lockfile label"),
            ('["ca6", "ord", "top"]', '"sentinel"', "lockfile court list"),
            ('["ca6", "ord", "top"]', '["sentinel court id"]', "lockfile court id"),
            ('["ca6", "ord", "top"]', '["ord", "ca6"]', "unsorted lockfile court list"),
        ):
            with self.subTest(fault=fault, new=new):
                with self.assertRaisesRegex(CourtFileError, fault) as caught:
                    self.read(_COURTS.replace(old, new, 1))
                self.assertNotIn("sentinel", str(caught.exception))

    def test_reader_refuses_wrong_containers_and_unreadable_files(self) -> None:
        for key in ("courts", "lockfiles"):
            with self.subTest(key=key):
                document = json.loads(_COURTS)
                document[key] = []
                with self.assertRaisesRegex(CourtFileError, rf"{key} mapping"):
                    self.read(json.dumps(document))
        self.path.write_bytes(b"\xff")
        with self.assertRaisesRegex(CourtFileError, "UTF-8"):
            read_court_file(self.path)
        self.path.unlink()
        with self.assertRaisesRegex(CourtFileError, "unreadable"):
            read_court_file(self.path)

    def test_circuit_forms_and_offsets(self) -> None:
        for court, spellings in {
            "ca6": (
                "Court of Appeals for the Sixth Circuit", "SiXTH\n\t CIRCUIT", "6th Circuit",
                "Sixth Cir.", "6th Cir.", "6TH CIR",
            ),
            "cadc": (
                "Court of Appeals for the D.C. Circuit", "District of Columbia Circuit",
                "D.C. Circuit", "D. C. Cir.", "D C Cir", "DC Cir", "DCCir",
            ),
            "cafc": ("Federal Circuit", "Fed. Circuit", "Fed. Cir.", "Fed Cir", "Fed.Cir."),
            "cax": ("Circuit Court of Example",),
        }.items():
            for spelling in spellings:
                with self.subTest(spelling=spelling):
                    prefix = "Straße — "
                    message = f"{prefix}{spelling} applies?"
                    self.assertEqual(self.file.match(message, ()), (
                        NamedCourt(court, len(prefix), len(prefix) + len(spelling)),
                    ))

    def test_shared_forms_boundaries_and_no_scope_names(self) -> None:
        for message in (
            "Bankruptcy Appellate Panel of the Sixth Circuit",
            "Supreme Court of the United States",
            "Tennessee v. Example",
            "W.D. Tennessee",
            "the arrest occurred in the District of Columbia",
            "Fifteenth Circuit",
            "Ninth Cir.",
            "xSixth Circuit or Sixth Circuitous or _6th Cir.",
            "Sixth Circuit Court of Appeals for the Ninth Circuit",
        ):
            with self.subTest(message=message):
                self.assertEqual(self.file.match(message, ()), ())

        message = "Bankruptcy Appellate Panel of the Sixth Circuit and the Sixth Circuit BAP"
        self.assertEqual(self.file.match(message, ()), (
            NamedCourt("ca6", message.rindex("Sixth Circuit"), message.index(" BAP")),
        ))

    def test_full_names_take_precedence_over_shorter_court_forms(self) -> None:
        for court, message in (
            ("tennapp", "Sixth Circuit Court of Tennessee"),
            ("tnwd", "District Court of the Sixth Circuit, W.D. Tennessee"),
        ):
            with self.subTest(message=message):
                self.assertEqual(self.file.match(message, ()), (NamedCourt(court, 0, len(message)),))

    def test_two_scoping_courts_drop_their_shared_forms(self) -> None:
        court_file = self.read(_COURTS.replace("Circuit Court of Example", "Ninth Circuit"))
        for message in ("Ninth Circuit", "9th Circuit", "9th Cir."):
            with self.subTest(message=message):
                self.assertEqual(court_file.match(message, ()), ())
        message = "Court of Appeals for the Ninth Circuit"
        self.assertEqual(court_file.match(message, ()), (NamedCourt("ca9", 0, len(message)),))

    def test_shared_abbreviations_drop_the_whole_form(self) -> None:
        court_file = self.read(_COURTS.replace("  Ninth   CIR  ", "D C Cir"))
        for message in ("D.C. Cir.", "D C Cir", "DCCir"):
            with self.subTest(message=message):
                self.assertEqual(court_file.match(message, ()), ())
        message = "D.C. Circuit"
        self.assertEqual(court_file.match(message, ()), (NamedCourt("cadc", 0, len(message)),))

    def test_citation_parentheticals_and_exact_span_discards(self) -> None:
        def exact(message: str, kind: ObjectType, spelling: str) -> ExactObject:
            start = message.index(spelling)
            return ExactObject(kind, start, start + len(spelling), spelling)

        for tail in (" (6th Cir.)", ", 552 (6th Cir. 2025)", ", 552–54, 558 (Sixth Circuit)"):
            with self.subTest(tail=tail):
                message = "126 F.4th 547" + tail
                self.assertEqual(self.file.match(message, (exact(message, "case_cite", "126 F.4th 547"),)), ())

        for message in (
            "No. 99-345 (Sixth Circuit)", "No. 99-345, at *4–*7 (Sixth Circuit)",
            "No. 99-345, at 5 & n.2 (6th Cir.)", "No. 99-345, slip op. at 5 (6th Cir.)",
            "No. 99-345, at 5 n.2, 7 and 9 (6th Cir.)",
        ):
            with self.subTest(message=message):
                self.assertEqual(self.file.match(message, extract(message)), ())

        for message in ("The rule (Sixth Circuit 2025)", "The rule (2025 (Sixth Circuit))"):
            with self.subTest(message=message):
                self.assertEqual(self.file.match(message, ()), ())

        message = "A party called Sixth Circuit Company"
        self.assertEqual(self.file.match(message, (exact(message, "caption", "Circuit Company"),)), ())

        for message in ("Can the defendant receive a Class D or E felony sentence?", "plan D or me"):
            with self.subTest(message=message):
                self.assertEqual(self.file.match(message, ()), ())
        for message in ("D. Or.", "D.Or.", "D. Or", "DOr", "District of Oregon"):
            with self.subTest(message=message):
                self.assertEqual(self.file.match(message, ()), (NamedCourt("ord", 0, len(message)),))

        for message in (
            "What is the standard of review (Sixth Circuit)?",
            "What relief is available under 28 U.S.C. § 2255 (Sixth Circuit)?",
            "Compare No. 99-345 with the answer here (Sixth Circuit).",
            "No. 99-345. What is the result (Sixth Circuit)?",
        ):
            with self.subTest(message=message):
                start = message.index("Sixth Circuit")
                self.assertEqual(self.file.match(message, extract(message)), (
                    NamedCourt("ca6", start, start + len("Sixth Circuit")),
                ))

    def test_resolution_preserves_first_match_order_and_checks_own_ids(self) -> None:
        message = "Ninth Circuit or 6th Cir.? Compare the Sixth Circuit."
        named = self.file.match(message, ())
        self.assertEqual(named, (
            NamedCourt("ca9", 0, len("Ninth Circuit")),
            NamedCourt("ca6", message.index("6th Cir."), message.index("?")),
        ))
        courts = ("ca6", "ca9", "ord", "tnmd", "tnwd", "top")
        self.assertEqual(self.file.resolve(named, (_FIRST_LOCKFILE,)), StatedScope(named, courts, ("ca9",)))
        scope = self.file.resolve(named, (_FIRST_LOCKFILE, _SECOND_LOCKFILE))
        self.assertEqual(scope, StatedScope(named, courts, ()))
        self.assertNotIn("named=", repr(scope))
        self.assertNotIn("start=", repr(scope))
        self.assertNotIn("Ninth Circuit", repr(scope))

    def test_district_and_state_widening(self) -> None:
        for identifier, courts, outside in (
            ("tnmd", ("tnmd", "top"), ("tnmd",)),
            ("tenn", ("tenn", "tennapp", "tennx", "top"), ("tenn",)),
            ("tennapp", ("tennapp", "top"), ("tennapp",)),
        ):
            with self.subTest(identifier=identifier):
                named = (NamedCourt(identifier, 0, 10),)
                self.assertEqual(self.file.resolve(named, (_FIRST_LOCKFILE,)), StatedScope(named, courts, outside))

    def test_missing_lockfile_refuses_only_a_scoped_turn(self) -> None:
        self.assertIsNone(self.file.resolve((), (_MISSING_LOCKFILE,)))
        named = self.file.match("Sixth Circuit", ())
        with self.assertRaises(LockfileOutsideRelease) as caught:
            self.file.resolve(named, (_FIRST_LOCKFILE, _MISSING_LOCKFILE))
        self.assertNotIn(_MISSING_LOCKFILE, str(caught.exception))


class CommittedCourtForms(unittest.TestCase):
    court_map: CourtMap
    file: CourtFile
    lockfile_courts: dict[str, tuple[str, ...]]

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parents[1] / "courts.yaml"
        loaded = load_court_map(path)
        assert loaded.ok and loaded.court_map is not None, render_errors(loaded.errors)
        cls.court_map = loaded.court_map
        cls.lockfile_courts = fixture_lockfile_courts()
        directory = cls.enterClassContext(tempfile.TemporaryDirectory())
        rendered = Path(directory) / "courts.json"
        rendered.write_text(api_courts_text(cls.court_map, cls.lockfile_courts), encoding="utf-8")
        cls.file = read_court_file(rendered)

    def test_lockfile_membership_uses_the_named_court_even_when_widening_finds_installed_courts(self) -> None:
        supreme, = self.court_map.ids_at_level("scotus")
        label = next(label for label, courts in self.lockfile_courts.items() if supreme in courts)
        installed = set(self.lockfile_courts[label])
        present, absent, another_absent = self.file.match("Sixth Circuit or Ninth Circuit or Fifth Circuit", ())
        self.assertIn(present.court, installed)
        self.assertNotIn(absent.court, installed)
        self.assertNotIn(another_absent.court, installed)

        scoped = self.file.resolve((present, absent, another_absent), (label,))
        absent_only = self.file.resolve((absent,), (label,))
        assert scoped is not None and absent_only is not None
        self.assertEqual(scoped.out_of_corpus, tuple(sorted((absent.court, another_absent.court))))
        self.assertIn(supreme, absent_only.courts)
        self.assertEqual(absent_only.out_of_corpus, (absent.court,))

    def test_pinned_labels_contribute_their_union_in_either_order(self) -> None:
        supreme, = self.court_map.ids_at_level("scotus")
        narrow = next(label for label, courts in self.lockfile_courts.items() if supreme not in courts)
        wide = next(label for label, courts in self.lockfile_courts.items() if supreme in courts)
        # The Supreme Court witnesses the labels' difference at the resolver boundary.
        # The matcher itself consumes its name without naming a court.
        named = (NamedCourt(supreme, 0, 1),)
        scoped = self.file.resolve(named, (narrow,))
        assert scoped is not None
        self.assertEqual(scoped.out_of_corpus, (supreme,))
        for labels in ((narrow, wide), (wide, narrow)):
            with self.subTest(labels=labels):
                self.assertEqual(self.file.resolve(named, labels), StatedScope(named, scoped.courts, ()))

    def test_all_scoping_map_names_and_derived_forms_match_unless_shared(self) -> None:
        enabled = {
            (pattern.pattern, identifier)
            for forms in self.file._forms.values() for pattern, identifier in forms
            if identifier is not None
        }
        dropped: set[str] = set()
        for court in self.court_map.courts.values():
            if court.level in ("scotus", "other"):
                continue
            name = " ".join(court.name.casefold().split())
            forms = tuple(scope._court_forms(self.file._courts[court.id]))
            self.assertIn(name, forms, court.id)
            for form in forms:
                self.assertNotIn(form, scope._STATE_ABBREVIATIONS)
                message = court.name if form == name else form
                if (scope._form_pattern(form).pattern, court.id) in enabled:
                    expected: tuple[NamedCourt, ...] = (NamedCourt(court.id, 0, len(message)),)
                else:
                    dropped.add(form)
                    expected = ()
                self.assertEqual(self.file.match(message, ()), expected, f"{court.id}: {message}")
        self.assertEqual(len(dropped), 0, sorted(dropped))

    def test_common_forms_match_in_mixed_case_without_periods_and_across_lines(self) -> None:
        by_name = {court.name: court.id for court in self.court_map.courts.values() if court.level != "other"}
        for form, name in (
            ("6th Cir.", "Court of Appeals for the Sixth Circuit"),
            ("Sixth Circuit", "Court of Appeals for the Sixth Circuit"),
            ("D.C. Cir.", "Court of Appeals for the D.C. Circuit"),
            ("M.D. Tenn.", "District Court, M.D. Tennessee"),
            ("S.D.N.Y.", "District Court, S.D. New York"),
            ("Middle District of Tennessee", "District Court, M.D. Tennessee"),
            ("Supreme Court of Tennessee", "Tennessee Supreme Court"),
            ("D. Kan.", "District Court, D. Kansas"),
            ("E.D. Va.", "District Court, E.D. Virginia"),
            ("D. Md.", "District Court, D. Maryland"),
            ("W.D. Pa.", "District Court, W.D. Pennsylvania"),
            ("District of Kansas", "District Court, D. Kansas"),
            ("D.D.C.", "District Court, District of Columbia"),
            ("District Court for the District of Columbia", "District Court, District of Columbia"),
            ("D.V.I.", "District Court, Virgin Islands"),
            ("District Court for the Virgin Islands", "District Court, Virgin Islands"),
            ("D.N. Mar. I.", "District Court, Northern Mariana Islands"),
            ("District Court for the Northern Mariana Islands", "District Court, Northern Mariana Islands"),
            ("Tennessee Court of Appeals", "Court of Appeals of Tennessee"),
        ):
            on_two_lines = form.replace(" ", "\n", 1) if " " in form else form.replace(".", ".\n", 1)
            for spelling in (form, form.swapcase(), form.replace(".", ""), on_two_lines):
                with self.subTest(spelling=spelling):
                    self.assertEqual(self.file.match(spelling, ()), (NamedCourt(by_name[name], 0, len(spelling)),))

    def test_abbreviation_table_covers_every_district_jurisdiction(self) -> None:
        self.assertEqual(len(scope._STATE_ABBREVIATIONS), len(STATE_CODES))
        for court in self.court_map.courts.values():
            if court.level == "district":
                short = court.name.casefold().removeprefix("district court, ")
                state = re.sub(r"^(?:[a-z]\.)+ ", "", short)
                self.assertIn(state, scope._STATE_ABBREVIATIONS, court.id)

    def test_supreme_court_other_courts_and_bare_places_name_no_court(self) -> None:
        for court in self.court_map.courts.values():
            if court.level in ("scotus", "other"):
                self.assertEqual(self.file.match(court.name, ()), (), court.id)
        for message in (
            *scope._STATE_ABBREVIATIONS,
            "Tennessee v. Smith",
            "Fifteenth Circuit",
            "Bankruptcy Appellate Panel of the Sixth Circuit",
            "the arrest occurred in the District of Columbia",
        ):
            self.assertEqual(self.file.match(message, ()), (), message)

    def test_resolution_matches_the_host_map_plus_circuit_districts(self) -> None:
        circuit = "Court of Appeals for the Sixth Circuit"
        district = "District Court, M.D. Tennessee"
        high_court = "Tennessee Supreme Court"
        by_name = {court.name: court.id for court in self.court_map.courts.values() if court.level != "other"}
        home = set(self.court_map.controlling_courts(by_name[circuit], (by_name[district],), (by_name[high_court],)))
        for names in (
            (circuit,), (district,), (high_court,), ("Court of Appeals of Tennessee",),
            ("Court of Appeals for the Ninth Circuit", high_court, "District Court, S.D. New York"),
        ):
            with self.subTest(names=names):
                identifiers = tuple(by_name[name] for name in names)
                named = self.file.match(" and ".join(names), ())
                self.assertEqual(tuple(match.court for match in named), identifiers)
                resolved = self.file.resolve(named, ())
                assert resolved is not None
                circuits = {identifier for identifier in identifiers if self.court_map.courts[identifier].level == "circuit"}
                expected = set(self.court_map.scope_courts(identifiers)) | {
                    court.id for court in self.court_map.courts.values()
                    if court.level == "district" and court.circuit in circuits
                }
                self.assertEqual(resolved.courts, tuple(sorted(expected)))
                self.assertTrue(home.intersection(resolved.courts))

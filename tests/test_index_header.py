"""Contracts for deterministic context headers and their embedding spaces."""

import hashlib
import unittest
from datetime import date

from test_models_lock import LOCK

from gideon.host import models
from gideon.host.index import header


class Composition(unittest.TestCase):
    def test_h1_collapses_each_part_and_uses_the_first_cite_and_decided_year(self) -> None:
        self.assertEqual(
            header.compose(
                "h1", "  Example\t v.\n Fable  ", (" 123\t Fict.\n  456 ", "789 Fict. 101"),
                "ignored-docket", " fiction\t court ", date(2099, 1, 1), " separate__\t writing ",
            ),
            "Example v. Fable, 123 Fict. 456 (fiction court 2099) — separate writing",
        )

    def test_h1_bounds_the_collapsed_name_in_code_points(self) -> None:
        name = "é" * header.CASE_NAME_MAX_LENGTH
        for expected in (name, name[:-1]):
            with self.subTest(code_points=len(expected)):
                self.assertEqual(
                    header.compose(
                        "h1", f"  {expected}\t beyond  ", ("123 Fict. 456",), None,
                        "fiction", date(2099, 1, 1), "majority",
                    ),
                    f"{expected}, 123 Fict. 456 (fiction 2099) — majority",
                )

    def test_h1_formats_missing_names_cites_and_dockets_without_stray_punctuation(self) -> None:
        for name, cites, docket, prefix in (
            ("Example v. Fable", (), "  D-\t  123  ", "Example v. Fable, No. D- 123 "),
            ("Example v. Fable", (), " \t ", "Example v. Fable "),
            (None, ("123 Fict. 456",), None, "123 Fict. 456 "),
            (" \t ", (), "D-123", "No. D-123 "),
            (None, (), None, ""),
        ):
            with self.subTest(name=name, cites=cites, docket=docket):
                self.assertEqual(
                    header.compose("h1", name, cites, docket, "fiction", date(2099, 1, 1), "majority"),
                    f"{prefix}(fiction 2099) — majority",
                )

    def test_bare_and_joined_inputs_and_hashes(self) -> None:
        text_slice = "  Fictitious slice.\n"
        context = "Fábula (fiction 2099) — majority"
        self.assertIsNone(header.RELEASE_HEADER)
        self.assertEqual(
            header.compose(header.RELEASE_HEADER, None, (), None, "fiction", date(2099, 1, 1), "majority"),
            "",
        )
        self.assertEqual(header.BARE_HEADER_HASH, hashlib.sha256(b"").hexdigest())
        self.assertEqual(header.header_hash(context), hashlib.sha256(context.encode("utf-8")).hexdigest())
        self.assertIs(header.embedding_input("", text_slice), text_slice)
        self.assertEqual(header.embedding_input(context, text_slice), f"{context}\n\n{text_slice}")


class Identity(unittest.TestCase):
    def test_header_id_grammar_and_registry(self) -> None:
        pattern = f"^(?:{header.HEADER_ID_PATTERN})\\Z"
        self.assertEqual(set(header.HEADERS), {"h1"})
        for value in (*header.HEADERS, "h0", "h42"):
            with self.subTest(value=value):
                self.assertRegex(value, pattern)
        for value in ("", "h", "H1", "h-1", "h1-extra", "h1\n", "h１"):
            with self.subTest(value=value):
                self.assertNotRegex(value, pattern)

    def test_space_suffix_round_trips_only_registered_ids(self) -> None:
        space = "fictitious-space"
        self.assertEqual(header.embedding_space(space, None), space)
        self.assertEqual(header.embedding_space(space, "h1"), f"{space}-h1")
        self.assertEqual(header.header_for_space(f"{space}-h1"), "h1")
        for bare in (space, "h1", f"{space}h1", f"{space}-h1-extra", f"{space}-h999"):
            with self.subTest(bare=bare):
                self.assertIsNone(header.header_for_space(bare))

    def test_unknown_and_invalid_ids_are_refused(self) -> None:
        for value in ("h999", "H1", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    header.embedding_space("fictitious-space", value)
                with self.assertRaises(ValueError):
                    header.compose(value, None, (), None, "fiction", date(2099, 1, 1), "majority")

    def test_profile_spaces_do_not_use_a_registered_header_suffix(self) -> None:
        result = models.load_models_lock(LOCK)
        self.assertTrue(result.ok, result.errors)
        assert result.lock is not None
        spaces = [profile.embedding_space.id for profile in result.lock.profiles if profile.embedding_space is not None]
        self.assertTrue(spaces)
        for space in spaces:
            with self.subTest(space=space):
                self.assertIsNone(header.header_for_space(space))

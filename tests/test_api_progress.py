"""The service progress line's forms, event shape, and import boundary."""

import ast
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.api import progress

ROOT = Path(__file__).resolve().parent.parent
PROGRESS_PATH = ROOT / "gideon/api/progress.py"
SITE_EXAMPLE = ROOT / "config/site.example.yaml"


def string_values(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from string_values(nested)
    elif isinstance(value, str):
        yield value


class Descriptions(unittest.TestCase):
    """The line contains fixed words and clock-derived digits alone."""

    def test_forms_and_period(self) -> None:
        self.assertEqual(progress.PERIOD_SECONDS, 5)
        self.assertEqual(progress.OPENING_FORM, "Thinking…")
        self.assertEqual(progress.RUNNING_FORM, "Thinking… {elapsed}")
        self.assertEqual(progress.CLOSING_FORM, "Thought for {elapsed}")
        self.assertEqual(progress.opening_description(), "Thinking…")

    def test_elapsed_writer_at_boundaries(self) -> None:
        for seconds, expected in (
            (0, "0s"),
            (59, "59s"),
            (60, "1m 0s"),
            (65, "1m 5s"),
            (3599, "59m 59s"),
            (3600, "60m 0s"),
            (3660, "61m 0s"),
            (-3, "0s"),
            (1.5, "0s"),
            ("65", "0s"),
            (True, "0s"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(progress.elapsed_text(seconds), expected)

    def test_running_and_closing_descriptions(self) -> None:
        self.assertEqual(progress.running_description(65), "Thinking… 1m 5s")
        self.assertEqual(progress.closing_description(237), "Thought for 3m 57s")
        self.assertEqual(progress.closing_description(-1), "Thought for 0s")

    def test_predicate_accepts_every_built_form(self) -> None:
        descriptions = [progress.opening_description()]
        for seconds in (0, 5, 59, 60, 65, 3599, 3600, 3660):
            descriptions.extend(
                (
                    progress.running_description(seconds),
                    progress.closing_description(seconds),
                )
            )
        for description in descriptions:
            with self.subTest(description=description):
                self.assertTrue(progress.is_progress_description(description))

    def test_predicate_rejects_text_outside_the_forms(self) -> None:
        for description in (
            "Thinking… 5s extra",
            "Thinking… xs",
            "Thought for 1m xs",
            "Thinking… " + "9" * 100 + "s",
            "Thinking… the model is reasoning",
            "A model-generated answer",
            "Thinking… 1m 60s",
            "Thinking… 05s",
            5,
        ):
            with self.subTest(description=description):
                self.assertFalse(progress.is_progress_description(description))


class Event(unittest.TestCase):
    """The frontend event value has one builder and one reader."""

    def test_builder_and_reader_round_trip(self) -> None:
        self.assertEqual(progress.STATUS_EVENT_KEY, "event")
        for description, done in (
            (progress.opening_description(), False),
            (progress.running_description(5), False),
            (progress.closing_description(5), True),
        ):
            with self.subTest(description=description):
                event = progress.build_status_event(description, done)
                self.assertEqual(
                    event,
                    {
                        "type": "status",
                        "data": {"description": description, "done": done},
                    },
                )
                self.assertEqual(progress.read_status_event(event), description)

    def test_reader_refuses_unreadable_events(self) -> None:
        for value in (
            None,
            "status",
            {},
            {"type": "replace", "data": {"description": "Thinking…"}},
            {"type": "status"},
            {"type": "status", "data": None},
            {"type": "status", "data": {}},
            {"type": "status", "data": {"description": 5}},
        ):
            with self.subTest(value=value):
                self.assertIsNone(progress.read_status_event(value))


class Hygiene(unittest.TestCase):
    """The shared module uses the standard library and no office value."""

    def test_module_imports_are_standard_library_only(self) -> None:
        tree = ast.parse(
            PROGRESS_PATH.read_text(encoding="utf-8"), filename=str(PROGRESS_PATH)
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(
                        alias.name.split(".")[0],
                        sys.stdlib_module_names,
                        f"progress.py imports non-standard module {alias.name}; the turn harness runs it outside the service image",
                    )
            if isinstance(node, ast.ImportFrom):
                assert node.module is not None
                self.assertIn(
                    node.module.split(".")[0],
                    sys.stdlib_module_names,
                    f"progress.py imports non-standard module {node.module}; the turn harness runs it outside the service image",
                )

    def test_forms_contain_no_site_value(self) -> None:
        site = yaml.safe_load(SITE_EXAMPLE.read_text(encoding="utf-8"))
        assert isinstance(site, dict)
        forms = (
            progress.OPENING_FORM,
            progress.RUNNING_FORM,
            progress.CLOSING_FORM,
        )
        for value in string_values(site):
            for form in forms:
                with self.subTest(value=value, form=form):
                    self.assertNotIn(value, form)

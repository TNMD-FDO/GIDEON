"""The frontend feedback boundary and office rating summary."""

import json
import unittest
from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path
from typing import cast

from gideon.host import owui, secrets
from gideon.host.render.owui import ALLOWED_ENDPOINTS, FEEDBACK_LIST_ROUTE
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.improvement import owuifeedback, ratings
from gideon.improvement.feedback import FeedbackReading, FeedbackRecord
from gideon.improvement.sections import Context, once
from gideon.improvement.triggers import TriggerRegistry

ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = ROOT / "tests/fixtures/frontend/feedback-list.json"
SITE_PATH = Path("/tmp/fictional-feedback/site.yaml")
SITE_TEXT = (ROOT / "config/site.example.yaml").read_text(encoding="utf-8")
SENTINEL = "FICTIONAL_FEEDBACK_SENTINEL_7Q"


class FakeHost:
    """A dict-backed host that records reads and can refuse a secret read."""

    def __init__(self, *, include_site: bool = True, secret: str | None = "fictional-key") -> None:
        self.files = {}
        if include_site:
            self.files[str(SITE_PATH)] = SITE_TEXT
        if secret is not None:
            self.files[str(secrets.secret_path(owuifeedback.ADMIN_KEY_SECRET))] = secret
        self.unreadable: set[str] = set()
        self.reads: list[str] = []
        self.euid = 0

    def geteuid(self) -> int:
        return self.euid

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = str(path)
        self.reads.append(key)
        if key in self.unreadable:
            raise PermissionError("fictional unreadable secret")
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]


class FakeClient:
    def __init__(self, pages: list[owui.Response]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
        del kwargs
        self.calls.append((method, path))
        if len(self.calls) <= len(self.pages):
            return self.pages[len(self.calls) - 1]
        return self.pages[-1]


def _page() -> dict[str, object]:
    return json.loads(PAGE_PATH.read_text(encoding="utf-8"))


def _rated_item(model: str, identifier: str, rating: int, created_at: int) -> dict[str, object]:
    return {
        "id": identifier,
        "type": owuifeedback.RATING_TYPE,
        "data": {"rating": rating, "model_id": model},
        "meta": {"chat_id": "fictional-chat", "message_id": "fictional-message"},
        "created_at": created_at,
    }


def _response(body: object, status: int = 200) -> owui.Response:
    return owui.Response(status, body)


def _reader(
    pages: list[owui.Response],
    *,
    include_site: bool = True,
    secret: str | None = "fictional-key",
) -> tuple[FakeHost, FakeClient, FeedbackReading | Problem]:
    host = FakeHost(include_site=include_site, secret=secret)
    client = FakeClient(pages)
    result = owuifeedback.read(
        cast(Host, host),
        SITE_PATH,
        lambda **kwargs: cast(owui.Client, _check_factory(client, kwargs)),
    )
    return host, client, result


def _check_factory(client: FakeClient, kwargs: dict[str, object]) -> FakeClient:
    if kwargs.get("api_key") != "fictional-key":
        raise AssertionError("the adapter did not pass its fake key")
    return client


def _admitted(path: str, endpoint: str) -> bool:
    return path == endpoint or path.startswith(endpoint + "/")


class FeedbackBoundary(unittest.TestCase):
    def test_record_is_five_field_frozen_slotted_and_excludes_frontend_text(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(FeedbackRecord)),
            ("rating", "chat_id", "message_id", "model_id", "created_at"),
        )
        self.assertEqual(
            FeedbackRecord.__slots__,
            ("rating", "chat_id", "message_id", "model_id", "created_at"),
        )
        record = FeedbackRecord("up", "fictional-chat", "fictional-message", "fictional-model", 7)
        with self.assertRaises(FrozenInstanceError):
            record.rating = "down"  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            record.extra = SENTINEL  # type: ignore[attr-defined]

        page = _page()
        decoded = owuifeedback.decode_page(page)
        self.assertIsInstance(decoded, FeedbackReading)
        assert isinstance(decoded, FeedbackReading)
        items = page["items"]
        assert isinstance(items, list)
        self.assertEqual(len(decoded.records), len(items))
        for value in decoded.records:
            self.assertNotIn(SENTINEL, repr(value))
            self.assertNotIn(SENTINEL, str(value))
            self.assertNotIn(SENTINEL, repr(asdict(value)))
        self.assertNotIn(SENTINEL, repr(decoded))
        self.assertNotIn(SENTINEL, str(decoded))
        self.assertNotIn(SENTINEL, repr(asdict(decoded)))

    def test_bad_items_are_skipped_and_counted(self) -> None:
        valid = cast(list[dict[str, object]], _page()["items"])[0]
        cases = ("string rating", "boolean rating", "missing id", "another type", "non-integer time")
        for case in cases:
            with self.subTest(case=case):
                item = json.loads(json.dumps(valid))
                if case == "string rating":
                    item["data"]["rating"] = "-1"
                elif case == "boolean rating":
                    item["data"]["rating"] = True
                elif case == "missing id":
                    del item["meta"]["message_id"]
                elif case == "another type":
                    item["type"] = "comment"
                else:
                    item["created_at"] = 1.5
                reading = owuifeedback.decode_items([item])
                self.assertEqual(reading.records, ())
                self.assertEqual(reading.skipped, 1)

    def test_rating_mapping_and_page_shape(self) -> None:
        self.assertEqual(owuifeedback.RATING_VALUES, {1: "up", -1: "down"})
        self.assertEqual(owuifeedback.decode_rating(1), "up")
        self.assertEqual(owuifeedback.decode_rating(-1), "down")
        for invalid in (True, False, 0, 2, "1", None):
            with self.subTest(rating=invalid):
                self.assertIsNone(owuifeedback.decode_rating(invalid))
        page = _page()
        reading = owuifeedback.decode_page(page)
        self.assertIsInstance(reading, FeedbackReading)
        assert isinstance(reading, FeedbackReading)
        by_model = {record.model_id: record.rating for record in reading.records}
        items = page["items"]
        assert isinstance(items, list)
        self.assertEqual(
            by_model,
            {
                item["data"]["model_id"]: owuifeedback.RATING_VALUES[item["data"]["rating"]]
                for item in items
            },
        )
        self.assertEqual(FEEDBACK_LIST_ROUTE, "/api/v1/evaluations/feedbacks/list")
        self.assertEqual(owuifeedback.FEEDBACK_LIST_ROUTE, FEEDBACK_LIST_ROUTE)
        self.assertTrue(any(_admitted(FEEDBACK_LIST_ROUTE, endpoint) for endpoint in ALLOWED_ENDPOINTS))
        self.assertFalse(any(
            _admitted("/api/v1/evaluations/feedbacks/all/export", endpoint)
            for endpoint in ALLOWED_ENDPOINTS
        ))

    def test_walk_reads_two_pages_honours_total_and_drops_repeated_ids(self) -> None:
        source = _page()
        items = cast(list[dict[str, object]], source["items"])
        first = {"items": [items[0]], "total": 2}
        second = {"items": [items[0], items[1]], "total": 2}
        _, client, result = _reader([_response(first), _response(second)])
        self.assertIsInstance(result, FeedbackReading)
        assert isinstance(result, FeedbackReading)
        self.assertEqual(len(result.records), 2)
        self.assertEqual(client.calls, [("GET", f"{FEEDBACK_LIST_ROUTE}?page=1"), ("GET", f"{FEEDBACK_LIST_ROUTE}?page=2")])

    def test_empty_page_ends_walk_even_below_total(self) -> None:
        _, client, result = _reader([_response({"items": [], "total": 9})])
        self.assertEqual(result, FeedbackReading((), 0))
        self.assertEqual(len(client.calls), 1)

    def test_page_bound_refuses_with_read_count(self) -> None:
        class GrowingClient:
            def __init__(self) -> None:
                self.calls = 0

            def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
                del method, path, kwargs
                self.calls += 1
                item = _rated_item(
                    "fictional-model", f"fictional-id-{self.calls}", 1, self.calls
                )
                return owui.Response(
                    200,
                    {"items": [item], "total": owuifeedback.FEEDBACK_PAGE_LIMIT + 1},
                )

        host = FakeHost()
        client = GrowingClient()

        def factory(**kwargs: object) -> owui.Client:
            if kwargs.get("api_key") != "fictional-key":
                raise AssertionError("the adapter did not pass its fake key")
            return cast(owui.Client, client)

        result = owuifeedback.read(
            cast(Host, host),
            SITE_PATH,
            factory,
        )
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn(str(owuifeedback.FEEDBACK_PAGE_LIMIT), result.problem)
        self.assertIn(f"{owuifeedback.FEEDBACK_PAGE_LIMIT} items read", result.problem)
        self.assertEqual(client.calls, owuifeedback.FEEDBACK_PAGE_LIMIT)
        self.assertIn("FEEDBACK_PAGE_LIMIT", result.fix)

    def test_every_read_refusal_has_its_fix_and_never_quotes_a_body(self) -> None:
        cases: list[tuple[str, FeedbackReading | Problem, str, str]] = []
        host = FakeHost(include_site=False)
        cases.append(("site", owuifeedback.read(cast(Host, host), SITE_PATH), "site file", "Correct the site file"))

        missing = FakeHost(secret=None)
        cases.append(("missing key", owuifeedback.read(cast(Host, missing), SITE_PATH), "missing", "apply"))

        unreadable = FakeHost()
        unreadable.unreadable.add(str(secrets.secret_path(owuifeedback.ADMIN_KEY_SECRET)))
        cases.append(("unreadable key", owuifeedback.read(cast(Host, unreadable), SITE_PATH), "unreadable", "Correct the secret file"))

        unprivileged = FakeHost()
        unprivileged.euid = 1000
        unprivileged.unreadable.add(str(secrets.secret_path(owuifeedback.ADMIN_KEY_SECRET)))
        cases.append(("key unreadable without root", owuifeedback.read(cast(Host, unprivileged), SITE_PATH), "unreadable", "as root"))

        class FailingClient:
            def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
                del method, path, kwargs
                raise owui.OwuiError("fictional frontend refusal", "Fix the fictional frontend.")

        cases.append(("OwuiError", owuifeedback.read(
            cast(Host, FakeHost()), SITE_PATH,
            lambda **kwargs: cast(owui.Client, FailingClient()),
        ), "frontend refusal", "fictional frontend"))
        cases.append(("non-2xx", _reader([_response("FICTIONAL_BODY_SENTINEL", 503)])[2], "HTTP 503", "availability"))
        cases.append(("bad shape", _reader([_response({"items": [], "body": "FICTIONAL_BODY_SENTINEL"})])[2], FEEDBACK_LIST_ROUTE, "pinned frontend"))

        for name, result, expected_problem, expected_fix in cases:
            with self.subTest(name=name):
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(expected_problem, result.problem)
                self.assertIn(expected_fix, result.fix)
                self.assertNotIn("FICTIONAL_BODY_SENTINEL", result.problem)
                self.assertNotIn("FICTIONAL_BODY_SENTINEL", result.fix)


class RatingsSectionCase(unittest.TestCase):
    def test_window_edge_group_order_details_and_unreadable_count(self) -> None:
        now = 50 * 24 * 60 * 60
        edge = now - ratings.FEEDBACK_WINDOW_DAYS * 24 * 60 * 60
        reading = FeedbackReading(
            (
                FeedbackRecord("up", "chat-z", "message-z", "fictional-model-z", edge),
                FeedbackRecord("down", "chat-a", "message-a", "fictional-model-a", now - 10),
                FeedbackRecord("up", "chat-b", "message-b", "fictional-model-z", now - 2),
                FeedbackRecord("down", "chat-old", "message-old", "fictional-model-a", edge - 1),
            ),
            skipped=1,
        )
        section = ratings.FEEDBACK_SECTION
        context = _context(reading, now)
        report = section.render(context)
        self.assertNotIsInstance(report, Problem)
        assert not isinstance(report, Problem)
        self.assertEqual(section.name, "feedback")
        self.assertEqual(section.scope, "office")
        self.assertEqual(report.detail, "3 ratings in the last 30 days, 5 listed, 1 unreadable")
        self.assertEqual(tuple(row.name for row in report.rows), ("fictional-model-a", "fictional-model-z"))
        self.assertEqual(report.rows[0].state, "rated")
        self.assertEqual(report.rows[0].detail, "up 0, down 1, newest 1970-02-19T23:59:50Z")
        self.assertEqual(report.rows[1].detail, "up 2, down 0, newest 1970-02-19T23:59:58Z")

    def test_empty_reading_and_problem_passthrough(self) -> None:
        empty = ratings.FEEDBACK_SECTION.render(_context(FeedbackReading((), 0), 7))
        self.assertNotIsInstance(empty, Problem)
        assert not isinstance(empty, Problem)
        self.assertEqual(empty.detail, "0 ratings in the last 30 days, 0 listed")
        self.assertEqual(empty.rows, ())
        problem = Problem("fictional feedback unavailable", "Fix the fictional source.")
        self.assertEqual(ratings.FEEDBACK_SECTION.render(_context(problem, 7)), problem)


def _context(reading: FeedbackReading | Problem, now: float) -> Context:
    return Context(
        host=cast(Host, FakeHost()),
        checkout_root=ROOT,
        rendered_dir=Path("/tmp/fictional-rendered"),
        registry=cast(TriggerRegistry, None),
        build_box=False,
        query=lambda _sql: (),
        feedback=lambda: reading,
        now=lambda: now,
    )


class SharedRead(unittest.TestCase):
    def test_first_result_and_problem_are_cached_for_later_calls(self) -> None:
        results: tuple[FeedbackReading | Problem, ...] = (
            FeedbackReading((), 0),
            Problem("fictional read refusal", "Fix the fictional source."),
        )
        for result in results:
            with self.subTest(problem=isinstance(result, Problem)):
                calls = 0

                def read(captured: FeedbackReading | Problem = result) -> FeedbackReading | Problem:
                    nonlocal calls
                    calls += 1
                    return captured

                shared = once(read)
                self.assertIs(shared(), result)
                self.assertIs(shared(), result)
                self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

"""Alias reads and atomic store actions over stdin-fed fake hosts."""

import json
import os
import subprocess
import unittest
from collections.abc import Sequence
from typing import Literal

from derivedstores_fake import empty_store_secrets
from test_derivedstores import RENDERED, FakeHost, Step, os_step, q_step
from test_index_record import NAME, SPACE

from gideon.host import derivedstores, report, secrets, stack
from gideon.host.index.aliases import (
    AliasTargets,
    alias_name,
    point_alias,
    read_aliases,
)
from gideon.host.index.generations import generation_name
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, QDRANT_SERVICE_NAME
from gideon.host.report import Problem

OLD = generation_name("caselaw", "corpus-2099-01-01", SPACE)
OLDER = generation_name("caselaw", "corpus-2098-12-31", SPACE)
STORES: tuple[Literal["lexical", "dense"], ...] = ("lexical", "dense")


def alias_host(steps: Sequence[Step]) -> FakeHost:
    return FakeHost(steps, secrets_present=False, files=empty_store_secrets())


class Reads(unittest.TestCase):
    """The collection alias's targets come from both stores, without any writes."""

    def test_collection_name_is_the_alias_name(self) -> None:
        for collection in ("caselaw", "fictitious-collection"):
            with self.subTest(collection=collection):
                self.assertEqual(alias_name(collection), collection)

    def test_both_listings_filter_other_aliases_and_sort_generation_names(self) -> None:
        host = alias_host([
            os_step("GET", "/_alias/caselaw", {
                OLD: {"aliases": {"caselaw": {}, "other": {}}},
                OLDER: {"aliases": {"caselaw": {"routing": "fictitious-route"}}},
            }),
            q_step("GET", "/aliases", {"result": {"aliases": [
                {"alias_name": "other", "collection_name": "unrelated-generation"},
                {"alias_name": "caselaw", "collection_name": OLD},
                {"alias_name": "caselaw", "collection_name": OLDER},
            ]}}),
        ])
        self.assertEqual(
            read_aliases(host, RENDERED, "caselaw", command_path="index promote"),
            AliasTargets((OLDER, OLD), (OLDER, OLD)),
        )
        self.assertFalse(host.steps)
        self.assertEqual(len(host.calls), 2)
        self.assertEqual(host.reads, [
            os.fspath(secrets.secret_path(OPENSEARCH_PASSWORD_SECRET_NAME)),
            os.fspath(secrets.secret_path(QDRANT_SECRET_NAME)),
        ])
        for argv, request, _ in host.calls:
            assert request is not None
            self.assertNotIn("POST", request)
            self.assertNotIn("data =", request)
            self.assertEqual(argv[:2], ("docker", "compose"))

    def test_opensearch_404_means_absent_and_qdrant_ignores_other_aliases(self) -> None:
        host = alias_host([
            os_step("GET", "/_alias/caselaw", {"error": "alias missing", "status": 404}, 404),
            q_step("GET", "/aliases", {"result": {"aliases": [
                {"alias_name": "other", "collection_name": NAME},
            ]}}),
        ])
        self.assertEqual(
            read_aliases(host, RENDERED, "caselaw", command_path="index promote"),
            AliasTargets((), ()),
        )
        self.assertEqual(len(host.calls), 2)

    def test_empty_listings_have_no_targets(self) -> None:
        host = alias_host([
            os_step("GET", "/_alias/caselaw", {}),
            q_step("GET", "/aliases", {"result": {"aliases": []}}),
        ])
        self.assertEqual(read_aliases(host, RENDERED, "caselaw", command_path="index promote"), AliasTargets((), ()))

    def test_off_shape_opensearch_listing_refuses_before_qdrant(self) -> None:
        invalid: tuple[object, ...] = (
            None, [], "fictitious-reply", {NAME: None}, {NAME: []},
            {NAME: {}}, {NAME: {"aliases": []}}, {NAME: {"aliases": {"other": {}}}},
            {NAME: {"aliases": {"caselaw": None}}},
            {"": {"aliases": {"caselaw": {}}}},
            {"error": "fictitious-diagnostic"},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                host = alias_host([os_step("GET", "/_alias/caselaw", payload)])
                result = read_aliases(host, RENDERED, "caselaw", command_path="index promote")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid alias listing", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, OPENSEARCH_SERVICE_NAME), result.fix)
                self.assertIn(report.command("index promote"), result.fix)
                self.assertEqual(len(host.calls), 1)

    def test_off_shape_qdrant_listing_refuses_the_whole_read(self) -> None:
        invalid_entries: tuple[object, ...] = (
            None, {}, [], {"alias_name": "caselaw"}, {"collection_name": NAME},
            {"alias_name": "caselaw", "collection_name": None},
            {"alias_name": "caselaw", "collection_name": ""},
            {"alias_name": None, "collection_name": NAME},
            {"alias_name": "", "collection_name": NAME},
        )
        invalid: tuple[object, ...] = (
            None, [], {}, {"result": None}, {"result": []}, {"result": {}},
            {"result": {"aliases": None}}, {"result": {"aliases": {}}},
            *({"result": {"aliases": [entry]}} for entry in invalid_entries),
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                host = alias_host([
                    os_step("GET", "/_alias/caselaw", {OLD: {"aliases": {"caselaw": {}}}}),
                    q_step("GET", "/aliases", payload),
                ])
                result = read_aliases(host, RENDERED, "caselaw", command_path="index promote")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("invalid alias listing", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME), result.fix)
                self.assertIn(report.command("index promote"), result.fix)
                self.assertEqual(len(host.calls), 2)

    def test_http_and_transport_failures_refuse_without_retry(self) -> None:
        cases = (
            os_step("GET", "/_alias/caselaw", {"error": "forbidden"}, 403),
            Step(OPENSEARCH_SERVICE_NAME, "GET", "/_alias/caselaw", "malformed reply"),
            Step(OPENSEARCH_SERVICE_NAME, "GET", "/_alias/caselaw", error=FileNotFoundError()),
            Step(OPENSEARCH_SERVICE_NAME, "GET", "/_alias/caselaw", returncode=127),
            q_step("GET", "/aliases", {"status": {"error": "missing endpoint"}}, 404),
            Step(QDRANT_SERVICE_NAME, "GET", "/aliases", "malformed reply"),
            Step(QDRANT_SERVICE_NAME, "GET", "/aliases", error=subprocess.TimeoutExpired("fake", 1)),
        )
        for step in cases:
            with self.subTest(step=step):
                steps = [step] if step.service == OPENSEARCH_SERVICE_NAME else [
                    os_step("GET", "/_alias/caselaw", {}, 404), step,
                ]
                host = alias_host(steps)
                result = read_aliases(host, RENDERED, "caselaw", command_path="index promote")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.fix, stack.logs_fix(RENDERED, step.service))
                self.assertEqual(len(host.calls), len(steps))
                self.assertFalse(host.steps)


class Writes(unittest.TestCase):
    """Each changed target uses one atomic actions request; a rerun converges."""

    def test_lexical_actions_remove_all_targets_then_add_in_one_request(self) -> None:
        for current, expected, detail in (
            ((), [{"add": {"index": NAME, "alias": "caselaw"}}], f"none -> {NAME}"),
            ((OLDER, OLD), [
                {"remove": {"index": OLDER, "alias": "caselaw"}},
                {"remove": {"index": OLD, "alias": "caselaw"}},
                {"add": {"index": NAME, "alias": "caselaw"}},
            ], f"{OLDER}, {OLD} -> {NAME}"),
        ):
            with self.subTest(current=current):
                host = alias_host([os_step("POST", "/_aliases", {"acknowledged": True})])
                self.assertEqual(point_alias(host, RENDERED, "lexical", "caselaw", NAME, current, command_path="index promote"), detail)
                self.assertEqual(len(host.calls), 1)
                request = host.calls[0][1]
                assert request is not None
                data = next(line.removeprefix("data = ") for line in request.splitlines() if line.startswith("data = "))
                self.assertEqual(json.loads(json.loads(data)), {"actions": expected})
                self.assertEqual(point_alias(host, RENDERED, "lexical", "caselaw", NAME, (NAME,), command_path="index promote"), "unchanged")
                self.assertEqual(len(host.calls), 1)

    def test_dense_actions_delete_existing_alias_then_create_in_one_request(self) -> None:
        for current, expected, detail in (
            ((), [{"create_alias": {"collection_name": NAME, "alias_name": "caselaw"}}], f"none -> {NAME}"),
            ((OLD,), [
                {"delete_alias": {"alias_name": "caselaw"}},
                {"create_alias": {"collection_name": NAME, "alias_name": "caselaw"}},
            ], f"{OLD} -> {NAME}"),
        ):
            with self.subTest(current=current):
                host = alias_host([q_step("POST", "/collections/aliases", {"result": True, "status": "ok"})])
                self.assertEqual(point_alias(host, RENDERED, "dense", "caselaw", NAME, current, command_path="index promote"), detail)
                self.assertEqual(len(host.calls), 1)
                request = host.calls[0][1]
                assert request is not None
                self.assertEqual(json.loads(request.partition("\r\n\r\n")[2]), {"actions": expected})
                self.assertEqual(point_alias(host, RENDERED, "dense", "caselaw", NAME, (NAME,), command_path="index promote"), "unchanged")
                self.assertEqual(len(host.calls), 1)

    def test_unchanged_reads_no_secret_and_runs_no_child(self) -> None:
        for store in STORES:
            with self.subTest(store=store):
                host = FakeHost([], secrets_present=False)
                self.assertEqual(point_alias(host, RENDERED, store, "caselaw", NAME, (NAME,), command_path="index promote"), "unchanged")
                self.assertEqual(host.calls, [])
                self.assertEqual(host.reads, [])

    def test_invalid_acknowledgements_refuse_with_store_logs(self) -> None:
        invalid: tuple[object, ...] = (None, [], {}, {"acknowledged": False, "result": False}, {"acknowledged": 1, "result": 1})
        for store in STORES:
            for payload in invalid:
                with self.subTest(store=store, payload=payload):
                    step = os_step("POST", "/_aliases", payload) if store == "lexical" else q_step("POST", "/collections/aliases", payload)
                    host = alias_host([step])
                    result = point_alias(host, RENDERED, store, "caselaw", NAME, (OLD,), command_path="index promote")
                    self.assertIsInstance(result, Problem)
                    assert isinstance(result, Problem)
                    self.assertIn("invalid alias update", result.problem)
                    self.assertIn(stack.logs_fix(RENDERED, step.service), result.fix)
                    self.assertIn(report.command("index promote"), result.fix)
                    self.assertEqual(len(host.calls), 1)

    def test_failed_child_overrides_success_body_and_http_failures_refuse(self) -> None:
        for step in (
            Step(OPENSEARCH_SERVICE_NAME, "POST", "/_aliases", os_step("POST", "/_aliases", {"acknowledged": True}).stdout, returncode=7),
            os_step("POST", "/_aliases", {"error": "index missing"}, 404),
            Step(QDRANT_SERVICE_NAME, "POST", "/collections/aliases", q_step("POST", "/collections/aliases", {"result": True}).stdout, returncode=127),
            q_step("POST", "/collections/aliases", {"status": {"error": "collection missing"}}, 400),
        ):
            with self.subTest(step=step):
                host = alias_host([step])
                store: Literal["lexical", "dense"] = "lexical" if step.service == OPENSEARCH_SERVICE_NAME else "dense"
                result = point_alias(host, RENDERED, store, "caselaw", NAME, (OLD,), command_path="index promote")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.fix, stack.logs_fix(RENDERED, step.service))
                self.assertEqual(len(host.calls), 1)


class Credentials(unittest.TestCase):
    """Each store's selected credential travels only inside the stdin request."""

    def test_reads_and_writes_keep_secrets_out_of_argv(self) -> None:
        host = alias_host([
            os_step("GET", "/_alias/caselaw", {}, 404),
            q_step("GET", "/aliases", {"result": {"aliases": []}}),
            os_step("POST", "/_aliases", {"acknowledged": True}),
            q_step("POST", "/collections/aliases", {"result": True}),
        ])
        self.assertEqual(read_aliases(host, RENDERED, "caselaw", command_path="index promote"), AliasTargets((), ()))
        for store in STORES:
            self.assertEqual(point_alias(host, RENDERED, store, "caselaw", NAME, (), command_path="index promote"), f"none -> {NAME}")
        for argv, request, timeout in host.calls:
            assert request is not None
            secret_name = QDRANT_SECRET_NAME if QDRANT_SERVICE_NAME in argv else OPENSEARCH_PASSWORD_SECRET_NAME
            credential = host.files[os.fspath(secrets.secret_path(secret_name))].strip()
            self.assertIn(credential, request)
            self.assertNotIn(credential, " ".join(argv))
            self.assertNotIn(NAME, " ".join(argv))
            self.assertEqual(timeout, derivedstores.STORE_LIST_WAIT_SECONDS + derivedstores.STORE_REQUEST_MARGIN_SECONDS)

    def test_missing_secrets_refuse_before_the_affected_store_request(self) -> None:
        for store in STORES:
            with self.subTest(store=store):
                host = FakeHost([], secrets_present=False)
                result = point_alias(host, RENDERED, store, "caselaw", NAME, (), command_path="index promote")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(report.command("apply"), result.fix)
                self.assertEqual(host.calls, [])
        for secret_name, steps in (
            (OPENSEARCH_PASSWORD_SECRET_NAME, []),
            (QDRANT_SECRET_NAME, [os_step("GET", "/_alias/caselaw", {}, 404)]),
        ):
            with self.subTest(secret_name=secret_name):
                host = alias_host(steps)
                host.files.pop(os.fspath(secrets.secret_path(secret_name)))
                read_result = read_aliases(host, RENDERED, "caselaw", command_path="index promote")
                self.assertIsInstance(read_result, Problem)
                self.assertEqual(len(host.calls), len(steps))

"""Restore levelling over a generation record and the two derived-store APIs."""

import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace

from derivedstores_fake import (
    empty_store_secrets,
    level_store_result,
    recovery_store_result,
)
from test_index_record import LABEL, NAME, RENDERED, SPACE, row_for

from gideon.host import derivedstores, report, secrets, stack, worker
from gideon.host.corpus.record import database_fix
from gideon.host.index import level, record
from gideon.host.index.generations import generation_name
from gideon.host.render.opensearch import (
    OPENSEARCH_PASSWORD_SECRET_NAME,
    OPENSEARCH_SERVICE_NAME,
)
from gideon.host.render.qdrant import QDRANT_SECRET_NAME, QDRANT_SERVICE_NAME
from gideon.host.sysio import Command, PathLike, RealHost


def generation(
    state: str, *, label: str = LABEL, space: str = SPACE,
    counts: Mapping[str, int] | None = None,
) -> record.GenerationRow:
    """A visibly fictitious generation whose timestamps obey the record's checks."""

    row = row_for()
    complete = state in ("built", "live", "retired")
    return replace(
        row, gen_id=generation_name("caselaw", label, space),
        corpus_lockfile=label, lockfiles=(label,), embedding_space=space, state=state,
        counts={"indexed": 7, "present": 3, "dense_total": 12, "dense_points": 1} if counts is None else counts,
        dense_built_at=row.lexical_built_at if complete else None,
        built_at=row.lexical_built_at if complete else None,
        promoted_at=row.lexical_built_at if state in ("live", "retired") else None,
        retired_at=row.lexical_built_at if state == "retired" else None,
        clustered_at=row.lexical_built_at, failure_reason="store" if state == "failed" else None,
    )


class FakeHost(RealHost):
    """Keep record and store tables in memory, with injectable child failures."""

    def __init__(
        self, rows: Sequence[record.GenerationRow] = (), *,
        counts: Mapping[str, Mapping[str, int | None]] | None = None,
        aliases: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
    ) -> None:
        super().__init__()
        self.rows = list(rows)
        self.counts: dict[str, dict[str, int | None]] = {
            OPENSEARCH_SERVICE_NAME: {
                row.gen_id: row.counts.get("indexed", 0) + row.counts.get("present", 0) for row in rows
            },
            QDRANT_SERVICE_NAME: {row.gen_id: row.counts.get("dense_total", 0) for row in rows},
        } if counts is None else {store: dict(values) for store, values in counts.items()}
        self.aliases = {store: dict(values) for store, values in (aliases or {}).items()}
        self.files = dict(empty_store_secrets())
        self.reads: list[str] = []
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.events: list[str] = []
        self.failures: dict[str, tuple[int, str, str]] = {}
        self.errors: dict[str, OSError] = {}

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, timeout, passthrough
        assert env is None and input is not None
        command = tuple(argv)
        payload = None
        if command == tuple(worker.psql_argv(RENDERED)):
            if "SELECT row_to_json(generation)" in input:
                event = "record read"
            else:
                assert "DO $demotion$" in input
                payload = json.loads(input.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
                event = "demote " + payload["name"]
        elif QDRANT_SERVICE_NAME in command:
            method, path, _ = input.split("\r\n", 1)[0].split(" ")
            event = f"{QDRANT_SERVICE_NAME} {method} {path}"
        else:
            assert OPENSEARCH_SERVICE_NAME in command
            options = dict(line.split(" = ", 1) for line in input.splitlines())
            path = json.loads(options["url"]).removeprefix("http://127.0.0.1:9200")
            event = f'{OPENSEARCH_SERVICE_NAME} {json.loads(options["request"])} {path}'
        self.calls.append((command, input))
        self.events.append(event)
        if event in self.errors:
            raise self.errors[event]
        if event in self.failures:
            code, stdout, stderr = self.failures[event]
            return subprocess.CompletedProcess(list(command), code, stdout, stderr)
        if event == "record read":
            stdout = "".join(json.dumps(asdict(row)) + "\n" for row in self.rows)
            return subprocess.CompletedProcess(list(command), 0, stdout, "")
        if payload is not None:
            index = next(i for i, row in enumerate(self.rows) if row.gen_id == payload["name"])
            row = self.rows[index]
            if row.state not in ("built", "live"):
                return subprocess.CompletedProcess(list(command), 3, "", "generation is no longer built or live")
            lexical, dense = payload["lexical_short"], payload["dense_short"]
            self.rows[index] = replace(
                row, state="building", built_at=None, promoted_at=None,
                lexical_built_at=None if lexical else row.lexical_built_at,
                dense_built_at=None if dense else row.dense_built_at,
            )
            stdout = json.dumps(asdict(record.DemotionOutcome(row.gen_id, row.state, lexical, dense)))
            return subprocess.CompletedProcess(list(command), 0, stdout + "\n", "")
        result = level_store_result(command, input, RENDERED, counts=self.counts, aliases=self.aliases)
        if result is None:
            result = recovery_store_result(command, input, RENDERED)
        if result is None:
            raise AssertionError(f"unexpected child: {command}")
        return result

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        name = os.fspath(path)
        self.reads.append(name)
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]


class Levelling(unittest.TestCase):
    def checked(
        self, host: FakeHost, recovered: Sequence[derivedstores.StoreRecovery] = (),
    ) -> level.LevelReport:
        result = level.level_all(host, RENDERED, recovered=recovered, command_path="restore")
        self.assertIsInstance(result, level.LevelReport, result)
        assert isinstance(result, level.LevelReport)
        for outcome in result.outcomes:
            clause = level.level_clause(outcome)
            self.assertNotIn("; ", clause)
            self.assertTrue(any(pattern.fullmatch(clause) for pattern in level.level_clause_patterns()), clause)
        return result

    def test_empty_record_needs_no_store_and_has_its_own_clause(self) -> None:
        host = FakeHost()
        host.files.clear()
        recovered = (derivedstores.StoreRecovery("qdrant", (NAME,), 0.0, False),)
        result = self.checked(host, recovered)
        self.assertEqual(result, level.LevelReport((), (), ()))
        self.assertEqual(host.events, ["record read"])
        self.assertEqual(host.reads, [])
        self.assertEqual(level.level_clauses(result), ("level: no generation recorded",))
        self.assertTrue(any(pattern.fullmatch(level.level_clauses(result)[0]) for pattern in level.level_clause_patterns()))
        self.assertEqual(level.level_audit(result), {})
        self.assertEqual(level.next_lines(result), ())

    def test_live_whole_aliases_are_pointed_then_unchanged_on_a_second_run(self) -> None:
        row = generation("live")
        old = generation("retired", label="corpus-2098-01-01").gen_id
        host = FakeHost([row], aliases={
            "opensearch": {"caselaw": (old,)}, "qdrant": {},
        })
        result = self.checked(host)
        expected = level.LevelOutcome(
            row.gen_id, row.collection, row.state, "aliased",
            row.counts["indexed"] + row.counts["present"], row.counts["indexed"] + row.counts["present"],
            row.counts["dense_total"], row.counts["dense_total"],
            f"{old} -> {row.gen_id}", f"none -> {row.gen_id}", row.corpus_lockfile,
        )
        self.assertEqual(result, level.LevelReport((expected,), (), ()))
        self.assertEqual(host.rows, [row])
        self.assertEqual(host.aliases, {store: {"caselaw": (row.gen_id,)} for store in ("opensearch", "qdrant")})
        self.assertEqual(level.level_clause(expected),
            f"level {row.gen_id}: live, whole (opensearch {expected.lexical_count} of {expected.lexical_expected}, "
            f"qdrant {expected.dense_count} of {expected.dense_expected}), "
            f"alias lexical {old} -> {row.gen_id}, dense none -> {row.gen_id}")
        before = len(host.events)
        second = self.checked(host)
        self.assertEqual(second.outcomes, (replace(expected, lexical_alias="unchanged", dense_alias="unchanged"),))
        self.assertFalse(any("POST /_aliases" in event or "POST /collections/aliases" in event for event in host.events[before:]))
        self.assertEqual(host.rows, [row])

    def test_built_whole_and_counts_above_the_record_write_nothing(self) -> None:
        rows = [generation("built"), generation("built", label="corpus-2099-02-03")]
        host = FakeHost(rows)
        for store in host.counts:
            count = host.counts[store][rows[1].gen_id]
            assert count is not None
            host.counts[store][rows[1].gen_id] = count + 1
        result = self.checked(host)
        self.assertEqual([outcome.outcome for outcome in result.outcomes], ["whole", "whole"])
        self.assertEqual(host.rows, rows)
        self.assertEqual(result.next_builds, ())
        self.assertEqual(result.next_promotes, ())
        self.assertEqual(len(host.events), 1 + 2 + 2 * len(rows))
        self.assertEqual(result.outcomes[1].dense_expected, rows[1].counts["dense_total"])
        self.assertNotEqual(result.outcomes[1].dense_expected, rows[1].counts["dense_points"])
        for secret_name in (OPENSEARCH_PASSWORD_SECRET_NAME, QDRANT_SECRET_NAME):
            # One read for aliases, one shared by all the count requests.
            self.assertEqual(host.reads.count(os.fspath(secrets.secret_path(secret_name))), 2)

    def test_short_or_absent_halves_are_demoted_and_the_other_half_is_kept(self) -> None:
        for state in ("built", "live"):
            for lexical_short, dense_short, absent in (
                (True, False, False), (False, True, False), (True, True, False),
                (True, False, True), (False, True, True), (True, True, True),
            ):
                with self.subTest(state=state, lexical=lexical_short, dense=dense_short, absent=absent):
                    row = generation(state)
                    host = FakeHost([row], aliases={"opensearch": {"caselaw": (row.gen_id,)}})
                    for store, short in (("opensearch", lexical_short), ("qdrant", dense_short)):
                        if short:
                            count = host.counts[store][row.gen_id]
                            assert count is not None
                            host.counts[store][row.gen_id] = None if absent else count - 1
                    result = self.checked(host)
                    self.assertEqual(result.outcomes[0].outcome, "demoted")
                    self.assertEqual(result.outcomes[0].state, state)
                    self.assertEqual(result.outcomes[0].lexical_alias, "stands")
                    self.assertEqual(result.next_builds, ((row.collection, row.corpus_lockfile),))
                    self.assertEqual(result.next_promotes, (row.gen_id,) if state == "live" else ())
                    self.assertEqual(host.rows, [replace(
                        row, state="building", built_at=None, promoted_at=None,
                        lexical_built_at=None if lexical_short else row.lexical_built_at,
                        dense_built_at=None if dense_short else row.dense_built_at,
                    )])
                    self.assertEqual(host.events[-1], "demote " + row.gen_id)
                    self.assertEqual(host.aliases["opensearch"]["caselaw"], (row.gen_id,))
                    clause = level.level_clause(result.outcomes[0])
                    self.assertIn("alias lexical stands", clause)
                    if absent and lexical_short and dense_short:
                        self.assertIn("absent in qdrant and opensearch, now building", clause)
                    else:
                        self.assertIn(f"{state} -> building", clause)
                    before = len(host.events)
                    second = self.checked(host)
                    self.assertEqual(second.outcomes[0].outcome, "left")
                    self.assertEqual(level.level_clause(second.outcomes[0]), f"level {row.gen_id}: building, left to its resume")
                    self.assertEqual(host.events[before:], ["record read"])

    def test_inactive_rows_and_recovered_names_are_left_once_in_order(self) -> None:
        rows = [generation(state, label=f"corpus-2099-01-0{index}")
                for index, state in enumerate(("building", "retired", "failed"), start=1)]
        rowless = "fictitious-unrecorded-index"
        names = tuple(row.gen_id for row in rows)
        recovered = (
            derivedstores.StoreRecovery("qdrant", (*names, rowless), 0.0, False),
            derivedstores.StoreRecovery("opensearch", (rowless, *reversed(names)), 0.0, False),
        )
        host = FakeHost(rows)
        host.files.clear()
        result = self.checked(host, recovered)
        self.assertEqual(tuple(outcome.name for outcome in result.outcomes), (*names, rowless))
        self.assertEqual(tuple(outcome.state for outcome in result.outcomes), ("building", "retired", "failed", "none"))
        self.assertTrue(all(outcome.outcome == "left" for outcome in result.outcomes))
        self.assertEqual(host.events, ["record read"])
        self.assertEqual(host.reads, [])
        self.assertEqual(host.rows, rows)
        self.assertEqual(level.level_clause(result.outcomes[1]), f"level {names[1]}: retired, left")
        self.assertEqual(level.level_clause(result.outcomes[2]), f"level {names[2]}: failed, left")
        self.assertEqual(level.level_clause(result.outcomes[3]), f"level {rowless}: no row, left")

    def test_unreadable_expected_counts_are_left_without_count_requests(self) -> None:
        for state in ("built", "live"):
            for missing in ("indexed", "present", "dense_total"):
                with self.subTest(state=state, missing=missing):
                    row = generation(state)
                    row = replace(row, counts={key: count for key, count in row.counts.items() if key != missing})
                    host = FakeHost([row])
                    result = self.checked(host)
                    self.assertEqual(result.outcomes[0].outcome, "left")
                    self.assertIsNone(result.outcomes[0].lexical_count)
                    self.assertIsNone(result.outcomes[0].dense_count)
                    self.assertEqual(level.level_clause(result.outcomes[0]), f"level {row.gen_id}: {state}, counts unreadable, left")
                    self.assertEqual(host.events, ["record read", "opensearch GET /_alias/caselaw", "qdrant GET /aliases"])
                    self.assertEqual(host.rows, [row])

    def test_two_live_rows_refuse_before_any_store_request(self) -> None:
        host = FakeHost([generation("live"), generation("live", label="corpus-2099-02-03")])
        result = level.level_all(host, RENDERED, recovered=(), command_path="restore")
        assert isinstance(result, report.Problem)
        self.assertIn("multiple live generations", result.problem)
        self.assertIn(report.command("index report"), result.fix)
        self.assertEqual(host.events, ["record read"])

    def test_all_reads_precede_all_demotions_and_then_both_alias_writes(self) -> None:
        live = generation("live")
        short = generation("built", label="corpus-2099-02-03")
        other_short = generation("built", label=short.corpus_lockfile, space="fictitious-other-space")
        rows = [live, short, other_short]
        host = FakeHost(rows)
        for row in (short, other_short):
            host.counts["qdrant"][row.gen_id] = 0
        result = self.checked(host)
        self.assertEqual(host.events, [
            "record read", "opensearch GET /_alias/caselaw", "qdrant GET /aliases",
            *(event for row in rows for event in (
                f"opensearch GET /{row.gen_id}/_count", f"qdrant POST /collections/{row.gen_id}/points/count",
            )),
            "demote " + short.gen_id, "demote " + other_short.gen_id,
            "opensearch POST /_aliases", "qdrant POST /collections/aliases",
        ])
        self.assertEqual(tuple(outcome.name for outcome in result.outcomes), tuple(row.gen_id for row in rows))
        self.assertEqual(result.next_builds, ((short.collection, short.corpus_lockfile),))

    def test_any_read_failure_refuses_before_writing_and_names_its_fix(self) -> None:
        first = generation("built")
        last = generation("live", label="corpus-2099-02-03")
        for event in (
            "record read", "opensearch GET /_alias/caselaw", "qdrant GET /aliases",
            f"opensearch GET /{last.gen_id}/_count", f"qdrant POST /collections/{last.gen_id}/points/count",
        ):
            with self.subTest(event=event):
                host = FakeHost([first, last])
                host.counts["qdrant"][first.gen_id] = 0
                host.failures[event] = (3, "", "fictitious private diagnostic")
                result = level.level_all(host, RENDERED, recovered=(), command_path="restore")
                assert isinstance(result, report.Problem)
                self.assertEqual(host.events[-1], event)
                self.assertEqual(host.rows, [first, last])
                self.assertFalse(any(item.startswith("demote") or "POST /_aliases" in item or "POST /collections/aliases" in item for item in host.events))
                fix = database_fix(RENDERED, "restore") if event == "record read" else stack.logs_fix(RENDERED, event.split()[0])
                self.assertEqual(result.fix, fix)
                if event == "record read":
                    self.assertNotIn("fictitious private diagnostic", repr(result))

    def test_malformed_record_and_unavailable_store_refuse_with_a_fix(self) -> None:
        row = generation("live")
        for event, answer in (
            ("record read", (0, "{}\n", "")),
            ("opensearch GET /_alias/caselaw", (127, "", "")),
        ):
            with self.subTest(event=event):
                host = FakeHost([row])
                host.failures[event] = answer
                result = level.level_all(host, RENDERED, recovered=(), command_path="restore")
                assert isinstance(result, report.Problem)
                self.assertTrue(result.fix)
                self.assertEqual(host.events[-1], event)
                self.assertEqual(host.rows, [row])
        host = FakeHost([row])
        host.errors["record read"] = FileNotFoundError()
        result = level.level_all(host, RENDERED, recovered=(), command_path="restore")
        assert isinstance(result, report.Problem)
        self.assertIn("Restore Docker Compose and psql", result.fix)
        self.assertEqual(host.events, ["record read"])

    def test_failed_demotion_keeps_prior_writes_and_never_points_an_alias(self) -> None:
        rows = [generation("built"), generation("built", label="corpus-2099-02-03"),
                generation("live", label="corpus-2099-03-03")]
        host = FakeHost(rows)
        for row in rows[:2]:
            host.counts["qdrant"][row.gen_id] = 0
        refused = "demote " + rows[1].gen_id
        host.failures[refused] = (3, "", "fictitious database diagnostic")
        result = level.level_all(host, RENDERED, recovered=(), command_path="restore")
        assert isinstance(result, report.Problem)
        self.assertEqual(result.fix, database_fix(RENDERED, "restore"))
        self.assertEqual(host.events[-1], refused)
        self.assertEqual([row.state for row in host.rows], ["building", "built", "live"])
        self.assertFalse(any("POST /_aliases" in event or "POST /collections/aliases" in event for event in host.events))
        host.failures.clear()
        result = self.checked(host)
        self.assertEqual([outcome.outcome for outcome in result.outcomes], ["left", "demoted", "aliased"])

    def test_failed_alias_write_leaves_demotions_and_converges_on_a_rerun(self) -> None:
        for event in ("opensearch POST /_aliases", "qdrant POST /collections/aliases"):
            with self.subTest(event=event):
                live = generation("live")
                short = generation("built", label="corpus-2099-02-03")
                host = FakeHost([live, short])
                host.counts["qdrant"][short.gen_id] = None
                host.failures[event] = (7, "", "fictitious store diagnostic")
                result = level.level_all(host, RENDERED, recovered=(), command_path="restore")
                assert isinstance(result, report.Problem)
                self.assertEqual(result.fix, stack.logs_fix(RENDERED, event.split()[0]))
                self.assertEqual(host.events[-1], event)
                self.assertEqual(host.rows[1].state, "building")
                self.assertEqual(host.rows[0], live)
                host.failures.clear()
                before = len(host.events)
                result = self.checked(host)
                self.assertEqual(result.outcomes[1].outcome, "left")
                self.assertEqual(host.aliases, {store: {"caselaw": (live.gen_id,)} for store in ("opensearch", "qdrant")})
                if event.startswith("qdrant"):
                    self.assertEqual(result.outcomes[0].lexical_alias, "unchanged")
                    self.assertNotIn("opensearch POST /_aliases", host.events[before:])

    def test_audit_contains_only_the_record_states_store_readings_and_alias_outcomes(self) -> None:
        row = generation("live")
        host = FakeHost([row], aliases={"opensearch": {"caselaw": (row.gen_id,)}})
        host.counts["qdrant"][row.gen_id] = None
        result = self.checked(host)
        expected = {row.gen_id: {
            "state": row.state, "outcome": "demoted",
            "lexical_count": host.counts["opensearch"][row.gen_id],
            "lexical_expected": row.counts["indexed"] + row.counts["present"],
            "dense_count": None, "dense_expected": row.counts["dense_total"],
            "lexical_alias": "stands", "dense_alias": None,
        }}
        self.assertEqual(level.level_audit(result), expected)
        self.assertEqual(json.loads(json.dumps(level.level_audit(result))), expected)
        for secret in host.files.values():
            self.assertNotIn(secret.strip(), json.dumps(level.level_audit(result)))

    def test_next_lines_deduplicate_builds_keep_order_and_use_both_command_forms(self) -> None:
        live = generation("live")
        built = generation("built", label="corpus-2099-02-03")
        duplicate = generation("built", label=built.corpus_lockfile, space="fictitious-other-space")
        host = FakeHost([built, live, duplicate])
        for row in host.rows:
            host.counts["qdrant"][row.gen_id] = None
        result = self.checked(host)
        self.assertEqual(result.next_builds, ((built.collection, built.corpus_lockfile), (live.collection, live.corpus_lockfile)))
        self.assertEqual(result.next_promotes, (live.gen_id,))
        self.addCleanup(report.set_installed_form, False)
        for installed in (False, True):
            report.set_installed_form(installed)
            prefix = "gideon" if installed else "sudo python3 -m gideon"
            self.assertEqual(level.next_lines(result), (
                f"{prefix} index build {built.collection} --lockfile {built.corpus_lockfile}",
                f"{prefix} index build {live.collection} --lockfile {live.corpus_lockfile}",
                f"{prefix} index promote {live.gen_id}",
            ))

    def test_patterns_reject_extra_clauses_invalid_counts_and_unknown_outcomes(self) -> None:
        outcome = self.checked(FakeHost([generation("built")])).outcomes[0]
        clause = level.level_clause(outcome)
        for invalid in (
            clause + "; unexpected", "extra " + clause, clause + " extra",
            clause.replace("opensearch ", "opensearch -"),
            clause.replace("built, whole", "failed, whole"),
            f"level {NAME}: unexplained outcome", "level: no generation recorded; extra",
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(any(pattern.fullmatch(invalid) for pattern in level.level_clause_patterns()))

    def test_store_fake_composes_with_recovery_and_keeps_credentials_on_stdin(self) -> None:
        host = FakeHost([generation("live")])
        key = host.files[os.fspath(secrets.secret_path(QDRANT_SECRET_NAME))].strip()
        reply = derivedstores.qdrant_request(
            host, RENDERED, key, "PUT", f"/collections/{NAME}/snapshots/recover?wait=true",
            body={"location": "file:///fictitious.snapshot"},
        )
        self.assertNotIsInstance(reply, report.Problem)
        self.checked(host)
        for argv, request in host.calls:
            assert request is not None
            if "gideon_worker" in argv:
                self.assertNotIn("SELECT", " ".join(argv))
                continue
            secret_name = QDRANT_SECRET_NAME if QDRANT_SERVICE_NAME in argv else OPENSEARCH_PASSWORD_SECRET_NAME
            credential = host.files[os.fspath(secrets.secret_path(secret_name))].strip()
            self.assertIn(credential, request)
            self.assertNotIn(credential, " ".join(argv))
            self.assertNotIn(NAME, " ".join(argv))
            if QDRANT_SERVICE_NAME in argv and "snapshots/recover" not in request:
                self.assertIsNone(recovery_store_result(argv, request, RENDERED))

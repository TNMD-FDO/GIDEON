"""Risk-state distribution parsing and stdin-fed materialization contracts."""

import copy
import json
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import test_index_record

from gideon.host import report, stack, staging, worker
from gideon.host.index import treatment
from gideon.host.render import worker as identity
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike

RENDERED = test_index_record.RENDERED
CORPUS = "/fictitious-corpus-rendered"
COMMAND_PATH = "index build caselaw --lockfile corpus-2099-01-03"
TREATMENT_SNAPSHOT = f"{identity.OVERRULED_SOURCE}-2099-01-01"


def treatment_for() -> treatment.Treatment:
    """Return a fictitious distribution under the release's pattern set."""

    return treatment.Treatment(
        "2099-01-03T12:00:00+00:00", identity.PATTERN_SET_ID,
        identity.SIGNAL_PATTERN, treatment.SIGNAL_LIST, TREATMENT_SNAPSHOT,
        7, {"negative": 2, "caution": 1, "unchecked": 4},
        {"pattern": 3, "list": 2}, {"overruled": 4, "disapproved": 1},
        3, {"non_holding": 2, "lineage_unverified": 1, "unresolved": 3},
    )


def read_for(distribution: treatment.Treatment | None = None) -> treatment.TreatmentRead:
    """Return distinct fictitious cluster rows and the distribution's corpus figures."""

    distribution = treatment_for() if distribution is None else distribution
    rows: list[treatment.TreatmentRow] = []
    for state in ("negative", "caution"):
        for _ in range(distribution.by_state[state]):
            rows.append(treatment.TreatmentRow(
                len(rows), state, ({"source": "pattern", "state": state},),
            ))
    return treatment.TreatmentRead(
        tuple(rows), distribution.clusters, distribution.by_source, distribution.by_signal,
        distribution.outside, distribution.by_no_state_reason,
    )


class MaterializeHost(test_index_record.FakeHost):
    """Answer each treatment script separately and inject a failure at either call."""

    def __init__(
        self, *, read_stdout: str | None = None, fail_script: str = treatment.READ_SQL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.read_stdout = json.dumps(read_for().to_payload()) if read_stdout is None else read_stdout
        self.fail_script = fail_script

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), input))
        assert input is not None
        failing = input.endswith(self.fail_script)
        if failing and self.error is not None:
            raise self.error
        stdout = self.read_stdout if input.endswith(treatment.READ_SQL) else self.stdout
        return subprocess.CompletedProcess(list(argv), self.code if failing else 0, stdout, self.stderr)


def older_pattern_set() -> str:
    """Derive the preceding set's id rather than restating a release value."""

    prefix, version = identity.PATTERN_SET_ID.rsplit("@", 1)
    assert int(version) > 1
    return f"{prefix}@{int(version) - 1}"


class Parser(unittest.TestCase):
    def setUp(self) -> None:
        self.row = treatment_for()
        self.payload: dict[str, Any] = self.row.to_json()

    def test_distribution_round_trips_through_json(self) -> None:
        self.assertEqual(treatment.Treatment.from_json(json.loads(json.dumps(self.payload))), self.row)
        self.assertEqual(set(self.payload), treatment.DISTRIBUTION_KEYS)
        empty = replace(self.row, treatment_snapshot=None, clusters=0,
                        by_state=dict.fromkeys(treatment.RISK_STATES, 0),
                        by_source={}, by_signal={}, outside=0, by_no_state_reason={})
        self.assertEqual(treatment.Treatment.from_json(empty.to_json()), empty)

    def test_nonobjects_missing_and_extra_fields_refuse(self) -> None:
        value: object
        for value in (None, [], "fictitious nonobject", 1):
            with self.subTest(value=value):
                self.assertIsNone(treatment.Treatment.from_json(value))
        for key in self.payload:
            with self.subTest(missing=key):
                self.assertIsNone(treatment.Treatment.from_json({
                    name: value for name, value in self.payload.items() if name != key
                }))
        self.assertIsNone(treatment.Treatment.from_json({**self.payload, "unexpected": 1}))

    def test_every_count_refuses_negative_boolean_fractional_and_text_values(self) -> None:
        for name in ("clusters", "outside", "by_state", "by_source", "by_signal", "by_no_state_reason"):
            keys = tuple(self.payload[name]) if isinstance(self.payload[name], dict) else (None,)
            for key in keys:
                for count in (-1, True, 0.5, "1", None):
                    with self.subTest(name=name, key=key, count=count):
                        value = copy.deepcopy(self.payload)
                        if key is None:
                            value[name] = count
                        else:
                            value[name][key] = count
                        self.assertIsNone(treatment.Treatment.from_json(value))

    def test_count_maps_refuse_nonobjects_and_unknown_keys(self) -> None:
        replacement: object
        for name in ("by_state", "by_source", "by_signal", "by_no_state_reason"):
            for replacement in ([], None, {"fictitious-unknown-key": 1}, {1: 1}):
                with self.subTest(name=name, replacement=replacement):
                    self.assertIsNone(treatment.Treatment.from_json({**self.payload, name: replacement}))

    def test_closed_vocabulary_counts_parse(self) -> None:
        for name, allowed in (
            ("by_source", treatment.BY_SOURCE_KEYS),
            ("by_signal", treatment.BY_SIGNAL_KEYS),
            ("by_no_state_reason", treatment.BY_NO_STATE_REASON_KEYS),
        ):
            for key in allowed:
                with self.subTest(name=name, key=key):
                    observed = treatment.Treatment.from_json({**self.payload, name: {key: 1}})
                    self.assertIsNotNone(observed)
                    assert observed is not None
                    self.assertEqual(getattr(observed, name), {key: 1})

    def test_states_must_cover_the_universe(self) -> None:
        self.assertIsNone(treatment.Treatment.from_json({**self.payload, "clusters": self.row.clusters + 1}))
        for state in treatment.RISK_STATES:
            with self.subTest(missing=state):
                states = {key: count for key, count in self.row.by_state.items() if key != state}
                value = {**self.payload, "clusters": sum(states.values()), "by_state": states}
                self.assertIsNone(treatment.Treatment.from_json(value))

    def test_materialization_time_requires_an_aware_iso_string(self) -> None:
        for value in ("2099-01-03T12:00:00", "2099-01-03", "fictitious invalid time", 1, None):
            with self.subTest(value=value):
                self.assertIsNone(treatment.Treatment.from_json({**self.payload, "materialized_at": value}))

    def test_switches_require_booleans(self) -> None:
        for name in ("signal_pattern", "signal_list"):
            for value in (0, 1, "on", None):
                with self.subTest(name=name, value=value):
                    self.assertIsNone(treatment.Treatment.from_json({**self.payload, name: value}))

    def test_treatment_snapshot_requires_its_source_and_a_real_date(self) -> None:
        value: object
        for value in ("fictions-2099-01-01", "treatment-2099-02-30", "treatment-2099-1-1",
                      "../treatment-2099-01-01", 1, []):
            with self.subTest(value=value):
                self.assertIsNone(treatment.Treatment.from_json({**self.payload, "treatment_snapshot": value}))

    def test_pattern_set_requires_a_positive_integer_and_accepts_older_sets(self) -> None:
        for value in ("treatment/patterns@0", "treatment/patterns@-1", "treatment/patterns@01",
                      "treatment/patterns@1.5", "treatment/patterns@", "other/patterns@1", 1, None):
            with self.subTest(value=value):
                self.assertIsNone(treatment.Treatment.from_json({**self.payload, "pattern_set": value}))
        older = replace(self.row, pattern_set=older_pattern_set())
        self.assertEqual(treatment.Treatment.from_json(older.to_json()), older)


class ReadParser(unittest.TestCase):
    def setUp(self) -> None:
        self.row = read_for()
        self.payload: dict[str, Any] = self.row.to_payload()

    def test_read_round_trips_rows_signals_and_figures(self) -> None:
        self.assertEqual(treatment.TreatmentRead.from_json(json.loads(json.dumps(self.payload))), self.row)
        self.assertEqual([row.cluster_id for row in self.row.rows], sorted(row.cluster_id for row in self.row.rows))
        payload = copy.deepcopy(self.payload)
        payload["rows"][0]["signals"].append({"source": "list", "row_id": 42})
        parsed = treatment.TreatmentRead.from_json(payload)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.to_payload(), payload)
        empty = treatment.TreatmentRead((), 0, {}, {}, 0, {})
        self.assertEqual(treatment.TreatmentRead.from_json(empty.to_payload()), empty)

    def test_nonobjects_missing_and_extra_fields_refuse(self) -> None:
        value: object
        for value in (None, [], "fictitious nonobject", 1):
            with self.subTest(value=value):
                self.assertIsNone(treatment.TreatmentRead.from_json(value))
        for key in self.payload:
            with self.subTest(missing=key):
                self.assertIsNone(treatment.TreatmentRead.from_json({
                    name: value for name, value in self.payload.items() if name != key
                }))
        self.assertIsNone(treatment.TreatmentRead.from_json({**self.payload, "unexpected": 1}))
        for key in self.payload["rows"][0]:
            payload = copy.deepcopy(self.payload)
            del payload["rows"][0][key]
            self.assertIsNone(treatment.TreatmentRead.from_json(payload))
        payload = copy.deepcopy(self.payload)
        payload["rows"][0]["unexpected"] = 1
        self.assertIsNone(treatment.TreatmentRead.from_json(payload))

    def test_invalid_cluster_rows_and_duplicate_ids_refuse(self) -> None:
        value: object
        for value in (None, {}, "fictitious rows", [None], [1], ["fictitious row"]):
            with self.subTest(rows=value):
                self.assertIsNone(treatment.TreatmentRead.from_json({**self.payload, "rows": value}))
        for key, values in (
            ("cluster_id", (-1, True, 0.5, "1", None, [])),
            ("state", ("unchecked", "fictitious unknown state", None, {}, 1)),
            ("signals", ([], {}, None, [1], [None], ["fictitious signal"])),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    payload = copy.deepcopy(self.payload)
                    payload["rows"][0][key] = value
                    self.assertIsNone(treatment.TreatmentRead.from_json(payload))
        payload = copy.deepcopy(self.payload)
        payload["rows"].append(copy.deepcopy(payload["rows"][0]))
        self.assertIsNone(treatment.TreatmentRead.from_json(payload))
        self.assertIsNone(treatment.TreatmentRead.from_json({**self.payload, "clusters": len(self.row.rows) - 1}))

    def test_corpus_figures_refuse_invalid_counts_and_unknown_vocabulary(self) -> None:
        value: object
        for name in ("clusters", "outside", "by_source", "by_signal", "by_no_state_reason"):
            keys = tuple(self.payload[name]) if isinstance(self.payload[name], dict) else (None,)
            for key in keys:
                for value in (-1, True, 0.5, "1", None):
                    with self.subTest(name=name, key=key, value=value):
                        payload = copy.deepcopy(self.payload)
                        if key is None:
                            payload[name] = value
                        else:
                            payload[name][key] = value
                        self.assertIsNone(treatment.TreatmentRead.from_json(payload))
        for name in ("by_source", "by_signal", "by_no_state_reason"):
            for value in ([], None, {"fictitious unknown key": 1}, {1: 1}):
                with self.subTest(name=name, value=value):
                    self.assertIsNone(treatment.TreatmentRead.from_json({**self.payload, name: value}))


class Materialize(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = test_index_record.inputs_for()
        self.row = treatment_for()

    def test_two_payloads_bind_scope_rows_and_switches_on_stdin_alone(self) -> None:
        self.inputs = replace(self.inputs, pipeline="1000.0.0")
        for signal_pattern in (True, False):
            for signal_list in (True, False):
                with self.subTest(signal_pattern=signal_pattern, signal_list=signal_list):
                    row = replace(self.row, signal_pattern=signal_pattern, signal_list=signal_list)
                    host = MaterializeHost(stdout=json.dumps(row.to_json()))
                    with patch.object(treatment, "SIGNAL_PATTERN", signal_pattern), patch.object(treatment, "SIGNAL_LIST", signal_list):
                        observed = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT)
                    self.assertEqual(observed, row)
                    self.assertEqual(len(host.calls), 2)
                    argv, sql = host.calls[0]
                    self.assertEqual(argv, worker.psql_argv(RENDERED))
                    self.assertIn("ON_ERROR_STOP=1", argv)
                    assert sql is not None
                    payload = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
                    expected = {
                        "doc_ids": None,
                        "source": self.inputs.snapshot[:-staging.DATE_SUFFIX_LENGTH],
                        "snapshot_date": self.inputs.snapshot[-10:], "courts": list(self.inputs.courts),
                        "pipeline": self.inputs.pipeline,
                        "pattern_set": identity.PATTERN_SET_ID,
                        "signal_pattern": signal_pattern, "signal_list": signal_list,
                        "treatment_source": TREATMENT_SNAPSHOT[:-staging.DATE_SUFFIX_LENGTH],
                        "treatment_snapshot_date": TREATMENT_SNAPSHOT[-10:],
                    }
                    self.assertEqual(payload, expected)
                    self.assertTrue(sql.endswith(treatment.READ_SQL))
                    write_argv, write_sql = host.calls[1]
                    self.assertEqual(write_argv, worker.psql_argv(RENDERED))
                    assert write_sql is not None
                    written = json.loads(write_sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
                    self.assertEqual(written, {
                        "gen_id": self.inputs.name, **read_for().to_payload(),
                        **{key: expected[key] for key in (
                            "pattern_set", "signal_pattern", "signal_list",
                            "treatment_source", "treatment_snapshot_date",
                        )},
                    })
                    self.assertTrue(write_sql.endswith(treatment.WRITE_SQL))
                    self.assertEqual(write_sql.count("\\set v_payload "), 1)
                    self.assertEqual(sql.count("\\set v_payload "), 1)
                    for value in (self.inputs.name, self.inputs.snapshot, identity.PATTERN_SET_ID,
                                  TREATMENT_SNAPSHOT, json.dumps(expected, separators=(",", ":"))):
                        self.assertNotIn(value, " ".join(argv))
                        self.assertNotIn(value, " ".join(write_argv))

    def test_absent_treatment_pin_binds_both_snapshot_fields_as_null(self) -> None:
        row = replace(self.row, treatment_snapshot=None)
        host = MaterializeHost(stdout=json.dumps(row.to_json()))
        self.assertEqual(treatment.materialize(host, RENDERED, self.inputs, None), row)
        sql = host.calls[0][1]
        assert sql is not None
        payload = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
        self.assertIsNone(payload["treatment_source"])
        self.assertIsNone(payload["treatment_snapshot_date"])
        self.assertEqual(payload["signal_list"], treatment.SIGNAL_LIST)

    def test_invalid_snapshot_grammars_refuse_before_running_with_the_fix(self) -> None:
        for snapshot in ("../fictions-2099-01-02", "fictions-2099-1-2", "fictions-2099-02-30"):
            with self.subTest(caselaw_snapshot=snapshot):
                host = test_index_record.FakeHost()
                result = treatment.materialize(host, RENDERED, replace(self.inputs, snapshot=snapshot),
                                               TREATMENT_SNAPSHOT, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("snapshot", result.problem)
                self.assertIn("YYYY-MM-DD", result.fix)
                self.assertIn(report.command(COMMAND_PATH), result.fix)
                self.assertEqual(host.calls, [])
        for snapshot in ("../treatment-2099-01-01", "treatment-2099-1-1",
                         "treatment-2099-02-30", "fictions-2099-01-01"):
            with self.subTest(treatment_snapshot=snapshot):
                host = test_index_record.FakeHost()
                result = treatment.materialize(host, RENDERED, self.inputs, snapshot, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("snapshot", result.problem)
                self.assertIn("YYYY-MM-DD", result.fix)
                self.assertIn(report.command(COMMAND_PATH), result.fix)
                self.assertEqual(host.calls, [])

    def test_document_bounds_and_directories_are_bound_on_stdin(self) -> None:
        for documents in (None, ("fictitious-doc-1", "fictitious-doc-2"), ()):
            with self.subTest(documents=documents):
                host = MaterializeHost(stdout=json.dumps(self.row.to_json()))
                result = treatment.materialize(
                    host, RENDERED, self.inputs, TREATMENT_SNAPSHOT,
                    corpus_dir=CORPUS, documents=documents,
                )
                self.assertEqual(result, self.row)
                self.assertEqual(len(host.calls), 2)
                for (argv, sql), directory in zip(host.calls, (CORPUS, RENDERED), strict=True):
                    self.assertEqual(argv, worker.psql_argv(directory))
                    assert sql is not None
                    payload = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
                    self.assertNotIn(json.dumps(payload, separators=(",", ":")), " ".join(argv))
                    if directory == CORPUS:
                        self.assertEqual(payload["doc_ids"], None if documents is None else list(documents))
                        self.assertNotIn("gen_id", payload)
                    else:
                        self.assertEqual(payload["gen_id"], self.inputs.name)
                        self.assertNotIn("doc_ids", payload)
                for doc_id in documents or ():
                    self.assertTrue(all(doc_id not in " ".join(argv) for argv, _ in host.calls))

    def test_install_fake_records_the_two_script_directories_and_payloads(self) -> None:
        import test_corpus_install

        host = test_corpus_install.FakeHost()
        observed = treatment.materialize(
            host, test_corpus_install.CI_ROOT, self.inputs, TREATMENT_SNAPSHOT,
            corpus_dir=test_corpus_install.RENDERED, documents=("fictitious-doc",),
        )
        self.assertIsInstance(observed, treatment.Treatment)
        assert isinstance(observed, treatment.Treatment)
        self.assertEqual(observed.by_state, host.treatment_distribution.by_state)
        self.assertEqual(host.treatment_reads[0]["doc_ids"], ["fictitious-doc"])
        self.assertEqual(host.treatment_payloads[0]["gen_id"], self.inputs.name)
        self.assertEqual(host.treatment_payloads[0]["rows"], read_for(host.treatment_distribution).to_payload()["rows"])
        self.assertEqual([directory for directory, _ in host.statement_directories],
                         [test_corpus_install.RENDERED, str(test_corpus_install.CI_ROOT)])
        for (_, sql), script in zip(host.statement_directories, (treatment.READ_SQL, treatment.WRITE_SQL), strict=True):
            self.assertTrue(sql.endswith(script))

    def test_empty_corpus_read_writes_a_valid_empty_distribution(self) -> None:
        empty = replace(self.row, clusters=0, by_state=dict.fromkeys(treatment.RISK_STATES, 0),
                        by_source={}, by_signal={}, outside=0, by_no_state_reason={})
        host = MaterializeHost(read_stdout=json.dumps(read_for(empty).to_payload()),
                               stdout=json.dumps(empty.to_json()))
        self.assertEqual(treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT,
                                              documents=()), empty)
        sql = host.calls[1][1]
        assert sql is not None
        written = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
        self.assertEqual(written["rows"], [])
        self.assertEqual(written["clusters"], 0)

    def test_failed_exits_override_valid_rows_and_use_each_directorys_fix(self) -> None:
        for script, directory, calls, message in (
            (treatment.READ_SQL, CORPUS, 1, "treatment read failed"),
            (treatment.WRITE_SQL, RENDERED, 2, "treatment materialization failed"),
        ):
            with self.subTest(script=script):
                host = MaterializeHost(code=3, stdout=json.dumps(self.row.to_json()),
                                       stderr="private fictitious diagnostic sentinel", fail_script=script)
                result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT,
                                               corpus_dir=CORPUS, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, f"{message} (exit 3)")
                self.assertIn(stack.logs_fix(directory, "postgres"), result.fix)
                self.assertIn(report.command(COMMAND_PATH), result.fix)
                self.assertNotIn(host.stderr, result.problem + result.fix)
                self.assertEqual(len(host.calls), calls)

    def test_unavailable_tools_refuse_with_installed_command_and_no_retry(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        for script, calls in ((treatment.READ_SQL, 1), (treatment.WRITE_SQL, 2)):
            for code, error in ((127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
                with self.subTest(script=script, code=code, error=error):
                    host = MaterializeHost(code=code, error=error, fail_script=script)
                    result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT,
                                                   corpus_dir=CORPUS, command_path=COMMAND_PATH)
                    self.assertIsInstance(result, Problem)
                    assert isinstance(result, Problem)
                    self.assertIn("command", result.problem)
                    self.assertIn("Restore Docker Compose and psql", result.fix)
                    self.assertIn(f"gideon {COMMAND_PATH}", result.fix)
                    self.assertNotIn("python3", result.fix)
                    self.assertEqual(len(host.calls), calls)

    def test_invalid_reads_refuse_before_writing_with_the_corpus_fix(self) -> None:
        for stdout in ("", "fictitious malformed JSON", "[]", json.dumps({}),
                       json.dumps({**read_for().to_payload(), "clusters": -1}),
                       json.dumps(read_for().to_payload()) * 2):
            with self.subTest(stdout=stdout):
                host = MaterializeHost(read_stdout=stdout)
                result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT,
                                               corpus_dir=CORPUS, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertEqual(result.problem, "treatment read returned an invalid row")
                self.assertIn(stack.logs_fix(CORPUS, "postgres"), result.fix)
                self.assertIn(report.command(COMMAND_PATH), result.fix)
                self.assertEqual(len(host.calls), 1)

    def test_invalid_write_rows_and_a_fresh_pattern_set_mismatch_refuse(self) -> None:
        for stdout in ("", "fictitious malformed JSON", "[]", json.dumps({}),
                       json.dumps({**self.row.to_json(), "clusters": -1}),
                       json.dumps(self.row.to_json()) * 2,
                       json.dumps(replace(self.row, pattern_set=older_pattern_set()).to_json())):
            with self.subTest(stdout=stdout):
                host = MaterializeHost(stdout=stdout)
                result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT,
                                               corpus_dir=CORPUS, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("materialization", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                self.assertIn(report.command(COMMAND_PATH), result.fix)
                self.assertEqual(len(host.calls), 2)
        mismatch = replace(self.row, pattern_set=older_pattern_set())
        result = treatment.materialize(MaterializeHost(stdout=json.dumps(mismatch.to_json())),
                                       RENDERED, self.inputs, TREATMENT_SNAPSHOT)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("different pattern set", result.problem)

    def test_read_script_only_reads_and_preserves_the_bound_and_signal_order(self) -> None:
        sql = treatment.READ_SQL
        for statement in ("INSERT", "UPDATE", "DELETE", "BEGIN;", "COMMIT;"):
            self.assertNotIn(statement, sql)
        self.assertIn("p.value->'doc_ids' = 'null'::jsonb", sql)
        self.assertIn("d.doc_id IN (", sql)
        self.assertIn("jsonb_array_elements_text(NULLIF(p.value->'doc_ids', 'null'::jsonb))", sql)
        selected = sql.split("selected_signals AS (", 1)[1].split("), pattern AS (", 1)[0]
        self.assertIn("JOIN public.citations AS c ON c.citation_id = s.citation_id", selected)
        self.assertIn("AND c.pipeline = p.value->>'pipeline'", selected)
        self.assertIn("jsonb_agg(k.element ORDER BY k.arm, k.element_id)", sql)
        self.assertIn("jsonb_agg(to_jsonb(g) ORDER BY g.cluster_id)", sql)
        self.assertNotIn("public.documents", treatment.WRITE_SQL)
        self.assertNotIn("public.citation_signals", treatment.WRITE_SQL)

    def test_script_replaces_one_generation_inside_a_transaction(self) -> None:
        sql = treatment.WRITE_SQL
        begin = sql.index("BEGIN;")
        delete = sql.index("DELETE FROM public.cluster_treatment")
        insert = sql.index("INSERT INTO public.cluster_treatment")
        commit = sql.index("COMMIT;")
        self.assertLess(begin, delete)
        self.assertLess(delete, insert)
        self.assertLess(insert, commit)
        self.assertIn("WHERE gen_id = (SELECT value->>'gen_id' FROM payload)", sql[delete:insert])
        self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_summary_renders_identifiers_switches_and_counts(self) -> None:
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                row = replace(self.row, signal_pattern=enabled, signal_list=enabled)
                state_counts = ", ".join(f"{key} {row.by_state[key]}" for key in treatment.RISK_STATES)
                source_counts = ", ".join(f"{key} {row.by_source.get(key, 0)}" for key in sorted(treatment.BY_SOURCE_KEYS))
                reason_counts = ", ".join(f"{key} {row.by_no_state_reason.get(key, 0)}" for key in identity.NO_STATE_REASONS)
                switch = "on" if enabled else "off"
                self.assertEqual(treatment.summary(row), (
                    f"materialized_at {row.materialized_at}; pattern_set {row.pattern_set}; "
                    f"signal_pattern {switch}, signal_list {switch}; clusters {row.clusters}; "
                    f"{state_counts}; elements {source_counts}; outside {row.outside}; no state {reason_counts}"
                ))
                self.assertNotIn("=", treatment.summary(row))
                self.assertNotIn(str(RENDERED), treatment.summary(row))

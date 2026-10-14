"""Risk-state distribution parsing and stdin-fed materialization contracts."""

import copy
import json
import subprocess
import unittest
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import test_index_record

from gideon.host import report, stack, staging, worker
from gideon.host.index import treatment
from gideon.host.render import worker as identity
from gideon.host.report import Problem

RENDERED = test_index_record.RENDERED
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


class Materialize(unittest.TestCase):
    def setUp(self) -> None:
        self.inputs = test_index_record.inputs_for()
        self.row = treatment_for()

    def test_one_payload_binds_scope_and_switches_on_stdin_alone(self) -> None:
        for signal_pattern in (True, False):
            for signal_list in (True, False):
                with self.subTest(signal_pattern=signal_pattern, signal_list=signal_list):
                    row = replace(self.row, signal_pattern=signal_pattern, signal_list=signal_list)
                    host = test_index_record.FakeHost(stdout=json.dumps(row.to_json()))
                    with patch.object(treatment, "SIGNAL_PATTERN", signal_pattern), patch.object(treatment, "SIGNAL_LIST", signal_list):
                        observed = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT)
                    self.assertEqual(observed, row)
                    self.assertEqual(len(host.calls), 1)
                    argv, sql = host.calls[0]
                    self.assertEqual(argv, worker.psql_argv(RENDERED))
                    self.assertIn("ON_ERROR_STOP=1", argv)
                    assert sql is not None
                    payload = json.loads(sql.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
                    expected = {
                        "gen_id": self.inputs.name,
                        "source": self.inputs.snapshot[:-staging.DATE_SUFFIX_LENGTH],
                        "snapshot_date": self.inputs.snapshot[-10:], "courts": list(self.inputs.courts),
                        "pattern_set": identity.PATTERN_SET_ID,
                        "signal_pattern": signal_pattern, "signal_list": signal_list,
                        "treatment_source": TREATMENT_SNAPSHOT[:-staging.DATE_SUFFIX_LENGTH],
                        "treatment_snapshot_date": TREATMENT_SNAPSHOT[-10:],
                    }
                    self.assertEqual(payload, expected)
                    self.assertTrue(sql.endswith(treatment.MATERIALIZE_SQL))
                    self.assertEqual(sql.count("\\set v_payload "), 1)
                    for value in (self.inputs.name, self.inputs.snapshot, identity.PATTERN_SET_ID,
                                  TREATMENT_SNAPSHOT, json.dumps(expected, separators=(",", ":"))):
                        self.assertNotIn(value, " ".join(argv))

    def test_absent_treatment_pin_binds_both_snapshot_fields_as_null(self) -> None:
        row = replace(self.row, treatment_snapshot=None)
        host = test_index_record.FakeHost(stdout=json.dumps(row.to_json()))
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

    def test_failed_exit_overrides_a_valid_row_and_hides_diagnostics(self) -> None:
        host = test_index_record.FakeHost(code=3, stdout=json.dumps(self.row.to_json()),
                                          stderr="private fictitious diagnostic sentinel")
        result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT, command_path=COMMAND_PATH)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
        self.assertIn(report.command(COMMAND_PATH), result.fix)
        self.assertNotIn(host.stderr, result.problem + result.fix)
        self.assertEqual(len(host.calls), 1)

    def test_unavailable_tools_refuse_with_installed_command_and_no_retry(self) -> None:
        report.set_installed_form(True)
        self.addCleanup(report.set_installed_form, False)
        for code, error in ((127, None), (0, FileNotFoundError()), (0, subprocess.SubprocessError())):
            with self.subTest(code=code, error=error):
                host = test_index_record.FakeHost(code=code, error=error)
                result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("command", result.problem)
                self.assertIn("Restore Docker Compose and psql", result.fix)
                self.assertIn(f"gideon {COMMAND_PATH}", result.fix)
                self.assertNotIn("python3", result.fix)
                self.assertEqual(len(host.calls), 1)

    def test_invalid_rows_and_a_fresh_pattern_set_mismatch_refuse(self) -> None:
        for stdout in ("", "fictitious malformed JSON", "[]", json.dumps({}),
                       json.dumps({**self.row.to_json(), "clusters": -1}),
                       json.dumps(self.row.to_json()) * 2,
                       json.dumps(replace(self.row, pattern_set=older_pattern_set()).to_json())):
            with self.subTest(stdout=stdout):
                host = test_index_record.FakeHost(stdout=stdout)
                result = treatment.materialize(host, RENDERED, self.inputs, TREATMENT_SNAPSHOT, command_path=COMMAND_PATH)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("materialization", result.problem)
                self.assertIn(stack.logs_fix(RENDERED, "postgres"), result.fix)
                self.assertIn(report.command(COMMAND_PATH), result.fix)
                self.assertEqual(len(host.calls), 1)
        mismatch = replace(self.row, pattern_set=older_pattern_set())
        result = treatment.materialize(test_index_record.FakeHost(stdout=json.dumps(mismatch.to_json())),
                                       RENDERED, self.inputs, TREATMENT_SNAPSHOT)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("different pattern set", result.problem)

    def test_script_replaces_one_generation_inside_a_transaction(self) -> None:
        sql = treatment.MATERIALIZE_SQL
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

"""Contracts for the local backup-set model."""

import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from gideon.host import backupset
from gideon.host.render.ci import CI_ROOT
from gideon.host.sysio import Command, PathLike


def aware(hour: int, *, minute: int = 0) -> datetime:
    return datetime(2026, 9, 2, hour, minute, tzinfo=UTC)


def entry(
    path: str,
    *,
    kind: backupset.EntryKind = "f",
    size: int = 10,
    mtime: float = 100.0,
    sha256: str | None = "a" * 64,
) -> backupset.Entry:
    return backupset.Entry(path, kind, size, 1000, 1000, 0o644, mtime, sha256)


def manifest(label: str, finished: datetime) -> backupset.Manifest:
    return backupset.Manifest(
        1,
        label,
        backupset.kind_of(label) or backupset.Kind.NIGHTLY,
        finished - timedelta(minutes=1),
        finished,
        "release",
        "/opt/gideon",
        "commit",
        "gideon.example",
        None,
        "backup-label",
        "full",
        finished,
        {"gideon": {"audit_log": 2}},
        {"data": (entry("one.txt"), entry("directory", kind="d", sha256=None))},
        "b" * 64,
        ("age1recipient",),
        backupset.LinkVerdict(2, 2),
        "f" * 64,
        backupset.AccountIds(999, 983),
    )


class FakeHost:
    """A dict-backed seam fake for set discovery."""

    def __init__(
        self,
        *,
        names: Sequence[str] = (),
        files: Mapping[str, str] | None = None,
        getent: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        self.names = list(names)
        self.files = dict(files or {})
        self.getent = getent

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout
        if tuple(argv) == ("getent", "passwd", backupset.SERVICE_ACCOUNT):
            return self.getent or subprocess.CompletedProcess(list(argv), 127, "", "not configured")
        return subprocess.CompletedProcess(list(argv), 127, "", "not configured")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        try:
            return self.files[os.fspath(path)]
        except KeyError:
            raise FileNotFoundError(os.fspath(path)) from None

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path).endswith("/sets"):
            return list(self.names)
        return []

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise FileNotFoundError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def geteuid(self) -> int:
        return 0


class LayoutAndLabels(unittest.TestCase):
    def test_layout_and_inventory_roots_are_ordered(self) -> None:
        self.assertEqual(backupset.set_dir("label"), f"{backupset.SETS_DIR}/label")
        self.assertEqual(
            backupset.partial_dir("label"), f"{backupset.SETS_DIR}/label.partial"
        )
        roots = backupset.inventory_roots("/work/GIDEON")
        self.assertEqual(
            [root.name for root in roots],
            [
                "etc-gideon",
                "checkout",
                "data-registry",
                "data-bulk-openwebui",
                "pgbackrest",
            ],
        )
        self.assertEqual(
            roots[0].exclusions,
            (
                "secrets/",
                "rendered/open-webui/env",
                "rendered/searxng/env",
                "no-gpu",
                "build-box",
                "backup_age_identity",
            ),
        )
        self.assertEqual(roots[1].source, "/work/GIDEON")
        self.assertEqual(roots[1].exclusions, backupset.CHECKOUT_EXCLUSIONS)
        self.assertIn(".venv/", roots[1].exclusions)
        self.assertEqual(roots[-1].source, backupset.REPOSITORY_PATH)
        self.assertEqual(
            [(root.snapshotted, root.restore_in_place) for root in roots],
            [(True, True), (True, False), (True, True), (True, True), (False, False)],
        )

    def test_ci_root_is_outside_every_inventory_root(self) -> None:
        ci_root = CI_ROOT
        for root in backupset.inventory_roots("/work/GIDEON"):
            self.assertFalse(ci_root == root.source or ci_root.startswith(f"{root.source}/"))
            self.assertFalse(root.source == ci_root or root.source.startswith(f"{ci_root}/"))

    def test_labels_grammar_and_kind(self) -> None:
        local = datetime(2026, 9, 2, 7, 8, 9, tzinfo=ZoneInfo("America/Chicago"))
        nightly = backupset.nightly_label(local)
        pre_restore = backupset.pre_restore_label(local)
        self.assertEqual(nightly, "20260902T120809Z")
        self.assertEqual(pre_restore, "pre-restore-20260902T120809Z")
        for label, kind in (
            (nightly, backupset.Kind.NIGHTLY),
            (pre_restore, backupset.Kind.PRE_RESTORE),
            ("pre-release_1.2", backupset.Kind.LABELLED),
        ):
            self.assertIsNone(backupset.validate_label(label))
            self.assertIs(backupset.kind_of(label), kind)
            self.assertIsNotNone(backupset.LABEL.fullmatch(label))
        for label in ("", "2026-09-02T12:08:09Z", "pre-", "post-release"):
            problem = backupset.validate_label(label)
            self.assertIsInstance(problem, backupset.Problem)
            assert problem is not None
            self.assertIn("pre-[A-Za-z0-9._-]+", problem.fix)
            self.assertIsNone(backupset.kind_of(label))

    def test_naive_label_time_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            backupset.nightly_label(datetime(2026, 9, 2, 12, 0))  # noqa: DTZ001


class InventoryParsing(unittest.TestCase):
    def test_find_listing_records_the_root_as_dot_and_keeps_spaces(self) -> None:
        listing = (
            "d\t4096\t1000\t1000\t755\t1700000000.000000000\t\0"
            "f\t17\t1001\t1002\t640\t1700000001.125\tread me.txt\0"
            "l\t8\t1001\t1002\t777\t1700000002\tlink name\0"
        )
        parsed = backupset.parse_find_listing(listing)
        self.assertEqual(
            parsed,
            (
                backupset.Entry(
                    backupset.ROOT_ENTRY, "d", 4096, 1000, 1000, 0o755, 1700000000.0, None
                ),
                backupset.Entry(
                    "read me.txt", "f", 17, 1001, 1002, 0o640, 1700000001.125, None
                ),
                backupset.Entry(
                    "link name", "l", 8, 1001, 1002, 0o777, 1700000002.0, None
                ),
            ),
        )
        # The format is find's escape form: no real tab or NUL may sit in argv.
        self.assertEqual(backupset.FIND_FORMAT, r"%y\t%s\t%U\t%G\t%m\t%T@\t%P\0")
        self.assertNotIn("\0", backupset.FIND_FORMAT)
        self.assertNotIn("\t", backupset.FIND_FORMAT)

    def test_sha256sum_parses_spaces_and_gnu_escaped_names(self) -> None:
        digest = "c" * 64
        text = (
            f"{digest}  read me.txt\n"
            rf"\{digest}  odd\nname\\with-slash"
            "\n"
        )
        parsed = backupset.parse_sha256sum(text)
        self.assertEqual(parsed["read me.txt"], digest)
        self.assertEqual(parsed["odd\nname\\with-slash"], digest)

    def test_merge_hashes_attaches_only_file_hashes_and_reports_missing(self) -> None:
        entries = (entry("file"), entry("directory", kind="d", sha256=None))
        merged = backupset.merge_hashes(entries, {"file": "d" * 64})
        self.assertIsInstance(merged, tuple)
        assert isinstance(merged, tuple)
        self.assertEqual(merged[0].sha256, "d" * 64)
        self.assertIsNone(merged[1].sha256)
        missing = backupset.merge_hashes(entries, {})
        self.assertIsInstance(missing, backupset.Problem)
        assert isinstance(missing, backupset.Problem)
        self.assertIn("file", missing.problem)
        self.assertIn("backup run", missing.fix)


class ManifestContracts(unittest.TestCase):
    def test_manifest_round_trip_is_deterministic(self) -> None:
        value = manifest("20260902T120809Z", aware(12, minute=8))
        text = value.to_json()
        self.assertEqual(text, value.to_json())
        parsed = backupset.parse_manifest(text)
        self.assertEqual(parsed, value)
        self.assertEqual(
            json.loads(text)["gideon_ids"],
            {"gid": 983, "uid": 999},
        )
        self.assertEqual(json.loads(text)["version"], 1)
        self.assertIn('"archive_through"', text)

    def test_manifest_without_gideon_ids_is_an_earlier_shape(self) -> None:
        value = manifest("20260902T120809Z", aware(12))
        document = json.loads(value.to_json())
        del document["gideon_ids"]
        parsed = backupset.parse_manifest(json.dumps(document))
        self.assertIsInstance(parsed, backupset.Manifest)
        assert isinstance(parsed, backupset.Manifest)
        self.assertIsNone(parsed.gideon_ids)

        without_record = replace(value, gideon_ids=None)
        serialized = json.loads(without_record.to_json())
        self.assertNotIn("gideon_ids", serialized)

    def test_manifest_gideon_ids_malformed_shapes_name_the_component(self) -> None:
        value = manifest("20260902T120809Z", aware(12))
        cases = (
            ("not-an-object", "gideon_ids"),
            ({"uid": -1, "gid": 983}, "gideon_ids.uid"),
            ({"uid": "999", "gid": 983}, "gideon_ids.uid"),
            ({"gid": 983}, "gideon_ids.uid"),
            ({"uid": 999}, "gideon_ids.gid"),
            ({"uid": 999, "gid": "983"}, "gideon_ids.gid"),
        )
        for malformed, field in cases:
            with self.subTest(field=field, malformed=malformed):
                document = json.loads(value.to_json())
                document["gideon_ids"] = malformed
                problem = backupset.parse_manifest(json.dumps(document))
                self.assertIsInstance(problem, backupset.Problem)
                assert isinstance(problem, backupset.Problem)
                self.assertIn(field, problem.problem)
                self.assertIn("backup run", problem.fix)

    def test_manifest_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            backupset.Manifest(
                1,
                "20260902T120809Z",
                backupset.Kind.NIGHTLY,
                datetime(2026, 9, 2, 12, 0),  # noqa: DTZ001
                aware(12),
                "release",
                "/checkout",
                "commit",
                "host",
                None,
                "backup",
                "full",
                aware(12),
                {},
                {},
                "a" * 64,
                ("age1recipient",),
                backupset.LinkVerdict(0, 0),
                "f" * 64,
            )

    def test_manifest_recipient_shapes_are_compatible_and_consistent(self) -> None:
        value = manifest("20260902T120809Z", aware(12, minute=8))
        document = json.loads(value.to_json())
        del document["recipients"]
        fallback = backupset.parse_manifest(json.dumps(document))
        self.assertIsInstance(fallback, backupset.Manifest)
        assert isinstance(fallback, backupset.Manifest)
        self.assertEqual(fallback.recipients, ("age1recipient",))

        two = replace(value, recipients=("age1recipient", "age1box"))
        text = two.to_json()
        self.assertEqual(backupset.parse_manifest(text), two)
        serialized = json.loads(text)
        self.assertEqual(serialized["recipient"], "age1recipient")
        self.assertEqual(serialized["recipients"], ["age1recipient", "age1box"])

        for recipients in ([], ["age1recipient", 3], ["age1other", "age1box"]):
            with self.subTest(recipients=recipients):
                broken = json.loads(value.to_json())
                broken["recipients"] = recipients
                problem = backupset.parse_manifest(json.dumps(broken))
                self.assertIsInstance(problem, backupset.Problem)
                assert isinstance(problem, backupset.Problem)
                self.assertIn("recipients", problem.problem)

    def test_every_manifest_field_has_a_named_parse_problem(self) -> None:
        value = manifest("20260902T120809Z", aware(12))
        document = json.loads(value.to_json())
        fields = tuple(document)
        for field in fields:
            with self.subTest(field=field):
                broken = dict(document)
                del broken[field]
                problem = backupset.parse_manifest(json.dumps(broken))
                if field == "recipients":
                    self.assertIsInstance(problem, backupset.Manifest)
                    assert isinstance(problem, backupset.Manifest)
                    self.assertEqual(problem.recipients, (document["recipient"],))
                    continue
                if field == "gideon_ids":
                    self.assertIsInstance(problem, backupset.Manifest)
                    assert isinstance(problem, backupset.Manifest)
                    self.assertIsNone(problem.gideon_ids)
                    continue
                self.assertIsInstance(problem, backupset.Problem)
                assert isinstance(problem, backupset.Problem)
                self.assertIn(field, problem.problem)
                self.assertIn("backup run", problem.fix)
        document["recipients"] = "not-a-list"
        problem = backupset.parse_manifest(json.dumps(document))
        self.assertIsInstance(problem, backupset.Problem)
        assert isinstance(problem, backupset.Problem)
        self.assertIn("recipients", problem.problem)
        document["version"] = 2
        problem = backupset.parse_manifest(json.dumps(document))
        self.assertIsInstance(problem, backupset.Problem)
        assert isinstance(problem, backupset.Problem)
        self.assertIn("version", problem.problem)
        document["version"] = 1
        document["inventory"] = {"data": [{"path": "one"}]}
        problem = backupset.parse_manifest(json.dumps(document))
        self.assertIsInstance(problem, backupset.Problem)
        assert isinstance(problem, backupset.Problem)
        self.assertIn("inventory.data[0]", problem.problem)
        malformed = backupset.parse_manifest("not json")
        self.assertIsInstance(malformed, backupset.Problem)
        document["inventory"] = {"data": [dict(json.loads(value.to_json())["inventory"]["data"][0], sha256="zz")]}
        bad_hash = backupset.parse_manifest(json.dumps(document))
        self.assertIsInstance(bad_hash, backupset.Problem)
        assert isinstance(bad_hash, backupset.Problem)
        self.assertIn("sha256", bad_hash.problem)

    def test_push_record_round_trip_and_parse_problem(self) -> None:
        value = backupset.PushRecord(
            "20260902T120809Z", aware(12, minute=10), "20260902T120809Z", aware(12)
        )
        self.assertEqual(backupset.parse_push_record(value.to_json()), value)
        document = json.loads(value.to_json())
        del document["archive_through"]
        problem = backupset.parse_push_record(json.dumps(document))
        self.assertIsInstance(problem, backupset.Problem)
        assert isinstance(problem, backupset.Problem)
        self.assertIn("archive_through", problem.problem)
        self.assertIn("backup push", problem.fix)
        with self.assertRaises(ValueError):
            backupset.PushRecord(
                "20260902T120809Z", datetime(2026, 9, 2, 12, 0), "set", aware(12)  # noqa: DTZ001
            )


class DiscoveryAndSelection(unittest.TestCase):
    def test_list_sets_marks_complete_partial_and_unparseable(self) -> None:
        newest = "20260902T120000Z"
        oldest = "20260901T120000Z"
        staging = "/tmp/backup-staging"
        files = {
            f"{staging}/sets/{newest}/manifest.json": manifest(
                newest, aware(12)
            ).to_json(),
            f"{staging}/sets/{oldest}/manifest.json": manifest(
                oldest, aware(10)
            ).to_json(),
            f"{staging}/sets/broken/manifest.json": "not json",
        }
        host = FakeHost(
            names=["broken", "20260901T120000Z.partial", oldest, newest], files=files
        )
        refs = backupset.list_sets(host, staging)
        self.assertEqual([ref.label for ref in refs], [newest, oldest, "20260901T120000Z.partial", "broken"])
        self.assertTrue(refs[0].complete)
        self.assertIsNotNone(refs[0].manifest)
        self.assertFalse(refs[-1].complete)

    def test_missing_sets_directory_is_empty(self) -> None:
        host = FakeHost()
        self.assertEqual(backupset.list_sets(host, "/tmp/no-such-staging"), ())

    def test_select_set_truth_table(self) -> None:
        oldest = backupset.SetRef("old", "/old", aware(10), True)
        newest = backupset.SetRef("new", "/new", aware(12), True)
        partial = backupset.SetRef("partial.partial", "/partial", None, False)
        sets = (partial, oldest, newest)
        self.assertIs(backupset.select_set(sets), newest)
        selected = backupset.select_set(sets, aware(11))
        self.assertIs(selected, oldest)
        selected = backupset.select_set(sets, aware(14))
        self.assertIs(selected, newest)
        before = backupset.select_set(sets, aware(9))
        self.assertIsInstance(before, backupset.Problem)
        assert isinstance(before, backupset.Problem)
        self.assertIn("oldest complete set", before.problem)
        self.assertEqual(before.fix, "Choose a later --at, or omit it for the newest set.")
        empty = backupset.select_set((partial,))
        self.assertIsInstance(empty, backupset.Problem)
        assert isinstance(empty, backupset.Problem)
        self.assertEqual(empty.fix, "Run sudo python3 -m gideon backup run, then retry.")

    def test_select_set_by_label_names_the_complete_labels_on_a_miss(self) -> None:
        oldest = backupset.SetRef("pre-v1.0.0", "/old", aware(10), True)
        newest = backupset.SetRef("20260101T000000Z", "/new", aware(12), True)
        partial = backupset.SetRef("pre-v1.0.1.partial", "/partial", None, False)
        sets = (partial, newest, oldest)
        self.assertIs(backupset.select_set_by_label(sets, "pre-v1.0.0"), oldest)
        for label in ("pre-v1.0.1.partial", "pre-v1.0.1", ""):
            with self.subTest(label=label):
                missed = backupset.select_set_by_label(sets, label)
                self.assertIsInstance(missed, backupset.Problem)
                assert isinstance(missed, backupset.Problem)
                self.assertIn("the complete sets are: 20260101T000000Z, pre-v1.0.0.", missed.problem)
                self.assertIn("Choose one of them", missed.fix)
        none = backupset.select_set_by_label((partial,), "pre-v1.0.0")
        assert isinstance(none, backupset.Problem)
        self.assertIn("the complete sets are: none.", none.problem)


class TimeAndRetention(unittest.TestCase):
    def test_parse_at_forms_and_naive_office_timezone(self) -> None:
        naive = backupset.parse_at("2026-09-02 12:34", "America/Chicago")
        self.assertIsInstance(naive, datetime)
        assert isinstance(naive, datetime)
        self.assertEqual(naive.utcoffset(), timedelta(hours=-5))
        with_seconds = backupset.parse_at("2026-09-02T12:34:56Z", "UTC")
        self.assertEqual(with_seconds, datetime(2026, 9, 2, 12, 34, 56, tzinfo=UTC))
        offset = backupset.parse_at("2026-09-02T12:34+02:00", "UTC")
        self.assertEqual(
            offset, datetime(2026, 9, 2, 12, 34, tzinfo=timezone(timedelta(hours=2)))
        )
        fractional = backupset.parse_at("2026-09-02T12:34:56.123456-07:00", "UTC")
        self.assertIsInstance(fractional, datetime)
        invalid = backupset.parse_at("2026/09/02 12:34", "UTC")
        self.assertIsInstance(invalid, backupset.Problem)
        assert isinstance(invalid, backupset.Problem)
        self.assertIn("ISO 8601", invalid.problem)
        self.assertIn("--at", invalid.fix)
        bad_zone = backupset.parse_at("2026-09-02 12:34", "Not/AZone")
        self.assertIsInstance(bad_zone, backupset.Problem)
        assert isinstance(bad_zone, backupset.Problem)
        self.assertIn("office.timezone", bad_zone.fix)

    def test_pgbackrest_target_requires_awareness_and_keeps_offset(self) -> None:
        value = backupset.parse_at("2026-09-02T12:34:56-05:00", "UTC")
        assert isinstance(value, datetime)
        self.assertEqual(backupset.pgbackrest_target(value), "2026-09-02 12:34:56-05:00")
        # A set's archive boundary is sub-second; the target keeps it (pgBackRest
        # accepts up to six fractional digits), else the restore stops short of the set.
        self.assertEqual(
            backupset.pgbackrest_target(value.replace(microsecond=346453)),
            "2026-09-02 12:34:56.346453-05:00",
        )
        with self.assertRaises(ValueError):
            backupset.pgbackrest_target(datetime(2026, 9, 2, 12, 0))  # noqa: DTZ001

    def test_prune_uses_finished_and_partial_mtime(self) -> None:
        now = aware(12)
        old = backupset.SetRef("old", "/sets/old", now - timedelta(days=8), True)
        boundary = backupset.SetRef(
            "boundary", "/sets/boundary", now - timedelta(days=7), True
        )
        recent = backupset.SetRef("recent", "/sets/recent", now, True)
        candidates = backupset.prune_candidates(
            (recent, boundary, old),
            {
                "fresh.partial": now.timestamp() - 3600,
                "old.partial": now.timestamp() - 2 * 86400,
            },
            now,
            7,
        )
        self.assertEqual(candidates, ("/sets/old", backupset.partial_dir("old")))
        elsewhere = backupset.prune_candidates((), {"x.partial": 0.0}, now, 7, staging="/tmp/staging")
        self.assertEqual(elsewhere, ("/tmp/staging/sets/x.partial",))


class CarryForwardAndSampling(unittest.TestCase):
    def test_carry_forward_requires_same_metadata_and_excludes_info_files(self) -> None:
        previous = entry("blob", size=20, mtime=3.0, sha256="e" * 64)
        same = entry("blob", size=20, mtime=3.0)
        self.assertEqual(
            backupset.carry_forward({"blob": previous}, same).sha256, "e" * 64
        )
        changed = entry("blob", size=21, sha256=None)
        self.assertIsNone(backupset.carry_forward({"blob": previous}, changed).sha256)
        self.assertIsNone(
            backupset.carry_forward(
                {"backup.info": previous},
                entry("backup.info", size=20, mtime=3.0, sha256=None),
            ).sha256
        )
        self.assertIsNone(
            backupset.carry_forward(
                {"archive.info.copy": previous},
                entry("archive.info.copy", size=20, mtime=3.0, sha256=None),
            ).sha256
        )
        directory = entry("blob", kind="d", sha256=None, size=20, mtime=3.0)
        self.assertIs(
            backupset.carry_forward({"blob": previous}, directory), directory
        )

    def test_sample_paths_is_sorted_every_kth_and_has_a_floor(self) -> None:
        paths = ("z", "a", "f", "k", "p", "u")
        self.assertEqual(backupset.sample_paths(paths, percent=20), ("a", "z"))
        self.assertEqual(
            backupset.sample_paths(paths, percent=1, floor=3), ("a", "f", "k")
        )
        self.assertEqual(
            backupset.sample_paths(("c", "a"), percent=1, floor=3), ("a", "c")
        )
        with self.assertRaises(ValueError):
            backupset.sample_paths(paths, percent=0)


class OwnershipContracts(unittest.TestCase):
    def test_owner_map_maps_each_component_and_identifies_recorded_owners(self) -> None:
        record = backupset.AccountIds(999, 983)
        host = backupset.AccountIds(998, 997)
        owner_map = backupset.OwnerMap(record, host)
        cases = (
            ((999, 983), (998, 997), True),
            ((999, 0), (998, 0), True),
            ((0, 983), (0, 997), True),
            ((123, 456), (123, 456), False),
        )
        for owner, expected, matches in cases:
            with self.subTest(owner=owner):
                self.assertEqual(owner_map.map(*owner), expected)
                self.assertEqual(owner_map.matches(*owner), matches)
        self.assertFalse(owner_map.is_identity)

        equal_ids = backupset.OwnerMap(record, record)
        self.assertEqual(equal_ids.map(record.uid, record.gid), (record.uid, record.gid))
        self.assertTrue(equal_ids.matches(record.uid, record.gid))

        identity = backupset.OwnerMap(None, host)
        self.assertTrue(identity.is_identity)
        self.assertEqual(identity.map(999, 983), (999, 983))
        self.assertFalse(identity.matches(999, 983))


class AccountResolutionContracts(unittest.TestCase):
    def test_gideon_account_ids_reads_a_passwd_record(self) -> None:
        command = ("getent", "passwd", backupset.SERVICE_ACCOUNT)
        host = FakeHost(
            getent=subprocess.CompletedProcess(
                list(command),
                0,
                f"{backupset.SERVICE_ACCOUNT}:x:999:983::/home/"
                f"{backupset.SERVICE_ACCOUNT}:/bin/bash\n",
                "",
            )
        )
        self.assertEqual(
            backupset.gideon_account_ids(host), backupset.AccountIds(999, 983)
        )

    def test_gideon_account_ids_rejects_a_failed_command_and_malformed_line(self) -> None:
        command = ("getent", "passwd", backupset.SERVICE_ACCOUNT)
        cases = (
            subprocess.CompletedProcess(list(command), 2, "", "not found\n"),
            subprocess.CompletedProcess(
                list(command),
                0,
                f"{backupset.SERVICE_ACCOUNT}:x:not-a-uid:983::/nonexistent:/bin/false\n",
                "",
            ),
            subprocess.CompletedProcess(
                list(command),
                0,
                f"{backupset.SERVICE_ACCOUNT}:x:999:983\n",
                "",
            ),
            subprocess.CompletedProcess(
                list(command),
                0,
                "other:x:999:983::/nonexistent:/bin/false\n",
                "",
            ),
        )
        for response in cases:
            with self.subTest(response=response):
                self.assertIsNone(backupset.gideon_account_ids(FakeHost(getent=response)))

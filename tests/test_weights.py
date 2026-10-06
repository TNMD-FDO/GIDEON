"""Weights layout, process identity, and pull-record contracts."""

import os
import stat
import subprocess
import unittest
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock
from urllib.parse import quote

import yaml  # type: ignore[import-untyped]

from gideon.host import report
from gideon.host.egress import EgressAllowlist, load_egress_allowlist
from gideon.host.models import (
    CandidatePin,
    HardwareProfile,
    PinnedFile,
    load_models_lock,
    load_models_lock_text,
)
from gideon.host.render.grafana import PUBLIC_REPOSITORY_URL
from gideon.host.report import Problem
from gideon.host.site import SiteConfig
from gideon.host.steps import Wgetrc, temporary_wgetrc, wget_argv_for_site
from gideon.host.sysio import Command, PathLike
from gideon.host.weights import (
    BLOB_MODE,
    DIRECTORY_MODE,
    HUB_DIR_NAME,
    MODELS_ROOT,
    PULL_RECORD_PATH,
    CandidateEntry,
    FileState,
    ProcessIdentity,
    PullEntry,
    PullOutcome,
    PullRecord,
    SnapshotRef,
    _retry_fix,
    bare_digest,
    blob_link_target,
    blob_path,
    claim,
    classify_model,
    clear_retiring,
    converge_model,
    dump_pull_record,
    kept_lines,
    load_pull_record,
    model_row,
    new_pull_entry,
    parse_pull_record,
    process_identity,
    process_is_alive,
    prune,
    pull_profile,
    reconcile_candidates,
    refs_main_content,
    refs_main_path,
    release,
    repository_dir,
    repository_folder,
    resolve_url,
    rotate,
    run_models_pull,
    save_pull_record,
    snapshot_path,
    snapshot_refs,
)

ROOT = Path(__file__).resolve().parent.parent
MODELS = load_models_lock(ROOT / "models.lock").lock
assert MODELS is not None, "the committed models.lock must load"
PROFILE = MODELS.profile(MODELS.reference)
assert PROFILE is not None
MODEL = PROFILE.models[0]
PINNED_FILE = MODEL.files[0]


class FakeHost:
    """A dict-backed Host owned by this test module."""

    def __init__(
        self,
        files: Mapping[str, str] | None = None,
        *,
        sizes: Mapping[str, int] | None = None,
        links: Mapping[str, str] | None = None,
        digests: Mapping[str, str] | None = None,
        available: int = 1_000_000_000_000,
        wget: Callable[[list[str], "FakeHost"], subprocess.CompletedProcess[str]]
        | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.sizes = dict(sizes or {})
        self.links = dict(links or {})
        self.digests = dict(digests or {})
        self.available = available
        self.wget = wget
        self.writes: list[tuple[str, str, int]] = []
        self.reads: list[str] = []
        self.runs: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.mkdir_calls: list[tuple[str, int, bool, bool]] = []
        self.unlink_calls: list[str] = []
        self.chmod_calls: list[tuple[str, int]] = []
        self.rm_calls: list[str] = []
        self.directories: set[str] = set()
        self.verify_returncode = 0
        self.euid = 0

    def _directories(self) -> set[str]:
        directories = set(self.directories)
        for path in (*self.files, *self.sizes, *self.links):
            current = Path(path).parent
            while str(current) != "/":
                directories.add(str(current))
                current = current.parent
        return directories

    def _size(self, key: str) -> int:
        return self.sizes.get(key, len(self.files.get(key, "").encode()))

    def _move(self, source: str, target: str) -> None:
        if source in self.files:
            self.files[target] = self.files.pop(source)
        if source in self.sizes:
            self.sizes[target] = self.sizes.pop(source)
        if source in self.digests:
            self.digests[target] = self.digests.pop(source)

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
        del check, cwd, env, timeout, passthrough
        command = tuple(argv)
        self.runs.append(command)
        self.inputs.append(input)
        actual = list(command)
        if actual and actual[0] == "env":
            while len(actual) > 1 and "=" in actual[1]:
                actual.pop(1)
            actual.pop(0)
        if actual[:1] == ["readlink"] and len(actual) == 2:
            target = self.links.get(actual[1])
            return subprocess.CompletedProcess(actual, 0 if target is not None else 1, (target or "") + ("\n" if target else ""), "")
        if actual[:3] == ["sha256sum", "-c", "-"]:
            lines = [] if input is None else input.splitlines()
            output: list[str] = []
            for line in lines:
                expected, path = line.split("  ", 1)
                output.append(f"{path}: {'OK' if self.digests.get(path) == expected else 'FAILED'}")
            return subprocess.CompletedProcess(actual, getattr(self, "verify_returncode", 0), "\n".join(output) + ("\n" if output else ""), "")
        if actual[:1] == ["sha256sum"] and len(actual) == 2:
            path = actual[1]
            digest = self.digests.get(path)
            if digest is None:
                return subprocess.CompletedProcess(actual, 1, "", "")
            return subprocess.CompletedProcess(actual, 0, f"{digest}  {path}\n", "")
        if actual[:1] == ["df"]:
            return subprocess.CompletedProcess(actual, 0, f"Avail\n{self.available}\n", "")
        if actual[:2] == ["rm", "-rf"]:
            target_path = Path(actual[2])
            self.rm_calls.append(str(target_path))
            for store in (self.files, self.sizes, self.links, self.digests):
                for key in tuple(store):
                    stored_path = Path(key)
                    if stored_path == target_path or target_path in stored_path.parents:
                        store.pop(key, None)
            self.directories = {
                path for path in self.directories
                if Path(path) != target_path and target_path not in Path(path).parents
            }
            return subprocess.CompletedProcess(actual, 0, "", "")
        if actual[:2] == ["mv", "-f"]:
            self._move(actual[2], actual[3])
            return subprocess.CompletedProcess(actual, 0, "", "")
        if actual[:2] == ["ln", "-sfn"] and len(actual) == 4:
            self.links[actual[3]] = actual[2]
            return subprocess.CompletedProcess(actual, 0, "", "")
        if actual[:1] == ["wget"] and self.wget is not None:
            return self.wget(actual, self)
        return subprocess.CompletedProcess(actual, 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding
        key = os.fspath(path)
        self.writes.append((key, text, mode))
        self.files[key] = text
        self.sizes[key] = len(text.encode())

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.sizes or key in self.links or key in self._directories()

    def listdir(self, path: PathLike) -> list[str]:
        root = Path(path)
        if str(root) not in self._directories() and not any(
            Path(name).parent == root for name in (*self.files, *self.sizes, *self.links)
        ):
            raise FileNotFoundError(str(root))
        names = {
            Path(name).name
            for name in (*self.files, *self.sizes, *self.links, *self._directories())
            if Path(name).parent == root
        }
        return sorted(names)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        key = os.fspath(path)
        self.unlink_calls.append(key)
        if key not in self.files and key not in self.sizes and key not in self.links and not missing_ok:
            raise FileNotFoundError(key)
        self.files.pop(key, None)
        self.sizes.pop(key, None)
        self.links.pop(key, None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key in self.directories or key in self._directories():
            return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        if key in self.links:
            target = str((Path(key).parent / self.links[key]).resolve())
            return self.stat(target)
        if key in self.files or key in self.sizes:
            values = [stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, self._size(key), 0, 0, 0]
            return os.stat_result(values)
        raise FileNotFoundError(key)

    def chmod(self, path: PathLike, mode: int) -> None:
        self.chmod_calls.append((os.fspath(path), mode))

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = DIRECTORY_MODE,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        key = os.fspath(path)
        self.mkdir_calls.append((key, mode, parents, exist_ok))
        if parents:
            current = Path(key)
            parents_to_add: list[str] = []
            while str(current) != "/":
                parents_to_add.append(str(current))
                current = current.parent
            self.directories.update(parents_to_add)
        else:
            self.directories.add(key)

    def geteuid(self) -> int:
        return self.euid


def ref(role: str, revision: str) -> SnapshotRef:
    return SnapshotRef(role, f"example-org/{role}", revision)


def entry(*refs: SnapshotRef, completed: str | None = "") -> PullEntry:
    return PullEntry(
        profile="example-profile",
        models=refs,
        started="2026-09-04T10:00:00+00:00",
        completed=completed,
        identity=ProcessIdentity(1234, "boot-a", 77),
    )


class Layout(unittest.TestCase):
    """The cache layout follows huggingface_hub's hub-cache vocabulary."""

    def test_layout_is_derived_from_the_loaded_lock(self) -> None:
        digest = bare_digest(PINNED_FILE.sha256)
        self.assertEqual(repository_folder(MODEL.repo), f"models--{MODEL.repo.replace('/', '--')}")
        self.assertEqual(repository_dir(MODEL.repo), MODELS_ROOT / HUB_DIR_NAME / repository_folder(MODEL.repo))
        self.assertEqual(blob_path(MODEL.repo, PINNED_FILE.sha256).name, digest)
        self.assertEqual(
            snapshot_path(MODEL.repo, MODEL.revision, PINNED_FILE.path),
            repository_dir(MODEL.repo) / "snapshots" / MODEL.revision / PINNED_FILE.path,
        )
        self.assertEqual(blob_link_target(PINNED_FILE.path, PINNED_FILE.sha256), f"../../blobs/{digest}")
        self.assertEqual(blob_link_target("nested/file.bin", digest), f"../../../blobs/{digest}")
        self.assertEqual(refs_main_path(MODEL.repo), repository_dir(MODEL.repo) / "refs" / "main")
        self.assertEqual(refs_main_content(MODEL.revision), MODEL.revision)
        self.assertEqual(
            resolve_url(MODEL, PINNED_FILE.path),
            f"https://huggingface.co/{MODEL.repo}/resolve/{MODEL.revision}/"
            f"{quote(PINNED_FILE.path, safe='/')}",
        )
        self.assertEqual(MODEL.serve.env.get("HF_HOME"), str(MODELS_ROOT))
        self.assertEqual(BLOB_MODE, 0o444)
        self.assertEqual(DIRECTORY_MODE, 0o755)

    def test_digest_normalization_accepts_lock_and_bare_forms(self) -> None:
        self.assertEqual(bare_digest(PINNED_FILE.sha256), bare_digest(bare_digest(PINNED_FILE.sha256)))


class Proxy(unittest.TestCase):
    def test_site_argv_and_temporary_wgetrc(self) -> None:
        site = cast(SiteConfig, SimpleNamespace(egress_proxy="http://proxy.example:3128"))
        argv = wget_argv_for_site(site, "/tmp/file.partial", "https://example.test/file")
        self.assertEqual(
            argv,
            [
                "wget",
                "-e",
                "https_proxy=http://proxy.example:3128",
                "-e",
                "http_proxy=http://proxy.example:3128",
                "-qO",
                "/tmp/file.partial",
                "https://example.test/file",
            ],
        )
        host = FakeHost({"/etc/gideon/secrets/proxy_auth": "alice:s3cret\n"})
        with temporary_wgetrc(host, command="gideon models pull") as setup:
            self.assertTrue(setup.ok)
            assert setup.path is not None
            self.assertEqual(host.writes[0][1], "proxy_user = alice\nproxy_password = s3cret\n")
            self.assertEqual(host.writes[0][2], 0o600)
            self.assertEqual(setup.prefix(argv)[0], "env")
            self.assertTrue(setup.prefix(argv)[1].startswith("WGETRC=/run/"))
            self.assertNotIn("s3cret", setup.prefix(argv))
        self.assertFalse(host.exists(host.writes[0][0]))

    def test_wgetrc_is_removed_when_the_caller_raises(self) -> None:
        host = FakeHost({"/etc/gideon/secrets/proxy_auth": "alice:s3cret"})
        with self.assertRaisesRegex(RuntimeError, "stop"), temporary_wgetrc(host, command="gideon models pull"):
            raise RuntimeError("stop")
        self.assertFalse(host.exists(host.writes[0][0]))


class Identity(unittest.TestCase):
    def test_identity_reads_proc_fields_through_host(self) -> None:
        tail = " ".join(["S", *(["0"] * 18), "991"])
        host = FakeHost({"/proc/sys/kernel/random/boot_id": "boot-a\n"})
        with mock.patch("gideon.host.weights.os.getpid", return_value=1234):
            host.files["/proc/1234/stat"] = f"1234 (worker) {tail}"
            self.assertEqual(process_identity(host), ProcessIdentity(1234, "boot-a", 991))

    def test_liveness_requires_boot_and_start_time(self) -> None:
        identity = ProcessIdentity(1234, "boot-a", 991)
        stat = f"1234 (worker) {' '.join(['S', *(['0'] * 18), '991'])}"
        host = FakeHost({"/proc/sys/kernel/random/boot_id": "boot-a\n", "/proc/1234/stat": stat})
        self.assertTrue(process_is_alive(host, identity))
        host.files["/proc/1234/stat"] = stat.replace("991", "992")
        self.assertFalse(process_is_alive(host, identity))
        host.files["/proc/1234/stat"] = stat
        host.files["/proc/sys/kernel/random/boot_id"] = "boot-b\n"
        self.assertFalse(process_is_alive(host, identity))
        host.files.pop("/proc/1234/stat")
        self.assertFalse(process_is_alive(host, identity))


class PullRecordContracts(unittest.TestCase):
    def test_yaml_round_trip_and_atomic_host_write(self) -> None:
        record = PullRecord(in_progress=entry(ref("generator", "a" * 40), completed=None))
        text = dump_pull_record(record)
        loaded = parse_pull_record(text)
        self.assertEqual(loaded, record)
        host = FakeHost()
        self.assertIsNone(save_pull_record(host, record))
        self.assertEqual(host.writes[0][0], str(PULL_RECORD_PATH))
        self.assertEqual(parse_pull_record(host.writes[0][1]), record)

    def test_candidate_slot_round_trips_and_is_omitted_when_empty(self) -> None:
        candidate = recorded_candidate()
        record = PullRecord(candidates=(candidate,))
        text = dump_pull_record(record)
        self.assertIn("candidates:", text)
        self.assertEqual(parse_pull_record(text), record)
        self.assertEqual(yaml.safe_load(text)["candidates"][candidate.name]["role"], candidate.role)

        empty_text = dump_pull_record(PullRecord())
        self.assertNotIn("candidates:", empty_text)
        self.assertEqual(parse_pull_record(empty_text), PullRecord())
        self.assertEqual(parse_pull_record(empty_text + "candidates: {}\n"), PullRecord())

    def test_malformed_candidate_slot_is_a_record_problem(self) -> None:
        document = yaml.safe_load(dump_pull_record(PullRecord(candidates=(recorded_candidate(),))))
        entry_value = document["candidates"][FICTITIOUS_CANDIDATE.name]
        ordinary_malformed = parse_pull_record("in_progress: []\n")
        assert isinstance(ordinary_malformed, Problem)
        cases: tuple[tuple[str, object, str], ...] = (
            ("non-mapping slot", [], "candidates must be a mapping"),
            ("bad name", {"bad.name": entry_value}, "candidates.bad.name"),
            ("non-mapping entry", {FICTITIOUS_CANDIDATE.name: []}, "candidates.alternate"),
            ("wrong fields", {FICTITIOUS_CANDIDATE.name: {"role": entry_value["role"]}}, "must contain exactly"),
            ("non-string field", {FICTITIOUS_CANDIDATE.name: {**entry_value, "started": 17}}, ".started"),
            ("bad role", {FICTITIOUS_CANDIDATE.name: {**entry_value, "role": "Bad Role"}}, ".role"),
            ("bad repository", {FICTITIOUS_CANDIDATE.name: {**entry_value, "repo": "../escape"}}, ".repo"),
            ("bad revision", {FICTITIOUS_CANDIDATE.name: {**entry_value, "revision": "bad"}}, ".revision"),
            ("bad completion", {FICTITIOUS_CANDIDATE.name: {**entry_value, "completed": False}}, ".completed"),
        )
        for label, candidate_values, expected in cases:
            with self.subTest(label=label):
                malformed = {**document, "candidates": candidate_values}
                result = parse_pull_record(yaml.safe_dump(malformed))
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn(expected, result.problem)
                self.assertIn(str(PULL_RECORD_PATH), result.problem)
                self.assertEqual(result.fix, ordinary_malformed.fix)

    def test_missing_record_is_empty_and_malformed_record_is_a_problem(self) -> None:
        self.assertEqual(load_pull_record(FakeHost()), PullRecord())
        malformed = parse_pull_record("in_progress: []\ncurrent: null\nprevious: null\nretiring: []\n")
        self.assertIsInstance(malformed, Problem)
        assert isinstance(malformed, Problem)
        self.assertIn("pulls.yaml", malformed.problem)

    def test_record_values_are_held_to_the_lock_grammars(self) -> None:
        good = entry(ref("generator", "a" * 40), completed=None)
        for role, repo, revision, field in (
            ("generator", "example-org/generator", "../blobs", "revision"),
            ("generator", "../../etc", "a" * 40, "repo"),
            ("Bad Role", "example-org/generator", "a" * 40, "role"),
        ):
            bad = PullEntry(good.profile, (SnapshotRef(role, repo, revision),), good.started, None, good.identity)
            result = parse_pull_record(dump_pull_record(PullRecord(in_progress=bad)))
            self.assertIsInstance(result, Problem, field)
            assert isinstance(result, Problem)
            self.assertIn(f".{field}", result.problem)
        self.assertEqual(parse_pull_record(dump_pull_record(PullRecord(in_progress=good))), PullRecord(in_progress=good))

    def test_prune_refuses_a_snapshot_outside_its_repository(self) -> None:
        host = FakeHost()
        record = PullRecord(retiring=(SnapshotRef("generator", "example-org/generator", "../../escape"),))
        result = prune(host, record, MODELS_ROOT)
        self.assertIsInstance(result, Problem)
        self.assertFalse(any(command[:1] == ("rm",) for command in host.runs))

    def test_failed_b_then_rollback_a_journals_b_at_claim(self) -> None:
        a = ref("generator", "a" * 40)
        b = ref("generator", "b" * 40)
        record = PullRecord(current=entry(a))
        claimed_b = claim(record, entry(b))
        assert isinstance(claimed_b, PullRecord)
        claimed_a = claim(claimed_b, entry(a))
        assert isinstance(claimed_a, PullRecord)
        self.assertEqual(claimed_a.retiring, (b,))

    def test_interrupted_current_verify_does_not_retire_current(self) -> None:
        a = ref("generator", "a" * 40)
        b = ref("generator", "b" * 40)
        record = PullRecord(current=entry(a))
        interrupted = claim(record, entry(a))
        assert isinstance(interrupted, PullRecord)
        upgraded = claim(interrupted, entry(b))
        assert isinstance(upgraded, PullRecord)
        self.assertEqual(upgraded.retiring, ())

    def test_rollback_rotation_does_not_retire_a(self) -> None:
        a = ref("generator", "a" * 40)
        b = ref("generator", "b" * 40)
        record = PullRecord(current=entry(b), previous=entry(a))
        claimed = claim(record, entry(a))
        assert isinstance(claimed, PullRecord)
        rotated = rotate(claimed, "2026-09-04T10:05:00+00:00")
        assert isinstance(rotated, PullRecord)
        assert rotated.current is not None
        assert rotated.previous is not None
        self.assertEqual(rotated.current.models, (a,))
        self.assertEqual(rotated.previous.models, (b,))
        self.assertEqual(rotated.retiring, ())

    def test_two_model_rotation_retires_only_the_moved_reference(self) -> None:
        moved = ref("generator", "a" * 40)
        stable = ref("embedder", "b" * 40)
        new_moved = ref("generator", "c" * 40)
        record = PullRecord(current=entry(stable), previous=entry(moved, stable))
        claimed = claim(record, entry(new_moved, stable))
        assert isinstance(claimed, PullRecord)
        rotated = rotate(claimed, "2026-09-04T10:05:00+00:00")
        assert isinstance(rotated, PullRecord)
        self.assertEqual(rotated.retiring, (moved,))

    def test_two_roles_sharing_a_snapshot_do_not_retire_each_other(self) -> None:
        first = SnapshotRef("generator", FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision)
        second = replace(first, role="alternate-role")
        claimed = claim(PullRecord(in_progress=entry(first)), entry(second))
        self.assertIsInstance(claimed, PullRecord)
        assert isinstance(claimed, PullRecord)
        self.assertEqual(claimed.retiring, ())

        rotated = rotate(
            PullRecord(in_progress=entry(second), current=entry(first)),
            "2026-09-04T10:05:00+00:00",
        )
        self.assertIsInstance(rotated, PullRecord)
        assert isinstance(rotated, PullRecord)
        self.assertEqual(rotated.retiring, ())
        self.assertIsNone(rotated.previous)

    def test_claim_and_rotate_keep_a_candidate_snapshot_named(self) -> None:
        candidate = recorded_candidate()
        same_snapshot = SnapshotRef("former-role", candidate.repo, candidate.revision)
        another = ref("generator", "c" * 40)
        claimed = claim(
            PullRecord(in_progress=entry(same_snapshot), candidates=(candidate,)),
            entry(another),
        )
        self.assertIsInstance(claimed, PullRecord)
        assert isinstance(claimed, PullRecord)
        self.assertEqual(claimed.retiring, ())

        rotated = rotate(
            PullRecord(
                in_progress=entry(another),
                current=entry(ref("generator", "d" * 40)),
                previous=entry(same_snapshot),
                candidates=(candidate,),
            ),
            "2026-09-04T10:05:00+00:00",
        )
        self.assertIsInstance(rotated, PullRecord)
        assert isinstance(rotated, PullRecord)
        self.assertEqual(rotated.retiring, ())
        self.assertEqual(rotated.candidates, (candidate,))

    def test_dead_claim_with_the_same_set_is_resumed_under_new_identity(self) -> None:
        model = ref("generator", "a" * 40)
        old = entry(model)
        resumed = PullEntry(
            old.profile,
            old.models,
            old.started,
            old.completed,
            ProcessIdentity(old.identity.pid + 1, old.identity.boot_id, old.identity.start_time + 1),
        )
        result = claim(PullRecord(in_progress=old), resumed)
        self.assertIsInstance(result, PullRecord)
        assert isinstance(result, PullRecord)
        self.assertEqual(result.in_progress, resumed)
        self.assertEqual(result.retiring, ())

    def test_clear_and_release_finish_the_record(self) -> None:
        a = ref("generator", "a" * 40)
        record = PullRecord(in_progress=entry(a), retiring=(a,))
        self.assertEqual(clear_retiring(record).retiring, ())
        self.assertEqual(release(record), PullRecord())

    def test_dead_reused_or_rebooted_claim_can_be_replaced(self) -> None:
        old = entry(ref("generator", "a" * 40))
        replacement = entry(ref("generator", "b" * 40))
        for alive in (False,):
            result = claim(PullRecord(in_progress=old), replacement, alive=alive)
            self.assertIsInstance(result, PullRecord)
            assert isinstance(result, PullRecord)
            self.assertEqual(result.in_progress, replacement)

    def test_alive_claim_is_refused_without_replacing_it(self) -> None:
        old = entry(ref("generator", "a" * 40))
        replacement = entry(ref("generator", "b" * 40))
        result = claim(PullRecord(in_progress=old), replacement, alive=True)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("wait for it", result.problem)


FICTITIOUS_LOCK_TEXT = """\
version: 1
reference: 1x1v-1d
profiles:
  1x1v-1d:
    requires:
      platform: x86_64
      gpu:
        architecture: Fictitious
        compute_capability: "1.0"
        model: Fictitious GPU
        count: 1
        vram_gb: 1
      dram_gb: 1
      data_volume_gb: 1
    memory:
      fictitious-generator:
        gb: 1
        role: generator
    models:
      generator:
        repo: fictitious-org/fictitious-model
        revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        gpu: 0
        serve:
          served_name: fictitious-generator
          env:
            HF_HOME: /data/models
          flags: {}
        files:
          alpha.bin:
            sha256: sha256:1111111111111111111111111111111111111111111111111111111111111111
            size: 100000004
          beta.bin:
            sha256: sha256:2222222222222222222222222222222222222222222222222222222222222222
            size: 100000006
          nested/gamma.bin:
            sha256: sha256:3333333333333333333333333333333333333333333333333333333333333333
            size: 100000008
          zeta.bin:
            sha256: sha256:4444444444444444444444444444444444444444444444444444444444444444
            size: 100000010
"""
FICTITIOUS_RESULT = load_models_lock_text(FICTITIOUS_LOCK_TEXT)
assert FICTITIOUS_RESULT.lock is not None
FICTITIOUS_MODEL: HardwareProfile = cast(
    HardwareProfile, FICTITIOUS_RESULT.lock.profile("1x1v-1d")
)
assert FICTITIOUS_MODEL is not None
FICTITIOUS_PIN = FICTITIOUS_MODEL.models[0]
FICTITIOUS_CANDIDATE_TEXT = """candidates:
  alternate:
    role: generator
    repo: fictitious-org/alternate
    revision: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    serve:
      served_name: fictitious-alternate
      env:
        HF_HOME: /data/models
      flags: {}
    files:
      nested/alternate.bin:
        sha256: sha256:5555555555555555555555555555555555555555555555555555555555555555
        size: 100000002
"""
FICTITIOUS_CANDIDATE_RESULT = load_models_lock_text(
    FICTITIOUS_LOCK_TEXT + FICTITIOUS_CANDIDATE_TEXT
)
assert FICTITIOUS_CANDIDATE_RESULT.lock is not None
FICTITIOUS_CANDIDATE = cast(
    CandidatePin, FICTITIOUS_CANDIDATE_RESULT.lock.candidate("alternate")
)
assert FICTITIOUS_CANDIDATE is not None


def recorded_candidate(
    pin: CandidatePin = FICTITIOUS_CANDIDATE,
    *,
    started: str = "2026-09-04T10:00:00+00:00",
    completed: str | None = "2026-09-04T10:01:00+00:00",
) -> CandidateEntry:
    return CandidateEntry(
        pin.name, pin.role, pin.repo, pin.revision, started, completed
    )


FICTITIOUS_SITE = cast(
    SiteConfig, SimpleNamespace(egress_proxy="http://proxy.invalid:3128")
)


def _wrong_digest_wget(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
    partial = command[command.index("-qO") + 1]
    host.sizes[partial] = FICTITIOUS_PIN.files[0].size
    host.digests[partial] = "f" * 64
    return subprocess.CompletedProcess(command, 0, "", "HTTP/1.1 200 OK\n")


class ModelConvergence(unittest.TestCase):
    """Classification and convergence use only the loaded fictitious lock."""

    def test_fixes_follow_the_run_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        for installed in (False, True):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                prefix = "gideon" if installed else "sudo python3 -m gideon"
                retry = f"then re-run {prefix} models pull."

                record = parse_pull_record("in_progress: []\n")
                self.assertIsInstance(record, Problem)
                assert isinstance(record, Problem)
                self.assertTrue(record.fix.endswith(retry), record.fix)

                foreign = snapshot_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision, "foreign.txt")
                outcome = converge_model(
                    FakeHost({str(foreign): "foreign"}),
                    FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT,
                )
                self.assertEqual(outcome.kind, "failed")
                self.assertIn(f"Remove foreign.txt, then re-run {prefix} models pull;", outcome.fix)

                host = FakeHost(available=0)
                outcome = converge_model(
                    host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT,
                )
                self.assertEqual(outcome.kind, "failed")
                self.assertEqual(outcome.fix, f"Free space on /data, {retry}")

                file = FICTITIOUS_PIN.files[0]
                blob = str(blob_path(FICTITIOUS_PIN.repo, file.sha256))
                host = FakeHost(sizes={blob: file.size}, digests={blob: bare_digest(file.sha256)})
                host.verify_returncode = 127
                outcome = converge_model(
                    host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT,
                )
                self.assertEqual(outcome.kind, "failed")
                self.assertIn("Install coreutils", outcome.fix)
                self.assertTrue(outcome.fix.endswith(retry), outcome.fix)

                missing_wget = FakeHost(
                    wget=lambda command, _host: subprocess.CompletedProcess(command, 127, "", "")
                )
                outcome = converge_model(
                    missing_wget, FICTITIOUS_SITE, FICTITIOUS_PIN,
                    _no_proxy_wgetrc(), root=MODELS_ROOT,
                )
                self.assertEqual(outcome.kind, "failed")
                self.assertEqual(
                    outcome.fix,
                    f"Install wget with sudo apt-get install wget, {retry}",
                )

                blocked = FakeHost(
                    wget=lambda command, _host: subprocess.CompletedProcess(command, 8, "", "")
                )
                outcome = converge_model(
                    blocked, FICTITIOUS_SITE, FICTITIOUS_PIN,
                    _no_proxy_wgetrc(), root=MODELS_ROOT,
                )
                self.assertEqual(outcome.kind, "failed")
                self.assertTrue(outcome.fix.endswith(retry), outcome.fix)

                unprivileged = FakeHost()
                unprivileged.euid = 1000
                error = StringIO()
                with redirect_stderr(error):
                    code = run_models_pull(SimpleNamespace(), host=unprivileged)
                self.assertEqual(code, 1)
                self.assertIn(f"Fix: Run {prefix} models pull.", error.getvalue())

    def test_profile_and_candidate_rows_use_their_own_labels(self) -> None:
        for pin, label in (
            (FICTITIOUS_PIN, FICTITIOUS_PIN.role),
            (FICTITIOUS_CANDIDATE, FICTITIOUS_CANDIDATE.name),
        ):
            with self.subTest(label=label), redirect_stdout(StringIO()):
                host = FakeHost(wget=_successful_wget)
                outcome = converge_model(
                    host, FICTITIOUS_SITE, pin, _no_proxy_wgetrc(), root=MODELS_ROOT
                )
                self.assertEqual(outcome.role, label)
                self.assertEqual(model_row(outcome).name, label)
                self.assertEqual(
                    resolve_url(pin, pin.files[0].path),
                    f"https://huggingface.co/{pin.repo}/resolve/{pin.revision}/"
                    f"{quote(pin.files[0].path, safe='/')}",
                )

    def test_classification_truth_table_and_one_verification_pass(self) -> None:
        alpha, beta, gamma, zeta = FICTITIOUS_PIN.files
        snapshot = snapshot_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision, alpha.path)
        files = {str(snapshot.parent / "foreign.txt"): "foreign"}
        sizes = {
            str(blob_path(FICTITIOUS_PIN.repo, alpha.sha256)): alpha.size,
            str(blob_path(FICTITIOUS_PIN.repo, beta.sha256)): beta.size,
            str(blob_path(FICTITIOUS_PIN.repo, gamma.sha256).with_name(f"{bare_digest(gamma.sha256)}.partial")): 2,
            str(blob_path(FICTITIOUS_PIN.repo, zeta.sha256)): zeta.size + 1,
        }
        links = {
            str(snapshot): blob_link_target(alpha.path, alpha.sha256),
        }
        host = FakeHost(files, sizes=sizes, links=links)
        host.digests[str(blob_path(FICTITIOUS_PIN.repo, alpha.sha256))] = bare_digest(alpha.sha256)
        host.digests[str(blob_path(FICTITIOUS_PIN.repo, beta.sha256))] = bare_digest(beta.sha256)
        host.verify_returncode = 1  # stdout, rather than the exit code, is authoritative.

        states = classify_model(host, FICTITIOUS_PIN, MODELS_ROOT)
        assert not isinstance(states, Problem)

        self.assertEqual(states[alpha.path], FileState.PRESENT)
        self.assertEqual(states[beta.path], FileState.UNLINKED)
        self.assertEqual(states[gamma.path], FileState.PARTIAL)
        self.assertEqual(states[zeta.path], FileState.MISMATCHED)
        self.assertEqual(states["foreign.txt"], FileState.FOREIGN)
        verification = next(
            index for index, command in enumerate(host.runs) if command[:3] == ("sha256sum", "-c", "-")
        )
        self.assertEqual(
            host.inputs[verification],
            "".join(
                f"{bare_digest(file.sha256)}  {blob_path(FICTITIOUS_PIN.repo, file.sha256, MODELS_ROOT)}\n"
                for file in (alpha, beta)
            ),
        )

    def test_missing_snapshot_makes_every_lock_file_absent(self) -> None:
        states = classify_model(FakeHost(), FICTITIOUS_PIN, MODELS_ROOT)
        self.assertEqual(
            states,
            {file.path: FileState.ABSENT for file in FICTITIOUS_PIN.files},
        )

    def test_missing_tools_refuse_rather_than_refetch(self) -> None:
        file = FICTITIOUS_PIN.files[0]
        blob = str(blob_path(FICTITIOUS_PIN.repo, file.sha256))
        host = FakeHost(sizes={blob: file.size}, digests={blob: bare_digest(file.sha256)})
        host.verify_returncode = 127
        outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("sha256sum failed", outcome.problem)
        self.assertFalse(any(command[:1] == ("wget",) for command in host.runs))

        absent = FakeHost(wget=lambda command, host: subprocess.CompletedProcess(command, 127, "", ""))
        outcome = converge_model(absent, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("apt-get install wget", outcome.fix)

    def test_foreign_file_refuses_before_wget_and_free_space_names_decimal_shortfall(self) -> None:
        foreign = snapshot_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision, "foreign.txt")
        host = FakeHost({str(foreign): "foreign"}, available=0)
        with redirect_stdout(StringIO()) as output:
            outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("foreign.txt", outcome.fix)
        self.assertEqual(host.runs, [])
        self.assertEqual(output.getvalue(), "")

        host = FakeHost(available=sum(file.size for file in FICTITIOUS_PIN.files) - 100_000_000)
        outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("0.1 GB", outcome.problem)
        self.assertTrue(any(command[:1] == ("df",) for command in host.runs))

    def test_a_damaged_blob_counts_as_freed_space(self) -> None:
        # Every blob present at the pinned size but damaged: with nothing free, the
        # repair still fits, because each blob is removed before its own re-fetch.
        sizes = {str(blob_path(FICTITIOUS_PIN.repo, f.sha256)): f.size for f in FICTITIOUS_PIN.files}
        links = {
            str(snapshot_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision, f.path)): blob_link_target(f.path, f.sha256)
            for f in FICTITIOUS_PIN.files
        }
        host = FakeHost(sizes=sizes, links=links, available=0)

        def successful(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
            partial = command[command.index("-qO") + 1]
            file = next(f for f in FICTITIOUS_PIN.files if partial.endswith(f"{bare_digest(f.sha256)}.partial"))
            host.sizes[partial] = file.size
            host.digests[partial] = bare_digest(file.sha256)
            return subprocess.CompletedProcess(command, 0, "", "")

        host.wget = successful
        with redirect_stdout(StringIO()):
            outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "fetched")
        self.assertEqual({file.action for file in outcome.files}, {"refetched"})

    def test_exact_proxy_wget_argv_hosts_refs_and_second_classification(self) -> None:
        def wget(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
            partial = command[command.index("-qO") + 1]
            file = next(file for file in FICTITIOUS_PIN.files if partial.endswith(f"{bare_digest(file.sha256)}.partial"))
            host.sizes[partial] = file.size
            host.digests[partial] = bare_digest(file.sha256)
            # wget -S indents every header line it echoes.
            stderr = "  HTTP/1.1 302 Found\n  Location: https://z.invalid/file\n  HTTP/1.1 302 Found\n  Location: https://a.invalid/file\n  HTTP/1.1 200 OK\n"
            return subprocess.CompletedProcess(command, 0, "", stderr)

        host = FakeHost(
            {"/etc/gideon/secrets/proxy_auth": "alice:s3cret\n"},
            wget=wget,
        )
        with temporary_wgetrc(host, command="gideon models pull") as setup:
            with redirect_stdout(StringIO()) as output:
                outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, setup, root=MODELS_ROOT)
            self.assertTrue(setup.ok)
            assert setup.path is not None
            wget_commands = [command for command in host.runs if "wget" in command]
            self.assertEqual(len(wget_commands), len(FICTITIOUS_PIN.files))
            expected = wget_argv_for_site(
                FICTITIOUS_SITE,
                str(blob_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.files[0].sha256).with_name(f"{bare_digest(FICTITIOUS_PIN.files[0].sha256)}.partial")),
                resolve_url(FICTITIOUS_PIN, FICTITIOUS_PIN.files[0].path),
            )
            expected[1:1] = [
                "-c", "-S", "--tries=3", "--waitretry=5", "--retry-connrefused",
                "--connect-timeout=30", "--read-timeout=60",
            ]
            self.assertEqual(wget_commands[0][0], "env")
            self.assertEqual(wget_commands[0][1], f"WGETRC={setup.path}")
            self.assertEqual(list(wget_commands[0][2:]), expected)
            self.assertNotIn("s3cret", "\n".join(" ".join(command) for command in host.runs))
            self.assertNotIn("s3cret", output.getvalue())
        self.assertFalse(host.exists(setup.path))
        self.assertEqual(outcome.kind, "fetched")
        self.assertEqual(outcome.hosts, ("a.invalid", "huggingface.co", "z.invalid"))
        self.assertIn("fetched alpha.bin (100.0 MB)", output.getvalue())
        self.assertEqual(FICTITIOUS_PIN.revision, host.files[str(refs_main_path(FICTITIOUS_PIN.repo))])
        self.assertFalse(host.files[str(refs_main_path(FICTITIOUS_PIN.repo))].endswith("\n"))
        reclassified = classify_model(host, FICTITIOUS_PIN, MODELS_ROOT)
        assert not isinstance(reclassified, Problem)
        self.assertTrue(all(state is FileState.PRESENT for state in reclassified.values()))
        row = model_row(outcome)
        self.assertTrue(row.ok)
        self.assertIn("via a.invalid, huggingface.co, z.invalid", row.detail)

    def test_size_and_digest_mismatch_remove_partial_and_refuse(self) -> None:
        file = FICTITIOUS_PIN.files[0]

        def wrong_size(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
            partial = command[command.index("-qO") + 1]
            host.sizes[partial] = file.size + 1
            return subprocess.CompletedProcess(command, 0, "", "HTTP/1.1 200 OK\n")

        host = FakeHost(wget=wrong_size)
        outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        partial = _partial_for(file)
        self.assertEqual(outcome.kind, "failed")
        self.assertFalse(host.exists(partial))
        self.assertIn("lock requires", outcome.problem)
        self.assertIn("source archive is republished", outcome.fix)

        host = FakeHost(wget=_wrong_digest_wget)
        outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "failed")
        self.assertFalse(host.exists(_partial_for(file)))
        self.assertIn("digest", outcome.problem)
        self.assertIn("source archive is republished", outcome.fix)

    def test_lock_rejected_fetch_fix_names_public_repository_issues(self) -> None:
        host = FakeHost(wget=_wrong_digest_wget)
        outcome = converge_model(host, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("digest", outcome.problem)
        self.assertIn(f"{PUBLIC_REPOSITORY_URL}/issues", outcome.fix)
        self.assertNotIn("report it to TNMD", outcome.fix)

    def test_network_refusals_refetch_relink_and_resume(self) -> None:
        file = FICTITIOUS_PIN.files[0]

        def response(stderr: str) -> Callable[[list[str], FakeHost], subprocess.CompletedProcess[str]]:
            return lambda command, host: subprocess.CompletedProcess(command, 8, "", stderr)

        missing = FakeHost(wget=response("HTTP/1.1 404 Not Found\n"))
        outcome = converge_model(missing, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertIn("HTTP 404", outcome.problem)
        self.assertIn("source archive is republished", outcome.fix)

        blocked = FakeHost(wget=response(""))
        outcome = converge_model(blocked, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertIn("huggingface.co", outcome.problem)
        self.assertIn("install-upgrade group", outcome.fix)

        blob = blob_path(FICTITIOUS_PIN.repo, file.sha256)
        link = snapshot_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision, file.path)
        refetch = FakeHost(
            sizes={str(blob): file.size + 1},
            links={str(link): "wrong"},
        )

        def successful(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
            partial = command[command.index("-qO") + 1]
            host.sizes[partial] = file.size
            host.digests[partial] = bare_digest(file.sha256)
            return subprocess.CompletedProcess(command, 0, "", "HTTP/1.1 200 OK\n")

        refetch.wget = successful
        with redirect_stdout(StringIO()) as output:
            outcome = converge_model(refetch, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.files[0].action, "refetched")
        self.assertIn("refetched alpha.bin", output.getvalue())

        relink_sizes = {
            str(blob_path(FICTITIOUS_PIN.repo, pinned.sha256)): pinned.size
            for pinned in FICTITIOUS_PIN.files
        }
        relink_digests = {
            str(blob_path(FICTITIOUS_PIN.repo, pinned.sha256)): bare_digest(pinned.sha256)
            for pinned in FICTITIOUS_PIN.files
        }
        relink_links = {
            str(snapshot_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.revision, pinned.path)):
            blob_link_target(pinned.path, pinned.sha256)
            for pinned in FICTITIOUS_PIN.files[1:]
        }
        relink = FakeHost(sizes=relink_sizes, digests=relink_digests, links=relink_links)
        with redirect_stdout(StringIO()) as output:
            outcome = converge_model(relink, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.files[0].action, "relinked")
        self.assertIn("relinked alpha.bin", output.getvalue())
        self.assertFalse(any(command[:1] == ("wget",) for command in relink.runs))

        resumed = FakeHost(
            sizes={_partial_for(file): 3},
        )
        resumed.wget = successful
        with redirect_stdout(StringIO()) as output:
            outcome = converge_model(resumed, FICTITIOUS_SITE, FICTITIOUS_PIN, _no_proxy_wgetrc(), root=MODELS_ROOT)
        self.assertEqual(outcome.files[0].action, "resumed")
        self.assertIn(f"resumed {file.path} from 0.0 KB (100.0 MB)", output.getvalue())


FICTITIOUS_EGRESS_TEXT = """\
version: 1
groups:
  host-provisioning:
    - host: provision.invalid
      probe_url: https://provision.invalid/
  install-upgrade:
    - host: firewall.invalid
      probe_url: https://firewall.invalid/
  corpus:
    - host: corpus.invalid
      probe_url: https://corpus.invalid/
  image-build:
    - host: build.invalid
      probe_url: https://build.invalid/
"""

FICTITIOUS_SITE_TEXT = """\
office:
  name: Fictitious Office
  short_name: FICT
  timezone: UTC
hostname: gideon.invalid
lan_cidrs: [192.0.2.0/24]
jurisdiction:
  circuit: ca6
  districts: [fictitious]
  states: [fictitious]
auth:
  ldap:
    host: ldap.invalid
backup:
  target:
    host: backup.invalid
    path: /backup
alerts:
  smtp:
    host: smtp.invalid
    from: gideon@fictitious.invalid
  recipients: [admin@fictitious.invalid]
hardware_profile: 1x1v-1d
"""


def _loaded_egress(text: str) -> EgressAllowlist:
    result = load_egress_allowlist(
        "/fixture/egress.yaml", host=FakeHost({"/fixture/egress.yaml": text})
    )
    assert result.allowlist is not None, result.errors
    return result.allowlist


def _add_process_files(host: FakeHost, *, boot: str = "boot-pull", start: int = 42) -> None:
    pid = os.getpid()
    tail = " ".join(["S", *("0" for _ in range(18)), str(start)])
    host.files["/proc/sys/kernel/random/boot_id"] = f"{boot}\n"
    host.files[f"/proc/{pid}/stat"] = f"{pid} (models pull) {tail}"


def _successful_wget(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
    partial = command[command.index("-qO") + 1]
    pinned = next(
        file for file in (*FICTITIOUS_PIN.files, *FICTITIOUS_CANDIDATE.files)
        if partial.endswith(f"{bare_digest(file.sha256)}.partial")
    )
    host.sizes[partial] = pinned.size
    host.digests[partial] = bare_digest(pinned.sha256)
    return subprocess.CompletedProcess(command, 0, "", "HTTP/1.1 200 OK\n")


class PruneContracts(unittest.TestCase):
    """Pruning follows disk reachability, including unrecorded snapshots."""

    def test_prune_keeps_unrecorded_snapshot_and_reachable_blob(self) -> None:
        old_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        experiment_revision = "cccccccccccccccccccccccccccccccccccccccc"
        old_snapshot = snapshot_path(FICTITIOUS_PIN.repo, old_revision, FICTITIOUS_PIN.files[0].path)
        experiment_file = snapshot_path(FICTITIOUS_PIN.repo, experiment_revision, FICTITIOUS_PIN.files[1].path)
        old_blob = blob_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.files[0].sha256)
        kept_blob = blob_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.files[1].sha256)
        orphan = blob_path(FICTITIOUS_PIN.repo, FICTITIOUS_PIN.files[2].sha256).with_name("orphan.partial")
        host = FakeHost(
            {str(old_snapshot): "old", str(experiment_file): "experiment"},
            sizes={str(old_blob): FICTITIOUS_PIN.files[0].size, str(kept_blob): FICTITIOUS_PIN.files[1].size, str(orphan): 2},
            links={str(experiment_file): blob_link_target(FICTITIOUS_PIN.files[1].path, FICTITIOUS_PIN.files[1].sha256)},
        )
        record = PullRecord(retiring=(SnapshotRef("generator", FICTITIOUS_PIN.repo, old_revision),))
        lines = prune(host, record, MODELS_ROOT)
        self.assertIsInstance(lines, list)
        assert isinstance(lines, list)
        self.assertIn(f"  removed {old_snapshot.parent}", lines)
        self.assertIn(f"  removed {old_blob}", lines)
        self.assertIn(f"  removed {orphan}", lines)
        self.assertTrue(host.exists(experiment_file))
        self.assertTrue(host.exists(kept_blob))
        self.assertFalse(host.exists(old_blob))
        self.assertFalse(host.exists(orphan))

    def test_prune_removes_empty_repository_and_kept_lines_filter_shared_refs(self) -> None:
        gone_repo = "gone-org/gone-model"
        gone_revision = "dddddddddddddddddddddddddddddddddddddddd"
        record = PullRecord(
            current=entry(ref("generator", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")),
            previous=entry(
                ref("generator", "ffffffffffffffffffffffffffffffffffffffff"),
                ref("embedder", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"),
            ),
            retiring=(SnapshotRef("generator", gone_repo, gone_revision),),
        )
        lines = prune(FakeHost(), record, MODELS_ROOT)
        assert isinstance(lines, list)
        self.assertIn(f"  removed {repository_dir(gone_repo)}", lines)
        self.assertEqual(
            kept_lines(record),
            [
                (
                    "  kept snapshot ffffffffffffffffffffffffffffffffffffffff of example-org/generator "
                    "(the previous set, for rollback)"
                ),
                (
                    "  kept snapshot eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee of example-org/embedder "
                    "(the previous set, for rollback)"
                ),
            ],
        )


class CandidateReconciliation(unittest.TestCase):
    def test_keep_retire_promote_and_touch_one_name(self) -> None:
        candidate = recorded_candidate()
        record = PullRecord(candidates=(candidate,))
        kept = reconcile_candidates(record, (FICTITIOUS_CANDIDATE,), {candidate.name})
        self.assertEqual(kept.candidates, (candidate,))
        self.assertEqual(kept.retiring, ())

        retired = reconcile_candidates(record, (), {candidate.name})
        self.assertEqual(retired.candidates, ())
        self.assertEqual(
            retired.retiring,
            (SnapshotRef(candidate.role, candidate.repo, candidate.revision),),
        )

        promoted = reconcile_candidates(
            replace(
                record,
                in_progress=entry(SnapshotRef("generator", candidate.repo, candidate.revision)),
            ),
            (),
            {candidate.name},
        )
        self.assertEqual(promoted.candidates, ())
        self.assertEqual(promoted.retiring, ())

        other = replace(candidate, name="other", repo="fictitious-org/other")
        selected = reconcile_candidates(
            PullRecord(candidates=(candidate, other)),
            (),
            {candidate.name},
        )
        self.assertEqual(selected.candidates, (other,))
        self.assertEqual(
            selected.retiring,
            (SnapshotRef(candidate.role, candidate.repo, candidate.revision),),
        )

    def test_candidate_pull_leaves_another_recorded_name_alone(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        selected = recorded_candidate(completed=None)
        other = replace(
            recorded_candidate(), name="other", repo="fictitious-org/other"
        )
        self.assertIsNone(
            save_pull_record(host, PullRecord(candidates=(selected, other)))
        )

        with redirect_stdout(StringIO()):
            outcome = pull_profile(
                host,
                FICTITIOUS_SITE,
                FICTITIOUS_MODEL,
                _loaded_egress((ROOT / "config/egress.yaml").read_text()),
                candidates=(FICTITIOUS_CANDIDATE,),
                candidate=FICTITIOUS_CANDIDATE,
            )

        self.assertTrue(outcome.ok)
        saved = load_pull_record(host)
        assert isinstance(saved, PullRecord)
        self.assertIn(other, saved.candidates)
        self.assertEqual(saved.retiring, ())

    def test_rename_keeps_tracking_then_bare_pull_retires_removed_block(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        allowlist = _loaded_egress((ROOT / "config/egress.yaml").read_text())
        with redirect_stdout(StringIO()):
            pulled = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
            )
        self.assertTrue(pulled.ok)
        original = load_pull_record(host)
        assert isinstance(original, PullRecord)
        self.assertEqual(len(original.candidates), 1)

        renamed_pin = replace(FICTITIOUS_CANDIDATE, name="replacement")
        before_wget = sum(command[0] == "wget" for command in host.runs)
        with redirect_stdout(StringIO()) as output:
            kept = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(renamed_pin,),
            )
        self.assertTrue(kept.ok)
        self.assertEqual(sum(command[0] == "wget" for command in host.runs), before_wget)
        self.assertIn("kept candidate replacement", output.getvalue())
        renamed = load_pull_record(host)
        assert isinstance(renamed, PullRecord)
        self.assertEqual(renamed.candidates, (replace(original.candidates[0], name="replacement"),))
        self.assertEqual(renamed.retiring, ())

        snapshot = snapshot_path(
            renamed_pin.repo, renamed_pin.revision, renamed_pin.files[0].path
        )
        with redirect_stdout(StringIO()) as output:
            removed = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(),
            )
        self.assertTrue(removed.ok)
        self.assertIn(f"removed {snapshot.parents[1]}", output.getvalue())
        self.assertFalse(host.exists(snapshot))
        final = load_pull_record(host)
        assert isinstance(final, PullRecord)
        self.assertEqual(final.candidates, ())
        self.assertNotIn("candidates:", host.files[str(PULL_RECORD_PATH)])

    def test_promotion_keeps_snapshot_without_fetching(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        allowlist = _loaded_egress((ROOT / "config/egress.yaml").read_text())
        with redirect_stdout(StringIO()):
            pulled = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
            )
        self.assertTrue(pulled.ok)
        promoted_pin = replace(
            FICTITIOUS_PIN,
            repo=FICTITIOUS_CANDIDATE.repo,
            revision=FICTITIOUS_CANDIDATE.revision,
            serve=FICTITIOUS_CANDIDATE.serve,
            files=FICTITIOUS_CANDIDATE.files,
        )
        promoted_profile = replace(FICTITIOUS_MODEL, models=(promoted_pin,))
        before_wget = sum(command[0] == "wget" for command in host.runs)
        with redirect_stdout(StringIO()):
            promoted = pull_profile(
                host, FICTITIOUS_SITE, promoted_profile, allowlist,
                candidates=(),
            )
        self.assertTrue(promoted.ok)
        self.assertEqual(sum(command[0] == "wget" for command in host.runs), before_wget)
        record = load_pull_record(host)
        assert isinstance(record, PullRecord)
        self.assertEqual(record.candidates, ())
        self.assertEqual(record.retiring, ())
        self.assertEqual(record.current.models if record.current else (), snapshot_refs(promoted_profile.models))
        self.assertTrue(host.exists(snapshot_path(promoted_pin.repo, promoted_pin.revision, promoted_pin.files[0].path)))


class PullProfileContracts(unittest.TestCase):
    """The journal writes surround pruning and model convergence."""

    def test_candidate_retries_follow_the_run_form_once(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        allowlist = _loaded_egress((ROOT / "config/egress.yaml").read_text())

        def blocked_candidate(
            command: list[str], host: FakeHost
        ) -> subprocess.CompletedProcess[str]:
            if any(FICTITIOUS_CANDIDATE.repo in part for part in command):
                return subprocess.CompletedProcess(command, 8, "", "")
            return _successful_wget(command, host)

        for installed in (False, True):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                prefix = "gideon" if installed else "sudo python3 -m gideon"
                retry = f"re-run {prefix} models pull {FICTITIOUS_CANDIDATE.name}."

                host = FakeHost(wget=blocked_candidate)
                _add_process_files(host)
                with redirect_stdout(StringIO()) as output:
                    outcome = pull_profile(
                        host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                        candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
                    )
                self.assertFalse(outcome.ok)
                self.assertTrue(outcome.fix.endswith(retry), outcome.fix)
                self.assertIn(retry, output.getvalue())

                already_named = f"then {retry}"
                self.assertEqual(_retry_fix(already_named, FICTITIOUS_CANDIDATE), already_named)
                self.assertEqual(
                    _retry_fix("then re-run models pull.", FICTITIOUS_CANDIDATE),
                    f"then re-run models pull {FICTITIOUS_CANDIDATE.name}.",
                )

                invalid_proxy = FakeHost({"/etc/gideon/secrets/proxy_auth": "not-a-pair"})
                _add_process_files(invalid_proxy)
                outcome = pull_profile(
                    invalid_proxy, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                    candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
                )
                self.assertFalse(outcome.ok)
                self.assertTrue(outcome.fix.endswith(f"then {retry}"), outcome.fix)

    def test_candidate_rows_and_record_writes_then_verified_rerun_and_bare_keep(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        allowlist = _loaded_egress((ROOT / "config/egress.yaml").read_text())
        current = entry(*snapshot_refs(FICTITIOUS_MODEL.models), completed="2026-09-04T09:00:00+00:00")
        previous = entry(
            SnapshotRef("generator", "fictitious-org/previous", "c" * 40),
            completed="2026-09-03T09:00:00+00:00",
        )
        self.assertIsNone(save_pull_record(host, PullRecord(current=current, previous=previous)))
        host.writes.clear()
        times = iter(
            (
                "2026-09-04T10:00:00+00:00",
                "2026-09-04T10:01:00+00:00",
                "2026-09-04T10:02:00+00:00",
                "2026-09-04T10:03:00+00:00",
            )
        )
        with redirect_stdout(StringIO()) as output:
            outcome = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
                now=lambda: next(times),
            )
        self.assertTrue(outcome.ok)
        self.assertEqual([model.role for model in outcome.models], [FICTITIOUS_PIN.role, FICTITIOUS_CANDIDATE.name])
        rows = output.getvalue()
        self.assertLess(rows.index(f"{FICTITIOUS_PIN.role}: ok"), rows.index(f"{FICTITIOUS_CANDIDATE.name}: ok"))
        self.assertIn(f"{FICTITIOUS_CANDIDATE.name}: ok — fetched", rows)

        writes = [text for path, text, _mode in host.writes if path == str(PULL_RECORD_PATH)]
        self.assertEqual(len(writes), 4)
        records = [parse_pull_record(text) for text in writes]
        self.assertTrue(all(isinstance(record, PullRecord) for record in records))
        first, second, third, fourth = cast(tuple[PullRecord, ...], tuple(records))
        self.assertEqual(first.candidates[0].started, "2026-09-04T10:01:00+00:00")
        self.assertTrue(all(record.candidates[0].completed is None for record in (first, second, third)))
        self.assertEqual(fourth.candidates[0].completed, "2026-09-04T10:03:00+00:00")
        self.assertEqual(first.in_progress.models if first.in_progress else (), current.models)
        self.assertTrue(all(record.current is not None and record.current.models == current.models for record in (first, second, third, fourth)))
        self.assertTrue(all(record.previous is not None and record.previous.models == previous.models for record in (first, second, third, fourth)))
        self.assertIsNone(fourth.in_progress)

        before_wget = sum(command[0] == "wget" for command in host.runs)
        with redirect_stdout(StringIO()) as output:
            repeated = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
            )
        self.assertTrue(repeated.ok)
        self.assertEqual(sum(command[0] == "wget" for command in host.runs), before_wget)
        self.assertIn(f"{FICTITIOUS_CANDIDATE.name}: ok — present", output.getvalue())

        with redirect_stdout(StringIO()) as output:
            bare = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, allowlist,
                candidates=(FICTITIOUS_CANDIDATE,),
            )
        self.assertTrue(bare.ok)
        self.assertEqual(sum(command[0] == "wget" for command in host.runs), before_wget)
        self.assertIn(f"kept candidate {FICTITIOUS_CANDIDATE.name}", output.getvalue())
        self.assertNotIn(f"{FICTITIOUS_CANDIDATE.name}: ok", output.getvalue())
        saved = load_pull_record(host)
        assert isinstance(saved, PullRecord)
        self.assertEqual(saved.candidates[0].name, FICTITIOUS_CANDIDATE.name)

    def test_candidate_partial_survives_first_prune_and_resumes(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        file = FICTITIOUS_CANDIDATE.files[0]
        partial = blob_path(FICTITIOUS_CANDIDATE.repo, file.sha256).with_name(
            f"{bare_digest(file.sha256)}.partial"
        )
        old_revision = "d" * 40
        snapshots = repository_dir(FICTITIOUS_CANDIDATE.repo) / "snapshots"
        host.directories.update({
            str(snapshots),
            str(snapshots / old_revision),
            str(snapshots / FICTITIOUS_CANDIDATE.revision),
        })
        host.sizes[str(partial)] = 3
        self.assertIsNone(
            save_pull_record(
                host,
                PullRecord(retiring=(SnapshotRef(FICTITIOUS_CANDIDATE.role, FICTITIOUS_CANDIDATE.repo, old_revision),)),
            )
        )
        with redirect_stdout(StringIO()) as output:
            outcome = pull_profile(
                host,
                FICTITIOUS_SITE,
                FICTITIOUS_MODEL,
                _loaded_egress((ROOT / "config/egress.yaml").read_text()),
                candidates=(FICTITIOUS_CANDIDATE,),
                candidate=FICTITIOUS_CANDIDATE,
            )
        self.assertTrue(outcome.ok)
        self.assertIn(f"resumed {file.path}", output.getvalue())
        self.assertEqual(outcome.models[-1].files[0].action, "resumed")
        self.assertNotIn(str(partial), host.unlink_calls)

    def test_success_has_four_record_writes_and_second_run_is_present(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        covered = _loaded_egress((ROOT / "config" / "egress.yaml").read_text())
        with redirect_stdout(StringIO()) as output:
            outcome = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL, covered,
                candidates=(),
                now=lambda: "2026-09-04T12:00:00+00:00",
            )
        self.assertIsInstance(outcome, PullOutcome)
        self.assertTrue(outcome.ok)
        record_path = str(MODELS_ROOT / "gideon" / "pulls.yaml")
        record_writes = [write for write in host.writes if write[0] == record_path]
        self.assertEqual(len(record_writes), 4)
        records = [parse_pull_record(write[1]) for write in record_writes]
        self.assertTrue(all(isinstance(record, PullRecord) for record in records))
        first, second, third, fourth = cast(tuple[PullRecord, ...], tuple(records))
        self.assertIsNotNone(first.in_progress)
        self.assertEqual(second.retiring, ())
        self.assertIsNotNone(third.current)
        self.assertIsNone(fourth.in_progress)
        self.assertIn("fetched", output.getvalue())

        before_wget = sum(command[0] == "wget" for command in host.runs)
        second_outcome = pull_profile(
            host, FICTITIOUS_SITE, FICTITIOUS_MODEL, covered,
            candidates=(),
            now=lambda: "2026-09-04T12:01:00+00:00",
        )
        self.assertTrue(second_outcome.ok)
        self.assertEqual(sum(command[0] == "wget" for command in host.runs), before_wget)
        self.assertTrue(all(file.action == "verified" for file in second_outcome.models[0].files))

    def test_uncovered_host_and_wgetrc_refusal_leave_claim(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        with redirect_stdout(StringIO()) as output:
            outcome = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL,
                _loaded_egress(FICTITIOUS_EGRESS_TEXT),
                candidates=(),
                now=lambda: "2026-09-04T12:00:00+00:00",
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.uncovered, ("huggingface.co",))
        self.assertIn("uncovered host huggingface.co", output.getvalue())
        self.assertIn("; uncovered: huggingface.co", output.getvalue())

        failing = FakeHost({"/etc/gideon/secrets/proxy_auth": "not-a-pair"}, wget=_successful_wget)
        _add_process_files(failing)
        outcome = pull_profile(
            failing, FICTITIOUS_SITE, FICTITIOUS_MODEL,
            _loaded_egress(FICTITIOUS_EGRESS_TEXT),
            candidates=(),
            now=lambda: "2026-09-04T12:00:00+00:00",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.models, ())
        self.assertIn("must contain user:password", outcome.problem)
        self.assertFalse(any(command[0] == "wget" for command in failing.runs))

        def refused_wget(command: list[str], host: FakeHost) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 8, "", "HTTP/1.1 404 Not Found\n")

        failed = FakeHost(wget=refused_wget)
        _add_process_files(failed)
        outcome = pull_profile(
            failed, FICTITIOUS_SITE, FICTITIOUS_MODEL,
            _loaded_egress(FICTITIOUS_EGRESS_TEXT),
            candidates=(),
            now=lambda: "2026-09-04T12:00:00+00:00",
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.models[0].kind, "failed")
        record = load_pull_record(failed)
        self.assertIsInstance(record, PullRecord)
        assert isinstance(record, PullRecord)
        self.assertIsNotNone(record.in_progress)
        self.assertIsNone(record.current)

    def test_alive_claim_refuses_before_commands_and_root_is_required(self) -> None:
        host = FakeHost(wget=_successful_wget)
        _add_process_files(host)
        old = new_pull_entry(
            FICTITIOUS_MODEL.name, FICTITIOUS_MODEL.models,
            ProcessIdentity(os.getpid(), "boot-pull", 42),
            "2026-09-04T11:00:00+00:00",
        )
        save_pull_record(host, PullRecord(in_progress=old))
        host.runs.clear()
        outcome = pull_profile(
            host, FICTITIOUS_SITE, FICTITIOUS_MODEL,
            _loaded_egress(FICTITIOUS_EGRESS_TEXT), candidates=(), now=lambda: "now",
        )
        self.assertFalse(outcome.ok)
        self.assertIn("wait for it", outcome.problem)
        self.assertEqual(host.runs, [])

        error = StringIO()
        unprivileged = FakeHost()
        unprivileged.euid = 1000
        with redirect_stderr(error):
            code = run_models_pull(SimpleNamespace(), host=unprivileged)
        self.assertEqual(code, 1)
        self.assertIn("root is required", error.getvalue())
        self.assertIn("sudo python3 -m gideon models pull", error.getvalue())

    def test_command_boundary_loads_fictitious_inputs_and_returns_pull_status(self) -> None:
        files = {
            "/fixture/site.yaml": FICTITIOUS_SITE_TEXT,
            "/fixture/models.lock": FICTITIOUS_LOCK_TEXT,
            "/fixture/egress.yaml": FICTITIOUS_EGRESS_TEXT,
        }
        host = FakeHost(files)
        success = PullOutcome((), (), True)
        with mock.patch("gideon.host.weights.pull_profile", return_value=success) as pull:
            code = run_models_pull(
                SimpleNamespace(), host=host, site_path="/fixture/site.yaml",
                models_path="/fixture/models.lock", egress_path="/fixture/egress.yaml",
                root="/fixture",
            )
        self.assertEqual(code, 0)
        pull.assert_called_once()

        failure = PullOutcome((), (), False, "fictitious refusal", "fictitious fix")
        error = StringIO()
        with mock.patch("gideon.host.weights.pull_profile", return_value=failure), redirect_stderr(error):
            code = run_models_pull(
                SimpleNamespace(), host=host, site_path="/fixture/site.yaml",
                models_path="/fixture/models.lock", egress_path="/fixture/egress.yaml",
                root="/fixture",
            )
        self.assertEqual(code, 1)
        self.assertIn("fictitious refusal", error.getvalue())

    def test_failed_candidate_fix_retries_the_candidate_form(self) -> None:
        def blocked_candidate(
            command: list[str], host: FakeHost
        ) -> subprocess.CompletedProcess[str]:
            if any(FICTITIOUS_CANDIDATE.repo in part for part in command):
                return subprocess.CompletedProcess(command, 8, "", "")
            return _successful_wget(command, host)

        host = FakeHost(wget=blocked_candidate)
        _add_process_files(host)
        with redirect_stdout(StringIO()) as output:
            outcome = pull_profile(
                host, FICTITIOUS_SITE, FICTITIOUS_MODEL,
                _loaded_egress((ROOT / "config/egress.yaml").read_text()),
                candidates=(FICTITIOUS_CANDIDATE,), candidate=FICTITIOUS_CANDIDATE,
            )
        self.assertFalse(outcome.ok)
        retry = f"re-run sudo python3 -m gideon models pull {FICTITIOUS_CANDIDATE.name}."
        self.assertTrue(outcome.fix.endswith(retry), outcome.fix)
        self.assertIn(retry, output.getvalue())
        saved = load_pull_record(host)
        assert isinstance(saved, PullRecord)
        self.assertIsNone(saved.candidates[0].completed)

    def test_unknown_candidate_refuses_before_record_or_command(self) -> None:
        for lock_text, available in (
            (FICTITIOUS_LOCK_TEXT, "none"),
            (FICTITIOUS_LOCK_TEXT + FICTITIOUS_CANDIDATE_TEXT, FICTITIOUS_CANDIDATE.name),
        ):
            with self.subTest(available=available):
                host = FakeHost({
                    "/fixture/site.yaml": FICTITIOUS_SITE_TEXT,
                    "/fixture/models.lock": lock_text,
                })
                error = StringIO()
                with redirect_stderr(error):
                    code = run_models_pull(
                        SimpleNamespace(candidate="missing"),
                        host=host,
                        site_path="/fixture/site.yaml",
                        models_path="/fixture/models.lock",
                        egress_path="/fixture/egress.yaml",
                        root="/fixture",
                    )
                self.assertEqual(code, 1)
                self.assertIn(f"candidates held: {available}", error.getvalue())
                self.assertIn("candidates.missing", error.getvalue())
                self.assertIn("docs/runbooks/release-files.md §4", error.getvalue())
                self.assertIn("Fix:", error.getvalue())
                self.assertNotIn(str(PULL_RECORD_PATH), host.reads)
                self.assertEqual(host.runs, [])
                self.assertEqual(host.writes, [])


def _partial_for(pinned: PinnedFile) -> str:
    return str(blob_path(FICTITIOUS_PIN.repo, pinned.sha256).with_name(f"{bare_digest(pinned.sha256)}.partial"))


def _no_proxy_wgetrc() -> Wgetrc:
    return Wgetrc(None, None, "")


if __name__ == "__main__":
    unittest.main()

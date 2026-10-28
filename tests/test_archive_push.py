"""Archive pushes over fictitious pinned sources and a dict-backed host."""

import contextlib
import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest import mock

import test_backup_push
import test_models_lock
import yaml  # type: ignore[import-untyped]

from gideon.cli import main
from gideon.host import archive, backuplock, models, nogpu, report, weights
from gideon.host.checks.network import PROBE_RETRY_WAIT_SECONDS, PROBE_TIMEOUT_SECONDS
from gideon.host.corpus import lockfile
from gideon.host.corpus.sources import SOURCES
from gideon.host.sysio import Command, PathLike

CHECKOUT = Path("/fictitious/checkout")
LOCKFILES = CHECKOUT / "corpus/lockfiles"
SNAPSHOTS = Path("/fictitious/snapshots")
MODELS = Path("/fictitious/models")
RENDERED = Path("/fictitious/rendered")
SITE_PATH = Path("/fictitious/site.yaml")
CONFIG_PATH = Path("/fictitious/archive.conf")
NOW = datetime(2099, 1, 3, 4, 5, 6, tzinfo=UTC)
REMOTE = "fictitious-office:"
PROXY_AUTH = Path("/etc/gideon/secrets/proxy_auth")
PROXY = "http://proxy.example:3128"
PROXY_USER = "fictitious-user"
PROXY_PASSWORD = "fictitious-password"
SNAPSHOT_DATE = "2099-01-01"


def stats_text(files: int, byte_count: int) -> str:
    return json.dumps({"stats": {"transfers": files, "bytes": byte_count}}) + "\n"


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    input: str | None
    timeout: float | None
    selected: tuple[str, ...] = ()


class FakeHost:
    """A filesystem, remote file store, and recorded text command boundary."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()
        self.links: dict[str, str] = {}
        self.modes: dict[str, int] = {}
        self.owners: dict[str, int] = {}
        self.remote: dict[str, bytes] = {}
        self.calls: list[Call] = []
        self.writes: list[tuple[str, str, int]] = []
        self.reads: list[str] = []
        self.locks: dict[str, str] = {}
        self.claims: list[str] = []
        self.releases: list[str] = []
        self.sleeps: list[float] = []
        self.euid = 0
        self.remotes = REMOTE + "\n"
        self.failures: dict[str, tuple[int, str, str] | Exception] = {}
        self.probes: dict[str, list[tuple[int, str] | Exception]] = {}
        self.corrupt: set[str] = set()
        self.audit_down = False
        self.audit_write_fails = False
        self.audit_sql: list[str] = []
        self.stats_override: str | None = None

    def put(self, path: PathLike, data: bytes, *, mode: int = 0o644) -> None:
        key = str(path)
        self.files[key] = data
        self.modes[key] = mode
        self.directories.update(str(parent) for parent in Path(path).parents)

    def read_bytes(self, path: PathLike) -> bytes:
        key = str(path)
        self.reads.append(key)
        key = self.links.get(key, key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        self.writes.append((str(path), text, mode))
        self.put(path, text.encode(encoding), mode=mode)

    def exists(self, path: PathLike) -> bool:
        key = self.links.get(str(path), str(path))
        return key in self.files or key in self.directories

    def stat(self, path: PathLike) -> os.stat_result:
        key = self.links.get(str(path), str(path))
        if key in self.files:
            mode = stat.S_IFREG | self.modes.get(key, 0o644)
            size = len(self.files[key])
        elif key in self.directories:
            mode, size = stat.S_IFDIR | 0o755, 0
        else:
            raise FileNotFoundError(key)
        return os.stat_result((mode, 1, 1, 1, self.owners.get(key, 0), 0, size, 0, 0, 0))

    def listdir(self, path: PathLike) -> list[str]:
        if str(path) not in self.directories:
            raise FileNotFoundError(str(path))
        return sorted({Path(key).name for key in (*self.files, *self.directories, *self.links)
                       if Path(key).parent == Path(path)})

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        if str(path) not in self.files and not missing_ok:
            raise FileNotFoundError(str(path))
        self.files.pop(str(path), None)

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        self.directories.add(str(path))
        if parents:
            self.directories.update(str(parent) for parent in Path(path).parents)

    def chmod(self, path: PathLike, mode: int) -> None:
        self.stat(path)
        self.modes[str(path)] = mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.stat(path)
        self.owners[str(path)] = uid

    def geteuid(self) -> int:
        return self.euid

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = str(path)
        self.claims.append(key)
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        self.releases.append(str(path))
        self.locks.pop(str(path), None)

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        selected: tuple[str, ...] = ()
        if "--files-from" in command:
            selected = tuple(self.read_text(command[command.index("--files-from") + 1]).splitlines())
        self.calls.append(Call(command, dict(env or {}), input, timeout, selected))

        def result(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        probe = command[2:] if command[0] == "env" else command
        if probe[0] == "wget":
            outcomes = self.probes.get(probe[-1], [])
            outcome = outcomes.pop(0) if outcomes else (0, "")
            if isinstance(outcome, Exception):
                raise outcome
            return result(outcome[0], stderr=outcome[1])
        if command[0] == "docker" and "gideon_audit" in command:
            if input == "SELECT 1;\n":
                return result(int(self.audit_down))
            assert input is not None
            self.audit_sql.append(input)
            return result(int(self.audit_write_fails))
        if command[0] != "rclone":
            raise AssertionError(f"unexpected command: {command}")
        action = command[3]
        failure = self.failures.get(action)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return result(*failure)
        if action == "listremotes":
            return result(stdout=self.remotes)
        if action == "copy":
            local_root, remote_root = command[-2:]
            transferred: list[bytes] = []
            for name in selected:
                data = self.read_bytes(Path(local_root) / name)
                remote_file = f"{remote_root}/{name}"
                if self.remote.get(remote_file) != data:
                    self.remote[remote_file] = data
                    transferred.append(data)
            return result(stderr=self.stats_override if self.stats_override is not None else
                          stats_text(len(transferred), sum(map(len, transferred))))
        if action == "check":
            local_root, remote_root = command[-2:]
            differences = []
            for name in selected:
                remote_file = f"{remote_root}/{name}"
                matches = (remote_file not in self.corrupt
                           and self.remote.get(remote_file) == self.read_bytes(Path(local_root) / name))
                differences.append(f"{'=' if matches else '*'} {name}\n")
            self.write_text(command[command.index("--combined") + 1], "".join(differences), mode=0o600)
            failed = any(line.startswith("*") for line in differences)
            return result(int(failed), stdout="comparison report" if failed else "",
                          stderr="hash comparison failed" if failed else "")
        if action == "lsf":
            prefix = command[-1] + "/"
            return result(stdout="".join(f"{path.removeprefix(prefix)};{len(data)}\n"
                                         for path, data in sorted(self.remote.items()) if path.startswith(prefix)))
        raise AssertionError(f"unexpected rclone action: {action}")

    def rclone_calls(self, action: str) -> list[Call]:
        return [call for call in self.calls if call.argv[0] == "rclone" and call.argv[3] == action]


def make_host() -> FakeHost:
    host = FakeHost()
    host.put(SITE_PATH, test_backup_push.SITE.encode())
    host.put(nogpu.BUILD_BOX_PATH, b"Fictitious build box\n")
    host.put(CONFIG_PATH, b"Fictitious credential never read by Python\n", mode=0o600)
    host.put("/usr/bin/rclone", b"")
    groups = {
        name: [{"host": f"{name}.example", "probe_url": f"https://{name}.example/"}]
        for name in ("host-provisioning", "install-upgrade", "corpus", "image-build")
    }
    groups["archive"] = [
        {"host": "api.archive.example", "probe_url": "https://api.archive.example/"},
        {"host": "upload.archive.example", "probe_url": "https://upload.archive.example/"},
    ]
    host.put(CHECKOUT / "config/egress.yaml", yaml.safe_dump({"version": 1000, "groups": groups}).encode())
    pins: dict[str, lockfile.SourcePin] = {}
    for source in SOURCES:
        contents = {source.captured_file: b"Fictitious saved source\n"} if source.captured_file else {
            "objects/first.bin": b"Fictitious first object\n",
            "objects/second.bin": b"Fictitious second object\n",
        }
        entries = tuple(lockfile.SidecarEntry(path, hashlib.sha256(data).hexdigest(), len(data))
                        for path, data in sorted(contents.items()))
        sidecar = lockfile.render_sidecar(entries).encode()
        index_name = source.captured_file or "listing.xml"
        index_data = contents[index_name] if source.captured_file else b"<fictitious-listing/>\n"
        url = f"https://sources.example/{source.name}/{index_name}"
        pins[source.name] = lockfile.SourcePin(
            SNAPSHOT_DATE, url, None, hashlib.sha256(sidecar).hexdigest(), len(entries),
            sum(entry.size for entry in entries),
            (lockfile.IndexDocument(index_name, url, hashlib.sha256(index_data).hexdigest(), len(index_data)),),
            entries, ("ca6",) if source.carries_courts else None, source.captured,
        )
        for path, data in contents.items():
            host.put(SNAPSHOTS / f"{source.name}-{SNAPSHOT_DATE}" / path, data)
        host.put(SNAPSHOTS / f"{source.name}-{SNAPSHOT_DATE}" / ".fetch.json", b"Fictitious fetch record\n")
        for label in ("corpus-2099-01-02", "corpus-2099-01-03"):
            host.put(LOCKFILES / label / f"{source.name}.sha256", sidecar)
            host.put(LOCKFILES / label / f"{source.name}.{index_name}", index_data)
    for label in ("corpus-2099-01-02", "corpus-2099-01-03"):
        cut = lockfile.Lockfile(lockfile.SCHEMA_VERSION, label, "1000.0.0", "2099-01-03T04:05:06Z", "tranche", pins)
        host.put(LOCKFILES / f"{label}.yaml", lockfile.render_lockfile(cut).encode())
    host.put(LOCKFILES / "notes/info.txt", b"Fictitious archive note\n")
    host.put(LOCKFILES / "corpus-2099-01-04/unfinished.txt", b"Fictitious unfinished cut\n")
    host.put(SNAPSHOTS / "resolve/listing.xml", b"Fictitious resolve record\n")

    document = yaml.safe_load(test_models_lock.VALID)
    profile = document["profiles"][document["reference"]]
    model = profile["models"]["generator"]
    model["repo"] = "example/shared-archive-model"
    model["files"] = {
        "nested/tokenizer.json": {"sha256": "sha256:" + "e" * 64, "size": 19},
        "weights.bin": {"sha256": "sha256:" + "d" * 64, "size": 17},
    }
    other = copy.deepcopy(profile)
    absent = copy.deepcopy(model)
    absent.update(repo="example/absent-archive-model", revision="b" * 40, gpu=1)
    other["models"]["embed"] = absent
    document["profiles"]["2x1000v-2000d"] = other
    candidate = yaml.safe_load(test_models_lock.CANDIDATE)
    document.update(candidate)
    for body in (model, other["models"]["generator"], absent, *document["candidates"].values()):
        for entry in body["files"].values():
            entry["sha256"] = "sha256:" + hashlib.sha256(b"x" * entry["size"]).hexdigest()
    host.put(CHECKOUT / "models.lock", yaml.safe_dump(document, sort_keys=False).encode())
    loaded = models.load_models_lock(CHECKOUT / "models.lock", host=host)
    assert loaded.lock is not None, loaded.errors
    shared = loaded.lock.profiles[0].models[0]
    cached_pins: tuple[models.ModelPin | models.CandidatePin, ...] = (shared, *loaded.lock.candidates)
    for pin in cached_pins:
        directory = weights.repository_dir(pin.repo, MODELS) / "snapshots" / pin.revision
        for entry in pin.files:
            blob = weights.blob_path(pin.repo, entry.sha256, MODELS)
            host.put(blob, b"x" * entry.size)
            link = str(directory / entry.path)
            host.links[link] = str(blob)
            host.directories.update(str(parent) for parent in Path(link).parents)
    missing_pin = shared.files[-1]
    host.links.pop(str(weights.snapshot_path(shared.repo, shared.revision, missing_pin.path, MODELS)))
    host.remote[f"{REMOTE}models/{shared.repo}/{shared.revision}/{missing_pin.path}"] = b"r" * missing_pin.size
    source = next(source for source in SOURCES if not source.captured)
    missing_path = SNAPSHOTS / f"{source.name}-{SNAPSHOT_DATE}" / "objects/second.bin"
    data = host.files.pop(str(missing_path))
    host.remote[f"{REMOTE}corpus/snapshots/{missing_path.relative_to(SNAPSHOTS)}"] = data
    return host


def invoke(host: FakeHost, *, installed: bool = False) -> tuple[int, str, str]:
    runner = partial(
        archive.run_archive_push, host=host, rendered_dir=RENDERED, site_path=SITE_PATH,
        root=CHECKOUT, snapshots_root=SNAPSHOTS, models_root=MODELS, config_path=CONFIG_PATH,
        now=NOW, sleep=host.sleeps.append,
    )
    out, err = io.StringIO(), io.StringIO()
    environment = {report.GIDEON_INSTALLED_COMMAND: "1"} if installed else {}
    with (
        mock.patch.dict(os.environ, environment, clear=True),
        mock.patch.object(archive, "run_archive_push", wraps=runner),
        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
    ):
        code = main(["archive", "push"])
    return code, out.getvalue(), err.getvalue()


def detail(sql: str) -> dict[str, object]:
    line = next(line for line in sql.splitlines() if line.startswith("\\set v_detail "))
    return json.loads(line[len("\\set v_detail '"):-1].replace("''", "'").replace("\\\\", "\\"))


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(report.set_installed_form, False)

    def loaded(self, host: FakeHost) -> tuple[tuple[lockfile.Lockfile, ...], models.ModelsLock]:
        directory = lockfile.read_lockfile_directory(LOCKFILES, host=host)
        self.assertEqual(directory.errors, ())
        loaded = models.load_models_lock(CHECKOUT / "models.lock", host=host)
        self.assertEqual(loaded.errors, ())
        assert loaded.lock is not None
        return directory.lockfiles, loaded.lock

    def assert_no_copy(self, host: FakeHost) -> None:
        self.assertEqual(host.rclone_calls("copy"), [])
        self.assertEqual(host.audit_sql, [])

    def test_preconditions_refuse_before_copy_in_both_command_forms(self) -> None:
        cases = (
            ("root", lambda host: setattr(host, "euid", 1000), "root privileges", "archive push"),
            ("marker", lambda host: host.files.pop(str(nogpu.BUILD_BOX_PATH)), "not the build box", "host provision --build-box"),
            ("conflicting markers", lambda host: host.put(nogpu.NO_GPU_PATH, b"Fictitious mode"), "not the build box", "host provision --build-box"),
            ("credential missing", lambda host: host.files.pop(str(CONFIG_PATH)), "credential file is unavailable", "docs/runbooks/release-files.md §10"),
            ("credential owner", lambda host: host.owners.update({str(CONFIG_PATH): 1000}), "root-owned", "docs/runbooks/release-files.md §10"),
            ("credential mode", lambda host: host.modes.update({str(CONFIG_PATH): 0o640}), "wider than 0600", "chmod 0600"),
            ("rclone", lambda host: host.files.pop("/usr/bin/rclone"), "tool is missing", "host provision --only archive-tools"),
            ("no remote", lambda host: setattr(host, "remotes", ""), "exactly one remote", "docs/runbooks/release-files.md §10"),
            ("two remotes", lambda host: setattr(host, "remotes", "first:\nsecond:\n"), "exactly one remote", "docs/runbooks/release-files.md §10"),
            ("site", lambda host: host.put(SITE_PATH, b"[]"), "site", "Fix:"),
            ("egress", lambda host: host.put(CHECKOUT / "config/egress.yaml", b"[]"), "egress", "config/egress.yaml"),
            ("models", lambda host: host.put(CHECKOUT / "models.lock", b"[]"), "models.lock", "release-files.md"),
            ("lockfile", lambda host: host.put(LOCKFILES / "corpus-2099-01-02.yaml", b"[]"), "lockfile root", "Restore the lockfile"),
            ("record", lambda host: setattr(host, "audit_down", True), "audit writer is unavailable", "logs postgres"),
        )
        for installed in (False, True):
            for name, change, problem, fix in cases:
                with self.subTest(case=name, installed=installed):
                    host = make_host()
                    change(host)
                    code, out, err = invoke(host, installed=installed)
                    self.assertEqual(code, 1, out + err)
                    self.assertEqual(out, "")
                    self.assertIn("preconditions: refuse", err)
                    self.assertIn(problem, err)
                    self.assertIn(fix, err)
                    self.assertNotIn("internal error", err)
                    self.assert_no_copy(host)
                    self.assertEqual(host.locks, {})
                    if name == "root":
                        form = "gideon" if installed else "sudo python3 -m gideon"
                        self.assertIn(f"Run {form} archive push as root, then retry.", err)
                        self.assertEqual(host.claims, [])
                    else:
                        self.assertEqual(host.releases, [backuplock.ARCHIVE_LOCK.path])
                    if installed:
                        self.assertNotIn("python3 -m gideon", err)

    def test_missing_artifacts_and_companions_use_the_loader_fixes(self) -> None:
        host = make_host()
        first = self.loaded(host)[0][0]
        paths = [SITE_PATH, CHECKOUT / "config/egress.yaml", CHECKOUT / "models.lock"]
        paths.extend(LOCKFILES / first.label / f"{source}.sha256" for source in first.sources)
        for path in paths:
            with self.subTest(path=path):
                host = make_host()
                host.files.pop(str(path))
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertIn("preconditions: refuse", err)
                self.assertIn("Fix:", err)
                self.assertIn(path.name, err)
                self.assertNotIn("internal error", err)
                self.assert_no_copy(host)

    def test_archive_group_must_exist_and_have_hosts(self) -> None:
        for empty in (False, True):
            with self.subTest(empty=empty):
                host = make_host()
                document = yaml.safe_load(host.read_text(CHECKOUT / "config/egress.yaml"))
                if empty:
                    document["groups"]["archive"] = []
                else:
                    del document["groups"]["archive"]
                host.put(CHECKOUT / "config/egress.yaml", yaml.safe_dump(document).encode())
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertIn("archive", err)
                self.assertIn("config/egress.yaml", err)
                self.assert_no_copy(host)

    def test_lock_refusal_preserves_the_holder_and_never_takes_the_corpus_lock(self) -> None:
        for record in (backuplock.Record("gideon archive push", os.getpid() + 1, NOW).to_json(), "unreadable holder"):
            with self.subTest(record=record):
                host = make_host()
                host.locks[backuplock.ARCHIVE_LOCK.path] = record
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertEqual(out, "")
                self.assertIn("archive push lock is held", err)
                self.assertIn("Wait", err)
                if record.startswith("{"):
                    self.assertIn(f"ps -p {os.getpid() + 1}", err)
                    self.assertIn("gideon archive push", err)
                self.assertEqual(host.locks, {backuplock.ARCHIVE_LOCK.path: record})
                self.assertEqual(host.claims, [backuplock.ARCHIVE_LOCK.path])
                self.assertEqual(host.releases, [])
                self.assert_no_copy(host)

    def test_listremotes_failures_are_refusals_even_with_plausible_stdout(self) -> None:
        for failure in ((1, REMOTE + "\n", "Fictitious list failure"), (127, "", "rclone unavailable"), OSError("cannot execute rclone")):
            with self.subTest(failure=failure):
                host = make_host()
                host.failures["listremotes"] = failure
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertIn("preconditions: refuse", err)
                self.assertIn("Fix:", err)
                self.assertEqual(len(host.rclone_calls("listremotes")), 1)
                self.assert_no_copy(host)
                self.assertEqual(host.locks, {})

    def test_inventory_uses_pins_and_regular_lockfiles_and_deduplicates_revisions(self) -> None:
        host = make_host()
        cuts, model_lock = self.loaded(host)
        selected = archive.inventory(cuts, model_lock, LOCKFILES, host=host,
                                     snapshots_root=SNAPSHOTS, models_root=MODELS)
        self.assertEqual(len(cuts), 2)
        self.assertEqual(len(model_lock.profiles), 2)
        self.assertEqual(len(model_lock.candidates), 1)
        self.assertEqual(selected.lockfiles, tuple(cut.label for cut in cuts))
        snapshots, lockfiles_root, *model_roots = selected.roots
        self.assertEqual((snapshots.name, snapshots.local_root, snapshots.remote_root, snapshots.follow_links),
                         ("snapshots", SNAPSHOTS, "corpus/snapshots", False))
        expected_snapshots = {f"{name}-{pin.snapshot_date}" for cut in cuts for name, pin in cut.sources.items()}
        self.assertEqual(set(selected.snapshots), expected_snapshots)
        expected_files = {
            f"{name}-{pin.snapshot_date}/{entry.path}": entry.size
            for cut in cuts for name, pin in cut.sources.items() for entry in pin.entries
        }
        self.assertEqual({file.path: file.size for file in snapshots.files}, expected_files)
        missing = [file for file in snapshots.files if not file.present]
        self.assertEqual(len(missing), 1)
        self.assertEqual(len(snapshots.files), len(expected_files))
        self.assertTrue(all(".fetch.json" not in file.path and not file.path.startswith("resolve/")
                            for file in snapshots.files))
        self.assertEqual((lockfiles_root.local_root, lockfiles_root.remote_root, lockfiles_root.follow_links),
                         (LOCKFILES, "corpus/lockfiles", False))
        expected_lockfiles = {str(Path(path).relative_to(LOCKFILES)): len(data)
                              for path, data in host.files.items() if Path(path).is_relative_to(LOCKFILES)}
        self.assertEqual({file.path: file.size for file in lockfiles_root.files}, expected_lockfiles)
        self.assertTrue(all(file.present for file in lockfiles_root.files))
        self.assertIn("corpus-2099-01-04/unfinished.txt", expected_lockfiles)
        shared = model_lock.profiles[0].models[0]
        absent = model_lock.profiles[1].models[1]
        candidate = model_lock.candidates[0]
        self.assertEqual(selected.absent_models, (f"{absent.repo}@{absent.revision}",))
        expected_models = {f"{pin.repo}@{pin.revision}" for pin in (shared, candidate)}
        self.assertEqual(set(selected.models), expected_models)
        self.assertEqual({root.name for root in model_roots}, expected_models)
        self.assertEqual(len(model_roots), len(expected_models))
        for root in model_roots:
            pin = next(pin for pin in (shared, candidate) if root.name == f"{pin.repo}@{pin.revision}")
            self.assertEqual(root.local_root, weights.repository_dir(pin.repo, MODELS) / "snapshots" / pin.revision)
            self.assertEqual(root.remote_root, f"models/{pin.repo}/{pin.revision}")
            self.assertTrue(root.follow_links)
            self.assertEqual({file.path: file.size for file in root.files}, {entry.path: entry.size for entry in pin.files})
            self.assertEqual(sum(not file.present for file in root.files), int(pin == shared))
        self.assertNotIn(str(CONFIG_PATH), host.reads)

    def test_remote_paths_match_the_archive_layout(self) -> None:
        for remote in (REMOTE, REMOTE.rstrip(":")):
            self.assertEqual(archive.remote_path(remote, "corpus/snapshots", "example-2099-01-01/objects/first.bin"),
                             f"{REMOTE}corpus/snapshots/example-2099-01-01/objects/first.bin")
            self.assertEqual(archive.remote_path(remote, "corpus/lockfiles/", "notes/info.txt"),
                             f"{REMOTE}corpus/lockfiles/notes/info.txt")
            self.assertEqual(archive.remote_path(remote, "models/example/repo/revision"),
                             f"{REMOTE}models/example/repo/revision")

    def test_success_prints_six_rows_and_writes_one_complete_audit_record(self) -> None:
        host = make_host()
        cuts, model_lock = self.loaded(host)
        initial_remote = dict(host.remote)
        code, out, err = invoke(host)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual([line.split(":", 1)[0] for line in out.splitlines()],
                         ["preconditions", "probe", "inventory", "copy", "check", "audit"])
        self.assertTrue(all(": ok" in line for line in out.splitlines()))
        absent = model_lock.profiles[1].models[1]
        self.assertIn(f"{absent.repo}@{absent.revision}", out)
        self.assertEqual(len(host.audit_sql), 1)
        sql = host.audit_sql[0]
        self.assertIn("\\set v_kind 'archive_push'", sql)
        row = detail(sql)
        self.assertEqual(set(row), {
            "lockfiles", "snapshots", "models", "absent_models", "files_sent", "bytes_sent",
            "files_checked", "bytes_total", "missing_checked", "duration_s",
        })
        self.assertEqual(row["lockfiles"], [cut.label for cut in cuts])
        self.assertEqual(row["snapshots"], sorted({f"{name}-{pin.snapshot_date}" for cut in cuts for name, pin in cut.sources.items()}))
        held: tuple[models.ModelPin | models.CandidatePin, ...] = (
            model_lock.profiles[0].models[0], model_lock.candidates[0],
        )
        self.assertEqual(row["models"], sorted(f"{pin.repo}@{pin.revision}" for pin in held))
        self.assertEqual(row["absent_models"], [f"{absent.repo}@{absent.revision}"])
        sent = {path: data for path, data in host.remote.items() if path not in initial_remote}
        self.assertGreater(len(sent), 0)
        self.assertEqual(row["files_sent"], len(sent))
        self.assertEqual(row["bytes_sent"], sum(map(len, sent.values())))
        self.assertEqual(row["files_checked"], len(host.remote))
        self.assertEqual(row["bytes_total"], sum(map(len, host.remote.values())))
        self.assertEqual(row["missing_checked"], len(initial_remote))
        self.assertEqual(row["duration_s"], 0)
        self.assertNotIn(str(CONFIG_PATH), host.reads)
        self.assertNotIn("Fictitious credential", out + err + sql)
        self.assertEqual(host.locks, {})
        self.assertEqual(host.claims, [backuplock.ARCHIVE_LOCK.path])
        self.assertEqual(host.releases, [backuplock.ARCHIVE_LOCK.path])

    def test_transfer_argv_file_lists_and_model_symlinks(self) -> None:
        host = make_host()
        cuts, model_lock = self.loaded(host)
        code, out, err = invoke(host)
        self.assertEqual(code, 0, out + err)
        copies, checks = host.rclone_calls("copy"), host.rclone_calls("check")
        shared = model_lock.profiles[0].models[0]
        pins: tuple[models.ModelPin | models.CandidatePin, ...] = (shared, *model_lock.candidates)
        expected_roots = {
            str(SNAPSHOTS): f"{REMOTE}corpus/snapshots",
            str(LOCKFILES): f"{REMOTE}corpus/lockfiles",
            **{str(weights.repository_dir(pin.repo, MODELS) / "snapshots" / pin.revision):
               f"{REMOTE}models/{pin.repo}/{pin.revision}" for pin in pins},
        }
        self.assertEqual({call.argv[-2]: call.argv[-1] for call in copies}, expected_roots)
        self.assertEqual({call.argv[-2]: call.argv[-1] for call in checks}, expected_roots)
        for call in (*copies, *checks):
            argv = call.argv
            self.assertTrue(call.selected)
            self.assertIn("--files-from", argv)
            self.assertEqual("--copy-links" in argv, argv[-1].startswith(f"{REMOTE}models/"))
            file_list = argv[argv.index("--files-from") + 1]
            written = [item for item in host.writes if item[0] == file_list]
            self.assertEqual(written[0][2], 0o600)
            self.assertEqual(tuple(written[0][1].splitlines()), call.selected)
            self.assertNotIn(file_list, host.files)
            self.assertTrue(all(host.exists(Path(argv[-2]) / path) for path in call.selected))
            self.assertGreater(call.timeout or 0, 0)
            if argv[3] == "copy":
                self.assertIn("--stats-one-line", argv)
                self.assertIn("-v", argv)
                self.assertIn("--no-update-modtime", argv)
            else:
                self.assertIn("--one-way", argv)
                self.assertNotIn("--download", argv)
        for root, remote in expected_roots.items():
            copied = next(call for call in copies if call.argv[-2] == root)
            checked = next(call for call in checks if call.argv[-2] == root)
            self.assertEqual(copied.selected, checked.selected)
            self.assertTrue(all(f"{remote}/{path}" in host.remote for path in copied.selected))
        snapshot_copy = next(call for call in copies if call.argv[-2] == str(SNAPSHOTS))
        expected = {f"{name}-{pin.snapshot_date}/{entry.path}" for cut in cuts for name, pin in cut.sources.items()
                    for entry in pin.entries if host.exists(SNAPSHOTS / f"{name}-{pin.snapshot_date}" / entry.path)}
        self.assertEqual(set(snapshot_copy.selected), expected)
        self.assertTrue(all("fetch" not in path and not path.startswith("resolve/") for path in snapshot_copy.selected))
        for call in host.rclone_calls("lsf"):
            self.assertEqual(call.argv[4:8], ("--format", "ps", "-R", "--files-only"))
        listings = [call.argv[-1] for call in host.rclone_calls("lsf")]
        self.assertEqual(len(listings), len(set(listings)))
        self.assertFalse(any(path.endswith(".list") for path in host.files))

    def test_every_rclone_call_has_config_and_proxy_only_in_the_environment(self) -> None:
        host = make_host()
        host.put(SITE_PATH, (test_backup_push.SITE + f"egress_proxy: {PROXY}\n").encode())
        host.put(PROXY_AUTH, f"{PROXY_USER}:{PROXY_PASSWORD}\n".encode(), mode=0o440)
        code, out, err = invoke(host)
        self.assertEqual(code, 0, out + err)
        calls = [call for call in host.calls if call.argv[0] == "rclone"]
        self.assertEqual({call.argv[3] for call in calls}, {"listremotes", "copy", "check", "lsf"})
        proxy = f"http://{PROXY_USER}:{PROXY_PASSWORD}@proxy.example:3128"
        for call in calls:
            self.assertEqual(call.argv[:3], ("rclone", "--config", str(CONFIG_PATH)))
            self.assertEqual(call.env["HTTP_PROXY"], proxy)
            self.assertEqual(call.env["HTTPS_PROXY"], proxy)
            self.assertIn("NO_PROXY", call.env)
            self.assertNotIn(PROXY_USER, " ".join(call.argv))
            self.assertNotIn(PROXY_PASSWORD, " ".join(call.argv))
            self.assertNotIn(PROXY, " ".join(call.argv))
        probes = [call for call in host.calls if call.argv[0] == "env"]
        self.assertEqual(len(probes), 2)
        for call in probes:
            self.assertTrue(call.argv[1].startswith("WGETRC="))
            path = call.argv[1].removeprefix("WGETRC=")
            written = next(item for item in host.writes if item[0] == path)
            self.assertEqual(written[2], 0o600)
            self.assertIn(f"proxy_user = {PROXY_USER}", written[1])
            self.assertIn(f"proxy_password = {PROXY_PASSWORD}", written[1])
            self.assertNotIn(path, host.files)
            self.assertNotIn(PROXY_PASSWORD, " ".join(call.argv))
        self.assertNotIn(PROXY_PASSWORD, out + err + "".join(host.audit_sql))

    def test_second_run_sends_nothing_and_checks_every_file_again(self) -> None:
        host = make_host()
        code, out, err = invoke(host)
        self.assertEqual(code, 0, out + err)
        archived = dict(host.remote)
        checks = len(host.rclone_calls("check"))
        code, out, err = invoke(host)
        self.assertEqual(code, 0, out + err)
        self.assertIn("copy: ok — sent 0 files, 0 bytes", out)
        self.assertEqual(host.remote, archived)
        self.assertEqual(len(host.rclone_calls("check")), checks * 2)
        self.assertEqual(len(host.audit_sql), 2)
        second = detail(host.audit_sql[-1])
        self.assertEqual(second["files_sent"], 0)
        self.assertEqual(second["bytes_sent"], 0)
        self.assertEqual(second["files_checked"], len(archived))

    def test_probe_status_verdicts_and_entry_specific_expectations(self) -> None:
        cases = (
            (0, "", None, True),
            (6, "HTTP/1.1 401 Unauthorized", None, True),
            (8, "HTTP/1.1 401 Unauthorized", None, True),
            (8, "HTTP/1.1 404 Not Found", None, True),
            (8, "HTTP/1.1 403 Forbidden", None, False),
            (8, "HTTP/1.1 407 Proxy Authentication Required", None, False),
            (8, "HTTP/1.1 503 Unavailable", None, False),
            (8, "HTTP/1.1 401 Unauthorized\nHTTP/1.1 403 Forbidden", None, False),
            (8, "", None, False),
            (8, "HTTP/1.1 403 Forbidden", [403], True),
        )
        for returncode, stderr, expect, passes in cases:
            with self.subTest(returncode=returncode, stderr=stderr, expect=expect):
                host = make_host()
                url = "https://api.archive.example/"
                host.probes[url] = [(returncode, stderr)]
                if expect is not None:
                    document = yaml.safe_load(host.read_text(CHECKOUT / "config/egress.yaml"))
                    document["groups"]["archive"][0]["expect"] = expect
                    host.put(CHECKOUT / "config/egress.yaml", yaml.safe_dump(document).encode())
                code, out, err = invoke(host)
                self.assertEqual(code, 0 if passes else 1, out + err)
                self.assertEqual(err, "")
                self.assertEqual(host.sleeps, [])
                probes = [call for call in host.calls if call.argv[0] == "wget"]
                self.assertEqual(len(probes), 2)
                for call in probes:
                    self.assertIn("--tries=1", call.argv)
                    self.assertIn("-S", call.argv)
                    self.assertIn(f"--connect-timeout={PROBE_TIMEOUT_SECONDS}", call.argv)
                    self.assertIn(f"--read-timeout={PROBE_TIMEOUT_SECONDS}", call.argv)
                if not passes:
                    self.assertIn("probe: refuse", out)
                    self.assertIn("api.archive.example", out)
                    self.assertIn("firewall", out)
                    self.assertIn("egress_proxy", out)
                    self.assertIn("docs/runbooks/release-files.md §5", out)
                    self.assert_no_copy(host)

    def test_probe_retries_only_unanswered_hosts_once_after_one_wait(self) -> None:
        for recovers in (False, True):
            with self.subTest(recovers=recovers):
                host = make_host()
                urls = ("https://api.archive.example/", "https://upload.archive.example/")
                for url in urls:
                    host.probes[url] = [(4, ""), (0, "") if recovers else (5, "")]
                code, out, err = invoke(host)
                self.assertEqual(code, 0 if recovers else 1, out + err)
                self.assertEqual(host.sleeps, [PROBE_RETRY_WAIT_SECONDS])
                probes = [call for call in host.calls if call.argv[0] == "wget"]
                self.assertEqual(len(probes), 4)
                self.assertEqual(probes[0].argv, probes[2].argv)
                self.assertEqual(probes[1].argv, probes[3].argv)
                self.assertIn("first no response" if recovers else "no response, then no response", out)
                if not recovers:
                    self.assert_no_copy(host)

    def test_probe_retry_leaves_answered_hosts_alone_and_names_a_recovery_on_failure(self) -> None:
        host = make_host()
        host.probes["https://api.archive.example/"] = [(4, ""), (0, "")]
        host.probes["https://upload.archive.example/"] = [(8, "HTTP/1.1 403 Forbidden")]
        code, out, err = invoke(host)
        self.assertEqual(code, 1, out + err)
        probes = [call.argv[-1] for call in host.calls if call.argv[0] == "wget"]
        self.assertEqual(probes.count("https://api.archive.example/"), 2)
        self.assertEqual(probes.count("https://upload.archive.example/"), 1)
        self.assertIn("upload.archive.example (HTTP 403)", out)
        self.assertIn("api.archive.example (first no response)", out)
        self.assert_no_copy(host)

    def test_probe_tool_failures_never_retry_or_copy(self) -> None:
        for failure in ((127, "wget unavailable"), (126, "wget not executable"), OSError("cannot execute wget")):
            with self.subTest(failure=failure):
                host = make_host()
                host.probes["https://api.archive.example/"] = [failure]
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertIn("probe: refuse", out)
                self.assertIn("Install or repair wget", out)
                self.assertEqual(host.sleeps, [])
                self.assertEqual(len([call for call in host.calls if call.argv[0] == "wget"]), 1)
                self.assert_no_copy(host)
                self.assertEqual(host.locks, {})

    def test_probe_and_copy_retry_fixes_use_the_invocation_form(self) -> None:
        for installed in (False, True):
            for stage in ("probe", "copy"):
                with self.subTest(installed=installed, stage=stage):
                    host = make_host()
                    if stage == "probe":
                        host.probes["https://api.archive.example/"] = [(8, "HTTP/1.1 403 Forbidden")]
                    else:
                        host.failures["copy"] = (1, "", "Fictitious transfer refusal")
                    code, out, err = invoke(host, installed=installed)
                    self.assertEqual(code, 1, out + err)
                    form = "gideon" if installed else "sudo python3 -m gideon"
                    self.assertIn(f"{form} archive push", out)
                    if installed:
                        self.assertNotIn("python3 -m gideon", out)
                    if stage == "probe":
                        self.assertIn("docs/runbooks/release-files.md §5", out)
                        self.assert_no_copy(host)
                    self.assertEqual(host.audit_sql, [])

    def test_failed_copy_uses_both_streams_and_stops_before_check_and_audit(self) -> None:
        for failure in ((1, "Fictitious stdout", "Fictitious stderr"), (127, "", "rclone unavailable"), OSError("cannot execute rclone")):
            with self.subTest(failure=failure):
                host = make_host()
                host.failures["copy"] = failure
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertIn("copy: refuse", out)
                self.assertIn("Fix:", out)
                if isinstance(failure, tuple) and failure[0] == 1:
                    self.assertIn(failure[1], out)
                    self.assertIn(failure[2], out)
                self.assertEqual(len(host.rclone_calls("copy")), 1)
                self.assertEqual(host.rclone_calls("check"), [])
                self.assertEqual(host.audit_sql, [])
                self.assertEqual(host.locks, {})
                self.assertFalse(any(path.endswith(".list") for path in host.files))

    def test_failed_child_is_not_passed_by_a_plausible_stats_body(self) -> None:
        host = make_host()
        host.failures["copy"] = (1, "", stats_text(100, 1000))
        code, out, err = invoke(host)
        self.assertEqual(code, 1, out + err)
        self.assertIn("copy: refuse", out)
        self.assertEqual(host.audit_sql, [])

    def test_unreadable_copy_stats_refuse_instead_of_recording_zero(self) -> None:
        host = make_host()
        host.stats_override = "Fictitious diagnostic without counters\n"
        code, out, err = invoke(host)
        self.assertEqual(code, 1, out + err)
        self.assertIn("transfer stats are unreadable", out)
        self.assertEqual(host.rclone_calls("check"), [])
        self.assertEqual(host.audit_sql, [])

    def test_check_differences_name_the_files_and_keep_both_child_streams(self) -> None:
        host = make_host()
        cuts, _ = self.loaded(host)
        source_name, pin = next(iter(cuts[0].sources.items()))
        entry = pin.entries[0]
        path = f"{source_name}-{pin.snapshot_date}/{entry.path}"
        host.corrupt.add(f"{REMOTE}corpus/snapshots/{path}")
        code, out, err = invoke(host)
        self.assertEqual(code, 1, out + err)
        self.assertIn("check: refuse", out)
        self.assertIn(f"differences: {path}", out)
        self.assertIn("hash comparison failed", out)
        self.assertIn("comparison report", out)
        self.assertEqual(host.audit_sql, [])
        self.assertEqual(host.locks, {})
        self.assertFalse(any(path.endswith(".list") for path in host.files))

    def test_missing_files_require_a_remote_copy_at_the_pinned_size_in_both_forms(self) -> None:
        for installed in (False, True):
            form = "gideon" if installed else "sudo python3 -m gideon"
            for kind in ("snapshot", "model", "candidate"):
                for state in ("matching", "absent", "wrong size"):
                    with self.subTest(installed=installed, kind=kind, state=state):
                        host = make_host()
                        _, model_lock = self.loaded(host)
                        fix = f"{form} models pull"
                        if kind == "snapshot":
                            remote_file = next(path for path in host.remote if ":corpus/snapshots/" in path)
                            fix = f"{form} corpus cut"
                        elif kind == "model":
                            remote_file = next(path for path in host.remote if ":models/" in path)
                        else:
                            pin = model_lock.candidates[0]
                            entry = next(entry for entry in pin.files if entry.size > 0)
                            host.links.pop(str(weights.snapshot_path(pin.repo, pin.revision, entry.path, MODELS)))
                            remote_file = f"{REMOTE}models/{pin.repo}/{pin.revision}/{entry.path}"
                            host.remote[remote_file] = b"r" * entry.size
                            fix += f" {pin.name}"
                        if state == "absent":
                            host.remote.pop(remote_file)
                        elif state == "wrong size":
                            host.remote[remote_file] += b"x"
                        code, out, err = invoke(host, installed=installed)
                        self.assertEqual(code, 0 if state == "matching" else 1, out + err)
                        self.assertEqual(err, "")
                        if state == "matching":
                            self.assertEqual(len(host.audit_sql), 1)
                        else:
                            self.assertIn("check: refuse", out)
                            self.assertIn(Path(remote_file).name, out)
                            self.assertIn(fix, out)
                            self.assertIn(f"{form} archive push", out)
                            self.assertEqual(host.audit_sql, [])
                            if kind == "snapshot":
                                self.assertIn("another holder", out)
                        if installed:
                            self.assertNotIn("python3 -m gideon", out)

    def test_oauth_and_invalid_grant_failures_name_reauthorisation_in_both_forms(self) -> None:
        for installed in (False, True):
            for diagnostic in ("Fictitious OAuth refresh refusal", "Fictitious invalid_grant refusal"):
                with self.subTest(installed=installed, diagnostic=diagnostic):
                    host = make_host()
                    host.failures["copy"] = (1, "", diagnostic)
                    code, out, err = invoke(host, installed=installed)
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("docs/runbooks/release-files.md §10", out)
                    form = "gideon" if installed else "sudo python3 -m gideon"
                    self.assertIn(f"{form} archive push", out)
                    self.assertEqual(host.audit_sql, [])

    def test_tokenizer_diagnostic_does_not_request_reauthorisation(self) -> None:
        host = make_host()
        host.failures["check"] = (1, "", "nested/tokenizer.json: Fictitious hash mismatch")
        code, out, err = invoke(host)
        self.assertEqual(code, 1, out + err)
        self.assertIn("tokenizer.json", out)
        self.assertNotIn("re-authorisation", out)
        self.assertNotIn("docs/runbooks/release-files.md §10", out)
        self.assertEqual(host.audit_sql, [])

    def test_remote_listing_failure_cannot_pass_missing_files(self) -> None:
        for failure in ((1, "objects/missing.bin;17\n", "Fictitious listing refusal"), OSError("cannot run lsf")):
            with self.subTest(failure=failure):
                host = make_host()
                host.failures["lsf"] = failure
                code, out, err = invoke(host)
                self.assertEqual(code, 1, out + err)
                self.assertIn("check: refuse", out)
                self.assertIn("corpus cut", out)
                self.assertEqual(host.audit_sql, [])

    def test_audit_write_failure_refuses_after_verification(self) -> None:
        host = make_host()
        host.audit_write_fails = True
        code, out, err = invoke(host)
        self.assertEqual(code, 1, out + err)
        self.assertIn("check: ok", out)
        self.assertIn("audit: refuse", out)
        self.assertIn("logs postgres", out)
        self.assertEqual(len(host.audit_sql), 1)
        self.assertEqual(host.locks, {})


class Parsers(unittest.TestCase):
    def test_stats_parser_uses_the_final_exact_counts(self) -> None:
        text = "Fictitious human diagnostic\n" + stats_text(2, 13) + "{}\n" + stats_text(7, 1000000000001)
        self.assertEqual(archive.parse_rclone_stats(text), archive.CopyStats(7, 1000000000001))
        self.assertEqual(archive.parse_rclone_stats(stats_text(0, 0)), archive.CopyStats(0, 0))

    def test_stats_parser_refuses_missing_or_invalid_counters(self) -> None:
        for text in ("", "{}", "[]", "invalid", '{"stats": {}}', '{"stats": {"bytes": 1}}',
                     stats_text(-1, 2), stats_text(1, -2),
                     json.dumps({"stats": {"transfers": True, "bytes": 1}}),
                     json.dumps({"stats": {"transfers": 1, "bytes": "2"}})):
            with self.subTest(text=text):
                self.assertIsNone(archive.parse_rclone_stats(text))

    def test_remote_listing_preserves_paths_and_sizes(self) -> None:
        self.assertEqual(
            archive.parse_remote_listing("nested/example.bin;17\nempty.json;0\nsemi;colon.bin;29\n"),
            (archive.RemoteEntry("nested/example.bin", 17), archive.RemoteEntry("empty.json", 0),
             archive.RemoteEntry("semi;colon.bin", 29)),
        )
        self.assertEqual(archive.parse_remote_listing(""), ())

    def test_remote_listing_refuses_malformed_or_duplicate_rows(self) -> None:
        for text in ("missing-size", ";17", "example;-1", "example;not-a-size", "example;17\nexample;17\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                archive.parse_remote_listing(text)



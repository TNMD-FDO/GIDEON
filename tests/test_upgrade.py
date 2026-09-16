"""The upgrade command's contracts (§2.1, §3.6 step 7): the forward path over a fake Host."""

import argparse
import ast
import contextlib
import io
import itertools
import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import gideon
from gideon.host import audit, backupset, nogpu, upgrade
from gideon.host.site import load_site
from gideon.host.steps.site_dirs import AGE_IDENTITY_PATH
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
RENDERED = "/etc/gideon/rendered"
CHECKOUT = "/opt/gideon"
PYTHON = "/usr/bin/python3"
OWNER = "csa"
FROM_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40
NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
CURRENT = upgrade.Version.parse(gideon.__version__)
# Tags relative to the running version, never typed: a higher patch, its pre-release
# (above the current version), and the current version's own pre-release (below it).
HIGHER = f"v{CURRENT.major}.{CURRENT.minor}.{CURRENT.patch + 1}"
HIGHER_PRE = f"{HIGHER}-rc.1"
LOWER_PRE = f"v{CURRENT}-rc.1"
NEXT_MAJOR = f"v{CURRENT.major + 1}.0.0"
FORWARD_ROWS = [
    "preconditions", "fetch", "version", "preflight", "backup", "audit-intent",
    "checkout", "provision", "preflight", "apply", "verify", "engine-verify", "audit-applied",
]
ROLLBACK_ROWS = [
    "preconditions", "select", "plan", "safety", "audit-intent", "stop", "identity", "checkout",
    "restore", "apply", "verify", "engine-verify", "audit-applied",
]
# The release a rollback returns to: the current version's own pre-release, so the
# fictitious set names a release other than the running one.
PREVIOUS = LOWER_PRE.removeprefix("v")
UNPARSABLE = "release: [\n"
MARKER = f"{backupset.STAGING}/rollback.json"


def marker_json(label: str, *, restore_needed: bool = True, restored: bool = False) -> str:
    return json.dumps(
        {"set_label": label, "restore_needed": restore_needed, "restored": restored, "started": NOW.isoformat()},
        sort_keys=True,
    ) + "\n"
_site = load_site(EXAMPLE).config
assert _site is not None
HOSTNAME = _site.hostname


def completed(argv: Sequence[str], *, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, "")


def manifest_json(
    label: str,
    *,
    release: str,
    commit: str = FROM_COMMIT,
    finished: datetime = NOW,
    recipients: tuple[str, ...] = ("age1" + "a" * 58,),
) -> str:
    entry = backupset.Entry("config.yaml", "f", 7, 0, 0, 0o644, 100.0, "a" * 64)
    return backupset.Manifest(
        1,
        label,
        backupset.kind_of(label) or backupset.Kind.LABELLED,
        finished - timedelta(minutes=5),
        finished,
        release,
        CHECKOUT,
        commit,
        HOSTNAME,
        None,
        "backup-label",
        "full",
        finished,
        {"gideon": {"public.users": 2}, "openwebui": {}},
        {"etc-gideon": (entry,)},
        "c" * 64,
        recipients,
        backupset.LinkVerdict(1, 1),
        "d" * 64,
    ).to_json()


HEALTHY = [
    {"Service": "postgres", "State": "running", "Health": "healthy"},
    {"Service": "caddy", "State": "running", "Health": ""},
]


class FakeHost:
    """A checkout owned by ``owner_uid``, git answering as configured, children by exit code."""

    def __init__(
        self,
        *,
        owner_uid: int = 1000,
        dirty: bool = False,
        fetch_ok: bool = True,
        tag_present: bool = True,
        head: str = FROM_COMMIT,
        tree_version: str | None = None,
        note: str | None = None,
        sets: Mapping[str, str] | None = None,
        applied_release: str | None = None,
        failing_child: str | None = None,
        version_output: str | None = None,
        ps_rows: Sequence[Mapping[str, str]] = HEALTHY,
        euid: int = 0,
        render_release: str | None = None,
        commit_present: bool = True,
        points_at: Sequence[str] = (),
        stack_after_apply: Sequence[Mapping[str, str]] = HEALTHY,
        declared: Sequence[str] = ("postgres", "caddy"),
        apply_records: bool = True,
        identity_present: bool = False,
        identity_unlink_error: bool = False,
    ) -> None:
        self.owner_uid = owner_uid
        self.dirty = dirty
        self.fetch_ok = fetch_ok
        self.tag_present = tag_present
        self.head = head
        self.tree_version = tree_version
        self.note = note
        self.applied_release = applied_release
        self.failing_child = failing_child
        self.version_output = version_output
        self.ps_rows = list(ps_rows)
        self.euid = euid
        self.commit_present = commit_present
        self.stack_after_apply = list(stack_after_apply)
        self.declared = list(declared)
        self.apply_records = apply_records
        self.identity_unlink_error = identity_unlink_error
        self.points_at = list(points_at)
        self.tag = ""
        self.calls: list[tuple[tuple[str, ...], str | None, bool]] = []
        self.files: dict[str, str] = {str(EXAMPLE): EXAMPLE.read_text()}
        if identity_present:
            self.files[os.fspath(AGE_IDENTITY_PATH)] = "identity\n"
        self.sets = dict(sets or {})
        for label, text in self.sets.items():
            self.files[os.path.join(backupset.set_dir(label), backupset.MANIFEST_NAME)] = text
        if applied_release is not None:
            self.files[f"{RENDERED}/applied.yaml"] = f"release: {applied_release}\nfiles: {{}}\n"
        self.set_commit = head  # what backup run records for a set taken now
        self.writes: list[tuple[str, str]] = []
        self.unlinked: list[str] = []
        if render_release == UNPARSABLE:
            self.files[f"{RENDERED}/manifest.yaml"] = UNPARSABLE
        elif render_release is not None:
            self.files[f"{RENDERED}/manifest.yaml"] = f"release: {render_release}\nfiles: {{}}\n"

    def add_set(self, label: str, *, release: str, commit: str | None = None) -> None:
        """What a backup run leaves behind: a complete set naming the checkout's commit."""

        self.sets[label] = manifest_json(label, release=release, commit=commit or self.set_commit)
        self.files[os.path.join(backupset.set_dir(label), backupset.MANIFEST_NAME)] = self.sets[label]

    def git_calls(self) -> list[tuple[str, ...]]:
        return [call[0] for call in self.calls if "git" in call[0][:4]]

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
        del check, input, env, timeout
        command = tuple(argv)
        self.calls.append((command, None if cwd is None else os.fspath(cwd), passthrough))
        if command[:2] == ("getent", "passwd"):
            return completed(command, stdout=f"{OWNER}:x:{self.owner_uid}:{self.owner_uid}::/home/{OWNER}:/bin/bash\n")
        if command[:3] == ("docker", "compose", "version"):
            return completed(command)
        if command[:2] == ("docker", "compose") and command[-2:] == ("--format", "json"):
            return completed(command, stdout=json.dumps(self.ps_rows))
        if command[:2] == ("docker", "compose") and command[-2:] == ("config", "--services"):
            return completed(command, stdout="".join(f"{name}\n" for name in self.declared))
        git = command[3:] if command[:2] == ("sudo", "-u") else command
        if git and git[0] == "git":
            return self._git(command, git[1:])
        if command[1:3] == ("-m", "gideon"):
            arguments = command[3:]
            if arguments == ("--version",):
                # The new tree answers with the version its __init__ declares.
                answer = self.version_output
                if answer is None:
                    answer = f"gideon {self.tree_version or self.tag.removeprefix('v')}\n"
                return completed(command, stdout=answer)
            failed = " ".join(arguments) == self.failing_child
            if arguments == ("apply",) and not failed:
                self.ps_rows = list(self.stack_after_apply)  # apply brings the stack up
                if self.apply_records:  # and records the tree's release after verify
                    release = self.tree_version or self.tag.removeprefix("v")
                    self.files[f"{RENDERED}/applied.yaml"] = f"release: {release}\nfiles: {{}}\n"
            return completed(command, returncode=int(failed))
        return completed(command)

    def _git(self, command: tuple[str, ...], arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if arguments == ("rev-parse", "--is-inside-work-tree"):
            return completed(command, stdout="true\n")
        if arguments == ("status", "--porcelain", "--untracked-files=no"):
            return completed(command, stdout=" M gideon/cli.py\n" if self.dirty else "")
        if arguments == ("fetch", "--tags", "origin"):
            return completed(command, returncode=int(not self.fetch_ok))
        if arguments == ("rev-parse", "HEAD"):
            return completed(command, stdout=f"{self.head}\n")
        if arguments[0] == "rev-parse" and arguments[1].endswith("^{commit}"):
            if not self.tag_present:
                return completed(command, returncode=128)
            return completed(command, stdout=f"{TARGET_COMMIT}\n")
        if arguments[0] == "show" and arguments[1].endswith(":gideon/__init__.py"):
            self.tag = arguments[1].split(":", 1)[0]
            version = self.tree_version or self.tag.removeprefix("v")
            return completed(command, stdout=f'"""GIDEON."""\n\n__version__ = "{version}"\n')
        if arguments[:2] == ("ls-tree", "--name-only"):
            if self.note is None:
                return completed(command, stdout="")
            tag = arguments[2]
            return completed(command, stdout=f"docs/2-changelog/w9_{tag}.md\n")
        if arguments[0] == "show" and "docs/2-changelog/" in arguments[1]:
            return completed(command, stdout=self.note or "")
        if arguments[:2] == ("checkout", "--detach"):
            self.head = TARGET_COMMIT
            return completed(command)
        if arguments[:2] == ("cat-file", "-e"):
            return completed(command, returncode=int(not self.commit_present))
        if arguments[:2] == ("tag", "--points-at"):
            return completed(command, stdout="".join(f"{tag}\n" for tag in self.points_at))
        raise AssertionError(f"unexpected git call: {arguments}")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        try:
            return self.files[os.fspath(path)]
        except KeyError as exc:
            raise FileNotFoundError(os.fspath(path)) from exc

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        del encoding, mode
        self.writes.append((os.fspath(path), text))
        self.files[os.fspath(path)] = text

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        if os.fspath(path) == os.fspath(AGE_IDENTITY_PATH) and self.identity_unlink_error:
            raise OSError("identity unlink refused")
        self.unlinked.append(os.fspath(path))
        if os.fspath(path) not in self.files and not missing_ok:
            raise FileNotFoundError(os.fspath(path))
        self.files.pop(os.fspath(path), None)

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) == backupset.SETS_DIR and self.sets:
            return list(self.sets)
        raise FileNotFoundError(os.fspath(path))

    def stat(self, path: PathLike) -> os.stat_result:
        if os.fspath(path) == CHECKOUT:
            return os.stat_result((0o40755, 1, 0, 1, self.owner_uid, self.owner_uid, 0, 0, 0, 0))
        if os.fspath(path) == os.fspath(AGE_IDENTITY_PATH) and os.fspath(path) in self.files:
            return os.stat_result((0o100400, 1, 0, 1, 0, 0, 9, 0, 0, 0))
        raise FileNotFoundError(os.fspath(path))

    def geteuid(self) -> int:
        return self.euid


class FakeAudit:
    """Records rows; ``fail_first`` refuses the first write only (Postgres down at intent)."""

    def __init__(self, *, write_problem: str | None = None, fail_first: bool = False) -> None:
        self.write_problem = write_problem
        self.fail_first = fail_first
        self.writes = 0
        self.rows: list[audit.AuditRow] = []

    def write_rows(self, io: FakeHost, rendered_dir: PathLike, rows: tuple[audit.AuditRow, ...]) -> str | None:
        del io, rendered_dir
        self.writes += 1
        if self.write_problem is not None or (self.fail_first and self.writes == 1):
            return self.write_problem or "audit writer failed: exit 1"
        self.rows.extend(rows)
        return None


class FakeRunners:
    """The current tree's in-process preflight and backup run, by exit code.

    A successful backup run leaves a complete set on the host it is bound to,
    recording ``set_commit`` when given, else the host's current commit.
    """

    def __init__(self, *, preflight: int = 0, backup: int = 0, set_commit: str | None = None) -> None:
        self.codes = {"preflight": preflight, "backup": backup}
        self.set_commit = set_commit
        self.host: FakeHost | None = None
        self.calls: list[tuple[str, argparse.Namespace]] = []

    def mapping(self) -> dict[str, upgrade.Runner]:
        return {name: self._runner(name) for name in self.codes}

    def _runner(self, name: str) -> upgrade.Runner:
        def run(child: argparse.Namespace) -> int:
            self.calls.append((name, child))
            code = self.codes[name]
            if name == "backup" and code == 0 and self.host is not None:
                self.host.add_set(child.label, release=str(CURRENT), commit=self.set_commit)
            return code

        return run


def rows_of(text: str) -> list[str]:
    names = set(FORWARD_ROWS) | set(ROLLBACK_ROWS)
    return [line.split(":", 1)[0] for line in text.splitlines() if line.split(":", 1)[0] in names and " — " in line]


def git_as_owner(*arguments: str) -> tuple[str, ...]:
    return ("sudo", "-u", OWNER, "git", *arguments)


class CommandRunner(unittest.TestCase):
    """The in-process invocation both paths' tests share."""

    def run_upgrade(
        self,
        host: Any,
        *,
        tag: str | None = HIGHER,
        acknowledge: bool = False,
        rollback: bool = False,
        runners: FakeRunners | None = None,
        audit_backend: FakeAudit | None = None,
    ) -> tuple[int, str, str, FakeRunners, FakeAudit]:
        nested = runners or FakeRunners()
        if isinstance(host, FakeHost):
            nested.host = host
        backend = audit_backend or FakeAudit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = upgrade.run_upgrade(
                argparse.Namespace(command_path="upgrade", tag=tag, rollback=rollback, acknowledge_breaking=acknowledge),
                host=host,
                site_path=EXAMPLE,
                rendered_dir=RENDERED,
                checkout=CHECKOUT,
                runners=nested.mapping(),
                python=PYTHON,
                now=NOW,
                audit=backend,
            )
        return code, out.getvalue(), err.getvalue(), nested, backend


class UpgradeTests(CommandRunner):
    """The forward path: ``upgrade <tag>``."""

    def test_forward_path_stage_order_argv_and_rows(self) -> None:
        host = FakeHost(applied_release=HIGHER.removeprefix("v"))
        code, out, err, runners, backend = self.run_upgrade(host)

        self.assertEqual(code, 0, out + err)
        self.assertEqual(rows_of(out), FORWARD_ROWS)
        self.assertEqual(
            host.git_calls(),
            [
                git_as_owner("rev-parse", "--is-inside-work-tree"),
                git_as_owner("status", "--porcelain", "--untracked-files=no"),
                git_as_owner("fetch", "--tags", "origin"),
                git_as_owner("rev-parse", f"{HIGHER}^{{commit}}"),
                git_as_owner("rev-parse", "HEAD"),
                git_as_owner("show", f"{HIGHER}:gideon/__init__.py"),
                git_as_owner("checkout", "--detach", HIGHER),
            ],
        )
        for command, cwd, _ in host.calls:
            if "git" in command[:4]:
                self.assertEqual(cwd, CHECKOUT, command)
        children = [(call[0][3:], call[1], call[2]) for call in host.calls if call[0][:3] == (PYTHON, "-m", "gideon")]
        self.assertEqual(
            children,
            [
                (("host", "provision"), CHECKOUT, True),
                (("preflight",), CHECKOUT, True),
                (("apply",), CHECKOUT, True),
                (("--version",), CHECKOUT, False),
                (("engine", "verify"), CHECKOUT, True),
            ],
        )
        self.assertEqual([name for name, _ in runners.calls], ["preflight", "backup"])
        backup_args = runners.calls[1][1]
        self.assertTrue(backup_args.full)
        self.assertEqual(backup_args.label, f"pre-{HIGHER}")
        self.assertIn(f"backup: ok — fresh pre-upgrade set pre-{HIGHER}", out)
        self.assertIn(f"checkout: ok — checked out {HIGHER} as {OWNER}", out)
        self.assertIn("verify: ok — ", out)
        self.assertEqual([row.detail["phase"] for row in backend.rows], ["intent", "applied"])
        intent, applied = backend.rows
        self.assertEqual(intent.kind, "upgrade")
        self.assertEqual(intent.detail["from_version"], str(CURRENT))
        self.assertEqual(intent.detail["to_tag"], HIGHER)
        self.assertEqual(intent.detail["from_commit"], FROM_COMMIT)
        self.assertEqual(intent.detail["to_commit"], TARGET_COMMIT)
        self.assertEqual(intent.detail["set_label"], f"pre-{HIGHER}")
        self.assertEqual(applied.run_id, intent.run_id)
        durations = applied.detail["durations"]
        assert isinstance(durations, Mapping)
        self.assertIn("new-tree-preflight", durations)
        self.assertIn("apply", durations)

    def test_no_gpu_marker_does_not_add_flags_to_child_argv(self) -> None:
        host = FakeHost(applied_release=HIGHER.removeprefix("v"))
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"

        code, out, err, _, _ = self.run_upgrade(host)

        self.assertEqual(code, 0, out + err)
        children = [
            call[0][3:]
            for call in host.calls
            if call[0][:3] == (PYTHON, "-m", "gideon")
        ]
        self.assertEqual(
            children,
            [
                ("host", "provision"),
                ("preflight",),
                ("apply",),
                ("--version",),
                ("engine", "verify"),
            ],
        )

    def test_a_root_owned_checkout_runs_git_without_sudo(self) -> None:
        host = FakeHost(owner_uid=0, applied_release=HIGHER.removeprefix("v"))
        code, out, _, _, _ = self.run_upgrade(host)
        self.assertEqual(code, 0, out)
        self.assertTrue(all(call[0] == "git" for call in host.git_calls()))

    def test_version_rules(self) -> None:
        cases: list[tuple[str, dict[str, Any], bool, int, str]] = [
            (LOWER_PRE, {}, False, 1, "upgrade --rollback"),
            (f"v{CURRENT}", {}, False, 0, "checkout already at"),
            (HIGHER_PRE, {}, False, 0, f"target {HIGHER_PRE.removeprefix('v')}"),
            (HIGHER, {"tree_version": HIGHER_PRE.removeprefix("v")}, False, 1, "names a tree at version"),
        ]
        for tag, host_kwargs, acknowledge, expected_code, text in cases:
            with self.subTest(tag=tag):
                head = TARGET_COMMIT if tag == f"v{CURRENT}" else FROM_COMMIT
                sets = {}
                if tag == f"v{CURRENT}":
                    # A re-run at the current version: the checkout stands at the tag
                    # and an earlier attempt's set for another release is what it reuses.
                    sets = {f"pre-{tag}": manifest_json(f"pre-{tag}", release="1000.0.0")}
                host = FakeHost(head=head, sets=sets, applied_release=tag.removeprefix("v"), **host_kwargs)
                code, out, _, _, _ = self.run_upgrade(host, tag=tag, acknowledge=acknowledge)
                self.assertEqual(code, expected_code, out)
                self.assertIn(text, out)

    def test_a_lower_tag_names_rollback_and_restore(self) -> None:
        code, out, _, runners, backend = self.run_upgrade(FakeHost(), tag=LOWER_PRE)
        self.assertEqual(code, 1)
        self.assertIn(f"version: refuse — target {LOWER_PRE} is lower than the current version {CURRENT}", out)
        self.assertIn("sudo python3 -m gideon upgrade --rollback", out)
        self.assertIn("sudo python3 -m gideon restore --from staging", out)
        self.assertEqual(runners.calls, [])
        self.assertEqual(backend.rows, [])

    def test_a_major_cross_prints_the_breaking_section_and_needs_the_flag(self) -> None:
        note = (
            "# Changelog - Week 9\n\n## Changes\n\nEverything.\n\n## Breaking\n\n"
            "The site key `office.short_name` is required.\n\n## Notes\n\nNone.\n"
        )
        without = FakeHost(note=note)
        code, out, _, runners, _ = self.run_upgrade(without, tag=NEXT_MAJOR)
        self.assertEqual(code, 1)
        self.assertIn("## Breaking\n\nThe site key `office.short_name` is required.", out)
        self.assertNotIn("Everything.", out)
        self.assertNotIn("## Notes", out)
        self.assertIn(f"re-run upgrade {NEXT_MAJOR} --acknowledge-breaking", out)
        self.assertEqual(runners.calls, [])

        with_flag = FakeHost(note=note, applied_release=NEXT_MAJOR.removeprefix("v"))
        code, out, _, _, _ = self.run_upgrade(with_flag, tag=NEXT_MAJOR, acknowledge=True)
        self.assertEqual(code, 0, out)
        self.assertIn("The site key `office.short_name` is required.", out)
        self.assertEqual(rows_of(out), FORWARD_ROWS)

        no_note = FakeHost()
        code, out, _, _, _ = self.run_upgrade(no_note, tag=NEXT_MAJOR)
        self.assertEqual(code, 1)
        self.assertIn("changelog names none", out)

    def test_pre_upgrade_set_rule(self) -> None:
        plain = f"pre-{HIGHER}"
        suffixed = f"{plain}-{backupset.nightly_label(NOW)}"
        with self.subTest(case="fresh plain label"):
            _, out, _, runners, _ = self.run_upgrade(FakeHost(applied_release=HIGHER.removeprefix("v")))
            self.assertEqual(runners.calls[1][1].label, plain)
            self.assertIn(f"backup: ok — fresh pre-upgrade set {plain}", out)
        with self.subTest(case="plain label taken and the checkout not moved"):
            host = FakeHost(sets={plain: manifest_json(plain, release=str(CURRENT))}, applied_release=HIGHER.removeprefix("v"))
            _, out, _, runners, backend = self.run_upgrade(host)
            self.assertEqual(runners.calls[1][1].label, suffixed)
            self.assertIn("never crossed the checkout", out)
            self.assertEqual(backend.rows[0].detail["set_label"], suffixed)
        with self.subTest(case="a partial leftover counts as taken"):
            host = FakeHost(sets={f"{plain}{backupset.PARTIAL_SUFFIX}": ""}, applied_release=HIGHER.removeprefix("v"))
            _, _, _, runners, _ = self.run_upgrade(host)
            self.assertEqual(runners.calls[1][1].label, suffixed)
        with self.subTest(case="checkout at the tag reuses the set naming another release"):
            older = manifest_json(plain, release=str(CURRENT), finished=NOW - timedelta(hours=2))
            newer = manifest_json(suffixed, release=str(CURRENT), finished=NOW - timedelta(hours=1))
            host = FakeHost(head=TARGET_COMMIT, sets={plain: older, suffixed: newer}, applied_release=HIGHER.removeprefix("v"))
            code, out, _, runners, backend = self.run_upgrade(host)
            self.assertEqual(code, 0, out)
            self.assertEqual([name for name, _ in runners.calls], ["preflight"])
            self.assertIn(f"backup: ok — reused {suffixed} taken at ", out)
            self.assertIn(f"for commit {FROM_COMMIT}", out)
            self.assertIn(f"checkout: ok — checkout already at {HIGHER}", out)
            self.assertNotIn(("checkout", "--detach", HIGHER), [call[-3:] for call in host.git_calls()])
            self.assertEqual(backend.rows[0].detail["set_label"], suffixed)
        with self.subTest(case="checkout at the tag with no set naming another release refuses"):
            host = FakeHost(
                head=TARGET_COMMIT,
                sets={
                    plain: manifest_json(plain, release=HIGHER.removeprefix("v")),
                    "pre-other": manifest_json("pre-other", release=str(CURRENT)),
                },
            )
            code, out, _, runners, backend = self.run_upgrade(host)
            self.assertEqual(code, 1)
            self.assertIn(f"backup: refuse — checkout is already at {HIGHER}, but no pre-upgrade set", out)
            self.assertIn("restore --from staging --set <label>", out)
            self.assertEqual([name for name, _ in runners.calls], ["preflight"])
            self.assertEqual(backend.rows, [])

    def test_next_step_by_failure_position(self) -> None:
        before = f"Correct the refusal, then re-run sudo python3 -m gideon upgrade {HIGHER}."
        after_checkout = f"re-run sudo python3 -m gideon upgrade {HIGHER}; to abandon, sudo python3 -m gideon upgrade --rollback moves the checkout back."
        rollback = "Fix: Run sudo python3 -m gideon upgrade --rollback."
        cases: list[tuple[str, FakeHost, FakeRunners, str, str, list[str]]] = [
            ("preflight", FakeHost(), FakeRunners(preflight=2), "preflight", before, []),
            ("backup", FakeHost(), FakeRunners(backup=2), "backup", before, []),
            ("provision", FakeHost(failing_child="host provision"), FakeRunners(), "provision", after_checkout, ["intent", "failed"]),
            ("new preflight", FakeHost(failing_child="preflight"), FakeRunners(), "preflight", after_checkout, ["intent", "failed"]),
            ("apply", FakeHost(failing_child="apply"), FakeRunners(), "apply", rollback, ["intent", "failed"]),
            ("verify", FakeHost(version_output="gideon 999.0.0\n"), FakeRunners(), "verify", rollback, ["intent", "failed"]),
            ("engine verify", FakeHost(failing_child="engine verify"), FakeRunners(), "engine-verify", rollback, ["intent", "failed"]),
        ]
        for name, host, runners, row, fix, phases in cases:
            with self.subTest(case=name):
                code, out, _, _, backend = self.run_upgrade(host, runners=runners)
                self.assertEqual(code, 1, out)
                failing = [line for line in out.splitlines() if line.startswith(f"{row}: refuse — ")]
                self.assertEqual(len(failing), 1, out)
                self.assertIn(fix, failing[0])
                self.assertEqual([r.detail["phase"] for r in backend.rows], phases)
                if phases:
                    self.assertIn(backend.rows[-1].detail["failed_phase"], ("provision", "new-tree-preflight", "apply", "verify", "engine-verify"))
                self.assertNotIn("audit-applied: ok — upgrade applied", out)

    def test_the_fresh_set_must_record_the_checkout_commit(self) -> None:
        host = FakeHost(applied_release=HIGHER.removeprefix("v"))
        code, out, _, _, backend = self.run_upgrade(host, runners=FakeRunners(set_commit="unknown"))
        self.assertEqual(code, 1)
        self.assertIn(
            f"backup: refuse — set pre-{HIGHER} records commit unknown, not the checkout's {FROM_COMMIT}, "
            f"so a rollback could not find it Fix: Make git -C {CHECKOUT} rev-parse HEAD answer as root, "
            f"then re-run sudo python3 -m gideon upgrade {HIGHER}.",
            out,
        )
        self.assertEqual(backend.rows, [])
        self.assertNotIn(git_as_owner("checkout", "--detach", HIGHER), host.git_calls())

    def test_verify_judges_every_declared_service_not_only_the_containers(self) -> None:
        host = FakeHost(
            applied_release=HIGHER.removeprefix("v"),
            declared=("postgres", "caddy", "open-webui"),
        )
        code, out, _, _, _ = self.run_upgrade(host)
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse — service open-webui has no container", out)

    def test_verify_checks_the_applied_record_and_the_stack(self) -> None:
        stale = FakeHost(applied_release=str(CURRENT), apply_records=False)
        code, out, _, _, _ = self.run_upgrade(stale)
        self.assertEqual(code, 1)
        self.assertIn(f"verify: refuse — applied record names {CURRENT}, expected {HIGHER.removeprefix('v')}", out)

        unhealthy = FakeHost(
            declared=("postgres",),
            stack_after_apply=[{"Service": "postgres", "State": "running", "Health": "starting"}],
        )
        code, out, _, _, _ = self.run_upgrade(unhealthy)
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse — service postgres is not healthy", out)

    def test_fetch_failure_with_and_without_the_tag(self) -> None:
        present = FakeHost(fetch_ok=False, applied_release=HIGHER.removeprefix("v"))
        code, out, _, _, _ = self.run_upgrade(present)
        self.assertEqual(code, 0, out)
        self.assertIn(f"fetch: ok — fetch failed; tag {HIGHER} was already present at {TARGET_COMMIT}, continuing", out)

        absent = FakeHost(fetch_ok=False, tag_present=False)
        code, out, _, runners, _ = self.run_upgrade(absent)
        self.assertEqual(code, 1)
        self.assertIn(
            f"fetch: refuse — fetch failed and tag {HIGHER} is not present Fix: Run git fetch --tags origin in {CHECKOUT} as {OWNER}, then re-run sudo python3 -m gideon upgrade {HIGHER}.",
            out,
        )
        self.assertEqual(runners.calls, [])

    def test_a_dirty_checkout_refuses_before_anything_else(self) -> None:
        host = FakeHost(dirty=True)
        code, out, _, runners, _ = self.run_upgrade(host)
        self.assertEqual(code, 1)
        self.assertIn(
            f"preconditions: refuse — checkout is dirty Fix: Commit or stash the checkout's changes as {OWNER}, "
            f"then re-run sudo python3 -m gideon upgrade {HIGHER}.",
            out,
        )
        self.assertEqual(host.git_calls()[-1], git_as_owner("status", "--porcelain", "--untracked-files=no"))
        self.assertEqual(runners.calls, [])

    def test_pre_run_refusals(self) -> None:
        cases = [
            (FakeHost(euid=1000), HIGHER, "gideon upgrade: root is required."),
            (FakeHost(), None, "gideon upgrade: a target tag is required."),
            (FakeHost(), "1.2.3", "gideon upgrade: target tag is invalid: 1.2.3."),
            (FakeHost(), "v1.2", "gideon upgrade: target tag is invalid: v1.2."),
        ]
        for host, tag, expected in cases:
            with self.subTest(tag=tag):
                code, out, err, runners, _ = self.run_upgrade(host, tag=tag)
                self.assertEqual(code, 1)
                self.assertIn(expected, err)
                self.assertIn("Fix: ", err)
                self.assertEqual(out, "")
                self.assertEqual(host.calls, [])
                self.assertEqual(runners.calls, [])


class VersionPrecedence(unittest.TestCase):
    def test_semver_precedence_over_pre_release_identifiers(self) -> None:
        parse = upgrade.Version.parse
        ordered = [
            "1.0.0-2", "1.0.0-1a", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
            "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.10", "1.0.0-rc.1", "1.0.0",
            "1.0.1-rc.2", "1.0.1-rc.10", "1.0.1", "1.1.0", "2.0.0-rc.1", "2.0.0",
        ]
        versions = [parse(text) for text in ordered]
        for lower, higher in itertools.pairwise(versions):
            with self.subTest(lower=str(lower), higher=str(higher)):
                self.assertLess(lower, higher)
                self.assertGreater(higher, lower)
        self.assertEqual(parse("1.0.0-rc.1"), parse("v1.0.0-rc.1"))
        self.assertEqual(str(parse("v3.2.1-beta.4")), "3.2.1-beta.4")

    def test_tag_grammar(self) -> None:
        for bad in ("1.2.3", "v1.2", "v01.2.3", "v1.2.3-rc.01", "v1.2.3+build", "vx.y.z", ""):
            with self.subTest(tag=bad), self.assertRaises(ValueError):
                upgrade.Version.from_tag(bad)
        self.assertEqual(upgrade.Version.from_tag("v1.2.3-rc.1").prerelease, ("rc", "1"))


class ModuleShape(unittest.TestCase):
    def test_upgrade_has_no_function_level_import(self) -> None:
        # After the checkout stage the tree under the running process is another
        # release's; a function-level import would load that tree's file.
        tree = ast.parse((ROOT / "gideon/host/upgrade.py").read_text())
        nested = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body
        ]
        self.assertEqual(nested, [])


def rollback_host(**kwargs: Any) -> FakeHost:
    """A box whose new release rendered, with one pre-upgrade set for the previous one."""

    defaults: dict[str, Any] = {
        "head": TARGET_COMMIT,
        "tree_version": PREVIOUS,
        "applied_release": PREVIOUS,
        "render_release": HIGHER.removeprefix("v"),
        "points_at": (LOWER_PRE,),
        "sets": {f"pre-{HIGHER}": manifest_json(f"pre-{HIGHER}", release=PREVIOUS)},
    }
    defaults.update(kwargs)
    return FakeHost(**defaults)


class RollbackTests(CommandRunner):
    """``upgrade --rollback [<tag>]``: the pre-upgrade set restored, the previous release applied."""

    def run_rollback(self, host: Any, **kwargs: Any) -> tuple[int, str, str, FakeRunners, FakeAudit]:
        kwargs.setdefault("tag", None)
        return self.run_upgrade(host, rollback=True, **kwargs)

    def test_rollback_restores_the_newest_pre_upgrade_set(self) -> None:
        plain = f"pre-{HIGHER}"
        suffixed = f"{plain}-{backupset.nightly_label(NOW - timedelta(hours=1))}"
        host = rollback_host(
            sets={
                plain: manifest_json(plain, release=PREVIOUS, finished=NOW - timedelta(hours=3)),
                suffixed: manifest_json(suffixed, release=PREVIOUS, finished=NOW - timedelta(hours=1)),
                # Newer, but never rollback material: an operator's label and a safety set.
                "pre-other": manifest_json("pre-other", release=PREVIOUS, finished=NOW - timedelta(minutes=30)),
                f"pre-rollback-{backupset.nightly_label(NOW)}": manifest_json(
                    f"pre-rollback-{backupset.nightly_label(NOW)}", release=PREVIOUS, finished=NOW - timedelta(minutes=10)
                ),
            }
        )
        code, out, err, runners, backend = self.run_rollback(host)

        self.assertEqual(code, 0, out + err)
        self.assertEqual(rows_of(out), ROLLBACK_ROWS)
        self.assertIn(f"select: ok — selected {suffixed}; release={PREVIOUS}; commit={FROM_COMMIT}; archive_through=", out)
        self.assertIn(f"plan: ok — restore needed; rendered manifest names {HIGHER.removeprefix('v')}, not {PREVIOUS}", out)
        safety_label = f"pre-rollback-{backupset.nightly_label(NOW)}"
        self.assertIn(f"safety: ok — whole stack running; safety set {safety_label} taken", out)
        self.assertEqual([name for name, _ in runners.calls], ["backup"])
        self.assertTrue(runners.calls[0][1].full)
        self.assertEqual(runners.calls[0][1].label, safety_label)
        self.assertIn("stop: ok — stack stopped", out)
        self.assertIn(f"checkout: ok — checked out {LOWER_PRE} for {FROM_COMMIT} as {OWNER}", out)

        git = host.git_calls()
        self.assertIn(git_as_owner("cat-file", "-e", f"{FROM_COMMIT}^{{commit}}"), git)
        self.assertIn(git_as_owner("tag", "--points-at", FROM_COMMIT), git)
        self.assertEqual(git[-1], git_as_owner("checkout", "--detach", LOWER_PRE))
        commands = [call[0] for call in host.calls]
        down = next(index for index, command in enumerate(commands) if command[:2] == ("docker", "compose") and command[-1] == "down")
        children = [(call[0][3:], call[1], call[2]) for call in host.calls if call[0][:3] == (PYTHON, "-m", "gideon")]
        self.assertEqual(
            children,
            [
                (("restore", "--from", "staging", "--set", suffixed), CHECKOUT, True),
                (("apply",), CHECKOUT, True),
                (("--version",), CHECKOUT, False),
                (("engine", "verify"), CHECKOUT, True),
            ],
        )
        restore_index = next(index for index, command in enumerate(commands) if command[3:4] == ("restore",))
        self.assertLess(down, restore_index)

        marker_writes = [json.loads(text) for path, text in host.writes if path == MARKER]
        self.assertEqual([m["restored"] for m in marker_writes], [False, True])
        self.assertEqual({m["set_label"] for m in marker_writes}, {suffixed})
        self.assertLess(
            next(index for index, call in enumerate(host.calls) if call[0][-1] == "down"),
            len(host.calls),
        )
        self.assertNotIn(MARKER, host.files)
        self.assertEqual(host.unlinked, [MARKER])
        # Removed only once the applied row is written: the row's write precedes the unlink.
        self.assertEqual(backend.writes, 2)

        self.assertEqual([row.kind for row in backend.rows], ["rollback", "rollback"])
        intent, applied = backend.rows
        self.assertEqual(intent.detail["phase"], "intent")
        self.assertEqual(intent.detail["set_label"], suffixed)
        self.assertEqual(intent.detail["from_version"], str(CURRENT))
        self.assertEqual(intent.detail["to_release"], PREVIOUS)
        self.assertEqual(intent.detail["commit"], FROM_COMMIT)
        self.assertTrue(intent.detail["restore_needed"])
        self.assertEqual(applied.detail["phase"], "applied")
        self.assertTrue(applied.detail["intent_recorded"])
        self.assertEqual(applied.run_id, intent.run_id)

    def test_identity_stage_removes_identity_for_one_recipient_set(self) -> None:
        host = rollback_host(identity_present=True)
        code, out, err, _runners, _backend = self.run_rollback(host)

        self.assertEqual(code, 0, out + err)
        self.assertNotIn(os.fspath(AGE_IDENTITY_PATH), host.files)
        self.assertIn(
            "identity: ok — box identity removed: the set's tree seals to one recipient",
            out,
        )

    def test_identity_stage_keeps_identity_for_two_recipient_set(self) -> None:
        label = f"pre-{HIGHER}"
        host = rollback_host(
            identity_present=True,
            sets={
                label: manifest_json(
                    label,
                    release=PREVIOUS,
                    recipients=("age1" + "a" * 58, "age1" + "c" * 58),
                )
            },
        )
        code, out, err, _runners, _backend = self.run_rollback(host)

        self.assertEqual(code, 0, out + err)
        self.assertIn(os.fspath(AGE_IDENTITY_PATH), host.files)
        self.assertIn(
            "identity: ok — box identity kept: the set's tree seals to 2 recipients",
            out,
        )

    def test_identity_stage_accepts_an_absent_identity(self) -> None:
        host = rollback_host()
        code, out, err, _runners, _backend = self.run_rollback(host)

        self.assertEqual(code, 0, out + err)
        self.assertIn(
            "identity: ok — box identity already absent: the set's tree seals to one recipient",
            out,
        )

    def test_checkout_only_rollback_still_runs_identity_stage(self) -> None:
        host = rollback_host(
            applied_release=PREVIOUS,
            render_release=PREVIOUS,
            identity_present=True,
        )
        code, out, err, _runners, _backend = self.run_rollback(host)

        self.assertEqual(code, 0, out + err)
        self.assertNotIn(os.fspath(AGE_IDENTITY_PATH), host.files)
        self.assertIn("identity: ok — box identity removed", out)
        self.assertIn("plan: ok — checkout-only", out)

    def test_failed_identity_unlink_stops_before_checkout(self) -> None:
        host = rollback_host(identity_present=True, identity_unlink_error=True)
        code, out, err, _runners, _backend = self.run_rollback(host)

        self.assertEqual(code, 1)
        self.assertIn("identity: refuse — box identity could not be removed", out)
        self.assertIn("Correct the refusal, then re-run sudo python3 -m gideon upgrade --rollback", out)
        self.assertNotIn(
            git_as_owner("checkout", "--detach", LOWER_PRE), host.git_calls()
        )

    def test_rollback_selects_by_tag_and_refuses_without_a_set(self) -> None:
        plain = f"pre-{HIGHER}"
        other = f"pre-{NEXT_MAJOR}"
        host = rollback_host(
            sets={
                plain: manifest_json(plain, release=PREVIOUS, finished=NOW - timedelta(hours=3)),
                other: manifest_json(other, release=PREVIOUS, finished=NOW - timedelta(hours=1)),
            }
        )
        code, out, _, _, _ = self.run_rollback(host, tag=HIGHER)
        self.assertEqual(code, 0, out)
        self.assertIn(f"select: ok — selected {plain};", out)

        code, out, _, _, backend = self.run_rollback(rollback_host(sets={other: manifest_json(other, release=PREVIOUS)}), tag=HIGHER)
        self.assertEqual(code, 1)
        self.assertIn(f"select: refuse — no complete pre-upgrade set is available for {HIGHER} Fix: Go back by hand: sudo python3 -m gideon restore --from staging --set <label>", out)
        self.assertEqual(backend.rows, [])

        code, out, _, runners, _ = self.run_rollback(rollback_host(sets={"pre-other": manifest_json("pre-other", release=PREVIOUS)}))
        self.assertEqual(code, 1)
        self.assertIn("select: refuse — no complete pre-upgrade set is available Fix: Go back by hand", out)
        self.assertEqual(runners.calls, [])

    def test_rollback_refuses_when_nothing_would_change_or_the_commit_is_gone(self) -> None:
        same = rollback_host(
            sets={f"pre-{HIGHER}": manifest_json(f"pre-{HIGHER}", release=str(CURRENT))},
            applied_release=str(CURRENT),
        )
        code, out, _, _, _ = self.run_rollback(same)
        self.assertEqual(code, 1)
        self.assertIn(f"select: refuse — the running tree and the applied record are both at {CURRENT}; there is nothing to roll back to Fix: Run sudo python3 -m gideon restore --from staging --set pre-{HIGHER} for the data alone.", out)

        gone = rollback_host(commit_present=False)
        code, out, _, _, _ = self.run_rollback(gone)
        self.assertEqual(code, 1)
        self.assertIn(f"select: refuse — set pre-{HIGHER} names commit {FROM_COMMIT}, which is not in the checkout Fix: Run git fetch --tags origin in the checkout as its owner, then re-run sudo python3 -m gideon upgrade --rollback.", out)
        self.assertFalse(any(command[-1] == "down" for command, _, _ in gone.calls))

    def test_a_rollback_stopped_after_its_checkout_resumes_from_the_previous_tree(self) -> None:
        # The previous tree runs this re-run: its version equals the set's release,
        # while the applied record still names the release being left.
        host = rollback_host(head=FROM_COMMIT, applied_release=HIGHER.removeprefix("v"), render_release=PREVIOUS)
        with patch.object(gideon, "__version__", PREVIOUS):
            code, out, _, _, _ = self.run_rollback(host)
        self.assertEqual(code, 0, out)
        self.assertIn(f"; resuming after a checkout to {PREVIOUS} (the applied record names {HIGHER.removeprefix('v')})", out)
        self.assertIn(f"checkout: ok — checkout already at {FROM_COMMIT}", out)

        finished = rollback_host(head=FROM_COMMIT, applied_release=PREVIOUS, render_release=PREVIOUS)
        with patch.object(gideon, "__version__", PREVIOUS):
            code, out, _, _, _ = self.run_rollback(finished)
        self.assertEqual(code, 1)
        self.assertIn(f"select: refuse — the running tree and the applied record are both at {PREVIOUS}; there is nothing to roll back to Fix: Run sudo python3 -m gideon restore --from staging --set pre-{HIGHER} for the data alone.", out)

    def test_a_recorded_rollback_resumes_where_it_stopped(self) -> None:
        label = f"pre-{HIGHER}"
        with self.subTest(case="restore failed past its files stage"):
            # The files stage restored the rendered manifest and the applied record,
            # so both name the set's release; only the record says the restore is pending.
            host = rollback_host(
                head=FROM_COMMIT, render_release=PREVIOUS, applied_release=PREVIOUS, ps_rows=[],
            )
            host.files[MARKER] = marker_json(label)
            with patch.object(gideon, "__version__", PREVIOUS):
                code, out, _, runners, _ = self.run_rollback(host)
            self.assertEqual(code, 0, out)
            self.assertIn(f"plan: ok — resuming the rollback of {label} begun {NOW.isoformat()}; the restore is still pending", out)
            self.assertIn("safety: ok — partial or stopped stack", out)
            self.assertEqual(runners.calls, [])
            children = [call[0][3:] for call in host.calls if call[0][:3] == (PYTHON, "-m", "gideon")]
            self.assertEqual(children[0], ("restore", "--from", "staging", "--set", label))
            self.assertNotIn(MARKER, host.files)
        with self.subTest(case="apply or verify failed after the restore"):
            host = rollback_host(head=FROM_COMMIT, render_release=PREVIOUS, applied_release=PREVIOUS)
            host.files[MARKER] = marker_json(label, restored=True)
            with patch.object(gideon, "__version__", PREVIOUS):
                code, out, _, _, _ = self.run_rollback(host)
            self.assertEqual(code, 0, out)
            self.assertIn("; the restore is done", out)
            self.assertIn("restore: ok — skipped (already restored by the earlier run)", out)
            children = [call[0][3:] for call in host.calls if call[0][:3] == (PYTHON, "-m", "gideon")]
            self.assertEqual(children, [("apply",), ("--version",), ("engine", "verify")])
            self.assertFalse(any(command[-1] == "down" for command, _, _ in host.calls))
        with self.subTest(case="a record for another set"):
            host = rollback_host()
            host.files[MARKER] = marker_json(f"pre-{NEXT_MAJOR}")
            code, out, _, _, backend = self.run_rollback(host)
            self.assertEqual(code, 1)
            self.assertIn(f"plan: refuse — a rollback of pre-{NEXT_MAJOR} begun {NOW.isoformat()} is in progress Fix: Re-run sudo python3 -m gideon upgrade --rollback {NEXT_MAJOR} to finish it, or Check {MARKER};", out)
            self.assertEqual(backend.rows, [])
            self.assertFalse(any(command[-1] == "down" for command, _, _ in host.calls))
        for case, text, reason in (
            ("not json", "{not json", "is malformed:"),
            ("a string for a flag", marker_json(label).replace("false", '"false"'), "must be true or false"),
            ("restored without a restore", marker_json(label, restore_needed=False, restored=True), "restored without a restore needed"),
            ("an operator label", marker_json("pre-other"), "not a pre-upgrade set label"),
            ("an extra field", marker_json(label).replace("{", '{"extra": 1, ', 1), "exactly the four rollback fields"),
            ("a bad instant", marker_json(label).replace(NOW.isoformat(), "yesterday"), "not an ISO 8601 instant"),
        ):
            with self.subTest(case=case):
                host = rollback_host()
                host.files[MARKER] = text
                code, out, _, _, _ = self.run_rollback(host)
                self.assertEqual(code, 1, out)
                self.assertIn(f"select: refuse — {MARKER} is malformed", out)
                self.assertIn(reason, out)
                self.assertIn(f"Fix: Check {MARKER}; remove it when the rollback it names is over, then re-run sudo python3 -m gideon upgrade --rollback.", out)
                self.assertFalse(any(command[-1] == "down" for command, _, _ in host.calls))
        with self.subTest(case="the applied row failed after verify"):
            # The record must survive a failed row, or the re-run would find nothing to do.
            host = rollback_host()
            code, out, _, _, backend = self.run_rollback(host, audit_backend=FakeAudit(write_problem="audit writer failed: exit 1"))
            self.assertEqual(code, 1)
            self.assertIn("audit-applied: refuse — rollback applied: audit writer failed: exit 1", out)
            self.assertIn(MARKER, host.files)
            self.assertEqual(host.unlinked, [])
            again = rollback_host(head=FROM_COMMIT, render_release=PREVIOUS, applied_release=PREVIOUS)
            again.files[MARKER] = host.files[MARKER]
            with patch.object(gideon, "__version__", PREVIOUS):
                code, out, _, _, backend = self.run_rollback(again)
            self.assertEqual(code, 0, out)
            self.assertIn("restore: ok — skipped (already restored by the earlier run)", out)
            self.assertEqual([row.detail["phase"] for row in backend.rows], ["intent", "applied"])
            self.assertNotIn(MARKER, again.files)

    def test_a_declared_service_without_a_container_is_not_a_whole_stack(self) -> None:
        host = rollback_host(declared=("postgres", "caddy", "open-webui"), stack_after_apply=[
            {"Service": "postgres", "State": "running", "Health": "healthy"},
            {"Service": "caddy", "State": "running", "Health": ""},
            {"Service": "open-webui", "State": "running", "Health": "healthy"},
        ])
        code, out, _, runners, _ = self.run_rollback(host)
        self.assertEqual(code, 0, out)
        self.assertIn("safety: ok — partial or stopped stack", out)
        self.assertEqual(runners.calls, [])

    def test_plan_stage_restores_unless_the_render_manifest_names_the_set_release(self) -> None:
        with self.subTest(case="checkout-only"):
            host = rollback_host(render_release=PREVIOUS)
            code, out, _, runners, backend = self.run_rollback(host)
            self.assertEqual(code, 0, out)
            self.assertIn(f"plan: ok — checkout-only; rendered manifest names {PREVIOUS}, so the new release never completed a render", out)
            self.assertIn("safety: ok — skipped (the new release never applied)", out)
            self.assertIn("stop: ok — skipped (the new release never applied)", out)
            self.assertIn("restore: ok — skipped (the new release never applied)", out)
            self.assertEqual(runners.calls, [])
            self.assertFalse(any(command[-1] == "down" for command, _, _ in host.calls))
            children = [call[0][3:] for call in host.calls if call[0][:3] == (PYTHON, "-m", "gideon")]
            self.assertEqual(children, [("apply",), ("--version",), ("engine", "verify")])
            self.assertFalse(backend.rows[0].detail["restore_needed"])
        for case, render_release, reason in (
            ("missing", None, "rendered manifest is missing"),
            ("unparsable", UNPARSABLE, "rendered manifest is unparsable"),
            ("newer", HIGHER.removeprefix("v"), f"rendered manifest names {HIGHER.removeprefix('v')}, not {PREVIOUS}"),
        ):
            with self.subTest(case=case):
                host = rollback_host(render_release=render_release)
                code, out, _, _, _ = self.run_rollback(host)
                self.assertEqual(code, 0, out)
                self.assertIn(f"plan: ok — restore needed; {reason}", out)
                self.assertTrue(any(command[-1] == "down" for command, _, _ in host.calls))

    def test_safety_set_only_for_a_whole_stack_taken_by_the_current_tree(self) -> None:
        for case, rows in (
            ("partial", [{"Service": "postgres", "State": "running", "Health": "healthy"}, {"Service": "caddy", "State": "exited", "Health": ""}]),
            ("stopped", []),
        ):
            with self.subTest(case=case):
                host = rollback_host(ps_rows=rows)
                code, out, _, runners, _ = self.run_rollback(host)
                self.assertEqual(code, 0, out)
                self.assertIn("safety: ok — partial or stopped stack; anything written since the upgrade began is not preserved", out)
                self.assertEqual(runners.calls, [])
                self.assertTrue(any(command[-1] == "down" for command, _, _ in host.calls))
        with self.subTest(case="safety set refused"):
            host = rollback_host()
            code, out, _, _, backend = self.run_rollback(host, runners=FakeRunners(backup=2))
            self.assertEqual(code, 1)
            self.assertIn("safety: refuse — backup run refused (exit 2) Fix: Correct backup run's refusal above, or stop the stack with docker compose -f /etc/gideon/rendered/compose.yaml down to roll back without a safety set, then re-run sudo python3 -m gideon upgrade --rollback.", out)
            self.assertFalse(any(command[-1] == "down" for command, _, _ in host.calls))
            self.assertEqual(backend.rows, [])

    def test_a_deferred_intent_row_is_said_and_carried(self) -> None:
        host = rollback_host()
        code, out, _, _, backend = self.run_rollback(host, audit_backend=FakeAudit(fail_first=True))
        self.assertEqual(code, 0, out)
        self.assertIn("audit-intent: ok — rollback intent deferred: rollback intent recorded: audit writer failed: exit 1", out)
        self.assertEqual([row.detail["phase"] for row in backend.rows], ["applied"])
        self.assertFalse(backend.rows[0].detail["intent_recorded"])

    def test_a_failed_child_records_the_failure_and_names_the_re_run(self) -> None:
        label = f"pre-{HIGHER}"
        for case, failing, row in (
            ("restore", f"restore --from staging --set {label}", "restore"),
            ("apply", "apply", "apply"),
            ("engine verify", "engine verify", "engine-verify"),
        ):
            with self.subTest(case=case):
                host = rollback_host(failing_child=failing)
                code, out, _, _, backend = self.run_rollback(host)
                self.assertEqual(code, 1)
                self.assertIn(f"{row}: refuse — {failing} failed (exit 1); its rows above carry the fix", out)
                expected_fix = (
                    "docker compose -f /etc/gideon/rendered/compose.yaml logs gideon-generator"
                    if row == "engine-verify"
                    else "Correct the refusal, then re-run sudo python3 -m gideon upgrade --rollback."
                )
                self.assertIn(expected_fix, out)
                self.assertEqual([r.detail["phase"] for r in backend.rows], ["intent", "failed"])
                self.assertEqual(backend.rows[-1].detail["failed_phase"], row)
                self.assertTrue(backend.rows[-1].detail["intent_recorded"])

    def test_checkout_uses_the_commit_without_a_tag_and_skips_when_already_there(self) -> None:
        untagged = rollback_host(points_at=("not-a-release", ""))
        code, out, _, _, _ = self.run_rollback(untagged)
        self.assertEqual(code, 0, out)
        self.assertEqual(untagged.git_calls()[-1], git_as_owner("checkout", "--detach", FROM_COMMIT))

        there = rollback_host(head=FROM_COMMIT, render_release=PREVIOUS)
        code, out, _, _, _ = self.run_rollback(there)
        self.assertEqual(code, 0, out)
        self.assertIn(f"checkout: ok — checkout already at {FROM_COMMIT}", out)
        self.assertNotIn(("checkout", "--detach"), [call[-3:-1] for call in there.git_calls()])

    def test_rollback_pre_run_refusals(self) -> None:
        code, _, err, _, _ = self.run_rollback(FakeHost(euid=1000))
        self.assertEqual(code, 1)
        self.assertIn("gideon upgrade: root is required. Fix: Run sudo python3 -m gideon upgrade --rollback as root.", err)
        code, _, err, _, _ = self.run_rollback(FakeHost(), tag="1.2.3")
        self.assertEqual(code, 1)
        self.assertIn("gideon upgrade: target tag is invalid: 1.2.3. Fix: Use a tag matching v<major>.<minor>.<patch>", err)

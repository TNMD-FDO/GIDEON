"""Contracts for the lock shared by backup and restore."""

import json
import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from gideon.host import backuplock
from gideon.host.report import Problem
from gideon.host.sysio import LockingHost, PathLike, RealHost

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class LockFake:
    """The lock-only part of the injectable seam."""

    def __init__(self) -> None:
        self.locks: dict[str, str] = {}
        self.mkdir_calls: list[tuple[str, int, bool, bool]] = []

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        self.mkdir_calls.append((os.fspath(path), mode, parents, exist_ok))

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        self.locks.pop(os.fspath(path), None)


class RecordContracts(unittest.TestCase):
    def test_record_json_round_trip(self) -> None:
        value = backuplock.Record("backup run", 123, NOW)
        self.assertEqual(backuplock.parse(value.to_json()), value)
        self.assertEqual(len(value.to_json().splitlines()), 1)

    def test_parse_rejects_empty_non_json_and_wrong_shapes(self) -> None:
        cases = (
            "",
            "not json",
            json.dumps([]),
            json.dumps({"command": "backup run", "started": NOW.isoformat()}),
            json.dumps(
                {"command": "backup run", "pid": True, "started": NOW.isoformat()}
            ),
            json.dumps(
                {
                    "command": "backup run",
                    "pid": 123,
                    "started": NOW.replace(tzinfo=None).isoformat(),
                }
            ),
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(backuplock.parse(text))


class LockContracts(unittest.TestCase):
    def test_take_has_held_nested_and_refused_outcomes(self) -> None:
        io = LockFake()
        host = cast(LockingHost, io)
        held = backuplock.take(host, command="backup run", now=NOW)
        self.assertEqual(held.state, backuplock.State.HELD)
        self.assertIsNone(held.problem)
        self.assertEqual(
            io.mkdir_calls,
            [(backuplock.LOCK_DIR, 0o700, True, True)],
        )
        stored = io.locks[backuplock.LOCK_PATH]
        stored_record = backuplock.parse(stored)
        self.assertIsNotNone(stored_record)
        assert stored_record is not None
        self.assertEqual(stored_record.command, "backup run")
        self.assertEqual(stored_record.started, NOW)

        nested = backuplock.take(host, command="backup push", now=NOW + timedelta(seconds=1))
        self.assertEqual(nested.state, backuplock.State.NESTED)
        self.assertIsNone(nested.problem)
        self.assertEqual(io.locks[backuplock.LOCK_PATH], stored)

        foreign = backuplock.Record("restore", os.getpid() + 1, NOW)
        io.locks[backuplock.LOCK_PATH] = foreign.to_json()
        refused = backuplock.take(host, command="backup run", now=NOW)
        self.assertEqual(refused.state, backuplock.State.REFUSED)
        self.assertEqual(
            refused.problem,
            Problem(
                f"the backup lock is held by restore since {NOW.isoformat()} "
                f"(pid {os.getpid() + 1}).",
                f"Wait for it to finish — ps -p {os.getpid() + 1} says whether it still runs "
                "— then retry.",
            ),
        )

    def test_take_refuses_empty_and_malformed_records(self) -> None:
        io = LockFake()
        host = cast(LockingHost, io)
        for text in ("", "not json"):
            with self.subTest(text=text):
                io.locks[backuplock.LOCK_PATH] = text
                refused = backuplock.take(host, command="restore", now=NOW)
                self.assertEqual(refused.state, backuplock.State.REFUSED)
                self.assertEqual(
                    refused.problem,
                    Problem(
                        "the backup lock is held by another gideon command, its record unreadable.",
                        "Wait for the running backup or restore to finish, then retry.",
                    ),
                )

    def test_release_pops_the_lock_path(self) -> None:
        io = LockFake()
        io.locks[backuplock.LOCK_PATH] = "record"
        backuplock.release(cast(LockingHost, io))
        self.assertNotIn(backuplock.LOCK_PATH, io.locks)


class RealLockContracts(unittest.TestCase):
    def test_real_lock_refuses_then_reopens_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.lock"
            first = backuplock.Record("backup run", 101, NOW).to_json()
            second = backuplock.Record("restore", 202, NOW + timedelta(seconds=1)).to_json()
            first_host = RealHost()
            second_host = RealHost()
            self.assertIsNone(first_host.take_lock(path, first))
            self.assertEqual(second_host.take_lock(path, second), first)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            first_host.release_lock(path)
            self.assertIsNone(second_host.take_lock(path, second))
            self.assertEqual(path.read_text(encoding="utf-8"), second)
            second_host.release_lock(path)
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

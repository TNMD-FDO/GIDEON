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
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import LockingHost, PathLike, RealHost

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class LockFake:
    """The lock-only part of the injectable seam."""

    def __init__(self) -> None:
        self.locks: dict[str, str] = {}
        self.mkdir_calls: list[tuple[str, int, bool, bool]] = []
        self.take_error: OSError | None = None

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
        if self.take_error is not None:
            raise self.take_error
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
        stored = io.locks[backuplock.BACKUP_LOCK.path]
        stored_record = backuplock.parse(stored)
        self.assertIsNotNone(stored_record)
        assert stored_record is not None
        self.assertEqual(stored_record.command, "backup run")
        self.assertEqual(stored_record.started, NOW)

        nested = backuplock.take(host, command="backup push", now=NOW + timedelta(seconds=1))
        self.assertEqual(nested.state, backuplock.State.NESTED)
        self.assertIsNone(nested.problem)
        self.assertEqual(io.locks[backuplock.BACKUP_LOCK.path], stored)

        foreign = backuplock.Record("restore", os.getpid() + 1, NOW)
        io.locks[backuplock.BACKUP_LOCK.path] = foreign.to_json()
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
        self.assertEqual(
            refused.holder,
            backuplock.Record("restore", os.getpid() + 1, NOW),
        )

    def test_take_refuses_empty_and_malformed_records(self) -> None:
        io = LockFake()
        host = cast(LockingHost, io)
        for text in ("", "not json"):
            with self.subTest(text=text):
                io.locks[backuplock.BACKUP_LOCK.path] = text
                refused = backuplock.take(host, command="restore", now=NOW)
                self.assertEqual(refused.state, backuplock.State.REFUSED)
                self.assertIsNone(refused.holder)
                self.assertEqual(
                    refused.problem,
                    Problem(
                        "the backup lock is held by another gideon command, its record unreadable.",
                        "Wait for the running backup or restore to finish, then retry.",
                    ),
                )

    def test_take_holder_is_none_for_held_and_nested_outcomes(self) -> None:
        host = cast(LockingHost, LockFake())
        held = backuplock.take(host, command="backup run", now=NOW)
        nested = backuplock.take(host, command="backup push", now=NOW)
        self.assertIsNone(held.holder)
        self.assertIsNone(nested.holder)

    def test_release_pops_the_lock_path(self) -> None:
        io = LockFake()
        io.locks[backuplock.BACKUP_LOCK.path] = "record"
        backuplock.release(cast(LockingHost, io))
        self.assertNotIn(backuplock.BACKUP_LOCK.path, io.locks)

    def test_claim_maps_lock_outcomes_and_release_claim_releases_only_taken(self) -> None:
        io = LockFake()
        host = cast(LockingHost, io)
        held = backuplock.claim(host, command="gideon engine verify", now=NOW)
        self.assertTrue(held.taken)
        self.assertEqual(held.detail, "engine lock taken")
        self.assertIsNone(held.holder)
        self.assertIsNone(held.refusal)
        backuplock.release_claim(host, held)
        self.assertNotIn(backuplock.ENGINE_LOCK.path, io.locks)

        io.locks[backuplock.ENGINE_LOCK.path] = backuplock.Record(
            "gideon engine verify", os.getpid(), NOW
        ).to_json()
        nested = backuplock.claim(host, command="nested", now=NOW)
        self.assertFalse(nested.taken)
        self.assertEqual(nested.detail, "engine lock held by this process")
        self.assertIsNone(nested.refusal)
        backuplock.release_claim(host, nested)
        self.assertIn(backuplock.ENGINE_LOCK.path, io.locks)

        foreign_record = backuplock.Record("eval run fixture", os.getpid() + 1, NOW)
        io.locks[backuplock.ENGINE_LOCK.path] = foreign_record.to_json()
        refused = backuplock.claim(host, command="gideon engine verify", now=NOW)
        self.assertFalse(refused.taken)
        self.assertEqual(refused.holder, foreign_record)
        self.assertEqual(
            refused.refusal,
            StageResult(
                "preconditions",
                False,
                f"the engine lock is held by {foreign_record.command} since "
                f"{NOW.isoformat()} (pid {foreign_record.pid}).",
                f"Wait for it to finish — ps -p {foreign_record.pid} says whether it still runs "
                "— then retry.",
            ),
        )
        backuplock.release_claim(host, refused)
        self.assertIn(backuplock.ENGINE_LOCK.path, io.locks)

    def test_claim_refuses_unreadable_record_and_oserror(self) -> None:
        io = LockFake()
        io.locks[backuplock.ENGINE_LOCK.path] = "unreadable fixture record"
        unreadable = backuplock.claim(
            cast(LockingHost, io), command="gideon engine verify", now=NOW
        )
        self.assertIsNone(unreadable.holder)
        self.assertEqual(
            unreadable.refusal,
            StageResult(
                "preconditions",
                False,
                "the engine lock is held by another gideon command, its record unreadable.",
                backuplock.ENGINE_LOCK.wait_fix,
            ),
        )

        io = LockFake()
        io.take_error = OSError("fixture lock access failure")
        unusable = backuplock.claim(
            cast(LockingHost, io), command="gideon engine verify", now=NOW
        )
        self.assertIsNone(unusable.holder)
        self.assertEqual(
            unusable.refusal,
            StageResult(
                "preconditions",
                False,
                "the engine lock could not be taken: fixture lock access failure",
                "Repair access to the engine lock, then retry.",
            ),
        )

    def test_lock_values_and_engine_refusal(self) -> None:
        self.assertEqual(
            backuplock.BACKUP_LOCK,
            backuplock.Lock(
                "/run/gideon/backup.lock",
                "backup",
                "Wait for the running backup or restore to finish, then retry.",
            ),
        )
        self.assertEqual(
            backuplock.ENGINE_LOCK,
            backuplock.Lock(
                "/run/gideon/engine.lock",
                "engine",
                "Wait for the running evaluation to finish, then retry.",
            ),
        )
        io = LockFake()
        io.locks[backuplock.ENGINE_LOCK.path] = backuplock.Record(
            "eval run smoke", os.getpid() + 1, NOW
        ).to_json()
        refused = backuplock.take(
            cast(LockingHost, io),
            command="eval run smoke",
            now=NOW,
            lock=backuplock.ENGINE_LOCK,
        )
        self.assertEqual(refused.state, backuplock.State.REFUSED)
        self.assertEqual(
            refused.problem,
            Problem(
                f"the engine lock is held by eval run smoke since {NOW.isoformat()} "
                f"(pid {os.getpid() + 1}).",
                f"Wait for it to finish — ps -p {os.getpid() + 1} says whether it still runs "
                "— then retry.",
            ),
        )


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

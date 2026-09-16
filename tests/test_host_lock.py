"""Contract tests for the committed host lock and its validator."""

import re
import tempfile
import unittest
from dataclasses import is_dataclass
from pathlib import Path

from gideon.host.lock import (
    IMAGE_REFERENCE,
    VERSION,
    HostLock,
    load_host_lock,
    render_errors,
)

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "host.lock"


class HostLockValidation(unittest.TestCase):
    def test_committed_lock_loads_with_validated_shapes_and_reference_facts(self) -> None:
        result = load_host_lock(LOCK)

        self.assertTrue(result.ok, render_errors(result.errors))
        self.assertIsInstance(result.lock, HostLock)
        assert result.lock is not None
        self.assertIsNotNone(re.fullmatch(r"\d{2}\.\d{2}", result.lock.os_lts))
        self.assertEqual(result.lock.driver.package, "nvidia-open")
        self.assertTrue(result.lock.driver.repo)
        self.assertTrue(result.lock.driver.branch.isdigit())
        self.assertTrue(
            result.lock.driver.tested is None
            or (isinstance(result.lock.driver.tested, str) and result.lock.driver.tested)
        )
        self.assertIsInstance(result.lock.minimums.docker, int)
        self.assertIsInstance(result.lock.minimums.compose, int)
        self.assertIsNotNone(VERSION.fullmatch(result.lock.minimums.toolkit))
        self.assertIsNotNone(IMAGE_REFERENCE.fullmatch(result.lock.registry_image))
        self.assertIsNotNone(VERSION.fullmatch(result.lock.gh_runner.version))
        self.assertEqual(len(result.lock.gh_runner.sha256), 64)
        self.assertTrue(
            result.lock.kernel_tested is None
            or (isinstance(result.lock.kernel_tested, str) and result.lock.kernel_tested)
        )
        self.assertEqual(
            result.lock.reference_host.controller.model, "HPE MR416i-o Gen11"
        )
        self.assertEqual(result.lock.reference_host.controller.personality, "RAID")
        self.assertEqual(
            [
                (disk.role, disk.raid_level)
                for disk in result.lock.reference_host.vd_layout
            ],
            [("os", "RAID1"), ("data", "RAID5")],
        )

    def test_lock_dataclasses_are_frozen_and_slotted(self) -> None:
        result = load_host_lock(LOCK)
        self.assertIsNotNone(result.lock)
        assert result.lock is not None
        for value in (
            result.lock,
            result.lock.driver,
            result.lock.minimums,
            result.lock.gh_runner,
            result.lock.acceptance_vm_image,
            result.lock.reference_host,
        ):
            self.assertTrue(is_dataclass(value))
            self.assertTrue(hasattr(value, "__slots__"))

    def test_unknown_keys_and_bad_values_are_collected(self) -> None:
        contents = """
os_lts: '26.04'
unexpected: true
driver:
  package: wrong
  branch: '580'
  repo: ubuntu2604/x86_64
  tested: null
minimums:
  docker: 29
  compose: 5
  toolkit: 'not-a-version'
registry_image: example:1
gh_runner:
  version: 1.0.0
  sha256: short
acceptance_vm_image:
  url: https://example.invalid/image
  sha256: short
reference_host:
  controller:
    model: model
    firmware: firmware
    personality: RAID
    slot: 21
    psoc: '0x0003'
    serial: serial
  vd_layout:
    - name: LDName_00
      role: os
      raid_level: RAID1
      size_tib: 1.454
      linux_device: sda
      wwn: wwn
      future_fact: true
"""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            result = load_host_lock(handle.name)

        self.assertFalse(result.ok)
        paths = {error.key_path for error in result.errors}
        self.assertIn("unexpected", paths)
        self.assertIn("driver.package", paths)
        self.assertIn("minimums.toolkit", paths)
        self.assertIn("registry_image", paths)
        self.assertIn("gh_runner.sha256", paths)
        self.assertIn("reference_host.vd_layout[0].future_fact", paths)
        for line, error in zip(
            render_errors(result.errors).splitlines(), result.errors, strict=True
        ):
            self.assertTrue(line.endswith(error.fix))

    def test_missing_lock_has_a_fix(self) -> None:
        result = load_host_lock("/tmp/gideon-host-lock-does-not-exist")

        self.assertFalse(result.ok)
        self.assertIn("host lock is missing", result.errors[0].problem)
        self.assertTrue(render_errors(result.errors).endswith(result.errors[0].fix))

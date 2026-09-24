"""The built-image contract: the release registry holds what `images.lock` says was built.

Runs on the self-hosted runner after ``mirror-images`` (never on a hosted
runner: it needs Docker and the box's loopback registry). For every built pin
in the committed lock it runs the build tool's check — the digest is present,
the image's labels equal the lock's base and build inputs, and the smoke
command passes — so a merge whose lock names a digest nobody built, or whose
inputs moved without a rebuild, is red on the box the same day. No stack, no
secrets: one ``docker pull`` per built pin.

Invoked as ``python3 -m unittest tests/contract/built_images.py`` with the
system Python; the file has no ``test_`` prefix so the hosted pytest never
collects it.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gideon.host.images import BuiltImagePin, load_image_lock, parse_registry
from gideon.host.sysio import RealHost
from tools.imagebuild.build import check

REGISTRY = os.environ.get("GIDEON_CONTRACT_REGISTRY", "127.0.0.1:5000")


class BuiltImages(unittest.TestCase):
    def test_every_built_pin_is_proven_against_the_registry(self) -> None:
        result = load_image_lock(ROOT / "images.lock")
        self.assertTrue(result.ok, result.errors)
        assert result.lock is not None
        target = parse_registry(REGISTRY)
        assert target is not None
        built = [pin for pin in result.lock.images if isinstance(pin, BuiltImagePin)]
        self.assertTrue(built, "images.lock has no built pin; the contract has nothing to prove")
        host = RealHost()
        for pin in built:
            with self.subTest(image=pin.name):
                report = check(host, target, pin)
                self.assertTrue(report.ok, f"{pin.name}: {report.problem} Fix: {report.fix}")


if __name__ == "__main__":
    unittest.main()

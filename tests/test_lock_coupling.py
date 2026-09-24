"""No test pins a value it also reads from a lock file.

The pin watch moves ``images.lock``, ``host.lock``, and ``models.lock`` values
on a bump branch, and that branch is the only place a test's copy of the value
and the lock disagree — the release gate runs on ``main``, where they are
equal by construction (PR #1, hotfix v0.0.14; PR #2 the same day). This
tripwire loads three locks through the product loaders, plus the Matt Pocock
skills' record the watch moves, and fails when any value the watch can move,
or a person moves after an on-box converge, appears whole-token in a test
module: not immediately preceded or followed by a letter or digit, so
``nvidia-driver-pinning-<branch>`` and ``<tested>-1ubuntu1`` count while a
digit glued to either side and a branch inside a hex digest do not. Comments
count. A test derives such a value from the loaded lock, or uses a visibly
fictitious one.
"""

import re
import unittest
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gideon.host.images import (
    BuiltImagePin,
    ImageLock,
    MirroredImagePin,
    load_image_lock,
)
from gideon.host.lock import HostLock, load_host_lock
from gideon.host.models import ModelsLock, load_models_lock
from tools.exportboundary import absent_from_export
from tools.pinwatch.skills import Provenance, parse_provenance

ROOT = Path(__file__).resolve().parent.parent
IMAGE_LOCK_PATH = ROOT / "images.lock"
HOST_LOCK_PATH = ROOT / "host.lock"
MODELS_LOCK_PATH = ROOT / "models.lock"
TOOLING_PATH = ROOT / "docs" / "agents" / "tooling.md"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Today's module count: a rename or a glob mistake cannot silently empty the scan.
MINIMUM_SCANNED_MODULES = 29


@dataclass(frozen=True, slots=True)
class PinnedValue:
    key_path: str
    value: str


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    pin: PinnedValue


def _committed_locks() -> tuple[ImageLock, HostLock, ModelsLock, Provenance | None]:
    image_result = load_image_lock(IMAGE_LOCK_PATH)
    host_result = load_host_lock(HOST_LOCK_PATH)
    models_result = load_models_lock(MODELS_LOCK_PATH)
    if not image_result.ok or image_result.lock is None:
        raise AssertionError(image_result.errors)
    if not host_result.ok or host_result.lock is None:
        raise AssertionError(host_result.errors)
    if not models_result.ok or models_result.lock is None:
        raise AssertionError(models_result.errors)
    provenance = None
    if not absent_from_export(TOOLING_PATH.relative_to(ROOT), ROOT):
        provenance = parse_provenance(TOOLING_PATH.read_text(encoding="utf-8"))
    return image_result.lock, host_result.lock, models_result.lock, provenance


def _bare_digest(digest: str) -> str:
    prefix = "sha256:"
    if not digest.startswith(prefix):
        raise AssertionError(f"unexpected digest: {digest!r}")
    return digest[len(prefix) :]


def pinned_values(
    image_lock: ImageLock,
    host_lock: HostLock,
    models_lock: ModelsLock,
    provenance: Provenance | None,
) -> tuple[PinnedValue, ...]:
    """Return every textual pin whose change can invalidate a test fake."""

    values: list[PinnedValue] = []
    for image in image_lock.images:
        if isinstance(image, MirroredImagePin):
            values.extend(
                (
                    PinnedValue(f"images.{image.name}.source", image.source),
                    PinnedValue(
                        f"images.{image.name}.digest", _bare_digest(image.digest)
                    ),
                )
            )
        elif isinstance(image, BuiltImagePin):
            values.extend(
                (
                    PinnedValue(f"images.{image.name}.base", image.base),
                    PinnedValue(
                        f"images.{image.name}.base_digest",
                        _bare_digest(image.base_digest),
                    ),
                    PinnedValue(
                        f"images.{image.name}.inputs_digest",
                        _bare_digest(image.inputs_digest),
                    ),
                    PinnedValue(
                        f"images.{image.name}.digest", _bare_digest(image.digest)
                    ),
                )
            )
            values.extend(
                PinnedValue(
                    f"images.{image.name}.build_args.{name}", value
                )
                for name, value in image.build_args.items()
            )
        else:
            raise TypeError(f"unknown image pin type: {type(image)!r}")

    registry_source, separator, registry_digest = host_lock.registry_image.partition(
        "@"
    )
    if not separator or not registry_source or not registry_digest.startswith(
        "sha256:"
    ):
        raise AssertionError(f"unexpected registry image: {host_lock.registry_image!r}")
    values.extend(
        (
            PinnedValue("registry_image.source", registry_source),
            PinnedValue("registry_image.digest", _bare_digest(registry_digest)),
            PinnedValue("gh_runner.version", host_lock.gh_runner.version),
            PinnedValue("gh_runner.sha256", host_lock.gh_runner.sha256),
            PinnedValue("minimums.toolkit", host_lock.minimums.toolkit),
            PinnedValue("driver.branch", host_lock.driver.branch),
        )
    )
    if host_lock.driver.tested is not None:
        values.append(PinnedValue("driver.tested", host_lock.driver.tested))
    values.extend(
        (
            PinnedValue("driver.keyring_sha256", host_lock.driver.keyring_sha256),
            PinnedValue("acceptance_vm_image.url", host_lock.acceptance_vm_image.url),
            PinnedValue(
                "acceptance_vm_image.sha256", host_lock.acceptance_vm_image.sha256
            ),
        )
    )
    if host_lock.kernel_tested is not None:
        values.append(PinnedValue("kernel_tested", host_lock.kernel_tested))
    for profile in models_lock.profiles:
        for model in profile.models:
            model_path = f"profiles.{profile.name}.models.{model.role}"
            values.extend(
                (
                    PinnedValue(f"{model_path}.repo", model.repo),
                    PinnedValue(f"{model_path}.revision", model.revision),
                )
            )
            values.extend(
                PinnedValue(
                    f"{model_path}.files.{file.path}.sha256",
                    _bare_digest(file.sha256),
                )
                for file in model.files
            )
    if provenance is not None:
        values.extend(
            (
                PinnedValue("skills.matt-pocock.commit", provenance.matt_commit),
                PinnedValue("skills.matt-pocock.date", provenance.matt_date),
            )
        )
    return tuple(values)


def _is_ascii_alnum(character: str) -> bool:
    return character.isascii() and character.isalnum()


def find_embedded_pins(
    text: str, values: Sequence[PinnedValue], *, path: Path | None = None
) -> list[Finding]:
    """Find whole-token pin embeddings, including occurrences in comments."""

    source_path = path or Path("<memory>")
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pin in values:
            start = 0
            while True:
                index = line.find(pin.value, start)
                if index < 0:
                    break
                end = index + len(pin.value)
                before = line[index - 1] if index else ""
                after = line[end] if end < len(line) else ""
                if not _is_ascii_alnum(before) and not _is_ascii_alnum(after):
                    findings.append(Finding(source_path, line_number, pin))
                start = end
    return findings


def scanned_modules() -> tuple[Path, ...]:
    # Fixtures are regenerated artifacts and the host baseline is a recording;
    # neither is a test-owned copy of a lock value that should be refactored.
    fixtures = ROOT / "tests" / "fixtures"
    return tuple(
        path
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if fixtures not in path.parents
    )


def _all_findings(values: tuple[PinnedValue, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for path in scanned_modules():
        findings.extend(
            find_embedded_pins(
                path.read_text(encoding="utf-8"), values, path=path
            )
        )
    return findings


class LockCouplingContracts(unittest.TestCase):
    def test_value_set_is_complete(self) -> None:
        image_lock, host_lock, models_lock, provenance = _committed_locks()
        values = pinned_values(image_lock, host_lock, models_lock, provenance)
        expected: list[str] = []
        for image in image_lock.images:
            if isinstance(image, MirroredImagePin):
                expected += [
                    f"images.{image.name}.source",
                    f"images.{image.name}.digest",
                ]
            elif isinstance(image, BuiltImagePin):
                expected += [
                    f"images.{image.name}.base",
                    f"images.{image.name}.base_digest",
                    f"images.{image.name}.inputs_digest",
                    f"images.{image.name}.digest",
                    *(
                        f"images.{image.name}.build_args.{name}"
                        for name in image.build_args
                    ),
                ]
        expected += [
            "registry_image.source",
            "registry_image.digest",
            "gh_runner.version",
            "gh_runner.sha256",
            "minimums.toolkit",
            "driver.branch",
        ]
        if host_lock.driver.tested is not None:
            expected.append("driver.tested")
        expected += [
            "driver.keyring_sha256",
            "acceptance_vm_image.url",
            "acceptance_vm_image.sha256",
        ]
        if host_lock.kernel_tested is not None:
            expected.append("kernel_tested")
        for profile in models_lock.profiles:
            for model in profile.models:
                model_path = f"profiles.{profile.name}.models.{model.role}"
                expected += [
                    f"{model_path}.repo",
                    f"{model_path}.revision",
                    *(
                        f"{model_path}.files.{file.path}.sha256"
                        for file in model.files
                    ),
                ]
        if provenance is not None:
            expected += [
                "skills.matt-pocock.commit",
                "skills.matt-pocock.date",
            ]
        self.assertEqual([pin.key_path for pin in values], expected)
        self.assertTrue(all(pin.value for pin in values))
        for pin in values:
            if pin.key_path.endswith((".digest", ".sha256")):
                self.assertIsNotNone(HEX64.fullmatch(pin.value))

    def test_no_test_module_embeds_a_committed_pin(self) -> None:
        image_lock, host_lock, models_lock, provenance = _committed_locks()
        values = pinned_values(image_lock, host_lock, models_lock, provenance)
        modules = scanned_modules()
        self.assertGreaterEqual(len(modules), MINIMUM_SCANNED_MODULES)

        findings = _all_findings(values)
        if findings:
            rendered = "\n".join(
                f"{finding.path.relative_to(ROOT)}:{finding.line}: "
                f"{finding.pin.key_path} ({finding.pin.value})"
                for finding in findings
            )
            rendered += (
                "\nFix: derive the value from the loaded lock or use a visibly "
                "fictitious lock text; see docs/4-unit-tests/TESTING.md"
            )
            self.fail(rendered)

    def test_synthetic_module_embedding_a_digest_is_caught(self) -> None:
        image_lock, host_lock, models_lock, provenance = _committed_locks()
        values = pinned_values(image_lock, host_lock, models_lock, provenance)
        digest_pin = next(pin for pin in values if pin.key_path.endswith(".digest"))
        prefixed = f"sha256:{digest_pin.value}"
        # A fake host's answer, a bare constant, and the sha256sum line shape.
        source = (
            f'files = {{"/tmp/images.lock": "digest: {prefixed}"}}\n'
            f"DIGEST = {prefixed!r}\n"
            f'sha = "{digest_pin.value}  /var/tmp/image"\n'
        )

        findings = find_embedded_pins(source, values)
        self.assertEqual(len(findings), 3)
        self.assertTrue(all(finding.pin == digest_pin for finding in findings))
        self.assertEqual([finding.line for finding in findings], [1, 2, 3])
        self.assertTrue(
            all(finding.pin.key_path.endswith(".digest") for finding in findings)
        )

    def test_boundary_rule(self) -> None:
        image_lock, host_lock, models_lock, provenance = _committed_locks()
        values = pinned_values(image_lock, host_lock, models_lock, provenance)
        digest = next(pin.value for pin in values if pin.key_path.endswith(".digest"))
        branch = host_lock.driver.branch
        version = host_lock.gh_runner.version
        tested = host_lock.driver.tested or version
        cases = (
            (f"sha256:{digest}", True),
            (f"{digest}  /path", True),
            (f"-{version}.tar.gz", True),
            (f"nvidia-driver-pinning-{branch}", True),
            (f"{tested}-1ubuntu1", True),
            (f'"{branch}"', True),
            (f"1{branch}", False),
            (f"{version}1", False),
            (f"a{branch}b", False),
            (f"v{version}", False),
        )
        for text, expected in cases:
            with self.subTest(text=text, expected=expected):
                self.assertEqual(bool(find_embedded_pins(text, values)), expected)


if __name__ == "__main__":
    unittest.main()

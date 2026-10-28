"""Image-store contracts: scoped reads, lock and container keeps, and reported removals."""

import json
import subprocess
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from gideon.host import imagestore, report
from gideon.host.images import (
    BuiltImagePin,
    ImagePin,
    MirroredImagePin,
    RegistryTarget,
    load_image_lock,
)
from gideon.host.sysio import Host

ROOT = Path(__file__).resolve().parent.parent
_COMMITTED_LOCK = load_image_lock(ROOT / "images.lock").lock
assert _COMMITTED_LOCK is not None, "the committed images.lock must load"
_MIRRORED = next(pin for pin in _COMMITTED_LOCK.images if isinstance(pin, MirroredImagePin))
_BUILT = next(pin for pin in _COMMITTED_LOCK.images if isinstance(pin, BuiltImagePin))
TARGET = RegistryTarget("https", "registry.example:5000", "gideon/")
OLD_DIGEST = "sha256:" + "e" * 64
OLDER_DIGEST = "sha256:" + "d" * 64
IMAGE_ID = "a" * 64


def image_row(pin: ImagePin, digest: str, *, tag: str | None = None) -> imagestore.ImageRow:
    return imagestore.ImageRow(
        imagestore.repository(TARGET, pin.name), tag, digest, IMAGE_ID[:12], "7.16GB"
    )


def image_listing(*rows: imagestore.ImageRow) -> str:
    return "".join(json.dumps({
        "Repository": row.repository,
        "Tag": row.tag if row.tag is not None else "<none>",
        "Digest": row.digest,
        "ID": row.id,
        "Size": row.size,
    }) + "\n" for row in rows)


def completed(
    argv: tuple[str, ...], *, stdout: str = "", stderr: str = "", returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class ScriptedHost:
    """A command-only host that checks the exact order and refuses unexpected calls."""

    def __init__(
        self, script: Sequence[tuple[tuple[str, ...], subprocess.CompletedProcess[str] | OSError]],
    ) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(command)
        if not self.script:
            raise AssertionError(f"unexpected command: {command}")
        expected, answer = self.script.pop(0)
        if command != expected:
            raise AssertionError(f"expected {expected}, got {command}")
        if isinstance(answer, OSError):
            raise answer
        return answer


class Argv(unittest.TestCase):
    """Every listing is scoped to one lock repository; removal is never forced."""

    def test_listing_joins_prefixed_and_unprefixed_targets(self) -> None:
        for prefix in ("", "gideon/"):
            with self.subTest(prefix=prefix):
                target = RegistryTarget("https", "registry.example:5000", prefix)
                expected = f"registry.example:5000/{prefix}{_MIRRORED.name}"
                self.assertEqual(imagestore.repository(target, _MIRRORED.name), expected)
                self.assertEqual(imagestore.list_argv(target, _MIRRORED.name), (
                    "docker", "image", "ls", "--digests", "--format", "{{json .}}",
                    "--filter", f"reference={expected}",
                ))

    def test_container_read_includes_stopped_containers_and_removal_uses_reference(self) -> None:
        self.assertEqual(imagestore.containers_argv(), (
            "docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}",
        ))
        reference = f"registry.example:5000/gideon/{_BUILT.name}@{OLD_DIGEST}"
        self.assertEqual(imagestore.remove_argv(reference), ("docker", "image", "rm", reference))


class Parsers(unittest.TestCase):
    def test_observed_digest_pull_and_build_tag_rows(self) -> None:
        pulled = image_row(_MIRRORED, _MIRRORED.digest)
        built = image_row(_BUILT, OLD_DIGEST, tag="18-gideon")
        self.assertEqual(imagestore.parse_images(image_listing(pulled, built)), (pulled, built))
        self.assertIsNone(pulled.tag)
        self.assertEqual(built.tag, "18-gideon")

    def test_observed_container_row_and_empty_listings(self) -> None:
        reference = f"{imagestore.repository(TARGET, _BUILT.name)}@{OLD_DIGEST}"
        self.assertEqual(
            imagestore.parse_containers(json.dumps({"Names": "gideon-ci-worker", "Image": reference})),
            (imagestore.ContainerRow("gideon-ci-worker", reference),),
        )
        self.assertEqual(imagestore.parse_images("\n"), ())
        self.assertEqual(imagestore.parse_containers(""), ())

    def test_malformed_lines_missing_fields_and_nonstring_fields_are_refused(self) -> None:
        image = json.loads(image_listing(image_row(_BUILT, OLD_DIGEST)))
        container = {"Names": "gideon-ci-worker", "Image": IMAGE_ID}
        for parser, row in ((imagestore.parse_images, image), (imagestore.parse_containers, container)):
            for field in row:
                for bad_row in (
                    {key: value for key, value in row.items() if key != field},
                    {**row, field: 42},
                ):
                    with self.subTest(parser=parser.__name__, field=field, row=bad_row):
                        result = parser(json.dumps(bad_row))
                        self.assertIsInstance(result, str)
                        self.assertIn("malformed", result)
                        self.assertIn(field, result)
            for line in ("not json", "[]", "null"):
                with self.subTest(parser=parser.__name__, line=line):
                    result = parser(line)
                    self.assertIsInstance(result, str)
                    self.assertIn("malformed", result)


class Planning(unittest.TestCase):
    """The lock and every container's held reference govern the shared daemon's keep set."""

    def test_pin_digests_and_built_base_are_kept(self) -> None:
        rows = {
            _MIRRORED.name: (image_row(_MIRRORED, _MIRRORED.digest), image_row(_MIRRORED, OLD_DIGEST)),
            _BUILT.name: (
                image_row(_BUILT, _BUILT.digest), image_row(_BUILT, _BUILT.base_digest),
                image_row(_BUILT, OLDER_DIGEST),
            ),
        }
        plan = imagestore.prune_plan((_MIRRORED, _BUILT), rows, (), target=TARGET)
        self.assertEqual(tuple(item.reference for item in plan.removals), (
            f"{imagestore.repository(TARGET, _MIRRORED.name)}@{OLD_DIGEST}",
            f"{imagestore.repository(TARGET, _BUILT.name)}@{OLDER_DIGEST}",
        ))
        self.assertEqual(plan.in_use, ())

    def test_unbuilt_pin_keeps_its_base_alone(self) -> None:
        pin = replace(_BUILT, digest="unbuilt", inputs_digest="unbuilt")
        previous = image_row(pin, _BUILT.digest)
        plan = imagestore.prune_plan(
            (pin,), {pin.name: (image_row(pin, pin.base_digest), previous)}, (), target=TARGET,
        )
        self.assertEqual(plan.removals, (
            imagestore.Removal(f"{previous.repository}@{previous.digest}", previous.size, None),
        ))

    def test_digestless_and_foreign_repository_rows_are_never_candidates(self) -> None:
        row = image_row(_BUILT, OLD_DIGEST, tag="18-gideon")
        rows = (
            replace(row, digest="<none>"),
            replace(row, repository=row.repository + "-other"),
            replace(row, repository=f"registry.example:5000/other/{_BUILT.name}"),
        )
        plan = imagestore.prune_plan((_BUILT,), {_BUILT.name: rows}, (), target=TARGET)
        self.assertEqual(plan, imagestore.PrunePlan((), ()))

    def test_lock_name_with_no_rows_and_unlocked_names_have_no_removals(self) -> None:
        plan = imagestore.prune_plan(
            (_MIRRORED,), {"unlocked": (image_row(_BUILT, OLD_DIGEST),)}, (), target=TARGET,
        )
        self.assertEqual(plan, imagestore.PrunePlan((), ()))

    def test_digest_tag_and_image_id_references_keep_container_names(self) -> None:
        row = image_row(_BUILT, OLD_DIGEST, tag="18-gideon")
        reference = f"{row.repository}@{row.digest}"
        for image in (reference, f"{row.repository}:{row.tag}", IMAGE_ID, f"sha256:{IMAGE_ID}"):
            for row_id in (row.id, f"sha256:{row.id}"):
                with self.subTest(image=image, row_id=row_id):
                    containers = (
                        imagestore.ContainerRow("gideon-ci-worker", image),
                        imagestore.ContainerRow("drill-worker", image),
                        imagestore.ContainerRow("unrelated", "f" * 64),
                    )
                    plan = imagestore.prune_plan(
                        (_BUILT,), {_BUILT.name: (replace(row, id=row_id),)}, containers, target=TARGET,
                    )
                    self.assertEqual(plan.removals, ())
                    self.assertEqual(plan.in_use, (
                        imagestore.InUse(reference, ("gideon-ci-worker", "drill-worker")),
                    ))

    def test_tagged_duplicate_rows_yield_one_removal_with_its_tag(self) -> None:
        row = image_row(_BUILT, OLD_DIGEST, tag="18-gideon")
        plan = imagestore.prune_plan(
            (_BUILT,), {_BUILT.name: (replace(row, tag=None), row, row)}, (), target=TARGET,
        )
        self.assertEqual(plan.removals, (
            imagestore.Removal(f"{row.repository}@{row.digest}", row.size, f"{row.repository}:{row.tag}"),
        ))
        self.assertEqual(plan.in_use, ())


class Runner(unittest.TestCase):
    """Reference removals are scoped, re-runnable, and stop on refusal."""

    def test_tag_goes_first_and_digest_only_while_its_row_still_stands(self) -> None:
        row = image_row(_BUILT, OLD_DIGEST, tag="18-gideon")
        listing = imagestore.list_argv(TARGET, _BUILT.name)
        containers = imagestore.containers_argv()
        reference = f"{row.repository}@{row.digest}"
        tag_reference = f"{row.repository}:{row.tag}"
        digest_remove = imagestore.remove_argv(reference)
        tag_remove = imagestore.remove_argv(tag_reference)
        for remaining, takes_digest in (
            ((), False),
            ((replace(row, tag=None),), True),
        ):
            with self.subTest(remaining=remaining):
                script = [
                    (listing, completed(listing, stdout=image_listing(row))),
                    (containers, completed(containers)),
                    (tag_remove, completed(tag_remove)),
                    (listing, completed(listing, stdout=image_listing(*remaining))),
                ]
                if takes_digest:
                    script.append((digest_remove, completed(digest_remove)))
                host = ScriptedHost(script)

                result = imagestore.prune(cast(Host, host), TARGET, (_BUILT,))

                self.assertTrue(result.ok, result.problem)
                self.assertEqual(result.removed, (
                    imagestore.Removal(reference, row.size, tag_reference),
                ))
                self.assertEqual(
                    imagestore.detail(result),
                    f"removed {reference} and {tag_reference} ({row.size})",
                )
                self.assertEqual(host.calls.count(digest_remove), int(takes_digest))
                self.assertEqual(host.script, [])

    def test_a_refusal_at_either_step_leaves_a_row_the_next_run_removes(self) -> None:
        row = image_row(_BUILT, OLD_DIGEST, tag="18-gideon")
        listing = imagestore.list_argv(TARGET, _BUILT.name)
        containers = imagestore.containers_argv()
        reference = f"{row.repository}@{row.digest}"
        tag_reference = f"{row.repository}:{row.tag}"
        digest_remove = imagestore.remove_argv(reference)
        tag_remove = imagestore.remove_argv(tag_reference)
        untagged = replace(row, tag=None)

        tag_refused = ScriptedHost([
            (listing, completed(listing, stdout=image_listing(row))),
            (containers, completed(containers)),
            (tag_remove, completed(tag_remove, returncode=1, stderr="fixture conflict")),
        ])
        result = imagestore.prune(cast(Host, tag_refused), TARGET, (_BUILT,))
        self.assertFalse(result.ok)
        assert result.problem is not None
        self.assertNotIn("untagged", result.problem)

        digest_refused = ScriptedHost([
            (listing, completed(listing, stdout=image_listing(row))),
            (containers, completed(containers)),
            (tag_remove, completed(tag_remove)),
            (listing, completed(listing, stdout=image_listing(untagged))),
            (digest_remove, completed(digest_remove, returncode=1, stderr="fixture conflict")),
        ])
        result = imagestore.prune(cast(Host, digest_refused), TARGET, (_BUILT,))
        self.assertFalse(result.ok)
        assert result.problem is not None
        self.assertIn(f"untagged {tag_reference}", result.problem)
        self.assertEqual(result.removed, ())

        retry = ScriptedHost([
            (listing, completed(listing, stdout=image_listing(untagged))),
            (containers, completed(containers)),
            (digest_remove, completed(digest_remove)),
        ])
        result = imagestore.prune(cast(Host, retry), TARGET, (_BUILT,))
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(result.removed, (imagestore.Removal(reference, row.size, None),))
        self.assertEqual(retry.script, [])

    def test_refusal_after_one_removal_preserves_progress_and_both_daemon_streams(self) -> None:
        first = image_row(_MIRRORED, OLD_DIGEST)
        second = image_row(_MIRRORED, OLDER_DIGEST)
        listing = imagestore.list_argv(TARGET, _MIRRORED.name)
        containers = imagestore.containers_argv()
        first_reference = f"{first.repository}@{first.digest}"
        first_remove = imagestore.remove_argv(first_reference)
        second_remove = imagestore.remove_argv(f"{second.repository}@{second.digest}")
        host = ScriptedHost([
            (listing, completed(listing, stdout=image_listing(first, second))),
            (containers, completed(containers)),
            (first_remove, completed(first_remove)),
            (second_remove, completed(second_remove, returncode=1, stderr="fixture conflict", stdout="fixture daemon detail")),
        ])

        result = imagestore.prune(cast(Host, host), TARGET, (_MIRRORED,))

        self.assertFalse(result.ok)
        self.assertEqual(result.removed, (
            imagestore.Removal(first_reference, first.size, None),
        ))
        self.assertIsNotNone(result.problem)
        assert result.problem is not None
        self.assertIn(f"removed {first_reference} ({first.size})", result.problem)
        self.assertIn("fixture conflict | fixture daemon detail", result.problem)
        self.assertIn(report.command("apply"), result.fix)
        self.assertEqual(host.script, [])

    def test_nothing_to_remove_reads_every_lock_name_but_no_containers(self) -> None:
        script = [
            (imagestore.list_argv(TARGET, pin.name), completed(
                imagestore.list_argv(TARGET, pin.name), stdout=image_listing(image_row(pin, pin.digest)),
            ))
            for pin in (_MIRRORED, _BUILT)
        ]
        host = ScriptedHost(script)

        result = imagestore.prune(cast(Host, host), TARGET, (_MIRRORED, _BUILT))

        self.assertTrue(result.ok, result.problem)
        self.assertEqual(result.removed, ())
        self.assertEqual(imagestore.detail(result), "nothing to remove")
        self.assertNotIn(imagestore.containers_argv(), host.calls)
        self.assertEqual(host.script, [])

    def test_unavailable_listing_refuses_with_fix_in_the_current_command_form(self) -> None:
        listing = imagestore.list_argv(TARGET, _MIRRORED.name)
        for installed in (False, True):
            for answer in (
                OSError("fixture docker unavailable"),
                completed(listing, returncode=127, stderr="fixture docker unavailable"),
            ):
                with self.subTest(installed=installed, answer=answer):
                    host = ScriptedHost([(listing, answer)])
                    report.set_installed_form(installed)
                    try:
                        result = imagestore.prune(cast(Host, host), TARGET, (_MIRRORED,))
                        self.assertFalse(result.ok)
                        self.assertIsNotNone(result.problem)
                        assert result.problem is not None
                        self.assertIn("fixture docker unavailable", result.problem)
                        self.assertIn(report.command("apply"), result.fix)
                        self.assertEqual(host.calls, [listing])
                    finally:
                        report.set_installed_form(False)

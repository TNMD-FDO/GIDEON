"""Contracts for the generated release-note pin history (§21)."""

import contextlib
import io
import subprocess
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch as mock_patch

import gideon
from gideon.host.sysio import Host, PathLike
from tools.pinwatch import bumped

ROOT = Path(__file__).resolve().parent.parent
FAKE_ROOT = Path("/fictitious/checkout")
IMAGE_DIGEST = "sha256:" + "a" * 64
NEW_IMAGE_DIGEST = "sha256:" + "b" * 64
MODEL_REVISION = "c" * 40
NEW_MODEL_REVISION = "d" * 40


def _models_lock(*, revision: str = MODEL_REVISION, second: bool = False) -> str:
    extra = ""
    if second:
        extra = (
            "  2x2v-2d:\n"
            "    requires:\n"
            "      platform: x86_64\n"
            "      gpu:\n"
            "        architecture: fictitious\n"
            "        compute_capability: \"1.0\"\n"
            "        model: fictitious\n"
            "        count: 1\n"
            "        vram_gb: 2\n"
            "      dram_gb: 2\n"
            "      data_volume_gb: 2\n"
            "    memory:\n"
            "      generator:\n"
            "        gb: 8\n"
            "        role: generator\n"
            "    models:\n"
            "      generator:\n"
            "        repo: example/second-model\n"
            f"        revision: {NEW_MODEL_REVISION}\n"
            "        gpu: 0\n"
            "        serve:\n"
            "          served_name: second\n"
            "          env: {}\n"
            "          flags: {}\n"
            "        files:\n"
            "          config.json:\n"
            "            sha256: sha256:2222222222222222222222222222222222222222222222222222222222222222\n"
            "            size: 3\n"
        )
    return (
        "version: 1\n"
        "reference: 1x1v-1d\n"
        "profiles:\n"
        "  1x1v-1d:\n"
        "    requires:\n"
        "      platform: x86_64\n"
        "      gpu:\n"
        "        architecture: fictitious\n"
        "        compute_capability: \"1.0\"\n"
        "        model: fictitious\n"
        "        count: 1\n"
        "        vram_gb: 1\n"
        "      dram_gb: 1\n"
        "      data_volume_gb: 1\n"
        "    memory:\n"
        "      generator:\n"
        "        gb: 7\n"
        "        role: generator\n"
        "    models:\n"
        "      generator:\n"
        "        repo: example/model\n"
        f"        revision: {revision}\n"
        "        gpu: 0\n"
        "        serve:\n"
        "          served_name: main\n"
        "          env: {}\n"
        "          flags: {}\n"
        "        files:\n"
        "          config.json:\n"
        "            sha256: sha256:1111111111111111111111111111111111111111111111111111111111111111\n"
        "            size: 3\n"
        + extra
    )


def _images_lock(*, moved: bool = True, new_digest: bool = False, new: bool = False) -> str:
    source = (
        "docker.io/library/example:1000.0.1"
        if moved
        else "docker.io/library/example:1000.0.0"
    )
    digest = NEW_IMAGE_DIGEST if new_digest else IMAGE_DIGEST
    extra = (
        "  new:\n"
        "    source: docker.io/library/example/new:1000.0.0\n"
        f"    digest: {IMAGE_DIGEST}\n"
        if new
        else ""
    )
    return (
        "version: 1\n"
        "images:\n"
        "  moved:\n"
        f"    source: {source}\n"
        f"    digest: {IMAGE_DIGEST}\n"
        "  digest:\n"
        "    source: docker.io/library/example/digest:1000.0.0\n"
        f"    digest: {digest}\n"
        + extra
    )


class BumpedHost:
    """Dict-backed text-only checkout and git seam for the generator."""

    def __init__(
        self,
        *,
        tags: tuple[str, ...],
        current: dict[str, str],
        old: dict[tuple[str, str], str],
        absent: set[tuple[str, str]] | None = None,
    ) -> None:
        self.tags = tags
        self.current = current
        self.old = old
        self.absent = absent or set()
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout, passthrough
        command = tuple(argv)
        self.calls.append(command)
        if command[:4] == ("git", "tag", "--list", "v*"):
            return subprocess.CompletedProcess(
                command, 0, "\n".join(self.tags) + "\n", ""
            )
        if command[:3] == ("git", "ls-tree", "--name-only"):
            tag, relative = command[3], command[5]
            listed = "" if (tag, relative) in self.absent else relative + "\n"
            return subprocess.CompletedProcess(command, 0, listed, "")
        if command[:2] == ("git", "show"):
            tag, relative = command[2].split(":", 1)
            text = self.old.get((tag, relative))
            if text is None:
                return subprocess.CompletedProcess(
                    command,
                    1,
                    "",
                    f"fatal: path '{relative}' does not exist in '{tag}'\n",
                )
            return subprocess.CompletedProcess(command, 0, text, "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected command")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        relative = Path(path).name
        if str(path).endswith("models.lock"):
            relative = "models.lock"
        if str(path).endswith("host.lock"):
            relative = "host.lock"
        if str(path).endswith("images.lock"):
            relative = "images.lock"
        if relative in self.current:
            return self.current[relative]
        return (ROOT / str(path).split("/checkout/", 1)[-1]).read_text()


def _host(
    *,
    current_images: str,
    current_models: str,
    old_images: str | None = None,
    old_models: str | None = None,
    tags: tuple[str, ...] = ("v0.1.0",),
    absent: set[tuple[str, str]] | None = None,
) -> BumpedHost:
    host_lock = (ROOT / "host.lock").read_text()
    old = {
        ("v0.1.0", "images.lock"): old_images or current_images,
        ("v0.1.0", "host.lock"): host_lock,
        ("v0.1.0", "models.lock"): old_models or current_models,
    }
    return BumpedHost(
        tags=tags,
        current={
            "images.lock": current_images,
            "host.lock": host_lock,
            "models.lock": current_models,
        },
        old=old,
        absent=absent,
    )


class SinceSelection(unittest.TestCase):
    def test_since_prefers_the_greatest_eligible_tag_below_checkout(self) -> None:
        tags = ("v0.1.0", "v0.2.0", "v0.2.3", "v0.2.4")
        with mock_patch.object(gideon, "__version__", "0.2.4"):
            self.assertEqual(bumped._since_tag(tags, None), "v0.2.3")

    def test_since_falls_back_below_checkout_and_rejects_current_or_missing(self) -> None:
        with mock_patch.object(gideon, "__version__", "0.1.8"):
            self.assertEqual(
                bumped._since_tag(("v0.1.0", "v0.1.7", "v0.2.0"), None), "v0.1.7"
            )
        with mock_patch.object(gideon, "__version__", "0.2.0"), self.assertRaises(
            bumped.BumpedError
        ):
            bumped._since_tag(("v0.2.0",), None)
        with self.assertRaises(bumped.BumpedError):
            bumped._since_tag((), None)

    def test_since_option_requires_a_reachable_well_formed_tag(self) -> None:
        tags = ("v0.1.0", "v0.2.0", "not-a-release")
        self.assertEqual(bumped._since_tag(tags, "v0.1.0"), "v0.1.0")
        with self.assertRaises(bumped.BumpedError):
            bumped._since_tag(tags, "v9.9.9")
        with self.assertRaises(bumped.BumpedError):
            bumped._since_tag(tags, "not-a-release")


class BumpedRendering(unittest.TestCase):
    def test_moved_digest_only_new_and_model_revision_lines_are_rendered(self) -> None:
        current_images = _images_lock(moved=True, new_digest=True, new=True)
        host = _host(
            current_images=current_images,
            current_models=_models_lock(revision=NEW_MODEL_REVISION, second=True),
            old_images=_images_lock(moved=False),
            old_models=_models_lock(),
        )
        with mock_patch.object(gideon, "__version__", "0.2.0"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    bumped.main(
                        ["--since", "v0.1.0", "--root", str(FAKE_ROOT)],
                        host=cast(Host, host),
                    ),
                    0,
                )
        text = output.getvalue()
        self.assertIn("Since v0.1.0:\n", text)
        self.assertIn("- images.moved: 1000.0.0 → 1000.0.1", text)
        self.assertIn(
            "- images.digest: rebuilt at the same version (the digest moved)", text
        )
        self.assertIn("- images.new: new at this release (1000.0.0)", text)
        self.assertIn(
            f"- models.generator: {MODEL_REVISION[:12]} → {NEW_MODEL_REVISION[:12]}",
            text,
        )
        self.assertIn(
            f"- models.2x2v-2d.generator: new at this release ({NEW_MODEL_REVISION[:12]})",
            text,
        )

    def test_a_lock_absent_at_the_tag_makes_its_pins_new(self) -> None:
        """A lock the tag does not carry is an empty document, read through ls-tree, never git show."""

        host = _host(
            current_images=_images_lock(moved=False),
            current_models=_models_lock(),
            absent={("v0.1.0", "models.lock")},
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                bumped.main(
                    ["--since", "v0.1.0", "--root", str(FAKE_ROOT)],
                    host=cast(Host, host),
                ),
                0,
            )
        self.assertEqual(
            output.getvalue(),
            f"Since v0.1.0:\n- models.generator: new at this release ({MODEL_REVISION[:12]})\n",
        )
        self.assertFalse(
            any(
                call[:2] == ("git", "show") and call[2].endswith("models.lock")
                for call in host.calls
            )
        )

    def test_unchanged_locks_render_the_single_none_line_byte_stably(self) -> None:
        outputs: list[str] = []
        for _ in range(2):
            host = _host(
                current_images=_images_lock(moved=False),
                current_models=_models_lock(),
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    bumped.main(
                        ["--since", "v0.1.0", "--root", str(FAKE_ROOT)],
                        host=cast(Host, host),
                    ),
                    0,
                )
            outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], "No pin moved since v0.1.0.\n")

    def test_refusal_prints_a_fix_and_nothing_to_stdout(self) -> None:
        host = _host(current_images="not: yaml: lock", current_models=_models_lock())
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = bumped.main(
                ["--since", "v0.1.0", "--root", str(FAKE_ROOT)],
                host=cast(Host, host),
            )
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("pin watch:", error.getvalue())
        self.assertIn("Fix:", error.getvalue())

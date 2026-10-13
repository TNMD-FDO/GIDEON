"""Contracts for the acceptance harness's pair forms, the real co-tenant beside GIDEON."""

import ast
import contextlib
import io
import json
import subprocess
import sys
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from test_acceptance import ROOT, FakeHost, completed, harness_context
from test_acceptance_cotenant import ScriptHost

from gideon.host import provision
from gideon.host import site as host_site
from gideon.host.report import StageResult
from gideon.host.steps import CheckResult, Disposition, Step
from gideon.host.sysio import Command, PathLike
from tools.acceptance import pair, seed, vm
from tools.acceptance.cli import main
from tools.acceptance.context import (
    DEFAULT_PAIR_VM_NAMES,
    DEFAULT_VM_NAME,
    HARNESS_ROOT,
)
from tools.acceptance.run import (
    PAIR_AFTER_IDENTIFIERS,
    PAIR_AFTER_STAGES,
    PAIR_BEFORE_IDENTIFIERS,
    PAIR_BEFORE_STAGES,
    STAGE_IDENTIFIERS,
    form_stages,
)
from tools.pinwatch.fetch import Response

TAG = "v1000.0.0"
COMPOSE = """services:
  app:
    image: ghcr.io/example/transcribe:${RELEASE_TAG:-dev}
  web:
    image: docker.io/library/caddy:synthetic
  optional:
    profiles: [gpu]
    image: ghcr.io/example/optional:synthetic
"""
IMAGES = {
    "app": f"ghcr.io/example/transcribe:{TAG}",
    "web": "docker.io/library/caddy:synthetic",
}


class PairFetcher:
    """Serve fictitious tag artifacts without network access."""

    def __init__(self, compose: str = COMPOSE, guide: str | None = None) -> None:
        self.compose = compose
        self.guide = (
            guide
            if guide is not None
            else "\n".join(
                command
                for block in pair.GUIDE_STEP_BLOCKS
                for command in block.commands
            )
        )
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> Response:
        self.calls.append(url)
        body = self.compose if url.endswith("/compose.yaml") else self.guide
        return Response(200, {}, body.encode())


class PairHost(ScriptHost):
    """Record the streamed bash pipeline as a successful host command."""

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
        result = super().run(
            argv,
            check=check,
            input=input,
            cwd=cwd,
            env=env,
            timeout=timeout,
            passthrough=passthrough,
        )
        if argv and argv[0] == "bash":
            return completed(argv)
        return result


def context(host: FakeHost | None = None) -> vm.HarnessContext:
    result = harness_context(host or ScriptHost())
    result.spec = replace(result.spec, pair="before")
    result.transcribe_tag = TAG
    result.transcribe_images = IMAGES.copy()
    return result


def tag_commands(
    host: FakeHost, images: tuple[str, ...] = tuple(IMAGES.values())
) -> None:
    host.commands[("git", "ls-remote", "--tags", pair.REPOSITORY)] = completed(
        ("git", "ls-remote"),
        stdout=(
            "111\trefs/tags/v999.9.9\n"
            "222\trefs/tags/v1000.0.0\n"
            "333\trefs/tags/v1000.0.0^{}\n"
            "444\trefs/tags/nightly\n"
        ),
    )
    for image in images:
        command = ("docker", "image", "inspect", image)
        host.commands[command] = completed(command)


class Selection(unittest.TestCase):
    def test_tag_resolution_and_override_and_image_list(self) -> None:
        for override in ("", TAG):
            with self.subTest(override=override):
                host = FakeHost()
                tag_commands(host)
                ctx = context(host)
                ctx.fetcher = PairFetcher()
                ctx.spec = replace(ctx.spec, transcribe_tag=override)

                result = pair.select(ctx)

                self.assertTrue(result.ok, result.detail)
                self.assertEqual(ctx.transcribe_tag, TAG)
                self.assertEqual(ctx.transcribe_images, IMAGES)
                self.assertEqual(
                    any(
                        call[0] == ("git", "ls-remote", "--tags", pair.REPOSITORY)
                        for call in host.calls
                    ),
                    not bool(override),
                )
                assert ctx.transcribe_images is not None
                self.assertNotIn("optional", ctx.transcribe_images)

    def test_compose_variables_and_guide_drift_refusals(self) -> None:
        images, failure = pair._compose_images(COMPOSE, TAG)
        self.assertIsNone(failure)
        self.assertEqual(images, IMAGES)
        refused, failure = pair._compose_images(
            "services:\n  app:\n    image: example/app:${UNSUPPORTED}\n", TAG
        )
        self.assertIsNone(refused)
        self.assertIn("unsupported variable", failure or "")

        guide = "\n".join(
            command for block in pair.GUIDE_STEP_BLOCKS for command in block.commands
        )
        for moved in (False, True):
            with self.subTest(moved=moved):
                host = FakeHost()
                tag_commands(host)
                ctx = context(host)
                altered = guide
                if moved:
                    block = pair.GUIDE_STEP_BLOCKS[0]
                    altered = altered.replace(
                        block.commands[0], "altered guide command"
                    )
                ctx.fetcher = PairFetcher(guide=altered)
                result = pair.select(ctx)
                self.assertEqual(result.ok, not moved)
                if moved:
                    self.assertIn(pair.GUIDE_STEP_BLOCKS[0].name, result.detail)
                    self.assertIn("GUIDE_STEP_BLOCKS", result.fix)

    def test_missing_box_image_names_both_operator_fixes(self) -> None:
        host = FakeHost()
        tag_commands(host)
        missing = ("docker", "image", "inspect", IMAGES["app"])
        host.commands[missing] = completed(missing, 1, stderr="No such image")
        ctx = context(host)
        ctx.fetcher = PairFetcher()

        result = pair.select(ctx)

        self.assertFalse(result.ok)
        self.assertIn(f"Upgrade Transcribe on the box to {TAG}", result.fix)
        self.assertIn("--transcribe-tag <box tag>", result.fix)


class Arrival(unittest.TestCase):
    def test_mount_prepare_and_clone_commands_are_ordered(self) -> None:
        host = PairHost()
        host.files[str(pair.FIXTURE_DIR / "mount-data.sh")] = (
            "synthetic mount fixture\n"
        )
        ctx = context(host)
        calls: list[str] = []
        root_commands: list[str] = []
        prepare_commands: list[tuple[str, ...]] = []
        clone_commands: list[tuple[str, ...]] = []

        def copy_in(*_args: object, **_kwargs: object) -> StageResult:
            calls.append("copy")
            return StageResult("copy", True, "copied", "")

        def run_as_root(
            _ctx: object, command: str, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append("root")
            root_commands.append(command)
            return completed(("ssh",), stdout="/data\n" if "findmnt" in command else "")

        def run_product(
            _ctx: object,
            stage: str,
            _transcript: str,
            command: object,
            **_kwargs: object,
        ) -> tuple[StageResult, str]:
            calls.append(stage)
            argv = tuple(cast(tuple[str, ...], command))
            if stage == "transcribe-clone":
                clone_commands.append(argv)
            else:
                prepare_commands.append(argv)
            return StageResult(stage, True, "ok", ""), ""

        with (
            patch.object(pair.vm, "copy_in", side_effect=copy_in),
            patch.object(pair.vm, "run_as_root", side_effect=run_as_root),
            patch.object(
                pair,
                "_docker_reading",
                side_effect=[(None, None), (("Docker synthetic", "today"), None)],
            ),
            patch.object(pair.vm, "run_product", side_effect=run_product),
        ):
            mounted = pair.mount_data(ctx)
            self.assertTrue(mounted.ok, mounted.detail)
            prepare = pair.prepare(ctx)
            self.assertTrue(prepare.ok, prepare.detail)
            self.assertIn("docker installed", prepare.detail)
            clone = pair.clone(ctx)
            self.assertTrue(clone.ok, clone.detail)

        stages = [
            name for name in calls if name in {"transcribe-prepare", "transcribe-clone"}
        ]
        self.assertEqual(
            stages,
            ["transcribe-prepare"]
            * (sum(block.run for block in pair.GUIDE_STEP_BLOCKS) + 1)
            + ["transcribe-clone"],
        )
        self.assertEqual(calls[:3], ["copy", "root", "root"])
        self.assertIn(pair.MOUNT_DATA_SCRIPT, root_commands[0])
        self.assertIn("findmnt", root_commands[1])
        expected_guide_commands: list[tuple[str, ...]] = [
            ("bash", "-o", "pipefail", "-c", pair._substitute(command))
            for block in pair.GUIDE_STEP_BLOCKS
            if block.run
            for command in block.commands
        ]
        expected_guide_commands.append(("docker", "compose", "version"))
        self.assertEqual(prepare_commands, expected_guide_commands)
        self.assertEqual(prepare_commands[-1], ("docker", "compose", "version"))
        self.assertIn(
            f"git clone --branch {TAG} {pair.REPOSITORY} .", clone_commands[0][-1]
        )
        self.assertEqual(clone_commands[0][:4], ("bash", "-o", "pipefail", "-c"))

    def test_prepare_records_docker_upgrade_and_restart_without_red(self) -> None:
        for before, after, phrases in (
            (
                ("Docker old", "yesterday"),
                ("Docker new", "today"),
                ("docker upgraded", "docker restarted"),
            ),
            (
                ("Docker old", "yesterday"),
                ("Docker old", "today"),
                ("docker restarted",),
            ),
        ):
            with self.subTest(before=before, after=after):
                ctx = context()
                readings = [(before, None), (after, None)]
                with (
                    patch.object(pair, "_docker_reading", side_effect=readings),
                    patch.object(
                        pair.vm,
                        "run_product",
                        return_value=(StageResult("prepare", True, "ok", ""), ""),
                    ),
                ):
                    result = pair.prepare(ctx)
                self.assertTrue(result.ok, result.detail)
                for phrase in phrases:
                    self.assertIn(phrase, result.detail)


class ImageTransfer(unittest.TestCase):
    def test_stream_shape_uses_run_ssh_options_and_public_digest_pull_only(
        self,
    ) -> None:
        digest = "docker.io/example/app@sha256:" + "e" * 64
        for reference, allow_pull in (
            (digest, True),
            ("ghcr.io/example/app@sha256:" + "e" * 64, False),
        ):
            with self.subTest(reference=reference):
                host = PairHost()
                ctx = context(host)
                ctx.transcribe_images = {"app": reference}
                inspected = completed(("ssh",), 1, stderr="No such image")
                verified = completed(("ssh",))
                with patch.object(
                    pair.vm, "run_as_root", side_effect=[inspected, verified, verified]
                ) as root_call:
                    result = pair.images(ctx)
                stream_call = next(call for call in host.calls if call[0][0] == "bash")
                argv = stream_call[0]
                self.assertEqual(argv[:4], ("bash", "-o", "pipefail", "-c"))
                self.assertIn("docker save", argv[4])
                self.assertIn("docker load", argv[4])
                self.assertIn("BatchMode=yes", argv[4])
                self.assertIn(str(ctx.spec.run_dir / "known_hosts"), argv[4])
                self.assertEqual(result.ok, allow_pull)
                self.assertEqual(
                    any(
                        "docker pull" in call.args[1]
                        for call in root_call.call_args_list
                    ),
                    allow_pull,
                )


class InstallAndUp(unittest.TestCase):
    def _configured_context(self) -> vm.HarnessContext:
        ctx = context()
        loaded = host_site.load_site(ROOT / "config/site.example.yaml")
        assert loaded.config is not None
        ctx.site = loaded.config
        ctx.vm_site = replace(
            loaded.config,
            hostname=seed.hostname_for(ctx.spec.vm_name),
            lan_cidrs=["192.0.2.0/24"],
        )
        ctx.address = "192.0.2.10"
        return ctx

    def test_install_stdin_order_tag_write_timer_and_redaction(self) -> None:
        ctx = self._configured_context()
        calls: list[tuple[str, str | None]] = []

        def ssh(
            _ctx: object, script: str, *, input: str | None = None, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((script, input))
            if script.startswith("test -f "):
                return completed(("ssh",))
            return completed(
                ("ssh",),
                stdout=f"{seed.hostname_for(ctx.spec.vm_name)} {ctx.address}\n",
            )

        with patch.object(pair.vm, "ssh", side_effect=ssh):
            result = pair.install(ctx)

        self.assertTrue(result.ok, result.detail)
        expected = pair._install_answers(ctx)
        self.assertIn(".env.example COMPOSE_PROFILES", calls[0][0])
        self.assertEqual(calls[0][1], "\n")
        self.assertEqual(calls[1][1], "\n".join(expected) + "\n")
        self.assertIn("./transcribe install", calls[1][0])
        self.assertNotIn(expected[0], calls[1][0])
        self.assertNotIn(expected[2], calls[1][0])
        self.assertIn(".env RELEASE_TAG", calls[3][0])
        self.assertIn(TAG, calls[3][1] or "")
        self.assertIn("secrets/ldap_bind_password", calls[4][0])
        self.assertIn("ca/office-root.pem", calls[4][0])
        self.assertIn("sudo chown transcribe:transcribe tls/key.pem", calls[4][0])
        self.assertIn("install-timers", calls[5][0])
        host = cast(ScriptHost, ctx.host)
        transcript = host.files[
            str(ctx.spec.out / f"{ctx.stage_index:02d}-transcribe-install.txt")
        ]
        self.assertNotIn(seed.hostname_for(ctx.spec.vm_name), transcript)
        self.assertNotIn(ctx.address, transcript)

    def test_up_checks_running_images_and_keeps_admin_password_on_stdin(self) -> None:
        ctx = context()
        listing = json.dumps(
            [
                {
                    "Project": pair.PROJECT,
                    "Service": name,
                    "State": "running",
                    "Image": image,
                }
                for name, image in IMAGES.items()
            ]
        )
        calls: list[tuple[str, str | None]] = []

        def ssh(
            _ctx: object, script: str, *, input: str | None = None, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((script, input))
            return completed(
                ("ssh",),
                stdout=listing if "compose ps" in script else "needs a terminal\n",
                returncode=0 if "compose ps" in script else 1,
            )

        with (
            patch.object(
                pair.vm,
                "run_product",
                return_value=(StageResult("up", True, "ok", ""), ""),
            ) as up_command,
            patch.object(pair.vm, "ssh", side_effect=ssh),
        ):
            result = pair.up(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(
            up_command.call_args.args[3], ["docker", "compose", "up", "-d"]
        )
        self.assertIn("requires a terminal", result.detail)
        admin_script, password = calls[-1]
        self.assertIn("create-local-admin", admin_script)
        self.assertEqual(len((password or "").splitlines()), 2)
        self.assertNotIn((password or "").splitlines()[0], admin_script)
        self.assertNotIn((password or "").splitlines()[0], result.detail)

        for row in (
            {
                "Project": pair.PROJECT,
                "Service": "app",
                "State": "exited",
                "Image": IMAGES["app"],
            },
            {
                "Project": pair.PROJECT,
                "Service": "app",
                "State": "running",
                "Image": "wrong",
            },
        ):
            with self.subTest(row=row):
                bad_ctx = context()
                bad_listing = json.dumps(
                    [
                        row,
                        *[
                            {
                                "Project": pair.PROJECT,
                                "Service": name,
                                "State": "running",
                                "Image": image,
                            }
                            for name, image in IMAGES.items()
                            if name != "app"
                        ],
                    ]
                )
                with (
                    patch.object(
                        pair.vm,
                        "run_product",
                        return_value=(StageResult("up", True, "ok", ""), ""),
                    ),
                    patch.object(
                        pair.vm,
                        "ssh",
                        return_value=completed(("ssh",), stdout=bad_listing),
                    ),
                ):
                    failed = pair.up(bad_ctx)
                self.assertFalse(failed.ok)


class Health(unittest.TestCase):
    def test_check_poll_passes_later_and_timeout_lists_last_failures(self) -> None:
        ctx = context()
        now = [0.0]
        ctx.clock = lambda: now[0]
        ctx.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        replies = [
            (
                StageResult(
                    "health",
                    False,
                    "./transcribe check exited 1; retry",
                    pair.HEALTH_FIX,
                ),
                "FAIL web unavailable\n",
            ),
            (StageResult("health", True, "passed", ""), "all checks pass\n"),
        ]
        with patch.object(pair.vm, "run_product", side_effect=replies) as run_product:
            passed, failure = pair._transcribe_check(ctx)
        self.assertIsNone(failure)
        self.assertEqual(passed, "./transcribe check passed")
        self.assertEqual(run_product.call_count, 2)

        now[0] = 0.0
        failure_row = StageResult(
            "health", False, "./transcribe check exited 1; retry", pair.HEALTH_FIX
        )
        with (
            patch.object(
                pair.vm,
                "run_product",
                return_value=(failure_row, "FAIL app check failed\n"),
            ),
            patch.object(pair, "HEALTH_TIMEOUT_SECONDS", 10),
            patch.object(pair, "HEALTH_POLL_SECONDS", 5),
        ):
            passed, failure = pair._transcribe_check(ctx)
        self.assertIsNone(passed)
        self.assertIn("FAIL app check failed", failure.detail if failure else "")

    def test_gideon_status_poll_clears_a_page_and_names_one_that_stays(self) -> None:
        ctx = context()
        now = [0.0]
        ctx.clock = lambda: now[0]
        ctx.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        firing = (
            "needs attention\n"
            "Push overdue: The newest backup push is overdue — runbook §4 (firing 0m)\n"
        )
        paged = StageResult("health", False, "python3 -m gideon status exited 1; transcript t", "fix")
        replies = [(paged, firing), (StageResult("health", True, "passed", ""), "all clear\n")]
        with patch.object(pair.vm, "run_product", side_effect=replies) as run_product:
            passed, failure = pair._gideon_status(ctx)
        self.assertIsNone(failure)
        self.assertEqual(passed, "GIDEON status passed")
        self.assertEqual(run_product.call_count, 2)

        now[0] = 0.0
        with (
            patch.object(pair.vm, "run_product", return_value=(paged, firing)),
            patch.object(pair, "HEALTH_TIMEOUT_SECONDS", 10),
            patch.object(pair, "HEALTH_POLL_SECONDS", 5),
        ):
            passed, failure = pair._gideon_status(ctx)
        self.assertIsNone(passed)
        self.assertIn("firing: Push overdue: The newest backup push is overdue", failure.detail if failure else "")

        broken = StageResult("health", False, "python3 -m gideon status exited 2; transcript t", "fix")
        with patch.object(pair.vm, "run_product", return_value=(broken, "")) as run_product:
            passed, failure = pair._gideon_status(ctx)
        self.assertIsNone(passed)
        self.assertEqual(run_product.call_count, 1)

    def test_health_gates_gideon_and_notes_restart_without_red(self) -> None:
        for install_url in (None, "https://gitea.invalid/repo"):
            ctx = context()
            ctx.install_url = install_url
            ctx.docker_reading = ("Docker old", "yesterday")
            with (
                patch.object(
                    pair, "_transcribe_check", return_value=("check passed", None)
                ),
                patch.object(
                    pair, "_transcribe_tls", return_value=("TLS passed", None)
                ),
                patch.object(
                    pair, "_transcribe_paths", return_value=("paths passed", None)
                ),
                patch.object(
                    pair,
                    "_docker_reading",
                    return_value=(("Docker new", "today"), None),
                ),
                patch.object(
                    pair.vm,
                    "run_product",
                    return_value=(StageResult("health", True, "ok", ""), ""),
                ) as product,
                patch.object(
                    pair.fullrestore,
                    "services_health",
                    return_value=StageResult("health", True, "services healthy", ""),
                ) as services,
            ):
                result = pair.health(ctx)
            self.assertTrue(result.ok, result.detail)
            self.assertIn("Docker version changed", result.detail)
            self.assertIn("Docker restarted", result.detail)
            self.assertEqual(product.call_count, int(install_url is not None))
            self.assertEqual(services.call_count, int(install_url is not None))

    def test_tls_mismatch_and_transcribe_paths_are_judged(self) -> None:
        ctx = context()
        ctx.spec = replace(ctx.spec, run_dir=Path("/tmp/pair-run"))
        with (
            patch.object(
                pair.vm,
                "read_file",
                side_effect=[
                    (StageResult("read", True, "ok", ""), "root"),
                    (StageResult("read", True, "ok", ""), "leaf"),
                ],
            ),
            patch.object(pair.tls, "served_fingerprint", return_value="served"),
            patch.object(pair.tls, "file_fingerprint", return_value="file"),
        ):
            result, failure = pair._transcribe_tls(ctx)
        self.assertIsNone(result)
        self.assertIn("not its VM leaf", failure.detail if failure else "")

        paths = [
            *(
                Path(pair.DATA_DIR) / name
                for name in ("", "scratch", "cases", "uploads")
            ),
            *(Path("/etc/systemd/system") / unit for unit in pair.TIMER_UNITS),
        ]
        rows = "".join(f"directory {path} transcribe\n" for path in paths[:4])
        rows += "".join(f"file {path}\n" for path in paths[4:])
        with patch.object(
            pair.vm, "run_as_root", return_value=completed(("ssh",), stdout=rows)
        ):
            reading, failure = pair._transcribe_paths(ctx)
        self.assertIsNone(failure)
        self.assertIn("6 timer units", reading or "")


class ProvisionAndMeasure(unittest.TestCase):
    @staticmethod
    def rendered_failed(name: str, detail: str) -> str:
        class NamedStep(Step):
            pass

        step = NamedStep()
        step.name = name
        output = io.StringIO()
        check = CheckResult(Disposition.UNFIXABLE, detail, "fix")
        with contextlib.redirect_stdout(output):
            provision._render_step(
                step, provision._StepState(provision.StepOutcome.FAILED, check)
            )
        return output.getvalue()

    def test_first_provision_accepts_only_one_real_docker_store_refusal(self) -> None:
        detail = (
            f"containerd store is populated under {pair.docker_step._CONTAINERD_DEFAULT_ROOT} "
            f"but empty under {pair.docker_step._CONTAINERD_ROOT}"
        )
        for text, expected in (
            (self.rendered_failed("docker-engine", detail), True),
            (
                self.rendered_failed("docker-engine", detail)
                + self.rendered_failed("other", "unfixable"),
                False,
            ),
            (self.rendered_failed("other", detail), False),
        ):
            with self.subTest(text=text):
                host = FakeHost()
                ctx = context(host)
                transcript = ctx.spec.out / "provision.txt"
                host.files[str(transcript)] = text
                ctx.transcripts.append(transcript)
                with patch.object(
                    pair.vm,
                    "provision",
                    return_value=StageResult("provision", False, "refused", vm.VM_FIX),
                ):
                    result = pair.first_provision(ctx)
                self.assertEqual(result.ok, expected)

    def test_store_move_and_followup_provision_are_gated_by_finding(self) -> None:
        ctx = context(ScriptHost())
        self.assertEqual(pair.store_move(ctx).detail, "nothing to move")
        self.assertEqual(pair.provision_after_move(ctx).detail, "not needed")
        ctx.store_refusal = "synthetic store finding"
        host = cast(ScriptHost, ctx.host)
        host.files[str(pair.FIXTURE_DIR / "store-move.sh")] = (
            "synthetic fixture script\n"
        )
        script = "counts inode=1/1 bytes=2/2 whiteouts=3/3 opaque=4/4\nlists equal\n"
        with (
            patch.object(
                pair.vm, "copy_in", return_value=StageResult("copy", True, "ok", "")
            ),
            patch.object(
                pair.vm,
                "run_product",
                return_value=(StageResult("store-move", True, "ok", ""), script),
            ),
        ):
            moved = pair.store_move(ctx)
        self.assertTrue(moved.ok, moved.detail)
        self.assertIn("synthetic store finding", moved.detail)
        self.assertIn("inode=1/1", moved.detail)
        self.assertIn("lists read equal", moved.detail)

    def test_measure_and_pair_size_boundaries(self) -> None:
        gib = 1024**3
        self.assertEqual(pair.pair_size(0, pair.PAIR_VCPUS / 2), (pair.PAIR_VCPUS, 16 * 1024))
        self.assertEqual(pair.pair_size(8 * gib, 1), (pair.PAIR_VCPUS, 16 * 1024))
        self.assertEqual(pair.pair_size(8 * gib + 1, 1), (pair.PAIR_VCPUS, 20 * 1024))
        self.assertEqual(pair.pair_size(0, pair.PAIR_VCPUS / 2 + 0.01)[0], pair.PAIR_VCPUS + 2)
        ctx = context()
        transcript = (
            "== free -b ==\nMem: 33554432 0 0\n"
            "== meminfo ==\nMemTotal: 32768 kB\nMemAvailable: 16384 kB\n"
            "== loadavg ==\n20.1 0.00 0.00 1/1 12\n"
            "== nproc ==\n12\ncontainer-count 3\n== disk usage ==\n"
        )
        with patch.object(
            pair.vm,
            "run_product",
            return_value=(StageResult("measure", True, "ok", ""), transcript),
        ):
            result = pair.measure(ctx)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("used 16777216 bytes", result.detail)
        self.assertIn("3 containers", result.detail)
        self.assertIn("size rule proposes 42 vCPUs", result.detail)


class FormsAndCli(unittest.TestCase):
    def test_tables_and_form_selection(self) -> None:
        before = [
            "preconditions",
            "select",
            "image",
            "seed",
            "services",
            "boot",
            "clone",
            "data-mount",
            "transcribe-prepare",
            "transcribe-clone",
            "transcribe-images",
            "transcribe-install",
            "transcribe-up",
            "health-arrival",
            "provision",
            "site",
            "provision-site",
            "store-move",
            "provision-moved",
            "reboot",
            "health-provision",
            "provision-2",
            "health-provision-2",
            "authorize",
            "preflight",
            "install",
            "push",
            "health-install",
            "measure",
            "upgrade",
            "health-upgrade",
            "uninstall",
            "health-uninstall",
            "purge",
            "health-purge",
            "teardown",
        ]
        after = [
            "preconditions",
            "select",
            "image",
            "seed",
            "services",
            "boot",
            "clone",
            "provision",
            "reboot",
            "site",
            "provision-2",
            "authorize",
            "preflight",
            "install",
            "push",
            "transcribe-prepare",
            "transcribe-clone",
            "transcribe-images",
            "transcribe-install",
            "transcribe-up",
            "health-arrival",
            "measure",
            "upgrade",
            "health-upgrade",
            "uninstall",
            "health-uninstall",
            "purge",
            "health-purge",
            "teardown",
        ]
        self.assertEqual(PAIR_BEFORE_IDENTIFIERS, tuple(before))
        self.assertEqual(PAIR_AFTER_IDENTIFIERS, tuple(after))
        for form, stages in (
            ("before", PAIR_BEFORE_STAGES),
            ("after", PAIR_AFTER_STAGES),
        ):
            with self.subTest(form=form):
                ctx = context()
                ctx.spec = replace(ctx.spec, pair=form)
                self.assertIs(form_stages(ctx.spec), stages)

    def test_pair_cli_dry_run_until_refusals_and_names(self) -> None:
        for name in DEFAULT_PAIR_VM_NAMES.values():
            self.assertLessEqual(len(name), 64)
            self.assertLessEqual(len(seed.hostname_for(name)), 64)
        host = ScriptHost()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["HEAD", "--pair", "before", "--dry-run"], host=host)
        self.assertEqual(code, 0)
        self.assertIn("(pair before)", output.getvalue())
        self.assertIn(
            f"VM: {pair.PAIR_VCPUS} vCPUs, {pair.PAIR_MEMORY_MIB / 1024:g} GiB RAM",
            output.getvalue(),
        )
        self.assertIn(DEFAULT_PAIR_VM_NAMES["before"], output.getvalue())

        until = io.StringIO()
        with contextlib.redirect_stdout(until):
            accepted_until = main(
                ["HEAD", "--pair", "before", "--until", "store-move", "--dry-run"],
                host=ScriptHost(),
            )
        self.assertEqual(accepted_until, 0)
        self.assertIn("store-move", until.getvalue())

        after_until = io.StringIO()
        with contextlib.redirect_stdout(after_until):
            accepted_after_until = main(
                ["HEAD", "--pair", "after", "--until", "health-arrival", "--dry-run"],
                host=ScriptHost(),
            )
        self.assertEqual(accepted_after_until, 0)
        self.assertIn("health-arrival", after_until.getvalue())

        receiving = io.StringIO()
        with contextlib.redirect_stdout(receiving):
            main(["HEAD", "--dry-run"], host=ScriptHost())
        expected_receiving = (
            "Acceptance dry run:\n"
            f"stages: {', '.join(STAGE_IDENTIFIERS)}\n"
            f"run directory: {HARNESS_ROOT / DEFAULT_VM_NAME}\n"
            f"output directory: {Path.cwd() / 'acceptance-out' / 'HEAD'}\n"
            "VM: 8 vCPUs, 16 GiB RAM, machine pc, "
            "/usr/bin/qemu-system-x86_64, default libvirt network\n"
        )
        self.assertEqual(receiving.getvalue(), expected_receiving)

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            refused = main(["HEAD", "--transcribe-tag", TAG], host=ScriptHost())
        self.assertEqual(refused, 1)
        self.assertIn("Fix: pass --pair before or --pair after", error.getvalue())

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            refused = main(
                ["HEAD", "--pair", "after", "--until", "store-move"],
                host=ScriptHost(),
            )
        self.assertEqual(refused, 1)
        self.assertIn("--pair before", error.getvalue())

        # This stage belongs to another existing form, so the refusal names
        # that form's flag rather than a pair form.
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            refused = main(
                ["HEAD", "--pair", "before", "--until", "intact-arrival"],
                host=ScriptHost(),
            )
        self.assertEqual(refused, 1)
        self.assertIn("--cotenant after", error.getvalue())

    def test_import_boundary(self) -> None:
        tree = ast.parse((ROOT / "tools/acceptance/pair.py").read_text())
        modules = [
            alias.name.split(" as ", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        allowed = {"yaml", "tools.pinwatch.fetch"}
        for module in modules:
            root = module.split(".", 1)[0]
            self.assertTrue(
                root in sys.stdlib_module_names
                or module in allowed
                or module.startswith(("gideon.host", "tools.acceptance")),
                module,
            )

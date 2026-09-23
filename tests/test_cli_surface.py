"""The recorded CLI surface (spec §20.2 + `backup drill` from the [22] Answer).

Phase B contract (bootstrap brief): ``--help`` lists every command group, and
every stub prints "not implemented" and exits non-zero.
"""

import contextlib
import io
import unittest

from gideon.cli import main
from gideon.host.steps import STEPS

TOP_LEVEL = [
    "host", "render", "apply", "preflight", "install", "upgrade", "tls",
    "users", "secrets", "engine", "models", "corpus", "index", "registry", "eval", "proposals", "status",
    "backup", "restore", "audit", "retention", "alerts",
]

STUBS = [
    ["host", "gpu"],
    ["corpus", "cut"],
    ["corpus", "install", "corpus-2026-08-31"],
    ["index", "build"],
    ["index", "promote", "1"],
    ["index", "gc"],
    ["index", "report"],
    ["registry", "gc"],
    ["audit", "query"],
    ["retention", "sweep"],
]


class Help(unittest.TestCase):
    def test_lists_every_top_level_command(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        for name in TOP_LEVEL:
            self.assertIn(name, out.getvalue())

    def test_eval_help_names_the_set_and_slice_contract(self) -> None:
        group = io.StringIO()
        with contextlib.redirect_stdout(group), self.assertRaises(SystemExit) as ctx:
            main(["eval", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("§18", group.getvalue())

        command = io.StringIO()
        with contextlib.redirect_stdout(command), self.assertRaises(SystemExit) as ctx:
            main(["eval", "run", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--slice NAME", command.getvalue())
        self.assertIn("--set DIR", command.getvalue())
        self.assertIn("--ranked FILE", command.getvalue())
        self.assertIn("§18", command.getvalue())
        self.assertIn("§18.6", command.getvalue())
        self.assertIn("§18.2", command.getvalue())

        reference = io.StringIO()
        with contextlib.redirect_stdout(reference), self.assertRaises(SystemExit) as ctx:
            main(["eval", "reference", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--run ID", reference.getvalue())
        self.assertIn("§18.6", reference.getvalue())

    def test_status_help_names_its_blocks_and_front_door_contract(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["status", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        for phrase in (
            "needs attention",
            "waiting on you",
            "at a glance",
            "front-door brief",
            "§20.2",
            "root",
            "writes nothing",
        ):
            self.assertIn(phrase, out.getvalue())


class Stubs(unittest.TestCase):
    def test_every_stub_refuses_as_not_implemented(self) -> None:
        for argv in STUBS:
            with self.subTest(command=" ".join(argv)):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn("not implemented", err.getvalue())


class RestoreSelection(unittest.TestCase):
    def test_at_and_set_are_exclusive_at_parse_time(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            main(["restore", "--from", "staging", "--at", "2026-09-03T01:00Z", "--set", "pre-v1.2.3"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not allowed with argument --at", err.getvalue())


class EntryStreams(unittest.TestCase):
    def test_the_entry_point_line_buffers_stdout(self) -> None:
        from gideon.__main__ import line_buffered

        stream = io.TextIOWrapper(io.BytesIO(), write_through=False)
        with contextlib.redirect_stdout(stream):
            line_buffered()
            self.assertTrue(stream.line_buffering)


class ProvisionList(unittest.TestCase):
    def test_list_prints_registry_names_and_summaries(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["host", "provision", "--list"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.getvalue(),
            "".join(
                f"{step.name}: {step.summary}"
                f"{' [gpu-host-only]' if step.gpu_host_only else ''}"
                f"{' [build-box-only]' if step.build_box_only else ''}\n"
                for step in STEPS
            ),
        )

    def test_provision_help_lists_the_no_gpu_flag(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["host", "provision", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(
            "--no-gpu",
            out.getvalue(),
        )
        self.assertIn(
            "declare a host without a GPU: the engine is pinned out",
            out.getvalue(),
        )
        self.assertIn("--build-box", out.getvalue())
        self.assertIn("declare this host the build box", out.getvalue())
        self.assertIn("KVM, registry, and", out.getvalue())

    def test_provision_modes_are_mutually_exclusive(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            main(["host", "provision", "--no-gpu", "--build-box"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())

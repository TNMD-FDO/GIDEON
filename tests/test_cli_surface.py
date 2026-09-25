"""The recorded CLI surface, with `backup drill` beside it.

``--help`` lists every command group, and every stub prints "not
implemented" and exits non-zero. A bare invocation prints the start screen,
and every stub names where it lands.
"""

import contextlib
import io
import unittest

from gideon.cli import SITUATIONS, main
from gideon.host.steps import STEPS

TOP_LEVEL = [
    "host", "render", "apply", "preflight", "install", "upgrade", "tls",
    "users", "secrets", "engine", "models", "corpus", "index", "registry", "eval", "proposals", "status",
    "backup", "restore", "audit", "retention", "alerts",
]

# Each stub with the phrase its help and refusal must carry: the slice it
# lands in, or the command's own words.
STUBS = [
    (["host", "gpu"], "escape hatch"),
    (["corpus", "cut"], "slice 3"),
    (["corpus", "install", "corpus-2026-08-31"], "slice 3"),
    (["index", "build"], "slice 3"),
    (["index", "promote", "1"], "slice 3"),
    (["index", "gc"], "slice 3"),
    (["index", "report"], "slice 3"),
    (["registry", "gc"], "no slice scheduled"),
    (["audit", "query"], "no slice scheduled"),
    (["retention", "sweep"], "slice 6"),
]


def collapsed(text: str) -> str:
    """Argparse wraps help lines, so a phrase is searched with whitespace collapsed."""
    return " ".join(text.split())


class Start(unittest.TestCase):
    def test_bare_invocation_prints_start_screen(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([])
        screen = out.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        commands = [line.strip() for line in screen.splitlines() if line.startswith("  gideon ")]
        self.assertEqual(commands[0], "gideon status")
        self.assertTrue(commands[-1].startswith("gideon --help"))
        self.assertIn("runs as root", screen)
        self.assertIn("at most once a sitting", collapsed(screen))

    def test_situation_commands_parse_and_are_not_stubs(self) -> None:
        stub_paths = {" ".join(argv[:2]) for argv, _ in STUBS}
        for situation in SITUATIONS:
            for path, _hint in situation.entries:
                with self.subTest(path=path):
                    self.assertNotIn(path, stub_paths)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                        main([*path.split(), "--help"])
                    self.assertEqual(ctx.exception.code, 0)


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
        self.assertIn("run the eval suites", group.getvalue())

        command = io.StringIO()
        with contextlib.redirect_stdout(command), self.assertRaises(SystemExit) as ctx:
            main(["eval", "run", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--slice NAME", command.getvalue())
        self.assertIn("--stack {production,ci}", command.getvalue())
        self.assertIn("--kind {manual,smoke}", command.getvalue())
        self.assertIn("--set DIR", command.getvalue())
        self.assertIn("--ranked FILE", command.getvalue())
        self.assertIn("frozen slice to run", command.getvalue())
        self.assertIn("eval set version directory", command.getvalue())
        self.assertIn("ranked-list JSONL file", command.getvalue())
        self.assertIn(
            "run the turns against the standing CI sibling instead of production",
            collapsed(command.getvalue()),
        )
        self.assertIn("the word the run's record carries for its purpose", command.getvalue())

        reference = io.StringIO()
        with contextlib.redirect_stdout(reference), self.assertRaises(SystemExit) as ctx:
            main(["eval", "reference", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--run ID", reference.getvalue())
        self.assertIn("recorded evaluation run id", reference.getvalue())

    def test_status_help_names_its_blocks_and_front_door_contract(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["status", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = collapsed(out.getvalue())
        for phrase in (
            "needs attention",
            "waiting on you",
            "at a glance",
            "front-door brief",
            "root",
            "writes nothing",
        ):
            self.assertIn(phrase, help_text)

    def test_eval_candidates_help_names_root_and_default_month(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["eval", "candidates", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("root", out.getvalue())
        self.assertIn("default: last month", collapsed(out.getvalue()))


class Stubs(unittest.TestCase):
    def test_every_stub_names_its_landing_in_help_and_refusal(self) -> None:
        for argv, landing in STUBS:
            path = " ".join(argv[:2])
            with self.subTest(command=path):
                command_help = io.StringIO()
                with contextlib.redirect_stdout(command_help), self.assertRaises(SystemExit) as ctx:
                    main([*argv[:2], "--help"])
                self.assertEqual(ctx.exception.code, 0)
                normalized_command_help = collapsed(command_help.getvalue())
                self.assertIn(path, normalized_command_help)
                self.assertIn(landing, normalized_command_help)

                group_help = io.StringIO()
                with contextlib.redirect_stdout(group_help), self.assertRaises(SystemExit) as ctx:
                    main([argv[0], "--help"])
                self.assertEqual(ctx.exception.code, 0)
                normalized_group_help = collapsed(group_help.getvalue())
                self.assertIn(argv[1], normalized_group_help)
                self.assertIn(landing, normalized_group_help)

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = main(argv)
                self.assertNotEqual(code, 0)
                self.assertIn("not implemented", err.getvalue())
                self.assertIn(landing, err.getvalue())


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

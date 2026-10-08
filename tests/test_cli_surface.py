"""The recorded CLI surface, with `backup drill` beside it.

``--help`` lists every command group, and every stub prints "not
implemented" and exits non-zero. A bare invocation prints the start screen,
and every stub names where it lands.
"""

import argparse
import contextlib
import io
import unittest
from pathlib import Path

from gideon.cli import SITUATIONS, _label_stub_groups, _stub, main, start_screen
from gideon.host.steps import STEPS

TOP_LEVEL = [
    "host", "render", "apply", "preflight", "install", "upgrade", "tls",
    "users", "secrets", "engine", "worker", "models", "corpus", "index", "registry", "eval", "proposals", "status",
    "backup", "restore", "audit", "retention", "alerts",
]

# Each stub with the phrase its help and refusal must carry: the slice it
# lands in, or the command's own words.
STUBS = [
    (["host", "gpu"], "escape hatch"),
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


def help_entries(text: str) -> dict[str, str]:
    """Read argparse's four-space subcommand entries and their wrapped lines."""
    entries: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("    ") and len(line) > 4 and not line[4].isspace():
            current, _, summary = line.strip().partition(" ")
            entries[current] = collapsed(summary)
        elif line.startswith("     ") and current is not None:
            entries[current] = collapsed(f"{entries[current]} {line}")
        else:
            current = None
    return entries


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
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(
            commands[-1],
            f"gideon --help, and the operator card at {root}/docs/runbooks/start-here.md",
        )
        self.assertIn("runs as root", screen)
        self.assertIn("at most once a sitting", collapsed(screen))

    def test_start_screen_names_the_card_under_a_given_tree(self) -> None:
        screen = start_screen(Path("/tmp/fictitious-tree"))
        self.assertIn(
            "  gideon --help, and the operator card at "
            "/tmp/fictitious-tree/docs/runbooks/start-here.md",
            screen.splitlines(),
        )

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

    def test_upgrade_help_lists_disruption_acknowledgment(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["upgrade", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = collapsed(out.getvalue())
        self.assertIn("--acknowledge-disruption", help_text)
        self.assertIn(
            "pass the disruption acknowledgment to the new release's host provision",
            help_text,
        )

    def test_corpus_help_names_the_watch(self) -> None:
        group = io.StringIO()
        with contextlib.redirect_stdout(group), self.assertRaises(SystemExit) as ctx:
            main(["corpus", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("upstream watch", group.getvalue())
        self.assertIn("watch", help_entries(group.getvalue()))

        command = io.StringIO()
        with contextlib.redirect_stdout(command), self.assertRaises(SystemExit) as ctx:
            main(["corpus", "watch", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("fetches no corpus file", command.getvalue().lower())
        self.assertIn("weekly timer", command.getvalue())

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
        self.assertIn("--kind {manual,smoke,nightly}", command.getvalue())
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
        run_help = collapsed(command.getvalue())
        self.assertIn("nightly also waits for a held engine and stops at 06:00", run_help)
        self.assertIn(
            "--decision a decision run: five repeats paired against --against's recorded run",
            run_help,
        )
        self.assertIn("--force start outside the window, recorded on the run row", run_help)
        self.assertIn("--against ID recorded run a --decision run pairs against", run_help)
        self.assertIn(
            "--challenger run the committed configuration experiment beside the release's configuration on the CI sibling",
            run_help,
        )

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
            "developer",
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
    def test_top_level_labels_only_groups_whose_commands_are_all_stubs(self) -> None:
        """A group holding only stubs carries their landing on its top-level line; any other group none."""
        top_help = io.StringIO()
        with contextlib.redirect_stdout(top_help), self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        top_entries = help_entries(top_help.getvalue())
        stub_only: set[str] = set()
        mixed: set[str] = set()
        for group in {argv[0] for argv, _ in STUBS}:
            with self.subTest(group=group):
                group_help = io.StringIO()
                with contextlib.redirect_stdout(group_help), self.assertRaises(SystemExit) as ctx:
                    main([group, "--help"])
                self.assertEqual(ctx.exception.code, 0)
                stub_names = {argv[1] for argv, _ in STUBS if argv[0] == group}
                listed_names = set(help_entries(group_help.getvalue()))
                self.assertTrue(stub_names)
                self.assertTrue(listed_names)
                self.assertLessEqual(stub_names, listed_names)
                entry = top_entries[group]
                landings = {landing for argv, landing in STUBS if argv[0] == group}
                if stub_names == listed_names:
                    stub_only.add(group)
                    summary, separator, tail = entry.partition(" (")
                    self.assertTrue(summary)
                    self.assertEqual(separator, " (")
                    self.assertTrue(tail.endswith(")"))
                    for landing in landings:
                        self.assertIn(landing, tail[:-1])
                else:
                    mixed.add(group)
                    for landing in landings:
                        self.assertNotIn(landing, entry)
        self.assertTrue(stub_only)
        self.assertTrue(mixed)

    def test_pass_deduplicates_landings_and_leaves_mixed_groups_plain(self) -> None:
        """The pass labels a stub-only group with its distinct landings in order and leaves the rest plain."""
        parser = argparse.ArgumentParser(prog="example")
        commands = parser.add_subparsers(dest="command")

        same = commands.add_parser("same", help="same summary")
        same_sub = same.add_subparsers(dest="subcommand")
        same_sub.add_parser("first").set_defaults(handler=_stub, landing="first place")
        same_sub.add_parser("second").set_defaults(handler=_stub, landing="first place")

        distinct = commands.add_parser("distinct", help="distinct summary")
        distinct_sub = distinct.add_subparsers(dest="subcommand")
        distinct_sub.add_parser("first").set_defaults(handler=_stub, landing="first place")
        distinct_sub.add_parser("second").set_defaults(handler=_stub, landing="second place")

        mixed = commands.add_parser("mixed", help="mixed summary")
        mixed_sub = mixed.add_subparsers(dest="subcommand")
        mixed_sub.add_parser("first").set_defaults(handler=_stub, landing="first place")
        mixed_sub.add_parser("working").set_defaults(handler=lambda _args: 0)

        commands.add_parser("plain", help="plain summary")
        _label_stub_groups(commands)
        entries = help_entries(parser.format_help())
        self.assertEqual(entries["same"], "same summary (first place)")
        self.assertEqual(entries["distinct"], "distinct summary (first place; second place)")
        self.assertEqual(entries["mixed"], "mixed summary")
        self.assertEqual(entries["plain"], "plain summary")

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
        help_text = collapsed(out.getvalue())
        self.assertIn(
            "--no-gpu",
            help_text,
        )
        self.assertIn(
            "declare a host without a GPU: the engine is pinned out",
            help_text,
        )
        self.assertIn("--build-box", help_text)
        self.assertIn("declare this host the build box", help_text)
        self.assertIn("KVM, registry, and", help_text)
        self.assertIn("--acknowledge-disruption", help_text)
        self.assertIn(
            "acknowledge that this run may restart or reboot every container on the box, "
            "after the maintenance window is announced",
            help_text,
        )

    def test_provision_modes_are_mutually_exclusive(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            main(["host", "provision", "--no-gpu", "--build-box"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())

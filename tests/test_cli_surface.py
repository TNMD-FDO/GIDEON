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
from unittest.mock import patch

from gideon.cli import (
    SITUATIONS,
    _label_stub_groups,
    _stub,
    build_parser,
    main,
    start_screen,
)
from gideon.host.index.generations import COLLECTIONS
from gideon.host.steps import STEPS

TOP_LEVEL = [
    "host", "render", "apply", "preflight", "install", "upgrade", "uninstall", "tls",
    "users", "secrets", "engine", "worker", "models", "corpus", "index", "registry", "eval", "proposals", "status",
    "backup", "restore", "audit", "retention", "alerts",
]

# Each stub with the phrase its help and refusal must carry: the slice it
# lands in, or the command's own words.
STUBS = [
    (["host", "gpu"], "escape hatch"),
    (["registry", "gc"], "no slice scheduled"),
    (["audit", "query"], "no slice scheduled"),
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
    def test_retention_sweep_help_names_its_work_and_remaining_work(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["retention", "sweep", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = collapsed(out.getvalue())
        for phrase in (
            "chats idle past the office's retention",
            "the files their messages name",
            "feedback rows past that interval or left on a swept chat",
            "one audit row per swept chat",
            "Bindings, index points, reference counts",
            "the uploads-root walk",
            "the files of a chat its user deleted",
            "the partition drop remain for slice 6",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, help_text)
        self.assertNotIn("not implemented", help_text)
        self.assertNotIn("lands in", help_text)

    def test_retention_group_has_no_landing(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(help_entries(out.getvalue())["retention"], "retention sweeps")

    def test_index_gc_help_names_the_hold(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["index", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("drop retired generations past their seven-day hold", collapsed(out.getvalue()))

    def test_index_promote_help_names_the_built_generation(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["index", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("switch serving to a built generation", collapsed(out.getvalue()))

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

    def test_uninstall_help_names_purge_scope(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["uninstall", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = collapsed(out.getvalue())
        self.assertIn("--purge", help_text)
        self.assertIn("remove /etc/gideon, GIDEON's /data directories, and its volumes", help_text)
        self.assertIn("never /data itself", help_text)

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
        self.assertIn("--generation NAME", command.getvalue())
        self.assertIn("frozen slice to run", command.getvalue())
        self.assertIn("eval set version directory", command.getvalue())
        self.assertIn("ranked-list JSONL file", command.getvalue())
        self.assertIn(
            "run the turns against the standing CI sibling instead of production",
            collapsed(command.getvalue()),
        )
        self.assertIn("the word the run's record carries for its purpose", command.getvalue())
        run_help = collapsed(command.getvalue())
        self.assertIn(
            "--generation NAME index generation a slice reading the stores runs over; "
            "default: the stack's live one",
            run_help,
        )
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


class IndexReport(unittest.TestCase):
    def test_help_describes_the_inventory_and_optional_generation(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["index", "report", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = collapsed(out.getvalue())
        for phrase in ("<generation>", "corpus inventory", "lockfiles", "live generation", "build report"):
            self.assertIn(phrase, help_text)

    def test_two_positionals_are_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            main(["index", "report", "first", "second"])
        self.assertEqual(ctx.exception.code, 2)


class IndexBuild(unittest.TestCase):
    def test_sample_dispatches_to_the_non_host_entry_with_its_parsed_arguments(self) -> None:
        from gideon.evaluation import sample

        collection = COLLECTIONS[0].name
        for flags, label in (([], None), (["--lockfile", "corpus-2099-01-03"], "corpus-2099-01-03")):
            with (
                self.subTest(label=label),
                patch.object(sample, "run_sample_build", return_value=3) as run,
                patch("gideon.host.cli.run_index_build") as ordinary,
            ):
                self.assertEqual(main(["index", "build", collection, "--sample", *flags]), 3)
            ordinary.assert_not_called()
            run.assert_called_once()
            args = run.call_args.args[0]
            self.assertEqual((args.collection, args.lockfile, args.sample), (collection, label, True))

    def test_sample_dispatch_guard_reports_an_exception_without_a_traceback(self) -> None:
        from gideon.evaluation import sample

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sample, "run_sample_build", side_effect=ValueError("Fictitious sample defect")),
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(["index", "build", COLLECTIONS[0].name, "--sample"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("gideon index build: internal error", errors.getvalue())
        self.assertIn("Fix:", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_ordinary_build_dispatches_to_the_host_entry(self) -> None:
        with patch("gideon.host.cli.run_index_build", return_value=3) as run:
            self.assertEqual(main(["index", "build", COLLECTIONS[0].name]), 3)
        run.assert_called_once()
        self.assertFalse(run.call_args.args[0].sample)

    def test_help_lists_collections_and_lockfile_selection(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["index", "build", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = collapsed(out.getvalue())
        for collection in COLLECTIONS:
            self.assertIn(collection.name, help_text)
        for phrase in ("--lockfile LABEL", "newest", "--sample", "build the eval sample generation on the CI sibling",
                       "lexical index", "its near-duplicate clusters", "from the record",
                       "its vectors in the vector store", "build report",
                       "cited case's risk state for the generation", "distribution of risk states"):
            self.assertIn(phrase, help_text)

    def test_collection_required_for_real_build_and_unknown_choice_refuses(self) -> None:
        for argv in (["index", "build"], ["index", "build", "unknown"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_parser_and_real_handler_accept_lockfile_and_default_to_newest(self) -> None:
        for flags, label in (([], None), (["--lockfile", "corpus-2099-01-03"], "corpus-2099-01-03")):
            args = build_parser().parse_args(["index", "build", COLLECTIONS[0].name, *flags])
            self.assertEqual(args.collection, COLLECTIONS[0].name)
            self.assertEqual(args.lockfile, label)
            self.assertFalse(args.sample)
            self.assertFalse(hasattr(args, "dense_limit"))
            with patch("gideon.host.index.build.run_index_build", return_value=0) as run:
                self.assertEqual(main(["index", "build", COLLECTIONS[0].name, *flags]), 0)
            self.assertEqual(run.call_args.args[0].lockfile, label)
            self.assertNotIn("dense_limit", run.call_args.kwargs)

    def test_handler_guards_unexpected_errors(self) -> None:
        err = io.StringIO()
        with patch("gideon.host.index.build.run_index_build", side_effect=ValueError("fictitious defect")), contextlib.redirect_stderr(err):
            self.assertEqual(main(["index", "build", COLLECTIONS[0].name]), 1)
        self.assertIn("gideon index build: internal error", err.getvalue())
        self.assertIn("Fix:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


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

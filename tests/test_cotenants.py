"""Running-container ownership and disruption refusal contracts."""

import os
import subprocess
import unittest
from collections.abc import Sequence
from typing import cast

from gideon.host import report
from gideon.host.cotenants import (
    ACKNOWLEDGE_DISRUPTION_FLAG,
    DOCKER_ACTIVE_ARGV,
    DOCKER_PS_ARGV,
    DOCKER_VERSION_ARGV,
    OWNERSHIP_PREFIX,
    ContainerRow,
    DaemonState,
    describe,
    foreign,
    guard,
    is_marked,
    network_ps_argv,
    parse_rows,
    publish_ps_argv,
    running_containers,
)
from gideon.host.lock import HostLock
from gideon.host.render import ci, compose, drill
from gideon.host.steps import ProvisionContext, StepFailure
from gideon.host.sysio import Host


def completed(
    argv: Sequence[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class RunHost:
    """A command-only host that refuses any unlisted read."""

    def __init__(
        self, answers: dict[tuple[str, ...], subprocess.CompletedProcess[str]]
    ) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append(key)
        if key not in self.answers:
            raise AssertionError(f"unexpected command: {key}")
        return self.answers[key]


def context(host: RunHost, *, acknowledged: bool = False) -> ProvisionContext:
    return ProvisionContext(
        host=cast(Host, host),
        lock=cast(HostLock, object()),
        site=None,
        disruption_acknowledged=acknowledged,
    )


class Reader(unittest.TestCase):
    def test_publish_read_filters_the_shared_name_and_project_format(self) -> None:
        self.assertEqual(
            publish_ps_argv(443),
            ("docker", "ps", "--filter", "publish=443", "--format", DOCKER_PS_ARGV[-1]),
        )

    def test_network_read_filters_the_shared_name_and_project_format(self) -> None:
        self.assertEqual(
            network_ps_argv("gideon_integration"),
            (
                "docker", "ps", "--filter", "network=gideon_integration", "--format",
                DOCKER_PS_ARGV[-1],
            ),
        )

    def test_parser_returns_valid_rows_with_optional_project(self) -> None:
        cases = (
            ("one\tgideon\n", (ContainerRow("one", "gideon"),)),
            (
                "one\tgideon\ntwo\ttranscribe\n",
                (ContainerRow("one", "gideon"), ContainerRow("two", "transcribe")),
            ),
            ("gideon-registry\t\n", (ContainerRow("gideon-registry", None),)),
            ("", ()),
        )
        for stdout, expected in cases:
            with self.subTest(stdout=stdout):
                self.assertEqual(parse_rows(stdout), expected)

    def test_absent_docker_stops_before_daemon_and_listing_reads(self) -> None:
        host = RunHost({DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV, returncode=127)})

        listing = running_containers(cast(Host, host))

        self.assertIs(listing.state, DaemonState.ABSENT)
        self.assertEqual(listing.rows, ())
        self.assertEqual(host.calls, [DOCKER_VERSION_ARGV])

    def test_inactive_daemon_stops_before_listing_read(self) -> None:
        host = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, returncode=3, stdout="inactive\n"),
            }
        )

        listing = running_containers(cast(Host, host))

        self.assertIs(listing.state, DaemonState.NOT_ACTIVE)
        self.assertEqual(host.calls, [DOCKER_VERSION_ARGV, DOCKER_ACTIVE_ARGV])

    def test_active_daemon_with_no_running_containers(self) -> None:
        host = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, stdout="active\n"),
                DOCKER_PS_ARGV: completed(DOCKER_PS_ARGV),
            }
        )

        listing = running_containers(cast(Host, host))

        self.assertIs(listing.state, DaemonState.LISTED)
        self.assertEqual(listing.rows, ())
        self.assertEqual(host.calls, [DOCKER_VERSION_ARGV, DOCKER_ACTIVE_ARGV, DOCKER_PS_ARGV])

    def test_unreadable_listing_keeps_first_diagnostic_line(self) -> None:
        host = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, stdout="active\n"),
                DOCKER_PS_ARGV: completed(
                    DOCKER_PS_ARGV, returncode=1, stderr="permission denied\nmore detail\n"
                ),
            }
        )

        listing = running_containers(cast(Host, host))

        self.assertIs(listing.state, DaemonState.UNREADABLE)
        self.assertEqual(listing.diagnostic, "permission denied")

    def test_tab_joined_rows_skip_malformed_lines_and_empty_names(self) -> None:
        host = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, stdout="active\n"),
                DOCKER_PS_ARGV: completed(
                    DOCKER_PS_ARGV,
                    stdout="one\tproject-a\nregistry\t\nmalformed\n\tproject-b\n",
                ),
            }
        )

        listing = running_containers(cast(Host, host))

        self.assertEqual(
            listing.rows,
            (ContainerRow("one", "project-a"), ContainerRow("registry", None)),
        )
        self.assertEqual(
            DOCKER_PS_ARGV[-1],
            '{{.Names}}\t{{.Label "com.docker.compose.project"}}',
        )


class Ownership(unittest.TestCase):
    def test_mark_recognizes_projects_and_the_unlabelled_registry_name(self) -> None:
        cases = (
            (ContainerRow("container", "gideon"), True),
            (ContainerRow("container", "gideon-ci"), True),
            (ContainerRow("container", "gideon-drill"), True),
            (ContainerRow("gideon-registry", None), True),
            (ContainerRow("container", "transcribe"), False),
            (ContainerRow("gideon-worker", "transcribe"), False),
            (ContainerRow("other-box", None), False),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(is_marked(row), expected)

    def test_gideon_projects_and_unlabeled_registry_are_marked(self) -> None:
        projects = (
            "gideon",
            "gideon-ci",
            "gideon-ci-sentinel",
            "gideon-ci-contract",
            "gideon-drill",
        )
        rows = [ContainerRow(f"container-{project}", project) for project in projects]
        rows.extend((ContainerRow("gideon-registry", None), ContainerRow("other", None)))

        self.assertEqual(foreign(rows), {None: ["other"]})

    def test_foreign_projects_keep_first_seen_order_and_render_on_one_line(self) -> None:
        groups = foreign(
            (
                ContainerRow("alpha-one", "alpha"),
                ContainerRow("loose-one", None),
                ContainerRow("beta-one", "beta"),
                ContainerRow("alpha-two", "alpha"),
                ContainerRow("loose-two", None),
            )
        )

        self.assertEqual(list(groups), ["alpha", None, "beta"])
        self.assertEqual(
            describe(groups),
            "project alpha (alpha-one, alpha-two); no Compose project "
            "(loose-one, loose-two); project beta (beta-one)",
        )

    def test_named_projects_carry_the_ownership_prefix(self) -> None:
        for project in (compose.PROJECT_NAME, drill.DRILL_PROJECT, ci.CI_PROJECT):
            with self.subTest(project=project):
                self.assertTrue(project.startswith(OWNERSHIP_PREFIX))


class Guard(unittest.TestCase):
    def test_acknowledgment_reads_nothing(self) -> None:
        host = RunHost({})

        guard(context(host, acknowledged=True), "restarting Docker")

        self.assertEqual(host.calls, [])

    def test_absent_docker_and_empty_listing_pass(self) -> None:
        absent = RunHost({DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV, returncode=127)})
        guard(context(absent), "restarting Docker")
        self.assertEqual(absent.calls, [DOCKER_VERSION_ARGV])

        empty = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, stdout="active\n"),
                DOCKER_PS_ARGV: completed(DOCKER_PS_ARGV),
            }
        )
        guard(context(empty), "restarting Docker")
        self.assertEqual(empty.calls, [DOCKER_VERSION_ARGV, DOCKER_ACTIVE_ARGV, DOCKER_PS_ARGV])

    def test_inactive_daemon_refuses_and_names_live_restore_without_listing(self) -> None:
        host = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, returncode=3, stdout="inactive\n"),
            }
        )

        with self.assertRaises(StepFailure) as raised:
            guard(context(host), "restarting Docker")

        self.assertIn("live-restore", raised.exception.detail)
        self.assertIn("Start Docker", raised.exception.fix)
        self.assertIn(ACKNOWLEDGE_DISRUPTION_FLAG, raised.exception.fix)
        self.assertEqual(host.calls, [DOCKER_VERSION_ARGV, DOCKER_ACTIVE_ARGV])

    def test_unreadable_listing_refuses_with_diagnostic_and_repair(self) -> None:
        host = RunHost(
            {
                DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, stdout="active\n"),
                DOCKER_PS_ARGV: completed(
                    DOCKER_PS_ARGV, returncode=1, stderr="permission denied\nsecond line\n"
                ),
            }
        )

        with self.assertRaises(StepFailure) as raised:
            guard(context(host), "restarting Docker")

        self.assertIn("permission denied", raised.exception.detail)
        self.assertNotIn("second line", raised.exception.detail)
        self.assertIn("Repair docker ps", raised.exception.fix)

    def test_foreign_listing_names_mutation_and_fix_in_both_command_forms(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        for installed, command_prefix in (
            (False, "sudo python3 -m gideon"),
            (True, "gideon"),
        ):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                host = RunHost(
                    {
                        DOCKER_VERSION_ARGV: completed(DOCKER_VERSION_ARGV),
                        DOCKER_ACTIVE_ARGV: completed(DOCKER_ACTIVE_ARGV, stdout="active\n"),
                        DOCKER_PS_ARGV: completed(DOCKER_PS_ARGV, stdout="foreign-box\tother\n"),
                    }
                )

                with self.assertRaises(StepFailure) as raised:
                    guard(context(host), "restarting Docker")

                self.assertEqual(
                    raised.exception.detail,
                    "restarting Docker reaches every container on the box, and containers "
                    "outside GIDEON's ownership mark are running: project other (foreign-box)",
                )
                self.assertEqual(
                    raised.exception.fix,
                    "Announce a maintenance window to every project sharing the daemon "
                    "(docs/runbooks/install-upgrade.md §9), then re-run "
                    f"{command_prefix} host provision {ACKNOWLEDGE_DISRUPTION_FLAG}.",
                )

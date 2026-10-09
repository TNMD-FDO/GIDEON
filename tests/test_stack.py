"""Command shapes for one-off service-image execution."""

import unittest
from pathlib import Path

from gideon.host.render.api import API_MOUNT_TARGET, API_WORKING_DIRECTORY
from gideon.host.stack import (
    compose_project_argv,
    container_remove_argv,
    image_present_argv,
    image_run_argv,
)


class ImageCommands(unittest.TestCase):
    def test_compose_project_without_rendered_file(self) -> None:
        self.assertEqual(
            compose_project_argv("gideon-drill", "down", "--volumes", "--remove-orphans"),
            ["docker", "compose", "-p", "gideon-drill", "down", "--volumes", "--remove-orphans"],
        )

    def test_run_mounts_checkout_package_read_only_without_network_or_daemon_logs(self) -> None:
        reference = "registry.example/gideon@sha256:" + "a" * 64
        checkout = Path("/fictitious-checkout")
        self.assertEqual(
            image_run_argv(
                reference, checkout, "python", "-m", "gideon.casecite", name="gideon-casecite-1"
            ),
            [
                "docker", "run", "--rm", "-i", "--name", "gideon-casecite-1", "--network", "none",
                "--log-driver", "none", "-v",
                f"{checkout / 'gideon'}:{API_MOUNT_TARGET}:ro",
                "-w", API_WORKING_DIRECTORY, reference,
                "python", "-m", "gideon.casecite",
            ],
        )

    def test_present_probe_uses_the_same_reference(self) -> None:
        reference = "registry.example/gideon@sha256:" + "b" * 64
        self.assertEqual(image_present_argv(reference), ["docker", "image", "inspect", reference])

    def test_remove_forces_the_named_container(self) -> None:
        self.assertEqual(
            container_remove_argv("gideon-casecite-1"),
            ["docker", "rm", "-f", "gideon-casecite-1"],
        )

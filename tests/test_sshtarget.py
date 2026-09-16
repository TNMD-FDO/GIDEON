"""SSH and remote checksum argv contracts."""

import shlex
import unittest
from types import SimpleNamespace
from typing import cast

from gideon.host.site import SiteConfig
from gideon.host.sshtarget import (
    BACKUP_KEY,
    BACKUP_KNOWN_HOSTS,
    SSH_TIMEOUT_SECONDS,
    parse_check_output,
    remote_check_argv,
    remote_script,
    rsync_ssh_option,
    ssh_argv,
)


def site() -> SiteConfig:
    return cast(
        SiteConfig,
        SimpleNamespace(
            backup=SimpleNamespace(
                target=SimpleNamespace(
                    host="nas.example",
                    path="/volume/backup root",
                    user="backup-user",
                )
            )
        ),
    )


class SshTarget(unittest.TestCase):
    def test_ssh_argv(self) -> None:
        self.assertEqual(
            ssh_argv(site(), "true", "a remote word"),
            [
                "ssh",
                "-i",
                str(BACKUP_KEY),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={BACKUP_KNOWN_HOSTS}",
                "-o",
                f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
                "backup-user@nas.example",
                "true",
                "a remote word",
            ],
        )

    def test_rsync_ssh_option_is_one_shell_joined_value(self) -> None:
        self.assertEqual(
            rsync_ssh_option(),
            shlex.join(
                [
                    "ssh",
                    "-i",
                    str(BACKUP_KEY),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-o",
                    f"UserKnownHostsFile={BACKUP_KNOWN_HOSTS}",
                    "-o",
                    f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
                ]
            ),
        )

    def test_remote_script_keeps_quotes_and_spaces_in_one_word(self) -> None:
        script = 'printf "%s" "a phrase with \'quotes\'"'
        self.assertEqual(remote_script(script), f"sh -c {shlex.quote(script)}")
        self.assertEqual(shlex.split(remote_script(script)), ["sh", "-c", script])

    def test_remote_check_argv_quotes_directory_and_keeps_stdin_out_of_argv(self) -> None:
        expected_script = "cd '/volume/backup root' && sha256sum -c -"
        self.assertEqual(
            remote_check_argv(site(), "/volume/backup root"),
            ssh_argv(site(), remote_script(expected_script)),
        )
        argv = remote_check_argv(site(), "/volume/backup root")
        self.assertEqual(argv[-1], remote_script(expected_script))
        self.assertNotIn("expected-digest", " ".join(argv))

    def test_parse_check_output(self) -> None:
        okay, failed = parse_check_output(
            "files/etc.conf: OK\nfiles/a path: FAILED\n"
            "files/gone: FAILED open or read\n"
            "sha256sum: WARNING: 1 computed checksum did NOT match\n"
        )
        self.assertEqual(okay, ("files/etc.conf",))
        self.assertEqual(failed, ("files/a path", "files/gone"))

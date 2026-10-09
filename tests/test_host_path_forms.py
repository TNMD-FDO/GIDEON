"""Seven host command entries render their refusals in the invoked form."""

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gideon import cli
from gideon.host import report
from gideon.host.sysio import PathLike, RealHost


class HostPathForms(unittest.TestCase):
    def test_root_fixes_follow_the_entry_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        with (
            patch.object(RealHost, "geteuid", return_value=1000) as geteuid,
            patch.object(RealHost, "read_text", side_effect=AssertionError("box read")) as read_text,
            patch.object(RealHost, "exists", side_effect=AssertionError("box exists")) as exists,
            patch.object(RealHost, "listdir", side_effect=AssertionError("box list")) as listdir,
            patch.object(RealHost, "run", side_effect=AssertionError("box command")) as run,
        ):
            for installed in (False, True):
                environment = (
                    {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                    if installed else {}
                )
                for argv, path in (
                    (["host", "provision"], "host provision"),
                    (["preflight"], "preflight"),
                    (["users", "reconcile"], "users reconcile"),
                    (["tls", "reload"], "tls reload"),
                    (["alerts", "test"], "alerts test"),
                    (["worker", "verify"], "worker verify"),
                ):
                    with self.subTest(installed=installed, path=path):
                        out, err = io.StringIO(), io.StringIO()
                        with (
                            patch.dict(os.environ, environment, clear=True),
                            contextlib.redirect_stdout(out),
                            contextlib.redirect_stderr(err),
                        ):
                            code = cli.main(argv)
                        command = "gideon" if installed else "python3 -m gideon"
                        sudo_command = "gideon" if installed else "sudo python3 -m gideon"
                        self.assertEqual(code, 1)
                        if path == "alerts test":
                            self.assertEqual(
                                out.getvalue(),
                                f"preconditions: refuse — root privileges are required. Fix: Run {sudo_command} alerts test.\n",
                            )
                            self.assertEqual(err.getvalue(), "")
                        elif path == "worker verify":
                            self.assertEqual(out.getvalue(), "")
                            self.assertEqual(
                                err.getvalue(),
                                f"preconditions: refuse — root privileges are required Fix: Run {sudo_command} worker verify, then retry.\n",
                            )
                        else:
                            self.assertEqual(out.getvalue(), "")
                            period = "." if path in {"users reconcile", "tls reload"} else ""
                            self.assertEqual(
                                err.getvalue(),
                                f"gideon {path}: root is required{period} Fix: Run {command} {path} as root, for example with sudo.\n",
                            )
                        if installed:
                            self.assertNotIn("python3 -m gideon", out.getvalue() + err.getvalue())
            self.assertEqual(geteuid.call_count, 12)
            read_text.assert_not_called()
            exists.assert_not_called()
            listdir.assert_not_called()
            run.assert_not_called()

    def test_registry_mirror_skopeo_refusal_follows_the_entry_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        original_read_text = RealHost.read_text
        site_path = Path("/etc/gideon/site.yaml")
        images_path = Path(__file__).resolve().parents[1] / "images.lock"
        reads: list[Path] = []

        def read_artifact(host: RealHost, path: PathLike, *, encoding: str = "utf-8") -> str:
            selected = Path(path)
            reads.append(selected)
            if selected == site_path:
                raise FileNotFoundError(os.fspath(path))
            if selected == images_path:
                return original_read_text(host, path, encoding=encoding)
            raise AssertionError(f"unexpected read: {path}")

        for installed in (False, True):
            with self.subTest(installed=installed):
                reads.clear()
                environment = (
                    {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                    if installed else {}
                )
                out, err = io.StringIO(), io.StringIO()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(RealHost, "read_text", autospec=True, side_effect=read_artifact),
                    patch.object(RealHost, "run", autospec=True, side_effect=OSError("skopeo unavailable")) as run,
                    patch.object(RealHost, "exists", side_effect=AssertionError("box exists")),
                    patch.object(RealHost, "listdir", side_effect=AssertionError("box list")),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = cli.main(["registry", "mirror", "--to", "127.0.0.1:5000"])
                command = "gideon" if installed else "sudo python3 -m gideon"
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(
                    err.getvalue(),
                    f"gideon registry mirror: skopeo is unavailable: skopeo unavailable Fix: Run {command} host provision --only host-tools, then re-run registry mirror.\n",
                )
                self.assertEqual(reads, [site_path, images_path])
                run.assert_called_once()
                self.assertEqual(run.call_args.args[1], ["skopeo", "--version"])
                if installed:
                    self.assertNotIn("python3 -m gideon", err.getvalue())


if __name__ == "__main__":
    unittest.main()

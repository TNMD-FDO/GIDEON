"""The install path's entry commands read the invocation form before refusing."""

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gideon import cli
from gideon.host import report, uninstall
from gideon.host.sysio import RealHost


class InstallPathForms(unittest.TestCase):
    def test_entry_fixes_follow_the_run_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        with (
            patch.object(RealHost, "geteuid", return_value=1000) as geteuid,
            patch.object(RealHost, "read_text", side_effect=AssertionError("box read")) as read_text,
        ):
            for installed in (False, True):
                for argv, path in (
                    (["apply"], "apply"),
                    (["render"], "render"),
                    (["secrets", "rotate", "engine_api_key"], "secrets rotate engine_api_key"),
                    (["models", "pull"], "models pull"),
                    (["install"], "install"),
                    (["upgrade", "v1.2.3"], "upgrade v1.2.3"),
                    (["uninstall"], "uninstall"),
                ):
                    with self.subTest(installed=installed, path=path):
                        environment = (
                            {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                            if installed
                            else {}
                        )
                        out, err = io.StringIO(), io.StringIO()
                        with (
                            patch.dict(os.environ, environment, clear=True),
                            contextlib.redirect_stdout(out),
                            contextlib.redirect_stderr(err),
                        ):
                            code = cli.main(argv)
                        self.assertEqual(code, 1)
                        self.assertEqual(out.getvalue(), "")
                        prefix = "gideon" if installed else "python3 -m gideon"
                        sudo_prefix = "gideon" if installed else "sudo python3 -m gideon"
                        if path == "install":
                            fix = f"Fix: Run {sudo_prefix} install, then retry."
                        elif path.startswith("upgrade"):
                            fix = f"Fix: Run {sudo_prefix} {path} as root."
                        elif path == "uninstall":
                            checkout = Path(uninstall.__file__).resolve().parents[2]
                            fix = f"Fix: Run sudo python3 -m gideon uninstall from {checkout}."
                        elif path.startswith("secrets rotate") or path == "models pull":
                            fix = f"Fix: Run {sudo_prefix} {path}."
                        else:
                            fix = f"Fix: Run {prefix} {path} as root, for example with sudo."
                        self.assertIn(fix, err.getvalue())
            self.assertEqual(geteuid.call_count, 14)
            read_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()

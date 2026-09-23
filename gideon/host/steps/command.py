"""The ``gideon`` command on ``PATH``: the installed release run as root from its home."""

import shlex
from pathlib import Path
from stat import S_IMODE

from gideon.host.steps import CheckResult, Disposition, ProvisionContext, Step

COMMAND_PATH = Path("/usr/local/bin/gideon")
INSTALL_HOME = Path("/opt/gideon")
COMMAND_MODE = 0o755
# The long form: the fix for a missing or broken command cannot be the command.
COMMAND_FIX = "Run sudo python3 -m gideon host provision --only gideon-command from the checkout."
COMMAND_ANNOUNCEMENT = "The gideon command is installed: run gideon from any directory to see where to start."


def command_text(home: Path) -> str:
    """The wrapper for the release installed at *home*: refuse without a checkout, re-run under sudo, run from home."""
    refusal = (
        f"gideon: {home} holds no GIDEON checkout. Fix: clone the release tag to {home} "
        "as docs/runbooks/install-upgrade.md §1 says, then re-run."
    )
    return (
        "#!/bin/sh\n"
        "# Owned by gideon host provision, which rewrites it at every run: an edit does not survive.\n"
        f"# Runs {home}'s release as root from {home}, whatever the working directory.\n"
        f"[ -f {shlex.quote(str(home / 'gideon' / '__main__.py'))} ] || "
        f"{{ echo {shlex.quote(refusal)} >&2; exit 1; }}\n"
        f"[ \"$(id -u)\" = 0 ] || exec sudo {shlex.quote(str(COMMAND_PATH))} \"$@\"\n"
        f"cd {shlex.quote(str(home))} || exit 1\n"
        "exec python3 -m gideon \"$@\"\n"
    )


class GideonCommandStep(Step):
    """Install the wrapper that runs the installed release from any directory."""

    name = "gideon-command"
    summary = "install /usr/local/bin/gideon, the command that runs the installed release as root from /opt/gideon"
    needs_site = False

    def check(self, context: ProvisionContext) -> CheckResult:
        try:
            current = context.host.read_text(COMMAND_PATH)
            mode = S_IMODE(context.host.stat(COMMAND_PATH).st_mode)
        except FileNotFoundError:
            return CheckResult(Disposition.DRIFT, f"{COMMAND_PATH} is missing", COMMAND_FIX)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {COMMAND_PATH}: {exc}",
                f"Repair access to {COMMAND_PATH}, then re-run provision.",
            )
        if current != command_text(INSTALL_HOME):
            return CheckResult(Disposition.DRIFT, f"{COMMAND_PATH} differs from the release's command", COMMAND_FIX)
        if mode != COMMAND_MODE:
            return CheckResult(
                Disposition.DRIFT, f"{COMMAND_PATH} mode is {mode:04o}, not {COMMAND_MODE:04o}", COMMAND_FIX
            )
        return CheckResult(Disposition.CONVERGED, f"{COMMAND_PATH} is the release's command at mode {COMMAND_MODE:04o}", "")

    def apply(self, context: ProvisionContext) -> str:
        context.host.mkdir(COMMAND_PATH.parent, mode=0o755, parents=True, exist_ok=True)
        context.host.write_text(COMMAND_PATH, command_text(INSTALL_HOME), mode=COMMAND_MODE)
        return COMMAND_ANNOUNCEMENT

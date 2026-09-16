"""Handlers for the bare-host commands."""

import argparse
import sys
from collections.abc import Callable

from gideon.host import (
    alerts,
    apply,
    backup,
    drill,
    engine,
    install,
    preflight,
    provision,
    registry,
    restore,
    rotate,
    tls,
    upgrade,
    users,
    weights,
)
from gideon.host.render import command as render_command
from gideon.host.report import refusal


def run_provision(args: argparse.Namespace) -> int:
    return provision.run_provision(args)


def run_preflight(args: argparse.Namespace) -> int:
    return preflight.run_preflight(args)


def run_gpu(args: argparse.Namespace) -> int:
    print(f"gideon {args.command_path}: not implemented", file=sys.stderr)
    return 1


def run_render(args: argparse.Namespace) -> int:
    return render_command.run_render(args)


def run_apply(args: argparse.Namespace) -> int:
    return apply.run_apply(args)


def run_secrets_rotate(args: argparse.Namespace) -> int:
    return _guarded("secrets rotate", rotate.run_secrets_rotate, args)


def run_install(args: argparse.Namespace) -> int:
    return _guarded("install", install.run_install, args)


def run_upgrade(args: argparse.Namespace) -> int:
    return _guarded("upgrade", upgrade.run_upgrade, args)


def run_registry_mirror(args: argparse.Namespace) -> int:
    return registry.run_registry_mirror(args)


def run_models_pull(args: argparse.Namespace) -> int:
    return _guarded("models pull", weights.run_models_pull, args)


def run_tls_reload(args: argparse.Namespace) -> int:
    return tls.run_tls_reload(args)


def run_users_reconcile(args: argparse.Namespace) -> int:
    return users.run_reconcile(args)


def _guarded(command: str, run: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """The command boundary: an unexpected exception is one refusal line, never a traceback."""

    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001  # CLI boundary
        print(
            refusal(
                command,
                f"internal error: {type(exc).__name__}: {exc}",
                "Report this with the command's transcript, then retry.",
            ),
            file=sys.stderr,
        )
        return 1


def run_backup_run(args: argparse.Namespace) -> int:
    return _guarded("backup run", backup.run_backup_run, args)


def run_backup_push(args: argparse.Namespace) -> int:
    return _guarded("backup push", backup.run_backup_push, args)


def run_restore(args: argparse.Namespace) -> int:
    return _guarded("restore", restore.run_restore, args)


def run_backup_drill(args: argparse.Namespace) -> int:
    return _guarded("backup drill", drill.run_backup_drill, args)


def run_alerts_test(args: argparse.Namespace) -> int:
    return _guarded("alerts test", alerts.run_alerts_test, args)


def run_engine_verify(args: argparse.Namespace) -> int:
    return _guarded("engine verify", engine.run_engine_verify, args)

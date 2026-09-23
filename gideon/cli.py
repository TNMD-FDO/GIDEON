"""The one product CLI (spec §20.2): ``python3 -m gideon <command>``.

A bare invocation prints the start screen and exits 0. A command is a stub
until a TRIP plan lands its behaviour: its help names where it lands, and it
prints "not implemented" with the same text and exits non-zero.
``preflight.sh``, ``install.sh``, and ``upgrade.sh`` at the repo root are thin
entrypoints over this CLI (§2.2).

This module is on the bare-host path (§1.9 step 3 runs ``python3 -m gideon
host provision`` on a fresh Ubuntu Server install), so at module level it may
import only the standard library, ``yaml``, and ``gideon.host`` — enforced by
tests/test_host_import_boundary.py. Commands outside the host subtree grow
heavier dependencies inside their handlers, never at module level.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import gideon
from gideon.host import cli as host_cli

_EVERYTHING_ELSE: Final = "Everything else\n  gideon --help, and the runbooks under docs/runbooks/"
_ROOT_LINE: Final = (
    "Every command runs as root: sudo asks for the password at most once a sitting,\n"
    "how often being the office's policy."
)


@dataclass(frozen=True, slots=True)
class Situation:
    """A start-screen heading and the ``(command path, argument hint)`` pairs under it."""

    heading: str
    entries: tuple[tuple[str, str], ...]


SITUATIONS: Final = (
    Situation("An alert email arrived, or something looks wrong", (("status", ""),)),
    Situation("A decision is waiting on a person", (("proposals", ""),)),
    Situation(
        "The site file, a certificate, or the mail relay changed",
        (("apply", ""), ("tls reload", ""), ("alerts test", "")),
    ),
    Situation(
        "Backups, and going back in time",
        (("backup run", ""), ("backup push", ""), ("backup drill", ""), ("restore", "--from staging|target")),
    ),
    Situation("Upgrade day", (("upgrade", "<tag>"), ("upgrade", "--rollback"))),
    Situation("After an engine or driver change", (("engine verify", ""),)),
    Situation("Someone joined or left", (("users reconcile", "--now"),)),
)


def start_screen() -> str:
    """What a bare ``gideon`` prints: the situations, the everything-else line, the root line."""
    blocks = [
        "\n".join([situation.heading, *(f"  gideon {path} {hint}".rstrip() for path, hint in situation.entries)])
        for situation in SITUATIONS
    ]
    return "\n\n".join(["Where to start:", "\n".join([*blocks, _EVERYTHING_ELSE]), _ROOT_LINE])


_LANDING: Final = {
    "host gpu": "the escape hatch, pulled only on eval evidence of reranker latency during bulk embedding (§1.5)",
    "corpus cut": "lands in slice 3 (v0.4.0)",
    "corpus install": "lands in slice 3 (v0.4.0)",
    "index build": "lands in slice 3 (v0.4.0)",
    "index promote": "lands in slice 3 (v0.4.0)",
    "index gc": "lands in slice 3 (v0.4.0)",
    "index report": "lands in slice 3 (v0.4.0)",
    "registry gc": "in the §20.2 surface, no slice scheduled",
    "audit query": "in the §20.2 surface, no slice scheduled",
    "retention sweep": "lands in slice 6 (v0.7.0)",
}


def _start(_args: argparse.Namespace) -> int:
    print(start_screen())
    return 0


def _stub(args: argparse.Namespace) -> int:
    landing = getattr(args, "landing", "")
    print(f"gideon {args.command_path}: not implemented{f'; {landing}' if landing else ''}", file=sys.stderr)
    return 1


def _stub_parser(
    parent: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    command_path: str,
    summary: str,
) -> argparse.ArgumentParser:
    """Add a stub parser whose help, description, and refusal share its landing text."""
    landing = _LANDING[command_path]
    labeled = f"{summary} ({landing})"
    parser = parent.add_parser(name, help=labeled, description=labeled)
    parser.set_defaults(handler=_stub, command_path=command_path, landing=landing)
    return parser


def _run_eval(args: argparse.Namespace) -> int:
    from gideon.evaluation import command

    return host_cli._guarded("eval run", command.run_eval, args)


def _run_reference(args: argparse.Namespace) -> int:
    from gideon.evaluation import reference_command

    return host_cli._guarded("eval reference", reference_command.run_reference, args)


def _run_proposals(args: argparse.Namespace) -> int:
    from gideon.improvement import proposals

    return host_cli._guarded("proposals", proposals.run_proposals, args)


def _run_status(args: argparse.Namespace) -> int:
    from gideon.status import command

    return host_cli._guarded("status", command.run_status, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gideon",
        description="GIDEON product CLI. Run gideon alone to see where to start.",
    )
    parser.set_defaults(handler=_start, command_path="")
    parser.add_argument(
        "--version", action="version", version=f"gideon {gideon.__version__}"
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=False)

    host = commands.add_parser("host", help="host provisioning and GPU layout (§1.5)")
    host_sub = host.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    provision = host_sub.add_parser(
        "provision", help="bring the host to the target state (check/apply)"
    )
    provision.add_argument("--only", metavar="<step>", help="run a single step")
    provision.add_argument(
        "--dry-run", action="store_true", help="report failing checks, change nothing"
    )
    provision.add_argument(
        "--list", action="store_true", help="list the provisioning steps"
    )
    provision_mode = provision.add_mutually_exclusive_group()
    provision_mode.add_argument(
        "--no-gpu",
        action="store_true",
        help="declare a host without a GPU: the engine is pinned out (§2.5, [29] item 12)",
    )
    provision_mode.add_argument(
        "--build-box",
        action="store_true",
        help="declare this host the build box: the KVM, registry, and runner steps converge here and on no other host (§1.5, §1.8; slice-0 ticket 21)",
    )
    provision.set_defaults(handler=host_cli.run_provision, command_path="host provision")
    gpu = _stub_parser(host_sub, "gpu", "host gpu", "GPU layout escape hatch (MIG)")
    gpu.add_argument("--mig", metavar="<layout>", help="MIG layout for GPU 1, e.g. 2x48")

    render = commands.add_parser(
        "render", help="site file + release + profile + host facts → /etc/gideon/rendered (§3.5)"
    )
    render.add_argument("--diff", action="store_true", help="show what would change")
    render.set_defaults(handler=host_cli.run_render, command_path="render")

    apply_ = commands.add_parser(
        "apply", help="render → diff → recreate changed services → verify (§3.5)"
    )
    apply_.set_defaults(handler=host_cli.run_apply, command_path="apply")

    preflight = commands.add_parser(
        "preflight", help="provisioning checks + install-time checks (§1.5)"
    )
    preflight.set_defaults(handler=host_cli.run_preflight, command_path="preflight")

    install = commands.add_parser(
        "install", help="install sequence: preflight → apply → backup → drill (§3.6)"
    )
    install.set_defaults(handler=host_cli.run_install, command_path="install")

    upgrade = commands.add_parser(
        "upgrade", help="upgrade to a tag or rollback with --rollback (§2.1, §3.6)"
    )
    upgrade.add_argument("tag", nargs="?", help="release tag to upgrade to")
    upgrade.add_argument(
        "--rollback", action="store_true", help="restore the pre-upgrade backup"
    )
    upgrade.add_argument(
        "--acknowledge-breaking",
        action="store_true",
        help="required to cross a major version",
    )
    upgrade.set_defaults(handler=host_cli.run_upgrade, command_path="upgrade")

    tls = commands.add_parser("tls", help="TLS certificate operations (§1.6)")
    tls_sub = tls.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    tls_reload = tls_sub.add_parser("reload", help="pick up replaced certificate files")
    tls_reload.set_defaults(handler=host_cli.run_tls_reload, command_path="tls reload")

    secrets = commands.add_parser("secrets", help="secret files: rotation (§1.7)")
    secrets_sub = secrets.add_subparsers(
        dest="subcommand", metavar="<subcommand>", required=True
    )
    rotate = secrets_sub.add_parser(
        "rotate",
        help="regenerate one rotatable secret and recreate exactly its consumers (§1.7)",
    )
    rotate.add_argument(
        "name",
        metavar="<name>",
        help=(
            "the secret to rotate: engine_api_key, webui_secret_key, searxng_secret_key "
            "(a new value written to the file), gideon_admin_api_key, gideon_eval_api_key "
            "(the file removed and re-minted); every other registry name refuses before any "
            "change, naming the office's path"
        ),
    )
    rotate.set_defaults(handler=host_cli.run_secrets_rotate, command_path="secrets rotate")

    users = commands.add_parser("users", help="user and group reconciliation (§4)")
    users_sub = users.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    reconcile = users_sub.add_parser(
        "reconcile", help="reconcile Open WebUI users against LDAP (§4.1)"
    )
    reconcile.add_argument("--now", action="store_true", help="run once, immediately")
    reconcile.set_defaults(handler=host_cli.run_users_reconcile, command_path="users reconcile")

    engine = commands.add_parser("engine", help="serving-engine operations (§5)")
    engine_sub = engine.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    verify = engine_sub.add_parser("verify", help="engine verification gate (§6.7)")
    verify.set_defaults(handler=host_cli.run_engine_verify, command_path="engine verify")

    models = commands.add_parser("models", help="model artifacts (§2.4)")
    models_sub = models.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    pull = models_sub.add_parser(
        "pull",
        help="fetch and verify the profile's models into /data/models (§5.4, §2.4)",
    )
    pull.set_defaults(handler=host_cli.run_models_pull, command_path="models pull")

    corpus = commands.add_parser("corpus", help="corpus lockfile cuts and installs (§8)")
    corpus_sub = corpus.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    cut = _stub_parser(corpus_sub, "cut", "corpus cut", "cut a corpus lockfile")
    cut.add_argument("--base", metavar="<lockfile>", help="lockfile to derive from")
    cut.add_argument("--add-courts", metavar="<ids>", help="court ids to add to the base")
    corpus_install = _stub_parser(
        corpus_sub, "install", "corpus install", "install a corpus lockfile"
    )
    corpus_install.add_argument("label", help="lockfile label, e.g. corpus-2026-08-31")

    index = commands.add_parser("index", help="index generations (§9, §7.5)")
    index_sub = index.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    build = _stub_parser(index_sub, "build", "index build", "build a new index generation")
    build.add_argument("--sample", action="store_true", help="build the eval sample")
    promote = _stub_parser(
        index_sub, "promote", "index promote", "switch serving to a generation"
    )
    promote.add_argument("gen", help="generation to promote")
    _stub_parser(index_sub, "gc", "index gc", "drop retired generations past their hold")
    _stub_parser(index_sub, "report", "index report", "report generations and build state")

    registry = commands.add_parser("registry", help="local release registry (§1.8)")
    registry_sub = registry.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    mirror = registry_sub.add_parser(
        "mirror", help="copy images.lock into the release registry by digest (§2.4)"
    )
    mirror.add_argument(
        "--to", metavar="<registry>", help="destination registry (default: the site file's registry key)"
    )
    mirror.set_defaults(handler=host_cli.run_registry_mirror, command_path="registry mirror")
    _stub_parser(registry_sub, "gc", "registry gc", "garbage-collect unreferenced blobs")

    eval_ = commands.add_parser("eval", help="evaluation suites (§18)")
    eval_sub = eval_.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    eval_run = eval_sub.add_parser("run", help="run the eval suites (§18)")
    eval_run.add_argument("--decision", action="store_true", help="a decision run")
    eval_run.add_argument("--force", action="store_true", help="run despite a dirty state")
    eval_run.add_argument(
        "--slice",
        metavar="NAME",
        help="frozen slice to run (§18.6)",
    )
    eval_run.add_argument(
        "--set",
        metavar="DIR",
        help="eval set version directory (§18.6)",
    )
    eval_run.add_argument(
        "--ranked",
        metavar="FILE",
        help="ranked-list JSONL file for the judgments metrics (§18.2)",
    )
    eval_run.set_defaults(handler=_run_eval, command_path="eval run")
    eval_reference = eval_sub.add_parser(
        "reference", help="write a reference from a recorded run (§18.6)"
    )
    eval_reference.add_argument(
        "--run",
        required=True,
        metavar="ID",
        help="recorded evaluation run id (§18.6)",
    )
    eval_reference.set_defaults(handler=_run_reference, command_path="eval reference")

    proposals = commands.add_parser(
        "proposals",
        help="read the improvement proposals (§20.2, ADR-0049); read-only report",
    )
    proposals.set_defaults(handler=_run_proposals, command_path="proposals")

    status_help = (
        "the box status: needs attention, waiting on you, at a glance "
        "(§20.2, the front-door brief); needs root; writes nothing"
    )
    status = commands.add_parser("status", help=status_help, description=status_help)
    status.set_defaults(handler=_run_status, command_path="status")

    backup = commands.add_parser("backup", help="backup set, off-box push, drill (§19)")
    backup_sub = backup.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    backup_run = backup_sub.add_parser(
        "run", help="snapshot + pgBackRest + manifest (§19.1, ADR-0005)"
    )
    backup_run.add_argument(
        "--full", action="store_true", help="force a full pgBackRest backup"
    )
    backup_run.add_argument(
        "--label", metavar="<label>", help="operator label for a safety backup"
    )
    backup_run.set_defaults(handler=host_cli.run_backup_run, command_path="backup run")
    push = backup_sub.add_parser(
        "push", help="one rsync-over-SSH to backup.target (§19.1, ADR-0026)"
    )
    push.add_argument(
        "--verify-all", action="store_true", help="verify every inventoried file"
    )
    push.set_defaults(handler=host_cli.run_backup_push, command_path="backup push")
    drill = backup_sub.add_parser(
        "drill", help="restore drill into the gideon-drill project (§19.2, [22])"
    )
    drill.set_defaults(handler=host_cli.run_backup_drill, command_path="backup drill")

    restore = commands.add_parser(
        "restore", help="restore from a backup set (§19.2, ADR-0005)"
    )
    restore.add_argument(
        "--from",
        dest="source",
        choices=["staging", "target"],
        required=True,
        help="restore from local staging or the off-box target",
    )
    restore_target = restore.add_mutually_exclusive_group()
    restore_target.add_argument("--at", metavar="<ts>", help="point in time to restore to")
    restore_target.add_argument(
        "--set",
        metavar="<label>",
        help="restore the named staging set to its archive boundary (ADR-0005 rollback)",
    )
    restore.set_defaults(handler=host_cli.run_restore, command_path="restore")

    audit = commands.add_parser("audit", help="the append-only audit log (§19)")
    audit_sub = audit.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    query = _stub_parser(
        audit_sub, "query", "audit query", "query audit rows by kb, user, or chat"
    )
    query.add_argument("--kb", metavar="<id>", help="knowledge-base id")
    query.add_argument("--user", metavar="<id>", help="OWUI user id")
    query.add_argument("--chat", metavar="<id>", help="chat id")
    query.add_argument("--since", metavar="<ts>", help="start of the window")
    query.add_argument("--until", metavar="<ts>", help="end of the window")
    query.add_argument("--json", action="store_true", help="machine-readable output")

    retention = commands.add_parser("retention", help="retention sweeps (§19)")
    retention_sub = retention.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    _stub_parser(
        retention_sub, "sweep", "retention sweep", "expire chats, uploads, partitions"
    )

    alerts = commands.add_parser("alerts", help="alerting (§19)")
    alerts_sub = alerts.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    test = alerts_sub.add_parser(
        "test", help="send one test message through the relay (§19.5)"
    )
    test.set_defaults(handler=host_cli.run_alerts_test, command_path="alerts test")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.handler(args)
    return result

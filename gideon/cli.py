"""The one product CLI (spec §20.2): ``python3 -m gideon <command>``.

A command is a stub until a TRIP plan lands its behaviour: it prints
"not implemented" and exits non-zero. ``preflight.sh``, ``install.sh``, and
``upgrade.sh`` at the repo root are thin entrypoints over this CLI (§2.2).

This module is on the bare-host path (§1.9 step 3 runs ``python3 -m gideon
host provision`` on a fresh Ubuntu Server install), so at module level it may
import only the standard library, ``yaml``, and ``gideon.host`` — enforced by
tests/test_host_import_boundary.py. Commands outside the host subtree grow
heavier dependencies inside their handlers, never at module level.
"""

import argparse
import sys
from collections.abc import Sequence

import gideon
from gideon.host import cli as host_cli


def _stub(args: argparse.Namespace) -> int:
    print(f"gideon {args.command_path}: not implemented", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gideon",
        description=(
            "GIDEON product CLI. All commands are stubs until their slice "
            "lands; the surface is spec §20.2."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"gideon {gideon.__version__}"
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

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
    gpu = host_sub.add_parser("gpu", help="GPU layout escape hatch (MIG)")
    gpu.add_argument("--mig", metavar="<layout>", help="MIG layout for GPU 1, e.g. 2x48")
    gpu.set_defaults(handler=host_cli.run_gpu, command_path="host gpu")

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
    cut = corpus_sub.add_parser("cut", help="cut a corpus lockfile")
    cut.add_argument("--base", metavar="<lockfile>", help="lockfile to derive from")
    cut.add_argument("--add-courts", metavar="<ids>", help="court ids to add to the base")
    cut.set_defaults(handler=_stub, command_path="corpus cut")
    corpus_install = corpus_sub.add_parser("install", help="install a corpus lockfile")
    corpus_install.add_argument("label", help="lockfile label, e.g. corpus-2026-08-31")
    corpus_install.set_defaults(handler=_stub, command_path="corpus install")

    index = commands.add_parser("index", help="index generations (§9, §7.5)")
    index_sub = index.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    build = index_sub.add_parser("build", help="build a new index generation")
    build.add_argument("--sample", action="store_true", help="build the eval sample")
    build.set_defaults(handler=_stub, command_path="index build")
    promote = index_sub.add_parser("promote", help="switch serving to a generation")
    promote.add_argument("gen", help="generation to promote")
    promote.set_defaults(handler=_stub, command_path="index promote")
    index_gc = index_sub.add_parser("gc", help="drop retired generations past their hold")
    index_gc.set_defaults(handler=_stub, command_path="index gc")
    report = index_sub.add_parser("report", help="report generations and build state")
    report.set_defaults(handler=_stub, command_path="index report")

    registry = commands.add_parser("registry", help="local release registry (§1.8)")
    registry_sub = registry.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    mirror = registry_sub.add_parser(
        "mirror", help="copy images.lock into the release registry by digest (§2.4)"
    )
    mirror.add_argument(
        "--to", metavar="<registry>", help="destination registry (default: the site file's registry key)"
    )
    mirror.set_defaults(handler=host_cli.run_registry_mirror, command_path="registry mirror")
    registry_gc = registry_sub.add_parser("gc", help="garbage-collect unreferenced blobs")
    registry_gc.set_defaults(handler=_stub, command_path="registry gc")

    eval_ = commands.add_parser("eval", help="evaluation suites (§18)")
    eval_sub = eval_.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    eval_run = eval_sub.add_parser("run", help="run the eval suites")
    eval_run.add_argument("--decision", action="store_true", help="a decision run")
    eval_run.add_argument("--force", action="store_true", help="run despite a dirty state")
    eval_run.set_defaults(handler=_stub, command_path="eval run")

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
    query = audit_sub.add_parser("query", help="query audit rows by kb, user, or chat")
    query.add_argument("--kb", metavar="<id>", help="knowledge-base id")
    query.add_argument("--user", metavar="<id>", help="OWUI user id")
    query.add_argument("--chat", metavar="<id>", help="chat id")
    query.add_argument("--since", metavar="<ts>", help="start of the window")
    query.add_argument("--until", metavar="<ts>", help="end of the window")
    query.add_argument("--json", action="store_true", help="machine-readable output")
    query.set_defaults(handler=_stub, command_path="audit query")

    retention = commands.add_parser("retention", help="retention sweeps (§19)")
    retention_sub = retention.add_subparsers(dest="subcommand", metavar="<subcommand>", required=True)
    sweep = retention_sub.add_parser("sweep", help="expire chats, uploads, partitions")
    sweep.set_defaults(handler=_stub, command_path="retention sweep")

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

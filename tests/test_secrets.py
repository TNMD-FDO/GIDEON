"""Generated secrets: the registry, absent-only generation, and the shared reader."""

import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gideon.host import secrets as secrets_module
from gideon.host.render.command import _SMTP_PASSWORD_NAME
from gideon.host.render.proxy import PROXY_AUTH_NAME
from gideon.host.secrets import (
    ROTATABLE_NAMES,
    SECRET_REGISTRY,
    SECRETS_DIR,
    SUPPLIED_NAMES,
    SUPPLIED_REGISTRY,
    current_directory,
    ensure_generated,
    is_generated,
    read_secret,
    rotate_generated,
    secret_path,
    select_directory,
    write_secret,
)
from gideon.host.sysio import Command, PathLike, RealHost
from gideon.host.tls import KEY_PATH

PAIR_KEY_TEXT = "fixture private key bytes stay in the child\n"
PAIR_CERT_TEXT = "fixture certificate\n"
PAIR_PUBLIC_TEXT = "fixture public key\n"
PAIR_NAMES = ("opensearch_transport_key", "opensearch_transport_cert")


def seed_pair(files: dict[str, str], directory: Path = SECRETS_DIR) -> None:
    """Place a matching, visibly fictitious transport pair in a fake file map."""

    files[str(directory / PAIR_NAMES[0])] = PAIR_KEY_TEXT
    files[str(directory / PAIR_NAMES[1])] = PAIR_CERT_TEXT


def answer_pair_command(
    argv: Command, files: dict[str, str], public_keys: dict[str, str]
) -> subprocess.CompletedProcess[str] | None:
    """Answer only the pair's openssl reads/writes and same-directory moves."""

    command = tuple(argv)
    if command[:2] == ("openssl", "genpkey"):
        output = command[command.index("-out") + 1]
        files[output] = PAIR_KEY_TEXT
        public_keys[output] = f"fixture public key {len(public_keys) + 1}\n"
        return subprocess.CompletedProcess(list(command), 0, "", "")
    if command[:2] == ("openssl", "req"):
        key = command[command.index("-key") + 1]
        output = command[command.index("-out") + 1]
        if key not in files:
            return subprocess.CompletedProcess(list(command), 1, "", "fixture key missing\n")
        files[output] = PAIR_CERT_TEXT
        public_keys[output] = public_keys.get(key, PAIR_PUBLIC_TEXT)
        return subprocess.CompletedProcess(list(command), 0, "", "")
    if (command[:2] == ("openssl", "pkey") and "-pubout" in command) or (
        command[:2] == ("openssl", "x509") and "-pubkey" in command
    ):
        path = command[command.index("-in") + 1]
        if path not in files:
            return subprocess.CompletedProcess(list(command), 1, "", "fixture input missing\n")
        return subprocess.CompletedProcess(
            list(command), 0, public_keys.get(path, PAIR_PUBLIC_TEXT), ""
        )
    if command[:3] == ("mv", "-f", "-T"):
        source, target = command[3:]
        if source not in files:
            return subprocess.CompletedProcess(list(command), 1, "", "fixture source missing\n")
        files[target] = files.pop(source)
        if source in public_keys:
            public_keys[target] = public_keys.pop(source)
        return subprocess.CompletedProcess(list(command), 0, "", "")
    return None


class FakeHost:
    """Files map path → text; writes record their mode; a path in ``unwritable`` raises."""

    def __init__(self, files: Mapping[str, str] | None = None, *, euid: int = 0, dirs: set[str] | None = None, service_group: str | None = "gideon:x:4242:\n") -> None:
        self.files = dict(files or {})
        self.euid = euid
        self.dirs = set(dirs if dirs is not None else {str(SECRETS_DIR)})
        self.service_group = service_group
        self.modes: dict[str, int] = {}
        self.chowns: list[tuple[str, int, int]] = []
        self.unwritable: set[str] = set()
        self.chown_failure = False
        self.calls: list[tuple[str, ...]] = []
        self.events: list[tuple[object, ...]] = []
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.public_keys: dict[str, str] = {}
        self.command_failures: dict[str, subprocess.CompletedProcess[str]] = {}

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        del check, cwd, env
        command = tuple(argv)
        self.calls.append(command)
        self.events.append(("run", command))
        if command == ("getent", "group", "gideon"):
            return subprocess.CompletedProcess(
                list(command), 0 if self.service_group is not None else 2, self.service_group or "", ""
            )
        if command and command[0] in {"openssl", "mv"}:
            if command[0] == "openssl":
                assert input == "" and timeout is not None
            failure = self.command_failures.get(command[1])
            if failure is not None:
                if command[1] == "genpkey" and failure.returncode != 127:
                    self.files[command[command.index("-out") + 1]] = "partial child output"
                return failure
            answered = answer_pair_command(command, self.files, self.public_keys)
            if answered is not None:
                return answered
        raise NotImplementedError(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        self.reads.append(key)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        key = os.fspath(path)
        if key in self.unwritable:
            raise PermissionError(key)
        self.files[key] = text
        self.modes[key] = mode
        self.writes.append(key)

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.dirs

    def listdir(self, path: PathLike) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        key = os.fspath(path)
        if key not in self.files and not missing_ok:
            raise FileNotFoundError(key)
        self.files.pop(key, None)
        self.public_keys.pop(key, None)

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        key = os.fspath(path)
        self.modes[key] = mode
        self.events.append(("chmod", key, mode))

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        if self.chown_failure:
            raise PermissionError(os.fspath(path))
        self.chowns.append((os.fspath(path), uid, gid))
        self.events.append(("chown", os.fspath(path), uid, gid))

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        raise NotImplementedError

    def geteuid(self) -> int:
        return self.euid


PASSWORD_KIND = [secret.name for secret in SECRET_REGISTRY if secret.kind == "password"]
MINTED_KIND = [secret.name for secret in SECRET_REGISTRY if secret.kind == "minted"]


class Registry(unittest.TestCase):
    def test_the_release_inventory(self) -> None:
        self.assertEqual(
            PASSWORD_KIND,
            [
                "postgres_superuser_password",
                "postgres_openwebui_password",
                "postgres_gideon_password",
                "postgres_gideon_audit_password",
                "webui_secret_key",
                "gideon_admin_password",
                "gideon_eval_password",
                "grafana_admin_password",
                "postgres_gideon_ro_metrics_password",
                "postgres_gideon_eval_password",
                "engine_api_key",
                "gideon_api_key",
                "searxng_secret_key",
                "qdrant_api_key",
                "opensearch_password",
            ],
        )
        self.assertEqual(MINTED_KIND, ["gideon_admin_api_key", "gideon_eval_api_key"])
        self.assertEqual(
            [secret.name for secret in SECRET_REGISTRY if secret.kind == "key"],
            [PAIR_NAMES[0]],
        )
        self.assertEqual(
            [secret.name for secret in SECRET_REGISTRY if secret.kind == "certificate"],
            [PAIR_NAMES[1]],
        )
        certificate = next(secret for secret in SECRET_REGISTRY if secret.name == PAIR_NAMES[1])
        self.assertEqual(certificate.issued_from, PAIR_NAMES[0])
        self.assertEqual(
            [secret.name for secret in SECRET_REGISTRY if secret.print_once],
            ["gideon_admin_password", "grafana_admin_password"],
        )
        eval_secret = next(secret for secret in SECRET_REGISTRY if secret.name == "postgres_gideon_eval_password")
        self.assertEqual(eval_secret.kind, "password")
        self.assertFalse(eval_secret.print_once)
        self.assertEqual(eval_secret.rotation, "role")
        self.assertTrue(all(secret.consumer for secret in SECRET_REGISTRY))

        engine = next(secret for secret in SECRET_REGISTRY if secret.name == "engine_api_key")
        self.assertEqual(engine.kind, "password")
        self.assertFalse(engine.print_once)
        self.assertEqual(engine.consumer, "the engine's API key (gideon-generator)")
        api = next(secret for secret in SECRET_REGISTRY if secret.name == "gideon_api_key")
        self.assertEqual(api.kind, "password")
        self.assertFalse(api.print_once)
        self.assertEqual(api.consumer, "the gideon-api connection key carried by Open WebUI")
        audit = next(
            secret
            for secret in SECRET_REGISTRY
            if secret.name == "postgres_gideon_audit_password"
        )
        self.assertEqual(
            audit.consumer,
            "audit writer database role and gideon-api trip writer",
        )

    def test_every_entry_has_the_release_rotation_class(self) -> None:
        classes = {secret.name: secret.rotation for secret in SECRET_REGISTRY}
        self.assertEqual(
            classes,
            {
                "postgres_superuser_password": "role",
                "postgres_openwebui_password": "role",
                "postgres_gideon_password": "role",
                "postgres_gideon_audit_password": "role",
                "webui_secret_key": "rewrite",
                "gideon_admin_password": "account",
                "gideon_eval_password": "account",
                "grafana_admin_password": "seeded",
                "postgres_gideon_ro_metrics_password": "role",
                "postgres_gideon_eval_password": "role",
                "engine_api_key": "rewrite",
                "gideon_api_key": "rewrite",
                "searxng_secret_key": "rewrite",
                "qdrant_api_key": "rewrite",
                "opensearch_password": "rewrite",
                "opensearch_transport_key": "rewrite",
                "opensearch_transport_cert": "rewrite",
                "gideon_admin_api_key": "remint",
                "gideon_eval_api_key": "remint",
            },
        )
        self.assertEqual(
            ROTATABLE_NAMES,
            {
                "engine_api_key",
                "gideon_api_key",
                "webui_secret_key",
                "searxng_secret_key",
                "qdrant_api_key",
                "opensearch_password",
                "opensearch_transport_key",
                "opensearch_transport_cert",
                "gideon_admin_api_key",
                "gideon_eval_api_key",
            },
        )

    def test_supplied_registry_names_match_the_render_constants(self) -> None:
        self.assertEqual(SUPPLIED_NAMES, {secret.name for secret in SUPPLIED_REGISTRY})
        self.assertEqual(
            SUPPLIED_NAMES,
            {Path(KEY_PATH).name, _SMTP_PASSWORD_NAME, PROXY_AUTH_NAME, "ldap_bind_password"},
        )
        tls_replacement = next(
            secret.replaced_by
            for secret in SUPPLIED_REGISTRY
            if secret.name == "tls_key"
        )
        self.assertIn("docs/runbooks/office-services-setup.md §4", tls_replacement)
        self.assertTrue(tls_replacement.endswith("sudo python3 -m gideon tls reload."))

    def test_supplied_secrets_are_not_generated(self) -> None:
        self.assertTrue(is_generated("gideon_admin_password"))
        self.assertFalse(is_generated("ldap_bind_password"))
        self.assertFalse(is_generated("tls_key"))


class Reader(unittest.TestCase):
    def test_selected_directory_moves_readers_and_fix_text(self) -> None:
        original = current_directory()
        selected = Path("/tmp/fictitious-gideon-secrets")
        try:
            select_directory(selected)
            self.assertEqual(current_directory(), selected)
            self.assertEqual(secret_path("x"), selected / "x")
            host = FakeHost({f"{selected}/x": "value\n"}, dirs={str(selected)})
            result = read_secret(host, "x")
            self.assertTrue(result.ok)
            self.assertEqual(result.value, "value")

            host.unwritable.add(f"{selected}/postgres_superuser_password")
            refused = ensure_generated(host)
            self.assertFalse(refused.ok)
            self.assertIn(str(selected), refused.fix)
        finally:
            select_directory(original)

    def test_reads_and_strips_the_trailing_newline(self) -> None:
        host = FakeHost({f"{SECRETS_DIR}/x": "value\n"})
        result = read_secret(host, "x")
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "value")
        self.assertFalse(result.missing)

    def test_missing_is_flagged_with_the_path(self) -> None:
        result = read_secret(FakeHost(), "x")
        self.assertFalse(result.ok)
        self.assertTrue(result.missing)
        self.assertIn(f"{SECRETS_DIR}/x", result.problem or "")
        self.assertIn("then retry", result.fix)

    def test_names_never_escape_the_directory(self) -> None:
        for name in ("", "../etc/passwd", "a/b"):
            with self.subTest(name=name):
                result = read_secret(FakeHost({"/etc/passwd": "x"}), name)
                self.assertFalse(result.ok)
                self.assertFalse(result.missing)


class Ensure(unittest.TestCase):
    def test_root_is_required(self) -> None:
        result = ensure_generated(FakeHost(euid=1000))
        self.assertFalse(result.ok)
        self.assertIn("sudo", result.fix)

    def test_missing_directory_names_the_provision_step(self) -> None:
        result = ensure_generated(FakeHost(dirs=set()))
        self.assertFalse(result.ok)
        self.assertIn("secrets-dirs", result.fix)

    def test_missing_service_group_names_the_provision_step(self) -> None:
        result = ensure_generated(FakeHost(service_group=None))
        self.assertFalse(result.ok)
        self.assertIn("service-user", result.fix)

    def test_write_secret_sets_root_group_and_mode(self) -> None:
        host = FakeHost()
        problem = write_secret(host, "supplied", "secret")
        self.assertIsNone(problem)
        path = f"{SECRETS_DIR}/supplied"
        self.assertEqual(host.modes[path], 0o440)
        self.assertEqual(host.chowns, [(path, 0, 4242)])

    def test_creates_only_absent_password_kind_secrets_at_0440_and_prints_once(self) -> None:
        host = FakeHost({f"{SECRETS_DIR}/webui_secret_key": "keep-me\n"})
        result = ensure_generated(host)
        self.assertTrue(result.ok)
        self.assertEqual(
            result.created,
            tuple(name for name in PASSWORD_KIND if name != "webui_secret_key") + PAIR_NAMES,
        )
        self.assertEqual(host.files[f"{SECRETS_DIR}/webui_secret_key"], "keep-me\n")
        for name in PASSWORD_KIND:
            if name == "webui_secret_key":
                continue
            path = f"{SECRETS_DIR}/{name}"
            self.assertEqual(host.modes[path], 0o440)
            self.assertTrue(host.files[path].endswith("\n"))
            self.assertGreaterEqual(len(host.files[path].strip()), 40)
            self.assertIn((path, 0, 4242), host.chowns)
        self.assertEqual(set(result.printed), {"gideon_admin_password", "grafana_admin_password"})
        self.assertEqual(result.printed["gideon_admin_password"] + "\n", host.files[f"{SECRETS_DIR}/gideon_admin_password"])
        self.assertEqual(result.printed["grafana_admin_password"] + "\n", host.files[f"{SECRETS_DIR}/grafana_admin_password"])
        for name in MINTED_KIND:
            self.assertNotIn(f"{SECRETS_DIR}/{name}", host.files)

    def test_skipped_entries_are_never_created(self) -> None:
        host = FakeHost()
        skip = ("engine_api_key", "grafana_admin_password", "opensearch_password", *PAIR_NAMES)
        result = ensure_generated(host, skip=skip)
        self.assertTrue(result.ok)
        self.assertEqual(result.created, tuple(name for name in PASSWORD_KIND if name not in skip))
        for name in skip:
            self.assertNotIn(f"{SECRETS_DIR}/{name}", host.files)
        self.assertFalse(any(call[0] == "openssl" for call in host.calls))
        self.assertEqual(set(result.printed), {"gideon_admin_password"})

    def test_second_run_creates_nothing_and_prints_nothing(self) -> None:
        host = FakeHost()
        ensure_generated(host)
        again = ensure_generated(host)
        self.assertTrue(again.ok)
        self.assertEqual(again.created, ())
        self.assertEqual(dict(again.printed), {})

    def test_write_failure_refuses_naming_the_directory(self) -> None:
        host = FakeHost()
        host.unwritable.add(f"{SECRETS_DIR}/postgres_superuser_password")
        result = ensure_generated(host)
        self.assertFalse(result.ok)
        self.assertIn("/etc/gideon/secrets", result.fix)


class TransportPair(unittest.TestCase):
    """A child owns each private file until the owned temporary is moved."""

    def test_key_and_certificate_land_by_owned_temporary_moves(self) -> None:
        host = FakeHost()
        result = ensure_generated(host)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(result.created[-2:], PAIR_NAMES)
        for name in PAIR_NAMES:
            with self.subTest(name=name):
                path = str(SECRETS_DIR / name)
                temporary = str(SECRETS_DIR / f".{name}.new")
                self.assertIn(path, host.files)
                self.assertNotIn(temporary, host.files)
                self.assertNotIn(path, host.reads + host.writes)
                self.assertIn(("chmod", temporary, 0o440), host.events)
                self.assertIn(("chown", temporary, 0, 4242), host.events)
                move = ("run", ("mv", "-f", "-T", temporary, path))
                self.assertLess(host.events.index(("chmod", temporary, 0o440)), host.events.index(move))
                self.assertLess(host.events.index(("chown", temporary, 0, 4242)), host.events.index(move))
                self.assertNotIn(PAIR_KEY_TEXT.strip(), " ".join(part for call in host.calls for part in call))
        self.assertEqual(
            [call[1] for call in host.calls if call[0] == "openssl" and call[1] in {"genpkey", "req"}],
            ["genpkey", "req"],
        )

    def test_matching_standing_pair_is_not_reissued(self) -> None:
        files: dict[str, str] = {}
        seed_pair(files)
        host = FakeHost(files)
        result = ensure_generated(host)
        self.assertTrue(result.ok, result.problem)
        self.assertFalse(set(result.created) & set(PAIR_NAMES))
        self.assertFalse(any(call[:2] == ("openssl", "req") for call in host.calls))
        self.assertNotIn(str(SECRETS_DIR / PAIR_NAMES[1]), host.writes)
        self.assertFalse(any(call[:3] == ("mv", "-f", "-T") for call in host.calls))
        self.assertIn("pkey", [call[1] for call in host.calls if call[0] == "openssl"])
        self.assertIn("x509", [call[1] for call in host.calls if call[0] == "openssl"])

    def test_absent_certificate_is_issued_from_standing_key(self) -> None:
        host = FakeHost({str(SECRETS_DIR / PAIR_NAMES[0]): PAIR_KEY_TEXT})
        result = ensure_generated(host)
        self.assertTrue(result.ok, result.problem)
        self.assertIn(PAIR_NAMES[1], result.created)
        self.assertNotIn(PAIR_NAMES[0], result.created)
        self.assertFalse(any(call[:2] == ("openssl", "genpkey") for call in host.calls))
        self.assertTrue(any(call[:2] == ("openssl", "req") for call in host.calls))

    def test_new_key_reissues_standing_certificate(self) -> None:
        host = FakeHost({str(SECRETS_DIR / PAIR_NAMES[1]): PAIR_CERT_TEXT})
        result = ensure_generated(host)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(result.created[-2:], PAIR_NAMES)
        self.assertEqual(host.public_keys[str(SECRETS_DIR / PAIR_NAMES[0])],
                         host.public_keys[str(SECRETS_DIR / PAIR_NAMES[1])])

    def test_mismatched_certificate_is_reissued_without_touching_key(self) -> None:
        files: dict[str, str] = {}
        seed_pair(files)
        host = FakeHost(files)
        host.public_keys[str(SECRETS_DIR / PAIR_NAMES[1])] = "different public key\n"
        result = ensure_generated(host)
        self.assertTrue(result.ok, result.problem)
        self.assertIn(PAIR_NAMES[1], result.created)
        self.assertNotIn(PAIR_NAMES[0], result.created)
        self.assertFalse(any(call[:2] == ("openssl", "genpkey") for call in host.calls))
        self.assertEqual(host.public_keys[str(SECRETS_DIR / PAIR_NAMES[1])], PAIR_PUBLIC_TEXT)

    def test_unreadable_standing_key_refuses_without_remaking_it(self) -> None:
        files: dict[str, str] = {}
        seed_pair(files)
        host = FakeHost(files)
        host.command_failures["pkey"] = subprocess.CompletedProcess(
            ["openssl", "pkey"], 1, "", "fixture key unreadable\n"
        )
        result = ensure_generated(host)
        self.assertFalse(result.ok)
        self.assertIn("fixture key unreadable", result.problem or "")
        self.assertFalse(any(call[:2] == ("openssl", "genpkey") for call in host.calls))
        self.assertFalse(any(call[:2] == ("openssl", "req") for call in host.calls))
        self.assertEqual(host.files[str(SECRETS_DIR / PAIR_NAMES[0])], PAIR_KEY_TEXT)

    def test_unreadable_standing_certificate_is_reissued(self) -> None:
        files: dict[str, str] = {}
        seed_pair(files)
        host = FakeHost(files)
        host.command_failures["x509"] = subprocess.CompletedProcess(
            ["openssl", "x509"], 1, "", "fixture certificate unreadable\n"
        )
        result = ensure_generated(host)
        self.assertTrue(result.ok, result.problem)
        self.assertIn(PAIR_NAMES[1], result.created)
        self.assertNotIn(PAIR_NAMES[0], result.created)
        self.assertFalse(any(call[:2] == ("openssl", "genpkey") for call in host.calls))
        self.assertTrue(any(call[:2] == ("openssl", "req") for call in host.calls))

    def test_failed_child_leaves_no_registry_file_and_names_the_pair_fix(self) -> None:
        host = FakeHost()
        temporary = str(SECRETS_DIR / f".{PAIR_NAMES[0]}.new")
        host.files[temporary] = "old partial temporary"
        host.command_failures["genpkey"] = subprocess.CompletedProcess(
            ["openssl", "genpkey"], 1, "", "fixture openssl diagnostic\n"
        )
        result = ensure_generated(host)
        self.assertFalse(result.ok)
        self.assertIn("fixture openssl diagnostic", result.problem or "")
        for name in PAIR_NAMES:
            self.assertIn(str(SECRETS_DIR / name), result.fix)
            self.assertNotIn(str(SECRETS_DIR / name), host.files)
        self.assertIn("sudo python3 -m gideon apply", result.fix)
        self.assertNotIn(temporary, host.files)

    def test_missing_openssl_names_its_package(self) -> None:
        host = FakeHost()
        host.command_failures["genpkey"] = subprocess.CompletedProcess(
            ["openssl", "genpkey"], 127, "", "openssl: command not found\n"
        )
        result = ensure_generated(host)
        self.assertFalse(result.ok)
        self.assertIn("openssl: command not found", result.problem or "")
        self.assertIn("apt-get install -y openssl", result.fix)

    def test_key_rotation_interrupted_after_move_is_repaired_by_apply(self) -> None:
        files: dict[str, str] = {}
        seed_pair(files)
        host = FakeHost(files)
        host.command_failures["req"] = subprocess.CompletedProcess(
            ["openssl", "req"], 1, "", "fixture issue failed\n"
        )
        rotated = rotate_generated(host, PAIR_NAMES[0])
        self.assertFalse(rotated.ok)
        self.assertTrue(rotated.written)
        self.assertIn("fixture issue failed", rotated.problem or "")
        self.assertIn("sudo python3 -m gideon apply", rotated.fix)
        self.assertIn(f"secrets rotate {PAIR_NAMES[0]} again", rotated.fix)
        del host.command_failures["req"]
        host.calls.clear()
        repaired = ensure_generated(host)
        self.assertTrue(repaired.ok, repaired.problem)
        self.assertIn(PAIR_NAMES[1], repaired.created)
        self.assertNotIn(PAIR_NAMES[0], repaired.created)
        self.assertFalse(any(call[:2] == ("openssl", "genpkey") for call in host.calls))
        self.assertEqual(host.public_keys[str(SECRETS_DIR / PAIR_NAMES[0])],
                         host.public_keys[str(SECRETS_DIR / PAIR_NAMES[1])])

    def test_rotation_dispatches_by_kind(self) -> None:
        files: dict[str, str] = {}
        seed_pair(files)
        host = FakeHost(files)
        password = rotate_generated(host, "opensearch_password")
        self.assertTrue(password.ok)
        self.assertTrue(password.written)
        self.assertFalse(any(call[0] == "openssl" for call in host.calls))

        host.calls.clear()
        key = rotate_generated(host, PAIR_NAMES[0])
        self.assertTrue(key.ok, key.problem)
        self.assertTrue(key.written)
        self.assertEqual([call[1] for call in host.calls if call[0] == "openssl"],
                         ["genpkey", "req"])

        host.calls.clear()
        certificate = rotate_generated(host, PAIR_NAMES[1])
        self.assertTrue(certificate.ok, certificate.problem)
        self.assertTrue(certificate.written)
        self.assertEqual([call[1] for call in host.calls if call[0] == "openssl"], ["req"])

    @unittest.skipUnless(shutil.which("openssl"), "openssl is unavailable")
    def test_real_openssl_pair_verifies_and_second_ensure_is_absent_only(self) -> None:
        class LocalHost(RealHost):
            def geteuid(self) -> int:
                return 0

            def chown(self, path: PathLike, uid: int, gid: int) -> None:
                del path, uid, gid

            def run(
                self,
                argv: Command,
                *,
                check: bool = False,
                input: str | None = None,
                cwd: PathLike | None = None,
                env: Mapping[str, str] | None = None,
                timeout: float | None = None,
                passthrough: bool = False,
            ) -> subprocess.CompletedProcess[str]:
                if tuple(argv) == ("getent", "group", "gideon"):
                    return subprocess.CompletedProcess(list(argv), 0, "gideon:x:4242:\n", "")
                return super().run(
                    argv, check=check, input=input, cwd=cwd, env=env,
                    timeout=timeout, passthrough=passthrough,
                )

        original = current_directory()
        try:
            with tempfile.TemporaryDirectory() as directory:
                select_directory(Path(directory))
                host = LocalHost()
                first = ensure_generated(host)
                self.assertTrue(first.ok, first.problem)
                key = secret_path(PAIR_NAMES[0])
                cert = secret_path(PAIR_NAMES[1])
                verify = host.run(["openssl", "verify", "-CAfile", str(cert), str(cert)], input="", timeout=30)
                self.assertEqual(verify.returncode, 0, verify.stderr)
                key_public = host.run(["openssl", "pkey", "-in", str(key), "-pubout"], input="", timeout=30)
                cert_public = host.run(["openssl", "x509", "-in", str(cert), "-noout", "-pubkey"], input="", timeout=30)
                self.assertEqual((key_public.returncode, cert_public.returncode), (0, 0))
                self.assertEqual(key_public.stdout.strip(), cert_public.stdout.strip())
                end = host.run(["openssl", "x509", "-in", str(cert), "-noout", "-enddate"], input="", timeout=30)
                self.assertEqual(end.returncode, 0, end.stderr)
                expires = datetime.strptime(end.stdout.strip().removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
                expected = datetime.now(UTC) + timedelta(days=secrets_module.OPENSEARCH_CERT_DAYS)
                self.assertLess(abs(expires - expected), timedelta(days=1))
                second = ensure_generated(host)
                self.assertTrue(second.ok, second.problem)
                self.assertEqual(second.created, ())
        finally:
            select_directory(original)


class Rotation(unittest.TestCase):
    def test_rewrites_a_fresh_value_at_the_same_path(self) -> None:
        path = f"{SECRETS_DIR}/engine_api_key"
        host = FakeHost({path: "old-value\n"})

        first = rotate_generated(host, "engine_api_key")
        self.assertTrue(first.ok)
        self.assertTrue(first.written)
        self.assertNotEqual(host.files[path], "old-value\n")
        self.assertGreaterEqual(len(host.files[path].strip()), 40)
        self.assertEqual(host.modes[path], 0o440)
        self.assertEqual(host.chowns, [(path, 0, 4242)])
        first_value = host.files[path]
        self.assertNotIn(first_value, str(first))

        second = rotate_generated(host, "engine_api_key")
        self.assertTrue(second.ok)
        self.assertTrue(second.written)
        self.assertNotEqual(host.files[path], first_value)

    def test_refuses_a_non_rewrite_entry(self) -> None:
        host = FakeHost({f"{SECRETS_DIR}/gideon_admin_api_key": "old\n"})
        result = rotate_generated(host, "gideon_admin_api_key")
        self.assertFalse(result.ok)
        self.assertFalse(result.written)
        self.assertIn("remint", result.problem or "")
        self.assertEqual(host.files[f"{SECRETS_DIR}/gideon_admin_api_key"], "old\n")

    def test_refuses_when_the_service_group_is_missing(self) -> None:
        path = f"{SECRETS_DIR}/engine_api_key"
        host = FakeHost({path: "old\n"}, service_group=None)
        result = rotate_generated(host, "engine_api_key")
        self.assertFalse(result.ok)
        self.assertFalse(result.written)
        self.assertIn("service group", result.problem or "")
        self.assertEqual(host.files[path], "old\n")

    def test_reports_a_value_written_but_unowned(self) -> None:
        path = f"{SECRETS_DIR}/engine_api_key"
        host = FakeHost({path: "old\n"})
        host.chown_failure = True
        result = rotate_generated(host, "engine_api_key")
        self.assertFalse(result.ok)
        self.assertTrue(result.written)
        self.assertIn("unowned", result.problem or "")
        self.assertNotEqual(host.files[path], "old\n")


class Fingerprint(unittest.TestCase):
    """The keyed secrets fingerprint a backup manifest carries."""

    def test_keyed_by_the_session_key_digest_and_order_independent(self) -> None:
        digests = {"webui_secret_key": "1" * 64, "ldap_bind_password": "2" * 64, "tls_key": "3" * 64}
        value = secrets_module.secrets_fingerprint(digests)
        self.assertIsNotNone(value)
        assert value is not None
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        reordered = dict(reversed(list(digests.items())))
        self.assertEqual(secrets_module.secrets_fingerprint(reordered), value)
        upper = {name: digest.upper() for name, digest in digests.items()}
        self.assertEqual(secrets_module.secrets_fingerprint(upper), value)
        # A different session key or any other secret changes the fingerprint.
        self.assertNotEqual(secrets_module.secrets_fingerprint({**digests, "webui_secret_key": "9" * 64}), value)
        self.assertNotEqual(secrets_module.secrets_fingerprint({**digests, "tls_key": "9" * 64}), value)
        # Without the key there is nothing safe to fingerprint.
        self.assertIsNone(secrets_module.secrets_fingerprint({"ldap_bind_password": "2" * 64}))

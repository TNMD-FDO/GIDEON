"""Generated secrets: the registry, absent-only generation, and the shared reader (spec §1.7)."""

import os
import subprocess
import unittest
from collections.abc import Mapping
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
    ensure_generated,
    is_generated,
    read_secret,
    rotate_generated,
    write_secret,
)
from gideon.host.sysio import Command, PathLike
from gideon.host.tls import KEY_PATH


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

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout
        command = tuple(argv)
        if command == ("getent", "group", "gideon"):
            return subprocess.CompletedProcess(
                list(command), 0 if self.service_group is not None else 2, self.service_group or "", ""
            )
        raise NotImplementedError

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        key = os.fspath(path)
        if key in self.unwritable:
            raise PermissionError(key)
        self.files[key] = text
        self.modes[key] = mode

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.dirs

    def listdir(self, path: PathLike) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        if self.chown_failure:
            raise PermissionError(os.fspath(path))
        self.chowns.append((os.fspath(path), uid, gid))

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
            ],
        )
        self.assertEqual(MINTED_KIND, ["gideon_admin_api_key", "gideon_eval_api_key"])
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
        self.assertEqual(api.consumer, "the gideon-api connection key (gideon-api)")
        audit = next(
            secret
            for secret in SECRET_REGISTRY
            if secret.name == "postgres_gideon_audit_password"
        )
        self.assertEqual(
            audit.consumer,
            "audit writer database role, frontend guardrail trip writer, and gideon-api trip writer",
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

    def test_supplied_secrets_are_not_generated(self) -> None:
        self.assertTrue(is_generated("gideon_admin_password"))
        self.assertFalse(is_generated("ldap_bind_password"))
        self.assertFalse(is_generated("tls_key"))


class Reader(unittest.TestCase):
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
        self.assertEqual(result.created, tuple(name for name in PASSWORD_KIND if name != "webui_secret_key"))
        self.assertEqual(host.files[f"{SECRETS_DIR}/webui_secret_key"], "keep-me\n")
        for name in result.created:
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
    """The keyed secrets fingerprint a backup manifest carries (§19.1, ticket 06)."""

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

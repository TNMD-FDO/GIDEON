"""The frontend contract: the apply manifest is desired state (spec §3.5, ticket 23 item 11).

Runs on the self-hosted runner after ``mirror-images`` (never on a hosted
runner: it needs Docker and the release registry's mirrored digests) as
``python3 -m unittest tests/contract/owui_apply_manifest.py`` — the file has
no ``test_`` prefix so the hosted pytest run never collects it.  It brings up
a throwaway Compose project (Postgres, Open WebUI, and a standard-library model
stub on loopback) in production order — Postgres, ``stores.converge``, then the
frontend — adds a Function by hand, pushes the rendered example manifest with
``owui.bootstrap``, and proves the hand Function is gone, the rendered branch-gate Function, both model
records with General's empty attachment list, groups, and keys match, and a
further push changes nothing. The rendered Function is hand-edited, toggled,
and removed through the frontend routes, General's attachment list is
hand-added, and the next desired-state push restores all of it.

It also proves the signed-in environment facts: the rendered ``default_models``
value and the disabled evaluation arena are visible through session-only config
routes, survive their panel edits only until the frontend restarts, and return
to the rendered values after restart (see
``docs/research/owui-default-model-and-arena.md`` §§1–2, 4).

Environment: ``GIDEON_CONTRACT_REGISTRY`` (default ``127.0.0.1:5000``),
``GIDEON_CONTRACT_PORT`` (default ``18081``).  The caller needs Docker access.
"""

import json
import os
import secrets as token_secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import gideon.host.secrets as secret_files
from gideon.host import owui, stores
from gideon.host.images import load_image_lock, parse_registry, reference
from gideon.host.models import HardwareProfile, load_models_lock, select_profile
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.render.owui import (
    ALLOWED_ENDPOINTS,
    BASE_MODEL_CAPABILITIES,
    BREAK_GLASS,
    EVAL_IDENTITY,
    GENERAL_CAPABILITIES,
    GENERAL_PRESET_ID,
    SERVICE_GROUP,
)
from gideon.host.site import load_site
from gideon.host.sysio import RealHost

MANIFEST = ROOT / "tests/fixtures/render/example/open-webui/manifest.yaml"
COMPOSE_TEMPLATE = ROOT / "tests/contract/compose.yaml"
STUB_TEMPLATE = ROOT / "tests/contract/sentinel/stub.py"
EXAMPLE_SITE = ROOT / "config/site.example.yaml"
REGISTRY = os.environ.get("GIDEON_CONTRACT_REGISTRY", "127.0.0.1:5000")
PORT = int(os.environ.get("GIDEON_CONTRACT_PORT", "18081"))
BASE_URL = f"http://127.0.0.1:{PORT}"
READY_ATTEMPTS = 90
HEALTHY_ATTEMPTS = 60

HAND_ADDED_FUNCTION = {
    "id": "hand_added",
    "name": "Hand Added",
    "content": "class Filter:\n    def __init__(self):\n        pass\n\n    def inlet(self, body, __user__=None):\n        return body\n",
    "meta": {"description": "contract probe"},
}


def stub_model_id() -> str:
    """Return the selected profile's generator served name for the stub."""

    site_result = load_site(EXAMPLE_SITE)
    models_result = load_models_lock(ROOT / "models.lock")
    assert site_result.config is not None and not site_result.errors
    assert models_result.lock is not None and not models_result.errors
    profile = select_profile(models_result.lock, site_result.config.hardware_profile)
    assert isinstance(profile, HardwareProfile)
    generator = profile.model("generator")
    assert generator is not None
    return generator.serve.served_name


def image_references() -> dict[str, str]:
    lock = load_image_lock(ROOT / "images.lock").lock
    target = parse_registry(REGISTRY)
    assert lock is not None and target is not None
    return {pin.name: reference(target, pin) for pin in lock.images}


class ContractStack:
    """The throwaway project: a temp directory holding the Compose file, secrets, and env."""

    def __init__(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="gideon-ci-contract-"))
        self.secrets = self.directory / "secrets"
        self.secrets.mkdir(mode=0o700)
        # Every role the stores stage converges needs its password file, so the
        # set is derived from the release's role table — a new role (v0.0.22's
        # metrics reader) must never leave this harness behind.
        self.passwords = {
            "postgres_superuser_password": token_secrets.token_urlsafe(24),
            **{spec.secret_name: token_secrets.token_urlsafe(24) for spec in stores.ROLE_SPECS},
            "webui_secret_key": token_secrets.token_urlsafe(24),
            "gideon_admin_password": token_secrets.token_urlsafe(24),
            "gideon_eval_password": token_secrets.token_urlsafe(24),
        }
        for name, value in self.passwords.items():
            path = self.secrets / name
            path.write_text(value + "\n")
            path.chmod(0o440)
        shutil.copy(COMPOSE_TEMPLATE, self.directory / "compose.yaml")
        shutil.copy2(STUB_TEMPLATE, self.directory / "stub.py")
        database_url = (
            f"postgresql://openwebui:{quote(self.passwords['postgres_openwebui_password'], safe='')}"
            "@postgres:5432/openwebui"
        )
        (self.directory / "open-webui.env").write_text(
            f"DATABASE_URL={database_url}\nWEBUI_ADMIN_PASSWORD={self.passwords['gideon_admin_password']}\n"
        )
        (self.directory / "open-webui.env").chmod(0o600)
        images = image_references()
        # Compose reads the project directory's .env for interpolation, so the
        # product's own invocations (stack.exec_argv) resolve the same values.
        (self.directory / ".env").write_text(
            f"POSTGRES_IMAGE={images['postgres']}\n"
            f"OPEN_WEBUI_IMAGE={images['open-webui']}\n"
            f"CONTRACT_PORT={PORT}\n"
            f"ALLOWED_ENDPOINTS={','.join(ALLOWED_ENDPOINTS)}\n"
            f"DEFAULT_MODELS={GENERAL_PRESET_ID}\n"
            "STUB_MODE=ok\n"
            f"STUB_MODEL_ID={stub_model_id()}\n"
        )

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = ["docker", "compose", "--project-directory", str(self.directory), "-f", str(self.directory / "compose.yaml"), *args]
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    def wait_healthy(self, service: str) -> None:
        for _ in range(HEALTHY_ATTEMPTS):
            result = self.compose("ps", "--all", "--format", "json")
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("Service") == service and row.get("Health") == "healthy":
                    return
            time.sleep(5)
        raise AssertionError(f"{service} did not become healthy: {self.compose('logs', service).stdout[-2000:]}")

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans")
        shutil.rmtree(self.directory, ignore_errors=True)


class ContractHost(RealHost):
    """The real host, except that ownership is not this harness's concern.

    The product writes every secret as ``root:gideon`` (v0.0.22); the job runs
    as the runner user, which may not give a file to root, and the stack's
    secrets live in a temporary directory only this process reads.
    """

    def chown(self, path: object, uid: int, gid: int) -> None:
        del path, uid, gid


def client_factory(*, api_key: str | None = None, token: str | None = None) -> owui.Client:
    return owui.Client(BASE_URL, api_key=api_key, token=token)


class ApplyManifestContract(unittest.TestCase):
    stack: ContractStack
    original_secrets_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.stack = ContractStack()
        # The product resolves secret paths at call time through the secrets
        # module; pointing that one constant at the stack's directory is the
        # harness's seam (never the real /etc/gideon/secrets).
        cls.original_secrets_dir = secret_files.SECRETS_DIR
        setattr(secret_files, "SECRETS_DIR", cls.stack.secrets)  # noqa: B010
        try:
            up = cls.stack.compose("up", "-d", "postgres")
            assert up.returncode == 0, up.stderr
            cls.stack.wait_healthy("postgres")
            converged = stores.converge(ContractHost(), cls.stack.directory, root=ROOT)
            assert converged.ok, converged.problem
            # Derived from the release's tables, so a new role or migration
            # moves the product and the fixture together, never this assertion.
            assert converged.created_roles == tuple(spec.name for spec in stores.ROLE_SPECS), converged
            assert converged.applied_migrations == tuple(
                sorted(path.stem for path in (ROOT / "migrations").glob("*.sql"))
            ), converged
            up = cls.stack.compose("up", "-d", "open-webui")
            assert up.returncode == 0, up.stderr
            ready = owui.wait_ready(client_factory(), attempts=READY_ATTEMPTS, sleep=time.sleep)
            assert ready.ok, f"{ready.problem} {cls.stack.compose('logs', 'open-webui').stdout[-2000:]}"
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        setattr(secret_files, "SECRETS_DIR", cls.original_secrets_dir)  # noqa: B010
        cls.stack.down()

    def manifest(self) -> dict[str, object]:
        document = owui.load_manifest(ContractHost(), MANIFEST)
        return dict(document)

    def admin_session(self) -> owui.Client:
        token = client_factory().signin(BREAK_GLASS.email, self.stack.passwords["gideon_admin_password"])
        return client_factory(token=token)

    def _assert_manifest_model_state(self, admin: owui.Client, admin_id: str) -> None:
        """Hold the split admin listings to the rendered base and General records."""

        base = admin.request("GET", "/api/v1/models/base")
        self.assertEqual(base.status, 200, base.body)
        assert isinstance(base.body, list)
        self.assertEqual(len(base.body), 1)
        record = base.body[0]
        assert isinstance(record, dict)
        self.assertEqual(record["id"], ENGINE_SERVICE_NAME)
        self.assertEqual(record["name"], ENGINE_SERVICE_NAME)
        self.assertIsNone(record["base_model_id"])
        self.assertTrue(record["is_active"])
        self.assertEqual(record["params"], {})
        meta = record["meta"]
        assert isinstance(meta, dict)
        self.assertEqual(meta["capabilities"], dict(BASE_MODEL_CAPABILITIES))
        # Readable by every verified user but hidden from the selector: the base
        # hop of General's access check (docs/research/owui-preset-system-prompt.md §6).
        self.assertIs(meta["hidden"], True)
        self.assertTrue(
            all(value is None for key, value in meta.items() if key not in {"capabilities", "hidden"})
        )
        # These ModelMeta defaults are materialized as null by the pinned row shape.
        self.assertIsNone(meta.get("profile_image_url"))
        self.assertIsNone(meta.get("description"))
        self.assertIsNone(meta.get("knowledge"))
        base_grants = record["access_grants"]
        assert isinstance(base_grants, list)
        self.assertEqual(len(base_grants), 1)
        base_grant = base_grants[0]
        assert isinstance(base_grant, dict)
        self.assertEqual(
            (base_grant["principal_type"], base_grant["principal_id"], base_grant["permission"]),
            ("user", "*", "read"),
        )
        self.assertEqual((base_grant["resource_type"], base_grant["resource_id"]), ("model", ENGINE_SERVICE_NAME))
        self.assertEqual(record["user_id"], admin_id)

        presets = admin.request("GET", "/api/v1/models/list?page=1")
        self.assertEqual(presets.status, 200, presets.body)
        assert isinstance(presets.body, dict)
        # The admin preset listing returns General's full params and grants;
        # the base route is separate (docs/research/owui-preset-system-prompt.md
        # §2.1–2.3, docs/research/owui-model-record.md §1.3).
        items = presets.body["items"]
        assert isinstance(items, list)
        self.assertEqual(len(items), 1)
        general = items[0]
        assert isinstance(general, dict)
        rendered_models = self.manifest()["models"]
        assert isinstance(rendered_models, list)
        rendered_general = rendered_models[1]
        assert isinstance(rendered_general, dict)
        self.assertEqual(general["id"], GENERAL_PRESET_ID)
        self.assertEqual(general["name"], rendered_general["name"])
        self.assertEqual(general["base_model_id"], ENGINE_SERVICE_NAME)
        self.assertTrue(general["is_active"])
        self.assertEqual(general["params"], rendered_general["params"])
        general_meta = general["meta"]
        assert isinstance(general_meta, dict)
        rendered_meta = rendered_general["meta"]
        assert isinstance(rendered_meta, dict)
        self.assertEqual(general_meta["description"], rendered_meta["description"])
        self.assertEqual(general_meta["capabilities"], dict(GENERAL_CAPABILITIES))
        self.assertEqual(general_meta["suggestion_prompts"], [])
        self.assertEqual(general_meta["suggestion_prompts"], rendered_meta["suggestion_prompts"])
        self.assertTrue(
            all(
                value is None
                for key, value in general_meta.items()
                if key not in {"description", "capabilities", "suggestion_prompts", "filterIds"}
            )
        )
        # The rendered attachment list is empty since the cutover, and the push
        # overwrites a live one with it — the key is kept so that it does.
        self.assertEqual(rendered_meta["filterIds"], [])
        self.assertEqual(general_meta["filterIds"], rendered_meta["filterIds"])
        grants = general["access_grants"]
        assert isinstance(grants, list)
        self.assertEqual(len(grants), 1)
        grant = grants[0]
        assert isinstance(grant, dict)
        self.assertEqual(
            (grant["principal_type"], grant["principal_id"], grant["permission"]),
            ("user", "*", "read"),
        )
        self.assertEqual(grant["resource_type"], "model")
        self.assertEqual(grant["resource_id"], GENERAL_PRESET_ID)
        self.assertEqual(general["user_id"], admin_id)
        self.assertTrue(general.get("write_access"))

        for group in admin.groups():
            chat = group.permissions.get("chat")
            self.assertIsInstance(chat, dict)
            assert isinstance(chat, dict)
            self.assertFalse(chat["web_upload"])

    def _assert_live_model_listing(self, client: owui.Client, *, arena: bool = False) -> None:
        """Hold the chat-facing listing to the stub model and General."""

        listing = client.request("GET", "/api/models")
        self.assertEqual(listing.status, 200, listing.body)
        self.assertIsInstance(listing.body, dict)
        assert isinstance(listing.body, dict)
        rows = listing.body["data"]
        self.assertIsInstance(rows, list)
        assert isinstance(rows, list)
        ids = {
            row["id"]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        expected = {stub_model_id(), GENERAL_PRESET_ID}
        if arena:
            self.assertEqual(len(rows), len(expected) + 1)
            self.assertEqual(len(ids - expected), 1)
        else:
            self.assertEqual(ids, expected)
            self.assertEqual(len(rows), len(expected))

    def _assert_manifest_function_state(self, admin: owui.Client) -> None:
        """Hold the Function listing, source row, and empty valves to the manifest."""

        document = self.manifest()
        functions = document["functions"]
        assert isinstance(functions, list)
        expected_ids = [function["id"] for function in functions]

        listing = admin.request("GET", "/api/v1/functions/")
        self.assertEqual(listing.status, 200, listing.body)
        assert isinstance(listing.body, list)
        # A set: the pinned list route's order follows neither the ids nor the
        # push (measured at general-turn ticket 04), and the inlets' running
        # order is the frontend's own per-request sort by (priority, id).
        self.assertEqual(sorted(row["id"] for row in listing.body), sorted(expected_ids))

        for expected in functions:
            assert isinstance(expected, dict)
            identifier = expected["id"]
            assert isinstance(identifier, str)
            observed = admin.function_by_id(identifier)
            self.assertIsNotNone(observed)
            assert observed is not None
            self.assertEqual(observed["content"], expected["content"])
            self.assertEqual(observed["type"], expected["type"])
            self.assertEqual(observed["name"], expected["name"])
            self.assertEqual(observed["is_active"], expected["is_active"])
            self.assertEqual(observed["is_global"], expected["is_global"])
            expected_meta = expected["meta"]
            observed_meta = observed["meta"]
            assert isinstance(expected_meta, dict)
            assert isinstance(observed_meta, dict)
            self.assertEqual(observed_meta["description"], expected_meta["description"])
            self.assertEqual(admin.function_valves(identifier), {})

    def test_hand_added_function_and_model_are_removed_and_restorations_are_idempotent(self) -> None:
        session = self.admin_session()
        created = session.request("POST", "/api/v1/functions/create", HAND_ADDED_FUNCTION)
        self.assertEqual(created.status, 200, created.body)
        stray = session.request("POST", "/api/v1/groups/create", {"name": "stray", "description": "", "permissions": {}})
        self.assertEqual(stray.status, 200, stray.body)

        first = owui.bootstrap(ContractHost(), client_factory, self.manifest(), rendered_dir=self.stack.directory)
        self.assertTrue(first.ok, first.problem)
        self.assertEqual(first.created_groups, ("GIDEON-Users", "GIDEON-Admins", SERVICE_GROUP))
        self.assertEqual(first.removed_groups, ("stray",))
        self.assertEqual(first.removed_functions, ("hand_added",))
        self.assertEqual(first.minted, ("gideon_admin_api_key", "gideon_eval_api_key"))
        for name in first.minted:
            self.assertEqual((self.stack.secrets / name).stat().st_mode & 0o777, 0o440)

        admin = client_factory(api_key=(self.stack.secrets / "gideon_admin_api_key").read_text().strip())
        self._assert_manifest_function_state(admin)
        groups = {group.name: group for group in admin.groups()}
        self.assertEqual(set(groups), {"GIDEON-Users", "GIDEON-Admins", SERVICE_GROUP})

        def features_of(group: owui.Group) -> Any:
            return group.permissions["features"]

        self.assertTrue(features_of(groups[SERVICE_GROUP])["api_keys"])
        self.assertFalse(features_of(groups["GIDEON-Users"])["api_keys"])
        self.assertFalse(features_of(groups["GIDEON-Admins"])["api_keys"])
        users = {user.email: user for user in admin.users_all()}
        self.assertEqual(users[EVAL_IDENTITY.email].role, "user")
        self.assertEqual(admin.group_members(groups[SERVICE_GROUP].id), (users[EVAL_IDENTITY.email].id,))
        self._assert_manifest_model_state(admin, users[BREAK_GLASS.email].id)

        # The eval key reaches what it is for and nothing administrative.
        eval_client = client_factory(api_key=(self.stack.secrets / "gideon_eval_api_key").read_text().strip())
        self._assert_live_model_listing(eval_client)
        self.assertIn(eval_client.request("GET", "/api/v1/users/all").status, (401, 403))
        # The base listing remains admin-only (docs/research/owui-model-record.md §8.3).
        self.assertIn(eval_client.request("GET", "/api/v1/models/base").status, (401, 403))
        # The admin key is bound to the allowlist too.
        self.assertEqual(admin.request("GET", "/api/v1/auths/").status, 403)

        # The admin model editor uses these full-column update/create routes
        # (docs/research/owui-model-record.md §6 and §8.2); use its session,
        # not the API key, for the hand edits.  The base record's edit flips a
        # capability, drops `hidden`, and empties the grant list at once.
        edited_capabilities = dict(BASE_MODEL_CAPABILITIES)
        edited_capabilities["builtin_tools"] = True
        edited = session.request(
            "POST",
            "/api/v1/models/model/update",
            {
                "id": ENGINE_SERVICE_NAME,
                "base_model_id": None,
                "name": ENGINE_SERVICE_NAME,
                "meta": {"capabilities": edited_capabilities},
                "params": {},
                "access_grants": [],
                "is_active": True,
            },
        )
        self.assertEqual(edited.status, 200, edited.body)
        # The update route replaces the full preset row, including params,
        # description, grants, and the attachment list; this hand edit adds a
        # stray Function id (docs/research/owui-preset-system-prompt.md
        # §2.1–2.3, docs/research/owui-model-record.md §1.2, §6).
        edited_general = session.request(
            "POST",
            "/api/v1/models/model/update",
            {
                "id": GENERAL_PRESET_ID,
                "base_model_id": ENGINE_SERVICE_NAME,
                "name": "General",
                "meta": {
                    "description": "hand-edited",
                    "capabilities": dict(GENERAL_CAPABILITIES),
                    "suggestion_prompts": [
                        {"title": ["Fictitious", "hand-edit"], "content": "hand-edited"}
                    ],
                    "filterIds": [HAND_ADDED_FUNCTION["id"]],
                },
                "params": {"system": "hand-edited"},
                "access_grants": [],
                "is_active": True,
            },
        )
        self.assertEqual(edited_general.status, 200, edited_general.body)

        users_group = next(group for group in admin.groups() if group.name == "GIDEON-Users")
        permissions = dict(users_group.permissions)
        chat = permissions["chat"]
        assert isinstance(chat, dict)
        permissions["chat"] = {**chat, "web_upload": True}
        edited_group = session.request(
            "POST",
            f"/api/v1/groups/id/{users_group.id}/update",
            {"name": users_group.name, "description": "", "permissions": permissions},
        )
        self.assertEqual(edited_group.status, 200, edited_group.body)

        document = self.manifest()
        functions = document["functions"]
        assert isinstance(functions, list)
        for function in functions:
            assert isinstance(function, dict)
            identifier = function["id"]
            assert isinstance(identifier, str)
            function_edit = session.request(
                "POST",
                f"/api/v1/functions/id/{identifier}/update",
                {
                    **function,
                    "content": "class Filter:\n    pass\n",
                },
            )
            self.assertEqual(function_edit.status, 200, function_edit.body)
            toggled = session.request(
                "POST", f"/api/v1/functions/id/{identifier}/toggle"
            )
            self.assertEqual(toggled.status, 200, toggled.body)
            globally_toggled = session.request(
                "POST", f"/api/v1/functions/id/{identifier}/toggle/global"
            )
            self.assertEqual(globally_toggled.status, 200, globally_toggled.body)

        stray = session.request(
            "POST",
            "/api/v1/models/create",
            {
                "id": "stray_model",
                "base_model_id": ENGINE_SERVICE_NAME,
                "name": "Stray",
                "meta": {},
                "params": {},
            },
        )
        self.assertEqual(stray.status, 200, stray.body)

        second = owui.bootstrap(ContractHost(), client_factory, self.manifest(), rendered_dir=self.stack.directory)
        self.assertTrue(second.ok, second.problem)
        self.assertEqual(second.updated_groups, ("GIDEON-Users",))
        self.assertEqual(second.removed_models, ("stray_model",))
        self.assertEqual(second.removed_functions, ())
        self._assert_manifest_function_state(admin)
        self._assert_manifest_model_state(admin, users[BREAK_GLASS.email].id)

        # The pinned delete route answers the DELETE method alone (a POST is 405).
        for function in functions:
            assert isinstance(function, dict)
            identifier = function["id"]
            assert isinstance(identifier, str)
            removed = session.request(
                "DELETE", f"/api/v1/functions/id/{identifier}/delete"
            )
            self.assertEqual(removed.status, 200, removed.body)
        third = owui.bootstrap(ContractHost(), client_factory, self.manifest(), rendered_dir=self.stack.directory)
        self.assertTrue(third.ok, third.problem)
        self.assertEqual(third.removed_functions, ())
        self._assert_manifest_function_state(admin)

        fourth = owui.bootstrap(ContractHost(), client_factory, self.manifest(), rendered_dir=self.stack.directory)
        self.assertTrue(fourth.ok, fourth.problem)
        self.assertEqual(fourth, owui.BootstrapReport())

    def test_rendered_environment_facts_return_after_frontend_restart(self) -> None:
        """Panel edits are process-local; restart restores the rendered environment facts."""

        admin = self.admin_session()
        config = admin.request("GET", "/api/config")
        self.assertEqual(config.status, 200, config.body)
        assert isinstance(config.body, dict)
        self.assertIsInstance(config.body["default_models"], str)
        self.assertEqual(config.body["default_models"], GENERAL_PRESET_ID)

        self._assert_live_model_listing(admin)

        edited_models = admin.request(
            "POST",
            "/api/v1/configs/models",
            {
                "DEFAULT_MODELS": "fictitious-panel-default",
                "DEFAULT_PINNED_MODELS": None,
                "MODEL_ORDER_LIST": None,
            },
        )
        self.assertEqual(edited_models.status, 200, edited_models.body)
        edited_config = admin.request("GET", "/api/config")
        self.assertEqual(edited_config.status, 200, edited_config.body)
        assert isinstance(edited_config.body, dict)
        self.assertEqual(edited_config.body["default_models"], "fictitious-panel-default")

        edited_arena = admin.request(
            "POST",
            "/api/v1/evaluations/config",
            {"ENABLE_EVALUATION_ARENA_MODELS": True},
        )
        self.assertEqual(edited_arena.status, 200, edited_arena.body)
        arena_config = admin.request("GET", "/api/v1/evaluations/config")
        self.assertEqual(arena_config.status, 200, arena_config.body)
        assert isinstance(arena_config.body, dict)
        self.assertTrue(arena_config.body["ENABLE_EVALUATION_ARENA_MODELS"])

        # The live listing now proves the arena entry appears beside the stub
        # model and General; the config read-back is what the restart reverts.
        self._assert_live_model_listing(admin, arena=True)

        restarted = self.stack.compose("restart", "open-webui")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        ready = owui.wait_ready(client_factory(), attempts=READY_ATTEMPTS, sleep=time.sleep)
        self.assertTrue(ready.ok, ready.problem)

        restored_config = admin.request("GET", "/api/config")
        self.assertEqual(restored_config.status, 200, restored_config.body)
        assert isinstance(restored_config.body, dict)
        self.assertEqual(restored_config.body["default_models"], GENERAL_PRESET_ID)
        self._assert_live_model_listing(admin)
        restored_arena = admin.request("GET", "/api/v1/evaluations/config")
        self.assertEqual(restored_arena.status, 200, restored_arena.body)
        assert isinstance(restored_arena.body, dict)
        self.assertFalse(restored_arena.body["ENABLE_EVALUATION_ARENA_MODELS"])

    def test_store_convergence_is_idempotent_and_the_audit_role_is_insert_only(self) -> None:
        again = stores.converge(ContractHost(), self.stack.directory, root=ROOT)
        self.assertEqual(again, stores.ConvergeReport())
        select = self.stack.compose("exec", "-T", "postgres", "psql", "-U", "gideon_audit", "-d", "gideon", "-tAc", "SELECT count(*) FROM audit_log")
        self.assertNotEqual(select.returncode, 0)
        self.assertIn("permission denied", select.stderr)


if __name__ == "__main__":
    unittest.main()

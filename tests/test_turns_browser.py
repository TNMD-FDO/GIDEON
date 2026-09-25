"""Tests for the browser page seam and launcher preconditions."""

import ast
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import unquote

from gideon import guardrail
from gideon.evaluation.turns import browser, chromium, classify, run
from gideon.host import models, site, tls
from gideon.host.owui import Client, OwuiError, Response
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike
from tools.turns import cli

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = Path("/etc/gideon/site.yaml")
SITE_TEXT = (ROOT / "config/site.example.yaml").read_text(encoding="utf-8")
ALLOWED_EXTERNAL = frozenset(sys.stdlib_module_names) | {"yaml"}


class FakeHost:
    """A small dict-backed Host for launcher and footprint checks."""

    def __init__(self, *, site_text: str = SITE_TEXT, models_text: str | None = None) -> None:
        self.euid = 0
        self.files: dict[str, str] = {
            str(SITE_PATH): site_text,
            str(ROOT / "models.lock"):
                (ROOT / "models.lock").read_text(encoding="utf-8")
                if models_text is None
                else models_text,
        }
        self.directories: dict[str, list[str]] = {}
        self.read_paths: list[str] = []
        self.stats: dict[str, SimpleNamespace] = {}
        self.commands: list[list[str]] = []
        self.command_results: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}

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
        del check, input, cwd, env, timeout, passthrough
        command = [str(value) for value in argv]
        self.commands.append(command)
        return self.command_results.get(
            tuple(command), subprocess.CompletedProcess(command, 0, "", "")
        )

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.read_paths.append(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        if key not in self.directories:
            raise NotADirectoryError(key)
        return list(self.directories[key])

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        metadata = self.stats[os.fspath(path)]
        return os.stat_result((metadata.st_mode, 0, 0, 1, metadata.st_uid, 0, 0, 0, 0, 0))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del mode, parents, exist_ok
        self.directories.setdefault(os.fspath(path), [])

    def geteuid(self) -> int:
        return self.euid


def _base_model_id(host: FakeHost) -> str:
    """Derive the fake site's generator name through the committed lock loader."""

    site_result = site.load_site(SITE_PATH, host=host)
    assert site_result.config is not None and not site_result.errors
    models_result = models.load_models_lock(ROOT / "models.lock", host=host)
    assert models_result.lock is not None and not models_result.errors
    selected = models.select_profile(
        models_result.lock, site_result.config.hardware_profile
    )
    assert not isinstance(selected, Problem)
    generator = selected.model("generator")
    assert generator is not None
    return generator.serve.served_name


def completed(command: Sequence[str], returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), returncode, stdout, "certutil failed")


class BrowserSurface(TestCase):
    def test_disposition_allows_only_the_site_origin_and_in_page_schemes(self) -> None:
        hostname = "Frontend.Example"
        allowed = (
            "https://frontend.example/",
            "https://FRONTEND.EXAMPLE:443/auth",
            "wss://frontend.example/ws/socket.io",
            "data:text/plain,ok",
            "blob:https://other.example/id",
            "about:blank",
        )
        refused = (
            "http://frontend.example/",
            "https://frontend.example:8443/",
            "wss://other.example/ws",
            "https://other.example/",
            "ftp://frontend.example/file",
            "javascript:alert(1)",
            "not a URL",
        )
        for value in allowed:
            with self.subTest(value=value):
                self.assertEqual(chromium.disposition(value, hostname), "continue")
        for value in refused:
            with self.subTest(value=value):
                self.assertEqual(chromium.disposition(value, hostname), "abort")

    def test_request_log_renders_and_counts_off_host_names(self) -> None:
        ticks = iter((10.0, 10.25, 10.75, 11.0))
        log = chromium.RequestLog(lambda: next(ticks))
        log.append("request", "GET", "https://frontend.example/", "continue")
        log.append("websocket", "GET", "wss://outside.example/ws", "abort")
        log.append("request", "GET", "https://outside.example/", "abort")

        counts = log.counts()
        self.assertEqual((counts.on_host, counts.off_host), (1, 2))
        self.assertEqual(counts.off_hostnames, ("outside.example",))
        self.assertEqual(
            log.render().splitlines(),
            [
                "0.250s request GET https://frontend.example/ continue",
                "0.750s websocket GET wss://outside.example/ws abort",
                "1.000s request GET https://outside.example/ abort",
            ],
        )

    def test_page_error_carries_certificate_flag(self) -> None:
        error = browser.PageError("certificate verify failed", certificate=True)
        self.assertEqual(str(error), "certificate verify failed")
        self.assertTrue(error.certificate)

    def test_trust_ca_creates_database_adds_ca_and_is_idempotent(self) -> None:
        host = FakeHost()
        store = str(chromium.TRUST_STORE_PATH)
        host.directories[str(chromium.BROWSERS_DIR)] = []
        host.command_results[("certutil", "-d", f"sql:{store}", "-N", "--empty-password")] = completed([])
        host.command_results[("certutil", "-d", f"sql:{store}", "-L", "-n", chromium.TRUST_NICKNAME)] = completed([], 255)
        host.command_results[(
            "certutil", "-d", f"sql:{store}", "-A", "-t", "C,,", "-n",
            chromium.TRUST_NICKNAME, "-i", tls.CA_PATH,
        )] = completed([])

        detail, problem = chromium.trust_ca(host)
        self.assertIsNone(problem)
        self.assertIn("trusted", detail)
        self.assertEqual(host.commands, [
            ["certutil", "-d", f"sql:{store}", "-N", "--empty-password"],
            ["certutil", "-d", f"sql:{store}", "-L", "-n", chromium.TRUST_NICKNAME],
            ["certutil", "-d", f"sql:{store}", "-A", "-t", "C,,", "-n", chromium.TRUST_NICKNAME, "-i", tls.CA_PATH],
        ])

        host.files[str(chromium.TRUST_STORE_PATH / "cert9.db")] = "database"
        host.command_results[("certutil", "-d", f"sql:{store}", "-L", "-n", chromium.TRUST_NICKNAME)] = completed([], stdout=chromium.TRUST_NICKNAME)
        detail, problem = chromium.trust_ca(host)
        self.assertIsNone(problem)
        self.assertIn("already trusted", detail)
        self.assertEqual(host.commands[-1], ["certutil", "-d", f"sql:{store}", "-L", "-n", chromium.TRUST_NICKNAME])

    def test_trust_ca_reports_missing_certutil_and_import_failure_without_bytes(self) -> None:
        store = str(chromium.TRUST_STORE_PATH)
        host = FakeHost()
        host.command_results[("certutil", "-d", f"sql:{store}", "-N", "--empty-password")] = completed([], 127)
        detail, problem = chromium.trust_ca(host)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("libnss3-tools", problem.fix)
        self.assertNotIn(tls.CA_PATH, detail)

        host = FakeHost()
        host.command_results[("certutil", "-d", f"sql:{store}", "-L", "-n", chromium.TRUST_NICKNAME)] = completed([], 255)
        secret_bytes = "-----BEGIN CERTIFICATE-----private-----END CERTIFICATE-----"
        host.command_results[(
            "certutil", "-d", f"sql:{store}", "-A", "-t", "C,,", "-n",
            chromium.TRUST_NICKNAME, "-i", tls.CA_PATH,
        )] = completed([], 2, secret_bytes)
        _detail, problem = chromium.trust_ca(host)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn(store, problem.problem)
        self.assertNotIn(secret_bytes, problem.problem + problem.fix)

    def test_password_file_checks_owner_mode_and_content(self) -> None:
        host = FakeHost()
        self.assertIsNotNone(chromium.password_file_problem(host))

        path = str(chromium.PASSWORD_FILE)
        host.files[path] = "secret\n"
        host.stats[path] = SimpleNamespace(st_uid=1000, st_mode=0o600)
        problem = chromium.password_file_problem(host)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("chown", problem.fix)

        host.stats[path] = SimpleNamespace(st_uid=0, st_mode=0o640)
        problem = chromium.password_file_problem(host)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("chmod 0600", problem.fix)

        host.stats[path] = SimpleNamespace(st_uid=0, st_mode=0o600)
        host.files[path] = "\n"
        problem = chromium.password_file_problem(host)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("empty", problem.problem)

        host.files[path] = "secret\r\n"
        self.assertIsNone(chromium.password_file_problem(host))
        self.assertEqual(chromium.read_password(host), "secret")

    def test_playwright_pin_and_browser_footprint_preconditions(self) -> None:
        from importlib.metadata import PackageNotFoundError

        missing = chromium.playwright_problem(
            lambda _name: (_ for _ in ()).throw(PackageNotFoundError("playwright"))
        )
        self.assertIsNotNone(missing)
        assert missing is not None
        self.assertIn(f"playwright=={chromium.PLAYWRIGHT_VERSION}", missing.fix)
        mismatch = chromium.playwright_problem(lambda _name: "not-the-installed-pin")
        self.assertIsNotNone(mismatch)
        self.assertIsNone(
            chromium.playwright_problem(lambda _name: chromium.PLAYWRIGHT_VERSION)
        )

        host = FakeHost()
        self.assertIsNotNone(chromium.browser_problem(host, chromium.BROWSERS_DIR))
        shell = "chromium_headless_shell-1234"
        host.directories[str(chromium.BROWSERS_DIR)] = [shell]
        host.directories[str(chromium.BROWSERS_DIR / shell)] = []
        self.assertIsNone(chromium.browser_problem(host, chromium.BROWSERS_DIR))

    def test_turn_modules_keep_playwright_behind_the_browser_boundary(self) -> None:
        turn_roots = (
            (ROOT / "gideon/evaluation/turns", ALLOWED_EXTERNAL | {"gideon"}),
            (ROOT / "tools/turns", ALLOWED_EXTERNAL | {"gideon", "tools"}),
        )
        for turn_root, allowed_external in turn_roots:
            for path in sorted(turn_root.glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                if path.name != "chromium.py":
                    for node in self._module_imports(tree):
                        for target in self._import_targets(node):
                            self.assertIn(
                                target.partition(".")[0],
                                allowed_external,
                                f"{path}:{node.lineno} imports {target}",
                            )
                    continue
                for import_node in ast.walk(tree):
                    if not isinstance(import_node, (ast.Import, ast.ImportFrom)):
                        continue
                    targets = self._import_targets(import_node)
                    if not any(target.partition(".")[0] == "playwright" for target in targets):
                        continue
                    self.assertTrue(
                        self._inside_function(tree, import_node)
                        or self._inside_type_checking(tree, import_node),
                        f"{path}:{import_node.lineno} imports Playwright outside its boundary",
                    )

    @staticmethod
    def _import_targets(node: ast.stmt) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        assert isinstance(node, ast.ImportFrom)
        base = node.module or ""
        return [base, *[f"{base}.{alias.name}" for alias in node.names]]

    @staticmethod
    def _module_imports(tree: ast.AST) -> tuple[ast.stmt, ...]:
        imports: list[ast.stmt] = []

        class Visitor(ast.NodeVisitor):
            def visit_Import(self, node: ast.Import) -> None:
                imports.append(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                imports.append(node)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                del node

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                del node

        Visitor().visit(tree)
        return tuple(imports)

    @staticmethod
    def _inside_function(tree: ast.AST, target: ast.AST) -> bool:
        found = False

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[ast.AST] = []

            def generic_visit(self, node: ast.AST) -> None:
                nonlocal found
                if node is target and any(
                    isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for parent in self.stack
                ):
                    found = True
                    return
                self.stack.append(node)
                super().generic_visit(node)
                self.stack.pop()

        Visitor().visit(tree)
        return found

    @staticmethod
    def _inside_type_checking(tree: ast.AST, target: ast.AST) -> bool:
        found = False

        class Visitor(ast.NodeVisitor):
            def visit_If(self, node: ast.If) -> None:
                nonlocal found
                is_guard = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
                if is_guard:
                    for child in node.body:
                        if target is child or target in ast.walk(child):
                            found = True
                            break
                if not found:
                    self.generic_visit(node)

        Visitor().visit(tree)
        return found


class FakePage:
    """A phase-driven Page fake for authentication and one completed turn."""

    def __init__(
        self,
        frontend: "FakeFrontend",
        *,
        email_form: bool = False,
        certificate: bool = False,
        drains: Sequence[browser.PageDrain] = (),
    ) -> None:
        self.frontend = frontend
        self.phase = "email" if email_form else "auth"
        self.certificate = certificate
        self.drains = list(drains)
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.presses: list[str] = []
        self.gotos: list[str] = []
        self.waits: list[float] = []
        self.sleeps: list[float] = []
        self.screenshots: list[Path] = []
        self.screenshot_written = False
        self.watched = False
        self.expanded = False
        self.token = "browser-session-token"
        self._stop_seen = False
        self._chat_id = "browser-chat"
        self._last_regions: dict[str, str] = {"block": "", "answer": ""}

    def goto(self, path: str) -> None:
        self.gotos.append(path)
        if self.certificate and path.endswith("/auth"):
            raise browser.PageError("certificate verify failed", certificate=True)
        if path == "/":
            self.phase = "chat"
            self._stop_seen = False
            self.expanded = False

    def fill(self, selector: str, text: str) -> None:
        self.fills.append((selector, text))

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if selector == browser.LDAP_TOGGLE_SELECTOR:
            self.phase = "auth"
        elif selector == browser.AUTHENTICATE_SELECTOR:
            self.phase = "chat"
        elif selector == browser.SEND_BUTTON_SELECTOR:
            self.phase = "running"
            prompt = dict(self.fills).get(browser.CHAT_INPUT_SELECTOR, "")
            self.frontend.create_chat(prompt, self._chat_id)
        elif selector == browser.COLLAPSIBLE_BUTTON_SELECTOR:
            self.expanded = True

    def press(self, key: str) -> None:
        self.presses.append(key)

    def text(self, selector: str) -> str | None:
        if selector == browser.MODEL_SELECTOR:
            return "General"
        if selector.startswith(browser.INTEGRATION_TOGGLE_SELECTOR):
            return "Web Search"
        return None

    def texts(self, selector: str) -> Sequence[str]:
        if selector == browser.MODEL_OPTION_SELECTOR:
            return ("General", "Fast")
        if selector == browser.MENU_ITEM_SELECTOR:
            return ("Attach file", "Use tools")
        if selector == browser.INTEGRATION_TOGGLE_SELECTOR:
            return ("Web Search",)
        return ()

    def count(self, selector: str) -> int:
        if selector == browser.USERNAME_SELECTOR:
            return int(self.phase == "auth")
        if selector in {
            browser.PASSWORD_SELECTOR,
            browser.AUTHENTICATE_SELECTOR,
        }:
            return int(self.phase == "auth")
        if selector == browser.LDAP_TOGGLE_SELECTOR:
            return int(self.phase == "email")
        if selector == browser.CHAT_INPUT_SELECTOR:
            return int(self.phase in {"chat", "running", "done"})
        if selector == browser.COLLAPSIBLE_BUTTON_SELECTOR:
            return int(self.phase in {"running", "done"})
        if selector == browser.STOP_BUTTON_SELECTOR:
            if self.phase == "running" and not self._stop_seen:
                self._stop_seen = True
                return 1
            return 0
        if selector == browser.MESSAGE_SELECTOR:
            return int(self.phase in {"running", "done"})
        if selector in {
            browser.MODEL_SELECTOR,
            browser.INPUT_MENU_SELECTOR,
            browser.INTEGRATION_MENU_SELECTOR,
        }:
            return int(self.phase in {"chat", "running", "done"})
        if selector == browser.INTEGRATION_TOGGLE_SELECTOR:
            return 1
        return 0

    def attribute(self, selector: str, name: str) -> str | None:
        if selector == browser.COLLAPSIBLE_BUTTON_SELECTOR and name == "aria-expanded":
            return "true" if self.expanded else "false"
        if selector.startswith(browser.INTEGRATION_TOGGLE_SELECTOR) and name == "aria-pressed":
            return "true"
        return None

    def url(self) -> str:
        if self.phase == "auth" or self.phase == "email":
            return "https://frontend.example/auth"
        if self.phase in {"chat", "running", "done"}:
            return f"https://frontend.example/c/{self._chat_id}" if self.phase != "chat" else "https://frontend.example/"
        return "https://frontend.example/"

    def storage(self, key: str) -> str | None:
        return self.token if key == "token" and self.phase in {"chat", "running", "done"} else None

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fake screenshot", encoding="utf-8")
        self.screenshot_written = True
        self.screenshots.append(path)

    def wait_until(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
        self.waits.append(timeout_seconds)
        if predicate():
            return True
        if self.phase == "running":
            self.phase = "done"
        return predicate()

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def watch(self) -> None:
        self.watched = True

    def drain(self) -> browser.PageDrain:
        if self.drains:
            drained = self.drains.pop(0)
            self._last_regions = dict(drained.regions)
            return drained
        self.phase = "done"
        return browser.PageDrain(frames=(), regions=dict(self._last_regions))


class FakeClient(Client):
    """A token-checking client over the browser test frontend."""

    def __init__(self, frontend: "FakeFrontend", token: str | None) -> None:
        super().__init__("http://fake", token=token)
        self.frontend = frontend

    def request(self, method: str, path: str, body: object | None = None) -> Response:
        return self.frontend.handle(method, path, body, self._token)


class FakeFrontend:
    """The API read-back and cleanup surface behind the browser page."""

    def __init__(self) -> None:
        self.token = "browser-session-token"
        self.chats: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str, object | None, str | None]] = []
        gate_texts = classify.load_gate_texts(ROOT)
        self.branch_refusal = gate_texts.branch_refusal
        self.base_model_id: str | None = None
        self.base_model_status = 400
        self.base_model_detail = self.branch_refusal
        self.base_model_content = "A clean base-model answer."
        self.base_model_stray = False
        self.completions_transport_error = False

    def factory(self, *, token: str | None = None) -> Client:
        return FakeClient(self, token)

    def create_chat(self, prompt: str, chat_id: str) -> None:
        user_id = "browser-user"
        assistant_id = "browser-assistant"
        self.chats[chat_id] = {
            "id": chat_id,
            "chat": {
                "history": {
                    "currentId": assistant_id,
                    "messages": {
                        user_id: {"id": user_id, "role": "user", "content": prompt},
                        assistant_id: {
                            "id": assistant_id,
                            "role": "assistant",
                            "parentId": user_id,
                            "content": "A clean doctrinal answer.",
                            "done": True,
                            "output": [{"type": "message", "content": []}],
                        },
                    },
                }
            },
        }

    def handle(
        self,
        method: str,
        path: str,
        body: object | None,
        token: str | None,
    ) -> Response:
        self.calls.append((method, path, body, token))
        if token != self.token:
            return Response(403, {"detail": "not owner"})
        if method == "POST" and path == "/api/chat/completions":
            if self.completions_transport_error:
                raise OwuiError("connection refused by the ingress", "Check the ingress, then retry.")
            request = body if isinstance(body, Mapping) else {}
            if request.get("model") == self.base_model_id:
                if self.base_model_stray:
                    self.create_chat("probe", "probe-stray")
                if self.base_model_status != 200:
                    return Response(self.base_model_status, {"detail": self.base_model_detail})
                return Response(
                    200,
                    {
                        "choices": [
                            {"message": {"content": self.base_model_content, "reasoning": ""}}
                        ]
                    },
                )
            # The probe's one completion is the bare base-model request; any
            # other falls through to the handler's not-found answer rather than
            # being given a plausible reply this fake does not model.
        if method == "GET" and path == "/api/v1/chats/list":
            return Response(200, [{"id": chat_id} for chat_id in self.chats])
        if method == "GET" and path.startswith("/api/v1/chats/"):
            return Response(200, self.chats.get(unquote(path.removeprefix("/api/v1/chats/"))))
        if method == "DELETE" and path.startswith("/api/v1/chats/"):
            self.chats.pop(unquote(path.removeprefix("/api/v1/chats/")), None)
            return Response(200, True)
        return Response(404, {"detail": "not found"})


class BrowserTurnIntegration(TestCase):
    def _host(self) -> FakeHost:
        host = FakeHost()
        password_path = str(chromium.PASSWORD_FILE)
        host.files[password_path] = "browser-password\n"
        host.stats[password_path] = SimpleNamespace(st_uid=0, st_mode=0o600)
        shell = "chromium_headless_shell-from-test"
        host.directories[str(chromium.BROWSERS_DIR)] = [shell]
        host.directories[str(chromium.BROWSERS_DIR / shell)] = []
        return host

    def _drains(self) -> tuple[browser.PageDrain, ...]:
        return (
            browser.PageDrain(
                frames=(
                    {
                        "instant": 12.5,
                        "summary": "Thinking...",
                        "expanded": True,
                        "block": {"text": "", "extended": False},
                        "answer": {"text": "A clean doctrinal answer.", "extended": False},
                    },
                ),
                regions={"block": "", "answer": "A clean doctrinal answer."},
            ),
        )

    def test_signin_takes_the_ldap_toggle_and_reconstructs_frame_text(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, email_form=True)
        token = browser.signin(page, "gideon-test-user", "browser-password", timeout=1.0)
        self.assertEqual(token, page.token)
        self.assertIn(browser.LDAP_TOGGLE_SELECTOR, page.clicks)
        self.assertIn((browser.PASSWORD_SELECTOR, "browser-password"), page.fills)

        page = FakePage(
            frontend,
            drains=(
                browser.PageDrain(
                    frames=(
                        {
                            "instant": 1,
                            "summary": "Thinking...",
                            "expanded": True,
                            "block": {"text": "first", "extended": False},
                            "answer": {"text": "answer", "extended": False},
                        },
                        {
                            "instant": 2,
                            "summary": "Thought for 1 second",
                            "expanded": True,
                            "block": {"text": " then", "extended": True},
                            "answer": {"text": " replaced", "extended": False},
                        },
                    ),
                    regions={"block": "first then", "answer": "answer replaced"},
                ),
            ),
        )
        with TemporaryDirectory() as directory:
            result = browser.turn(
                page,
                "prompt",
                judge=lambda _block, _answer: None,
                is_replacement=lambda _answer: False,
                monotonic=lambda: 0.0,
                poll=0.0,
                page_timeout=1.0,
                deadline=1.0,
                out=Path(directory),
                row_name="answered",
            )
        self.assertIsNone(result.problem)
        self.assertEqual(result.chat_id, "browser-chat")
        # Two frames reconstructed, then the regions read at the end as a third
        # state because they differ from the last frame's texts.
        self.assertEqual(
            [(entry.block_length, entry.answer_length) for entry in result.entries],
            [(len("first"), len("answer")), (len("first then"), len(" replaced")), (len("first then"), len("answer replaced"))],
        )
        # The first state whose block carried text is kept too (the withholding failure's evidence).
        self.assertEqual(result.texts, {0: ("first", "answer"), 2: ("first then", "answer replaced")})
        self.assertIsNotNone(result.block_opened_at)
        self.assertEqual(result.regions["answer"], "answer replaced")

    def _run(
        self,
        *,
        page: FakePage,
        args: Sequence[str],
        page_factory: Callable[
            [str, chromium.RequestLog], tuple[browser.Page, Callable[[], None], str]
        ]
        | None = None,
        frontend: FakeFrontend | None = None,
        host: FakeHost | None = None,
    ) -> tuple[int, str, str, FakeFrontend, FakeHost]:
        frontend = frontend or page.frontend
        host = host if host is not None else self._host()
        frontend.base_model_id = _base_model_id(host)
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n  - id: answered\n    prompt: doctrinal prompt\n    expect: answered\n",
                encoding="utf-8",
            )
            output = Path(directory) / "out"
            stdout = StringIO()
            stderr = StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                patch("tools.turns.cli.chromium.playwright_problem", return_value=None),
                patch("tools.turns.cli.chromium.browser_problem", return_value=None),
            ):
                code = cli.main(
                    [str(cases_path), "--browser", "--out", str(output), *args],
                    host=host,
                    client_factory=frontend.factory,
                    page_factory=page_factory
                    or (
                        lambda _hostname, _log: (
                            page,
                            lambda: setattr(page, "closed", True),
                            "browser detail",
                        )
                    ),
                    now=lambda: datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
                    checkout=ROOT,
                    site_path=Path("/etc/gideon/site.yaml"),
                )
            return code, stdout.getvalue(), stderr.getvalue(), frontend, host

    def test_one_browser_turn_reads_current_record_and_cleans_up(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())
        code, stdout, _stderr, frontend, host = self._run(page=page, args=[])

        self.assertEqual(code, 0)
        self.assertIn("browser: ok — browser detail", stdout)
        self.assertIn("signin: ok — signed in as gideon-test-user through the LDAP form", stdout)
        self.assertIn(
            "observe: ok — selector: General (2 listed: General, Fast); + menu: Attach file, Use tools; integrations: Web Search on",
            stdout,
        )
        self.assertIn("answered: ok", stdout)
        self.assertNotIn(page.token, stdout)
        self.assertNotIn("browser-password", stdout)
        self.assertTrue(page.watched)
        self.assertIn((browser.PASSWORD_SELECTOR, "browser-password"), page.fills)
        self.assertTrue(page.screenshots)
        self.assertTrue(page.screenshot_written)
        self.assertEqual(
            {path.name for path in page.screenshots},
            {"observe.png", "observe-plus.png", "observe-integrations.png", "answered.png"},
        )
        self.assertEqual(page.presses, ["Escape", "Escape", "Escape"])
        self.assertTrue(getattr(page, "closed", False))
        self.assertEqual([call[0] for call in frontend.calls if call[0] == "DELETE"], ["DELETE"])
        self.assertEqual(frontend.chats, {})
        records = [value for key, value in host.files.items() if key.endswith("answered.json")]
        self.assertEqual(len(records), 1)
        self.assertNotIn("browser-password", records[0])
        record = json.loads(records[0])
        self.assertEqual(record["browser"]["chat_id"], "browser-chat")
        self.assertEqual(record["browser"]["states"][0]["tripped"], None)
        self.assertFalse(record["browser"]["states"][0]["block_text"])
        self.assertIsNone(record["browser"]["reasoning_painted_at"])
        self.assertIsNone(record["browser"]["refused_index"])
        self.assertIsNone(record["browser"]["refused_at"])
        self.assertEqual(record["browser"]["ended_at"], 0.0)
        self.assertFalse(record["verdict"]["reasoning_stored"])
        self.assertEqual(set(record["browser"]["texts"]), {"0"})

    def test_certificate_failure_is_a_browser_row_without_signin(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, certificate=True)
        code, stdout, _stderr, _frontend, _host = self._run(page=page, args=[])
        self.assertEqual(code, 1)
        self.assertIn("browser: refuse", stdout)
        self.assertIn("--trust-ca", stdout)
        self.assertNotIn("signin:", stdout)
        self.assertTrue(getattr(page, "closed", False))

    def test_browser_out_and_stream_refusals_and_dry_run(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend)
        host = self._host()
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n"
                "  - {id: answered, prompt: p, expect: answered}\n"
                "  - {id: answered-two, prompt: q, expect: answered}\n",
                encoding="utf-8",
            )
            missing = StringIO()
            with redirect_stdout(missing), patch("tools.turns.cli.chromium.playwright_problem", return_value=None):
                code = cli.main([str(cases_path), "--browser"], host=host, checkout=ROOT)
            self.assertEqual(code, 1)
            self.assertIn("--out", missing.getvalue())

            streamed = StringIO()
            with redirect_stdout(streamed), patch("tools.turns.cli.chromium.playwright_problem", return_value=None):
                code = cli.main([str(cases_path), "--browser", "--out", str(Path(directory) / "out"), "--stream"], host=host, checkout=ROOT)
            self.assertEqual(code, 1)
            self.assertIn("--probe-inlet", streamed.getvalue())

            dry = StringIO()
            factory_calls: list[str] = []

            def page_factory(
                hostname: str, _log: chromium.RequestLog
            ) -> tuple[browser.Page, Callable[[], None], str]:
                factory_calls.append(hostname)
                return page, lambda: None, "unused"

            with redirect_stdout(dry), patch("tools.turns.cli.chromium.playwright_problem", return_value=None), patch("tools.turns.cli.chromium.browser_problem", return_value=None):
                code = cli.main(
                    [str(cases_path), "--browser", "--out", str(Path(directory) / "dry"), "--dry-run"],
                    host=host,
                    page_factory=page_factory,
                    checkout=ROOT,
                )
            self.assertEqual(code, 0)
            self.assertIn("mode: browser", dry.getvalue())
            self.assertIn("engine calls: 6 (3 per browser turn)", dry.getvalue())
            self.assertIn(str(chromium.BROWSER_HOME), dry.getvalue())
            self.assertEqual(factory_calls, [])

    def test_browser_precondition_refusals_are_rows_with_their_fixes(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend)
        host = self._host()
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n  - {id: answered, prompt: p, expect: answered}\n",
                encoding="utf-8",
            )
            refusals = (
                (
                    "password_file_problem",
                    Problem("password refusal", "type it again"),
                    "type it again",
                ),
                (
                    "playwright_problem",
                    Problem("pin refusal", "install the pin"),
                    "install the pin",
                ),
                (
                    "browser_problem",
                    Problem("shell refusal", "install the shell"),
                    "install the shell",
                ),
            )
            for helper, problem, fix in refusals:
                with self.subTest(helper=helper):
                    stdout = StringIO()
                    target = f"tools.turns.cli.chromium.{helper}"
                    with redirect_stdout(stdout), patch(target, return_value=problem):
                        code = cli.main(
                            [str(cases_path), "--browser", "--out", str(Path(directory) / helper)],
                            host=host,
                            page_factory=lambda _hostname, _log: (page, lambda: None, "unused"),
                            checkout=ROOT,
                        )
                    self.assertEqual(code, 1)
                    self.assertIn("preconditions: refuse", stdout.getvalue())
                    self.assertIn(fix, stdout.getvalue())

    def test_case_narrows_browser_run_and_search_refusal_follows_selection(self) -> None:
        host = self._host()
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n"
                "  - id: plain\n    prompt: p\n    expect: answered\n"
                "  - id: searched\n    prompt: s\n    expect: answered\n    search: true\n",
                encoding="utf-8",
            )
            frontend = FakeFrontend()
            page = FakePage(frontend, drains=self._drains())
            output = StringIO()
            with (
                redirect_stdout(output),
                patch("tools.turns.cli.chromium.playwright_problem", return_value=None),
                patch("tools.turns.cli.chromium.browser_problem", return_value=None),
            ):
                code = cli.main(
                    [
                        str(cases_path),
                        "--browser",
                        "--case",
                        "plain",
                        "--out",
                        str(Path(directory) / "plain-out"),
                    ],
                    host=host,
                    client_factory=frontend.factory,
                    page_factory=lambda _hostname, _log: (
                        page,
                        lambda: None,
                        "browser detail",
                    ),
                    now=lambda: datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
                    checkout=ROOT,
                    site_path=SITE_PATH,
                )
            self.assertEqual(code, 0)
            self.assertIn("2 cases; 1 of 2 selected", output.getvalue())
            self.assertIn("plain: ok", output.getvalue())
            self.assertNotIn("searched:", output.getvalue())

            refused = StringIO()
            with redirect_stdout(refused):
                code = cli.main(
                    [
                        str(cases_path),
                        "--browser",
                        "--case",
                        "searched",
                        "--out",
                        str(Path(directory) / "searched-out"),
                    ],
                    host=host,
                    checkout=ROOT,
                    site_path=SITE_PATH,
                )
            self.assertEqual(code, 1)
            self.assertIn("search cases run in the API mode", refused.getvalue())

    def test_unfiltered_browser_refuses_before_root_check(self) -> None:
        host = self._host()
        host.euid = 1000
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n  - id: one\n    prompt: p\n    expect: answered\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [
                        str(cases_path),
                        "--unfiltered",
                        "--browser",
                        "--out",
                        str(Path(directory) / "out"),
                    ],
                    host=host,
                    checkout=ROOT,
                    site_path=SITE_PATH,
                )
            self.assertEqual(code, 1)
            self.assertIn("--unfiltered is not available with --browser", output.getvalue())
            self.assertIn("Drop --browser", output.getvalue())
            self.assertEqual(host.read_paths, [])

    def test_inlet_probe_row_is_clean(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())
        code, stdout, _stderr, frontend, host = self._run(
            page=page, args=["--probe-inlet"]
        )
        self.assertEqual(code, 0)
        self.assertIn(
            "inlet-base-model: ok — refused with the branch refusal (HTTP 400)", stdout
        )
        self.assertIn("summary: ok — 1 turns", stdout)
        probe_calls = [call for call in frontend.calls if call[1] == "/api/chat/completions"]
        self.assertEqual(len(probe_calls), 1)
        base_body = probe_calls[0][2]
        assert isinstance(base_body, Mapping)
        self.assertEqual(base_body["model"], frontend.base_model_id)
        self.assertNotIn("chat_id", base_body)
        self.assertIsNotNone(frontend.base_model_id)
        self.assertEqual(frontend.chats, {})
        base_record = json.loads(
            next(value for key, value in host.files.items() if key.endswith("inlet-base-model.json"))
        )
        self.assertEqual(base_record["status"], 400)
        self.assertEqual(base_record["body"]["detail"], frontend.branch_refusal)
        self.assertIn(str(ROOT / "models.lock"), host.read_paths)

    def test_probe_gate_text_loader_failure_is_content_free(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())
        with patch(
            "gideon.evaluation.turns.classify._load_function",
            side_effect=RuntimeError("private gate text"),
        ):
            code, stdout, _stderr, _frontend, _host = self._run(
                page=page, args=["--probe-inlet"]
            )
        function_path = ROOT / classify.BRANCH_GATE_FUNCTION
        self.assertEqual(code, 1)
        self.assertIn(
            f"preconditions: refuse — inlet gate text could not be loaded from {function_path}",
            stdout,
        )
        self.assertIn(f"Correct {function_path}, then retry.", stdout)
        self.assertNotIn("private gate text", stdout)

    def test_inlet_probe_failures_never_print_response_text(self) -> None:
        for status, detail in ((200, "private base answer"), (400, "private branch detail")):
            with self.subTest(status=status):
                frontend = FakeFrontend()
                frontend.base_model_status = status
                frontend.base_model_detail = detail
                page = FakePage(frontend, drains=self._drains())
                code, stdout, _stderr, _frontend, _host = self._run(
                    page=page, args=["--probe-inlet"]
                )
                self.assertEqual(code, 1)
                self.assertIn(
                    f"inlet-base-model: refuse — HTTP {status}, not the branch refusal",
                    stdout,
                )
                self.assertNotIn(detail, stdout)
                self.assertIn("2 turns" if status == 200 else "1 turns", stdout)

    def test_inlet_probe_deletes_and_reports_a_stray_chat(self) -> None:
        frontend = FakeFrontend()
        frontend.base_model_stray = True
        page = FakePage(frontend, drains=self._drains())
        code, stdout, _stderr, frontend, _host = self._run(
            page=page, args=["--probe-inlet"]
        )
        self.assertEqual(code, 0)
        self.assertIn(
            "inlet-base-model: ok — refused with the branch refusal (HTTP 400); stray chat deleted",
            stdout,
        )
        self.assertIn(("DELETE", "/api/v1/chats/probe-stray", None, frontend.token), frontend.calls)

    def test_probe_transport_failure_keeps_its_problem_and_fix(self) -> None:
        frontend = FakeFrontend()
        frontend.completions_transport_error = True
        page = FakePage(frontend, drains=self._drains())
        code, stdout, _stderr, _frontend, host = self._run(page=page, args=["--probe-inlet"])
        self.assertEqual(code, 1)
        # A request that fails carries its problem and the frontend's logs fix,
        # owuiturn.py's rule for every call it makes — never an HTTP 0.
        self.assertIn("inlet-base-model: refuse — probe error: connection refused by the ingress Fix: docker compose", stdout)
        self.assertNotIn("HTTP 0", stdout)
        record = json.loads(next(value for key, value in host.files.items() if key.endswith("inlet-base-model.json")))
        self.assertIn("logs open-webui", record["problem"]["fix"])

    def test_probe_internal_error_names_the_one_row(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())
        with patch(
            "gideon.evaluation.turns.run.session.probe_bare",
            side_effect=RuntimeError("probe broke"),
        ):
            code, stdout, _stderr, _frontend, _host = self._run(
                page=page, args=["--probe-inlet"]
            )
        self.assertEqual(code, 1)
        self.assertIn("inlet-base-model: refuse — internal error: RuntimeError: probe broke", stdout)

    def test_no_painted_frame_fails_even_with_a_clean_end_state(self) -> None:
        page = FakePage(
            FakeFrontend(),
            drains=(
                browser.PageDrain(frames=(), regions={"block": "", "answer": "A clean end state."}),
            ),
        )
        code, stdout, _stderr, _frontend, host = self._run(page=page, args=[])
        self.assertEqual(code, 1)
        self.assertIn("live: no frames", stdout)
        record = json.loads(next(value for key, value in host.files.items() if key.endswith("answered.json")))
        self.assertEqual([state["painted"] for state in record["browser"]["states"]], [False])
        self.assertTrue(record["browser"]["no_frames"])

    def test_probe_and_trust_flags_require_browser(self) -> None:
        host = self._host()
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n  - {id: answered, prompt: p, expect: answered}\n",
                encoding="utf-8",
            )
            for flag in ("--probe-inlet", "--trust-ca"):
                output = StringIO()
                with redirect_stdout(output):
                    code = cli.main(
                        [str(cases_path), flag], host=host, checkout=ROOT
                    )
                self.assertEqual(code, 1)
                self.assertIn("requires --browser", output.getvalue())

    def test_probe_model_lock_preconditions_use_loader_and_profile_selection(self) -> None:
        cases_text = "cases:\n  - {id: answered, prompt: p, expect: answered}\n"
        variants = (
            (
                "malformed lock",
                FakeHost(models_text="fictitious: malformed lock\n"),
                "models.lock",
                "Edit models.lock",
            ),
            (
                "profile missing",
                FakeHost(site_text=SITE_TEXT + "\nhardware_profile: fictitious-profile\n"),
                "not in models.lock",
                "Set hardware_profile",
            ),
        )
        for label, host, expected, expected_fix in variants:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                cases_path = Path(directory) / "cases.yaml"
                cases_path.write_text(cases_text, encoding="utf-8")
                output = StringIO()
                with redirect_stdout(output):
                    code = cli.main(
                        [
                            str(cases_path),
                            "--browser",
                            "--probe-inlet",
                            "--dry-run",
                            "--out",
                            str(Path(directory) / "out"),
                        ],
                        host=host,
                        checkout=ROOT,
                        site_path=SITE_PATH,
                    )
                self.assertEqual(code, 1)
                self.assertIn("preconditions: refuse", output.getvalue())
                self.assertIn(expected, output.getvalue())
                self.assertIn(expected_fix, output.getvalue())
                self.assertIn(str(ROOT / "models.lock"), host.read_paths)

    def test_probe_gate_load_failure_is_a_precondition_row_with_its_fix(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend)
        gate_path = ROOT / classify.BRANCH_GATE_FUNCTION
        load_function = classify._load_function

        def broken_gate(path: Path, name: str) -> object:
            if path == gate_path:
                raise ImportError("broken gate")
            return load_function(path, name)

        with patch(
            "gideon.evaluation.turns.classify._load_function", side_effect=broken_gate
        ):
            code, stdout, _stderr, _frontend, _host = self._run(
                page=page, args=["--probe-inlet"]
            )
        self.assertEqual(code, 1)
        self.assertIn(
            f"preconditions: refuse — inlet gate text could not be loaded from {gate_path}",
            stdout,
        )
        self.assertIn(f"Correct {gate_path}, then retry.", stdout)
        self.assertNotIn("broken gate", stdout)
        self.assertNotIn("signin:", stdout)

    def test_run_without_probe_never_loads_the_gate_texts(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())
        with patch(
            "tools.turns.cli.classify.load_gate_texts",
            side_effect=AssertionError("gate opened without probe"),
        ) as load_gate_texts:
            code, stdout, _stderr, _frontend, _host = self._run(page=page, args=[])
        self.assertEqual(code, 0)
        self.assertIn("summary: ok", stdout)
        load_gate_texts.assert_not_called()

    def test_run_entry_refuses_a_probe_without_a_gate_before_signin(self) -> None:
        spec = run.RunSpec(
            cases=ROOT / "tests/test_turns_browser.py",
            repeat=1,
            stream=False,
            out=None,
            force=False,
            dry_run=False,
            sentinel="fictitious-sentinel",
            probe_inlet=True,
        )

        def client_factory(**_kwargs: object) -> Client:
            raise AssertionError("signin reached without a gate")

        with self.assertRaisesRegex(ValueError, "inlet gates' texts"):
            run.run(
                spec,
                cases=(),
                password="fictitious-password",
                guardrail=object(),
                gate_texts=None,
                client_factory=client_factory,
                now=lambda: datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            )

    def test_trust_ca_row_runs_before_launch_and_failure_blocks_factory(self) -> None:
        host = self._host()
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n  - {id: answered, prompt: p, expect: answered}\n",
                encoding="utf-8",
            )
            factory_calls: list[str] = []

            def factory(
                hostname: str, _log: chromium.RequestLog
            ) -> tuple[browser.Page, Callable[[], None], str]:
                factory_calls.append(hostname)
                return page, lambda: None, "unused"

            output = StringIO()
            with (
                redirect_stdout(output),
                patch("tools.turns.cli.chromium.playwright_problem", return_value=None),
                patch("tools.turns.cli.chromium.browser_problem", return_value=None),
            ):
                code = cli.main(
                    [str(cases_path), "--browser", "--trust-ca", "--out", str(Path(directory) / "out")],
                    host=host,
                    client_factory=frontend.factory,
                    page_factory=factory,
                    checkout=ROOT,
                )
            self.assertEqual(code, 0)
            self.assertIn("trust-ca: ok", output.getvalue())
            self.assertEqual(len(factory_calls), 1)

            failed_host = self._host()
            failed_frontend = FakeFrontend()
            store = str(chromium.TRUST_STORE_PATH)
            failed_host.command_results[
                ("certutil", "-d", f"sql:{store}", "-N", "--empty-password")
            ] = completed([], 2)
            factory_calls.clear()
            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [str(cases_path), "--browser", "--trust-ca", "--out", str(Path(directory) / "failed")],
                    host=failed_host,
                    client_factory=failed_frontend.factory,
                    page_factory=factory,
                    checkout=ROOT,
                )
            self.assertEqual(code, 1)
            self.assertIn("trust-ca: refuse", output.getvalue())
            self.assertEqual(factory_calls, [])

    def test_requests_log_and_run_record_include_browser_evidence(self) -> None:
        frontend = FakeFrontend()
        page = FakePage(frontend, drains=self._drains())

        def factory(
            hostname: str, log: chromium.RequestLog
        ) -> tuple[browser.Page, Callable[[], None], str]:
            log.append("request", "GET", f"https://{hostname}/", "continue")
            log.append("request", "GET", "https://outside.example/", "abort")
            log.append("websocket", "GET", f"wss://{hostname}/ws", "continue")
            return page, lambda: setattr(page, "closed", True), "browser detail"

        code, stdout, _stderr, _frontend, host = self._run(
            page=page, args=[], page_factory=factory
        )
        self.assertEqual(code, 0)
        self.assertIn("requests: ok — 2 requests to gideon.example.org; 1 off-host aborted (outside.example)", stdout)
        request_log = next(value for key, value in host.files.items() if key.endswith("requests.log"))
        self.assertIn("continue", request_log)
        self.assertIn("abort", request_log)
        run_record = json.loads(next(value for key, value in host.files.items() if key.endswith("run.json")))
        browser_record = run_record["browser"]
        self.assertEqual(browser_record["requests"], {"on_host": 2, "off_host": 1, "off_hostnames": ["outside.example"]})
        self.assertEqual(browser_record["observation"]["selector_label"], "General")

    def test_dry_run_probe_prints_engine_calls_and_never_launches(self) -> None:
        host = self._host()
        expected_base_model = _base_model_id(host)
        with TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.yaml"
            cases_path.write_text(
                "cases:\n"
                "  - {id: answered, prompt: p, expect: answered}\n"
                "  - {id: answered-two, prompt: q, expect: answered}\n",
                encoding="utf-8",
            )
            calls: list[str] = []

            def factory(
                hostname: str, _log: chromium.RequestLog
            ) -> tuple[browser.Page, Callable[[], None], str]:
                calls.append(hostname)
                return FakePage(FakeFrontend()), lambda: None, "unused"

            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [str(cases_path), "--browser", "--probe-inlet", "--dry-run", "--out", str(Path(directory) / "out")],
                    host=host,
                    page_factory=factory,
                    checkout=ROOT,
                )
            self.assertEqual(code, 0)
            self.assertIn("probe: inlet gate", output.getvalue())
            self.assertIn(
                "engine calls: 6 (3 per browser turn)",
                output.getvalue(),
            )
            self.assertIn(f"probe: inlet gate ({expected_base_model})", output.getvalue())
            self.assertEqual(calls, [])

    def test_browser_turn_factor_and_probe_call_drive_the_window_guard(self) -> None:
        host = self._host()
        office = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        with TemporaryDirectory() as directory:
            def write_cases(path: Path, count: int) -> None:
                path.write_text(
                    "cases:\n"
                    + "\n".join(
                        f"  - {{id: case-{index}, prompt: p, expect: answered}}"
                        for index in range(count)
                    )
                    + "\n",
                    encoding="utf-8",
                )

            refused_cases = Path(directory) / "refused-cases.yaml"
            write_cases(refused_cases, 5)
            refused_frontend = FakeFrontend()
            refused_page = FakePage(refused_frontend)
            refused_factory_calls: list[str] = []

            def refused_factory(
                hostname: str, _log: chromium.RequestLog
            ) -> tuple[browser.Page, Callable[[], None], str]:
                refused_factory_calls.append(hostname)
                return refused_page, lambda: None, "unused"

            output = StringIO()
            with redirect_stdout(output):
                code = cli.main(
                    [
                        str(refused_cases),
                        "--browser",
                        "--out",
                        str(Path(directory) / "refused"),
                    ],
                    host=host,
                    client_factory=refused_frontend.factory,
                    page_factory=refused_factory,
                    now=lambda: office,
                    checkout=ROOT,
                )
            self.assertEqual(code, 1)
            refused_output = output.getvalue()
            self.assertIn("5 turns, 15 engine calls (3 per browser turn)", refused_output)
            self.assertIn("--force", refused_output)
            self.assertEqual(refused_factory_calls, [])
            self.assertEqual(refused_frontend.calls, [])

            def run_browser_cases(
                count: int,
                name: str,
                args: Sequence[str] = (),
            ) -> tuple[int, str, FakeFrontend, list[str]]:
                cases_path = Path(directory) / f"{name}-cases.yaml"
                write_cases(cases_path, count)
                frontend = FakeFrontend()
                run_host = self._host()
                frontend.base_model_id = _base_model_id(run_host)
                page = FakePage(frontend, drains=self._drains() * (count * 3))
                factory_calls: list[str] = []

                def factory(
                    hostname: str, _log: chromium.RequestLog
                ) -> tuple[browser.Page, Callable[[], None], str]:
                    factory_calls.append(hostname)
                    return page, lambda: None, "unused"

                output = StringIO()
                with redirect_stdout(output):
                    code = cli.main(
                        [
                            str(cases_path),
                            "--browser",
                            "--out",
                            str(Path(directory) / name),
                            *args,
                        ],
                        host=run_host,
                        client_factory=frontend.factory,
                        page_factory=factory,
                        now=lambda: office,
                        checkout=ROOT,
                    )
                return code, output.getvalue(), frontend, factory_calls

            code, admitted_output, frontend, factory_calls = run_browser_cases(2, "admitted")
            self.assertEqual(code, 0)
            self.assertIn("2 turns; 6 engine calls (3 per browser turn)", admitted_output)
            self.assertEqual(len(factory_calls), 1)
            self.assertTrue(frontend.calls)

            code, probed_output, _frontend, _factory_calls = run_browser_cases(
                2, "probed", ("--probe-inlet",)
            )
            self.assertEqual(code, 0)
            self.assertIn(
                "2 turns; 6 engine calls (3 per browser turn)",
                probed_output,
            )
            self.assertIn("summary: ok — 2 turns", probed_output)

            code, forced_output, _frontend, _factory_calls = run_browser_cases(
                5, "forced", ("--force",)
            )
            self.assertEqual(code, 0)
            self.assertIn("5 turns; 15 engine calls (3 per browser turn)", forced_output)
            self.assertIn("window overridden by --force", forced_output)

    def test_live_verdict_uses_the_first_painted_state_as_zero(self) -> None:
        pattern = "deadline/date-near-deadline-word@1"
        tripped = browser.LiveEntry(1000.0, "Thinking", True, 0, 28, False, False, pattern, False, False)
        replaced = browser.LiveEntry(1250.0, "Answer", False, 0, 90, False, False, None, True, True)
        verdict = classify.live_verdict((tripped, replaced))
        self.assertEqual(verdict.first_trip_index, 0)
        self.assertEqual(verdict.trips, (pattern, None))
        self.assertEqual(verdict.replaced_index, 1)
        self.assertEqual(verdict.first_trip_at, 0.0)
        self.assertEqual(verdict.replaced_at, 0.25)
        self.assertFalse(verdict.on_screen_at_end)
        self.assertEqual(
            classify.live_field(verdict), f"live: {pattern} at 0.0s, replaced at 0.2s"
        )

        gone = browser.LiveEntry(1400.0, "Answer", False, 0, 12, False, False, None, False, False)
        verdict = classify.live_verdict((tripped, gone))
        self.assertEqual(verdict.gone_index, 1)
        self.assertEqual(classify.live_field(verdict), f"live: {pattern} at 0.0s, gone at 0.4s")
        self.assertFalse(classify.live_fails(verdict, False))
        self.assertTrue(classify.live_fails(verdict, True))

        still = browser.LiveEntry(1400.0, "Answer", False, 0, 28, False, False, pattern, False, False)
        verdict = classify.live_verdict((tripped, still))
        self.assertTrue(verdict.on_screen_at_end)
        self.assertEqual(classify.live_field(verdict), f"live: {pattern} at 0.0s, on screen at end")
        self.assertTrue(classify.live_fails(verdict, False))

    def test_refusal_predicate_accepts_a_stream_suffix_and_rejects_a_prefix(self) -> None:
        is_refusal = classify.refusal_test(guardrail)
        for refusal in guardrail.REFUSALS:
            with self.subTest(refusal=refusal):
                self.assertTrue(is_refusal("safe doctrinal prefix\n\n" + refusal))
                self.assertFalse(is_refusal("safe doctrinal prefix"))

    def test_clean_stream_refusal_records_its_first_and_last_painted_times(self) -> None:
        refusal = guardrail.DEADLINE_REFUSAL
        watch = browser._Watch(
            classify.live_judge(guardrail, "Explain the general doctrine."),
            classify.refusal_test(guardrail),
        )
        watch.take_frames(
            (
                browser_frame(1000.0, "safe doctrinal prefix"),
                browser_frame(1250.0, "safe doctrinal prefix\n\n" + refusal),
                browser_frame(1500.0, "safe doctrinal prefix\n\n" + refusal),
            )
        )
        verdict = classify.live_verdict(watch.entries)
        self.assertIsNone(verdict.first_trip_index)
        self.assertEqual(verdict.refused_index, 1)
        self.assertEqual(verdict.refused_at, 0.25)
        self.assertEqual(verdict.ended_at, 0.5)
        self.assertEqual(
            classify.live_field(verdict),
            "live: clean, refused at 0.2s, ended at 0.5s",
        )

    def test_stream_prefix_refusal_keeps_trip_on_screen_at_end(self) -> None:
        refusal = guardrail.DEADLINE_REFUSAL
        answer = "the deadline is June 5, 2027"
        watch = browser._Watch(
            classify.live_judge(guardrail, "Explain the rule."),
            classify.refusal_test(guardrail),
        )
        watch.take_frames(
            (
                browser_frame(1000.0, answer),
                browser_frame(1250.0, answer + "\n\n" + refusal),
            )
        )
        verdict = classify.live_verdict(watch.entries)
        self.assertEqual(verdict.replaced_index, 1)
        self.assertTrue(verdict.on_screen_at_end)
        self.assertTrue(classify.live_fails(verdict, False))
        self.assertIn("replaced at 0.2s, on screen at end", classify.live_field(verdict))

    def test_flash_fails_under_the_shared_stream_policy(self) -> None:
        refusal = guardrail.DEADLINE_REFUSAL
        page = FakePage(
            FakeFrontend(),
            drains=(
                browser.PageDrain(
                    frames=(
                        browser_frame(1000.0, "the deadline is June 5, 2027"),
                    ),
                    regions={"block": "", "answer": "the deadline is June 5, 2027"},
                ),
                browser.PageDrain(
                    frames=(browser_frame(1500.0, refusal),),
                    regions={"block": "", "answer": refusal},
                ),
            ),
        )
        code, stdout, _stderr, _frontend, _host = self._run(page=page, args=[])
        self.assertEqual(code, 1)
        self.assertIn("answered: refuse", stdout)
        self.assertIn("replaced at 0.5s", stdout)

        page = FakePage(
            FakeFrontend(),
            drains=(
                browser.PageDrain(
                    frames=(browser_frame(1000.0, "the deadline is June 5, 2027"),),
                    regions={"block": "", "answer": "the deadline is June 5, 2027"},
                ),
                browser.PageDrain(
                    frames=(browser_frame(1500.0, refusal),),
                    regions={"block": "", "answer": refusal},
                ),
            ),
        )
        with patch("gideon.evaluation.turns.run.STREAM_LEAK_FAILS", False):
            code, stdout, _stderr, _frontend, _host = self._run(page=page, args=[])
        self.assertEqual(code, 0)
        self.assertIn("replaced at 0.5s", stdout)

    def test_live_judge_judges_the_answer_and_withholds_the_block(self) -> None:
        judge = classify.live_judge(
            guardrail, "I calculated the § 2255 deadline as June 5, 2027. Is that correct?"
        )
        watch = browser._Watch(judge, classify.refusal_test(guardrail))
        refusal = guardrail.DEADLINE_REFUSAL
        watch.take_frames(
            (
                browser_frame(1000.0, "the deadline is June 5, 2027"),
                browser_frame(1500.0, refusal),
            )
        )
        verdict = classify.live_verdict(watch.entries)
        self.assertEqual(verdict.trips[0], "deadline/date-near-deadline-word@1")
        self.assertTrue(classify.live_fails(verdict, run.STREAM_LEAK_FAILS))

        watch = browser._Watch(
            classify.live_judge(guardrail, "Explain the AEDPA deadline."),
            classify.refusal_test(guardrail),
        )
        watch.take_frames(
            (
                browser_frame(1000.0, "safe answer", block="the deadline is June 5, 2027"),
            )
        )
        verdict = classify.live_verdict(watch.entries)
        self.assertEqual(verdict.trips, (None,))
        self.assertEqual(verdict.reasoning_painted_at, 0.0)
        self.assertEqual(classify.live_field(verdict), "live: reasoning painted at 0.0s")
        self.assertTrue(classify.live_fails(verdict, run.STREAM_LEAK_FAILS))

    def test_live_judge_resets_since_for_a_non_extending_re_render(self) -> None:
        prompt = "Explain the AEDPA deadline under these dates."
        judge = classify.live_judge(guardrail, prompt)
        watch = browser._Watch(judge, classify.refusal_test(guardrail))
        watch.take_frames(
            (
                browser_frame(1000.0, "safe doctrinal text " * 200),
                browser_frame(1500.0, "the deadline is June 5, 2027"),
            )
        )
        self.assertIsNone(watch.entries[0].tripped)
        self.assertEqual(watch.entries[1].tripped, "deadline/date-near-deadline-word@1")

    def test_live_on_screen_and_no_states_fail(self) -> None:
        page = FakePage(
            FakeFrontend(),
            drains=(
                browser.PageDrain(
                    frames=(browser_frame(1000.0, "the deadline is June 5, 2027"),),
                    regions={"block": "", "answer": "the deadline is June 5, 2027"},
                ),
            ),
        )
        code, stdout, _stderr, _frontend, _host = self._run(page=page, args=[])
        self.assertEqual(code, 1)
        self.assertIn("on screen at end", stdout)

        page = FakePage(
            FakeFrontend(),
            drains=(browser.PageDrain(frames=(), regions={"block": "", "answer": ""}),),
        )
        code, stdout, _stderr, _frontend, _host = self._run(page=page, args=[])
        self.assertEqual(code, 1)
        self.assertIn("live: no states", stdout)

    def test_reasoning_block_is_ignored_and_recorded_for_withholding(self) -> None:
        page = FakePage(
            FakeFrontend(),
            drains=(
                browser.PageDrain(
                    frames=(
                        {
                            "instant": 1000.0,
                            "summary": "Thinking",
                            "expanded": True,
                            "block": {
                                "text": "the deadline is June 5, 2027",
                                "extended": False,
                            },
                            "answer": {"text": "safe", "extended": False},
                        },
                        {
                            "instant": 1100.0,
                            "summary": "Thinking",
                            "expanded": True,
                            "block": {
                                "text": "the deadline is June 5, 2027",
                                "extended": False,
                            },
                            "answer": {"text": "safe", "extended": False},
                        },
                    ),
                    regions={"block": "the deadline is June 5, 2027", "answer": "safe"},
                ),
            ),
        )
        code, stdout, _stderr, _frontend, host = self._run(page=page, args=[])
        self.assertEqual(code, 1)
        self.assertIn("live: reasoning painted at 0.0s", stdout)
        record = json.loads(next(value for key, value in host.files.items() if key.endswith("answered.json")))
        self.assertEqual(record["browser"]["states"][0]["tripped"], None)
        self.assertTrue(record["browser"]["states"][0]["block_text"])
        self.assertNotIn("June 5, 2027", json.dumps(record["browser"]["states"]))
        self.assertEqual(record["browser"]["texts"]["0"]["block"], "the deadline is June 5, 2027")
        self.assertEqual(record["browser"]["reasoning_painted_at"], 0.0)
        self.assertFalse(record["browser"]["states"][0]["answer_extended"])


def browser_frame(instant: float, answer: str, *, block: str = "") -> dict[str, object]:
    """Make a frame-shaped mapping without duplicating the observer parser."""

    return {
        "instant": instant,
        "summary": "Answer",
        "expanded": False,
        "block": {"text": block, "extended": False},
        "answer": {"text": answer, "extended": False},
    }

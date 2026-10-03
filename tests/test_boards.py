"""Browser board check rows over a fake page and host."""

from __future__ import annotations

import importlib.metadata
import io
import json
import os
import subprocess
import unittest
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from gideon.evaluation.turns import chromium
from gideon.evaluation.turns.browser import PageError
from gideon.host import grafana, site, tls
from gideon.host.render import ARTIFACTS
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import Problem
from gideon.host.secrets import secret_path
from gideon.host.sysio import PathLike
from tools.boards import cli, inventory, page

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = Path("/example/site.yaml")
RENDERED_ROOT = Path("/example/rendered")
OUTPUT = Path("/example/output")
PASSWORD = "fictitious-board-password"
SITE_TEXT = (ROOT / "config/site.example.yaml").read_text()
BOARD_PATH = next(
    RENDERED_ROOT / artifact.relative_path
    for artifact in ARTIFACTS
    if PurePosixPath(artifact.relative_path).parent
    == PurePosixPath("grafana/dashboards")
)
DASHBOARD_PATHS = tuple(
    RENDERED_ROOT / artifact.relative_path
    for artifact in ARTIFACTS
    if PurePosixPath(artifact.relative_path).parent
    == PurePosixPath("grafana/dashboards")
)


def board_document(
    uid: str,
    panels: list[tuple[str, str]],
    *,
    rows: Sequence[tuple[str, Sequence[tuple[str, str]]]] = (),
) -> str:
    return json.dumps(
        {
            "uid": uid,
            "title": f"Board {uid}",
            "panels": [
                {
                    "title": title,
                    "type": kind,
                    "gridPos": {"x": 0, "y": index * 8, "w": 12, "h": 8},
                }
                for index, (title, kind) in enumerate(panels)
            ]
            + [
                {
                    "title": row_title,
                    "type": "row",
                    "collapsed": True,
                    "gridPos": {
                        "x": 0,
                        "y": (len(panels) + index) * 9,
                        "w": 24,
                        "h": 1,
                    },
                    "panels": [
                        {
                            "title": title,
                            "type": kind,
                            "gridPos": {
                                "x": 0,
                                "y": (len(panels) + index) * 9 + 1,
                                "w": 12,
                                "h": 8,
                            },
                        }
                        for title, kind in nested
                    ],
                }
                for index, (row_title, nested) in enumerate(rows)
            ],
        }
    )


class FakeHost:
    """Only the file and process seam the board command needs."""

    def __init__(self, document: str, *, password: str = PASSWORD) -> None:
        self.euid = 0
        self.files: dict[str, str] = {
            str(SITE_PATH): SITE_TEXT,
            str(secret_path("grafana_admin_password")): password + "\n",
            str(BOARD_PATH): document,
        }
        self.directories: dict[str, list[str]] = {
            str(chromium.BROWSERS_DIR): ["chromium_headless_shell-example"],
            str(chromium.BROWSERS_DIR / "chromium_headless_shell-example"): [],
        }
        self.commands: list[tuple[str, ...]] = []
        self.chowns: list[str] = []
        self.find_result: subprocess.CompletedProcess[str] | None = None

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
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
        key = os.fspath(path)
        self.files[key] = text
        parent = str(Path(key).parent)
        self.directories.setdefault(parent, [])
        if Path(key).name not in self.directories[parent]:
            self.directories[parent].append(Path(key).name)

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        if key not in self.directories:
            raise NotADirectoryError(key)
        return list(self.directories[key])

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

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout, passthrough
        self.commands.append(tuple(argv))
        if argv and argv[0] == "find" and self.find_result is not None:
            return self.find_result
        return subprocess.CompletedProcess(argv, 0, "", "")

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del uid, gid
        self.chowns.append(os.fspath(path))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        raise FileNotFoundError(os.fspath(path))


class FakePage:
    """A selector keyed stand-in for a signed-in board page."""

    def __init__(
        self,
        host: FakeHost,
        panels: list[tuple[str, str, str]],
        *,
        password: str = PASSWORD,
        refuse_fill: bool = False,
        login_mode: str = "success",
        boards: Mapping[str, list[tuple[str, str, str]]] | None = None,
        fail_navigation: set[str] | None = None,
        sections: Mapping[str, str] | None = None,
        rows: Mapping[str, list[tuple[str, str, str]]] | None = None,
        open_rows: set[str] | None = None,
        unopenable_rows: set[str] | None = None,
    ) -> None:
        self.host = host
        self.password = password
        self.refuse_fill = refuse_fill
        self.login_mode = login_mode
        self.boards = boards or {}
        self.fail_navigation = fail_navigation or set()
        self.sections = sections or {}
        self.rows = rows or {}
        self.open_rows = open_rows or set()
        self.unopenable_rows = unopenable_rows or set()
        self.default_panels = panels
        self.current_url = ""
        self.tooltip = ""
        self.hovered = False
        self.login_error = False
        self.events: list[str] = []
        self.fill_selectors: list[str] = []
        self.click_selectors: list[str] = []
        self.count_selectors: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.sleep_count = 0
        self.closed = False
        self.values: dict[str, str] = {}
        self.present: set[str] = set()
        self.row_buttons: dict[str, str] = {}

        self._install_panels(panels)

    def _install_panels(self, panels: list[tuple[str, str, str]]) -> None:
        self.values.clear()
        self.present.clear()
        self.hovered = False
        self._add_panels(panels)

    def _add_panels(self, panels: list[tuple[str, str, str]]) -> None:
        for title, kind, content in panels:
            section = self.sections.get(
                title, f'section[data-testid="{page.PANEL_SECTION_PREFIX}{title}"]'
            )
            self.present.add(section)
            self.values[f"{section} {page.PANEL_CONTENT_SELECTOR}"] = content
            if kind == "error":
                self.present.add(f"{section} {page.ERROR_SELECTOR}")
                self.values[f"{section} {page.DATA_ERROR_SELECTOR}"] = page.NO_DATA_TEXT
            elif kind == "empty":
                self.values[f"{section} {page.DATA_ERROR_SELECTOR}"] = page.NO_DATA_TEXT
            elif kind == "data_error":
                self.values[f"{section} {page.DATA_ERROR_SELECTOR}"] = content
            elif kind == "loading":
                self.present.add(f"{section} {page.LOADING_SELECTOR}")
            elif kind == "rendered_blank":
                self.present.add(f"{section} {page.PANEL_CONTENT_SELECTOR} *")

    def goto(self, path: str) -> None:
        self.events.append("goto")
        if path == page.LOGIN_PATH and self.login_mode == "certificate":
            raise PageError(f"certificate error with {self.password}", certificate=True)
        if "/d/" in urlsplit(path).path:
            uid = unquote(urlsplit(path).path.split("/d/", 1)[1])
            if uid in self.fail_navigation:
                raise PageError(f"navigation failed with {self.password}")
            self._install_panels(self.boards.get(uid, self.default_panels))
            self.row_buttons = {
                f'button[data-testid="{page.ROW_BUTTON_PREFIX}{title}"]': (
                    "Collapse row"
                    if title in self.open_rows
                    else page.ROW_EXPAND_LABEL_PREFIX
                )
                for title in self.rows
            }
            for title in self.open_rows:
                self._add_panels(self.rows[title])
        self.current_url = path

    def fill(self, selector: str, text: str) -> None:
        del text
        self.fill_selectors.append(selector)
        if self.refuse_fill:
            raise PageError(f"page fill failed with {self.password}")

    def click(self, selector: str) -> None:
        self.click_selectors.append(selector)
        row_selector = selector.removesuffix(
            f'[aria-label^="{page.ROW_EXPAND_LABEL_PREFIX}"]'
        )
        if row_selector in self.row_buttons and row_selector != selector:
            self.events.append("row click")
            self.row_buttons[row_selector] = "Collapse row"
            title = row_selector.removeprefix(
                f'button[data-testid="{page.ROW_BUTTON_PREFIX}'
            ).removesuffix('"]')
            if title not in self.unopenable_rows:
                self._add_panels(self.rows[title])
            return
        if self.login_mode == "failed":
            self.login_error = True
            return
        self.current_url = "/grafana/"

    def count(self, selector: str) -> int:
        self.count_selectors.append(selector)
        if selector in {
            page.USERNAME_SELECTOR,
            page.PASSWORD_SELECTOR,
            page.LOGIN_SELECTOR,
        }:
            return 1
        if selector == page.LOGIN_ERROR_SELECTOR:
            return int(self.login_error)
        if selector == page.TOOLTIP_SELECTOR:
            return int(self.hovered)
        if selector in self.row_buttons:
            return 1
        for label in (page.ROW_EXPAND_LABEL_PREFIX, "Collapse row"):
            row_selector = selector.removesuffix(f'[aria-label^="{label}"]')
            if row_selector != selector and row_selector in self.row_buttons:
                return int(self.row_buttons[row_selector].startswith(label))
        return int(selector in self.present or selector in self.values)

    def text(self, selector: str) -> str | None:
        if selector == page.TOOLTIP_SELECTOR and self.hovered:
            return self.tooltip
        return self.values.get(selector)

    def url(self) -> str:
        return self.current_url

    def hover(self, selector: str) -> None:
        assert selector in self.present
        self.events.append("hover")
        self.hovered = True
        self.tooltip = "Fictitious query refused"

    def resize(self, width: int, height: int) -> None:
        assert width > 0 and height > 0
        self.events.append("resize")
        self.resizes.append((width, height))

    def screenshot(self, path: Path) -> None:
        self.events.append("screenshot")
        self.host.write_text(path, "fictitious image")

    def wait_until(self, predicate: object, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        assert callable(predicate)
        return bool(predicate())

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.sleep_count += 1


class FakeClient:
    """A Grafana API response or safe failure, with its requests recorded."""

    def __init__(
        self,
        response: grafana.Response,
        *,
        failure: grafana.GrafanaError | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.requests: list[tuple[str, str]] = []

    def request(self, method: str, path: str) -> grafana.Response:
        self.requests.append((method, path))
        if self.failure is not None:
            raise self.failure
        return self.response


class BoardRows(unittest.TestCase):
    def invoke(
        self,
        host: FakeHost,
        browser_page: FakePage,
        *,
        argv: Sequence[str] = ("--out", str(OUTPUT)),
        client_factory: Callable[..., inventory.BoardClient] | None = None,
        playwright_problem: Problem | None = None,
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(
                chromium, "playwright_problem", return_value=playwright_problem
            ),
            patch("tools.boards.cli.sudo_ids", return_value=(1000, 1000)),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = cli.main(
                argv,
                host=host,
                page_factory=lambda hostname, log: (
                    browser_page,
                    lambda: setattr(browser_page, "closed", True),
                ),
                client_factory=client_factory,
                site_path=SITE_PATH,
                rendered_root=RENDERED_ROOT,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def run_board(
        self,
        expected: list[tuple[str, str]],
        visible: list[tuple[str, str, str]],
        *,
        refuse_fill: bool = False,
    ) -> tuple[int, str, str, FakeHost, FakePage]:
        document = board_document("example-board", expected)
        host = FakeHost(document)
        browser_page = FakePage(host, visible, refuse_fill=refuse_fill)
        code, stdout, stderr = self.invoke(host, browser_page)
        return code, stdout, stderr, host, browser_page

    def run_named_dashboard(
        self, dashboard: object, *, uid: str = "outside-board"
    ) -> tuple[int, str]:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(grafana.Response(200, {"dashboard": dashboard}))
        code, stdout, _ = self.invoke(
            host,
            FakePage(host, [("Figure", "drawn", "12 points")]),
            argv=[uid, "--out", str(OUTPUT)],
            client_factory=lambda *, credential=None: client,
        )
        return code, stdout

    def test_drawn_and_empty_panels_pass_with_their_rows(self) -> None:
        expected = [("Figure", "timeseries"), ("No results", "timeseries")]
        code, stdout, stderr, host, browser_page = self.run_board(
            expected,
            [
                ("Figure", "drawn", "12 points"),
                ("No results", "empty", page.NO_DATA_TEXT),
            ],
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"panel: ok — example-board: {expected[0][0]}: drawn", stdout)
        self.assertIn(f"panel: ok — example-board: {expected[1][0]}: empty", stdout)
        self.assertIn("board: ok", stdout)
        self.assertIn("summary: ok", stdout)
        self.assertIn("record: ok", stdout)
        self.assertTrue(browser_page.closed)
        self.assertEqual(browser_page.events[-1], "screenshot")
        self.assertIn(str(OUTPUT / "requests.log"), host.files)
        self.assertNotIn(
            PASSWORD, stdout + stderr + host.files[str(OUTPUT / "requests.log")]
        )
        self.assertTrue(host.commands)
        self.assertTrue(
            all(PASSWORD not in " ".join(command) for command in host.commands)
        )

    def test_errored_panel_fails_and_carries_the_tooltip(self) -> None:
        expected = [("Faulty figure", "timeseries")]
        code, stdout, stderr, host, browser_page = self.run_board(
            expected, [("Faulty figure", "error", page.NO_DATA_TEXT)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn(
            "panel: refuse — example-board: Faulty figure: errored; Fictitious query refused",
            stdout,
        )
        self.assertIn("board: refuse", stdout)
        self.assertIn("summary: refuse", stdout)
        self.assertLess(
            browser_page.events.index("screenshot"), browser_page.events.index("hover")
        )
        self.assertIn(str(OUTPUT / "example-board.png"), host.files)

    def test_page_error_cannot_print_or_record_the_password(self) -> None:
        code, stdout, stderr, host, browser_page = self.run_board(
            [("Figure", "timeseries")], [], refuse_fill=True
        )
        self.assertEqual(code, 1)
        self.assertIn("signin: refuse", stdout)
        self.assertNotIn("panel:", stdout)
        self.assertTrue(browser_page.closed)
        for value in (
            stdout,
            stderr,
            host.files[str(OUTPUT / "requests.log")],
            *(" ".join(cmd) for cmd in host.commands),
        ):
            self.assertNotIn(PASSWORD, value)

    def test_data_error_no_data_is_empty(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Empty figure", "timeseries")],
            [("Empty figure", "empty", page.NO_DATA_TEXT)],
        )
        self.assertEqual(code, 0)
        self.assertIn("Empty figure: empty; No data", stdout)

    def test_table_no_rows_is_empty(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Empty table", "table")],
            [("Empty table", "drawn", page.TABLE_EMPTY_TEXT)],
        )
        self.assertEqual(code, 0)
        self.assertIn("Empty table: empty; No rows", stdout)

    def test_stat_whole_no_data_is_empty(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Empty stat", "stat")],
            [("Empty stat", "drawn", page.STAT_EMPTY_TEXT)],
        )
        self.assertEqual(code, 0)
        self.assertIn("Empty stat: empty; No data", stdout)

    def test_bar_gauge_whole_no_data_is_empty(self) -> None:
        """A bar gauge without a series has Grafana's no-data text."""

        code, stdout, _, _, _ = self.run_board(
            [("Empty bar", "bargauge")],
            [("Empty bar", "drawn", page.STAT_EMPTY_TEXT)],
        )
        self.assertEqual(code, 0)
        self.assertIn("panel: ok — example-board: Empty bar: empty; No data", stdout)

    def test_alert_list_empty_text_is_empty(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Empty alerts", "alertlist")],
            [("Empty alerts", "drawn", page.ALERT_EMPTY_TEXT)],
        )
        self.assertEqual(code, 0)
        self.assertIn("Empty alerts: empty; No alerts matching filters", stdout)

    def test_stat_with_no_data_among_other_text_is_drawn(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Mixed stat", "stat")],
            [("Mixed stat", "drawn", "No data appears in a label, 42 is the value")],
        )
        self.assertEqual(code, 0)
        self.assertIn("Mixed stat: drawn", stdout)

    def test_panel_loading_at_bound_fails(self) -> None:
        code, stdout, _, _, browser_page = self.run_board(
            [("Slow figure", "timeseries")],
            [("Slow figure", "loading", "Loading")],
        )
        self.assertEqual(code, 1)
        self.assertIn("Slow figure: loading", stdout)
        self.assertIn("summary: refuse", stdout)
        self.assertEqual(
            browser_page.sleep_count,
            page.SETTLE_SECONDS // page.SETTLE_INTERVAL_SECONDS,
        )

    def test_panel_missing_from_page_fails(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Present", "timeseries"), ("Absent", "table")],
            [("Present", "drawn", "12 points")],
        )
        self.assertEqual(code, 1)
        self.assertIn("Absent: missing", stdout)
        self.assertIn("board: refuse", stdout)

    def test_error_marker_wins_over_no_data_body(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Broken", "timeseries")],
            [("Broken", "error", page.NO_DATA_TEXT)],
        )
        self.assertEqual(code, 1)
        self.assertIn("Broken: errored", stdout)
        self.assertNotIn("Broken: empty", stdout)

    def test_data_error_other_than_no_data_is_errored(self) -> None:
        detail = "Data is missing a time field"
        code, stdout, _, _, browser_page = self.run_board(
            [("Wrong shape", "timeseries")],
            [("Wrong shape", "data_error", detail)],
        )
        self.assertEqual(code, 1)
        self.assertIn(f"Wrong shape: errored; {detail}", stdout)
        self.assertNotIn("hover", browser_page.events)

    def test_mounted_panel_without_content_is_loading(self) -> None:
        code, stdout, _, _, _ = self.run_board(
            [("Not started", "timeseries")],
            [("Not started", "drawn", "")],
        )
        self.assertEqual(code, 1)
        self.assertIn("Not started: loading", stdout)

    def run_rendered_blank(self, kind: str) -> tuple[int, str]:
        host = FakeHost(board_document("example-board", [("Blank", kind)]))
        browser_page = FakePage(host, [("Blank", "rendered_blank", "")])
        code, stdout, _ = self.invoke(host, browser_page)
        return code, stdout

    def test_rendered_blank_stat_is_drawn_for_the_screenshot(self) -> None:
        code, stdout = self.run_rendered_blank("stat")
        self.assertEqual(code, 0)
        self.assertIn("Blank: drawn; blank value", stdout)

    def test_rendered_blank_bar_gauge_is_drawn_for_the_screenshot(self) -> None:
        """A mounted bar gauge with a blank value is visible."""

        code, stdout = self.run_rendered_blank("bargauge")
        self.assertEqual(code, 0)
        self.assertIn("panel: ok — example-board: Blank: drawn; blank value", stdout)

    def test_rendered_blank_timeseries_is_still_loading(self) -> None:
        code, stdout = self.run_rendered_blank("timeseries")
        self.assertEqual(code, 1)
        self.assertIn("Blank: loading", stdout)

    def test_collapsed_row_opens_once_before_screenshot(self) -> None:
        """The screenshot and panel rows account for an opened row."""

        host = FakeHost(
            board_document(
                "example-board",
                [("Visible", "stat")],
                rows=[
                    (
                        "Host detail",
                        [("Host load", "timeseries"), ("Host memory", "stat")],
                    )
                ],
            )
        )
        browser_page = FakePage(
            host,
            [("Visible", "drawn", "up")],
            rows={
                "Host detail": [
                    ("Host load", "drawn", "12 points"),
                    ("Host memory", "drawn", "42 percent"),
                ]
            },
        )
        code, stdout, stderr = self.invoke(host, browser_page)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("panel: ok — example-board: Host load: drawn", stdout)
        self.assertIn("panel: ok — example-board: Host memory: drawn", stdout)
        self.assertEqual(browser_page.events.count("row click"), 1)
        self.assertLess(
            browser_page.events.index("row click"),
            browser_page.events.index("screenshot"),
        )
        button = f'button[data-testid="{page.ROW_BUTTON_PREFIX}Host detail"]'
        self.assertEqual(
            browser_page.click_selectors.count(
                f'{button}[aria-label^="{page.ROW_EXPAND_LABEL_PREFIX}"]'
            ),
            1,
        )
        self.assertEqual(browser_page.count(f'{button}[aria-label^="Collapse row"]'), 1)

    def test_already_open_row_is_not_clicked(self) -> None:
        """An open row stays open for its panel reading."""

        host = FakeHost(
            board_document(
                "example-board",
                [],
                rows=[("Host detail", [("Host load", "timeseries")])],
            )
        )
        browser_page = FakePage(
            host,
            [],
            rows={"Host detail": [("Host load", "drawn", "12 points")]},
            open_rows={"Host detail"},
        )
        code, stdout, stderr = self.invoke(host, browser_page)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("panel: ok — example-board: Host load: drawn", stdout)
        self.assertEqual(browser_page.events.count("row click"), 0)
        self.assertEqual(browser_page.click_selectors, [page.LOGIN_SELECTOR])

    def test_row_that_never_opens_reports_its_panels_missing(self) -> None:
        """A failed row opening names the row on each missing panel."""

        host = FakeHost(
            board_document(
                "example-board",
                [("Visible", "stat")],
                rows=[
                    (
                        "Host detail",
                        [("Host load", "timeseries"), ("Host memory", "stat")],
                    )
                ],
            )
        )
        browser_page = FakePage(
            host,
            [("Visible", "drawn", "up")],
            rows={
                "Host detail": [
                    ("Host load", "drawn", "12 points"),
                    ("Host memory", "drawn", "42 percent"),
                ]
            },
            unopenable_rows={"Host detail"},
        )
        code, stdout, stderr = self.invoke(host, browser_page)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("panel: ok — example-board: Visible: drawn", stdout)
        for title in ("Host load", "Host memory"):
            self.assertIn(
                f"panel: refuse — example-board: {title}: missing; row Host detail did not open",
                stdout,
            )
        self.assertIn("board: refuse", stdout)
        self.assertIn("summary: refuse", stdout)

    def test_screenshot_precedes_tooltip_hover(self) -> None:
        _, _, _, _, browser_page = self.run_board(
            [("Broken", "timeseries")],
            [("Broken", "error", page.NO_DATA_TEXT)],
        )
        self.assertLess(
            browser_page.events.index("screenshot"), browser_page.events.index("hover")
        )

    def test_sign_in_attempts_form_once_after_failure(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        browser_page = FakePage(host, [], login_mode="failed")
        code, stdout, _ = self.invoke(host, browser_page)
        self.assertEqual(code, 1)
        self.assertIn("signin: refuse", stdout)
        self.assertEqual(browser_page.fill_selectors.count(page.USERNAME_SELECTOR), 1)
        self.assertEqual(browser_page.fill_selectors.count(page.PASSWORD_SELECTOR), 1)
        self.assertEqual(browser_page.click_selectors, [page.LOGIN_SELECTOR])

    def test_failed_sign_in_alert_has_a_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        browser_page = FakePage(host, [], login_mode="failed")
        code, stdout, stderr = self.invoke(host, browser_page)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn(
            "signin: refuse — Grafana refused the break-glass sign-in", stdout
        )
        self.assertIn("Fix: ", stdout)
        self.assertIn("secrets rotate grafana_admin_password", stdout)
        self.assertNotIn(PASSWORD, stdout)

    def test_certificate_page_error_has_trust_ca_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        browser_page = FakePage(host, [], login_mode="certificate")
        code, stdout, stderr = self.invoke(host, browser_page)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("signin: refuse — the browser does not trust", stdout)
        self.assertIn("Fix: Run with --trust-ca", stdout)
        self.assertNotIn(PASSWORD, stdout)
        self.assertEqual(browser_page.fill_selectors, [])

    def test_no_out_refuses_on_stderr_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        code, stdout, stderr = self.invoke(host, FakePage(host, []), argv=[])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--out is required", stderr)
        self.assertIn("Fix: ", stderr)

    def test_nonempty_out_refuses_on_stderr_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        host.directories[str(OUTPUT)] = ["prior-file"]
        code, stdout, stderr = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("exists and is not empty", stderr)
        self.assertIn("Fix: ", stderr)

    def test_nonroot_precondition_refuses_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        host.euid = 1000
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse — root is required", stdout)
        self.assertIn("Fix: Run sudo", stdout)

    def test_bad_site_precondition_refuses_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        host.files[str(SITE_PATH)] = "{}"
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse", stdout)
        self.assertIn("Fix: Correct /etc/gideon/site.yaml", stdout)

    def test_missing_playwright_precondition_refuses_with_fix(self) -> None:
        def unavailable(_: str) -> str:
            raise importlib.metadata.PackageNotFoundError("playwright")

        problem = chromium.playwright_problem(unavailable)
        self.assertIsNotNone(problem)
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        code, stdout, _ = self.invoke(
            host, FakePage(host, []), playwright_problem=problem
        )
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse — Playwright is not installed", stdout)
        self.assertIn("Fix: ", stdout)

    def test_wrong_playwright_version_precondition_refuses_with_fix(self) -> None:
        problem = chromium.playwright_problem(lambda _: "1000.0.0")
        self.assertIsNotNone(problem)
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        code, stdout, _ = self.invoke(
            host, FakePage(host, []), playwright_problem=problem
        )
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse — Playwright 1000.0.0", stdout)
        self.assertIn("Fix: ", stdout)

    def test_missing_headless_shell_precondition_refuses_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        host.directories.pop(str(chromium.BROWSERS_DIR))
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn(
            "preconditions: refuse — Playwright Chromium headless shell", stdout
        )
        self.assertIn("Fix: ", stdout)

    def test_unreadable_secret_precondition_refuses_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        host.files.pop(str(secret_path("grafana_admin_password")))
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse — Secret file is missing", stdout)
        self.assertIn("Fix: ", stdout)

    def test_no_provisioned_board_without_uid_refuses_with_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        host.files.pop(str(BOARD_PATH))
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("preconditions: refuse — no provisioned boards", stdout)
        self.assertIn("Fix: Run sudo python3 -m gideon apply", stdout)

    def test_navigation_failure_continues_to_next_board(self) -> None:
        first_uid, second_uid = "first-board", "second-board"
        host = FakeHost(board_document(first_uid, [("First", "timeseries")]))
        host.files[str(DASHBOARD_PATHS[1])] = board_document(
            second_uid, [("Second", "timeseries")]
        )
        browser_page = FakePage(
            host,
            [],
            boards={second_uid: [("Second", "drawn", "12 points")]},
            fail_navigation={first_uid},
        )
        code, stdout, stderr = self.invoke(host, browser_page)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn(
            f"board: refuse — {first_uid}: Grafana board could not be read", stdout
        )
        self.assertIn(f"panel: ok — {second_uid}: Second: drawn", stdout)
        self.assertIn(f"board: ok — {second_uid}", stdout)
        self.assertLess(
            stdout.index(f"board: refuse — {first_uid}"),
            stdout.index(f"board: ok — {second_uid}"),
        )
        self.assertNotIn(PASSWORD, stdout)

    def test_registry_inventory_uses_only_present_dashboard_artifacts(self) -> None:
        host = FakeHost(board_document("seed", [("Figure", "timeseries")]))
        expected_uids: set[str] = set()
        for index, path in enumerate(DASHBOARD_PATHS):
            uid = f"registry-{index}"
            host.files[str(path)] = board_document(uid, [("Figure", "timeseries")])
            expected_uids.add(uid)
        host.files[str(RENDERED_ROOT / "grafana/dashboards/foreign.json")] = (
            board_document("foreign", [("Figure", "timeseries")])
        )
        found = inventory.provisioned_boards(host, rendered_root=RENDERED_ROOT)
        self.assertEqual(
            {board.uid for board in found if isinstance(board, inventory.Board)},
            expected_uids,
        )
        self.assertEqual(len(found), len(DASHBOARD_PATHS))

    def test_no_gpu_rendered_tree_has_fewer_boards(self) -> None:
        host = FakeHost(board_document("seed", [("Figure", "timeseries")]))
        for index, path in enumerate(DASHBOARD_PATHS):
            host.files[str(path)] = board_document(
                f"registry-{index}", [("Figure", "timeseries")]
            )
        complete = inventory.provisioned_boards(host, rendered_root=RENDERED_ROOT)
        gpu_path = next(path for path in DASHBOARD_PATHS if path.name == "gpu.json")
        host.files.pop(str(gpu_path))
        without_gpu = inventory.provisioned_boards(host, rendered_root=RENDERED_ROOT)
        self.assertEqual(len(without_gpu), len(complete) - 1)

    def test_named_uid_uses_quoted_grafana_read_and_credential(self) -> None:
        uid = "outside/board with space"
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        browser_page = FakePage(host, [("Figure", "drawn", "12 points")])
        client = FakeClient(
            grafana.Response(
                200,
                {
                    "dashboard": json.loads(
                        board_document(uid, [("Figure", "timeseries")])
                    )
                },
            )
        )
        credentials: list[tuple[str, str] | None] = []

        def make_client(*, credential: tuple[str, str] | None = None) -> FakeClient:
            credentials.append(credential)
            return client

        code, stdout, stderr = self.invoke(
            host,
            browser_page,
            argv=[uid, "--out", str(OUTPUT)],
            client_factory=make_client,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(credentials, [(GRAFANA_ADMIN_USER, PASSWORD)])
        self.assertEqual(
            client.requests,
            [("GET", "/api/dashboards/uid/outside%2Fboard%20with%20space")],
        )
        self.assertIn(f"board: ok — {uid}", stdout)
        self.assertNotIn(PASSWORD, stdout + host.files[str(OUTPUT / "requests.log")])
        self.assertTrue(all(PASSWORD not in " ".join(cmd) for cmd in host.commands))

    def test_default_named_client_uses_ingress_hostname_and_office_ca(self) -> None:
        uid = "outside-board"
        loaded = site.load_site_text(SITE_TEXT)
        self.assertIsNotNone(loaded.config)
        assert loaded.config is not None
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(
            grafana.Response(
                200,
                {
                    "dashboard": json.loads(
                        board_document(uid, [("Figure", "timeseries")])
                    )
                },
            )
        )
        ingress_calls: list[tuple[str, object]] = []
        credentials: list[tuple[str, str] | None] = []

        def make_client(*, credential: tuple[str, str] | None = None) -> FakeClient:
            credentials.append(credential)
            return client

        def ingress(hostname: str, *, ca_path: object) -> Callable[..., FakeClient]:
            ingress_calls.append((hostname, ca_path))
            return make_client

        with patch.object(grafana, "ingress_client_factory", side_effect=ingress):
            code, _, _ = self.invoke(
                host,
                FakePage(host, [("Figure", "drawn", "12 points")]),
                argv=[uid, "--out", str(OUTPUT)],
            )
        self.assertEqual(code, 0)
        self.assertEqual(ingress_calls, [(loaded.config.hostname, tls.CA_PATH)])
        self.assertEqual(credentials, [(GRAFANA_ADMIN_USER, PASSWORD)])

    def test_provisioned_uid_never_reaches_client_factory(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        browser_page = FakePage(host, [("Figure", "drawn", "12 points")])

        def forbidden_factory(
            *, credential: tuple[str, str] | None = None
        ) -> FakeClient:
            raise AssertionError("provisioned board requested a client")

        code, stdout, _ = self.invoke(
            host,
            browser_page,
            argv=["example-board", "--out", str(OUTPUT)],
            client_factory=forbidden_factory,
        )
        self.assertEqual(code, 0)
        self.assertIn("board: ok — example-board", stdout)

    def test_named_uid_404_row_names_provisioned_uids(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(grafana.Response(404))
        code, stdout, _ = self.invoke(
            host,
            FakePage(host, []),
            argv=["unknown-board", "--out", str(OUTPUT)],
            client_factory=lambda *, credential=None: client,
        )
        self.assertEqual(code, 1)
        self.assertIn("board: refuse — unknown-board: no such board", stdout)
        self.assertIn("example-board", stdout)
        self.assertIn("Fix: ", stdout)

    def test_named_uid_grafana_error_keeps_its_problem_and_fix(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(
            grafana.Response(200),
            failure=grafana.GrafanaError(
                "Fictitious Grafana refusal", "Repair fictitious service."
            ),
        )
        code, stdout, _ = self.invoke(
            host,
            FakePage(host, []),
            argv=["outside", "--out", str(OUTPUT)],
            client_factory=lambda *, credential=None: client,
        )
        self.assertEqual(code, 1)
        self.assertIn("board: refuse — outside: Fictitious Grafana refusal", stdout)
        self.assertIn("Fix: Repair fictitious service.", stdout)

    def test_named_uid_invalid_json_client_error_is_board_refusal(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(
            grafana.Response(200),
            failure=grafana.GrafanaError(
                "Grafana returned invalid JSON for the dashboard.",
                "Correct the stored dashboard, then retry.",
            ),
        )
        code, stdout, _ = self.invoke(
            host,
            FakePage(host, []),
            argv=["outside", "--out", str(OUTPUT)],
            client_factory=lambda *, credential=None: client,
        )
        self.assertEqual(code, 1)
        self.assertIn("board: refuse — outside: Grafana returned invalid JSON", stdout)
        self.assertIn("Fix: Correct the stored dashboard", stdout)

    def test_named_uid_other_http_status_is_board_refusal(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(grafana.Response(503))
        code, stdout, _ = self.invoke(
            host,
            FakePage(host, []),
            argv=["outside", "--out", str(OUTPUT)],
            client_factory=lambda *, credential=None: client,
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "board: refuse — outside: Grafana dashboard lookup returned HTTP 503",
            stdout,
        )
        self.assertIn("Fix: ", stdout)

    def test_named_uid_without_dashboard_object_is_board_refusal(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        client = FakeClient(grafana.Response(200, {"message": "no dashboard"}))
        code, stdout, _ = self.invoke(
            host,
            FakePage(host, []),
            argv=["outside", "--out", str(OUTPUT)],
            client_factory=lambda *, credential=None: client,
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "board: refuse — outside: Grafana returned no dashboard object", stdout
        )
        self.assertIn("Fix: ", stdout)

    def test_non_json_rendered_document_is_one_board_refusal(self) -> None:
        host = FakeHost("{not JSON")
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn(
            f"board: refuse — {BOARD_PATH.name}: dashboard is not JSON", stdout
        )
        self.assertIn("Fix: ", stdout)

    def test_rendered_document_without_uid_is_board_refusal(self) -> None:
        document = json.loads(
            board_document("example-board", [("Figure", "timeseries")])
        )
        document.pop("uid")
        host = FakeHost(json.dumps(document))
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("dashboard has no uid", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_rendered_panel_with_empty_title_is_board_refusal(self) -> None:
        host = FakeHost(board_document("example-board", [("", "timeseries")]))
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("dashboard panel has no title", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_rendered_duplicate_titles_are_board_refusal(self) -> None:
        host = FakeHost(
            board_document(
                "example-board", [("Repeated", "timeseries"), ("Repeated", "table")]
            )
        )
        code, stdout, _ = self.invoke(host, FakePage(host, []))
        self.assertEqual(code, 1)
        self.assertIn("dashboard repeats panel title: Repeated", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_unreadable_rendered_board_does_not_stop_next_board(self) -> None:
        host = FakeHost("{not JSON")
        host.files[str(DASHBOARD_PATHS[1])] = board_document(
            "next-board", [("Figure", "timeseries")]
        )
        browser_page = FakePage(host, [("Figure", "drawn", "12 points")])
        code, stdout, _ = self.invoke(host, browser_page)
        self.assertEqual(code, 1)
        self.assertIn("dashboard is not JSON", stdout)
        self.assertIn("board: ok — next-board", stdout)

    def test_named_uid_never_falls_back_past_an_unreadable_rendered_board(
        self,
    ) -> None:
        host = FakeHost("{not JSON")

        def forbidden_factory(
            *, credential: tuple[str, str] | None = None
        ) -> FakeClient:
            raise AssertionError("an unreadable rendered tree reached Grafana's copy")

        code, stdout, _ = self.invoke(
            host,
            FakePage(host, [("Figure", "drawn", "12 points")]),
            argv=["outside-board", "--out", str(OUTPUT)],
            client_factory=forbidden_factory,
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "board: refuse — outside-board: not among the readable rendered boards",
            stdout,
        )

    def test_named_dashboard_without_uid_is_board_refusal(self) -> None:
        dashboard = json.loads(
            board_document("outside-board", [("Figure", "timeseries")])
        )
        dashboard.pop("uid")
        code, stdout = self.run_named_dashboard(dashboard)
        self.assertEqual(code, 1)
        self.assertIn("dashboard has no uid", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_named_dashboard_empty_panel_title_is_board_refusal(self) -> None:
        dashboard = json.loads(board_document("outside-board", [("", "timeseries")]))
        code, stdout = self.run_named_dashboard(dashboard)
        self.assertEqual(code, 1)
        self.assertIn("dashboard panel has no title", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_named_dashboard_duplicate_titles_are_board_refusal(self) -> None:
        dashboard = json.loads(
            board_document(
                "outside-board", [("Repeated", "timeseries"), ("Repeated", "table")]
            )
        )
        code, stdout = self.run_named_dashboard(dashboard)
        self.assertEqual(code, 1)
        self.assertIn("dashboard repeats panel title: Repeated", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_named_dashboard_collapsed_row_without_title_is_board_refusal(self) -> None:
        """A collapsed row needs a title for its browser handle."""

        dashboard = json.loads(
            board_document(
                "outside-board", [], rows=[("", [("Host load", "timeseries")])]
            )
        )
        code, stdout = self.run_named_dashboard(dashboard)
        self.assertEqual(code, 1)
        self.assertIn("dashboard collapsed row has no title", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: Correct the dashboard in Grafana, then retry.", stdout)

    def test_named_dashboard_repeated_row_title_is_board_refusal(self) -> None:
        """Collapsed row handles must be unique on the board."""

        dashboard = json.loads(
            board_document(
                "outside-board",
                [],
                rows=[
                    ("Host detail", [("Host load", "timeseries")]),
                    ("Host detail", [("Host memory", "stat")]),
                ],
            )
        )
        code, stdout = self.run_named_dashboard(dashboard)
        self.assertEqual(code, 1)
        self.assertIn("dashboard repeats row title: Host detail", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: Correct the dashboard in Grafana, then retry.", stdout)

    def test_named_dashboard_non_object_document_is_board_refusal(self) -> None:
        code, stdout = self.run_named_dashboard("{not JSON")
        self.assertEqual(code, 1)
        self.assertIn("Grafana returned no dashboard object", stdout)
        self.assertIn("board: refuse", stdout)
        self.assertIn("Fix: ", stdout)

    def test_viewport_height_uses_grid_row_with_floor_and_allowance(self) -> None:
        document = json.loads(
            board_document("example-board", [("Figure", "timeseries")])
        )
        host = FakeHost(json.dumps(document))
        browser_page = FakePage(host, [("Figure", "drawn", "12 points")])
        code, _, _ = self.invoke(host, browser_page)
        self.assertEqual(code, 0)
        self.assertEqual(
            browser_page.resizes, [(page.VIEWPORT_WIDTH, page.VIEWPORT_FLOOR)]
        )

        document["panels"][0]["gridPos"].update({"y": 44, "h": 7})
        host = FakeHost(json.dumps(document))
        browser_page = FakePage(host, [("Figure", "drawn", "12 points")])
        code, _, _ = self.invoke(host, browser_page)
        lowest = (
            document["panels"][0]["gridPos"]["y"]
            + document["panels"][0]["gridPos"]["h"]
        )
        expected_height = (
            lowest * (page.GRID_CELL_HEIGHT + page.GRID_CELL_MARGIN)
            + page.VIEWPORT_ALLOWANCE
        )
        self.assertEqual(code, 0)
        self.assertEqual(browser_page.resizes, [(page.VIEWPORT_WIDTH, expected_height)])
        self.assertLess(
            browser_page.events.index("resize"), browser_page.events.index("goto", 1)
        )

    def test_quoted_title_is_escaped_in_panel_selector(self) -> None:
        title = 'A "quoted" title'
        selector = (
            'section[data-testid="data-testid Panel header A \\"quoted\\" title"]'
        )
        host = FakeHost(board_document("example-board", [(title, "timeseries")]))
        browser_page = FakePage(
            host,
            [(title, "drawn", "12 points")],
            sections={title: selector},
        )
        code, stdout, _ = self.invoke(host, browser_page)
        self.assertEqual(code, 0)
        self.assertIn(f"{title}: drawn", stdout)
        self.assertIn(selector, browser_page.count_selectors)

    def test_record_hands_back_output_files_and_bytecode_caches(self) -> None:
        host = FakeHost(board_document("example-board", [("Figure", "timeseries")]))
        cache = ROOT / "tools/boards/__pycache__"
        host.directories[str(cache)] = ["example.pyc"]
        host.find_result = subprocess.CompletedProcess(["find"], 0, f"{cache}\n", "")
        browser_page = FakePage(host, [("Figure", "drawn", "12 points")])
        code, stdout, _ = self.invoke(host, browser_page)
        self.assertEqual(code, 0)
        self.assertIn("record: ok", stdout)
        for path in (
            OUTPUT,
            OUTPUT / "requests.log",
            OUTPUT / "example-board.png",
            cache,
            cache / "example.pyc",
        ):
            self.assertIn(str(path), host.chowns)


if __name__ == "__main__":
    unittest.main()

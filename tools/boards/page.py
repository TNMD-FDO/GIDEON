"""Check Grafana board markup against expected panels.

Selectors follow the pinned frontend described in "Grafana 13.0.2's browser
surface for a headless board check, and the Playwright 1.62.0 facts it needs";
re-read them when the Grafana pin moves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.parse import quote, urlsplit

from gideon.evaluation.turns.browser import PageError
from gideon.host import grafana, secrets
from gideon.host.render.grafana import GRAFANA_SUB_PATH
from gideon.host.report import Problem, one_line
from tools.boards.inventory import Board, CollapsedRow, ExpectedPanel

# Research note section 1, Sign-in: the local form.
LOGIN_PATH: Final = f"{GRAFANA_SUB_PATH}login"
USERNAME_SELECTOR: Final = '[data-testid="data-testid Username input field"]'
PASSWORD_SELECTOR: Final = '[data-testid="data-testid Password input field"]'
LOGIN_SELECTOR: Final = '[data-testid="data-testid Login button"]'
LOGIN_ERROR_SELECTOR: Final = '[data-testid="data-testid Alert error"]'

# Research note section 2, Opening a board: route and refresh.
BOARD_QUERY: Final = "kiosk&refresh=1d"

# Research note section 3, Panel markup: chrome and contents.
PANEL_SECTION_PREFIX: Final = "data-testid Panel header "
PANEL_CONTENT_SELECTOR: Final = '[data-testid="data-testid panel content"]'
LOADING_SELECTOR: Final = '[role="status"][aria-label="Panel loading bar"]'
ALERT_LOADING_TEXT: Final = "Loading..."
ERROR_SELECTOR: Final = '[data-testid="data-testid Panel status error"]'
TOOLTIP_SELECTOR: Final = '[data-testid="data-testid tooltip"]'
DATA_ERROR_SELECTOR: Final = '[data-testid="data-testid Panel data error message"]'
NO_DATA_TEXT: Final = "No data"
TABLE_EMPTY_TEXT: Final = "No rows"
STAT_EMPTY_TEXT: Final = "No data"
ALERT_EMPTY_TEXT: Final = "No alerts matching filters"

# Research note section 10, Collapsed rows, as its Settled section corrects it:
# the toggle button's test id and its closed-state label.
ROW_BUTTON_PREFIX: Final = "data-testid dashboard-row-toggle-for-"
ROW_EXPAND_LABEL_PREFIX: Final = "Expand row"

# Research note sections 5 and 9, Lazy loading and Playwright: grid and viewport.
GRID_CELL_HEIGHT: Final = 30
GRID_CELL_MARGIN: Final = 8
VIEWPORT_WIDTH: Final = 1600
VIEWPORT_ALLOWANCE: Final = 400
VIEWPORT_FLOOR: Final = 900

# Research note sections 3 and 9, Panel markup and Playwright: bounded reads.
SETTLE_SECONDS: Final = 60
SETTLE_INTERVAL_SECONDS: Final = 1
MESSAGE_LENGTH: Final = 300

type PanelState = Literal["drawn", "empty", "errored", "loading", "missing"]
FAILING_STATES: Final[frozenset[PanelState]] = frozenset(
    {"errored", "loading", "missing"}
)


class BoardPage(Protocol):
    """Browser operations needed to read one Grafana board."""

    def goto(self, path: str) -> None: ...

    def fill(self, selector: str, text: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def count(self, selector: str) -> int: ...

    def text(self, selector: str) -> str | None: ...

    def url(self) -> str: ...

    def hover(self, selector: str) -> None: ...

    def resize(self, width: int, height: int) -> None: ...

    def screenshot(self, path: Path) -> None: ...

    def wait_until(
        self, predicate: Callable[[], bool], timeout_seconds: float
    ) -> bool: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class PanelReading:
    """One expected panel's visible state and safe detail."""

    title: str
    state: PanelState
    detail: str = ""


_PAGE_FIX: Final = "Check the Grafana ingress and browser request log, then retry."


def _signin_fix() -> str:
    return (
        f"Rewrite {secrets.secret_path('grafana_admin_password')} in place from the office "
        "password manager if the file is wrong. If Grafana's stored password is what "
        f"differs: {grafana.administrator_rotation_route()}. After repeated failures "
        "wait five minutes for Grafana's login lockout, then retry."
    )


def _login_page(page: BoardPage) -> bool:
    return urlsplit(page.url()).path.rstrip("/") == LOGIN_PATH.rstrip("/")


def sign_in(
    page: BoardPage, username: str, password: str, *, timeout: float
) -> Problem | None:
    """Submit the local form once and wait for navigation away from login."""

    try:
        page.goto(LOGIN_PATH)
        if not page.wait_until(
            lambda: all(
                page.count(selector) > 0
                for selector in (USERNAME_SELECTOR, PASSWORD_SELECTOR, LOGIN_SELECTOR)
            ),
            timeout,
        ):
            return Problem("Grafana sign-in form did not appear", _PAGE_FIX)
        page.fill(USERNAME_SELECTOR, username)
        page.fill(PASSWORD_SELECTOR, password)
        page.click(LOGIN_SELECTOR)
        page.wait_until(
            lambda: not _login_page(page) or page.count(LOGIN_ERROR_SELECTOR) > 0,
            timeout,
        )
        if page.count(LOGIN_ERROR_SELECTOR) > 0:
            return Problem("Grafana refused the break-glass sign-in", _signin_fix())
        if _login_page(page):
            return Problem("Grafana sign-in did not finish", _signin_fix())
        return None
    except PageError as exc:
        if exc.certificate:
            return Problem(
                "the browser does not trust the site's certificate",
                "Run with --trust-ca once, then retry.",
            )
        return Problem("Grafana sign-in page could not be read", _PAGE_FIX)


def _css_attribute_value(value: str) -> str:
    """Escape a CSS quoted attribute value, including a title containing a quote."""

    return "".join(
        f"\\{ord(char):x} "
        if ord(char) < 32
        else "\\" + char
        if char in {'"', "\\"}
        else char
        for char in value
    )


def _panel_section(title: str) -> str:
    return f'section[data-testid="{_css_attribute_value(PANEL_SECTION_PREFIX + title)}"]'


def _row_button(title: str) -> str:
    return f'button[data-testid="{_css_attribute_value(ROW_BUTTON_PREFIX + title)}"]'


def _inside(section: str, selector: str) -> str:
    return f"{section} {selector}"


def _content(page: BoardPage, section: str) -> str | None:
    return page.text(_inside(section, PANEL_CONTENT_SELECTOR))


def _blank_stat(page: BoardPage, section: str, panel: ExpectedPanel) -> bool:
    # A stat or bar gauge over null values has a blank value element; a panel
    # that has not rendered has empty content.
    return panel.type in {"stat", "bargauge"} and (
        page.count(_inside(section, f"{PANEL_CONTENT_SELECTOR} *")) > 0
    )


def _has_content(
    page: BoardPage, section: str, panel: ExpectedPanel, content: str | None
) -> bool:
    return bool(content and content.strip()) or _blank_stat(page, section, panel)


def _is_loading(page: BoardPage, section: str, content: str | None) -> bool:
    return page.count(_inside(section, LOADING_SELECTOR)) > 0 or (
        content is not None and content.strip() == ALERT_LOADING_TEXT
    )


def open_board(page: BoardPage, board: Board) -> None:
    """Size the viewport from the lowest grid row before navigating."""

    row_height = GRID_CELL_HEIGHT + GRID_CELL_MARGIN
    grid_height = board.lowest_grid_row * row_height
    page.resize(VIEWPORT_WIDTH, max(VIEWPORT_FLOOR, grid_height + VIEWPORT_ALLOWANCE))
    page.goto(f"{GRAFANA_SUB_PATH}d/{quote(board.uid, safe='')}?{BOARD_QUERY}")


def _wait_for_selector(page: BoardPage, selector: str, timeout: float) -> bool:
    return page.wait_until(lambda: page.count(selector) > 0, timeout)


def _expand_rows(page: BoardPage, board: Board, *, timeout: float) -> tuple[CollapsedRow, ...]:
    """Open collapsed rows and return those whose panels did not mount."""

    unopened: list[CollapsedRow] = []
    for row in board.collapsed_rows:
        button = _row_button(row.title)
        expand_button = (
            f'{button}[aria-label^="{_css_attribute_value(ROW_EXPAND_LABEL_PREFIX)}"]'
        )
        first_panel = _panel_section(row.panel_titles[0])
        try:
            if not _wait_for_selector(page, button, timeout):
                unopened.append(row)
                continue
            if page.count(expand_button) > 0:
                page.click(expand_button)
            if not _wait_for_selector(page, first_panel, timeout):
                unopened.append(row)
        except PageError:
            unopened.append(row)
    return tuple(unopened)


def _ready(page: BoardPage, board: Board) -> bool:
    # A mounted panel can show neither the loading bar nor content before its
    # query starts, so content or the error marker is the positive signal.
    for panel in board.panels:
        section = _panel_section(panel.title)
        if page.count(section) == 0:
            return False
        content = _content(page, section)
        if _is_loading(page, section, content):
            return False
        if page.count(_inside(section, ERROR_SELECTOR)) == 0 and not _has_content(
            page, section, panel, content
        ):
            return False
    return True


def settle(page: BoardPage, board: Board) -> bool:
    """Require two ready reads one page interval apart, stopping at the bound."""

    previous = _ready(page, board)
    for _ in range(SETTLE_SECONDS // SETTLE_INTERVAL_SECONDS):
        page.sleep(SETTLE_INTERVAL_SECONDS)
        current = _ready(page, board)
        if previous and current:
            return True
        previous = current
    return False


def _message(page: BoardPage, marker: str, *, timeout: float) -> str:
    try:
        page.hover(marker)
        if not page.wait_until(lambda: page.count(TOOLTIP_SELECTOR) > 0, timeout):
            return "message unread"
        content = page.text(TOOLTIP_SELECTOR)
    except PageError:
        return "message unread"
    return one_line(content or "")[:MESSAGE_LENGTH] or "message unread"


def classify(page: BoardPage, panel: ExpectedPanel, *, timeout: float) -> PanelReading:
    """Classify one expected panel from its section, marker, and content."""

    section = _panel_section(panel.title)
    if page.count(section) == 0:
        return PanelReading(panel.title, "missing")
    marker = _inside(section, ERROR_SELECTOR)
    if page.count(marker) > 0:
        return PanelReading(
            panel.title, "errored", _message(page, marker, timeout=timeout)
        )
    content = _content(page, section)
    if _is_loading(page, section, content) or not _has_content(
        page, section, panel, content
    ):
        return PanelReading(panel.title, "loading")
    text = (content or "").strip()
    if not text:
        # The screenshot shows what a blank stat or bar gauge should display.
        return PanelReading(panel.title, "drawn", "blank value")
    data_error = page.text(_inside(section, DATA_ERROR_SELECTOR))
    if data_error is not None:
        detail = one_line(data_error).strip()
        if detail != NO_DATA_TEXT:
            return PanelReading(panel.title, "errored", detail[:MESSAGE_LENGTH])
        return PanelReading(panel.title, "empty", detail)
    empty_text = {
        "table": TABLE_EMPTY_TEXT,
        "stat": STAT_EMPTY_TEXT,
        "bargauge": STAT_EMPTY_TEXT,
        "alertlist": ALERT_EMPTY_TEXT,
    }.get(panel.type)
    if empty_text is not None and text == empty_text:
        return PanelReading(panel.title, "empty", empty_text)
    return PanelReading(panel.title, "drawn")


def check_board(
    page: BoardPage, board: Board, screenshot: Path, *, timeout: float
) -> tuple[PanelReading, ...] | Problem:
    """Open, settle, capture, then read tooltip messages from one board."""

    try:
        open_board(page, board)
        unopened = {
            title: row.title
            for row in _expand_rows(page, board, timeout=timeout)
            for title in row.panel_titles
        }
        # A row that did not open has no panels to wait for.
        reachable = tuple(panel for panel in board.panels if panel.title not in unopened)
        settle(page, replace(board, panels=reachable))
        page.screenshot(screenshot)
        return tuple(
            PanelReading(panel.title, "missing", f"row {unopened[panel.title]} did not open")
            if panel.title in unopened
            else classify(page, panel, timeout=timeout)
            for panel in board.panels
        )
    except PageError:
        return Problem("Grafana board could not be read", _PAGE_FIX)

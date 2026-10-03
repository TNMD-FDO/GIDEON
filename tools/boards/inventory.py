"""Expected Grafana boards and panels from dashboard documents and rendered files."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol
from urllib.parse import quote

from gideon.host import grafana
from gideon.host.render import ARTIFACTS
from gideon.host.report import Problem
from gideon.host.sysio import Host

RENDERED_ROOT: Final[Path] = Path("/etc/gideon/rendered")
_DASHBOARD_DIR: Final[PurePosixPath] = PurePosixPath("grafana/dashboards")
_REMOTE_FIX: Final = "Check Grafana availability and the dashboard, then retry."
_DOCUMENT_FIX: Final = "Correct the dashboard in Grafana, then retry."
_UNKNOWN_FIX: Final = (
    "Choose an existing uid or a provisioned uid ({uids}), then retry."
)


@dataclass(frozen=True, slots=True)
class ExpectedPanel:
    """A titled panel the browser must account for."""

    title: str
    type: str


@dataclass(frozen=True, slots=True)
class Board:
    """One dashboard's identity, expected panels, and lowest grid row."""

    uid: str
    title: str
    panels: tuple[ExpectedPanel, ...]
    lowest_grid_row: int


def parse_board(document: str) -> Board:
    """Read one dashboard JSON document, including panels nested under rows."""

    try:
        dashboard = json.loads(document)
    except json.JSONDecodeError as exc:
        raise ValueError("dashboard is not JSON") from exc
    if not isinstance(dashboard, dict):
        raise ValueError("dashboard must be an object")
    uid = dashboard.get("uid")
    title = dashboard.get("title")
    if not isinstance(uid, str) or not uid.strip():
        raise ValueError("dashboard has no uid")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("dashboard has no title")

    expected: list[ExpectedPanel] = []
    titles: set[str] = set()
    lowest_grid_row = 0

    def visit(raw_panels: object) -> None:
        nonlocal lowest_grid_row
        if not isinstance(raw_panels, list):
            raise ValueError("dashboard panels must be a list")
        for panel in raw_panels:
            if not isinstance(panel, dict):
                raise ValueError("dashboard panel must be an object")
            panel_type = panel.get("type")
            if not isinstance(panel_type, str) or not panel_type:
                raise ValueError("dashboard panel has no type")
            position = panel.get("gridPos")
            if not isinstance(position, dict):
                raise ValueError("dashboard panel has no grid position")
            y, height = position.get("y"), position.get("h")
            if type(y) is not int or type(height) is not int or y < 0 or height <= 0:
                raise ValueError("dashboard panel has an invalid grid position")
            lowest_grid_row = max(lowest_grid_row, y + height)
            if panel_type != "row":
                panel_title = panel.get("title")
                if not isinstance(panel_title, str) or not panel_title.strip():
                    raise ValueError("dashboard panel has no title")
                if panel_title in titles:
                    raise ValueError(f"dashboard repeats panel title: {panel_title}")
                titles.add(panel_title)
                expected.append(ExpectedPanel(panel_title, panel_type))
            if "panels" in panel:
                visit(panel["panels"])

    visit(dashboard.get("panels"))
    return Board(uid, title, tuple(expected), lowest_grid_row)


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A dashboard file present on the host that gives no board, named by its file."""

    name: str
    reason: str


class BoardClient(Protocol):
    """The Grafana read needed for a named dashboard."""

    def request(self, method: str, path: str) -> grafana.Response: ...


def provisioned_boards(
    host: Host, *, rendered_root: Path = RENDERED_ROOT
) -> tuple[Board | Unreadable, ...]:
    """Read only registry dashboards present directly in this host's rendered tree.

    A file that cannot be read or parsed is its own entry, so the boards
    beside it are still opened.
    """

    boards: list[Board | Unreadable] = []
    for artifact in ARTIFACTS:
        relative = PurePosixPath(artifact.relative_path)
        if relative.parent != _DASHBOARD_DIR:
            continue
        path = rendered_root / artifact.relative_path
        if not host.exists(path):
            continue
        try:
            boards.append(parse_board(host.read_text(path)))
        except OSError:
            boards.append(Unreadable(relative.name, "dashboard file could not be read"))
        except ValueError as exc:
            boards.append(Unreadable(relative.name, str(exc)))
    return tuple(boards)


def named_board(
    client: BoardClient, uid: str, *, provisioned_uids: Sequence[str]
) -> Board | Problem:
    """Read a named dashboard from Grafana's stored copy."""

    try:
        response = client.request("GET", f"/api/dashboards/uid/{quote(uid, safe='')}")
    except grafana.GrafanaError as exc:
        return Problem(f"{uid}: {exc.problem}", exc.fix)
    if response.status == 404:
        names = ", ".join(sorted(provisioned_uids)) or "none"
        return Problem(f"{uid}: no such board", _UNKNOWN_FIX.format(uids=names))
    if response.status != 200:
        return Problem(
            f"{uid}: Grafana dashboard lookup returned HTTP {response.status}",
            _REMOTE_FIX,
        )
    body = response.body
    if not isinstance(body, dict) or not isinstance(body.get("dashboard"), dict):
        return Problem(f"{uid}: Grafana returned no dashboard object", _REMOTE_FIX)
    try:
        board = parse_board(json.dumps(body["dashboard"]))
    except (TypeError, ValueError) as exc:
        return Problem(f"{uid}: {exc}", _DOCUMENT_FIX)
    if board.uid != uid:
        return Problem(
            f"{uid}: Grafana returned a different dashboard uid", _REMOTE_FIX
        )
    return board

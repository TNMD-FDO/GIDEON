"""The pinned Chromium launcher and the browser-mode host footprint."""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from gideon.evaluation.turns.browser import Page, PageDrain, PageError
from gideon.host import tls
from gideon.host.report import Problem
from gideon.host.sysio import Host

if TYPE_CHECKING:
    import playwright.sync_api


# Playwright 1.62.0 bundles Chromium 151.0.7922.34 at revision 1234.
PLAYWRIGHT_VERSION: Final[str] = "1.62.0"
HARNESS_HOME: Final[Path] = Path("/root/.config/gideon-turns")
PASSWORD_FILE: Final[Path] = HARNESS_HOME / "gideon-test-user.password"
BROWSERS_DIR: Final[Path] = HARNESS_HOME / "browsers"
BROWSER_HOME: Final[Path] = HARNESS_HOME / "home"
# Chromium's Linux NSS database default has been this path since M146; see
# the research note's Part B §3.4.
TRUST_STORE_PATH: Final[Path] = BROWSER_HOME / ".local/share/pki/nssdb"
TRUST_NICKNAME: Final[str] = "gideon-office-ca"
PAGE_TIMEOUT_SECONDS: Final[float] = 30.0
# certutil reads a password from its stdin when it wants one; the seam gives a
# child no stdin, and this bound closes the other way a prompt could hang a run.
CERTUTIL_TIMEOUT_SECONDS: Final[float] = 60.0

type Disposition = Literal["continue", "abort"]
type RequestKind = Literal["request", "websocket"]


@dataclass(frozen=True, slots=True)
class RequestEntry:
    """One request or WebSocket disposition observed by the browser context."""

    elapsed: float
    kind: RequestKind
    method: str
    url: str
    disposition: Disposition


@dataclass(frozen=True, slots=True)
class RequestCounts:
    """The request restriction counts and distinct off-host names."""

    on_host: int
    off_host: int
    off_hostnames: tuple[str, ...]


class RequestLog:
    """Keep request evidence in memory until the run writes its log."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started = monotonic()
        self._entries: list[RequestEntry] = []

    @property
    def entries(self) -> tuple[RequestEntry, ...]:
        return tuple(self._entries)

    def append(
        self,
        kind: RequestKind,
        method: str,
        url: str,
        disposition_value: Disposition,
    ) -> None:
        """Append an entry stamped against the log's injected monotonic clock."""

        self._entries.append(
            RequestEntry(
                elapsed=max(0.0, self._monotonic() - self._started),
                kind=kind,
                method=method,
                url=url,
                disposition=disposition_value,
            )
        )

    def render(self) -> str:
        """Render one stable, non-secret line for every observed entry."""

        return "\n".join(
            f"{entry.elapsed:.3f}s {entry.kind} {entry.method} "
            f"{entry.url} {entry.disposition}"
            for entry in self._entries
        )

    def counts(self) -> RequestCounts:
        """Count allowed and blocked entries and name every blocked host."""

        off_hostnames = sorted(
            {
                parsed.hostname
                for entry in self._entries
                if entry.disposition == "abort"
                for parsed in (urlsplit(entry.url),)
                if parsed.hostname is not None
            }
        )
        off_host = sum(entry.disposition == "abort" for entry in self._entries)
        return RequestCounts(
            on_host=len(self._entries) - off_host,
            off_host=off_host,
            off_hostnames=tuple(off_hostnames),
        )


def disposition(url: str, hostname: str) -> Disposition:
    """Allow only the site's HTTPS/WSS origin and in-page URL schemes."""

    try:
        parsed = urlsplit(url)
    except ValueError:
        return "abort"
    scheme = parsed.scheme.casefold()
    if scheme in {"data", "blob", "about"}:
        return "continue"
    if scheme not in {"https", "wss"} or parsed.hostname is None:
        return "abort"
    if parsed.hostname.casefold() != hostname.casefold():
        return "abort"
    try:
        port = parsed.port
    except ValueError:
        return "abort"
    if port not in (None, 443):
        return "abort"
    return "continue"


# The only JavaScript run by the harness. It observes mutations but records
# only what a painted animation frame showed; see the plan's Implementation
# Details §1 and the research note's Part A §4.
OBSERVER_SCRIPT: Final[str] = r"""
(() => {
  const key = "__gideon_turn_observer__";
  const previous = window[key];
  if (previous && previous.stop) previous.stop();
  const state = {
    frames: [],
    dirty: false,
    lastBlock: "",
    lastAnswer: "",
    stopped: false,
  };
  const textWithout = (whole, part) => {
    if (!part) return whole;
    const index = whole.indexOf(part);
    return index < 0 ? whole : whole.slice(0, index) + whole.slice(index + part.length);
  };
  // The turn's assistant message: the last element whose id is message-<uuid>
  // without the user-message class (the user's own prompt carries it, and the
  // composer's id starts with message- too — found on the box). Inside it the
  // answer is the rendered markdown under its #response-content-container (an
  // id the frontend gives every response's content wrapper), and the
  // reasoning collapsible is the aria-expanded button there whose summary
  // reads Thinking or Thought (the note's Part A section 3.3); the model-name
  // header and the timestamp sit outside that container and are never read.
  const assistantMessage = () => {
    const all = Array.from(document.querySelectorAll('[id^="message-"]')).filter(
      (element) => /^message-[0-9a-f-]{36}$/.test(element.id) && !element.classList.contains("user-message")
    );
    return all.length ? all[all.length - 1] : null;
  };
  const reasoningButton = (content) => {
    const buttons = Array.from(content.querySelectorAll('button[aria-expanded]'));
    return buttons.find((button) => /^(Thinking|Thought)/.test((button.innerText || "").trim())) || null;
  };
  const current = () => {
    const message = assistantMessage();
    const content = message ? message.querySelector("#response-content-container") : null;
    if (!content) return { block: "", answer: "", summary: "", expanded: false };
    const button = reasoningButton(content);
    const root = button ? button.parentElement : null;
    const answer = Array.from(content.querySelectorAll(".markdown-prose"))
      .filter((element) => !root || !root.contains(element))
      .map((element) => element.innerText || "")
      .join("\n");
    if (!button) return { block: "", answer, summary: "", expanded: false };
    const expanded = button.getAttribute("aria-expanded") === "true";
    // The block is the collapsible's other children read live: its body slot
    // is the button's sibling, and a live element's innerText leaves out the
    // summary's spinner stylesheet, which a detached clone's innerText carried
    // ahead of "Thinking..." and so hid the button from a text match while
    // the reasoning was in progress (slice-1 ticket 37's proof).
    let block = "";
    if (expanded && root) {
      block = Array.from(root.children)
        .filter((child) => child !== button)
        .map((child) => child.innerText || "")
        .join("\n");
    }
    return { block, answer, summary: button.innerText || "", expanded };
  };
  const capture = (instant) => {
    const value = current();
    if (!value.summary && !value.block && !value.answer) return false;
    const blockExtended = value.block.startsWith(state.lastBlock);
    const answerExtended = value.answer.startsWith(state.lastAnswer);
    state.frames.push({
      instant,
      summary: value.summary,
      expanded: value.expanded,
      block: {
        text: blockExtended ? value.block.slice(state.lastBlock.length) : value.block,
        extended: blockExtended,
      },
      answer: {
        text: answerExtended ? value.answer.slice(state.lastAnswer.length) : value.answer,
        extended: answerExtended,
      },
    });
    state.lastBlock = value.block;
    state.lastAnswer = value.answer;
    return true;
  };
  const observer = new MutationObserver(() => { state.dirty = true; });
  if (document.body) observer.observe(document.body, {
    subtree: true,
    childList: true,
    characterData: true,
  });
  const paint = (instant) => {
    if (state.dirty && capture(instant)) state.dirty = false;
    if (!state.stopped) window.requestAnimationFrame(paint);
  };
  state.stop = () => {
    state.stopped = true;
    observer.disconnect();
  };
  state.drain = () => {
    const frames = state.frames.splice(0);
    const regions = current();
    return {
      frames,
      regions: { block: regions.block, answer: regions.answer },
    };
  };
  window[key] = state;
  window.requestAnimationFrame(paint);
})()
"""

_DRAIN_SCRIPT: Final[str] = (
    "() => window.__gideon_turn_observer__.drain()"
)
# The recipe names bash: sh is dash on the box, whose read has no -s, and a
# dash run of it silently wrote an empty file once.
_PASSWORD_RECIPE: Final[str] = (
    "sudo bash -c 'read -s -p \"password: \" p; echo; "
    f"printf \"%s\\n\" \"$p\" > {PASSWORD_FILE}; chmod 600 {PASSWORD_FILE}'"
)
_PASSWORD_READ_FIX: Final[str] = f"Create the file as root with {_PASSWORD_RECIPE}, then retry."
_PASSWORD_EMPTY_FIX: Final[str] = f"Retype the password with {_PASSWORD_RECIPE}, then retry."
_PASSWORD_MODE_FIX: Final[str] = (
    f"Run `sudo chown root:root {PASSWORD_FILE} && sudo chmod 0600 {PASSWORD_FILE}`, then retry."
)
_PLAYWRIGHT_FIX: Final[str] = (
    f"Run `.venv/bin/pip install playwright=={PLAYWRIGHT_VERSION}`, then retry."
)
_BROWSER_FIX: Final[str] = (
    f"Run `sudo env PLAYWRIGHT_BROWSERS_PATH={BROWSERS_DIR} .venv/bin/playwright "
    "install --only-shell chromium`, then install-deps chromium, and retry."
)
_CERTUTIL_FIX: Final[str] = "Install libnss3-tools with apt, then retry."


_STORE_FIX: Final[str] = f"Repair the NSS store at {TRUST_STORE_PATH}, then retry."


def _certutil(host: Host, argv: list[str]) -> subprocess.CompletedProcess[str] | Problem:
    """Run one certutil command under the bound; a missing binary is its own problem."""

    try:
        result = host.run(argv, timeout=CERTUTIL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return Problem(
            f"certutil did not finish within {CERTUTIL_TIMEOUT_SECONDS:.0f} s", _STORE_FIX
        )
    if result.returncode == 127:
        return Problem("certutil is not installed", _CERTUTIL_FIX)
    return result


def _certutil_problem(result: subprocess.CompletedProcess[str], action: str) -> Problem | None:
    """A non-zero exit as a problem naming the store and the action, never a byte of output."""

    if result.returncode == 0:
        return None
    return Problem(
        f"certutil {action} failed for NSS store {TRUST_STORE_PATH} "
        f"(exit {result.returncode})",
        _STORE_FIX,
    )


def trust_ca(host: Host) -> tuple[str, Problem | None]:
    """Trust the office CA in the browser-only NSS store, idempotently.

    The database is created only when absent; the nickname is looked up first
    (certutil answers 255 for an absent nickname, so any non-zero code short of
    a missing binary means "add it", and the add's own result is the verdict).
    """

    store = str(TRUST_STORE_PATH)
    if not host.exists(TRUST_STORE_PATH / "cert9.db"):
        host.mkdir(TRUST_STORE_PATH, mode=0o700, parents=True, exist_ok=True)
        created = _certutil(host, ["certutil", "-d", f"sql:{store}", "-N", "--empty-password"])
        if isinstance(created, Problem):
            return "NSS database was not created", created
        problem = _certutil_problem(created, "database creation")
        if problem is not None:
            return "NSS database was not created", problem

    listing = _certutil(host, ["certutil", "-d", f"sql:{store}", "-L", "-n", TRUST_NICKNAME])
    if isinstance(listing, Problem):
        return "CA lookup did not run", listing
    if listing.returncode == 0:
        return f"CA already trusted as {TRUST_NICKNAME}", None

    added = _certutil(
        host,
        [
            "certutil",
            "-d",
            f"sql:{store}",
            "-A",
            "-t",
            "C,,",
            "-n",
            TRUST_NICKNAME,
            "-i",
            tls.CA_PATH,
        ],
    )
    if isinstance(added, Problem):
        return "CA was not added", added
    problem = _certutil_problem(added, "CA import")
    if problem is not None:
        return "CA was not added", problem
    return f"CA trusted as {TRUST_NICKNAME}", None


def password_file_problem(host: Host) -> Problem | None:
    """Validate the test account password file without exposing its contents."""

    if not host.exists(PASSWORD_FILE):
        return Problem(f"password file is missing: {PASSWORD_FILE}", _PASSWORD_READ_FIX)
    try:
        metadata = host.stat(PASSWORD_FILE)
    except OSError as exc:
        return Problem(f"password file could not be inspected: {exc}", _PASSWORD_MODE_FIX)
    if metadata.st_uid != 0 or metadata.st_mode & 0o077:
        return Problem(
            f"password file must be root-owned and mode 0600: {PASSWORD_FILE}",
            _PASSWORD_MODE_FIX,
        )
    try:
        password = read_password(host)
    except OSError as exc:
        return Problem(f"password file could not be read: {exc}", _PASSWORD_READ_FIX)
    if not password:
        return Problem(f"password file is empty: {PASSWORD_FILE}", _PASSWORD_EMPTY_FIX)
    return None


def read_password(host: Host) -> str:
    """Read the password and remove its trailing line ending."""

    return host.read_text(PASSWORD_FILE).rstrip("\r\n")


def playwright_problem(
    version_of: Callable[[str], str] | None = None,
) -> Problem | None:
    """Require the installed Playwright package to match the tool's pin."""

    reader = version_of or importlib.metadata.version
    try:
        installed = reader("playwright")
    except importlib.metadata.PackageNotFoundError:
        return Problem("Playwright is not installed", _PLAYWRIGHT_FIX)
    if installed != PLAYWRIGHT_VERSION:
        return Problem(
            f"Playwright {installed} is installed; {PLAYWRIGHT_VERSION} is required",
            _PLAYWRIGHT_FIX,
        )
    return None


def browser_problem(host: Host, browsers_dir: Path) -> Problem | None:
    """Require a downloaded Playwright Chromium headless shell directory."""

    try:
        names = host.listdir(browsers_dir)
    except OSError:
        names = []
    for name in names:
        if not name.startswith("chromium_headless_shell-"):
            continue
        candidate = browsers_dir / name
        if not host.exists(candidate):
            continue
        try:
            host.listdir(candidate)
        except OSError:
            continue
        return None
    return Problem(
        f"Playwright Chromium headless shell is missing under {browsers_dir}",
        _BROWSER_FIX,
    )


def _page_error(exc: Exception) -> PageError:
    """Playwright's own error as the seam's, the certificate case flagged from its message."""

    message = str(exc)
    lowered = message.casefold()
    certificate = any(marker in lowered for marker in ("certificate", "err_cert", "ssl_error"))
    return PageError(message, certificate=certificate)


class PlaywrightPage:
    """Adapt one Playwright page to the harness's small :class:`Page` seam.

    Every call turns a Playwright error (a runtime-only class here) into a
    :class:`PageError`, so the driver's rows never see a foreign exception.
    """

    def __init__(self, page: playwright.sync_api.Page, hostname: str) -> None:
        self._page = page
        self._origin = f"https://{hostname}"

    def goto(self, path: str) -> None:
        target = path if urlsplit(path).scheme else f"{self._origin}{path}"
        try:
            self._page.goto(target)
        except Exception as exc:
            raise _page_error(exc) from exc

    def fill(self, selector: str, text: str) -> None:
        try:
            self._page.locator(selector).first.fill(text)
        except Exception as exc:
            raise _page_error(exc) from exc

    def click(self, selector: str) -> None:
        try:
            self._page.locator(selector).first.click()
        except Exception as exc:
            raise _page_error(exc) from exc

    def press(self, key: str) -> None:
        try:
            self._page.keyboard.press(key)
        except Exception as exc:
            raise _page_error(exc) from exc

    def text(self, selector: str) -> str | None:
        try:
            locator = self._page.locator(selector)
            if locator.count() == 0:
                return None
            return locator.first.inner_text()
        except Exception as exc:
            raise _page_error(exc) from exc

    def texts(self, selector: str) -> tuple[str, ...]:
        try:
            locator = self._page.locator(selector)
            return tuple(locator.nth(index).inner_text() for index in range(locator.count()))
        except Exception as exc:
            raise _page_error(exc) from exc

    def count(self, selector: str) -> int:
        try:
            return self._page.locator(selector).count()
        except Exception as exc:
            raise _page_error(exc) from exc

    def attribute(self, selector: str, name: str) -> str | None:
        try:
            locator = self._page.locator(selector)
            if locator.count() == 0:
                return None
            return locator.first.get_attribute(name)
        except Exception as exc:
            raise _page_error(exc) from exc

    def url(self) -> str:
        try:
            return self._page.url
        except Exception as exc:
            raise _page_error(exc) from exc

    def storage(self, key: str) -> str | None:
        try:
            value = self._page.evaluate("key => window.localStorage.getItem(key)", key)
        except Exception as exc:
            raise _page_error(exc) from exc
        return value if isinstance(value, str) else None

    def screenshot(self, path: Path) -> None:
        try:
            self._page.screenshot(path=str(path), full_page=True)
        except Exception as exc:
            raise _page_error(exc) from exc

    def sleep(self, seconds: float) -> None:
        """Pause while the page keeps running.

        Under the sync API, every routed request and every page event is
        serviced only while the driver is inside a Playwright call; a Python
        ``time.sleep`` here left the page's request after the sign-in held by
        the route for the whole wait (found on the box, F_0.1.12). The page's
        own timeout is a call, so the page runs for its length.
        """

        try:
            self._page.wait_for_timeout(seconds * 1000)
        except Exception as exc:
            raise _page_error(exc) from exc

    def wait_until(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.sleep(min(0.05, remaining))

    def watch(self) -> None:
        try:
            self._page.evaluate(OBSERVER_SCRIPT)
        except Exception as exc:
            raise _page_error(exc) from exc

    def drain(self) -> PageDrain:
        try:
            raw = self._page.evaluate(_DRAIN_SCRIPT)
        except Exception as exc:
            raise _page_error(exc) from exc
        if not isinstance(raw, Mapping):
            raise PageError("the page observer returned an invalid drain")
        frames = raw.get("frames")
        regions = raw.get("regions")
        if not isinstance(frames, list) or not isinstance(regions, Mapping):
            raise PageError("the page observer returned an invalid drain")
        if not all(isinstance(frame, Mapping) for frame in frames):
            raise PageError("the page observer returned an invalid frame")
        normalized_regions: dict[str, str] = {}
        for name in ("block", "answer"):
            value = regions.get(name, "")
            if not isinstance(value, str):
                raise PageError("the page observer returned invalid regions")
            normalized_regions[name] = value
        return PageDrain(frames=tuple(frames), regions=normalized_regions)


def launch(
    hostname: str,
    *,
    browsers_dir: Path,
    browser_home: Path,
    request_log: RequestLog,
    page_timeout: float,
) -> tuple[Page, Callable[[], None]]:
    """Launch the pinned headless shell with the one-host request boundary."""

    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
    environment: dict[str, str | float | bool] = dict(os.environ)
    environment["HOME"] = str(browser_home)

    # Keep the Playwright dependency behind the browser-mode launch boundary;
    # the API mode remains importable with only the repository's stdlib tools.
    from playwright.sync_api import sync_playwright

    playwright_instance: playwright.sync_api.Playwright = sync_playwright().start()
    browser: playwright.sync_api.Browser | None = None
    context: playwright.sync_api.BrowserContext | None = None
    try:
        browser = playwright_instance.chromium.launch(
            headless=True,
            args=[
                f"--host-resolver-rules=MAP {hostname} 127.0.0.1",
                "--no-proxy-server",
            ],
            env=environment,
        )
        context = browser.new_context(service_workers="block")
        context.set_default_timeout(page_timeout * 1000)

        def route_request(route: playwright.sync_api.Route) -> None:
            request = route.request
            allowed = disposition(request.url, hostname)
            request_log.append("request", request.method, request.url, allowed)
            if allowed == "continue":
                route.continue_()
            else:
                route.abort()

        def route_websocket(socket: playwright.sync_api.WebSocketRoute) -> None:
            allowed = disposition(socket.url, hostname)
            request_log.append("websocket", "GET", socket.url, allowed)
            if allowed == "continue":
                socket.connect_to_server()
            else:
                socket.close()

        context.route("**/*", route_request)
        context.route_web_socket("**/*", route_websocket)
        page = context.new_page()
    except BaseException:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        playwright_instance.stop()
        raise

    def close() -> None:
        try:
            context.close()
        finally:
            try:
                browser.close()
            finally:
                playwright_instance.stop()

    return PlaywrightPage(page, hostname), close

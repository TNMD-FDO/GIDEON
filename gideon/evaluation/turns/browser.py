"""The narrow page seam used by the browser turn harness, and the driver over it."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

from gideon.evaluation.turns.session import LOGS_FIX
from gideon.host.report import Problem

type Frame = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PageDrain:
    """One drain of the frame observer: the captured frames, and the regions as they stand."""

    frames: tuple[Frame, ...]
    regions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class LiveState:
    """One painted message state reconstructed from an observer frame."""

    instant: float
    summary: str
    expanded: bool
    block: str
    answer: str
    block_extended: bool
    answer_extended: bool


@dataclass(frozen=True, slots=True)
class LiveEntry:
    """The compact record of one painted state: its shape, and what the judge said of it.

    The texts themselves are kept only for the states that matter (the first
    trip, the replacement, the end), so a turn of tens of thousands of frames
    costs linear memory.
    """

    instant: float
    summary: str
    expanded: bool
    block_length: int
    answer_length: int
    block_extended: bool
    answer_extended: bool
    tripped: str | None
    refused: bool
    replaced: bool
    # A frame the browser painted, or the regions read directly at the end.
    painted: bool = True
    # The block carried text beyond whitespace in this state: the withholding
    # check's flag, whether the state was painted or read at the end.
    block_text: bool = False


@dataclass(frozen=True, slots=True)
class BrowserTurn:
    """The page evidence and outcome of one browser turn."""

    chat_id: str | None
    entries: tuple[LiveEntry, ...]
    texts: Mapping[int, tuple[str, str]]
    block_opened_at: float | None
    screenshot: Path
    regions: Mapping[str, str]
    elapsed: float
    problem: Problem | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    """Facts visible in the new-chat composer and its menus."""

    selector_label: str
    models: tuple[str, ...]
    plus_items: tuple[str, ...]
    integrations: tuple[tuple[str, bool], ...]
    screenshots: tuple[Path, ...]


class PageError(Exception):
    """A page operation failed, with certificate failures called out safely."""

    def __init__(self, problem: str, *, certificate: bool = False) -> None:
        super().__init__(problem)
        self.problem = problem
        self.certificate = certificate


class Page(Protocol):
    """The browser operations the turn driver is allowed to use."""

    def goto(self, path: str) -> None: ...

    def fill(self, selector: str, text: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def press(self, key: str) -> None: ...

    def text(self, selector: str) -> str | None: ...

    def texts(self, selector: str) -> Sequence[str]: ...

    def count(self, selector: str) -> int: ...

    def attribute(self, selector: str, name: str) -> str | None: ...

    def url(self) -> str: ...

    def storage(self, key: str) -> str | None: ...

    def screenshot(self, path: Path) -> None: ...

    def wait_until(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool: ...

    def sleep(self, seconds: float) -> None: ...

    def watch(self) -> None: ...

    def drain(self) -> PageDrain: ...


# Selectors for the LDAP form and its alternate-form toggle.
USERNAME_SELECTOR = "#username"
PASSWORD_SELECTOR = "#password"
AUTHENTICATE_SELECTOR = 'role=button[name="Authenticate"]'
LDAP_TOGGLE_SELECTOR = 'text="Continue with LDAP"'

# Selectors for the composer, its send and stop buttons, the model selector,
# and the two menus.
CHAT_INPUT_SELECTOR = "#chat-input"
SEND_BUTTON_SELECTOR = "#send-message-button"
STOP_BUTTON_SELECTOR = '[aria-label="Stop"]'
MODEL_SELECTOR = "#model-selector-model-button"
INPUT_MENU_SELECTOR = "#input-menu-button"
INTEGRATION_MENU_SELECTOR = "#integration-menu-button"
# The model list's rows are options; the "+" menu's items are plain
# buttons inside its role=menu container; an integrations toggle is a pressed
# button in the same kind of container.
MODEL_OPTION_SELECTOR = "role=option"
MENU_ITEM_SELECTOR = '[role="menu"] button'
INTEGRATION_TOGGLE_SELECTOR = '[role="menu"] button[aria-pressed]'
# A menu renders a moment after its button is clicked; the pause is the
# page's own, so the render is serviced — a starting value.
MENU_SETTLE_SECONDS = 0.5

# A message container's id is message-<uuid>;
# the user's own carries the user-message class and the composer's id starts
# with message- too, so the turn's assistant message is the last container
# with neither. Its reasoning collapsible is the aria-expanded button under
# its #response-content-container (the id the frontend gives every response's
# content wrapper) whose summary reads Thinking or Thought (the
# model-name header is another aria-expanded button, outside that container);
# the pulsing cursor renders while the message is not done.
MESSAGE_SELECTOR = '[id^="message-"]:not(.user-message):not(#message-input-container)'
LAST_MESSAGE_SELECTOR = f"{MESSAGE_SELECTOR} >> nth=-1"
# The content container holds one aria-expanded button, the collapsible; a
# text match on its summary misses it (the button's text starts with
# whitespace), so the container's scope is the rule.
COLLAPSIBLE_BUTTON_SELECTOR = (
    f"{LAST_MESSAGE_SELECTOR} >> #response-content-container >> button[aria-expanded]"
)
CURSOR_SELECTOR = f"{LAST_MESSAGE_SELECTOR} >> span.animate-pulse"

_CERTIFICATE_FIX = "Run with --trust-ca once, then retry."


def open_auth(page: Page, hostname: str) -> Problem | None:
    """The browser stage's first navigation: the auth page, or the row's problem."""

    try:
        page.goto(f"https://{hostname}/auth")
    except PageError as exc:
        if exc.certificate:
            return Problem(
                "the browser does not trust the site's certificate", _CERTIFICATE_FIX
            )
        return Problem("the browser could not load the authentication page", LOGS_FIX)
    return None


def _on_auth(page: Page) -> bool:
    try:
        return urlsplit(page.url()).path.rstrip("/") == "/auth"
    except ValueError:
        return False


def signin(page: Page, account: str, password: str, *, timeout: float) -> str | Problem:
    """Sign in through the rendered LDAP form; the token, or a problem naming no page text."""

    try:
        has_username = page.wait_until(lambda: page.count(USERNAME_SELECTOR) > 0, timeout)
        if not has_username and page.count(LDAP_TOGGLE_SELECTOR) > 0:
            page.click(LDAP_TOGGLE_SELECTOR)
            has_username = page.wait_until(
                lambda: page.count(USERNAME_SELECTOR) > 0, timeout
            )
        if not has_username:
            return Problem("the LDAP sign-in form was not found", LOGS_FIX)
        if page.count(PASSWORD_SELECTOR) == 0:
            return Problem("the LDAP password field was not found", LOGS_FIX)
        if page.count(AUTHENTICATE_SELECTOR) == 0:
            return Problem("the LDAP authentication button was not found", LOGS_FIX)
        page.fill(USERNAME_SELECTOR, account)
        page.fill(PASSWORD_SELECTOR, password)
        page.click(AUTHENTICATE_SELECTOR)
        ready = page.wait_until(
            lambda: not _on_auth(page) and page.count(CHAT_INPUT_SELECTOR) > 0, timeout
        )
        if not ready:
            if _on_auth(page):
                return Problem("the sign-in did not leave the authentication page", LOGS_FIX)
            return Problem("the chat composer did not appear after sign-in", LOGS_FIX)
        token = page.storage("token")
        if not token:
            return Problem("the signed-in page holds no session token", LOGS_FIX)
        return token
    except PageError:
        return Problem("the sign-in page could not be read", LOGS_FIX)


def observe(page: Page, out: Path, *, timeout: float) -> Observation | Problem:
    """The new chat page's selector and two menus, with a screenshot of each open."""

    screenshots: list[Path] = []
    try:
        page.goto("/")
        if not page.wait_until(lambda: page.count(CHAT_INPUT_SELECTOR) > 0, timeout):
            return Problem("the chat composer did not appear", LOGS_FIX)
        if page.count(MODEL_SELECTOR) == 0:
            return Problem("the model selector button was not found", LOGS_FIX)
        label = page.text(MODEL_SELECTOR)
        if label is None:
            return Problem("the model selector label was not found", LOGS_FIX)
        page.click(MODEL_SELECTOR)
        page.sleep(MENU_SETTLE_SECONDS)
        models = tuple(page.texts(MODEL_OPTION_SELECTOR))
        model_screenshot = out / "observe.png"
        page.screenshot(model_screenshot)
        screenshots.append(model_screenshot)
        page.press("Escape")

        if page.count(INPUT_MENU_SELECTOR) == 0:
            return Problem("the plus menu button was not found", LOGS_FIX)
        page.click(INPUT_MENU_SELECTOR)
        page.sleep(MENU_SETTLE_SECONDS)
        plus_items = tuple(text.strip() for text in page.texts(MENU_ITEM_SELECTOR))
        plus_screenshot = out / "observe-plus.png"
        page.screenshot(plus_screenshot)
        screenshots.append(plus_screenshot)
        page.press("Escape")

        integrations: list[tuple[str, bool]] = []
        if page.count(INTEGRATION_MENU_SELECTOR) > 0:
            page.click(INTEGRATION_MENU_SELECTOR)
            page.sleep(MENU_SETTLE_SECONDS)
            for index in range(page.count(INTEGRATION_TOGGLE_SELECTOR)):
                selector = f"{INTEGRATION_TOGGLE_SELECTOR} >> nth={index}"
                name = page.text(selector)
                pressed = page.attribute(selector, "aria-pressed") == "true"
                integrations.append((name or "", pressed))
            integration_screenshot = out / "observe-integrations.png"
            page.screenshot(integration_screenshot)
            screenshots.append(integration_screenshot)
            page.press("Escape")
        return Observation(
            selector_label=label,
            models=models,
            plus_items=plus_items,
            integrations=tuple(integrations),
            screenshots=tuple(screenshots),
        )
    except PageError:
        return Problem("the browser observations could not be read", LOGS_FIX)


def _chat_id(url: str) -> str | None:
    try:
        parts = [part for part in urlsplit(url).path.split("/") if part]
    except ValueError:
        return None
    if len(parts) < 2 or parts[-2] != "c" or not parts[-1]:
        return None
    return unquote(parts[-1])


def _frame_state(
    frame: Mapping[str, object], previous_block: str, previous_answer: str
) -> LiveState | None:
    """One captured frame applied to the previous state; ``None`` for a malformed frame."""

    instant = frame.get("instant")
    summary = frame.get("summary")
    expanded = frame.get("expanded")
    block = frame.get("block")
    answer = frame.get("answer")
    if (
        isinstance(instant, bool)
        or not isinstance(instant, (int, float))
        or not isinstance(summary, str)
        or not isinstance(expanded, bool)
        or not isinstance(block, Mapping)
        or not isinstance(answer, Mapping)
    ):
        return None
    block_text = block.get("text")
    block_extended = block.get("extended")
    answer_text = answer.get("text")
    answer_extended = answer.get("extended")
    if (
        not isinstance(block_text, str)
        or not isinstance(block_extended, bool)
        or not isinstance(answer_text, str)
        or not isinstance(answer_extended, bool)
    ):
        return None
    return LiveState(
        instant=float(instant),
        summary=summary,
        expanded=expanded,
        block=previous_block + block_text if block_extended else block_text,
        answer=previous_answer + answer_text if answer_extended else answer_text,
        block_extended=block_extended,
        answer_extended=answer_extended,
    )


def _quiet_done(page: Page, since_flip: float, page_timeout: float) -> bool:
    """A turn over before any poll saw the Stop button: a message, no cursor, the bound passed."""

    return (
        since_flip >= page_timeout
        and page.count(MESSAGE_SELECTOR) > 0
        and page.count(CURSOR_SELECTOR) == 0
    )


class _Watch:
    """The painted states of one turn, judged as they arrive, kept compact."""

    def __init__(
        self,
        judge: Callable[[str, str], str | None],
        is_replacement: Callable[[str], bool],
    ) -> None:
        self._judge = judge
        self._is_replacement = is_replacement
        self.entries: list[LiveEntry] = []
        self.texts: dict[int, tuple[str, str]] = {}
        self.block = ""
        self.answer = ""
        self._summary = ""
        self._expanded = False
        self._instant = 0.0
        self._tripped_once = False
        self._refused_once = False
        self._replaced_once = False
        self._reasoning_once = False

    def take(self, state: LiveState, *, painted: bool = True) -> None:
        tripped = self._judge(state.block, state.answer)
        refused = self._is_replacement(state.answer)
        replaced = self._tripped_once and refused
        index = len(self.entries)
        self.entries.append(
            LiveEntry(
                instant=state.instant,
                summary=state.summary,
                expanded=state.expanded,
                block_length=len(state.block),
                answer_length=len(state.answer),
                block_extended=state.block_extended,
                answer_extended=state.answer_extended,
                tripped=tripped,
                refused=refused,
                replaced=replaced,
                painted=painted,
                block_text=bool(state.block.strip()),
            )
        )
        if tripped is not None and not self._tripped_once:
            self._tripped_once = True
            self.texts[index] = (state.block, state.answer)
        if refused and not self._refused_once:
            self._refused_once = True
            self.texts[index] = (state.block, state.answer)
        if replaced and not self._replaced_once:
            self._replaced_once = True
            self.texts[index] = (state.block, state.answer)
        if self.entries[index].block_text and not self._reasoning_once:
            # The first state whose block carried text: the withholding
            # failure's evidence, kept for the session as a trip's is.
            self._reasoning_once = True
            self.texts[index] = (state.block, state.answer)
        self.block, self.answer = state.block, state.answer
        self._summary, self._expanded, self._instant = state.summary, state.expanded, state.instant

    def take_frames(self, frames: Sequence[Frame]) -> None:
        for frame in frames:
            state = _frame_state(frame, self.block, self.answer)
            if state is None:
                raise PageError("the browser observer returned an invalid frame")
            self.take(state)

    def take_regions(self, regions: Mapping[str, str]) -> None:
        """The regions read directly at the end, as a state, when no frame showed them."""

        block = regions.get("block", "")
        answer = regions.get("answer", "")
        if not self.entries and not block and not answer:
            return
        if block == self.block and answer == self.answer:
            return
        self.take(
            LiveState(
                instant=self._instant,
                summary=self._summary,
                expanded=self._expanded,
                block=block,
                answer=answer,
                block_extended=block.startswith(self.block),
                answer_extended=answer.startswith(self.answer),
            ),
            painted=False,
        )

    def finish(self) -> None:
        if self.entries:
            self.texts[len(self.entries) - 1] = (self.block, self.answer)


def turn(
    page: Page,
    prompt: str,
    *,
    judge: Callable[[str, str], str | None],
    is_replacement: Callable[[str], bool],
    monotonic: Callable[[], float],
    poll: float,
    page_timeout: float,
    deadline: float,
    out: Path,
    row_name: str,
) -> BrowserTurn:
    """One turn from the composer of a new chat, every painted state judged as it arrives.

    *judge* answers a pattern id or ``None`` for a block text and an answer,
    the block ignored since the Filter withholds the reasoning (the block's
    text beyond whitespace is the withholding check's flag, not the judge's);
    *is_replacement* says whether an answer is the guardrail's refusal, alone
    or after a prefix and the stream separator.
    The pause between drains is the page's own (:meth:`Page.sleep`), never a
    bare sleep: the routed requests the page makes meanwhile are serviced only
    while the driver is inside a page call, so a Python sleep would freeze the
    page for its whole length.

    The turn is done when the composer's Stop button has been seen and is gone
    (the frontend renders it while the current message is not done), or —
    for a turn over before the first poll saw the button — when the page
    bound has passed since the URL flipped with a message on screen and no
    pulsing cursor in it. The regions read directly at the last drain become
    one more judged state when no frame showed them, so the end state is
    always judged.
    """

    started = monotonic()
    screenshot = out / f"{row_name}.png"
    watch = _Watch(judge, is_replacement)
    block_opened_at: float | None = None
    regions: Mapping[str, str] = {"block": "", "answer": ""}
    chat_id: str | None = None
    stop_seen = False
    done = False
    problem: Problem | None = None

    try:
        page.goto("/")
        page.watch()
        page.fill(CHAT_INPUT_SELECTOR, prompt)
        page.click(SEND_BUTTON_SELECTOR)
    except PageError:
        problem = Problem("the browser turn could not be started", LOGS_FIX)

    flipped_at = monotonic()
    if problem is None:
        try:
            flipped = page.wait_until(
                lambda: _chat_id(page.url()) is not None, max(0.0, deadline - monotonic())
            )
            if flipped:
                chat_id = _chat_id(page.url())
                flipped_at = monotonic()
            else:
                problem = Problem("the browser turn's URL never became a chat", LOGS_FIX)
        except PageError:
            problem = Problem("the browser turn's URL could not be read", LOGS_FIX)

    def drain_once() -> None:
        nonlocal regions
        drain = page.drain()
        regions = drain.regions
        watch.take_frames(drain.frames)

    if problem is None and chat_id is not None:
        while monotonic() < deadline:
            try:
                if block_opened_at is None and page.count(COLLAPSIBLE_BUTTON_SELECTOR) > 0:
                    if page.attribute(COLLAPSIBLE_BUTTON_SELECTOR, "aria-expanded") != "true":
                        page.click(COLLAPSIBLE_BUTTON_SELECTOR)
                    block_opened_at = monotonic()
                drain_once()
                if page.count(STOP_BUTTON_SELECTOR) > 0:
                    stop_seen = True
                elif stop_seen or _quiet_done(page, monotonic() - flipped_at, page_timeout):
                    done = True
                    break
            except PageError:
                problem = Problem("the browser turn could not be observed", LOGS_FIX)
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            if poll > 0:
                page.sleep(min(poll, remaining))

        try:
            drain_once()
            watch.take_regions(regions)
        except PageError:
            if problem is None:
                problem = Problem("the browser turn's last state could not be read", LOGS_FIX)
    watch.finish()

    try:
        page.screenshot(screenshot)
    except PageError:
        if problem is None:
            problem = Problem("the browser turn's screenshot could not be taken", LOGS_FIX)

    if problem is None:
        if page.count(MESSAGE_SELECTOR) == 0:
            problem = Problem("the browser page never showed a message", LOGS_FIX)
        elif not done:
            problem = Problem("the browser turn timed out", LOGS_FIX)
    return BrowserTurn(
        chat_id=chat_id,
        entries=tuple(watch.entries),
        texts=dict(watch.texts),
        block_opened_at=block_opened_at,
        screenshot=screenshot,
        regions=regions,
        elapsed=monotonic() - started,
        problem=problem,
    )

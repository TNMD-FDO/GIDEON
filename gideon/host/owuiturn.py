"""Shared managed turns for the pinned Open WebUI frontend.

The recipe relies on the pinned routes' facts that the managed completion
returns ``null`` and persists the turn (§1.5), the chat list is the way to
learn the created chat id (§2), the stored history contains the outlet's
edits (§3), deletion reports a boolean (§4), omitting the background-task
key avoids follow-up work (§5.1), and the session token is not endpoint-
allowlisted (§7.2) in ``docs/research/owui-chat-routes-machine-caller.md``.
Its three callers are ``tools.turns.run.ApiTurnDriver``, ``engine verify``'s
frontend case, and ``tests/contract/search_sentinel.py``. Each identifies its
own turn's chat through ``find_turn_chat`` — the one chat, among those new
since the turn, whose stored ``history.messages`` holds both minted ids — and
deletes that chat alone, so two sessions signed in as the one eval identity at
once neither read nor delete each other's records (slice-1 ticket 68).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import quote

from gideon.host import stack
from gideon.host.owui import Client, OwuiError, Response
from gideon.host.report import Problem

_RENDERED_DIR: Final = "/etc/gideon/rendered"
# Omitting ``page`` leaves the pinned frontend's chat list unpaged (the route
# note's §2.2), so the listing difference is the whole candidate set.
_CHAT_LIST_PATH: Final = "/api/v1/chats/list"
_CHAT_PATH: Final = "/api/v1/chats/"
COMPLETIONS_PATH: Final = "/api/chat/completions"
# A chat the caller cannot read answers 401 with the not-found detail, the one
# status the pinned route gives whether the chat never existed or belongs to
# another account (the note's §3.1); 404 is admitted as the status that detail
# names elsewhere. The body's wording is the frontend's own and is not read.
_NOT_FOUND_STATUSES: Final = (401, 404)
_DELETE_FIX: Final = "Delete the chat through the frontend, then retry."
_FIND_FIX: Final = (
    "List the account's chats through the frontend and delete the turn's chat "
    "by its prompt tag, then retry."
)
# The frontend's own logs are the next step when a turn or a record is not
# what the pinned routes promise (docs/research/owui-chat-routes-machine-caller.md).
LOGS_FIX: Final = stack.logs_fix(_RENDERED_DIR, "open-webui")


@dataclass(frozen=True, slots=True)
class StoredTurn:
    """The stored user and assistant messages and any record problem."""

    assistant: Mapping[str, object] | None = None
    user: Mapping[str, object] | None = None
    problem: Problem | None = None


@dataclass(frozen=True, slots=True)
class TurnChat:
    """The chat identified for a turn, its stored messages, or a refusal.

    ``chat_id`` and ``stored`` are both set, or both ``None`` with ``problem``
    carrying the refusal; ``candidates`` counts the chats new since the turn
    either way.
    """

    chat_id: str | None
    stored: StoredTurn | None
    candidates: int
    problem: Problem | None

    def refusal(self) -> Problem:
        """Why no chat was identified, for the caller's row or assertion."""

        return self.problem or Problem(
            "the turn's chat was not identified.", _FIND_FIX
        )


def _invalid(path: str) -> OwuiError:
    return OwuiError(f"Open WebUI returned an invalid response for {path}.")


def _chat_path(chat_id: str) -> str:
    return f"{_CHAT_PATH}{quote(chat_id, safe='')}"


def _decoded(path: str, response: Response) -> object | None:
    """The response's body, or a refusal naming the status that denied it."""

    if response.status < 200 or response.status >= 300:
        raise OwuiError(f"Open WebUI {path} returned HTTP {response.status}.")
    return response.body


def _json(client: Client, method: str, path: str) -> object | None:
    """One request whose success is its status; the decoded body, or a refusal."""

    return _decoded(path, client.request(method, path))


def signin(
    client_factory: Callable[..., Client], email: str, password: str
) -> str:
    """Sign in with a credential-free client and return its session token."""

    return client_factory().signin(email, password)


def chat_ids(client: Client) -> frozenset[str]:
    """Return the signed-in account's unpaged chat ids."""

    body = _json(client, "GET", _CHAT_LIST_PATH)
    if not isinstance(body, list):
        raise _invalid(_CHAT_LIST_PATH)
    identifiers: set[str] = set()
    for value in body:
        if not isinstance(value, Mapping):
            raise _invalid(_CHAT_LIST_PATH)
        identifier = value.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise _invalid(_CHAT_LIST_PATH)
        identifiers.add(identifier)
    return frozenset(identifiers)


def managed_turn(
    client: Client,
    *,
    model: str,
    prompt: str,
    user_id: str,
    assistant_id: str,
    timestamp: int,
    features: Mapping[str, bool] | None = None,
) -> Problem | None:
    """Submit one managed turn: the frontend creates the chat and answers ``null``.

    The body is the browser's new-chat form (ticket 05's recipe): ``parent_id``
    null, the ``user_message`` with the two ids the caller mints, no
    ``session_id`` (that selects the background fan-out branch) and no
    ``background_tasks`` (absent, no title or tag task runs — one engine call).
    ``features`` is the machine-caller mapping read by the completions route;
    when supplied it is carried under the top-level ``features`` key
    (the note's §5.3).  When absent, the body retains the existing shape used
    by the API driver and ``engine verify``'s frontend case.
    """

    body = {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
        "parent_id": None,
        "user_message": {
            "id": user_id,
            "role": "user",
            "content": prompt,
            "parentId": None,
            "childrenIds": [assistant_id],
            "timestamp": timestamp,
            "models": [model],
        },
        "id": assistant_id,
    }
    if features is not None:
        body["features"] = dict(features)
    try:
        response = client.request("POST", COMPLETIONS_PATH, body)
    except OwuiError as exc:
        return Problem(exc.problem, LOGS_FIX)
    if response.status != 200:
        return Problem(
            f"Open WebUI {COMPLETIONS_PATH} returned HTTP {response.status}.",
            LOGS_FIX,
        )
    if response.body is not None:
        return Problem(
            "Open WebUI managed turn returned a non-null response body.",
            LOGS_FIX,
        )
    return None


def _record(
    path: str, body: object | None
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """The ``history`` and ``history.messages`` of one decoded chat record."""

    if not isinstance(body, Mapping):
        raise _invalid(path)
    chat = body.get("chat")
    if not isinstance(chat, Mapping):
        raise _invalid(path)
    history = chat.get("history")
    if not isinstance(history, Mapping):
        raise _invalid(path)
    messages = history.get("messages")
    if not isinstance(messages, Mapping):
        raise _invalid(path)
    return history, messages


def _history(
    client: Client, chat_id: str
) -> tuple[str, Mapping[str, object], Mapping[str, object]]:
    """Read a chat's record and return its path, ``history``, and ``history.messages``."""

    path = _chat_path(chat_id)
    history, messages = _record(path, _json(client, "GET", path))
    return path, history, messages


def _candidate_messages(client: Client, chat_id: str) -> Mapping[str, object] | None:
    """One candidate's ``history.messages``, or ``None`` when it is gone.

    A not-found status is the skip: between the listing and this read another
    session deleted the chat it made, and the route reports that the same way
    it reports a chat this account never owned.
    """

    path = _chat_path(chat_id)
    response = client.request("GET", path)
    if response.status in _NOT_FOUND_STATUSES:
        return None
    _history_value, messages = _record(path, _decoded(path, response))
    return messages


def _stored_messages(
    messages: Mapping[str, object],
    *,
    user_id: str,
    assistant_id: str,
) -> StoredTurn:
    """Apply the finished-and-error-free rules to two message ids in one record."""

    raw_assistant = messages.get(assistant_id)
    raw_user = messages.get(user_id)
    assistant = (
        cast(Mapping[str, object], raw_assistant)
        if isinstance(raw_assistant, Mapping)
        else None
    )
    user = cast(Mapping[str, object], raw_user) if isinstance(raw_user, Mapping) else None
    if assistant is None:
        return StoredTurn(
            assistant=None,
            user=user,
            problem=Problem(
                f"assistant message {assistant_id} is missing from the chat record.",
                LOGS_FIX,
            ),
        )
    if "error" in assistant:
        return StoredTurn(
            assistant=assistant,
            user=user,
            problem=Problem(
                f"assistant message {assistant_id} carries an error.",
                LOGS_FIX,
            ),
        )
    if assistant.get("done") is not True:
        return StoredTurn(
            assistant=assistant,
            user=user,
            problem=Problem(
                f"assistant message {assistant_id} is unfinished.",
                LOGS_FIX,
            ),
        )
    return StoredTurn(assistant=assistant, user=user)


def find_turn_chat(
    client: Client,
    *,
    ids_before: frozenset[str],
    user_id: str,
    assistant_id: str,
) -> TurnChat:
    """Identify a turn's own chat among those new since it, by its minted ids.

    The listing difference is the candidate filter — the pinned frontend
    answers no route for the chat holding a message id (the note's §2) — and
    the read by id is the proof. Every candidate is read, so a second match is
    a refusal rather than an arbitrary first one, and a candidate another
    session has deleted since the listing is skipped, not raised.
    """

    candidate_ids = sorted(chat_ids(client) - ids_before)
    matches: list[tuple[str, StoredTurn]] = []
    vanished = 0
    for chat_id in candidate_ids:
        messages = _candidate_messages(client, chat_id)
        if messages is None:
            vanished += 1
            continue
        if user_id not in messages or assistant_id not in messages:
            continue
        stored = _stored_messages(
            messages, user_id=user_id, assistant_id=assistant_id
        )
        matches.append((chat_id, stored))
    if len(matches) == 1:
        matched_id, stored = matches[0]
        return TurnChat(matched_id, stored, len(candidate_ids), None)
    if not matches:
        return TurnChat(
            None,
            None,
            len(candidate_ids),
            Problem(
                f"the turn's chat was not found; {len(candidate_ids)} new chats, "
                f"{vanished} of them gone.",
                _FIND_FIX,
            ),
        )
    return TurnChat(
        None,
        None,
        len(candidate_ids),
        Problem(
            f"the turn's ids are in {len(matches)} of {len(candidate_ids)} new chats.",
            _FIND_FIX,
        ),
    )


def stored_current(client: Client, chat_id: str) -> StoredTurn:
    """Read the current assistant message and its parent user message.

    The browser mints both ids, so the record's ``history.currentId`` names the
    assistant message and its ``parentId`` the user message it answers.
    """

    path, history, messages = _history(client, chat_id)
    current_id = history.get("currentId")
    if not isinstance(current_id, str) or not current_id:
        raise _invalid(path)
    raw_assistant = messages.get(current_id)
    if not isinstance(raw_assistant, Mapping):
        raise _invalid(path)
    parent_id = raw_assistant.get("parentId")
    if not isinstance(parent_id, str) or not parent_id:
        raise _invalid(path)
    return _stored_messages(messages, user_id=parent_id, assistant_id=current_id)


def delete_chat(client: Client, chat_id: str) -> Problem | None:
    """Delete one chat and require Open WebUI's boolean success response."""

    path = _chat_path(chat_id)
    try:
        response = client.request("DELETE", path)
    except OwuiError as exc:
        return Problem(exc.problem, LOGS_FIX)
    if response.status != 200:
        return Problem(
            f"Open WebUI {path} returned HTTP {response.status} while deleting.",
            LOGS_FIX,
        )
    if response.body is not True:
        return Problem("Open WebUI did not confirm chat deletion.", _DELETE_FIX)
    return None

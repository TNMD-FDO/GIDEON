"""Office-wide chat, upload, and feedback retention through Open WebUI.

Selection finishes before deletion. Each owner's chats go before their
uploads, with surviving chats and knowledge bases protecting shared files.
Audit writes carry identifiers and counts alone.
"""

import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal
from urllib.parse import quote

from gideon.host import audit, owui, owuiturn, report, secrets, site, stack, tls
from gideon.host.render.owui import BREAK_GLASS, FEEDBACK_LIST_ROUTE
from gideon.host.report import Problem, StageResult
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final[str] = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final[str] = "/etc/gideon/rendered"
_AUDIT_FIX: Final[str] = stack.logs_fix(_RENDERED_DIR, "postgres")
_FRONTEND_FIX: Final[str] = stack.logs_fix(_RENDERED_DIR, "open-webui")
# Exempt operational constant: the reboot catch-up wait, in attempts of 5 s.
_READY_ATTEMPTS: Final[int] = 12
# The admin chat listing's fixed page size.
_CHAT_PAGE_SIZE: Final[int] = 60
RETENTION_DAYS: Final[Mapping[str, int]] = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "60d": 60,
    "90d": 90,
    "180d": 180,
    "1y": 365,
}
# An audit row keeps a feedback row's chat reference only when it is id-shaped.
_CHAT_ID_SHAPE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True, slots=True)
class SweepReport:
    """The run's counts and the first content-free problem of each kind."""

    accounts: int = 0
    chats_read: int = 0
    candidates: int = 0
    chats_swept: int = 0
    chats_vanished: int = 0
    chats_resumed: int = 0
    chats_failed: int = 0
    files_named: int = 0
    files_deleted: int = 0
    files_gone: int = 0
    files_kept: int = 0
    files_failed: int = 0
    feedback_read: int = 0
    feedback_by_age: int = 0
    feedback_with_chat: int = 0
    feedback_failed: int = 0
    audit_written: int = 0
    chats_problem: Problem | None = None
    files_problem: Problem | None = None
    feedback_problem: Problem | None = None
    audit_problem: Problem | None = None


@dataclass(frozen=True, slots=True)
class SweptChat:
    """A deleted chat and the file outcomes and feedback ids swept with it."""

    owner_id: str
    chat_id: str
    files: tuple[tuple[str, str], ...]
    feedback: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ListedChat:
    chat_id: str
    expired: bool


@dataclass(frozen=True, slots=True)
class _Feedback:
    feedback_id: str
    user_id: str
    chat_id: str | None
    expired: bool


def cutoff(clock_now: float, value: str) -> int:
    """The epoch-second boundary for an office retention interval."""

    if value not in RETENTION_DAYS:
        raise ValueError("retention.chats in the site file is invalid.")
    return int(clock_now) - RETENTION_DAYS[value] * 24 * 60 * 60


def _root_fix() -> str:
    return f"Run {report.command('retention sweep', sudo=False)} as root, for example with sudo."


def _password_fix() -> str:
    return f"Run {report.command('apply')}, then retry"


def _refuse(problem: str, fix: str) -> int:
    print(report.refusal("retention sweep", problem, fix), file=sys.stderr)
    return 1


def _invalid() -> owui.OwuiError:
    return owui.OwuiError("Open WebUI returned an invalid retention response.", _FRONTEND_FIX)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\r\n\x00"):
        raise _invalid()
    return value


def _expired(item: Mapping[str, object], boundary: int) -> bool:
    updated_at = item.get("updated_at")
    if isinstance(updated_at, bool) or not isinstance(updated_at, int):
        raise _invalid()
    return updated_at < boundary


def _body(response: owui.Response) -> object:
    if response.status != 200:
        raise owui.OwuiError(
            f"Open WebUI retention request returned HTTP {response.status}.", _FRONTEND_FIX
        )
    return response.body


@dataclass(frozen=True, slots=True)
class _ChatRecord:
    expired: bool
    files: tuple[str, ...]


def _chat_record(client: owui.Client, chat_id: str, boundary: int) -> _ChatRecord | None:
    """A chat's own expiry and file ids, read by id; None once it is gone."""

    response = client.request("GET", f"/api/v1/chats/{quote(chat_id, safe='')}")
    if response.status in owuiturn.NOT_FOUND_STATUSES:
        return None
    body = _body(response)
    if not isinstance(body, Mapping):
        raise _invalid()
    # The listing may be minutes old: a chat resumed since is judged afresh.
    expired = _expired(body, boundary)
    chat = body.get("chat")
    if not isinstance(chat, Mapping):
        raise _invalid()
    sources: list[object] = [chat.get("files")]
    history = chat.get("history")
    if history is not None:
        if not isinstance(history, Mapping):
            raise _invalid()
        messages = history.get("messages", {})
        if not isinstance(messages, Mapping):
            raise _invalid()
        for message in messages.values():
            if not isinstance(message, Mapping):
                raise _invalid()
            sources.append(message.get("files"))
    identifiers: set[str] = set()
    for source in sources:
        if source is None:
            continue
        if not isinstance(source, list):
            raise _invalid()
        for item in source:
            if not isinstance(item, Mapping) or item.get("type") != "file":
                continue
            identifier = item.get("id")
            if isinstance(identifier, str) and identifier:
                identifiers.add(_identifier(identifier))
    return _ChatRecord(expired, tuple(sorted(identifiers)))


def _file_detail(identifier: str, outcome: str) -> Mapping[str, object]:
    result: dict[str, object] = {"id": identifier, "outcome": outcome}
    if outcome.startswith("kept:"):
        result.update(outcome="kept", reason=outcome.partition(":")[2])
    return result


class _Sweep:
    """One selected run, retaining identifiers rather than chat records."""

    def __init__(
        self, client: owui.Client, io: Host, rendered_dir: PathLike, boundary: int
    ) -> None:
        self.client = client
        self.io = io
        self.rendered_dir = rendered_dir
        self.boundary = boundary
        self.summary = SweepReport()
        self.run_id = str(uuid.uuid4())
        self.actor_user_id: str | None = None
        self.chats: dict[str, tuple[_ListedChat, ...]] = {}
        self.feedback: tuple[_Feedback, ...] = ()
        self.knowledge_files: set[str] = set()
        self.chats_complete = False
        self.knowledge_complete = False

    def fail(
        self, stage: Literal["chats", "files", "feedback", "audit"], problem: str
    ) -> None:
        failure = Problem(problem, _AUDIT_FIX if stage == "audit" else _FRONTEND_FIX)
        if stage == "chats":
            self.summary = replace(
                self.summary, chats_failed=self.summary.chats_failed + 1,
                chats_problem=self.summary.chats_problem or failure,
            )
        elif stage == "files":
            self.summary = replace(
                self.summary, files_failed=self.summary.files_failed + 1,
                files_problem=self.summary.files_problem or failure,
            )
        elif stage == "feedback":
            self.summary = replace(
                self.summary, feedback_failed=self.summary.feedback_failed + 1,
                feedback_problem=self.summary.feedback_problem or failure,
            )
        else:
            self.summary = replace(self.summary, audit_problem=self.summary.audit_problem or failure)

    def select(self) -> None:
        try:
            users = self.client.users_all()
            self.summary = replace(self.summary, accounts=len(users))
            self.actor_user_id = next(
                (
                    _identifier(user.id) for user in users
                    if user.email.casefold() == BREAK_GLASS.email.casefold()
                ),
                None,
            )
            if self.actor_user_id is None:
                raise _invalid()
            for user in users:
                self.chats[_identifier(user.id)] = self.list_chats(user.id)
            self.chats_complete = True
        except (owui.OwuiError, OSError):
            self.fail("chats", "Open WebUI accounts or chat listing could not be read.")

        try:
            for kb in self.client.knowledge_all():
                for item in self.client.paged_items(f"/api/v1/knowledge/{quote(kb.id, safe='')}/files"):
                    self.knowledge_files.add(_identifier(item.get("id")))
            self.knowledge_complete = True
        except (owui.OwuiError, OSError):
            self.fail("files", "Open WebUI knowledge file listing could not be read.")

        try:
            items = self.client.paged_items(FEEDBACK_LIST_ROUTE)
            self.summary = replace(self.summary, feedback_read=len(items))
            feedback: dict[str, _Feedback] = {}
            for item in items:
                identifier = _identifier(item.get("id"))
                meta = item.get("meta")
                chat_id = meta.get("chat_id") if isinstance(meta, Mapping) else None
                feedback[identifier] = _Feedback(
                    identifier,
                    _identifier(item.get("user_id")),
                    chat_id if isinstance(chat_id, str) else None,
                    _expired(item, self.boundary),
                )
            self.feedback = tuple(feedback.values())
        except (owui.OwuiError, OSError):
            self.fail("feedback", "Open WebUI feedback listing could not be read.")

    def list_chats(self, user_id: str) -> tuple[_ListedChat, ...]:
        chats: dict[str, _ListedChat] = {}
        page = 1
        while True:
            body = _body(self.client.request(
                "GET", f"/api/v1/chats/list/user/{quote(user_id, safe='')}?page={page}"
            ))
            if not isinstance(body, list):
                raise _invalid()
            self.summary = replace(self.summary, chats_read=self.summary.chats_read + len(body))
            for item in body:
                if not isinstance(item, Mapping):
                    raise _invalid()
                chat_id = _identifier(item.get("id"))
                chats[chat_id] = _ListedChat(chat_id, _expired(item, self.boundary))
            if len(body) < _CHAT_PAGE_SIZE:
                self.summary = replace(
                    self.summary,
                    candidates=self.summary.candidates + sum(chat.expired for chat in chats.values()),
                )
                return tuple(chats.values())
            page += 1

    def row(
        self, kind: str, *, user_id: str | None = None, chat_id: str | None = None,
        detail: Mapping[str, object],
    ) -> audit.AuditRow:
        return audit.AuditRow(
            run_id=self.run_id, kind=kind, actor_user_id=self.actor_user_id,
            user_id=user_id, chat_id=chat_id, kb_ids=(), detail=detail,
        )

    def write(self, rows: tuple[audit.AuditRow, ...]) -> bool:
        if not rows:
            return True
        problem = audit.write_rows(self.io, self.rendered_dir, rows)
        if problem is not None:
            self.fail("audit", "Retention audit write failed.")
            return False
        self.summary = replace(self.summary, audit_written=self.summary.audit_written + len(rows))
        return True

    def apply(self, interval: str) -> None:
        if self.actor_user_id is None:
            self.fail("audit", "Retention audit writer has no administrator id.")
            return
        if not self.chats_complete:
            return
        candidate_owners = {
            chat.chat_id: owner_id
            for owner_id, chats in self.chats.items() for chat in chats if chat.expired
        }
        feedback_candidates = sum(
            item.expired or item.chat_id in candidate_owners for item in self.feedback
        )
        # The insert, rather than the reachability probe, must succeed before
        # any delete. Applied rows follow each finished account's work.
        if not self.write((self.row("retention_sweep", detail={
            "interval": interval,
            "cutoff": self.boundary,
            "accounts": self.summary.accounts,
            "chats_read": self.summary.chats_read,
            "candidates": self.summary.candidates,
            "feedback_read": self.summary.feedback_read,
            "feedback_candidates": feedback_candidates,
        }),)):
            return

        feedback_by_owner: dict[str, list[_Feedback]] = {owner_id: [] for owner_id in self.chats}
        # A rater may belong to another account. A candidate chat's feedback
        # waits for that chat's outcome so it is attributed to the right row.
        for item in self.feedback:
            owner_id = candidate_owners.get(item.chat_id or "", item.user_id)
            feedback_by_owner.setdefault(owner_id, []).append(item)
        for owner_id, feedback in feedback_by_owner.items():
            swept = self.sweep_chats(owner_id)
            feedback_ids, age_rows = self.sweep_feedback(feedback, {chat.chat_id for chat in swept})
            swept = tuple(replace(chat, feedback=tuple(feedback_ids.get(chat.chat_id, ()))) for chat in swept)
            self.write(tuple(
                self.row(
                    "chat_swept", user_id=chat.owner_id, chat_id=chat.chat_id,
                    detail={
                        "files": [_file_detail(identifier, outcome) for identifier, outcome in chat.files],
                        "feedback": list(chat.feedback),
                    },
                ) for chat in swept
            ) + age_rows)

    def sweep_chats(self, owner_id: str) -> tuple[SweptChat, ...]:
        discovered = self.discover(self.chats.get(owner_id, ()))
        if discovered is None:
            return ()
        candidates, protected = discovered
        swept: dict[str, tuple[str, ...]] = {}
        for chat_id, files in candidates.items():
            try:
                problem = owuiturn.delete_chat(self.client, chat_id)
            except (owui.OwuiError, OSError):
                problem = Problem("Open WebUI chat deletion failed.", _FRONTEND_FIX)
            if problem is not None:
                protected.update(files)
                self.fail("chats", "Open WebUI did not confirm chat deletion.")
                continue
            swept[chat_id] = files
            self.summary = replace(self.summary, chats_swept=self.summary.chats_swept + 1)

        targets = sorted({identifier for files in swept.values() for identifier in files})
        self.summary = replace(self.summary, files_named=self.summary.files_named + len(targets))
        outcomes = {identifier: self.sweep_file(owner_id, identifier, protected) for identifier in targets}
        return tuple(
            SweptChat(owner_id, chat_id, tuple((identifier, outcomes[identifier]) for identifier in files), ())
            for chat_id, files in swept.items()
        )

    def discover(
        self, chats: tuple[_ListedChat, ...]
    ) -> tuple[dict[str, tuple[str, ...]], set[str]] | None:
        """An owner's deletable chats with their file ids, and the ids its other chats name.

        None leaves every chat of the owner for the next run: a chat deleted
        before its files' references are known would strand those files,
        since no later listing names that chat again.
        """

        candidates: dict[str, tuple[str, ...]] = {}
        protected: set[str] = set()
        complete = True
        for chat in chats:
            if not chat.expired:
                continue
            try:
                record = _chat_record(self.client, chat.chat_id, self.boundary)
            except (owui.OwuiError, OSError):
                complete = False
                self.fail("chats", "Open WebUI candidate chat could not be read.")
                continue
            if record is None:
                self.summary = replace(self.summary, chats_vanished=self.summary.chats_vanished + 1)
            elif not record.expired:
                protected.update(record.files)
                self.summary = replace(self.summary, chats_resumed=self.summary.chats_resumed + 1)
            else:
                candidates[chat.chat_id] = record.files
        if complete and any(candidates.values()):
            complete = self.knowledge_complete
            for chat in chats:
                if chat.expired or not complete:
                    continue
                try:
                    record = _chat_record(self.client, chat.chat_id, self.boundary)
                except (owui.OwuiError, OSError):
                    complete = False
                    self.fail("files", "Open WebUI surviving chat could not be read.")
                    continue
                if record is not None:
                    protected.update(record.files)
        if not complete:
            self.fail("chats", "An account's chats were left for the next run: their uploads' references could not be read.")
            return None
        return candidates, protected

    def sweep_file(self, owner_id: str, identifier: str, protected: set[str]) -> str:
        try:
            path = f"/api/v1/files/{quote(identifier, safe='')}"
            response = self.client.request("GET", path)
            if response.status == 404:
                self.summary = replace(self.summary, files_gone=self.summary.files_gone + 1)
                return "gone"
            body = _body(response)
            if not isinstance(body, Mapping):
                raise _invalid()
            file_owner = _identifier(body.get("user_id"))
            reason = None
            if file_owner != owner_id:
                reason = "other-owner"
            elif identifier in protected:
                reason = "still-named"
            elif identifier in self.knowledge_files:
                reason = "knowledge"
            if reason is not None:
                self.summary = replace(self.summary, files_kept=self.summary.files_kept + 1)
                return f"kept:{reason}"
            response = self.client.request("DELETE", path)
            if response.status == 404:
                self.summary = replace(self.summary, files_gone=self.summary.files_gone + 1)
                return "gone"
            _body(response)
            self.summary = replace(self.summary, files_deleted=self.summary.files_deleted + 1)
            return "deleted"
        except (owui.OwuiError, OSError):
            self.fail("files", "Open WebUI upload read or deletion failed.")
            return "failed"

    def sweep_feedback(
        self, feedback: list[_Feedback], swept_ids: set[str]
    ) -> tuple[dict[str, list[str]], tuple[audit.AuditRow, ...]]:
        with_chat: dict[str, list[str]] = {}
        age_rows: list[audit.AuditRow] = []
        for item in feedback:
            linked = item.chat_id in swept_ids
            if not linked and not item.expired:
                continue
            try:
                response = self.client.request(
                    "DELETE", f"/api/v1/evaluations/feedback/{quote(item.feedback_id, safe='')}"
                )
                if _body(response) is not True:
                    raise _invalid()
            except (owui.OwuiError, OSError):
                self.fail("feedback", "Open WebUI did not confirm feedback deletion.")
                continue
            if linked and item.chat_id is not None:
                with_chat.setdefault(item.chat_id, []).append(item.feedback_id)
                self.summary = replace(self.summary, feedback_with_chat=self.summary.feedback_with_chat + 1)
            else:
                chat_id = item.chat_id
                if chat_id is not None and _CHAT_ID_SHAPE.fullmatch(chat_id) is None:
                    chat_id = None
                age_rows.append(self.row(
                    "feedback_swept", user_id=item.user_id,
                    detail={"feedback_id": item.feedback_id, "chat_id": chat_id},
                ))
                self.summary = replace(self.summary, feedback_by_age=self.summary.feedback_by_age + 1)
        return with_chat, tuple(age_rows)


def _print_report(summary: SweepReport) -> int:
    rows = (
        ("chats", summary.chats_problem, (
            f"accounts={summary.accounts}, read={summary.chats_read}, candidates={summary.candidates}, "
            f"swept={summary.chats_swept}, vanished={summary.chats_vanished}, "
            f"resumed={summary.chats_resumed}, failed={summary.chats_failed}"
        )),
        ("files", summary.files_problem, (
            f"named={summary.files_named}, deleted={summary.files_deleted}, gone={summary.files_gone}, "
            f"kept={summary.files_kept}, failed={summary.files_failed}"
        )),
        ("feedback", summary.feedback_problem, (
            f"read={summary.feedback_read}, swept-by-age={summary.feedback_by_age}, "
            f"swept-with-chat={summary.feedback_with_chat}, failed={summary.feedback_failed}"
        )),
        ("audit", summary.audit_problem, f"written={summary.audit_written}"),
    )
    for name, problem, counts in rows:
        report.print_stage(StageResult(
            name, problem is None,
            counts if problem is None else f"{counts}; {problem.problem}",
            "" if problem is None else problem.fix,
        ))
    return int(any(problem is not None for _name, problem, _counts in rows))


def run_retention_sweep(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    client_factory: Callable[..., owui.Client] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    """Delete expired chats, their unshared uploads, and retired feedback."""

    io = host or RealHost()
    if io.geteuid() != 0:
        return _refuse("root is required.", _root_fix())
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        return _refuse(site.render_errors(loaded.errors), "Correct the site file, then retry.")
    config = loaded.config
    try:
        boundary = cutoff(clock(), config.retention.chats)
    except ValueError:
        return _refuse("retention.chats in the site file is invalid.", "Correct the site file, then retry.")

    password = secrets.read_secret(io, "gideon_admin_password")
    if not password.ok or password.value is None:
        if password.missing:
            return _refuse("the break-glass password is missing.", _password_fix())
        return _refuse(
            password.problem or "the break-glass password is unavailable.",
            password.fix or _password_fix(),
        )
    make_client = client_factory or owui.ingress_client_factory(config.hostname, ca_path=tls.CA_PATH)
    try:
        ready = owui.wait_ready(make_client(), attempts=_READY_ATTEMPTS, sleep=sleep)
    except (owui.OwuiError, OSError):
        return _refuse("Open WebUI readiness request failed.", _FRONTEND_FIX)
    if not ready.ok:
        return _refuse("Open WebUI is not ready.", _FRONTEND_FIX)
    audit_problem = audit.probe(io, rendered_dir)
    if audit_problem is not None:
        return _refuse(f"audit writer is unavailable: {audit_problem}", _AUDIT_FIX)

    try:
        token = owuiturn.signin(make_client, BREAK_GLASS.email, password.value)
        client = make_client(token=token)
    except (owui.OwuiError, OSError):
        report.print_stage(StageResult("signin", False, "Open WebUI sign-in failed.", _FRONTEND_FIX))
        return 1
    report.print_stage(StageResult("signin", True, "signed in as the break-glass administrator", ""))
    sweep = _Sweep(client, io, rendered_dir, boundary)
    sweep.select()
    sweep.apply(config.retention.chats)
    return _print_report(sweep.summary)

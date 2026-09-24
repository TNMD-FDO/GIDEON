"""The pinned Open WebUI's feedback source: the one module that knows its shape.

It reads the admin list route with the break-glass key, page by page, and
copies five values from each rating — nothing of a comment, a tag, a rater,
or a chat. The admin ``/feedbacks/list`` route returns 30 rows per page and
includes the rater's name and email; this reader copies only five values.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import owui, secrets, site, tls
from gideon.host.render.owui import FEEDBACK_LIST_ROUTE
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.improvement import sections
from gideon.improvement.feedback import (
    FeedbackReading,
    FeedbackRecord,
    FeedbackSource,
    Rating,
)

ADMIN_KEY_SECRET: Final[str] = "gideon_admin_api_key"
RATING_TYPE: Final[str] = "rating"
# The frontend's integers; a boolean is an int to Python and is refused.
RATING_VALUES: Final[Mapping[int, Rating]] = {1: "up", -1: "down"}
# The route answers 30 items a page; 100 pages is a starting bound.
FEEDBACK_PAGE_LIMIT: Final[int] = 100
_SITE_FIX: Final[str] = "Correct the site file, then retry."
_KEY_FIX: Final[str] = "Run sudo python3 -m gideon apply, which mints the key, then retry."
_FRONTEND_FIX: Final[str] = "Check Open WebUI availability, then retry."
_SHAPE_FIX: Final[str] = "Run sudo python3 -m gideon apply to converge the pinned frontend, then retry."
_BOUND_FIX: Final[str] = (
    "Raise FEEDBACK_PAGE_LIMIT in gideon/improvement/owuifeedback.py in a release, then retry."
)


def _host_fix(host: Host, fix: str) -> str:
    # The site file and the key may be root's alone, and proposals runs its
    # other sections without root; as root, the file's own fix stands.
    return sections.READ_FIX if host.geteuid() != 0 else fix


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def decode_rating(value: object) -> Rating | None:
    """Return the known direction for an integer rating, excluding booleans."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return RATING_VALUES.get(value)


def _record(item: object) -> FeedbackRecord | None:
    if not isinstance(item, Mapping) or item.get("type") != RATING_TYPE:
        return None
    data = item.get("data")
    meta = item.get("meta")
    if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
        return None
    value = data.get("rating")
    created_at = item.get("created_at")
    rating = decode_rating(value)
    if rating is None:
        return None
    if isinstance(created_at, bool) or not isinstance(created_at, int):
        return None
    chat_id = _text(meta.get("chat_id"))
    message_id = _text(meta.get("message_id"))
    model_id = _text(data.get("model_id"))
    if chat_id is None or message_id is None or model_id is None:
        return None
    return FeedbackRecord(rating, chat_id, message_id, model_id, created_at)


def decode_items(items: Sequence[object]) -> FeedbackReading:
    """Read each item as a rating record, counting every other item as skipped."""

    records = tuple(record for item in items if (record := _record(item)) is not None)
    return FeedbackReading(records, len(items) - len(records))


def _envelope(body: object) -> tuple[Sequence[object], int] | Problem:
    items = body.get("items") if isinstance(body, Mapping) else None
    total = body.get("total") if isinstance(body, Mapping) else None
    if not isinstance(items, list) or isinstance(total, bool) or not isinstance(total, int):
        return Problem(
            f"Open WebUI answered {FEEDBACK_LIST_ROUTE} without its items and total.",
            _SHAPE_FIX,
        )
    return items, total


def decode_page(body: object) -> FeedbackReading | Problem:
    """Decode one list response over the six fields a record is read from."""

    envelope = _envelope(body)
    if isinstance(envelope, Problem):
        return envelope
    return decode_items(envelope[0])


def _walk(client: owui.Client) -> FeedbackReading | Problem:
    # A rating created or updated mid-walk shifts the pages, so an item whose
    # frontend id was already listed is dropped and the walk runs on to the
    # total or an empty page.
    seen: set[str] = set()
    fresh: list[object] = []
    for page in range(1, FEEDBACK_PAGE_LIMIT + 1):
        response = client.request("GET", f"{FEEDBACK_LIST_ROUTE}?page={page}")
        if not 200 <= response.status < 300:
            return Problem(
                f"Open WebUI answered {FEEDBACK_LIST_ROUTE} with HTTP {response.status}.",
                _FRONTEND_FIX,
            )
        envelope = _envelope(response.body)
        if isinstance(envelope, Problem):
            return envelope
        items, total = envelope
        if not items:
            break
        for item in items:
            identifier = _text(item.get("id")) if isinstance(item, Mapping) else None
            if identifier is not None:
                if identifier in seen:
                    continue
                seen.add(identifier)
            fresh.append(item)
        if len(fresh) >= total:
            break
    else:
        return Problem(
            f"Open WebUI's feedback list ran past {FEEDBACK_PAGE_LIMIT} pages "
            f"({len(fresh)} items read).",
            _BOUND_FIX,
        )
    return decode_items(fresh)


def read(
    host: Host,
    site_path: PathLike,
    client_factory: Callable[..., owui.Client] | None = None,
) -> FeedbackReading | Problem:
    """Read every rating the frontend lists, over the ingress with the admin key."""

    loaded = site.load_site(Path(site_path), host=host)
    if loaded.errors or loaded.config is None:
        problem = site.render_errors(loaded.errors) or "site file could not be loaded."
        return Problem(problem, _host_fix(host, _SITE_FIX))

    secret = secrets.read_secret(host, ADMIN_KEY_SECRET)
    if not secret.ok or secret.value is None:
        if secret.missing:
            return Problem("the break-glass API key is missing.", _KEY_FIX)
        problem = secret.problem or "the break-glass API key is unavailable."
        return Problem(problem, _host_fix(host, secret.fix or _KEY_FIX))

    make_client = client_factory or owui.ingress_client_factory(
        loaded.config.hostname, ca_path=tls.CA_PATH
    )
    try:
        return _walk(make_client(api_key=secret.value))
    except owui.OwuiError as exc:
        return Problem(exc.problem, exc.fix or _FRONTEND_FIX)
    except OSError:
        return Problem(f"Open WebUI request failed for {FEEDBACK_LIST_ROUTE}.", _FRONTEND_FIX)


@dataclass(frozen=True, slots=True)
class _BoundSource:
    host: Host
    site_path: PathLike
    client_factory: Callable[..., owui.Client] | None

    def read(self) -> FeedbackReading | Problem:
        return read(self.host, self.site_path, self.client_factory)


def source(
    host: Host,
    site_path: PathLike,
    client_factory: Callable[..., owui.Client] | None = None,
) -> FeedbackSource:
    """Bind a read to the seam; nothing is read until ``read`` is called."""

    return _BoundSource(host, site_path, client_factory)

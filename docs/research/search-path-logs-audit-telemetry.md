---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
  - pin: images.searxng
    version: 2026.9.7-3e454637f
  - pin: images.caddy
    version: "2.11"
---

# Search-path logs, audit, and telemetry — verified facts

Scope: GIDEON slice-1 ticket 16 (`.scratch/slice-1/issues/16-no-search-query-text.md`); spec
§15's last sentence and greenfield issue [22] item 15. Serves a plan that must (1) fix the
frontend's log/audit/telemetry levels as rendered facts, (2) decide what to do about the two
failure-path log lines already found by `docs/research/searxng-service-and-owui-search.md`
(§5e, §11) that carry the search query at levels no global level removes, and (3) build a CI
search-sentinel test over a throwaway Compose stack (Postgres + Open WebUI + SearXNG + a stub
Chat Completions server wired as SearXNG's one engine through the generic `json_engine`) that
drives one search end to end and greps every log for a sentinel, including a run where
SearXNG's engine answers an HTTP error and one where the frontend's SearXNG call answers a
non-2xx.

**Sources and method.** Open WebUI: tag `v0.11.3`, commit `2a960a59fe1dbbd35282f0556b3666d81102e781`
(same commit the cited prior notes verified against); backend paths relative to
`backend/open_webui/`, frontend paths relative to `src/`. SearXNG: commit
`3e454637fb9829756c805dd9c02100f0bc9520fd` (the commit `docs/research/searxng-service-and-owui-search.md`
§1 already tied to the pinned image tag `2026.9.7-3e454637f`); paths relative to that repo's
root. granian: `2.8.2` (read from SearXNG's `requirements-server.txt` at the pinned commit;
source fetched from `emmett-framework/granian` at tag `v2.8.2`). curl_cffi: `0.16.1` (read
from SearXNG's `requirements.txt` at the pinned commit; not separately re-read from its own
source beyond the one class cited in Part B3, which the prior note already read). Caddy: the
lock names the tracking tag `2.11`; GitHub's tag list resolves the newest patch under that
minor series to `v2.11.4`, which is the tag Part C's source citations were read at — the lock's
own digest was not independently re-resolved against Docker Hub in this note, so if `2.11` had
already moved past `v2.11.4` by the pin's actual resolution date the citations below still
describe the same code (the cited struct/behavior has been stable across the 2.11 series) but
the exact patch tag is not guaranteed byte-identical. Docker and systemd: official docs and
systemd's own `NEWS` file, cited by URL/tag.

Every Open WebUI, SearXNG, and granian quote below with a `file:line` citation was fetched
from `raw.githubusercontent.com` at the exact commit/tag and, for the OWUI backend files,
downloaded byte-for-byte and line-numbered directly from that download. For the SearXNG,
granian, and one OWUI file (`utils/logger.py`) quoted at file:line, the fetch tool returned
the file's content through a text-processing step rather than a byte-identical download; each
such file was then written back out and re-numbered from that returned text before citing a
line number, so a citation for those files could be off by a line if that intermediate step
silently added or dropped a blank line — flagged once here rather than on every citation.
Caddy, Docker, and systemd facts are cited to docs/source by URL, no line numbers claimed.

---

## Part A — Open WebUI v0.11.3

### A1. `env.py`'s logging setup, and how each module gets its level

**There is no `SRC_LOG_LEVELS` table at this tag — it is a dead, empty legacy variable.**
`env.py:127`: `SRC_LOG_LEVELS = {}  # Legacy variable, do not remove`. Grepping every fetched
backend file (`env.py`, `main.py`, `config.py`, `routers/retrieval.py`,
`retrieval/web/searxng.py`, `retrieval/web/utils.py`, `retrieval/web/main.py`,
`utils/middleware.py`, `routers/tasks.py`, `utils/task.py`, `utils/session_pool.py`,
`utils/audit.py`) for `SRC_LOG_LEVELS` finds only that one definition and no reads of it
anywhere. **Every module in this codebase logs through a plain
`logging.getLogger(__name__)`, with no per-module level ever set on that logger object** —
confirmed by reading each of the eight modules named in the ticket's A1 list in full or at
every logging-related line (below); none calls `.setLevel(...)` on its own logger. There is
also no `RAG_LOG_LEVEL` (or any other per-source `*_LOG_LEVEL`) anywhere in `env.py` — the
only `_LOG_LEVEL`-suffixed name in the whole file is `GLOBAL_LOG_LEVEL` itself and
`AUDIT_LOG_LEVEL` (a *content*-detail enum, not a Python severity — see A6).

**`GLOBAL_LOG_LEVEL`, applied twice, by two different mechanisms, at two different times.**

1. At **import time** (`env.py:107-121`), before the app exists:
   ```python
   # env.py:107-121
   GLOBAL_LOG_LEVEL = os.getenv('GLOBAL_LOG_LEVEL', '').upper()
   if GLOBAL_LOG_LEVEL in logging.getLevelNamesMapping():
       _log_cfg: dict[str, Any] = {'level': GLOBAL_LOG_LEVEL, 'force': True}
       if LOG_FORMAT == 'json':
           _json_handler = logging.StreamHandler(sys.stdout)
           _json_handler.setFormatter(JSONFormatter())
           _log_cfg['handlers'] = [_json_handler]
       else:
           _log_cfg['stream'] = sys.stdout
       logging.basicConfig(**_log_cfg)
   else:
       GLOBAL_LOG_LEVEL = 'INFO'

   log = logging.getLogger(__name__)
   log.info('GLOBAL_LOG_LEVEL: %s', GLOBAL_LOG_LEVEL)
   ```
   Plain stdlib `logging.basicConfig` — sets the **root** logger's level and a stdout handler.
   Unset/invalid `GLOBAL_LOG_LEVEL` defaults to `INFO`.

2. At **app startup**, inside FastAPI's `lifespan`, `main.py` imports and calls
   `start_logger()` (`main.py:122` import, `main.py:343` call, inside the `lifespan` context
   manager — confirmed by search) from `utils/logger.py`, which **replaces** the above with a
   loguru pipeline (`utils/logger.py:168-223`, full function reconstructed and re-numbered
   locally, see the sources note):
   ```python
   # utils/logger.py:179-196 (start_logger)
   logger.remove()

   audit_filter = lambda record: True if ENABLE_AUDIT_STDOUT else 'auditable' not in record['extra']
   if LOG_FORMAT == 'json':
       logger.add(_json_sink, level=GLOBAL_LOG_LEVEL, filter=audit_filter, diagnose=LOGURU_DIAGNOSE)
   else:
       logger.add(sys.stdout, level=GLOBAL_LOG_LEVEL, format=stdout_format, filter=audit_filter, diagnose=LOGURU_DIAGNOSE)
   ```
   ```python
   # utils/logger.py:211
   logging.basicConfig(handlers=[InterceptHandler()], level=GLOBAL_LOG_LEVEL, force=True)
   ```
   `InterceptHandler.emit` (`utils/logger.py:99-123`) forwards every stdlib `logging` call
   (i.e. every `log.debug/info/warning/error/exception` in the modules below) into loguru's
   `logger`, which then applies its **own** `GLOBAL_LOG_LEVEL` threshold a second time via the
   `logger.add(..., level=GLOBAL_LOG_LEVEL, ...)` sink above. **Net effect: one knob,
   `GLOBAL_LOG_LEVEL`, gates every stdlib logger in the process, enforced twice (root logger
   level, then loguru sink level) but never per-module**, because no module's own
   `logging.Logger` object ever has an explicit level set — its *effective* level is always
   inherited from the root, which `start_logger()` pins to `GLOBAL_LOG_LEVEL`.

**Per-module logger source names** (all `logging.getLogger(__name__)`, so the source name is
the dotted module path — none of these files rename their logger or call `.setLevel`):
- `routers/retrieval.py:` `log = logging.getLogger(__name__)` → source `open_webui.routers.retrieval`.
- `retrieval/web/searxng.py:` same pattern → `open_webui.retrieval.web.searxng`.
- `retrieval/web/utils.py:68` `log = logging.getLogger(__name__)` → `open_webui.retrieval.web.utils`.
- `retrieval/web/main.py:` same pattern → `open_webui.retrieval.web.main`.
- `utils/middleware.py:` `log = logging.getLogger(__name__)` → `open_webui.utils.middleware`.
- `routers/tasks.py:37` `log = logging.getLogger(__name__)` → `open_webui.routers.tasks`.
- `utils/task.py:` same pattern → `open_webui.utils.task`.
- `utils/session_pool.py:39` `log = logging.getLogger(__name__)` → `open_webui.utils.session_pool`.
- `main.py:` uses the module-level `log`/`logger` from `env.py`'s own `logging.getLogger(__name__)`
  (`env.py:120`) → `open_webui.env`, plus its own `logging.getLogger(__name__)` for `main.py`
  itself → `open_webui.main`.

None of these ever calls `logging.getLogger(...).setLevel(...)` — confirmed by reading every
line quoted in A2 below plus the full text of `retrieval/web/searxng.py`, `utils/session_pool.py`,
and `routers/tasks.py`.

### A2. Every log call on the search path that could carry the prompt, queries, a query-bearing URL, or a fetched-page URL

| file:line | source | level | carries |
|---|---|---|---|
| `routers/tasks.py:420` | `open_webui.routers.tasks` | **INFO** | `log.info('Reusing cached queries: %s', request.state.cached_queries)` — **the generated search queries themselves**, verbatim, at INFO. Only reachable when `ENABLE_QUERIES_CACHE=true` (env.py:366, default `False`) causes a second `generate_queries` call within the same HTTP request to hit the cache middleware.py sets at `middleware.py:1557-1558`. See the flag note below. |
| `routers/tasks.py:440` | `open_webui.routers.tasks` | DEBUG | `log.debug('generating %s queries using model %s for user %s', type, task_model_id, user.email)` — type/model/user only, no query text. |
| `routers/retrieval.py:2818` | `open_webui.routers.retrieval` | DEBUG | `logging.debug('trying to web search with %s', (config.WEB_SEARCH_ENGINE, form_data.queries))` — the raw generated queries. |
| `routers/retrieval.py:2861` | `open_webui.routers.retrieval` | DEBUG | `log.debug('urls: %s', urls)` — every fetched-page URL (search-result links). |
| `retrieval/web/searxng.py:63` | `open_webui.retrieval.web.searxng` | DEBUG | `log.debug('searching %s', query_url)` — the bare SearXNG endpoint (already stripped of any `?...`, per `docs/research/searxng-service-and-owui-search.md` §8), **not** the built request with `q=`. |
| `routers/retrieval.py:2864` | `open_webui.routers.retrieval` | **ERROR** | `log.exception('Web search failed')` inside `process_web_search`'s `except Exception as e:` around the `search_web(...)` gather — traceback text ends in the underlying exception's `str()`, which for a failed SearXNG call is aiohttp's `ClientResponseError` carrying the full request URL with `q=` (already established by the cited note, §11; re-confirmed here, see A3). |
| `routers/retrieval.py:2960` | `open_webui.routers.retrieval` | **ERROR** | `log.exception('Web search content loading failed')` — the page-loader's own outer exception handler; see A3 for exactly what can reach it. |
| `utils/middleware.py:1561` | `open_webui.utils.middleware` | ERROR | `log.exception(e)` inside `chat_web_search_handler`'s `generate_queries` try-block — `e` here is `Exception(detail)` built from the query-generation task's own JSON-error `detail` (see A2's "task model reply" note below); this is a query-*generation* failure (before any web request happens), not a web-search-request failure, so it would not by itself carry a URL. |
| **`utils/middleware.py:1655`** | `open_webui.utils.middleware` | **ERROR** | `log.exception(e)` inside `chat_web_search_handler`'s outer `except Exception as e:` around the `process_web_search(...)` call — **a new finding, not in the cited prior note**: `e` here is the `HTTPException` that `routers/retrieval.py:2865` (or `:2961`) raised. That `raise HTTPException(...)` is not written with `from e` or `from None`, so Python's implicit exception chaining leaves the original exception (e.g. aiohttp's query-bearing `ClientResponseError`) attached as `__context__`. `logging.Logger.exception()`'s traceback formatter (`traceback.format_exception`) renders chained exceptions by default, so **this second, differently-named logger source (`open_webui.utils.middleware`, not `open_webui.routers.retrieval`) also prints the full query-bearing URL in its traceback text, at ERROR**, for the same failure. A sentinel grep over "every log" therefore needs no special handling for this — it is still ERROR, in the same container's stdout stream — but a plan that tried to silence only `open_webui.routers.retrieval` (which A4 shows is not even possible) would still miss this second site. |

**Does anything at INFO or above carry the prompt/queries/a URL on the success path?**
Yes — `routers/tasks.py:420`'s `log.info('Reusing cached queries: %s', ...)`, but only when
`ENABLE_QUERIES_CACHE=true` (default `False`, `env.py:366`) *and* `generate_queries` is called
a second time within the same incoming HTTP request (the cache lives on `request.state`,
per-request, set at `utils/middleware.py:1557-1558`: `if ENABLE_QUERIES_CACHE:
request.state.cached_queries = queries`). Whether GIDEON's own turn shape ever calls
`generate_queries` twice in one request (e.g. once for `web_search`, once for a
`retrieval`-type query rewrite in the same turn) was not traced further — GIDEON's rendered
config disables retrieval/embedding (`BYPASS_EMBEDDING_AND_RETRIEVAL`, no knowledge bases),
which makes a same-request second call unlikely but not something this note rules out from
source. **This flag being left at its default (`False`) is itself a fact worth pinning by
name, since turning it on would open an INFO-level path with no exception required at all.**

**What `chat_web_search_handler` logs when `process_web_search` raises**: `log.exception(e)`
at `utils/middleware.py:1655` (ERROR — see above), and separately it emits a **fixed, generic**
status message to the chat UI via `event_emitter` (`utils/middleware.py:1656-1668`):
```python
# utils/middleware.py:1656-1668
detail = e.detail if isinstance(e, HTTPException) else None
await event_emitter({'type': 'status', 'data': {'action': 'web_search',
    'description': (str(detail) if detail else 'An error occurred while searching the web'),
    'queries': queries, 'done': True, 'error': True}})
```
`e.detail` here is **not** the raw exception text — it is
`ERROR_MESSAGES.DEFAULT(e, ERROR_MESSAGES.WEB_SEARCH_ERROR)` from
`routers/retrieval.py:2867`/`:2963`. `ERROR_MESSAGES.DEFAULT` is `constants.py`'s
`_error_message` function (line number not independently pinned — fetched via a
text-processing step, not byte-for-byte, and not re-verified against a local line-numbered
copy):
```python
def _error_message(err='', fallback='') -> str:
    if not err:
        return 'Something went wrong :/'
    if isinstance(err, OSError) and err.errno in _ERRNO_MESSAGES:
        return f'[ERROR: {_ERRNO_MESSAGES[err.errno]}]'
    if isinstance(err, Exception):
        return f'[ERROR: {fallback}]' if fallback else 'Something went wrong :/'
    return f'[ERROR: {err}]'
```
Since `e` passed in is always an `Exception` instance here, this hits the third branch and
returns the fixed string `'[ERROR: Something went wrong while searching the web.]'` — **the
exception's own text is discarded, not interpolated**. So the chat UI's status line (and hence
anything the chat transcript/history stores from it) never carries the query-bearing URL; only
the server-side ERROR log lines above do.

**What `generate_queries` logs of the task model's reply**: **nothing.** Reading
`routers/tasks.py:403-476` in full: the function builds `content` (the query-generation
prompt, itself built from the user's messages by `query_generation_template`, not logged), the
`payload`, and returns `generate_chat_completion(...)`'s result directly, or on failure
`JSONResponse(content={'detail': str(e)})` (`routers/tasks.py:472-476`) with **no log call at
all** in that except block. The task model's raw completion text (`response =
res['choices'][0]['message']['content']`, parsed back in `utils/middleware.py:1542`) is never
logged anywhere in either function.

### A3. `safe_web` loader per-URL failure handling, and reachability of `retrieval.py:2960`

`SafeWebBaseLoader` (`retrieval/web/utils.py:900-1012`) has **two different failure-handling
shapes for its two load paths**:
- **Sync (`lazy_load`, `retrieval/web/utils.py:980-993`)**: a per-URL `try`/`except` that
  **catches and continues**: `log.exception(f'Error loading {path}: {e}')` (ERROR, carries the
  failed URL in the message) and moves to the next URL — this path is *not* the one
  `process_web_search` uses.
- **Async (`alazy_load`/`aload`, `retrieval/web/utils.py:1003-1012`)** — the path
  `process_web_search` actually calls (`docs = await loader.aload()`,
  `routers/retrieval.py:2902`) — has **no per-URL try/except of its own**: `alazy_load` calls
  `results = await self.fetch_all(self.web_paths)` (inherited from langchain_community's
  `WebBaseLoader`, not overridden here) and only wraps the *parsing* step
  (`_document_from_html`) per document, not the fetch. `SafeWebBaseLoader._fetch`
  (`retrieval/web/utils.py:937-964`) itself only catches `aiohttp.ClientConnectionError` for
  its own retry loop (`retrieval/web/utils.py:958-963`, `log.warning(f'Error fetching {url}
  with attempt {i+1}/{retries}: {e}. Retrying...')`, WARNING, carries the URL) — any other
  exception (e.g. `response.raise_for_status()`'s `ClientResponseError` on a non-2xx fetched
  page, `retrieval/web/utils.py:955-956`) is **not caught inside `_fetch`** and propagates
  straight up through `fetch_all` → `alazy_load` → `aload` → `get_web_loader`'s caller.

  **So `routers/retrieval.py:2960`'s `log.exception('Web search content loading failed')` is
  reachable from a single failed page fetch, not only a loader-wide exception** — provided
  langchain_community's `fetch_all` (not part of this repo; pinned at `langchain-community==0.4.2`
  per `backend/requirements.txt`) does not itself swallow a per-URL exception before it
  reaches `aload()`. **Not independently verified**: `fetch_all`'s own exception handling —
  this note could not locate a matching tag on `langchain-ai/langchain`'s GitHub releases for
  `langchain-community==0.4.2` in the time available (the community package is a separate
  release train from the `langchain` monorepo's own tags), so whether `fetch_all` catches and
  logs per-URL internally, or lets the first failure abort the whole batch, is unresolved from
  source here — the same gap the cited prior note already flagged (its item 5) for the
  loader-exception question generally.

  **Corrected 2026-09-10 (slice-1 ticket 71), from the pinned image's source read and run inside the running container:** `raise_for_status` is langchain's default `False` and `get_web_loader` never sets it, so a non-2xx page never raises at `:955-956` — its body is returned as the document (a refusal page enters the context as a source, silently). `get_web_loader` passes `continue_on_failure=True`, and langchain's `WebBaseLoader._fetch_with_rate_limit` (the `fetch_all` task wrapper, read from the installed package) catches every exception `_fetch` raises, logs `Error fetching {url}, skipping due to continue_on_failure=True` at WARNING under its own logger (a third URL-bearing line of the residual's class; one in the frontend's journal since 2026-09-08), and returns `""`, so a per-URL failure never reaches `:2960` — that traceback is reachable from the parse step or `gather` itself alone. `_fetch`'s aiohttp session sets no timeout unless `WEB_LOADER_TIMEOUT` supplies one (`get_web_loader` passes it as `requests_kwargs['timeout']`, each fetch's total), so a page that never answers costs aiohttp's own 300 s default; `v0.1.44` renders 30 s, measured with the loader class in the container (the Post tarpitting any address-carrying agent, skipped after 30.6 s with the other pages intact).

### A4. Can the two ERROR lines be silenced by a per-source level? What else would be lost? Does `RAG_LOG_LEVEL=ERROR` still print the traceback?

**No per-source override exists at all at this tag.** A1 already establishes there is no
`SRC_LOG_LEVELS` table and no per-module `*_LOG_LEVEL` environment variable anywhere in
`env.py` — `RAG_LOG_LEVEL` does **not exist** as a name in this codebase (searched the whole
of `env.py` and `config.py`'s fetched ranges; not found). The **only** lever is
`GLOBAL_LOG_LEVEL`, which is process-wide (A1). Raising it above `ERROR` (i.e. to `CRITICAL`)
would be the only way to drop `routers/retrieval.py:2864`/`:2960` and
`utils/middleware.py:1655` — and doing so silences **every** ERROR-level line in the entire
backend process, not just these three. Everything this note traced that would go with it
(from the ranges actually read in this research; not an exhaustive scan of the full
3376-line `routers/retrieval.py` or 6400-line `utils/middleware.py`):
- Every other `log.exception`/`log.error` call this note encountered outside the search path
  (e.g. `routers/tasks.py:207,272,337,396,549` — `log.error('Exception occurred', exc_info=True)`
  or `f'Error generating chat completion: {e}'` for title/follow-up/tags/image-prompt/autocomplete
  generation failures) — none of these carry user prompt text (they log a fixed string plus
  `exc_info`, whose traceback would name the *model call* failure, not the prompt), but they
  are real operational diagnostics a General-only deployment would lose entirely.
- `retrieval/web/utils.py:1026` (`get_web_loader`) `log.warning(f'All provided URLs were
  blocked or invalid: {urls}')` — WARNING, would go silent at CRITICAL, and it is itself a
  URL-bearing line (though for *blocked/invalid* URLs, i.e. an SSRF-guard rejection, which is
  arguably a security-relevant line worth keeping visible).
- `retrieval/web/utils.py:962` (`_fetch`'s retry warning, A3) — URL-bearing, would go silent.

**Is `log.exception`'s traceback text (where the URL sits) emitted by the formatter at any
level ≤ ERROR, so `RAG_LOG_LEVEL=ERROR` would still print it?** The question's premise
(a `RAG_LOG_LEVEL` variable) does not hold at this tag, but the underlying mechanism question
still has a clean answer from the code read in A1: `logging.Logger.exception(msg)` always
logs at `ERROR` severity (`exc_info=True`); loguru's sink threshold and the stdlib root's
threshold are both compared against the record's own level (`ERROR`, numeric 40), not against
some separate "does this look like a traceback" test. So **any configured threshold at or
below `ERROR` (`GLOBAL_LOG_LEVEL` ∈ {DEBUG, INFO, WARNING, ERROR}) lets the full traceback —
URL included — through; only a threshold strictly above `ERROR` (`CRITICAL`) drops it.** This
matches the ticket's premise exactly, just with `GLOBAL_LOG_LEVEL` in place of the
hypothesized `RAG_LOG_LEVEL`.

### A5. The SSRF guard: can a CI stack be reached, and does `search_searxng` bypass the check?

**The flag exists and is named `ENABLE_LOCAL_WEB_FETCH`, default `False`**, with a deprecated
alias, `config.py:1104-1112`:
```python
ENABLE_LOCAL_WEB_FETCH = (
    os.getenv('ENABLE_LOCAL_WEB_FETCH', os.getenv('ENABLE_RAG_LOCAL_WEB_FETCH', 'False')).lower() == 'true'
)
ENABLE_RAG_LOCAL_WEB_FETCH = ENABLE_LOCAL_WEB_FETCH  # Deprecated compatibility alias
```
It gates exactly one thing: whether `retrieval/web/utils.py`'s `_assert_addresses_allowed`
(`retrieval/web/utils.py:111-126`) rejects a **non-global** resolved IP address:
```python
# retrieval/web/utils.py:122-126
if not ENABLE_LOCAL_WEB_FETCH:
    for address in candidates:
        if not address.is_global:
            log.warning(f'Blocked non-global address: {address}')
            raise ValueError(ERROR_MESSAGES.INVALID_URL)
```
With `ENABLE_LOCAL_WEB_FETCH=true`, a Compose-internal private address (a stub container's
IP, e.g. `172.x`/`10.x`) is **not** rejected by this check. A separate, unconditional
block-list (`_assert_host_allowed`/`_assert_addresses_allowed`'s `is_host_blocked` check,
`retrieval/web/utils.py:106-120`) still always blocks a fixed, hardcoded list of cloud
metadata and reserved addresses regardless of `ENABLE_LOCAL_WEB_FETCH`
(`DEFAULT_WEB_FETCH_FILTER_LIST`, `config.py:1116-1135` — `169.254.169.254`,
`fd00:ec2::254`, `metadata.google.internal`, etc.) — none of these overlap with a normal
Compose service IP, so they do not interfere with a CI stub. **So: setting
`ENABLE_LOCAL_WEB_FETCH=true` in the CI stack's Open WebUI environment is what lets the
`safe_web` loader fetch a page from a stub container on the Compose network** (the ticket's
plan needs this, since the loader's SSRF guard would otherwise reject the stub's private IP).

**Confirmed: `search_searxng`'s call to `SEARXNG_QUERY_URL` runs no SSRF check.**
`retrieval/web/searxng.py`'s `session = await get_session()` uses the **shared pooled**
session from `utils/session_pool.py:54-83` (`get_session()`), which is a plain
`aiohttp.ClientSession` with a plain `aiohttp.TCPConnector` (`utils/session_pool.py:70`) — not
`_SSRFSafeConnector`. `validate_url`/`_assert_host_allowed`/`_assert_addresses_allowed` are
defined in `retrieval/web/utils.py` and are never imported into `retrieval/web/searxng.py` (not
found in that file's import list) or into `utils/session_pool.py`. So a private
`SEARXNG_QUERY_URL` (e.g. `http://searxng:8080/search`) needs **no** `ENABLE_LOCAL_WEB_FETCH`
flag at all — it was never subject to the SSRF guard in the first place, consistent with the
cited prior note's §8/§9 findings (not re-derived here).

### A6. The audit file

All from `env.py` (values already quoted at their line numbers) and `utils/audit.py` /
`utils/logger.py`:

- **`AUDIT_LOG_LEVEL`**: `env.py:1213`, `os.getenv('AUDIT_LOG_LEVEL', 'NONE').upper()`.
  Values, from `utils/audit.py:53-57`'s `AuditLevel(str, Enum)`: `NONE`, `METADATA`,
  `REQUEST`, `REQUEST_RESPONSE`. **This is a content-detail axis, unrelated to Python log
  severity** — the audit *file* sink's own loguru threshold is hardcoded `'INFO'`
  (`utils/logger.py:201`, `logger.add(AUDIT_LOGS_FILE_PATH, level='INFO', ...)`), not driven by
  `AUDIT_LOG_LEVEL`'s value at all.
- **`AUDIT_LOGS_FILE_PATH`**: `env.py:1203`, default `f'{DATA_DIR}/audit.log'` — i.e. inside
  the container, `${DATA_DIR}/audit.log` (`DATA_DIR` default `backend/data`, `env.py:222`, or
  wherever GIDEON's render points `DATA_DIR`).
- **Rotation**: `AUDIT_LOG_FILE_ROTATION_SIZE` (env.py:1205, default `'10MB'`), passed straight
  to loguru's `rotation=` kwarg (`utils/logger.py:202`) — **not** `AUDIT_LOG_ROTATION`, and
  **not** `MAX_BODY_LOG_SIZE` (`env.py:1215-1217`, default `2048` — that caps how many bytes of
  a request/response *body* are buffered per request for the `REQUEST`/`REQUEST_RESPONSE`
  levels, a different knob).
- **`AUDIT_EXCLUDED_PATHS`**: `env.py:1220-1226`, default `['chats', 'chat', 'folders']`
  (parsed from `'/chats,/chat,/folders'`, leading slashes stripped). `AUDIT_INCLUDED_PATHS`
  (env.py:1230-1232, default `[]`) is a whitelist that, when non-empty, makes
  `AUDIT_EXCLUDED_PATHS` moot (`utils/audit.py:150-160`, whitelist wins, and a warning is
  logged if both are set).
- **A `METADATA` entry field by field** (`utils/audit.py:36-50` dataclass,
  `:275-305` `_log_audit_entry`): `id` (uuid4), `user` (`{id, name, email, role}` via
  `model_dump(include=...)`), `audit_level` (the string `'METADATA'`/etc.), `verb`
  (`request.method`), **`request_uri` = `str(request.url)`, which includes the query string**
  (Starlette's `Request.url` renders the full URL with its query), `source_ip`
  (`request.client.host`), `user_agent` (`request.headers.get('user-agent')`),
  `response_status_code` (captured only if the response passed through
  `_capture_response`, which only runs at `REQUEST_RESPONSE` — at `METADATA` this stays
  `None`). **At `METADATA`, `request_object`/`response_object` are empty strings** — the
  receive/send wrappers that populate `AuditContext.request_body`/`response_body`
  (`utils/audit.py:262-273`) only run when `audit_level in (REQUEST, REQUEST_RESPONSE)`
  (`utils/audit.py:190-194`) for the request body, and only at `REQUEST_RESPONSE`
  (`utils/audit.py:179`) for the response body — so **at `METADATA` a `POST
  /api/chat/completions` body is not written; at `REQUEST` (and `REQUEST_RESPONSE`) it is**,
  confirming the ticket's premise exactly. Since the search route is `POST
  /api/v1/retrieval/process/web/search` with the queries in the JSON **body**, not the URL,
  `request_uri` at any audit level never carries the search query text itself — only a
  `REQUEST`/`REQUEST_RESPONSE`-level `request_object` would.
- **Sink target: file-only by default, not stdout.** `utils/logger.py:181` builds
  `audit_filter = lambda record: True if ENABLE_AUDIT_STDOUT else 'auditable' not in
  record['extra']` and applies it to the **stdout** sink (`utils/logger.py:183-196`) — so by
  default (`ENABLE_AUDIT_STDOUT=False`, `env.py:1197`) records bound `auditable=True`
  (every audit entry, via `AuditLogger.__init__`'s `logger.bind(auditable=True)`,
  `utils/audit.py:69`) are **excluded** from stdout and reach **only** the dedicated file sink
  (`utils/logger.py:199-207`, filtered the opposite way:
  `filter=lambda record: record['extra'].get('auditable') is True`). Setting
  `ENABLE_AUDIT_STDOUT=true` makes audit entries appear on stdout too (in the plain
  `stdout_format`/JSON console format, not the file's structured `file_format`).

### A7. Telemetry

Env vars (`env.py:1242-1277`), all default `False`/unset unless noted:
`ENABLE_OTEL`, `ENABLE_OTEL_TRACES`, `ENABLE_OTEL_METRICS`, `ENABLE_OTEL_LOGS` (all
`os.getenv(..., 'False')`); `OTEL_EXPORTER_OTLP_ENDPOINT` (default
`'http://localhost:4317'`); `OTEL_METRICS_EXPORTER_OTLP_ENDPOINT` /
`OTEL_LOGS_EXPORTER_OTLP_ENDPOINT` (default to the traces endpoint);
`OTEL_EXPORTER_OTLP_INSECURE` and its metrics/logs variants (default `False`);
`OTEL_SERVICE_NAME` (default `'open-webui'`); `OTEL_RESOURCE_ATTRIBUTES`;
`OTEL_TRACES_SAMPLER` (default `'parentbased_always_on'`); `OTEL_BASIC_AUTH_USERNAME`/`_PASSWORD`
(and per-signal variants); `OTEL_METRICS_EXPORT_INTERVAL_MILLIS` (default `10000`);
`OTEL_OTLP_SPAN_EXPORTER`/`OTEL_METRICS_OTLP_SPAN_EXPORTER`/`OTEL_LOGS_OTLP_SPAN_EXPORTER`
(`'grpc'` or `'http'`, default `'grpc'`).

**Two gates, not one**: `main.py` only imports/calls `utils/telemetry/setup.py`'s `setup()`
`if ENABLE_OTEL:`; inside `setup()`, the instrumentors (and every span attribute below) are
only wired **`if ENABLE_OTEL_TRACES:`** — a second, separate flag. Metrics are wired
separately `if ENABLE_OTEL_METRICS:` (`setup_metrics(...)`, not itself traced further here).
**Note the ticket's `ENABLE_OTEL_TRACES` was not in its own env-var list but is required** —
`ENABLE_OTEL=true` alone does not turn on tracing/instrumentation.

**Instrumentors installed** (`utils/telemetry/instrumentors.py`, `Instrumentor._instrument`):
`FastAPIInstrumentor`, `SQLAlchemyInstrumentor`, `RedisInstrumentor`, `RequestsInstrumentor`,
`LoggingInstrumentor`, `HTTPXClientInstrumentor`, `AioHttpClientInstrumentor`,
`SystemMetricsInstrumentor` — i.e. **yes**, both the `aiohttp` client (used by
`search_searxng` and the `safe_web` loader's async fetch) and `requests` (used by the
SSRF-safe synchronous session and several search-engine modules) are instrumented, alongside
`httpx` and FastAPI's own server-side spans.

**Full URL, including query string, is a span attribute — confirmed, this is the exact
mechanism [22] worried about.** `SpanAttributes.HTTP_URL = 'http.url'` (legacy semconv key,
sourced from `opentelemetry.semconv._incubating.attributes.http_attributes`, per
`utils/telemetry/constants.py`). Each client instrumentor's request hook sets it to the
**string form of the full request URL** — `aiohttp_request_hook`:
```python
span.set_attributes(attributes={SpanAttributes.HTTP_URL: str(request.url), ...})
```
and identically (modulo the URL's own type) in `requests_hook` (`request.url`) and
`httpx_request_hook` (`str(request.url)`). For an aiohttp GET built the way
`search_searxng` builds it (`aiohttp`'s `params=` kwarg merged into the URL before the
request is sent), `str(request.url)` **includes every query parameter**, `q=<the search
query>` among them. So **with `ENABLE_OTEL=true` and `ENABLE_OTEL_TRACES=true`, every
SearXNG search request (and every page fetch) produces a span whose `http.url` attribute
carries the full query-bearing (or page-URL-bearing) request URL**, exported to whatever
`OTEL_EXPORTER_OTLP_ENDPOINT` is configured — independent of `GLOBAL_LOG_LEVEL` entirely,
since this is a trace export, not a log line.

**`ENABLE_OTEL_LOGS` appears to be an unwired flag at this tag.** `utils/telemetry/logs.py`
defines `setup_logging()`/`otel_handler` (an OTLP `LoggingHandler`, built unconditionally at
**import** time — the module has no gate on `ENABLE_OTEL_LOGS` itself) and `InterceptHandler.emit`
(`utils/logger.py:117-123`) references it: `if ENABLE_OTEL and ENABLE_OTEL_LOGS: from
open_webui.utils.telemetry.logs import otel_handler; ... otel_handler.emit(record)` — so
**every stdlib log record, once intercepted, would also be emitted to the OTel log exporter
when both flags are true**, including the two ERROR lines from A2/A3 (their formatted message
via `record.msg`, not necessarily the full traceback text, since `otel_handler.emit(record)`
receives the raw `LogRecord`, and only its `.msg`/`.args` are rewritten by the surrounding
code — **not independently traced further**: whether OpenTelemetry's own `LoggingHandler`
attaches the exception's traceback text as an log-record attribute the way loguru's console
sink does). A full-repo search for `ENABLE_OTEL_LOGS`/`otel_handler`/`addHandler` in `main.py`
found **no other occurrence** — this note did not exhaustively search every file in the
backend for a third wiring site, only `main.py` and `env.py`.

### A8. Chat-page URL parameters that carry a prompt or toggle web search

`src/lib/components/chat/Chat.svelte` reads several `$page.url.searchParams` values. The two
relevant to this ticket, quoted verbatim:
```js
} else if ($page.url.searchParams.get('q')) {
    const q = $page.url.searchParams.get('q') ?? '';
    messageInput?.setText(q);
    if (q) {
        if (($page.url.searchParams.get('submit') ?? 'true') === 'true') {
            await tick();
            submitHandler(q);
        }
    }
}
```
```js
if ($page.url.searchParams.get('web-search') === 'true') {
    webSearchEnabled = true;
}
```
So **`?q=<text>` prefills the composer, and — since `submit` defaults to `'true'` when the
parameter is absent — auto-submits the prompt** unless the URL explicitly adds `&submit=false`;
**`?web-search=true` turns web search on** for that load. A URL such as
`https://<host>/?q=<prompt>&web-search=true` therefore both carries the prompt in its own
query string and, on load, drives a live web search — any HTTP access log that records the
request line/URI (Caddy's, per Part C) would capture the prompt text this way, independent of
the application's own logging. Other parameters found in the same file, existence/name only
(not otherwise relevant to this ticket): `models`/`model`, `youtube` (auto-attaches a YouTube
URL via `uploadWeb`), `load-url` (auto-attaches a web URL via `uploadWeb` — the route ticket 28
already covers), `image-generation`, `code-interpreter`, `tools`/`tool-ids`, `call`.

---

## Part B — SearXNG at `3e454637f`

### B1. Any settings key/env var reaching the `searx.network` logger — none

`searx/__init__.py` (whole file, reconstructed and re-numbered locally — see the sources
note) has exactly two logging branches inside `init_settings()`, chosen by `general.debug`
(env var `SEARXNG_DEBUG`, per `docs/research/searxng-service-and-owui-search.md` §5e, not
re-derived):
```python
# searx/__init__.py:57-64 (module scope) / :57-64 constants
LOG_FORMAT_PROD: str = '%(asctime)-15s %(levelname)s:%(name)s: %(message)s'
LOG_LEVEL_PROD = logging.WARNING
```
```python
# searx/__init__.py:57-64 (init_settings, production branch)
sxng_debug = get_setting("general.debug")
if sxng_debug:
    _logging_config_debug()
else:
    logging.basicConfig(level=LOG_LEVEL_PROD, format=LOG_FORMAT_PROD)
    logging.root.setLevel(level=LOG_LEVEL_PROD)
    logging.getLogger('werkzeug').setLevel(level=LOG_LEVEL_PROD)
    logger.info(msg)
```
(Line numbers: this block sits at `searx/__init__.py:57-64` in the reconstructed, re-numbered
copy — see the file's `init_settings` function starting at line 33.) **Neither branch, nor
`searx/settings_defaults.py`'s `SCHEMA` table (already fully reproduced in the cited prior
note §5), nor `searx/settings.yml`, exposes any setting or environment variable that names a
specific logger** (e.g. `searx.network`) — the production branch's *only* control is
`logging.basicConfig(level=WARNING)` + `logging.root.setLevel(WARNING)`, both root-wide, plus
one explicit third-party override (`werkzeug`). `_logging_config_debug()` (debug branch) is
likewise root-wide (`coloredlogs.install(level=log_level, ...)` or
`logging.basicConfig(level=..., format=LOG_FORMAT_DEBUG)`), reading only one env var,
`SEARXNG_DEBUG_LOG_LEVEL` (default `'DEBUG'`), which sets the *root's* level in debug mode, not
any one logger's. **Plain answer: no, SearXNG's own settings/env surface offers no per-logger
control at all, in either branch.**

### B2. granian `--log-config`: format, timing, and whether it can silence `searx.network` without patching the image

SearXNG's `requirements-server.txt` pins `granian==2.8.2` (read at the SearXNG commit).
granian source read at tag `v2.8.2` (`emmett-framework/granian`).

**The option**: `granian/cli.py`'s `cli()` command declares
```python
@option('--log-config', type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True, path_type=pathlib.Path), help='Logging configuration file (json)')
```
with parameter name `log_config`. Since the CLI is wired `cli(auto_envvar_prefix='GRANIAN')`
(cited already in the prior note for a different option, same mechanism), click derives the
env var from the **parameter name**, giving **`GRANIAN_LOG_CONFIG`** — a path to a **JSON
file** (not an inline JSON string), read and `json.loads`'d by the CLI:
```python
log_dictconfig = None
if log_config:
    with log_config.open() as log_config_file:
        try:
            log_dictconfig = json.loads(log_config_file.read())
        except Exception:
            print('Unable to parse provided logging config.')
            raise click.exceptions.Exit(1)
```
The parsed dict is passed straight through as `Server(..., log_dictconfig=log_dictconfig, ...)`
— **it is a `logging.config.dictConfig`-shaped document** (`version`, `loggers`, `handlers`,
`formatters`, etc.), confirmed by how it is consumed (below).

**Where and when it is applied — per worker process, before the app is imported.**
granian's own base config (`granian/log.py:31-54`, reconstructed and line-numbered locally):
```python
# granian/log.py:31-54
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {...},
    'handlers': {'console': {...}, 'access': {...}},
    'loggers': {
        '_granian': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'granian.access': {'handlers': ['access'], 'level': 'INFO', 'propagate': False},
    },
}
```
```python
# granian/log.py:63-73
def configure_logging(level, config=None, enabled=True):
    log_config = copy.deepcopy(LOGGING_CONFIG)
    if config:
        log_config.update(config)
    log_config['loggers'].setdefault('_granian', {})['level'] = log_levels_map[level]
    logging.config.dictConfig(log_config)
    if not enabled:
        logger.setLevel(logging.CRITICAL + 1)
```
`log_config.update(config)` is a **shallow, top-level `dict.update`** — a custom config's own
`loggers` key **replaces** granian's default `loggers` dict wholesale (not a deep merge); its
`handlers`/`formatters` keys, if present, would likewise replace granian's wholesale, but if
*absent* from the custom config, granian's own `handlers`/`formatters` definitions survive
untouched (since `update()` only touches keys the custom dict actually sets). The line right
after re-inserts `_granian`'s **level** (forced to the CLI's `--log-level` value) if that key
went missing, but not its `handlers`/`propagate` — so a custom config that wants to keep
granian's own console/access logging working alongside a new entry should re-declare
`_granian` and `granian.access` in its own `loggers` block rather than relying on that
`setdefault`. `disable_existing_loggers` stays `False` (granian's own default) unless the
custom config's top-level dict explicitly sets it, since that key is untouched by a custom
config that only supplies `loggers`.

**`configure_logging()` runs inside each worker process, before the ASGI app is imported.**
`granian/server/mp.py`'s per-worker bootstrap function (`wrap_target`, name confirmed via
targeted search of that file) runs, in order:
```
configure_logging(log_level, log_config, log_enabled)
load_env(env_files)
...
callback = callback_loader()   # imports/loads the application (searx.webapp:app)
```
i.e. **`configure_logging()` — hence `logging.config.dictConfig(...)` with any custom
`loggers` entries — completes before `searx.webapp` (and therefore `searx/__init__.py`'s own
`logging.basicConfig(level=WARNING)` / `logging.root.setLevel(WARNING)`) is even imported**,
in every worker process (the file frames worker start as either `fork` or `spawn`, falling
back to `spawn` when `fork` is unavailable, but `wrap_target` — the function that calls
`configure_logging` then loads the callback — runs inside the worker either way, so the
ordering holds regardless of which start method is used).

**Answer to the central question: yes.** Python's `logging` module lets a named logger carry
its own explicitly-set level, and a later call to `logging.basicConfig()` /
`logging.root.setLevel()` only touches the **root** logger — it never resets a child logger's
own already-set level. Since a custom `GRANIAN_LOG_CONFIG` JSON's `dictConfig` call (setting,
say, `{"loggers": {"searx.network": {"level": "ERROR"}}}`, plus re-declarations of
`_granian`/`granian.access` to avoid losing granian's own logging) runs and completes *before*
`searx/__init__.py`'s production-branch `logging.basicConfig(level=WARNING)` runs, the
`searx.network` logger's explicit `ERROR` level survives that later call untouched. A
`logger.warning(...)` call against it (network.py:255's own message, numeric level 30) is then
below the logger's own effective threshold (`ERROR`, numeric 40) and is dropped before any
handler sees it — **without patching the image**, via a mounted/rendered JSON file and
`GRANIAN_LOG_CONFIG=/path/to/that/file.json`.

`--log-level`/`GRANIAN_LOG_LEVEL` only sets `_granian`'s own logger level (the
`setdefault('_granian', {})['level'] = log_levels_map[level]` line above) — it does not touch
application loggers at all. `--no-log`/`GRANIAN_LOG_ENABLED=false` (`log_enabled=False`) only
sets `_granian`'s logger to `CRITICAL + 1` (`granian/log.py:72-73`) — it silences **granian's
own** startup/lifecycle log lines, not `searx`'s application loggers, which are untouched by
this flag.

### B3. `error_recorder.py`, `abstract.py`, `online.py`: what an `ErrorContext` carries, and confirming `network.py:249-258` is the only full-URL line above DEBUG on the engine-request path

**`add_error_context` logs at WARNING, and the message is never a full URL.**
`searx/metrics/error_recorder.py:87-90`:
```python
def add_error_context(engine_name: str, error_context: ErrorContext) -> None:
    errors_for_engine = errors_per_engines.setdefault(engine_name, {})
    errors_for_engine[error_context] = errors_for_engine.get(error_context, 0) + 1
    engines[engine_name].logger.warning('%s', str(error_context))
```
`str(error_context)` resolves to `ErrorContext.__repr__` (no `__str__` defined), which embeds
`filename`, `function`, `line_no`, `code`, `exception_classname`, `log_message`,
`log_parameters`, `secondary` — never the exception's own message text directly, only what
`get_messages()`/`log_parameters` supplies. For a `RequestException` (the class curl_cffi's
HTTP errors, timeouts, and connection errors all derive from), `get_messages`
(`searx/metrics/error_recorder.py:122-138`) routes to
`get_request_exception_messages` (`:110-118`):
```python
def get_hostname(exc: RequestException) -> str | None:
    url = getattr(getattr(exc, "request", None), "url", None)
    if url is None:
        url = getattr(getattr(exc, "response", None), "url", None)
    return urlparse(str(url)).netloc if url else None


def get_request_exception_messages(exc):
    response = getattr(exc, "response", None)
    status_code = reason = None
    if isinstance(exc, HTTPError) and response is not None:
        status_code = str(response.status_code)
        reason = response.reason
    return (status_code, reason, get_hostname(exc))
```
**`get_hostname` returns only `urlparse(...).netloc` — the host[:port], never the path or
query string.** So every `ErrorContext`-based WARNING (the engine-exception path via
`abstract.py`'s `handle_exception` → `count_exception`/`count_error` →
`add_error_context`) is **hostname-only by construction**, confirming the cited prior note's
"hostname-only rule" exactly, from the function itself rather than by inference.
`abstract.py`'s `EngineProcessor.handle_exception` (quoted in full below) does no logging of
its own — it only calls `count_exception`/`count_error` (which do the WARNING above) and
`result_container.add_unresponsive_engine`/metrics/suspend bookkeeping; there is no separate
`log_error_once` function anywhere in `abstract.py`.

**Confirmed: `network.py:249-258` (`patch_response`'s automatic raise-and-log) is the only
line above DEBUG on the engine-request path that logs a *full URL*.** Every other
WARNING/ERROR-or-above call this note found on that path
(`error_recorder.py:90`'s `add_error_context`; `online.py`'s own `self.logger.debug(...)`
calls in every `except` branch of `search()`, all DEBUG; `webapp.py`'s `logger.exception(e,
exc_info=True)` for an unhandled 500 at the route level, which is a search()-call-level bug,
not a per-engine error, and was not seen to carry a URL in the excerpt read) either logs no
URL at all or logs hostname-only. `patch_response` itself (already quoted by the cited prior
note, `network.py:249-258`) is reproduced here for convenience:
```python
def patch_response(self, response, do_raise_for_httperror):
    if do_raise_for_httperror:
        try:
            raise_for_httperror(response)
        except:
            method = response.request.method if response.request else "?"
            url = response.request.url if response.request else response.url
            self._logger.warning(f"HTTP Request failed: {method} {url}")
            raise
    return response
```

**Critical, ticket-specific finding: this line only fires when `do_raise_for_httperror` is
true for that request, and SearXNG's own generic `json_engine` — the module the ticket's CI
plan uses to wire the stub — explicitly sets it to `False`.** Tracing the plumbing:
`searx/search/processors/online.py`'s `_send_http_request`
(`searx/search/processors/online.py:193`, reconstructed/re-numbered locally):
```python
request_args["raise_for_httperror"] = params.get("raise_for_httperror", True)
```
default `True` (`online.py:114`, `default_request_params()`'s `"raise_for_httperror": True`).
But `searx/engines/json_engine.py`'s `request()` function — the one every `json_engine`-based
engine uses to build its outbound request — **unconditionally overrides it**:
```python
# searx/engines/json_engine.py:355-356
params['soft_max_redirects'] = soft_max_redirects
params['raise_for_httperror'] = False
```
So for a `json_engine`-wired stub, the network layer's `patch_response` never raises or logs
on an HTTP error — the response is returned to the engine's own `response()` function
untouched, which then calls `raise_for_httperror(resp)` **itself**, directly and unguarded,
one function later:
```python
# searx/engines/json_engine.py:396-403 (response)
def response(resp):
    results = []
    if no_result_for_http_status and resp.status_code in no_result_for_http_status:
        return results
    raise_for_httperror(resp)
    ...
```
This is the plain function from `searx/network/raise_for_httperror.py:61-76` (reconstructed
locally, full file), which for `status_code >= 400` raises `SearxEngineAccessDeniedException`
(402/403), `SearxEngineTooManyRequestsException` (429), or, for anything else (e.g. a stub
answering 500), `resp.raise_for_status()` — a plain `curl_cffi.requests.exceptions.HTTPError`.
**None of these three raise paths goes through `patch_response`'s except-block** — they
propagate out of `self.engine.response(response)` inside `online.py`'s `_search_basic`, back
to `OnlineProcessor.search()`'s own try/except (`online.py:242-283`), whose branches are *all*
`self.logger.debug(...)` after calling `handle_exception` (hostname-only WARNING via
`count_exception`, established above) — **never the query/URL-bearing WARNING at
`network.py:255`.**

**Practically: a plan that drives "SearXNG's engine answers an HTTP error" through a
`json_engine`-wired stub will not exercise `network.py:255` at all** — that WARNING line only
fires for engines that leave `raise_for_httperror` at its default `True` (confirmed, by
checking their `request()` functions for the string `raise_for_httperror`, that
`searx/engines/bing.py` and `searx/engines/startpage.py` — two of GIDEON's own configured
engines per the cited prior note's §5a — do **not** set it, so they keep the default and would
trigger `network.py:255` on a real HTTP error from Bing/Startpage). This is a real gap between
what the ticket's planned CI scenario (stub via `json_engine`) can prove and what the prior
research already established is leaking in production (a real, non-`json_engine` engine
failing). It does not mean the CI test is wrong to exist — the WARNING-level and
`ErrorContext` mechanisms it *would* exercise (hostname-only) are still worth a green grep —
only that this specific scenario, wired this way, is not a regression test for the
`network.py:255` line specifically; it would need either a different (non-`json_engine`)
engine wiring for the stub, or a documented acceptance that this particular leak is proven
some other way (e.g. by code inspection, as this note and the cited prior one have done, or by
a stub reachable through an engine module that does not disable `raise_for_httperror`).

**curl_cffi's `HTTPError` message shape** (`curl_cffi==0.16.1` per SearXNG's
`requirements.txt`): **not independently re-read from curl_cffi's own source in this note** —
the cited prior note already established (§11, quoting `aiohttp`'s equivalent, not curl_cffi's)
that a `ClientResponseError.__str__` includes the full URL; this note did not re-fetch
`curl_cffi/requests/models.py`'s `raise_for_status` to confirm curl_cffi's own `HTTPError.__str__`
shape at `0.16.1` specifically — **flagged as unverified** (the question is nonetheless largely
moot for `json_engine`'s own path, which never reaches `patch_response`'s logger regardless of
what the exception's `str()` contains, since that WARNING call site is skipped entirely per
above; it would matter for a non-`json_engine` engine's `curl_cffi.requests.exceptions.HTTPError`
reaching `network.py:255`'s f-string, which uses `response.request.url`, an attribute access,
not the exception's own `__str__`, so curl_cffi's `HTTPError.__str__` shape is in fact
irrelevant to that specific line either way).

### B4. `general.debug: true` and the DEBUG lines that carry query text

Not exhaustively re-traced line-by-line against every file named in the ticket's B4 list in
the time available for this note (`searx/webapp.py`, `searx/search/__init__.py`,
`searx/search/models.py`, `searx/network/network.py`, `searx/engines/json_engine.py`) —
what is confirmed from the files this note did read in full: `general.debug: true`
(env var `SEARXNG_DEBUG`) only changes which of `searx/__init__.py`'s two logging branches
runs (B1) — it does not, by itself, add any new log call anywhere; it only **lowers the root
threshold** from `WARNING` to whatever `SEARXNG_DEBUG_LOG_LEVEL` resolves to (default
`'DEBUG'`), which then lets every existing `logger.debug(...)` call already in the source
(e.g. `online.py`'s per-exception `self.logger.debug(...)` calls, B3) print. **Confirmed
equivalence of the env var and the settings key**: `general.debug`'s `SettingsValue` schema
entry in `searx/settings_defaults.py` (already fully quoted, table form, in the cited prior
note §5e) names `SEARXNG_DEBUG` as its `environ_name`, and `SettingsValue.__call__` (same
note) always lets the env var win when set — so `SEARXNG_DEBUG=true` and a settings.yml
`general.debug: true` are equivalent, and the env var takes precedence if both are present.
**Not verified in this note**: the exact DEBUG-level lines in `searx/webapp.py`'s route
handler and `searx/search/__init__.py`/`models.py` that would show the raw query text
end-to-end (distinct from `online.py`'s per-engine DEBUG lines, which show the *exception*,
not necessarily the query) — flagged as unverified; the CI plan should treat `general.debug`
as **off** (its default) rather than rely on a specific enumerated DEBUG line list here.

### B5. `json_engine.py` settings, response shapes, and `settings_loader`'s `keep_only`/user-engine merge

**Settings keys** (all declared as module-level variables in `searx/engines/json_engine.py`,
each overridable per-engine in `settings.yml`): `search_url` (a `str.format()` template with
`{query}`/`{pageno}`/`{lang}`/`{time_range}`/`{safe_search}` placeholders — the query itself is
url-encoded via `urlencode({'q': query})[2:]` before substitution, `json_engine.py:326-345`,
so a `{query}` placeholder in `search_url` receives an already-percent-encoded value), `paging`
(bool, default `False`), `page_size`/`first_page_num`/`send_page_num_on_first_page`,
`method` (default `'GET'`), `request_body` (for `POST`), `headers`/`cookies` (dicts),
`results_query`/`url_query`/`url_prefix`/`title_query`/`content_query`/`thumbnail_query`/
`thumbnail_prefix`/`suggestion_query` (all slash-separated JSON-path query strings, default
`''`/`None`/`False` as shown), `title_html_to_text`/`content_html_to_text` (bool),
`time_range_support`/`time_range_url`/`time_range_map`, `safesearch`/`safe_search_map`,
`no_result_for_http_status` (list, default `[]`), `soft_max_redirects` (default `0`). **`categories`
and `shortcut` are not declared in `json_engine.py` at all** — they are handled generically by
the engine loader for *every* engine type, not something the JSON engine module itself
defines or requires (see below for whether they are required there).

**`request()` unconditionally disables the network layer's automatic raise-and-log** —
`json_engine.py:356`, `params['raise_for_httperror'] = False` — already the central finding of
B3.

**`response()` accepts a bare JSON array or a JSON object with a `results_query`-addressed
list**; if `results_query` is empty, the whole decoded JSON body is treated as the results
list (`json_engine.py:406-411`: `if results_query: rs = query(json, results_query)[0] ...
else: rs = json`). Each result item is passed through `extract_response_info`
(`json_engine.py:365-392`), which needs at minimum `url_query` and `title_query` to resolve
(a `try/except: return None` on failure) — `content_query` and `thumbnail_query` are
best-effort (caught and defaulted).

**`settings_loader.py`'s `keep_only`/`remove` and user-engine merge** — already fully quoted
and verified by the cited prior note (§5a, `searx/settings_loader.py:143-166` for
`keep_only`/`remove`, `:159-164` for the `user_engines`/`update_dict` merge branch); not
re-derived here. That note's finding stands: `use_default_settings: {engines: {keep_only:
[]}}` (an empty list, or a list naming no default engine) leaves **no** default engine in
`engines`, and the user's own top-level `engines:` list is then merged in by `name` — for a
`name` not among the defaults, this **adds** a brand-new engine entry (the `user_engines`
branch the prior note quoted), which is exactly the shape GIDEON's stub-as-one-engine CI setup
needs (`keep_only: []` to drop every real default engine, then one `engines:` entry naming
the stub with `engine: json_engine`).

**Correction (verified in the B7 follow-up below): neither `shortcut` nor `categories` is
required.** `searx/engines/__init__.py`'s `ENGINE_DEFAULT_ARGS` dict supplies
`"shortcut": "-"` and `"categories": ["general"]` as defaults, applied to every engine
namespace before `update_engine_attributes` overlays the settings.yml entry's own keys; a
user engine that names neither still loads. `register_engine` only enforces *uniqueness*
(`if engine.shortcut in engine_shortcuts: ... sys.exit(1)`), which cannot fire for a
single-engine (`keep_only: []` plus one `engines:` entry) CI config, since there is nothing
else loaded to collide with. See B7 for the full quotes.

### B6. `/search` route: non-2xx conditions, and the botdetection ERROR line

`searx/webapp.py`'s `/search` route (around lines 552-673 per the tool's location estimate;
not independently re-numbered in this note): returns **403** via `flask.abort(403)` when the
requested `format` is not in `settings['search']['formats']` (already established by the
cited prior note §5b, `webapp.py:625-631`, quoted there); returns **400** via
`index_error(output_format, 'No query'), 400` when `q` is empty/missing for a non-HTML
format; returns **400** via `index_error(output_format, e.message), 400` on a caught
`SearxParameterException`; and returns **500** via
```python
except Exception as e:
    logger.exception(e, exc_info=True)
    return index_error(output_format, gettext('search error')), 500
```
on any other unhandled exception at the route level (a bug in `search()` itself, not a
per-engine failure — per-engine failures are caught inside `online.py`'s own `search()`, B3,
and do not propagate up to this handler). **When every engine fails or a network error
occurs at the per-engine level, the route still answers HTTP 200** for the HTML format — the
per-engine failures are recorded on `result_container` (`add_unresponsive_engine`, via
`handle_exception`, B3) and rendered as empty results / an "unresponsive engines" notice in
the template, not surfaced as a non-2xx status; for the JSON format (`format=json`, what Open
WebUI actually sends), the same 200-with-partial-or-empty-results shape applies, since nothing
in `/search`'s exception handling distinguishes the JSON branch for this case — **this
specific empty-results/200 claim for the JSON format specifically was not independently
re-verified against `searx/webapp.py`'s JSON-serialization branch in this note**, only
inferred from the general per-engine-failure handling being format-agnostic; flagged as a
minor gap.

**The botdetection ERROR line carries no request data.** `searx/botdetection/trusted_proxies.py`:
```python
if not x_forwarded_for and not x_real_ip:
    log_error_only_once("X-Forwarded-For nor X-Real-IP header is set!")
```
`log_error_only_once` (`searx/botdetection/_helpers.py`, full file read):
```python
_logged_errors: list[str] = []

def log_error_only_once(err_msg: str):
    if err_msg not in _logged_errors:
        logger.error(err_msg)
        _logged_errors.append(err_msg)
```
The call passes only the static string `"X-Forwarded-For nor X-Real-IP header is set!"` — no
header value, IP, path, or other per-request data — and, being deduplicated by exact message
text, logs **at most once per process lifetime** regardless of how many requests trigger it.

### B7. A shipped engine module the CI stub can stand behind

Follow-up (coordinator, 2026-09-08): since `json_engine` disables `raise_for_httperror` (B3),
the CI run that must exercise `network.py:255` needs a **shipped, non-`json_engine` engine
module** left at its default, whose outbound URL the stub can be substituted into through
`settings.yml` alone. All five points below re-verified at the same SearXNG commit.

**1. The settings-to-attribute loader, and the `shortcut`/`categories` defaults.**
`searx/engines/__init__.py`'s `update_engine_attributes` is the generic loop that turns a
`settings.yml` engine entry into module attributes:
```python
for param_name, param_value in engine_data.items():
    if param_name == "about":
        continue
    if param_name == 'categories':
        if isinstance(param_value, str):
            param_value = list(map(str.strip, param_value.split(',')))
        engine.categories = param_value
    else:
        setattr(engine, param_name, param_value)
```
So `base_url: http://stub:8000` in a `bing`/`brave` entry's `settings.yml` block sets
`bing.base_url`/`brave.base_url` via the plain `setattr` branch (neither name is `about` or
`categories`), overriding the module's own hardcoded value read at import. This runs from
`load_engine`, whose full body (quoted for the defaulting order):
```python
def load_engine(engine_data):
    ...
    engine = load_module(module_name + '.py', ENGINE_DIR)
    check_engine_module(engine)
    update_engine_attributes(engine, engine_data)
    update_attributes_for_tor(engine)
    ...
    if not is_engine_active(engine):
        return None
    if is_missing_required_attributes(engine):
        return None
    set_loggers(engine, engine_name)
    if not call_engine_setup(engine, engine_data):
        return None
    if not any(cat in settings['categories_as_tabs'] for cat in engine.categories):
        engine.categories.append(DEFAULT_CATEGORY)
    return engine
```
(`load_module` itself applies `ENGINE_DEFAULT_ARGS` to the namespace before `load_engine`'s
caller reaches `update_engine_attributes` — confirmed by the defaults existing on the engine
namespace prior to any settings.yml overlay.) `ENGINE_DEFAULT_ARGS`:
```python
ENGINE_DEFAULT_ARGS: dict[str, t.Any] = {
    "engine_type": "online", "paging": False, "max_page": 0, "time_range_support": False,
    "safesearch": False, "language_support": False, "categories": ["general"], "language": "",
    "region": "", "enable_http": False, "shortcut": "-", "timeout": settings["outgoing"]["request_timeout"],
    "display_error_messages": True, "disabled": False, "inactive": False, "about": EngineAbout(),
    "using_tor_proxy": False, "send_accept_language_header": True, "tokens": [], "weight": 1.0,
}
```
**`shortcut` defaults to `"-"` and `categories` defaults to `["general"]` — neither must be
set by a user engine entry.** `is_missing_required_attributes` (the only "required attribute"
gate `load_engine`'s docstring refers to) is generic and content-blind:
```python
def is_missing_required_attributes(engine):
    """An attribute is required when its name doesn't start with `_`. Required attributes
    must not be None."""
    missing = False
    for engine_attr in dir(engine):
        if not engine_attr.startswith('_') and getattr(engine, engine_attr) is None:
            logger.error('Missing engine config attribute: "{0}.{1}"'.format(engine.name, engine_attr))
            missing = True
    return missing
```
— it only fails an engine whose *some* attribute is literally `None`; since `shortcut`/`categories`
both have non-`None` defaults, an engine entry naming neither passes. `register_engine`
enforces uniqueness, not presence:
```python
def register_engine(engine):
    if engine.name in engines:
        logger.error('Engine config error: ambiguous name: {0}'.format(engine.name))
        sys.exit(1)
    engines[engine.name] = engine
    if engine.shortcut in engine_shortcuts:
        logger.error('Engine config error: ambiguous shortcut: {0}'.format(engine.shortcut))
        sys.exit(1)
    engine_shortcuts[engine.shortcut] = engine.name
    for category_name in engine.categories:
        categories.setdefault(category_name, []).append(engine)
```
For a CI config that does `use_default_settings: {engines: {keep_only: []}}` plus exactly one
`engines:` entry (the stub, per B5), there is only ever one engine loaded, so this
uniqueness check cannot fire regardless of whether `shortcut`/`categories` are specified.

**2. `searx/engines/bing.py`.** Module-level URL:
```python
base_url = "https://www.bing.com"
"""Bing-Web search URL"""
```
`request()` builds the URL with the query in the query string, and never touches
`raise_for_httperror` (searched the whole file — the string does not appear):
```python
def request(query: str, params: "OnlineParams"):
    engine_region = traits.get_region(params["searxng_locale"], traits.all_locale)
    override_accept_language(params, engine_region)
    query_params: dict[str, str | int] = {
        "q": query,
        "adlt": _safesearch_map.get(params.get("safesearch", 0), "off"),
    }
    locale_params = get_locale_params(engine_region)
    if locale_params:
        query_params.update(locale_params)
    params["url"] = f"{base_url}/search?{urlencode(query_params)}"
```
So overriding `base_url` alone (e.g. `http://stub:8000`) is enough — `request()` appends its
own `/search` path segment, so the stub receives `GET /search?q=<query>&adlt=off` and nothing
else needs to change. `request()` sets no cookies and only conditionally overrides one header
(`Accept-Language`, from `override_accept_language`, computed from `params`/`traits`, not from
any prior response) — **no prior request/response dependency**. `response()` walks a fixed
XPath (`//ol[@id="b_results"]/li[contains(@class, "b_algo")]`) over the parsed HTML body and
appends a result per match; against a minimal `<html><body></body></html>` body, `html.fromstring`
parses fine and `eval_xpath_list` returns an empty list, so the `for item in
eval_xpath_list(...)` loop runs zero times and `response()` **returns `[]` with no
exception** — a 200-with-empty-page response from the stub is silently accepted as "zero
results," not an error (so proving the ERROR-log scenario needs the stub to answer a genuine
non-2xx status, not just an empty body).

**3. `searx/engines/brave.py`, as a second candidate.** Module-level URL:
```python
base_url = "https://search.brave.com/"
```
`request()`:
```python
args: dict[str, t.Any] = {"q": query, "source": "web"}
...
params["url"] = f"{base_url}{brave_category}?{urlencode(args)}"
```
— the query is likewise in the URL's query string. `raise_for_httperror` does not appear
anywhere in this file either (same default-`True` path as bing.py). Unlike bing.py, `request()`
**does** set several cookies, but each is computed locally from `params`/config, not fetched
from a prior response: `params["cookies"]["safesearch"] = safesearch_map.get(params["safesearch"], "off")`,
`params["cookies"]["useLocation"] = "0"`, `params["cookies"]["country"] = engine_region.split("-")[-1].lower()`,
`params["cookies"]["ui_lang"] = ui_lang` — no prior request/response dependency here either.
`response()` builds an `EngineResults()` and appends per XPath match the same way bing.py
does; against an empty/no-match page the result-building loops simply execute zero times and
an empty `EngineResults()` is returned, no exception raised. **Caveat**: `brave_category` is a
path fragment concatenated directly onto `base_url` (not re-quoted here in full — this note
did not pin its exact value/leading-slash shape), so a stub standing in for `brave` needs
`base_url` chosen with that concatenation in mind (e.g. ending or not ending in `/` to match);
**bing.py's hardcoded `/search` suffix inside `request()` itself makes it the simpler of the
two to point at a bare `http://stub:8000` `base_url` with no further shape assumptions.**

**4. Logger names — `searx.network` is the parent of every per-engine network logger, and a
`dictConfig` level set on it alone covers all of them.** `searx/network/network.py`:
```python
from searx import logger, sxng_debug
...
logger = logger.getChild('network')
```
— the module logger is `searx.network` (a child of the top-level `searx` logger,
`searx/__init__.py:28`, `logger = logging.getLogger('searx')`). Each `Network` object (one is
constructed per configured `network:` — every engine either shares the default network or has
its own, keyed by name) takes its own logger in `__init__`:
```python
def __init__(self, ..., logger_name: str = None):
    ...
    self._logger = logger.getChild(logger_name) if logger_name else logger
```
`logger_name` is supplied by the caller as the network/engine's own name, so a per-engine
network's logger is named `searx.network.<name>` (e.g. `searx.network.bing`,
`searx.network.brave`, or the stub's own configured network/engine name) — a genuine child of
`searx.network` in Python's dotted logger hierarchy. **No `.setLevel()` call exists anywhere
in `searx/network/network.py`** on either the module logger or any per-network child (searched
the whole file) — so none of these loggers ever carries its own explicit level; each inherits
its *effective* level from the nearest ancestor that has one set. A `GRANIAN_LOG_CONFIG`
`dictConfig` entry that sets a level on `searx.network` alone (not needing to enumerate
`searx.network.bing`, `searx.network.brave`, etc. individually) therefore governs every one of
them, including whichever engine's network object logs `network.py:255`'s "HTTP Request
failed" warning — confirming B2's fix works uniformly across every engine, not just a
specifically-named one.

**5. `webapp.py`'s JSON branch answers 200 with an empty `results` list when every engine
fails — now confirmed from source (resolves item 8 of "what could not be verified" below).**
```python
# searx/webapp.py, /search route, JSON branch
if output_format == 'json':
    response = webutils.get_json_response(search_query, result_container)
    return Response(response, mimetype='application/json')
```
No status code is set on this `Response`, so Flask answers the default `200`. `get_json_response`
(`searx/webutils.py`):
```python
def get_json_response(sq: "SearchQuery", rc: "ResultContainer") -> str:
    """Returns the JSON string of the results to a query (``application/json``)"""
    data = {
        'query': sq.query,
        'results': [_.as_dict() for _ in rc.get_ordered_results()],
        'answers': [_.as_dict() for _ in rc.answers],
        'corrections': list(rc.corrections),
        'infoboxes': rc.infoboxes,
        'suggestions': list(rc.suggestions),
        'unresponsive_engines': get_translated_errors(rc.unresponsive_engines),
    }
    response = json.dumps(data, cls=JSONEncoder)
    return response
```
There is no length check or conditional anywhere in this function — when every engine fails,
`rc.get_ordered_results()` is empty and `'results': []` is serialized exactly like a
successful-but-empty search; the only difference is `'unresponsive_engines'` being populated
(via `handle_exception`'s `add_unresponsive_engine`, B3 — a translated, generic per-engine
error message, not a URL). **So Open WebUI's `search_searxng` (which calls
`response.raise_for_status()` on its own aiohttp response, `searxng.py:71`, per the cited
prior note §11) never sees a non-2xx from a "no results" SearXNG answer — only a genuine
SearXNG-side non-2xx (403/429/500, B6) would trigger the aiohttp-side `ClientResponseError`
that leak line depends on.**

**Net answer for the CI plan**: wire the stub as `bing` (simplest — no cookies, one path
segment hardcoded in `request()`) with `settings.yml`'s `engines:` entry overriding only
`base_url` to the stub's Compose-network address; `raise_for_httperror` stays at its
`online.py` default `True` for this engine, so a stub response with a genuine non-2xx status
(not just an empty body) reaches `network.py:255` through the same automatic
`patch_response` path B3 traced, producing the query-bearing WARNING the sentinel test needs
to prove against — unlike a `json_engine`-wired stub, which B3 showed cannot reach that line
at all.

---

## Part C — Caddy

Pin: `images.lock`'s `caddy: source: docker.io/library/caddy:2.11` — a **tracking** minor-series
tag; the newest matching tag on `caddyserver/caddy`'s GitHub releases is `v2.11.4` (checked via
the GitHub tags API), which is the tag this note's Go-source citations were read at. The lock's
own digest was not independently re-resolved against Docker Hub to confirm it is byte-identical
to `v2.11.4` specifically (see the sources note at the top) — the struct/behavior cited below
has been stable across the 2.11 series, so this does not put the *facts* in doubt, only the
exact patch-tag label.

**Access-log fields.** `modules/caddyhttp/marshalers.go`'s `LoggableHTTPRequest.MarshalLogObject`
(at `v2.11.4`) adds these fields to the structured access-log entry: `remote_ip`, `remote_port`,
`client_ip`, `proto`, `method`, `host`, `uri` (the request's URI, which for Caddy — like any
standard HTTP access logger — includes the query string), `headers`, `transfer_encoding`, `tls`.

**Header redaction, and the `log_credentials` option.** `modules/caddyhttp/logging.go`'s
`ServerLogConfig` struct declares:
```go
ShouldLogCredentials bool `json:"should_log_credentials,omitempty"`
```
with a doc comment: "If true, credentials that are otherwise omitted, will be logged. The
definition of credentials is defined by https://fetch.spec.whatwg.org/#credentials, and this
includes some request and response headers, i.e `Cookie`, `Set-Cookie`, `Authorization`, and
`Proxy-Authorization`." **Default is `false`** (the zero value of a Go `bool`), so these four
headers are redacted **by default**. Caddy's own docs (`caddyserver.com/docs/caddyfile/directives/log`,
checked against the same `2.11` line) state the redaction explicitly and name the Caddyfile
knob: "By default, headers with potentially sensitive information (`Cookie`, `Set-Cookie`,
`Authorization` and `Proxy-Authorization`) will be logged as `REDACTED` in access logs," and
this "can be disabled with the `log_credentials` global server option" — confirmed to be a
**global `options` block** setting (`caddyserver.com/docs/caddyfile/options#log-credentials`),
not a per-`log`-directive subdirective (this note did not find `log_credentials` parsed inside
`caddyconfig/httpcaddyfile/builtins.go`'s `parseLog`/`parseLogSkip`/`parseLogName` functions,
consistent with it living in the separate global-options parser instead — not independently
traced to that parser's source file in the time available).

**Request/response bodies are never in the access log.** Not found anywhere in
`marshalers.go`'s field list above, nor mentioned by the docs page as a loggable field —
Caddy's structured access log is metadata-only (method, URI, headers, status, timing), by
construction of what `MarshalLogObject` emits.

---

## Part D — Docker and systemd (docs, as instructed)

**`logging: driver: none`** (Docker Compose service option → Docker Engine's `none` logging
driver). Docker's own docs (`docs.docker.com/engine/logging/configure/`, current at fetch
time): "No logs are available for the container and `docker logs` does not return any
output." This applies identically to `docker compose logs <service>` (which shells out to the
same driver-backed log read as `docker logs`) — with `driver: none`, that command returns
nothing for the service, and nothing reaches the systemd journal either, since the `none`
driver captures no output from the container at all (there is no secondary path to the
journal independent of the configured driver).

**`journald` driver options.** Same docs page: accepted options are `tag` (log tag /
`SYSLOG_IDENTIFIER` template), `labels`/`labels-regex`, `env`/`env-regex` (include selected
container labels/env vars as journal fields) — **no content-filtering or redaction option** is
documented for this driver; it forwards whatever the container writes to stdout/stderr,
verbatim, as the journal entry's `MESSAGE=` field, tagged with fields including
`CONTAINER_ID`/`CONTAINER_ID_FULL`/`CONTAINER_NAME` (fixed at container start; a later
`docker rename` does not update already-written entries) and
`CONTAINER_TAG`/`SYSLOG_IDENTIFIER`/`IMAGE_NAME`.

**`LogFilterPatterns=` is per-unit, and every journald-driver container's entries are
submitted under the same unit.** From systemd's own `NEWS` file for the `v253` release
(`raw.githubusercontent.com/systemd/systemd/v253/NEWS`, the version that introduced it):
"A new `LogFilterPatterns=` option has been added for units. It may be used to specify
accept/deny regular expressions for log messages generated by the unit, that shall be
enforced by systemd-journald. Rejected messages are neither stored in the journal nor
forwarded. This option may be used to suppress noisy or uninteresting messages from units."
It is documented as a `systemd.exec(5)` setting (confirmed via `systemd.directives(7)`'s
index, per a web search of systemd's own manpage cross-reference — not independently loaded
from `freedesktop.org`'s own man page in this note, which returned 403/incomplete fetches;
the `NEWS`-file quote above is the primary-source citation used). **Because `dockerd` (or the
container runtime it delegates to) is the process that submits journald-driver container log
entries — not the container's own process running as a distinct systemd unit — those entries
are attributed to whichever unit `dockerd`'s own process belongs to (typically
`docker.service`)**, not to a per-container unit; a `LogFilterPatterns=` set on that one unit
would therefore apply to **every** container using the `journald` driver on that host at
once, not to one specific container. This attribution point is architectural reasoning from
how the `journald` driver is documented to work (Docker's own docs do not name the
`_SYSTEMD_UNIT` journal field explicitly for container entries) rather than a single quoted
sentence confirming it — **flagged as the weakest-sourced claim in Part D**.

---

## What could not be verified

1. **A3 / langchain_community's `fetch_all`** — whether it swallows a per-URL fetch exception
   internally before it reaches `SafeWebBaseLoader.alazy_load`/`aload`, or lets the first
   failure abort the whole batch (which is what would make `routers/retrieval.py:2960`
   reachable from one bad URL). Could not find a matching GitHub tag for
   `langchain-community==0.4.2` (a separate release train from the `langchain` monorepo's own
   tags) in the time available.
2. **A2 / `constants.py`'s exact line number** for `_error_message` (`ERROR_MESSAGES.DEFAULT`)
   — quoted from a text-processing fetch, not a byte-for-byte download; the function's
   behavior (returns a fixed string for any `Exception` input) is high-confidence, the line
   number is not.
3. **A3, A6, A7's line numbers for OWUI files fetched only by targeted excerpt** (not the
   whole file downloaded and re-numbered) carry the caveat stated once at the top of this
   note.
4. **A7 / whether OpenTelemetry's `LoggingHandler` attaches a chained exception's traceback
   text as an exported log attribute** the way loguru's own sinks do — not traced past
   `InterceptHandler.emit`'s call into `otel_handler.emit(record)`.
5. **B3 / curl_cffi's `HTTPError.__str__` shape at `0.16.1`** — not re-read from curl_cffi's
   own source in this note (largely moot for the `json_engine` path specifically, per B3's own
   reasoning, but left open for a non-`json_engine` engine).
6. **B4** — the specific DEBUG-level lines in `searx/webapp.py`'s route handler and
   `searx/search/__init__.py`/`models.py` that would show raw query text under
   `general.debug: true` were not individually traced to file:line; only the mechanism
   (raising the root threshold) is confirmed.
7. ~~**B5 / `shortcut` uniqueness or presence requirement**~~ — **resolved in B7**: neither
   `shortcut` (default `"-"`) nor `categories` (default `["general"]`) is required; only
   cross-engine uniqueness is enforced, moot for a single-engine CI config.
8. ~~**B6 / the JSON-format (`format=json`) response's exact shape when every engine fails**~~
   — **resolved in B7**: `webapp.py`'s JSON branch and `webutils.get_json_response` confirm
   HTTP 200 with `'results': []` and a populated (generic, non-URL) `'unresponsive_engines'`.
9. **B7 / `brave.py`'s `brave_category` value and exact concatenation shape** — not pinned in
   this note; flagged as a reason to prefer `bing.py` for the CI stub, which has no such
   ambiguity.
10. **B7 / whether every engine's `network:` configuration actually results in a distinct
    `searx.network.<name>` child logger** (as opposed to several engines sharing one network
    object/logger) was confirmed for the *mechanism* (`Network.__init__`'s `logger_name`
    parameter) but not traced through `searx/network/__init__.py`'s own per-engine network
    construction/caching to confirm every engine gets its own `Network` instance rather than
    a shared default — immaterial to B7's conclusion either way, since a shared default
    network's logger is just `searx.network` itself, still covered by the same dictConfig
    entry.
11. **Caddy's exact patched tag** the `2.11` tracking pin resolves to (see the sources note) —
   this note cites `v2.11.4`, the newest `2.11.x` GitHub tag at verification time, not a
   digest-confirmed match to `images.lock`'s pinned digest.
12. **Part D's unit-attribution claim** for journald-driver container logs — architectural
    reasoning from how the driver is documented, not a single directly-quoted primary source
    naming `_SYSTEMD_UNIT=docker.service` for container entries specifically.

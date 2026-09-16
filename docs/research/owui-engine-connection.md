---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — frontend-to-engine Chat Completions connection

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`,
`package.json` version `0.11.3`). All backend paths are relative to
`backend/open_webui/`; frontend paths are relative to `src/`. `docs.openwebui.com`
was not consulted — every claim below is read directly from this tag's source.

Context assumed throughout: `ENABLE_PERSISTENT_CONFIG=false` (env-only config),
one OpenAI-compatible connection to `http://gideon-generator:8000/v1` (vLLM
v0.27.1, `--reasoning-parser qwen3`, bearer key), no admin-panel edits.

Config system at this tag (confirmed unchanged from the earlier
`research/owui-v0.11.0-deployment-contract` note, re-verified here): a per-key
DB table (`models/config.py`'s `Config` class), not the legacy single-blob
`PersistentConfig`. `Config.persistent_enabled_for(key)` (`models/config.py:133-138`)
returns `False` for every key once `ENABLE_PERSISTENT_CONFIG=false`
(`config.py:3237`, `os.getenv('ENABLE_PERSISTENT_CONFIG', 'True')`), and every
`Config.get`/`get_many` read then falls through to `Config.DEFAULTS[key]` —
the value computed once from the env var at process import — never a DB
row. This is the mechanism behind every "PersistentConfig status" answer
below: all of `openai.*` and `task.*` keys are gated by this same flag.

---

## 1. The OpenAI-compatible connection from env

All of `config.py:308-364` ("OPENAI_API" section):

```python
ENABLE_OPENAI_API = os.getenv('ENABLE_OPENAI_API', 'True').lower() == 'true'   # config.py:317

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')                              # config.py:320 (singular)
OPENAI_API_BASE_URL = os.getenv('OPENAI_API_BASE_URL', '')                    # config.py:321 (singular)
# ... OPENAI_API_BASE_URL defaults to 'https://api.openai.com/v1' if empty,
#     and a trailing '/' is stripped (config.py:325-330)

OPENAI_API_KEYS = os.getenv('OPENAI_API_KEYS', '')
OPENAI_API_KEYS = OPENAI_API_KEYS if OPENAI_API_KEYS != '' else OPENAI_API_KEY # config.py:333-334
OPENAI_API_KEYS = [url.strip() for url in OPENAI_API_KEYS.split(';')]         # config.py:336 — ';'-separated

OPENAI_API_BASE_URLS = os.getenv('OPENAI_API_BASE_URLS', '')
OPENAI_API_BASE_URLS = OPENAI_API_BASE_URLS if OPENAI_API_BASE_URLS != '' else OPENAI_API_BASE_URL  # config.py:339-340
OPENAI_API_BASE_URLS = [
    url.strip() if url != '' else 'https://api.openai.com/v1' for url in OPENAI_API_BASE_URLS.split(';')
]                                                                              # config.py:342-344 — ';'-separated

OPENAI_API_CONFIGS = {}
_openai_api_configs = os.getenv('OPENAI_API_CONFIGS', '')                    # config.py:347-355
if _openai_api_configs:
    try:
        parsed = JSONCodec.loads(_openai_api_configs)
        if isinstance(parsed, dict):
            OPENAI_API_CONFIGS = parsed
        else:
            log.warning('OPENAI_API_CONFIGS must be a JSON object, ignoring')
    except (JSONCodec.JSONDecodeError, TypeError):
        log.warning('OPENAI_API_CONFIGS is not valid JSON, ignoring')
```

**Names/formats/defaults:**
- `ENABLE_OPENAI_API` — bool, default `True`.
- `OPENAI_API_BASE_URLS` — `;`-separated list, default falls back to the
  singular `OPENAI_API_BASE_URL`, which itself defaults to
  `https://api.openai.com/v1`.
- `OPENAI_API_KEYS` — `;`-separated list, default falls back to the singular
  `OPENAI_API_KEY` (empty string default).
- `OPENAI_API_CONFIGS` — a single JSON **object**, default `{}` (unset ⇒
  empty dict, confirmed at `config.py:347`). Not a list.

These are registered as persisted-config-eligible keys at `config.py:2841-2843`
(`'openai.enable'`, `'openai.api_keys'`, `'openai.api_base_urls'`,
`'openai.api_configs'`) — i.e. `PersistentConfig`-gated exactly like every
other admin setting, so with `ENABLE_PERSISTENT_CONFIG=false` these four
values are frozen at the env-derived default for the life of the process
(`models/config.py:133-138`, `196-218`).

**Key/URL pairing — by index.** `routers/openai.py`'s
`get_openai_connection(idx)` (`openai.py:340-345`):
```python
async def get_openai_connection(idx: int) -> tuple[str, str, dict]:
    _, api_base_urls, api_keys, api_configs = await get_openai_runtime_config()
    url = api_base_urls[idx]
    key = api_keys[idx]
    api_config = api_configs.get(str(idx), api_configs.get(url, {}))
    return url, key, api_config
```
`OPENAI_API_CONFIGS` keys are the **string index** into the URL/key lists
("0", "1", …) — confirmed by the admin `/config/update` handler
(`openai.py:551-568`), which validates `api_configs` keys against
`valid_keys = set(map(str, range(len(form_data.OPENAI_API_BASE_URLS))))`.
The `api_configs.get(url, {})` fallback is explicitly commented `# Legacy
support` (`openai.py:151-153`, `690-693`) — a pre-index config format kept
for old configs, not the primary key. If `len(OPENAI_API_KEYS) !=
len(OPENAI_API_BASE_URLS)`, `normalize_openai_api_keys` pads/truncates the
key list to match the URL list length by position (`openai.py:330-337`),
reinforcing index-based pairing.

**What `OPENAI_API_CONFIGS[idx]` can carry**, gathered from every
`api_config.get(...)` call site in `routers/openai.py` and the admin
`AddConnectionModal.svelte`:
`enable` (bool, default `True`, `openai.py:699`), `model_ids` (list, default
`[]`, `openai.py:700`), `prefix_id` (`openai.py:481,735,962,...`),
`connection_type` (default `'external'`, `openai.py:734`), `tags` (list,
`openai.py:736`), `provider` (`openai.py:737`), `auth_type` (`'bearer'` |
`'none'` | `'session'` | `'system_oauth'` | `'azure_ad'` |
`'microsoft_entra_id'`, default `'bearer'`, `openai.py:184-212`), `headers`
(dict, custom headers, `openai.py:213-215`), `azure` / `api_version`
(`openai.py:1571-1584`), and **`api_type`** (`openai.py:1567`).

**The per-connection Chat-Completions-vs-Responses switch — found.** It is
`api_type`, checked once, in the main chat-completion route:
```python
is_responses = api_config.get('api_type') == 'responses'   # openai.py:1567
```
and used a few lines later to pick the request path (`openai.py:1573-1601`):
non-Azure case, `else: request_url = f'{url}/chat/completions'` when
`is_responses` is falsy, `f'{url}/responses'` (with
`convert_to_responses_payload`) when `api_config['api_type'] == 'responses'`.
**Default when `OPENAI_API_CONFIGS` is unset (or the connection's entry omits
`api_type`): `api_config.get('api_type')` is `None`, `is_responses` is
`False`, so the request goes to `POST {base_url}/chat/completions`.** The
admin UI encodes the same default explicitly:
`src/lib/components/AddConnectionModal.svelte:50`:
```js
let apiType = ''; // '' = chat completions (default), 'responses' = Responses API
```
There is a **separate, unrelated** `@router.post('/responses')` endpoint
(`openai.py:1847`) that forwards arbitrary `ResponsesForm` payloads
(`openai.py:1827-1842`) directly to `{url}/responses`; it is not wired into
`generate_chat_completion`/the chat pipeline (`utils/chat.py:295-303` only
ever calls the `/chat/completions`-mapped `generate_openai_chat_completion`)
and is not reached unless something explicitly POSTs to it. It is not a
per-connection toggle and does not affect a normal chat turn.

**Conclusion for the plan:** with no `api_type` set for the `gideon-generator`
connection, every chat turn goes to `http://gideon-generator:8000/v1/chat/completions`
by default — matches the plan's requirement without any extra config.

---

## 2. Model discovery and an engine that answers later

**When it's fetched:** on every `/api/models` (and `/api/v1/models`) request
from a verified user (`main.py:874-877`, `get_all_models(request, refresh=...,
user=user)`), not only at startup. The startup pre-fetch at `main.py:395-417`
only runs `if await Config.get('models.base_models_cache')`, and that flag's
env default is `ENABLE_BASE_MODELS_CACHE = os.getenv('ENABLE_BASE_MODELS_CACHE',
'False')` (`config.py:311`) — **off by default**, so with GIDEON's plain env
config the model list is not fetched once and frozen at boot; it is
re-fetched (subject to the cache below) on demand.

**Caching:** `openai.get_all_models` is wrapped in `@cached(ttl=MODELS_CACHE_TTL,
...)` (`openai.py:792-798`), and:
```python
MODELS_CACHE_TTL = os.getenv('MODELS_CACHE_TTL', '1')   # env.py:1021, default 1 second
```
So a stale/empty model list is refreshed at most 1 second after being read —
effectively "the next request after the engine starts listening" sees it.

**Exception handling on a refused connection:** `send_get_request`
(`openai.py:88-118`) wraps the `aiohttp.ClientSession.get(...)` in a bare
`try/except Exception`:
```python
    except Exception as e:
        # Handle connection error here
        log.error(f'Connection error: {e}')
        return None
```
(`openai.py:116-117`). A refused connection (engine not listening yet) logs
one line and returns `None` — no crash, no exception propagates. In
`get_all_models_responses`/`get_all_models` (`openai.py:678-865`), `None`
responses are filtered out by `get_merged_models`'s `if model_list is not
None and 'error' not in model_list` guard (`openai.py:832`), so the engine's
absence yields an empty model entry for that connection, not a 500 or an
empty overall page — other configured connections (if any) are unaffected.
The one-time startup pre-fetch (only relevant if
`ENABLE_BASE_MODELS_CACHE=true`) is separately wrapped in `try/except
Exception` with `log.warning('Failed to pre-fetch models at startup: %s', e)`
(`main.py:417-418`) — also non-fatal.

**Model appears without a frontend restart:** yes — the frontend calls
`getModels()` → `GET /api/models` (`src/lib/apis/index.ts:23-36`) from
`src/routes/+layout.svelte` and `src/routes/(app)/+layout.svelte` (both
`import`/call `getModels`), i.e. on every navigation/load of the app shell,
not once per browser session. Combined with the 1-second cache TTL, the
model shows up on the very next such fetch after the engine's listener opens
— no explicit restart of the Open WebUI container is needed.

**`model_ids` skips the fetch — confirmed.** In
`get_all_models_responses` (`openai.py:696-720`):
```python
enable = api_config.get('enable', True)
model_ids = api_config.get('model_ids', [])
if enable:
    if len(model_ids) == 0:
        request_tasks.append(get_models_request(request, url, api_keys[idx], user=user, config=api_config))
    else:
        model_list = {'object': 'list', 'data': [{'id': model_id, ...} for model_id in model_ids]}
        request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, model_list)))
```
When `model_ids` is non-empty, a synthetic model list is built in-process
(`asyncio.sleep(0, model_list)`) and `GET {base}/models` is **never called**
for that connection. (Also true for the non-streaming `GET /api/openai/models`
route — `openai.py:891`, `'data': api_config.get('model_ids', []) or []`.)
This means: if GIDEON's rendered `OPENAI_API_CONFIGS` sets `model_ids:
["gideon-generator"]`, the model appears in the UI immediately at process
start (synthetically), *before* the engine ever answers — worth flagging,
since that changes the "appears once the engine starts listening" story: a
chat turn sent to that synthetic model entry before the engine is ready would
still hit a refused connection at completion time, just with the model
already selectable.

---

**On-box observation (slice-1 ticket 05, `.scratch/slice-1/assets/05-on-box.txt` §5):**
with the engine stopped, the frontend's `/api/models` kept listing
`gideon-generator` while `send_get_request` logged its one `Connection error`
line, and the model needed no frontend restart once the engine was back. The
running frontend therefore keeps its last merged list across a failed fetch
rather than showing the "empty entry" this section's reading of
`get_merged_models` predicts; the exact path that retains it was not traced.
The tolerance the ticket needs holds either way.

---

## 3. The task model

**Selection function** — `utils/task.py:16-27`:
```python
def get_task_model_id(default_model_id: str, task_model: str, task_model_external: str, models) -> str:
    task_model_id = default_model_id
    if models.get(task_model_id, {}).get('connection_type') == 'local':
        if task_model and task_model in models:
            task_model_id = task_model
    else:
        if task_model_external and task_model_external in models:
            task_model_id = task_model_external
    return task_model_id
```
It is called from `get_task_model_generation_config` (`routers/tasks.py:80-99`)
with `config.get('task.model.default')` (⇐ `TASK_MODEL`) and
`config.get('task.model.external')` (⇐ `TASK_MODEL_EXTERNAL`).

**Which one applies to an OpenAI-compatible model:** `connection_type` for an
OpenAI-compatible connection defaults to `'external'`
(`openai.py:734,842`: `api_config.get('connection_type', 'external')` /
`model.get('connection_type', 'external')`) — `'local'` is only the Ollama
default (`utils/models.py:46`, `fetch_ollama_models`). So unless GIDEON's
rendered config explicitly sets `connection_type: 'local'` for the
`gideon-generator` connection, `gideon-generator`'s entry in
`request.app.state.MODELS` has `connection_type == 'external'`, which routes
task-model selection into the **`else` branch → `TASK_MODEL_EXTERNAL`**, not
`TASK_MODEL`. `TASK_MODEL` only applies to models whose connection is marked
`'local'` (Ollama, or an OpenAI connection explicitly tagged
`connection_type: 'local'`).

**Fallback to the chat's model when unset:** `task_model_id` is initialised
to `default_model_id` (the chat's own model) and is only overridden if the
configured task model string is truthy *and* present in `models`
(`task.py:21,24`). With `TASK_MODEL_EXTERNAL` at its default `''`
(falsy), the condition is never true, so **`task_model_id` stays
`gideon-generator`** — task calls run on the same engine/model as the chat,
with no admin action required.

**Defaults / PersistentConfig status:**
- `TASK_MODEL = os.getenv('TASK_MODEL', '')` — `config.py:2193`.
- `TASK_MODEL_EXTERNAL = os.getenv('TASK_MODEL_EXTERNAL', '')` —
  `config.py:2195`.
- Registered as `'task.model.default'` / `'task.model.external'`
  (`config.py:3135-3136`), gated by the same `ENABLE_PERSISTENT_CONFIG` flag
  as everything else (`routers/tasks.py`'s `TASK_CONFIG_KEYS` maps them
  identically, `tasks.py:41-42`).

**Task-toggle env vars and defaults** (`config.py`):
| Var | Line | Default |
|---|---|---|
| `ENABLE_TITLE_GENERATION` | 2312 | `True` |
| `ENABLE_TAGS_GENERATION` | 2310 | `True` |
| `ENABLE_SEARCH_QUERY_GENERATION` | 2315 | `True` |
| `ENABLE_RETRIEVAL_QUERY_GENERATION` | 2317 | `True` |
| `ENABLE_FOLLOW_UP_GENERATION` | 2308 | `True` |
| `ENABLE_AUTOCOMPLETE_GENERATION` | 2346 | `False` |
| `ENABLE_IMAGE_PROMPT_GENERATION` | 1352 | `true` |

No `ENABLE_EMOJI_GENERATION` or `ENABLE_MOA_GENERATION`/toggle exists at this
tag — `generate_emoji` (`tasks.py:557-604`) and `generate_moa_response`
(`tasks.py:611-655`) have **no enable-flag gate at all** in `routers/tasks.py`;
they run whenever something (a UI reaction button / a manual API call) POSTs
to `/emoji/completions` or `/moa/completions`. (Grepped
`ENABLE_.*GENERATION|EMOJI|MOA` across `config.py`; nothing else found —
flag: this is an absence claim, re-check if a toggle exists under a
differently-named key such as `ui.*` before relying on it.)

**What a task call sends** (from `routers/tasks.py`, each route builds its
own `payload` dict then calls `apply_task_model_params` then
`generate_chat_completion`):
- `stream`: **always `False`** for title (182–187), follow-ups (249–252),
  tags (314–317), image-prompt (373–376), queries (450–453), autocomplete
  (526–529), emoji (581–584). Only `generate_moa_response` honours the
  caller's own stream flag: `'stream': form_data.get('stream', False)`
  (`tasks.py:639`).
- `max_tokens`/cap: only two routes set one explicitly.
  - Title: `task_model_params = task_model_params or {'max_tokens':
    models[task_model_id].get('info', {}).get('params', {}).get('max_tokens',
    1000)}` (`tasks.py:180-183`) — **1000** unless the model's own preset (or
    admin `TASK_MODEL_PARAMS`) already set one.
  - Emoji: `apply_task_model_params(payload, models, task_model_id,
    {'max_tokens': 4})` (`tasks.py:599`) — **hard-coded 4**.
  - Follow-ups, tags, image-prompt, queries, autocomplete: no inline
    default; `apply_task_model_params(payload, models, task_model_id,
    task_model_params)` is called with `task_model_params` from
    `get_task_model_generation_config`, which is `{}` unless an admin set
    `TASK_MODEL_PARAMS` (default `None` → `{}`, `tasks.py:88-89`). Given
    `apply_task_model_params` (`tasks.py:74-78`) is a no-op when both
    `params` and `payload.get('params')` are falsy, **these five task calls
    carry no cap at all by default** — they inherit whatever default vLLM
    applies.
- Temperature: **never set** by any task route; only reaches the payload via
  `TASK_MODEL_PARAMS`/a model preset, same mechanism as `max_tokens` above.
- **Nothing suppresses thinking.** Grepped every task route in
  `routers/tasks.py` for `reasoning_effort`, `chat_template_kwargs`, `think`,
  `enable_thinking` — none appear. `apply_model_params_to_body_openai`
  (`utils/payload.py:164-186`) does forward a `reasoning_effort` param *if* a
  model preset sets one (`mappings['reasoning_effort'] = str`,
  `payload.py:181`), but no task route sets it itself, and there is no
  `chat_template_kwargs`/`enable_thinking` passthrough anywhere in this
  tag's `payload.py` or `tasks.py`. **Confirms the ticket's concern
  concretely**: title generation (`max_tokens: 1000`, no thinking
  suppression) and emoji generation (`max_tokens: 4`) can exhaust their
  budget while `gideon-generator` is still inside its `reasoning` delta,
  yielding an empty `content` in the non-streaming response.

**Title generation on empty/failed content — the exact fallback**
(`utils/middleware.py:3762-3800`, this is where `generate_title` is actually
invoked after a chat turn, not from the frontend):
```python
if res and isinstance(res, dict):
    if len(res.get('choices', [])) == 1:
        response_message = res.get('choices', [])[0].get('message', {})
        title_string = (
            response_message.get('content')
            or response_message.get('reasoning_content')
            or message.get('content', user_message)
        )
    else:
        title_string = ''
    title_string = title_string[title_string.find('{') : title_string.rfind('}') + 1]
    try:
        title = JSONCodec.loads(title_string).get('title', user_message)
    except Exception as e:
        title = ''
    if not title:
        title = messages[0].get('content', user_message)
    await Chats.update_chat_title_by_id(metadata['chat_id'], title)
```
Notable: the non-streaming fallback chain checks `response_message.get('reasoning_content')`
(not `reasoning`) as its second choice before falling back to the assistant's
own answer/user message — **if `content` is empty but vLLM populated
`message.reasoning_content` in the non-streaming response**, that raw
reasoning text becomes `title_string`, almost certainly fails the
`{...}`-JSON-slice + `JSONCodec.loads` parse, so `title` ends up `''`, and
the final fallback is `messages[0].get('content', user_message)` — **the
chat's first user message becomes the title**. **Unverified / flagged**:
whether vLLM's non-streaming OpenAI-compatible response for the `qwen3`
reasoning parser actually populates a top-level `message.reasoning_content`
field (vs. only ever streaming `delta.reasoning` in SSE, as observed on the
box) is a vLLM-side fact outside this note's scope (OWUI source only) — check
against vLLM v0.27.1 source/docs before relying on this exact code path.
If vLLM's non-streaming response has no `reasoning_content` key at all, the
effective fallback title is simply the first user message either way.

**Inlet/outlet Filters on task calls:** every task route in `routers/tasks.py`
calls only `process_pipeline_inlet_filter` (`routers/pipelines.py:63-121`,
imported at `tasks.py:20`) before `generate_chat_completion` — this is the
**"Pipelines" project HTTP-filter mechanism** (POSTs to
`{connection_url}/{filter_id}/filter/inlet` for filters attached to a
model's `urlIdx`), not Open WebUI's native Filter *Functions*. Grepping
`routers/tasks.py` for `process_filter_functions` (the native inlet/outlet
Functions mechanism used by the main chat pipeline, imported at
`middleware.py:99` and invoked at `middleware.py:2635, 3104, 3408, 3984,
4842, 5983, 6203, 6337, 6361`) finds **zero** call sites — native Filter
Functions never run on any task-generation call. **No outlet filter of any
kind runs on task calls** — `process_pipeline_outlet_filter` is imported
only in `middleware.py` (line 63), never in `tasks.py`. So: pipeline-style
inlet filters *can* run on task calls (only relevant if a "Pipelines"-style
model connection is configured, which GIDEON's plain vLLM connection is
not); native Filter Functions and all outlet filters do not run on task
calls at all.

---

## 4. The reasoning block in the stream handler

**Delta fields recognised** — both `reasoning_content` and `reasoning` (and
also `thinking`), found in the streaming accumulator
(`utils/middleware.py:3573`):
```python
reasoning_content = delta.get('reasoning_content') or delta.get('reasoning') or delta.get('thinking')
```
and again at the SSE-forwarding/replay path (`middleware.py:5174-5178`):
```python
reasoning_content = (
    delta.get('reasoning_content')
    or delta.get('reasoning')
    or ...
)
```
So vLLM's observed `{"delta":{"reasoning":"We"}}` chunks are recognised
directly — no extra config needed.

**`<think>`-style tag detection** is a *separate*, complementary mechanism
for models that inline reasoning inside `content` instead of a dedicated
delta field. `DEFAULT_REASONING_TAGS` (`middleware.py:231-240`):
```python
DEFAULT_REASONING_TAGS = [
    ('<think>', '</think>'), ('<thinking>', '</thinking>'),
    ('<reason>', '</reason>'), ('<reasoning>', '</reasoning>'),
    ('<thought>', '</thought>'), ('<Thought>', '</Thought>'),
    ('<|begin_of_thought|>', '<|end_of_thought|>'),
    ('◁think▷', '◁/think▷'),
]
```
Gated by `DETECT_REASONING_TAGS = reasoning_tags_param is not False`
(`middleware.py:4623-4624`) where `reasoning_tags_param =
metadata.get('params', {}).get('reasoning_tags')` — **on by default**, no
env var governs it (grepped `REASONING` across `config.py`/`env.py`: no
hits); it is only a per-model/per-request Advanced-Params setting
(`src/lib/components/chat/Settings/Advanced/AdvancedParams.svelte:24,
281-337` — UI states "Enabled"/"Disabled"/"Custom" tag pair). Since vLLM's
`--reasoning-parser qwen3` strips `<think>` tags server-side and delivers
thinking via the dedicated `reasoning` delta field, this tag-detection path
is not the one exercised for `gideon-generator` — the `delta.get('reasoning')`
branch above is.

**Internal representation, not literal `<details>` markup in transit.** Both
paths converge on a structured `output` list item, not raw HTML text
(`middleware.py:3575-3592`):
```python
if not output or output[-1].get('type') != 'reasoning':
    output.append({
        'type': 'reasoning', 'id': output_id('r'), 'status': 'in_progress',
        'start_tag': '<think>', 'end_tag': '</think>',
        'attributes': {'type': 'reasoning_content'},
        'content': [], 'summary': None, 'started_at': time.time(),
    })
append_output_text(output[-1], reasoning_content)
```
and once `content` starts arriving, the reasoning item is closed out with a
computed `duration` (`middleware.py:3596-3600`):
```python
if output[-1].get('type') == 'reasoning':
    output[-1]['status'] = 'completed'
    output[-1]['ended_at'] = time.time()
    output[-1]['duration'] = int(output[-1]['ended_at'] - output[-1]['started_at'])
```
This `output` array (assistant message field, separate from `content`) is
what gets sent to the client and persisted.

**Client-side rendering — not a literal `<details>` DOM tag.** The frontend
converts each `type: 'reasoning'` output item into a "detail token" via
`buildReasoningToken` (`src/lib/components/chat/Messages/structuredOutput.ts:238-253`):
```ts
function buildReasoningToken(item: OutputItem, isLastItem: boolean) {
	const duration = item.duration ?? '';
	const isDone = isDoneStatus(item.status) || item.duration !== undefined || !isLastItem;
	...
	return {
		summary: isDone ? `Thought for ${duration || 0} seconds` : 'Thinking...',
		text,
		attributes: { type: 'reasoning', done: isDone ? 'true' : 'false', duration: String(duration) },
	};
}
```
`StructuredOutputRenderer.svelte` (lines 122-134) renders this token through
a custom `Collapsible` Svelte component (`src/lib/components/common/Collapsible.svelte`)
— a `div`-based collapsible, **not** a native `<details>` HTML element (no
`<details` string appears in that file). So **the exact literal markup
`<details type="reasoning" done="true" duration="…">` the ticket describes is
not what v0.11.3 actually renders for a live OWUI-native chat turn** — that
is the syntax the *markdown parser* (`src/lib/utils/marked/extension.ts:88`,
`` `<details ${attributesString}>` ``) can recognise inside raw message
`content` text (relevant for imported/legacy messages, or content that
literally contains such markup, e.g. from an external Filter), and it is
the same `{type, done, duration}` attribute shape as the live token above —
but the live rendering path uses a Svelte component tree, not raw HTML
`<details>` in the DOM or in transit. **Correction flagged**: treat "reasoning
block" as the `output[].type == 'reasoning'` structured item + its
`{type, done, duration}` attributes, not a literal `<details>` string, when
building anything downstream (e.g. a proof/screenshot) around this tag.

**On by default; governing knob:** on by default (no `ENABLE_REASONING*` env
exists at this tag); the only control is the per-request/per-model
`reasoning_tags` param (`false` disables tag *detection*, not delta-field
recognition — `delta.get('reasoning')`/`reasoning_content` handling has no
opt-out found in this tag's `middleware.py`).

**Stored in the DB or dropped:** stored — as the message's separate `output`
field (`Chats.upsert_message_to_chat_by_id_and_message_id`, referenced at
`middleware.py:3955, 4012`, persists `output` alongside `content`), not
folded into `content`. `content` itself remains the plain final-answer text.

**Outlet Filter's `body["messages"][-1]["content"]` — only the answer,
confirmed structurally.** The outlet-filter payload builder
(`middleware.py:3944-3953`) computes each message's `content` as:
```python
'content': m.get('content') or get_output_text(m.get('output')),
```
and `get_output_text` (`utils/misc.py:241-260`) explicitly **skips**
non-`'message'`-type output items:
```python
for item in output:
    if not isinstance(item, dict) or item.get('type') != 'message':
        continue
    ...
```
so a `type: 'reasoning'` item is never concatenated in — the reasoning block
never reaches `body["messages"][-1]["content"]` for an outlet Filter; the
Filter only ever sees the final answer text.

---

## 5. Proxy handling

**`trust_env` — yes, everywhere in `routers/openai.py`.** Every
`aiohttp.ClientSession(...)` construction relevant to the OpenAI connection
passes `trust_env=True`:
- Model-list GET: `openai.py:96`, `async with aiohttp.ClientSession(timeout=_MODEL_LIST_TIMEOUT, trust_env=True) as session:`
- The shared pooled session used for chat completions (`get_session()`,
  `utils/session_pool.py:71-77`):
  ```python
  _session = aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=True)
  ```
- Pipeline inlet/outlet filter sessions (`routers/pipelines.py`, same
  pattern).

Per aiohttp's own documented `ClientSession(trust_env=...)` semantics (not
re-verified here beyond citing the call sites — aiohttp's docs are the
primary source for what `trust_env` itself does, not this repo), setting it
`True` makes the session read `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (and
`.netrc`) from the process environment for outbound requests. **Consequence
for GIDEON**: with `trust_env=True` set unconditionally (no env var in this
codebase turns it off — grepped `trust_env` across
`routers/openai.py`/`utils/session_pool.py`: always `True`, never
conditional), a site that sets `HTTP_PROXY`/`HTTPS_PROXY` in the container's
environment **will** have Open WebUI attempt to route requests to
`gideon-generator` through that proxy unless `gideon-generator` (or its
Compose network alias/IP) is listed in `NO_PROXY`.

**TLS verification on a plain-HTTP base URL:** `ssl=AIOHTTP_CLIENT_SESSION_SSL`
is passed on every request (`openai.py:112,424,510,639,903,1103,1129,1630,
1783,1909,2031`), where:
```python
AIOHTTP_CLIENT_SESSION_SSL = _parse_ssl_env(os.getenv('AIOHTTP_CLIENT_SESSION_SSL', 'True'))  # env.py:616
```
This is standard aiohttp behaviour, not an OWUI-specific check: the `ssl=`
kwarg only affects HTTPS connections; for an `http://` base URL (GIDEON's
`http://gideon-generator:8000/v1`) it has no effect (aiohttp does not
attempt TLS on a plain-HTTP connection regardless of this value). **Not
independently re-verified against aiohttp's source in this pass** —
flagged as an aiohttp-library fact, not an Open WebUI one.

**Headers added:** `get_headers_and_cookies` (`openai.py:150-215`) always
sets `Content-Type: application/json`, adds `HTTP-Referer`/`X-Title` only
when `'openrouter.ai' in url` (not applicable here), and adds
`Authorization: Bearer {key}` when `auth_type` is `'bearer'` or unset
(the default). User-info forwarding headers are added only if
`ENABLE_FORWARD_USER_INFO_HEADERS` (`env.py:976`, default `os.getenv(...,
'False')`) is true — off by default, so no extra `X-OpenWebUI-*` headers
reach `gideon-generator` unless GIDEON explicitly turns that on.

---

## 6. The chat-completions request shape

The frontend/backend chat pipeline (`utils/chat.py:151-303` →
`routers/openai.py:1464-1660`, `generate_chat_completion`) forwards:
- `model` — the connection's model id, stripped of any `prefix_id` via
  `strip_provider_model_prefix` (`openai.py:1533-1534`, `utils/model_ids.py:1-4`).
- `messages` — forwarded as-is (with tool-message image parts stripped for
  non-Responses requests, `openai.py:1601-1608`, not relevant to plain text
  chat).
- `stream` — whatever the caller (frontend UI) set; if not streaming,
  `stream_options` is explicitly popped (`openai.py:1611-1612`,
  `if not is_streaming_request: payload.pop('stream_options', None)`) — so
  `stream_options` (e.g. `{'include_usage': true}`, a UI-exposed setting
  per `translation.json` strings) is only sent when the request streams.
- **Sampling params/preset:** `apply_model_params_to_body_openai(params,
  payload)` (`openai.py:1497`, `utils/payload.py:164-186`) only inserts a
  key when `value is not None and key not in form_data` — i.e., it is
  strictly additive and **never overrides** a value the frontend already
  set, and if the model has no non-empty `params` at all
  (`model_info.params.model_dump()` is falsy, `openai.py:1493-1497`), the
  payload passes through completely unmodified. The mapped/cast keys it can
  add are `temperature, top_p, min_p, max_tokens, frequency_penalty,
  presence_penalty, reasoning_effort, seed, stop, logit_bias,
  response_format` (`utils/payload.py:180-190`). **With no model preset
  configured for `gideon-generator`, the connection carries no sampling
  override at all** — exactly the plan's assumption.
- `max_tokens` handling: `gideon-generator` does not match
  `is_openai_new_model` (`openai.py:1193-1202`, only matches `^o\d+` or
  `gpt-(\d+)` with N≥5), so it is **not** treated as an OpenAI "reasoning
  model" — no `max_tokens`→`max_completion_tokens` rename, no system→
  developer role rewrite happens (`openai.py:1543-1544`,
  `openai_reasoning_model_handler` only invoked in the `is_openai_new_model`
  branch). The only transformation applied for a non-`api.openai.com` URL is
  backward-compat the other way: if the caller already sent
  `max_completion_tokens`, it is renamed back to `max_tokens`
  (`openai.py:1545-1549`). So a plain chat turn to `gideon-generator` sends
  `max_tokens` (if the UI/preset set one) untouched, never
  `max_completion_tokens` — vLLM's OpenAI-compatible server accepts
  `max_tokens` natively, so no rejection risk from this rewrite. **Not
  verified**: whether vLLM v0.27.1 rejects any other field OWUI might add
  (e.g. `logit_bias`, `seed`) — out of scope for this OWUI-only note.

---

## Summary of flags for the plan

1. Confirmed from source, no ambiguity: `api_type` (absent/`''`) ⇒ Chat
   Completions at `/chat/completions` is the default and only path a normal
   chat turn takes; `/responses` is unrelated and unreached.
2. Confirmed: refused connections during model discovery are swallowed
   (`log.error` + `None`/empty list), 1-second cache, model shows up on the
   next `/api/models` fetch without a restart — *unless* `model_ids` is set
   in `OPENAI_API_CONFIGS`, in which case the model is selectable
   immediately (synthetic list), before the engine is actually reachable.
3. Confirmed: task calls default to `TASK_MODEL_EXTERNAL` (not `TASK_MODEL`)
   for an OpenAI-compatible connection unless `connection_type: 'local'` is
   set, and fall back to the chat's own model when unset — no thinking
   suppression on any task call; title (1000) and emoji (4) caps can be hit
   mid-reasoning; title's empty-content fallback ultimately becomes the
   user's first message.
4. Confirmed: both `reasoning` and `reasoning_content` deltas are recognised;
   the structured `output` item (not literal `<details>` text) is what's
   actually produced and stored; outlet Filters never see the reasoning text
   in `content`.
5. Confirmed: `trust_env=True` unconditionally — `NO_PROXY` must include
   `gideon-generator` on any host with an egress proxy set globally.
6. Confirmed: no sampling override reaches vLLM without an explicit model
   preset; `max_tokens` (not `max_completion_tokens`) is what a plain chat
   turn sends.

**Unverified / out of this note's scope** (vLLM-side, not Open WebUI):
whether vLLM v0.27.1's non-streaming Chat Completions response (with
`--reasoning-parser qwen3`) populates a top-level `message.reasoning_content`
field, and whether it accepts every field OWUI's `apply_model_params_to_body_openai`
can add. Check against vLLM's own source/docs before relying on those two
points.

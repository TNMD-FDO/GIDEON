---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — task calls vs. a chat turn on the wire

For general-turn ticket 08 (`.scratch/general-turn/issues/08-citation-stamp-in-the-service.md`).
Builds on `docs/research/owui-engine-connection.md` §3 ("The task model", "What a
task call sends", "Title generation on empty/failed content") and
`docs/frontend-contract.md` §2 rows 5 and 7 — cited, not re-derived, below.

Source of truth: `git clone --depth 1 --branch v0.11.3
https://github.com/open-webui/open-webui.git`, resolved to commit
`2a960a59fe1dbbd35282f0556b3666d81102e781` (same commit the engine-connection
note verified). Backend paths are relative to `backend/open_webui/`, frontend
paths to `src/`. `docs.openwebui.com` was not consulted. GIDEON-repo paths
(`gideon/host/render/owui.py`, `compose/open-webui/functions/branch_gate.py`)
are cited separately as GIDEON's own code, not the v0.11.3 pin.

---

## Key finding for the plan

**Under GIDEON's actual render, one wire fact does separate a task call from
any whole (non-streamed) chat completion a signed-in user can produce: the
presence of a `system`-role message in the upstream `messages[]` array.** A
task call (title, tags, follow-ups, queries, autocomplete, emoji, image
prompt — every one except MOA) never carries one; any `user`-role chat
completion, streamed or whole, browser or API-key, is forced through
General's preset and therefore always does. This is not a fact of vanilla
v0.11.3 — it falls out of three GIDEON-specific render choices working
together (§1.4 below), and the chain is worth restating precisely because it
corrects an assumption a plan might otherwise make from the engine-connection
note's generic reading of the task-model mechanism (§1.4's "Correction"
callout).

The `stream` field alone (§2) does **not** separate them: it is entirely
caller-controlled on the wire, with no server-side floor tying it to any
permission or preset. GIDEON's render hides the browser's own in-UI
streaming toggle (`chat.controls`/`chat.params: false`), but a `user`-role
personal API key can still reach `/api/chat/completions` directly and set
`stream: false` itself, producing a genuine non-streamed **chat**
completion — exactly the "direct path" the ticket's Discovery note names.
The system-message signal is what catches that case.

---

## 1. The upstream request of a task call vs. a chat turn

### 1.1 Body fields that never differ, or never survive to the wire

- **`metadata` is popped before the request leaves Open WebUI, identically
  for both.** `routers/openai.py:1490`: `metadata = payload.pop('metadata',
  None)` — this runs once, in the single `generate_chat_completion` function
  both a task call and a chat turn pass through (`utils/chat.py:151-307` →
  `routers/openai.py:1464-1660`, no branch on caller). Every task route
  builds its payload with `'metadata': {..., 'task': str(TASKS.X), ...}`
  (`routers/tasks.py:188-193,253-258,318-323,377-382,454-459,530-535,585-590`)
  — the `task`/`task_body`/`chat_id` keys are all inside this popped dict, so
  **no `task` or `background` field of any kind reaches the upstream JSON
  body**. Confirmed absent by grep across `routers/openai.py`,
  `utils/chat.py`, `utils/payload.py` for a `task`/`background` payload key —
  none found; this is an absence claim.
- **No OpenAI `user` field either way.** `payload['user']` is only set `if
  'pipeline' in model and model.get('pipeline')` (`openai.py:1538-1544`) —
  not GIDEON's plain OpenAI-compatible model, task call or chat turn alike.
  Matches frontend-contract.md §2 row 5's "sends no OpenAI `user` field".
- **`stream_options` popped identically when not streaming.**
  `openai.py:1611-1613`: `is_streaming_request = bool(payload.get('stream',
  False)); if not is_streaming_request: payload.pop('stream_options',
  None)`. Same code, same effect on a task call's forced `stream: False` and
  on a user-chosen non-streamed chat turn.
- **`max_tokens` handling is the same code path** (`openai.py:1546-1556`) —
  no task-specific branch; the per-route caps (title 1000, emoji 4, others
  uncapped by default) are already established in the engine-connection
  note and are applied *before* this point, as ordinary payload keys
  indistinguishable from a chat turn's own `max_tokens`.

### 1.2 Headers: no task-identifying header without per-connection config — confirmed at the source

`get_headers_and_cookies` (`openai.py:154-220`) is the single header-building
function for this route, called once per request (`openai.py:1565`) whether
`metadata` came from a task call or a chat turn. It sets, unconditionally,
`Content-Type` and (only for `openrouter.ai`) `HTTP-Referer`/`X-Title`; then,
`if ENABLE_FORWARD_USER_INFO_HEADERS and user:` (GIDEON renders this on, per
frontend-contract.md §2 row 5) adds the `X-OpenWebUI-User-*` headers via
`include_user_info_headers` (`utils/headers.py:50-75`) and, `if metadata and
metadata.get('chat_id')`, the chat-id header
(`FORWARD_SESSION_INFO_HEADER_CHAT_ID`, `openai.py:180-181`) — both fire for
a task call too, since every task route's `metadata` carries `chat_id`
(`routers/tasks.py:192,257,322,381,458,534,589`). **These are exactly the
rows frontend-contract.md §2 row 5 already names as riding both kinds of
call**, confirmed again here at the header-builder itself.

The only place a task-identifying header could appear is a **per-connection
custom header** with a `{{TASK}}` token:
`headers.py:100-102,105-154`'s `get_custom_headers`/`parse_custom_headers`
interpolates `'{{TASK}}': metadata.get('task', '') or ''`
(`headers.py:136`) into `custom_headers`, which is only reached from
`get_headers_and_cookies` `if config.get('headers') and
isinstance(config.get('headers'), dict)` (`openai.py:216-218`) — `config`
here is the per-connection `api_config`, i.e. `OPENAI_API_CONFIGS[idx]`'s
own `headers` dict (the engine-connection note's §1 already established what
this object can carry, including `headers`). **GIDEON's render sets no
`OPENAI_API_CONFIGS` at all** (`gideon/host/render/owui.py:465-466`'s own
comment: "no OPENAI_API_CONFIGS: its absence is what keeps the request shape
Chat Completions and the discovery real") — so `config.get('headers')` is
falsy, `get_custom_headers` is never called, and **no `{{TASK}}`-templated
header, or any other custom header, reaches `gideon-generator`**. This
confirms frontend-contract.md §2 row 7's "the lever, if one is ever wanted,
is a `{{TASK}}` header on the connection, which needs the per-connection
configuration the render leaves out on purpose" precisely, at the mechanism
level.

### 1.3 The messages list's shape — a single templated user message, and (usually) no system message

Every non-MOA task route builds `'messages': [{'role': 'user', 'content':
content}]` — one message (`routers/tasks.py:186,251,316,375,452,528,583`),
where `content` is the filled-in template (`utils/task.py`'s
`title_generation_template`/`tags_generation_template`/etc., called at
`tasks.py:179,247,312,371,448,524,579`). **Default template opening lines**
(`config.py`, exact quotes):

| Task | Line | Opens with |
|---|---|---|
| Title | `config.py:2224-2225` | `### Task:\nGenerate a concise title summarizing the chat history.` |
| Tags | `config.py:2249-2250` | `### Task:\nGenerate 1-3 broad tags categorizing...` |
| Image prompt | `config.py:2269-2270` | `### Task:\nGenerate a detailed prompt for am image generation task...` |
| Follow-ups | `config.py:2292-2293` | `### Task:\nSuggest 3-5 relevant follow-up questions...` |
| Queries (search & retrieval) | `config.py:2322-2323` | `### Task:\nAnalyze the chat history to determine the necessity of generating search queries...` |
| Autocomplete | `config.py:2353-2354` | `### Task:\nYou are an autocompletion system...` |
| Emoji | `config.py:2452` | **Not** `### Task:` — `Your task is to reflect the speaker's likely facial expression...` |
| MOA | `config.py:2456` | **Not** `### Task:` — `You have been provided with a set of responses from various models...` |

So the "every default template opens with `### Task:`" assumption in the
ticket's framing holds for six of eight, not all — emoji and MOA are
worded differently and carry no strict JSON-output instruction either (see
§3). None of this is a wire *fact* usable by General's service without
inspecting message content (a heuristic, not a protocol-level signal); it is
recorded here because the ticket asked for it and because it matters for §3.

**Env var overrides, and whether GIDEON changes any of them:** title, tags,
image, follow-up, query and autocomplete templates each have a same-named
env var (`TITLE_GENERATION_PROMPT_TEMPLATE` `config.py:2222`,
`TAGS_GENERATION_PROMPT_TEMPLATE` `2247`,
`IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE` `2267`,
`FOLLOW_UP_GENERATION_PROMPT_TEMPLATE` `2290`,
`QUERY_GENERATION_PROMPT_TEMPLATE` `2320`,
`AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE` `2350`, all default `''`),
registered into the `task.*.prompt_template` Config keys at
`config.py:3145-3160` and read at each route's call site
(`routers/tasks.py:173,241,306,365,442,518` — `if template != '': use it,
else: use the DEFAULT_*`). **Emoji and MOA have no env override at all**:
`generate_emoji` and `generate_moa_response` use
`DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE`/`DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE`
directly (`tasks.py:577,628`), with no `Config.get` call for a template in
either route body. Grepped `gideon/host/render/owui.py` for
`PROMPT_TEMPLATE` — **no hits**: GIDEON renders none of the six overridable
templates, so every default template above applies unmodified; the two
unconfigurable ones (emoji, MOA) can never change without an Open WebUI
version bump regardless.

**Whether `params.system` (General's instruction) is applied to a task
call, and which id the call goes out under — this needed tracing past the
engine-connection note's generic assumption, and the answer is
GIDEON-specific:**

The engine-connection note's §3 assessed `get_task_model_id` with
`TASK_MODEL_EXTERNAL` treated as unset (`''`, its env default) and a
simpler "one connection, no preset" picture. **GIDEON's actual render sets
it explicitly**: `gideon/host/render/owui.py:474`, `"TASK_MODEL_EXTERNAL":
generator.serve.served_name` — the *base* model's served name (e.g.
`gideon-generator`), not General's preset id. Tracing `get_task_model_id`
(`utils/task.py:16-27`) with GIDEON's actual two-row model shape
(`gideon/host/render/owui.py:587-606`'s `base_model_record`,
`629-656`'s `general_preset_record`):

1. `default_model_id` = the chat's own model, i.e. General's preset id
   (`gideon-general`) — what the task endpoint's caller sends as `model`
   (`routers/tasks.py:157,230,295,354,431,507,566`, or, for the server-side
   auto-generation path, `message['model']` at `middleware.py:3715,3772,3828`).
2. `models.get('gideon-general', {}).get('connection_type')` — a preset
   (custom model row with `base_model_id` set) inherits `connection_type`
   from its base at merge time: `utils/models.py:216-223`, `connection_type
   = base_model.get('connection_type', None)`. The base row's own
   `connection_type` for an OpenAI-compatible connection defaults to
   `'external'` (`routers/openai.py:734`, `api_config.get('connection_type',
   'external')`) — GIDEON renders no `OPENAI_API_CONFIGS` entry that would
   override this. So the preset's inherited `connection_type` is
   `'external'`, **not** `'local'`.
3. `get_task_model_id`'s `else` branch therefore runs:
   `if task_model_external and task_model_external in models: task_model_id
   = task_model_external` (`utils/task.py:23-25`). GIDEON's
   `TASK_MODEL_EXTERNAL` is `gideon-generator`, and that id **is** in
   `models` (it's the base row itself, `base_model_record`'s `id`). So
   **`task_model_id` resolves to `gideon-generator`, the base — every
   non-MOA task call's `payload['model']` is the base id from the start**,
   not the preset id.
4. Back in `routers/openai.py`'s `generate_chat_completion`:
   `model_info = await Models.get_model_by_id(model_id)`
   (`openai.py:1493`, a primary-key lookup on the Models table,
   `models/models.py:500-504`, `db.get(Model, id)`). GIDEON's render syncs a
   real Models-table row for the base id (`base_model_record`,
   `gideon/host/render/owui.py:587-606`), with **`"params": {}`**
   (line 600) and `"base_model_id": None` (line 598) — so this lookup
   succeeds, `model_info.base_model_id` is falsy (no rewrite needed, the
   payload's `model` is already the base id), and `params =
   model_info.params.model_dump()` is `{}` (`ModelParams` is a bare
   `BaseModel` with `extra='allow'` and no declared fields,
   `models/models.py:72-75`, so an empty stored dict round-trips to an
   empty dict). `if params:` (`openai.py:1506`) is **false** — **neither
   `apply_model_params_to_body_openai` nor `apply_system_prompt_to_body`
   runs at all** for a task call resolved this way.

**Conclusion, corrected from the engine-connection note's generic reading:
under GIDEON's render, a task call (title, tags, follow-ups, queries,
autocomplete, emoji, image prompt) goes out under the base model's id
directly, carries no `system` message, and gets none of General's
`params.system` instruction.** (MOA is the one exception — see below.) This
happens to land on the same numeric `task_model_id` the engine-connection
note predicted (`gideon-generator`) but for a different, GIDEON-specific
reason — that note's assumption of an *unset* `TASK_MODEL_EXTERNAL` does not
hold for the actual render, and a plan should not carry forward its "task
calls run on the same engine/model as the chat, with no admin action
required" framing as implying the *preset* applies; it does not.

A **normal chat turn**, by contrast, is sent by the browser with `model:
gideon-general` (the preset — `DEFAULT_MODELS: GENERAL_PRESET_ID`,
`gideon/host/render/owui.py:503`). `Models.get_model_by_id('gideon-general')`
finds `general_preset_record`, whose `params` is `{"system":
texts.system_prompt, "function_calling": ...}` (`gideon/host/render/owui.py:642-645`)
— truthy, so `apply_system_prompt_to_body` **does** run
(`openai.py:1507-1511`), prepending a `role: system` message
(`utils/payload.py:49-65`, `add_or_update_system_message`) before the payload
is rewritten to the base id (`openai.py:1497-1502`). **So every
preset-routed chat turn carries a `system` message; no task call (except
MOA) ever does.** This is the wire fact behind the "Key finding" above; §1.4
below is why it holds for *every* `user`-role whole completion, not just
streamed ones.

**MOA is the one outlier.** `generate_moa_response`
(`routers/tasks.py:610-660`) does **not** call
`get_task_model_generation_config` at all — it uses `model_id =
form_data['model']` directly (`tasks.py:620`, whatever the caller's JSON
body named), so if the browser sends the chat's own preset id (it does —
`src/lib/components/chat/Chat.svelte:3884-3888`'s `mergeResponses` passes
`message.model`), MOA's payload goes through the **preset** id, gets
rewritten to the base and **does** receive `params.system` — the one task
call that carries General's instruction. It is also the one task call whose
`stream` the caller controls (`'stream': form_data.get('stream', False)`,
`tasks.py:639`) and whose frontend JS always sets `stream: true`
(`src/lib/apis/index.ts:1096-1117`, `generateMoACompletion`). Practical
reachability of MOA under GIDEON's render is addressed in §4.

### 1.4 Why the system-message signal is not just a UI convention but a structural one

`user`-role access to the base model outside a preset is refused for a
**chat**-pipeline completion by GIDEON's own inlet Filter, the branch gate
(`compose/open-webui/functions/branch_gate.py:60-78`): `if role == 'user'
and not _is_preset(__model__): raise BranchRefusal(...)`. This is a native
Filter *Function*, and the engine-connection note already established (§3,
"Inlet/outlet Filters on task calls") that **native Filter Functions never
run on any task-generation call** — so the branch gate cannot and does not
apply to task calls (consistent with their resolving to the base id
unimpeded, §1.3 above), but it **does** apply to any `/api/chat/completions`
request, browser-originated or not, since Filter Functions run inside the
same middleware pipeline regardless of how the caller authenticated
(session cookie or a personal API key resolving to `get_verified_user`).

GIDEON further restricts what a personal API key can reach at all:
`ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS: "true"` with `API_KEYS_ALLOWED_ENDPOINTS`
built from `ALLOWED_ENDPOINTS` (`gideon/host/render/owui.py:96-108,452-454`),
which lists `/api/chat/completions` and `/api/models` among chat-relevant
paths but **not** `/api/v1/tasks/*` — an API-key client cannot reach the
task endpoints directly at all (this is GIDEON's own render, not a v0.11.3
default; the v0.11.3 restriction mechanism itself was not otherwise traced
in this pass).

Put together: a `user`-role caller's only way to reach
`/api/chat/completions` — browser UI or personal API key — is refused
unless the targeted model is a preset (`_is_preset` requires a non-empty
`base_model_id`), and General's is the only preset GIDEON renders. So
**every `user`-role whole chat completion is forced through General's
preset and therefore always carries the injected `system` message**; no
task call ever does (MOA excepted, and MOA is not reachable via the
task-endpoint restriction bypass either — see §4). An **admin** or the
**eval identity** is exempted from the branch gate
(`branch_gate.py:74`) and could in principle produce a system-message-free
whole completion outside this mechanism — not a `user`-role case, and the
forwarded `X-OpenWebUI-User-Role`/`X-OpenWebUI-User-Email` headers
(frontend-contract.md §2 row 5, confirmed riding both call kinds in §1.2
above) are available to the service if it wants to fold that distinction in
too. This last point is a synthesis of already-established facts, not
independently re-traced end-to-end in this pass.

---

## 2. Can a browser chat turn be a whole (non-streamed) completion on this pin?

**Yes. The simplest and most robust reason: `stream` is entirely
caller-controlled input, with no server-side enforcement tying it to any
permission or preset value — confirmed by reading the mapping, not just the
absence of a check.**

`apply_model_params_to_body_openai` (`utils/payload.py:164-195`), the one
function that lets a model's/preset's own `params` add fields to an outgoing
payload, first calls `remove_open_webui_params`
(`payload.py:135-160`) which strips `stream_response` (among other
OWUI-only concepts) out of `params` before anything is mapped, and its own
`mappings` dict (`payload.py:182-194`) — `temperature, top_p, min_p,
max_tokens, frequency_penalty, presence_penalty, reasoning_effort, seed,
stop, logit_bias, response_format` — **has no `stream` entry at all**. So
neither a preset's own `params.stream_response` nor any admin setting ever
writes the wire's `stream` key; the only place `stream` is ever set on
`payload` is whatever the caller's own request body already had it as
(`openai.py:1489`, `payload = {**form_data}`, and `is_streaming_request =
bool(payload.get('stream', False))` at `openai.py:1611`, read back
unmodified). **Any authenticated caller that can reach
`/api/chat/completions` chooses `stream` itself, unconditionally.**

For the **browser UI's own** requests specifically, the client decides what
value to put in that field before sending it:
`src/lib/components/chat/Chat.svelte:3459-3463`:
```js
const stream =
    model?.info?.params?.stream_response ??
    $settings?.params?.stream_response ??
    params?.stream_response ??
    true;
```
— (1) the model's own preset `params.stream_response` (GIDEON's rendered
preset sets none: `gideon/host/render/owui.py:642-645`'s
`general_preset_record` `params` is only `{"system": ..., "function_calling":
...}`), (2) the user's personal setting, (3) a call-site override, (4)
default **`true`**. The personal setting lives in the Advanced Params panel
(`src/lib/components/chat/Settings/Advanced/AdvancedParams.svelte:20,129-140`),
shown to a non-admin only when `$user?.permissions.chat?.controls` **and**
`$user?.permissions.chat?.params` are both truthy
(`src/lib/components/chat/Settings/General.svelte:271`). **GIDEON's own
render sets both to `false`**
(`compose/open-webui/permissions.yaml:44,47`, `controls: false`, `params:
false`) — so the in-UI toggle is hidden from an ordinary signed-in user, and
through the browser chat UI alone a chat turn will, in practice, default to
streamed.

That hidden toggle does not close the field off server-side, though: the
settings-update endpoint (`routers/users.py:498-511`,
`POST /api/v1/users/user/settings/update`) gates only on the
`settings.interface` permission (GIDEON renders it `true`,
`compose/open-webui/permissions.yaml:88`, "upstream default") — there is no
field-level check there tying `chat.controls`/`chat.params` to whether
`params.stream_response` specifically may be written, so a user with API
access could still `POST` `{"params": {"stream_response": false}}` to that
endpoint directly and have their next browser-sent chat turn pick it up via
step (2) above. More directly still, since `stream` is caller-controlled
with no server check at all (this section's opening paragraph): any
`user`-role caller with a personal API key — which GIDEON's render permits
against `/api/chat/completions` specifically
(`ENABLE_API_KEYS: "true"`, `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS: "true"`,
`/api/chat/completions` present in `API_KEYS_ALLOWED_ENDPOINTS`,
`gideon/host/render/owui.py:96-108,452-454`) — can simply set `"stream":
false` in their own request body and have it honoured unconditionally. This
is the "direct path" the ticket's Discovery note names.

**So "streamed ⇒ chat turn, whole ⇒ task call" does not hold.** What breaks
it: `stream` has no server-side floor at all; GIDEON's render closes the
ordinary in-UI browser toggle (`chat.controls`/`chat.params: false`) but not
the settings-API or the direct `/api/chat/completions` paths available to
any `user`-role identity with API-key access. Any of these produce a genuine
whole **chat** completion — and, per §1.3–1.4, all of them still carry the
`system`-role message a task call never does, because they all resolve
through General's preset (the branch gate refuses anything else for a
`user` role) regardless of how `stream` was chosen.

---

## 3. How each task output is parsed, and what happens to trailing text

Two separate parsers exist at this tag for several task types: a
**server-side Python parse inside Open WebUI's own backend**
(`utils/middleware.py`, run when the backend calls the task function
directly as part of its own post-turn pipeline — title/tags/follow-ups
after every saved chat turn, search/retrieval queries before the model
call), and a **frontend TypeScript parse** (`src/lib/apis/index.ts`, run
when the browser calls the `/api/v1/tasks/*/completions` HTTP endpoint
itself — confirmed live for title, tags, emoji and autocomplete via actual
call sites in `.svelte` components; `generateQueries`'s JS parse exists in
`apis/index.ts` but has **no caller anywhere in `src/lib/components` or
`src/routes`** — grepped, zero hits — so it is dead code at this tag and
queries are only ever parsed server-side). In every case, whatever text
follows the closing `}` (or the last emoji character, for emoji) that these
parses locate is **discarded**, not stored, not forwarded, not re-parsed —
the slice operation itself drops it; it never "leaks" into a stored title,
tag, search query, image prompt or follow-up under a well-formed JSON
object. What differs by task is the **fallback when no valid object is
found at all**.

| Task | Parser (file:line) | Slice | On malformed-but-braced JSON | On no `{`/`}` found at all |
|---|---|---|---|---|
| Title (auto, after turn) | `middleware.py:3779-3803` | `title_string[title_string.find('{') : title_string.rfind('}') + 1]` | `title = ''` → falls through | `title = messages[0].get('content', user_message)` — **the user's own first message becomes the title** |
| Title (manual "regenerate", browser) | `src/lib/apis/index.ts:797-829` | `indexOf('{')`/`lastIndexOf('}')`, single-quote→double-quote sanitize first | `catch` → `return null` (caller keeps the old title) | same — `return null` |
| Tags (auto) | `middleware.py:3835-3858` | `tags_string[tags_string.find('{') : tags_string.rfind('}') + 1]` | `except: pass` — **tags silently left unchanged** | same, `pass` |
| Tags (manual, browser) | `apis/index.ts:869-902` | `indexOf`/`lastIndexOf`, quote sanitize | `catch` → `return []` | `return []` |
| Follow-ups (auto only — no frontend caller for `generateFollowUps` exists in this codebase at all) | `middleware.py:3723-3759` | `follow_ups_string[.find('{') : .rfind('}') + 1]` | `except: pass` — no `chat:message:follow_ups` event emitted, nothing stored | same, `pass` |
| Search queries (auto, web search) | `middleware.py:1542-1555` | `response.rfind('{')` … `response.rfind('}') + 1` (**`rfind` for the open brace too**, not `find`) | `except: queries = [response]` **using the already-bracket-sliced substring** (trailing text after the last `}` was already cut before this fallback runs) | `queries = [response]` uses the **full raw content, trailing text included** — this is the "unparseable queries response used whole" case the ticket asked about, confirmed |
| Retrieval queries (auto, File Context) | `middleware.py:2015-2027` | same `rfind`/`rfind`+1 pattern | `except: queries_response = {'queries': [queries_response]}`, same already-sliced substring | same full-raw-content fallback as search queries |
| Queries (browser, `generateQueries`) | `apis/index.ts:994-1019` | `indexOf('{')`/`lastIndexOf('}')` | `return [response]` (full un-sliced original string, since the slice+`JSON.parse` are combined in one `try`) | `return [response]` — **dead code path (no caller), listed for completeness** |
| Autocomplete (browser only) | `apis/index.ts:1067-1092` | `indexOf('{')`/`lastIndexOf('}')` | `return response` — the **entire raw content, trailing text included**, becomes the autocomplete suggestion | same, `return response` |
| Emoji (browser only) | `apis/index.ts:941-949` | **not JSON at all** — `content.replace(/["']/g, '')`, then a Unicode `\p{Extended_Pictographic}` regex; **first match wins** (`response.match(...)[0]`) | n/a | `return null` if no pictographic character anywhere in the whole string, trailing text included |
| Image prompt (auto only, gated — see §4) | `middleware.py:1911-1928` | `response.rfind('{')` … `response.rfind('}') + 1` | `except: prompt = user_message` | `prompt = user_message` — the user's own message becomes the image prompt |
| MOA | none — a real synthesized answer, streamed to the browser as ordinary chat text (`apis/index.ts:1096-1117`, `stream: true`) | — | — | — |

**Answering the ticket's specific question directly:** a
`"\n\nGeneral does not verify citations."` stamp appended *after* a
well-formed JSON object's closing `}` is discarded by every one of these
parses without exception — the slice never includes it, in both the
server-side (Python `find`/`rfind`) and browser-side (JS `indexOf`/
`lastIndexOf`) forms, and in the `rfind`-of-`{`-too variant used for
queries. **It would surface** only if the task output's JSON object is
itself malformed or entirely absent (no `{`/`}` at all) — and even then, in
four of the eight parses that fallback lands only for the two **query**
generation paths (search and retrieval) and the **autocomplete** path,
where the *entire raw content including the stamp* becomes the literal
search/retrieval query text or the literal autocomplete suggestion; title,
tags and follow-ups instead fall back to the **user's own message text**
(never the model's output) or silently drop the update, so the stamp cannot
surface there even on total parse failure. The emoji parse cannot surface
the stamp's text at all (it only ever returns a single matched emoji
character or `null`), and MOA is not a JSON-parsed task at all — a stamp on
a citation-shaped MOA answer would render to the user exactly as a stamped
chat answer does.

---

## 4. Which task calls are live under GIDEON's render

From `gideon/host/render/owui.py:472-505` (the `connected` branch), quoted
literally:

| Toggle | GIDEON's render | Live? |
|---|---|---|
| `ENABLE_TITLE_GENERATION` | `"true"` (`owui.py:475`) | **Live** |
| `ENABLE_TAGS_GENERATION` | `"true"` (`owui.py:476`) | **Live** |
| `ENABLE_SEARCH_QUERY_GENERATION` | `"true"` (`owui.py:477`) | **Live** (only fires when a chat's own per-chat web-search toggle is on, per frontend-contract.md §2 row 3) |
| `ENABLE_RETRIEVAL_QUERY_GENERATION` | `"false"` (`owui.py:488`) | Dead — `generate_queries(..., type='retrieval')` raises the feature-disabled exception inside `chat_completion_files_handler`'s own `try`, which is swallowed (`middleware.py:2028-2029`, `except: pass`); `BYPASS_EMBEDDING_AND_RETRIEVAL`/`BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL` are also both `"true"` (`owui.py:455,458`), so this path is doubly inert |
| `ENABLE_FOLLOW_UP_GENERATION` | `"false"` (`owui.py:489`) | Dead |
| `ENABLE_AUTOCOMPLETE_GENERATION` | `"false"` (`owui.py:490`) | Dead — `routers/tasks.py:481-485` raises `HTTPException(400, FEATURE_DISABLED)` before any model call; the browser's `generateAutoCompletion` callers (`RichTextInput.svelte:851`, `MessageInput.svelte:2046`) would only ever see that 400 |
| Image prompt (`ENABLE_IMAGE_PROMPT_GENERATION`, gated behind `image_generation.enable` first) | Neither `ENABLE_IMAGE_GENERATION` nor `ENABLE_IMAGE_PROMPT_GENERATION` is rendered by GIDEON (grepped `gideon/host/render/owui.py` — no hits for either name) | Dead — `ENABLE_IMAGE_GENERATION` defaults to `os.getenv('ENABLE_IMAGE_GENERATION', '').lower() == 'true'` (`config.py:1336`), i.e. **off** by default; `middleware.py:1875`'s `elif not await Config.get('image_generation.enable'):` branch fires first, so the `generate_image_prompt` call at `middleware.py:1892` inside the `else:` block is never reached |
| Emoji | No toggle exists at this tag (confirmed absence, engine-connection note §3) | Reachable only through `CallOverlay.svelte:509`, the voice-call UI. GIDEON's render sets no audio/STT/TTS configuration at all (grepped `gideon/host/render/owui.py` for `AUDIO`/`VOICE`/`SPEECH`/`WHISPER` — no hits), so whether the Call feature is even functional enough to reach this button was **not independently verified** in this pass — flagged as not traced, likely dead in practice but not confirmed structurally dead the way autocomplete/follow-ups/retrieval/image-prompt are |
| MOA | No toggle exists at this tag | Reachable only via `Chat.svelte:3872-3888`'s `mergeResponses`, itself only reachable from a UI state with **two or more responses to one message**; GIDEON explicitly disables Arena (`"ENABLE_EVALUATION_ARENA_MODELS": "false"`, `owui.py:549`) and renders a single visible model (`DEFAULT_MODELS: GENERAL_PRESET_ID`, the base hidden by the branch gate, frontend-contract.md §2 row 8) — no rendered path puts a second response on a message. The `/api/v1/tasks/moa/completions` endpoint itself carries no admin toggle and is not on GIDEON's `API_KEYS_ALLOWED_ENDPOINTS` list (`owui.py:96-108`), so an API key cannot reach it either. Practically unreachable under this render; not literally disabled by any flag |

**Net for the plan:** the parses that matter in production are **title**
(both the auto and manual-regenerate paths), **tags** (both paths), and
**search queries** (auto only, per-chat web-search toggle permitting).
Follow-ups, retrieval queries, autocomplete and image-prompt are dead paths
under the current render and need no defense against the stamp today, only
if a future render turns their toggles on. Emoji and MOA are edge cases
outside GIDEON's rendered toggle surface entirely, MOA is the one task type
that *would* carry General's system prompt and stream like a chat answer,
should the render ever change to make it reachable.

---

## Summary of flags for the plan

1. No body field, header, or model-id string on the wire distinguishes a
   task call from a chat turn by itself. The one structural signal that
   does, under GIDEON's specific render, is the **presence of a
   `system`-role message** in `messages[]` — task calls (MOA excepted)
   never carry one because `TASK_MODEL_EXTERNAL` is rendered to the base
   model's id and that base's Models-table row has empty `params`; every
   `user`-role chat completion is forced through General's preset (which
   always injects `params.system`) by the branch gate Filter, a GIDEON
   Function, not native v0.11.3 behavior. This is new since (and corrects a
   generic assumption in) `owui-engine-connection.md` §3 — see §1.3's
   "Correction" discussion.
2. `stream: false` alone never means "task call" — the wire's `stream`
   field is entirely caller-controlled with no server-side floor at all
   (`utils/payload.py`'s preset-param mapping has no `stream` entry). GIDEON
   closes the browser's own in-UI toggle (`chat.controls`/`chat.params:
   false`) but not the settings-API or a direct `/api/chat/completions` call
   from a `user`-role personal API key (which GIDEON's render allows). Any
   of these produce a whole **chat** completion, and all still carry the
   system message from point 1.
3. Trailing text after a task output's well-formed JSON object is always
   discarded by the parse (Python `find`/`rfind` slice, JS
   `indexOf`/`lastIndexOf` slice, or — for search/retrieval queries — a
   `rfind`/`rfind` slice) in both the server-side and browser-side parsers.
   It can only surface on total parse failure (no `{`/`}` found at all), and
   only for **search queries, retrieval queries, and autocomplete**, where
   the whole raw content (stamp included) becomes the literal output; title,
   tags and follow-ups fall back to the user's own message text or a silent
   no-op instead, never to the model's stamped output; emoji cannot surface
   it; MOA is not JSON-parsed at all.
4. Under GIDEON's current render, only title, tags and (per-chat-gated)
   search-query generation are live task paths; retrieval queries,
   follow-ups, autocomplete and image-prompt are structurally dead; emoji
   and MOA sit outside the toggle surface and are practically unreachable
   but not formally disabled.

**Not traced in this pass (flagged, not answered):**
- Whether the Call/voice-mode UI is functional enough under GIDEON's render
  to reach the emoji-generation button (§4).
- Whether `process_filter_functions` (and so the branch gate) truly runs
  identically for a session-cookie call and an API-key call to
  `/api/chat/completions` — asserted from `get_verified_user` unifying both
  auth methods and the middleware pipeline being auth-agnostic, but not
  independently re-traced end to end in this pass.
- Whether `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS`'s enforcement mechanism
  itself (which route decorator/dependency reads `API_KEYS_ALLOWED_ENDPOINTS`)
  was read at this tag — only the render-side flags were confirmed; the
  enforcement code path was not opened in this pass.

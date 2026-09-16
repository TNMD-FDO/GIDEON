---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — a preset's `params.system`: substitution, injection order, permissions, and web search without builtin tools

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`, matches
`images.lock`'s `open-webui` entry exactly). Backend paths are relative to
`backend/open_webui/`; frontend paths to `src/`. `docs.openwebui.com` was not consulted.

Companion notes, not repeated here except where this one supersedes/extends them:
`docs/research/owui-model-record.md` (the record, sync form, capabilities, merge, visibility)
and `docs/research/owui-engine-connection.md` (the Chat Completions connection, task-model
routing). Context assumed throughout, matching those notes: `ENABLE_PERSISTENT_CONFIG=false`,
`BYPASS_ADMIN_ACCESS_CONTROL=true`, no `OPENAI_API_CONFIGS`, `TASK_MODEL_EXTERNAL` = the base
model's own id (`gideon-generator`), GIDEON's `permissions.yaml` (`compose/open-webui/permissions.yaml`)
setting `chat.controls`/`chat.system_prompt`/`chat.params` all `false` for every `user`-role
account and `settings.interface: true`.

**Headline finding (Q4 — read this first, it changes ticket 07's plan):** with GIDEON's
target shape for a preset (`capabilities.builtin_tools: false`, `capabilities.web_search:
true`, no `params.function_calling` override, so it defaults to `'native'`), turning the
per-chat web-search toggle on does **nothing** — neither the native `search_web` tool (built-in
tools are off) nor the non-tool RAG path (`chat_web_search_handler`) ever runs, because that
RAG path is gated on `metadata.params.function_calling == 'legacy'`, a **separate** field from
`capabilities.builtin_tools`, and GIDEON's plan never sets it. See §4.

---

## 1. How a preset's `params.system` becomes the request's system message

### 1.0 Two independent application points, in this order

A browser chat turn (`POST /api/chat/completions`, `main.py:1085-1092`) runs:

```
main.py:chat_completion            — merges model params into form_data['params'] (§1.5)
  → process_chat                    (main.py:1622, local function)
      → process_chat_payload        (utils/middleware.py — "the middleware")
      → chat_completion_handler     (= utils/chat.py:generate_chat_completion)
          → generate_openai_chat_completion (= routers/openai.py:generate_chat_completion)
```

**The preset's `params.system` is applied exactly once, in `routers/openai.py`'s
`generate_chat_completion`, at `routers/openai.py:1500-1511`** — the same site the
model-record note's §4.2 already found for the *preset-inherits-connection* question, cited
here in full because it is also the *only* place the system-prompt string is actually spliced
into `form_data['messages']`:

```python
model_info = await Models.get_model_by_id(model_id)   # fresh DB read, by the pre-swap (preset's own) id
...
if model_info:
    ...
    params = model_info.params.model_dump()
    if params:
        system = params.pop('system', None)
        payload = apply_model_params_to_body_openai(params, payload)
        if not bypass_system_prompt:
            payload = await apply_system_prompt_to_body(system, payload, metadata, user)
```
(`routers/openai.py:1493, 1503-1511`, `bypass_system_prompt` confirmed new at this tag —
read from `request.state.bypass_system_prompt`, settable only by internal server-side callers
such as the native tool-call loop's own follow-up completions, never by an external request;
`routers/openai.py:1478-1485`.)

This runs **after** `process_chat_payload` (the middleware) has already finished. The
middleware does **not** itself splice the model's system prompt into `form_data['messages']`
— it only (a) applies the *existing* system message already in the request (the chat-controls/
personal one, if the client sent one — see §1.2) with variable substitution, via
`apply_system_prompt_to_body(..., replace=True)` (`utils/middleware.py:2503-2508`), and (b)
separately resolves the model's own system prompt purely to stash it in
`metadata['system_prompt']` (`utils/middleware.py:3059-3071`) as a restore point for the
**native tool-call loop** (so a follow-up completion after a tool call can revert to the
pre-RAG system content rather than one with file-context already injected) — not consumed to
build the outgoing request itself. **Verified: `resolved_model_system_prompt` computed at
`middleware.py:3062` is never written into `form_data['messages']`** — only into
`metadata['system_prompt']`, read back only inside the tool-call loop in `process_chat_response`
(`middleware.py:5566-5571`), a code path GIDEON's builtin-tools-off preset never enters. **No
double-application**: `form_data['params']['system']` (populated earlier still, §1.5) is
explicitly deleted, unread, inside `apply_params_to_form_data` before it ever reaches an
engine-param mapping (`utils/payload.py:96,101-103` — `'system'` is one of the
`open_webui_params` keys stripped from `params` before the rest are mapped to sampling
fields) — so that copy is inert, and the fresh `model_info.params.system` read at
`routers/openai.py:1507` is the sole source of truth.

### 1.1 (a) Prepended, not merged-in-place, not a replace

`apply_system_prompt_to_body` (`utils/payload.py:49-63`), called from `routers/openai.py:1511`
with **`replace` at its default `False`**, goes to the `else` branch:
`form_data['messages'] = add_or_update_system_message(system, form_data.get('messages', []))`.

`add_or_update_system_message` (`utils/misc.py:682-696`, default `append: bool = False`):
if `messages[0]` is already a system message, it calls `update_message_content(messages[0],
content, append=False)`, whose non-append branch (`utils/misc.py:667-670`) is
`message['content'] = f'{content}\n{message["content"]}'` — **the preset's system prompt goes
first, a newline, then whatever was already there.** If there is no existing system message,
one is inserted at position 0 with the preset's text alone (`utils/misc.py:692-694`).

**Concretely, for a browser turn on General**: if the acting user set neither a personal
General-settings system prompt nor a per-chat one (both possible even with GIDEON's
permission set — see §1.4), `form_data['messages'][0]` is exactly
`{role: 'system', content: <preset's resolved system text>}`. If the user *did* set a personal
one, the final system message is `"<preset text>\n<user's personal text, already variable-
substituted by the middleware>"`.

### 1.2 (b) Variable substitution — the full chain, and what happens to an unmatched token

`resolve_system_prompt` (`utils/payload.py:17-41`, called from `apply_system_prompt_to_body`)
runs, **in this order, each a plain string transform**:

1. `render_chat_variables(system, metadata.get('chat_variables', {}), required=False)`
   (`utils/chat_variables.py:254-273`) — regex `{{chat.variables.KEY}}` (case-sensitive path
   segment, `CHAT_VARIABLE_ANY_RE`). Per-chat custom form variables (chat's own `variables`
   feature, unrelated to this ticket). **An unresolved key is replaced with the empty
   string**, not left literal — the regex always matches and substitutes (`validated.get(key,
   '')`, `chat_variables.py:270-271`).
2. `render_user_variables(system, user.variables)` (`utils/chat_variables.py:276-288`) —
   regex `{{user.variables.KEY}}` (`USER_VARIABLE_ANY_RE`). Per-user custom profile
   variables. Same behaviour: **an unresolved or invalid key becomes `''`**, always matched
   and substituted (`chat_variables.py:284-286`).
3. `prompt_variables_template(system, metadata.get('variables', {}))` (`utils/task.py:30-33`)
   — only runs `if variables:` (skipped entirely if `metadata['variables']` is empty/absent).
   Plain `template.replace(literal_key, value)` per dict entry, where the dict's own keys
   already carry the braces (`'{{USER_NAME}}'`, etc. — not a regex). **A key with no matching
   entry in the dict is left completely untouched** (a `str.replace` on a substring that isn't
   present is a no-op) — this is the family that answers "a literal `{{` with no recognized
   token": **left alone, verbatim, no error.**
4. `await prompt_template(system, user)` (`utils/task.py:36-104`) — server-computed fallback,
   same `str.replace`-per-token mechanism, same "unmatched left alone" behaviour, populated
   from the DB user record (`utils/task.py:70-78`, `91-103`).

**`metadata['variables']` (step 3) is populated only by the browser client, on every
message send** — `src/lib/components/chat/Chat.svelte:3576-3581` sends
`variables: { ...getPromptVariables($user?.name, $settings?.userLocation ? userLocation :
undefined, $user?.email) }` as part of the outgoing payload, and `main.py:1259`
(`'variables': form_data.get('variables', {})`) carries it into `metadata` untouched.
`getPromptVariables` (`src/lib/utils/index.ts:1198-1209`) returns exactly these nine keys:

```
{{USER_NAME}}, {{USER_EMAIL}}, {{USER_LOCATION}}, {{CURRENT_DATETIME}}, {{CURRENT_DATE}},
{{CURRENT_TIME}}, {{CURRENT_WEEKDAY}}, {{CURRENT_TIMEZONE}}, {{USER_LANGUAGE}}
```

**This is the load-bearing, non-obvious finding for `{{CURRENT_TIMEZONE}}` and
`{{USER_LANGUAGE}}`: they are substituted for a browser turn, but only because the frontend
sends them in `metadata.variables` and step 3 above consumes that dict — `prompt_template`
(step 4, the server-only fallback) does *not* know either token** (grepped `utils/task.py` in
full: its own hardcoded list is `CURRENT_DATE, CURRENT_TIME, CURRENT_DATETIME,
CURRENT_WEEKDAY, USER_NAME, USER_EMAIL, USER_BIO, USER_GENDER, USER_BIRTH_DATE, USER_AGE,
USER_LOCATION, USER_GROUPS` — no timezone, no language). **Consequence for an API caller (see
§1.5) that doesn't send `variables`**: `{{CURRENT_TIMEZONE}}` and `{{USER_LANGUAGE}}` in a
preset's system prompt would reach the engine **completely unsubstituted, as literal text** —
every other token in this family has a step-4 fallback; these two do not. If General's system
prompt text is ever written to include either token, this is worth flagging to whoever writes
that copy.

**Full step-4-only variable set** (server-computed, used whenever `metadata.variables` doesn't
carry a given token — i.e., always for an API caller, and as an additional/overlapping source
for a browser turn on the few tokens step 3 doesn't send): `{{USER_BIO}}`, `{{USER_GENDER}}`,
`{{USER_BIRTH_DATE}}`, `{{USER_AGE}}`, `{{USER_GROUPS}}` (`utils/task.py:96-102`) — these have
**no frontend-side equivalent at all**, so they only ever resolve via the server DB read, for
both browser and API callers alike.

The two token *families* behave differently on a genuinely-unrecognized `{{...}}` string: the
`chat.variables.`/`user.variables.` family (steps 1-2) match a broad regex and always emit
something (empty string on failure); the flat named-token family (steps 3-4) is pure
substring replacement and leaves anything not on its list untouched. A bare `{{` or `}}` that
matches neither regex and no literal token is left completely as-is in every case — no code
path here ever raises on unmatched braces.

### 1.3 (c) Nothing else is added by the system-prompt path itself when every capability is off

Confirmed no additional text is spliced into the system message by the mechanisms this ticket
asked about, beyond what §1.1-1.2 already describes:

- **Memory**: `add_memory_context` (`utils/memory.py:289-291`, `model_allows_memory` gate)
  runs, if at all, at `utils/middleware.py:2667` — well before the preset's own system prompt
  is applied (`routers/openai.py:1507`, a different function entirely) and, per
  `docs/research/owui-model-record.md` §2.5, is fully suppressed by
  `meta.capabilities.memory: false` regardless of the frontend's `ENABLE_MEMORIES` default or
  any user's personal toggle. **Confirmed unchanged at this tag** — `model_allows_memory`
  short-circuits before any memory content is read. GIDEON additionally sets the
  `features.memories` *permission* false in `compose/open-webui/permissions.yaml`, an
  independent belt-and-suspenders gate on the same code path
  (`is_builtin_tool_enabled('memory') and features.get('memory') and
  get_model_capability('memory') and await has_user_permission('memories')`,
  `utils/tools.py:658-663`).
- **A "date line" or similar frontend-injected text**: not found. Grepped every
  `add_or_update_system_message`/`apply_system_prompt_to_body` call site in
  `utils/middleware.py` (voice-mode template, folder `system_prompt`, the chat-controls
  message, RAG citation template) — none of them fire for a plain chat turn with no folder,
  no voice mode, and no attached files/knowledge. `DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE`'s
  own "Today's date is: `{{CURRENT_DATE}}`." line (`config.py:2331`) is that **task's own**
  prompt template (search-query generation), unrelated to the chat's own system message.
- **File/RAG context**: `chat_completion_files_handler` (gated by `capabilities.file_context`)
  injects source context into the **user**-message/prompt path
  (`apply_source_context_to_messages`, `utils/middleware.py:3074-3076`; under the default
  `RAG_SYSTEM_CONTEXT=False`, `env.py`, set true it would append to the system message
  instead), not the system message — confirmed by reading the call site, separate from the
  system-prompt splice. **Corrected 2026-09-09 (slice-1 ticket 65):** the hedge that stood
  here ("no files are ever attached with no attachment UI reachable") was wrong for General:
  the non-tool web search attaches its loaded pages to the request as a `web_search` files
  item (`chat_web_search_handler`'s bypass append), so the handler is reached on every
  searched turn and the record's `file_context` decides whether the pages reach the model.
  GIDEON rendered the flag false through `v0.1.35`, which dropped every page after the
  "Searched N sites" status, and renders it true from ticket 65's release; `file_upload:
  false` still keeps attachments out (`docs/research/owui-model-record.md` §2.2–2.3).

### 1.4 (d) Personal vs. per-chat system prompt: override, not merge — and GIDEON's permission set does not close the personal one

**The frontend combines them with `??` (fallback), never string-concatenation** —
`src/lib/components/chat/Chat.svelte:3467-3468`:
```js
params?.system || $settings.system
    ? { role: 'system', content: `${params?.system ?? $settings?.system ?? ''}` }
    : undefined
```
`params` here is `Chat.svelte`'s own local state for **Chat Controls' per-chat System
Prompt / Params** (persisted with the chat, `structuredClone(chatContent?.params ?? {})`,
`Chat.svelte:2320`) — a completely different object from a model record's `params`.
`$settings.system` is the **personal** Settings → General → System Prompt
(`src/lib/components/chat/Settings/General.svelte:260-265`, saved as `settings.system` via
`saveSettings`). **The per-chat one wins outright if set; there is no combination of the
two** — only one or the other (or neither) becomes the single system message the frontend
sends, which the middleware then re-processes (§1.1) and the preset's own prompt is
subsequently prepended to.

**Permission gates, confirmed from source, matching the ticket's names**:
- `chat.controls` gates the entire Chat Controls panel's visibility:
  `src/lib/components/chat/ChatControls.svelte:65`,
  `showControlsTab = $user?.role === 'admin' || ($user?.permissions?.chat?.controls ?? true)`.
  With GIDEON's `chat.controls: false`, a `user`-role account never sees the Controls tab at
  all, so the per-chat System Prompt and Params fields inside it are unreachable regardless of
  their own sub-gates.
- Independently (defense in depth, only relevant if `controls` were ever re-enabled without
  these): `chat.system_prompt` gates the System Prompt field specifically
  (`src/lib/components/chat/Controls/Controls.svelte:109`,
  `$user?.role === 'admin' || ($user?.permissions.chat?.system_prompt ?? true)`), and
  `chat.params` gates the sampling-params section (`Controls.svelte:131`, same pattern). Both
  are nested inside the outer `controls` gate (`Controls.svelte:56`) in the actual component
  tree, so GIDEON's `controls: false` alone already hides both.

**The gap: none of `chat.controls`/`chat.system_prompt`/`chat.params` gate the *personal*
Settings → General → System Prompt field.** `General.svelte:260-265` has **no permission
check of any kind** around the `system` textarea — it is gated only by whichever permission
governs reaching the Settings modal at all, which is `settings.interface`
(`USER_PERMISSIONS_SETTINGS_INTERFACE`, `config.py:1863` section), enforced server-side at
save time in `routers/users.py:498-511` (`update_user_settings_by_session_user` — the *only*
permission check in that whole endpoint is `settings.interface`; it separately strips
`toolServers`/`notifications` keys under unrelated permission checks, but never touches
`system`). **GIDEON's `compose/open-webui/permissions.yaml` sets `settings.interface: true`**
— so a `user`-role account can always set a personal system prompt via Settings → General,
and (per §1.1) it still becomes part of every chat's outgoing system message, with the
preset's own system prompt prepended in front of it, not overriding it. This is worth flagging
to whoever wrote §15/ticket 07's spec: disabling Chat Controls does not close this surface.

### 1.5 (e) Task calls (title/tags/search-query generation) do **not** get the preset's system prompt

Confirmed via the routing chain, not merely the model-record note's unreachable-code argument
(which covered builtin tools, a different question):

- Every task route (`routers/tasks.py`, e.g. `generate_title`, `generate_chat_tags`,
  `generate_search_query`) computes `task_model_id, task_model_params =
  get_task_model_generation_config(model_id, models)` and sets `payload['model'] =
  task_model_id` (`routers/tasks.py:169-185, 237-250, 302-315`, etc.) **before** calling
  `generate_chat_completion`.
- `get_task_model_id` (`utils/task.py:16-27`, cited in the engine-connection note's §3):
  since `gideon-generator`'s connection is `'external'` (not `'local'`), it takes the `else`
  branch — with GIDEON's `TASK_MODEL_EXTERNAL` = the base model's own id, `task_model_id`
  always resolves to **`gideon-generator` (the base model), never the preset the chat was
  actually using.**
- `routers/openai.py`'s `generate_chat_completion` (§1.0) then does `model_info =
  Models.get_model_by_id('gideon-generator')` — the **base model's own DB record** — and pops
  `system` from *that* record's `params`, not the preset's. **So a preset's `params.system`
  never reaches a task call under GIDEON's routing; only the base model's own `params.system`
  (if GIDEON's base-model record ever set one) would.** This is a general mechanism finding,
  independent of what GIDEON's actual base-model record currently contains.
- `chat_web_search_handler`'s own query-generation call (§4) goes through this identical task
  path (`generate_queries`, `utils/task.py`/`routers/tasks.py`'s `generate_search_query`),
  so it is likewise unaffected by the preset's system prompt.

### 1.6 (f) An API caller (bearer key, no `session_id`) posting `model: <preset id>` to `/api/chat/completions` **does** get the preset's system prompt

The `session_id` gate (`utils/middleware.py:2769-2773`, per the model-record note's §3.2)
governs **only** whether builtin tools are attached — it has no bearing on the system-prompt
splice. Tracing the same call graph as §1.0 for a request with no `session_id` key in the
payload:
- `main.py`'s `chat_completion` route (`main.py:1085-1092`) requires only
  `Depends(get_verified_user)` — session cookie or bearer API key both satisfy this, no
  `session_id` field is required in the body (`metadata['session_id'] = form_data.pop
  ('session_id', None)`, `main.py:1249` — defaults to `None` cleanly if absent).
- `process_chat_payload` still runs unconditionally (§1.0's call graph has no
  `session_id`-gated branch point before it), then `chat_completion_handler` →
  `generate_openai_chat_completion` → `routers/openai.py:generate_chat_completion`, whose
  `model_info.params.system` pop (`openai.py:1507`) is keyed purely on `model_id` (from
  `form_data.get('model')`) and the internal `bypass_system_prompt` flag (never set by an
  external request, §1.0) — **no condition here reads `session_id` or `metadata.session_id`
  at all.**
- **Conclusion: yes** — an API caller's `model: <preset id>` turn on `/api/chat/completions`
  (or `/api/v1/chat/completions`, the same route) gets the preset's system prompt prepended
  exactly as a browser turn would, the only difference being that `metadata.variables` will
  typically be empty (an API caller wouldn't normally send the browser's
  `getPromptVariables()` payload) — so per §1.2, any `{{CURRENT_TIMEZONE}}`/`{{USER_LANGUAGE}}`
  token in the preset's text would go through unsubstituted, and the other named tokens would
  fall through to the server-computed `prompt_template` (step 4) instead of the frontend's
  values.

---

## 2. The `/list` read-back — `params`, `access_grants`, `meta`, `write_access`

### 2.1 `write_access` for the syncing admin — confirmed `True`, and `params` round-trips

`routers/models.py:186-198` (the `/list` handler body, superseding nothing in the model-record
note — same lines it already cited, read here in full for the round-trip question):

```python
write_access = (
    (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
    or user.id == model.user_id
    or model.id in writable_model_ids
)
if not write_access:
    data['params'] = {}
items.append(ModelAccessResponse(**data, write_access=write_access))
```

With GIDEON's `BYPASS_ADMIN_ACCESS_CONTROL=true` and the syncing caller being an admin,
`write_access` is `True` unconditionally (first clause alone decides it) — **`params` is
returned intact, never zeroed, for the admin API key**, confirmed.

### 2.2 `ModelParams` declares **no fields at all** — full-fidelity, no coercion, of anything

```python
class ModelParams(BaseModel):
    """Parameters for model inference (temperature, top_p, etc.)."""
    model_config = ConfigDict(extra='allow')
```
(`models/models.py:71-74`.) **This is the single most important fact for the round-trip
question**: despite the docstring, `ModelParams` declares zero typed fields — `system`,
`temperature`, every sampling knob, everything is stored purely as pydantic v2 "extra" data,
with **no validator, no type coercion, no stripping of any kind** on any key. A pushed
`params: {"system": "line one\n\nline two   "}` (embedded newlines, trailing spaces) is
preserved byte-for-byte through `ModelModel.model_dump()` at sync time (a plain dict merge
into the JSON column, `models/models.py:598-647`, cited in the model-record note's §1.2) and
comes back byte-for-byte in the `/list` response body for a write-access caller — confirmed by
there being no code between the DB write and this read that touches the `params` JSON blob at
all except the `del model['info']['params']` deletion the model-record note's §4.1 already
covered (that deletion applies to the *merged live-models* dict used by `/api/models`, an
entirely separate code path from this `/list`/`/base` admin listing, which reads the DB
directly via `search_models`/`_to_model_model` and never touches that deletion). **No numeric
casting either** — with no declared `float`/`int` fields, a pushed `"temperature": "0.7"` would
stay the *string* `"0.7"` all the way through (not itself tested here, but follows directly
from the class having no typed fields to coerce into).

### 2.3 `access_grants` — present, real, batch-fetched, and shows the *current* stored grants (not payload echo)

`Models.search_models` (`models/models.py:347-447`, superseding nothing but adding detail to
the model-record note's citation) batch-fetches real grant rows and attaches them per item
**before** building the response, rather than echoing whatever was last synced:
```python
model_ids = [model.id for model, _ in items]
grants_map = await AccessGrants.get_grants_by_resources('model', model_ids, db=db)
...
ModelUserResponse(**(await self._to_model_model(model, access_grants=grants_map.get(model.id, []), db=db)).model_dump(), user=...)
```
(`models/models.py:430-445`.) Each entry is a full `AccessGrantModel`
(`id, resource_type, resource_id, principal_type, principal_id, permission, created_at`,
per the model-record note's §1.1) reflecting the **DB's current stored grants** for that
model id — the same rows `set_access_grants` wrote at the last `/sync` (model-record note
§1.2, §5.2), not a re-derivation or a payload echo. For GIDEON's expected shape (no grants
pushed, or `access_grants: []`), this key comes back as an empty list `[]` per item.

### 2.4 `meta` — complete apart from `profile_image_url`, plus one synthesized key

`routers/models.py:186-188` strips `profile_image_url` from `meta` explicitly
(`data['meta'].pop('profile_image_url', None)`), matching the route decorator's own
`response_model_exclude={'items': {'__all__': {'meta': {'profile_image_url'}}}}` (belt and
suspenders — either alone would remove it). **One key is added that was never in the stored
record**: `add_chat_variables_schema` (`routers/models.py:52-57, 186`) inspects the model's
own `params.system` for `{{chat.variables.KEY[:type...]}}` syntax and, if any is found,
sets `meta.chat_variables_schema` to a generated form schema — present only when the preset's
system prompt actually uses that syntax (General's plan doesn't), absent otherwise. No other
`meta` key is altered, added, or removed on read.

---

## 3. `meta.description` — rendering surfaces, format, length, and the suggestion/image fallbacks

### 3.1 Rendered as Markdown, three browser surfaces, no length limit anywhere

`ModelMeta.description: str | None` (`models/models.py:81-82`) carries **no `max_length` or
any other constraint** — confirmed by reading the full field declaration and its validators
(`models/models.py:78-108`); only `profile_image_url` and `knowledge` have `field_validator`s.
Every rendering site pipes the raw string through `marked.parse(sanitizeResponseContent(...))`
then `DOMPurify.sanitize(...)` — full Markdown rendering, HTML-sanitized:
- **Model dropdown/selector**: `src/lib/components/chat/ModelSelector/ModelItem.svelte:250-254`
  — a tooltip on hover, Markdown-rendered.
- **New-chat placeholder screen**: both `ChatPlaceholder.svelte:101-113` and
  `Placeholder.svelte:185-207` (two component variants at this tag, functionally identical for
  this question) render it as the headline sub-text, visually clamped via CSS
  `line-clamp-3` (`ChatPlaceholder.svelte:103`) — a **display clamp, not a character/byte
  limit**; the full text is still in the DOM and still searchable/selectable, just visually
  truncated to 3 lines.
- **Model selector's fuzzy search index**: `Selector.svelte:293,311` includes the raw
  (un-rendered) description text as a `desc` search key for Fuse.js filtering — plain text
  substring matching against the Markdown source, not the rendered HTML.

No dedicated standalone "model info page" was found in this tag's `src/lib/components/chat/`
tree — the three surfaces above are the description's only browser appearances for a
chat-facing user; the admin/workspace `ModelEditor` (model-record note §6) is where an admin
*edits* it, a plain textarea with no Markdown preview at this tag (not re-verified here in
detail, out of scope for this ticket's question).

### 3.2 `suggestion_prompts` absent → falls back to the global default, then to nothing

`ChatPlaceholder.svelte:141-144` / `Placeholder.svelte:283-286` (identical pattern):
```js
suggestionPrompts={atSelectedModel?.info?.meta?.suggestion_prompts ??
    models[selectedModelIdx]?.info?.meta?.suggestion_prompts ??
    $config?.default_prompt_suggestions ??
    []}
```
With `meta.suggestion_prompts` absent/null on the record, the new-chat screen shows
`$config.default_prompt_suggestions` — the **global** admin-configured default
(`config.py:1699`, `DEFAULT_PROMPT_SUGGESTIONS`, env `DEFAULT_PROMPT_SUGGESTIONS` as a JSON
array, falling back to a **non-empty hardcoded stock list** if the env is unset or `[]`
— `config.py:1664-1699`; not re-transcribed here) — **not an empty list**, unless GIDEON's
render explicitly sets that env var to `'[]'`. If GIDEON leaves `DEFAULT_PROMPT_SUGGESTIONS`
unset, General (and the base model) would show Open WebUI's stock suggestion prompts on the
new-chat screen rather than nothing — worth confirming against GIDEON's actual rendered env if
"no suggestions" is the intended behaviour for a preset that doesn't set its own.

### 3.3 `profile_image_url` absent → server-side 302 to the license-protected default logo

The frontend always requests the image via a fixed URL
(`${WEBUI_API_BASE_URL}/models/model/profile/image?id=<id>&lang=<lang>`,
`ChatPlaceholder.svelte:57`), never reading `profile_image_url` directly (consistent with it
being stripped from every listing response, §2.4). Server-side, `GET /model/profile/image`
(`routers/models.py:642-720`): when the DB record's `meta.profile_image_url` is absent (and no
matching arena-model fallback exists), the route falls through every branch to:
```python
return RedirectResponse(url='/static/favicon.png', status_code=status.HTTP_302_FOUND)
```
(`routers/models.py:717-720`, with an explicit code comment: "LICENSE covers this Open WebUI
fallback logo. Do not alter, remove, obscure, or replace it except as LICENSE permits.") — a
real HTTP redirect, not an inline default image; the same fallback is hit client-side too via
the `<img>` tag's `onerror` handler pointing at `/favicon.png` (`ChatPlaceholder.svelte:60-67`)
for any image-load failure (e.g. a 404, though this route never actually 404s — it always
resolves to *some* redirect or streamed image).

---

## 4. Web search with `capabilities.builtin_tools: false` — the RAG path is gated on `function_calling`, not on builtin tools, and GIDEON's plan leaves it unreachable

### 4.1 The two paths, and the gate that actually matters

`utils/middleware.py:2666-2678` (inside `process_chat_payload`, the `features` dict already
popped off `form_data`):
```python
if 'web_search' in features and features['web_search'] and await Config.get('web.search.enable'):
    if getattr(user, 'role', None) == 'admin' or await has_permission(
        getattr(user, 'id', ''), 'features.web_search', await Config.get('user.permissions')
    ):
        # Skip forced RAG web search when native FC is enabled - model can use web_search tool
        if metadata.get('params', {}).get('function_calling') == 'legacy':
            form_data = await chat_web_search_handler(request, form_data, extra_params, user)
```
**The non-tool RAG path (`chat_web_search_handler`) is gated on
`metadata.params.function_calling == 'legacy'` — a `ModelParams` field the ticket didn't ask
about, entirely separate from `capabilities.builtin_tools`.** The comment states the authors'
own assumption plainly: under native function calling (the default), they expect the
`search_web` **tool** to be used instead, so the RAG path is deliberately skipped.

`metadata.params.function_calling` defaults to `'native'` when nothing overrides it —
`main.py:1263-1272`:
```python
'function_calling': (
    form_data.get('params', {}).get('function_calling')
    or model_info_params.get('function_calling')
    or 'native'
),
```
`model_info_params` here is the **preset's own** `params` merged with global
`models.default_params` (`main.py:1141-1148`) — so **the only way to get `'legacy'` is for the
preset's (or a request's) own `params.function_calling` to explicitly say so.** GIDEON's
ticket 07 plan, as read, sets `capabilities.builtin_tools: false` and
`capabilities.web_search: true` but nowhere sets `params.function_calling: 'legacy'` — so
`function_calling` stays `'native'`.

**The tool path**, per the model-record note's §3.2, requires `capabilities.builtin_tools`
true (`utils/middleware.py:2769-2773`) to even call `get_builtin_tools()`, which is what would
offer `search_web`/`fetch_url` (`utils/tools.py:678-685`). GIDEON's `builtin_tools: false`
suppresses this unconditionally.

**Net result for General's exact target shape (verified, not inferred): both paths are
closed simultaneously.** Turning the web-search toggle on in the composer sends
`features.web_search: true`; `chat_web_search_handler` never runs (native FC); no
`search_web` tool is ever attached (builtin tools off) — **the toggle has zero effect on the
model's behaviour**, silently. There is no error, no warning surfaced to the user; the turn
just proceeds without any search having happened, indistinguishable in the response from web
search never having been toggled at all (not independently confirmed here whether any
`status`/event is still emitted for the toggle itself — `chat_web_search_handler`'s own
`status: web_search: Searching the web` event, `middleware.py:1502-1510`, is inside the
function that never gets called, so **no such status event fires either** — the toggle is
inert end-to-end, not merely silent-but-attempted).

**To make GIDEON's requirement ("the non-tool path must exist because the engine has no
tool-call parser") actually true, the preset (or the base model) needs `params.function_calling:
'legacy'` set alongside `capabilities.builtin_tools: false`.** This also affects the other two
`function_calling`-gated RAG paths in the same block (`image_generation`'s prompt injection,
`middleware.py:2680-2688`; `code_interpreter`'s XML-tag prompt injection,
`middleware.py:2691-2699`) — both are off in General's planned capabilities anyway, so setting
`function_calling: 'legacy'` would not turn on anything unwanted there, only make the RAG
branch reachable for whichever of the three features has its capability turned on. (Memory's
own context-injection, `middleware.py:2660-2665`, is **not** gated by `function_calling` at
all — it runs whenever `features.memory` and `Config.get('memories.system_context.enable')`
are both true, independent of native/legacy; not affected by this recommendation either way,
and already closed by `capabilities.memory: false` per the model-record note.)

### 4.2 The composer's web-search button and the confirmation dialog

**Button gating — three conditions, not two:**
```js
$: showWebSearchButton =
    selectedModelIds.length === webSearchCapableModels.length &&
    $config?.features?.enable_web_search &&
    ($_user.role === 'admin' || $_user?.permissions?.features?.web_search);
```
(`src/lib/components/chat/MessageInput.svelte:805-808`.) Beyond `capabilities.web_search`
(via `webSearchCapableModels`, `getCapableModelIds(..., 'web_search', ...)`, default `true`
when unset, `MessageInput.svelte:745, 757-762`) and the global `ENABLE_WEB_SEARCH`
(`$config.features.enable_web_search`), there is a **third gate not in the ticket's list**:
the acting user's own `features.web_search` **permission**
(`USER_PERMISSIONS_FEATURES_WEB_SEARCH`) — an admin always passes it; a `user`-role account
needs `permissions.chat` — actually `permissions.features.web_search` true. GIDEON's
`permissions.yaml` sets `features: web_search: true`, so this gate is open for every
`user`-role account, consistent with the plan's intent — but it is a real, independent
condition worth naming since a future permission change could hide the button even with the
model capability and global flag both on.

**Confirmation dialog — global only, no per-model override key exists.** `ENABLE_WEB_SEARCH_
CONFIRMATION` (`config.py:1157`, default `False`) and `WEB_SEARCH_CONFIRMATION_CONTENT`
(`config.py:1159-1161`) are both plain env-derived globals, exposed to the frontend as
`config.features.enable_web_search_confirmation` /
`config.features.web_search_confirmation_content` and consumed only that way
(`Chat.svelte:350, 3127, 4193-4194`). Grepped `ModelMeta`/`meta.capabilities` for any
confirmation-related key: none exists — a model record cannot turn this dialog on or off for
itself; it is one setting for the whole deployment.

---

## 5. (Lower priority — ticket 28) `file_upload: false` capability + `chat.file_upload`/`chat.web_upload: true` permissions: what the composer offers, and the "Attach Webpage" gap

### 5.1 "Attach Webpage" is gated by the permission alone — the file-upload capability is irrelevant to it

Two independent items in the "More" (`+`) menu, with two independent gates
(`src/lib/components/chat/MessageInput/InputMenu.svelte:66-73`):
```js
let fileUploadEnabled = true;
$: fileUploadEnabled =
    fileUploadCapableModels.length === selectedModels.length &&
    ($user?.role === 'admin' || $user?.permissions?.chat?.file_upload);

let webUploadEnabled = true;
$: webUploadEnabled = $user?.role === 'admin' || ($user?.permissions?.chat?.web_upload ?? true);
```
`fileUploadCapableModels` is derived from `capabilities.file_upload` per selected model
(default `true` if unset, same `getCapableModelIds` helper as §4.2). **The regular file-upload
item is gated by both the model capability and the `chat.file_upload` permission — but the
"Attach Webpage" item checks `webUploadEnabled` only, which reads solely `$user.permissions
.chat.web_upload` (or admin role).** It has **no reference anywhere to `capabilities.file_
upload`, `fileUploadCapableModels`, or any other model-record field** — confirmed by reading
the button's own template and click-handler (`InputMenu.svelte:245-263`), which check only
`webUploadEnabled`. **On a model with `capabilities.file_upload: false` and
`chat.web_upload: true`, "Attach Webpage" is the one enabled item in that menu** — matching
ticket 26's on-box observation exactly (the break-glass admin saw everything greyed out except
"Attach Webpage"; an admin also always passes `webUploadEnabled` via the `role === 'admin'`
clause regardless of any permission value, so that observation doesn't by itself distinguish
"permission gate" from "admin bypass" — this source read is what pins it down to the
permission specifically for a `user`-role account).

### 5.2 The server-side fetch: no permission re-check, honours the proxy env, no INFO-level URL log

The modal's submit calls `processUrl` (`src/lib/apis/retrieval/index.ts:339-372`) →
`POST /api/v1/retrieval/process/url` → `routers/retrieval.py:2287-2352` (`process_url`),
requiring only `Depends(get_verified_user)`.

**No server-side re-check of `chat.web_upload` was found anywhere in this call chain.**
Grepped `web_upload` across the entire backend (`backend/open_webui/`): the only two hits are
the permission's own default-value line in `config.py:1998` and its Pydantic field default in
`routers/users.py:257` — **it is never read by any route handler to gate behaviour.** The
permission is enforced **only** client-side (`webUploadEnabled` in §5.1, and an equivalent
guard in `Chat.svelte:1769-1772`'s `uploadWeb` before it even calls `processUrl`). **A verified
user (or a bearer API key) with `chat.web_upload: false` could still call
`POST /api/v1/retrieval/process/url` directly and have the URL fetched** — the permission does
not close this path server-side. Worth flagging plainly for ticket 28's "is this an egress path
§15's posture must close" question: as read at this tag, **it is not closed by the permission
at all, only hidden by the UI.**

**Fetch mechanics** (`routers/retrieval.py:2176-2189`, `_fetch_url`, called from `process_url`
for the direct-file-download branch, and `process_web`/`get_content_from_url` for the
HTML-page branch): both go through `get_ssrf_safe_session()`
(`retrieval/web/utils.py:269-282`), called with its default `trust_env=True`. The function's
own docstring is explicit: **"trust_env also enables environment proxies, and proxied traffic
bypasses the connect-time IP check, because the proxy resolves the hostname instead."** So
**yes, the frontend container's proxy env (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`, if rendered)
is honoured for this fetch** — aiohttp's `trust_env=True` reads those variables from the
process environment. The same function also runs an SSRF guard (`validate_url`,
`retrieval/web/utils.py:129`, and a custom `_SSRFSafeConnector` that re-validates resolved IPs
per-connection) — not fully traced here (out of scope: this ticket asked about proxy/logging,
not the SSRF allow/deny rules themselves).

**Logging**: grepped every `log.*` call in `process_url`/`process_web`
(`routers/retrieval.py:2287-2432`): the URL appears in a `log.warning` (YouTube-transcript
failure only) and a `log.debug` (full extracted text content) — **no `log.info` call logs the
URL** in this code path at this tag. The URL also never appears in a query string (it's a JSON
POST body field), so it would not surface in a standard access-log line either, only in an
explicit debug-level application log if one were enabled.

### 5.3 Added 2026-09-05 (slice-1 ticket 28's triage): three facts §5.2 did not reach, read at the same tag

**The route has three branches, and one is a file upload.** `process_url` (`routers/retrieval.py:2287-2352`)
dispatches on what the URL answers: (a) a YouTube URL goes to `process_web` and the transcript loader;
(b) an HTML/text URL goes to `process_web` → `get_content_from_url` (`retrieval/utils.py:233-286`: `validate_url`,
an SSRF-guarded `requests` probe, then `get_web_loader` with `trust_env=config.get('web_search_trust_env')`);
(c) any other content type — PDF, DOCX, image — is downloaded by `_fetch_url` (`:2176-2189`, capped by
`FILE_MAX_SIZE`) and handed to **`upload_file_handler`** (`routers/files.py:315`) with `metadata={'source_url': url}`,
producing a File row, `process_file`, and a `file` item on the chat (`Chat.svelte:1809-1818`). So with
`capabilities.file_upload: false` hiding the file items, "Attach Webpage" still attaches a document to the chat by URL.

**Embedding.** Branch (b) calls `save_docs_to_vector_db` unless `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL` is true
(`:2398-2410`) — the web-search bypass, not the file one — under a collection named by the URL's SHA-256. Branch (c)
runs `process_file`, which under `BYPASS_EMBEDDING_AND_RETRIEVAL=true` stores the extracted text on the File row and
writes no vectors (`:2002-2015`). Neither flag is set by the frontend's own defaults (`config.py:959`, `:1166`).

**Logging, corrected.** §5.2's "no `log.info` carries the URL" is literally true and under-reports: `process_web` logs
`log.warning('YouTube transcript unavailable for %s: %s', form_data.url, e)` (`:2376`), which the default INFO level
emits, and its generic handler runs `log.exception(e)` (`:2382`) on a fetch failure, whose text names the host. The
loader's proxy switch is `WEB_SEARCH_TRUST_ENV` (`config.py:1206`, default `True`), read through `get_loader_config`
(`retrieval/utils.py:130-148`), so the environment proxies are honoured at the upstream default and stop being honoured
if that value is ever rendered false; `_fetch_url`'s session is `get_ssrf_safe_session()` at its `trust_env=True`
default. Not verified: whether the YouTube transcript client reads the environment proxies (`youtube_proxy_url` is its
own setting, unset), and the frontend's admin surfaces that call the same route.

---

## Unverified / out of this note's scope

- `chat_web_search_handler`'s exact RAG-injection mechanics once reached (how search results
  become message content) — read only far enough to confirm it calls `generate_queries` (a
  task call, §1.5) and starts an event-emitter status; the actual context-injection call
  (presumably into `sources`/`apply_source_context_to_messages`, mirroring the file-context
  path) was not traced line-by-line, since the gate in §4.1 is what matters for the ticket and
  the injection shape itself is unaffected by that gate.
- `validate_url`'s exact SSRF allow/deny rules (`retrieval/web/utils.py:129-`) — read only
  enough to confirm it runs and that `trust_env` still lets a configured proxy through; the
  specific IP-range/scheme rules were not enumerated.
- Whether any admin-configurable per-model override for `ENABLE_WEB_SEARCH_CONFIRMATION`
  exists via some mechanism other than `ModelMeta`/`capabilities` (e.g. a Function/Filter
  hook) — only the direct model-record fields were checked, per the ticket's framing.
- The exact hardcoded stock list `DEFAULT_PROMPT_SUGGESTIONS` falls back to when its env is
  unset (`config.py:1668-1699`) — confirmed non-empty and present, contents not transcribed
  here as not load-bearing for the ticket's question (only "does it fall back to something or
  nothing" was asked).
- Whether GIDEON's actual render currently sets `DEFAULT_PROMPT_SUGGESTIONS` or leaves it at
  the stock default — a render/config question, not a source-behaviour question; not checked
  against GIDEON's own manifest/render code in this pass.

---

## 6. Added after the on-box turn (2026-09-04): a preset's access check walks the base chain, and `meta.hidden` is the selector's switch

**Observed on the box** (`.scratch/slice-1/assets/07-on-box.txt` §5): with General's record carrying the
public-read grant and the base model's record carrying none (`v0.1.8`'s shape), a users-group member
saw General in the selector and got "Model not found" on the first message; the eval identity's API
turn on `gideon-general` answered `400 {"detail":"Model not found"}`, and the frontend's journal
logged `open_webui.main:chat_completion:1618 - Error processing chat metadata: Model not found`.

**Why** (`main.py:1106-1136`, verified against the same tagged source as the sections above, fetched
raw at `v0.11.3`): the chat route looks the selected id up in `request.app.state.MODELS`, reads its
row, and — with `BYPASS_MODEL_ACCESS_CONTROL` unset and, for a non-admin, regardless of
`BYPASS_ADMIN_ACCESS_CONTROL` (`main.py:1121`) — calls `check_model_access(user, model, model_info=…)`
(`utils/models.py:454-495`). That function checks the selected record's own grant
(`AccessGrants.has_access` on the preset's id, `utils/models.py:478-489`) and then, under the comment
"Enforce access on chained base models" (`:491-495`), calls `has_base_model_access`
(`utils/access_control/__init__.py:301-342`), which walks `base_model_id` hop by hop and requires at
each hop that the base model's row exists and that the caller owns it or holds a read grant on it; a
base **without a row** passes only for an admin (`:327-328`; the docstring: "a shared preset cannot be
used to reach a base model the caller could not use directly"). So on the pinned tag a preset is usable
by a `user`-role account only if every base beneath it is readable by that account: an empty grant
list on the base (the model-record note's §5.3 "admin-only shape") makes every preset over it answer
"Model not found", and so does no row at all. The OpenAI router repeats the check on the preset's row
(`routers/openai.py:1513`, the `utils/access_control` variant `check_model_access`, `:345-384`, which
walks the chain at `:387`).

**The shape that works**: the base model's record carries the public-read grant `{user, "*", read}`
too. That makes `gideon-generator` listable to `user`-role accounts by `get_filtered_models`; what
keeps it out of the UI is **`meta.hidden: true`** on its record — a client-side rule: the selector
drops hidden models unless `includeHidden` (`Selector.svelte:64`, default `false`; the filters at
`:370` and `:817`); `Chat.svelte` excludes them from the available ids (`:187`) and from the default
and selected models (`:2021`, `:2086`); the model editor's base-model picker hides them from
non-admins (`ModelEditor.svelte:178-189`). `/api/models` still returns a hidden model — no backend
read of `hidden` exists (grepped `utils/models.py`, `main.py`) — so a signed-in user could address
`gideon-generator` directly with a hand-made request; users cannot mint API keys under §4.2's set.
**Verified on the box**: after the base record gained the grant and the flag, the eval identity's
probe on `gideon-general` answered `200` with `model: gideon-generator` and a reply (transcript §4b),
and the frontend contract module passed with the base record's hand-edit now also unhiding it and
emptying its grant list.

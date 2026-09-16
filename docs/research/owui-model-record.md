---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — the model record: sync form, capabilities, builtin tools, merge, visibility

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`). Backend
paths are relative to `backend/open_webui/`; frontend paths to `src/`. `docs.openwebui.com`
was not consulted as a citation anywhere below.

Companion notes, not repeated here: `docs/research/owui-engine-connection.md` (connection,
discovery, task model, stream handler) and `docs/research/owui-v0.11.0-deployment-contract.md`
(the v0.11.0-era read of `/sync`, re-verified and superseded below at v0.11.3's line numbers
where it differs).

Context assumed throughout: `ENABLE_PERSISTENT_CONFIG=false`, `BYPASS_ADMIN_ACCESS_CONTROL=true`
(`config.py:2100-2105`, itself a plain module constant computed once from
`os.getenv('BYPASS_ADMIN_ACCESS_CONTROL', os.getenv('ENABLE_ADMIN_WORKSPACE_CONTENT_ACCESS', 'True'))`
— not itself gated by `ENABLE_PERSISTENT_CONFIG`, since it is never registered in
`config.py`'s `Config.DEFAULTS` table), no `OPENAI_API_CONFIGS`/`model_ids`, `models` list
in the manifest currently empty.

---

## 1. The sync form, `sync_models`, and `/list`

### 1.1 The route and its form

`routers/models.py:556-575`:

```python
class SyncModelsForm(BaseModel):
    models: list[ModelModel] = []


@router.post('/sync', response_model=list[ModelModel])
async def sync_models(
    request: Request,
    form_data: SyncModelsForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    models = await Models.sync_models(user.id, form_data.models, db=db)
    ...
```

**The sync form's item type is `ModelModel`, not `ModelForm`.** `ModelForm` (`models/models.py:175-184`,
used by `/create`, `/import`, `/model/update`) is the lenient shape with defaults
(`is_active: bool = True`, `access_grants: list[dict] | None = None`). `ModelModel`
(`models/models.py:133-150`) is the **DB round-trip shape**, and every field on it that
lacks a default is **required in the sync payload**:

```python
class ModelModel(BaseModel):
    id: str
    user_id: str
    base_model_id: str | None = None

    name: str
    params: ModelParams
    meta: ModelMeta

    access_grants: list[AccessGrantModel] = Field(default_factory=list)

    is_active: bool
    updated_at: int  # timestamp in epoch
    created_at: int  # timestamp in epoch

    model_config = ConfigDict(from_attributes=True)
```

So **each record in the sync payload must carry `id`, `user_id`, `name`, `params` (key
present, may be `{}`), `meta` (key present, may be `{}`), `is_active`, `updated_at`,
`created_at`** — pydantic will 422 on a record missing any of these keys, even though
(§1.2) `user_id` and `updated_at` are unconditionally overwritten server-side and
`created_at` is not defaulted by the server at all (the caller's value is stored verbatim).
`base_model_id` is the only optional field (defaults `None`).

**`access_grants`, if present, is typed `list[AccessGrantModel]`, not `list[dict]`** —
stricter than `ModelForm`'s `list[dict] | None`. `AccessGrantModel`
(`models/access_grants.py:47-56`) has **no defaults on any field**:

```python
class AccessGrantModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    resource_type: str
    resource_id: str
    principal_type: str
    principal_id: str
    permission: str
    created_at: int
```

So a grant entry in the sync payload must supply `id`, `resource_type`, `resource_id`, and
`created_at` too, even though (§1.2) `set_access_grants` **ignores all four** of those and
only reads `principal_type`/`principal_id`/`permission` off the grant. **Flag for the
render**: this is real friction, not a defensive read — omitting any of those keys 422s
the whole sync call before `sync_models` ever runs; the render must synthesize placeholder
`id`/`resource_type`/`resource_id`/`created_at` values per grant. `resource_type: "model"`
and `resource_id: <the model's own id>` are natural placeholders (they match what the
server forces anyway); `id`/`created_at` can be anything of the right type.

### 1.2 What `sync_models` does — upsert and delete, superseding the v0.11.0 note's line numbers

`models/models.py:598-647` (v0.11.0 note cited `routers/models.py:487-502`; at this tag the
logic lives in `models/models.py`'s `ModelsTable.sync_models`, called from
`routers/models.py:567`):

```python
async def sync_models(
    self, user_id: str, models: list[ModelModel], db: AsyncSession | None = None
) -> list[ModelModel]:
    async with get_async_db_context(db) as db:
        result = await db.execute(select(Model))
        existing_models = result.scalars().all()
        existing_ids = {model.id for model in existing_models}
        new_model_ids = {model.id for model in models}

        for model in models:
            model_data = {
                **model.model_dump(exclude={'access_grants'}),
                'user_id': user_id,
                'updated_at': int(time.time()),
            }
            if model.id in existing_ids:
                await db.execute(update(Model).filter_by(id=model.id).values(**model_data))
            else:
                db.add(Model(**model_data))
            await AccessGrants.set_access_grants('model', model.id, model.access_grants, db=db)

        for model in existing_models:
            if model.id not in new_model_ids:
                await AccessGrants.revoke_all_access('model', model.id, db=db)
                await db.delete(model)

        await db.commit()
        ...
```

Confirmed, precisely:

- **`user_id` is always forced to the caller** (`user_id`, the syncing admin's id) —
  whatever `user_id` the payload's `ModelModel` carried is discarded (`model_data` is built
  from the dump *then* `user_id` is overwritten by the dict-merge). This is true for both
  new inserts and updates of existing rows.
- **`updated_at` is always forced to `int(time.time())`** — the payload's value is
  likewise discarded.
- **`created_at` is *not* overwritten** — it comes straight from the payload
  (`model.model_dump()` includes it, nothing in `model_data` replaces it). For an existing
  row this means **a re-sync overwrites the DB's original `created_at` with whatever the
  payload says** — `ModelModel.created_at` has no default, so the caller must always supply
  a value; if GIDEON's render doesn't track the model's true first-seen time, repeating the
  same fixed epoch value on every push, or `int(time.time())` at render time, are both fine
  since nothing in this tag reads `created_at` for anything but display/sort
  (`routers/models.py:397-414`, `search_models`'s `order_by=created_at`).
- **Existing-id update is a full column replace** (`update(Model).filter_by(id=...).values(**model_data)`)
  — `id`, `base_model_id`, `name`, `params`, `meta`, `is_active` are all overwritten
  wholesale from the payload; there is no per-key merge of `meta`/`params` (a payload with
  `meta: {}` clobbers whatever `meta` the DB row had, e.g. a hand-edited `description` or
  `profile_image_url` made through the admin UI — see §6).
- **`access_grants` is always fully replaced** via `AccessGrants.set_access_grants('model',
  model.id, model.access_grants, db=db)` (`models/access_grants.py:443-478`) for every
  record in the payload, new or existing — all prior grants for that model id are deleted
  and re-inserted from the payload's list (§5 has the exact grant semantics).
- **Any existing DB row whose id is absent from the payload is hard-deleted** — not
  soft-disabled: `await AccessGrants.revoke_all_access('model', model.id, db=db)` then
  `await db.delete(model)` — a real `DELETE FROM model WHERE id=...`, all of that model's
  grants gone too. This is the "desired state, complete set" semantics the v0.11.0 note
  described, confirmed unchanged and re-cited at these v0.11.3 line numbers.
- **`filter_allowed_access_grants`, `is_valid_model_id`, the `base_model_id == id`
  self-reference guard, and `_verify_knowledge_file_access` are never called anywhere in the
  `/sync` path** — those all live only in `create_new_model`/`import_models`/
  `update_model_by_id` (`routers/models.py:265-311, 395-430, 811-832`; confirmed by
  grepping every call site of each — none inside `sync_models`, `models/models.py:598-647`,
  or the `/sync` route handler). `/sync` is a much more permissive raw upsert than every
  other write path, gated only by `get_admin_user` (§7 has the practical implications).

### 1.3 `GET /list` — paged, but **presets only**; `GET /base` — base models, unpaged, admin-only

`routers/models.py:131-213` (`@router.get('/list', ...)`, `PAGE_ITEM_COUNT = 30`):
`Models.search_models(...)`, which unconditionally filters
`stmt = stmt.filter(Model.base_model_id != None)` (`models/models.py:357`, **outside** the
`if filter:` block, so it is never skippable by any `view_option`/`tag`/`query` combination).
**`GET /list` never returns a base-model record (`base_model_id IS NULL`) at all** — only
presets. Response is `ModelAccessListResponse{items: list[ModelAccessResponse], total}`
(`models/models.py:157-172`), each item a full `ModelModel` plus `user: UserResponse | None`
and `write_access: bool`, with `meta.profile_image_url` stripped
(`routers/models.py:196-198`, served instead via `GET /model/profile/image`) and `params`
zeroed out (`{}`) for any caller without write access (`routers/models.py:199-206`).

`GET /export` (`routers/models.py:339-359`, `Models.get_models(...)`) has the **identical**
unconditional `Model.base_model_id != None` filter (`models/models.py:254`) — also presets
only.

**`GET /base`** (`routers/models.py:220-227`, admin-only via `get_admin_user`) is the one
endpoint that lists base-model DB rows: `Models.get_base_models(...)` →
`select(Model).filter(Model.base_model_id.is_(None))` (`models/models.py:323-335`),
unpaged, no access filtering (admin-only route). **There is no single endpoint that
returns every `Model` row (base + preset) together** — `ModelsTable.get_all_models` (the
unfiltered `select(Model)`, `models/models.py:236-249`) is only called internally by the
merge (`utils/models.py:167`) and by three unrelated admin routers
(`routers/users.py:1123`, `routers/groups.py:355`, `routers/knowledge.py:1719`), never
exposed as its own route. **For the plan**: to see whether `gideon-generator` already has a
record before a push, call `GET /base` (it will show up there once pushed, `base_model_id`
null); `GET /list`/`GET /export` will never show it. In practice this doesn't matter much
— `/sync`'s desired-state-replace semantics (§1.2) mean the render doesn't need to diff
against current state before pushing; it only needs to push the complete set it wants to
exist.

---

## 2. `meta.capabilities`

### 2.1 The full key set exposed by the product (admin/workspace Model editor)

`src/lib/components/workspace/Models/Capabilities.svelte:11-63` — the authoritative list of
capability keys the UI itself edits (this is the same `Capabilities.svelte` used from both
the workspace Model editor and, per §6, the admin Settings > Models base-model editor):

`vision`, `file_upload`, `file_context`, `web_search`, `image_generation`,
`code_interpreter`, `terminal`, `usage`, `citations`, `status_updates`, `memory`,
`builtin_tools`. (`file_context`'s checkbox is hidden in the UI when `file_upload` is off,
`Capabilities.svelte:75-81` — display logic only; the backend does not couple the two the
same way, see §2.3.)

### 2.2 Backend read sites, each with its default when the key is absent

All read as `(model.get('info', {}).get('meta', {}).get('capabilities') or {}).get(key,
default)` off the *selected chat model's own* `info.meta.capabilities` dict (never merged
from a base model onto a preset — see §4.2):

| Key | Default | Read site | What it gates |
|---|---|---|---|
| `builtin_tools` | `True` | `utils/middleware.py:2769-2773` | Whether *any* builtin tool is attached natively at all (§3). |
| `terminal` | `True` | `utils/middleware.py:2933` | Whether `terminal_id` resolves to attached terminal tools. |
| `file_context` | `True` | `utils/middleware.py:3047-3048`, `utils/tools.py:501` | Whether attached-file/knowledge content is auto-injected into context (`chat_completion_files_handler`); when explicitly `False`, gates whether the built-in `list_chat_files`/`query_chat_files`/`grep_chat_files`/`view_file` tools are offered instead (`utils/tools.py:595-602`, also requires `file_upload` true and `is_builtin_tool_enabled('files')`). **Corrected 2026-09-09 (slice-1 ticket 65):** the handler this key gates is the only step that turns *any* files item into the citation template's context — attached files, knowledge, **and the non-tool web search's loaded pages**, which `chat_web_search_handler` appends to `form_data['files']` as a `web_search` item carrying `docs` under `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL` (the `elif results.get('docs')` branch of its files append). With the key false the search's pages are fetched and dropped, the "Searched N sites" status having been emitted before the gate; GIDEON rendered it false on General's record through `v0.1.35` and renders it true from ticket 65's release. The handler also calls `generate_queries(type='retrieval')` unless every files item carries `context: 'full'`, which the bypass item does not, so that task-model call runs on every searched turn and its result goes unread by the docs branch of `get_sources_from_items`; `ENABLE_RETRIEVAL_QUERY_GENERATION=false` (`config.py`, default true) makes `routers/tasks.py`'s `generate_queries` raise the feature-disabled `HTTPException` inside the handler's own `try`, which passes. The cache the search handler fills (`request.state.cached_queries`) is written only under `ENABLE_QUERIES_CACHE`, off in GIDEON's render, so it never serves the retrieval call. Read from the pinned image's own source inside the running container, `v0.11.3`. |
| `code_interpreter` | `True` | `utils/tools.py:709-715` (native path); `utils/middleware.py:4629-4636` (**legacy** `function_calling` path only, gated additionally by `metadata.params.function_calling == 'legacy'`) | Whether `execute_code` is offered as a tool. |
| `citations` | `True` | `utils/middleware.py:5561-5563` | Whether citation sources are attached/rendered server-side; frontend independently re-checks the same default (`ContentRenderer.svelte:109,129`, `ResponseMessage.svelte:897`, default `true` there too). |
| `memory` | `True` | `utils/tools.py:658-663` (gates memory *tools*); `utils/memory.py:285-286` (`model_allows_memory`, gates `add_memory_context`'s context-injection) | Both the memory tool functions and the separate auto-injection of stored memories into the system prompt. See §2.4 for how this combines with the per-chat toggle. |
| `web_search` | `True` | `utils/tools.py:678-685` | Whether `search_web`/`fetch_url` are offered — combined with global `web.search.enable` config and the per-chat `features.web_search` flag (§2.4). |
| `image_generation` | `True` | `utils/tools.py:689-704` | Whether `generate_image`/`edit_image` are offered (two separate checks, one per global config: `image_generation.enable`, `images.edit.enable`). |
| `usage` | **absent → falsy (`None`)**, no default arg | `main.py:1174`: `if form_data.get('stream') and model_capabilities.get('usage'):` | Whether `stream_options.include_usage: True` is added to a streaming request. **This is the one key here that defaults OFF, not on.** |
| `file_upload` | not read with a default at all — only ever read via `get_model_capability('file_upload')` which defaults `True` (`utils/tools.py:534-536`) | `utils/tools.py:597` | Only used inside the `file_context`-off branch above (§ file_context row) to decide whether to offer the raw chat-file tools. **Not found gating anything else server-side** — e.g. it does not gate whether the chat UI accepts a file upload at all; that appears to be a pure frontend affordance not tied to this key (grepped `file_upload` across `backend/open_webui/`: the only non-permission hit is `utils/tools.py:597`, cited here; `config.py:1997`'s `'file_upload': USER_PERMISSIONS_CHAT_FILE_UPLOAD` is an unrelated user-*permission* key, not this model capability). |
| `vision`, `status_updates` | n/a — **not read anywhere in the Python backend at this tag.** Grepped `'vision'`/`"vision"`/`status_updates` across `backend/open_webui/`: zero hits outside the Pydantic-free `ModelMeta.capabilities: dict` passthrough. | frontend only: `vision` gates a client-side toast + attach-image warning (`Chat.svelte:3308-3327`, default `true` via `?? true`); `status_updates` gates whether status/progress events render in the message UI (`ResponseMessage.svelte:188,686`, default `true`) | **Display/UX-only at this tag** — pushing `vision: false` or `status_updates: false` changes nothing server-side; it only changes frontend rendering/warnings. |

Distinguishing behaviour-gating vs. display-only, per the ticket's ask:
- **Gates real backend behaviour** (tools attached, content injected, stream option sent):
  `builtin_tools`, `terminal`, `file_context`, `file_upload` (narrowly), `code_interpreter`,
  `memory`, `web_search`, `image_generation`, `usage`.
- **Server-side display gate but no tool/behaviour change**: `citations` (still
  double-checked client-side).
- **Display-only, no backend read at all**: `vision`, `status_updates`.

### 2.3 `file_upload`/`file_context` interaction — precise, not the UI's simplified pairing

The UI hides the `file_context` checkbox when `file_upload` is off (§2.1), suggesting a
dependency, but the **backend reads them independently with independent defaults**: with
neither key set (both default `True`), `file_context_enabled` is `True`
(`middleware.py:3047-3048`) so `chat_completion_files_handler` still runs, and
`utils/tools.py:595-602`'s combined condition (`file_upload AND NOT file_context AND
has_chat_files`) is `False` (since `file_context` is `True`) so the raw file-browsing tools
are never offered — i.e., with defaults, files get auto-injected into context and no
separate file tools are exposed. Setting `file_context: false` (leaving `file_upload`
default `True`) flips both: context auto-injection stops and the file tools switch on
(if `is_builtin_tool_enabled('files')`, default `True`, and files are actually attached).

**Added 2026-09-09 (slice-1 ticket 65):** General's record now carries `file_context: true`
with `file_upload: false` and `builtin_tools: false`, so the combined condition is false on
two counts (the file tools never appear) while the injection branch runs — the branch the
non-tool web search's pages need, since they arrive as a files item (§2.2's corrected row).

### 2.4 `meta.defaultFeatureIds` — the per-chat default-on mechanism (this is what answers the "web search default-off" ask)

There is no `meta.default_features` key; the mechanism is `meta.defaultFeatureIds: list[str]`
(a free-form array field on `ModelMeta` via its `extra='allow'`, since `ModelMeta` doesn't
declare it — `models/models.py:78-86`), consumed only on the **frontend**, only for a
newly-loaded chat/model selection:

`src/lib/components/chat/Chat.svelte:1059-1084`:

```js
if (model?.info?.meta?.defaultFeatureIds) {
    if (model.info?.meta?.capabilities?.['image_generation'] && $config?.features?.enable_image_generation && (...)) {
        imageGenerationEnabled = model.info.meta.defaultFeatureIds.includes('image_generation');
    }
    if (model.info?.meta?.capabilities?.['web_search'] && $config?.features?.enable_web_search && (...)) {
        webSearchEnabled = model.info.meta.defaultFeatureIds.includes('web_search');
    }
    if (model.info?.meta?.capabilities?.['code_interpreter'] && $config?.features?.enable_code_interpreter && (...)) {
        codeInterpreterEnabled = model.info.meta.defaultFeatureIds.includes('code_interpreter');
    }
}
```

`webSearchEnabled` (default `let webSearchEnabled = false;`, `Chat.svelte:321`) feeds a
reactive `webSearchActive` (`Chat.svelte:328-341`) which is what actually gets sent as
`features.web_search` in `getFeatures()` (`Chat.svelte:3364-3388`, `web_search:
webSearchActive`). **Net effect for the plan**: with `capabilities.web_search: true` (or
simply omitted — default `True`) and **no `meta.defaultFeatureIds` entry for
`'web_search'`**, the web-search toggle is offered (the button appears, gated by
`capabilities.web_search`) but starts **off** for every new chat — exactly the "available,
default-off per chat" the spec wants, with zero extra configuration needed beyond leaving
`defaultFeatureIds` unset. There is a separate always-on override, a personal per-user
setting `$settings.webSearch === 'always'` (`Chat.svelte:338-339`) — not a model-record
concern, out of scope here.

There is a second, unrelated `meta.defaultFeatureIds` consumer for **automations**
(headless/scheduled runs, not browser chats): `utils/automations.py:307-334`,
`_resolve_model_defaults`, which pre-enables `web_search`/`image_generation` (not
`code_interpreter`, explicitly excluded — comment: "does not work in headless backend
execution") for an automation's own generation turn when both `defaultFeatureIds` lists the
feature and the model capability and admin config allow it. Not relevant to a browser turn.

### 2.5 The "Memory" toggle — not a capability key, not a params field; a per-user personal setting layered under the model capability

`meta.capabilities.memory` (§2.2, default `True`) only gates whether memory tools/injection
are *offered at all* for a given model. Whether memory is actually *used* on a given chat
turn is `features.memory`, computed per-chat in `getFeatures()`
(`Chat.svelte:3364-3388`):

```js
if ($settings?.memory ?? $config?.features?.enable_memories ?? false) {
    features = { ...features, memory: true };
}
```

`$settings.memory` is the user's own personal setting (`Personalization.svelte:100`,
`enableMemory = $settings?.memory ?? $config?.features?.enable_memories ?? false`; saved via
`saveSettings({ memory: enableMemory })`, `Personalization.svelte:133`) — it is **not** part
of the model record. It falls back to the **admin's global** `ENABLE_MEMORIES` config
(`config.py:424`, `os.getenv('ENABLE_MEMORIES', 'True')` — **default `True`**, registered as
`'memories.enable'`, `config.py:2856`). Under `ENABLE_PERSISTENT_CONFIG=false` this env
default is frozen for the process, so **every user who has not explicitly turned their own
personal Memory setting off gets `features.memory: true` on every chat by default** —
confirming the spec's premise ("the frontend defaults it on").

Backend gating is `is_builtin_tool_enabled('memory') and features.get('memory') and
get_model_capability('memory') and await has_user_permission('memories')`
(`utils/tools.py:658-663`) — an AND of all four. **The one place GIDEON's model record can
force memory off deterministically, regardless of the admin's global `ENABLE_MEMORIES`
default or any individual user's personal setting, is `meta.capabilities.memory: false`** —
that alone makes the AND false for every user on this model, satisfying "must be switched
off explicitly" at the model-record level without touching a global config key GIDEON
otherwise leaves alone.

### 2.6 `models.default_metadata` — a separate, currently-dormant global-defaults mechanism

`utils/models.py:333-352`: an admin-configured global `default_metadata` dict (config key
`'models.default_metadata'`, env `DEFAULT_MODEL_METADATA`,
`config.py:1710-1715`, default `'{}'`) is merged onto **every** discovered model's `info`
during the same merge pass that applies DB records (§4), including models with **no** DB
record at all (`if info is None: model['info'] = {'meta': deepcopy(default_metadata)}`,
`utils/models.py:341-343`) — capabilities specifically merge as "global defaults as base,
per-model overrides win" (`{**value, **existing}`, `utils/models.py:349-350`). **Confirmed
dormant under GIDEON's plain env config**: `DEFAULT_MODEL_METADATA` unset →
`default_metadata == {}` → falsy → the whole merge block (`utils/models.py:337-352`) is
skipped. Flagged only because it is a second place capabilities can originate from, distinct
from the per-model record, should GIDEON's config ever set `DEFAULT_MODEL_METADATA`.

---

## 3. The built-in tools

### 3.1 `get_builtin_tools` — the full category list and its gating (`utils/tools.py:522-802`)

Signature and helpers (`utils/tools.py:522-543`):

```python
async def get_builtin_tools(
    request: Request, extra_params: dict, features: dict = None, model: dict = None, is_note_chat: bool = False
) -> dict[str, dict]:
    ...
    def get_model_capability(name: str, default: bool = True) -> bool:
        return (model.get('info', {}).get('meta', {}).get('capabilities') or {}).get(name, default)

    def is_builtin_tool_enabled(category: str, default: bool = True) -> bool:
        builtin_tools = model.get('info', {}).get('meta', {}).get('builtinTools', {})
        return builtin_tools.get(category, default)
```

`meta.builtinTools` (capital-T, camelCase — a **separate free-form dict from
`meta.capabilities`**) is a per-category on/off map, every category defaulting `True`. Full
category list, each gated by `is_builtin_tool_enabled(category)` plus what else, and the
functions each adds:

| Category | Line | Extra gates (all AND'd with the category flag) | Functions added |
|---|---|---|---|
| `time` | `tools.py:579-580` | none | `get_current_timestamp`, `calculate_timestamp` — **ungated except by the category flag itself** (default on) |
| `user_input` | `tools.py:582-583` | none (explicit `default=True`) | `ask_user` — likewise ungated by role/permission |
| `files` | `tools.py:595-602` | `capabilities.file_upload` AND NOT `capabilities.file_context` AND chat has files AND `has_user_chat_permission('file_upload')` | `list_chat_files`, `query_chat_files`, `grep_chat_files`, `view_file` |
| `knowledge` | `tools.py:608-643` | branches on `ENABLE_KB_EXEC` env and whether the model/chat has attached knowledge | `kb_exec`/`query_knowledge_files`/`view_note` (KB-exec branch) or `list_knowledge`/`search_knowledge_files`/`grep_knowledge_files`/`query_knowledge_files`/`view_file`/`view_knowledge_file`/`view_note` (attached-knowledge branch) or `list_knowledge_bases`/`search_knowledge_bases`/`query_knowledge_bases`/`grep_knowledge_files`/`search_knowledge_files`/`query_knowledge_files`/`view_knowledge_file` (no-knowledge branch) |
| `chats` | `tools.py:646-647` | none | `search_chats`, `view_chat` |
| `subagents` | `tools.py:649-655` | `config.get('subagents.enable')`, not an internal/direct request | `delegate_task`, `timer` |
| `memory` | `tools.py:658-675` | `features.get('memory')` AND `capabilities.memory` AND `has_user_permission('memories')` (§2.5) | `search_memories`, `list_memory_paths`, `read_memory_path`, `list_memories`, `update_memory`, `add_memory`, `replace_memory_content`, `delete_memory` |
| `web_search` | `tools.py:678-685` | `config.get('web.search.enable')` AND `capabilities.web_search` AND `features.get('web_search')` AND `has_user_permission('web_search')` | `search_web`, `fetch_url` |
| `image_generation` | `tools.py:689-704` | (per-op) `config.get('image_generation.enable')`/`config.get('images.edit.enable')` AND `capabilities.image_generation` AND `features.get('image_generation')` AND `has_user_permission('image_generation')` | `generate_image`, `edit_image` |
| `code_interpreter` | `tools.py:709-715` | `config.get('code_interpreter.enable')` AND `capabilities.code_interpreter` AND `features.get('code_interpreter')` AND `has_user_permission('code_interpreter')` | `execute_code` |
| `notes` | `tools.py:718-721` | `config.get('notes.enable')` AND `has_user_permission('notes')` (or `is_note_chat`) | `search_notes`, `view_note`, `write_note`, `replace_note_content` |
| `channels` | `tools.py:724-732` | `config.get('channels.enable')` AND `has_user_permission('channels')` | `search_channels`, `search_channel_messages`, `view_channel_thread`, `view_channel_message` |
| — (skills) | `tools.py:735-736` | `extra_params.__skill_ids__` truthy — not itself a `builtinTools` category | `view_skill` |
| `tasks` | `tools.py:740-741` | chat has a saved `chat_id` | `create_tasks`, `update_task` |
| `automations` | `tools.py:744-751` | `config.get('automations.enable')` AND `has_user_permission('automations')` | `create_automation`, `update_automation`, `list_automations`, `toggle_automation`, `delete_automation` |
| `calendar` | `tools.py:754-757` | `config.get('calendar.enable')` AND `has_user_permission('calendar')` | `search_calendar_events`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` |
| `notifications` | `tools.py:759-764` | `config.get('ui.enable_user_webhooks')` AND `has_user_permission('webhooks')` | `notify` |

`has_user_permission`/`has_user_chat_permission` (`tools.py:560-576`) always return `True`
for `user.get('role') == 'admin'`, else defer to `has_permission(user_id,
'features.<key>'/'chat.<key>', user.permissions)` — i.e. every category above is also
subject to the acting user's own role/permission, on top of the model-capability and
global-config gates in the table. **`time` and `user_input` are the two categories with no
permission/config/capability gate whatsoever** at this tag — confirmed by their entries
above having no extra condition — matching the tracker's finding that the built-in time
tools (and `ask_user`) are attached for every role once `use_builtin_tools` is true (§3.2).

`terminal` is **not** one of `get_builtin_tools`'s `builtinTools` categories — it is a
separate direct capability check in `middleware.py:2933` (§2.2), resolved outside this
function entirely, gated only by `terminal_id` being set on the request and
`capabilities.terminal` (default `True`).

### 3.2 Exactly when builtin tools are attached natively, and what's sent

`utils/middleware.py:2769-2773`:

```python
use_builtin_tools = is_note_chat or (
    bool(metadata.get('session_id'))
    and metadata.get('params', {}).get('function_calling') != 'legacy'
    and (model.get('info', {}).get('meta', {}).get('capabilities') or {}).get('builtin_tools', True)
)
```

Three conditions AND'd (besides the `is_note_chat` OR-branch, irrelevant to a plain chat):
**`session_id` present in `metadata`**, **`params.function_calling` is not the literal
string `'legacy'`**, and **`capabilities.builtin_tools` defaults `True`**. Attachment itself,
`middleware.py:2981-3037`:

```python
# Inject builtin tools for native function calling based on enabled features and model capability.
# Only inject when the request originates from the UI (identified by session_id).
# API callers don't expect hidden tools; they can explicitly request tools via tool_ids.
if use_builtin_tools:
    ...
    builtin_tools = await get_builtin_tools(request, {...}, features, model, is_note_chat=is_note_chat)
    for name, tool_dict in builtin_tools.items():
        if name not in tools_dict:
            tools_dict[name] = tool_dict

if tools_dict:
    metadata['tools'] = tools_dict
    if metadata.get('params', {}).get('function_calling') != 'legacy':
        # native
        form_data['tools'] = [
            {'type': 'function', 'function': tool.get('spec', {})} for tool in tools_dict.values()
        ]
        if inlet_filter_tools:
            form_data['tools'].extend(inlet_filter_tools)
    else:
        # legacy: prompt-based
        form_data, flags = await chat_completion_tools_handler(request, form_data, extra_params, user, models, tools_dict)
```

Confirmed:
- **Sent natively as `form_data['tools']`** — a plain OpenAI-style `tools` array, one entry
  per resolved tool's `spec`.
- **No `tool_choice` is ever set by this code.** Grepped `tool_choice` across
  `backend/open_webui/`: it appears only as (a) an Anthropic→OpenAI conversion mapping
  (`utils/anthropic.py:452-464`, irrelevant to a plain OpenAI-compatible connection), (b) a
  passthrough field name in an allowlist (`routers/openai.py:1174`), and (c) a field on the
  unrelated `/responses` form (`routers/openai.py:1838`). **Open WebUI never adds
  `tool_choice` to a Chat Completions payload itself** — whatever the vLLM engine defaults
  `tool_choice` to (the tracker's finding: `auto`, requiring
  `--enable-auto-tool-choice`/`--tool-call-parser`) is entirely the engine's own default,
  not something OWUI is asking for.
- **The comment at `middleware.py:2982-2983` is the authoritative statement of the
  `session_id` gate's intent**: "Only inject when the request originates from the UI
  (identified by session_id). API callers don't expect hidden tools; they can explicitly
  request tools via tool_ids."
- **Legacy `function_calling: "legacy"` does not route builtin tools through a prompt
  instead of native `tools` — it stops builtin tools from being added at all.** Because
  `use_builtin_tools`'s own condition already excludes `function_calling == 'legacy'`
  (`middleware.py:2771`), `get_builtin_tools()` is never even called when legacy is set;
  the `else` branch at `middleware.py:3037-3042` (`chat_completion_tools_handler`, prompt-
  based) only ever processes whatever is already in `tools_dict` from **other** sources
  (explicit `tool_ids`, MCP, terminal — populated earlier in the same function, not shown
  above), never the builtin category. So **once `capabilities.builtin_tools` is `false`,
  `function_calling: "legacy"` is moot for the builtin-tools question specifically** — both
  routes end at "no builtin tools," reached by different code paths (the capability check
  short-circuits `use_builtin_tools` directly; legacy short-circuits it too, for an
  unrelated reason).

### 3.3 Task calls never attach builtin tools — confirmed by an unreachable-code argument, not a flag check

`routers/tasks.py` has **zero occurrences of `session_id`** (grepped the whole file) and
every task route (`generate_title`, `generate_chat_tags`, `generate_search_query`, etc.)
calls `generate_chat_completion` directly after only
`process_pipeline_inlet_filter` (per `docs/research/owui-engine-connection.md`'s §3) — it
never enters `utils/middleware.py`'s `process_chat_payload`, the function containing
`use_builtin_tools`/`get_builtin_tools` at all. The `use_builtin_tools` code block
(§3.2) is therefore simply **unreached** for a task call, independent of what
`capabilities.builtin_tools` is set to — confirming the ticket's premise more strongly than
a flag check would: there is no flag to check because the code path doesn't run.

---

## 4. Merging a DB record onto a discovered model — `utils/models.py:69-451`, `get_all_models`

`custom_models = await Models.get_all_models()` (`utils/models.py:167`, the **unfiltered**
`select(Model)` — both base and preset rows, confirmed no `PersistentConfig`/`Config`
reference anywhere in `models/models.py`, so this table is untouched by
`ENABLE_PERSISTENT_CONFIG`).

### 4.1 A record with `base_model_id is None` (a base-model override, GIDEON's `gideon-generator` record)

`utils/models.py:178-206`:

```python
for custom_model in custom_models:
    if custom_model.base_model_id is None:
        model = base_model_lookup.get(custom_model.id)
        if model:
            if custom_model.is_active:
                model['name'] = custom_model.name
                model['info'] = custom_model.model_dump()
                schema = get_chat_variables_schema(...)
                if schema:
                    model['info'].setdefault('meta', {})['chat_variables_schema'] = schema
                action_ids = []
                filter_ids = []
                if 'info' in model:
                    if 'meta' in model['info']:
                        if ENABLE_PLUGINS:
                            action_ids.extend(model['info']['meta'].get('actionIds', []))
                            filter_ids.extend(model['info']['meta'].get('filterIds', []))
                    if 'params' in model['info']:
                        del model['info']['params']
                model['action_ids'] = action_ids
                model['filter_ids'] = filter_ids
            else:
                models = [m for m in models if m is not model]
```

Confirmed:
- **Only applies when the id was actually discovered** (`base_model_lookup.get(custom_model.id)`
  truthy — `base_model_lookup` is built from the engine's own `/v1/models` response, per
  the engine-connection note). **If the engine is down and `gideon-generator` isn't
  discovered, this whole branch is a no-op — the record itself is untouched in the DB, but
  it produces no list entry at all.** No synthetic/phantom entry is created for a base
  override the way there is for a preset (§4.2) — this is the asymmetry the ticket asked
  about.
- **Fields applied onto the discovered entry when `is_active` is true**: `name` (overwrites
  the discovered `name`), and the **entire** `info` dict is replaced with
  `custom_model.model_dump()` — i.e. the whole `ModelModel` (`id`, `user_id`,
  `base_model_id`, `name`, `params`, `meta`, `access_grants`, `is_active`, `created_at`,
  `updated_at`), **immediately followed by deleting `info['params']`**
  (`if 'params' in model['info']: del model['info']['params']`, line 200-201) — so **the
  record's `params` never reaches `GET /api/models`'s response at all**, even though it is
  stored and, per §4.3, still applied when a completion is actually generated. `meta`
  (hence `meta.capabilities`) **does** survive into `info` and is what `middleware.py`/
  `utils/tools.py` read for capability/builtin-tools gating (§2, §3).
- **`is_active: false` removes the discovered model from the candidate list entirely**
  (`models = [m for m in models if m is not model]`, line 206) — for every caller,
  admin included; this happens before any access filtering (§5) even runs.
- Action/filter ids only apply when `ENABLE_PLUGINS` is set (out of scope for a plain
  capability push).

### 4.2 A record with `base_model_id` set (a preset)

`utils/models.py:208-263`: only added `elif custom_model.is_active` and only if
`custom_model.id not in existing_ids` (i.e. it doesn't collide with an already-discovered
id). Unlike §4.1, **there is no check that the base model was actually found** —
`base_model = base_model_lookup.get(custom_model.base_model_id)` is looked up but its
absence doesn't skip the branch; `owned_by`/`connection_type`/`pipe`/`provider`/`loaded`
just fall back to `'openai'`/`None`/(absent)/(absent)/(absent) when the base isn't
discovered. **A preset is listed even when its base model is currently undiscovered** —
the asymmetry with §4.1 flagged above. A synthetic entry is built:
`{'id': custom_model.id, 'name', 'object':'model', 'created': custom_model.created_at,
'owned_by', 'connection_type', 'preset': True, ...}`, with `info = custom_model.model_dump()`
minus `params` (same deletion as §4.1, line 242-244).

**What a preset actually inherits at generation time — not from this merged dict.** The
merged preset entry carries **no `urlIdx`** (not present anywhere in the dict built at
lines 225-236). The real inheritance happens per-request, in
`routers/openai.py:1492-1500`, at the top of `POST /chat/completions`:

```python
model_id = form_data.get('model')
model_info = await Models.get_model_by_id(model_id)   # fresh DB read, not app-state MODELS
if model_info:
    if model_info.base_model_id:
        base_model_id = request.base_model_id if hasattr(request, 'base_model_id') else model_info.base_model_id
        payload['model'] = base_model_id
        model_id = base_model_id
    params = model_info.params.model_dump()
    if params:
        system = params.pop('system', None)
        payload = apply_model_params_to_body_openai(params, payload)
        ...
...
models = request.app.state.OPENAI_MODELS
model = models.get(model_id)   # looked up by the *base* id now
if model:
    idx = model['urlIdx']
```

So: the preset's `id` in the outgoing payload is **swapped to `base_model_id`**, and *then*
`urlIdx` (hence the connection: base URL, key, `api_config`) is resolved by looking up that
base id in `request.app.state.OPENAI_MODELS` — this is how a preset "inherits the
connection." **A preset's own `params` (sampling, system prompt) are applied via
`model_info.params`, read fresh from the DB by the *original* preset id, before the id gets
swapped** — so the preset's params do take effect on the actual request, confirming the
`del model['info']['params']` deletion in §4.1/§4.2 only strips `params` from what
`/api/models` *reports*, not from what's *used*. **Capabilities are not inherited from the
base at all** — every capability read site (§2.2, §3) reads
`model.get('info',{}).get('meta',{}).get('capabilities')` off whichever id is
`request.app.state.MODELS[form_data['model']]` at the point of the read (i.e. the
**originally-selected** id — the preset's own merged entry, carrying only the preset's own
`meta`, never the base's). A preset therefore needs its own `meta.capabilities` if it wants
different capability behaviour than leaving the key unset (default `True` for almost
everything, per §2.2) — there is no fallback to the base model's capabilities.

A second, separate access check (`utils/access_control.check_model_access`, imported
`routers/openai.py:42`, called `routers/openai.py:1510-1511`) runs at generation time too —
not traced further in this note; §5 covers the listing/visibility checks that are the
concern for the plan.

---

## 5. Access-control semantics for a model record

### 5.1 `access_control` is gone from the `Model`/`ModelModel`/`ModelForm` shape — `access_grants` is the only field

Grepped `access_control` across `backend/open_webui/`: the only hits touching models are (a)
`main.py:638-640`, a **boot-time migration** of legacy `arena_models` config JSON (a
*different* config blob, not the `Model` table) from `access_control` dict shape to
`access_grants` via `migrate_access_control` (`utils/access_control/__init__.py:179-`), and
(b) the same helper's own definition/docstring. **`ModelMeta`, `ModelModel`, and `ModelForm`
declare no `access_control` field at all** (`models/models.py:78-184`) — confirming and
extending the v0.11.0 note: `access_control` is not merely renamed, it is **not accepted**
by `/sync` (or `/create`/`/update`) as a field on a model record. Because `ModelModel`'s
`model_config` doesn't set `extra='forbid'` (only `from_attributes=True`,
`models/models.py:148-150`), pydantic v2's default `extra='ignore'` behaviour means a stray
`"access_control": ...` key in a payload record is **silently dropped**, not rejected — a
render bug that includes it would not surface as an error.

### 5.2 The grant shape and what "public" actually is

`normalize_access_grants` (`models/access_grants.py:150-188`) accepts
`{principal_type, principal_id, permission}` triples, `principal_type` one of `'user'`,
`'group'`, `'anyone'`; only `principal_type/permission/principal_id` are kept (id/
resource_type/resource_id/created_at supplied by the caller are discarded — §1.1).
`AccessGrants.set_access_grants` (`models/access_grants.py:443-478`) then stores each grant
with `resource_type`/`resource_id` **forced** to the call's own arguments (`'model'`,
`model.id`) — **not** whatever the payload's grant object said, further confirming those
fields are pure ceremony in the sync payload.

**`'anyone'` is a distinct, separate concept from public-to-every-verified-user — do not
conflate them.** `has_access` (`models/access_grants.py:562-620`) and
`get_accessible_resource_ids` (`models/access_grants.py:622-673`), the two functions that
gate every "can this verified user see this model" check, only ever match:
- `principal_type == 'user' AND principal_id == '*'` (public, to any *verified* user), or
- `principal_type == 'user' AND principal_id == <this user's id>` (direct grant), or
- `principal_type == 'group' AND principal_id IN <this user's groups>`.

**Neither function ever matches `principal_type == 'anyone'`.** That principal type is a
separate, explicitly-named "no-auth" concept (`has_anyone_access`,
`models/access_grants.py:540-556` — "Check for a no-auth anyone:\* grant. Callers must opt
in explicitly.") used for unauthenticated public-sharing features elsewhere in the app, not
for verified-user visibility. **The correct grant for "visible to every verified user" is
`{principal_type: 'user', principal_id: '*', permission: 'read'}`** — confirmed
independently by the product's own admin UI using exactly this shape for its public/private
toggle: `isPublicModel`/`toggleModelPrivacyHandler`
(`src/lib/components/admin/Settings/Models.svelte:114-118, 554-571`).

### 5.3 `/api/models` filtering — `get_filtered_models`, `utils/models.py:498-552`

Called from the main chat-facing listing endpoint, `GET /api/models`/`GET /api/v1/models`
(`main.py:874-890`) — **not** the same code path as the admin `/list`/`/base` endpoints
(§1.3), which use their own DB-level `_has_permission` filtering.

```python
async def get_filtered_models(models, user, db=None):
    if (
        user.role == 'user' or (user.role == 'admin' and not BYPASS_ADMIN_ACCESS_CONTROL)
    ) and not BYPASS_MODEL_ACCESS_CONTROL:
        ...
        for model in models:
            if model.get('arena'):
                ...  # arena-specific, not relevant here
            model_info = model_infos.get(model['id'])   # None unless a DB record produced model['info']
            if model_info:
                if (
                    (user.role == 'admin' and BYPASS_ADMIN_ACCESS_CONTROL)
                    or user.id == model_info.get('user_id')
                    or model['id'] in accessible_model_ids
                ):
                    filtered_models.append(model)
            elif user.role == 'admin':
                # No DB entry means no access control configured yet;
                # only admins can see unconfigured models.
                filtered_models.append(model)
        return filtered_models
    else:
        return models
```

`BYPASS_MODEL_ACCESS_CONTROL` (`env.py:817`, `os.getenv('BYPASS_MODEL_ACCESS_CONTROL',
'False')` — **default `False`**, not set by GIDEON's stated config) is the only thing that
skips filtering globally.

**The load-bearing, easy-to-miss finding**: with GIDEON's actual config
(`BYPASS_ADMIN_ACCESS_CONTROL=true`, `BYPASS_MODEL_ACCESS_CONTROL` unset/false):
- For an **admin**, the top condition (`admin and not BYPASS_ADMIN_ACCESS_CONTROL`) is
  `False`, so **filtering is skipped entirely** — `return models` unfiltered
  (line 551-552). Admins always see every discovered model, recorded or not.
- For a **`user`-role account**, the top condition is `True` regardless of
  `BYPASS_ADMIN_ACCESS_CONTROL` — filtering **does** run. And for a model with **no DB
  record at all** (`model_info` is `None`, i.e. `model.get('info')` was never set because
  no `custom_models` entry matched it in §4), the `elif user.role == 'admin':` branch is the
  *only* branch that appends it — **a `user`-role account never sees a discovered model that
  has no DB record**, full stop. The code's own comment states the intent plainly: "No DB
  entry means no access control configured yet; only admins can see unconfigured models."

**This means `gideon-generator` today (no record at all) is already admin-only-visible to
any `user`-role account**, not "public to everyone" as the naive "no access_control = null =
public" mental model from pre-`access_grants` Open WebUI would suggest. Pushing a record
with `access_grants: []` (empty — the "private" state per §5.2, and per the admin UI's own
toggle) produces **the identical outcome**: `model_info` is now populated (so the code takes
the `if model_info:` branch instead), but with no accessible grants and (per §1.2) `user_id`
forced to the syncing break-glass admin — a `user`-role account still fails every check
(`user.id == model_info.get('user_id')` false unless that literal admin account is the one
logged in; `accessible_model_ids` empty) and is excluded; an admin with
`BYPASS_ADMIN_ACCESS_CONTROL` still short-circuits past filtering entirely regardless.

**For the plan's two target shapes:**
- **"Keep the base model exactly as visible as it is today"**: an empty (or omitted)
  `access_grants` list reproduces today's outcome exactly (admin-visible, `user`-role
  invisible) — confirmed above to be the *same* outcome as having no record at all, not a
  restriction relative to it. GIDEON does not need any special grant to preserve current
  visibility.
- **"Restrict to admins only"**: this is, per the above, **already the shape from the
  previous bullet** — under this deployment's actual flags, "no grants" already *is*
  admin-only. There is no separate, more-restrictive shape to reach for a `user`-role
  account (short of disabling the model, `is_active: false`, which hides it from admins
  too). The only way this changes is if `BYPASS_MODEL_ACCESS_CONTROL` were ever turned on
  (not GIDEON's config) or if a `user`-role account were literally the model's own
  `user_id` owner (impossible here, since `/sync` always sets `user_id` to the calling
  admin).
- To make the model visible to `user`-role accounts too (not asked for now, but the
  opposite direction), push `access_grants: [{principal_type: 'user', principal_id: '*',
  permission: 'read', ...}]` (plus the placeholder `id`/`resource_type`/`resource_id`/
  `created_at` fields §1.1 requires) per §5.2.

**Correction (2026-09-04, `v0.1.9`)**: the empty grant list keeps the base model out of a
`user`-role account's *listing*, but it also makes every preset over it *unusable* for that
account — the chat route walks a preset's `base_model_id` chain at generation time and requires a
read grant (or ownership) at every hop, a base with no row passing only for an admin; see
`docs/research/owui-preset-system-prompt.md` §6. From `v0.1.9` the base model's record carries the
public-read grant and `meta.hidden`, the selector's client-side switch.

---

## 6. The admin UI's view — a base-model record is editable, and a hand-edit writes the same row

**Workspace > Models (`/workspace/models`, `src/lib/components/workspace/Models.svelte`)
never shows a base-model record at all** — it lists via `getWorkspaceModels` →
`GET /list`, which (§1.3) filters `base_model_id != None` unconditionally. `gideon-generator`
(no `base_model_id`) will not appear there regardless of whether it has a DB record.

**Admin Settings > Models (`src/lib/components/admin/Settings/Models.svelte`) is the page
that shows it.** It loads base models via `getBaseModels` → `GET /base`
(`Models.svelte:270`), lists them alongside presets, and opening a base-model row does
**not** navigate to the `/workspace/models/edit` route (that's reserved for presets,
`openModelHandler`, `Models.svelte:607-615`: `if (isPresetModel(model)) { goto(...); return; }
selectedModelId = model.id;`) — instead it opens the **same `ModelEditor` component**
in-page (`Models.svelte:1241-1253`, `<ModelEditor edit model={...} preset={false}
onSubmit={... upsertModelHandler(model) ...} />`), which embeds
`Capabilities.svelte` (§2.1) among its fields. **This is "the pencil on a base model"** —
confirmed present and editable, including capabilities, for a base-model record pushed by
`/sync`.

**A hand-edit there writes the exact row `/sync` owns.** `upsertModelHandler`
(`Models.svelte:468-497`): if the id is already a known base model or preset, it calls
`updateModelById` (→ `POST /model/update`, `routers/models.py:780-857`,
`Models.update_model_by_id`, `models/models.py:542-557` — a full-column update by `id`
against the same `Model` table row `/sync` writes); otherwise `createNewModel` (→
`POST /create`). Both target the identical `Model` row keyed by `id` that `sync_models`
upserts (§1.2). **So yes: a re-sync (desired-state, full column replace) will silently
revert any hand-edit made on this page** — including a hand-toggled capability, a
hand-edited `name`/`description`, or a hand-toggled privacy grant
(`toggleModelPrivacyHandler`, `Models.svelte:554-589`, which also writes through a
dedicated `updateModelAccessGrants` call to `POST /model/access/update`,
`routers/models.py:863-939` — again the same `AccessGrant` rows §1.2's `set_access_grants`
replaces on every sync).

---

## 7. What would reject or alter the record at this tag

Recapping and consolidating findings from §1 and §5 that bear directly on this:

- **No `id` grammar/length check on `/sync`** — `is_valid_model_id` (`routers/models.py:91-93`,
  non-empty and ≤256 chars) is never called from the sync path (confirmed by grep, §1.2).
  Pydantic's `id: str` alone accepts any string including `""`. (`/create` and `/import` do
  enforce it; `/sync` does not.)
- **No `base_model_id == id` self-guard on `/sync`** — `create_new_model` silently clears
  `base_model_id` when it equals `id` (`routers/models.py:270-272`); `sync_models` has no
  equivalent, so a self-referencing preset (`base_model_id == id`) would be stored as-is (a
  latent footgun, not GIDEON's concern for a plain base-model-plus-preset push where the
  ids differ).
- **`meta`, `params`, `is_active`, `updated_at`, `created_at` keys must all be present** in
  every record (§1.1) — `params: {}` and `meta: {}` are valid values, but the *keys* are
  required by `ModelModel`'s lack of defaults; omitting any of them 422s the whole request
  before any row is touched (`SyncModelsForm.models` is validated as a whole list up front).
- **No required `meta` sub-keys** — `profile_image_url` and `description` both default
  `None` on `ModelMeta` (`models/models.py:81-82`); neither is required. `profile_image_url`,
  if supplied, goes through `validate_profile_image_url` and is silently cleared to `None`
  (with a one-time warning log, not an error) if it fails validation
  (`models/models.py:88-102`) — not itself re-verified in this note (out of scope: the
  validator's own rules live in `utils/validate.py`, not read here).
- **`name` is required** (`str`, no default) — must be present, any non-`None` string
  (including `""`) passes pydantic; no further server-side check found.
- **`sync` does not validate `base_model_id` against any known model set** — nothing in
  `sync_models` (`models/models.py:598-647`) checks a preset's `base_model_id` resolves to
  anything. An unresolvable `base_model_id` is accepted at sync time and only surfaces later,
  at generation time, as a 404 from `routers/openai.py:1522-1528` (`models.get(model_id)` —
  the swapped base id — comes back `None`).
- **`access_grants` entries need the four placeholder fields** `id`/`resource_type`/
  `resource_id`/`created_at` even though all four are discarded server-side (§1.1, §5.2) —
  the single most actionable "gotcha" for the render script.

---

## 8. Live effect, the hand-edit route, and a phantom-entry check (added for a contract-test pass)

### 8.1 A synced record takes effect on the next `/api/models` fetch and the next chat turn — no restart, no TTL on the DB read

`ModelsTable.get_all_models` (`models/models.py:236-248`, called from the merge at
`utils/models.py:167`) is a **plain, undecorated `select(Model)`** — grepped
`models/models.py` for `@cached`/`@lru_cache`/any decorator import: none. Every call is a
live DB read; there is no TTL or in-process cache on the DB side of the merge (contrast the
engine-discovery side, which does have a 1s `@cached` TTL per
`docs/research/owui-engine-connection.md` §2 — that TTL only bounds *discovery*, not the
DB record read).

**`GET /api/models`/`GET /api/v1/models` (`main.py:874-877`) calls `get_all_models(request,
refresh=refresh, user=user)` unconditionally on every request** — not gated by any
"already populated" check — so every hit re-reads `Models.get_all_models()` from the DB and
re-runs the merge (§4). Per the engine-connection note's §2, the frontend calls this on
every app-shell load/navigation, not once per session.

**`POST /api/chat/completions`/`POST /api/v1/chat/completions` (`main.py:1085-1092`) only
calls `get_all_models` itself when the cache is empty**:

```python
async def chat_completion(request: Request, form_data: dict, user=Depends(get_verified_user)):
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)
    model_id = form_data.get('model', None)
    ...
    model = request.app.state.MODELS[model_id]
```

so a chat turn's model-selection step reads whatever `request.app.state.MODELS` currently
holds — populated by the **last** `get_all_models()` call in that worker process, from
*either* route. Since the frontend fetches `/api/models` on every navigation (before a user
can even open a chat and press send), in practice `request.app.state.MODELS` is fresh by
the time a chat turn is sent, with no restart needed. The write-back
(`utils/models.py:441-449`):

```python
models_dict = {model['id']: model for model in models}
if isinstance(request.app.state.MODELS, RedisDict):
    request.app.state.MODELS.set(models_dict)   # shared across workers/instances via Redis
else:
    request.app.state.MODELS = models_dict       # plain in-process dict, this worker only
```

**Caveat for a multi-worker deployment without Redis configured** (`RedisDict`,
`socket/utils.py:64-`, only instantiated when a Redis URL is configured — not re-verified
further here, out of scope): each worker process holds its own `request.app.state.MODELS`,
refreshed only by requests *that worker* handles. A record synced through one worker is
live for chat turns handled by *that* worker immediately (next `/api/models` hit routed to
it); another worker only picks it up on its own next `/api/models` hit. With Redis
configured, `RedisDict` makes this consistent across workers/instances immediately. Which
of these applies to GIDEON's deployment (worker count, Redis presence) was not checked in
this pass — flagged as the one open variable for a contract test that cares about
cross-worker timing.

`routers/openai.py:1493` (`model_info = await Models.get_model_by_id(model_id)`, §4.2) is
likewise a fresh, uncached per-request DB read at generation time, independent of
`request.app.state.MODELS` entirely — a preset's/base override's `params` are always current
as of the moment of the call, regardless of any `app.state.MODELS` staleness.

### 8.2 The admin UI's hand-edit route, exactly

`src/lib/apis/models/index.ts:290-304`, `updateModelById`:

```js
export const updateModelById = async (token: string, id: string, model: object) => {
    const { base_model_id, name, meta, params, access_grants, is_active } = model as any;
    const payload = { id, base_model_id, name, meta, params, access_grants, is_active };
    const res = await fetch(`${WEBUI_API_BASE_URL}/models/model/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', authorization: `Bearer ${token}` },
        body: JSON.stringify(payload)
    })
    ...
```

**`POST /api/v1/models/model/update`** (`WEBUI_API_BASE_URL` is `/api/v1`) — **`id` travels
in the JSON body, not as a query parameter or path segment** (only `toggleModelById`/
`getModelById` use `?id=...` query strings, `src/lib/apis/models/index.ts:220-224,
255-259`). Backend side, `routers/models.py:780-849`, `update_model_by_id`:

```python
@router.post('/model/update', response_model=ModelModel | None)
async def update_model_by_id(request: Request, form_data: ModelForm, user=Depends(get_verified_user), db=...):
    model = await Models.get_model_by_id(form_data.id, db=db)
    if not model:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.NOT_FOUND)
    ...
    model = await Models.update_model_by_id(form_data.id, ModelForm(**form_data.model_dump()), db=db)
```

Requires the row to **already exist** (401 if `Models.get_model_by_id` finds nothing) —
this is why the admin page's `upsertModelHandler`
(`src/lib/components/admin/Settings/Models.svelte:468-497`, §6) branches to `createNewModel`
(`POST /api/v1/models/create`) instead when the id isn't yet a known base model or preset.
`Models.update_model_by_id` (`models/models.py:542-557`) does a full-column
`update(Model).filter_by(id=id).values(**data)` against the **same** `Model` table row
`sync_models` upserts (§1.2, §6) — confirmed, this is the exact route/method/body shape a
contract test should use to hand-edit the base model's record before proving a re-sync
reverts it: `POST /api/v1/models/model/update`, JSON body `{id: "gideon-generator",
base_model_id: null, name, meta, params, access_grants, is_active}`, admin bearer token.

### 8.3 A base-model record whose id matches nothing discovered: no phantom entry, and in an engine-less stack the DB row is never even read for `/api/models`

Already established in §4.1 with citation (`utils/models.py:178-183`): the base-model-
override merge branch only executes `if model := base_model_lookup.get(custom_model.id)`
is truthy — when the id was actually discovered. **No `else` branch exists that adds a
synthetic/phantom entry for an undiscovered base-model id** — a base-model record for an id
nothing discovers produces **zero** entries in `/api/models`, full stop, unlike a preset
record (§4.2), which *is* added even when its `base_model_id` resolves to nothing.

**Sharper point for a contract stack with literally no engine/connection configured**:
`utils/models.py:108-113`:

```python
models = [model.copy() for model in base_models]
# If there are no models, return an empty list
if len(models) == 0:
    return []
```

If `base_models` (the discovery result, §4 preamble) is empty — no `OPENAI_API_CONFIGS`/
connection reachable at all, not merely this one id missing — `get_all_models` **returns
`[]` immediately, before line 167's `custom_models = await Models.get_all_models()` DB read
ever executes.** In that state a pushed base-model record is inert in the strongest possible
sense: it is not merely filtered out downstream, the DB is never even queried for it on that
call. (A pushed **preset** record would be similarly invisible in this state too, since the
same early return skips the entire `for custom_model in custom_models:` loop at
`utils/models.py:178` regardless of record type — the early return is unconditional on
having *any* discovered models, not specific to base-model overrides.) A contract test
against a stack with no engine should therefore expect `GET /api/models` to return `[]`
(module state, or an equivalent "no models" list gated only by whether `evaluation.arena.enable`
is on, §4 preamble) regardless of what has been synced, and should not treat that as
evidence the record itself was rejected or mishandled by `/sync` — `Models.sync_models`
itself (§1.2) still performs the DB write in that state; only the read-side merge produces
nothing to show for it until a base model is discovered.

---

## Unverified / out of this note's scope

- `validate_profile_image_url`'s own acceptance rules (`utils/validate.py`) — not read;
  irrelevant unless GIDEON's record sets `meta.profile_image_url`.
- `utils.access_control.check_model_access` (imported `routers/openai.py:42`, called at
  `routers/openai.py:1510-1511`) — a second, generation-time access check distinct from
  `utils/models.py`'s `check_model_access`/`get_filtered_models` (§5); not traced in this
  note since §5's listing-time checks are what govern whether GIDEON's record is *visible*
  at all, which was the object of the ticket's ask.
- Whether any other admin route or import/export path can smuggle an `access_control` key
  back onto a model row through a different mechanism than `/sync` — only `/sync`,
  `/create`, `/import`, `/model/update` were checked; all reject/ignore it the same way.

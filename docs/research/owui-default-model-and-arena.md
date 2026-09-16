---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — the default model for every user, and the evaluation arena model

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`, matches
`docs/research/owui-preset-system-prompt.md`'s pinned commit and `images.lock`'s `open-webui`
entry). Backend paths relative to `backend/open_webui/`; frontend paths to `src/`.
`docs.openwebui.com` was not consulted; every claim below is cited to a file and line in that
clone.

Companion notes, not repeated here except where this one extends them:
`docs/research/owui-model-record.md` (the record, sync form, capabilities, merge, visibility)
and `docs/research/owui-preset-system-prompt.md` §6 (the on-box finding that a preset needs its
base model to carry a public-read access grant, kept hidden only by `meta.hidden`). Context
assumed throughout: `ENABLE_PERSISTENT_CONFIG=false`, `BYPASS_ADMIN_ACCESS_CONTROL=true`,
`BYPASS_MODEL_ACCESS_CONTROL` unset, `ENABLE_OPENAI_API=true` with one vLLM connection serving
`gideon-generator`, and the two pushed model rows: `gideon-generator` (`meta.hidden: true`,
public-read grant, every capability off) and `gideon-general` (base_model_id =
`gideon-generator`, public-read grant, not hidden).

**Headline finding #1 (read before ticket 08's plan):** at this tag there is no
`PersistentConfig` class at all — grepped the full `backend/` tree, zero hits. The ticket's
framing (`PersistentConfig.__init__`/`.save()`) describes an older Open WebUI architecture that
this tag has already replaced with a per-key `Config` model (`models/config.py`), confirmed by
that file's own docstring: *"Replaces the old single-row JSON blob machinery with a simple
per-key model"* (`models/config.py:1-8`), and by a real migration named for the change,
`migrations/versions/3ff2c63645b8_reshape_config_to_per_key_rows.py`. §2 below answers the
ticket's actual question (does a panel edit survive a restart under
`ENABLE_PERSISTENT_CONFIG=false`) against this real class.

**Headline finding #2 (read before deciding the arena model's fate):** the default arena model
ships with **no `access_grants`** in its hardcoded `meta`, and `has_access`'s own documented
semantics are *"None or `[]` → private (owner-only, deny all)"* — so under GIDEON's flags the
default arena model is invisible and unreachable for every `user`-role account already, with no
render change needed, and reachable only by an admin. But the random arena pick that happens
*after* that gate performs **no further access check on the sub-model it draws** at the point it
draws it (`utils/middleware.py:2365-2401`) — the only access check that ends up covering the
drawn sub-model is a second, independent `check_model_access` inside the OpenAI router
(`routers/openai.py:1513`) that re-reads `form_data['model']` (by then already overwritten with
the drawn id) and is a **complete no-op for an admin regardless of `BYPASS_ADMIN_ACCESS_CONTROL`**
(`utils/access_control/__init__.py:365`, a bare `if user.role != 'admin':`). So today, an admin's
arena turn can silently draw the hidden `gideon-generator` row (no system prompt, no capability
gating) as "Model A"/"Model B" — see §4.3. And because GIDEON's own base model *must* carry a
public-read grant for the preset chain to work at all (per the preset-system-prompt note's §6),
the same grant that makes `gideon-general` usable would also make `gideon-generator` pass this
second check for a **`user`-role** account too, the day anyone gives the arena model itself a
public-read grant — see §4.4.

---

## 1. `DEFAULT_MODELS` — env, `/api/config`, and how a new chat picks its starting model(s)

### 1.1 The env var: a plain string, not JSON, not (any longer) a `PersistentConfig`

```python
DEFAULT_MODELS = os.getenv('DEFAULT_MODELS', None)
```
(`config.py:1659`.) No `JSONCodec.loads`, no comma-split at this point — a raw string or `None`.
It is folded into the startup defaults dict under the dotted key `ui.default_models`:
```python
'ui.default_models': DEFAULT_MODELS,
```
(`config.py:3095`, inside the `DEFAULT_CONFIG = {...}` dict at `config.py:2835-3234`), which is
handed to the `Config` class once, at import time:
```python
ENABLE_PERSISTENT_CONFIG = os.getenv('ENABLE_PERSISTENT_CONFIG', 'True').lower() == 'true'
...
Config.configure(
    defaults=DEFAULT_CONFIG,
    enable_persistent=ENABLE_PERSISTENT_CONFIG,
    enable_oauth_persistent=ENABLE_OAUTH_PERSISTENT_CONFIG,
)
```
(`config.py:3237-3244`.) `Config` (imported at `config.py:37` from `open_webui.models.config`) is
a SQLAlchemy model with per-key rows (`key TEXT PRIMARY KEY, value JSON`,
`models/config.py:101-112`), not the pre-rewrite `PersistentConfig` object; see §2 for its read/
write semantics under `ENABLE_PERSISTENT_CONFIG=false`.

### 1.2 `/api/config` — gated on being signed in at all, comma-split happens only client-side

`GET /api/config` (`main.py:2197-2198`, `get_app_config`) takes **no** `Depends(...)` — it
decodes whatever bearer token or cookie is present by hand (`main.py:2199-2224`) and leaves
`user = None` if none decodes. The block that carries `default_models` is conditioned on that:
```python
**(
    {
        'default_models': config.get('ui.default_models'),
        'default_pinned_models': config.get('ui.default_pinned_models'),
        ...
    }
    if user is not None
    else {}
),
```
(`main.py:2359-2362`, the `if user is not None` at `main.py:2382` closing this block — confirmed
by reading the full conditional wrapper) — **an anonymous caller's `/api/config` response has no
`default_models` key at all**; only a request that resolves to a real user gets it. `config` here
is `await Config.get_many(...)` over a fixed key list that explicitly includes `'ui.default_models'`
(`main.py:2260`). The value handed to the client is the **raw string** (or `null`) — no
comma-splitting happens on the backend; that is purely a frontend concern (§1.3).

### 1.3 New-chat model selection — `Chat.svelte`'s precedence chain, verified against two independent implementations of the same logic

`Chat.svelte` computes the "available" set once, excluding hidden models:
```js
const availableModels = $models
    .filter((m) => !(m?.info?.meta?.hidden ?? false))
    .map((m) => m.id);
const defaultModels = $config?.default_models ? $config?.default_models.split(',') : [];
```
(`src/lib/components/chat/Chat.svelte:2020-2024` — comma-split confirmed, client-side only.) A
second, earlier-declared copy of the same two computations exists as reusable helpers used by an
earlier reactive block (`getAvailableModelIds`/`getDefaultModelIds`,
`Chat.svelte:185-188`, feeding `normalizeSelectedModels`, `Chat.svelte:189-206`) — both read
`$models`/`$config.default_models` identically; cited together because they agree on every rule
below, which is the strongest evidence this is really the app's one algorithm and not a stray
duplicate.

**Precedence, on a brand-new chat (`chatIdProp === ''`), highest first** (`Chat.svelte:2041-2105`,
corroborated by the earlier `normalizeSelectedModels` helper at `Chat.svelte:189-206`):

1. **`?model=` / `?models=` query parameters** (`Chat.svelte:2041-2064`) — if present, they win
   outright, checked first, before folder/session/settings/default are even looked at. **This is
   the one branch whose own "unavailable" filter does *not* exclude hidden models** — it filters
   only by presence in the raw `$models` array:
   ```js
   selectedModels = selectedModels.filter((modelId) =>
       $models.map((m) => m.id).includes(modelId)
   );
   ```
   (`Chat.svelte:2062-2064`, contrast with `availableModels` at `Chat.svelte:2020-2022`, which is
   the hidden-excluding set used by every other branch, `Chat.svelte:2087, 2094`.) A single-id
   `?model=<id>` that **is** found in `$models` is accepted directly
   (`Chat.svelte:2049-2056`: `!$models.find(...)` → open the selector pre-filled instead; found →
   `selectedModels = urlModels`) — no hidden check anywhere in that branch. See §6 for what this
   means for `gideon-generator`.
2. **A folder's own `model_ids`**, if the chat was opened from a folder with pinned models
   (`Chat.svelte:2069`, `$selectedFolder?.data?.model_ids`).
3. **`sessionStorage.selectedModels`** — a same-tab carry-over the app itself writes every time
   `selectedModels` changes on an open chat (`Chat.svelte:927-932`) and reads once, consuming it
   (`Chat.svelte:2073`, `JSON.parse(...)`, then `sessionStorage.removeItem(...)`).
4. **`$settings?.models`** — the user's personal default, written by the model selector's own
   "Set as default" action:
   ```js
   settings.set({ ...$settings, models: selectedModels });
   await updateUserSettings(localStorage.token, { ui: $settings });
   ```
   (`src/lib/components/chat/ModelSelector.svelte:25-33`, wired to the selector's
   `onSetDefault={saveDefaultModel}` at `ModelSelector.svelte:81`) — this **is** the "Settings →
   Interface default-model setting" the ticket asked about; it is reached from the model-picker
   dropdown's own star/pin-style "set default" control, not a separate Settings page field.
5. **`$config.default_models`** (the admin's `DEFAULT_MODELS` env/panel value, comma-split) —
   used only if none of 2-4 produced anything (`Chat.svelte:2078-2081`).
6. **Final fallback**: if the chosen list is still empty after the branch above runs its own
   hidden/availability filter (`Chat.svelte:2087`), retry `defaultModels` filtered by
   `availableModels`, and if still empty, `selectedModels = [availableModels?.at(0) ?? '']`
   (`Chat.svelte:2090-2103`) — **the first non-hidden model the caller can see**, or a bare `['']`
   placeholder if literally nothing is visible (`Chat.svelte:2104-2105`).

Branches 2-5 (and the final fallback) all run through the same line,
`selectedModels = selectedModels.filter((modelId) => availableModels.includes(modelId))`
(`Chat.svelte:2087`) — **hidden models are excluded from every path except the raw query-param
one.** A model with `info.meta.hidden: true` is therefore excluded from the selector's own item
list too, independently — see §6.

**An already-saved chat is different**: reopening one restores `chatContent.models` verbatim with
no availability/hidden filter at all (`Chat.svelte:2287-2290`), falling back to
`normalizeSelectedModels` (the same chain as above) only if that list is empty/blank
(`Chat.svelte:2294-2300`) — a chat once pinned to a since-hidden or since-deleted model id keeps
referencing it by id until the user changes it.

---

## 2. The admin panel's edit of the default model, and what `ENABLE_PERSISTENT_CONFIG=false` does to it

### 2.1 The route and its body shape — matches the ticket's assumption exactly

```python
class ModelsConfigForm(BaseModel):
    DEFAULT_MODELS: str | None
    DEFAULT_PINNED_MODELS: str | None
    MODEL_ORDER_LIST: list[str] | None
    DEFAULT_MODEL_METADATA: dict | None = None
    DEFAULT_MODEL_PARAMS: dict | None = None

@router.post('/models', response_model=ModelsConfigForm)
async def set_models_config(request: Request, form_data: ModelsConfigForm, user=Depends(get_admin_user)):
    await Config.upsert(config_updates(form_data.model_dump(), MODELS_CONFIG_KEYS))
    ...
```
(`routers/configs.py:731-736, 751-753`), mounted at `app.include_router(configs.router,
prefix='/api/v1/configs', ...)` (`main.py:831`) — so the live route is
**`POST /api/v1/configs/models`**, exactly as the ticket named it. `MODELS_CONFIG_KEYS` maps
`DEFAULT_MODELS → 'ui.default_models'` and `MODEL_ORDER_LIST → 'ui.model_order_list'`
(`routers/configs.py:62-68`); `config_updates` is a plain dict comprehension keyed off that map
(`routers/configs.py:85-86`) — `DEFAULT_MODELS` really is a comma-separated `str | None` on the
wire, `MODEL_ORDER_LIST` really is a `list[str] | None`, confirming both halves of Q1/Q3's typing
question independent of the frontend.

### 2.2 `Config.upsert`, `Config.get`, and `Config.configure` — the exact lines that decide "does it survive a restart"

```python
DEFAULTS: ClassVar[dict[str, Any]] = {}
PERSISTENT_ENABLED: ClassVar[bool] = True
OAUTH_PERSISTENT_ENABLED: ClassVar[bool] = False

@classmethod
def configure(cls, *, defaults=None, enable_persistent=True, enable_oauth_persistent=False):
    cls.DEFAULTS = dict(defaults or {})
    cls.PERSISTENT_ENABLED = enable_persistent
    cls.OAUTH_PERSISTENT_ENABLED = enable_oauth_persistent

@classmethod
def persistent_enabled_for(cls, key: str) -> bool:
    if not cls.PERSISTENT_ENABLED:
        return False
    if key.startswith('oauth.') and not cls.OAUTH_PERSISTENT_ENABLED:
        return False
    return True
```
(`models/config.py:110-137`.) With GIDEON's `ENABLE_PERSISTENT_CONFIG=false`,
`Config.configure(..., enable_persistent=False, ...)` runs once at import
(`config.py:3237-3244`), so `PERSISTENT_ENABLED = False` for the life of the process, and
`persistent_enabled_for(key)` returns `False` for **every** non-`oauth.` key — `ui.default_models`
and `ui.model_order_list` included.

Read path:
```python
@staticmethod
async def get(key: str, default: Any = None) -> Any:
    if not Config.persistent_enabled_for(key):
        return Config.default_value(key, default)
    async with get_async_db() as db:
        row = await db.get(Config, key)
        return row.value if row else Config.default_value(key, default)
```
(`models/config.py:140-146`) — the DB is **never touched** for a non-persistent key; every read
returns `Config.DEFAULTS.get(key, default)`.

Write path:
```python
@staticmethod
async def upsert(updates: dict) -> None:
    persistent_updates = {}
    for key, value in updates.items():
        value = _json_value(value)
        if Config.persistent_enabled_for(key):
            persistent_updates[key] = value
        else:
            Config.DEFAULTS[key] = value
    if not persistent_updates:
        return
    async with get_async_db() as db:
        ...  # DB upsert, only for persistent_updates
```
(`models/config.py:197-220`.) For a non-persistent key, `upsert` writes straight into the
**in-process class dict** `Config.DEFAULTS[key] = value` and returns — `persistent_updates` stays
empty, so **the DB `config` table is never touched at all** for `ui.default_models`/
`ui.model_order_list` under GIDEON's flags.

**Net effect, precisely**: an admin's `POST /api/v1/configs/models` edit **does take effect
immediately**, backend-wide, for every subsequent `Config.get('ui.default_models')` call
(including the next `/api/config` response and the next `/api/models` sort) — because it mutates
the live `Config.DEFAULTS` dict that `Config.get` falls back to — but it is **never persisted to
the database**. On the next container start, module-level code reruns top to bottom:
`config.py:1659` re-reads `DEFAULT_MODELS` from the environment, `config.py:2835-3234` rebuilds
`DEFAULT_CONFIG` from that fresh value, and `Config.configure(defaults=DEFAULT_CONFIG, ...)`
(`config.py:3240-3244`) **replaces** `Config.DEFAULTS` wholesale — the admin's in-memory edit is
gone, and the value reverts to whatever `DEFAULT_MODELS` is rendered as in the environment. This
confirms the ticket's expectation exactly, against the actual (rewritten) class rather than the
`PersistentConfig` the ticket named.

---

## 3. `MODEL_ORDER_LIST` — sort order, applied uniformly to admin and user alike

```python
model_order_list = await Config.get('ui.model_order_list')
if model_order_list:
    model_order_dict = {model_id: i for i, model_id in enumerate(model_order_list)}
    models.sort(
        key=lambda model: (
            model_order_dict.get(model.get('id', ''), float('inf')),
            (model.get('name', '') or ''),
        )
    )
```
(`main.py:905-913`, inside `GET /api/models`/`GET /api/v1/models`, `main.py:875-877`.) This sort
runs **after** `get_filtered_models` has already cut the list down to what the caller can see
(`models = await get_filtered_models(models, user)`, `main.py:889`) — so it reorders whatever
survived filtering, for admin and `user`-role callers alike; there is no role branch anywhere in
this block. A model absent from the list sorts last (`float('inf')`), then by name.

The frontend's `ModelSelector.svelte` builds its item list straight from `$models` in whatever
order `/api/models` returned (`items={$models.map((model) => ({ value: model.id, ... }))}`,
`ModelSelector.svelte:69-73`) with no independent client-side re-sort by id found in
`Selector.svelte` beyond its own pinned-models-first/search-relevance ordering (not re-verified
line-by-line here, out of scope for this question) — so `MODEL_ORDER_LIST` is what an admin (who
can see more than one model) sees the selector's base ordering follow. **With only one model
visible to a `user`-role account (`gideon-general`, per GIDEON's shape), `MODEL_ORDER_LIST` has
no observable effect for that account** — there's nothing to reorder.

---

## 4. `ENABLE_EVALUATION_ARENA_MODELS` / `EVALUATION_ARENA_MODELS` — defaults, visibility, the random-pick mechanism, and the feedback/leaderboard feature

### 4.1 Config lines and the default arena model's shape

```python
ENABLE_EVALUATION_ARENA_MODELS = os.getenv('ENABLE_EVALUATION_ARENA_MODELS', 'True').lower() == 'true'
try:
    evaluation_arena_models = JSONCodec.loads(os.getenv('EVALUATION_ARENA_MODELS', '[]'))
    if not isinstance(evaluation_arena_models, list) or not all(
        isinstance(model, dict) for model in evaluation_arena_models
    ):
        raise ValueError('EVALUATION_ARENA_MODELS must be a JSON list of objects')
except Exception as e:
    log.exception(...)
    evaluation_arena_models = []
EVALUATION_ARENA_MODELS = evaluation_arena_models

DEFAULT_ARENA_MODEL = {
    'id': 'arena-model',
    'name': 'Arena Model',
    'meta': {
        'profile_image_url': '/favicon.png',
        'description': 'Submit your questions to anonymous AI chatbots and vote on the best response.',
        'model_ids': None,
    },
}
```
(`config.py:2068-2089`.) Default: **arena feature on** (`True` string default), **no curated
arena models configured** (`EVALUATION_ARENA_MODELS` defaults to `[]`). **`DEFAULT_ARENA_MODEL`'s
`meta` carries no `access_grants` key at all** — this is the fact behind Headline finding #2.
Both flags are exposed under `evaluation.arena.enable` / `evaluation.arena.models`
(`config.py:3126-3127`) through the same `Config` class as §2 — an admin's toggle in
Settings → Evaluations is subject to the identical `ENABLE_PERSISTENT_CONFIG=false` behavior
already proven in §2.2 (not re-derived here; the mechanism is generic to every `Config` key).

### 4.2 Where the arena entry is appended, and how access to it is decided

`get_all_models` (`utils/models.py:69-`) appends arena entries to the base model list after
building it, gated on the config flag:
```python
if config.get('evaluation.arena.enable'):
    arena_models = []
    arena_config = config.get('evaluation.arena.models') or []
    if len(arena_config) > 0:
        arena_models = [{'id': model['id'], 'name': model['name'],
                          'info': {'meta': model['meta']}, ...,
                          'owned_by': 'arena', 'arena': True} for model in arena_config]
    else:
        arena_models = [{'id': DEFAULT_ARENA_MODEL['id'], 'name': DEFAULT_ARENA_MODEL['name'],
                          'info': {'meta': DEFAULT_ARENA_MODEL['meta']}, ...,
                          'owned_by': 'arena', 'arena': True}]
    models = models + arena_models
```
(`utils/models.py:115-149`.) This runs **once, for the whole process** (feeding
`request.app.state.MODELS`, `utils/models.py:441-448`) — it is not itself per-user; access is
decided later, in two independent places that both key on `meta.access_grants` (the ticket's
`access_control` name does not exist for this shape — that field name belongs to the `models`
table's DB row, not to a config-driven arena entry):

- **`GET /api/models`'s filtering** (`get_filtered_models`, `utils/models.py:498-546`): the whole
  filtering block is skipped (models returned as-is) unless
  `(user.role == 'user' or (admin and not BYPASS_ADMIN_ACCESS_CONTROL)) and not
  BYPASS_MODEL_ACCESS_CONTROL` (`utils/models.py:499-500`). Under GIDEON's flags
  (`BYPASS_ADMIN_ACCESS_CONTROL=true`, `BYPASS_MODEL_ACCESS_CONTROL` unset → `False` by its own
  default, `env.py:817`): for an **admin** the condition is `False`, so `get_filtered_models`
  returns the untouched list — **an admin always sees the arena entry** (when the feature is on)
  regardless of its grants. For a **`user`-role** account the condition is `True`, and the arena
  branch of the loop runs:
  ```python
  if model.get('arena'):
      meta = model.get('info', {}).get('meta', {})
      access_grants = meta.get('access_grants', [])
      if await has_access(user.id, permission='read', access_grants=access_grants, user_group_ids=user_group_ids):
          filtered_models.append(model)
      continue
  ```
  (`utils/models.py:521-530`.) `has_access`'s own docstring states the semantics plainly:
  *"None or `[]` → private (owner-only, deny all)"* (`utils/access_control/__init__.py:113-121`,
  the check itself at `:124-125`: `if not access_grants: return False`). **With the default arena
  model's empty `meta.access_grants`, `has_access` returns `False` for every `user`-role account
  — the arena entry never appears in a `user`-role account's `/api/models` response, and
  therefore never appears in the selector.**
- **The direct-turn check**, `main.py:chat_completion` (`main.py:1120-1136`), runs
  `check_model_access` (the `utils/models.py:454-495` variant, imported at `main.py:246-247`)
  whenever `not BYPASS_MODEL_ACCESS_CONTROL and (user.role != 'admin' or not
  BYPASS_ADMIN_ACCESS_CONTROL)` (`main.py:1121`) — for GIDEON's admin this is `False` (skipped
  entirely); for a `user`-role account it always runs, and dispatches on `model.get('arena')`:
  ```python
  if model.get('arena'):
      meta = model.get('info', {}).get('meta', {})
      access_grants = meta.get('access_grants', [])
      if not await has_access(user.id, permission='read', access_grants=access_grants, db=db):
          raise Exception('Model not found')
  ```
  (`utils/models.py:454-461`.) Same empty-grants → `False` outcome — a `user`-role account
  POSTing `model: "arena-model"` directly gets **"Model not found"**, matching the `/api/models`
  omission exactly.

**Answer to "is the default arena model visible to users or only admins": only admins, under
GIDEON's current flags and the default (curated-list-empty) arena shape**, purely because the
hardcoded `DEFAULT_ARENA_MODEL['meta']` carries no `access_grants` — not because of any
GIDEON-specific setting. A curated `EVALUATION_ARENA_MODELS` entry *can* carry its own
`access_grants` (the admin UI's `ArenaModelModal.svelte` writes `access_grants: accessGrants`
into the pushed shape, `src/lib/components/admin/Settings/Evaluations/ArenaModelModal.svelte:
87-89`, and reads it back the same way, `:114-116`) — so an admin could deliberately open the
arena to `user`-role accounts by adding a curated entry with a public-read grant; nothing in the
default shape does this.

### 4.3 What an arena turn actually does — the random pick has no access check of its own

```python
if model.get('owned_by') == 'arena':
    arena_model_ids = model.get('info', {}).get('meta', {}).get('model_ids')
    arena_filter_mode = model.get('info', {}).get('meta', {}).get('filter_mode')
    if arena_model_ids and arena_filter_mode == 'exclude':
        arena_model_ids = [
            available_model['id']
            for available_model in request.app.state.MODELS.values()
            if available_model.get('owned_by') != 'arena' and available_model['id'] not in arena_model_ids
        ]
    if isinstance(arena_model_ids, list) and arena_model_ids:
        selected_model_id = random.choice(arena_model_ids)
    else:
        arena_model_ids = [
            available_model['id']
            for available_model in request.app.state.MODELS.values()
            if available_model.get('owned_by') != 'arena'
        ]
        selected_model_id = random.choice(arena_model_ids)

    selected_model = request.app.state.MODELS.get(selected_model_id)
    if selected_model:
        model = selected_model
        form_data['model'] = selected_model_id
        metadata['selected_model_id'] = selected_model_id
```
(`utils/middleware.py:2374-2401`, inside `process_chat_payload`, which runs **after** the outer
`check_model_access` call on the arena entry itself, §4.2.) With `meta.model_ids: None` (the
default arena model's exact shape), the `else` branch fires: the pool is **every non-arena entry
currently in `request.app.state.MODELS`**, the process-wide model cache — for GIDEON's shape that
is exactly `{gideon-generator, gideon-general}`, since that dict is built by `get_all_models`
*before* any per-user access filtering is applied (`get_filtered_models` is called only inside
specific routes such as `GET /api/models`, never when populating `request.app.state.MODELS`
itself, `utils/models.py:441-448`) — **the hidden base model is in the pool**; nothing in this
function reads `meta.hidden` (grepped `utils/middleware.py` and `utils/models.py` for `hidden`:
zero hits in either). `random.choice` then picks one of the two with equal probability, swaps it
in as `model`, and rewrites `form_data['model']` to its id — this is what the frontend later
labels "Model A"/"Model B" until the user's rating reveals `selected_model_id`
(`Chat.svelte:2799-2802`, `if (selected_model_id) { message.selectedModelId = ...; message.arena
= true; }`).

**The only access check that can still catch this swap** is a second, independently-named
`check_model_access` inside the OpenAI router, reached once `form_data['model']` has already been
overwritten:
```python
model_id = form_data.get('model')
model_info = await Models.get_model_by_id(model_id)
...
await check_model_access(user, model_info, bypass_filter)
```
(`routers/openai.py:1490-1513`, importing `check_model_access` from `open_webui.utils.
access_control` at `routers/openai.py:42` — a **different function** from the one used in §4.2's
`main.py`/`utils/models.py` checks, despite the identical name). That function's body:
```python
async def check_model_access(user, model_info, bypass_filter=False):
    if bypass_filter:
        return
    if model_info:
        if user.role != 'admin':
            ...  # AccessGrants.has_access + has_base_model_access walk
    else:
        if user.role != 'admin':
            raise HTTPException(403, 'Model not found')
```
(`utils/access_control/__init__.py:345-384`.) **The `user.role != 'admin'` guard is
unconditional — it does not consult `BYPASS_ADMIN_ACCESS_CONTROL` at all.** For an admin, this
whole re-check is therefore a no-op regardless of the flag, for whichever sub-model the random
draw landed on. **Conclusion: under GIDEON's shape today, an admin's arena turn can draw the
hidden `gideon-generator` row with zero access checks anywhere in the chain** (the outer
per-arena-entry check at §4.2 is itself skipped for admin via `BYPASS_ADMIN_ACCESS_CONTROL`, and
this inner per-drawn-model check is skipped for admin unconditionally) — the model that answers
is indistinguishable in the UI from `gideon-general` until the reveal, and its output is the raw
engine with none of the preset's `params.system`/capability restrictions applied (per the
preset-system-prompt note's §1, that prompt lives only on `gideon-general`'s own record, applied
by id — `routers/openai.py:1500-1511` — and `gideon-generator` has none by GIDEON's own manifest).

### 4.4 The same public-read grant that makes the preset chain work would also open this door to `user`-role accounts

The preset-system-prompt note's §6 established, from an on-box failure and this same source,
that a preset is unusable by a `user`-role account **unless its base model also carries a
public-read grant** (`utils/access_control/__init__.py:301-338`, `has_base_model_access`, which
denies an unreadable base at any hop) — so GIDEON's working configuration necessarily gives
`gideon-generator` a public-read `access_grants` entry, with only `meta.hidden: true` (a purely
client-side selector filter, §6 below) keeping it out of the UI. That same grant is exactly what
`utils/access_control.check_model_access`'s `AccessGrants.has_access` check (§4.3, the
`user.role != 'admin'` branch) tests. **The day the arena entry itself is given a public-read
`access_grants` (the default has none, §4.2, so this is not live today) a `user`-role account's
arena turn drawing `gideon-generator` would pass this re-check too** — because the base model's
own access, not the arena entry's, is what gates the drawn sub-model, and GIDEON's base model is
already necessarily public-read. This is a real, config-shape-specific trap for whoever decides
"open the arena to everyone" later, not merely a generic warning; recording it here since ticket
08 is the one deciding the arena model's fate.

### 4.5 Feedback and the leaderboard

`POST /api/v1/evaluations/feedback` (`routers/evaluations.py:408-429`, `Depends(get_verified_user)`
— any signed-in user, not admin-only) stores a `FeedbackModel` whose `data` carries `model_id`
(the model the rating favored) and `sibling_model_ids` (the other side(s) compared)
(`models/feedbacks.py:79-80`). `GET /api/v1/evaluations/leaderboard`
(`routers/evaluations.py:216-230`, `Depends(get_admin_user)` — **admin-only**) pulls
`Feedbacks.get_feedbacks_for_leaderboard`, runs an Elo-style computation
(`_calculate_elo`, not traced line-by-line here — out of scope for this question) and returns a
sorted `LeaderboardEntry` list keyed by `model_id`; `GET /leaderboard/{model_id}/history`
(`routers/evaluations.py:251-260`) is also admin-only. Ordinary feedback creation/reading/deletion
(`/feedback`, `/feedbacks/user`) is `get_verified_user`; the model-id list and the aggregate
listing/export/leaderboard endpoints are all `get_admin_user`
(`routers/evaluations.py:216, 251, 268, 310, 316, 321, 338, 383`).

### 4.6 `ENABLE_EVALUATION_ARENA_MODELS=false` — removes the arena model everywhere, but the admin UI stays reachable

Because the whole arena-append block in `get_all_models` is gated on
`config.get('evaluation.arena.enable')` (`utils/models.py:116`) with no role branch inside that
`if`, setting the flag off removes the arena entry from `request.app.state.MODELS` for **every**
caller, admin included — there is nothing left for any of §4.2's checks to even find, and no
"Model A"/"Model B" pairing can occur for anyone.

The **Admin → Evaluations top-level page** (leaderboard + feedback tabs) is a separate, always-
visible admin nav item — `src/routes/(app)/admin/+layout.svelte:80-85` renders the
`/admin/evaluations` tab unconditionally, with no `$config.features...` guard of any kind (unlike
the `/admin/functions` tab two lines below it, which *is* wrapped in `{#if $config?.features?.
enable_plugins}`, `+layout.svelte:86-93` — direct contrast confirming the Evaluations tab has no
such gate). **The Settings → Evaluations panel's own toggle row is likewise always rendered**
(`src/lib/components/admin/Settings/Evaluations.svelte:113-121`, inside an
`{#if evaluationConfig !== null}` that only depends on the admin-only `getConfig` call having
succeeded, `Evaluations.svelte:79-86`) — turning the flag off only hides the **curated-model-list
sub-section** beneath it (`{#if evaluationConfig.ENABLE_EVALUATION_ARENA_MODELS}`,
`Evaluations.svelte:126`), so an admin can always find the toggle again to turn it back on.
**Answer: no, `ENABLE_EVALUATION_ARENA_MODELS=false` does not hide the Admin → Evaluations
leaderboard page or the Settings toggle for admins — it only removes the arena model from the
model list (for everyone) and hides the per-model arena config rows in Settings.**

---

## 5. Anything else that could hand a `user`-role account a model other than `gideon-general`

- **A base model with no DB row at all**: `has_base_model_access` treats a base id with no
  matching row as admin-only (`utils/access_control/__init__.py:322-323`, `if base_model_info is
  None: return user_role == 'admin'`) and `get_filtered_models` does the same for a model whose
  own row is missing (`utils/models.py:472-474`, `elif user.role == 'admin': filtered_models.
  append(model)`) — not applicable to GIDEON's shape since both `gideon-generator` and
  `gideon-general` have real rows, but a mechanism worth knowing if a future ticket ever pushes a
  preset over an unregistered base.
- **Task models** (title/tag/search-query generation): per the preset-system-prompt note's §1.5,
  these resolve to `TASK_MODEL_EXTERNAL` (GIDEON: `gideon-generator`'s own id) and are invoked
  server-side by id — they are never offered as a selectable chat model in the UI; not a distinct
  exposure surface.
- **Pipeline/manifold Functions** (`get_function_models`, `functions.py:71-`): gated on
  `ENABLE_PLUGINS` (`env.py:1165`, defaults `True`) and require an **active `pipe`-type Function
  row** in the database (`Functions.get_functions_by_type('pipe', active_only=True)`,
  `functions.py:77`) — each active pipe (or each of a manifold's `pipes()` sub-entries) becomes
  its own selectable model id. GIDEON's stated manifest pushes model records only, via
  `/api/v1/models/sync`; no evidence here of a Function being installed by that manifest — this
  is a real, generic surface (a Function admin-installs later would add models silently) but not
  something present in the setup described.
- **The `?model=`/`?models=` query-param path (§1.3, item 1) is itself the most direct answer**:
  it is the one place a hidden model becomes selectable by id for **any** signed-in account
  (admin or `user`-role alike) that can already see it in `$models` — see §6.

---

## 6. Hidden-model visibility — is the exclusion role-specific, and can it still be reached?

### 6.1 The selector's own filter — off by default for everyone, including admin, in the chat's own picker

```svelte
export let includeHidden = false;
...
.filter((item) => includeHidden || !(item.model?.info?.meta?.hidden ?? false));
```
(`src/lib/components/chat/ModelSelector/Selector.svelte:64, 370, 817` — the same predicate at
both the list-building site and the keyboard-navigation site.) `includeHidden` is a prop with no
default override anywhere in the chat page's own picker: `ModelSelector.svelte`'s `<Selector
... />` invocation (`ModelSelector.svelte:66-84`) never passes `includeHidden`, so it stays
`false` — **for every role**, admin included, the chat composer's model dropdown excludes a
hidden model. The **only** place in the whole frontend that ever passes `includeHidden={true}` is
the Workspace → Models editor's base-model picker, and only for an admin:
```svelte
includeHidden={$user?.role === 'admin'}
```
(`src/lib/components/workspace/Models/ModelEditor.svelte:717`) — a different UI surface (choosing
what to build a new preset *on top of*), not the chat page. So: **the hidden filter in the
chat-facing selector applies identically regardless of role; the one role-specific exception
(admin sees hidden bases) exists only in the admin model-editing screen, not in chat.**

`Chat.svelte`'s own `availableModels`/`normalizeSelectedModels` hidden filter (§1.3) likewise has
no role branch anywhere in that code (`Chat.svelte:2020-2022, 185-188`) — the new-chat
default/fallback logic treats an admin's own available set the same way, excluding hidden models
from what a *default* can resolve to.

### 6.2 What still gets past the filter for anyone, admin or not

Two paths bypass the selector/availability hidden-filter, and neither checks role:

1. **`?model=<hidden-id>`** (§1.3 item 1): the query-param branch's own filter checks only
   membership in the raw `$models` array (`Chat.svelte:2062-2064`), not `meta.hidden`. Since
   `/api/models` itself never reads or strips `hidden` server-side (grepped `utils/models.py`,
   `main.py` for a `hidden` read: none — the flag is purely additive metadata the frontend
   chooses to act on), a hidden-but-otherwise-readable model (i.e. one with a real access grant,
   like GIDEON's `gideon-generator` per the preset-system-prompt note's §6) **is** present in
   `$models` for anyone who can read it — currently that includes `user`-role accounts too, since
   the public-read grant on `gideon-generator` is unconditional on role. So **`?model=
   gideon-generator` (or `?models=gideon-generator`) selects the raw hidden base model directly,
   for an admin and a `user`-role account alike** — confirmed by source; not on-box-verified in
   this pass.
2. **The arena random pick** (§4.3): draws from `request.app.state.MODELS.values()` with no
   `meta.hidden` check at all — the hidden base model is a candidate whenever it exists in that
   global dict, independent of who is running the arena turn. Whether a given *role* can reach
   that arena turn at all is the separate access question answered in §4.2-§4.4: today, only an
   admin can start one (the default arena model's empty grants block `user`-role accounts
   entirely); if that ever changes, §4.4's point applies — the draw itself still performs no
   hidden check.

**Net: the exclusion of a hidden model is a UI convenience the selector and the new-chat default
logic apply uniformly to every role, not a security boundary — it is never enforced by any
backend check on `hidden`, so both the query-param path and the arena draw can hand any caller
(admin or `user`-role, whoever can otherwise read the row) the raw hidden model, exactly as the
preset-system-prompt note's §6 already found for a hand-crafted API request.**

---

## Unverified / out of this note's scope

- `Selector.svelte`'s own item-ordering logic beyond the hidden filter (pinned-first, search
  relevance) — read only far enough to confirm it does not independently re-sort by
  `MODEL_ORDER_LIST` or by id in a way that would override §3's server-side sort; not traced
  line-by-line.
- `_calculate_elo` and `_get_top_tags` (`routers/evaluations.py`) — read only enough to confirm
  the leaderboard is Elo-based and keyed by `model_id`/`sibling_model_ids`; the rating-update
  formula itself was not verified.
- Whether GIDEON's actual render currently sets `EVALUATION_ARENA_MODELS` to a non-empty curated
  list with its own `access_grants` — a render/manifest question, not a source-behaviour
  question; not checked against GIDEON's own compose/manifest files in this pass (the note
  assumes the tag's own default, empty-list shape, per the ticket's framing).
- On-box confirmation of §6.2's two bypass claims (`?model=gideon-generator` for a `user`-role
  session; an admin's arena turn actually drawing `gideon-generator` in practice) — both are
  derived from reading the source precisely, not observed on a running GIDEON deployment in this
  pass.
- The exact default of `ENABLE_PLUGINS`'s interaction with any Function GIDEON might install
  later (§5) — confirmed only the upstream default (`True`) and the gating mechanism; GIDEON's
  own manifest was not inspected for an actual Function push.

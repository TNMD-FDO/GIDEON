---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — the global outlet Filter Function: sync, selection, loading, and the outlet hook

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`, matches
`images.lock`). Backend paths are relative to `backend/open_webui/`; frontend paths to `src/`.
`docs.openwebui.com` was not consulted anywhere below — every claim is read directly from
this tag's source, and every decisive line is quoted.

Companion notes, not repeated here except where cited: `docs/research/owui-model-record.md`
(the model record's sync/read-back method, `meta.filterIds` at `models/models.py`-adjacent
frontend code, and §1's "source-of-truth by read-back" method this note follows for
Functions); `docs/research/owui-engine-connection.md` §3–4 (task calls never run outlet
filters, the `reasoning` vs `reasoning_content` delta fields, the `output[].type == 'reasoning'`
structured item, and the outlet payload's `content` line at `middleware.py:3944-3953` —
confirmed again below at its home in `outlet_filter_handler`); `docs/research/owui-preset-system-prompt.md`
(unrelated to this note's scope).

---

## 1. The sync form and what it stores

### 1.1 The route, the form, the field list

`routers/functions.py:158-159`:

```python
class SyncFunctionsForm(BaseModel):
    functions: list[FunctionWithValvesModel] = []
```

`FunctionWithValvesModel` (`models/functions.py:59-75`) is the sync item type:

```python
class FunctionWithValvesModel(BaseModel):
    id: str
    user_id: str | None = None
    name: str
    type: str
    content: str
    meta: FunctionMeta
    valves: dict | None = None
    is_active: bool = False
    is_global: bool = False
    updated_at: int
    created_at: int
```

So: **`valves` is a field** (`dict | None = None`), **`is_active`** and **`is_global`** are
fields (each `bool = False` when omitted), **`type`** is a required field (no default — the
payload must supply it), and **`meta`** is required and typed `FunctionMeta`
(`models/functions.py:37-40`):

```python
class FunctionMeta(BaseModel):
    description: str | None = None
    manifest: dict | None = {}
    model_config = ConfigDict(extra='allow')
```

`extra='allow'` — any other sub-key on `meta` round-trips verbatim (this is the field the
next ticket's per-model `filterIds` attachment rides beside, on the *model* record, not this
one).

### 1.2 The route body: loads content, validates valves, never checks the id, never derives type

`routers/functions.py:162-183`, in full:

```python
@router.post('/sync', response_model=list[FunctionWithValvesModel])
async def sync_functions(
    request: Request,
    form_data: SyncFunctionsForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        for function in form_data.functions:
            function.content = replace_imports(function.content)
            function_module, function_type, frontmatter = await load_function_module_by_id(
                function.id,
                content=function.content,
            )

            if hasattr(function_module, 'Valves') and function.valves:
                Valves = function_module.Valves
                try:
                    Valves(**{k: v for k, v in function.valves.items() if v is not None})
                except Exception as e:
                    log.exception(f'Error validating valves for function {function.id}: {e}')
                    raise e

        return await Functions.sync_functions(user.id, form_data.functions, db=db)
    except Exception as e:
        log.exception(f'Failed to load a function: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e, 'Error loading function'),
        )
```

- **Content is loaded and validated**: `load_function_module_by_id` execs the (import-rewritten)
  source and requires a `Pipe`/`Filter`/`Action`/`Event` class to exist, or the sync 400s
  (`utils/plugin.py:296-306`, quoted in §4). A syntactically-broken or classless Filter never
  reaches the database.
- **`function_type` and `frontmatter` are computed and discarded.** Neither is assigned back
  onto `function.type` or `function.meta.manifest` before the call to `Functions.sync_functions`
  — unlike `/create` and `/id/{id}/update`, which do (`form_data.meta.manifest = frontmatter`,
  `routers/functions.py:207`, `394`). **The sync does not derive `type` from content**; the
  payload's own `type` field is what gets stored (§1.3).
- **Valves are validated only if the module has a `Valves` class and the payload supplies
  `valves`** — `if hasattr(function_module, 'Valves') and function.valves:`. A Filter with no
  `Valves` class, or a payload with `valves: null`, skips this block entirely (no validation,
  no error).
- **No `isidentifier()` check.** That check exists only in `create_new_function`
  (`routers/functions.py:206`: `if not form_data.id.isidentifier():` → 400 "Only alphanumeric
  characters and underscores are allowed in the id"). `sync_functions` has no such gate on
  `function.id` anywhere in the route or in `Functions.sync_functions` — **a hyphenated id is
  accepted through `/sync`** (it is never checked against `str.isidentifier()`, which would
  reject a hyphen).
- **`request.app.state.FUNCTIONS` is never touched** by this route (contrast `/create`
  `routers/functions.py:220-221` and `/id/{id}/update` `:394-395`, which both call
  `get_functions_cache(request)` and write the freshly-loaded module into it immediately).
  Why this is safe for a chat turn is answered in §4: the cache is content-keyed and
  self-invalidates on the next request regardless.

### 1.3 `Functions.sync_functions`: per-field verbatim/defaulted/overwritten, and the failure path

`models/functions.py:140-182`, in full:

```python
    async def sync_functions(
        self,
        user_id: str,
        functions: list[FunctionWithValvesModel],
        db: AsyncSession | None = None,
    ) -> list[FunctionWithValvesModel]:
        # Synchronize functions by updating existing ones, inserting new ones,
        # and removing those that are no longer present.
        try:
            async with get_async_db_context(db) as db:
                # Get existing functions
                result = await db.execute(select(Function))
                existing_functions = result.scalars().all()
                existing_ids = {func.id for func in existing_functions}

                # Prepare a set of new function IDs
                new_function_ids = {func.id for func in functions}

                # Update or insert functions
                for func in functions:
                    func_data = func.model_dump()
                    func_data['valves'] = encrypt_valves(func_data['valves']) if func_data.get('valves') else None
                    func_data['user_id'] = user_id
                    func_data['updated_at'] = int(time.time())

                    if func.id in existing_ids:
                        await db.execute(update(Function).filter_by(id=func.id).values(**func_data))
                    else:
                        new_func = Function(**func_data)
                        db.add(new_func)

                # Remove functions that are no longer present
                for func in existing_functions:
                    if func.id not in new_function_ids:
                        await db.delete(func)

                await db.commit()

                result = await db.execute(select(Function))
                return [FunctionModel.model_validate(func) for func in result.scalars().all()]
        except Exception as e:
            log.exception(f'Error syncing functions for user {user_id}: {e}')
            return []
```

Per field, against the exact payload keys:

| field | stored |
|---|---|
| `id`, `name`, `type`, `content`, `meta`, `is_active`, `is_global`, `created_at` | verbatim from the payload (`func.model_dump()`, no overwrite in this method) |
| `valves` | verbatim if present, but **encrypted** (`encrypt_valves`, a no-op wrapper when `ENABLE_VALVE_ENCRYPTION` is unset — `utils/valves.py:21-24`); **forced to `None` if falsy/absent** (`func_data.get('valves')` is falsy for `None`, `{}`) |
| `user_id` | **overwritten** — always the calling admin's id, never the payload's `user_id` |
| `updated_at` | **overwritten** — always `int(time.time())` at sync time, never the payload's `updated_at` |

**This is a genuine difference from the models sync** (`docs/research/owui-model-record.md`
§1.2, which the render must account for): the Functions sync does **not** overwrite
`created_at` — the payload's value is stored as given. Deletion (rows in the DB but absent
from the payload) **does happen**, unconditionally: the second loop deletes every existing
row whose id is not in `new_function_ids`.

**The failure path**: `except Exception as e: log.exception(...); return []` — any DB error
during the whole sync (insert, update, delete, or the final re-select) is swallowed and the
method returns an **empty list**, not an exception. Combined with the route's own
`response_model=list[FunctionWithValvesModel]`, a swallowed failure serializes as `200 OK`
with body `[]` — indistinguishable at the HTTP layer from "there are now zero functions."
This is exactly the shape ticket 26's note already flags for the models sync, and it is why
the read-back (§2, and this ticket's own comparison) must not trust the sync response alone.

---

## 2. The read-back: which route returns what

| route | response model (`routers/functions.py`) | `content`? | `valves`? | `is_global`/`is_active`? |
|---|---|---|---|---|
| `GET /` (`:47-52`) | `list[FunctionResponse]` | no | no | yes |
| `GET /list` (`:55-60`, admin-only) | `list[FunctionUserResponse]` | no | no | yes |
| `GET /export?include_valves=` (`:68-76`, admin-only) | `list[FunctionModel \| FunctionWithValvesModel]` | yes | only if `include_valves=true` | yes |
| `GET /id/{id}` (`:269-278`, admin-only) | `FunctionModel \| None` | **yes** | no | yes |
| `GET /id/{id}/valves` (`:458-470`, admin-only) | `dict \| None` | — | **yes** (this is the only route that returns valves for a single function) | — |

`FunctionResponse` (`models/functions.py:80-90`):

```python
class FunctionResponse(BaseModel):
    id: str
    user_id: str | None = None
    type: str
    name: str
    meta: FunctionMeta
    is_active: bool
    is_global: bool
    updated_at: int
    created_at: int
```

No `content` field at all — `GET /` (the route `_read_back` in `gideon/host/owui.py` currently
calls) cannot distinguish two Functions of the same id with different `content`; only the id
set, and `meta`/`is_active`/`is_global`/`type`, are visible from it. `FunctionModel`
(`models/functions.py:43-56`) adds `content: str` but has no `valves` field. `FunctionWithValvesModel`
(§1.1) is the only response model carrying both `content` and `valves` together, and it is
returned by `/sync` itself (echoing what was just written) and by `/export?include_valves=true`
— **a canonical Function comparison for ticket 09's read-back should use `GET /id/{id}` for
`content`/`meta`/`is_active`/`is_global` plus `GET /id/{id}/valves` for valves**, mirroring
what `docs/research/owui-model-record.md` §1.2 established as precedent for models.

### 2.1 Where valves live and how they are set

`valves = Column(JSONField, nullable=True)` is a column directly on `Function`
(`models/functions.py:28`) — not a separate table, not derived from a `Valves` class default.
`POST /id/{id}/valves/update` (`routers/functions.py:514-554`) is the only write path outside
`/sync`: it 401s if the function has no `Valves` class (`:544-547`); otherwise it constructs
`Valves(**form_data)`, `model_dump(exclude_unset=True)`, and calls
`Functions.update_function_valves_by_id` (`models/functions.py:319-330`), which encrypts and
stores exactly that dict.

**A Function with no `Valves` class**, read through `GET /id/{id}/valves`, still returns
whatever the `valves` column holds — the route (`:458-470`) does not check for a `Valves`
class, unlike `/valves/update`. `decrypt_valves` (`utils/valves.py:27-38`) returns `{}` for a
`None`/empty column value:

```python
def decrypt_valves(valves) -> dict:
    if not valves:
        return {}
```

So the answer is `{}`, not `null`, for a Function that has never had valves written — whether
or not it defines a `Valves` class.

---

## 3. Global Filter selection for a chat turn

All of this lives in `utils/filter.py`.

### 3.1 Which filters are candidates

`models/functions.py:277-287`:

```python
    async def get_active_function_ids_by_type(
        self, type: str, db: AsyncSession | None = None
    ) -> list[tuple[str, bool]]:
        """Return (id, is_global) for active functions without fetching plugin source."""
        async with get_async_db_context(db) as db:
            result = await db.execute(select(Function.id, Function.is_global).filter_by(type=type, is_active=True))
            return [(id, bool(is_global)) for id, is_global in result.all()]

    async def get_active_filter_ids(self, db: AsyncSession | None = None) -> list[tuple[str, bool]]:
        """Return (id, is_global) for active filters without fetching plugin source."""
        return await self.get_active_function_ids_by_type('filter', db=db)
```

**`is_active=True` is required** — this is the only DB filter (`type='filter'`,
`is_active=True`); `is_global` is fetched but not filtered on here, it's read per-row.

### 3.2 The per-model attachment key, and the merge

`utils/filter.py:59-65`:

```python
def get_model_filter_ids(model, active_filters):
    filter_ids = [fid for fid, is_global in active_filters if is_global]
    if isinstance(model, dict) and 'info' in model and 'meta' in model['info']:
        filter_ids.extend(model['info']['meta'].get('filterIds', []))
        filter_ids = list(set(filter_ids))
    active_filter_ids = {fid for fid, _ in active_filters}
    return [fid for fid in filter_ids if fid in active_filter_ids]
```

**The per-model attachment key is `filterIds`** (camelCase), read off
`model['info']['meta']['filterIds']` — the same key `docs/research/owui-model-record.md:526`
already found on the frontend model-selector side, confirmed here on the backend selection
path too. The merge: start from every **global** active filter id, extend with the model's
`filterIds`, `set()`-dedupe, then **intersect against the active-filter id set again** — so a
`filterIds` entry naming an inactive or non-`filter`-typed function is silently dropped, and
**a filter that is both global and attached per-model runs once**, not twice (the `set()` call
is exactly the dedup).

**`is_global` is not required for the per-model attachment path** — only `is_active=True` and
`type='filter'` are required (via `active_filter_ids`); `is_global` only decides automatic
inclusion in the first list comprehension.

### 3.3 Priority: read from the DB `valves` column, defaulting through the `Valves` class

`utils/filter.py:88-103` (inside `resolve_filter_pipeline`):

```python
    async def get_priority(function_id):
        try:
            function_module = await get_function_module(request, function_id, function=functions_by_id.get(function_id))
            if function_module and hasattr(function_module, 'Valves'):
                valves_db = valves_by_id.get(function_id)
                valves = function_module.Valves(**(valves_db if valves_db else {}))
                return getattr(valves, 'priority', 0)
        except Exception:
            pass
        return 0

    priorities = {}
    for fid in filter_ids:
        priorities[fid] = await get_priority(fid)
    filter_ids.sort(key=lambda fid: (priorities.get(fid, 0), fid))
```

Priority comes from `valves_by_id.get(function_id)` — `Functions.get_function_valves_by_ids`
(`models/functions.py:303-317`), a batch read of the **DB `valves` column**, decrypted —
instantiated into the module's own `Valves` pydantic class (so a key absent from the DB row
falls back to that class's own field default), then `getattr(valves, 'priority', 0)`.

- **A Filter with no `Valves` class gets priority `0`** — the `if function_module and
  hasattr(function_module, 'Valves')` guard is false, so the function falls through to the
  unconditional `return 0` at the end.
- Sort is `(priorities.get(fid, 0), fid)` — priority ascending, id as tiebreak.

### 3.4 Task calls run no outlet filter (confirmed directly)

`routers/tasks.py:20` imports only `from open_webui.routers.pipelines import
process_pipeline_inlet_filter` — no import of `process_filter_functions`, no import of any
outlet variant, anywhere in that file. This directly confirms
`docs/research/owui-engine-connection.md` §3: **no outlet filter of any kind (native or
pipeline) runs on a task call** (title, tags, or otherwise).

---

## 4. Loading and caching (`utils/plugin.py`)

### 4.1 Frontmatter

`extract_frontmatter` (`utils/plugin.py:151-184`) parses `key: value` lines inside a leading
`"""..."""` docstring block with no fixed key allowlist — any `[a-z_]+: ...` line is captured
into the dict. **Only one key is functionally consumed anywhere in the backend**: `requirements`
— `grep`-verified (`utils/plugin.py:225`, `:275`, `:472`, `:478` are the only four call sites
reading `frontmatter.get('requirements', ...)` in the whole `backend/open_webui/` tree). It
triggers a pip install **only when `content` is supplied directly** (the `/sync`, `/create`,
and `/update` load paths, and the outlet/inlet-hook cold-load path when the DB has no matching
cache entry) via `install_frontmatter_requirements` — gated by
`ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` and skipped under `OFFLINE_MODE`
(`utils/plugin.py:422-430`), and further gated by an in-process `_installed_requirements` set
so a given requirement string is only ever installed once per process.

### 4.2 `replace_imports`: exact rule, and a real gotcha

`utils/plugin.py:187-201`, in full:

```python
def replace_imports(content):
    """
    Replace the import paths in the content.
    """
    replacements = {
        'from utils': 'from open_webui.utils',
        'from apps': 'from open_webui.apps',
        'from main': 'from open_webui.main',
        'from config': 'from open_webui.config',
    }

    for old, new in replacements.items():
        content = content.replace(old, new)

    return content
```

Four **plain substring replacements** (`str.replace`, no regex, no word boundary, not
line-anchored) — a stdlib-only Filter with none of the literal substrings `from utils`,
`from apps`, `from main`, `from config` anywhere in its source is untouched, verbatim in and
verbatim out. **Flag for the guardrail's own source**: because this is a bare substring match
over the *entire file* (not just import lines), any of those four substrings occurring inside
a docstring, a log message, or — concretely, for a deadline guardrail — inside the refusal
text or a comment (e.g. "...counted **from** the notice **config**uration date..." is not a
risk since it needs the literal substring `from config` with that exact spacing, but a phrase
like "the clock runs from config" would be) will be silently mangled. Cheap to avoid (don't
use those four substrings verbatim in the source), but real and undocumented, and re-triggered
on **every** cold-cache load, not just `/sync` (see §4.3).

### 4.3 Where content is written back to the DB, and why `/sync` itself does not drift

`load_function_module_by_id` (`utils/plugin.py:259-315`) execs `content` into a fresh module
and returns `(Pipe(), 'pipe', frontmatter)` / `(Filter(), 'filter', frontmatter)` /
`(Action(), 'action', frontmatter)` / `(Event(), 'event', frontmatter)` by `hasattr` on the
module, in that priority order (`:296-306`); with none of the four classes, it raises
`Exception('No Function class found in the module')`. On any exception it also **deactivates
the function** (`utils/plugin.py:312`: `await Functions.update_function_by_id(function_id,
{'is_active': False})`) before re-raising.

The DB write-back that could make a read-back differ from what `/sync` pushed lives in
`get_function_module_from_cache`, **not** in `/sync` itself:

```python
async def get_function_module_from_cache(
    request, function_id, function: FunctionModel | None = None, load_from_db=True
):
    ...
    if load_from_db:
        if function is None:
            function = await Functions.get_function_by_id(function_id)
        if not function:
            raise Exception(f'Function not found: {function_id}')
        content = function.content

        new_content = replace_imports(content)
        if new_content != content:
            content = new_content
            # Update the function content in the database
            await Functions.update_function_by_id(function_id, {'content': content})
        ...
```

(`utils/plugin.py:375-397`). Since `/sync` already applies `replace_imports` before storing
(`routers/functions.py:171`), a chat turn's re-application of `replace_imports` is a no-op for
content with none of the four substrings (`new_content == content`, no write) — **the stored
content stays byte-identical to what apply pushed**, so a read-back after a chat turn matches
the manifest, as ticket 09's note assumes. The comment on the line above (`utils/plugin.py:384-385`,
`"# This is useful for hooks like 'inlet' or 'outlet' where the content might change and we
want to ensure the latest content is used."`) is itself the documented reason `load_from_db`
defaults `True` for inlet/outlet.

### 4.4 The cache: content-equality keyed, no restart needed

```python
        if function_id in function_contents_cache and function_id in functions_cache:
            if function_contents_cache[function_id] == content:
                return functions_cache[function_id], None, None

        function_module, function_type, frontmatter = await load_function_module_by_id(function_id, content)
    ...
    functions_cache[function_id] = function_module
    function_contents_cache[function_id] = content
```

(`utils/plugin.py:399-414`). The cache key is **content-string equality**, not a hash, but
the effect is the same: on every `load_from_db=True` call (i.e. every inlet and outlet — see
`filter.py:178`: `load_from_db=(filter_type != 'stream')`), the DB `content` column is read
first and compared byte-for-byte against the last-loaded content; any mismatch (including one
from a fresh `/sync` push) forces a re-exec into a fresh module object. **A `/sync`'d content
change takes effect on the very next chat turn with no frontend or backend restart** — the
caches (`get_functions_cache`/`get_function_contents_cache`, `utils/plugin.py:324-337`) live on
`request.app.state`, but they are never trusted without the DB comparison first when
`load_from_db=True`. The `stream` hook alone uses `load_from_db=False` (cache-only, for
per-chunk performance — `utils/plugin.py:404-411`), so a content change picked up by that
turn's inlet/outlet is *not* re-checked again mid-stream, but that's irrelevant to an outlet
Filter.

### 4.5 Failure during import or during the handler itself: inlet fails closed, outlet fails open

`process_filter_function` (`utils/filter.py:165-207`, the try/except at `:188-205`) re-raises on any exception from the
handler call:

```python
    try:
        ...
        form_data = await run_filter_handler(handler, params)
    except Exception:
        if filter_type == 'inlet':
            log.debug('Error in inlet filter %s', filter_id, exc_info=True)
        else:
            log.exception('Error in %s filter %s', filter_type, filter_id)
        raise
```

`process_filter_functions` (`utils/filter.py:212-250`) does not catch it — the exception
propagates to the caller. **The two callers handle it oppositely:**

- **Inlet — fails closed.** `middleware.py:2631-2642`:
  ```python
      if ENABLE_PLUGINS:
          try:
              filter_functions = await get_filter_functions(request, model, metadata.get('filter_ids', []))
              form_data, flags = await process_filter_functions(
                  request=request, filter_context=filter_context,
                  filter_functions=filter_functions, filter_type='inlet',
                  form_data=form_data, extra_params=extra_params,
              )
          except Exception as e:
              raise Exception(f'{e}')
  ```
  re-raised out of `process_chat_payload`, which surfaces as an error response to the client —
  a broken inlet Filter (or one whose module fails to import) fails the whole turn.

- **Outlet — fails open, silently.** The entire body of `outlet_filter_handler`
  (`middleware.py:3899`–`4029`) is one `try:`/`except Exception as e: log.debug('Error running
  outlet filters: %s', e)` (`middleware.py:4029-4030`). An import failure, a `Valves`
  validation error, or an exception raised inside the guardrail's own `outlet()` method is
  caught here, logged at **debug** level only, and the function simply returns — **the
  already-persisted assistant message (written earlier by the streaming/non-streaming response
  handler, before `outlet_filter_handler` runs) is left exactly as generated and shown to the
  user.** For a deadline guardrail this is the opposite of the ticket's fail-closed intent for
  an actual trip: a bug in the guardrail's own code (not a trip) fails open, not closed. This
  is proven by the code, not inferred — flagged prominently as the single fact most worth the
  implementer's attention.

---

## 5. The outlet hook — where it runs, the exact payload, and the replacement recipe

### 5.1 Server-side, inline — not a client round-trip

`outlet_filter_handler` (`middleware.py:3872-3878`) says so in its own docstring:

```python
async def outlet_filter_handler(ctx):
    """Run outlet filters inline after chat completion.

    Replaces the separate POST /api/chat/completed round-trip.
    Persists outlet-modified content to DB and emits a chat:outlet event
    so the frontend can sync its in-memory state. Returns immediately when
    the model has no filters.
    ...
```

Confirmed independently on the route side, `main.py:2063-2065`:

```python
@app.post('/api/chat/completed')
async def chat_completed(request: Request, form_data: dict, user=Depends(get_verified_user)):
    """Deprecated: outlet filters now run inline during chat completion.
    Kept for backward compatibility with external integrations."""
```

And on the frontend: `src/lib/components/chat/Chat.svelte:2533-2537` —

```js
const chatCompletedHandler = async (_chatId, modelId, responseMessageId, messages) => {
    // Backend handles outlet filters and persistence inline.
    // Just refresh the sidebar chat list.
    if ($chatId == _chatId && !$temporaryChatEnabled) {
        await refreshChatList(localStorage.token);
    }
};
```

For a normal UI chat turn the client **never** calls `/api/chat/completed` to run outlet
filters — that route is dead code kept only for external API integrations that still call it
directly. `outlet_filter_handler` has four call sites, all in `utils/middleware.py`: the
primary UI SSE streaming path's normal (non-cancelled) completion — inside the nested
`response_handler` closure (`:4257`) of `streaming_chat_response_handler` — calls it at
`:6278`, immediately after `ctx['assistant_message']` is built from the accumulated `output`
(`:6273-6277`) and right after that same output is first persisted via
`Chats.upsert_message_to_chat_by_id_and_message_id` (`:6251-6259`); the buffered/converted
(non-streaming-upstream) path, `non_streaming_chat_response_handler`, calls it twice
(`:4170` and `:4209`); and the deprecated API pass-through `stream_wrapper` (`:6319`), gated
by `ENABLE_API_OUTLET_FILTERS`/`has_api_outlet_filters` (`:6325`, §9.2), calls it once more (`:6378`).

### 5.2 The exact `form_data` (`outlet_data`) the outlet receives

`middleware.py:3945-3961`:

```python
        outlet_data = {
            'model': model_id,
            'messages': [
                {
                    'id': m.get('id'),
                    'role': m.get('role'),
                    'content': m.get('content') or get_output_text(m.get('output')),
                    'info': m.get('info'),
                    'timestamp': m.get('timestamp'),
                    # Deepcopy so in-place filter mutations do not alias messages_map's baseline
                    **({'output': copy.deepcopy(m['output'])} if m.get('output') else {}),
                    **({'usage': m['usage']} if m.get('usage') else {}),
                    **({'sources': m['sources']} if m.get('sources') else {}),
                }
                for m in message_list
            ],
            'filter_ids': metadata.get('filter_ids', []),
            'chat_id': chat_id,
            'session_id': metadata.get('session_id'),
            'id': message_id,
        }
```

Top-level keys: `model` (the model **id string**, not the model dict — `model_id = model.get('id')
if isinstance(model, dict) else model`, `:3902`), `messages`, `filter_ids`, `chat_id`,
`session_id`, `id` (the message id, confirming the question's list exactly).

Per-message keys: `id`, `role`, `content` (falls back through `get_output_text(output)` when
`content` is empty — same helper the engine-connection note already traced), `info`,
`timestamp`, and conditionally `output` (a **deepcopy** of the stored structured items —
including any `type: 'reasoning'` item, see §6), `usage`, `sources`.

**Extra params offered to a handler** (`middleware.py:3966-3972`):

```python
        extra_params = {
            '__event_emitter__': event_emitter,
            '__event_call__': event_caller,
            '__user__': user.model_dump() if isinstance(user, UserModel) else {},
            '__metadata__': metadata,
            '__request__': request,
            '__model__': model,
        }
```

**Only these six.** `get_filter_params` (`filter.py:135-144`) intersects this dict against the
handler's own signature and adds `'__id__': filter_id` (the *Filter's own* id, not the chat's):

```python
def get_filter_params(sig, filter_id, filter_type, form_data, extra_params):
    params = {'event': form_data} if filter_type == 'stream' else {'body': form_data}
    return params | {
        k: v
        for k, v in {**extra_params, '__id__': filter_id}.items()
        if k in sig.parameters
    }
```

**`__chat_id__` and `__message_id__` are *not* in the outlet's `extra_params`** — grep-confirmed
across the file: those two keys appear in the **inlet**'s extra_params (`middleware.py:4241-4242`),
the **stream**-adjacent extra_params (`middleware.py:2525-2526`), and the request-filter
extra_params (`middleware.py:3422-3423`), but the block quoted above (the one actually passed
to outlet) has neither key, nor `__oauth_token__`. A guardrail `outlet` handler that declares
`__chat_id__`/`__message_id__` as parameters will simply never receive them (they're absent
from `sig.parameters` intersection, so nothing is passed for those names — a Python default on
the parameter is what saves it from a `TypeError`). **Use `body['chat_id']` and `body['id']`
instead** — both are present at the top level of `outlet_data` (confirmed above).

**Sync and async handlers both accepted**: `run_filter_handler` (`filter.py:159-162`):

```python
async def run_filter_handler(handler, params):
    if inspect.iscoroutinefunction(handler):
        return await handler(**params)
    return handler(**params)
```

### 5.3 What the server does with the returned body

`middleware.py:3995-4028`, in full (already quoted for the `output_changed` gate in §4.5's
sibling analysis — reproduced here with the persistence/emit consequence spelled out):

```python
        if outlet_result and outlet_result.get('messages'):
            if not is_unsaved_chat and messages_map:
                for message in outlet_result['messages']:
                    outlet_message_id = message.get('id')
                    if outlet_message_id and outlet_message_id in messages_map:
                        original_message = messages_map[outlet_message_id]
                        original_content = original_message.get('content') or get_output_text(
                            original_message.get('output')
                        )
                        message_content = message.get('content') or get_output_text(message.get('output'))
                        content_changed = original_content != message_content
                        output_changed = message.get('output') and message.get('output') != original_message.get(
                            'output'
                        )
                        if content_changed or output_changed:
                            message_update = {
                                'originalContent': original_content,
                                **({'output': message['output']} if output_changed else {}),
                            }
                            if content_changed:
                                message_update['content'] = message_content or ''
                            await Chats.upsert_message_to_chat_by_id_and_message_id(
                                chat_id,
                                outlet_message_id,
                                message_update,
                            )

            if event_emitter:
                await event_emitter(
                    {
                        'type': 'chat:outlet',
                        'data': {'messages': outlet_result['messages']},
                    }
                )
```

Three load-bearing facts, each proven by this exact code:

1. **The whole persist/emit block requires `outlet_result` to be truthy and carry `messages`.**
   `outlet_result` is whatever `process_filter_functions` returns as `form_data` — which is
   whatever the Filter's `outlet()` method **returns**. `process_filter_function` does
   `form_data = await run_filter_handler(handler, params)` (`filter.py:199`) — **it replaces**
   `form_data`, it does not merge. **A Filter that mutates `body` in place but forgets an
   explicit `return body` returns `None` implicitly (ordinary Python), and the entire
   persist-and-emit step (both the DB write and the `chat:outlet` socket event) is silently
   skipped.** This is the single most important implementation requirement the recipe below
   depends on, and it is not stated anywhere in the platform's own comments — it falls directly
   out of `form_data = await run_filter_handler(...)` plus the `if outlet_result and
   outlet_result.get('messages'):` gate.

2. **`content_changed` is a straight inequality** on `content` (or its `get_output_text(output)`
   fallback) — setting `messages[-1]['content']` to the fixed refusal text makes this `True`
   whenever that text differs from the original answer (always true in practice), and the
   persisted `message_update['content']` becomes exactly that fixed text.

3. **`output_changed` requires the *new* `output` to be truthy** —
   `message.get('output') and message.get('output') != original_message.get('output')` is a
   Python `and`: if the Filter sets `output` to `None`, `[]`, or omits the key, the left operand
   is falsy and `output_changed` short-circuits to that falsy value. **`'output'` is then absent
   from `message_update` entirely** (`**({'output': message['output']} if output_changed else
   {})`), so `Chats.upsert_message_to_chat_by_id_and_message_id` never touches the stored
   `output` field — **the old structured items (the reasoning item and the old message item)
   remain persisted in the database**, even though the Filter "cleared" `output` in its return
   value. **A falsy clear does not persist.** To actually clear the persisted reasoning block,
   the Filter must set `output` to a **new, non-empty, and different** list — e.g. a single
   `{'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type':
   'output_text', 'text': FIXED_TEXT}]}` item (see `docs/research/owui-engine-connection.md`
   §4 for this item shape) — so the truthy check and the inequality both pass.

Socket events emitted: exactly one, `chat:outlet`, carrying `{'messages': outlet_result['messages']}`
verbatim (whatever the Filter returned, not the persisted `message_update` — these can differ:
e.g. if the Filter sets `output: []`, the DB keeps the old output (point 3 above) but the
**socket event still carries the Filter's `output: []`**, because the emit uses `outlet_result`
directly, not `message_update`). No `chat:message`/`chat:completion` event is emitted from this
path — those are the streaming-delta event types, unrelated to the outlet.

### 5.4 How the client applies the `chat:outlet` event

`src/lib/components/chat/Chat.svelte:1277-1291`:

```js
} else if (type === 'chat:outlet') {
    // Outlet filter ran on backend — sync in-memory state
    const outletMessages = data.messages ?? [];
    for (const msg of outletMessages) {
        if (msg?.id && history.messages[msg.id]) {
            const existing = history.messages[msg.id];
            if (existing.content !== msg.content) {
                history.messages[msg.id] = {
                    ...existing,
                    originalContent: existing.content,
                    ...msg
                };
            }
        }
    }
    history = history;
    return; // Patches history.messages directly; skip the trailing write-back.
}
```

- **Gated on `content` alone**: `if (existing.content !== msg.content)` — the whole per-message
  update (spreading every key of `msg` over `existing`, `output` included) is skipped if
  `msg.content` happens to equal the client's current in-memory content, *even if* `msg.output`
  differs. Irrelevant for the guardrail (its whole point is that `content` always changes on a
  trip), but a real asymmetry against the server-side gate (§5.3 point 3 keys on `output`
  truthiness; this client gate keys on `content` equality) worth knowing if a future Filter ever
  wants to change `output` alone.
- **`{...existing, originalContent: existing.content, ...msg}`**: a plain object spread. If
  `msg` carries an `output` key (any value, including `null`/`[]`), it **overwrites**
  `existing.output` in the browser's live state immediately — unlike the server persistence
  gate, the client spread has no truthiness condition. So a Filter that returns `output: []` (or
  omits it) produces different outcomes on the two sides: the **live** render clears
  immediately (client spread applies whatever `msg.output` is, or leaves it alone if the key is
  absent), but the **persisted** row (§5.3 point 3) does not, so a page reload re-fetches the
  old, un-cleared `output` from the chat's stored messages and the reasoning block reappears.

### 5.5 The renderer prefers `output` over `content` whenever `output` is non-empty

`src/lib/components/chat/Messages/ContentRenderer.svelte:283-296`:

```svelte
{#if output?.length}
    <StructuredOutputRenderer
        {id} {chatId} {messageId} {output} {model} {save} {preview} {compactPreview} {done}
        ...
    />
{:else if $settings?.renderMarkdownInAssistantMessages ?? true}
    <div class="markdown-prose">
        <Markdown ... content={formatMessageContent(content)} ... />
```

fed by `ResponseMessage.svelte:826-830`: `content={message.content}` `output={message.output}`.
**This is unconditional on the value of `content`.** If `output` is a non-empty array, the
`StructuredOutputRenderer` renders it (reasoning item, old message item, and all) **regardless
of what `content` holds** — a Filter that only sets `content` and leaves the old `output` array
in place will show the *old* reasoning block and old answer text on screen, not the fixed
refusal text, because `content` is never even reached by the template.

### 5.6 The recipe — what is proven, and what is not

**Proven by the code above** (not the frontend's `chat:outlet` handler alone — the render
branch in §5.5 is what makes this non-optional):

1. The Filter's `outlet(self, body)` (or async equivalent) must **return** `body` (§5.3 point 1
   — an implicit `None` return skips persistence *and* the socket emit entirely).
2. `body['messages'][-1]['content']` must be set to the fixed refusal text (drives
   `content_changed`, persisted verbatim).
3. `body['messages'][-1]['output']` must be set to a **new, non-empty, different** list — not
   `None`/`[]` (§5.3 point 3: a falsy value is never persisted to the DB, and §5.5: a
   non-empty `output` is what the renderer shows in preference to `content`, so leaving the old
   non-empty `output` in place defeats the whole point even though `content` changed). The
   simplest such list is a single `type: 'message'` item carrying the fixed text as its only
   `output_text` part, structurally mirroring the shape a real completion produces
   (`docs/research/owui-engine-connection.md` §4) but with no `reasoning` item.
4. No `__event_emitter__` call is required for the base case — `outlet_filter_handler` performs
   the DB write and the `chat:outlet` emit itself, unconditionally, once the Filter returns a
   body whose `messages` list is present and (per point 3) whose `output` is truthy-and-changed.
   An explicit `event_emitter({'type': 'replace', ...})` call from inside the Filter is
   **not** exercised by any code path read above — nothing in `outlet_filter_handler` or the
   `chat:outlet`/`chat:message` frontend handlers treats a Filter-emitted `replace` event
   specially beyond what `type === 'chat:message' || type === 'replace': message.content =
   data.content` already does for the streaming path (`Chat.svelte:1252-1253`) — that handler
   only ever sets `content`, never touches `output`, so it cannot substitute for point 3 even if
   used. **Not proven / not needed**: any additional `__event_emitter__` call from inside the
   guardrail's `outlet()`; the return-body edit (points 2–3) is both necessary and sufficient
   per the code read here.

---

## 6. Reaching the reasoning text from the outlet

**Yes — the outlet body already carries it, no extra read needed.** `middleware.py:3955`:

```python
                    **({'output': copy.deepcopy(m['output'])} if m.get('output') else {}),
```

`m` here is a raw persisted message dict from `Chats.get_messages_map_by_chat_id(chat_id)`
(`middleware.py:3937`, feeding `get_message_list` at `:3941`) for a saved chat, or from `ctx['assistant_message']`
(itself `{'content': content, 'output': response_output, **usage}`, `middleware.py:4165-4169`)
for an unsaved/temp chat — **both paths carry the full structured `output` list, unfiltered by
item type**, unlike the `content` field on the very next line (`m.get('content') or
get_output_text(m.get('output'))`), which — per `docs/research/owui-engine-connection.md` §4 —
specifically **excludes** `type: 'reasoning'` items via `get_output_text`'s `item.get('type')
!= 'message': continue` filter (`utils/misc.py:246-248`, `docs/research/owui-model-record.md`
does not cover this file — confirmed fresh here). So a Filter's `outlet(self, body)` reads
`body['messages'][-1].get('output', [])` and filters for `item['type'] == 'reasoning'` itself
to find the reasoning text (via `get_output_text`-style concatenation over that item's
`content` parts, or by walking `item['content']` directly since the item's shape mirrors the
`type: 'message'` shape per `docs/research/owui-engine-connection.md` §4's quoted
`append_output_text`/`output[-1]` construction).

**A `Chats` read is not the cheapest route here — it's unnecessary**, since the body already
has it. As a fallback (e.g. if a future refactor stops including `output` in the outlet body,
or for a Filter that wants the canonical DB state rather than the deepcopy handed to it): `from
open_webui.models.chats import Chats` (`utils/middleware.py:46`, same import an outlet Filter
could make), then `await Chats.get_message_by_id_and_message_id(chat_id, message_id)`
(`models/chats.py:1106`) — an **async** method (uses `get_async_db_context` internally, the
same pattern every other `Chats`/`Functions` method in this codebase uses), so it is safe to
`await` from an **async** `outlet` handler (§5.2 confirms both sync and async handlers are
accepted) but cannot be called from a **sync** `outlet` handler without a `run_until_complete`-style
workaround, which is not advisable inside a request-handling coroutine. Prefer an async
`outlet` if a DB read is ever needed; for GIDEON's guardrail, it is not, per the paragraph
above.

---

## 7. Lower priority — the `stream` hook, briefly

Signature/param shape (`filter.py:135-136`): `params = {'event': form_data} if filter_type ==
'stream' else {'body': form_data}` — a `stream` handler's own body parameter is named `event`,
not `body`. Invoked once per SSE chunk from the upstream, **before** any reasoning-specific
extraction (`middleware.py:4835-4847`):

```python
                            data = JSONCodec.loads(data)

                            if filter_functions:
                                data, _ = await process_filter_functions(
                                    request=request, filter_context=filter_context,
                                    filter_functions=filter_functions, filter_type='stream',
                                    form_data=data, extra_params=filter_extra_params,
                                )

                            if data:
                                if 'event' in data and not getattr(request.state, 'direct', False):
                                    await event_emitter(data.get('event', {}))
```

**A falsy return drops the chunk** — `if data:` gates everything downstream (event emission,
`selected_model_id` handling, etc.) on the (possibly filter-replaced) `data` being truthy;
`process_filter_functions` again does a straight replace (`form_data = await
run_filter_handler(...)`, same mechanism as §5.3 point 1), so a `stream` handler that returns
`None`/`{}` makes that chunk vanish. **Reasoning deltas pass through it**: this call sits
directly on the raw parsed SSE JSON (`data = JSONCodec.loads(data)`), before the
reasoning-detection/accumulation logic the engine-connection note traces (its `reasoning`/
`reasoning_content` handling, gated further down in the same function) — a chunk shaped
`{"choices":[{"delta":{"reasoning":"..."}}]}` is exactly what a `stream` filter receives, whole.
`__body__` (the outer, non-chunk request body) is additionally offered here only
(`middleware.py:4841`: `filter_extra_params = {'__body__': form_data, **extra_params} if
filter_functions else None`) — not offered to inlet/outlet.

**`__metadata__` identity across hooks**: `metadata` is built once per request
(`process_chat_payload`'s own parameter) and threaded by reference into every `ctx['metadata']`
assignment downstream (`middleware.py:444`, `:3145`, then read back unchanged at `:3658`,
`:3885`, `:4037`, `:4225`) — no reassignment to a new dict was found between those points, so
the same dict object should back `__metadata__` across a single request's inlet, stream, and
outlet hooks. **Not independently exercised with a live request in this pass** — flagged as
inferred from the reference-only assignment pattern, not confirmed by tracing every branch
between definition and each hook's call site.

---

---

## 8. Follow-up: the replacement item's shape, hyphenated ids, and the `toggle` attribute

### 8.1 The `type: 'message'` output item as the streaming accumulator builds it, and how it closes

The sibling construction to the engine-connection note's `type: 'reasoning'` quote lives in the
same function, `update_assistant_message_from_stream` (`middleware.py:3532`):

```python
def update_assistant_message_from_stream(assistant_message, raw):
    ...
    def append_output_text(item, text):
        parts = item.setdefault('content', [])
        if parts and parts[-1].get('type') == 'output_text':
            parts[-1]['text'] += text
        else:
            parts.append({'type': 'output_text', 'text': text})
```

(`middleware.py:3532-3542`.) **`append_output_text` merges into the last part when it is
already `type: 'output_text'`** (string concatenation, `+=`), **and only appends a new part
otherwise** — so a single delta run never produces more than one part unless something else
(a reasoning→message transition) intervenes.

The `type: 'message'` item itself, first created the moment `content` deltas start arriving
(`middleware.py:3601-3611`):

```python
                    if not output or output[-1].get('type') != 'message':
                        output.append(
                            {
                                'type': 'message',
                                'id': output_id('msg'),
                                'status': 'in_progress',
                                'role': 'assistant',
                                'content': [],
                            }
                        )

                    append_output_text(output[-1], content)
```

Every key at creation: `type: 'message'`, `id: output_id('msg')` (`middleware.py:253-255`:
`f'{prefix}_{uuid4().hex[:24]}'`, so `msg_<24-hex-chars>` — **not** a bare `output_id('m')`,
the question's guess was close but the actual prefix is `'msg'`), `status: 'in_progress'`,
`role: 'assistant'`, `content: []` (populated afterward by `append_output_text`, which
appends `{'type': 'output_text', 'text': content}`, `middleware.py:3542`). No `started_at`
field is ever set on a message item — that field (and `ended_at`/`duration`) exists only on
`type: 'reasoning'` items (`middleware.py:3588`, `:3598-3600`).

**Reading the parts back**: `get_output_text` (`utils/misc.py:241-261`), already partly quoted
in the engine-connection note, is exact about what it concatenates:

```python
def get_output_text(output: list | None) -> str:
    if not isinstance(output, list):
        return ''

    texts = []
    for item in output:
        if not isinstance(item, dict) or item.get('type') != 'message':
            continue

        parts = item.get('content') or []
        if not isinstance(parts, list):
            continue

        text = ''.join(
            str(part.get('text')) for part in parts if isinstance(part, dict) and part.get('text') is not None
        )
        if text and not text.isspace():
            texts.append(text)

    return '\n'.join(texts)
```

(`utils/misc.py:241-261`.) Item-level filter is `item.get('type') != 'message'` (so a
`reasoning` item is skipped entirely, confirming the engine-connection note); **part-level
filter is `part.get('text') is not None`, not a check on `part.get('type')`** — any part
dict carrying a non-`None` `text` key is concatenated, joined with `''` inside one item, and
multiple `message` items are joined across each other with `'\n'`. **A replacement item with
a single `{'type': 'output_text', 'text': FIXED_TEXT}` part is read back as exactly
`FIXED_TEXT`** — no separator artifacts, since there's only one item and one part.

**How a message item is finalized when the stream ends** (the natural, non-cancelled path,
`middleware.py:6234-6237`):

```python
                # Mark all in-progress items as completed
                for item in output:
                    if item.get('status') == 'in_progress':
                        item['status'] = 'completed'
```

This is the *only* place a `message` item's `status` flips to `'completed'` in the streaming
path — no `ended_at`/`duration` is ever added to it (those two fields are reasoning-only, per
§8.1's creation snippet above). The **non-streaming** path builds an already-finished item
directly, confirming the same finished shape has no extra fields beyond `status: 'completed'`
(`middleware.py:4127-4134`):

```python
                        response_output.append(
                            {
                                'type': 'message',
                                'id': output_id('msg'),
                                'status': 'completed',
                                'role': 'assistant',
                                'content': [{'type': 'output_text', 'text': content}],
                            }
                        )
```

**The finished shape a replacement item should mirror**: exactly these five keys —
`{'type': 'message', 'id': output_id('msg')-shaped string, 'status': 'completed', 'role':
'assistant', 'content': [{'type': 'output_text', 'text': FIXED_TEXT}]}`. No timing fields.

### 8.2 What the frontend renders for a `type: 'message'` item, and the reasoning item's part shape

`structuredOutput.ts`'s `GROUPABLE_OUTPUT_TYPES` (`:85-92`) — the set of item types folded into
collapsible `<details>` tokens — is `{'reasoning', 'function_call', 'open_webui:code_interpreter',
'web_search_call', 'file_search_call', 'computer_call'}`. **`'message'` is not in this set**, so
a message item takes the direct branch in `buildOutputDisplayItems` (`:409-420`):

```ts
		if (item.type === 'message') {
			const text = getMessageText(item);
			if (text.trim()) {
				flushDetails();
				displayItems.push({
					type: 'message',
					id: item.id ?? `message-${index}`,
					text
				});
			}
			return;
		}
```

`getMessageText` (`:129-131`) is `getTextFromParts(item.content ?? [])`, and `getTextFromParts`
(`:100-109`) — the client-side twin of the server's `get_output_text` — reads **every part's
`.text` field with no check on `part.type` at all**:

```ts
function getTextFromParts(parts: OutputContentPart[] = []): string {
	return parts
		.map((part) => {
			if (part?.text === undefined || part?.text === null) {
				return '';
			}
			return typeof part.text === 'string' ? part.text : String(part.text);
		})
		.join('');
}
```

`getReasoningText` (`:133-136`) calls the same `getTextFromParts`, over `item.summary` if
non-empty else `item.content` — **the reasoning item's parts are the identical
`{'type': 'output_text', 'text': ...}` shape as a message item's parts, not a distinct part
type**; the distinction between "reasoning" and "message" lives entirely on the *item's*
`type` field, never on the part. This matches the server-side construction, which uses the
same `append_output_text` helper for both (§8.1).

**Rendering**: `StructuredOutputRenderer.svelte`'s `displayItem.type === 'message'` branch
(`:73-96`) feeds `displayItem.text` straight into the same `Markdown` component
`ContentRenderer.svelte` uses for a plain `content` render:

```svelte
	{#if displayItem.type === 'message'}
		{#if renderMarkdown}
			<div class="markdown-prose">
				<Markdown
					id={`${id}-${displayItem.id}`}
					{chatId}
					{messageId}
					content={formatMessageContent(displayItem.text)}
					...
```

(`StructuredOutputRenderer.svelte:73-80`.) **Confirmed: a `message` item with a single
`output_text` part renders as ordinary markdown prose, pixel-identical to a plain `content`
render** — same component, same `class="markdown-prose"` wrapper, same `formatMessageContent`
call. A guardrail's replacement item needs nothing special here beyond the shape in §8.1.

### 8.3 Module naming with a hyphenated id, and route validation

`load_function_module_by_id` (`utils/plugin.py:277-279`):

```python
    module_name = f'function_{function_id}'
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
```

`types.ModuleType(name)` accepts **any string** as a module's `__name__` — Python only requires
identifier syntax for the dotted path an `import` *statement* parses; a direct `ModuleType`
construction and a direct `sys.modules[...]=` assignment are plain string-keyed operations with
no such requirement. **`function_gideon-deadline-guardrail` is a perfectly valid module name
and `sys.modules` key** under this construction. The source itself is executed via `exec(content,
module.__dict__)` (`utils/plugin.py:291`) against a **temp file** whose name is random
(`tempfile.NamedTemporaryFile(delete=False)`, `utils/plugin.py:283`) — **no filesystem path is
ever derived from `function_id`**, and cleanup on failure is `del sys.modules[module_name]`
(`utils/plugin.py:310`), again a plain dict-key delete. **No code path in this function calls
`importlib.import_module` or `__import__`** — grep-confirmed absent from the whole file (only
`from importlib import util` is imported, for an unrelated helper elsewhere in the module, and
`util` itself is never called in `load_function_module_by_id`).

**Route validation**: the only `isidentifier()` check in `routers/functions.py` is
`:206`, inside `create_new_function` (`:199-210`):

```python
    if not form_data.id.isidentifier():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only alphanumeric characters and underscores are allowed in the id',
        )
```

grep for `isidentifier` across the whole file returns exactly this one line. **None of
`update_function_by_id` (`/id/{id}/update`, `:378-417`), `delete_function_by_id`
(`/id/{id}/delete`, `:431-449`), `toggle_function_by_id` (`/id/{id}/toggle`, `:287-339`), or
`toggle_global_by_id` (`/id/{id}/toggle/global`, `:341-376`) call `isidentifier()` or any other
id-shape check** — each takes `id: str` as a FastAPI path parameter (the default string
converter, which accepts any character except `/`) and passes it straight to
`Functions.get_function_by_id`/`update_function_by_id`/`delete_function_by_id`, all of which
are plain `db.get(Function, id)`/`filter_by(id=id)` calls with no server-side shape
constraint. **`gideon-deadline-guardrail` works on every one of these four routes**, and
(per §1.2) on `/sync` as well — the only route that would ever reject a hyphenated id is
`/create`, which GIDEON's `functions_sync` (`gideon/host/owui.py:368-370`) does not call.

### 8.4 The `toggle` attribute: what it gates, and what a Filter without it can never expose

`get_active_status`, inside `resolve_filter_pipeline` (`utils/filter.py:76-82`):

```python
    async def get_active_status(filter_id):
        function_module = await get_function_module(request, filter_id, function=functions_by_id.get(filter_id))

        if getattr(function_module, 'toggle', None):
            return filter_id in (enabled_filter_ids or set())

        return True
```

**A Filter class with `toggle = True`** (a plain class attribute — `getattr(function_module,
'toggle', None)` reads it off the instantiated module object) **is only "active" for a given
turn if its id is in `enabled_filter_ids`** — the per-chat opt-in set, which flows from the
frontend's `filter_ids` metadata (`Chat.svelte:3562`:
`filter_ids: selectedFilterIds.length > 0 ? selectedFilterIds : undefined`) into
`metadata.get('filter_ids', [])` (the same `enabled_filter_ids` parameter `get_filter_functions`
forwards everywhere it's called, outlet included — §5.2). **A Filter class with no `toggle`
attribute (or `toggle` falsy) always returns `True` here — it always runs, unconditionally,
regardless of any per-chat state.**

The per-chat *button* only exists at all for a toggleable Filter, and only because
`utils/models.py`'s model-listing builder gates it the same way, one layer up
(`utils/models.py:427-435`):

```python
                function_module = functions_cache.get(filter_id)
                if function_module is None:
                    log.info('Failed to load filter module: %s', filter_id)
                    filter_items_by_id[filter_id] = []
                    continue
                if getattr(function_module, 'toggle', None):
                    items = get_filter_items_from_module(filter_function, function_module)
                else:
                    items = []
```

`model['filters']` (consumed by the frontend as `toggleFilters`,
`MessageInput.svelte:794-796`: `.map((id) => ($models.find(...) || {})?.filters ?? [])`) is
**only ever populated for a Filter whose module sets `toggle` truthy** — the button rendered
in `IntegrationsMenu.svelte`'s `toggleFilters` loop (`:329-341`, `on:click` toggling
`selectedFilterIds`) simply never appears for a Filter without `toggle`. **Confirmed on both
sides**: a Filter with no `toggle` attribute always runs (`filter.py:82`) *and* has no
user-facing switch to attempt turning it off (`models.py:432-435`) — there is no path by which
a chat user can disable GIDEON's guardrail unless its source sets `toggle`, which the plan does
not call for.

`get_filter_items_from_module` (`utils/models.py:297-308`) is also where `icon` comes from —
`function.meta.manifest.get('icon_url', None) or getattr(module, 'icon_url', None) or
getattr(module, 'icon', None)` — **irrelevant for an outlet-only, non-`toggle` Filter**: this
function is never called for it at all (gated by the same `if getattr(function_module,
'toggle', None):` above), so neither a `meta.manifest.icon_url` value nor a module-level `icon`
attribute has anywhere to surface. **`file_handler`** is gated in `process_filter_function`
strictly on `filter_type == 'inlet'` (`utils/filter.py:184-186`: `function_module.file_handler
if filter_type == 'inlet' and hasattr(function_module, 'file_handler') else None`) — never
read, checked, or acted on for `filter_type == 'outlet'`, so it has no effect on an outlet-only
Filter either.

---

## 9. Env/config switches that gate Filter Functions

### 9.1 `ENABLE_PLUGINS`: a plain env read, not persistent config, default on

`env.py:1165`:

```python
ENABLE_PLUGINS = os.getenv('ENABLE_PLUGINS', 'True').lower() == 'true'
```

A bare module-level constant computed once at process import from `os.getenv` — **default
`True`**. It carries no `PersistentConfig`/`Config` wrapper anywhere in `env.py` (grep-confirmed:
no `PersistentConfig` or `class Config` reference in that file), unlike the DB-backed per-key
`config.py` values the companion notes describe — **it cannot be changed from the admin UI at
runtime; only a container restart with a different `ENABLE_PLUGINS` env value changes it.**

It gates, by grep across the whole `backend/open_webui/` tree:

- **Filter selection, all hook types uniformly** — `resolve_filter_pipeline` short-circuits to
  `return [], []` when it's false (`utils/filter.py:69`), and `process_filter_functions` itself
  short-circuits to `return form_data, {}` (`utils/filter.py:220-221`) — since inlet, outlet,
  stream, and the "request" filter type all funnel through this one function, **this single
  check covers every hook type, outlet included**, with no separate outlet-specific gate needed.
- **`outlet_filter_handler` directly**, redundantly with the above: `filter_functions = (await
  get_filter_functions(...) if ENABLE_PLUGINS else [])` (`middleware.py:3901`, and again at
  `middleware.py:4246` for a sibling extra-params block) — if false, `filter_functions` is `[]`
  and (per §4.5/§5.1) the handler returns early with no pipeline-outlet fallback engaged either.
- **Function loading**: `load_function_module_by_id` raises `RuntimeError('Plugins are disabled
  by ENABLE_PLUGINS=false')` immediately if false (`utils/plugin.py:260-261`), before any exec.
- **`POST /api/v1/functions/sync` is *not* gated at the route level** — grep-confirmed:
  `routers/functions.py` checks `if not ENABLE_PLUGINS: return []` only on `GET /` (`:49`),
  `GET /list` (`:57`), and `GET /export` (`:74`); the `/sync` route (`:162-183`) has no such
  early return. But **it fails indirectly**: `sync_functions` calls `load_function_module_by_id`
  for every function in the payload (`routers/functions.py:172-175`), which raises the
  `RuntimeError` above when `ENABLE_PLUGINS` is false — caught by the route's own
  `except Exception as e: raise HTTPException(400, ...)` (`:181-183`), so **a sync attempt
  400s ("Error loading function") for every function in the payload**, not a silent no-op.

### 9.2 Other switches, each with its default

No `ENABLE_FUNCTIONS`-style separate flag and no `FUNCTIONS_DIR` requirement exist at this tag
— grep-confirmed absent from both `env.py` and `config.py`. `ENABLE_PLUGINS` is the single,
generic gate covering Tools, Functions (Pipes/Filters/Actions), all together.

- **`ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`** (`env.py:1166-1168`, default `True`) — gates
  whether a Filter's frontmatter `requirements:` line triggers `pip install`
  (`utils/plugin.py:422-430`). Irrelevant to GIDEON's guardrail if its source declares no
  `requirements:` frontmatter key (stdlib-only).
- **`OFFLINE_MODE`** (`env.py:1180`, default `False`) — its only effect on Functions is
  additionally skipping that same pip install (`utils/plugin.py:428-430`: `if OFFLINE_MODE:
  log.info(...); return`); it does not stop a Filter from loading or running otherwise, and
  its other side effects (`env.py:1182-1184`: sets `HF_HUB_OFFLINE=1`, forces
  `ENABLE_VERSION_UPDATE_CHECK` false) are unrelated to Functions.
- **`ENABLE_VALVE_ENCRYPTION`** (`env.py:752`, default `False`) — gates whether `encrypt_valves`/
  `decrypt_valves` (§2.1) actually encrypt, or pass through verbatim; does not gate whether a
  Filter runs.
- **`ENABLE_API_OUTLET_FILTERS`** (`env.py:1038`, default `True`) — gates only the two
  API/pass-through branches already flagged as out of scope in §5.1 (`middleware.py:4202`,
  `:6325-6337`); the UI chat path's `outlet_filter_handler` call on the primary streaming branch
  (`middleware.py:6278`, §5.1) is unconditional on this flag.

### 9.3 GIDEON's rendered environment: none of these are touched

`gideon/host/render/owui.py`'s `owui_environment` (`:253-336`) sets roughly forty Compose
environment keys — `WEBUI_*`, `ENABLE_PERSISTENT_CONFIG`, `ENABLE_SIGNUP`/`ENABLE_LOGIN_FORM`,
the `LDAP_*` block, `ENABLE_API_KEYS*`, `STORAGE_PROVIDER`, `ENABLE_OLLAMA_API`/`ENABLE_OPENAI_API`,
the engine-connection block (`TASK_MODEL_EXTERNAL`, `ENABLE_TITLE_GENERATION`, etc.),
`ENABLE_VERSION_UPDATE_CHECK`, `TZ`, and the permission-tree environment — but **grep-confirmed:
none of `ENABLE_PLUGINS`, `OFFLINE_MODE`, `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`,
`ENABLE_VALVE_ENCRYPTION`, or `ENABLE_API_OUTLET_FILTERS` appears anywhere in that function**.
**Their absence means every one defaults per §9.1/§9.2 — `ENABLE_PLUGINS=True` in particular —
so nothing in GIDEON's rendered frontend turns any of these gates off.** The guardrail Filter
will load, sync, and run under the defaults with no additional environment change needed.


---

## 10. The eval identity's door: does the outlet's replacement reach a bearer-key API caller?

Scenario per the coordinator: a bearer-key caller posts to `POST /api/chat/completions` with
`model: gideon-general`, no `session_id`, no `chat_id` (`docs/research/owui-preset-system-prompt.md`
§1.6's "eval identity" shape — and, per that note's §6, "users cannot mint API keys under
§4.2's set," confirmed independently below).

### 10.0 Why this caller gets `event_emitter = None`, and why that's the fork in the road

`main.py:1190`: `is_new_chat = 'parent_id' in form_data and form_data['parent_id'] is None and
not form_data.get('chat_id')` — **requires the payload to carry a `parent_id: null` key**, the
UI's own convention for "first message of a new chat." A minimal API payload (`{"model": ...,
"messages": [...]}`, no `parent_id`) fails this, so `is_new_chat` is `False`, and the
`chat_id`-synthesis at `main.py:1276-1277` (`if is_new_chat: metadata['chat_id'] =
str(uuid4())`) **never fires**. `chat_id = form_data.pop('chat_id', None) or ''`
(`main.py:1209`) stays `''`, and `metadata['chat_id']` (`main.py:1247`) is built from that same
empty string.

`get_event_emitter_and_caller` (`utils/middleware.py:3120-3135`):

```python
async def get_event_emitter_and_caller(metadata):
    event_emitter = None
    event_caller = None

    # event_emitter only needs user_id + chat_id + message_id.
    # It broadcasts to user:{user_id} room AND persists to DB,
    # so it works for backend-initiated calls (automations, API).
    if metadata.get('chat_id') and metadata.get('message_id'):
        event_emitter = await get_event_emitter(metadata)
    ...
    return event_emitter, event_caller
```

With `metadata.get('chat_id')` falsy, **`event_emitter` stays `None`** — not a no-op callable,
literally `None` — and this is the exact object threaded into `ctx['event_emitter']`
(`build_chat_response_context`, `utils/middleware.py:3138-3150`) that both response handlers
and `outlet_filter_handler` read.

**This one fact forks both the streaming and non-streaming code paths** into their
already-largely-analysed "fallback"/API branches (§5, §9), confirmed exactly at the two
`if event_emitter:` gates:

- Non-streaming, `non_streaming_chat_response_handler` (`utils/middleware.py:4049`):
  `if event_emitter:` wraps the UI-oriented block (`:4049-4197` — the `chat:completion` emits,
  the Chats persistence, and the *other* `outlet_filter_handler` call at `:4170`) — **all of
  it skipped** for this caller. Control falls through past `:4197`'s `return response` (that
  return is inside the skipped block) to the code at module-body indentation below it.
- Streaming, `streaming_chat_response_handler` (`utils/middleware.py:4252`): `if event_emitter:`
  gates the entire "Standard streaming response handler" (the background-task path with
  `response_handler`, Redis-cached partial output, and the outlet call at `:6278`) — **also
  skipped**. The `else:` branch, "Fallback to the original response" (`:6317`), containing
  `stream_wrapper`, is what actually runs for this caller.

### 10.1 Non-streaming: the outlet runs, but the response body is built from the untouched original

The code that actually executes for this caller, past the skipped `if event_emitter:` block
(`utils/middleware.py:4199-4214`, matching the coordinator's `:4195-4210` — the exact block
is quoted in full):

```python
    choices = response_data.get('choices', [])
    output = response_data.get('output')
    content = choices[0].get('message', {}).get('content') if choices else ''
    if ENABLE_API_OUTLET_FILTERS and (content or output):
        usage = normalize_usage(response_data.get('usage', {}) or {})
        ctx['assistant_message'] = {
            **({'content': content} if content else {}),
            **({'output': output} if output else {}),
            **({'usage': usage} if usage else {}),
        }
        await outlet_filter_handler(ctx)

    if isinstance(response, dict):
        response = merge_events_into_response(response_data, events)

    return response
```

**`outlet_filter_handler` does run** here (assuming `ENABLE_API_OUTLET_FILTERS` default `True`,
§9.2, and a normal completion has `content`) — GIDEON's guardrail `outlet()` executes and can
compute a replacement body. **But its return value is never wired into `response`.** The
return statement builds `response` from `merge_events_into_response(response_data, events)`
(`utils/middleware.py:3504-3517`, quoted once more for the decisive point: `return
{**extra_response, **response_data}` or bare `response_data`) — **`response_data` is the
original upstream completion, parsed before the outlet ever ran** (`get_response_data(response)`
at `:4042`), and `merge_events_into_response` never reads `ctx`, `ctx['assistant_message']`, or
anything `outlet_filter_handler` touched. `outlet_filter_handler` itself returns `None`
(no `return` statement anywhere in its body, `:3872-4030`) and its caller here discards that
`None` (the `await outlet_filter_handler(ctx)` line is a bare statement, not an assignment).
**The HTTP response body is the original completion, regardless of what the outlet returned —
proven by the return statement itself never touching `outlet_result`.**

### 10.2 Streaming: bytes are relayed live; the `stream` hook's edits ride along, the outlet's do not

`stream_wrapper` (`utils/middleware.py:6319-6379`), the branch this caller's streaming request
takes (§10.0):

```python
        async def stream_wrapper(original_generator, events):
            ...
            async for data in original_generator:
                if filter_functions:
                    line = data.decode('utf-8', 'replace') if isinstance(data, bytes) else data
                    if isinstance(line, str) and line.startswith('data:'):
                        payload = line.removeprefix('data:').strip()
                        if payload and payload != '[DONE]':
                            try:
                                event = JSONCodec.loads(payload)
                            except JSONCodec.JSONDecodeError:
                                event = None

                            if isinstance(event, dict):
                                event, _ = await process_filter_functions(
                                    request=request, filter_context=filter_context,
                                    filter_functions=filter_functions, filter_type='stream',
                                    form_data=event, extra_params=extra_params,
                                )
                                data = wrap_item(JSONCodec.dumps(event)) if event else None

                if data:
                    if has_api_outlet_filters:
                        update_assistant_message_from_stream(assistant_message, data)
                    yield data

            if has_api_outlet_filters and assistant_message:
                ctx['assistant_message'] = assistant_message
                await outlet_filter_handler(ctx)
```

**Confirmed: the SSE bytes are relayed as they arrive.** Each chunk from `original_generator`
is `yield`ed (`:6374`) inside the `async for` loop, one at a time, as a `StreamingResponse`
(`:6380`) — whatever is yielded is transmitted to the client immediately; there is no
buffering or later rewrite of bytes already sent. **The `stream` hook's replacement *is*
relayed to the API caller**: `process_filter_functions(..., filter_type='stream', ...)`
(`:6361-6368`) can replace `event` (and therefore `data`, `:6369`), and that replaced `data` is
exactly what gets `yield`ed at `:6374` — **a stream Filter that swaps a chunk's `delta` does
change what the API client receives**, chunk by chunk, live. The **outlet** call, by contrast,
sits at `:6376-6378`, **strictly after the `async for` loop has exhausted** `original_generator`
— i.e., after every byte has already been yielded (sent). By the time `outlet_filter_handler`
runs, there is no request left to rewrite: **the outlet cannot change SSE bytes already relayed,
proven by its call site's position after the loop that does the relaying**, not merely by
inference.

### 10.3 With no chat to persist, what the outlet handler does with a returned body: nothing at all

`chat_id = ''` (§10.0) makes `is_saved_chat_id(chat_id)` false, so `is_unsaved_chat = True`
inside `outlet_filter_handler` (`middleware.py:3898`). Tracing the same function the main note's
§5.3 already quotes in full:

- **The persist branch never runs**: `if not is_unsaved_chat and messages_map:` (`:3996`) is
  `False and ...` — skipped unconditionally. There is no chat row to write to, so nothing is
  persisted, whatever the guardrail's `outlet()` returned.
- **The emit branch never runs either, and for a stronger reason than "no listener": there is
  no `event_emitter` to call.** `if event_emitter:` (`:4022`) reads `ctx.get('event_emitter')`
  — which is `None` (§10.0), not a callable that reaches an empty Socket.IO room. Contrast the
  UI case: `get_event_emitter`'s inner closure (`socket/main.py:1057-1084`) *would* emit to
  `room = f'user:{user_id}'` (`:1072`) if it ran, reaching nothing unless that same user
  identity also has a live browser tab open elsewhere — but here it is never even constructed,
  so there is no emit call to make, empty room or not.

**So: nothing — not "an event to nobody," but no event at all, and no persistence.** The
guardrail's `outlet()` method still executes and computes a replacement body (the Filter code
itself has no awareness of any of this), but every consumer of that return value is absent for
this caller: no DB row, no socket emit, and (§10.1/§10.2) no wiring into the HTTP response
either.

### 10.4 Which callers the outlet's replacement actually reaches

- **A UI chat turn (a real browser session with a saved chat and a live socket connection)**:
  reached, fully — `event_emitter` is a real callable (`chat_id`/`message_id` both present),
  `is_unsaved_chat` is `False` once the chat is saved, so the outlet's returned body is both
  persisted (`Chats.upsert_message_to_chat_by_id_and_message_id`, main note §5.3) and pushed
  live via `chat:outlet` to the browser (main note §5.4). This is the path the guardrail is
  built for.
- **An API non-streaming call** (this section): **not reached** — the outlet runs, but its
  result is discarded before the HTTP response is built (§10.1); the caller receives the
  original, unmodified completion.
- **An API streaming call** (this section): **not reached, and not reachable in principle** —
  every SSE byte is already on the wire before the outlet ever runs (§10.2); only a `stream`-hook
  edit (a different mechanism, §7) can affect what a streaming API caller sees.

### 10.5 Can a `user`-role account hold an API key under GIDEON's rendered permissions?

**No.** `compose/open-webui/permissions.yaml:67`: `api_keys: false` under `features:` — the
same conclusion `docs/research/owui-preset-system-prompt.md` §6 already reached independently
("users cannot mint API keys under §4.2's set"). Whatever identity holds the bearer key in this
scenario, it is not a plain `user`-role account operating under this permission tree; GIDEON's
render never grants that role the ability to create one.


---

## 11. An inlet gate on the same Filter: what it can see, how the UI addresses it, and how its refusal surfaces

### 11.2 What the browser sends as `session_id`/`chat_id`, and how `main.py` maps them (answered first, per the coordinator)

`Chat.svelte`'s submit payload (`src/lib/components/chat/Chat.svelte:3588-3589`):

```js
session_id: $socket?.id,
chat_id: _chatId || undefined,
```

**`session_id` is the live Socket.IO client's connection id, nothing more** — `$socket` is the
app's socket store; `.id` is assigned by the socket.io client on connect and becomes `undefined`
on disconnect (until a reconnect assigns a new one). A JS `undefined` value is dropped by
`JSON.stringify`, so **while the socket is disconnected, the request simply carries no
`session_id` key at all** — indistinguishable, at the body level, from an API caller who never
sent one.

**`chat_id`, per chat type**:

- **A normal (saved) chat turn**: `_chatId = createdChat.id` (`Chat.svelte:3286`), a real,
  server-issued chat UUID.
- **A temporary chat**: `_chatId = createTemporaryChatId($socket?.id)` (`Chat.svelte:3296`,
  `:3965`), and `createTemporaryChatId` (`src/lib/utils/chatId.ts:5-6`) is literally
  `` `temporary:${sessionId}` ``. **If the socket is disconnected at that moment, `$socket?.id`
  is `undefined`, and the resulting `chat_id` is the literal string `"temporary:undefined"`**
  (template-literal coercion) — a real, slightly odd edge case, not a crash: the string still
  satisfies neither `is_saved_chat_id` nor is malformed enough to error, it just carries no
  usable session correlation.
- **A turn while the socket is disconnected** (either chat type): `session_id` is absent
  (above); `chat_id` is either the real saved UUID (normal chat — unaffected, since it comes
  from the chat's own row, not the socket) or `"temporary:undefined"` (temporary chat, above).

The Python-side mirror, `utils/chat_id.py:15-20`:

```python
def is_saved_chat_id(chat_id: Optional[str]) -> bool:
    return bool(chat_id) and not chat_id.startswith(NON_SAVED_CHAT_ID_PREFIXES)


def is_temporary_chat_id(chat_id: Optional[str]) -> bool:
    return bool(chat_id) and chat_id.startswith(TEMPORARY_CHAT_ID_PREFIXES)
```

(`NON_SAVED_CHAT_ID_PREFIXES = ('temporary:', 'local:', 'channel:')`, `:4-12`.)

**`main.py`'s mapping into `metadata`**, all inside `chat_completion` (`:1087`):

```python
chat_id = form_data.pop('chat_id', None) or ''                       # main.py:1209
...
metadata = {
    ...
    'chat_id': chat_id,                                               # main.py:1247
    ...
    'session_id': form_data.pop('session_id', None),                  # main.py:1251
    ...
}
...
is_new_chat = 'parent_id' in form_data and form_data['parent_id'] is None and not form_data.get('chat_id')   # main.py:1190
...
if is_new_chat:
    metadata['chat_id'] = str(uuid4())                                # main.py:1276-1277
```

`session_id` is never synthesized — it is a bare `.pop(..., None)`, whatever the caller sent
or nothing. `chat_id` synthesis is conditioned on `is_new_chat`, itself conditioned on the
payload literally carrying a `parent_id: null` key with no `chat_id` — a UI convention for
"first message of a brand-new chat," reproducible by any caller, API or otherwise, that sends
the same shape.

**So: none of `session_id`, `chat_id`, or the `is_new_chat` synthesis is a reliable "this came
from the UI" signal for a handler to trust.** All three are ordinary JSON fields (or their
absence) that any caller can supply or omit at will; nothing here is a session token verified
against a live connection, and nothing in the inlet's `extra_params` (§11.1) or `body` exposes
whether `session_id` corresponds to a socket actually connected in `room=f'user:{user_id}'`
(`socket/main.py:1072`, quoted in the main note's §10.3) — that check, if it exists at all, is
socket-server state a Filter has no documented route to from `__request__`/`__metadata__`/
`body`. A gate that wants to key off "arrived through the chat UI" has nothing sturdier to test
than these same conventions — which is exactly why §11.4 confirms an API-key caller need not
even actively hide `session_id`; it can simply not send one, same as a disconnected browser.

### 11.3 How an inlet exception surfaces — answered second, per the coordinator

The inlet call itself re-raises everything as a bare `Exception` (`utils/middleware.py:2631-2642`,
already quoted in the main note's §4.5): `except Exception as e: raise Exception(f'{e}')`. That
propagates out of `process_chat_payload` into `process_chat`'s own try/except
(`main.py:1624-1719`, itself called from the `/api/chat/completions` route,
`main.py:1085-1087`):

```python
        except Exception as e:
            error_detail = e.detail if isinstance(e, HTTPException) else str(e)
            log.error('Error processing chat payload: %s', error_detail)
            if metadata.get('chat_id') and metadata.get('message_id'):
                # Update the chat message with the error
                try:
                    if is_saved_chat_id(metadata.get('chat_id')):
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata['chat_id'],
                            metadata['message_id'],
                            {
                                'parentId': metadata.get('user_message_id', None),
                                'error': {'content': error_detail},
                            },
                        )

                    event_emitter = await get_event_emitter(metadata)
                    if event_emitter:
                        await event_emitter(
                            {
                                'type': 'chat:message:error',
                                'data': {'error': {'content': error_detail}},
                            }
                        )
                        await event_emitter(
                            {'type': 'chat:tasks:cancel'},
                        )

                except Exception:
                    pass
            else:
                # No chat_id/message_id → legacy/direct API path with no
                # WebSocket error channel.  We must surface the error as
                # a proper HTTP response; without this the function would
                # return None which FastAPI serializes as null.  #23924
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_detail,
                )
```

(`main.py:1657-1695`.) **`error_detail = str(e)` for a bare `Exception`** — the inlet Filter's
own raised message text (whatever string it raised with) becomes `error_detail` verbatim; no
wrapping or generic replacement.

**An API caller (no `chat_id`/`message_id` — the eval identity's shape) hits the `else` branch:
`raise HTTPException(400, detail=error_detail)`.** FastAPI serializes this as `HTTP 400` with
body `{"detail": "<the exception's message text>"}` — **yes, the raw text is in the body**, and
the comment even names why this branch exists (bug `#23924`: a bare `None` return would
otherwise serialize as JSON `null`, hiding the failure entirely).

**A UI turn (`chat_id` and `message_id` both present) takes the `if` branch instead — no
HTTPException, no bad HTTP status.** The route's own eventual HTTP response is unaffected (this
branch never raises); the failure is instead: (a) persisted onto the message's `error.content`
field, but only `if is_saved_chat_id(...)` — **a temporary chat's error is never persisted**,
only emitted; and (b) emitted as two socket events, `chat:message:error` (`data.error.content =
error_detail`) then `chat:tasks:cancel`, via `get_event_emitter(metadata)` — the same
unconditionally-real closure (no chat_id/message_id gate on construction here, unlike
`get_event_emitter_and_caller`, §10.0) that emits to `room=f'user:{user_id}'`.

**The browser's handling**: `Chat.svelte:1269-1270`:

```js
} else if (type === 'chat:message:error') {
    message.error = data.error;
}
```

sets `message.error` on the in-memory message; `ResponseMessage.svelte:893-894` then renders
it **inline in the assistant message bubble**, not a toast:

```svelte
{#if message?.error}
    <Error content={message?.error?.content ?? message.content} />
{/if}
```

**No toast is fired for this path** — grepping `Chat.svelte`'s giant socket-event dispatcher
around the `chat:message:error` branch shows no accompanying `toast.error(...)` call. A toast
*does* fire, separately, if the whole HTTP request itself rejects — `Chat.svelte`'s own
`.catch()` on `generateOpenAIChatCompletion` (`:3613-3635`):

```js
).catch(async (error) => {
    ...
    toast.error(`${errorMessage}`);
    responseMessage.error = { content: error };
    responseMessage.done = true;
    ...
    return null;
});
```

— but that only fires for a genuinely failed *fetch* (a non-2xx status, e.g. the API-caller's
400 above, or a network error), which a normal UI turn does not produce for this failure mode
(the `if` branch above never raises). **So for a real browser chat turn, an inlet refusal shows
as the inline `<Error>` banner on the assistant message via the socket event, not a toast** —
and, per §11.2's disconnected-socket case, that banner never appears at all if the socket
happens to be disconnected at the moment the event is emitted (`get_event_emitter`'s inner
closure only actually calls `sio.emit` if `room in sio.manager.rooms.get('/', {})` or the Redis
manager is in use, `socket/main.py:1074-1084`; a disconnected client is in neither).

### 11.1 The inlet's `extra_params`, `__user__`'s fields, and whether a credential type is knowable

**Correction on the citation the coordinator pointed at**: `middleware.py` around `:4241` is
`streaming_chat_response_handler`'s own (near-identical) `extra_params` block, built for that
function's later `stream`-hook calls — not the inlet's. **The actual inlet `extra_params`** is
built earlier, inside `process_chat_payload`, and is the dict `process_filter_functions(...,
filter_type='inlet', ...)` receives at `middleware.py:2635-2642` (main note §4.5). In full
(`middleware.py:2517-2527`):

```python
    extra_params = {
        '__event_emitter__': event_emitter,
        '__event_call__': event_caller,
        '__user__': user.model_dump() if isinstance(user, UserModel) else {},
        '__metadata__': metadata,
        '__oauth_token__': await get_system_oauth_token(request, user),
        '__request__': request,
        '__model__': model,
        '__chat_id__': metadata.get('chat_id'),
        '__message_id__': metadata.get('message_id'),
    }
```

Nine keys, every one available to an inlet handler that declares the matching parameter name
(`get_filter_params`, `utils/filter.py:135-144`, adds a tenth, `__id__`, the Filter's own id).
**`__request__` is offered to the inlet** — confirmed by its presence above.

**`__user__` is `user.model_dump()`** on `UserModel` (`models/users.py:85-115`):

```python
class UserModel(BaseModel):
    id: str
    email: str
    username: str | None = None
    role: str = 'pending'
    name: str
    profile_image_url: str | None = None
    profile_banner_image_url: str | None = None
    bio, gender, date_of_birth, timezone: str | None = None (various)
    presence_state, status_emoji, status_message, status_expires_at: ...
    info: dict | None = None
    variables: dict = Field(default_factory=dict, exclude=True)
    settings: UserSettings | None = None
    oauth: dict | None = None
    scim: dict | None = None
    last_active_at, updated_at, created_at: int
```

So `__user__` carries `id`, `role`, `email`, `name` (all present, as asked), plus profile/status
fields, an `info` dict, `settings`, and raw `oauth`/`scim` provider blobs (unnormalized — no
parsed "groups" list; `variables` is explicitly `exclude=True` so it never appears in the dump
despite being a model field). **There is no `groups` field, and no field of any kind recording
how this particular request was authenticated** — `UserModel` is identical whether the request
came in on a session cookie, a JWT, or an API key.

**Can a handler tell an API-key credential from a session token?** Not from a dedicated field
— but yes, indirectly, from the raw credential string, which the discriminator itself uses
(`utils/auth.py:372-373`):

```python
    # auth by api key
    if token.startswith('sk-'):
        user = await get_current_user_by_api_key(request, token)
```

This is the **only** place the distinction is made, and it is made once, before the Filter
chain ever runs — the result (which branch ran) is not recorded anywhere a Filter can read
except as an OpenTelemetry span attribute (`current_span.set_attribute('client.auth.type',
'api_key')` / `'jwt'`, `utils/auth.py:376-385`, `:423-432`), which is an observability sink, not
exposed via `__request__`/`__user__`/`__metadata__`. A Filter that truly needs this would have
to **re-derive it itself**, via `__request__`, either `request.state.token.credentials` (set by
ASGI middleware, `utils/asgi_middleware.py:125-131`, normalized from the `Authorization` header,
a `token` cookie, or a custom API-key header — indistinguishably, so the *source* header is
already lost by the time `request.state.token` is set) or `request.headers.get('authorization',
'')`, and apply the same `.startswith('sk-')` (well, `Bearer sk-`) check by hand. Nothing built
for this purpose exists.

### 11.4 A bearer-API-key call carries no `session_id` unless the caller sends one — confirmed

`main.py:1251`: `'session_id': form_data.pop('session_id', None)` — a bare pop with default
`None`. Nothing else in `main.py` or `utils/middleware.py` populates `metadata['session_id']`;
unlike `chat_id` (§11.2's `is_new_chat` synthesis), there is no server-side fallback that
manufactures one. **Confirmed: the eval identity's bearer-key call carries `session_id: None`
unless its own request body includes the key.**


## Unverified / out of scope

- §7's `__metadata__` identity claim is inferred from the assignment pattern, not confirmed
  with a runtime trace or a test that mutates `metadata` mid-request and observes the mutation
  visible in a later hook.
- ~~Whether `ENABLE_API_OUTLET_FILTERS` ... is on by default~~ — resolved in §9: default `True`
  (`env.py:1038`), and, as already noted here, irrelevant to the UI chat path regardless, which
  always runs `outlet_filter_handler` unconditionally on the streaming branch (`middleware.py:6278`).
- §8.2 confirms `StructuredOutputRenderer.svelte` reads a `type: 'message'` item's parts through
  the same generic `part.text`-only accessor the server's `get_output_text` uses (no `part.type`
  check on either side) — so a replacement item's parts need the `text` key populated; the
  `type: 'output_text'` label itself is not actually read by either renderer, only conventional.
- ~~`ENABLE_VALVE_ENCRYPTION`'s default~~ — resolved in §9: default `False` (`env.py:752`),
  unset by GIDEON's rendered environment.
- Pipeline outlet filters (`process_pipeline_outlet_filter`, `routers/pipelines.py`) were read
  only enough to confirm they run in the same `outlet_filter_handler` (§5.1, before the native
  Function outlet filters) and are wrapped in their own separate `try/except` that also fails
  open (`middleware.py:3967-3970`) — the pipeline-outlet payload shape itself was not traced;
  out of scope since GIDEON's guardrail is a native Filter Function, not a pipeline.

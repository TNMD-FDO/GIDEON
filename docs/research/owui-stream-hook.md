---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — the `stream` Filter hook: everything a stream-side withholding Filter must know

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (matches `images.lock` and the
commit the two companion notes below already resolved to). Backend paths are relative to
`backend/open_webui/`. `docs.openwebui.com` was not consulted — every claim below is read
directly from this tag's source, and every decisive line is quoted with its path and line
numbers as they stand in this exact commit (a few lines differ by a handful of positions from
the ticket's own approximate numbers — this note gives the numbers found in the tagged tree).

Companion notes, not repeated here except where cited: `docs/research/owui-filter-function.md`
(§4.5 the inlet/outlet failure modes; §5.2 the outlet payload and `extra_params`; §7 the
`stream` hook "briefly" — every claim in that §7 is independently re-traced and either
confirmed or extended below, including the one point it flagged as inferred, not confirmed);
`docs/research/owui-engine-connection.md` (§4 the reasoning delta accumulation and
`DEFAULT_REASONING_TAGS`; §1/§6 the Chat-Completions-vs-Responses-API connection switch).
GIDEON's own `compose/open-webui/functions/arithmetic_guardrail.py` was read for context (its
current `inlet`/`outlet` shape, `replace_message`'s five-key `output` item, and the
`SessionRefusal`/fail-closed pattern ticket 10's `stream` hook will extend) but is not itself a
primary source for Open WebUI's behaviour.

---

## 1. The `stream` hook's call and its extra parameters, both paths

### 1.1 The UI path (`process_chat_response` → `streaming_chat_response_handler`)

The per-chunk `extra_params` are **not** built at the stream-hook call site itself — they are
built once, earlier, as the base `extra_params` for the whole streaming response, and then
extended with one more key right before the SSE loop. The base dict,
`utils/middleware.py:4233-4243` (`streaming_chat_response_handler`, before the nested
`response_handler` closure is even defined):

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

**Nine keys.** This closure variable is what `response_handler` (defined a few lines later,
`:4257`) captures. Immediately before the per-line SSE loop, `filter_extra_params` adds one
more key on top of exactly this dict, `utils/middleware.py:4800`:

```python
                    filter_extra_params = {'__body__': form_data, **extra_params} if filter_functions else None
```

**`form_data` here is `ctx['form_data']`** — the outer request body sent to the engine for
this turn, captured once by closure (`streaming_chat_response_handler`'s own
`form_data = ctx['form_data']`, `:4219`), not the per-chunk SSE payload; it is the same object
for every chunk of the stream. So **the `stream` hook's `extra_params`, UI path, is ten keys**:
`__body__`, `__event_emitter__`, `__event_call__`, `__user__`, `__metadata__`,
`__oauth_token__`, `__request__`, `__model__`, `__chat_id__`, `__message_id__`. The call site,
`utils/middleware.py:4838-4849`:

```python
                        try:
                            data = JSONCodec.loads(data)

                            if filter_functions:
                                data, _ = await process_filter_functions(
                                    request=request,
                                    filter_context=filter_context,
                                    filter_functions=filter_functions,
                                    filter_type='stream',
                                    form_data=data,
                                    extra_params=filter_extra_params,
                                )
```

This confirms and extends the companion note's §7 finding — `__body__` is offered to `stream`
alone among the three hooks (outlet's own six-key `extra_params`, quoted in that note at
§5.2, has no `__body__`); the UI stream hook additionally gets `__oauth_token__`, `__chat_id__`
and `__message_id__`, none of which are in outlet's set either.

### 1.2 The API path (`stream_wrapper`)

`stream_wrapper` is the `else` branch of `streaming_chat_response_handler`'s own
`if event_emitter:` gate (`utils/middleware.py:4249` vs. `:6318`) — i.e. it runs precisely when
`get_event_emitter_and_caller(metadata)` (`:3119-3131`) found no `chat_id`+`message_id` pair to
build a socket emitter for, which is the shape of the eval identity's API call. Both branches
are mutually exclusive per request (one request takes exactly one), but `stream_wrapper` is
defined in the same enclosing function and closes over the **same** `extra_params` object built
at `:4233` — reused **verbatim, with no `__body__` added**:

```python
            for event in events:
                event, _ = await process_filter_functions(
                    request=request,
                    filter_context=filter_context,
                    filter_functions=filter_functions,
                    filter_type='stream',
                    form_data=event,
                    extra_params=extra_params,
                )
```
(`utils/middleware.py:6336-6344`, the pre-stream inlet-produced-events replay) and again for
every network chunk, `:6349-6368`:
```python
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
                                    request=request,
                                    filter_context=filter_context,
                                    filter_functions=filter_functions,
                                    filter_type='stream',
                                    form_data=event,
                                    extra_params=extra_params,
                                )
```

**Answer: `__body__` is not there; `__metadata__` is.** The API path's `extra_params` is the
same nine-key dict as §1.1's base dict — `__metadata__` present, `__body__` absent — with one
caveat worth flagging even though it doesn't change what a `stream` handler can *receive*:
because this is the branch taken when `event_emitter` is falsy, the dict's own
`'__event_emitter__': event_emitter` and `'__event_call__': event_caller` entries are `None` in
this branch (the values were computed once at `:3119-3131` before the branch split); a
`stream` handler on this path that declares `__event_emitter__` as a parameter receives `None`,
not a working emitter. (`getattr(request.state, 'direct', False)` also gates whether an
`'event'` key inside a filter-returned chunk gets replayed to `event_emitter` at all on the UI
path, `:4849` — irrelevant here since this whole call already sits in the branch where there is
no socket to emit to.)

Also worth noting: this path checks for `[DONE]` **before** parsing (`if payload and payload !=
'[DONE]':`), so unlike the UI path's exception-driven detection (§5(e) below) there is no risk
of a `[DONE]`-shaped exception reaching the hook here either — the effect (hook never called for
`[DONE]`) is the same on both paths, by different mechanisms.

### 1.3 How the handler's own signature decides what it receives

`utils/filter.py:135-144`, `get_filter_params`:

```python
def get_filter_params(sig, filter_id, filter_type, form_data, extra_params):
    params = {'event': form_data} if filter_type == 'stream' else {'body': form_data}
    return params | {
        k: v
        for k, v in {
            **extra_params,
            '__id__': filter_id,
        }.items()
        if k in sig.parameters
    }
```

- **The chunk parameter is named `event` for a `stream` handler** (`body` for inlet/outlet) —
  confirmed exactly as the companion note's §7 already found.
- **Selection is by exact parameter name, checked with `k in sig.parameters`.** `sig.parameters`
  is `inspect.signature(handler).parameters` (`utils/filter.py:190`), a mapping keyed by each
  declared parameter's own name. **A `**kwargs` catch-all does not work as a substitute for
  declaring the dunder names explicitly**: if a `stream` handler is `def stream(self, event,
  **kwargs)`, `sig.parameters` contains the keys `'event'` and `'kwargs'` — `'__user__' in
  sig.parameters` is `False` regardless of the `**kwargs` presence, because the check is a
  literal string match against parameter *names*, and `VAR_KEYWORD` parameters are keyed by
  their own name (`'kwargs'`), never by the names of what they might absorb at call time. **Only
  a handler that spells out each dunder name it wants (`__metadata__`, `__body__`, `__user__`,
  …) as its own named parameter receives that value**; anything not named is silently omitted
  (not passed as `None`, simply never a key in the call).
- **`event` is passed as a keyword argument, not positionally.** `run_filter_handler`
  (`utils/filter.py:159-162`):
  ```python
  async def run_filter_handler(handler, params):
      if inspect.iscoroutinefunction(handler):
          return await handler(**params)
      return handler(**params)
  ```
  `handler(**params)` — a plain keyword-argument call. This works transparently for the normal
  case (`def stream(self, event, __metadata__=None, ...)`, a positional-or-keyword parameter
  accepting a keyword call), but a `stream` method declared with `event` as
  positional-only (`def stream(self, event, /, ...)`) would raise a `TypeError` here — not
  exercised anywhere in this codebase's own Filters, flagged as a real constraint on how the
  guardrail must declare its signature.
- `handler` is a **bound method** on the loaded `Filter()` instance
  (`getattr(function_module, filter_type, None)`, `utils/filter.py:180`), so `self` is already
  bound and never appears in `sig.parameters` — nothing above needs to account for it.

---

## 2. `__metadata__` identity across the three hooks of one request

**Confirmed by tracing every assignment between the object's construction and each hook's call
site — no copy, no rebuild, no `{**metadata}`, anywhere on the path.** The companion note's §7
flagged this as inferred from a reference-only pattern; this section traces every branch named
there plus the object's origin and every downstream reader, and finds the same conclusion with
no exception.

**Construction — one dict, once per HTTP request.** `main.py:1243` (inside the chat-completion
route, well before any hook runs):
```python
        metadata = {
            'user_id': user.id,
            ...
        }
```
followed later by `request.state.metadata = metadata` and `form_data['metadata'] = metadata`
(`main.py:~1610`, immediately preceding the `try/except` that defines `process_chat`) — the
same object is stashed on both the live `Request` object and the outbound `form_data` at this
point, still before any inlet/stream/outlet hook has run.

**Inlet — a parameter, mutated, never reassigned.** `process_chat_payload(request, form_data,
user, metadata, model)` (`utils/middleware.py:2365`) takes `metadata` as a parameter and returns
it unchanged as an object (`return form_data, metadata, events`); grepping the full body
(`:2365`–its return) for `metadata = ` or `metadata = {` finds only key-level mutations
(`metadata['chat_id'] = ''`, `metadata['system_prompt'] = ...`, thirteen more like it) — **never
a rebinding of the name `metadata` to a new object**. The caller, `main.py`'s `process_chat`:
```python
            form_data, metadata, events = await process_chat_payload(request, form_data, user, metadata, model)
```
(`main.py:1626`) — since `process_chat_payload` never rebinds `metadata` internally, the value
returned and reassigned here is the identical object passed in.

**Into `ctx`, once.** `build_chat_response_context` (`utils/middleware.py:3134-3146`):
```python
async def build_chat_response_context(request, form_data, user, model, metadata, tasks, events):
    event_emitter, event_caller = await get_event_emitter_and_caller(metadata)
    return {
        ...
        'metadata': metadata,
        ...
    }
```
— a plain dict-literal assignment of the reference, called once per turn
(`main.py`'s `process_chat`: `ctx = await build_chat_response_context(request, form_data, user,
model, metadata, tasks, events)`).

**Every downstream reader reads `ctx['metadata']` directly — four call sites, one pattern.**
Grepping `utils/middleware.py` for `metadata = ctx['metadata']` finds exactly these:
`background_tasks_handler` (`:3658`), `outlet_filter_handler` (`:3885`),
`non_streaming_chat_response_handler` (`:4037`), `streaming_chat_response_handler` (`:4225`).
Every one is a bare subscript read, never `{**ctx['metadata']}` or `.copy()` or `deepcopy(...)`.
`streaming_chat_response_handler`'s own read at `:4225` is what feeds the `__metadata__` entry
of the `extra_params` dict quoted in §1.1 above — the **same** object `process_chat_payload`
mutated during inlet, unpacked into `ctx` once, and read back by `outlet_filter_handler` and
`background_tasks_handler` after the stream ends.

**A global grep for any copy of this object turns up nothing on the flowing path.** The only
two `{**metadata` occurrences in the whole file are unrelated, one-off, short-lived dicts built
for a single event-emit call and immediately discarded — `main.py:1504`:
`await emit_chat_list_event({**metadata, 'message_id': user_message['id']}, chat_id)` and
`main.py:1843`: `{**metadata, 'message_id': message_ids[0]['message_id']}` — neither reassigns
the tracked `metadata` variable that flows into the hooks; both build a new, separate dict for
one notification and throw it away.

**Conclusion, now confirmed rather than inferred**: `__metadata__` in the `stream` hook's
`extra_params` (§1.1/§1.2) is the exact same dict object the inlet Filter's `__metadata__`
parameter received and mutated, and the exact same object `outlet_filter_handler` later reads
as `__metadata__` for the outlet call. A key the guardrail writes during inlet is visible to
every later `stream` call of the same turn and to that turn's outlet call; a key a `stream` call
writes on chunk N is visible to `stream` on chunk N+1 and to the outlet — this is exactly the
mechanism ticket 10's per-request lag-window state depends on.

**One boundary to flag**: this identity holds for **one HTTP request** (one `metadata =
{...}` construction at `main.py:1243`). A *new* chat turn constructs a brand-new `metadata`
dict from scratch — nothing above claims or implies persistence across turns; see §3 for what
that means for the task calls made after a turn's own hooks have run.

---

## 3. Is `metadata` serialized, persisted, logged, or transmitted anywhere after the hooks can write into it?

Short answer: **it is logged (a real, decisive finding, at `DEBUG` level, on every turn
including a task sub-call), it is spread — as a shallow copy — into every post-answer task
call's own payload, and from there it is popped back off before the actual HTTP POST reaches
the engine; it is not written to the `Chats` DB table as an object, and it does not reach the
socket `chat:outlet`/`chat:completion` events (those carry other fields, not `metadata`
itself).** Each path, in the order metadata can flow after inlet/stream have first been able to
write to it:

### 3.1 The debug log — a real, unconditional leak of whatever the Filter wrote

`utils/chat.py:151-158`, the very top of the shared `generate_chat_completion` dispatcher every
chat-completion call (the main turn **and** every task sub-call) funnels through:
```python
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    bypass_filter: bool = False,
    bypass_system_prompt: bool = False,
):
    log.debug('generate_chat_completion: %s', form_data)
```
By the time the main turn's own request reaches this line, `form_data['metadata']` is already
the live, inlet-mutated `metadata` object (attached at `main.py`'s `process_chat_payload`,
`utils/middleware.py:2840`: `form_data['metadata'] = metadata`) — so a Filter's inlet writes are
in this log line at `DEBUG`. §3.2 below shows this same log line also fires, with the object
recopied, for every title/tags/follow-up task call made *after* the turn's stream/outlet hooks
have written to it. **This is gated on `GLOBAL_LOG_LEVEL=DEBUG`**, not on by default, but it is
the one place in this codebase an arbitrary custom key on `__metadata__` reaches a sink outside
the request/response cycle with no filtering at all.

### 3.2 Task calls (title, tags, follow-ups, …) do receive a copy of `__metadata__` — confirmed, not merely plausible

Every task route in `routers/tasks.py` builds its own `payload['metadata']` as an explicit
shallow spread of `request.state.metadata` — the same object §2 traced. `generate_title`,
`routers/tasks.py:181-190`:
```python
    payload = {
        'model': task_model_id,
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'metadata': {
            **(request.state.metadata if hasattr(request.state, 'metadata') else {}),
            'task': str(TASKS.TITLE_GENERATION),
            'task_body': form_data,
            'chat_id': form_data.get('chat_id', None),
        },
    }
```
Grepping `routers/tasks.py` for `request.state.metadata` finds this exact `{**request.state.metadata,
...}` pattern at eight sites — `:189` (title), `:254` (follow-ups), `:319` (tags), `:378`
(image-prompt), `:455` (queries), `:531` (retrieval query), `:586` (autocomplete), `:641`
(emoji) — **every task route does this**, unconditionally, whether or not `tasks.py` itself
ever explicitly threads a `metadata` parameter through its own call signature. Since
`background_tasks_handler` (§2's trace) calls these routes with a hand-built payload that has
**no** `metadata` key of its own (`{'model': message['model'], 'messages': messages,
'message_id': ..., 'chat_id': ...}`, `utils/middleware.py:3708-3716` for follow-ups and
similarly for title/tags), it is specifically **this spread inside the task route itself**, not
anything `background_tasks_handler` does, that reattaches the shared object's contents.

**Because `request.state` is the live FastAPI `Request` for the whole HTTP call**, and
`background_tasks_handler` runs *after* `outlet_filter_handler` in the same call
(`utils/middleware.py:6275-6277`: `await outlet_filter_handler(ctx); await
background_tasks_handler(ctx)`), **a key the guardrail's `outlet` wrote into `__metadata__`
moments earlier is present in `request.state.metadata` when `generate_title` builds its own
payload** — the spread is a shallow copy taken *after* the outlet ran. This directly answers the
ticket's question: **yes, task calls made after the answer do see the guardrail's key** — as a
copy, in the task's own `payload['metadata']`, which (like the main turn) is logged once at
`utils/chat.py:158`'s `DEBUG` line and then popped back off before the network call (§3.3). The
task's *messages* themselves (what the engine actually reasons over) never include this
metadata — only the outer `payload` dict briefly carries it.

### 3.3 Stripped before the network call to vLLM — confirmed, both for the main turn and for tasks

`routers/openai.py:1490` (the `/chat/completions` route, the only route a plain OpenAI-compatible
connection's `generate_chat_completion` reaches — see the engine-connection note §1):
```python
    payload = {**form_data}
    metadata = payload.pop('metadata', None)
```
This runs at the very top of the function, before any further payload shaping. **`metadata` is
removed from `payload` here and never added back to it** before the outbound
`aiohttp` POST is built (grepped the rest of the function: `metadata` is used only as a local
variable, passed into two helper calls — see §3.4 — never re-inserted as a payload key). So
**the object itself, and any arbitrary key a Filter wrote onto it, never reaches the JSON body
POSTed to `gideon-generator`** — for the main turn and identically for every task sub-call
(title, tags, …), which route through this exact same function.

The one **narrow, config-gated exception is `generate_direct_chat_completion`**
(`utils/chat.py:45-58`, the browser-direct-WebSocket-to-model path, not GIDEON's server-side
connection): `metadata = form_data.pop('metadata', {})` there too pops it off before the
remaining `form_data` is sent over the socket channel — same strip, different function, not a
counter-example. GIDEON's connection to `gideon-generator` is the server-side OpenAI-compatible
one (per `docs/research/owui-engine-connection.md` §1), so `generate_direct_chat_completion` is
not on GIDEON's path at all; noted only because it is the one other place a `metadata` pop
exists, and it pops the same way.

### 3.4 The two narrow places `metadata` content *can* still shape the outbound request or headers — both fixed-key allowlists, neither a generic reflection

Before being popped, `metadata` (the local variable, not the payload key) is passed into two
helpers at `routers/openai.py:1508` (`apply_system_prompt_to_body(system, payload, metadata,
user)`) and `:1583` (`get_headers_and_cookies(request, url, key, api_config, metadata,
user=user)`).

- **`resolve_system_prompt`** (`utils/payload.py:17-40`) reads exactly two metadata keys:
  ```python
      if metadata:
          system = render_chat_variables(system, metadata.get('chat_variables', {}), required=False)
      ...
      if metadata:
          variables = metadata.get('variables', {})
          if variables:
              system = prompt_variables_template(system, variables)
  ```
  Only `metadata['chat_variables']` and `metadata['variables']` are ever read, and only to
  substitute named template placeholders (e.g. `{{name}}`) that the **admin's own system-prompt
  template** must already reference — a Filter writing an unrelated custom key (anything not
  named `chat_variables` or `variables`) is never read here. And since §2 established `metadata`
  is rebuilt fresh every turn (`main.py:1243`), even a Filter that *did* write into
  `metadata['variables']` could not leak into a **later** turn's system prompt this way — only
  into the same turn's own request, which for the `stream`/outlet hooks is already too late
  (the request to the engine for that turn has already been sent by the time those hooks run).
- **`parse_custom_headers`** (`utils/headers.py:100-140`) substitutes a fixed, hard-coded
  allowlist of template tokens (`{{CHAT_ID}}`, `{{MESSAGE_ID}}`, `{{USER_MESSAGE_ID}}`,
  `{{FILE_ID}}`, `{{TASK}}`, `{{USER_ID}}`, …, `utils/headers.py:124-135`) into custom header
  *values* — and only runs at all when the admin has configured `headers` on that connection's
  `OPENAI_API_CONFIGS` entry (`routers/openai.py`'s `if config.get('headers')...` gate,
  §5.2/§1.1 of the engine-connection note already establishes GIDEON's config has none). No
  generic metadata dump, no custom-key passthrough — same allowlist shape as
  `resolve_system_prompt`.

**Conclusion for §3.4**: neither path can be used to smuggle an arbitrary Filter-written
`__metadata__` key into the outbound engine request or its headers; both read a small, named,
hard-coded set of keys the guardrail would have to deliberately collide with.

### 3.5 The `Chats` DB, socket events, and the Pipelines HTTP-filter path

- **Not written to `Chats` as an object.** Grepped every `Chats.upsert_message_to_chat_by_id_and_message_id`
  and `Chats.update_chat_title_by_id`/`update_chat_tags_by_id` call in `utils/middleware.py`:
  none ever writes a `metadata` field into the message/chat row — the writes are always
  `content`, `output`, `done`, `usage`, `error`, `selectedModelId`, `followUps`, or a title/tags
  string (see §5(h)/§7 below for the exact shapes). `metadata` (or a Filter's custom key on it)
  is never itself a column value.
- **Not in socket events.** The only two socket event types this pipeline emits that carry
  message content are `chat:completion` (built from `content_parts`/`output`/`usage`/`error`,
  never `metadata`) and `chat:outlet` (built from `outlet_result['messages']`, the outlet's
  **return value**, not `__metadata__` — see the companion note §5.3). Grepped for
  `event_emitter(` calls whose `data` includes the literal `metadata` key: none.
- **Not sent anywhere by `process_pipeline_inlet_filter`, for GIDEON's setup.** Every task route
  calls `process_pipeline_inlet_filter(request, payload, user, models)` (a *different*,
  HTTP-based "Pipelines"-project filter mechanism, unrelated to native Filter Functions) before
  stripping `stream`; that function (`routers/pipelines.py:63-121`) does:
  ```python
      sorted_filters = get_sorted_filters(model_id, models)
      ...
      if not sorted_filters:
          return payload
  ```
  With no Pipelines-attached (`urlIdx`-bearing) filter configured for `gideon-generator` — true
  for GIDEON's plain vLLM connection, confirmed already in `docs/research/owui-engine-connection.md`
  §3 — `sorted_filters` is empty and the function returns `payload` (`metadata` key and all)
  **unchanged, with no network call made**. This is a real path where `payload['metadata']`
  *would* be POSTed to a third-party Pipelines server if one were ever attached — flagged for
  awareness, not exercised by GIDEON's current configuration.
- **`review_memory_after_turn`** (`utils/memory.py:409-436`, called from
  `background_tasks_handler` with `metadata=metadata` — the object itself, no copy) reads only
  `metadata.get('features')` and is gated behind `memories.background_review.enable` (off unless
  an admin turns memory review on); not a generic sink either, and out of scope for GIDEON's
  guardrail unless that feature is enabled.

**Net answer to the ticket's question**: a dict of strings/ints the guardrail stores under one
custom key of `__metadata__` cannot reach a DB row, a socket event, or the actual HTTP request
body sent to `gideon-generator` — every one of those sinks either never reads `metadata` at all
or reads a small fixed set of unrelated key names. The one real, unconditional leak is the
`DEBUG`-level `log.debug('generate_chat_completion: %s', form_data)` line, which fires once for
the main turn and once more per task call made afterward (title, tags, follow-ups, …), each time
carrying a full (recopied, for tasks) snapshot of `__metadata__` including whatever the
guardrail wrote.

---

## 4. The sync-handler execution model

**Called inline, in the current coroutine — not offloaded to a thread pool.** `run_filter_handler`,
`utils/filter.py:159-162`, in full:
```python
async def run_filter_handler(handler, params):
    if inspect.iscoroutinefunction(handler):
        return await handler(**params)
    return handler(**params)
```
For a **synchronous** `stream` handler (the `else` branch, `return handler(**params)`), this is
a **plain, blocking function call** — no `run_in_threadpool`, no `asyncio.to_thread`, no
`loop.run_in_executor` anywhere in `utils/filter.py` or on the call path from
`process_filter_function` (`utils/filter.py:199`: `form_data = await run_filter_handler(handler,
params)` — the `await` here awaits the *coroutine* `run_filter_handler` itself, not any
offloading inside it; for a sync handler `run_filter_handler`'s body never actually suspends).
Grepped `utils/filter.py` end to end for `to_thread`/`run_in_threadpool`/`executor`: zero hits.

**Consequence for the per-chunk CPU budget**: the guardrail's `stream` handler runs **on the
single asyncio event loop thread**, once per SSE chunk, for its full duration, **blocking every
other coroutine on that loop** (other requests' I/O, other chunks' event emission, the websocket
heartbeat, etc.) for exactly as long as the handler takes. This is the same execution model
`process_filter_function`'s inlet/outlet calls use (same function, same lack of offload) — the
`stream` hook is not special-cased for concurrency, it is simply called far more often (once per
chunk instead of once per turn), so a per-chunk cost that would be negligible for inlet/outlet
becomes the dominant cost multiplied by chunk count for `stream`.

---

## 5. What the UI stream handler does with the chunk the hook returns

Everything below is the code immediately after the `stream` hook call quoted in §1.1
(`utils/middleware.py:4838-4849`), inside the same `try:` block, still per SSE line.

### 5(a) One delta with both `reasoning` and `content`: reasoning appended, reasoning item closed, then content appended — confirmed, in that order, from a single chunk

The extraction, `utils/middleware.py:5169-5178` (comment included — it is itself evidence the
platform expects a `stream` filter to reshape delta values):
```python
                                    # content and reasoning deltas are raw JSON: a stream filter can make them any type
                                    value = delta.get('content')
                                    if value and not isinstance(value, str):
                                        value = f'{value}'

                                    reasoning_content = (
                                        delta.get('reasoning_content')
                                        or delta.get('reasoning')
                                        or delta.get('thinking')
                                    )
```
**`content` (`value`) is read first, `reasoning`/`reasoning_content`/`thinking` second** — but
extraction order is not processing order. The processing that follows (lines 5196-5303) handles
`reasoning_content` **before** `value`, in this sequence, all still within one iteration of the
per-line loop for one chunk:
1. `if reasoning_content or (...):` (`:5196`) — creates a `type: 'reasoning'` output item if
   none is open, or reuses the existing one, and appends `reasoning_content` to it
   (`reasoning_item['content']`, via the same append-last-part-or-push-new-part pattern the
   engine-connection note's §4 already documents for this item).
2. `if value:` (`:5276`) — **only entered when `content` is truthy** (see §5(b) immediately
   below) — closes the still-open reasoning item (`status: 'completed'`, `ended_at`, `duration`
   computed) and opens a fresh `type: 'message'` item.
3. Further down (`:5330-5395`, not reproduced in full here — the same shape §8.1 of the
   companion filter-function note already quotes for `append_output_text`), `value` is appended
   into that (now current) message item's text, `tag_output_handler` runs over it (§9 below),
   and a `response.output_text.delta` (or `.reasoning_text.delta`, decided by `target_item.get('type')`)
   event is queued.

**So yes — for one chunk carrying both fields, the exact order is: reasoning text appended to
the reasoning item, the reasoning item closed out, then the content text appended to a new
message item — entirely from that single chunk**, matching the ticket's suspected order exactly.

### 5(b) What closes the reasoning item: the first non-empty `content`, not an empty-string `content: ""`

`utils/middleware.py:5276-5288`:
```python
                                    if value:
                                        if (
                                            output
                                            and output[-1].get('type') == 'reasoning'
                                            and output[-1].get('attributes', {}).get('type') == 'reasoning_content'
                                        ):
                                            reasoning_item = output[-1]
                                            reasoning_item['ended_at'] = time.time()
                                            reasoning_item['duration'] = int(
                                                reasoning_item['ended_at'] - reasoning_item['started_at']
                                            )
                                            reasoning_item['status'] = 'completed'
```
`if value:` is a truthiness check on the Python string `value = delta.get('content')` (possibly
stringified — `:5171-5172`). **An explicit `delta: {"content": ""}` chunk is falsy and never
enters this block** — the reasoning item stays open (`status: 'in_progress'`) through such a
chunk. Only a chunk whose `content` is a non-empty string closes it. This is a decisive,
literal-code answer, not an inference from behaviour.

### 5(c) `finish_reason`: never read, never needed

Grepped `utils/middleware.py` end to end for `finish_reason`: **zero occurrences**. The
streaming handler determines "no more chunks" purely from the SSE generator ending (or the
`[DONE]` sentinel, §5(e)) — `choices[0].get('finish_reason')` is simply never inspected anywhere
in this file. A `stream` handler that trips on `finish_reason == 'stop'` to decide "this is the
last real chunk" is inventing its own signal; the platform code around it never looks at that
field at all.

### 5(d) A `choices: []` + `usage` chunk (`stream_options.include_usage`): merged and emitted, `choices` ignored, no delta processing, then `continue`

`utils/middleware.py:4952-4989` (the `else:` branch for a non-`response.*`-typed,
non-`event`/`selected_model_id` chunk):
```python
                                else:
                                    choices = data.get('choices', [])

                                    # Normalize usage data to standard format
                                    raw_usage = data.get('usage', {}) or {}
                                    raw_usage.update(data.get('timings', {}))  # llama.cpp
                                    if raw_usage:
                                        usage = merge_usage(usage, raw_usage)
                                        await event_emitter(
                                            {
                                                'type': 'chat:completion',
                                                'data': {
                                                    'usage': usage,
                                                },
                                            }
                                        )

                                    if not choices:
                                        error = data.get('error', {})
                                        if error:
                                            ...
                                        continue

                                    delta = choices[0].get('delta', {})
```
A trailing usage chunk with `choices: []` and a `usage` object: `raw_usage` is truthy, so
`usage` is merged and a `chat:completion`/`usage` socket event is emitted **unconditionally,
whether or not `choices` is empty** — then `if not choices:` is true, no `error` key is present,
so the block simply `continue`s — **no `delta` is read, no content/reasoning accumulation
happens for this chunk.** Note the **`stream` hook already ran on this exact chunk** before this
branch is reached (§1.1's call site sits above all of this) — so a `choices: []`/`usage`-only
chunk **is** one of the shapes the hook must tolerate (§8).

**Does the UI path ever request `include_usage`?** Yes, conditionally — `main.py:1173-1175`,
set for *any* streaming request (UI or API) whose model record advertises usage reporting:
```python
        # Providers only report token counts when asked, so ask on every caller's behalf.
        if form_data.get('stream') and model_capabilities.get('usage'):
            form_data['stream_options'] = {**(form_data.get('stream_options') or {}), 'include_usage': True}
```
This is set once per turn in `main.py`, before `process_chat_payload` runs, independent of
which of §1.1/§1.2's branches later handles the response — **not verified in this pass**
whether `gideon-generator`'s own rendered model record sets `capabilities.usage: true` by
default (an `owui-model-record.md`-scope question); flagged rather than assumed.

### 5(e) `data: [DONE]`: the hook is never called for it — confirmed by the exact exception path, not by a dedicated check

There is **no explicit `if data == '[DONE]': ...` branch** on the UI path. Instead, the entire
per-line body — the stream-hook call included — sits inside one `try:` block,
`utils/middleware.py:4838` through the matching `except` at `:5449`:
```python
                        try:
                            data = JSONCodec.loads(data)

                            if filter_functions:
                                data, _ = await process_filter_functions(...)
                            ...
                        except (asyncio.CancelledError, KeyboardInterrupt):
                            raise
                        except Exception as e:
                            done = 'data: [DONE]' in line
                            if done:
                                pass
                            else:
                                log.debug('Error: %s', e)
                                continue
```
`JSONCodec.loads('[DONE]')` (after the `data:` prefix is stripped, `:4836`) raises a JSON-decode
error **before** the `if filter_functions:` line is ever reached — the stream hook's call sits
strictly downstream of the parse, inside the same `try`, so it is never invoked for this line.
The broad `except Exception as e:` catches the decode failure, checks the **original** `line`
(not the failed-to-parse `data`) for the literal substring `'data: [DONE]'`, and if found simply
`pass`es (no `continue`, no `break` — control falls through to the end of the `except` and the
enclosing `async for line in response.body_iterator:` loop proceeds to its next iteration, which
normally ends because the upstream generator is exhausted right after `[DONE]`). `done` is a
local, otherwise-unused variable (grepped the rest of the function: it is read nowhere else) —
its only effect is choosing `pass` (silent) vs. `log.debug(...); continue` (a genuine parse
failure on a non-`[DONE]` line).

**This is also §6's mechanism for a raised `stream` handler exception** — see below.

### 5(f) The role chunk (`delta: {"role": "assistant", "content": ""}`): silently absorbed, no explicit handling exists

Grepped `utils/middleware.py` for any read of `delta.get('role')` or `delta['role']`: **zero
occurrences** anywhere in the streaming handler. A role-announcement chunk with `content: ""`
and no `reasoning`/`reasoning_content`/`thinking` key: `value = delta.get('content')` is `''`
(falsy), `reasoning_content` is `None` (falsy) — neither the reasoning-append branch (§5(a)
step 1) nor the content/reasoning-close branch (§5(b)) is entered, so the chunk produces no
output-item mutation, no event emission, nothing — it is fully absorbed with no observable
effect once past the stream hook (which, per §1.1, still receives and can inspect/replace it,
since it runs before any of this delta logic).

### 5(g) `chat:completion`/"done" events are built only from the accumulated, already-filtered `content_parts`/`output` — confirmed, no other field carries unfiltered text

The final completion event, `utils/middleware.py:6238-6245`:
```python
                current_output = full_output()
                title = await Chats.get_chat_title_by_id(metadata['chat_id']) if save_to_chat else ''
                data = {
                    'done': True,
                    'output': current_output,
                    'title': title,
                    **({'usage': usage} if usage else {}),
                }
```
`current_output` is `full_output()` (`prior_output + output` or `output`, `:4596-4597`) — the
same structured array §5(a)–(b) built exclusively from post-stream-hook `delta.content`/
`delta.reasoning` values (the hook runs before any of that accumulation, §1.1). Every
`chat:completion`/`response:completion` event emitted during the stream (the delta events at
`:5405-5420`, the usage event at §5(d), the final `done` event above) is built from `output`,
`content_parts`, or `usage` — never from a separate, unfiltered copy of the upstream chunk.
Grepped the whole streaming handler (`:4257`–`:6260`) for `content_blocks`/`'sources'`/
`'replace'` as event-payload keys: `content_blocks` appears only inside a code comment
(`:4267`, documenting that the *old* mechanism it replaced used content_blocks — confirming this
one no longer does); `'replace'` appears only as Python's `str.replace(...)` method, never an
event `type`; `sources` (`:5879`) is populated from `metadata.get('sources', [])` — retrieval/RAG
citations computed once at inlet time, unrelated to and never overwritten by the assistant's
generated text. **No side channel exists in this backend flow through which text the `stream`
hook withheld could reach the browser** — everything the client receives is built from the same
accumulator the hook already filtered.

### 5(h) DB persistence during streaming: one final write, not incremental — the Redis-backed resume cache is separate from the `Chats` table

The comment at the point of the real (Chats-table) write says it outright,
`utils/middleware.py:6246-6258`:
```python
                if save_to_chat:
                    # Save final output once. The delta path keeps in-progress
                    # state in response_streams instead of writing tokens to DB.
                    await Chats.upsert_message_to_chat_by_id_and_message_id(
                        metadata['chat_id'],
                        metadata['message_id'],
                        {
                            'done': True,
                            'output': current_output,
                            **({'usage': usage} if usage else {}),
                        },
                    )
```
**`Chats` (the permanent DB table) is written exactly once, at normal completion**, with `done`,
`output`, and (if present) `usage` — **`content` is not one of the keys in this write**. What
*does* happen periodically during the stream, via `save_current_response_stream()`
(`utils/middleware.py:4693-4711`, called at several points including inside
`flush_pending_delta_data`), is a write to a **Redis-backed (or in-process fallback) resume
buffer**, `tasks.py`'s `save_response_stream` (`tasks.py:172-192`):
```python
async def save_response_stream(redis, task_id, chat_id, message_id, content, output):
    ...
    data = {'chat_id': chat_id, 'message_id': message_id, 'content': content, 'output': output}
    if redis:
        await redis.hset(REDIS_RESPONSE_STREAMS_KEY, task_id, dumps_bytes(data))
        ...
    else:
        response_streams[task_id] = data
```
— keyed by `REDIS_RESPONSE_STREAMS_KEY`/`task_id`, used to let a reconnecting client resume an
in-flight stream, **not** a `Chats` row. Since `content`/`output` fed into this are
`joined_content = ''.join(content_parts)` and `current_stream_output = full_output()`
(`:4700-4711`) — both already post-stream-hook accumulators — **a reconnect during the stream
replays the already-filtered content, never a raw pre-hook copy.**

**What the outlet payload's `messages[-1].content`/`output` are built from**: the `ctx['assistant_message']`
handed to `outlet_filter_handler`, `utils/middleware.py:6269-6273`:
```python
                ctx['assistant_message'] = {
                    'content': ''.join(content_parts) or get_output_text(current_output),
                    'output': current_output,
                    **({'usage': usage} if usage else {}),
                }
                await outlet_filter_handler(ctx)
```
— the same `content_parts`/`current_output` accumulators, one more time. (The companion note's
§5.2 already traces exactly how this feeds `outlet_data['messages'][-1]`; this section confirms
the values it starts from are the filtered-stream accumulators, not a separate raw copy, and
that the DB's `content` column for a streamed message is otherwise never set outside the outlet
path — see the companion note's own flag on this in its §5.6.)

---

## 6. Failure mode of the `stream` hook

Per `docs/research/owui-filter-function.md` §4.5, `process_filter_function`
(`utils/filter.py:188-205`) always re-raises on any exception from the handler call, logging at
`log.exception` for a non-inlet `filter_type` (i.e. for `stream` too, same `else` branch as
outlet). What each caller does with that re-raised exception **differs sharply between the two
paths**.

### 6.1 UI path (`streaming_chat_response_handler`): fails open **per chunk**, not per stream — the chunk is dropped, the stream continues

The exception propagates out of `process_filter_functions` at the exact call site quoted in
§1.1 (`utils/middleware.py:4842-4849`), which sits inside the same `try:`/`except` block already
quoted in full at §5(e):
```python
                        try:
                            data = JSONCodec.loads(data)
                            if filter_functions:
                                data, _ = await process_filter_functions(
                                    ..., filter_type='stream', form_data=data, extra_params=filter_extra_params,
                                )
                            ...
                        except (asyncio.CancelledError, KeyboardInterrupt):
                            raise
                        except Exception as e:
                            done = 'data: [DONE]' in line
                            if done:
                                pass
                            else:
                                log.debug('Error: %s', e)
                                continue
```
A raised exception from the guardrail's own `stream()` method is **not** the `[DONE]` sentinel
(`line` is the real chunk text, so `'data: [DONE]' in line` is `False`), so it takes the
`else:` branch: `log.debug('Error: %s', e); continue`. **Effect: this one SSE line's processing
is abandoned entirely** — nothing from this chunk is appended to `content_parts`/`output`, no
event is emitted for it, no persistence happens for it (since the stream hook is the *first*
thing done with `data` after parsing, §1.1) — **but the `async for line in
response.body_iterator:` loop simply proceeds to the next line.** The stream is not aborted, no
error event reaches the browser for this specific failure, and the user sees a stream with one
chunk's worth of text silently missing rather than a truncated or error-terminated response.
This is neither "abort the whole stream" nor "pass the chunk through unfiltered" — it is a
third outcome: **drop this chunk, keep streaming.**

### 6.2 API path (`stream_wrapper`): no try/except at all around the hook call — the whole stream aborts

`utils/middleware.py:6336-6368` (quoted in full in §1.2) has **no surrounding try/except of any
kind** around either `process_filter_functions` call (the pre-stream `events` replay or the
per-chunk one). A raised exception here propagates directly out of the `stream_wrapper` async
generator while it is being iterated by Starlette's `StreamingResponse` machinery. Since this
generator is what the HTTP response body is streamed from, an unhandled exception mid-generator
terminates that generation — the ASGI response send loop sees the exception and the connection
is torn down (headers having already been sent for an SSE stream, there is no clean "error JSON"
response to substitute; the effect on the wire is an abruptly closed connection, not a graceful
`[DONE]` or an OpenAI-style `error` object). **This is a materially more severe failure mode than
the UI path**: on the API path (the eval identity's streaming calls), a `stream` handler
exception does not merely drop one chunk — it kills the entire response stream. A guardrail
implementation that is safe to let occasionally raise on the UI path (§6.1's per-chunk fail-open)
is not equally safe on the API path, where the same raise ends the whole call.

---

## 7. Stop / cancellation

**The outlet does not run on the Stop/cancel path — confirmed by code, UI path only (the API
path has no cancellation handling of its own, see below).**

**How Stop reaches the running stream**: the whole per-turn `process_chat(...)` coroutine
(which the UI's `streaming_chat_response_handler`/`response_handler` runs inside, awaited
directly rather than as a separate task, `utils/middleware.py:6313`: `return await
response_handler(response, events)`) is itself registered as a cancelable asyncio Task at a
higher level — `main.py:1824`: `task_id, _ = await create_task(...)` with
`task_id=per_model_metadata['task_id']`, the same `task_id` that was placed onto `metadata['task_id']`
at construction time (`main.py:1792`). The Stop button's `POST /api/tasks/stop/{task_id}`
(`main.py:2101-2104`) calls `stop_task(request.app.state.redis, task_id)`
(`tasks.py:232-260`), which — in the non-Redis, single-process case — does:
```python
    task = tasks.pop(task_id, None)
    ...
    task.cancel()  # Request task cancellation
    try:
        await task  # Wait for the task to handle the cancellation
    except asyncio.CancelledError:
        # Task successfully canceled
```
`task.cancel()` raises `asyncio.CancelledError` at whatever `await` the task is currently
suspended on inside the streaming loop — caught by `streaming_chat_response_handler`'s own
`except asyncio.CancelledError:` (`utils/middleware.py:6278-6298`, in full):
```python
            except asyncio.CancelledError:
                log.warning('Task was cancelled!')

                if hasattr(response, 'body_iterator') and hasattr(response.body_iterator, 'aclose'):
                    try:
                        await asyncio.shield(response.body_iterator.aclose())
                    except (asyncio.CancelledError, Exception):
                        pass

                async def save_cancelled_state():
                    await event_emitter({'type': 'chat:tasks:cancel'})
                    if save_to_chat:
                        await Chats.upsert_message_to_chat_by_id_and_message_id(
                            metadata['chat_id'],
                            metadata['message_id'],
                            {
                                'done': True,
                                'output': full_output(),
                            },
                        )
                    await clear_response_stream(request.app.state.redis, response_stream_task_id)

                try:
                    await asyncio.shield(save_cancelled_state())
                except (asyncio.CancelledError, Exception):
                    pass
                raise  # re-raise CancelledError for proper propagation
```
**`outlet_filter_handler(ctx)` is not called anywhere in this branch** — contrast the normal
completion path immediately above it in the same function
(`utils/middleware.py:6273-6277`, quoted already at §5(h)): `await outlet_filter_handler(ctx);
await background_tasks_handler(ctx)`, both **absent** from the cancellation branch. Instead:

- The DB write is a **direct** `Chats.upsert_message_to_chat_by_id_and_message_id` with
  `{'done': True, 'output': full_output()}` — **no `content` key**, exactly the same two-key
  shape as the normal-completion DB write (§5(h)), and **`full_output()` is `prior_output +
  output`**, i.e. whatever the accumulator held at the moment of cancellation — already reflecting
  every `stream`-hook rewrite/withholding applied to chunks processed before the cancel, since
  filtering happens before accumulation (§1.1) either way.
- The socket event emitted is `{'type': 'chat:tasks:cancel'}` — **not** `chat:outlet`, and
  carries no message content at all.
- `clear_response_stream` removes the Redis/in-process resume-buffer entry (§5(h)) for this
  task, so a reconnect after a genuine cancel finds nothing to resume from.
- `CancelledError` is **re-raised** at the end (`raise`), for correct propagation up through
  whatever awaited this coroutine.

**Consequence for ticket 10's outlet-side flush**: because `outlet_filter_handler` never runs on
this path, **the outlet's logic for flushing a still-held lag-window tail on a clean stream that
ended without a finish chunk cannot run on a Stop-cancelled stream** — the held tail is simply
never written anywhere; the persisted message ends at whatever was already released into
`output` before the cancellation. There is no code path by which a cancel could be mistaken for
"clean end" by the outlet, because the outlet is not invoked at all in this branch — but by the
same token, a genuinely held tail (text the guardrail's lag window was still sitting on when Stop
was pressed) is **lost**, not flushed, not shown, on a cancelled stream. If ticket 10 wants a
cancelled stream to still show its held tail, that flush would have to be added into
`save_cancelled_state` itself (or the guardrail would have to accept that a cancel simply drops
whatever it was holding) — **not proven to happen anywhere in this tag's own code**; flagged as
a gap the ticket should decide on deliberately rather than assume away.

**The API path (`stream_wrapper`) has no cancellation handling of its own at all** — no
`except asyncio.CancelledError` anywhere in that function (§1.2/§6.2's full quote). A
disconnect or cancellation on that path would propagate as an unhandled `CancelledError` through
the async generator, which Starlette's ASGI machinery handles generically (tearing down the
response); the trailing `if has_api_outlet_filters and assistant_message: ... await
outlet_filter_handler(ctx)` line after the `async for` loop (`utils/middleware.py:6376-6378`)
is simply never reached if the generator is cancelled mid-iteration, for the same structural
reason as §6.2. The eval identity's API stream has no "Stop" concept in this codebase distinct
from an ordinary client disconnect.

### Addendum, 2026-09-17 (slice-1 ticket 52): the hook cancels the turn itself

The cancel this section describes as the composer's is reachable from inside the hook. The six
facts the triage read on the pinned sources (Open WebUI v0.11.3, vLLM v0.27.1), with this note's
own line references:

1. **The task id is the branch discriminator.** A request carrying a session id and a chat id
   runs `process_chat` as a registered task whose id the frontend also writes onto the request's
   metadata (`main.py:1792`, read back at `main.py:1824`). The direct branch — the harness's
   managed turns and raw replays, which send no session id — runs inside the HTTP request's own
   task, with no task id on the metadata and no cancel handling at all (§7's "The API path"
   paragraph), so cancelling there tears the response down.
2. **The hook runs inside that task.** The sync `stream` handler is called inline in the
   streaming loop (`process_filter_functions`, `utils/middleware.py:4842-4849`), so
   `asyncio.current_task()` inside the hook is the frontend's chat task on the browser path —
   the very task `stop_task` cancels.
3. **A cancel from inside lands at the next suspension.** The loop processes the hook's return
   only when it is truthy, so a dropped chunk's next suspension is the next read; the frontend's
   cancel branch then shields an `aclose()` of the body iterator, emits `chat:tasks:cancel`,
   writes `{done: true, output: <prior + accumulated>}` to the chat, and re-raises. The outlet
   is not in that branch, and the stored `content` is filled from the output's text.
4. **The engine aborts at its next scheduler step**, its client gone
   (`async_llm.py`'s `except (CancelledError, GeneratorExit): abort`).
5. **No event the hook can emit makes the stored message the refusal alone** — a `replace` event
   writes `content` only, never `output`, and the client renders the output's text — so a
   whole-message replacement on this path would need a write through the frontend's chat model.
   Declined: the released prefix never held a matched span, so prefix-then-refusal is doctrine.
6. **The trip's record already rides the hook** (slice-1 ticket 11, `v0.1.34`):
   `_record_stream_trip` dispatches the row at the trip, before the refusal chunk returns, on a
   daemon thread. The cancel comes after the record by construction.

**The "genuinely held tail" gap above does not apply to the hook's own cancel.** That gap is
real for a composer Stop, which can land while the lag window still holds text. Under the hook's
cancel there is no held tail: the trip discards the texts and the refusal leaves in the trip's
own chunk, before the chunk that cancels. Nothing is lost that a person had not already been
shown.

---

## 8. Chunk shapes the hook must recognise

With a plain vLLM Chat Completions upstream, no Pipes, and no Tools, the shapes below are what
can reach the `stream` hook's `event` parameter on the UI path (`process_filter_functions`,
`utils/middleware.py:4842-4849`, called on every parsed SSE line before any shape-specific
branching happens at `:4870` onward). Every shape is checked **after** `JSONCodec.loads`
succeeds — a line that fails to parse (including `[DONE]`, §5(e)) never reaches the hook at all.

1. **The ordinary Chat Completions delta chunk** — `{"id": ..., "choices": [{"index": 0, "delta":
   {"content": "..."} | {"reasoning": "..."} | {}, "finish_reason": null | "stop" | ...}], ...}`.
   The mainline shape; everything in §5 assumes this.
2. **A `choices: []` chunk carrying `usage`** — the trailing chunk produced when
   `stream_options.include_usage` is set (§5(d)); `{"id": ..., "choices": [], "usage": {...}}`
   (optionally with a llama.cpp-style `"timings"` key, also merged — `:4954`). Confirmed reaching
   the branch at `utils/middleware.py:4952` **after** the stream-hook call.
3. **An error chunk shaped as a normal SSE `data:` line** — `data: {"error": {...}}\n\n` parses
   successfully (it is valid JSON), so it **does** reach the stream hook; only *after* the hook
   runs does the `if not choices: error = data.get('error', {})` branch (`:4956-4960`) detect and
   handle it. (A different shape — a bare JSON error line **without** the `data:` prefix, some
   upstreams' way of reporting a connection-level failure — is handled at
   `utils/middleware.py:4808-4820`, entirely **before** the `data:`-prefix check and thus **never**
   reaches the stream hook; distinguish "an error inside a normal SSE event" (hook sees it) from
   "a raw non-SSE error line" (hook does not).)
4. **A `{"selected_model_id": ...}` meta-chunk** — the Arena-model-resolution announcement,
   checked right after the `if data:` gate (`utils/middleware.py:4850` region, `if
   'selected_model_id' in data: ...`). Structurally unrelated to `choices`; the hook sees this
   whole dict as `event` and must not assume `choices` exists.
5. **A `{"event": {...}}` wrapper chunk** — from a Pipe function's own event emission
   (`if 'event' in data and not getattr(request.state, 'direct', False): await
   event_emitter(data.get('event', {}))`, immediately after the `if data:` gate). **Not produced
   by vLLM or by GIDEON's plain connection** (no Pipes configured), but the mechanism exists in
   the same code path the hook sits in front of — a hook that only recognises `choices`-shaped
   dicts would silently mishandle this shape if a Pipe were ever introduced later.
6. **A Responses-API-shaped `{"type": "response...."}` event** — handled by a wholly separate
   branch (`elif data.get('type', '').startswith('response.'):`, `:4872` onward,
   `handle_responses_streaming_event`). **Not produced by a Chat Completions upstream** (vLLM's
   raw SSE chunks carry no top-level `type` key at all, so `data.get('type', '')` is `''` and
   this branch is never taken for GIDEON's connection) — included here only because the same
   `stream` hook call sits upstream of this branch too, so the hook technically could receive
   this shape if `api_type: 'responses'` were ever configured (§8's second half explains why
   that is not GIDEON's default).

**A guardrail hook should therefore fail closed on anything that is not shape (1) or (2)** (a
`choices`-bearing dict with a `delta`, or an empty-`choices`-plus-`usage` dict) — shapes (3)–(6)
either need no text-withholding action (usage-only, selected-model-id, error) or cannot occur
under GIDEON's actual configuration (Pipe events, Responses-API events) but exist in the shared
code path regardless.

### The Responses API connection kind — exists at v0.11.3, config-gated, not GIDEON's default

Confirmed directly (re-verified independently of the engine-connection note, which already
covers this in more depth at its §1): `routers/openai.py:1567`:
```python
    is_responses = api_config.get('api_type') == 'responses'
```
selects between `POST {url}/responses` (with `convert_to_responses_payload`) and the default
`POST {url}/chat/completions` a few lines later (`:1573-1601`). `api_type` is one field of a
per-connection `OPENAI_API_CONFIGS[idx]` entry (`config.py:308-364`'s `OPENAI_API_CONFIGS`
parsing, quoted in full in the engine-connection note §1).

**With `OPENAI_API_CONFIGS` absent from the environment**: `config.py:347-355`:
```python
    OPENAI_API_CONFIGS = {}
    _openai_api_configs = os.getenv('OPENAI_API_CONFIGS', '')
    if _openai_api_configs:
        try:
            parsed = JSONCodec.loads(_openai_api_configs)
            ...
```
An unset/empty env var leaves `_openai_api_configs == ''`, so the `if` body never runs and
`OPENAI_API_CONFIGS` stays `{}`. `get_openai_connection(idx)` (`routers/openai.py:340-345`) then
does `api_config = api_configs.get(str(idx), api_configs.get(url, {}))` — with `api_configs ==
{}`, both lookups miss and `api_config` is `{}`. `{}.get('api_type')` is `None`, `is_responses`
is `False` — **the connection is Chat Completions.** This matches exactly what GIDEON's own test
already asserts: `tests/test_render_owui.py:166-167`:
```python
        for name in ("OPENAI_API_CONFIGS", "TASK_MODEL", "TASK_MODEL_PARAMS", "TEMPERATURE"):
            self.assertNotIn(name, env)
```
and the render's own comment naming the same reasoning, `gideon/host/render/owui.py:317-319`:
```python
        # One Chat Completions connection, discovered from the engine's model
        # list (no OPENAI_API_CONFIGS: its absence is what keeps the request
        # shape Chat Completions and the discovery real); ...
```
So: shape (6) above is a real code path in this tag, gated behind an env var GIDEON's render
deliberately never sets, confirmed absent by GIDEON's own test — not something the guardrail's
`stream` hook needs to actually handle for GIDEON's deployment, only something it should not
crash on if it ever saw it.

---

## 9. The frontend's own reasoning-tag detection on `content`

**Confirmed: yes, a second, independent tag-detection mechanism scans the accumulated `content`
text (not `delta.reasoning`) for `<think>`-style tags on every content delta, on by default,
running in the backend (not "frontend" despite the ticket's phrasing — this is
`utils/middleware.py`, the Python streaming handler) — and it is safe against a lag-window split
at an arbitrary character boundary, because it always operates on the item's whole accumulated
text with a rewinding scan cursor, not on the current delta in isolation.**

**It runs, gated the same way the engine-connection note's §4 already found**, right after a
content delta is appended to the current message item's text, `utils/middleware.py:5397-5411`:
```python
                                        if DETECT_REASONING_TAGS:
                                            output, _ = tag_output_handler(
                                                'reasoning',
                                                reasoning_tags,
                                                output,
                                            )

                                            output, _ = tag_output_handler(
                                                'solution',
                                                DEFAULT_SOLUTION_TAGS,
                                                output,
                                            )
```
`DETECT_REASONING_TAGS = reasoning_tags_param is not False` (`:4624`) — on unless a per-request
`params.reasoning_tags` is explicitly `False`; `reasoning_tags` defaults to
`DEFAULT_REASONING_TAGS` (`:4648-4652`, the same eight tag pairs the engine-connection note
quotes, `<think>`/`</think>` first).

**It scans `content`, specifically**: `tag_output_handler`'s own body
(`utils/middleware.py:4350-4353`) only proceeds `if last_type == 'message':` — i.e. only when
the current (last) output item is a `type: 'message'` item, which is exactly the item type
`content` deltas build (§5(a)); it is never invoked against a `type: 'reasoning'` item under
construction, so `delta.reasoning`/`reasoning_content` text (already routed straight to its own
dedicated reasoning item, per §5(a) step 1 and the engine-connection note §4) is **not**
re-scanned for tags — the two mechanisms are complementary and non-overlapping, exactly as the
engine-connection note inferred, now confirmed at the exact call site.

**How it buffers — the decisive answer to the split-boundary question**: `tag_output_handler`
does **not** operate on the current delta string; it re-reads the **entire accumulated text of
the current output item** on every call and tracks a per-item scan cursor that always rewinds
far enough to catch a tag whose opening bracket was already scanned as plain text last time,
`utils/middleware.py:4350-4364`:
```python
                if last_type == 'message':
                    # Use the output item's own text for tag detection
                    item = output[-1]
                    item_text = get_last_text(output)
                    scanned_length = get_scanned_length(item, item_text)
                    max_start_tag_length = max((len(start_tag) for start_tag, _ in tags), default=1)
                    search_start = max(0, scanned_length - max_start_tag_length + 1)

                    if scanned_length and any(
                        start_tag.startswith('<') and start_tag.endswith('>') for start_tag, _ in tags
                    ):
                        open_tag_start, last_tag_boundary = get_tag_boundaries(item, item_text, scanned_length)
                        if open_tag_start > last_tag_boundary:
                            search_start = min(search_start, open_tag_start)
```
`item_text = get_last_text(output)` (`:4353`) pulls the **whole** accumulated `output_text` of
the current message item — i.e. everything the `stream` hook has released into it so far across
every prior chunk, not just this chunk's `value`. `search_start` is computed as `scanned_length -
max_start_tag_length + 1` — always re-examining the last `(longest start tag − 1)` characters
already scanned, specifically so a tag whose opening `<` landed right at the end of a previous
scan is not missed; `get_tag_boundaries` further widens `search_start` back to any dangling
unclosed `<` found before the last real tag boundary. The actual `re.search(...)` for each start
tag then runs over `item_text` (the full string) from `search_start` onward
(`utils/middleware.py:4364` onward, `match = re.compile(_start_tag_pattern(start_tag)).search(item_text,
search_start)`).

**Consequence for the guardrail's lag window**: because detection runs against the item's
cumulative released text with this rewind margin, **a withheld/rewritten `content` delta split
at an arbitrary character boundary cannot cause the scanner to miss a tag that straddles the
split** — the two halves are concatenated into `item_text` by the ordinary
`append_output_text` accumulation (§5(a)/companion note §8.1) before this scanner ever runs, and
the rewind window exists precisely to handle a tag whose start was scanned (as plain text) in an
earlier, smaller `item_text` and now needs to be re-examined once more characters have arrived.
The only effect of a split is a one-chunk **delay** in when the tag is recognised (it cannot be
found until the chunk containing its closing character has been released and appended) —
inherent to any incremental scanner, not a defect the guardrail's specific choice of split point
introduces. **Not independently exercised with a live split-boundary test in this pass** — this
conclusion is derived directly from the quoted mechanics, not from running the code, so it is
flagged as a from-source deduction rather than an on-box observation.

---

## Summary of what is confirmed vs. flagged as inferred/unverified

**Confirmed directly from this tag's source** (the great majority of this note): the ten/nine-key
`extra_params` split between the UI and API `stream` call sites (§1); `__kwargs`-catch-alls do
not receive dunder params (§1.3); `__metadata__` object identity across inlet/stream/outlet for
one request, traced through every assignment (§2); the debug-log leak and the task-call spread-
and-strip round trip for `__metadata__` (§3); the sync `stream` handler runs inline, unoffloaded
(§4); the exact order/closing condition for a mixed reasoning+content delta, `finish_reason`
never read, the usage-chunk and `[DONE]` handling, DB persistence being a single final write
(§5); the UI path's per-chunk fail-open vs. the API path's whole-stream-abort on a raised `stream`
exception (§6); the outlet's absence from the Stop/cancel branch on the UI path, and the API
path's total lack of cancellation handling (§7); the enumerated chunk shapes and the
`OPENAI_API_CONFIGS`-absent-means-Chat-Completions default, cross-checked against GIDEON's own
render and test (§8); the reasoning-tag scanner's item-level, rewind-margin buffering (§9).

**Flagged as not independently verified in this pass**: whether `gideon-generator`'s own
rendered model record sets `capabilities.usage: true` (§5(d), an `owui-model-record.md`-scope
question, not re-checked here); the split-boundary conclusion in §9 is a deduction from the
quoted mechanics, not exercised against a live, deliberately-split stream in this pass.

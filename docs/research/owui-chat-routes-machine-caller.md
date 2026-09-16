---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
---
# Open WebUI v0.11.3 — the chat/chats routes for a machine caller with a session token

Source of truth: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`, matches
`images.lock`'s `open-webui` pin: `ghcr.io/open-webui/open-webui:v0.11.3`). Backend paths are
relative to `backend/open_webui/`; frontend paths to `src/`. `docs.openwebui.com` was not
consulted anywhere below — every claim is read directly from this tag's source, and every
decisive line is quoted.

Scenario throughout: `python3 -m tools.turns` signs in as `gideon-eval` (role `user`) via
`POST /api/v1/auths/signin`, holds the returned **session token** (a JWT, not an API key) in
memory, and drives a managed turn: `POST /api/chat/completions` with `stream: true`,
`parent_id: null`, no `chat_id`, a `user_message`, and a top-level `id` — the shape
`.scratch/slice-1/issues/05-frontend-engine-connection.md` proved on the box.

Companion notes, not repeated here except where cited: `docs/research/owui-filter-function.md`
§5 (the outlet's exact payload and persistence recipe — `originalContent`, the `output_changed`
gate), §10 (the eval identity's `event_emitter = None` fork when `chat_id`/`session_id` are
absent — the bearer-key/no-chat scenario, distinct from this note's managed-turn scenario where
a `chat_id` *is* synthesized), §11 (the inlet's `session_id`/`chat_id` conventions, and how an
inlet exception surfaces as an HTTP 400 versus a socket event); `docs/research/owui-engine-connection.md`
§4 (the `reasoning`/`reasoning_content` delta fields and the structured `output` item, confirmed
again below at the completion handlers that build it after a non-streaming/converted response).

---

## 1. The new-chat path: what gets created, whose ids are used, what a missing `user_message` does, and the response body

### 1.1 `is_new_chat`, and how the route reads the payload

`main.py:1190`:

```python
is_new_chat = 'parent_id' in form_data and form_data['parent_id'] is None and not form_data.get('chat_id')
```

Requires the literal key `parent_id` present and `None`, and no `chat_id`. GIDEON's payload
(`parent_id: null`, no `chat_id`) satisfies this.

Ids are read off the payload, not minted, except for the chat id itself:

- **Assistant message id** — the single-model fallback branch of the `message_ids` parsing
  (`main.py:1196-1206`), taken because the payload sends no `message_ids` array:

  ```python
  message_ids = form_data.pop('message_ids', None)
  ...
  else:
      # Single-model fallback
      message_ids = [{'model_id': model_id, 'message_id': form_data.pop('id', None)}]
  ```

  **The caller's top-level `id` becomes the assistant message's id** — popped verbatim, never
  replaced by a server-minted uuid.

- **User message id** — `user_message = form_data.pop('user_message', None) or form_data.pop('parent_message', None)`
  (`main.py:1208`), then `'user_message_id': user_message.get('id') if user_message else None`
  (`main.py:1249`). **`user_message.id` is read from the payload verbatim**, never minted.

- **Chat id** — the only id the server mints on this path: `main.py:1276-1277`,
  `if is_new_chat: metadata['chat_id'] = str(uuid4())`. (`chat_id = form_data.pop('chat_id', None) or ''`
  at `main.py:1209` stays `''` until this line overwrites `metadata['chat_id']`.)

### 1.2 `user_message.childrenIds` is overwritten, not read

`main.py:1330-1348`, inside the `is_new_chat` branch (only entered `if metadata.get('chat_id') and user:`,
`main.py:1246`, which is true here since `chat_id` was just synthesized):

```python
user_message = metadata.get('user_message') or {}
user_message_id = user_message.get('id') if user_message else None

history_messages = {}
all_assistant_ids = [entry['message_id'] for entry in message_ids if entry.get('message_id')]

if user_message_id and user_message:
    user_message['childrenIds'] = all_assistant_ids
    history_messages[user_message_id] = user_message
```

**`user_message['childrenIds'] = all_assistant_ids` unconditionally overwrites** whatever
`childrenIds` the payload sent, with the list of assistant message ids computed from
`message_ids` (i.e., `[<the top-level id>]` for a single-model call). GIDEON's payload already
sends `childrenIds: [<assistant id>]`, so the overwrite happens to be a no-op in value, but it
is a genuine overwrite, not a read: a caller that sent a different (or empty) `childrenIds`
would have it silently replaced.

### 1.3 The chat record `Chats.insert_new_chat` builds

Continuing the same block (`main.py:1350-1394`):

```python
for entry in message_ids:
    target_model_id = entry['model_id']
    assistant_message_id = entry['message_id']
    if assistant_message_id:
        assistant_message = {
            'id': assistant_message_id,
            'parentId': user_message_id,
            'childrenIds': [],
            'role': 'assistant',
            'content': '',
            'done': False,
            'model': target_model_id,
            'timestamp': int(time.time()),
        }
        ...
        history_messages[assistant_message_id] = assistant_message

await Chats.insert_new_chat(
    chat_id,
    user.id,
    ChatForm(
        chat={
            'id': chat_id,
            'title': 'New Chat',
            'models': [entry['model_id'] for entry in message_ids],
            'history': {
                'currentId': all_assistant_ids[0] if all_assistant_ids else user_message_id,
                'messages': history_messages,
            },
            'messages': [
                {'role': 'user', 'content': user_message.get('content', '')},
            ]
            if user_message_id
            else [],
            'files': metadata.get('files') or [],
            'tags': [],
            'timestamp': int(time.time() * 1000),
        },
        variables=chat_variables,
        folder_id=metadata.get('folder_id'),
    ),
)
```

So: `title` is always the literal string `'New Chat'` at creation (any title-generation runs
later, asynchronously, only if `background_tasks.title_generation` is truthy — §5); `models` is
the list of model ids from `message_ids` (one entry, `gideon-general`, here); `history.messages`
holds the user-message dict (as sent, plus the overwritten `childrenIds`) and one assistant
placeholder (`content: ''`, `done: False`, no `output` key yet); `history.currentId` is the
assistant id; the flat `chat.messages` list holds **one bare `{role, content}` dict with no id**
— see §3.4 below for why this flat list never grows past this point.

`Chats.insert_new_chat` itself (`models/chats.py:540-591`) stores `form_data.chat` as the
`chat` column verbatim (`self._clean_null_bytes(form_data.chat)`, `models/chats.py:557`) and
`title` from `form_data.chat['title']` (`:552-554`) — no field renaming, no id re-derivation.
It also dual-writes every `history.messages` entry into a separate `chat_message` table
(`models/chats.py:572-589`) — out of scope for the read-back this note is built around, since
`GET /api/v1/chats/{id}` (§3) reads the `chat` JSON column, not this table.

### 1.4 What happens if `user_message` is absent with `parent_id: null`

Trace the same code with `user_message = None`:

- `metadata['user_message_id']` is `None` (`main.py:1249`).
- Inside the block: `user_message = metadata.get('user_message') or {}` → `{}`; then
  `user_message_id = user_message.get('id') if user_message else None` — **`user_message` is
  `{}`, which is falsy in Python, so the whole ternary evaluates to `None` regardless of what
  `.get('id')` would have returned** (`main.py:1341`).
- `if user_message_id and user_message:` is `False` — **no entry is added to `history_messages`
  for a user message at all.**
- The assistant placeholder is still built and added, with `'parentId': user_message_id` = `None`.
- `'currentId': all_assistant_ids[0] if all_assistant_ids else user_message_id` — still the
  assistant id, since `all_assistant_ids` is non-empty (from `message_ids`, populated from the
  top-level `id` regardless of `user_message`).
- `'messages': [...] if user_message_id else []` — **`[]`**: the flat `chat.messages` list is
  created empty.

**Net effect: a chat is created whose `history.messages` contains only the empty assistant
placeholder (no user message anywhere in the record — neither in `history.messages` nor in the
flat `messages` list), and whose `models` list still reflects the payload's `model`.** Nothing
raises; `is_new_chat` and the insert both proceed. This is a real, reachable edge case for a
caller that forgets `user_message`, not a 400.

### 1.5 The response body — really `null` for `stream: true`, the actual completion for `stream: false`

Two conditions gate everything downstream: `event_emitter` (real iff `metadata['chat_id']` and
`metadata['message_id']` are both set, `get_event_emitter_and_caller`, `utils/middleware.py:3120-3135`,
quoted in full in `owui-filter-function.md` §10.0) and the **fan-out gate**,
`main.py:1775-1776`:

```python
# Fan out: one task per model
if metadata.get('session_id') and metadata.get('chat_id'):
```

**This is the one place `session_id` actually changes control flow on the new-chat path.**
GIDEON's payload sends no `session_id` key, so `form_data.pop('session_id', None)` (`main.py:1251`)
leaves `metadata['session_id']` as `None` — falsy — so the fan-out branch (which schedules each
model as a Redis-tracked background task and returns immediately with
`{'status': True, 'task_ids': [...], 'chat_id': chat_id}`, `main.py:1846-1850`) is **not** taken.
Control falls to the `else` at `main.py:1856-1857`:

```python
else:
    # Legacy/direct: single model, synchronous
    metadata['message_id'] = message_ids[0]['message_id']
    return await process_chat(request, form_data, user, metadata, model, tasks)
```

This is the branch GIDEON's harness actually exercises: **synchronous**, in-request completion
— exactly what makes "read the stream, then read the stored chat back" meaningful (nothing is
still running in the background when the HTTP response returns).

`process_chat` (`main.py:1624-1727`) returns `await process_chat_response(response, ctx)`
(`main.py:1727`), which dispatches on `isinstance(response, StreamingResponse)`
(`utils/middleware.py:6387-6400`) to `streaming_chat_response_handler` or
`non_streaming_chat_response_handler`.

**Streaming (`stream: true`)** — with `event_emitter` real (chat_id + message_id both set),
`streaming_chat_response_handler` (`utils/middleware.py:4217-4315`) takes the
`if event_emitter:` branch (`:4252`), the "Standard streaming response handler," and returns
`await response_handler(response, events)` (`:6315`). `response_handler` is a large nested
closure that streams, accumulates `output`, persists via
`Chats.upsert_message_to_chat_by_id_and_message_id` (`:6251-6259`), calls
`outlet_filter_handler(ctx)` (`:6278`) and `background_tasks_handler(ctx)` (`:6279`), and then —
after `if response.background is not None: await response.background()` (`:6313-6314`) — **ends
with no `return` statement**, so it implicitly returns `None`. That `None` propagates back
through `streaming_chat_response_handler` → `process_chat_response` → `process_chat` → the
FastAPI route, which serializes it as HTTP `200` with body **`null`**. Confirmed by the code
path itself, not by inference from an absent return type annotation.

**Non-streaming (`stream: false`)** — `non_streaming_chat_response_handler`
(`utils/middleware.py:4033-4197`) also takes its `if event_emitter:` branch (`:4049`), performs
the same persistence/outlet/background-task sequence, and then (`:4173`):

```python
response = build_response_object(response, merge_events_into_response(response_data, events))
...
return response
```

**The response body is the actual completion** — `response_data` (the parsed upstream
completion, `get_response_data(response)` at `:4042`) merged with any `events` — returned to the
caller, not `null`. So the coordinator's framing is exactly right: `stream: true` on this
synchronous path returns `null`; `stream: false` returns the completion JSON.

---

## 2. Where the chat id is exposed, and the list/search routes

### 2.1 No header, no `metadata.chat_id` in the body — confirmed absent

`main.py`'s `/api/chat/completions` route sets no custom response header anywhere (`grep` for
`response.headers[` in `main.py` turns up only the CORS header at `main.py:310` and file-download
headers unrelated to this route) and, per §1.5, the body is literally `null` on the streaming
path. **The caller cannot learn `chat_id` from this response at all — it must list its chats.**

### 2.2 `GET /api/v1/chats/` and `GET /api/v1/chats/list` — the same route

`routers/chats.py:248-249`:

```python
@router.get('/', response_model=list[ChatTitleIdResponse])
@router.get('/list', response_model=list[ChatTitleIdResponse])
async def get_session_user_chat_list(
    request: Request,
    user=Depends(get_verified_user),
    page: int | None = None,
    include_pinned: bool | None = False,
    include_folders: bool | None = False,
    sort_by: str = 'updated_at',
    sort_dir: str = 'desc',
    db: AsyncSession = Depends(get_async_session),
):
```

Both paths are the **same function** — `/` and `/list` are interchangeable. `user=Depends(get_verified_user)`
— any `user`- or `admin`-role account (`VERIFIED_USER_ROLES = {'user', 'admin'}`,
`utils/auth.py:520`), no admin requirement.

**Response item** (`models/chats.py:304-311`):

```python
class ChatTitleIdResponse(BaseModel):
    id: str
    title: str
    updated_at: int
    created_at: int
    last_read_at: int | None = None
    snippet: str | None = None
    active: bool = False
```

**Ordering**: default `sort_by='updated_at'`, `sort_dir='desc'` — `chat_list_order`
(`models/chats.py:105-111`) returns `(Chat.updated_at.desc(), Chat.id)` for the non-unread sort
mode, i.e. **newest `updated_at` first**, chat id as tiebreak.

**Pagination**: `page` query param, 1-indexed. `routers/chats.py:262-274`: when `page` is given,
`limit = 60; skip = (page - 1) * limit`; when `page` is omitted (`None`), **no limit or offset is
applied at all** — `get_chat_title_id_list_by_user_id` (`models/chats.py:1546-1590`) only calls
`.offset()`/`.limit()` `if skip:`/`if limit:` (`:1573-1576`), both falsy when `None` — so a plain
`GET /list` with no `page` returns **every** matching chat.

**Filtering** (`models/chats.py:1557-1570`, inside `get_chat_title_id_list_by_user_id`):

```python
stmt = select(Chat.id, Chat.title, Chat.updated_at, Chat.created_at, Chat.last_read_at).filter_by(user_id=user_id)
stmt = stmt.where(Chat.meta['internal'].as_boolean().is_not(True))
if not include_folders:
    stmt = stmt.filter_by(folder_id=None)
if not include_pinned:
    stmt = stmt.filter(or_(Chat.pinned == False, Chat.pinned == None))
if not include_archived:
    stmt = stmt.filter_by(archived=False)
```

**`filter_by(user_id=user_id)` — a `user`-role caller sees only their own chats**, confirmed
directly (no cross-user branch in this query at all, unlike `get_chat_by_id_for_user` in §3).
**Pinned chats are excluded by default** (`include_pinned` defaults `False` in the route
signature, `routers/chats.py:254`); so are folder-nested chats and archived chats, each
requiring its own explicit query flag to include. `route`'s own `include_archived` is not even
an accepted query param here (the function signature has no `include_archived` field — the
model default `False` from `get_chat_title_id_list_by_user_id`'s own signature always applies,
`models/chats.py:1549`) — **archived chats can never be included through this route at all**;
`GET /api/v1/chats/archived` (`routers/chats.py:1060`) is the separate route for those.

### 2.3 `GET /api/v1/chats/search?text=`

`routers/chats.py:865-901`:

```python
@router.get('/search', response_model=list[ChatTitleIdResponse])
async def search_user_chats(
    request: Request,
    text: str,
    page: int | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if page is None:
        page = 1
    limit = 60
    skip = (page - 1) * limit
    ...
    for chat in await Chats.get_chats_by_user_id_and_search_text(user.id, text, skip=skip, limit=limit, db=db):
        chat_list.append(ChatTitleIdResponse(
            id=chat.id, title=chat.title, updated_at=chat.updated_at,
            created_at=chat.created_at, last_read_at=chat.last_read_at,
            snippet=chat_search_snippet(chat.chat, search_text),
        ))
```

Unlike `/list`, `page` here defaults to `1` (not "no pagination") — a search always page-limits
to 60. `get_chats_by_user_id_and_search_text` (`models/chats.py:1966-2076`) confirms it is a
**case-insensitive substring/term match** over both **title** and **message content**:
`Chat.title.ilike(bindparam(...))` with `f'%{phrase_query}%'`, paired with an
`OR` against `chat_search_message_content_match_sql(dialect_name, ...)` (a dialect-specific
content match — LIKE-based on SQLite, confirmed by the surrounding `if dialect_name == 'sqlite':`
branch at `:2076`) for the phrase and for each individual term (`:2044-2069`). It also recognizes
special filter tokens inside `text` — `tag:<name>`, `folder:<name>`, `pinned:true/false`,
`archived:true/false`, `shared:true/false` (`:1985-2013`) — stripped from the free-text search
before the phrase/term match runs. Always scoped to `Chat.user_id == user_id` (`:2019`) — same
own-chats-only rule as `/list`; **archived chats are excluded by default here too** unless
`archived:true` appears in `text` (`:2024-2027`).

### 2.4 No route answers "the chat holding message id X"

Collecting the sections above and §1.5, §5.4: at this tag there is no way to ask the frontend
which chat holds a given message id, so a caller that minted the ids of its own turn cannot look
its chat up directly.

- The **listing** item is `ChatTitleIdResponse` — id, title, and the timestamps (§2.2). No
  message ids, no message content.
- The **search** route matches **content**, by a case-insensitive LIKE over title and message
  text (§2.3). A message *id* is never compared, and its `page` defaults to 1, so it is
  page-limited besides.
- The **completion's response** carries the chat id only on the fan-out branch that a
  `session_id` selects (§1.5); without one, `stream: true` answers a bare `null`. That branch
  runs the turn in the background and answers before any content exists, so taking it would end
  the synchronous read-back a caller's turn timing rests on (§5.4).
- `POST /api/v1/chats/new` mints its id **server-side**, and a chat created there puts the
  following completion on the *follow-up* path (`is_new_chat` false, §1.1), not the new-chat path
  a person's first message takes.
- A chat the caller cannot read answers **401 with the not-found detail** (§3.1) — the one status
  for "never existed" and "not yours" alike.

So the only identification available is: list before the turn, list after, and **read each new
chat by id**, taking the one whose `chat.history.messages` holds both minted ids (§3.1, §3.2).
That is what `gideon/host/owuiturn.py`'s `find_turn_chat` does, and the 401 above is why a
candidate that vanishes between the two steps is skipped rather than raised (slice-1 ticket 68,
2026-09-15).

---

## 3. `GET /api/v1/chats/{id}` — the read-back

### 3.1 Response model, and the two-step ownership/admin check

`routers/chats.py:1324-1345`:

```python
@router.get('/{id}', response_model=ChatResponse | None)
async def get_chat_by_id(
    id: str,
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    chat = await Chats.get_chat_by_id_for_user(id, user, db=db)
    if chat:
        data = ChatResponse.model_validate(chat, from_attributes=True).model_dump()
        data = overlay_response_streams(data, await get_response_streams_by_chat_id(request.app.state.redis, id))
        data['context_usage'] = await get_chat_context_usage(chat)
        return data
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.NOT_FOUND)
```

Note the status code on a miss is **401**, not 404 — `ERROR_MESSAGES.NOT_FOUND` text under an
`HTTP_401_UNAUTHORIZED` status, so a caller cannot distinguish "chat doesn't exist" from
"not authorized to read it" from the status code alone.

`ChatResponse` (`models/chats.py:279-303`):

```python
class ChatResponse(BaseModel):
    id: str
    user_id: str
    title: str
    chat: dict
    updated_at: int
    created_at: int
    share_id: str | None = None
    archived: bool
    pinned: bool | None = False
    meta: dict = {}
    variables: dict = {}
    folder_id: str | None = None
    tasks: list | None = None
    summary: str | None = None
    current_message_id: str | None = None
    context_usage: dict | None = None
```

`Chats.get_chat_by_id_for_user` (`models/chats.py:1756-1786`):

```python
async def get_chat_by_id_for_user(self, id: str, user, db=None) -> ChatModel | None:
    chat = await self.get_chat_by_id_and_user_id(id, user.id, db=db)
    if chat:
        return chat
    chat = await self.get_chat_by_id(id, db=db)
    if not chat:
        return None
    if user.role == 'admin' and (ENABLE_ADMIN_CHAT_ACCESS or is_internal_chat(chat.meta)):
        return chat
    if await AccessGrants.has_access(user_id=user.id, resource_type='shared_chat', resource_id=id, permission='read', db=db):
        return chat
    if chat.folder_id:
        ...has_folder_access(...)
    return None
```

**Owner always gets it** (the first branch, an `Chat.id == id AND Chat.user_id == user_id`
query, `models/chats.py:1735-1755`). **An admin gets any chat only if `ENABLE_ADMIN_CHAT_ACCESS`
is on** (an env-gated config; not confirmed on for GIDEON's render — out of scope here, see
`owui-preset-system-prompt.md`/render config notes) **or the chat is internal**; otherwise an
admin needs an explicit access grant or folder access, same as a non-owner `user`. For GIDEON's
harness (`gideon-eval`, role `user`, reading back its own chat) only the owner branch matters.

### 3.2 The stored assistant message's field set, and where the outlet's edits land

The completion handlers write the assistant message via
`Chats.upsert_message_to_chat_by_id_and_message_id` (streaming: `utils/middleware.py:6251-6259`;
non-streaming: `:4155-4165`), both writing at minimum `{'done': True, 'role': 'assistant'
(non-streaming only), 'output': <items>, 'usage': <...>}` on top of the placeholder record
created in §1.3 (`id`, `parentId`, `childrenIds`, `role`, `content`, `timestamp`, `model`). The
outlet's own persistence write (`middleware.py:3995-4022`, quoted in full in
`owui-filter-function.md` §5.3) is a further merge of the same kind — see §3.3 for
`originalContent`.

**Everything the outlet or the completion writes lands only in `chat.history.messages`, never
in the flat `chat.messages` list.** `Chats.upsert_message_to_chat_by_id_and_message_id`
(`models/chats.py:1141-1191`) reads `chat.get('history', {})`, calls the static
`upsert_message_to_history(history, message_id, message)`, reassigns `chat['history'] =
history`, and commits — **it never reads or writes `chat['messages']`** (confirmed by `grep`:
neither `main.py` nor `utils/middleware.py` nor `models/chats.py` assigns to a
`chat['messages']`/`chat.get('messages')` key anywhere outside the one-time construction at chat
creation, §1.3). **The flat `chat.messages` list is frozen at creation** — for GIDEON's harness
turn it stays exactly `[{'role': 'user', 'content': <the prompt>}]` (no id, no assistant entry,
ever) for the life of the chat. **The judge must read `chat.history.messages[<assistant id>]`,
not `chat.messages`.**

`upsert_message_to_history` (`models/chats.py:982-1023`), the merge itself:

```python
def upsert_message_to_history(history: dict, message_id: str, message: dict) -> dict:
    messages = history.setdefault('messages', {})
    if message_id in messages:
        messages[message_id] = {**messages[message_id], **message}
    else:
        ...  # first-write path; not taken for the outlet's edit, since the completion
             # handler already created the entry
    ...
    return messages[message_id]
```

For the outlet's edit (the message id already exists, created by the completion handler moments
earlier), this is a **plain shallow-merge `{**existing, **update}`** — every key in `message`
overwrites the same key on the existing record; every key absent from `message` is left as-is.

### 3.3 `originalContent`: exactly which code writes it, and under what condition

`utils/middleware.py:3995-4022` (already quoted in full in `owui-filter-function.md` §5.3 —
reproduced here for the field-by-field answer):

```python
if outlet_result and outlet_result.get('messages'):
    if not is_unsaved_chat and messages_map:
        for message in outlet_result['messages']:
            outlet_message_id = message.get('id')
            if outlet_message_id and outlet_message_id in messages_map:
                original_message = messages_map[outlet_message_id]
                original_content = original_message.get('content') or get_output_text(original_message.get('output'))
                message_content = message.get('content') or get_output_text(message.get('output'))
                content_changed = original_content != message_content
                output_changed = message.get('output') and message.get('output') != original_message.get('output')
                if content_changed or output_changed:
                    message_update = {
                        'originalContent': original_content,
                        **({'output': message['output']} if output_changed else {}),
                    }
                    if content_changed:
                        message_update['content'] = message_content or ''
                    await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, outlet_message_id, message_update)
```

- **Written by**: `outlet_filter_handler` in `utils/middleware.py`, via the `message_update`
  dict passed to `Chats.upsert_message_to_chat_by_id_and_message_id` (§3.2's merge function),
  which lands it in `chat.history.messages[<id>].originalContent` — **`chat.messages` is never
  touched** (§3.2).
- **Condition**: `if content_changed or output_changed:` — `originalContent` is written
  **whenever either `content` or `output` differs from what was stored before the outlet ran**,
  not "only when `content` changed." Its *value* is always
  `original_message.get('content') or get_output_text(original_message.get('output'))` — the
  pre-outlet content (falling back to the pre-outlet output's text) — regardless of which of
  `content_changed`/`output_changed` triggered the write. So a Filter that only replaces
  `output` (leaving `content` byte-identical) still gets `originalContent` written, as long as
  `output_changed` is true.
- **`output_changed` requires the *new* `output` to be truthy** — `message.get('output') and
  message.get('output') != original_message.get('output')` — a falsy replacement (`None`, `[]`,
  or omitted) makes `output_changed` falsy, so `'output'` is absent from `message_update`
  entirely and the old `output` array is never overwritten (this is `owui-filter-function.md`
  §5.3 point 3, cited rather than re-derived here).

### 3.4 `output` is persisted for a message the outlet did not change, and its item shape

The completion handler's own write (§3.2, `middleware.py:6251-6259` streaming /
`:4155-4165` non-streaming) persists `output` **unconditionally on every completed turn**,
before the outlet even runs — so both the reasoning item and the message item are in the DB
regardless of whether a Filter later edits anything. Confirmed by the write itself:

```python
await Chats.upsert_message_to_chat_by_id_and_message_id(
    metadata['chat_id'], metadata['message_id'],
    {'done': True, 'output': current_output, **({'usage': usage} if usage else {})},
)
```

(`utils/middleware.py:6248-6258`, streaming path — `current_output = full_output()`.)

**Item shapes**, from the streaming accumulator (`utils/middleware.py:5210-5296`) and the
non-streaming builder (`utils/middleware.py:4098-4128`, used when the upstream response has no
native `output` field and one is synthesized from `choices[0].message`):

Reasoning item (non-streaming synthesis, `:4098-4113`):

```python
reasoning_item = {
    'type': 'reasoning',
    'id': output_id('r'),
    'status': 'completed',
    'start_tag': '<think>',
    'end_tag': '</think>',
    'attributes': {'type': 'reasoning_content'},
    'content': [{'type': 'output_text', 'text': reasoning_content}] if reasoning_content else [],
    'summary': None,
}
```

The **streaming** accumulator additionally tracks `started_at`/`ended_at` and computes
`duration` once the reasoning block closes (`:5213-5217`, `:5279-5284`):

```python
reasoning_item['ended_at'] = time.time()
reasoning_item['duration'] = int(reasoning_item['ended_at'] - reasoning_item['started_at'])
reasoning_item['status'] = 'completed'
```

— **`duration` (an integer, seconds) is present only on a `reasoning` item, only once
`status` flips to `completed`**; the non-streaming synthesis path above never sets it at all
(no `started_at`/`ended_at` tracked there, since the whole response arrives at once).

Message item (both paths — non-streaming `:4114-4121`, streaming `:5286-5296`):

```python
{
    'type': 'message',
    'id': output_id('msg'),
    'status': 'completed',  # 'in_progress' while streaming, flipped to 'completed' at the end
    'role': 'assistant',
    'content': [{'type': 'output_text', 'text': content}],
}
```

`output_id(prefix)` (`utils/middleware.py:253`) mints the item's own id (`r...`/`msg...`
prefixed) — a different id space from the message's own `id`/`message_id`.

---

## 4. Deletion

### 4.1 `DELETE /api/v1/chats/{id}`

`routers/chats.py:1567-1615`, in full:

```python
@router.delete('/{id}', response_model=bool)
async def delete_chat_by_id(request: Request, id: str, user=Depends(get_verified_user), db=Depends(get_async_session)):
    if user.role == 'admin':
        chat = await Chats.get_chat_by_id(id, db=db)
    else:
        if not await has_permission(user.id, 'chat.delete', await Config.get('user.permissions')):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
        chat = await Chats.get_chat_by_id_and_user_id(id, user.id, db=db)

    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    await stop_item_tasks(request.app.state.redis, id)
    await Chats.delete_orphan_tags_for_user(chat.meta.get('tags', []), user.id, threshold=1, db=db)
    for child_id in await Chats.get_internal_chat_ids_by_parent_id(id, chat.user_id):
        await stop_item_tasks(request.app.state.redis, child_id)
        await Chats.delete_chat_by_id_and_user_id(child_id, chat.user_id)

    if user.role == 'admin':
        result = await Chats.delete_chat_by_id(id, db=db)
    else:
        result = await Chats.delete_chat_by_id_and_user_id(id, user.id, db=db)
    ...
    return result
```

- **Response body**: `bool` — `True` on success (or `False` if the underlying model-layer
  delete swallowed an exception, `models/chats.py:2481-2502`, both wrapped in
  `try/except Exception: return False`).
- **Permission for a `user` role**: exactly `chat.delete` under `user.permissions`
  (`has_permission(user.id, 'chat.delete', ...)`, `routers/chats.py:1579`) — a 401 if absent.
  GIDEON's rendered permission set grants `chat.delete: true` per the task's premise, so
  `gideon-eval` passes this gate.
- **`Chats.get_chat_by_id_and_user_id(id, user.id, ...)`** (`models/chats.py:1735-1755`) scopes
  the delete to the caller's own chat by construction (`filter_by(id=id, user_id=user_id)`) — a
  `user`-role caller cannot delete another user's chat even with `chat.delete` granted, because
  the row simply won't be found (`if not chat: raise 404`) for an id it doesn't own.
- **An admin deleting another user's chat is allowed**: the `if user.role == 'admin':` branch
  (`:1576-1577`) fetches by id alone (`Chats.get_chat_by_id`, no `user_id` filter,
  `models/chats.py:1691-1710`) and deletes with `Chats.delete_chat_by_id(id, db=db)`
  (`:1608-1609`, `models/chats.py:2481-2491`) — **no `chat.delete` permission check at all for
  an admin** (the `has_permission` call is inside the `else:` branch only).
- **What it deletes**: `delete_chat_by_id_and_user_id` (`models/chats.py:2493-2503`) — nulls
  `AutomationRun.chat_id` for any automation referencing the chat (does not delete the
  automation), deletes every `ChatMessage` row for the chat (the dual-write table from §1.3),
  deletes the `Chat` row itself, and finally `return True and await
  self.delete_shared_chat_by_chat_id(id, db=session)` — **a shared-chat snapshot (if the chat
  was ever shared, `share_id` set) is also deleted**. Tags on the chat itself are handled
  separately, at the route level, by `Chats.delete_orphan_tags_for_user` (only removes a tag
  from the user's tag list if this was its last reference, `threshold=1`) — not inside the
  model-layer delete.

### 4.2 `DELETE /api/v1/chats/` — all of the caller's chats

`routers/chats.py:707-726`:

```python
@router.delete('/', response_model=bool)
async def delete_all_user_chats(request: Request, user=Depends(get_verified_user), db=Depends(get_async_session)):
    if user.role == 'user' and not await has_permission(user.id, 'chat.delete', await Config.get('user.permissions')):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
    result = await Chats.delete_chats_by_user_id(user.id, db=db)
    ...
    return result
```

Yes, this route exists. **Note the check is `user.role == 'user'`**, not `!= 'admin'` — an
admin (and, oddity worth flagging, a hypothetical third role value that is neither `'user'` nor
`'admin'`, though `get_verified_user` already excludes anything outside `{'user', 'admin'}`)
skips the permission check entirely. `delete_chats_by_user_id` (`models/chats.py:2504-2521`)
deletes every shared-chat snapshot for the user first, nulls `AutomationRun.chat_id` for all of
the user's chats, deletes every `ChatMessage` row across all of them, then deletes every `Chat`
row `filter_by(user_id=user_id)`.

---

## 5. `background_tasks`: defaults, the all-off shape, `features`, and `session_id`'s actual role

### 5.1 Absent `background_tasks` runs nothing — confirmed by the gate itself, not a config default

`tasks = form_data.pop('background_tasks', None)` (`main.py:1096`) is passed through untouched
into `ctx['tasks']` (`build_chat_response_context`, `utils/middleware.py:3138-3150`) and read by
`background_tasks_handler` (`utils/middleware.py:3654-3835`, called at the end of both the
streaming (`:6279`) and non-streaming (`:4171`) success paths). The controlling gate,
`utils/middleware.py:3710`:

```python
if message and 'model' in message:
    if tasks and messages:
        if TASKS.FOLLOW_UP_GENERATION in tasks and tasks[TASKS.FOLLOW_UP_GENERATION]:
            ...
    if is_saved_chat_id(metadata.get('chat_id')):
        if TASKS.TITLE_GENERATION in tasks:
            ...
        if TASKS.TAGS_GENERATION in tasks and tasks[TASKS.TAGS_GENERATION]:
            ...
```

**`if tasks and messages:` is the single gate for the whole block** — when `background_tasks`
is absent from the request, `tasks` is `None`, this condition is `False`, and **no follow-up
generation, no title generation, no tags generation runs at all** — not "defaults to the
config's `ENABLE_TITLE_GENERATION`/`ENABLE_TAGS_GENERATION`." Those config keys
(`task.title.enable`/`task.tags.enable`/`task.follow_up.enable`, `config.py:3149-3151`,
env-backed by `ENABLE_TITLE_GENERATION`/`ENABLE_TAGS_GENERATION`/`ENABLE_FOLLOW_UP_GENERATION`,
each defaulting `'True'`, `config.py:2308-2312`) are read only by `routers/tasks.py`'s own
`/api/v1/tasks/config` endpoint (`routers/tasks.py:52-54`, `:117-126`) — **the UI's own signal
for whether to populate `background_tasks` in its request**, not a server-side fallback applied
inside `background_tasks_handler` itself. **This is a genuinely absent default, confirmed by the
gate, not by an unread config lookup.**

**A title-generation call, when it does run, is also the only mechanism that ever renames the
chat away from `'New Chat'`** (`update_chat_title_by_id`, `:3798`, `:3812`) — so a harness turn
sending `background_tasks: {}` or omitting the key entirely leaves the chat titled `'New Chat'`
forever.

### 5.2 The all-off shape, confirmed against `TASKS` and the browser's own payload

`constants.py:135-142`:

```python
class TASKS(str, Enum):
    ...
    TITLE_GENERATION = 'title_generation'
    FOLLOW_UP_GENERATION = 'follow_up_generation'
    TAGS_GENERATION = 'tags_generation'
```

`{"title_generation": false, "tags_generation": false, "follow_up_generation": false}` matches
these three enum values exactly, and each check above is a plain truthiness test
(`tasks[TASKS.TITLE_GENERATION]`, `tasks[TASKS.FOLLOW_UP_GENERATION]`,
`tasks[TASKS.TAGS_GENERATION]`) — `False` for all three short-circuits every branch inside
`background_tasks_handler`'s outer `if tasks and messages:`/`if is_saved_chat_id(...)` gates
without even calling the generator. **The only difference between sending this dict and
omitting `background_tasks` is the (harmless) exception to the title path**:
`if TASKS.TITLE_GENERATION in tasks:` (`:3762`) is `True` either way the key is present — but
the nested `if tasks[TASKS.TITLE_GENERATION]:` (`:3768`) being `False` skips the actual
`generate_title` call; the *only* code that still runs is a fallback,
`if title == None and len(messages) == 2 and (not messages_map or len(messages_map) <= 2):
title = messages[0].get('content', user_message)` (`:3806-3816`) — **this unconditionally titles
a brand-new 2-message chat from the user's own first message, with no engine call**, regardless
of `title_generation: false`. This is the one background-tasks side effect that survives sending
the all-off form (it does not survive omitting `background_tasks` altogether, since the whole
block is gated on `tasks` being truthy). **So: to guarantee zero extra engine calls, omit
`background_tasks` entirely; sending it all-`false` is one non-engine DB write away from
identical (a title derived from the prompt with no model call), not fully inert.**

The browser's own construction of this same dict (`Chat.svelte:3599-3607`) confirms the field
names independently:

```js
background_tasks: {
    ...(!$temporaryChatEnabled && (!_chatId || ...) ? {
        title_generation: $settings?.title?.auto ?? true,
        tags_generation: $settings?.autoTags ?? true
    } : {}),
    follow_up_generation: $settings?.autoFollowUps ?? true
}
```

### 5.3 `features` defaults to `{}` when absent

`main.py:1258`: `'features': form_data.get('features', {})` — a bare `.get` with an empty-dict
default, not popped, not merged with any config-derived default set. Every individual feature
flag (`web_search`, `image_generation`, `code_interpreter`, etc.) is read downstream as
`metadata.get('features', {}).get('<flag>')`-style lookups against this same dict, so an absent
`features` key means every feature is off by construction — no server-side default flips one on.

### 5.4 `session_id` is not needed for the turn's completion or persistence on this path

`get_event_emitter_and_caller` (`utils/middleware.py:3120-3135`, quoted in full in
`owui-filter-function.md` §10.0): `if metadata.get('chat_id') and metadata.get('message_id'):
event_emitter = await get_event_emitter(metadata)` — **only `chat_id` and `message_id`**, no
`session_id` check. Both persistence (`Chats.upsert_message_to_chat_by_id_and_message_id`) and
the outlet's own persistence write (§3.3) run off this same `event_emitter`/`ctx`, so a harness
turn with a real synthesized `chat_id` (§1) and a caller-supplied `id` (→ `message_id`) gets full
persistence with **no `session_id` in the request at all** — confirmed independently of
`owui-filter-function.md` §11.2's inlet-side finding, at the completion/persistence layer this
note is about. `session_id`'s only load-bearing effect on this exact path is the fan-out gate at
§1.5 (`main.py:1776`) — its presence would route the turn onto the background-task/immediate-`{status:true}`-response
branch instead of the synchronous one the harness relies on.

---

## 6. The raw streaming route with no `parent_id`/`chat_id`/`user_message`

This is `owui-filter-function.md` §10.2's exact scenario (a bearer-key caller, `chat_id = ''`,
`event_emitter = None`), cited rather than re-derived: `stream_wrapper`
(`utils/middleware.py:6318-6379`) is the branch taken, relaying each upstream SSE chunk as it
arrives (`async for data in original_generator: ... yield data`, `:6373-6374`) with no
buffering — bytes already sent cannot be rewritten by a later hook.

**Confirmed here, additionally:**

- **Format**: OWUI does not re-frame the upstream bytes into a different wire format — the
  provider call (`routers/openai.py`, the `StreamingResponse` construction at, e.g.,
  `:1676`/`:1788`/`:1916`/`:2038`, each gated on `'text/event-stream' in
  r.headers.get('Content-Type', '')`) streams the raw upstream body through. GIDEON's on-box
  observation (ticket 04/05: 100–270 SSE lines, `reasoning` deltas then `content` deltas, no
  `<think>` text) is the authoritative confirmation of the literal `data: {json}\n\n` /
  `data: [DONE]\n\n` framing and the `reasoning`-named delta field for the pinned vLLM build —
  cited here rather than re-derived, since that framing is vLLM's own OpenAI-compatible server
  behavior, not Open WebUI's.
- **`wrap_item`/`stream_wrapper` on the fallback path**: only the **`stream`-type** Filter hook
  (`process_filter_functions(..., filter_type='stream', ...)`, `utils/middleware.py:6361-6368`)
  can rewrite a chunk before it's relayed — `data = wrap_item(JSONCodec.dumps(event)) if event
  else None` (`:6369`) — a Filter's `stream()` method output, not the outlet. No outlet Filter
  runs until *after* the whole loop (`:6376-6378`), by which point every byte is already on the
  wire, per `owui-filter-function.md` §10.2's proof.
- **`usage`/`sources` events interleaved**: `stream_wrapper`'s pre-loop
  `for event in events: ...` (`utils/middleware.py:6353-6360`) only has anything to emit if
  `events` (built by `process_chat_payload`, starting `events = []` at `:2544`) is non-empty.
  **The only append site in the whole function is `events.append({'sources': sources})`**
  (`:3087`, inside the RAG/citation-building code, reached only when web search / knowledge
  retrieval actually ran). For a plain `{"model": ..., "messages": [...], "stream": true}`
  payload with no `features.web_search`/knowledge/files, `events` stays `[]`, and **no extra
  `sources` (or any other) events are interleaved** — confirmed by there being exactly one
  `events.append` call site in the file (`grep`-verified), not by assuming the feature is
  disabled by default. A `usage` chunk, if present at all, would be the upstream provider's own
  native SSE event (gated by `stream_options.include_usage`, itself gated on
  `model_capabilities.get('usage')` from the model record, `main.py:1160-1162`) — out of scope
  for this note (an OWUI model-record question, not a routing one).
- **Inlet-raise response for this caller vs. the eval identity**: `owui-filter-function.md`
  §11.3 already traces this exactly for "an API caller (no `chat_id`/`message_id`)" — the
  `else` branch of `process_chat`'s exception handler (`main.py:1693-1699`) raises
  `HTTPException(400, detail=error_detail)`, the raw exception text in the body. That is the
  **upstream Open WebUI** behavior for any caller without `chat_id`/`message_id` set — GIDEON's
  own guardrail Filter, per the coordinator's note, exempts the `gideon-eval` identity from its
  own inlet check by email, so this 400 path is what a *different*, non-exempt `user`-role
  caller would see; `gideon-eval` itself does not trip the inlet at all and so never reaches this
  branch on that account.

---

## 7. Sign-in, token lifetime, bearer acceptance, and sign-out

### 7.1 `POST /api/v1/auths/signin`

`routers/auths.py:716-836`, form and success path (rate-limited on repeated failure,
`signin_rate_limiter.is_limited`, `:809-813`; body validated against `models/auths.py:61-63`,
`class SigninForm(BaseModel): email: str; password: str`):

```python
user = await Auths.authenticate_user(form_data.email.lower(), lambda pw: verify_password(form_data.password, pw), db=db)
if user:
    return await create_session_response(request, user, db, response, set_cookie=True, source=auth_source)
else:
    raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)
```

`create_session_response` (`routers/auths.py:166-227`) builds the response:

```python
expires_delta = parse_duration(await Config.get('auth.jwt_expiry'))
expires_at = int(time.time()) + int(expires_delta.total_seconds()) if expires_delta else None
token = create_token(data={'id': user.id}, expires_delta=expires_delta)
...
return {
    'token': token,
    'token_type': 'Bearer',
    'expires_at': expires_at,
    'id': user.id,
    'email': user.email,
    'name': user.name,
    'role': user.role,
    'profile_image_url': f'/api/v1/users/{user.id}/profile/image',
    'permissions': user_permissions,
}
```

`token_type` is the literal string `'Bearer'`; `expires_at` is a Unix timestamp (or `None` if
`auth.jwt_expiry` parses to no expiry). `auth.jwt_expiry` is backed by `JWT_EXPIRES_IN`
(`config.py:2479`, default `'4w'`; `config.py:3164`, `'auth.jwt_expiry': JWT_EXPIRES_IN`) — the
env var GIDEON renders to `12h`. A `set_cookie=True` call also sets an `httponly` cookie named
`token` with the same value and `max_age` — irrelevant to a headless caller that never sends
cookies and holds the `token` field from the JSON body instead.

### 7.2 The session token is a plain bearer everywhere `get_verified_user` is used — no endpoint allowlist applies to it

`utils/auth.py:347-398`, `get_current_user`, the single discriminator:

```python
if token.startswith('sk-'):
    user = await get_current_user_by_api_key(request, token)
    ...
    return user
# auth by jwt token
try:
    data = decode_token(token)
    ...
    user = await Users.get_user_by_id(data['id'])
    ...
    return user
```

**Only a token literally prefixed `sk-` is routed to `get_current_user_by_api_key`.** A signin
JWT never starts with `sk-` (`create_api_key`, `utils/auth.py:340-342`, is the only minter of
`sk-`-prefixed strings, used solely for the separate API-keys feature), so it always takes the
JWT branch: `decode_token` + `is_valid_token` + `Users.get_user_by_id`, then an unconditional
`return user` — **no endpoint check of any kind**. `API_KEYS_ALLOWED_ENDPOINTS`
(`config.py`-backed as `auth.api_key.endpoint_restrictions`/`auth.api_key.allowed_endpoints`,
read at `utils/auth.py:472-475`) is enforced **only inside `get_current_user_by_api_key`**
(`utils/auth.py:490-499`), which the JWT branch never calls. **Confirmed: a session token from
`/api/v1/auths/signin` is accepted as a bearer on `/api/chat/completions` and every
`/api/v1/chats/*` route** (all depend on `get_verified_user` → `get_current_user`, the same
function traced above), with none of the API-key allowlist logic in play — it is architecturally
inapplicable to this token type, not merely permissive for it.

### 7.3 `POST /api/v1/auths/signout` — real server-side revocation, but only when Redis is configured

`routers/auths.py:954-975`:

```python
@router.post('/signout')
async def signout(request: Request, response: Response, db=Depends(get_async_session)):
    token = None
    auth_header = request.headers.get('Authorization')
    if auth_header:
        auth_cred = get_http_authorization_cred(auth_header)
        if auth_cred is not None:
            token = auth_cred.credentials
    if token is None:
        token = request.cookies.get('token')
    ...
    if token:
        ...
        await invalidate_token(request, token)
        ...
    response.delete_cookie('token')
    ...
```

**This route exists, accepts the bearer token from the `Authorization` header (not only a
cookie), and calls `invalidate_token`** — this is genuine server-side revocation, not merely a
cookie clear. `invalidate_token` (`utils/auth.py:283-303`):

```python
async def invalidate_token(request, token):
    decoded = decode_token(token)
    if not decoded:
        return
    if request.app.state.redis:
        jti = decoded.get('jti')
        exp = decoded.get('exp')
        if jti and exp:
            ttl = exp - int(datetime.now(UTC).timestamp())
            if ttl > 0:
                await request.app.state.redis.set(f'{REDIS_KEY_PREFIX}:auth:token:{jti}:revoked', '1', ex=ttl)
```

**The entire revocation write is inside `if request.app.state.redis:`.** `is_valid_token`
(`utils/auth.py:251-279`), which every subsequent JWT-bearing request runs through inside
`get_current_user` (`utils/auth.py:390-395`), checks the same Redis key — also inside its own
`if redis:` guard (`:257`). **Without Redis, both the write and the read no-op, so a signed-out
token is never actually rejected — it stays valid until its natural `exp`.**

`app.state.redis = get_redis_client(async_mode=True)` (`main.py:384`), and `get_redis_client`
(`utils/redis.py:88-104`): `if not REDIS_URL and not sentinel_list: return None`. **GIDEON's
render sets no `REDIS_URL`/sentinel variables anywhere for the Open WebUI service** (`grep -rn
"REDIS" gideon/ compose/` in this repo returns nothing), so `app.state.redis` is `None` on
GIDEON's box, and `main.py:509`'s fallback (`app.state.redis = None`, the module-level default
before the async startup hook runs) confirms the type is expected to be nullable. **Answer: the
route exists and its code path is a real per-token invalidation mechanism, but on GIDEON's
current, Redis-less render it is inert — `gideon-eval`'s session token cannot be invalidated
early by calling signout; the harness must simply hold it in memory for the run and let it expire
naturally** (12h, GIDEON's rendered `JWT_EXPIRES_IN`) or rely on a future Redis deployment to make
signout effective.

---

## Unverified / out of scope

- **`ENABLE_ADMIN_CHAT_ACCESS`'s value on GIDEON's render** (§3.1) — not checked in this note;
  irrelevant to `gideon-eval` reading back its own chat as owner, but relevant if a break-glass
  admin ever needs to read the harness's chats directly.
- **`model_capabilities.get('usage')` for `gideon-general`** (§6) — whether the model record
  sets `capabilities.usage`, which would add a native `usage` SSE event via
  `stream_options.include_usage` — this is an `owui-model-record.md`-shaped question, not
  re-derived here.
- **The exact SSE wire framing and delta field names are cited to GIDEON's own on-box
  observation** (ticket 04/05), not re-derived from vLLM v0.27.1's source — that server is
  outside Open WebUI's own repository and this note's stated scope.
- **`chat_search_message_content_match_sql`'s PostgreSQL branch** (§2.3) was read only far
  enough to confirm the SQLite LIKE-based branch exists (`models/chats.py:2076`'s
  `if dialect_name == 'sqlite':` guard implies a separate Postgres branch below it); GIDEON's
  render's actual DB backend was not cross-checked against this note.
- **`Config.get('auth.enable_api_keys')`/`features.api_keys` for `gideon-eval`** were not
  re-verified here — `docs/research/owui-filter-function.md` §10.5 already established `user`-role
  accounts cannot mint API keys under GIDEON's rendered permissions (`api_keys: false`,
  `compose/open-webui/permissions.yaml:67`), cited rather than re-derived; this note only needed
  the separate fact that a *session* token is architecturally exempt from the API-key allowlist
  regardless (§7.2).
- **Whether `chat_search_snippet` ever leaks content from a message the caller can't otherwise
  read** — not investigated; out of scope for a same-owner read.

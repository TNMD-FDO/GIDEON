---
verified_against:
  - pin: images.open-webui
    version: v0.11.0
---

# Open WebUI v0.11.0 deployment contract

Source of truth: `git clone --depth 1 --branch v0.11.0 https://github.com/open-webui/open-webui.git`,
resolved to commit `f9590b8017199e56d5e953657e6498e3cef1d246` (tagged `v0.11.0`,
`package.json` version `0.11.0`). All file paths below are relative to
`backend/open_webui/` unless stated otherwise. Docs cross-checks are against
`docs.openwebui.com/reference/env-configuration/` and
`docs.openwebui.com/features/authentication-access/auth/ldap/`.

**Important context**: this tag postdates the assistant's knowledge cutoff by
several months and the codebase has clearly been reworked since the version
most public write-ups describe — the config system is a per-key DB table
(`Config.get('dotted.key')`) rather than the older single-JSON-blob
`PersistentConfig` class, auth/DB code is fully async (SQLAlchemy async
sessions), and there's a new internal event bus (`EVENTS`/`publish_event`).
Where this note cites "the old X" it means: don't assume behavior from an
older Open WebUI version or a blog post — everything below was read directly
from this tag's source.

---

## A. First-admin seeding — CONFIRMED, two independent mechanisms

**1. Env-var seeding at process startup (no HTTP call), the one the question asks about:**

- `WEBUI_ADMIN_EMAIL` — `env.py:752`, `os.getenv('WEBUI_ADMIN_EMAIL', '')`
- `WEBUI_ADMIN_PASSWORD` — `env.py:753`, default `''`
- `WEBUI_ADMIN_NAME` — `env.py:754`, default `'Admin'`

Wired up in `main.py:348-351` (inside the FastAPI `lifespan`/startup handler,
after `import_legacy_config_json()` / `seed_registered_defaults()` /
`initialize_runtime_config()`):

```python
# Create admin account from env vars if specified and no users exist
if WEBUI_ADMIN_EMAIL and WEBUI_ADMIN_PASSWORD:
    if await create_admin_user(WEBUI_ADMIN_EMAIL, WEBUI_ADMIN_PASSWORD, WEBUI_ADMIN_NAME):
        # Disable signup since we now have an admin
        await Config.upsert({'ui.enable_signup': False})
```

`create_admin_user` (`utils/auth.py:525-556`) is idempotent and safe to leave
set across restarts: it no-ops if `Users.has_users()` is already true, and
only then inserts a user with `role='admin'` directly (bypasses the
first-user-becomes-admin race logic entirely, since it constructs the row
with `role='admin'` up front). Both `WEBUI_ADMIN_EMAIL` and
`WEBUI_ADMIN_PASSWORD` must be set (truthy) or nothing happens. This runs
**before** signup is disabled, so a restart with the vars left set and a
DB that already has users is a harmless no-op.

**2. Fallback: first `POST /api/v1/auths/signup` becomes admin.** Confirmed in
`signup_handler` (`routers/auths.py:827-882`): it inserts the user with
`role=await Config.get('ui.default_user_role')` first (default `'pending'`,
see item F), then atomically re-checks `Users.get_num_users(db=db) == 1` and
promotes to `'admin'` if so, and sets `ui.enable_signup = False` — i.e. the
*first* successful signup, whatever role it lands on, becomes admin only if
it is the only user afterward; every subsequent signup keeps whatever
`DEFAULT_USER_ROLE` gives it.

**Does this still work with `ENABLE_SIGNUP=false`?** Yes, deliberately.
`signup()` (`routers/auths.py:885-901`) gates as follows:

```python
if WEBUI_AUTH:
    if has_users:
        if not await Config.get('ui.enable_signup') or not await Config.get('ui.enable_login_form'):
            raise HTTPException(403, ...)
    # Don't gate the first admin on ENABLE_SIGNUP: it auto-disables and can persist stale across a DB reset.
    elif not await Config.get('ui.enable_login_form') and not ENABLE_INITIAL_ADMIN_SIGNUP:
        raise HTTPException(403, ...)
```

So when the user table is empty, `ui.enable_signup` is **not consulted at
all** — the only gate is `ENABLE_LOGIN_FORM` (`config.py:1625`, default
`True`). The first signup is blocked only if *both* `ENABLE_LOGIN_FORM=false`
*and* `ENABLE_INITIAL_ADMIN_SIGNUP` (`env.py:707`, default `False`) is not
set to `true`. In the default configuration (`ENABLE_LOGIN_FORM=True`), the
first `/auths/signup` call always succeeds and becomes admin regardless of
`ENABLE_SIGNUP`.

**Third, undocumented-by-the-question path found in `backend/start.sh`**
(HuggingFace Spaces only): if `SPACE_ID` is set and `ADMIN_USER_EMAIL` +
`ADMIN_USER_PASSWORD` are set (note: different names, no `WEBUI_` prefix),
the entrypoint script boots uvicorn, polls `/health`, then does exactly the
HTTP `POST /api/v1/auths/signup` call itself, then restarts the server. This
is a distinct, Spaces-specific mechanism from `WEBUI_ADMIN_EMAIL/PASSWORD`
and does involve an HTTP call (self-issued by the container). Don't confuse
the two variable pairs in a deployment plan.

Docs (`docs.openwebui.com`, per web search of the docs repo) describe
`WEBUI_ADMIN_EMAIL`/`WEBUI_ADMIN_PASSWORD` consistently with the source:
created only when no users exist, `ENABLE_SIGNUP` auto-disabled afterward.

---

## B. Persistent config — CONFIRMED, with one clarification

- `ENABLE_PERSISTENT_CONFIG` — `config.py:3186`, `os.getenv('ENABLE_PERSISTENT_CONFIG', 'True')`, default `True`.
- Sibling: `ENABLE_OAUTH_PERSISTENT_CONFIG` (`config.py:3187`, default `False`) — a second, independent gate specifically for `oauth.*`-prefixed keys, on top of the main flag.

Config is no longer a single JSON blob; it's a per-key SQL table,
`open_webui.models.config.Config` (`models/config.py:99-236`), table name
`config` (`key TEXT PK, value JSON, updated_at BIGINT`). Semantics when
`ENABLE_PERSISTENT_CONFIG=false`:

- `Config.persistent_enabled_for(key)` returns `False` for every key (or, if only `ENABLE_OAUTH_PERSISTENT_CONFIG` is off, for `oauth.*` keys specifically) — `models/config.py:130-136`.
- `Config.get(key)` then **never touches the DB**; it returns `Config.default_value(key, default)`, i.e. the in-process `DEFAULTS` dict built from env vars at import time (`config.py`'s giant `DEFAULT_CONFIG` dict, registered via `Config.configure(defaults=DEFAULT_CONFIG, ...)` at `config.py:3189-3193`) — `models/config.py:139-145`.
- `Config.upsert(updates)` (used by every admin-panel "save settings" call) checks the same gate per key: if persistence is disabled for that key, it **only mutates the in-memory `Config.DEFAULTS[key]`** and returns — no DB write (`models/config.py:196-218`). That in-memory dict is process-local and rebuilt from env vars on every startup, so an admin-panel edit made while `ENABLE_PERSISTENT_CONFIG=false` is visible for the life of that process but is gone on the next restart (env vars win again). This exactly matches the claim in the question.

**What is NOT config, and therefore always persists in the DB regardless of
`ENABLE_PERSISTENT_CONFIG`:** functions, models, groups, users, and
knowledge bases are ordinary SQLAlchemy ORM tables, not `Config` rows —
confirmed by `__tablename__` in each model:

| Domain | File | Table(s) |
|---|---|---|
| Functions | `models/functions.py:20` | `function` |
| Models | `models/models.py:117` | `model` |
| Groups | `models/groups.py:38,74` | `group`, `group_member` |
| Users | `models/users.py:49,144` | `user`, `api_key` |
| Knowledge | `models/knowledge.py:50,65,101` | `knowledge`, `knowledge_directory`, `knowledge_file` |

These are written via normal `INSERT`/`UPDATE` on their own tables and have
no dependency on the `Config` class or `ENABLE_PERSISTENT_CONFIG` at all.

---

## C. LDAP — CONFIRMED with several deployment-relevant nuances

Env vars and defaults, all in `config.py:2754-2786` (each is also exposed as
a `Config` key of the same shape, e.g. `ldap.enable`, listed at
`config.py:3159-3175`; the admin panel writes through `Config.upsert`, the
login flow reads through `Config.get`, so **whichever value is currently in
the `config` table wins over the env var once persistence is on** — same
caveat as item B):

| Purpose | Env var | Default |
|---|---|---|
| Enable LDAP | `ENABLE_LDAP` | `false` |
| Server label | `LDAP_SERVER_LABEL` | `'LDAP Server'` |
| Host | `LDAP_SERVER_HOST` | `'localhost'` |
| Port | `LDAP_SERVER_PORT` | `389` |
| Mail attribute | `LDAP_ATTRIBUTE_FOR_MAIL` | `'mail'` |
| Username attribute | `LDAP_ATTRIBUTE_FOR_USERNAME` | `'uid'` |
| Bind (app) DN | `LDAP_APP_DN` | `''` |
| Bind password | `LDAP_APP_PASSWORD` | `''` |
| Search base | `LDAP_SEARCH_BASE` | `''` |
| Additional search filter(s) | `LDAP_SEARCH_FILTERS` (reads `LDAP_SEARCH_FILTER` first, falls back to `LDAP_SEARCH_FILTERS`) | `''` |
| TLS on/off | `LDAP_USE_TLS` | `True` |
| CA cert file | `LDAP_CA_CERT_FILE` | `''` |
| Certificate validation | `LDAP_VALIDATE_CERT` | `True` |
| Cipher suite | `LDAP_CIPHERS` | `'ALL'` |
| Group management | `ENABLE_LDAP_GROUP_MANAGEMENT` | `False` |
| Group creation | `ENABLE_LDAP_GROUP_CREATION` | `False` |
| Group attribute | `LDAP_ATTRIBUTE_FOR_GROUPS` | `'memberOf'` |

Note the env-var-name quirk: `LDAP_SEARCH_FILTER` (singular, undocumented
alias) is checked *before* `LDAP_SEARCH_FILTERS` (plural, the documented
name) — `config.py:2772`. Same double-name pattern shows up for the API-key
vars in item E.

**Login flow, `ldap_auth` in `routers/auths.py:471-706`:**

1. Rejects if `Config.get('ldap.enable')` is falsy, if `ENABLE_PASSWORD_AUTH` is off (`env.py`/`config.py:1629`, default `True` — this flag also gates local `/signin`, since LDAP auth is still "password auth" from the app's point of view), or if the submitted password is empty/whitespace (explicit RFC 4513 §5.1.2 unauthenticated-bind guard, added as a hardening fix — comment cites this by name at lines 488-494).
2. Binds as the app/service account (`LDAP_APP_DN`/`LDAP_APP_PASSWORD`; anonymous bind if `LDAP_APP_DN` is empty).
3. Searches with filter **`(&({LDAP_ATTRIBUTE_FOR_USERNAME}={escaped_username}){LDAP_SEARCH_FILTERS})`** (`routers/auths.py:557`) — the configured `LDAP_SEARCH_FILTERS` string is spliced in **verbatim, unparenthesized, directly inside the outer `(&...)`**. That means the operator must supply it as a complete, self-parenthesized LDAP filter clause (e.g. `(objectClass=person)` or `(&(objectClass=person)(memberOf=CN=owui,...))`), not a bare attribute=value pair — an unparenthesized fragment will produce a malformed filter.
4. On a found entry, re-binds as the user's own DN with the submitted password to actually verify credentials (line 630-641).
5. **User auto-creation happens with no reference to `ui.enable_signup` or `ENABLE_LOGIN_FORM` anywhere in this function** — confirmed by grep; the only signup-related check in `ldap_auth` is the top-of-function `ENABLE_PASSWORD_AUTH` gate. So **yes, first LDAP login creates a local user even when `ENABLE_SIGNUP=false`** (and even when the login form is hidden). The new user's role is `await Config.get('ui.default_user_role')` (`routers/auths.py:652`) — i.e. `DEFAULT_USER_ROLE`, default `'pending'` (`config.py:1699`) — **unless** this is the very first user in the table, in which case the same TOCTOU-safe "promote to admin if `get_num_users()==1` after insert" logic applies (lines 659-663), exactly mirroring `signup_handler`.
6. A user with role `'pending'`: `create_session_response` does **not** block them — they get a JWT/cookie and a `SessionUserResponse` just like anyone else. What blocks them is `get_verified_user` (`utils/auth.py:494-500,491`), which requires `user.role in VERIFIED_USER_ROLES = {'user', 'admin'}` and 401s otherwise — used as the FastAPI dependency on essentially every chat/model/workspace endpoint. So a pending user can log in and reach endpoints gated only by `get_current_user`, but not ones gated by `get_verified_user`/`get_admin_user`. The `ui.pending_user_overlay_title`/`ui.pending_user_overlay_content` config keys (`config.py:3053-3054`, backed by `PENDING_USER_OVERLAY_TITLE`/`_CONTENT` env vars) exist specifically to show that user a blocking overlay in the frontend.
7. **Group sync** (only entered `if ENABLE_LDAP_GROUP_MANAGEMENT and user_groups:`, lines 689-696):
   - Group names are derived by extracting the **CN component of each `memberOf` DN** via `extract_group_cn_from_dn` (`routers/auths.py:455-465`), which uses `ldap3`'s `parse_dn` (handles escaped commas correctly, not naive `.split(',')`).
   - If `ENABLE_LDAP_GROUP_CREATION=True`, `Groups.create_groups_by_group_names(user.id, user_groups, db=db)` (`models/groups.py:478-512`) runs first, inserting any of those CNs that don't already exist as a `Group` row (matched purely by `Group.name == cn`, case-sensitive).
   - `Groups.sync_groups_by_group_names(user.id, user_groups, db=db)` (`models/groups.py:514-...`) always runs (independent of the creation flag): it computes `target_group_ids` = groups whose `name` is in the CN list, `existing_group_ids` = groups the user currently belongs to, then **deletes `GroupMember` rows for `existing - target`** and inserts rows for `target - existing`. **So yes: with `ENABLE_LDAP_GROUP_MANAGEMENT=True` and `ENABLE_LDAP_GROUP_CREATION=False`, a user is removed from any Open WebUI group whose name is not among their current `memberOf` CNs — matching is by group `name` string equality to the DN's CN — but they are only added to groups that already exist in Open WebUI** (since creation is off, an unmatched CN with no pre-existing same-named group is silently skipped, never created, and the user just doesn't end up in it).
8. **Local login form availability alongside LDAP:** `ENABLE_LDAP`/`ldap.enable` and `ENABLE_LOGIN_FORM`/`ui.enable_login_form` are independent config keys — grepped every reference to `ldap.enable` in the router and startup code and found no code path that flips `ui.enable_login_form` when LDAP is turned on. `/signin` (the local-password endpoint) is gated only by `ENABLE_PASSWORD_AUTH` (default `True`) and, for the "auth disabled" branch, `WEBUI_AUTH==False`; it is not disabled by `ENABLE_LDAP`. So: with defaults, a local admin account can sign in via `/signin` regardless of whether LDAP is enabled or reachable — this is a config choice the operator preserves, not something the app enforces automatically.

Docs cross-check (`docs.openwebui.com/features/authentication-access/auth/ldap/`, via search)
describe `ENABLE_LDAP_GROUP_MANAGEMENT`/`ENABLE_LDAP_GROUP_CREATION`/`LDAP_ATTRIBUTE_FOR_GROUPS` consistently with source; the docs summary found does not spell out the removal-on-sync behavior or the CN-matching detail — that part is confirmed only by reading `sync_groups_by_group_names` directly, flagged here as source-only.

---

## D. Sync + admin CRUD endpoints — CONFIRMED

**`POST /api/v1/functions/sync`** (`routers/functions.py:162-191`, admin-only via `Depends(get_admin_user)`):
- Body: `SyncFunctionsForm { functions: list[FunctionWithValvesModel] = [] }` (`routers/functions.py:158-159`).
- Semantics — **true desired-state sync, not a pure upsert**: `Functions.sync_functions` (`models/functions.py:140-182`) updates functions whose `id` already exists, inserts new ones, and **deletes every existing `Function` row whose `id` is not present in the payload** (`models/functions.py:171-174`, `await db.delete(func)`). An empty `functions: []` payload deletes all functions.

**`POST /api/v1/models/sync`** (`routers/models.py:487-502`, admin-only):
- Body: `SyncModelsForm { models: list[ModelModel] = [] }` (`routers/models.py:483-484`).
- Same true desired-state semantics: `Models.sync_models` (`models/models.py:593-...`) upserts, then for every existing model not in the new set it calls `AccessGrants.revoke_all_access('model', model.id, db=db)` and deletes the row (`models/models.py:629-633`). Also calls `AccessGrants.set_access_grants('model', model.id, model.access_grants, db=db)` per synced model, so access grants ride along in the same payload (`model.access_grants`, excluded from the plain column dump via `exclude={'access_grants'}`).

**Groups endpoints** (`routers/groups.py`), all under `/api/v1/groups`:

| Method + path | Auth | Body | Notes |
|---|---|---|---|
| `GET /` | `get_verified_user` | — | Non-admins see only groups they're a member of (`filter['member_id']`); admins see all. |
| `POST /create` | `get_admin_user` | `GroupForm { name: str, description: str, permissions: Optional[dict], data: Optional[dict] }` | **No `user_ids` field.** |
| `GET /id/{id}` | `get_admin_user` | — | |
| `GET /id/{id}/info` | `get_verified_user` | — | |
| `GET /id/{id}/export` | `get_admin_user` | — | Adds `user_ids: list[str]`. |
| `POST /id/{id}/users` | `get_admin_user` | — | Lists member `UserInfoResponse`s. |
| `POST /id/{id}/update` | `get_admin_user` | `GroupUpdateForm` = `GroupForm` (same shape, no `user_ids`) | |
| `POST /id/{id}/users/add` | `get_admin_user` | `UserIdsForm { user_ids: Optional[list[str]] }` | |
| `POST /id/{id}/users/remove` | `get_admin_user` | `UserIdsForm` | |
| `DELETE /id/{id}/delete` | `get_admin_user` | — | |
| `GET /id/{id}/preview` | — | — | |

So, directly answering the question: **`permissions` IS settable on
create/update** (part of `GroupForm`, `models/groups.py:114-118`); **`user_ids`
is NOT settable via create/update** — membership is managed exclusively
through the separate `/id/{id}/users/add` and `/id/{id}/users/remove`
endpoints, each taking a `UserIdsForm{user_ids}` body.

**Users listing** (`routers/users.py`):
- `GET /api/v1/users/` (`routers/users.py:66-110`, admin-only): paginated, `page` query param, page size fixed at `PAGE_ITEM_COUNT = 30` (`routers/users.py:63`); optional `query`/`order_by`/`direction` filters. Response `UserGroupIdsListResponse` = `{ users: [UserGroupIdsModel...], total: int }`, each item being the full `UserModel` fields (see below) plus `group_ids: list[str]`.
- `GET /api/v1/users/all` and `GET /api/v1/users/search` are separate, similarly-shaped endpoints (`/all` admin-only and unpaginated; `/search` verified-user, paginated).
- `UserModel` fields (`models/users.py:82-...`, mirroring the `user` table at `models/users.py:49-77`): `id`, `email`, `username`, `role` (default `'pending'`), `name`, `profile_image_url`, plus profile/status/metadata/timestamp fields including `last_active_at`, `updated_at`, `created_at`. So yes — id, email, name, role, and `last_active_at` are all present on every listed user.

**Updating a user's role**: `POST /api/v1/users/{user_id}/update` (`routers/users.py:866-982`, admin-only), body `UserUpdateForm { role: str | None, name, email, profile_image_url, password }` (`models/users.py:262-267`). `role` is a plain `str | None` field with **no enum/`Literal` validation** at the Pydantic level — but the only role strings the rest of the codebase treats as meaningful are `'admin'`, `'user'`, `'pending'` (see `VERIFIED_USER_ROLES = {'user','admin'}` at `utils/auth.py:491`, and the trusted-role-header allow-list `{'admin','user','pending'}` at `routers/auths.py:768`). The endpoint has a hard-coded guard preventing anyone but the primary (first-ever) admin from changing that user's own role away from `'admin'`, and preventing any other admin from modifying the primary admin at all (`routers/users.py:874-900`). A role change triggers `disconnect_user_sessions` and a `USER_ROLE_UPDATED` event.

**Admin-created user with password**: `POST /api/v1/auths/add` (`routers/auths.py:1081-1141`, admin-only), body `AddUserForm(SignupForm) { role: str | None = 'pending' }` (`models/auths.py:94-95`, inheriting `SignupForm`'s `email`/`password`/`name`/`profile_image_url`) — i.e. an admin can pass `role: "admin"` explicitly to mint a second admin with a chosen password in one call. Confirms the `/auths/add` endpoint name and shape guessed in the question.

**Knowledge list for admins**: `GET /api/v1/knowledge/` (`routers/knowledge.py:130-176`; there is no `/list` alias for this — `/list` isn't a route on this router). Auth is `get_verified_user`, not admin-only, and it is **filtered by default**: non-admins (or admins when `BYPASS_ADMIN_ACCESS_CONTROL` is false) only see knowledge bases they own or that belong to a group they're in (`routers/knowledge.py:144-148`). `BYPASS_ADMIN_ACCESS_CONTROL` (`config.py:2062-2068`, falls back to the older `ENABLE_ADMIN_WORKSPACE_CONTENT_ACCESS` name) **defaults to `True`**, so out of the box an admin calling this endpoint does see every knowledge base, each with its `user_id` field (`KnowledgeModel.user_id`, `models/knowledge.py:87`) intact. It's paginated (`page` param, `PAGE_ITEM_COUNT`=30/page, same constant as users).

---

## E. API keys & permissions — CONFIRMED

- `ENABLE_API_KEYS` — `config.py:2420`, default `False` (must be explicitly turned on).
- `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` — `config.py:2422-2427`; reads the new plural name first, **falls back to the old singular `ENABLE_API_KEY_ENDPOINT_RESTRICTIONS`** if unset; default `False`.
- `API_KEYS_ALLOWED_ENDPOINTS` — `config.py:2430`; reads the new plural name first, falls back to old singular `API_KEY_ALLOWED_ENDPOINTS`; default `''`. **Format: comma-separated list of path prefixes** (e.g. `/api/v1/chats,/api/v1/models`), split on `,` and `.strip()`ed (`utils/auth.py:467`). **Matching rule**: `request_path == allowed OR request_path.startswith(allowed + '/')` (`utils/auth.py:469`) — so it's exact-match-or-path-prefix (a segment boundary is enforced by the appended `/`, so `/api/v1/mod` would not match a request to `/api/v1/models`).
- Enforcement lives in `get_current_user_by_api_key` (`utils/auth.py:433-488`), which is the auth path taken whenever a bearer/cookie/`x-api-key` token starts with `sk-`. It checks, in order: `auth.enable_api_keys` must be true (else 403); if the caller is **not** `admin`, `features.api_keys` permission must be true for them (via `has_permission`, which unions group permissions — see below); **then, unconditionally regardless of role**, if `auth.api_key.endpoint_restrictions` is on, the raw ASGI `request.scope['path']` (explicitly *not* something derived from the `Host` header — a comment cites `CVE-2026-48710`) must match one of the allowed prefixes or the request gets 403'd. **So yes — the endpoint restriction applies to admin users' API keys too**; only the `features.api_keys` *minting/using-at-all* permission check is skipped for admins, not the endpoint allowlist.
- Minting a key: `POST /api/v1/auths/api_key` (`routers/auths.py:1466-1487`), auth'd via `Depends(get_current_user)` — i.e. requires an existing session (cookie or JWT bearer), not an existing API key, since none exists yet. Gated by the same `_check_api_key_permission` helper (`routers/auths.py:1454-1462`): requires `auth.enable_api_keys` true, and for non-admins requires `features.api_keys` permission. There are matching `GET`/`DELETE /api_key` endpoints under the same guard. Each user has at most one API key (`Users.update_user_api_key_by_id` overwrites).

**Default-permissions env vars** (`config.py:1699-1874` and following; pattern confirmed via `docs.openwebui.com` cross-check for `DEFAULT_USER_ROLE`): every leaf of the permissions tree has a `USER_PERMISSIONS_<SECTION>_<KEY>` env var, e.g. `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS` (default `False`), `USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS`, `USER_PERMISSIONS_ACCESS_GRANTS_ALLOW_USERS` (default `True`), `USER_PERMISSIONS_CHAT_CONTROLS` (default `True`), etc. — confirmed present in `config.py` for every field of the shapes below.

**Full `permissions` dict shape** (Pydantic models in `routers/users.py:175-266`, backing both the group `permissions` column and the default-permissions env block; note this is a **superset** of what the question guessed — there are six top-level sections, not four):

- `workspace`: `models`, `knowledge`, `prompts`, `tools`, `skills` (all default `False`) — access flags — plus `models_import`, `models_export`, `prompts_import`, `prompts_export`, `tools_import`, `tools_export`, `skills_import`, `skills_export` (all default `False`).
- `sharing`: `models`, `public_models`, `knowledge`, `public_knowledge`, `prompts`, `public_prompts`, `tools` (all default `False`), `public_tools` (default `True`), `skills`, `public_skills`, `notes` (default `False`), `public_notes` (default `True`), `folders`, `public_chats`, `public_calendars` (all default `False`).
- `access_grants`: `allow_users` (default `True`), `allow_groups` (default `True`).
- `chat`: `controls`, `valves`, `system_prompt`, `params`, `file_upload`, `web_upload`, `delete`, `delete_message`, `continue_response`, `regenerate_response`, `rate_response`, `edit`, `share`, `export`, `import` (aliased from `import_`), `stt`, `tts`, `call`, `multiple_models`, `temporary` (all default `True`), `temporary_enforced` (default `False`).
- `features`: `api_keys` (default `False`), `notes`, `channels`, `folders` (default `True`), `direct_tool_servers` (default `False`), `web_search`, `image_generation`, `code_interpreter`, `memories`, `calendar` (default `True`), `automations`, `webhooks` (default `False`).
- `settings`: `interface` (default `True`).

**Effective-permissions computation** (`utils/access_control/__init__.py:32-69`, `get_permissions(user_id, default_permissions, db)`):
1. Start from a deep copy of `default_permissions` (the env-derived `user.permissions` config value, i.e. `DEFAULT_USER_PERMISSIONS`).
2. Fetch every group the user belongs to (`Groups.get_groups_by_member_id`).
3. For each group, recursively `combine_permissions`: for every leaf key present in the group's `permissions` dict, `permissions[key] = permissions[key] or group_value` — **boolean OR, i.e. union / most-permissive-wins across all groups** (explicit comment: "most permissive value is used (True > False)").
4. `fill_missing_permissions` backfills any key a group didn't specify from the defaults, so the result always has every leaf.
5. **A user in zero groups gets exactly `default_permissions` unchanged** — the loop over `user_groups` simply doesn't execute, so nothing modifies the deep-copied defaults.

`has_permission(user_id, permission_key, default_permissions, db)` (same file, `:72-...`) does a similar per-group OR walk but is used for point checks (e.g. `features.api_keys` in the API-key code above) and falls back to `default_permissions` if no group grants it.

---

## F. Sessions / audit / misc env — CONFIRMED

All in `env.py` unless noted:

- `JWT_EXPIRES_IN` — `config.py:2432`, default `'4w'`. Format parsed by `parse_duration` (`utils/misc.py:899-927`): regex `(-?\d+(\.\d+)?)(ms|s|m|h|d|w)`, so multi-unit strings like `1d12h` sum together; `'-1'` or `'0'` means "never expires" (`parse_duration` returns `None`, and `create_session_response` then omits an expiry — `routers/auths.py:183-186`). `'12h'` is valid.
- `WEBUI_SESSION_COOKIE_SECURE` — `env.py:724`, default `'false'`.
- `WEBUI_SESSION_COOKIE_SAME_SITE` — `env.py:723`, default `'lax'`.
- `WEBUI_AUTH_COOKIE_SAME_SITE` — `env.py:725`, defaults to whatever `WEBUI_SESSION_COOKIE_SAME_SITE` resolved to (i.e. same value unless overridden separately).
- `WEBUI_AUTH_COOKIE_SECURE` — `env.py:726-731`, defaults to whatever `WEBUI_SESSION_COOKIE_SECURE` resolved to. These are the two cookie attributes actually applied when setting the `token` cookie in `create_session_response` (`routers/auths.py:193-204`, `samesite=WEBUI_AUTH_COOKIE_SAME_SITE, secure=WEBUI_AUTH_COOKIE_SECURE`).
- `AUDIT_LOG_LEVEL` — `env.py:1160`, default `'NONE'`; valid values per the inline comment and docs cross-check: `NONE | METADATA | REQUEST | REQUEST_RESPONSE`.
- `AUDIT_LOGS_FILE_PATH` — `env.py:1150`, default `f'{DATA_DIR}/audit.log'`.
- `AUDIT_EXCLUDED_PATHS` — `env.py:1168-1173`, default `'/chats,/chat,/folders'` (comma-split, each entry has its leading `/` stripped internally, so it's stored as `chats,chat,folders`).
- `ENABLE_ADMIN_CHAT_ACCESS` — `config.py:2070`, default `'True'`.
- `BYPASS_ADMIN_ACCESS_CONTROL` — `config.py:2062-2068`, default `'True'` (falls back to the older `ENABLE_ADMIN_WORKSPACE_CONTENT_ACCESS`, also default `True`).
- `WEBUI_NAME` — `env.py:891`, default `'Open WebUI'`.
- `WEBUI_URL` — `config.py:1620`, default `''`.
- `DEFAULT_USER_ROLE` — `config.py:1699`, default `'pending'`.
- `ENABLE_SIGNUP` — `config.py:1623`, default: `False` if `WEBUI_AUTH` is falsy, else `os.getenv('ENABLE_SIGNUP', 'True')` — i.e. defaults `True` under normal auth-enabled operation.
- `BYPASS_EMBEDDING_AND_RETRIEVAL` — `config.py:949`, default `'False'`.
- `STORAGE_PROVIDER` — `config.py:144`, default `'local'` (comment notes `s3` as an alternative).
- `DATA_DIR` — `env.py:222` (and platform-specific variants below it for HF Spaces/legacy paths), default `BACKEND_DIR / 'data'`.
- `ENABLE_OLLAMA_API` — `config.py:227`, default `'True'`.
- `ENABLE_OPENAI_API` — `config.py:309`, default `'True'`.
- Health endpoints, all in `main.py`: `GET /health` (`main.py:2768-2770`, unconditional `{"status": True}`, used by the Docker `HEALTHCHECK`, see item H), `GET /health/db` (`main.py:2814-2818`, pings the DB and 500s on failure), and (not asked for but relevant to a deployment plan) `GET /ready` (`main.py:2773-2811`) which additionally checks `app.state.startup_complete` and Redis connectivity if Redis is configured — this is the one to point a k8s readiness probe at rather than `/health`.

Doc cross-check (`docs.openwebui.com/reference/env-configuration/`) matched source exactly for every field the fetch found content for (`ENABLE_PERSISTENT_CONFIG`, `DEFAULT_USER_ROLE`, `ENABLE_SIGNUP`, `BYPASS_ADMIN_ACCESS_CONTROL`, `AUDIT_LOG_LEVEL`, `AUDIT_LOGS_FILE_PATH`); no contradictions found. The fetch didn't surface `ENABLE_API_KEYS`/`JWT_EXPIRES_IN`/cookie vars/`DATABASE_*`/`WEBUI_SECRET_KEY` on that page (likely just fetch-tool page-size truncation, not necessarily absent from the docs) — those are reported here from source only.

---

## G. Postgres — CONFIRMED

**`DATABASE_URL` driver handling** (`internal/db.py`): plain `postgresql://` works fine and does **not** need to be written as `postgresql+psycopg://` by the operator — the app rewrites it internally for its async engine:

```python
# _make_async_url, internal/db.py:209-232
if url.startswith('postgresql+psycopg2://'):
    return url.replace('postgresql+psycopg2://', 'postgresql+psycopg://', 1)
if url.startswith('postgresql://'):
    return url.replace('postgresql://', 'postgresql+psycopg://', 1)
if url.startswith('postgres://'):
    return url.replace('postgres://', 'postgresql+psycopg://', 1)
```

The **sync** engine (used for Alembic migrations and a few sync code paths) is built from the *unmodified* `SQLALCHEMY_DATABASE_URL` (`internal/db.py:314-329`, plain `create_engine(...)`), so it goes through SQLAlchemy's default dialect selection for `postgresql://`, which is `psycopg2`. **Both drivers ship in the image**: `backend/requirements.txt` pins `psycopg[binary]==3.3.4` (used by the async engine, auto-selected via the URL rewrite above) and `psycopg2-binary==2.9.12` (used by the sync engine/Alembic). An operator can hand this app a bare `postgresql://user:pass@host/db` and both code paths work without modification.

SSL query params (`sslmode`, etc.) are stripped from the URL and reattached as a libpq-compatible dict for both engines (`internal/db.py:67-149`), so `?sslmode=require` in `DATABASE_URL` is honored.

**Pool env vars**, all in `env.py:301-324`, parsed defensively (fall back to the default on any parse error):
- `DATABASE_POOL_SIZE` — default `None` (unset ⇒ SQLAlchemy's own default pool, `NullPool`/default `QueuePool` behavior depending on code path — see `internal/db.py:315-329`; only becomes an explicit `QueuePool(pool_size=...)` when this is set to a positive int).
- `DATABASE_POOL_MAX_OVERFLOW` — default `0`.
- `DATABASE_POOL_TIMEOUT` — default `30` (seconds).
- `DATABASE_POOL_RECYCLE` — default `3600` (seconds).

**Postgres extensions**: no `CREATE EXTENSION` statements exist anywhere under `backend/open_webui/migrations/versions/` — grepped the whole directory tree, zero hits. So the Alembic migration chain itself requires no pre-created extension and generates all IDs in Python (`str(uuid.uuid4())`), not via a Postgres-side function. The **only** `CREATE EXTENSION` statements in the whole tree are in `retrieval/vector/dbs/pgvector.py:114-140` — `vector` and `pgcrypto` — and those only run, at runtime (not migration time), when `VECTOR_DB=pgvector` is selected as the retrieval backend, and only if the corresponding flags `PGVECTOR_CREATE_EXTENSION` / `PGVECTOR_PGCRYPTO` are enabled; each is wrapped in `IF NOT EXISTS` and a defensive check to tolerate managed-Postgres offerings (Azure is called out by name in a comment) where the app's role may lack `CREATE EXTENSION` privilege. **Conclusion for a deployment plan targeting the main relational DB (not pgvector): no extension needs to be pre-provisioned.** If pgvector is chosen as the vector store, the DB role either needs `CREATE EXTENSION` privilege or the operator must pre-install `vector`/`pgcrypto` and turn those two flags off.

**`WEBUI_SECRET_KEY` handling in `backend/start.sh`** (the container entrypoint):

```bash
KEY_FILE="${WEBUI_SECRET_KEY_FILE:-.webui_secret_key}"
WEBUI_SECRET_KEY_LENGTH="${WEBUI_SECRET_KEY_LENGTH:-24}"
...
if [[ -z "${WEBUI_SECRET_KEY:-}" && -z "${WEBUI_JWT_SECRET_KEY:-}" ]]; then
  if [[ ! -f "$KEY_FILE" ]]; then
    head -c "$WEBUI_SECRET_KEY_LENGTH" /dev/random | base64 > "$KEY_FILE"
  fi
  WEBUI_SECRET_KEY=$(cat "$KEY_FILE")
fi
```

So yes: **`WEBUI_SECRET_KEY_FILE` is supported** (defaults to `.webui_secret_key`, relative to the script's own directory since it `cd`s there first) — set it to point at a mounted secret file and the entrypoint will read (or, if absent, generate-then-persist) the key there. `WEBUI_SECRET_KEY_LENGTH` (default `24` bytes) controls the length of an auto-generated key. Note this generation logic lives only in `start.sh`; `env.py:716-719` itself has **no hardcoded fallback** for `WEBUI_SECRET_KEY` — if it's empty at Python import time and `WEBUI_AUTH` is true, the app raises `SystemExit` (confirmed by the comment and the `if WEBUI_AUTH and WEBUI_SECRET_KEY == '': raise SystemExit(...)` immediately below the env read). So a deployment that bypasses `start.sh` (e.g. runs `uvicorn` directly, or overrides the container `CMD`) must set `WEBUI_SECRET_KEY` itself — the generate-to-file behavior will not happen.

---

## H. Image — CONFIRMED, plus extra variants found

**Image reference and variants**: from `.github/workflows/docker.yaml`, the published image is `ghcr.io/open-webui/open-webui` (`REGISTRY: ghcr.io`, `IMAGE_NAME=${GITHUB_REPOSITORY,,}`), built with a build matrix whose `suffix` values are `""` (main/CPU image), `"-cuda"`, `"-cuda126"`, `"-ollama"`, and `"-slim"` — so for this tag the pullable references are `ghcr.io/open-webui/open-webui:v0.11.0`, `:v0.11.0-cuda`, `:v0.11.0-cuda126`, `:v0.11.0-ollama`, and `:v0.11.0-slim`. (The `-cuda126` and `-slim` variants weren't mentioned in the question but exist in this tag's release workflow — worth knowing if pinning by digest per variant.)

**`HEALTHCHECK`**: yes, defined in the repo `Dockerfile`:

```dockerfile
EXPOSE 8080
HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1
```

i.e. it curls `GET /health` and asserts the JSON body's `status` field is `true` via `jq`.

**Port**: `8080` (`EXPOSE 8080`, and `start.sh` defaults `PORT="${PORT:-8080}"` for the uvicorn bind).

**User**: build args `ARG UID=0` / `ARG GID=0` (top of `Dockerfile`), applied via `USER $UID:$GID` near the end — **the shipped image runs as root (UID 0 / GID 0) by default**. The Dockerfile comment is explicit: "Override at your own risk - non-root configurations are untested." There's an opt-in `USE_PERMISSION_HARDENING=true` build arg that additionally chgrps `/app` and `/root` to group `0` with SGID directories, aimed at OpenShift's arbitrary-UID model, but it does not change the default UID/GID — an operator wanting non-root must pass `--build-arg UID=<n> --build-arg GID=<n>` themselves and accept it's not a tested configuration upstream.

---

## Summary of anything not verified / docs-only / flagged

- Nothing in this note rests on a blog post or third-party write-up; every claim above cites a specific file/line/function in the `v0.11.0` tag tree, or a specific `docs.openwebui.com` page fetched/searched live.
- Two doc cross-checks (`env-configuration`, LDAP page) matched source with no contradictions found on the fields checked; a few env vars (`ENABLE_API_KEYS`, `JWT_EXPIRES_IN`, cookie vars, `DATABASE_URL`/`DATABASE_POOL_SIZE`, `WEBUI_SECRET_KEY`) were not surfaced by the specific doc fetch used (likely a page-length/extraction limit of the fetch tool rather than genuine absence from the docs site) — those items are reported from source only and should be treated as unconfirmed-against-docs, not doc-contradicted.
- The LDAP group-removal-on-sync behavior and the exact `LDAP_SEARCH_FILTERS` splicing (unparenthesized) are confirmed only by reading `routers/auths.py`/`models/groups.py` directly; the docs summaries found describe the feature at a higher level and don't spell out either mechanic, so don't assume the docs alone would catch a misconfigured filter string.
- `UserUpdateForm.role` has no server-side enum validation — a typo'd role string would be accepted and stored, silently breaking that user's access, since only `'admin'`/`{'user','admin'}` membership checks are hardcoded elsewhere. Worth a client-side/ops-side guard in any automation that calls this endpoint.

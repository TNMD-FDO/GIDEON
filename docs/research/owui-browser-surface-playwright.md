---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
  - pin: dev.playwright
    version: 1.62.0
---
# Open WebUI v0.11.3's browser surface, and Playwright for Python (current release) — the properties a headless-Chromium driver needs

Source of truth for Open WebUI: `git clone --depth 1 --branch v0.11.3 https://github.com/open-webui/open-webui.git`,
resolved to commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (tagged `v0.11.3`, matches the pin used by the
companion notes below). Backend paths are relative to `backend/open_webui/`; frontend paths to `src/`.
`docs.openwebui.com` was not consulted anywhere below — every Part A claim is read directly from this tag's
source, and every decisive line is quoted.

Source of truth for Playwright: `playwright.dev`'s Python docs sources at
`github.com/microsoft/playwright`, tag `v1.62.0` (the release on PyPI at research time, see Part B §1), plus
`github.com/microsoft/playwright-python` tag `v1.62.0`, plus PyPI's JSON API for the package/file metadata.
Chromium's own documentation (`chromium.googlesource.com`) is cited for the NSS/TLS-trust and proxy claims.

Companion notes, not repeated here except where cited: `docs/research/owui-filter-function.md` (the outlet
hook, the `chat:outlet` socket event and how the client patches `history.messages`, the `ContentRenderer`
render-preference of `output` over `content`); `docs/research/owui-chat-routes-machine-caller.md` (the
session-token sign-in/read-back/delete routes a machine caller uses, and Redis-less sign-out's inert
server-side revocation). Both are treated as already-verified per this ticket's brief and are cited, not
re-derived.

---

# Part A — Open WebUI v0.11.3's browser surface

## 1. The auth page: LDAP form, submit route, success/failure handling

### 1.1 Which form renders, and how the page picks LDAP mode

`src/routes/auth/+page.svelte:34`:

```js
let mode = $config?.features.enable_ldap ? 'ldap' : 'signin';
```

`mode` is a plain reactive-once local variable set at component init from the backend config's
`features.enable_ldap` flag — **not** re-evaluated against signup/login-form flags. The whole
name/email/password block, including this branch, is gated at the container level
(`src/routes/auth/+page.svelte:293`):

```svelte
{#if $config?.features.enable_login_form || $config?.features.enable_ldap || form}
```

so with `ENABLE_LDAP=true` and no `?form=` query param, this block renders regardless of
`enable_login_form`, and `mode === 'ldap'` selects the LDAP branch inside it.

### 1.2 The LDAP username/password fields — labelled and typed as a username, not an email

`src/routes/auth/+page.svelte:315-330`:

```svelte
{#if mode === 'ldap'}
	<div class="mb-2">
		<label for="username" class="text-sm font-normal text-left mb-1 block"
			>{$i18n.t('Username')}</label
		>
		<input
			bind:value={ldapUsername}
			type="text"
			class="my-0.5 w-full text-sm outline-hidden bg-transparent placeholder:text-gray-300 dark:placeholder:text-gray-600"
			autocomplete="username"
			name="username"
			id="username"
			placeholder={$i18n.t('Enter Your Username')}
			required
		/>
	</div>
{:else}
	<!-- email input, mode !== 'ldap' -->
```

So: `id="username"`, `name="username"`, `type="text"` (not `type="email"`), `autocomplete="username"`,
placeholder `"Enter Your Username"`, label text `"Username"` — confirmed labelled as a username, never
as an email, in the LDAP branch. The password field is shared across all three modes
(`:353-364`, a `SensitiveInput` wrapping a real `<input>`): `id="password"`, `name="password"`,
`type="password"`, `autocomplete="current-password"` (since `mode !== 'signup'`), `required`,
`aria-required="true"`.

### 1.3 The submit button

`src/routes/auth/+page.svelte:390-403`, the `mode === 'ldap'` branch:

```svelte
<button
	class="bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5 disabled:opacity-50 flex justify-center"
	type="submit"
	disabled={submitting}
>
	<div class="self-center">{$i18n.t('Authenticate')}</div>
	{#if submitting}
		<div class="ml-1.5 self-center"><Spinner /></div>
	{/if}
</button>
```

Text is `"Authenticate"` (not "Sign in" — that text is reserved for the non-LDAP branch), `type="submit"`,
no `id`/`aria-label` of its own — a Playwright locator should target it by role+text (`get_by_role("button",
name="Authenticate")`) or by the enclosing `<form>`'s submit. The form itself submits via
`on:submit={(e) => { e.preventDefault(); submitHandler(); }}` (`:266-270`), and `submitHandler`
(`:108-124`) dispatches to `ldapSignInHandler` when `mode === 'ldap'`.

### 1.4 The route the LDAP submit calls

`src/lib/apis/auths/index.ts:113-138`, `ldapUserSignIn`, in full:

```ts
export const ldapUserSignIn = async (user: string, password: string) => {
	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}/auths/ldap`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		credentials: 'include',
		body: JSON.stringify({ user: user, password: password })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});
	if (error) { throw error; }
	return res;
};
```

`POST ${WEBUI_API_BASE_URL}/auths/ldap` — body keys are **`user`** and **`password`** (not `username`,
not `email`); `credentials: 'include'` so the response's `Set-Cookie` is honored. On a non-2xx response
the caught `error` is `err.detail` (the backend's error-detail string) and is re-thrown.

### 1.5 What a successful sign-in does — token storage, and the redirect

`src/routes/auth/+page.svelte`, `ldapSignInHandler` (`:100-105`) calls `setSessionUser` (`:47-69`), in
full:

```js
const setSessionUser = async (sessionUser, redirectPath: string | null = null) => {
	if (sessionUser) {
		console.log(sessionUser);
		toast.success($i18n.t(`You're now logged in.`));
		if (sessionUser.token) {
			localStorage.token = sessionUser.token;
		}
		$socket.emit('user-join', { auth: { token: sessionUser.token } });
		await user.set(sessionUser);
		await config.set(await getBackendConfig());

		const timezone = getUserTimezone();
		if (sessionUser.token && timezone) {
			updateUserTimezone(sessionUser.token, timezone);
		}

		if (!redirectPath) {
			redirectPath = $page.url.searchParams.get('redirect') || '/';
		}
		goto(redirectPath);
		localStorage.removeItem('redirectPath');
	}
};
```

**Token storage is `localStorage.token`** (plain `localStorage`, not `sessionStorage`), set from the
response body's `token` field — the same JSON body shape the `/auths/signin` route returns per
`docs/research/owui-chat-routes-machine-caller.md` §7.1 (`{token, token_type, expires_at, id, email,
name, role, ...}`). A `token` cookie is *also* set by the server (`credentials:'include'` on the fetch,
and the backend's `create_session_response(..., set_cookie=True, ...)` per that note) but the frontend
never reads it back from a cookie on this path — it stores the JSON body's `token` directly. The success
toast text is `"You're now logged in."`; the redirect target defaults to `/` (`$page.url.searchParams.get('redirect') || '/'`)
and uses SvelteKit's `goto`, not a full page navigation.

### 1.6 A failed LDAP sign-in

`src/routes/auth/+page.svelte:100-105`, `ldapSignInHandler`:

```js
const ldapSignInHandler = async () => {
	const sessionUser = await ldapUserSignIn(ldapUsername, password).catch((error) => {
		toast.error(`${error}`);
		return null;
	});
	await setSessionUser(sessionUser);
};
```

`toast.error(\`${error}\`)` — the thrown value from §1.4 is `err.detail` (a string, per the backend's
`HTTPException(400, detail=...)` shape for bad LDAP credentials), so the toast text is exactly the
backend's `detail` string. `setSessionUser(null)` then no-ops (the whole body is `if (sessionUser)`),
so on failure nothing is stored and no redirect happens — the page stays on `/auth`.

### 1.7 No "sign in with email" toggle unless a login form is also enabled

`src/routes/auth/+page.svelte:591-606`:

```svelte
{#if $config?.features.enable_ldap && $config?.features.enable_login_form}
	<div class="mt-2">
		<button
			class="flex justify-center items-center text-xs w-full text-center underline"
			type="button"
			on:click={() => {
				if (mode === 'ldap') mode = ($config?.onboarding ?? false) ? 'signup' : 'signin';
				else mode = 'ldap';
			}}
		>
			<span>{mode === 'ldap' ? $i18n.t('Continue with Email') : $i18n.t('Continue with LDAP')}</span>
		</button>
	</div>
{/if}
```

**The toggle requires both `enable_ldap` and `enable_login_form`.** If GIDEON's render sets
`ENABLE_LOGIN_FORM=false` alongside `ENABLE_LDAP=true` (not independently verified here — see
"Unverified"), this whole block never renders and the LDAP form is the only form on the page, with no
email-mode escape hatch visible.

---

## 2. The chat page and composer

### 2.1 The composer element: a ProseMirror contenteditable node, `id="chat-input"`

`src/lib/components/chat/MessageInput.svelte:2016-2017`:

```svelte
<RichTextInput
	bind:this={chatInputElement}
	id="chat-input"
	editable={!showInputModal}
	...
```

`RichTextInput.svelte` renders a plain wrapper `<div bind:this={element} ...>` (`:1381-1385`, no id of
its own) and mounts a TipTap/ProseMirror `Editor` into it (`:772-773`, `element: element`); the `id` prop
is applied to the **editor's own DOM node**, not the wrapper, via `editorProps.attributes`
(`RichTextInput.svelte:986`):

```js
editorProps: {
	attributes: () => ({ id, 'aria-label': _placeholder }),
	...
```

ProseMirror's own reference docs (`prosemirror.net/docs/ref/#view.EditorView`) confirm this DOM node
"will have its `contentEditable` attribute determined by the `editable` prop" — i.e. **the element
carrying `id="chat-input"` is the contenteditable node itself**, not a `<textarea>`. (`package.json:137`
pins `prosemirror-view: ^1.34.3`.) A Playwright locator should target `#chat-input` directly (or
`page.locator("#chat-input")`), which is the actual editable surface.

### 2.2 Submit is triggered by plain Enter (default settings) as well as the send button

Inside the editor's own keydown handler, a plain (non-shift) Enter with no special context (not in a
code block/list/heading, and `shiftEnter` not requested) falls through untouched
(`src/lib/components/common/RichTextInput.svelte:1168-1169`):

```js
eventDispatch('keydown', { event });
return false;
```

The parent composer's `on:keydown` handler on that same component then decides to submit
(`src/lib/components/chat/MessageInput.svelte:2109-2123`):

```js
const enterPressed = ($settings?.ctrlEnterToSend ?? false)
	? (e.key === 'Enter' || e.keyCode === 13) && isCtrlPressed
	: (e.key === 'Enter' || e.keyCode === 13) && !e.shiftKey;

if (enterPressed) {
	e.preventDefault();
	if (prompt !== '' || files.length > 0) {
		dispatch('submit', prompt);
	}
}
```

With default settings (`ctrlEnterToSend` unset/false), a plain Enter (no shift) submits whenever the
prompt is non-empty or a file is attached. The `<form>` around the composer also submits on the send
button's click (`type="submit"`, see §2.3), converging on the same `dispatch('submit', prompt)`
(`MessageInput.svelte:1723-1727`).

### 2.3 The send button, and what disables it; the stop button while a turn is running

`src/lib/components/chat/MessageInput.svelte:2685-2694`:

```svelte
<button
	id="send-message-button"
	class="{...}"
	type="submit"
	disabled={(prompt === '' && files.length === 0) || uploadPending}
>
```

`id="send-message-button"`, wrapped in a `Tooltip content={$i18n.t('Send message')}` (no explicit
`aria-label` on the button itself). Disabled exactly when the prompt is empty **and** no files are
attached, or while an upload is pending.

While a turn is in flight, this button is replaced by a **Stop** button
(`MessageInput.svelte:2551-2556`, rendered when `isActive && prompt === '' && files.length === 0`):

```svelte
<button aria-label={$i18n.t('Stop')} class="..." on:click={() => { stopResponse(); }}>
```

`isActive` (`MessageInput.svelte:167-171`):

```js
$: isActive =
	!askUser?.show &&
	((taskIds && taskIds.length > 0) ||
		(history.currentId && history.messages[history.currentId]?.done != true) ||
		generating);
```

— true while a background task is tracked, the current message isn't `done`, or `generating` is set.

### 2.4 The model selector: real button id, hidden-model filtering, display text

The composer wires a single `ModelSelector` with a **static, literal** `id="model"`
(`src/lib/components/chat/ModelSelector.svelte:66`), which its child `Selector.svelte` turns into the
actual DOM button id (`src/lib/components/chat/ModelSelector/Selector.svelte:994`):

```svelte
<button ... id="model-selector-{id}-button" ...>
```

**For GIDEON's single-model composer the real button id is `model-selector-model-button`** — not
`model-selector-0-button` (there is no per-index numbering anywhere in this component; `id` is the
literal string passed in, and `ModelSelector.svelte` never parameterizes it by index).

Hidden models are excluded from the dropdown by default (`Selector.svelte:370`):

```js
.filter((item) => includeHidden || !(item.model?.info?.meta?.hidden ?? false));
```

so `gideon-general`'s hidden base model never appears as a selectable row unless a caller explicitly
asks for `includeHidden`. The selected model's display name renders as plain text inside the button
(`Selector.svelte:1013`, fed by `triggerLabel = selectedModel.label`, `:252-255`):

```svelte
<span class="min-w-0 flex-1 truncate">{triggerLabel}</span>
```

and the button's own `aria-label` (`:989-991`) is `"Selected model: {{modelName}}"` (or the placeholder
text if nothing is selected).

### 2.5 The "+" button is a different menu from the web-search/tools "Integrations" menu

Two distinct trigger buttons exist beside the composer, and they are not the same control:

- **`id="input-menu-button"`, `aria-label="More"`** (`MessageInput.svelte:2237-2244`) opens
  `InputMenu.svelte` — file/attachment actions only: Tool Permissions (if
  `$config?.features?.enable_tool_permissions`), Upload Files, Capture, Attach Webpage, Attach Files
  (submenu), and Notes (if `$config?.features?.enable_notes`) (`InputMenu.svelte:144-305`). File-upload
  items are gated by `fileUploadEnabled = ... ($user?.role === 'admin' || $user?.permissions?.chat?.file_upload)`
  (`InputMenu.svelte:66-68`) — matches GIDEON's `chat.file_upload: true`. Attach Webpage is gated
  separately by `webUploadEnabled = $user?.role === 'admin' || ($user?.permissions?.chat?.web_upload ?? true)`
  (`:73`). **None of these items are web search, code interpreter, or image generation.**

- **`id="integration-menu-button"`, `aria-label="Integrations"`** (`MessageInput.svelte:2294-2299`)
  opens `IntegrationsMenu.svelte`, and only renders at all when at least one of
  `showWebSearchButton || showImageGenerationButton || showCodeInterpreterButton || showToolsButton
  || showSkillsButton || toggleFilters.length > 0` is true (`MessageInput.svelte:2260`). This is
  where the web-search/code-interpreter/image-generation toggles actually live.

`showWebSearchButton` (`MessageInput.svelte:803-808`):

```js
$: showWebSearchButton =
	selectedModelIds.length === webSearchCapableModels.length &&
	$config?.features?.enable_web_search &&
	($_user.role === 'admin' || $_user?.permissions?.features?.web_search);
```

— matches GIDEON's render (preset offers the `web_search` capability, `features.web_search: true`).
`showCodeInterpreterButton`/`showImageGenerationButton` mirror this against
`enable_code_interpreter`/`enable_image_generation` and `permissions.features.code_interpreter`/
`image_generation` — both off per GIDEON's render, so neither button (nor its menu row) appears.

The Web Search toggle itself, inside `IntegrationsMenu.svelte:394-416`:

```svelte
{#if showWebSearchButton}
	<button
		class="..."
		aria-pressed={webSearchEnabled}
		on:click={() => { webSearchEnabled = !webSearchEnabled; onWebSearchToggle(webSearchEnabled); }}
	>
		<div class="flex-1 truncate">...<div class="truncate">{$i18n.t('Web Search')}</div></div>
		<div class="shrink-0" inert><Switch state={webSearchEnabled} /></div>
	</button>
{/if}
```

**`aria-pressed={webSearchEnabled}` is the reliable on/off read** for a Playwright locator (`get_attribute("aria-pressed")`
or an `aria-pressed`-scoped locator). Once on, a separate chip button reappears directly in the
composer row (`MessageInput.svelte:2431-2447`, `{#if webSearchEnabled && showWebSearchButton}`, no
`aria-label` of its own, wrapped in `Tooltip content="Web Search"`) that toggles it back off on click.

`chat.controls` is unrelated to any of this — it gates a separate side panel
(`ChatControls.svelte:65`: `showControlsTab = $user?.role === 'admin' || ($user?.permissions?.chat?.controls
?? true)`), not the composer's menus.

### 2.6 The URL updates right after the initial POST resolves, not after the stream finishes

`submitPrompt` (`src/lib/components/chat/Chat.svelte:2869-2911`) calls `sendMessage`, which for a
socket-tracked turn calls `sendMessageSocket` (`:3401`). The completion payload
(`Chat.svelte:3588-3589`, already established) sends `session_id: $socket?.id` and
`chat_id: _chatId || undefined` — for a brand-new chat `_chatId` is empty, so per
`docs/research/owui-chat-routes-machine-caller.md` §1.5 the backend's fan-out branch fires (both
`session_id` and `chat_id`'s absence trigger it) and returns immediately with
`{status: true, task_ids: [...], chat_id: <minted id>}`, **before any streamed content exists**.
`Chat.svelte:3650-3665` handles that response:

```js
if (res.chat_id && $chatId !== res.chat_id && $chatId === _chatId) {
	...
	await chatId.set(res.chat_id);
	if (!$temporaryChatEnabled && !embedded) {
		window.history.replaceState(history.state, '', `/c/${res.chat_id}`);
		await refreshChatList(localStorage.token);
		...
```

**The URL flips to `/c/<chat id>` via `history.replaceState` as soon as this initial POST resolves** —
well before the SSE stream completes, not after the completion finishes. (A separate, unrelated
`replaceState` call at `Chat.svelte:3950` belongs to a different code path, `initChatHandler`, which is
only reached from the dev/debug `createMessagePair`/`addMessages` helpers, not from `submitPrompt`.)

---

## 3. The assistant message during and after streaming

### 3.1 Message container id, and the streaming/"done" markers

`src/lib/components/chat/Messages/ResponseMessage.svelte:660-661`:

```svelte
<div class=" flex w-full message-{message.id}" id="message-{message.id}">
```

While streaming (`:884-889`):

```svelte
{#if !message.done && !message.error && (hasResponseContent || !hasVisibleStatus)}
	<div class="text-[0.9375rem] leading-relaxed">
		<span class="inline-block w-[0.125rem] h-3.5 bg-gray-400 dark:bg-gray-500 ml-0.5 animate-pulse align-text-bottom"></span>
	</div>
{/if}
```

— a pulsing cursor span, present only while `!message.done`. The per-message action-buttons row
(regenerate/copy/etc.) is gated the opposite way (`:934`): `{#if message.done || siblings.length > 1}`.
**For a fresh single-turn chat, "done" is signalled by the pulsing cursor disappearing and the buttons
row appearing** — both driven by the same `message.done` boolean, which a Playwright
`page.wait_for_function` or `locator.wait_for_function` (new in this exact pinned release, v1.62 — see
Part B §5) can poll for directly if the harness has access to the in-page Svelte state, or more simply
by waiting for the buttons row's container (`bind:this={buttonsContainerElement}`, `:927`) to appear.

### 3.2 The inlet-refusal `<Error>` banner

`ResponseMessage.svelte:892-894`:

```svelte
{#if message?.error}
	<Error content={message?.error?.content ?? message.content} />
{/if}
```

`src/lib/components/chat/Messages/Error.svelte`, in full:

```svelte
<div class="my-1.5 flex w-full items-start gap-2 rounded-2xl bg-black/[0.03] px-3 py-2 text-gray-500 dark:bg-white/[0.04] dark:text-gray-400">
	<Info className="mt-0.5 size-4 shrink-0 text-gray-400 dark:text-gray-500" strokeWidth="1.8" />
	<div class="min-w-0 break-words text-[0.8125rem] leading-5">{message}</div>
</div>
```

No `id`/`data-testid`/distinguishing class beyond generic Tailwind utility classes and an `Info` icon —
**there is no stable selector for this banner**; a Playwright locator would need to scope to
`#message-<id>` and match by the icon+text structure, or (more robustly) simply read the error text back
from the stored chat (`GET /api/v1/chats/{id}`, per the machine-caller note) rather than scraping the DOM.

### 3.3 The reasoning collapsible is a custom `Collapsible.svelte`, not a native `<details>`

`src/lib/components/chat/Messages/Collapsible.svelte` — a `<div>` wrapping a `<button type="button"
aria-expanded={open}>`, not a native `<details>`/`<summary>` pair anywhere in this component tree (`grep`
for `<details` in the codebase only turns up `ResponseMessage.svelte`/`ContentRenderer.svelte`, unrelated
to reasoning rendering). The summary text, for a `type: 'reasoning'` item specifically
(`Collapsible.svelte:93-108`):

```svelte
{#if attributes?.type === 'reasoning'}
	{#if (attributes?.done === 'true' || messageDone) && attributes?.duration}
		{#if attributes.duration < 1}
			{$i18n.t('Thought for less than a second')}
		{:else if attributes.duration < 60}
			{$i18n.t('Thought for {{DURATION}} seconds', { DURATION: attributes.duration })}
		{:else}
			{$i18n.t('Thought for {{DURATION}}', { DURATION: dayjs.duration(attributes.duration, 'seconds').humanize() })}
		{/if}
	{:else if attributes?.done === 'true' || messageDone}
		{$i18n.t('Thought')}
	{:else}
		{$i18n.t('Thinking...')}
	{/if}
{:else if attributes?.type === 'code_interpreter'}
	...
{:else}
	{title}
{/if}
```

This branch **ignores the `title` prop entirely for reasoning items** — the `title` passed in from the
caller is `detailToken.summary`, itself built by `structuredOutput.ts`'s `buildReasoningToken`
(`:239-248`, `summary: isDone ? \`Thought for ${duration || 0} seconds\` : 'Thinking...'`), but that
string is dead for a reasoning item since `Collapsible.svelte`'s own inline logic (quoted above) takes
over and computes its own phrasing. **So the text actually on screen is**: `"Thinking..."` while not
done; once done, `"Thought for less than a second"` (duration < 1s), `"Thought for N seconds"`
(1s ≤ duration < 60s — this is the case the ticket's guess matches), or a humanized duration string for
60s and over.

**Open/closed state**: `aria-expanded={open}` on the button (`Collapsible.svelte:76`) is the reliable
signal. **Whether the reasoning text is in the DOM while collapsed: no.** The content slot is wrapped
in a Svelte `{#if open && !hide}` (`Collapsible.svelte:176-181`, the non-`grow` branch used here), which
Svelte does not merely hide with CSS — it unmounts the block entirely when `open` is false. A fresh
reasoning collapsible starts **closed** by default: every call site passes `open={$settings?.expandDetails
?? false}` (`StructuredOutputRenderer.svelte:121/128/179/186`), and `expandDetails` defaults `false`.

### 3.4 The `chat:outlet` re-render — cited from the companion note

`docs/research/owui-filter-function.md` §5.4 (`Chat.svelte:1277-1291`, patches `history.messages[msg.id]`
in place, gated on `existing.content !== msg.content`) and §5.5 (`ContentRenderer.svelte:283-296`,
renders `output` in preference to `content` whenever `output` is a non-empty array, "unconditional on
the value of `content`") together establish that an outlet replacement's `output` item — not just its
`content` string — is what actually reaches the screen, and that the reasoning block only disappears
from a *live* render if the replacement's `output` array is new/non-empty/different (a falsy clear does
not persist server-side and does not force-clear client-side either). Not re-derived here; see that
note for the exact code.

---

## 4. Transport and every origin the page touches

### 4.1 Socket.IO: path, transport order, reconnection, and the version-mismatch reload

`src/routes/+layout.svelte:164-173`:

```js
const setupSocket = async (enableWebsocket) => {
	const _socket = io(`${WEBUI_BASE_URL}` || undefined, {
		reconnection: true,
		reconnectionDelay: 1000,
		reconnectionDelayMax: 5000,
		randomizationFactor: 0.5,
		path: '/ws/socket.io',
		transports: enableWebsocket ? ['websocket'] : ['polling', 'websocket'],
		auth: { token: localStorage.token }
	});
```

called as `setupSocket($config.features?.enable_websocket ?? true)` (`:1263`) — **defaults to
websocket-only transport** (no polling fallback) unless the backend config explicitly turns
`enable_websocket` off, in which case it's polling-first with an upgrade to websocket. Path is the
literal string `/ws/socket.io` (Socket.IO appends its own query string/trailing behavior; the base
path has no trailing slash in this source). On `connect` (`:180-210`), the client calls `getVersion()`
and compares `deployment_id`/`version` against the client's own build constants; on a mismatch it
unregisters any service workers and force-reloads the page (`location.href = location.href`). A
heartbeat (`_socket.emit('heartbeat', {})`) fires every `$config?.features?.websocket_heartbeat_interval
?? 30` seconds while connected (`:213-221`).

### 4.2 No foreign-origin fetches in a stock build

`src/app.html` contains no external `<script src>`, no `<link rel="stylesheet">` to a CDN, and no
`<link rel="preconnect">` — every icon/manifest reference is a same-origin `/static/...` or
`/manifest.json` path. Fonts are self-hosted (`src/app.css:3-11`):

```css
@font-face { ... src: url('/assets/fonts/Inter-Variable.ttf'); }
@font-face { ... src: url('/assets/fonts/Vazirmatn-Variable.ttf'); }
```

— no `fonts.googleapis.com`/`fonts.gstatic.com` reference anywhere in `src/app.css` or
`src/tailwind.css`. **No service worker is registered anywhere in the frontend source** — a repo-wide
grep for `serviceWorker.register`/`navigator.serviceWorker.register` in `src/` returns nothing; the only
`navigator.serviceWorker` usage is `unregisterServiceWorkers` (`+layout.svelte:84-101`), legacy cleanup
code that only ever *removes* prior registrations (called on version mismatch and once at mount,
`:101`/`:210`). The one route that could reach an external origin from the browser's perspective,
`GET /api/version/updates`, is itself same-origin (`src/lib/apis/index.ts:1559`:
`fetch(\`${WEBUI_BASE_URL}/api/version/updates\`, ...)`) and is doubly gated off for GIDEON's test
account: `if ($user?.role === 'admin' && $config?.features?.enable_version_update_check)`
(`src/routes/(app)/+layout.svelte:400`) — `gideon-eval` is `user`-role and the update check is off, so
this call never fires at all. **Conclusion: a stock v0.11.3 build touches exactly one origin
(`WEBUI_BASE_URL`, for both HTTP and the Socket.IO connection); an allowlist that aborts every request
not addressed to that host will not break the page.**

---

## 5. Sign-out

`src/lib/apis/auths/index.ts:382-407`, `userSignOut`, in full:

```ts
export const userSignOut = async () => {
	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}/auths/signout`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		credentials: 'include'
	})
		.then(async (res) => { if (!res.ok) throw await res.json(); return res.json(); })
		.catch((err) => { console.error(err); error = err.detail; return null; });
	if (error) { throw error; }
	sessionStorage.clear();
	return res;
};
```

`POST ${WEBUI_API_BASE_URL}/auths/signout` (matches the backend's `@router.post('/signout')`, per
`docs/research/owui-chat-routes-machine-caller.md` §7.3). On success it clears `sessionStorage` — **it
does not itself touch `localStorage.token`**. The UI call site does that separately
(`src/lib/components/layout/Sidebar/UserMenu.svelte:569-577`):

```js
const res = await userSignOut();
localStorage.removeItem('token');
location.href = getLogoutRedirectUrl(res?.redirect_url);
```

So a full sign-out is two client-side steps (`userSignOut()` then `localStorage.removeItem('token')`)
plus one navigation. Server-side, `docs/research/owui-chat-routes-machine-caller.md` §7.3 already
establishes: the `/auths/signout` route calls `response.delete_cookie('token')` (a real cookie clear)
and attempts `invalidate_token`, but on GIDEON's Redis-less render that invalidation write (and the
read every subsequent request would need to check) both no-op — **the bearer JWT itself remains valid
until its natural expiry even after calling signout**. For a Playwright harness holding the raw token
in memory (never relying on the cookie), calling `/auths/signout` clears server + browser cookie/storage
state but does **not** revoke the token string itself on this render; the harness should treat the
token as live until its `expires_at` regardless of whether it also drives a UI sign-out.

---

# Part B — Playwright for Python, current release

## 1. The current release, its Chromium, Python-version support, and the previous release

### 1.1 Version and date

PyPI (`pypi.org/pypi/playwright/json`, fetched live): **`playwright` 1.62.0**, uploaded **2026-07-31**.
Previous release: **1.61.0**, uploaded **2026-06-29** (fallback if 1.62.0 needs to be rolled back).

### 1.2 The Chromium build it bundles

`playwright-python` pins its Node driver by a `DRIVER_VERSION` file, one source of truth for the whole
release train (`microsoft/playwright-python`, tag `v1.62.0`, `DRIVER_VERSION`, fetched raw):

```
1.62.0
```

— i.e. the Python package's version and the driver's version are the same string at this release. The
driver's own `packages/playwright-core/browsers.json` at tag `v1.62.0` (`microsoft/playwright`,
fetched raw), the relevant entries:

```json
{
  "name": "chromium",
  "revision": "1234",
  "installByDefault": true,
  "browserVersion": "151.0.7922.34",
  "title": "Chrome for Testing"
},
{
  "name": "chromium-headless-shell",
  "revision": "1234",
  "installByDefault": true,
  "browserVersion": "151.0.7922.34",
  "title": "Chrome Headless Shell"
},
```

**Chromium build: revision `1234`, browser version `151.0.7922.34`** ("Chrome for Testing"), identical
for both the headed `chromium` binary and the `chromium-headless-shell` binary at this release.

### 1.3 Python-version support: no cp3xx/abi3 wheel tag at all — the package is pure Python

PyPI's file listing for `playwright==1.62.0` (fetched live):

| filename | tag |
|---|---|
| `playwright-1.62.0-py3-none-manylinux1_x86_64.whl` | `py3-none-manylinux1_x86_64`, 47,748,926 bytes |
| `playwright-1.62.0-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` | 47,441,423 bytes |
| `playwright-1.62.0-py3-none-macosx_10_13_x86_64.whl` | 43,732,091 bytes |
| `playwright-1.62.0-py3-none-macosx_11_0_arm64.whl` | 42,510,842 bytes |
| `playwright-1.62.0-py3-none-macosx_11_0_universal2.whl` | 43,732,093 bytes |
| `playwright-1.62.0-py3-none-win32.whl` / `win_amd64.whl` / `win_arm64.whl` | 38,164,450 / 38,164,458 / 34,208,868 bytes |

Every wheel is tagged **`py3-none-<platform>`** — there is no `cp3xx`/`abi3` component at all, because
the Python bindings are pure Python (they drive the Node.js-based `playwright` driver as a subprocess;
no C extension is compiled). `info.requires_python` is **`>=3.10`**, with no upper bound. **So "does it
support 3.14" reduces to "do its declared dependencies support 3.14," not to a wheel-tag question**:

- `info.requires_dist` (fetched live) pins **`greenlet<4.0.0,>=3.1.1`** and **`pyee<14,>=13`**.
- `greenlet` 3.5.5 (its current release, satisfying `<4.0.0`) ships linux x86_64 wheels tagged
  `cp310` through `cp315`, **including `cp314` and `cp314t`** (free-threaded), for
  `manylinux_2_24_x86_64.manylinux_2_28_x86_64` (PyPI JSON, fetched live) — Python 3.14 is covered.
- `pyee` 13.0.0 (satisfying `<14,>=13`) ships a single **`pyee-13.0.0-py3-none-any.whl`**,
  `requires_python >= 3.8` (PyPI JSON, fetched live) — pure Python, version-independent, trivially
  compatible with 3.14.

**Conclusion: `playwright` 1.62.0 installs cleanly on Python 3.14 x86_64 Linux** — there is no
`cp314`/`abi3` gate to clear for the `playwright` wheel itself, and both of its native/pinned
dependencies already publish what 3.14 needs. The linux x86_64 wheel (`manylinux1_x86_64`) is
**47,748,926 bytes** (≈ 45.5 MiB); this does not include the browser binaries, which `playwright
install` downloads separately (§2).

---

## 2. `playwright install chromium`: hosts, proxy, cache dir, and the headless-shell split

### 2.1 Download hosts, and proxy honoring

`packages/playwright-core/src/server/registry/index.ts:47-50` (tag `v1.62.0`, fetched raw):

```ts
const PLAYWRIGHT_CDN_MIRRORS = [
  'https://cdn.playwright.dev/dbazure/download/playwright', // ESRP CDN
  'https://playwright.download.prss.microsoft.com/dbazure/download/playwright', // Directly hit ESRP CDN
  'https://cdn.playwright.dev', // Hit the Storage Bucket directly
];
```

**Two hostnames to allowlist: `cdn.playwright.dev` and `playwright.download.prss.microsoft.com`**
(the third mirror reuses the first hostname on a different path). The docs
(`docs/src/browsers.md:596-602`, tag `v1.62.0`) confirm proxy honoring directly:

```
By default, Playwright downloads browsers from Microsoft's CDN.
...
HTTPS_PROXY=https://192.0.2.1 playwright install
```

— `HTTPS_PROXY`/`HTTP_PROXY` are honored by the install step. A related caveat for `install-deps`
specifically (`docs/src/browsers.md:735`): "If you are installing dependencies and need to use a proxy
on Linux, make sure to run the command as a root user. Otherwise, Playwright will attempt to become a
root and will not pass environment variables like `HTTPS_PROXY` to the linux package manager."

### 2.2 Default cache directory, and `PLAYWRIGHT_BROWSERS_PATH`

`docs/src/browsers.md:953-960` (tag `v1.62.0`):

```
Playwright downloads Chromium, WebKit and Firefox browsers into the OS-specific cache folders:
- %USERPROFILE%\AppData\Local\ms-playwright on Windows
- ~/Library/Caches/ms-playwright on macOS
- ~/.cache/ms-playwright on Linux
```

Overridable per-install and per-run via the `PLAYWRIGHT_BROWSERS_PATH` environment variable
(`docs/src/browsers.md:970-1050`).

### 2.3 `chromium` vs `chromium-headless-shell`: since v1.49, and what `playwright install chromium` actually fetches

Introduced in **Playwright v1.49** — GitHub issue `microsoft/playwright#33566` ("Changes in Chromium
headless in Playwright v1.49") documents Chromium's own removal of its old headless implementation and
Playwright's response of shipping a separate `chromium-headless-shell` build that "closely follows"
the old headless behavior; the issue states the change "should be transparent to you, there's no action
needed. Playwright will automatically pick between headed and headless browser builds."

**The primary-source answer for what `playwright install chromium` installs**,
`packages/playwright-core/src/server/registry/index.ts:1466-1471` (`resolveBrowsers`, tag `v1.62.0`):

```ts
for (const alias of aliases) {
	if (alias === 'chromium' || chromiumAliases.includes(alias)) {
		if (options.shell !== 'only')
			handleArgument('chromium');
		if (options.shell !== 'no')
			handleArgument('chromium-headless-shell');
	}
	...
```

**Passing `chromium` as the install argument expands to both `chromium` and `chromium-headless-shell`**
(plus `ffmpeg`, unconditionally added alongside any browser, `:1462-1463`), unless narrowed with
`--only-shell` (chromium only) or `--no-shell` (headless-shell only) — confirmed directly in the CLI's
own resolver, not inferred from the docs prose.

**What `headless=True` launches by default**, `packages/playwright-core/src/server/chromium/chromium.ts:417-424`
(`getExecutableName`, tag `v1.62.0`):

```ts
override getExecutableName(options: types.LaunchOptions): string {
	if (options.channel && registry.isChromiumAlias(options.channel)) return 'chromium';
	if (options.channel === 'chromium-tip-of-tree')
		return options.headless ? 'chromium-tip-of-tree-headless-shell' : 'chromium-tip-of-tree';
	if (options.channel) return options.channel;
	return options.headless ? 'chromium-headless-shell' : 'chromium';
}
```

**With no `channel` option set, `headless=True` launches `chromium-headless-shell`, not the full
Chromium binary.** So `--only-shell` at install time is sufficient for a harness that always launches
headless with no channel override — the full `chromium` binary is never touched at runtime in that
case, matching the docs' own framing (`docs/src/browsers.md:341-346`): "If you are only running tests
in headless shell (i.e. the `channel` option is **not** specified) ... you can avoid downloading the
full Chromium browser by passing `--only-shell` during installation."

### 2.4 `install-deps chromium` — the Ubuntu 24.04 apt package list

`packages/playwright-core/src/server/registry/nativeDeps.ts:457,471-477` (tag `v1.62.0`, the
`'ubuntu24.04-x64'.chromium` array, fetched raw), in full:

```ts
chromium: [
  'libasound2t64', 'libatk-bridge2.0-0t64', 'libatk1.0-0t64', 'libatspi2.0-0t64',
  'libcairo2', 'libcups2t64', 'libdbus-1-3', 'libdrm2', 'libgbm1',
  'libglib2.0-0t64', 'libnspr4', 'libnss3', 'libpango-1.0-0', 'libx11-6',
  'libxcb1', 'libxcomposite1', 'libxdamage1', 'libxext6', 'libxfixes3',
  'libxkbcommon0', 'libxrandr2'
]
```

(Command syntax confirmed at `docs/src/browsers.md:111-113`: `playwright install-deps chromium`.) The
`t64`-suffixed names reflect Ubuntu 24.04's 64-bit-time_t package transition; this list is
Chromium-only — it excludes the separate `tools`/`firefox`/`webkit` arrays in the same file (fonts,
`xvfb`, etc., needed only for other browsers or headed operation).

---

## 3. Launch options, and TLS trust for a custom root CA

### 3.1 `chromium_sandbox` defaults `False`, and Playwright itself adds `--no-sandbox`

`docs/src/api/params.md` macro `browser-option-chromiumsandbox` (tag `v1.62.0`): "`chromiumSandbox`
... Enable Chromium sandboxing. Defaults to `false`." And the consequence, in the launch-arg builder
itself, `packages/playwright-core/src/server/chromium/chromium.ts:388-389`:

```ts
if (options.chromiumSandbox !== true)
	chromeArguments.push('--no-sandbox');
```

**Confirmed directly, not inferred: because `chromium_sandbox` defaults to `False`, Playwright injects
`--no-sandbox` itself unless the caller explicitly passes `chromium_sandbox=True`.** Running as root
(a common CI/container posture) works with zero extra flags from the caller.

### 3.2 `--host-resolver-rules` and `--no-proxy-server`: no conflict with Playwright's own args, unless a SOCKS `proxy` option is also used

Playwright's own hardcoded Chromium switches (`packages/playwright-core/src/server/chromium/chromiumSwitches.ts:52-90`,
tag `v1.62.0`) contain no `--host-resolver-rules`, `--proxy-server`, or `--no-proxy-server` at all — the
list is startup/telemetry/feature-flag hardening only. Playwright only ever sets
`--host-resolver-rules` itself in one narrow case, `chromium.ts:390-398`:

```ts
const proxy = options.proxyOverride || options.proxy;
if (proxy) {
	const proxyURL = new URL(proxy.server);
	const isSocks = proxyURL.protocol === 'socks5:';
	if (isSocks && !options.socksProxyPort) {
		chromeArguments.push(`--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE ${proxyURL.hostname}"`);
	}
	chromeArguments.push(`--proxy-server=${proxy.server}`);
	...
```

— only when the `proxy` launch option is set to a `socks5://` URL with no `socksProxyPort`. **A launch
that does not use the `proxy` option at all (relying instead on a caller-supplied
`--host-resolver-rules=MAP host 127.0.0.1` arg and/or `--no-proxy-server`) has no conflict**: caller
args are appended last (`chromeArguments.push(...args)`, `chromium.ts:409`), after any of Playwright's
own. Chromium's own docs confirm `--no-proxy-server`'s purpose is necessary, not redundant, on Linux:
`chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/linux/proxy_config.md` (fetched live):
"For other desktop environments, Chromium's proxy settings can be configured using command-line flags
or environment variables" — i.e. **a headless Chromium with no GNOME/KDE session (any CI/container box)
will pick up `http_proxy`/`https_proxy` environment variables by default** if neither `--no-proxy-server`
nor an explicit `proxy` launch option is given.

### 3.3 `ignore_https_errors` defaults `False`

`docs/src/api/params.md` macro `context-option-ignorehttpserrors` (tag `v1.62.0`): "`ignoreHTTPSErrors`
... Whether to ignore HTTPS errors when sending network requests. Defaults to `false`." — confirmed.

### 3.4 TLS trust on Linux: the NSS shared DB, with an important correction to the assumed default path

Chromium's own current documentation, `chromium.googlesource.com/chromium/src/+/main/docs/linux/cert_management.md`
(fetched live — this is the live, present-day content of that file, not a cached/older version):

```
On Linux, Chromium uses the NSS Shared DB. If the built-in manager does not work for
you then you can configure certificates with the NSS command line tools.

Note: Since M146, Chromium defaults to $HOME/.local/share/pki/nssdb for the NSS Shared DB.

If you still have an existing $HOME/.pki/nssdb database, Chromium will use that instead.
Adjust the paths in the commands below to match whichever location your browser is using.
```

and, further down: `certutil -d sql:$HOME/.local/share/pki/nssdb -A -t "C,," -n <nickname> -i
<certificate filename>` for adding a trusted root.

**This corrects the plan's assumption.** The Chromium build this Playwright release bundles is
`151.0.7922.34` (§1.2) — well past M146 — so on a **fresh** box with no pre-existing
`~/.pki/nssdb`, the certificate must go into **`~/.local/share/pki/nssdb`**, not `~/.pki/nssdb`; the
old path is only honored as a fallback if it already exists from some prior NSS-based operation. The
add command itself is otherwise exactly as assumed: `certutil -d sql:$HOME/.local/share/pki/nssdb -A
-t "C,," -n <nick> -i ca.pem` (Debian/Ubuntu tool package: `libnss3-tools`, per the same doc's "Get the
tools" section — confirmed).

Whether locally-added roots remain trusted at all under the Chrome Root Store era: Chromium's own FAQ
(`chromium.googlesource.com/chromium/src/+/main/net/data/ssl/chrome_root_store/faq.md`, fetched live)
states, cross-platform: "the Chrome Certificate Verifier considers local trust decisions for both
adding and removing trust" — i.e. the Chrome Root Store supplements rather than replaces
locally-installed roots. The `cert_management.md` doc's own currency (actively describing an M146
change, on a build far newer than M146) is itself direct evidence that the NSS-DB mechanism remains
the live, correct mechanism today for Linux. **No headless-shell-specific documentation was found**;
since both `chromium` and `chromium-headless-shell` are built from the same Chromium source tree and
network stack, the same cert-verification path is expected to apply to both, but this specific
equivalence was not independently confirmed against a headless-shell-specific doc (flagged below).

---

## 4. Request interception and observation

### 4.1 `context.route()` — what it sees, and what it never sees

Playwright's own network docs (`playwright.dev/python/docs/network`, fetched live): "Playwright
supports WebSockets inspection, mocking and modifying out of the box," alongside `context.route()`'s
own scope: navigations, fetch/XHR, and subresources on any page in the context.

- **Service workers**: `context.route()`/`page.route()` do **not** intercept requests a Service Worker
  itself makes (a long-standing, explicitly documented limitation —
  `github.com/microsoft/playwright/issues/1090`); the documented workaround is the `service_workers`
  context option set to `'block'`, which "will block all registration of Service Workers" (default
  `'allow'`) — moot for Open WebUI v0.11.3 specifically, since it registers no service worker at all
  (Part A §4.2).
- **WebSockets**: `route()` does **not** see WebSocket connections at all. Observation instead goes
  through `page.on("websocket")`, firing a `WebSocket` object (`docs/src/api/class-websocket.md`,
  tag `v1.62.0`): "The WebSocket class represents WebSocket connections within a page," exposing
  `web_socket.url` and the `"framesent"`/`"framereceived"` (payload as string or bytes) and `"close"`
  events.
- **Blocking/routing a WebSocket to a foreign host**: `browser_context.route_web_socket(url, handler)`
  — **added in v1.48** (`docs/src/api/class-browsercontext.md`, tag `v1.62.0`), one version before the
  headless-shell split (§2.3). Only WebSockets created *after* the call are routed; the docs recommend
  calling it before creating any pages.
- **`route.abort()` / `route.continue_()`**: standard route-handler verbs — abort terminates the
  request unset, continue lets it proceed unmodified; `request.url` and the `context.on("request")`/
  `page.on("request")` events fire for every request including ones a handler goes on to abort, giving
  a complete same-origin/foreign-origin log regardless of what the route handler decides.

### 4.2 `page.request`/`context.request` (`APIRequestContext`): not routed, but cookie-sharing with the browser context

`docs/src/api/class-apirequestcontext.md` (tag `v1.62.0`), Cookie management section: "The
`APIRequestContext` returned by `browser_context.request` and `page.request` uses the same cookie jar
as its `BrowserContext`" — confirmed directly; a request made through `page.request` after a UI sign-in
carries the same cookies the browser session holds (relevant if the harness ever wants to make a raw
API call alongside a UI-driven flow). **`context.route()` handlers do not see `APIRequestContext`
requests** — this is a widely corroborated, real limitation (e.g. `github.com/microsoft/playwright/issues/20501`,
architecturally because `APIRequestContext` issues requests from the Node driver process directly
rather than through the page's own network stack that `route()` hooks into), but no single-sentence
statement of it was found in the API reference itself — **flagged as community-confirmed rather than
doc-quoted** (see "Unverified" below).

---

## 5. Driving the page

- **Typing into the contenteditable composer**: `locator.fill()` explicitly supports it —
  `docs/src/api/class-locator.md` (tag `v1.62.0`): "If the target element is not an `<input>`,
  `<textarea>` or `[contenteditable]` element, this method throws an error," directing to
  `locator.press_sequentially()` only "if there is special keyboard handling on the page" (the general
  guidance, `playwright.dev/python/docs/input`, fetched live: "Most of the time, you should input text
  with `locator.fill()`. You only need to type characters if there is special keyboard handling"). Given
  Open WebUI's composer has real custom keydown handling for Tab/Enter/heading-suggestion (Part A §2.2),
  `press_sequentially()` is the safer choice if `fill()` proves to skip any of that logic in practice —
  not independently exercised against this specific ProseMirror editor in this research pass.
- **`locator.press("Enter")`**: a real, synthesized key event through the standard Playwright input
  pipeline — reaches the same native `keydown` the app's own handler is attached to (Part A §2.2), so it
  should trigger the same submit path a manual Enter keystroke would.
- **`locator.inner_text()`**: returns `element.innerText` (`docs/src/api/class-locator.md:1635-1639`,
  since v1.14); the docs themselves note a preference for `LocatorAssertions.to_have_text(...,
  use_inner_text=True)` when the read is for an assertion rather than a value to inspect.
  `context.cookies()` returns each cookie's `name`/`value`/`domain`/`path`/`expires`
  (Unix seconds)/`http_only`/`secure`/`same_site`/`partition_key` (`docs/src/api/class-browsercontext.md:617-630`).
- **`page.wait_for_function`**: default timeout **30000 ms** (Python), default polling **`raf`**
  (`requestAnimationFrame`) — `docs/src/api/params.md` macros `wait-for-function-timeout` and
  `js-python-wait-for-function-polling` (tag `v1.62.0`); the default is overridable via
  `context.set_default_timeout`/`page.set_default_timeout`.
- **`locator.wait_for(state=...)`**: `state` defaults `'visible'`; the four values —
  `'attached'` (present in DOM), `'detached'` (not present), `'visible'` (non-empty bounding box, no
  `visibility:hidden`), `'hidden'` (opposite of visible) — `docs/src/api/params.md` macro
  `wait-for-selector-state` (tag `v1.62.0`). Worth noting: **`Locator.wait_for_function` was added in
  this exact pinned release, v1.62** (`docs/src/api/class-locator.md:2902-2903`) — a locator-scoped,
  re-resolving custom-condition wait, new enough that it postdates most existing Playwright guidance.
- **`page.screenshot(path=, full_page=)`**: `full_page` — "takes a screenshot of the full scrollable
  page, instead of the currently visible viewport. Defaults to `false`" — and `path` are both part of
  the shared `screenshot-options-common-list` macro (`docs/src/api/params.md:1347-1398`, tag `v1.62.0`).
- **`sync_playwright()`**: used as a context manager — `from playwright.sync_api import sync_playwright`
  / `with sync_playwright() as playwright: ...` (`docs/src/library-python.md`, tag `v1.62.0`).
  **Threading**: the same doc's own "Threading" section (`:199-201`): "Playwright's API is not
  thread-safe. If you are using Playwright in a multi-threaded environment, you should create a
  playwright instance per thread." (linking `github.com/microsoft/playwright-python/issues/623` for
  detail).
- **Per-action and global timeouts**: an individual call's own `timeout=` kwarg overrides the default
  for that call only; the global default is set once via `context.set_default_timeout(ms)` (or
  `page.set_default_timeout`, which takes priority over the context-level one per
  `docs/src/api/class-browsercontext.md:1444-1447`: "`Page.setDefaultNavigationTimeout`,
  `Page.setDefaultTimeout` and `BrowserContext.setDefaultNavigationTimeout` take priority over
  `BrowserContext.setDefaultTimeout`.").

---

## Unverified / out of scope

- **Whether GIDEON's render sets `ENABLE_LOGIN_FORM=false` alongside `ENABLE_LDAP=true`** (Part A §1.1,
  §1.7) — not cross-checked against GIDEON's own compose/render config in this pass; determines whether
  the "Continue with Email" toggle is visible beside the LDAP form on the box.
- **Whether GIDEON's render sets `enable_websocket`/`ENABLE_WEBSOCKET_SUPPORT`** (Part A §4.1) — not
  checked against GIDEON's render or the backend's own default for that flag; assumed default (`true`,
  websocket-only transport) per the frontend's own `?? true` fallback, not confirmed against the
  backend's actual default value in `backend/open_webui/config.py`.
- **The exact helper variables `hasResponseContent`/`hasVisibleStatus`** gating the streaming cursor
  (Part A §3.1, `ResponseMessage.svelte:884`) were read only at their use site, not traced back to their
  own definitions — the pulsing-cursor/buttons-row signal is confirmed sufficient regardless, since both
  are driven by the same `message.done` boolean at the outer gates quoted.
- **Whether `locator.fill()` cleanly drives Open WebUI's ProseMirror composer in practice**, given its
  custom Tab/Enter/heading-suggestion keydown handling (Part A §2.2, Part B §5) — the docs establish
  `fill()` is contenteditable-safe in general; this specific editor's behavior under `fill()` was not
  exercised live in this research pass, only read from source.
- **The `APIRequestContext`-is-not-routed claim** (Part B §4.2) is corroborated by a GitHub issue and
  general architectural reasoning (a separate request path from the Node driver process), not by an
  explicit single-sentence statement in Playwright's own API reference — flagged as the one Part B
  claim resting on secondary/community confirmation rather than a direct doc quote.
- **Whether Chromium's headless-shell binary validates certificates through the identical NSS-DB code
  path as the full `chromium` binary** (Part B §3.4) — inferred from both being built from the same
  Chromium source tree and network stack; no headless-shell-specific certificate-management doc was
  found to confirm this directly.
- **Chrome Root Store's Linux-specific FAQ wording** (Part B §3.4) — the FAQ's "local trust decisions"
  statement is general/cross-platform; its explicit worked examples cover Windows and macOS platform
  stores, not Linux's NSS DB by name. The Linux-specific confirmation instead rests on
  `cert_management.md`'s own currency (actively documenting a Chromium-151-era change) as evidence the
  NSS-DB mechanism it describes is still live.
- **`playwright install`'s underlying HTTP client's proxy-detection mechanism** (e.g. whether it reads
  `HTTPS_PROXY` via Node's standard env-var conventions or a Playwright-specific proxy-agent shim) was
  not traced into the fetch implementation itself — the docs' own worked example
  (`HTTPS_PROXY=... playwright install`) was taken as sufficient confirmation of the behavior, not the
  underlying code path.

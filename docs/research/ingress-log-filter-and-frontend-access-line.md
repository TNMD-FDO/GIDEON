---
verified_against:
  - pin: images.open-webui
    version: v0.11.3
  - pin: images.caddy
    version: "2.11"
---

# Ingress log filter and the frontend's access line — verified facts

Scope: GIDEON slice-1 ticket 69
(`.scratch/slice-1/issues/69-prompt-in-the-chat-page-url.md`), whose "Ruled at
triage, 2026-09-09" paragraph is the set of claims this note checks against
primary source. Part lettering follows the research request, not the
ticket's own lettered rulings.

**Sources and method.** Open WebUI: tag `v0.11.3`, commit
`2a960a59fe1dbbd35282f0556b3666d81102e781` (the commit the cited prior note,
`docs/research/search-path-logs-audit-telemetry.md`, already verified
against); backend paths relative to `backend/open_webui/` unless said
otherwise. Caddy: the lock names the tracking tag `2.11`; the newest patch
under that minor series at research time is `v2.11.4` — the same tag the
prior note's Caddy citations were read at (and, per that note, what the
box's own `caddy version` prints under the `2.11` tracking pin) — so this
note's Caddy citations are read at `v2.11.4`. uvicorn: `0.51.0`, the
version OWUI's own `backend/requirements.txt` pins at this tag (confirmed
below, Part B5); read at uvicorn's `0.51.0` tag.

`modules/logging/filterencoder.go`, `modules/logging/filters.go`,
`modules/caddyhttp/marshalers.go` (Caddy), and `env.py`, `utils/audit.py`,
`utils/logger.py`, `backend/start.sh`, `Dockerfile`,
`backend/requirements.txt` (Open WebUI), and `config.py`,
`protocols/http/h11_impl.py`, `protocols/http/httptools_impl.py` (uvicorn)
were fetched byte-for-byte from `raw.githubusercontent.com` at the exact
tag via a direct download and line-numbered from that download — their
`file:line` citations below are exact. One further Caddy file,
`internal/logmarshalers.go`, could only be reached through a fetch tool
that returns page content via a text-processing/summarization step (this
note's environment lost direct network access — see the note at the end of
Part A); it was fetched twice, the second time with an explicit
verbatim-reproduction prompt, and the two fetches were internally
consistent and included a distinctive, unlikely-to-be-paraphrased comment
(a joke referencing Caddy issue #5669) that corroborates it as genuine
source text — but it is cited without a claimed line number and flagged
here as not independently byte-verified. Caddy's own docs pages
(`caddyserver.com/docs/caddyfile/directives/log`,
`.../docs/json/logging/logs/encoder/filter/`) are cited by URL, no line
numbers claimed; the JSON reference sub-page for the `filter` encoder and
the `query` filter render through client-side JavaScript and returned no
extractable prose through the fetch tool available in this session — flagged
as not directly quotable; every claim it would have supported is instead
verified against the Go source directly, which is authoritative regardless.

---

## Part A — Caddy v2.11.4

### A1. The `filter` encoder's Caddyfile grammar

**`fields { }` is not required — a `<field> <filter> { … }` line may sit
directly inside `filter { }`.** `UnmarshalCaddyfile`'s own doc comment
states both forms as valid syntax:

```go
// UnmarshalCaddyfile sets up the module from Caddyfile tokens. Syntax:
//
//	filter {
//	    wrap <another encoder>
//	    fields {
//	        <field> <filter> {
//	            <filter options>
//	        }
//	    }
//	    <field> <filter> {
//	        <filter options>
//	    }
//	}
```
(`modules/logging/filterencoder.go:139-151`)

The parser confirms the comment is accurate, not aspirational. Inside the
block's token loop, `"fields"` is one recognized case (delegating to the
same `parseField` helper for each subdirective nested inside it), and
every *other* token hits the `default` case, which calls `parseField`
directly on it:

```go
case "fields":
    for nesting := d.Nesting(); d.NextBlock(nesting); {
        err := parseField()
        ...
    }

default:
    // if unknown, assume it's a field so that
    // the config can be flat
    err := parseField()
    ...
```
(`modules/logging/filterencoder.go:216-231`)

So `filter { request>uri query { replace q redacted } }` (no `fields`
wrapper) and `filter { fields { request>uri query { replace q redacted } } }`
(with one) are both accepted and produce the same `FieldsRaw["request>uri"]`
entry — `fields {}` is a grouping convenience, never a requirement. One
real constraint the grammar does add: a field name may carry only one
non-regexp filter — a second `<field> <filter>` line naming a field that
already has one is a hard Caddyfile parse error, `"field %s already has a
filter; multiple non-regexp filters per field are not supported"`
(`modules/logging/filterencoder.go:189-192`), which does not affect the
ticket's plan since its two filters target two different fields
(`request>uri`, `request>headers>Referer`).

**The `>` nested-field separator** is documented on the same
`UnmarshalCaddyfile`-adjacent struct comment: "Nested fields can be
referenced by representing a layer of nesting with `>`. In other words,
for an object like `{"a":{"b":0}}`, the inner field can be referenced as
`a>b`." (`modules/logging/filterencoder.go:50-53`). This isn't just a
parsing convention; the encoder builds the same path at *encode* time by
accumulating a `keyPrefix` as it walks into nested objects —
`AddObject`'s `fe.keyPrefix += key + ">"` (`modules/logging/filterencoder.go:270`)
— and looks each scalar field up as `fe.Fields[fe.keyPrefix+key]`
(e.g. `AddString`, `modules/logging/filterencoder.go:368-373`, via a shared
`filtered` helper). So a Caddy request log entry's `request` object
(itself nested one level, `headers` nested a second level inside that)
resolves `request>uri` and `request>headers>Referer` exactly as the
ticket's plan assumes.

**`wrap`** is an optional subdirective naming another logging-encoder
module (`caddy.logging.encoders.<name>`); `wrap json` loads the `json`
encoder module explicitly (`modules/logging/filterencoder.go:200-214`).
It is not required: if omitted, `Provision` defaults `fe.wrapped` to
`&JSONEncoder{}` unless the output is a terminal, in which case it later
switches to the console encoder (`modules/logging/filterencoder.go:83-91,
120-137`). So `wrap json` in the ticket's plan is explicit but redundant
with Caddy's own non-terminal default — harmless, and clearer to a
reader than relying on the default.

**Docs cross-check.** `caddyserver.com/docs/caddyfile/directives/log`'s
filter-encoder section quotes the identical grammar (a `fields {}` block,
*and* a bare `<field> <filter> …` line, *and* `wrap <encode_module> …`,
all inside `format filter { }`) and states the same `>` nesting rule and
the same fundamental-fields exclusion (`ts`, `level`, `logger`, `msg`
cannot be filtered) — consistent with the source read above. The
JSON-schema reference sub-page (`.../docs/json/logging/logs/encoder/filter/`)
render dynamically and returned no usable text in this session; not an
independent confirmation, but the source above already settles every
claim it would have supported.

**Against the exact shape in the research request** —
`filter { wrap json; fields { request>uri query { replace q redacted } } }`
— the field/filter/action grammar is exactly as parsed above (this shape
is accepted as written for the module syntax itself), with one caveat that
has nothing to do with module grammar: Caddyfile statements are
newline-separated, not semicolon-separated — there is no semicolon token
anywhere in `filterencoder.go`'s or `filters.go`'s Caddyfile parsing, and
Caddy's tokenizer does not treat `;` as a statement separator. The
semicolons in the request's one-line shorthand are not valid Caddyfile
syntax; a real Caddyfile needs `wrap json` and `fields { … }` (or the
field line directly) each on its own line:
```
format filter {
    wrap json
    fields {
        request>uri query {
            replace q redacted
        }
    }
}
```
This is a presentational correction only, not a grammar difference in the
module itself.

### A2. The `query` filter (`modules/logging/filters.go`)

**Actions and their Caddyfile arguments** — `UnmarshalCaddyfile`
(`modules/logging/filters.go:350-391`):
```go
case "replace":
    ...
    qfa.Type = replaceAction
    qfa.Parameter = d.Val()
    if !d.NextArg() { return d.ArgErr() }
    qfa.Value = d.Val()

case "hash":
    ...
    qfa.Type = hashAction
    qfa.Parameter = d.Val()

case "delete":
    ...
    qfa.Type = deleteAction
    qfa.Parameter = d.Val()
```
`replace <param> <value>` takes two arguments (parameter, replacement);
`delete <param>` and `hash <param>` each take exactly one (the parameter
name) — `hash`'s Caddyfile form has **no value argument at all**; nothing
in the grammar even accepts one for `hash`. `qfa.Value` is left at its Go
zero value, `""`, for both `hash` and `delete`.

**String field vs. string-array field** — `Filter`
(`modules/logging/filters.go:394-406`):
```go
func (m QueryFilter) Filter(in zapcore.Field) zapcore.Field {
    if array, ok := in.Interface.(internal.LoggableStringArray); ok {
        newArray := make(internal.LoggableStringArray, len(array))
        for i, s := range array {
            newArray[i] = m.processQueryString(s)
        }
        in.Interface = newArray
    } else {
        in.String = m.processQueryString(in.String)
    }
    return in
}
```
A plain string field (`request>uri`, a single URI string) has
`processQueryString` applied once; a string-array field (a header value,
`internal.LoggableStringArray`, since HTTP headers are logged as arrays —
Part A3) has it applied to *every element of the array independently* —
so a `Referer` header repeated across several request lines, or a header
that legitimately carries multiple values, is filtered element-by-element,
not just its first value.

**What `processQueryString` does** (`modules/logging/filters.go:408-434`):
```go
func (m QueryFilter) processQueryString(s string) string {
    u, err := url.Parse(s)
    if err != nil {
        return s
    }
    q := u.Query()
    for _, a := range m.Actions {
        switch a.Type {
        case replaceAction:
            for i := range q[a.Parameter] {
                q[a.Parameter][i] = a.Value
            }
        case hashAction:
            for i := range q[a.Parameter] {
                q[a.Parameter][i] = hash(a.Value)
            }
        case deleteAction:
            q.Del(a.Parameter)
        }
    }
    u.RawQuery = q.Encode()
    return u.String()
}
```
- It parses the field's string value as a URL (`url.Parse`) — for
  `request>uri` this is the request-target (`/path?query`); for a
  `Referer` header value it is a full absolute URL, and `url.Parse`
  handles both shapes, extracting `u.Query()` either way. If parsing
  fails, the original string is returned unchanged (fails open, silently).
- **A parameter absent from the query string is silently skipped, no
  error**: `q[a.Parameter]` on a `url.Values` (a `map[string][]string`)
  for a missing key is `nil`; ranging over a nil slice (`replace`,
  `hash`) iterates zero times, and `q.Del` on an absent key is a no-op.
  Nothing is added, nothing errors.
- **Every value under a repeated parameter is replaced/hashed**, not just
  the first (`for i := range q[a.Parameter]`).
- **Re-encoding**: `u.RawQuery = q.Encode()` — Go's `url.Values.Encode()`
  percent-encodes every key and value and **sorts the result by key**.
  This means **every logged query string that reaches a `query` filter is
  re-encoded**, not only the parameters an action named: any query string
  with more than one parameter has its parameter order rewritten to
  alphabetical, and every value (touched or not) passes through
  `Encode()`'s percent-encoding — so a value that happened to already be
  raw/unescaped in the original request line will appear percent-encoded
  in the logged line even if no filter action targeted it, and a URI or
  Referer field with no query string at all is unaffected (empty
  `u.RawQuery`, `Encode()` of an empty map is `""`, `u.String()`
  reproduces the original path).
- **A `replace`d value is written back through `Encode()`**, so yes, it is
  URL-encoded in the final logged line (e.g. `replace q redacted` writes
  the literal string `redacted`, which needs no escaping, but a value
  containing reserved characters would be percent-encoded like any other
  query value).

**The `hash` action confirmed to hash a constant, not the parameter's
content.** `hash(a.Value)` is called with `a.Value` — but for a `hash`
action, `a.Value` is never assigned by `UnmarshalCaddyfile` (only `hash
<param>` is parsed; no branch sets `qfa.Value` for the `hash` case, shown
above). So at this tag, `hash <param>` inside a `query` filter always
hashes the empty string, a compile-time constant for every invocation,
**not** the query parameter's actual value:
```go
// hash returns the first 4 bytes of the SHA-256 hash of the given data as hexadecimal
func hash(s string) string {
    return fmt.Sprintf("%.4x", sha256.Sum256([]byte(s)))
}
```
(`modules/logging/filters.go:76-79`) — with `s = ""`, this always
produces the same 4-byte hex prefix of `sha256("")` regardless of what the
redacted parameter actually held. **The ticket's claim is confirmed
exactly**: the `query` filter's `hash` action hashes its own empty
`Value` field, a constant, and using it would log the same fixed token
for every request regardless of the real parameter value — worthless as
redaction and worthless as a correlation key. (The sibling whole-field
`HashFilter`, a different module entirely, does hash the field's actual
content — the doc comment at `filters.go:81-84` and the general docs
page's one-line description of `hash` both describe that module, not the
`query` filter's per-parameter `hash` sub-action, which is a distinct,
narrower code path with this specific defect.)

### A3. The request marshaler (`modules/caddyhttp/marshalers.go`)

```go
func (r LoggableHTTPRequest) MarshalLogObject(enc zapcore.ObjectEncoder) error {
    ...
    enc.AddString("proto", r.Proto)
    enc.AddString("method", r.Method)
    enc.AddString("host", r.Host)
    enc.AddString("uri", r.RequestURI)
    enc.AddObject("headers", internal.LoggableHTTPHeader{
        Header:               r.Header,
        ShouldLogCredentials: r.ShouldLogCredentials,
    })
    ...
}
```
(`modules/caddyhttp/marshalers.go:35-61`) — `uri` is `r.RequestURI`, Go's
`http.Request.RequestURI`, the request-target exactly as the client sent
it on the request line, including its query string (e.g.
`/?q=<prompt>&web-search=true`); `headers` is the whole header set,
object-keyed by canonical header name (Go canonicalizes header names,
e.g. `Referer`), each value logged as a string array
(`internal.LoggableStringArray`), matching Part A2's array branch.

**Credential redaction** is implemented in a second file this session
could not byte-verify directly (see the method note above), fetched
through a summarizing tool twice with consistent, plausible results
including a distinctive joke comment (a strong but not certain signal of
genuine verbatim text):
```go
// LoggableHTTPHeader makes an HTTP header loggable with zap.Object().
// Headers with potentially sensitive information (Cookie, Set-Cookie,
// Authorization, and Proxy-Authorization) are logged with empty values.
type LoggableHTTPHeader struct {
    http.Header
    ShouldLogCredentials bool
}

func (h LoggableHTTPHeader) MarshalLogObject(enc zapcore.ObjectEncoder) error {
    ...
    for key, val := range h.Header {
        if !h.ShouldLogCredentials {
            switch strings.ToLower(key) {
            case "cookie", "set-cookie", "authorization", "proxy-authorization":
                val = []string{"REDACTED"}
            }
        }
        enc.AddArray(key, LoggableStringArray(val))
    }
    return nil
}
```
(`internal/logmarshalers.go`, no line number claimed — see method note).
This matches Caddy's own docs page for the `log` directive, which states
the identical four-header list verbatim and says the behavior is disabled
by the **global server option** `log_credentials` (not a `log`-block
subdirective — it lives in the Caddyfile's top-level `{ … }` global
options block, per
`caddyserver.com/docs/caddyfile/directives/log`, "This behaviour can be
disabled with the `log_credentials` global server option"). `Referer` is
not in the four-header redaction list, so it is logged in full — as the
ticket's plan assumes when it targets `request>headers>Referer` with its
own `query` filter.

### A4. Cost/ordering of two `query` filters on two different fields

Each field's filter is looked up independently, once, as that field is
encoded: a single map read, `fe.Fields[fe.keyPrefix+key]`
(`modules/logging/filterencoder.go:258, 368-373` and the shared `filtered`
helper), during the normal walk that produces the log entry — `AddString`
for `uri`, `AddArray` for each header's string-array value. `Fields` is a
`map[string]LogFieldFilter` (`modules/logging/filterencoder.go:62`), one
filter per full field path; there is no cross-field state, no shared
buffer, no ordering dependency between a filter on `request>uri` and one
on `request>headers>Referer` — they run independently, each doing its own
`url.Parse`/`Encode()` round-trip only for the one field it matches, and
only when that field is actually present in the entry being logged (a
request with no `Referer` header never triggers that filter's
`processQueryString` at all, since `AddArray` — and `AddObject`'s per-key
walk — only calls into the filter machinery for keys that are actually
written). Nothing in the source suggests declaration order inside the
Caddyfile matters, since each is keyed by its own distinct field path.

---

## Part B — Open WebUI v0.11.3

### B1. `env.py` — the two audit-mechanism environment variables

```python
# env.py:1207-1210
# Comma separated list of logger names to use for audit logging
# Default is "uvicorn.access" which is the access log for Uvicorn
# You can add more logger names to this list if you want to capture more logs
AUDIT_UVICORN_LOGGER_NAMES = os.getenv('AUDIT_UVICORN_LOGGER_NAMES', 'uvicorn.access').split(',')
```
Exact name `AUDIT_UVICORN_LOGGER_NAMES`; default `'uvicorn.access'`; parsed
by a plain comma split (`.split(',')`) with no whitespace trimming — a
value like `uvicorn.error, uvicorn.access` would leave a leading space on
the second name, which would then fail to match the real logger name
`uvicorn.access` (this note did not find any strip/trim step in `env.py`
around this line; a render that ever needs two names should not rely on
a space after the comma).

```python
# env.py:1234-1235
# When enabled, GET requests are also audited (disabled by default to avoid log noise)
ENABLE_AUDIT_GET_REQUESTS = os.getenv('ENABLE_AUDIT_GET_REQUESTS', 'False').lower() == 'true'
```
Exact name `ENABLE_AUDIT_GET_REQUESTS`; default `'False'` (i.e. off);
parsed as the standard OWUI boolean idiom, `.lower() == 'true'`.

### B2. `utils/logger.py` — emptying uvicorn's loggers and attaching the intercept handler

`start_logger()` (`utils/logger.py:168-223`, the function `main.py` calls
from inside FastAPI's `lifespan`, per the cited prior note's already-
verified `main.py:122,343` citations, not re-derived here):
```python
# utils/logger.py:211
logging.basicConfig(handlers=[InterceptHandler()], level=GLOBAL_LOG_LEVEL, force=True)

# utils/logger.py:213-216
for uvicorn_logger_name in ['uvicorn', 'uvicorn.error']:
    uvicorn_logger = logging.getLogger(uvicorn_logger_name)
    uvicorn_logger.setLevel(GLOBAL_LOG_LEVEL)
    uvicorn_logger.handlers = []

# utils/logger.py:218-221
for uvicorn_logger_name in AUDIT_UVICORN_LOGGER_NAMES:
    uvicorn_logger = logging.getLogger(uvicorn_logger_name)
    uvicorn_logger.setLevel(GLOBAL_LOG_LEVEL)
    uvicorn_logger.handlers = [InterceptHandler()]
```
Two distinct loops, and they are not symmetric. The first
(`'uvicorn'`, `'uvicorn.error'`, hard-coded, unconditional) always strips
whatever handlers those two loggers carried and sets their level to
`GLOBAL_LOG_LEVEL` — it never re-attaches anything to them. The second
loop, over `AUDIT_UVICORN_LOGGER_NAMES`, **replaces** (not appends —
plain assignment, `uvicorn_logger.handlers = [InterceptHandler()]`)
whatever handlers each named logger had with a single `InterceptHandler`
at `GLOBAL_LOG_LEVEL`. With the default `AUDIT_UVICORN_LOGGER_NAMES =
['uvicorn.access']`, this is the *only* place `uvicorn.access` is
touched by OWUI's own startup — its handlers (whatever uvicorn's own
`configure_logging()` gave it, Part C) are discarded and replaced with
`InterceptHandler`, which is why the access line reaches the frontend's
own loguru pipeline and its own format (`stdout_format` or `_json_sink`,
`utils/logger.py:28-90`) rather than uvicorn's raw
`'%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'`
console format. **This is also the mechanism the ticket's ruling turns on
its head**: if `AUDIT_UVICORN_LOGGER_NAMES` is changed to `uvicorn.error`,
`uvicorn.access` is no longer named in either loop and is left completely
untouched by `start_logger()` — so whatever uvicorn's own
`configure_logging()` did to it (including `--no-access-log`'s
handler-clearing, Part C1) survives app startup instead of being
overwritten a moment later.

**The access line's shape**, as it reaches stdout once intercepted: the
uvicorn-side message text is the fixed format string quoted in Part C2,
e.g. `127.0.0.1:53412 - "GET /?q=<prompt>&web-search=true HTTP/1.1" 200`;
`InterceptHandler.emit` (`utils/logger.py:99-123`) forwards this as
loguru's `message` via `record.getMessage()` (`utils/logger.py:115-116`),
which then goes through the same sink as every other line — `_json_sink`
(JSON, `utils/logger.py:50-90`, `LOG_FORMAT=json`) puts it in the `"msg"`
key of a one-line JSON object with `ts`/`level`/`caller`, or
`stdout_format` (`utils/logger.py:28-47`) puts it inline in the plain
console line — so an access line is identifiable by shape (the uvicorn
request-line text as the `msg`/message body, `caller` naming
`uvicorn.access` if the JSON sink is used — `record['name']` is the
`logging.getLogger("uvicorn.access")` name, carried through
`InterceptHandler` as `logger.opt(...)`'s bound record) rather than by a
dedicated field.

### B3. `utils/audit.py` — audited methods, always-logged routes, entry shape

```python
# utils/audit.py:121
DEFAULT_AUDITED_METHODS = {'PUT', 'PATCH', 'DELETE', 'POST'}
```
```python
# utils/audit.py:142-144
self.audited_methods = set(self.DEFAULT_AUDITED_METHODS)
if audit_get_requests:
    self.audited_methods.add('GET')
```
```python
# utils/audit.py:229-233
ALWAYS_LOG_ENDPOINTS = (
    '/api/v1/auths/signin',
    '/api/v1/auths/signout',
    '/api/v1/auths/signup',
)
```
```python
# utils/audit.py:235-260  (_should_skip_auditing)
if AUDIT_LOG_LEVEL == 'NONE':
    return True
if request.method not in self.audited_methods:
    return True
path = request.url.path.lower()
for endpoint in self.ALWAYS_LOG_ENDPOINTS:
    if path.startswith(endpoint):
        return False  # Do NOT skip logging for auth endpoints
if not request.headers.get('authorization') and not request.cookies.get('token'):
    return True
...
```
Confirms: POST/PUT/PATCH/DELETE always in `audited_methods`; GET joins
only when the middleware is constructed with `audit_get_requests=True`
(wired from `ENABLE_AUDIT_GET_REQUESTS`, not independently re-traced to
its call site in this note — `app.add_middleware`/`main.py` wiring was
not re-fetched, since B1 already establishes the flag's name/default and
the middleware's own constructor signature at
`utils/audit.py:123-132` takes `audit_get_requests: bool = False` as a
plain parameter); the three auth routes are checked *before* the
"authenticated only" skip, so they are audited whether or not the caller
is signed in (matching "always" in the ticket's ruling — a signed-out
`/auths/signin` attempt is audited).

**A `METADATA`-level entry's fields** — the dataclass has no `Referer`
field at all:
```python
# utils/audit.py:36-50
@dataclass(frozen=True)
class AuditLogEntry:
    id: str
    user: Optional[dict[str, Any]]
    audit_level: str
    verb: str
    request_uri: str
    user_agent: Optional[str] = None
    source_ip: Optional[str] = None
    request_object: Any = None
    response_object: Any = None
    response_status_code: Optional[int] = None
```
```python
# utils/audit.py:292-303
entry = AuditLogEntry(
    id=str(uuid.uuid4()),
    user=user,
    audit_level=self.audit_level.value,
    verb=request.method,
    request_uri=str(request.url),
    response_status_code=context.metadata.get('response_status_code', None),
    source_ip=request.client.host if request.client else None,
    user_agent=request.headers.get('user-agent'),
    request_object=request_body,
    response_object=response_body,
)
```
`request_uri=str(request.url)` is the full URL including its query
string (Starlette's `Request.url.__str__`); there is no header capture of
any kind on this dataclass — the ticket's claim that a `METADATA` entry
carries `request_uri` and no `Referer` is confirmed exactly, and further:
no audit entry of *any* level carries `Referer` — the field simply does
not exist on `AuditLogEntry`.

`AUDIT_EXCLUDED_PATHS` handling — normalized once at construction
(`utils/audit.py:136-140`, stripped and leading-`/`-stripped) into a
single compiled regex, `_excluded_pattern`
(`utils/audit.py:152-154`), matched only in "blacklist mode" (i.e. only
when `included_paths` is empty) inside `_should_skip_auditing`
(`utils/audit.py:256-258`) — this runs *after* the always-log-endpoints
check and the authentication check, so an excluded path is a fourth,
lowest-priority gate.

### B4. `backend/start.sh` and the top-level `Dockerfile`

```bash
# backend/start.sh:99-113
PYTHON_CMD=$(command -v python3 || command -v python)
UVICORN_WORKERS="${UVICORN_WORKERS:-1}"

if [[ "$#" -gt 0 ]]; then
  ARGS=("$@")
else
  ARGS=(--workers "$UVICORN_WORKERS" --ws-per-message-deflate "${UVICORN_WS_PER_MESSAGE_DEFLATE:-true}")
fi

exec env WEBUI_SECRET_KEY="${WEBUI_SECRET_KEY:-}" \
  "$PYTHON_CMD" -m uvicorn open_webui.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
    "${ARGS[@]}"
```
Defaults, only when the container is given **no** positional arguments:
`--workers 1` (`UVICORN_WORKERS` default `1`) and
`--ws-per-message-deflate true` (`UVICORN_WS_PER_MESSAGE_DEFLATE` default
`true`) — confirmed exactly. **Passing any container argument replaces
these defaults whole, it does not append to them**: the `if [[ "$#" -gt 0
]]` branch sets `ARGS=("$@")` unconditionally to the given arguments,
with no merging of the default `--workers`/`--ws-per-message-deflate`
pair — so a Compose `command:` that wants `--no-access-log` alongside
those two must restate all three explicitly (exactly the ticket's plan:
`--workers 1 --ws-per-message-deflate true --no-access-log`), or the
worker count and deflate setting silently revert to uvicorn's own library
defaults (`workers=None`→1 process without the `--workers` flag at all,
in-process, and `ws_per_message_deflate=True` per `config.py:205`
regardless — so dropping the flag here happens to be harmless for that
one setting specifically, but not for worker count under
`main:app` being invoked without `--workers`, which uvicorn runs
single-process either way at the default; the practical risk is a Compose
author assuming the image's shell defaults still apply once any argument
is passed, which this script disproves).

Four fixed CLI flags are always present regardless of `ARGS`: `--host`,
`--port`, `--forwarded-allow-ips` — these three are *not* subject to the
replace-vs-append question, since `start.sh` always passes them itself
before appending `"${ARGS[@]}"`.

`Dockerfile`:
```
# Dockerfile:33
WORKDIR /app
...
# Dockerfile:109
WORKDIR /app/backend
...
# Dockerfile:206
HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1
...
# Dockerfile:225
CMD [ "bash", "start.sh"]
```
No `ENTRYPOINT` instruction exists anywhere in the file (a full-file
search for the literal token `ENTRYPOINT` found zero matches) — so `CMD
["bash","start.sh"]` is the image's *entire* runnable command, and a
Compose `command:` fully **replaces** it (Docker's documented semantics
for `CMD` with no `ENTRYPOINT`: the container's runtime command *is*
`CMD`, wholesale-replaceable by `command:`). This confirms the ticket's
premise: the plan's `command:` list, `bash start.sh --workers 1
--ws-per-message-deflate true --no-access-log`, stands in for `["bash",
"start.sh"]` entirely, and `start.sh` in turn sees those four tokens as
its own `"$@"`, taking the `ARGS=("$@")` branch above.

### B5. uvicorn pin

```
# backend/requirements.txt:2
uvicorn[standard]==0.51.0
```
Confirmed: `0.51.0` exactly, with the `[standard]` extra (pulls in
`httptools`/`uvloop`/etc., relevant only in that it means the
`httptools_impl.py` protocol — not just `h11_impl.py` — is a live code
path; both were checked in Part C since either could be selected at
runtime).

### B6. The image's own healthcheck and whether it produces an access line

```
# Dockerfile:206
HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1
```
This is a plain `GET /health` over HTTP to the same uvicorn process,
issued by Docker's own healthcheck runner inside the container on its own
interval (Docker's default healthcheck interval, absent an `--interval`
here, is 30s — from Docker's own `HEALTHCHECK` documentation; not
re-verified against Docker's source in this note, cited from Docker's
public reference). It reaches uvicorn exactly like any other request —
nothing in `start.sh`, `Dockerfile`, or the uvicorn/OWUI logging code
this note read treats `/health` as a special, unlogged route. So under
today's rendered posture (no `--no-access-log`, `AUDIT_UVICORN_LOGGER_NAMES`
unset/default `uvicorn.access`), each healthcheck GET produces one access
line every interval, indefinitely, for the life of the container —
consistent with the ticket's own on-box count (62,837 of the frontend's
67,807 retained lines are access lines) and confirms Part B6's premise
that access lines are the bulk of the frontend's stdout **by construction
of the shipped image**, not an artifact of user traffic alone.

---

## Part C — uvicorn 0.51.0

### C1. `Config.access_log` and `configure_logging`

```python
# config.py:210
access_log: bool = True,
```
```python
# config.py:262
self.access_log = access_log
```
```python
# config.py:296
self.configure_logging()
```
(inside `Config.__init__`, i.e. `configure_logging()` runs synchronously
as soon as a `Config` object is constructed — before the ASGI app is
imported or a `Server`/`lifespan` exists.)
```python
# config.py:420-422
if self.access_log is False:
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
```
Exactly as the ticket states: `--no-access-log` (which sets
`Config.access_log = False`) empties `uvicorn.access`'s handler list and
turns off its propagation — both inside `configure_logging()`, which
`Config.__init__` runs immediately, well before FastAPI's `lifespan`
(and hence before OWUI's `start_logger()`, Part B2) executes.

### C2. Per-connection access-log gate: `hasHandlers()`, evaluated at connection open

Identical in both HTTP protocol implementations:
```python
# protocols/http/h11_impl.py:55-57
self.logger = logging.getLogger("uvicorn.error")
self.access_logger = logging.getLogger("uvicorn.access")
self.access_log = self.access_logger.hasHandlers()
```
```python
# protocols/http/httptools_impl.py:59-61
self.logger = logging.getLogger("uvicorn.error")
self.access_logger = logging.getLogger("uvicorn.access")
self.access_log = self.access_logger.hasHandlers()
```
These lines sit in each protocol class's `__init__`, which runs once per
accepted connection (a new protocol instance per connection, asyncio's
standard protocol-factory pattern) — **confirmed**: `self.access_log` is
a per-connection snapshot of `logging.getLogger("uvicorn.access").hasHandlers()`
taken when the connection opens, not a live read of `Config.access_log`
at request time. Both protocols gate the actual log call on this cached
flag, not on the config value directly:
```python
# protocols/http/h11_impl.py:481-489 (httptools_impl.py:484-492 is the same)
if self.access_log:
    self.access_logger.info(
        '%s - "%s %s HTTP/%s" %d',
        get_client_addr(self.scope),
        self.scope["method"],
        get_path_with_query_string(self.scope),
        self.scope["http_version"],
        status_code,
    )
```
(the request line's path includes its query string,
`get_path_with_query_string`, by name — consistent with what an access
line carries).

**The ticket's reasoning is confirmed, and this note can now state the
full mechanism precisely, end to end**: `Config.__init__` (Part C1) runs
first and, under `--no-access-log`, leaves `uvicorn.access` with zero
handlers and `propagate=False`. If nothing later re-attaches a handler to
`uvicorn.access`, then even the *first* connection's `hasHandlers()` call
(which itself also walks up the logger hierarchy while `propagate=True`,
per Python's stdlib `Logger.hasHandlers()` — moot here since
`propagate=False` was just set, so the walk stops at `uvicorn.access`
itself) returns `False`, and no connection ever logs an access line.
Open WebUI's own `start_logger()` (Part B2) runs later, inside the app's
`lifespan` startup — after `Config.__init__`, but before any connection
is accepted — and, under OWUI's *default* `AUDIT_UVICORN_LOGGER_NAMES`
(`uvicorn.access`), unconditionally re-attaches an `InterceptHandler` to
`uvicorn.access`, which is why `hasHandlers()` is `True` for every
connection today and `--no-access-log` alone does nothing under the
current rendered environment. **This is exactly why
`AUDIT_UVICORN_LOGGER_NAMES` must point elsewhere (`uvicorn.error`) for
`--no-access-log` to hold**: with that name changed, `start_logger()`
never touches `uvicorn.access` at all (Part B2's asymmetric loops), so
the empty-handlers/`propagate=False` state `Config.__init__` set survives
into the first and every later connection's `hasHandlers()` check, and no
access line is ever logged.

### C3. Default `LOGGING_CONFIG`

```python
# config.py:81-112
LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"()": "uvicorn.logging.DefaultFormatter",
                     "fmt": "%(levelprefix)s %(message)s", "use_colors": None},
        "access": {"()": "uvicorn.logging.AccessFormatter",
                    "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'},
    },
    "handlers": {
        "default": {"formatter": "default", "class": "logging.StreamHandler", "stream": "sys.stderr"},  # ext:// prefix omitted here: the tree's office-values rule reads it as an authority
        "access":  {"formatter": "access",  "class": "logging.StreamHandler", "stream": "sys.stdout"},  # ext:// prefix omitted here, as above
    },
    "loggers": {
        "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error":  {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}
```
`uvicorn.access` is given its own `access` handler (a plain
`StreamHandler` to `sys.stdout`) and `propagate: False`. `uvicorn.error`
carries **no** `handlers` key and **no** `propagate` key — it inherits
Python logging's defaults (no handlers of its own, `propagate=True`), so
by design its records bubble up to the parent `uvicorn` logger's
`default` handler (stderr) under uvicorn's own unmodified configuration.
`uvicorn`'s own top-level logger gets the `default` handler and
`propagate: False` (stopping bubbling to the root logger).

### C4. With `AUDIT_UVICORN_LOGGER_NAMES=uvicorn.error`, what reaches stdout, and is it harmless

Tracing OWUI's `start_logger()` (Part B2) under this changed value: the
first loop (hard-coded `['uvicorn', 'uvicorn.error']`) empties both
loggers' handlers; the second loop, now iterating `['uvicorn.error']`,
re-attaches a single `InterceptHandler` to `uvicorn.error` specifically
(not to `uvicorn`). `uvicorn.error`'s `propagate` was never set to
`False` by anything read in this note (uvicorn's own `LOGGING_CONFIG`
doesn't set it, Part C3; OWUI's loops don't touch `propagate` at all),
so it stays at Python's default, `True` — meaning a record logged on
`uvicorn.error` is handled by `uvicorn.error`'s own `InterceptHandler`
*and* then offered to `uvicorn`'s logger, whose own handlers were just
emptied (no-op) and whose `propagate=False` (set once, at uvicorn's own
`Config.__init__` time, and never touched by OWUI) stops it going any
further — so each `uvicorn.error` line reaches the frontend's stdout
exactly once via the intercept path, with no duplicate via the root
logger (root's own handler, set by `logging.basicConfig(...,
force=True)` at `utils/logger.py:211`, is a second `InterceptHandler`
instance, but it is never reached here because `uvicorn`'s
`propagate=False` blocks the chain before it gets there).

What actually gets logged on `logging.getLogger("uvicorn.error")` — the
module-level `logger` uvicorn itself uses
(`config.py:114`, `logger = logging.getLogger("uvicorn.error")`) — is
uvicorn's own lifecycle/diagnostic output: startup/shutdown messages,
worker-process messages, and any of uvicorn's own error-level logging;
this note did not exhaustively enumerate every `logger.info`/`.error`
call site across uvicorn's `main.py`/`server.py`/`lifespan.py` (out of
scope for this ticket, and the module names alone make the content
self-evident: process lifecycle, not per-request access lines). Critically,
none of uvicorn's own **access**-line logging lives on `uvicorn.error` —
that is exclusively `uvicorn.access` (Part C2/C3), a logger the "point
`AUDIT_UVICORN_LOGGER_NAMES` at `uvicorn.error`" change never touches —
so this reroute cannot itself leak a request URI or a `Referer`; it only
changes whether uvicorn's own startup/shutdown chatter, previously
silently dropped (emptied by the *first* loop under the *default*
config, with nothing in `AUDIT_UVICORN_LOGGER_NAMES` naming `uvicorn.error`
to re-attach a handler), now reaches the frontend's journal — the
ticket's "harmless" characterization is confirmed by tracing the actual
call sites' logger names, not just by their generic severity.

### B7. The ASGI middleware's redirects, the share target, the OpenSearch descriptor, and Caddy's `resp_headers` (added after the plan's Codex review, 2026-09-09)

Two more routes reach the chat page with a user's text in the URL, and one
more field of Caddy's line carries it. The plan's Codex review raised the
first; all three were read at the tags by the planning session.

**`utils/asgi_middleware.py`, `AppHTTPMiddleware._redirect_legacy_url`
(`:164-206`)** — the middleware's docstring lists "serve the legacy
`/watch` and `?shared=` redirects" (`:60`). The function acts on `GET`
alone (`:165-166`), only when the path ends in `/watch` or the raw query
carries the substring `shared` (`:173-174`), then parses the query
(`:176`): a `/watch` path with a `v` parameter becomes
`redirect_params['youtube'] = query_params['v'][0]` (`:179-180`); a
`shared` parameter's text is matched against `https://\S+` (`:185`) —
a YouTube link becomes `youtube=<video id>` (`:191-194`), any other link
`load-url=<link>` (`:196`), and plain text `q=<text>` (`:198`); the
response is `RedirectResponse(url=f'/?{urlencode(redirect_params)}')`
(`:201-202`), a relative `Location` whose query carries the text under
`q`, `youtube`, or `load-url`. So the prompt sits in the request line
twice over — as `shared` (or `v`) in the request URI, then as `q` (or
`youtube`, `load-url`) in the response's `Location` header, then in the
browser's next request URI and its `Referer`.

**Where `?shared=` comes from** — the web-app manifest `main.py` serves
declares a share target (`main.py:2878-2882`): `'share_target': {'action':
'/', 'method': 'GET', 'params': {'text': 'shared'}}` — a phone that
"shares" text to the installed app opens `/?shared=<text>`. **And a third
route to `?q=`**: the OpenSearch descriptor at `/opensearch.xml`
(`main.py:2896-2912`) offers browsers a search template
`{webui_url}/?q={searchTerms}`, so a browser's address-bar search against
the frontend lands on the chat page with the terms under `q`.

**Caddy logs response headers** — `modules/caddyhttp/server.go`'s
`logRequest` (`:843`) adds `zap.Object("resp_headers",
LoggableHTTPHeader{…})` (`:902`) to every access-log entry, the same
`LoggableHTTPHeader` marshaler Part A3 describes for the request's
headers: canonical header names as keys, each value a string array, the
four credential headers alone redacted. `Location` is not among the four,
so a redirect's target is logged in full under `resp_headers>Location` —
a field the `query` filter handles exactly as it handles
`request>headers>Referer` (a string array, per element; Part A2), and
`url.Parse` accepts the relative `/?q=…` form.

Consequence for the plan: the redaction list is `q`, `shared`, `redirect`,
`load-url`, `youtube`, and `v`, applied to `request>uri`,
`request>headers>Referer`, and `resp_headers>Location` — one list on
three fields, since the redirect rewrites `shared` and `v` into `q`,
`youtube`, or `load-url`, and a Referer can carry any of them.

---

## What could not be verified

- **`internal/logmarshalers.go`'s exact byte content and line numbers**
  (Part A3, the `LoggableHTTPHeader` credential-redaction code): this
  session lost its shell/network tool access partway through research (a
  worktree-isolation lock unrelated to the source material — see the note
  above) and could only reach this one remaining Caddy file through a
  fetch tool that runs content through a summarizing step. Two
  independent fetches agreed, including on a distinctive joke code
  comment, which is a real but not conclusive signal of verbatim
  accuracy. The four-header redaction list and the "logged with empty
  values"/`log_credentials` behavior it reports is independently
  corroborated by Caddy's own docs page, quoting the identical four
  header names verbatim, so the *substance* of this claim is confirmed
  by two independent sources even though the Go file itself is not
  byte-verified.
- **`uvicorn.error`'s exhaustive call-site inventory** (Part C4): this
  note confirms the mechanism (which logger, which handler, whether it
  propagates) precisely from source, but did not enumerate every
  `logger.info`/`logger.error`/`logger.warning` call across uvicorn's
  `main.py`, `server.py`, and `lifespan.py` to produce a complete list of
  what text would appear — out of scope for the ticket's question, which
  only needed the *route* (does it reach stdout, could it carry a URL)
  confirmed, not a full transcript.
- **`AUDIT_UVICORN_LOGGER_NAMES`'s wiring from the constructor to the
  middleware call site, and `ENABLE_AUDIT_GET_REQUESTS`'s wiring into
  `AuditLoggingMiddleware(audit_get_requests=...)`** (Part B3): this note
  read the middleware's own constructor signature and default
  (`audit_get_requests: bool = False`) and confirmed the environment
  variable's name/default/parsing in `env.py`, but did not re-fetch
  `main.py`'s `add_middleware(...)` call to confirm the variable is
  passed through unchanged (no renaming, no inversion) — a gap the cited
  prior note's own method notes flag as a common one for this codebase's
  wiring pattern; treated here as very likely but not independently
  re-read.
- **Docker's default `HEALTHCHECK` interval** (Part B6, "30s absent
  `--interval`") is cited to Docker's public reference documentation, not
  independently re-read from Docker Engine's own source in this note.
- **Whether GIDEON's own Compose `healthcheck:` (if any) overrides or
  duplicates the image's built-in `HEALTHCHECK`** was not checked — this
  note verified only the image's own shipped healthcheck exists and would
  produce a GET, not what GIDEON's current render actually does with it.
- **A live, on-box reproduction of any of the above** (a real request
  through Caddy with the `filter` encoder configured, or a real
  `--no-access-log` frontend) — this note is a source-reading exercise
  only, consistent with the acceptance criteria's separate on-box proof
  step, not a substitute for it.

## Environment note — why this note has no branch or commit

This research session was spawned as a background `researcher` while the
orchestrating session was already inside the ticket's cycle worktree
(`.claude/worktrees/prompt-in-the-chat-page-url`), the exact failure mode
`CLAUDE.md`'s "Subagents" section names: this session inherited that
worktree as a pinned working directory, and every `Bash` invocation was
refused outright (even a bare `cd` or `true`, regardless of target path)
once that pin took effect partway through this session — including the
`git worktree add`/commit/push sequence the researcher's own convention
calls for, and the direct network access (`curl`) this note's early
citations relied on before the pin took effect. `ExitWorktree` refused
too, for the same reason ("cannot be called from a subagent with a cwd
override"). All source-reading in Parts A–C above was completed either
before the lock took effect (direct `curl` fetches, byte-exact) or
afterward via the `WebFetch` tool (which is not cwd-gated); this file
itself was written with the `Write` tool to the session's scratchpad,
since writes into the shared checkout and into the pinned worktree's
`docs/research/` were both refused (the latter would in any case have
placed it on the cycle's `feat/prompt-in-the-chat-page-url` branch, not a
`research/*` branch off `main`). Per `CLAUDE.md`, the fix is on the
calling session: spawn `researcher` before `EnterWorktree`, or leave the
worktree with `ExitWorktree` (`keep`) before spawning/committing and
re-enter by path afterward. This note's content is complete and cited;
only the `research/ingress-log-filter` branch, its commit, and its push
are outstanding.

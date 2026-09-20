# GIDEON API service — architecture leaf

**Read this when** changing the mounted `gideon-api` service, its OpenAI-compatible routes, its image, or its source-digest boundary; the owning modules are `gideon/api/{settings,auth,upstream,relay,app,__main__}.py`, `host/render/api.py`, and their tests. The map is [`../ARCHI.md`](../ARCHI.md); its §8 table names every command's leaf.

## Inventory

| Surface | Code | Spec / ADR |
|---|---|---|
| the service entry point, health route, and `GET /v1/models` | `gideon/api/{__main__,app}.py` | §17.1, §19.5, ADR-0045 |
| startup environment and Compose-mounted keys | `gideon/api/settings.py`, `gideon/api/auth.py` | §1.7, §17.1 |
| `POST /v1/chat/completions`, the pass-through relay and its disconnect watch | `gideon/api/relay.py` | §17.1, §19.4, ADR-0045 |
| the bounded OpenAI-compatible engine client | `gideon/api/upstream.py` | §17.1, the HTTP stack research note |
| the built interpreter image and its dependency set | `images/gideon/Dockerfile`, `images.lock` | §17.1, ADR-0032, ADR-0047 |
| the mounted source digest and import boundary | `host/render/{api,command}.py`, `tests/test_service_import_boundary.py` | §3.5, ADR-0047 |

## Commands

- **`python -m gideon.api`** (`gideon/api/__main__.py`): reads the environment and both mounted secret files, refuses a missing or empty value, then runs the Starlette application under plain uvicorn. `/health` is unauthenticated and does not contact the engine; `GET /v1/models` requires the bearer key, passes the engine's status and body through, and returns a fixed 502 body on transport failure. The pure ASGI middleware logs only the method, matched route template, status, and elapsed seconds.

`POST /v1/chat/completions` sends the caller's body bytes and `Content-Type` to the engine unparsed — the model id and every other field pass by construction, the bearer becomes the engine's key, and no other caller header is forwarded. **The engine's answer decides the shape, never the request**: a 200 whose media type is `text/event-stream` — the text before any `;`, trimmed, compared without regard to case, so the engine's `; charset=utf-8` reads as a stream — is relayed one `aiter_raw()` chunk per body message with the engine's content-type header as sent and no length; anything else is read whole and returned under the engine's status, content type, and bytes. A transport failure or timeout before the headers, or during a whole read, is the fixed 502; a failure after a stream has started sends a blank line, the same error object as one `data:` event, the end marker, and a clean end, and names the failure by its exception class alone on `gideon.api.relay`. The endpoint is a pure ASGI callable — Starlette takes a non-function endpoint as an ASGI application — and runs its work in an `anyio` task group beside a task blocked on `receive()`: whichever finishes first cancels the group, so a caller's hang-up cancels the upstream wherever it is suspended and httpcore closes the connection under its own shield, which the engine reads as its client going away. The watch is the service's own, independent of the ASGI `spec_version` the server advertises, and it is the scope ticket 06's lag window and trip will sit inside. The upstream response is closed in the exit path under a shielded scope.

The request line gained two truths the watch makes necessary: `-` in the status field when no response had started, and the word `disconnected` when the caller left before the response finished — a disconnect arriving after the final body message is the server's own end-of-response signal and is not one.

## Configuration

The service reads `GIDEON_ENGINE_URL`, `GIDEON_ENGINE_API_KEY_FILE`, `GIDEON_API_KEY_FILE`, and `GIDEON_API_PORT`. The URL is the engine's OpenAI base ending in `/v1`; both key variables name Compose secret mounts inside the container, and a missing or empty file refuses startup with its path. The API key middleware compares the bearer value as bytes and answers unauthorized requests with an OpenAI-shaped 401 and `WWW-Authenticate: Bearer`.

The image has the pinned Python base and ten build arguments: `STARLETTE_VERSION`, `UVICORN_VERSION`, `HTTPX_VERSION`, `ANYIO_VERSION`, `HTTPCORE_VERSION`, `H11_VERSION`, `CERTIFI_VERSION`, `IDNA_VERSION`, `CLICK_VERSION`, and `TYPING_EXTENSIONS_VERSION`. It installs only those wheels, runs `pip check`, and runs as the unprivileged `gideon` user; the repository is not copied into it.

`API_SOURCES` is the checkout-relative package directory `gideon/api`. The render loader walks it through the `Host` seam, ignores `__pycache__` and `.pyc`, and hashes sorted relative paths and texts. The resulting `sha256:` digest is a Compose label, so a source move recreates `gideon-api` by its changed block. `tests/test_service_import_boundary.py` holds every declared Python file to the standard library, the image's four imported dependency roots, or the declared package, keeps `gideon` imports at module level, and keeps the parent initializer to its docstring and `__version__` assignment.

## Release constants by module

The constants follow [`render-apply.md`](render-apply.md)'s rule: a reader opens the module for the name and value.

- `gideon/api/upstream.py` — the connection and read bounds for the engine client, and the completion's own longer read bound (httpx's read bound is per network read, so a completion silent until it is finished needs it).
- `gideon/api/__main__.py` — uvicorn's graceful-shutdown bound.
- `gideon/host/render/api.py` — the service, image, secret, route, port, mount, working-directory, source, and probe identities shared by Compose, Prometheus, and Grafana, and the three forwarded user-header names (`X-OpenWebUI-User-Name`, `-Email`, `-Role`) that the turn harness's service door fills for the eval identity; no rendered artifact reads them yet, so they move no byte and are outside `API_SOURCES`.

## Tests

- `test_api_service.py` — settings refusals, bearer authentication, health, model-list pass-through, bounded upstream failures, and content-free request logging over `httpx.MockTransport`.
- `test_api_completions.py` — the completion relay driven at the ASGI level over a gated stub engine: the stream's order and per-event flush, the three content-type spellings, the whole and error paths, both disconnects at ASGI `2.3` and `2.4`, the mid-stream failure's events, the read bounds, and the log's two additions. Every wait is bounded, so a buffering relay fails rather than hangs.
- `test_api_relay_sockets.py` — the same two claims through a real `uvicorn.Server`, httpx's real transport, and `http.client` over ephemeral loopback ports: the first event read while the stub still holds the second, and the client's close reaching the stub. It is the re-proof CI runs when the pin watch moves the HTTP stack's pins; the box's proof runs once.
- `test_service_import_boundary.py` — the declared-source import and parent-initializer tripwires, including planted violations.
- `test_render_api.py` — the service block, key, probe, rule, source digest, recreation judgment, and GPU/no-GPU rendering.

## Cross-references

- Cites: [`render-apply.md`](render-apply.md), [`stack.md`](stack.md), [`engine-frontend.md`](engine-frontend.md), [`tests.md`](tests.md)
- Cited by: the map's §§2, 3, 4, 8, 12, and 16

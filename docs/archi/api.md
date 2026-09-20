# GIDEON API service — architecture leaf

**Read this when** changing the mounted `gideon-api` service, its OpenAI-compatible routes, its image, or its source-digest boundary; the owning modules are `gideon/api/{settings,auth,upstream,app,__main__}.py`, `host/render/api.py`, and their tests. The map is [`../ARCHI.md`](../ARCHI.md); its §8 table names every command's leaf.

## Inventory

| Surface | Code | Spec / ADR |
|---|---|---|
| the service entry point, health route, and `GET /v1/models` | `gideon/api/{__main__,app}.py` | §17.1, §19.5, ADR-0045 |
| startup environment and Compose-mounted keys | `gideon/api/settings.py`, `gideon/api/auth.py` | §1.7, §17.1 |
| the bounded OpenAI-compatible engine client | `gideon/api/upstream.py` | §17.1, the HTTP stack research note |
| the built interpreter image and its dependency set | `images/gideon/Dockerfile`, `images.lock` | §17.1, ADR-0032, ADR-0047 |
| the mounted source digest and import boundary | `host/render/{api,command}.py`, `tests/test_service_import_boundary.py` | §3.5, ADR-0047 |

## Commands

- **`python -m gideon.api`** (`gideon/api/__main__.py`): reads the environment and both mounted secret files, refuses a missing or empty value, then runs the Starlette application under plain uvicorn. `/health` is unauthenticated and does not contact the engine; `GET /v1/models` requires the bearer key, passes the engine's status and body through, and returns a fixed 502 body on transport failure. The pure ASGI middleware logs only the method, matched route template, status, and elapsed seconds.

## Configuration

The service reads `GIDEON_ENGINE_URL`, `GIDEON_ENGINE_API_KEY_FILE`, `GIDEON_API_KEY_FILE`, and `GIDEON_API_PORT`. The URL is the engine's OpenAI base ending in `/v1`; both key variables name Compose secret mounts inside the container, and a missing or empty file refuses startup with its path. The API key middleware compares the bearer value as bytes and answers unauthorized requests with an OpenAI-shaped 401 and `WWW-Authenticate: Bearer`.

The image has the pinned Python base and ten build arguments: `STARLETTE_VERSION`, `UVICORN_VERSION`, `HTTPX_VERSION`, `ANYIO_VERSION`, `HTTPCORE_VERSION`, `H11_VERSION`, `CERTIFI_VERSION`, `IDNA_VERSION`, `CLICK_VERSION`, and `TYPING_EXTENSIONS_VERSION`. It installs only those wheels, runs `pip check`, and runs as the unprivileged `gideon` user; the repository is not copied into it.

`API_SOURCES` is the checkout-relative package directory `gideon/api`. The render loader walks it through the `Host` seam, ignores `__pycache__` and `.pyc`, and hashes sorted relative paths and texts. The resulting `sha256:` digest is a Compose label, so a source move recreates `gideon-api` by its changed block. `tests/test_service_import_boundary.py` holds every declared Python file to the standard library, the image's four imported dependency roots, or the declared package, keeps `gideon` imports at module level, and keeps the parent initializer to its docstring and `__version__` assignment.

## Release constants by module

The constants follow [`render-apply.md`](render-apply.md)'s rule: a reader opens the module for the name and value.

- `gideon/api/upstream.py` — the connection and read bounds for the engine client.
- `gideon/api/__main__.py` — uvicorn's graceful-shutdown bound.
- `gideon/host/render/api.py` — the service, image, secret, route, port, mount, working-directory, source, and probe identities shared by Compose, Prometheus, and Grafana.

## Tests

- `test_api_service.py` — settings refusals, bearer authentication, health, model-list pass-through, bounded upstream failures, and content-free request logging over `httpx.MockTransport`.
- `test_service_import_boundary.py` — the declared-source import and parent-initializer tripwires, including planted violations.
- `test_render_api.py` — the service block, key, probe, rule, source digest, recreation judgment, and GPU/no-GPU rendering.

## Cross-references

- Cites: [`render-apply.md`](render-apply.md), [`stack.md`](stack.md), [`engine-frontend.md`](engine-frontend.md), [`tests.md`](tests.md)
- Cited by: the map's §§2, 3, 4, 8, 12, and 16

---
verified_against:
  - pin: images.searxng
    version: 2026.9.7-3e454637f
  - pin: images.open-webui
    version: v0.11.3
---
# SearXNG service and Open WebUI v0.11.3 web search — verified facts

Scope: GIDEON slice-1 ticket 15 (`.scratch/slice-1/issues/15-web-search-searxng.md`) and
ticket 16 (`.scratch/slice-1/issues/16-no-search-query-text.md`); spec §15 and §22.1's
line 632. Sources checked 2026-09-08.

**SearXNG**: `searxng/searxng` at commit `3e454637fb9829756c805dd9c02100f0bc9520fd`
(`git show -s` on `master`; `git log` confirms this is the tip as of 2026-09-08), which is
the exact commit the newest published container tag was built from (§1). File paths below
are relative to that repo's root unless marked otherwise. Its container base image,
`searxng/base`, is a separate repository read at its `main` tip, commit `319b08cb7c51...`
(2026-09-04) — that repo carries no version tags of its own; see §1.

**Open WebUI**: tag `v0.11.3`, commit `2a960a59fe1dbbd35282f0556b3666d81102e781` (same
commit `docs/research/owui-engine-connection.md` and `docs/research/owui-preset-system-prompt.md`
verified against). File paths are relative to `backend/open_webui/` unless noted; frontend
paths relative to `src/`. Where those two notes already established a fact, it is cited and
not re-derived (per this ticket's Q10 instruction).

Every quoted line was fetched from `raw.githubusercontent.com` at the exact commit/tag named,
or from the GitHub Contents/Commits/Search API, or from Docker Hub's public tag API, on
2026-09-08. `docs.searxng.org` was not used as a source of record — where a `.rst` file under
`docs/` in the pinned commit itself is quoted, that is source, not secondary documentation,
and is called out as such.

---

## Part A — the SearXNG image

### 1. Registries, tag grammar, newest tag, and the build workflow

**Two registries, one multi-arch manifest, two tags per release.** `.github/workflows/container.yml`
(commit above) has three jobs: `build` (one per arch, `amd64`/`arm64`/`armv7`, via
`make container.build`), `test`, and `release` — the release job logs into **both**
`docker.io` and `ghcr.io` and runs `make container.push`, whose implementation
(`utils/lib_sxng_container.sh:257-286`) does:

```sh
# utils/lib_sxng_container.sh:257-286
release_tags=("$DOCKER_TAG" "latest")
for tag in "${release_tags[@]}"; do
    podman manifest create "localhost/$CONTAINER_IMAGE_ORGANIZATION/$CONTAINER_IMAGE_NAME:$tag"
    for i in "${!archs[@]}"; do
        podman manifest add ... "containers-storage:ghcr.io/.../cache:$CONTAINER_IMAGE_NAME-${archs[$i]}${variants[$i]}"
    done
done
release_registries=("ghcr.io" "docker.io")
for registry in "${release_registries[@]}"; do
    for tag in "${release_tags[@]}"; do
        podman manifest push "localhost/.../$tag" "docker://$registry/.../$tag"
    done
done
```

So `docker.io/searxng/searxng` and `ghcr.io/searxng/searxng` both get the same two tags —
a dated version tag and `latest` — as **OCI image-index manifests covering all three
architectures** (confirmed independently from Docker Hub's API: the `latest` and
`2026.9.7-3e454637f` tags both report `"media_type": "application/vnd.oci.image.index.v1+json"`
with `amd64`/`arm64`/`arm/v7` sub-images, each `"os": "linux"`).

**Tag grammar** is computed in `searx/version.py:67-79` (`get_git_version`):

```python
# searx/version.py:69-73
git_commit_date_hash: str = subprocess_run(r"git show -s --date='format:%Y.%m.%d' --format='%cd+%h'")
git_commit_date_hash = git_commit_date_hash.replace('.0', '.')   # strip leading zero, month/day
docker_tag: str = git_commit_date_hash.replace("+", "-")
```

i.e. `<committer-year>.<committer-month>.<committer-day>-<short-hash>`, leading zeros on
month/day dropped. This is computed and frozen at container-build time by
`utils/lib_sxng_container.sh:88` (`python -m searx.version freeze`), from the commit
being built — **not** from whatever `master` is at pull time.

**Newest tag as of 2026-09-08**: `2026.9.7-3e454637f`, pushed 2026-09-07T11:24:39Z per the
Docker Hub tags API (`GET https://hub.docker.com/v2/repositories/searxng/searxng/tags`),
built from commit `3e454637fb9829756c805dd9c02100f0bc9520fd` — verified independently via
`GET https://api.github.com/repos/searxng/searxng/commits/3e454637f`, which returns that
full sha with commit message `[fix] braveapi: set JSON Accept header (#6666)`. No digest is
resolved here per the instruction; `images.lock` does not yet carry a `searxng` entry.

**Build inputs**: `container/builder.dockerfile` (compiles the venv from
`requirements.txt`/`requirements-server.txt`, compiles `.py`→`.pyc`, pre-gzips/brotlis
static assets) and `container/dist.dockerfile` (`FROM docker.io/searxng/base:searxng AS dist`,
copies the built venv and `searx/` tree in, `COPY --chown=977:977`). **Neither Dockerfile
declares a `HEALTHCHECK`** (confirmed by reading both files whole — 31 and 44 lines).

*What GIDEON does with this*: the mirrored pin in `images.lock` should record `docker_tag`
grammar as above so the pin-watch proposal text reads sensibly; GIDEON declares its own
Compose healthcheck per §3.

### 2. Entrypoint behaviour

**Base image env vars** (`searxng/base`, `main` @ `319b08cb7c51`, `Dockerfile`, the
`searxng` build stage):

```dockerfile
# searxng/base Dockerfile, "searxng" stage
COPY <<EOF /etc/passwd
root:x:0:0:root:/usr/local/searxng/:/usr/bin/sh
searxng:x:977:977:searxng:/usr/local/searxng/:/usr/bin/sh
EOF
RUN install -dm0555 -o 977 -g 977 /usr/local/searxng/; \
    install -dm0755 -o 977 -g 977 /etc/searxng/; \
    install -dm0755 -o 977 -g 977 /var/cache/searxng/
ENV ... __SEARXNG_CONFIG_PATH="/etc/searxng" __SEARXNG_DATA_PATH="/var/cache/searxng"
```

So the config directory the entrypoint manages is named by `__SEARXNG_CONFIG_PATH`
(double-underscore, **not** `SEARXNG_SETTINGS_PATH` — that is a separate, Python-level
env var read by `searx/settings_loader.py`, §Q2b below), fixed to `/etc/searxng`, owned
`977:977`, mode `0755`; data dir `__SEARXNG_DATA_PATH=/var/cache/searxng`, same owner/mode.
Base image is built on Void Linux (`xbps`) from scratch layers, not Alpine/Debian; the
final `searxng` stage's package list is `libstdc++ tzdata python3 wget` — **`wget` is
present, `curl` is not** (curl exists only in the `searxng-builder` and `ci` stages, not
in `dist`). **No `USER` directive** appears in either `searxng/base`'s `searxng` stage or
`container/dist.dockerfile`, so **the container runs as root (uid 0) by default**; the
`searxng` user (977:977) exists but is not activated unless the Compose file sets
`user: "977:977"` (upstream's own `container/docker-compose.yml` does not set it either).

**`container/entrypoint.sh` (126 lines), the `setup()` function** (lines 80-97):

```sh
# container/entrypoint.sh:80-97
setup() {
    local template_settings="/usr/local/searxng/settings.template.yml"
    local target_settings="$__SEARXNG_CONFIG_PATH/settings.yml"
    if [ ! -f "$target_settings" ]; then
        cp -pfT "$template_settings" "$target_settings"
        sed -i "s/ultrasecretkey/$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9')/g" "$target_settings"
    fi
    check_file "$target_settings"
}
```

**If `settings.yml` does not already exist** at `$__SEARXNG_CONFIG_PATH/settings.yml`, the
entrypoint copies `settings.template.yml` (the whole file, 8 lines — `use_default_settings:
true`, `server.secret_key: "ultrasecretkey"`, `server.image_proxy: true`) and rewrites the
placeholder secret key in place with `sed`, to a random 24-byte-base64-derived alnum string.
**If it already exists** (a GIDEON bind mount), the entrypoint does nothing to it beyond
`check_file` (exists-and-is-a-file) — no secret-key rewrite, no re-generation.

**Directories that must be writable, and the read-only-mount case.** Before `setup()`,
`volume_handler` runs on both `__SEARXNG_CONFIG_PATH` and `__SEARXNG_DATA_PATH`
(lines 72-78, 104-105): `check_directory` (must exist) then `setup_ownership`
(lines 35-70), which `stat`s the directory and, if not owned `searxng:searxng`, either
`chown -R` it (when `FORCE_OWNERSHIP` — default `true` — **and** running as root) or else
only prints a `WARNING` and continues. The script's shebang is `set -u` only (**no
`set -e`**), so a `chown -R` that fails against a read-only-mounted directory does not
abort startup — it errors to stderr and the script proceeds to `setup()` and then `exec`.
Net effect for a GIDEON-rendered, pre-populated, **read-only-mounted** `settings.yml`:
`setup()` never attempts to write it (branch not taken since the file exists);
`setup_ownership` on its parent directory may print a chown warning if ownership doesn't
match `977:977` but does not block startup either way, because the default container user
is root and the settings mount only needs to be *readable*, not writable, once populated.

**Port and bind**: base sets `GRANIAN_PORT="8080"`, `GRANIAN_HOST="::"` (`dist.dockerfile`);
`EXPOSE 8080`. The final line of the entrypoint is:

```sh
# container/entrypoint.sh:114-126
case "${SEARXNG_PORT:-}" in
    '') ;;
    *[!0-9]*) unset SEARXNG_PORT ;;
    *) export GRANIAN_PORT="$SEARXNG_PORT" ;;
esac
exec /usr/local/searxng/.venv/bin/granian searx.webapp:app
```

so a numeric `SEARXNG_PORT` env var re-points granian's own bind port, in addition to
overriding the Python-level `server.port` setting (next paragraph) — these are two
independent mechanisms that happen to be aliased together here.

**Env vars honoured by the settings loader** (`searx/settings_defaults.py:181-222`,
the `SCHEMA` dict — each entry is `SettingsValue(type, default, environ_name)`, and
`SettingsValue.__call__` (`settings_defaults.py:90-98`) **always lets the env var win**
over whatever is in `settings.yml` when the env var is set):

| Setting | Default | Env var |
|---|---|---|
| `general.debug` | `False` | `SEARXNG_DEBUG` |
| `server.port` | `8888` | `SEARXNG_PORT` |
| `server.bind_address` | `127.0.0.1` | `SEARXNG_BIND_ADDRESS` |
| `server.limiter` | `False` | `SEARXNG_LIMITER` |
| `server.public_instance` | `False` | `SEARXNG_PUBLIC_INSTANCE` |
| `server.secret_key` | *(none — must be set)* | `SEARXNG_SECRET` |
| `server.base_url` | `False` | `SEARXNG_BASE_URL` |
| `server.image_proxy` | `False` | `SEARXNG_IMAGE_PROXY` |
| `server.method` | `GET` | `SEARXNG_METHOD` |
| `valkey.url` | `False` | `SEARXNG_VALKEY_URL` |
| `redis.url` (deprecated) | `False` | `SEARXNG_REDIS_URL` |

No `GRANIAN_IMAGE_PROXY` etc. exist — granian's own env vars (`GRANIAN_*`, see §3) are
separate from this table and control the WSGI server process, not `searx`'s settings
object. `settings_loader.py`'s docstring (module top) is explicit that
**`SEARXNG_SETTINGS_PATH`** (a third, Python-only env var, distinct from the container's
`__SEARXNG_CONFIG_PATH`) is what `get_user_cfg_folder()` (`settings_loader.py:64-104`)
reads to locate the folder or file holding `settings.yml`; the container's
`entrypoint.sh` never sets it, so inside the container it is implicitly the directory
named by the base image's `__SEARXNG_CONFIG_PATH` (`/etc/searxng`), which is also
`get_user_cfg_folder()`'s rule-3 default (`Path("/etc/searxng")`) — the two env vars
agree by construction in this image, not because either reads the other.

*What GIDEON does with this*: the note flags a read-only mount as safe under §2's reading;
it does not decide GIDEON's actual mount mode or `user:` value.

### 3. HEALTHCHECK, `/healthz`, and available HTTP clients

No Docker `HEALTHCHECK` in either Dockerfile (§1). `searx/webapp.py:597-599`:

```python
@app.route('/healthz', methods=['GET'])
def health():
    return Response('OK', mimetype='text/plain')
```

`searx/limiter.py:154-155` exempts this path from the limiter unconditionally
(`if request.path == '/healthz': return None`), so it is reachable even with
`server.limiter: true` and no valkey configured (in which case the limiter installs
`_INSTALLED = False` and does nothing anyway — see §5c). The final `searxng` build
stage's package set is `libstdc++ tzdata python3 wget` (`searxng/base` Dockerfile) — no
`curl`. A Compose healthcheck run **inside** this container must use `wget`, e.g.
`wget -qO- http://localhost:8080/healthz` (not tested on the box by this note; the CI
container test in `utils/lib_sxng_container.sh:203` runs `curl -vf ... /healthz` but that
`curl` executes on the **build runner**, against the container's published port from
outside — it says nothing about what binaries exist inside the image).

### 4. The secret-key rule

`searx/webapp.py:1358-1361`, inside `init()`, which runs at module import (i.e. whenever
`searx.webapp:app` is loaded, including by granian at process start):

```python
# searx/webapp.py:1358-1361
if not app.debug and get_setting("server.secret_key") == 'ultrasecretkey':
    logger.error("server.secret_key is not changed. Please use something else instead of ultrasecretkey.")
    sys.exit(1)
```

So **the server refuses to start** (exit 1) if `server.secret_key` resolves to the literal
placeholder and `general.debug` is false (GIDEON's case). `SEARXNG_SECRET` overrides
`server.secret_key` unconditionally when set (`settings_defaults.py:218`,
`environ_name='SEARXNG_SECRET'`) — this happens whether or not the file on disk already
has a real key, so an env-supplied secret always wins over the file. The key is used
for the Flask session cookie (`app.secret_key`, `webapp.py:151`) and as the HMAC key for
signed URLs (`new_hmac`/`is_hmac_of`, `webapp.py:318,998` — e.g. the image-proxy and
`/client<token>.css` link-token mechanisms).

*What GIDEON does with this*: a rendered `settings.yml` with a real, install-time-generated
`secret_key` (or a `SEARXNG_SECRET` env var) avoids the exit-1 path entirely; either
mechanism is a real, verified escape from the placeholder check.

### 5. Settings for GIDEON's rendered `settings.yml`

**(a) `use_default_settings` + `engines.keep_only`/`remove` — exact semantics, from
`searx/settings_loader.py:127-167` (`update_settings`)**:

```python
# searx/settings_loader.py:143-166
use_default_settings: dict | None = user_settings.get('use_default_settings')
if isinstance(use_default_settings, dict):
    remove_engines = use_default_settings.get('engines', {}).get('remove')
    keep_only_engines = use_default_settings.get('engines', {}).get('keep_only')
...
if remove_engines is not None:
    engines = list(filterfalse(lambda e: e.get('name') in remove_engines, engines))
if keep_only_engines is not None:
    engines = list(filter(lambda e: e.get('name') in keep_only_engines, engines))
```

Confirmed exactly as the ticket assumed: `keep_only`/`remove` match on the default
`settings.yml`'s per-engine `name:` field, and **both are only read when
`use_default_settings` is written as a dict** — `use_default_settings: true` (a bare
bool) does not expose `engines.keep_only` at all (`is_use_default_settings`,
`settings_loader.py:169-177`, only extracts `remove_engines`/`keep_only_engines` in the
`isinstance(..., dict)` branch). GIDEON's rendered file therefore needs:

```yaml
use_default_settings:
  engines:
    keep_only: [duckduckgo, brave, startpage, wikipedia, bing]
```

not `use_default_settings: true` plus a sibling key.

**`keep_only` does *not* enable a `disabled: true` default engine.** `filter`/`filterfalse`
here only add or remove **list membership**; they never touch an engine dict's own
`disabled` field. Reading `searx/settings.yml` at this commit directly for the five named
engines plus `google`:

| `name:` | line | `disabled:` in defaults |
|---|---|---|
| `duckduckgo` | `searx/settings.yml:869-871` | *(absent — enabled)* |
| `brave` | `searx/settings.yml:3237-3244` | *(absent — enabled)*, `network: brave`, no `api_key` needed |
| `braveapi` | `searx/settings.yml:3231-3235` | `inactive: true`, needs `api_key` — **not** the engine GIDEON's `web.engines: [..., brave, ...]` should map to |
| `startpage` | `searx/settings.yml:2345-2349` | *(absent — enabled)* |
| `wikipedia` | `searx/settings.yml:547-552` | *(absent — enabled)* |
| `bing` | `searx/settings.yml:559-563` | **`disabled: true`** |
| `google` | `searx/settings.yml:1205-1208` | **`disabled: true`** |

**This is a real gap for the spec's default engine list.** `web.engines` defaults to
`[duckduckgo, brave, bing, startpage, wikipedia]` (spec line 155), but `bing`'s default
entry carries `disabled: true`; `keep_only: [bing]` keeps it *in the list* but does not
clear that flag, so Bing would still not be queried unless the render also emits a
top-level `engines:` override merging `disabled: false` onto it — a **separate** merge
path (`settings_loader.py:159-164`, the `user_engines`/`update_dict` branch, matched by
`name`, applied *after* `keep_only`/`remove`), e.g.:

```yaml
engines:
  - name: bing
    disabled: false
```

`google` is `disabled: true` by default too, so excluding it from `keep_only` is
sufficient — no extra "off" setting is needed for Google specifically, since upstream
already ships it off.

**(b) `search.formats: [html, json]`.** Default is `formats: [html]`
(`searx/settings.yml:82-84`). The 403 gate is in `searx/webapp.py:625-631`:

```python
output_format = sxng_request.form.get('format', 'html')
if output_format not in OUTPUT_FORMATS:
    output_format = 'html'
if output_format not in settings['search']['formats']:
    flask.abort(403)
```

Confirmed: a `format=json` request is refused with HTTP 403 unless `json` is listed in
`search.formats`.

**(c) `server.limiter` and valkey.** Default `server.limiter: false`
(`searx/settings.yml:96-98`, also the `SCHEMA` default). `searx/limiter.py:222-244`
(`initialize`):

```python
# searx/limiter.py:229-238
valkey_client = valkeydb.client()
botdetection.init(cfg, valkey_client)
if not (settings['server']['limiter'] or settings['server']['public_instance']):
    return
if not valkey_client:
    logger.error("The limiter requires Valkey, please consult the documentation: ...")
    if settings['server']['public_instance']:
        sys.exit(1)
    return
_INSTALLED = True
```

Confirmed: with `limiter: false` and `public_instance: false` (both defaults), the
function returns immediately after the early guard — **no valkey connection is required
and the server starts fine.** A valkey client is only fatal if `public_instance: true`
*and* no valkey is reachable.

**(d) `outgoing.proxies` and environment-variable proxy inheritance — the client library
has changed since the version the ticket's question assumed.** At this commit the
outbound HTTP client is **`curl_cffi`, not `httpx`** — `searx/network/client.py:8`
(`from curl_cffi import AsyncSession, ...`). `new_client()` (`client.py:70-111`) builds
proxy kwargs *only* from the settings-derived `proxies: dict[str, str]` argument
(mapped by `_proxy_kwargs`, `client.py:47-63`, itself fed from
`settings['outgoing']['proxies']` via `searx/network/network.py:354-361`); it never
reads `os.environ` for proxy variables and passes no `trust_env` argument of its own —
so whatever `curl_cffi.AsyncSession`'s own default applies. Reading
`curl_cffi`'s `requests/session.py` (`lexiforest/curl_cffi`, `main` branch) directly:

```python
# curl_cffi/requests/session.py:235, :515
trust_env: bool = True
...
trust_env: use http_proxy/https_proxy and other environments, default True.
```

So **when `outgoing.proxies` is empty/unset in `settings.yml`, SearXNG's requests do
still pick up `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` from the process environment**, via
`curl_cffi`'s own default `trust_env=True` — a real mechanism, just not the httpx one the
question named, and not something SearXNG's own code explicitly wires up (it is silent
about it; nothing in `searx/network/*` disables it). Docs at this commit
(`docs/admin/settings/settings_outgoing.rst:50-59`, checked in the pinned commit, not a
live doc fetch) already reflect the curl_cffi migration:

```rst
.. _curl_cffi proxies: https://curl-cffi.readthedocs.io/en/latest/quick_start.html

``proxies`` :
  Define one or more proxies you wish to use, see `curl_cffi proxies`_.
  ...
  HTTP, HTTPS, SOCKS4, SOCKS5 and SOCKS5h proxies are supported
  (``http://``, ``https://``, ``socks4://``, ``socks5://``, ``socks5h://``).
```

and the commented example in `searx/settings.yml:176-192` (`outgoing:` block) shows:

```yaml
#  proxies:
#    all://:
#      - http://proxy1:8080
#      - http://proxy2:8080
```

— i.e. credentials, if any, go in the proxy URL itself (`http://user:pass@proxy1:8080`);
there is no separate credentials field. **Uncertain / not verified further**: whether
`curl_cffi`'s `trust_env` also honours `NO_PROXY` exactly like Python's `requests`
(the session module's docstring groups it under "other environments" without spelling out
`NO_PROXY` specifically) — settled by reading `curl_cffi`'s C-extension proxy-resolution
code or by an on-box test with `NO_PROXY` set and an internal host name.

**(e) `general.debug`, production log level, and access logging.** Default
`general.debug: false`. `searx/__init__.py:174-183` (`init_settings`):

```python
# searx/__init__.py:157-160,174-183
LOG_LEVEL_PROD = logging.WARNING
...
sxng_debug = get_setting("general.debug")
if sxng_debug:
    _logging_config_debug()
else:
    logging.basicConfig(level=LOG_LEVEL_PROD, format=LOG_FORMAT_PROD)
    logging.root.setLevel(level=LOG_LEVEL_PROD)
    logging.getLogger('werkzeug').setLevel(level=LOG_LEVEL_PROD)
```

**With `debug: false` (GIDEON's case), the root logger threshold is `WARNING`, not
`INFO`** — every `logger.info(...)` call in `searx/*` (e.g. the "load the user settings
from ..." message, `settings_loader.py`'s own return value logged at
`__init__.py:186-190`) is filtered out by this root level and never appears at all in
production. This is stronger than "disable-logging"-style suppression; it is the
process default.

**A real query-text leak exists at the WARNING level regardless of any format/debug
setting.** `searx/network/network.py:249-258` (`patch_response`, called on every engine
request when `raise_for_httperror` is true, which is the default for engine calls):

```python
# searx/network/network.py:249-258
if do_raise_for_httperror:
    try:
        raise_for_httperror(response)
    except:
        method = response.request.method if response.request else "?"
        url = response.request.url if response.request else response.url
        self._logger.warning(f"HTTP Request failed: {method} {url}")
        raise
```

`self._logger` here is `logger.getChild('network')` (`searx/network/network.py:29`,
i.e. logger name `searx.network`), and `WARNING` is *above* the production floor set in
(e), so **this line is emitted** whenever an engine responds with an HTTP error status.
Many engines (e.g. `bing`, `startpage`) issue their outbound request as a `GET` with the
query text in the URL's own query string, so a transient upstream error surfaces the
search query text at `WARNING` in SearXNG's own log — independent of `search.formats`,
`general.debug`, or anything else in this section. This is the SearXNG-side mirror of
the Open WebUI-side finding in Part B §11.

There is a second, unrelated warning path — `network.py:297`
(`self._logger.warning('ConnectionError: the server has disconnected, retrying')`) — that
carries no URL or query text.

**Access logging (uwsgi vs. granian).** This image uses **granian**, not uwsgi (uwsgi's
`docs/admin/installation-uwsgi.rst` documents a now-secondary, non-container install
path). Granian (`emmett-framework/granian`, `master`, `granian/cli.py:257`):

```python
@option('--access-log/--no-access-log', 'log_access_enabled', default=False, help='Enable access log')
```

**default `False`** — granian does not print per-request access-log lines
(which would include the requested path, `/search?q=...`) unless explicitly turned on.
The CLI is wired with `cli(auto_envvar_prefix='GRANIAN')` (`cli.py:606`); click derives
the auto-envvar name from the **declared parameter name**, not the flag text
(`click/core.py:3434`, `envvar = f"{ctx.auto_envvar_prefix}_{self.name.upper()}"`, and
`Option._parse_decls`, `click/core.py:3263-3296`, sets `self.name` from the identifier
string passed as the option's second positional arg when one is given — here
`'log_access_enabled'`). So the env var that turns this on is
**`GRANIAN_LOG_ACCESS_ENABLED`**, not the commonly assumed `GRANIAN_ACCESS_LOG` — verified
by reading click's own source rather than assumed. Neither `container/dist.dockerfile`
nor `container/entrypoint.sh` sets it, so **access logging is off by default in this
image** and GIDEON does not need to do anything further to keep it off — only avoid
setting it.

**(f) `server.image_proxy`, `server.method`, `search.safe_search`, `search.default_lang`,
`server.public_instance` — defaults and per-request override.** Defaults (all
`searx/settings.yml`): `server.image_proxy: false` (line 100), `server.method: "GET"`
(line 105, and the `/search` Flask route itself is registered `methods=['GET', 'POST']`
regardless of this setting — `webapp.py:616` — so `server.method` only picks which verb
SearXNG's own HTML `<form>` uses; it does not restrict what an API caller like Open WebUI
may send, and does not stop a GET-with-querystring request from appearing in a URL-bearing
log line per (e)), `search.safe_search: 0` (line 41), `search.default_lang: "auto"`
(line 55), `server.public_instance: false` (line 98). Per-request overrides
(`sxng_request.preferences.get_value(...)`, `webapp.py:381,829`, backed by the
`Preferences`/`SettingsPref` machinery in `searx/preferences.py`, not traced line-by-line
here) accept `safesearch`, `language`, `categories`, `pageno`, `time_range` as request
form/query parameters — this is how Open WebUI's own request (Part B §8) supplies
`safesearch`/`language`/`pageno`/`categories`/`time_range` per call, overriding whatever
`settings.yml` says for those fields. **Not traced further**: the exact precedence between
a signed cookie-based preference and a bare query param on the same request; would be
settled by reading `searx/preferences.py`'s `Preferences.__init__`/`parse_form` if it
ever matters to GIDEON.

**(g) No non-search outbound call found; the engine "checker" module does not exist at
this commit.** Searched: `searx/search/` (only `__init__.py`, `models.py`, `processors/`
— no `checker/` subpackage), the whole repo tree via the GitHub trees API for any path
containing "check" (`docs/dev/plugins/tor_check.rst`, `searx/plugins/tor_check.py` — a
user-facing self-test *plugin*, run on-demand by a signed-in user, not a background job;
`utils/searxng_check.py` — an operator-run local *installation* sanity script, no network
call beyond a valkey ping), and `searx/webapp.py` for any `requests.get`/`aiohttp`/version-
check call (none found). **This differs from older SearXNG documentation/lore describing
a periodic "Checker" admin feature that probes every engine** — no such module exists in
this commit's source; if GIDEON's plan assumed one, that assumption does not hold at this
version. Two outbound calls *do* exist but only fire on direct end-user action, not
spontaneously: the `autocomplete` backend (`search.autocomplete: "duckduckgo"` by default
— calls DuckDuckGo's autocomplete endpoint as a user types, when the theme's autocomplete
UI is used) and `favicon_resolver` (blank/off by default, `search.favicon_resolver: ""`).
`general.enable_metrics: true` by default records only in-process counters exposed at
`/metrics` (`searx/metrics/`, `searx/openmetrics.py`) — no outbound call.

**(h) `/config` needs no format parameter and lists each engine's enabled state.**
`searx/webapp.py:1246-1247,1257-1264`:

```python
@app.route('/config')
def config():
    """Return configuration in JSON format."""
    ...
    _engines.append({
        'name': name,
        ...
        'enabled': not engine.disabled,
        ...
    })
    ...
    return jsonify({...})
```

Always returns JSON via Flask's `jsonify` regardless of query params — an operator inside
the Compose network can `GET /config` and check that no entry named `google` has
`"enabled": true`.

### 6. Memory

**None found.** Checked `docs/admin/installation-docker.rst` (this commit) and
`container/docker-compose.yml` for any RAM/idle-memory figure or `mem_limit`; the only
numbers present are container **image sizes** on disk from a worked example
(`docs/admin/installation-docker.rst:287-291`: `265 MB`/`687 MB`/etc. for various stages/
tags), not runtime RSS. Upstream's own `container/docker-compose.yml` sets no
`mem_limit`/`deploy.resources` on either the `core` or `valkey` service. Would be settled
by measuring on GIDEON's own box.

---

## Part B — Open WebUI v0.11.3's web-search rig

### 7. Environment variables and `PersistentConfig` status

All from `backend/open_webui/config.py` at `v0.11.3` unless noted. **`ENABLE_PERSISTENT_CONFIG`
status**: this tag has no `PersistentConfig` class at all (confirmed: no
`class PersistentConfig`/`PersistentConfig(` text anywhere in `config.py`) — every key
below is a plain `os.getenv(...)`-derived module variable that also appears as an entry
in the `DEFAULT_CONFIG` dict (`config.py:2835`, e.g. `'web.search.enable': ENABLE_WEB_SEARCH`
at `config.py:2953`), which is the mechanism `docs/research/owui-engine-connection.md`
already traced in full (`Config.persistent_enabled_for(key)` returning `False` once
`ENABLE_PERSISTENT_CONFIG=false`, falling through to `Config.DEFAULTS[key]` — the
env-derived value, never a DB row). That note's mechanism applies identically to every
`web.search.*`/`task.query.search.*` key here; not re-derived.

| Var | Default | `config.py` |
|---|---|---|
| `ENABLE_WEB_SEARCH` | `False` | `:1155` |
| `ENABLE_WEB_SEARCH_CONFIRMATION` | `False` | `:1157` |
| `WEB_SEARCH_CONFIRMATION_CONTENT` | `'Your query will be sent to the configured web search provider.'` | `:1159-1162` |
| `WEB_SEARCH_ENGINE` | `''` | `:1164` |
| `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL` | `False` | `:1166-1168` |
| `BYPASS_WEB_SEARCH_WEB_LOADER` | `False` | `:1171` |
| `WEB_SEARCH_RESULT_COUNT` | `3` | `:1173` |
| `WEB_SEARCH_DOMAIN_FILTER_LIST` | `[]` (**a JSON array string**, not a delimited list — parsed with `JSONCodec.loads`) | `:1176-1188` |
| `WEB_SEARCH_CONCURRENT_REQUESTS` | `0` (0 = unlimited) | `:1190` |
| `WEB_LOADER_ENGINE` | `''` (empty string selects the `safe_web` loader — see §8) | `:1196` |
| `USER_AGENT` | `''` — read in `env.py`, not `config.py`; `SafeWebBaseLoader.__init__` sets it on `self.session.headers`, which `_fetch` forwards to aiohttp on every page fetch, so unset it is langchain's `DefaultLangchainUserAgent` (added 2026-09-10, slice-1 ticket 71: the module read inside the running container) | `env.py` |
| `WEB_LOADER_TIMEOUT` | `''` — `get_web_loader` passes a numeric value through as `requests_kwargs['timeout']`, each fetch's total; unset, `_fetch`'s aiohttp session has no timeout and aiohttp's own 300 s default applies (added 2026-09-10, slice-1 ticket 71) | `:1201` |
| `WEB_LOADER_CONCURRENT_REQUESTS` | `10` | `:1199` |
| `ENABLE_WEB_LOADER_SSL_VERIFICATION` | `True` | `:1204` |
| `WEB_SEARCH_TRUST_ENV` | `True` | `:1206` |
| `SEARXNG_QUERY_URL` | `''` | `:1211` |
| `SEARXNG_LANGUAGE` | `'all'` | `:1215` |
| `ENABLE_SEARCH_QUERY_GENERATION` | `True` | `:2315` |

**`WEB_SEARCH_DOMAIN_FILTER_LIST` is a combined allow+block list, matched on DNS-label
boundaries, not a plain suffix/substring test.** `backend/open_webui/utils/misc.py`:

```python
# misc.py:73-88 (get_allow_block_lists)
for raw_entry in filter_list or []:
    entry = _strip_filter_entry(raw_entry)
    is_blocked = entry.startswith('!')
    if is_blocked:
        entry = _strip_filter_entry(entry[1:])
    ...
    (block_list if is_blocked else allow_list).append(entry)
```

```python
# misc.py:129-140 (_host_matches_pattern)
"""pattern matches host when equal or a parent domain of it, so example.com
matches api.example.com but not a host that merely ends in the same letters."""
# (the docstring's two example domains replaced here with documentation values)
...
return host == pattern or host.endswith('.' + pattern)
```

Entries with no leading `!` form an allow-list (if non-empty, only matching hosts survive);
entries prefixed `!` are always blocked; an entry can also be a CIDR/address, matched by
containment (`as_network`, `misc.py:118-126`) rather than string comparison. This is used
both by `retrieval/web/main.py`'s `get_filtered_results` (post-search result filtering)
and by the SSRF-safe fetch path's own allow/deny check (`retrieval/web/utils.py`,
`_assert_host_allowed`) — i.e. the *same* filter list governs which search-result URLs are
kept and which URLs the fetcher is willing to connect to.

**`safe_web` loader (`WEB_LOADER_ENGINE` `''` or `'safe_web'`)**:
`retrieval/web/utils.py:1049` (`if engine == '' or engine == 'safe_web':`) selects
`SafeWebBaseLoader` (`utils.py:900`). It layers SSRF protection at the connection level —
`_SSRFSafeConnector`/`_SSRFSafeAdapter` (`utils.py:238-268`) re-validate every resolved
address against the filter list and reject non-global IPs on **each new connection**
(defends against DNS-rebinding — the docstring at `utils.py:184-186` says so explicitly),
not just against the hostname once. Default request timeout comes from
`AIOHTTP_CLIENT_TIMEOUT` (`env.py:595-597`, default `300` seconds when unset). **Not
verified**: where `WEB_FETCH_MAX_CONTENT_LENGTH` (a config key present in
`routers/retrieval.py`'s `DEFAULT_CONFIG`/`RETRIEVAL_CONFIG` plumbing, default `None` = no
cap) is actually consulted — grepping `retrieval/web/utils.py` for it finds nothing; it may
be enforced elsewhere (e.g. a streaming read-size guard) or not enforced at all in this
path. Settled by tracing every call site of `WEB_FETCH_MAX_CONTENT_LENGTH` in
`routers/retrieval.py` and `retrieval/web/utils.py`'s loader classes.

`BYPASS_WEB_SEARCH_WEB_LOADER` (default `False`, GIDEON keeps it `False` per the ticket):
when `True`, `routers/retrieval.py`'s `process_web_search` builds `Document`s directly
from each **search result's own snippet** (`result.snippet`, the metasearch result
excerpt), skipping the page fetch entirely (`routers/retrieval.py:2872-2884`) — GIDEON
leaving this `False` is what makes the `safe_web` loader actually run and fetch full
pages, consistent with the ticket's stated intent.

### 8. What the SearXNG client sends

`retrieval/web/searxng.py:37-80` (`search_searxng`), called from
`routers/retrieval.py:2474-2482` (`search_web`'s `elif engine == 'searxng':` branch):

```python
# retrieval/web/searxng.py:52-61
params = {
    'q': query,
    'format': 'json',
    'pageno': 1,
    'safesearch': kwargs.get('safesearch', '1'),
    'language': kwargs.get('language', 'all').strip().rstrip(','),
    'time_range': kwargs.get('time_range', ''),
    'categories': ''.join(kwargs.get('categories', [])),
    'theme': 'simple',
    'image_proxy': 0,
}
session = await get_session()
async with session.get(query_url, headers=_SEARXNG_HEADERS, params=params, ssl=_get_ssl_context()) as response:
    response.raise_for_status()
    payload = await response.json()
```

```python
# routers/retrieval.py:2474-2482
elif engine == 'searxng':
    if config.SEARXNG_QUERY_URL:
        searxng_kwargs = {'language': config.SEARXNG_LANGUAGE}
        return await search_searxng(config.SEARXNG_QUERY_URL, query, config.WEB_SEARCH_RESULT_COUNT, config.WEB_SEARCH_DOMAIN_FILTER_LIST, **searxng_kwargs)
```

Method is **HTTP GET**. Only `language` is threaded through from Open WebUI's own
config (`config.SEARXNG_LANGUAGE`, default `'all'`); **`safesearch`, `time_range`, and
`categories` are never overridden by the caller, so every request Open WebUI sends
carries the hardcoded `safesearch=1` (moderate)**, regardless of GIDEON's SearXNG-side
`search.safe_search` setting — a real, verified fact worth flagging since it means the
SearXNG-side safe-search default is moot for OWUI-originated queries.

**Legacy `<query>` placeholder is normalised away, not honoured**:
`searxng.py:47-49`: `if '<query>' in query_url: query_url = query_url.split('?')[0]` —
any `?...` suffix on a configured `SEARXNG_QUERY_URL` (the historically-documented form,
`http://host/search?q=<query>`) is discarded; the code always builds its own query string
from `params` above via aiohttp's `params=` kwarg. So `SEARXNG_QUERY_URL` should simply be
the bare endpoint, e.g. `http://searxng:8080/search`.

**Session**: the shared pooled `aiohttp.ClientSession` from
`utils/session_pool.py:64-78` (`get_session()`), created once with
**`trust_env=True` hardcoded** (`session_pool.py:74`) — *not* gated by
`WEB_SEARCH_TRUST_ENV`. That config flag governs a *different*, one-off SSRF-safe session
(`get_ssrf_safe_session`/`get_ssrf_safe_requests_session`, `retrieval/web/utils.py:269-289`)
used only by the page-fetching loader, confirmed by `routers/retrieval.py:2897-2900`
(`get_web_loader(..., trust_env=loader_config.get('web_search_trust_env'), ...)`) — this
matches ticket 15's 2026-09-05 triage note exactly (cited there, not re-derived), and this
research adds the specific confirmation that the *search-query* HTTP call's own proxy
behaviour is `trust_env=True` unconditionally, separate from that config key.

**Result count**: enforced in `search_searxng` itself, `searxng.py:78-83`
(`results[:count]`, `count=WEB_SEARCH_RESULT_COUNT`) — a real code-level cap of 3 per
query by default.

### 9. Per-turn page bound, query-count enforcement, and truncation

**No code-level cap on the number of generated queries.** The prompt template
(`config.py:2322-2345`, `DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE`) asks the model to
"prioritize generating 1-3 broad and relevant search queries," but
`utils/middleware.py`'s `chat_web_search_handler` (`:1500-1585`) takes whatever list the
model's JSON response contains (`queries = queries.get('queries', [])`,
`middleware.py:1552`) and passes it straight to `process_web_search` with no slicing —
confirmed by reading the whole handler and `routers/retrieval.py`'s
`process_web_search` (`:2800-2860`), which builds one `search_web(...)` task per entry in
`form_data.queries` with no `[:3]`-style limit anywhere in between. **So "≤ 9 pages/turn"
(3 queries × 3 results) is a prompt-only expectation, not an enforced ceiling** — if the
model returns more than 3 queries, more than 9 URLs are fetched; only the **per-query**
result count is code-enforced (§8's `results[:count]`), and fetched URLs are deduplicated
by exact link before loading (`routers/retrieval.py:2856`, `urls = list(dict.fromkeys(urls))`).

**With `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=true`, page content goes into context
whole, with no embedding call and no truncation found in this path.**
`routers/retrieval.py:2896-2924`:

```python
loader = get_web_loader(urls, verify_ssl=..., requests_per_second=..., trust_env=..., loader_config=...)
docs = await loader.aload()
...
if config.BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL:
    return {
        'status': True, 'collection_name': None, 'filenames': urls, 'items': result_items,
        'docs': [{'content': doc.page_content, 'metadata': doc.metadata} for doc in docs],
        'loaded_count': len(docs),
    }
```

The full `doc.page_content` from the loader is passed through unmodified — no slicing, no
character cap, no call into an embedding model anywhere in this branch (the `else` branch,
`:2925-2946`, is the one that calls `save_docs_to_vector_db`, skipped entirely here).

**Traced 2026-09-09 (slice-1 ticket 65), replacing the "not traced further" that stood
here.** The bypass return's `docs` become a `web_search` item on `form_data['files']`
(`utils/middleware.py`, `chat_web_search_handler`'s `elif results.get('docs')` branch:
`docs`, `name`, `urls`, `queries`, and no `context: 'full'` mark); `process_chat_payload`
moves the files list into the request's metadata and runs `chat_completion_files_handler`
**only when the selected model record's `file_context` capability is true**
(`file_context_enabled`, default true when the key is absent) — the gate GIDEON's General
record held closed through `v0.1.35`, so every fetched page was dropped after the
"Searched N sites" status, which the search handler emits before the gate. Inside the
handler, `retrieval/utils.py`'s `get_sources_from_items` takes the `elif item.get('docs')`
branch for the item — every doc's whole `content` and `metadata`, no query, no `k`,
`full_context` irrelevant — and `apply_source_context_to_messages` formats every source
under the RAG citation template into the last user message (`RAG_SYSTEM_CONTEXT` off by
default). No context-length trim exists on this path: the engine's window is the only
bound, and a nine-page turn is tens of thousands of tokens. Before building the sources the
handler calls the retrieval query task (`generate_queries(type='retrieval')`) unless every
files item is marked full-context, which the bypass item is not; with `ENABLE_QUERIES_CACHE`
off (GIDEON's render, ticket 16) nothing serves that call from the search handler's own
queries, so GIDEON renders `ENABLE_RETRIEVAL_QUERY_GENERATION=false` and the handler
swallows the feature-disabled exception (`docs/research/owui-model-record.md` §2.2's
corrected row). Read from the pinned image's own source inside the running container.

### 10. The confirmation dialog — when it fires

`docs/research/owui-preset-system-prompt.md` §4.2 already established the *gating*
(global-only flag, no per-model override key, `ENABLE_WEB_SEARCH_CONFIRMATION`/
`WEB_SEARCH_CONFIRMATION_CONTENT`) — not repeated here. The *firing* mechanics, read from
`src/lib/components/chat/Chat.svelte` at `v0.11.3`:

```js
// Chat.svelte:349-354
const handleWebSearchToggle = (enabled) => {
    if (enabled && $config?.features?.enable_web_search_confirmation && !webSearchConfirmed) {
        webSearchEnabled = false;
        pendingWebSearchPrompt = null;
        openWebSearchConfirm();
    }
};
```

```js
// Chat.svelte:3124-3130 (inside the send handler)
if ($config?.features?.enable_web_search_confirmation && webSearchActive && !webSearchConfirmed) {
    pendingWebSearchPrompt = userPrompt ?? '';
    openWebSearchConfirm();
    return;   // blocks the send
}
```

So the dialog fires at **either** of two moments: the instant the user flips the
composer's search toggle on (immediately reverting the toggle to off and opening the
dialog before anything is sent), or — if search was somehow already active without a
confirmation — at send time, which intercepts the message and queues it as
`pendingWebSearchPrompt`. `confirmWebSearch` (`Chat.svelte:4100-4109`) sets
`webSearchConfirmed = true` and, if a prompt was queued, submits it immediately. That flag
resets — i.e. the dialog fires again — whenever search is toggled back off
(`Chat.svelte:363-365`, `$: if (!webSearchActive) { resetWebSearchConfirmation(); }`) or a
new chat is started (`Chat.svelte:1994`, `initNewChat` calls
`resetWebSearchConfirmation()`). **So: once per chat while the toggle stays on**, not once
ever and not once per message.

### 11. Logging on the search path — the DEBUG-only claim holds on the happy path, but
### not on a failed request

Confirmed at `v0.11.3`, exactly as `.scratch/slice-1/issues/16-no-search-query-text.md`
claims, **for the success path**:

- `retrieval/web/searxng.py:63`: `log.debug('searching %s', query_url)` — DEBUG, and
  `query_url` here is the bare endpoint (the `<query>`-stripped base URL, §8), not the
  full request including `q=`.
- `routers/retrieval.py:2817`: `logging.debug('trying to web search with %s', (config.WEB_SEARCH_ENGINE, form_data.queries))` — **DEBUG, and this one does carry the raw query text** (`form_data.queries`), consistent with the ticket's claim that this class of line only fires at DEBUG.
- `routers/retrieval.py:2858`: `log.debug('urls: %s', urls)` — DEBUG, carries fetched URLs.

**A real gap: on any failed SearXNG HTTP call, the full request URL — including the `q=`
query text — reaches an ERROR-level log line, not DEBUG.** `search_searxng` calls
`response.raise_for_status()` (`searxng.py:71`) on the aiohttp response; a non-2xx status
raises `aiohttp.ClientResponseError`, whose `__str__` (`aiohttp/client_exceptions.py:89-90`,
read from `aio-libs/aiohttp`, `master`) is:

```python
def __str__(self) -> str:
    return f"{self.status}, message={self.message!r}, url={str(self.request_info.real_url)!r}"
```

`request_info.real_url` is the fully-built request URL, i.e. it **includes every query
parameter from §8's `params` dict — `q=<the user's search query text>` among them.** This
exception propagates out of `search_searxng` → `search_web` → `process_web_search`'s outer
`except Exception as e:` block, `routers/retrieval.py:2858-2863`:

```python
except Exception as e:
    log.exception('Web search failed')
    raise HTTPException(...)
```

`logging.exception(...)` logs at **ERROR** severity with the full traceback attached — the
traceback's last line is the exception's `str()`, which per the code just quoted contains
the complete request URL with the query text in it. `ERROR` is above every log-level floor
GIDEON is expected to run at (INFO or WARNING; `env.py:107-121`,
`GLOBAL_LOG_LEVEL` gates via `logging.basicConfig`, and ERROR always exceeds either), so
**this line is not suppressed by choosing INFO over DEBUG** — it is a genuinely different
severity class from the DEBUG lines ticket 16 already accounts for. A second, identical
exception handler exists one function down for the page-loading step
(`routers/retrieval.py:2960`, `log.exception('Web search content loading failed')`) —
whatever exception the loader raises there could likewise carry a fetched URL in its
`str()`, depending on the underlying library (not exhaustively traced for every possible
loader exception type). **This is the one finding in this note that most directly bears
on ticket 16's acceptance criteria** — the sentinel test described there (drive one search
end-to-end, grep every log for the sentinel) should include a run where the SearXNG
backend is made to fail (e.g. stopped, or returning 500) to exercise this exact path,
since the happy-path sentinel test alone would not catch it.

*What GIDEON does with this*: not decided here; the fact is that this ERROR-level path
exists in the shipped v0.11.3 source and is reachable any time SearXNG itself returns a
non-2xx response to Open WebUI's query (SearXNG being unreachable, mid-restart, or
returning an engine error page some other way).

---

## Summary of items flagged uncertain (repeated from inline call-outs, for scanning)

1. Part A §5(d): whether `curl_cffi`'s `trust_env` honours `NO_PROXY` identically to
   `requests`/`httpx` — not verified past the session docstring's own wording.
2. Part A §5(f): the exact precedence between a SearXNG preference cookie and a bare query
   parameter for the same field on one request — not traced in `searx/preferences.py`.
3. Part B §8: where (if anywhere) `WEB_FETCH_MAX_CONTENT_LENGTH` is actually enforced in
   the `safe_web` loader path — no call site found in `retrieval/web/utils.py`.
4. Part B §9: whether prompt assembly downstream of `process_web_search` (in
   `utils/middleware.py`) imposes its own truncation on fetched page content before it
   reaches the generator — out of this note's traced call path.
5. Part B §11: whether the page-loader's own exception handler
   (`routers/retrieval.py:2960`) can carry a URL for every loader-exception type it might
   catch, or only some — not exhaustively traced.

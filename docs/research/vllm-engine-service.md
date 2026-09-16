---
verified_against:
  - pin: images.vllm-openai
    version: v0.27.1
---
# vLLM v0.27.1 engine service — verified facts

Scope: GIDEON slice-1 ticket 04 (the engine service). Every claim below is
checked against the **tagged source** at `vllm-project/vllm` tag `v0.27.1`
(commit `6e448d0ea9bf3d88d898b65449ca6dc2aec170ac`), the docs shipped in that
tag, and the Docker Hub API. File paths are relative to the repo root at that
tag unless noted. Where v0.27.1 differs from a reader's memory of older vLLM
(pre-refactor module layout), that's called out — this version has moved most
OpenAI-server code from `vllm/entrypoints/openai/` into
`vllm/entrypoints/serve/`.

## 1. API key from the environment

`VLLM_API_KEY` is a real, first-class env var:

```python
# vllm/envs.py:28
VLLM_API_KEY: str | None = None
# vllm/envs.py:792
"VLLM_API_KEY": lambda: os.environ.get("VLLM_API_KEY", None),
```

It is consumed in `vllm/entrypoints/openai/api_server.py` when the app is
built:

```python
# vllm/entrypoints/openai/api_server.py:309-313
# Ensure --api-key option from CLI takes precedence over VLLM_API_KEY
if tokens := [key for key in (args.api_key or [envs.VLLM_API_KEY]) if key]:
    from vllm.entrypoints.serve.utils.server_utils import AuthenticationMiddleware
    app.add_middleware(AuthenticationMiddleware, tokens=tokens)
```

So `VLLM_API_KEY` and `--api-key` are equivalent as a *source* of the key —
CLI wins if both are given, and if neither is given no `AuthenticationMiddleware`
is registered at all (the server is open).

The middleware and its path gate live in
`vllm/entrypoints/serve/utils/server_utils.py` (the module `api_server.py`
imports from is `vllm.entrypoints.serve.utils.server_utils`, **not** the
`cli_args.py` file named in the question — `cli_args.py` only defines the
`--api-key` argparse field itself):

```python
# vllm/entrypoints/serve/utils/server_utils.py:42
GUARDED_PREFIX = ("/v1", "/v2", "/inference", "/cohere")

class AuthenticationMiddleware:
    """
    Pure ASGI middleware that authenticates each request by checking
    if the Authorization Bearer token exists and equals anyof "{api_key}".

    Notes
    -----
    There are two cases in which authentication is skipped:
        1. The HTTP method is OPTIONS.
        2. The request path doesn't start with GUARDED_PREFIX (e.g. /health).
    """
    ...
    def __call__(self, scope, receive, send):
        if (scope["type"] not in ("http", "websocket")
            or scope.get("method") == "OPTIONS"):
            return self.app(scope, receive, send)
        root_path = scope.get("root_path", "")
        url_path = scope["path"].removeprefix(root_path)
        headers = Headers(scope=scope)
        if url_path.startswith(GUARDED_PREFIX) and not self.verify_token(headers):
            response = JSONResponse(content={"error": "Unauthorized"}, status_code=401)
            return response(scope, receive, send)
        return self.app(scope, receive, send)
```

`GUARDED_PREFIX` is an allowlist of *protected* prefixes
(`/v1`, `/v2`, `/inference`, `/cohere`), tested with `startswith`. Any path
that doesn't start with one of those four — `/health`, `/metrics`, `/ping`,
`/tokenize`, `/version`, etc. — bypasses the middleware entirely, by design,
independent of whether an API key is configured. **`GET /health` is
unauthenticated even when `VLLM_API_KEY`/`--api-key` is set.** This is
explicitly called out in the docstring's own example.

## 2. Readiness semantics of `/health`

`GET /health` is defined in `vllm/entrypoints/serve/instrumentator/health.py`
(not in `api_server.py` — routers were split out in this version):

```python
# vllm/entrypoints/serve/instrumentator/health.py
@router.get("/health", response_class=Response)
async def health(raw_request: Request) -> Response:
    """Health check."""
    client = engine_client(raw_request)
    if client is None:
        # Render-only servers have no engine; they are always healthy.
        return Response(status_code=200)
    try:
        await client.check_health()
        return Response(status_code=200)
    except EngineDeadError:
        return Response(status_code=503)
```

The important question is *when the FastAPI app (and therefore this route)
starts accepting connections at all*. In `run_server_worker`
(`vllm/entrypoints/openai/api_server.py:767-786`):

```python
async def run_server_worker(listen_address, sock, args, client_config=None, **uvicorn_kwargs):
    ...
    async with build_async_engine_client(args, client_config=client_config) as engine_client:
        shutdown_task = await build_and_serve(engine_client, listen_address, sock, args, **uvicorn_kwargs)
    ...
```

`build_async_engine_client` (line 108) is an `asynccontextmanager` whose body
`await`s `AsyncLLM.from_vllm_config(...)` to completion before `yield`ing the
client (`api_server.py:161-176`). `build_and_serve` (line 658), which
constructs the FastAPI app and calls `serve_http(...)` (uvicorn), is only
called **inside** that `async with` block — i.e. after the engine client is
fully constructed. `AsyncLLM.__init__` (`vllm/v1/engine/async_llm.py:149`)
in turn calls `EngineCoreClient.make_async_mp_client(...)`, which spawns the
`EngineCore` in a background process and blocks (with a timeout controlled by
`VLLM_ENGINE_READY_TIMEOUT_S`, `vllm/v1/engine/core_client.py:651-669`) until
that process sends a `"status": "READY"` handshake
(`vllm/v1/engine/core.py:1221`, sent by `EngineCoreProc`, a subclass of
`EngineCore`). `EngineCore.__init__` calls
`self._initialize_kv_caches(vllm_config)` (`core.py:143`) before that point,
and the worker-side `compile_or_warm_up_model` step it triggers performs
CUDA-graph capture (`self.model_runner.capture_model()`,
`vllm/v1/worker/gpu_worker.py:~715`, logged as `"Compile and warming up model
for size %d"`) — all before the READY handshake is sent.

So: **weight loading, KV-cache memory profiling, and CUDA-graph capture all
happen before the HTTP app object even exists**, let alone before uvicorn
starts accepting requests. There is no separate/earlier readiness endpoint,
and no code path returns 200 from `/health` before the model is ready — the
route doesn't exist until the app is built, which is after the engine is up.

One caveat worth flagging for a container healthcheck: `setup_server` (called
by `run_server` before the engine is built) **binds** the TCP socket early —
explicitly to avoid a Ray race (`api_server.py:635-637`, comment: "workaround
to make sure that we bind the port before the engine is set up... avoids race
conditions with ray") — via `create_server_socket`
(`api_server.py:578-593`), which only calls `sock.bind()`, **not**
`sock.listen()`. `listen()`/`accept()` only happen once uvicorn takes over
the socket inside `serve_http`, i.e. after the engine is ready. In practice
this means even a bare TCP connect (not just an HTTP `GET /health`) will be
refused until the model is loaded — there is no window where the port is
open but unready.

## 3. The image (`docker/Dockerfile`, `vllm-openai` target)

- **Base image / CUDA version**: `ARG CUDA_VERSION=13.0.3`;
  `FINAL_BASE_IMAGE=nvidia/cuda:${CUDA_VERSION}-base-ubuntu${UBUNTU_VERSION}`
  with `UBUNTU_VERSION=22.04` (`docker/Dockerfile:25-46`) — i.e.
  `nvidia/cuda:13.0.3-base-ubuntu22.04`. The build stage
  (`BUILD_BASE_IMAGE`) uses the `-devel` variant of the same CUDA version.
- **ENTRYPOINT / CMD**: the `vllm-openai` stage is:
  ```dockerfile
  FROM vllm-openai-base AS vllm-openai
  # USER vllm   <- commented out, opt-in only
  ENTRYPOINT ["vllm", "serve"]
  ```
  (`docker/Dockerfile:1088-1096`). There is **no `CMD`** anywhere in the
  file for this target — arguments given to `docker run` are appended after
  `vllm serve`.
- **USER / root**: the default `vllm-openai` target has no `USER`
  instruction (the line is present but commented out with a pointer to
  `docs/deployment/docker.md`), so **the process runs as root** by default.
  A `vllm` user (UID 2000, GID 0) is created earlier in the `vllm-base`
  stage (`docker/Dockerfile:758-771`) and is only switched to in the
  separate, opt-in `vllm-openai-nonroot` build target
  (`docker/Dockerfile:1113-1116`, `USER vllm`).
- **curl**: installed in the `vllm-base` stage
  (`docker/Dockerfile:673-678`, `apt-get install -y --no-install-recommends
  ... curl ...`), and `vllm-openai-base`/`vllm-openai` are built `FROM
  vllm-base` (not copied artifacts from a slimmer stage), so `curl` is
  present in the final `vllm-openai` image — usable for a container
  `HEALTHCHECK`.
- **`vllm` CLI on PATH**: the `ENTRYPOINT ["vllm", "serve"]` directive itself
  is proof the `vllm` console-script resolves via `PATH` inside the image
  (Docker resolves exec-form entrypoints through `PATH`); it's installed as
  vLLM's own package console script during the `build`/`vllm-base` stages'
  `pip`/`uv pip install` of the wheel.

## 4. Flags

All confirmed present at v0.27.1, in `vllm/entrypoints/openai/cli_args.py`
(`ServeArgs`/`FrontendArgs`-style dataclass) for server-only flags, or in
`vllm/engine/arg_utils.py` (`EngineArgs`/`AsyncEngineArgs`, whose fields
default to the underlying `vllm/config/*.py` dataclass field, per vLLM's
`@config`-decorator argparse-from-dataclass mechanism — there are no
`parser.add_argument(...)` calls to grep for most of these; the CLI field's
default *is* the config dataclass's default):

| Flag | Exists | Default | Source |
|---|---|---|---|
| `--generation-config` | yes | **`"auto"`** | `vllm/config/model.py:312` (`generation_config: str = "auto"`), wired at `arg_utils.py:688` |
| `--reasoning-parser qwen3` | yes | `"qwen3"` is a registered parser name | `vllm/reasoning/__init__.py`: `_REASONING_PARSERS_TO_REGISTER["qwen3"] = ("qwen3_engine_reasoning_parser", "Qwen3ParserReasoningAdapter")`, lazily registered via `ReasoningParserManager.register_lazy_module` |
| `--revision` | yes | `None` | `vllm/config/model.py:197` (`revision: str | None = None`) |
| `--served-model-name` | yes | `None` (falls back to the model arg) | `arg_utils.py:428`, `= ModelConfig.served_model_name` |
| `--kv-cache-dtype fp8` | yes | flag default is `"auto"`; `"fp8"` is a valid literal | `vllm/config/cache.py:19-37` (`CacheDType = Literal["auto", "float16", "bfloat16", "fp8", ...]`), default at `cache.py:76` (`cache_dtype: CacheDType = "auto"`) |
| `--scheduling-policy priority` | yes | flag default is `"fcfs"`; `"priority"` is a valid literal | `vllm/config/scheduler.py:22` (`SchedulerPolicy = Literal["fcfs", "priority"]`), default at `scheduler.py:99` (`policy: SchedulerPolicy = "fcfs"`) |
| `--long-prefill-token-threshold` | yes | `0` (disabled/unlimited) | `vllm/config/scheduler.py:70` (`long_prefill_token_threshold: int = Field(default=0, ge=0)`) |
| `--enable-chunked-prefill` | yes | CLI default is `None` (auto); the `SchedulerConfig` dataclass field itself defaults `True` | `arg_utils.py:619` (`enable_chunked_prefill: bool | None = None`) vs. `vllm/config/scheduler.py:74` (`enable_chunked_prefill: bool = True`) — `EngineArgs` resolves the `None` to a computed default later in `arg_utils.py` (`_set_default_...`, ~line 2608) |
| `--max-num-seqs` | yes | CLI default `None`; resolved at engine-config build time from a per-hardware/usage-context table (not a fixed constant) | `arg_utils.py:536` (`max_num_seqs: int | None = None`), resolution in `_set_default_max_num_seqs_and_batched_tokens_args` (`arg_utils.py:2712+`) |
| `--max-num-batched-tokens` | yes | CLI default `None`; same runtime-resolved behavior as above | `arg_utils.py:533` |
| `--gpu-memory-utilization` | yes | **`0.92`** | `vllm/config/cache.py:68` (`gpu_memory_utilization: float = Field(default=0.92, gt=0, le=1)`) |
| `--max-model-len` | yes | `None` (derived from the model's own config at load time) | `vllm/config/model.py:208` (`max_model_len: int = Field(default=None, ...)`) |
| `--api-key` | yes | `None`; type is `list[str] | None`, i.e. **multiple keys are natively supported** | `cli_args.py:283-285` |
| `--host` | yes | `None` (binds all interfaces — passed through as `sock_addr = (args.host or "", args.port)`) | `cli_args.py:246` |
| `--port` | yes | **`8000`** | `cli_args.py:248` |

Note on `--gpu-memory-utilization`: this is **0.92** at v0.27.1, not the
`0.9` some older vLLM docs/memory quote — worth double-checking against
whatever value GIDEON's compose/profile config assumes.

## 5. Offline hub cache on a read-only mount

**Yes** — with a pinned 40-hex commit `--revision`, resolution is both
network-free and *write*-free, verified at the `huggingface_hub` source
level (checked against the latest release, `v1.30.0`, since vLLM v0.27.1's
`requirements/common.txt` only pins `transformers >= 5.5.3` and does not
pin `huggingface_hub` directly — it's a transitive dependency of
`transformers`; the file-download/offline logic below is has been stable
across recent `huggingface_hub` releases, but this is a **secondary
inference on the exact version pin** — flagged as such):

- `HF_HOME` and the derived `HF_HUB_CACHE` (`$HF_HOME/hub`) are defined in
  `huggingface_hub/constants.py:158-178`. Model snapshots land at
  `$HF_HUB_CACHE/models--<org>--<repo>/snapshots/<commit>/...`, matching
  GIDEON's `models pull` layout.
- `HF_HUB_OFFLINE` is read in `constants.py:192`
  (`HF_HUB_OFFLINE = _is_true(os.environ.get("HF_HUB_OFFLINE") or
  os.environ.get("TRANSFORMERS_OFFLINE"))`), and is enforced centrally: any
  outbound HTTP call raises `OfflineModeIsEnabled`
  (`huggingface_hub/utils/_http.py:260-261`, "Cannot reach ...: offline mode
  is enabled") rather than attempting a socket connect.
- With a **commit-hash revision** (matches `REGEX_COMMIT_HASH`), both
  `hf_hub_download`'s cache-dir path and `snapshot_download` **skip the
  network call outright**, independent of `HF_HUB_OFFLINE`:
  ```python
  # huggingface_hub/file_download.py, _hf_hub_download_to_cache_dir
  # if user provides a commit_hash and they already have the file on disk, shortcut everything.
  if REGEX_COMMIT_HASH.match(revision):
      pointer_path = _get_pointer_path(storage_folder, revision, relative_filename)
      if os.path.exists(pointer_path):
          ...
          if not force_download:
              return pointer_path
  ```
  This path is pure `os.path.exists`/`os.path.getsize` — **no lock file is
  created, no directory is created, nothing is written** when the file is
  already present in the snapshot. The equivalent short-circuit for
  `snapshot_download`'s revision-to-commit resolution is
  `huggingface_hub/_snapshot_download.py:257-259`
  (`elif REGEX_COMMIT_HASH.match(revision): commit_hash = revision`).
- If a file that should be present under the pinned commit is *missing*
  (e.g. a lock/incomplete pull), the offline HEAD call fails immediately
  (`OfflineModeIsEnabled`), and — because there's no local pointer file
  either — `hf_hub_download` raises `LocalEntryNotFoundError`
  (`file_download.py:1905-1908`) **without touching disk on the way there
  either**; it does not fall through to the `.locks/` / temp-file / rename
  write path (that path is only reached once a download is about to start).
  So: cache hit → read-only, zero writes. Cache miss under
  `HF_HUB_OFFLINE=1` → hard failure with no writes, not a silent stall or a
  permission error from a read-only mount.
- Caveat not exercised by the above: if a model requires
  `trust_remote_code=True`, `transformers` caches the fetched Python module
  under `$HF_HOME/modules/transformers_modules/...` — that path *would*
  need to be writable if that feature is used. Not relevant for a model
  whose code is upstreamed into `transformers`/vLLM's own model
  implementations, but worth naming if GIDEON's profile ever needs
  `trust_remote_code`.
- No login/token write occurs at startup — `huggingface_hub` only writes
  `$HF_HOME/token` on an explicit `login()` call, which neither vLLM nor a
  plain `vllm serve` invocation makes.

**vLLM's own caches**, none of which live under `HF_HOME` and all of which
need to be writable somewhere (default: the container's `$HOME`, i.e. root's
home since the image runs as root by default — see §3):

| Cache | Env var | Default path | Source |
|---|---|---|---|
| vLLM general cache root | `VLLM_CACHE_ROOT` | `~/.cache/vllm` | `vllm/envs.py:34` |
| torch-compile / inductor cache | (derived from `VLLM_CACHE_ROOT`) | `$VLLM_CACHE_ROOT/torch_compile_cache/<hash>/` | `vllm/compilation/backends.py:1058-1070` (`CompilationConfig.cache_dir` defaults to `""`, filled in here) |
| XLA cache | `VLLM_XLA_CACHE_PATH` | `$VLLM_CACHE_ROOT/xla_cache` | `envs.py:58` |
| Assets cache (multimodal fetches) | `VLLM_ASSETS_CACHE` | `$VLLM_CACHE_ROOT/assets` | `envs.py:68` |
| Triton's own kernel cache | `TRITON_CACHE_DIR` (Triton's own var, not vLLM's) | `$HOME/.cache/...` if unset | Not in `envs.py` — vLLM doesn't set it; the Dockerfile's own comment confirms the default resolution: `docker/Dockerfile:1110-1112` ("All cache/config envs (HF_HOME, VLLM_CACHE_ROOT, TRITON_CACHE_DIR, ...) remain unset so their library defaults resolve to $HOME/.cache/...") |

The Dockerfile comment at `docker/Dockerfile:1110-1112` is itself useful
here — it's vLLM's own maintainers naming exactly this set of cache env vars
as "must be writable" for a non-default-UID container, which is directly
relevant to a read-only `/data/models` mount plan.

## 6. Usage statistics off

`vllm/usage/usage_lib.py`:

```python
# usage_lib.py:29-31
_config_home = envs.VLLM_CONFIG_ROOT
_USAGE_STATS_JSON_PATH = os.path.join(_config_home, "usage_stats.json")
_USAGE_STATS_DO_NOT_TRACK_PATH = os.path.join(_config_home, "do_not_track")

# usage_lib.py:51-68
def is_usage_stats_enabled():
    """Determine whether or not we can send usage stats to the server.
    The logic is as follows:
    - By default, it should be enabled.
    - Three environment variables can disable it:
        - VLLM_DO_NOT_TRACK=1
        - DO_NOT_TRACK=1
        - VLLM_NO_USAGE_STATS=1
    - A file in the home directory can disable it if it exists:
        - $HOME/.config/vllm/do_not_track
    """
    ...
    do_not_track = envs.VLLM_DO_NOT_TRACK
    no_usage_stats = envs.VLLM_NO_USAGE_STATS
    do_not_track_file = os.path.exists(_USAGE_STATS_DO_NOT_TRACK_PATH)
    _USAGE_STATS_ENABLED = not (do_not_track or no_usage_stats or do_not_track_file)
```

`VLLM_DO_NOT_TRACK` also honors the generic `DO_NOT_TRACK` env var
(`vllm/envs.py:806-810`: `os.environ.get("VLLM_DO_NOT_TRACK", None) or
os.environ.get("DO_NOT_TRACK", None)`). Any one of `VLLM_NO_USAGE_STATS=1`,
`VLLM_DO_NOT_TRACK=1`, or `DO_NOT_TRACK=1` is sufficient to disable
reporting; no need to set all three.

## 7. Shared memory in Docker

The docs (`docs/getting_started/installation/gpu.cuda.inc.md:260-273`, the
block included into `docs/deployment/docker.md` via `--8<--`) show the
standard quick-start command with `--ipc=host` unconditionally:

```bash
docker run --runtime nvidia --gpus all \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    --env "HF_TOKEN=$HF_TOKEN" \
    -p 8000:8000 \
    --ipc=host \
    vllm/vllm-openai:latest \
    --model Qwen/Qwen3-0.6B
```

The docs text itself doesn't caveat this as TP-only. Source inspection shows
why it's there and when it actually matters:

- vLLM's own shared-memory IPC — `ShmRingBuffer`/`MessageQueue` in
  `vllm/distributed/device_communicators/shm_broadcast.py` (`SHM_PATH =
  "/dev/shm"`, `class ShmRingBuffer` at line 251, default sizing in
  `MessageQueue.__init__` at line 466-474: `max_chunk_bytes: int = 1024 *
  1024 * 24` (24 MiB) `, max_chunks: int = 10` → roughly 240 MiB+ of backing
  shared memory when instantiated with defaults) — is **only instantiated
  when `world_size > 1`**:
  ```python
  # vllm/distributed/parallel_state.py:515-519
  from vllm.distributed.device_communicators.shm_broadcast import MessageQueue
  self.mq_broadcaster: MessageQueue | None = None
  if use_message_queue_broadcaster and self.world_size > 1:
      self.mq_broadcaster = MessageQueue.create_from_process_group(...)
  ```
  For a **single-GPU deployment with no tensor/pipeline/data parallelism**
  (`world_size == 1`), this object is never constructed, so this particular
  shared-memory mechanism uses **zero** bytes of `/dev/shm`.
- NCCL is the other consumer of `/dev/shm` (for CUDA IPC handles between
  ranks on the same node during multi-process/multi-GPU collectives).
  `docs/usage/troubleshooting.md:315` connects an unmounted/undersized
  `/dev/shm` specifically to **NCCL communicator initialization** failures
  — again a multi-rank concern.
- Multimodal tensor IPC has an opt-in shared-memory transport
  (`mm_tensor_ipc: MMTensorIPC` config), but it **defaults to
  `"direct_rpc"`, not `"torch_shm"`**
  (`vllm/config/multimodal.py:220`), so it doesn't consume `/dev/shm` by
  default either.

**Conclusion for GIDEON's case** (single GPU, no TP): the mechanisms that
actually drive vLLM's `--ipc=host`/`--shm-size` recommendation are
multi-process/multi-GPU-specific and are not instantiated at `world_size ==
1`. Docker's default 64 MB `/dev/shm` should be sufficient for this
deployment shape. This is our own inference from the `world_size > 1` guard
in `parallel_state.py`, not a statement the docs make explicitly — the docs'
example command applies `--ipc=host` unconditionally as a general default,
so if GIDEON drops it, that's a deliberate, source-justified deviation from
the documented quick-start, worth a one-line comment in the compose file.

## 8. Start-up log lines

KV cache size and concurrency (`vllm/v1/core/kv_cache_utils.py:2227-2239`):

```python
# GPU KV cache size in tokens = max_concurrency * max_model_len:
# the total tokens of context the pool can hold at peak
# utilization. Sourcing this from the concurrency calculation
# handles hybrid layouts correctly.
num_tokens, max_concurrency = get_kv_cache_capacity(vllm_config, kv_cache_config)
logger.info_once("GPU KV cache size: %s tokens", f"{num_tokens:,}")
logger.info_once(
    "Maximum concurrency for %s tokens per request: %.2fx",
    f"{max_model_len:,}", max_concurrency,
)
```

Rendered example: `GPU KV cache size: 123,456 tokens` and `Maximum
concurrency for 4,096 tokens per request: 12.34x`.

A related, earlier line from GPU memory profiling
(`vllm/v1/worker/gpu_worker.py:563-565`):
`logger.info_once("Available KV cache memory: %s GiB", ...)`.

**Hybrid Mamba/GDN models (e.g. `model_type: qwen3_5`, Qwen3-Next-style
Gated-DeltaNet)**: the comment at `kv_cache_utils.py:2227-2230` explicitly
states the same "GPU KV cache size"/"Maximum concurrency" lines above are
sourced from a calculation that "handles hybrid layouts correctly" — i.e.
**there is no separate log line for the mamba/GDN state cache size**; it's
folded into the same unified KV-cache accounting and the same two log lines.
The only mamba-specific *log line* found at v0.27.1 is a Triton kernel
warm-up notice, unrelated to cache sizing:
```python
# vllm/model_executor/layers/mamba/mamba_mixer2.py:596
logger.info_once("Warming up Mamba2 SSD Triton kernels...")
```
and a warning that only fires if the hybrid KV-cache manager is explicitly
disabled via `--disable-hybrid-kv-cache-manager`
(`kv_cache_utils.py:1583-1588`, `"Hybrid KV cache manager is disabled for
this hybrid model, ..."`) — not part of a normal startup. **Could not find**
a distinct "mamba/GDN state: N bytes" style line in this version; treat the
two lines above as the only startup signal for both attention and
mamba/GDN cache sizing on a hybrid model.

## 9. The tag on Docker Hub

Confirmed via the Docker Hub v2 API
(`https://hub.docker.com/v2/repositories/vllm/vllm-openai/tags/v0.27.1`):
the tag exists, `tag_status: "active"`, pushed 2026-08-11. It's a multi-arch
manifest list covering:

- `linux/amd64` — compressed size **≈ 9.11 GB** (9,110,652,559 bytes)
- `linux/arm64` — compressed size **≈ 10.53 GB** (10,533,135,621 bytes)

(Digests were incidentally visible in the API response but are not recorded
here as the pin source — per the task, digest resolution for the lockfile
is left to `skopeo` on the box.)

## 10. `--api-key` and process argv

- `args.api_key` is typed `list[str] | None` in
  `cli_args.py:283-285` ("If provided, the server will require **one of
  these keys**..."), and `AuthenticationMiddleware.__init__` takes `tokens:
  list[str]` and accepts a match against *any* of them
  (`server_utils.py:57-76`, `secrets.compare_digest` per token, ORed) — so
  **v0.27.1 natively supports multiple simultaneously-valid keys** via
  repeated `--api-key` values (nargs-style list argument, consistent with
  the `list[str]` field type and vLLM's `@config`-driven argparse
  generation).
- **No file-based form exists.** Grepped `cli_args.py` and
  `arg_utils.py` for `api_key_file`/`api-key-file`/`APIKeyFile`: no matches.
  The only two sources for the key are the CLI flag and the `VLLM_API_KEY`
  env var (§1).
- Caveat for GIDEON's planned approach (reading a secret file into
  `VLLM_API_KEY` inside the entrypoint rather than passing `--api-key`):
  this is exactly what `envs.VLLM_API_KEY` is for and avoids the argv
  exposure problem entirely (`--api-key <value>` would be visible via
  `docker top`/`/proc/<pid>/cmdline`/`ps`, since `vllm serve` is the
  container's own PID 1 argv, not just visible to other containers). One
  thing to double check operationally: env vars set on a process are
  visible to anything that can read `/proc/<pid>/environ` for that PID
  (typically root or the same UID inside the container/namespace) — same
  general exposure class as most container secret-injection patterns, but
  strictly less exposed than argv (which many tools log or display by
  default, e.g. `docker inspect`, shell history if constructed manually,
  process listings visible to unprivileged users). No vLLM-specific
  redaction of `VLLM_API_KEY` in logs was found, but since it's consumed
  directly via `os.environ.get` in `envs.py` and never echoed by the
  `log_non_default_args`/argument-logging path (which only logs `args.*`,
  i.e. CLI-sourced values, not env-sourced ones), it shouldn't appear in
  vLLM's own startup logs either way.

## 11. Request and access logging defaults

**Default: no user text (prompt or output) is logged, at any level, unless
two separate flags are both explicitly turned on.**

`vllm/engine/arg_utils.py`:

```python
# arg_utils.py:2808
class AsyncEngineArgs(EngineArgs):
    enable_log_requests: bool = False
```
```python
# arg_utils.py:2821-2828 (add_cli_args)
parser.add_argument(
    "--enable-log-requests",
    action=argparse.BooleanOptionalAction,
    default=AsyncEngineArgs.enable_log_requests,
    help="Enable logging request information, dependent on log level:\n"
    "- INFO: Request ID, parameters and LoRA request.\n"
    "- DEBUG: Prompt inputs (e.g: text, token IDs).\n"
    "You can set the minimum log level via `VLLM_LOGGING_LEVEL`.",
)
```

So `--enable-log-requests` (there is no separate `--disable-log-requests` at
v0.27.1 — `BooleanOptionalAction` gives `--enable-log-requests`/
`--no-enable-log-requests`) defaults to **off**. Even when turned on, prompt
text/token IDs only print at **DEBUG**; at INFO only request id, sampling
params, and LoRA request are logged.

A second, separate flag governs *output* logging, and requires the first:

```python
# cli_args.py:143-145
enable_log_outputs: bool = False
"""If set to True, log model outputs (generations).
Requires `--enable-log-requests`. As with `--enable-log-requests`,
information is only logged at INFO level at maximum."""
# cli_args.py:416-417 (validate_parsed_serve_args)
if args.enable_log_outputs and not args.enable_log_requests:
    raise TypeError("Error: --enable-log-outputs requires --enable-log-requests")
```

`--max-log-len` (`cli_args.py:129-131`, `int | None = None`) governs
truncation of what *would* be logged if request/output logging is enabled:
"Max number of prompt characters or prompt ID numbers being printed in log.
The default of None means unlimited." — it does not itself enable any
logging.

The actual logging code — v0.27.1's equivalent of the old
`serving_engine.py`'s `_log_inputs` is now a standalone class,
`vllm/entrypoints/serve/utils/request_logger.py` (**no**
`vllm/entrypoints/logger.py` file exists at this tag — it was moved/renamed
as part of the `entrypoints/openai` → `entrypoints/serve` split):

```python
class RequestLogger:
    def log_inputs(self, request_id, prompt, prompt_token_ids, prompt_embeds, params, lora_request):
        if logger.isEnabledFor(logging.DEBUG):
            ...
            logger.debug("Request %s details: prompt: %r, prompt_token_ids: %s, prompt_embeds shape: %s.", ...)
        logger.info("Received request %s: params: %s, lora_request: %s.", request_id, params, lora_request)

    def log_outputs(self, request_id, outputs, output_token_ids, finish_reason=None, is_streaming=False, delta=False):
        ...
        logger.info(
            "Generated response %s%s: output: %r, output_token_ids: %s, finish_reason: %s",
            request_id, stream_info, outputs, output_token_ids, finish_reason,
        )
```

Two things worth flagging precisely for GIDEON's §19.4 rule:

- `log_inputs`'s prompt-bearing branch is gated on `logging.DEBUG`, so
  leaving the log level at `INFO` (vLLM's default; see
  `VLLM_LOGGING_LEVEL` in `envs.py`) keeps prompt text out even if
  `--enable-log-requests` is passed.
- `log_outputs`, by contrast, logs the **full generated text at INFO**
  (`logger.info(..., outputs, ...)`) with **no DEBUG gate** — if it's ever
  called, model output text goes to the log at the default level. It is
  only reachable when **both** `--enable-log-requests` **and**
  `--enable-log-outputs` are passed (enforced by the `validate_parsed_serve_args`
  check above); GIDEON's render process must never pass `--enable-log-outputs`
  (nor `--enable-log-requests`, to be safe against future refactors of the
  INFO/DEBUG split) to satisfy §19.4.

**Simplest compliant configuration**: don't pass `--enable-log-requests` or
`--enable-log-outputs`, and don't set `VLLM_LOGGING_LEVEL=DEBUG`. Under that
configuration, no request or response logging call in
`request_logger.py` ever executes (the `RequestLogger` object may still be
constructed and its constructor's own log-level warnings fire, but
`log_inputs`/`log_outputs` are gated by the flags checked at the call site
in the serving classes, and `log_inputs`'s prompt branch is additionally
DEBUG-gated).

**Uvicorn access log** — `get_uvicorn_log_config`
(`vllm/entrypoints/serve/utils/server_utils.py:140-165`) returns `None`
(i.e. "use uvicorn's own defaults") unless `--log-config-file` or
`--disable-access-log-for-endpoints` is set. vLLM's own
`create_uvicorn_log_config` helper (used only when
`--disable-access-log-for-endpoints` is set, e.g. to silence `/health`
polling) documents uvicorn's default access-log record shape directly in
its docstring and reproduces it in the formatter it installs
(`vllm/logging_utils/access_log_filter.py`):

```python
# Uvicorn access log format:
#     '%s - "%s %s HTTP/%s" %d'
#     (client_addr, method, path, http_version, status_code)
# Example:
#     127.0.0.1:12345 - "GET /health HTTP/1.1" 200
...
"access": {
    "()": "uvicorn.logging.AccessFormatter",
    "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
},
```

So the access log — with or without vLLM's filter config — carries only
client address, HTTP method, path, protocol version, and status code. No
headers, no query-string values beyond what's embedded in the logged path,
no request/response bodies. (Caveat: a request path containing user data,
e.g. a path-embedded parameter, would show up here — not a concern for the
OpenAI-compatible endpoints, which take all payload via POST body.) Setting
`--disable-uvicorn-access-log` (default `False`) turns this off entirely if
even method/path/status is undesired; setting
`--disable-access-log-for-endpoints=/health,/metrics` (the mechanism the
`access_log_filter.py` module exists for) suppresses log noise from
health/metrics polling specifically, without disabling access logging for
the real endpoints.

One more relevant discovery, not directly asked for but bearing on the "no
user text in any log" rule: vLLM's own **crash-dump path** for internal
engine exceptions (`vllm/logging_utils/dump_input.py`,
`dump_engine_exception`) is deliberately designed to avoid leaking prompt
content. `prepare_object_to_dump` prefers an object's `anon_repr()` method
over `__repr__`/`__dict__` when present, and the request objects that flow
through the V1 scheduler (`vllm/v1/core/sched/output.py`,
`NewRequestData`/`CachedRequestData`) implement `anon_repr()` to log only
`prompt_token_ids_len`/`new_token_ids_lens` (integer counts) instead of the
actual token ID lists that their plain `__repr__` would include. This is a
vLLM-side mitigation already in place for the (INFO-level, unconditional)
crash-dump case — worth knowing about, not something GIDEON needs to build
around, since it fires only `contextlib.suppress(Exception)`-wrapped on an
actual internal engine crash and doesn't touch prompt text either way.

---

## Implications for the Compose service

- **Auth**: set `VLLM_API_KEY` from a mounted secret file in the
  entrypoint rather than passing `--api-key` — confirmed equivalent, and
  avoids the key appearing in argv/`docker top`/`ps`. Remember `GET
  /health` (and `/metrics`, `/ping`, `/version`, `/tokenizer_info`, etc. —
  anything not under `/v1`, `/v2`, `/inference`, `/cohere`) is
  unauthenticated by design; don't rely on the API key to gate health
  checks, and don't put anything sensitive in those unguarded routes.
- **Healthcheck**: an HTTP `GET /health` returning 200 is a trustworthy
  readiness signal — the model is fully loaded, KV cache is profiled, and
  CUDA graphs are captured by the time uvicorn can answer it at all (the
  socket isn't even `listen()`ing before then). A plain TCP check works
  too here, unusually, since the bind-without-listen window means even a
  raw connect fails until the app is actually serving — but prefer the
  HTTP check for a real 200/503 signal (503 on a dead engine post-startup).
  `curl` is present in the image for a Docker `HEALTHCHECK` instruction.
- **Root**: the stock `vllm-openai` image runs as root; if GIDEON wants
  non-root, use the `vllm-openai-nonroot` build target or `--user 2000:0`,
  and mount caches under `/home/vllm/...` instead of `/root/...` in that
  case (see §5's cache table for exactly which paths need to move).
- **Read-only `/data/models` mount**: safe, given GIDEON's `models pull`
  already fully populates the snapshot for the pinned commit before `vllm
  serve` runs — the commit-hash fast path in `huggingface_hub` never
  writes when the snapshot is already complete, and fails loudly (no
  silent stall, no permission error surfacing as something else) if a
  needed file is missing. Keep `VLLM_CACHE_ROOT` (and by extension the
  torch-compile/XLA/assets caches under it) and `TRITON_CACHE_DIR` on a
  writable path — none of them live under `HF_HOME`, so they don't
  conflict with the read-only weights mount, but they do need *some*
  writable location (default `$HOME/.cache/...`, i.e. `/root/.cache/...`
  since the container runs as root).
- **`--ipc=host`/`--shm-size`**: source-level analysis says this is not
  needed for GIDEON's single-GPU, no-TP shape — vLLM's own shm-backed
  `MessageQueue` only exists at `world_size > 1`, and multimodal shm IPC
  defaults off. The docs show `--ipc=host` unconditionally as a general
  default, so document the deviation with a one-line comment if it's
  dropped from the compose file.
- **Logging**: don't pass `--enable-log-requests` or `--enable-log-outputs`,
  and don't set `VLLM_LOGGING_LEVEL=DEBUG` — this keeps prompt text and
  model output text out of the logs entirely (§11). The uvicorn access log
  (on by default) only ever carries client address/method/path/protocol/
  status — safe to leave on, or silence `/health` noise specifically via
  `--disable-access-log-for-endpoints=/health`.
- **Flags worth pinning explicitly in the compose/profile config**: note
  the real defaults from §4 that differ from common assumptions —
  `--gpu-memory-utilization` defaults to **0.92** (not 0.9), and
  `--max-num-seqs`/`--max-num-batched-tokens` have no fixed default at all
  (computed per-hardware/usage-context at engine-config build time) —
  don't assume a specific number without setting the flag if the profile
  needs a guaranteed value.

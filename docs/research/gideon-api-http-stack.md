---
verified_against:
  - pin: images.vllm-openai
    version: v0.27.1
  - pin: images.open-webui
    version: v0.11.3
  - pin: images.gideon.build_args.STARLETTE_VERSION
    version: "1.6.0"
  - pin: images.gideon.build_args.UVICORN_VERSION
    version: "0.53.0"
  - pin: images.gideon.build_args.HTTPX_VERSION
    version: "0.28.1"
---
# `gideon-api`'s HTTP stack — Starlette + uvicorn + httpx, verified at today's tags

Scope: ticket 01 (`gideon-api` in the rendered stack), read against ticket 02 (a
pass-through completion) and ticket 06 (the service judges every completion).
Every claim below is read from the package's own tagged source or PyPI
metadata fetched live on 2026-09-19, except §5 (vLLM, already pinned at
`v0.27.1`, commit `6e448d0ea9bf3d88d898b65449ca6dc2aec170ac`, same clone the
existing vLLM notes use) and §6 (Open WebUI, already pinned at `v0.11.3`,
commit `2a960a59fe1dbbd35282f0556b3666d81102e781`, same commit
`owui-engine-connection.md`/`owui-stream-hook.md` resolve to). Starlette,
uvicorn, httpx, anyio, httpcore and h11 are not pins yet (ticket 01's own
open question: whether the HTTP server is a dependency at all); this note
does not repeat what the four companion notes already establish — each is
cited by path and section, not restated.

**New Python packages read at these tags** (git tag, PyPI `requires_python`,
Python 3.14 classifier present):

| Package | Version (tag) | `requires_python` | `Programming Language :: Python :: 3.14` classifier |
|---|---|---|---|
| starlette | 1.6.0 | `>=3.10` | yes |
| uvicorn | 0.53.0 | `>=3.10` | yes |
| httpx | 0.28.1 | `>=3.8` | no (classifiers stop at 3.12; `requires_python` has no upper bound) |
| anyio | 4.15.1 | `>=3.10` | yes (also lists 3.15) |
| httpcore | 1.0.9 | `>=3.8` | no (classifiers stop at 3.12; `requires_python` has no upper bound) |
| h11 | 0.16.0 | `>=3.8` | no classifiers list at all (no `Programming Language :: Python :: 3.*` entries) |

These are today's (2026-09-19) newest stable releases on PyPI for all six —
confirmed by `curl -s https://pypi.org/pypi/<name>/json`, `info.version`, no
pre-release markers. **Flag**: httpx, httpcore and h11 not carrying an
explicit `3.14` (or in h11's case any) classifier is metadata lag, not a
documented incompatibility — their `requires_python` floors have no ceiling,
so pip will install them on 3.14, but no upstream CI badge confirms 3.14 is
exercised. This is the one thing in this note that should be confirmed
empirically (build the image, run the test suite) rather than taken from
metadata alone.

---

## 1. Versions to pin, and the full non-extra dependency set

Each package's declared **non-extra** `requires_dist`, read from
`https://pypi.org/pypi/<name>/json` (`info.requires_dist`) at the versions in
the table above — extras (`extra == "..."`) excluded, and any marker that
resolves false on Python 3.14 excluded:

- **starlette 1.6.0**: `anyio<5,>=3.6.2`. (`typing-extensions>=4.10.0` is
  marked `; python_version < "3.13"` — false on 3.14, dropped.)
- **uvicorn 0.53.0**: `click>=7.0`, `h11>=0.8`. (`typing-extensions>=4.0` is
  marked `; python_version < "3.11"` — false on 3.14, dropped. Every other
  listed package — `httptools`, `python-dotenv`, `pyyaml`, `uvloop`,
  `watchfiles`, `websockets` — is marked `extra == "standard"`.)
- **httpx 0.28.1**: `anyio`, `certifi`, `httpcore==1.*`, `idna`. (`h2`,
  `socksio`, `zstandard`, `brotli`/`brotlicffi`, `click`/`pygments`/`rich`
  are all extras.)
- **anyio 4.15.1** (pulled in by both starlette and httpx): `idna>=2.8`,
  `typing_extensions>=4.16.0; python_version < "3.15"` — **true on 3.14**, so
  this one *is* pulled in despite neither starlette nor uvicorn needing it
  directly. (`exceptiongroup>=1.0.2; python_version < "3.11"` is false on
  3.14.)
- **httpcore 1.0.9** (pulled in by httpx): `certifi`, `h11>=0.16`. (`anyio`
  itself is only httpcore's own `extra == "asyncio"` marker — irrelevant here
  because httpx's own hard dependency on `anyio`, not httpcore's extra,
  is what actually lands anyio in the environment; a bare `pip install
  httpcore` with no extras would *not* pull anyio, but `pip install httpx`
  always does, per §1's list above, so the asyncio backend httpcore uses at
  runtime is always present when httpx is installed.)
- **h11 0.16.0**: no dependencies (`requires_dist` is `null`).
- **certifi 2026.7.22**, **idna 3.20**, **click 8.5.0**,
  **typing_extensions 4.16.0**: no dependencies at their current versions.

**So `pip install starlette==1.6.0 uvicorn==0.53.0 httpx==0.28.1` (no
extras) resolves to exactly ten packages**: starlette, uvicorn, httpx, anyio,
httpcore, h11, certifi, idna, click, typing_extensions — every one with no
upper Python-version ceiling that would exclude 3.14.

**`uvicorn[standard]` — what it adds, and why plain `uvicorn` is enough
here.** From uvicorn 0.53.0's own `extra == "standard"` markers
(`requires_dist` above): `uvloop` (a faster asyncio event-loop
implementation, POSIX-only, `sys_platform != "win32"`), `httptools` (a C-based
HTTP/1.1 parser, alternative to the pure-Python `h11`), `websockets`
(WebSocket protocol support), `watchfiles` (backs `--reload`'s
filesystem-change detection), `python-dotenv` (backs `--env-file`), `pyyaml`
(backs a YAML `--log-config` file). **None of these bear on ticket 01/02/06**:
`gideon-api` has no WebSocket route, Compose supplies its environment
directly (no `--env-file` needed), `--reload` is a dev-only flag (never used
in the rendered container), and a YAML log-config file is not part of the
spec §19.4 logging shape (route/status/seconds, no header values — plain
Python `logging` config, not a file, is enough). **What is lost without the
extra**: uvloop's lower per-request scheduling overhead (irrelevant at
GIDEON's single-connection-relay scale), and `httptools`'s faster HTTP
parsing (irrelevant for the same reason, and — per §2 below — h11 and
httptools report the **same** ASGI `spec_version`, so nothing about
disconnect-detection changes either way). **Recommendation stands: plain
`uvicorn`, no `[standard]` extra.**

**The base image.** `python:3.14-slim-trixie` is an **active, multi-platform**
tag on Docker Hub today (`GET
https://hub.docker.com/v2/repositories/library/python/tags/3.14-slim-trixie`,
`tag_status: "active"`, images for `amd64`, `arm64/v8`, `ppc64le`, `s390x`,
`386`, `arm/v5`, `arm/v7`, `riscv64` — pushed 2026-09-19, i.e. today).
`trixie` is Debian 13, matching Docker's current slim-base naming grammar
(`<python-minor>-slim-<debian-codename>`); the fully-pinned patch tag today is
`3.14.7-slim-trixie` (current patch per the tag listing), with `3.14-slim-trixie`
floating to the newest 3.14.x — the same floating-then-digest-pinned shape
`images.lock` already uses for `caddy:2.11` and `postgres:18` (mirrored pins
record the tag's resolved digest at pin time, not the patch number). All
three packages' `requires_python` floors (`>=3.8`/`>=3.10`) are satisfied by
3.14; see the classifier caveat in the table above.

---

## 2. Disconnect detection while sending nothing — the critical question

**With this Starlette + uvicorn pair, yes: a client disconnect is noticed
while the body iterator is suspended and nothing is being sent — because
uvicorn's own advertised ASGI `spec_version` forces Starlette's
`StreamingResponse` onto its concurrent-listener branch, not its
`send()`-raises-`OSError` branch.**

Starlette 1.6.0's `StreamingResponse.__call__`
(`starlette/responses.py:257-283`, quoted in full — this is the entire
method):

```python
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            ...
        spec_version = tuple(map(int, scope.get("asgi", {}).get("spec_version", "2.0").split(".")))

        if spec_version >= (2, 4):
            try:
                await self.stream_response(send)
            except OSError:
                raise ClientDisconnect()
        else:
            async with create_collapsing_task_group() as task_group:

                async def wrap(func: Callable[[], Awaitable[None]]) -> None:
                    await func()
                    task_group.cancel_scope.cancel()

                task_group.start_soon(wrap, partial(self.stream_response, send))
                await wrap(partial(self.listen_for_disconnect, receive))
        ...
```

`listen_for_disconnect` (`responses.py:242-246`) is a plain
`while True: message = await receive(); if message["type"] == "http.disconnect": break`.
So: at ASGI `spec_version >= 2.4`, Starlette trusts that a dead connection
makes `send()` raise `OSError` and relies on that alone (no concurrent
receive-watcher — cheaper, but only catches a disconnect at the *next send*).
At `spec_version < 2.4`, it runs `listen_for_disconnect` and
`stream_response` concurrently in one `anyio` task group; whichever finishes
first (via `wrap`) cancels the group's `cancel_scope`, which delivers
cancellation into whatever `stream_response` (hence the body iterator/async
generator, wherever it's suspended — an `await` inside the lag window's own
logic included) is doing, **with no dependency on anything being sent**.

**uvicorn 0.53.0 advertises `"2.3"`** for the HTTP scope in **both**
implementations it can select — confirmed by grep, one line each:

- `uvicorn/protocols/http/h11_impl.py:210`:
  `"asgi": {"version": self.asgi_version, "spec_version": "2.3"},`
- `uvicorn/protocols/http/httptools_impl.py:228`:
  `"asgi": {"version": self.asgi_version, "spec_version": "2.3"},`

(Only the *WebSocket* protocol implementations advertise `"2.4"` —
`websockets_impl.py:199`, `wsproto_impl.py:236`,
`websockets_sansio_impl.py:253` — irrelevant to `gideon-api`'s HTTP-only
routes.)

`tuple(map(int, "2.3".split(".")))` is `(2, 3)`, which is **not** `>= (2, 4)`,
so Starlette 1.6.0 against uvicorn 0.53.0's HTTP scope always takes the
**concurrent-listener branch**. Conclusion for ticket 02/06's lag window:
**a plain `starlette.responses.StreamingResponse` returned from a route,
served by plain `uvicorn`, already gets its body-iterator cancelled the
moment `http.disconnect` arrives — including during the tens of seconds the
lag window is silently withholding reasoning text and sending nothing.** No
extra code is required for Starlette's own routing layer to behave this way
today.

**The robust pattern anyway, and why ticket 06 needs it regardless.** This
behaviour is contingent on uvicorn continuing to advertise `"2.3"` — a future
uvicorn release could adopt `"2.4"` for HTTP the way it already has for
WebSockets, which would silently switch Starlette onto the `OSError`-only
path and **stop** detecting a disconnect during a silent lag window. More
concretely for GIDEON: ticket 06 needs to close the upstream not only on a
*client* disconnect but also on its **own** guardrail-trip decision — a
self-triggered cancellation with no ASGI message involved at all. Both needs
are met by the same shape, and it is exactly what Starlette's own `else`
branch already does: an `anyio` task group (or a bare `anyio.CancelScope`)
wrapping the httpx relay, with a second task doing
`while True: message = await receive(); if message["type"] == "http.disconnect": break`
(Starlette's own `Request.is_disconnected()`, by contrast,
`starlette/requests.py:328-340`, only **polls** — it opens an
already-cancelled `CancelScope` and does one non-blocking `receive()` per
call, so it must be called in a loop to be useful and never blocks waiting;
the persistent-task shape above is what actually blocks until the message
arrives with no polling latency) — cancelling the same scope the trip logic
cancels reaches the same suspended `await` inside the relay either way, and
(§3) httpcore's own internal shielding is what actually closes the socket
once that cancellation lands, independent of which of the two triggered it.
This is not a gap to fix; it is the shape ticket 06 was already going to
need for the trip, applied a second time for defense against a uvicorn
version bump.

**How uvicorn delivers `http.disconnect` to `receive()`.** In the h11
implementation, `receive()` (`h11_impl.py`, the `HTTPProtocol.receive`
method) blocks on `self.message_event.wait()` and returns
`{"type": "http.disconnect"}` once `self.disconnected` or
`self.response_complete` is set; `self.disconnected` is set from
`connection_lost` (the asyncio `Protocol.connection_lost` callback, fired
when the transport's underlying socket is actually torn down — a TCP FIN or
RST from the peer, or the local side closing it). This is a transport-level
signal, not an application-level heartbeat: it needs the client's socket to
actually close (or the OS to detect the peer is gone); it is not raised
merely because the client stopped reading fast enough (that shows up as
uvicorn's own write-side flow control instead, §4). Keep-alive is orthogonal:
a keep-alive connection reused for a *later* request is a fresh HTTP
transaction on the same TCP connection; the disconnect signal in question
belongs to the in-flight response's own connection state, not to
keep-alive's idle-timeout bookkeeping (`uvicorn/protocols/http/h11_impl.py`'s
`shutdown()`, quoted in §4, treats an in-flight cycle and an idle keep-alive
connection differently already).

---

## 3. Cancellation reaching httpx

**Yes — httpcore closes the connection outright (does not return it to the
pool) when the response body was not fully read at cancellation, and this
already happens under httpcore's own internal shield; the application does
not need to add its own shielding around the ordinary
`async with client.stream(...) as response: async for chunk in ...` idiom.**

`httpcore/_async/http11.py:324-352` (`HTTP11ConnectionByteStream`, the object
`httpx.Response.stream` iterates), quoted in full:

```python
class HTTP11ConnectionByteStream:
    def __init__(self, connection: AsyncHTTP11Connection, request: Request) -> None:
        self._connection = connection
        self._request = request
        self._closed = False

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        kwargs = {"request": self._request}
        try:
            async with Trace("receive_response_body", logger, self._request, kwargs):
                async for chunk in self._connection._receive_response_body(**kwargs):
                    yield chunk
        except BaseException as exc:
            # If we get an exception while streaming the response,
            # we want to close the response (and possibly the connection)
            # before raising that exception.
            with AsyncShieldCancellation():
                await self.aclose()
            raise exc

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            async with Trace("response_closed", logger, self._request):
                await self._connection._response_closed()
```

`except BaseException` catches `asyncio.CancelledError` (it is a
`BaseException` subclass), and the close happens **inside**
`AsyncShieldCancellation()` (`httpcore/_synchronization.py:190-209`) —
`anyio.CancelScope(shield=True)` under asyncio, `trio.CancelScope(shield=True)`
under trio — before `raise exc` re-raises the cancellation. `_response_closed`
(`http11.py:238-251`):

```python
    async def _response_closed(self) -> None:
        async with self._state_lock:
            if (
                self._h11_state.our_state is h11.DONE
                and self._h11_state.their_state is h11.DONE
            ):
                self._state = HTTPConnectionState.IDLE
                ...  # returned to the pool
            else:
                await self.aclose()   # -> self._network_stream.aclose(): the socket is closed
```

Only a fully-read message (`our_state`/`their_state` both `h11.DONE`) goes
back to `IDLE` (pool-reusable); anything else — including exactly the
"cancelled mid-stream" case — falls to the `else` and calls the
**connection's** `aclose()`, which closes the actual network stream
(`http11.py:254-257`: `self._state = CLOSED; await self._network_stream.aclose()`).
httpx's own `stream()` context manager (`httpx/_client.py:1588-1590`,
`async def stream(...): ... try: yield response finally: await response.aclose()`)
adds a second, outer `aclose()` call, but by the time it runs the response is
already `is_closed = True` from the inner shielded close above, so it's a
guarded no-op — **no extra shielding is needed at the application layer**
for the standard `async with client.stream(...) as response: async for
chunk in response.aiter_raw(): ...` shape, under either `asyncio` or
`anyio`'s own cancellation, because httpcore already shields its own cleanup
before propagating the cancellation upward.

**Which iterator to use.** `Response.aiter_raw()` (`httpx/_models.py:1037-1063`)
pushes each raw chunk from the wire through a `ByteChunker(chunk_size=None)`
(default), and `ByteChunker.decode()` with `chunk_size=None`
(`httpx/_decoders.py:237-239`) is `return [content] if content else []` —
**every chunk is passed through immediately and unmodified**, no buffering,
no re-chunking. `aiter_bytes()` (`_models.py:982-1004`) additionally runs the
chunk through a **content-decoder** (`self._get_content_decoder()` —
gzip/deflate/brotli/zstd), which re-chunks around decompressor output
boundaries when `Content-Encoding` is set (irrelevant if vLLM's SSE responses
carry no `Content-Encoding`, which they don't by default, but `aiter_raw()`
is the one that's *correct regardless*). `aiter_lines()` (`_models.py:1028-1034`)
decodes to text and buffers through a `LineDecoder`, so it waits for a full
line before yielding — fine for SSE's `\n`-terminated lines, but it is doing
text decoding and line-buffer bookkeeping the raw relay does not need.
**Recommendation: `aiter_raw()`** for the ticket 02/06 relay — a straight
byte pass-through, letting Starlette forward exactly what h11 assembled.

**Timeout.** `httpx.Timeout(5.0)` is httpx's own default
(`httpx/_config.py:246`: `DEFAULT_TIMEOUT_CONFIG = Timeout(timeout=5.0)`),
applying 5 s to `connect`, `read`, `write`, and `pool` alike. The `read`
timeout is **per network read, not a whole-stream deadline** — confirmed at
`httpcore/_async/http11.py:192-207`
(`_receive_response_body`: `while True: event = await self._receive_event(timeout=timeout); ...`)
and `:209-233` (`_receive_event`: `data = await self._network_stream.read(self.READ_NUM_BYTES, timeout=timeout)`
inside the same `while True`) — the same `timeout` value gates *each*
`_network_stream.read()` call, reset every time data arrives. So a long
silent reasoning phase needs `read` set comfortably above the longest
expected gap between engine chunks (e.g. 120–300 s), while `connect` can
stay low (a few seconds — GIDEON's engine is on the same Compose network, no
long TCP handshake expected). A concrete shape: `httpx.Timeout(5.0, read=180.0)`
(5 s connect/write/pool, 180 s read) rather than one flat number.

**`trust_env=False`.** Confirmed at `httpx/_client.py:1399`
(`allow_env_proxies = trust_env and transport is None`, gating whether
`get_environment_proxies()` — `httpx/_utils.py:30`, reads
`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` — is ever consulted) and
`httpx/_config.py:34-36` (`if trust_env and os.environ.get("SSL_CERT_FILE")`,
similarly for `SSL_CERT_DIR`) — `trust_env=False` stops both proxy-env-var
lookup and `SSL_CERT_FILE`/`SSL_CERT_DIR` lookup. **Correction to the
question's premise**: httpx does **not** read `.netrc` automatically at all,
regardless of `trust_env` — `netrc` support exists only as the explicit,
opt-in `httpx.NetRCAuth(file=...)` class (`httpx/_auth.py:145-167`), passed
as `auth=` on a request; there is no implicit netrc lookup for `trust_env`
to gate. For `gideon-api`'s httpx client talking to `gideon-generator` on
the Compose network, `trust_env=False` is the safe default regardless (no
proxy should ever apply to an internal service call, and no ambient
`SSL_CERT_FILE` should redirect TLS verification for a plain-HTTP internal
call).

---

## 4. uvicorn's write path, middleware shape, and logging/lifecycle

**No response-body buffering; `Transfer-Encoding: chunked` is added — but by
h11, not by uvicorn itself.** `uvicorn/protocols/http/h11_impl.py`'s
`send()` (`:455-517`) writes each `http.response.body` message straight to
the transport as it arrives (`output = self.conn.send(event=h11.Data(data=data)); self.transport.write(output)`,
`:495-497`) — no internal queue or buffer of body bytes. Flow control is the
asyncio `Transport`'s own write-buffer high/low water marks: uvicorn's
`send()` checks `if self.flow.write_paused: await self.flow.drain()`
(`:456-457`) before writing, and `pause_writing()`/`resume_writing()`
(`h11_impl.py:347-357`) are the standard asyncio `Protocol` callbacks the
event loop invokes once the transport's outstanding write buffer crosses its
high-water mark (uvicorn never calls `set_write_buffer_limits` itself, so
asyncio's own default applies); `FlowControl.drain()`
(`flow_control.py:11-19`) just awaits an `asyncio.Event` that
`resume_writing()` sets once the buffer drains. (`HIGH_WATER_LIMIT = 65536`
in the same module gates a *different* thing — pausing the read side once
buffered, unconsumed **request** body exceeds 64 KiB,
`h11_impl.py:266` — not the response write path.) Separately, **h11 itself**
decides response framing: `h11/_connection.py:602-644`
(`_clean_up_response_headers_for_sending`) computes `_body_framing` for the
outgoing `Response`, and when there's no `Content-Length` (the streaming
case) and the peer is HTTP/1.1+, it sets
`headers = set_comma_header(headers, b"transfer-encoding", [b"chunked"])`
(`:643`) before the headers are ever put on the wire — this runs inside
`Connection.send()` (`_connection.py:556`), which uvicorn's `h11_impl.py`
calls as `self.conn.send(event=response)` (`:486-487`). So: **yes,
`Transfer-Encoding: chunked` is added automatically for a `StreamingResponse`
with no `Content-Length`, but the layer doing it is h11, transparently, not
application or uvicorn code** — nothing in `gideon-api` needs to set that
header itself.

**Middleware shapes to avoid, and the one to use for the bearer key.**
`starlette.middleware.base.BaseHTTPMiddleware` (1.6.0) no longer *polls* for
a disconnect (`middleware/base.py:113-127`'s `receive_or_disconnect` now
races `wrapped_receive` against a `response_sent` `anyio.Event` in a real
task group, fixing the older busy-poll behaviour), but it still runs the
wrapped app in a **separate** task (`task_group.start_soon(coro)`,
`:144`) and re-wraps its response body in its own `_StreamingResponse`
(`:204-240`), whose `__call__` is a **bare** `async for chunk in
self.body_iterator: await send(...)` with **no disconnect-listening of its
own** — a client disconnect is still eventually observed (it propagates
through the inner app's own `receive_or_disconnect`, which does forward a
real `http.disconnect`), but only via that extra hop, and the class carries
Starlette's own documented caveats (buffering `request.body()` for
downstream middleware, background tasks not always running on a cancelled
request). `starlette.middleware.gzip.GZipMiddleware` at 1.6.0 already
excludes `text/event-stream` from `DEFAULT_EXCLUDED_CONTENT_TYPES`
(`middleware/gzip.py:13-27`), so it would not compress an SSE relay by
default even if added — but there is no reason to add it to `gideon-api` at
all (nothing it serves benefits from compression). **Recommended shape for
the bearer-key check: a pure ASGI middleware class** —
`def __init__(self, app): ...` / `async def __call__(self, scope, receive,
send): ...`, checking `scope["type"]`/the path/the `Authorization` header
and short-circuiting with a 401 `send()` before ever calling `self.app(...)`
— exactly vLLM's own `AuthenticationMiddleware` shape
(`docs/research/vllm-engine-service.md` §1 quotes it in full: pure ASGI,
`GUARDED_PREFIX` prefix check, `secrets.compare_digest` per token). This
runs before any routing, so a request without the key never reaches ticket
02/06's upstream call at all — matching ticket 02's own acceptance line — and
it is one plain `__call__`, no task group, no response re-wrapping, none of
`BaseHTTPMiddleware`'s caveats. Route-level dependency checks (only
meaningful with a framework that has a dependency-injection system, which
plain Starlette does not) would have to be repeated per route and could be
missed on a newly added one; the ASGI-middleware shape cannot be forgotten.

**Access logging.** uvicorn's default access line
(`h11_impl.py:475-483`, `self.access_logger.info('%s - "%s %s HTTP/%s" %d',
get_client_addr(self.scope), self.scope["method"],
get_path_with_query_string(self.scope), self.scope["http_version"], status)`)
carries **client address, method, path (with query string), HTTP version,
status** — no header values, no body. This matches `vllm-engine-service.md`
§11's citation of the same uvicorn record shape verbatim (vLLM's own
`RequestLogger`/uvicorn access-log discussion quotes the identical format
string). Switches, from `uvicorn/main.py`: `--log-config <file>` (`:206-210`,
`.ini`/`.json`/`.yaml`), `--log-level` (`:213-219`, default `info`),
`--access-log/--no-access-log` (`:220-225`, default **on**). For spec
§19.4's "route, status, seconds, never a header value or body" shape,
`gideon-api`'s own application-level logging (not uvicorn's access log) is
what should emit the seconds figure — uvicorn's own access line has no
duration field at all, so route+status+seconds needs its own log call around
the handler regardless of `--access-log`'s setting; leaving `--access-log`
on for the method/path/status line and adding the service's own
route+status+seconds line is one reasonable shape, or passing
`--no-access-log` and emitting only the service's own line is another — this
note does not choose between them.

**Programmatic launch and graceful shutdown.** `uvicorn.run(app, **kwargs)`
(`uvicorn/main.py:503-…`) builds a `Config(...)` from its keyword arguments
and calls `Server(config=config).run()` (`:621-624`) — the standard
`python -m <package>`-style entrypoint is a thin `if __name__ ==
"__main__": uvicorn.run("gideon_api.app:app", host=..., port=...)` (or the
`Config`/`Server` pair built by hand for finer control over the event loop).
On SIGTERM, `Server.handle_exit` (`server.py:351-356`) sets `should_exit =
True`, and the serve loop then calls `shutdown()` (`server.py:281-309`):
it stops accepting new connections (`server.close()`), then calls
`connection.shutdown()` on every open connection. For an **in-flight**
streaming cycle specifically, `h11_impl.py`'s own `shutdown()`
(`:336-344`) does **not** cut the connection — `if self.cycle is None or
self.cycle.response_complete: <close now> else: self.cycle.keep_alive =
False` — i.e. a mid-stream response is left to finish; the connection just
won't be kept alive for a *next* request. `Server.shutdown()` then
`await asyncio.wait_for(self._wait_tasks_to_complete(), timeout=self.config.timeout_graceful_shutdown)`
(`:296-299`) — **default `timeout_graceful_shutdown` is `None`** (unbounded
wait) unless `--timeout-graceful-shutdown <seconds>` is set
(`main.py:298-301`); past the timeout, every remaining task is
`.cancel()`led (`:307`), which — per §2/§3 above — correctly cancels the
relay generator and (via httpcore's shielded close) tears down the upstream
connection either way. **For Compose's recreate on `apply`**: Compose sends
SIGTERM then waits `stop_grace_period` (10 s by default) before SIGKILL;
with uvicorn's own `timeout_graceful_shutdown` left at its `None` default,
an open SSE stream would be allowed to run past Compose's own grace period
and get SIGKILLed instead of gracefully finishing — worth setting an
explicit `--timeout-graceful-shutdown` bound at or under Compose's configured
`stop_grace_period` if a clean finish (rather than a SIGKILL) is wanted for
in-flight streams during a recreate.

---

## 5. The engine side, vLLM v0.27.1

**HTTP-server half of the disconnect story** (the existing notes cover the
engine-core half already — see below). `vllm/entrypoints/openai/chat_completion/api_router.py:39-72`:
the `/v1/chat/completions` route handler `create_chat_completion` is wrapped
`@with_cancellation` then `@load_aware_call`, and on the streaming path
returns `fastapi.responses.StreamingResponse(content=generator,
media_type="text/event-stream")` (`:72`) — `generator` being
`handler.create_chat_completion(request, raw_request)`
(`OpenAIServingChat`'s own async generator over `AsyncLLM.generate()`).
`with_cancellation` (`vllm/entrypoints/serve/utils/api_utils.py:52-89`) races
the **handler function itself** (which, for the streaming case, does the
fast setup work of building the generator/response object) against a
`listen_for_disconnect(request)` task; its own docstring states the handoff
explicitly: *"In the case where a `StreamingResponse` is returned by the
handler, this wrapper will stop listening for disconnects and instead the
response object will start listening for disconnects."* So once the
`StreamingResponse` is returned, responsibility for a mid-stream disconnect
passes to `fastapi.responses.StreamingResponse` — the same Starlette class
this note's §2 already analysed (vLLM's own bundled Starlette/uvicorn
versions inside its image were not independently checked in this pass; only
the mechanism/class identity is confirmed here). Either way the generator
being iterated is cancelled, and `AsyncLLM.generate()`
(`vllm/v1/engine/async_llm.py:576-611`) has its own explicit handler,
confirming and slightly extending what `owui-stream-hook.md`'s 2026-09-17
addendum already cites (fact 4, `async_llm.py`'s `except (CancelledError,
GeneratorExit): abort`):

```python
        # If the request is disconnected by the client, generate()
        # is cancelled or the generator is garbage collected. So,
        # we abort the request if we end up here.
        except (asyncio.CancelledError, GeneratorExit):
            if q is not None:
                await self.abort(q.request_id, internal=True)
            if self.log_requests:
                logger.info("Request %s aborted.", request_id)
            raise
```

(`async_llm.py:606-611`) — this is the exact generator every chat-completion
stream iterates; a cancellation (from an HTTP disconnect via
`StreamingResponse`, or from `gideon-api` itself dropping/closing its own
httpx stream on a guardrail trip, which surfaces to vLLM identically as its
own client going away) reaches this `except` and calls `self.abort(...)`.

**The running-requests gauge**: `vllm:num_requests_running` — confirmed
verbatim, `vllm/v1/metrics/loggers.py:492-497`:
```python
        gauge_scheduler_running = self._gauge_cls(
            name="vllm:num_requests_running",
            documentation="Number of requests in model execution batches.",
            multiprocess_mode="mostrecent",
            labelnames=labelnames,
        )
```
This is the gauge ticket 02/06's proof watches falling to zero after a
disconnect/trip.

**`/v1/models` shape** — `ModelList`/`ModelCard`
(`vllm/entrypoints/openai/engine/protocol.py:89-101`): `{"object": "list",
"data": [{"id": ..., "object": "model", "created": <unix ts>, "owned_by":
"vllm", "root": ..., "parent": ..., "max_model_len": ..., "permission":
[]}]}`, served by `GET /v1/models` (`vllm/entrypoints/openai/models/api_router.py:19-24`).
**Auth**: `/v1/models` starts with `/v1`, one of `GUARDED_PREFIX`'s four
prefixes, so it requires the bearer key when one is configured; `/health`
does not start with any of them and is unauthenticated by design — both
already established, with the exact `AuthenticationMiddleware`/
`GUARDED_PREFIX` source, in `docs/research/vllm-engine-service.md` §1 (not
re-derived here).

**Addendum, 2026-09-19 — the engine's own in-stream error shape** (read at the
pin during ticket 02's `TRIP-1` discovery; recorded here so an engine bump's
re-read meets it). An error raised *after* a chat-completion stream has begun
cannot change the status that is already on the wire, so
`vllm/entrypoints/openai/chat_completion/serving.py:835-842` at `v0.27.1`
emits the error object as one `data:` event and then the end marker
`data: [DONE]`, ending the body cleanly. This is the shape `gideon-api`'s
relay copies for its own mid-stream failure (`gideon/api/relay.py`: a blank
line closing any event the failure cut in half, then the fixed
`upstream_unavailable` object as one `data:` event, then the end marker), and
it is the shape the pinned frontend reads — `docs/research/owui-stream-hook.md`
§8 item 3. A bump that changes it changes both the relay's failure path and
the frontend's reading of it, so this paragraph is the one to re-read.

---

## 6. The frontend as the service's client, Open WebUI v0.11.3

`owui-engine-connection.md` §1/§5 already establishes the connection shape
(Chat Completions by default, `Authorization: Bearer {key}` when `auth_type`
is `'bearer'`/unset, `trust_env=True` unconditionally) and the shared
`aiohttp.ClientSession` pool (`utils/session_pool.py`'s `get_session()`).
Extending it with the two things ticket 01/02 need — the model-list call and
what a cancelled streaming task actually closes:

**`GET /models`/model discovery** uses a **one-off** session, not the pool —
`routers/openai.py:96`: `async with aiohttp.ClientSession(timeout=_MODEL_LIST_TIMEOUT,
trust_env=True) as session:`, where `_MODEL_LIST_TIMEOUT =
aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)`
(`routers/openai.py:78`) and `AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST` reads env
var `AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST` (falling back to the legacy name
`AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST`), default **10 seconds**
(`env.py:642-648`).

**`POST /chat/completions`** uses the **shared pooled** session
(`utils/session_pool.py`'s `get_session()`, already documented in
`owui-engine-connection.md` §5) with a **per-request** timeout override —
`routers/openai.py:1622-1631`: `session = await get_session(); r = await
session.request(..., timeout=get_client_timeout(stream=is_streaming_request))`.
`get_client_timeout` (`session_pool.py:47-48`):
```python
def get_client_timeout(stream: bool = False) -> aiohttp.ClientTimeout:
    return _CLIENT_STREAM_TIMEOUT if stream else _CLIENT_TIMEOUT
```
with `_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)`
and `_CLIENT_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT,
sock_read=AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT)` (`session_pool.py:41-45`).
`AIOHTTP_CLIENT_TIMEOUT` defaults to **300 s** (`env.py:593-597`) and applies
as aiohttp's `total` — the whole request-plus-response-read span, streaming
included — while `AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT` (a per-chunk idle
timeout, aiohttp's `sock_read`) defaults to **unset/`None`**
(`env.py:600-610`, `if AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT == '': = None`).
**Consequence for a long turn**: with defaults, a stream through
`gideon-api` that runs past 300 s wall-clock (connect through last byte)
gets cut off by OWUI's own `total` timeout regardless of activity — worth
knowing if a lag-window-heavy completion can legitimately run that long,
though it's an existing OWUI-side behaviour, not something `gideon-api`
controls.

**What closes the upstream connection when the streaming task is
cancelled** — the mechanism `owui-stream-hook.md` §7's `asyncio.shield(response.body_iterator.aclose())`
call ultimately bottoms out in. `routers/openai.py:1674-1690` returns
`StreamingResponse(stream_wrapper(r), status_code=r.status,
headers=_clean_proxy_headers(r.headers))` for a streaming upstream response
`r`, and explicitly skips its own `finally: cleanup_response(r)` for the
streaming case (`routers/openai.py:1711-1712`: `finally: if not streaming:
await cleanup_response(r)`), delegating cleanup to `stream_wrapper` itself.
`stream_wrapper` — `utils/session_pool.py:119-133`, quoted in full:
```python
async def stream_wrapper(response, session=None, passthrough=False):
    try:
        if passthrough:
            stream = response.content.iter_any()
        else:
            stream = stream_chunks_handler(response.content)
        async for chunk in stream:
            yield chunk
    finally:
        await cleanup_response(response, session)
```
The `try/finally` runs `cleanup_response` (`:95-115`, `response.close()`, not
`.release()`) whether the generator finishes normally, is cancelled, or is
closed via `GeneratorExit` (Starlette's `body_iterator.aclose()` on Stop, per
`owui-stream-hook.md` §7). `aiohttp.ClientResponse.close()`
(confirmed at OWUI's own pinned `aiohttp==3.13.5`,
`aiohttp/client_reqrep.py:594-604`): `self._connection.close(); self._connection = None`
— the connection is **discarded**, not returned to the pool (`.release()`,
`:607-616`, is the pool-return path, and is not what `cleanup_response`
calls). So: **on Stop or an ordinary API disconnect, the frontend's own
aiohttp connection to `gideon-api` is genuinely torn down** — `gideon-api`
sees a real TCP close, matching what ticket 02's proof needs to observe, not
an idle pooled connection lingering.

---

## 7. Hash-pinned install inside an argument-driven Dockerfile

**pip's `--require-hashes` mode**, per pip's own docs
(`https://pip.pypa.io/en/stable/topics/secure-installs/`, rolling docs tree
at 2026-09-19 — not a tagged release, quoted verbatim): three restrictions,
all mandatory together —
1. *"Requirements must be pinned (either to a URL, filesystem path or using
   `==`)."*
2. *"Hashes are required for all requirements."*
3. *"Hashes are required for all dependencies. If there is a dependency that
   is not spelled out and hashed in the requirements file, it will result in
   an error."*

So hash-checking mode requires **every** package that ends up installed —
direct and transitive alike (for §1's ten-package set: starlette, uvicorn,
httpx, anyio, httpcore, h11, certifi, idna, click, typing_extensions) — to
appear pinned-and-hashed somewhere pip reads. `--only-binary` (pip's CLI
reference, `pip_install/`) is unrelated to hash-checking itself — it just
refuses source distributions — and `--no-deps` (same page) simply stops pip
from resolving *any* dependency at all for the given requirement, which is
orthogonal to hash-checking (it's how one avoids needing pip's resolver to
touch transitive deps in the first place, by naming every package — direct
and transitive — explicitly and passing `--no-deps` on each).

**Three honest options against GIDEON's constraints** (versions live as
`images.lock` `build_args`, no version in the Dockerfile; the build context
is `images/<name>/` alone; `inputs_digest` hashes only the base digest, the
build args, and the Dockerfile's bytes):

- **(a) One `ARG` per direct and transitive package, `--no-deps` per
  install.** Every one of the ten packages above becomes its own
  `ARG X_VERSION` (and, if hash-checked, an `ARG X_HASH` or a hash baked into
  a `pip install "x==${X_VERSION} --hash=sha256:${X_HASH}"` line), with
  `pip install --no-deps` so nothing floats. This keeps every version inside
  `inputs_digest`'s existing coverage (build args + Dockerfile bytes) with no
  new file to track, but it multiplies the number of `ARG`s tenfold beyond
  the three "direct" packages the maintainer actually chose, and a
  transitive dependency's *version* is pinned this way but each `ARG` still
  needs its hash pasted in by hand (or generated once via `pip hash` and
  copied in) since a bare version number isn't a hash.
- **(b) A hash-locked requirements file in `images/<name>/`.** The
  conventional shape (`pip-compile --generate-hashes` or `pip freeze` plus
  `pip hash`, then `pip install --require-hashes -r requirements.lock`) is
  the one pip's own docs are written around, and keeps the ARG surface small
  (three `ARG`s for the versions the maintainer sets, if that's still
  wanted, or none at all if the lock file is the sole source of truth). **As
  stated, this is not covered by `inputs_digest`** — the digest covers "the
  base digest, the build args, and the Dockerfile's bytes only" (per the
  ticket's own framing), so a requirements-lock file changing would not
  invalidate `inputs_digest` and the built image could silently drift from
  what the file says, unless `inputs_digest`'s coverage is extended to hash
  that file too (a change to how `inputs_digest` is computed, not covered by
  this note).
- **(c) Direct pins only, transitive versions floating** — the shape
  `images/postgres/Dockerfile` already uses for its one apt dependency
  (`ARG PGBACKREST_VERSION`, `apt-get install -y --no-install-recommends
  "pgbackrest=${PGBACKREST_VERSION}"`, no pin on `pgbackrest`'s own apt
  dependencies). Applied here: three `ARG`s (`STARLETTE_VERSION`,
  `UVICORN_VERSION`, `HTTPX_VERSION`), `pip install "starlette==${STARLETTE_VERSION}"
  "uvicorn==${UVICORN_VERSION}" "httpx==${HTTPX_VERSION}"` with pip's
  resolver left to pick anyio/httpcore/h11/certifi/idna/click/typing_extensions
  at whatever versions satisfy the three packages' own constraints at build
  time. Simplest, matches the one existing precedent, and needs no hash
  bookkeeping at all — but it is not reproducible byte-for-byte across two
  builds a version bump apart on PyPI (a transitive package publishing a new
  version between two builds changes what gets installed with no
  corresponding `images.lock` line recording it, and `--require-hashes`
  cannot be layered on top of it without also pinning the floaters, which
  reduces to (a) or (b) anyway).

This note does not choose between them — that's the plan's call, per the
ticket framing.

**Hosts a build must reach.** Confirmed directly: pip's default index is
`https://pypi.org/simple/` (pip's own CLI reference,
`pip_install/`'s `--index-url` default), and a live fetch of
`https://pypi.org/simple/httpx/` today returns download links pointing at
`https://files.pythonhosted.org/packages/...` — so both `pypi.org` (the
index/metadata host) and `files.pythonhosted.org` (the actual wheel/sdist
CDN) must be reachable during `RUN pip install`, mirroring the
`apt.postgresql.org`/`deb.debian.org` pair `built-images.md` §4 already names
for the `postgres` image's `image-build` egress-allowlist group.

**Docker's predefined proxy build args and pip.** Docker's own build docs
(`https://docs.docker.com/build/building/env-vars/`, rolling docs tree at
2026-09-19): `HTTP_PROXY`, `HTTPS_PROXY`, `FTP_PROXY`, `NO_PROXY`,
`ALL_PROXY` (case-insensitive) are honoured automatically for every `RUN`
step — *"You don't need to declare or reference these arguments in the
Dockerfile. Specifying a proxy with `--build-arg` is enough"* — and are
*"automatically excluded from the build cache and the output of `docker
history` by default"* (referencing them explicitly in the Dockerfile is what
would leak them into the cache/history — GIDEON's Dockerfiles don't need to,
matching `built-images.md` §4's existing claim for the `postgres` image's
apt calls). **pip itself honours these** — pip's own user guide
(`https://pip.pypa.io/en/stable/user_guide/`, "Using a Proxy Server") states
pip can be configured "by setting the standard environment-variables
`http_proxy`, `https_proxy` and `no_proxy`" — so `RUN pip install` behind
`egress_proxy` needs no special handling beyond what Docker already does for
every other `RUN` step in this image.

---

## Could not verify / flagged

- Whether httpx 0.28.1 and httpcore 1.0.9 are actually exercised by
  upstream CI on Python 3.14 — their PyPI classifiers don't list it (only
  `requires_python`'s unbounded floor permits it); recommend confirming with
  the image's own test run rather than trusting metadata alone (§1).
- vLLM v0.27.1's own bundled Starlette/uvicorn versions inside its published
  image were not pulled/inspected in this pass — §5's disconnect analysis
  confirms the *class* (`fastapi.responses.StreamingResponse`, a Starlette
  subclass) and the engine-core abort handler, not vLLM's own exact
  spec_version pairing; not needed for `gideon-api`'s own stack, flagged for
  completeness only.
- Whether `inputs_digest`'s current computation (base digest + build args +
  Dockerfile bytes) would need to be extended to cover a hash-locked
  requirements file if option (b) in §7 is chosen — out of this note's scope
  (a `tools.imagebuild`/lock-schema question, not an upstream-source one).
- The exact resolved wheel URLs/hashes for the ten packages in §1 at their
  current versions were not individually captured (only the index/host
  behaviour was verified) — whichever of §7's three shapes is chosen would
  need to generate its own hash list at that time via `pip hash` or
  `pip download --require-hashes`'s own hash-printing behaviour.

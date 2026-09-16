---
verified_against:
  - pin: models.generator
    version: 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a
---

# Hugging Face Hub HTTP API facts for a model pin watch

Scope: what the pin watch needs to (a) notice a pinned model repo's default
branch has moved past the pinned revision, and (b) rebuild, from the Hub
alone with `urllib` and no `huggingface_hub` client, the per-file
`{path: {sha256, size}}` record `models.lock` keeps at a revision (leading-dot
paths excluded, per `models.lock`'s `generator` pin and
`.scratch/slice-1/issues/01-models-lock-hardware-profile.md`'s 2026-09-04
ruling).

Every HTTP fact below was fetched live against `Qwen/Qwen3.8-27B-FP8` on
2026-09-09 (dates/times UTC as returned by the Hub's `Date` header). At fetch
time the repo's current `sha` **is** `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`
— the exact revision `models.lock` pins today — so this note verifies the
pinned revision against the Hub's live listing at that same commit; there is
no drift to report. Library claims are checked against `huggingface_hub`
tag `v1.30.0` (commit `48ef2781c2c4c2247431c97efcd5487e89d42732`), files read
at that tag via `raw.githubusercontent.com`. The Hub's endpoint-level
documentation now lives in an OpenAPI spec (`huggingface.co/docs/hub/api`
redirects readers to it); I read `https://huggingface.co/.well-known/openapi.json`
and `.../openapi.md` directly. **Two of the endpoints this note relies on —
plain `GET /api/models/{repo}` and `GET /api/models/{repo}/revision/{rev}` —
do not appear in that OpenAPI document at all** (confirmed by exhaustively
listing every `/api/models/...` path in the spec: `tree`, `treesize`,
`lfs-files`, `commits`, `refs`, `compare`, `paths-info`, `preupload`,
`xet-*-token`, `commit`, `tag`, `branch`, `resource-group`, `super-squash`,
`settings`, `notebook`, `scan`, `jwt`, `user-access-request/*` — no bare
`{namespace}/{repo}` or `.../revision/{rev}` GET). For those two, the only
primary sources are the `huggingface_hub` library's own request-construction
code (`HfApi.model_info`, which builds and calls exactly this URL) and the
live server responses I captured. This is flagged, not worked around.

## 1. `GET /api/models/{repo}` and `blobs=true`

Confirmed live (`curl https://huggingface.co/api/models/Qwen/Qwen3.8-27B-FP8`
and again with `?blobs=true`, response headers and bodies saved) and against
`huggingface_hub`'s construction of the call:

- **`sha`**: the current commit hash of the repo's default branch (normally
  `main`) — i.e. exactly what `git rev-parse main` would give. Confirmed by:
  (a) the `HfApi.model_info` docstring's `RevisionNotFoundError` note, (b) the
  `revision` parameter — passing none targets the default branch — and (c)
  live cross-check: `/api/models/Qwen/Qwen3.8-27B-FP8` and
  `/api/models/Qwen/Qwen3.8-27B-FP8/revision/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`
  return byte-identical `sha`/`lastModified`/`siblings`, and a `/commits/main`
  call (below) shows `017b9c7a…` as `main`'s newest commit. This is the field
  the pin watch diffs against `models.lock`'s `revision` to detect drift: a
  newer default-branch head is `sha != models.lock`'s pinned revision.
  (`huggingface_hub` source: `hf_api.py:3263-3335`, `v1.30.0`; live response.)
- **`lastModified`**: an ISO-8601 timestamp (`"2026-08-14T14:44:41.000Z"` for
  the pinned commit), equal to the pinned commit's own timestamp from
  `/api/models/{repo}/commits/main` (`"date": "2026-08-14T14:44:41.000Z"` for
  commit `017b9c7a…`). It is the head commit's timestamp, not a
  last-file-touched or last-API-cache timestamp. (Live cross-check.)
- **`siblings[]` without `blobs=true`**: each entry carries **only**
  `rfilename` — no `size`, no `blobId`, nothing else. Confirmed live: every
  one of the 81 siblings (`.gitattributes`, `LICENSE`, `README.md`,
  `chat_template.jinja`, `config.json`, …) came back as `{"rfilename": "..."}`
  and nothing more, for LFS and non-LFS files alike.
- **`siblings[]` with `blobs=true`**: each entry gains `blobId` (the git blob
  SHA-1 of that path at this commit — for a non-LFS file this is the hash of
  its actual bytes under git's `blob <size>\0<content>` framing; for an
  LFS-tracked file it is the blob hash of the *LFS pointer text file*, not the
  real content) and `size` (the **real** file size — for an LFS file this is
  the size of the actual large object, already dereferenced past the
  pointer). LFS-tracked entries additionally carry an `lfs` object:
  `{"sha256": "<64-hex>", "size": <int>, "pointerSize": <int>}`. Verified
  live, e.g. for `layers-0.safetensors`:
  `{"rfilename": "layers-0.safetensors", "blobId":
  "2414184f64bda500ff7d0997c6a5e1a5bf69f84f", "size": 383865448, "lfs":
  {"sha256": "07f700e293baeaf3cd4240c3df1a948c4403f16961ea7979e86c8d6a9f8fd466",
  "size": 383865448, "pointerSize": 134}}`. **`lfs.sha256` is exactly the LFS
  object id — the sha256 of the file's real bytes** — this is the value that
  goes straight into `models.lock`'s `files.<path>.sha256` for every LFS
  file, no download needed. Non-LFS entries never get an `lfs` object or any
  other sha256 anywhere in this response — only `blobId` (a sha1 of framed
  git-blob bytes, not usable as `models.lock`'s sha256). Confirmed by
  inspecting every one of the 14 non-LFS entries in the live response.

## 2. `GET /api/models/{repo}/revision/{revision}`

Live-confirmed to exist and to accept a full 40-hex commit hash: the same
document as endpoint 1, scoped to that historical commit rather than the
default branch, and it accepts `?blobs=true` the same way (verified: `sha`,
`lastModified`, and all 81 `siblings` came back identical to the un-scoped
call, since the pinned revision *is* the current default-branch head). Also
confirmed the path accepts a short (8-hex) prefix of the hash and resolves it
(`.../revision/017b9c7a` → `200`, `"sha":
"017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"` in the body) — useful only as a
convenience; the watch should always send and record the full 40-hex hash.
`huggingface_hub` builds this URL as
`f"{endpoint}/api/models/{repo_id}/revision/{quote(revision, safe='')}"`
(`hf_api.py:3320-3324`, `v1.30.0`) — note `revision` is URL-quoted, which
matters if a branch/tag name (not used by this watch) contained `/`.
**Not in the OpenAPI spec** — see the flag above; this endpoint's only
primary sources are the library's call-construction code and this note's live
capture.

## 3. `GET /api/models/{repo}/tree/{revision}?recursive=true` — the right listing

This is **the correct primary source for "every regular file at a revision,"
not endpoint 1/2's `siblings`**, for three reasons converging on the same
answer:

- It is what `HfApi.list_repo_tree` calls (`hf_api.py:3978-4107`, `v1.30.0`);
  `siblings`/`blobs=true` is `model_info`'s convenience duplicate of the same
  data for repos small enough that pagination doesn't matter (see below), but
  `tree` is the paginated, purpose-built listing endpoint and is what the
  library itself documents as the way to "list a repo tree's files and
  folders."
- It, unlike `siblings`, explicitly distinguishes files from directories via
  a `type` field, so filtering to real files is a documented `type == "file"`
  check rather than an inference from key presence.
- It is confirmed by the OpenAPI spec (`/api/models/{namespace}/{repo}/tree/{rev}/{path}`,
  read from `openapi.json`) with parameters `recursive` (bool, "returns the
  tree recursively"), `expand` (bool, "returns associated commit data for
  each entry and security scanner metadata"), `limit`, and `cursor`.

**Fields per entry** (live, `recursive=true`, no `expand`):
`{"type": "file"|"directory", "oid": "<sha1>", "size": <int>, "path":
"<relpath>", "lfs": {"oid": "<sha256>", "size": <int>, "pointerSize": <int>}}`
(the `lfs` key present only for LFS-tracked files). **Note the key-name
inconsistency against endpoint 1/2**: here the LFS sha256 is under
`lfs.oid`, not `lfs.sha256` — confirmed by direct inspection of the raw JSON
(`{"oid":"2414184f...","size":383865448,"lfs":{"oid":"07f700e293baeaf3...","size":383865448,"pointerSize":134}...`)
for the same file whose `model_info?blobs=true` entry used `lfs.sha256` for
the identical value. `huggingface_hub`'s own `RepoFile`/`BlobLfsInfo` dataclass
normalizes this for its callers (its docstring example shows the *normalized*
shape, `lfs={'size':..., 'sha256':..., 'pointer_size':...}`) — a `urllib`-only
watch reading the raw tree JSON must read `entry["lfs"]["oid"]`, not
`entry["lfs"]["sha256"]`, or it will get a `KeyError`/`None`. This repo's tree
entries also carry an Xet-specific `xetHash` field (`§4` explains why) that
`models.lock` does not need.

**Directories**: `recursive=true` does **not** drop directory entries — I
verified this against `prompthero/openjourney-v4` (which has real
subfolders): `?recursive=true` returned entries with `"type": "directory"`
*and* `"type": "file"` intermixed (e.g. `feature_extractor` as a directory
entry with `size: 0` and a nested `feature_extractor/preprocessor_config.json`
as a file entry). **The watch must filter `type == "file"`** — it is not
optional or already applied by `recursive=true`. Nested paths are single
tree entries whose `path` contains the full slash-joined relative path (no
per-segment entries).

**Pagination**: a `Link: <url>; rel="next"` response header, RFC 8288 style,
observed live:
`link: <https://huggingface.co/api/models/Qwen/.../tree/017b9c7a.../?expand=false&recursive=true&limit=5&cursor=ZXlKbW...>; rel="next"`
when a `limit` smaller than the result count is given. The `cursor` value is
an opaque base64url blob (undocumented internal shape — do not decode it,
just round-trip it as the next request's `cursor` query param). The default
page size is **documented as 1,000 without `expand`, 100 with `expand=true`**
(OpenAPI `openapi.json`, the `limit` parameter's description: `"1.000 by
default, 100 by default for expand=true"`) — this conflicts with
`huggingface_hub`'s own `list_repo_tree` docstring, which says expand mode
returns "only 50 results ... per page (instead of 1000)" (`hf_api.py:4000-4004`,
`v1.30.0`). **Flagged, unresolved**: I could not force a small enough
`expand=true` result set to observe which of 50/100 is the real default;
the watch should not hardcode either number and must always follow `Link`
until it is absent, using `recursive=true` without `expand` (the default
1,000/page value is uncontested and this repo's 81 entries fit in one page
with no `Link` header at all when `limit` is omitted — confirmed live).
`tools/pinwatch/oci.py`'s `_NEXT_LINK` regex
(`<([^>]+)>\s*;[^,]*\brel\s*=\s*"?next"?\b`) already parses exactly this
`Link` shape for the OCI registry and is reusable verbatim for the Hub's tree
pagination.

**`expand=true`**: adds `lastCommit` (`{"id", "title", "date"}` — the commit
that last touched that path) and `securityFileStatus` (malware/pickle/AV scan
results) per entry — verified live. Neither is needed to rebuild
`models.lock`'s record (path, sha256, size); the watch should not pass
`expand=true`, both because it adds nothing it needs and because its smaller,
disputed page size (see above) means more round trips for no benefit.

## 4. Downloading bytes: `resolve/{revision}/{path}` — redirects, headers, no-sha256-for-non-LFS confirmed

**Non-LFS file** (`config.json`, live): `GET
.../resolve/017b9c7a.../config.json` → `307 Temporary Redirect` to a
*same-host, relative* `Location: /api/resolve-cache/models/Qwen/.../config.json?...`
which itself resolves to `200` with the file's raw bytes
(`content-type: text/plain`, `content-length: 51350`). A plain
`urllib.request.urlopen()` **does follow this automatically** (`urlopen`
follows same-scheme redirects including relative `Location` values by
default) and lands on the real content — verified by downloading through
plain `urllib.request.urlopen(Request(url, method="HEAD"))`... equivalent
follow-through with `curl -L`, and independently by computing hashes of the
downloaded bytes.

**LFS file** (`layers-0.safetensors`, live, this specific repo is
**Xet-backed**, not plain S3/CloudFront LFS): `GET
.../resolve/017b9c7a.../layers-0.safetensors` → `302 Found` to
`https://us.aws.cdn.hf.co/xet-bridge-us/<repo-internal-id>/<xetHash>?X-Xet-Cas-Uid=public&...&Signature=...` —
a different host, a pre-signed URL. `urllib.request.urlopen()` follows
cross-host redirects too (also verified live: final `.geturl()` lands on
`us.aws.cdn.hf.co`), so plain `urllib` **is sufficient to fetch the actual
bytes** for both storage backends without special-casing. (The classic,
pre-Xet form for a plain-LFS repo redirects instead to
`cdn-lfs.huggingface.co` or a regional `cdn-lfs-us-1.huggingface.co` —
documented behavior per `hf_hub_url`'s docstring, "The resolved address can
either be a huggingface.co-hosted url, or a link to Cloudfront ... for large
files" (`file_download.py:212-267`, `v1.30.0`); I could not find a
non-Xet-backed LFS repo to confirm that exact hostname live, so that specific
hostname is carried from the library docstring, not directly observed this
session — flagged.)

**Response headers, and the central finding for §4 — confirmed, not
refuted**: the `Authorization`-bearing `huggingface.co` hop (the 307/302
response itself, *before* following to the CDN) carries:
`X-Repo-Commit: <commit-hash>`, `ETag`/`X-Linked-Etag`, `X-Linked-Size`, and
(for non-LFS, on the *final* 200 rather than the redirect) a plain `ETag`.
Concretely, live for `config.json`: the 307 carried
`x-linked-etag: "e66d0dcb2d8066e724407f0ded85b6c3c5cb213a"` — **and this is
the file's git blob SHA-1, not a sha256**. Proof: I downloaded the 51,350
content bytes and computed `sha1(b"blob 51350\x00" + content)` myself =
`e66d0dcb2d8066e724407f0ded85b6c3c5cb213a` — an exact match to both the
`ETag`/`X-Linked-Etag` value and to `oid` from the `/tree` endpoint (§3) for
the same path. The plain `sha1(content)` (no git framing) and `sha256(content)`
both differ from this. **This confirms the expected answer: no header
carries a sha256 for a non-LFS file; the ETag/X-Linked-Etag is the git blob
sha1, so the watch must download and hash non-LFS files itself** — exactly
as `.scratch/slice-1/issues/01-models-lock-hardware-profile.md`'s 2026-09-04
comment describes doing by hand. This matches `hf_hub_url`'s own docstring
verbatim: *"An object's ETag is: its git-sha1 if stored in git, or its
sha256 if stored in git-lfs"* (`file_download.py:260-262`, `v1.30.0`).

For the LFS file, the `huggingface.co` hop's headers (captured via `curl -sI`,
i.e. without following the redirect) were:
`x-linked-etag: "07f700e293baeaf3cd4240c3df1a948c4403f16961ea7979e86c8d6a9f8fd466"`
(= the LFS sha256, matching §1/§3's `lfs.sha256`/`lfs.oid`) and
`x-linked-size: 383865448`. **Important trap for a `urllib`-only
implementation that lets redirects run to completion**: once you follow all
the way to the Xet CDN host, that terminal response's own `ETag` is
`"94cb43e366d97765e4e950cfabd3ea29445cf28d824f3f2a73dcabf13dfb4854"` — the
tree's `xetHash`, a **different** value from the LFS sha256
(`07f700e2...`). `huggingface_hub` avoids this trap deliberately: its
`get_hf_file_metadata`/`_httpx_follow_hub_redirects_with_backoff` follows
redirects *only while they stay on the same or a known Hub host*, and stops
at the first redirect to any other host, reading `X-Repo-Commit`,
`X-Linked-Etag`, `X-Linked-Size` off *that* response — with the comment
*"Redirects to any other host (CDN, storage bucket) are not [followed]: the
file metadata is on the redirect response itself and the auth header must
not leave the Hub"* (`utils/_http.py:697-746`, `v1.30.0`). **Consequence for
this watch**: it never needs to inspect resolve-redirect headers at all for
sha256 purposes — §1/§3 already hand it every LFS file's sha256 directly in
the JSON body, no HEAD/redirect dance required — but if it ever does a HEAD
against `resolve/`, it must read the headers of the *first* redirect
response, not follow to the terminal CDN host, exactly per that library
comment.

## 5. Anonymous access, rate limits, gated/private response shape

**Rate limits** — confirmed both from the primary docs page
(`https://huggingface.co/docs/hub/rate-limits`) and by reading my own live
response headers, which matched exactly: anonymous (per-IP) limits are
**500 requests / 5 minutes for the "api" bucket** and **3,000 requests / 5
minutes for the "resolvers" bucket** (`GET .../resolve/...` and the
tree/model-info calls this note relies on land in "api"; only
`.../resolve/...` is "resolvers"). Live headers matched the documented
policy string format exactly:
`ratelimit: "api";r=485;t=163` / `ratelimit-policy: "fixed window";"api";q=500;w=300`
for `/api/models/...` calls, and
`ratelimit: "resolvers";r=2989;t=109` / `ratelimit-policy: "fixed window";"resolvers";q=3000;w=300`
for `/resolve/...` calls — `q` = quota for the window, `w` = window seconds,
`r` = remaining, `t` = seconds to reset. **On exceeding the limit**: the docs
page states a **`429 Too Many Requests`**, using the IETF
`draft-ietf-httpapi-ratelimit-headers` (draft 9) `RateLimit`/`RateLimit-Policy`
headers shown above — **no traditional `Retry-After` header**; the watch (or
anything polling this API) should compute its own backoff from `RateLimit`'s
`t=` value rather than expect `Retry-After`. I did not intentionally trigger
a live 429 (would require exhausting the anonymous quota); this is the
documented shape, not independently reproduced this session — flagged as
docs-only for the 429 body itself, though the headers above were directly
observed on ordinary 200s.

**`Authorization: Bearer <token>`**: standard bearer-token header;
`huggingface_hub` builds it via `_build_hf_headers`/`build_hf_headers`
(referenced throughout `hf_api.py`/`file_download.py`, `v1.30.0`) — not
independently re-derived here since the pin watch's target repo is public
and this note's live testing was entirely anonymous, matching the watch's
own anonymous, unauthenticated design (per `tools/pinwatch/oci.py`'s and
`tools/pinwatch/sources.py`'s `Fetcher.get` pattern, no token is threaded
through for this source either).

**Gated or private repo, anonymous request** — live-tested against a real
gated repo (`meta-llama/Llama-3.2-1B`) and a nonexistent repo name:
- `GET /api/models/{gated-repo}` (metadata, no `blobs=true`) → **`200`**,
  full metadata including `siblings` (filenames only) and
  `"gated": "manual"` — **gated-repo metadata is visible anonymously**; only
  file *content* is blocked.
- `GET {gated-repo}/resolve/main/{file}` anonymously → **`401`**, body
  `"Access to model meta-llama/Llama-3.2-1B is restricted. You must have
  access to it and be authenticated to access it. Please log in."`, header
  `x-error-code: GatedRepo`, `www-authenticate: Bearer realm="Authentication
  required"`.
- `GET /api/models/{nonexistent-or-private-repo}` anonymously → **`401`**,
  body `{"error":"Invalid username or password."}` (a deliberately generic
  message — the Hub does not distinguish "doesn't exist" from "exists but
  private" to an anonymous caller).
- `huggingface_hub`'s own `hf_raise_for_status` comment states this
  explicitly and is the authoritative explanation for why both cases above
  are `401` rather than `404`/`403`: *"401 is misleading as it is returned
  for: - private and gated repos if user is not authenticated - missing
  repos => for now, we process them as `RepoNotFound` anyway"*
  (`utils/_http.py:875-885`, `v1.30.0`). The library's `GatedRepoError`
  docstring example, by contrast, shows a **`403`** — that is the response
  for an *authenticated* user who is known but not (yet) approved for a
  manually-gated repo (`errors.py:331-344`, `v1.30.0`); anonymous access to
  a gated repo's files is `401`, not `403`. **Net answer to the question as
  posed**: it is `401` in both the "doesn't exist/private" and the
  "anonymous-against-gated" cases the watch will actually hit (its target,
  `Qwen/Qwen3.8-27B-FP8`, is public/ungated, so this only matters if the
  pinned repo ever became gated or is renamed/deleted) — `403` only appears
  once a token is involved, which is outside this watch's anonymous design.

## 6. Directories, symlinks, nested paths — answered in §3

Covered above: `tree?recursive=true` **does** list directory entries
(`type: "directory"`) alongside files, so the watch must filter on
`type == "file"`; `siblings` (endpoints 1/2) never lists directories at all
— it is a flat list of file paths only, which is presumably why
`models.lock`'s own file-collection ruling (per the 2026-09-04 comment) reads
naturally as "every regular file", since `siblings` never needed the filter.
Nested paths appear as a single entry with a slash-joined `path`
(`"feature_extractor/preprocessor_config.json"`), confirmed on
`prompthero/openjourney-v4`. **Symlinks**: no `type` value other than
`"file"`/`"directory"` was observed on either repo tested, and I found no
documented third `type`. Hugging Face repos are plain git(+LFS) repos over
HTTP, not filesystem checkouts, so a "symlink" as a tree entry is unlikely to
be a first-class concept server-side — but I did not find or test a repo
containing an actual symlink, so **this is unverified, not confirmed
absent** — flagged.

## 7. `sha` moves on every commit, including content-free ones — confirmed with a direct example from the pinned repo

`GET /api/models/Qwen/Qwen3.8-27B-FP8/commits/main?limit=5` (live,
undocumented in the OpenAPI paths list under this exact form but present as
`/api/models/{namespace}/{repo}/commits/{rev}`) returned, most recent first:
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a` "Update README.md" (2026-08-14T14:44:41Z,
**the pinned `sha`**), then `001718aae4c6e7936ec1999ab8501da0c045af82`
"Update README.md" (2026-08-14T12:58:18Z), then two more "Update README.md
(model card)" commits, then "Add LICENSE". I then pulled
`tree?recursive=true` at both `017b9c7a…` and `001718aa…` and diffed every
entry's `oid` by path: **all 81 paths are byte-identical between the two
commits except `README.md`**. This is a direct, reproducible proof (not an
inference) that a commit is the unit of revision and that a commit touching
only the README moves `sha` with zero change to any model file's hash or
size — exactly the scenario the question asks about, caught in the wild on
the repo this watch actually pins.

## Summary for the implementation

To rebuild `models.lock`'s `files` record for a pinned revision with
`urllib` alone: call `GET /api/models/{repo}/revision/{revision}?blobs=true`
(§1/§2) — for a repo this size (81 files, no pagination triggered) this is
sufficient and simplest; for a much larger repo, walk
`GET /api/models/{repo}/tree/{revision}?recursive=true` (§3) following `Link:
rel="next"` and filter `type == "file"`, reading `lfs.oid` (**not**
`lfs.sha256`, per §3's key-name warning) for LFS files. For every LFS entry,
`sha256`/`oid` and `size` come straight from the JSON — no download. For
every non-LFS entry (`.gitattributes` excluded as `models.lock` already
does), download `https://huggingface.co/{repo}/resolve/{revision}/{path}`
with a plain `urllib.request.urlopen()` (redirects, including cross-host
Xet-CDN ones, are followed automatically and land on real bytes — §4) and
compute `hashlib.sha256` over the downloaded bytes; `len(bytes)` is the size,
cross-checked against the tree/model-info `size` field. To detect drift,
compare `GET /api/models/{repo}` (no revision pinned) `sha` (§1) against
`models.lock`'s `revision`.

## Flags — claims not independently confirmed this session

- The exact pre-Xet CDN hostname (`cdn-lfs.huggingface.co` /
  `cdn-lfs-us-1.huggingface.co`) for a *non-Xet* LFS repo's resolve redirect
  — sourced from `hf_hub_url`'s docstring only; this repo is Xet-backed so I
  could not observe it live (§4).
- The live shape of a `429` response body/headers on this API — sourced from
  `https://huggingface.co/docs/hub/rate-limits` only; not reproduced (§5).
- The true default page size for `tree?...&expand=true` — OpenAPI spec says
  100, the `huggingface_hub` `list_repo_tree` docstring says 50; unresolved
  (§3). Irrelevant if the watch never passes `expand=true`, which this note
  recommends.
- Whether a `tree` entry can ever report `type` other than `file`/`directory`
  (e.g. a symlink) — not found, not ruled out (§6).
- `GET /api/models/{repo}` and `.../revision/{rev}` are absent from the
  current OpenAPI spec entirely; treated here as verified by direct live
  behavior plus the `huggingface_hub` source that constructs them, not by an
  API reference document (noted up top).

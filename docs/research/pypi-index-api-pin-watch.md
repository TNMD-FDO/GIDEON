---
verified_against:
  - pin: images.gideon.build_args.STARLETTE_VERSION
    version: "1.6.0"
---

# PyPI's Index API for a `pypi_project` pin watch

Scope: ticket `.scratch/general-turn/issues/10-pypi-watch-kind.md` (ruled
`ready-for-agent` 2026-09-19, `git show b2636bf`) needs a resolver that reads
**one fact** from PyPI over plain `urllib`, standard-library-and-PyYAML-only,
matching `hf-hub-model-api-pin-watch.md`'s rigor: the newest stable release
of a named project — stable meaning a PEP 440 final or post release, never a
pre-release or dev release, not fully yanked, with at least one file — read
from the **Index API** (PEP 691 JSON), not the legacy JSON API's `releases`
key. Every HTTP fact below was fetched live on **2026-09-19** (UTC, as
returned by each reply's `Date` header); every spec claim is cited to the PEP
or PyPA specification page that owns it, read the same day. The front matter
above binds this note to `images.gideon.build_args.STARLETTE_VERSION` — the
only PyPI-watched build argument ticket 01 will create — at the newest
stable version this note itself read live for starlette. That id is not yet
in the pin registry's vocabulary (`python3 -m tools.pinwatch.notes` will not
list it until ticket 01 lands the `gideon` image's `*_VERSION` build
arguments), so `tests/test_research_notes.py` would refuse this note on
`main` today; it is deliberately left on `research/pypi-index-api-pin-watch`
until ticket 01's release folds it in, the same arrangement
`research/gideon-api-http-stack` already uses. The other nine projects' newest
stable versions, read the same way the same day, are in §6's table for that
release to cross-check against.

**Headline answers** (detail below): the candidate attribution rule held for
all ten projects, zero failures, zero mis-attributions, zero file-less listed
versions, across 1,364 files and 864 listed versions. `versions` *can*
formally list a file-less version per PEP 700's spec text, but none of the
ten sampled projects has one. PyPI's live reply for a genuinely
currently-quarantined project could not be found without a long hunt (every
quarantined-then-restored or quarantined-then-removed candidate tried had
reverted to `active` or 404'd); PEP 792's own documented example is cited
instead and flagged as not independently live-confirmed. PEP 792's prose and
its own worked example disagree on the JSON key name (`state` vs `status`);
the live reply matches the example (`status`), which is flagged and treated
as authoritative.

---

## 1. The Index API's JSON project-detail reply

### 1.1 URL shape, content negotiation, trailing slash, name normalization

Confirmed live against all ten projects and against edge cases:

- **URL**: `https://pypi.org/simple/<project>/` — the JSON form of the URL
  PEP 503 §"Specification" defines (`/<project>/` below the `/simple/` base;
  PEP 503, "the format of this URL is `/<project>/`"). A request without the
  trailing slash 301-redirects to the slash form
  (`curl -s -D - https://pypi.org/simple/starlette` → `301`,
  `location: https://pypi.org/simple/starlette/`), matching PEP 503's "All
  URLs which respond with an HTML5 page MUST end with a `/` and the
  repository SHOULD redirect the URLs without a `/`".
- **Accept header**: `Accept: application/vnd.pypi.simple.v1+json` gets the
  JSON reply (`content-type: application/vnd.pypi.simple.v1+json`, confirmed
  on all ten). **No `Accept` header at all gets the legacy PEP 503 HTML page**
  (`content-type: text/html`, `200`) — confirmed live
  (`curl -s -D - https://pypi.org/simple/starlette/`, no `-H`). This matches
  PEP 691's content-negotiation rule that an absent `Accept` header is treated
  as `Accept: */*`, and PyPI's server-side default choice for `*/*` is the
  HTML representation, not JSON. **A watch that omits the `Accept` header
  will silently get HTML, not an error** — this is the one footgun in §1 a
  `urllib`-only client must not hit.
  (PEP 691 §"Version + Format Selection": "treating the absence of an Accept
  header as `Accept: */*`".)
- **`?format=` URL parameter**: PEP 691 §"URL Parameter" documents an
  optional `format` query parameter as an alternative to content negotiation
  for "easier human based exploration ... or to allow documentation or notes
  to link to a specific version+format." Confirmed live: `curl -s -D -
  "https://pypi.org/simple/starlette/?format=application/vnd.pypi.simple.v1+json"`
  (no `Accept` header at all) → `200`,
  `content-type: application/vnd.pypi.simple.v1+json`, valid JSON body. PyPI
  honours it. Not needed by the watch (a plain `Accept` header suffices and
  is what PEP 691 recommends clients use), but confirmed working as a
  fallback.
- **Name normalization (PEP 503 §"Normalized Names")**: `normalize(name) =
  re.sub(r"[-_.]+", "-", name).lower()`. Confirmed live: `STARLETTE`,
  `Typing_Extensions` (mixed separators and case) both 301-redirect to the
  normalized URL (`location: https://pypi.org/simple/starlette/` /
  `.../typing-extensions/`) rather than answering `200` directly. **This
  contradicts the task's premise that "the normalized name answers 200
  directly, and [ask] what a non-normalized name gets"** — on PyPI today,
  even an *already-normalized* casing test wasn't tried against a name that
  needed no change, but every non-normalized spelling tried (all-caps,
  underscore) got a 301, never a 200 with different content. PEP 503 itself
  only says a repository "MAY redirect unnormalized URLs to the canonical
  normalized URL ... however clients MUST NOT rely on this redirection and
  MUST request the normalized URL" — so the watch MUST normalize the project
  name itself (PEP 503's algorithm, stdlib `re`) before building the request
  URL, never depend on the redirect.
- **Unknown project**: `404`, body `404 Not Found` (13 bytes,
  `content-length: 13`), `content-type: text/plain; charset=UTF-8`. Not
  JSON. A `urllib`-only resolver must catch the `HTTPError` (status 404) and
  turn it into the run's failed row rather than trying to `json.loads` the
  body.

### 1.2 Every top-level and per-file key, and which PEP defines it

Read live from starlette (304 files, 202 versions) and cross-checked against
the other nine and against docs.pypi.org's own worked example
(`https://docs.pypi.org/api/index-api/`, read 2026-09-19, `beautifulsoup4`
example reply). Top-level keys observed: `files`, `meta`, `name`,
`project-status`, `versions`.

| Key | Defining PEP | Notes |
|---|---|---|
| `meta.api-version` | PEP 629 (format), required by PEP 691 | String, `"1.4"` on every reply read today (all ten projects and `docs.pypi.org`'s worked examples agree). |
| `meta._last-serial` | PEP 691 (`meta` is "information related to the response itself") | Not itself PEP-named per key, but present on every reply; matches `X-PyPI-Last-Serial` response header. |
| `name` | PEP 691 §"Project Detail" | "The normalized name of the project." Confirmed always PEP 503-normalized in the ten replies read. |
| `files` | PEP 691 §"Project Detail" | List of per-file dicts, order not significant (PEP 691: "neither PEP 503 nor this PEP requires any specific ordering"). |
| `files[].filename` | PEP 691 | — |
| `files[].url` | PEP 691 | Absolute in every reply observed. |
| `files[].hashes` | PEP 691 | Dict, always present (PEP 691: "The hashes dictionary MUST be present, even if no hashes are available"); every file in every one of the ten carried at least `sha256`. |
| `files[].requires-python` | PEP 691 (JSON name for PEP 503's `data-requires-python`, itself PEP 345) | `null` or a specifier string; no escaping needed in JSON (PEP 691 explicitly contrasts this with the HTML attribute's escaping requirement). |
| `files[].core-metadata` | PEP 714 (renames PEP 658's `dist-info-metadata` key) | `false` or a hash dict. |
| `files[].data-dist-info-metadata` | PEP 658 (JSON name), superseded by PEP 714's `core-metadata` | **Live observation, not spec-mandated**: PyPI still emits this legacy key too, with an identical value, on every file of every one of the ten projects (e.g. starlette `starlette-0.13.1-py3-none-any.whl`: both keys `{"sha256": "1719…"}`, identical hash). PEP 714's own non-normative "Recommendations" section says "Servers should not emit the old keys in JSON unless they know that no broken versions of pip will be used" — PyPI's live behavior does not follow that recommendation (it is a recommendation, not a MUST, so this is not a spec violation, just worth flagging: a resolver should read `core-metadata` only, per PEP 714 clients' MUST, and ignore `data-dist-info-metadata` if present). |
| `files[].gpg-sig` | PEP 691 | Not observed as present on any of the ten's files (optional; PyPI's GPG-signature support was deprecated separately and is out of this note's scope). |
| `files[].yanked` | PEP 691 (mechanism) + PEP 592 (semantics) | `false`, or a non-empty string reason. See §4. |
| `files[].size` | PEP 700 (mandatory) | Integer, bytes. |
| `files[].upload-time` | PEP 700 (optional) | ISO 8601 `yyyy-mm-ddThh:mm:ss.ffffffZ`; present on every file observed. |
| `files[].provenance` | PEP 740 | `null` or a URL string to a provenance (attestation) file (PEP 740 §"JSON-based Simple API": "The value of the provenance key SHALL be either a JSON string or `null`"). Observed `None`/URL string, never absent, on every file of every project read. |
| `versions` | PEP 700 | See §1.3. |
| `project-status` | PEP 792 | See §1.4. |

### 1.3 `versions` (PEP 700)

- **Definition**: PEP 700 §"Versions": `versions` "MUST contain a list of
  version strings specifying all of the project versions uploaded for this
  project. The value is logically a set... All of the files listed in the
  `files` key MUST be associated with one of the versions in the `versions`
  key. **The `versions` key MAY contain versions with no associated files**
  (to represent versions with no files uploaded, if the server has such a
  concept)."
- **Can it list a file-less version — yes per spec, not observed live**: PEP
  700 explicitly permits it (quoted above). §2's attribution run over all ten
  projects found **zero** `versions` entries with no attributed file — every
  one of 864 listed versions across the ten got at least one file. This is
  consistent with how a version normally reaches PyPI (an upload always
  creates a file), so a file-less version would need some other mechanism
  (e.g. a deleted-but-not-fully-purged release, or a future server concept
  PEP 700 leaves unspecified) that none of these ten happened to hit. **A
  resolver MUST still treat a listed version with zero files as "not a
  candidate release"** (the ticket's ruling (d): stable "has at least one
  file"), since the spec allows the case even though this sample never
  produced one.
- **Normalized or as-uploaded?**: PEP 700: "servers may hold 'legacy' data
  from before the adoption of PEP 440, version strings currently cannot be
  required to be valid PEP 440 versions... However, servers SHOULD use
  normalised PEP 440 versions where possible." Live: certifi's `versions`
  list carries `14.05.14` and `2015.04.28` — both PEP 440-**valid** but not
  in **normalized** form (a normalizing parser reduces `05`→`5`, `04`→`4`;
  PEP 440 §"Normalization", "Integer Normalization"). All other listed
  entries across all ten projects were already in normalized form (see §2's
  per-project `versions not in PEP440-normalized form` output — empty for
  nine of ten, these two for certifi). So: **no**, `versions` entries are not
  guaranteed normalized; a resolver comparing or displaying them must
  normalize before comparing, and if it proposes the value verbatim
  (§3's recommendation) it will sometimes propose an un-normalized string
  like `14.05.14` — which is fine, because that is the literal, installable
  string PyPI stored (see §3).
- **Is every file's version guaranteed to appear in `versions`?**: Per spec,
  yes ("All of the files listed in the `files` key MUST be associated with
  one of the versions"); confirmed live for all ten (§2: 100% of files
  attributed to a listed version, zero unattributable).

### 1.4 `project-status` (PEP 792)

- PEP 792 defines four statuses: `active` (default, no semantics, index
  allows uploads and downloads), `archived` (no new uploads; existing
  distributions still downloadable), `quarantined` (no new uploads, **index
  MUST NOT offer any distributions for download** — the entire `files` list
  is emptied), `deprecated` (same index semantics as `active`).
- **JSON shape, per PEP 792 §"JSON index"**: `project-status.status` holds
  the marker; `project-status.reason` optionally holds free text; the key is
  omitted entirely if the project is `active` and no reason is given. Live:
  every one of the ten replies carried `"project-status": {"status":
  "active"}` (i.e. PyPI chose to include the marker explicitly even for the
  default `active` status, rather than omitting the key as the "MAY choose to
  omit" language permits).
- **Flagged inconsistency inside PEP 792 itself**: the PEP's prose says "The
  per-project index SHOULD include a `project-status.state` key" (note:
  `state`), but the PEP's own worked JSON example two paragraphs later, and
  the live PyPI reply read today, both use `project-status.status` (note:
  `status`). The implemented and observed key is `status`; `state` appears
  to be a documentation typo in the PEP's prose. A resolver must read
  `status`.
- **Quarantined-project reply — not independently live-confirmed**: PEP 792's
  own worked example (§"JSON index") shows, for a hypothetical quarantined
  `sampleproject`:
  ```json
  {
    "meta": {"api-version": "1.4"},
    "project-status": {"status": "quarantined", "reason": "the project is haunted"},
    "alternate-locations": [],
    "files": [],
    "name": "sampleproject",
    "versions": ["1.2.0", "1.3.0", "1.3.1", "2.0.0", "3.0.0", "4.0.0"]
  }
  ```
  i.e. `versions` is left populated (history preserved) while `files` is
  emptied entirely, per the "index MUST NOT offer any distributions" rule.
  I tried to find a project that is *currently* quarantined on live PyPI to
  confirm this shape (searched news/blog coverage of recent PyPI malware
  incidents — `litellm`, `pytorch-lightning`, `helloharry123p`,
  `mcp-quarantine`, `sisaws`, `secmeasure` — and fetched each live): every
  quarantine-adjacent name either had already reverted to `{"status":
  "active"}` (the incident was resolved and the project restored) or 404'd
  (fully removed rather than left quarantined — PyPI's quarantine blog post,
  `https://blog.pypi.org/posts/2024-12-30-quarantine/`, read 2026-09-19,
  says of ~140 quarantined projects to date "only a single project has
  exited Quarantine, others have been removed", i.e. quarantine is usually a
  short-lived state before deletion, not a stable one to sample). **This is
  flagged as unverified live**; the PEP's own example is the best available
  primary source for the shape, and a resolver's "at least one file" /
  "not fully yanked" gates would correctly skip a quarantined project anyway
  (`files: []` yields no candidate, independent of whether `project-status`
  is even inspected).
- **`meta.api-version` obligations (PEP 629, extended to JSON by PEP 691)**:
  PEP 629 §"Clients": "When encountering a major version greater than
  expected, clients MUST hard fail... When encountering a minor version
  greater than expected, clients SHOULD warn." PEP 691 §"JSON Serialization":
  "meta.api-version key, which will be a string that contains the PEP 629
  Major.Minor version number, with the same fail/warn semantics as defined
  in PEP 629." Today's replies are all `"1.4"` (PEP 792's version). A
  resolver built against "major 1" tolerates any `"1.x"` and should log
  (not crash) on an unrecognized minor; it MUST refuse to parse (hard fail)
  a future `"2.x"` reply as if it were `"1.x"`.

### 1.5 Caching, rate limiting, User-Agent

From `https://docs.pypi.org/api/` §"API policies" ("Introduction", read
2026-09-19), quoted:

> **Caching**: All API requests are cached. Requests to the JSON, RSS or
> Index APIs are cached by our CDN provider. You can determine if you've hit
> the cache based on the `X-Cache` and `X-Cache-Hits` headers... Requests...
> also provide an `ETag` header. If you're making a lot of repeated
> requests, ensure your API consumer will respect this header...
>
> **Rate limiting**: Due to the heavy caching and CDN use, there is
> currently no rate limiting of PyPI APIs at the edge... If you plan to make
> a lot of requests..., adhere to these suggestions: Set your consumer's
> `User-Agent` header to uniquely identify your requests... Try not to make
> a lot of requests (thousands) in a short amount of time (minutes)...

No documented hard rate limit; a Monday, ten-project (or later, larger)
resolver run is trivially inside "not thousands of requests in minutes."
`ETag` and `Cache-Control: max-age=600, public` were present on every reply
read (confirmed live, `starlette.headers` etc. in this note's scratch
fetches); re-fetching the same project a few seconds later returned the
identical `ETag`. The watch does not currently do conditional (`If-None-Match`)
requests and doesn't need to for a once-a-week run, but could cheaply add one
later per this documented contract.

**Default `urllib` User-Agent — confirmed live, exact string captured**: PyPI
docs only *recommend* setting a custom `User-Agent`; they do not require one.
Live test (`http.client.HTTPConnection.debuglevel = 1` around
`urllib.request.urlopen`, `python3 --version` → `3.14.4`):

```
send: b'GET /simple/starlette/ HTTP/1.1\r\nAccept-Encoding: identity\r\nHost: pypi.org\r\nUser-Agent: Python-urllib/3.14\r\nAccept: application/vnd.pypi.simple.v1+json\r\nConnection: close\r\n\r\n'
```
→ `200`, full valid JSON reply. The exact default value is generated by
`AbstractHTTPHandler.do_open` (`Lib/urllib/request.py`, cpython tag `v3.13.7`,
line 398: `client_version = "Python-urllib/%s" % __version__`; `__version__ =
'%d.%d' % sys.version_info[:2]` at line 135) — only added when the caller
hasn't already set a `User-Agent` header. PyPI serves it without complaint;
following the recommendation (a distinctive UA identifying the watch) is
possible but not required for correctness.

### 1.6 Reply sizes, 2026-09-19

| Project | Reply bytes (`Content-Length`) |
|---|---|
| starlette | 154,334 |
| uvicorn | 149,861 |
| httpx | 74,245 |
| httpcore | 61,126 |
| h11 | 13,349 |
| anyio | 76,209 |
| idna | 38,802 |
| certifi | 75,538 |
| click | 67,410 |
| typing-extensions | 65,818 |

---

## 2. Attributing a file to a version from its filename, stdlib only

### 2.1 The specs

- **Wheel filename grammar** (`packaging.python.org/en/latest/specifications/binary-distribution-format/`,
  read 2026-09-19, §"File Format" → "File name convention"): the wheel
  filename is `{distribution}-{version}(-{build tag})?-{python
  tag}-{abi tag}-{platform tag}.whl`. §"Escaping and Unicode": "As the
  components of the filename are separated by a dash (`-`, HYPHEN-MINUS),
  this character cannot appear within any component. This is handled as
  follows: In distribution names, any run of `-_.` characters... should be
  replaced with `_`... Version numbers should be normalised according to the
  Version specifier specification. **Normalised version numbers cannot
  contain `-`.**" So a wheel's version segment is always the exact text
  between the (escaped, dash-free) name segment and the next dash.
- **Sdist filename / PEP 625** (`.../specifications/source-distribution-format/`,
  read 2026-09-19, §"Source distribution file name"): "The file name...
  must be in the form `{name}-{version}.tar.gz`, where `{name}` is
  normalised according to the same rules as for binary distributions..., and
  `{version}` is the canonicalized form of the project version." Also: "Code
  that processes source distribution files MAY recognise source distribution
  files by the `.tar.gz` suffix **and the presence of precisely one hyphen**
  in the filename... [and] use the distribution name and version from the
  filename without further verification" — i.e. the spec's own recognition
  heuristic assumes an unambiguous single-hyphen split once the name is
  underscore-normalized, which is exactly what `packaging`'s and pip's own
  parsers (§2.2) do differently for legacy vs. modern names.
- **When PyPI began enforcing PEP 625 on new sdist uploads**: `pypi/warehouse`
  PR #18924, "Fully support PEP 427 and PEP 625"
  (`https://github.com/pypi/warehouse/pull/18924`), merged **2025-10-23**
  (`merged_at: 2025-10-23T15:44:43Z`), closing issues #12245 ("Support PEP
  625", opened 2022-09-21) and #14156 ("Reject filenames with `_` in project
  name replaced with `.`, `-`", opened 2023-07-18). Before that PR, PyPI
  accepted (and still serves, for pre-2025-10-23 uploads) sdist filenames
  that don't follow PEP 625's underscore-normalized form — see the legacy
  examples below, all still live in the ten projects' `files` lists.
- **Legacy shapes still present in these ten projects' listings** (confirmed
  live, §2.3's script output): hyphenated (not underscore) project names in
  old sdist filenames (e.g. `typing-extensions-3.10.0.0.tar.gz`,
  hyphen kept rather than becoming `typing_extensions-...`); `.zip` sdists
  (h11's `0.5.0`/`0.6.0`/`0.7.0`, before it switched to `.tar.gz`); **no**
  `.tar.bz2`, `.tgz`, `.egg`, `.exe`, or `.msi` files anywhere in any of the
  ten (`Counter({'.tar.gz': 861, '.whl': 630, '.zip': 3})` across all ten
  combined — those older legacy suffixes exist on PyPI in general but not in
  this sample); **no build-tag wheels** in any of the ten (zero
  five-dash-segment `.whl` stems found).

### 2.2 How `packaging` and pip parse these, quoted with tag and path

**`packaging.utils.parse_wheel_filename`** (`pypa/packaging` tag `26.3`,
`src/packaging/utils.py`, lines 200–303): strips the `.whl` suffix, requires
4 or 5 dashes in the stem, `parts = filename.split("-", dashes - 2)`
(splitting from the **left**, name and version are `parts[0]`/`parts[1]`,
the trailing 2 or 3 parts are the build tag and compatibility tags), then
`version = Version(parts[1])`, raising `InvalidWheelFilename` if that's not a
valid PEP 440 string (line 245's changelog: "Raises `InvalidWheelFilename`
when the version component is invalid", added 23.2).

**`packaging.utils.parse_sdist_filename`** (same file, lines 306–370): only
accepts `.tar.gz` or `.zip` suffixes (raises `InvalidSdistFilename`
otherwise — so this parser alone would reject the `.tar.bz2`/`.tgz`/`.egg`
suffixes the task asked about, none of which occur in these ten anyway).
Critically, for the name/version split it does **not** assume a known
project name; it does `name_part, sep, version_part =
file_stem.rpartition("-")` (line 353) — splitting on the **rightmost** dash,
with the comment "We are requiring a PEP 440 version, which cannot contain
dashes, so we split on the last dash." This correctly handles a hyphenated
project name like `typing-extensions-4.12.2.tar.gz` (name-part
`typing-extensions`, version `4.12.2`) without ever needing to already know
the project name — a generic filename parser has no other way to disambiguate
which dash separates name from version.

**pip's link evaluator** (`pypa/pip` tag `26.2.1`,
`src/pip/_internal/index/package_finder.py`): for a `.whl` link, pip
delegates entirely to `packaging` — `pip/_internal/models/wheel.py` (same
tag) constructs `Wheel(filename)`, whose `__init__` calls
`packaging.utils.parse_wheel_filename` directly (lines 20–29) and stores
`self.version = str(_version)`. For a **legacy sdist / egg fragment**, pip
does *not* use `packaging.utils.parse_sdist_filename` at all — it uses its
own, project-name-aware separator search, because — unlike `packaging`'s
generic parser — pip already knows which project it is looking for
(`self._canonical_name`, `self.project_name`, from `evaluate_link`, line
170–260) and needs the split to be robust to non-normalized capitalization
in the filename too, not just hyphens. `_find_name_version_sep` (lines
1111–1134), quoted with its own docstring example of exactly the "hyphenated
project name" pitfall:

```python
def _find_name_version_sep(fragment: str, canonical_name: str) -> int:
    """Find the separator's index based on the package's canonical name.
    ...
    This function is needed since the canonicalized name does not necessarily
    have the same length as the egg info's name part. An example::

    >>> fragment = 'foo__bar-1.0'
    >>> canonical_name = 'foo-bar'
    >>> _find_name_version_sep(fragment, canonical_name)
    8
    """
    # Project name and version must be separated by one single dash. Find all
    # occurrences of dashes; if the string in front of it matches the canonical
    # name, this is the one separating the name and version parts.
    for i, c in enumerate(fragment):
        if c != "-":
            continue
        if canonicalize_name(fragment[:i]) == canonical_name:
            return i
    raise ValueError(f"{fragment} does not match {canonical_name}")
```

i.e. pip scans **left to right** through every dash and stops at the first
one whose preceding text, PEP 503-canonicalized, equals the already-known
canonical project name — the opposite direction from `packaging`'s
rightmost-dash sdist heuristic, and only workable because pip (like this
watch) already knows the target project name going in, exactly the
situation the task's candidate rule is built for.
`_extract_version_from_fragment` (lines 1137–1151) then takes everything
after that separator as the raw version text, unparsed (pip validates it as
PEP 440 later, in `InstallationCandidate`).

### 2.3 The candidate rule, verified live against the ten projects

**Rule tested** (per the task): lowercase the filename; strip the
project-name prefix, treating `-`, `_`, `.` as interchangeable inside the
name (PEP 503); require a literal `-` immediately after it; for a `.whl` the
version is the text up to the next `-`; for an sdist (or any other archive
suffix found: `.tar.gz`, `.zip`, `.tar.bz2`, `.tar.xz`, `.tgz`, `.tar`) the
version is the text up to the archive suffix; compare that text to the
`versions` entries after PEP 440 normalization of both sides (Appendix B's
regex, quoted verbatim below, implemented stdlib-`re`-only).

**Script** (throwaway, not committed — described here to be re-made):
`/tmp/.../scratchpad/verify/attribution_rule.py`, reading the ten cached
`https://pypi.org/simple/<project>/` JSON replies from
`/tmp/.../scratchpad/pypi-fetch/<project>.json` (this session's live
fetches). It implements:

- `normalize_version()`: PEP 440 §"Appendix B"'s `VERSION_PATTERN` regex
  (copied verbatim from the PEP, `re.VERBOSE | re.IGNORECASE`, stdlib `re`
  only — no `packaging`), then applies every normalization rule from PEP 440
  §"Normalization" (case fold; integer normalization dropping leading
  zeros; pre-release separator/spelling/implicit-number normalization
  including `alpha→a`, `beta→b`, `c→rc`, `pre→rc`, `preview→rc`; post-release
  separator/spelling/implicit-number normalization including `rev`/`r`→
  `.postN`, and the separator-`-`-only implicit-post form `1.0-1`→`1.0.post1`;
  dev-release normalization; local-version segment normalization; leading
  `v` stripped; whitespace stripped).
- `attribute(filename, project)`: builds a regex from the project name with
  `-`/`_`/`.` treated as interchangeable at each internal separator
  position, matches it plus a literal `-` at the start of the lowercased
  filename, then applies the wheel/archive-suffix rule above to the
  remainder.
- For each project: attributes every `files[]` entry to the listed `versions`
  entry with the matching normalized form, and reports any file that fails
  to match the name prefix, fails to parse as PEP 440, or normalizes to
  something not in the (normalized) `versions` set; and any `versions` entry
  that ends up with zero attributed files.

**Result, all ten, run 2026-09-19** — zero failures everywhere:

| Project | `versions` listed | `files` listed | Files attributed to exactly one listed version | Failures / mis-attributions | Versions with no file | `versions` entries not PEP440-normalized |
|---|---|---|---|---|---|---|
| starlette | 202 | 304 | 304 | 0 | none | none |
| uvicorn | 203 | 297 | 297 | 0 | none | none |
| httpx | 76 | 145 | 145 | 0 | none | none |
| httpcore | 64 | 119 | 119 | 0 | none | none |
| h11 | 13 | 26 | 26 | 0 | none | none |
| anyio | 72 | 144 | 144 | 0 | none | none |
| idna | 42 | 75 | 75 | 0 | none | none |
| certifi | 75 | 140 | 140 | 0 | none | **`14.05.14`, `2015.04.28`** |
| click | 65 | 128 | 128 | 0 | none | none |
| typing-extensions | 52 | 116 | 116 | 0 | none | none |

Totals: 864 versions listed, 1,364 files listed, 1,364/1,364 attributed,
0 failures. **The rule needed no minimal fix for this sample** — it survived
h11's `.zip` sdists, certifi's un-normalized leading-zero date versions
(`14.05.14`, `2015.04.28`), every hyphen/underscore/dot spelling of
`typing-extensions` in old sdist filenames, and every pre-release
(`0.12.0b1`, `1.0.0rc1`, per the triage commit) and post-release
(`0.26.0.post1`, `0.32.0.post1`) spelling that appears in these ten
projects' histories. The one structural gap the rule *would* fail on, not
exercised by this sample because none of the ten has it: a wheel with a
**build tag** (`name-version-buildtag-pytag-abitag-platformtag.whl`) — the
rule's "version is text up to the next `-`" still correctly stops at the
version/build-tag boundary in that case (the build tag comes *after* the
version, not embedded in it), so it would in fact still work; the real risk
identified from reading `packaging`'s and pip's source (§2.2) rather than
from a live failure is a project whose **name itself is a strict prefix of
another allowed name spelling within the same filename** combined with a
version that could be split at the wrong dash — this cannot happen under
the rule as specified, because the rule anchors on the *known* project name
first (not a leftmost-generic-dash search), the same choice pip's
`_find_name_version_sep` makes and `packaging`'s generic (name-unaware)
`parse_sdist_filename` cannot.

---

## 3. PEP 440, the subset the tool needs

- **Grammar** (PEP 440 §"Public version identifiers"):
  `[N!]N(.N)*[{a|b|rc}N][.postN][.devN]`. **Appendix B**'s canonical-form
  test regex: `^([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*((a|b|rc)(0|[1-9][0-9]*))?(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?$`.
  Appendix B's parsing regex (`VERSION_PATTERN`, quoted in full in §2.3's
  script description) additionally accepts the non-canonical spellings
  normalized below; a stable-only parser can ignore its `local` group
  entirely (PyPI rejects local-version uploads — see below) and can treat
  `epoch` as `0` when absent.
- **Total order** (§"Summary of permitted suffixes and relative ordering"):
  epoch compared numerically first (implicit `0`); release segments compared
  as `tuple(map(int, release_segment.split(".")))` with **zero-padding to a
  common length** so `1.0` and `1.0.0` compare equal; within a shared release
  segment, `.devN < aN < bN < rcN < <no suffix> < .postN`. A post release
  sorts strictly after its corresponding final and strictly before the next
  final (the PEP's own worked ordering example: `...1.0rc1, 1.0, 1.0+abc.5,
  ..., 1.0.post456, 1.0.15, 1.1.dev1`).
- **Alternate spellings that normalize to a post release** (§"Normalization",
  "Post release separators"/"spelling"/"implicit number"): `1.0-1` (implicit
  post, `-`-separator-only form), `1.0post1`, `1.0-post1`, `1.0.post1`
  (canonical), `1.0.r1`/`1.0-r1`/`1.0r1`, `1.0.rev1`/`1.0-rev1`/`1.0rev1`,
  `1.0.post` (implicit number, `.post0`) — all normalize to `1.0.post1` (or
  `1.0.post0` for the number-omitted form).
- **Leading `v`, leading zeros, case**: a single leading `v` is stripped and
  ignored (`v1.0` ≡ `1.0`); integer components are parsed via `int()` so
  leading zeros vanish (`14.05.14`→`14.5.14`, confirmed live in certifi's
  listed `versions`, §1.3/§2.3); all-ASCII-letter case-folds, normal form
  lowercase (`1.1RC1`→`1.1rc1`).
- **Excluded as not stable** (per the ticket's ruling (d), "a final or post
  release not fully yanked"): any version with a **pre-release segment**
  (`a`/`b`/`rc`, or their alternate spellings `alpha`, `beta`, `c`, `pre`,
  `preview` — all normalize into `a`/`b`/`rc`); any version with a **`.dev`**
  segment, whether standalone or attached to a pre-release or post-release
  (`1.0.dev1`, `1.0a1.dev1`, `1.0.post1.dev1` — all excluded, since a dev
  segment is present regardless of what else the version has); a **post
  release of a pre-release** (`1.0a1.post1`) is excluded too, because it
  still carries the pre-release segment (`a1`) that disqualifies it — it is
  not a "final or post release" in the ticket's sense, it's a post-release
  *of* a pre-release. **Local versions** (`+segment` suffix) are excluded
  from consideration as a matter of course for this watch because **PyPI
  itself refuses to accept an upload whose version has a local-version
  label**: `pypi/warehouse`'s upload validation
  (`warehouse/forklift/legacy.py`, `main` branch, read 2026-09-19, around
  line 599) special-cases the error message when `"use of local versions" in
  str(e)`, i.e. Warehouse's metadata validation rejects local-version
  uploads outright — confirmed by the presence of this dedicated error path,
  matching PEP 440 §"Local version identifiers": "Local version identifiers
  SHOULD NOT be used when publishing upstream projects to a public index
  server... As the Python Package Index is intended solely for..." (the PEP
  text is prescriptive; PyPI's upload code enforces it). None of the 864
  versions read across the ten projects carried a local-version label,
  consistent with this.
- **Value to propose: the `versions` entry as listed, not re-normalized**.
  PEP 440 §"Version matching": "By default, the version matching operator
  [`==`] is based on a strict equality comparison: the specified version must
  be exactly the same as the requested version. **The only substitution
  performed is the zero padding of the release segment**..." Since integer
  parsing (not textual padding of individual digits) is what makes `05`
  and `5` compare equal, and pip's own `==` resolution goes through the same
  PEP 440-normalization-aware comparison, `pip install
  "certifi==14.05.14"` and a hypothetical `pip install "certifi==14.5.14"`
  would resolve to the *same* installed file either way (assuming the latter
  exists as a valid `==` match against PyPI's data, which it does since
  comparison is int-based, not string-based) — **but only the literal
  `versions` string (`14.05.14`) is guaranteed to be the exact text PyPI
  actually stored and displays**, and using it verbatim in the Dockerfile's
  `*_VERSION` build argument avoids ever inventing a version-string spelling
  PyPI never published. This note recommends the resolver propose the
  `versions` list's own string, unmodified, as the `*_VERSION` value.

---

## 4. PEP 592 yank semantics for an exact `==` pin

- **Installer behavior on an exact pin** (PEP 592 §"Installers"): "An
  installer MUST ignore yanked releases, if the selection constraints can be
  satisfied with a non-yanked version... [suggested approach:] Yanked files
  are always ignored, **unless they are the only file that matches a version
  specifier that 'pins' to an exact version using either `==`... or
  `===`**. Matching this version specifier should otherwise be done as per
  PEP 440 for things like local versions, zero padding, etc." So a
  `pip install "starlette==0.20.2"` (a yanked release) still succeeds, with a
  warning, precisely because it pins exactly — which is exactly why the
  watch must never propose a fully-yanked version as the new pin (it would
  still technically install, but silently degrades to "the maintainer would
  have to know it's yanked to know why"), matching ruling (d)'s "not fully
  yanked" gate.
- **Per-release or per-file, UI vs. API — can a partly-yanked release
  exist?**: PEP 592 itself specifies yanking at the **file** level in the API
  (`data-yanked` / `yanked` is a per-file attribute) but explicitly notes,
  §"Warehouse/PyPI Implementation Notes": "While this PEP implements yanking
  at the file level, that is largely due to the shape the simple repository
  API takes, not a specific decision made by this PEP. **In Warehouse, the
  user experience will be implemented in terms of yanking or unyanking an
  entire release**, rather than as an operation on individual files, which
  will then be exposed via the API as individual files being yanked." PyPI's
  own user docs confirm this is still true today:
  `https://docs.pypi.org/project-management/yanking/` (read 2026-09-19):
  "**PyPI currently only supports yanking of entire releases, not individual
  files.**" So on PyPI as it exists today, **a partly-yanked release cannot
  be produced through the normal (UI) yank action** — every yank sets every
  file of that release's `yanked` field together. Live confirmation across
  all ten projects (§4's per-release grouping, using the §2 attribution to
  group files by release): **six fully-yanked releases found, zero partly-
  yanked releases found**:

  | Project | Yanked release | Files yanked / total files of that release | Reason |
  |---|---|---|---|
  | starlette | 0.20.2 | 2/2 | "Security" |
  | starlette | 0.41.1 | 2/2 | "python-multipart release was broken" |
  | uvicorn | 0.17.0 | 2/2 | "Needs `python_requires` specifier" |
  | anyio | 4.6.2 | 2/2 | "This was 4.5.2 code, mistagged" |
  | certifi | 2022.5.18 | 2/2 | "Incorrectly claims to support Python 3.5." |
  | click | 8.2.2 | 2/2 | "Unintended change in behavior of boolean options and None" |

  Every `yanked` value observed across all ten projects was either `false`
  or a non-empty reason string; **no bare `true` and no empty string** were
  seen anywhere. This matches PEP 691's spec ("either a boolean to indicate
  if the file has been yanked, or a non empty... string") — a bare-`true`
  yank is spec-legal but Warehouse's UI always asks for (and, per the docs
  page, "strongly encourages") a reason, which is presumably why none
  appeared. **No partly-yanked example was found**, on the ten projects or
  via a broader search for one; given the documented "entire releases only"
  UI constraint this is expected and not merely a sampling gap, though I
  cannot rule out some other upload path (e.g. a very old, pre-PEP-592-era
  quirk, or direct API manipulation) having produced one somewhere on PyPI
  that this search didn't surface — **flagged as not exhaustively verified**.

---

## 5. The legacy JSON API's deprecation of `releases`

- **Verbatim, read 2026-09-19**, `https://docs.pypi.org/api/json/`
  §"Deprecated keys":
  > The following keys are considered deprecated:
  > - `releases`: projects should shift to using the Index API to get this
  >   information, where possible.
  > - `downloads`: this key is always -1 and should not be used.
  > - `has_sig`: this key is always false and should not be used.
  > - `bugtrack_url`: this key is always null and should not be used.
  >
  > In the future, each of these keys may be removed entirely from this API
  > response.
- **History, `pypi/warehouse`** (all read 2026-09-19):
  - The `releases` deprecation note was **first added** by commit
    `d1579dfc5db13a5042582734bb58918db976b928`, "Deprecate the releases key
    on the non version json too" (PR #11777, Donald Stufft, **2022-07-07**),
    to `docs/api-reference/json.rst`, with the wording "projects should
    shift to using the simple API (which can be accessed as JSON via PEP
    691) to get this information where possible."
  - `has_sig`/`bugtrack_url` deprecation notes were added separately, later,
    by commit `54ff1484777353dfb40406049822b86564087926`, "Call out
    deprecated fields of APIs" (PR #13790, **2023-05-28**) — `releases` was
    not touched by that commit, it already had its own note.
  - The current combined "Deprecated keys" callout (grouping all four keys
    under one heading, and rewording `releases`'s note from "the simple API
    (PEP 691)" to "**the Index API**") was introduced by commit
    `6533e60bd2d84da783122545dc482f19f8ae33c5`, "docs: migrate JSON API docs
    to user-docs" (PR #17178, William Woodruff, merged **2024-12-03**), which
    moved the content from `docs/dev/api-reference/json.rst` to the current
    `docs/user/api/json.md` and rewrote the four separate `.. attention::`
    blocks into today's single `!!! warning "Deprecated keys"` block.

---

## 6. The other nine projects' newest stable versions, read live 2026-09-19

Computed by this note's own script (§2.3's `attribute()` +
PEP 440 normalization, applied to each project's `versions` list, filtered to
final-or-post releases with ≥1 file where not every file of that version is
yanked, per ruling (d)), cross-checked for the six of these ten that
`docs/research/gideon-api-http-stack.md` (`research/gideon-api-http-stack`
branch) already recorded from the legacy JSON API's `info.version` field the
same day — **all six agree**, which is a useful sanity check that the Index
API's `versions` list and the legacy API's `info.version` (deprecated for
this purpose, but not wrong where it still works) point at the same release
today.

| Project | Newest stable version (2026-09-19) | Cross-checked against `gideon-api-http-stack.md`? |
|---|---|---|
| starlette | **1.6.0** | yes — agrees (also the binding pin above) |
| uvicorn | 0.53.0 | yes — agrees |
| httpx | 0.28.1 | yes — agrees |
| httpcore | 1.0.9 | yes — agrees |
| h11 | 0.16.0 | yes — agrees |
| anyio | 4.15.1 | yes — agrees |
| idna | 3.20 | not in that note |
| certifi | 2026.7.22 | not in that note |
| click | 8.5.0 | not in that note |
| typing-extensions | 4.16.0 | not in that note |

---

## Flags — things not independently, exhaustively verified

1. **A currently-quarantined project's live JSON reply** (§1.4): not found
   without a long hunt; PEP 792's own worked example is cited in its place.
2. **PEP 792's `state` vs `status` key-name inconsistency** (§1.4): the
   PEP's prose text and its own worked example disagree; live behavior
   matches the example (`status`), treated as authoritative here.
3. **A partly-yanked release anywhere on PyPI** (§4): none found among the
   ten projects or via a broader search; PyPI's documented "entire releases
   only" yank UI makes this the expected (not merely sampled) result, but
   the search was not exhaustive across all of PyPI.
4. **The exact byte-for-byte reproducibility of `X-Cache`/`ETag` behavior
   under heavier request volume** (§1.5): only lightly exercised (a handful
   of repeated fetches per project in one session); the caching/rate-limit
   claims themselves are drawn directly from `docs.pypi.org`'s own policy
   text, not independently stress-tested.
5. **PEP 700's file-less-`versions`-entry allowance** (§1.3): confirmed as a
   spec-legal possibility from the PEP text; not observed on any of the ten
   projects sampled, so the resolver's handling of that case is
   spec-driven, not empirically exercised.

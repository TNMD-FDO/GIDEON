# Testing Guidelines

## Test Framework

pytest (pinned in `requirements-dev.txt`) as the runner over stdlib `unittest.TestCase` classes — no pytest fixtures or plugins so far. Configured in `pyproject.toml`: `testpaths = ["tests"]`, `pythonpath = ["."]` (tests import `gideon` from the checkout, no install step).

## Running Tests

The pinned tools live in a project venv (untracked). On a fresh clone or seat:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

(`requirements-dev.txt` is the one list: `.github/workflows/ci.yml` installs from it, and `tests/test_toolchain_pins.py` holds its two copies to it.)

```bash
# All tests (seconds today)
.venv/bin/pytest

# One file / one test
.venv/bin/pytest tests/test_cli_surface.py
.venv/bin/pytest tests/test_cli_surface.py::Stubs::test_every_stub_refuses_as_not_implemented

# The gate: ruff, mypy, then pytest -x, stopping at the first failure; test paths
# narrow the pytest step; --all adds the cases marked slow, which CI runs
python3 -m tools.gate
python3 -m tools.gate tests/test_cli_surface.py
python3 -m tools.gate --all
```

A case past about ten seconds carries `@pytest.mark.slow` (the marker is registered in `pyproject.toml`, `--strict-markers` refusing an unregistered one): the gate's default skips the mark so the local run stays under a minute, `--all` and CI run everything, and a bare `pytest` runs everything too. The two so marked are the arithmetic guardrail's character-by-character stream cases, whose cost is one judge call per released character, not repeated work a cache would remove.

No coverage tooling is pinned (the pinned toolchain is `requirements-dev.txt`). Adding `pytest-cov` is a TRIP-planned toolchain change if a gate ever needs numbers.

## Test Organization

- Flat `tests/` directory; files `tests/test_<area>.py`; `unittest`-style classes grouping related assertions (`class Help`, `class Stubs`, …).
- CLI tests run **in-process** against `cli.main(argv)` with `contextlib.redirect_stdout/stderr` — never subprocesses.
- The contract tier is the surface, the seam, the version, and the lock-coupling tripwire; [`docs/archi/tests.md`](../archi/tests.md) has the full contract list. These are contracts, not examples to copy blindly:
  - `test_cli_surface.py` — the recorded §20.2 surface (+ `backup drill`). `TOP_LEVEL` names every command group; `STUBS` lists what still refuses — it must **shrink in the same change** that lands a real handler.
  - `test_host_import_boundary.py` — the bare-host seam (stdlib + `yaml` only), AST-level. Never weaken it to make an import convenient.
  - `test_lock_coupling.py` — no test module embeds a value the pin watch can move (whole-token, comments included). Derive from the loaded lock or own a fictitious lock text; see "Lock files in tests" below.
  - `test_toolchain_pins.py` — holds the two copies no install reads, `pin-watch.yml`'s PyYAML line and the Playwright constant, to `requirements-dev.txt` without stating a version. Its failure names the edit that fixes it.
  - `test_research_notes.py` — the research notes' front matter (workflow ticket 05): every `docs/research/*.md` names the pins it was verified against with ids the pin watch's registry, `models.lock`'s reference profile, or `requirements-dev.txt` knows, and a version per item; the vocabulary is derived, never restated, and the negatives use fictitious names and versions.
  - `test_skills_vendored.py` — the vendored-skills tripwire (slice-0 ticket 18, `docs/agents/tooling.md` §8): every non-slot `skills-lock.json` entry's folder hash recomputed with the `skills` CLI's own algorithm, a TRIP skill's with its GIDEON blocks' lines removed first, the customized skills' GIDEON blocks against the tooling document's §4 table and each under the ceiling that section's machine-readable line states, no placeholder, conflict marker, non-executable `scripts/*.sh`, or case-duplicate directory, and the lock's TRIP `ref` equal to the document's provenance line. Its failure lists every finding with its fix; an unmarked edit to a vendored skill is red here, never silently discarded by the next upgrade.
  - `test_nogpu.py`, `test_acceptance.py`, `test_smtpsink.py`, `test_evidence_hygiene.py` — the no-GPU mode and the acceptance harness (slice-0 ticket 08's second release, ADR-0035): the marker written absent-only and refused on an NVIDIA device; the harness over a fake `Host` and a fake sink with an injected `sleep` and `clock` (no test ever sleeps or waits for real); the SMTP sink over a loopback socket with `smtplib` as the client — that module mints its own throwaway certificate with `openssl` into a temporary directory at class setup — one of the two tests that shell out, the other being `test_render.py`'s run of the engine's entrypoint wrapper under `/bin/sh` (slice-1 ticket 04) — and skips when `openssl` is absent; and the evidence-hygiene tripwire over every text asset under `.scratch/` (no complete age secret key, no print-once line carrying a value other than `<redacted>`), so a transcript that escaped redaction is red before it is pushed.
  - `test_cycles.py` — the cycles record (workflow tickets 06 and 34): the tool over a synthetic harness store the module builds itself, never a real transcript — the parse with one turn per `message.id`, the malformed shapes, discovery through a worktree's `.git` file, the bands from the tickets' directories, the merge and its byte-stability, the refusals leaving the record unchanged, a fixture sentence proven absent from every rendering; and an AST check holding `tools/cycles.py` to the standard library.
  - `test_tracker.py` — the tracker board (workflow ticket 01): the tool over a synthetic fixture tracker the module builds itself, never the real one — both header shapes, every blocker-grammar case, the closure and its dependency order, each lint rule and shape guard with path, line, and fix, the sections' orders and the threshold's boundary, both renderers byte-stable over two runs, the page self-contained, the refusals writing nothing; the standing directory's two states (`fired` in the grammar, the lint's three shapes, a shared number), its waiting tickets off the frontier, the decisions owed, and the triage owed and its fired ones in them, the standing section, and the constant held to the directory `issue-tracker.md` names; a real temporary git repository for the facts reader (skipped without `git`); the real tracker parsing without a refusal; `triage-labels.md`'s right-hand column held to the canonical `Statuses:` line; the threshold constant held to the figure `issue-tracker.md` states; and an AST check holding `tools/tracker.py` to the standard library.
  - `test_office_values.py` — the office-values tripwire over the whole tree (slice-1 ticket 54): no office-shaped hostname, RFC 1918 address, or distinguished name outside the documentation vocabulary (RFC 2606 names, RFC 5737 networks) and the site-key placeholders, by a rule that names no office value — its allowlist of registrable domains seeded from `config/egress.yaml` and the documentation hosts the tree cites, its public-suffix set derived from that allowlist, the vendored skill folders skipped, Python sources read through their literals and comments — with a seeded temporary tree proving each rule fires and each exemption stays silent, so a pasted transcript that escaped ticket 55's redaction is red before it is pushed.
  - `test_release_notes.py` — the release-note contract (spec §21, slice-1 ticket 17): `docs/release-notes/TEMPLATE.md` declares its version and exactly its required headings; every `docs/release-notes/v*.md` is held to the template version it names (file-name version, title, headings in order with text, no authoring comment or placeholder, the practice rule verbatim where its section appears); and from `__version__` `0.2.0` a note for the current version must exist. Each finding ends in its fix — the path to write, the template to copy — which is what a release or hotfix session reads when the file is missing.
  - `test_render_grafana.py`, `test_grafana.py`, `test_alerts.py` — the observability tier (slice-0 ticket 07, ADR-0034): Grafana's `ldap.toml` from three site shapes (an escaped DN round-trips through `tomllib`), the datasources and provider, every dashboard JSON parsed and held to the declared datasource uids, the fifteen rules' thresholds and no-data/error states (only target-down pages on silence; the drill threshold from the calendar's real longest gap), the drift rule present only with a tested driver in a fictitious lock, the heartbeat window per fixture timezone; the Grafana client over a loopback `http.server` (SNI and `Host`, the credential in no repr or error, the receivers API shapes, address redaction); and `alerts test`'s stages, refusals, and audit row over a fake `Host` and a fake client. The dashboards under `compose/grafana/dashboards/` are release content emitted verbatim — checked for parse and datasource consistency, never regenerated by a script.

## Writing Tests

- Match the existing style: `TestCase` classes, stdlib assertions, `subTest` for parametrized loops, docstrings citing the spec section the contract comes from.
- Test observable behaviour: exit codes, stdout/stderr text, persisted effects — never internal wiring.
- The refusal path is as important as the happy path: refusals print the fix (§1.5) and exit non-zero.
- Provisioning/install-shaped logic must be tested for re-runnability (run twice → converge).
- Never a real network call, GPU, or store in this suite. Stack-needing tests belong to the `gideon-ci` tier (spec §18), which arrives with slice 2 — don't build ad-hoc harnesses before it.
- Hard-to-cover code: see the seam ladder in `.claude/skills/TRIP-test/SKILL.md`; uncovered risky paths go in `docs/4-unit-tests/COVERAGE-DEBT.md` (`path | why hard | escape plan`).
- A fake `Host` raises `FileNotFoundError` for a missing file, as `RealHost` does — production code never catches a `KeyError` to suit a fake.
- A test that seeds a fake checkout with templates derives the list from `ARTIFACTS` (`path for artifact in ARTIFACTS for path in artifact.template_paths`), never a literal list: a new artifact then cannot leave the fake behind (`tests/test_apply.py`, `test_render.py`, `test_render_owui.py`, `test_drill.py` are the pattern).
- A fake served from threads mirrors the real service where a race can show: it records or logs before its first response byte (`tests/test_owui.py`, `v0.1.63`), and it mints ids that never repeat when callers overlap, since an id reused after a peer's delete can match a listing taken before it (`tests/test_turns.py`'s `Frontend`, `v0.1.67`). A race is proven by widening its window — a short sleep at the suspect point — never by rerunning until green.
- Command modules return `report.Problem` (a problem and its fix) or `StageResult` rows, never a string with an embedded `Fix:`; tests assert `.problem` and `.fix` separately. Anything that crosses the text-only `Host.run` is text: binary travels as base64, hashes as hex (the age header in `test_drill.py`).

## Lock files in tests

`images.lock`, `host.lock`, and `models.lock` move through pin-watch pull requests (ADR-0031) — and so does the Matt Pocock skills' record, the provenance line in `docs/agents/tooling.md` §2 (ADR-0033) — and a bump branch is the first place a test's copy of a lock value and the lock itself disagree — no gate runs there before the pull request exists. Slice-0 ticket 10's first two bump pull requests went red on exactly this (hotfix `v0.0.14`; the ticket is `.scratch/slice-0/issues/10-pin-watch.md` and the memo `docs/6-memo/lock-coupled-tests.md`, both in the development repository). The rule:

1. **Derive, never copy.** A test that exercises code reading a lock loads the committed file (`load_image_lock(ROOT / "images.lock")`, `load_host_lock(ROOT / "host.lock")`, `load_models_lock(ROOT / "models.lock")`) and builds every expectation — a digest, a package name, an archive path, a download URL — from the loaded value. `tests/test_host_steps.py` and `tests/test_apply.py` are the pattern.
2. **Or own a fictitious lock.** A test that needs its own lock text (`load_image_lock_text`, `parse_host_lock`, `load_models_lock_text`) uses values no upstream can ever publish for that pin: a fictitious repository (`docker.io/library/example:1.0`, `ghcr.io/example-org/example:v1.0`, or a repo under an `example` owner), a repeated-hex digest (`"sha256:" + "e" * 64`), a 40-character repeated-hex model revision (`"a" * 40`), a version far outside the real series (`1000.0.0`, driver branch `1000`), a fictitious cloud-image series. A stale real value (last month's tag, the previous digest) is still a bug: the next bump branch may be cut from a base where it is current. The same for the skill record: a provenance line with a repeated-hex commit. `tests/test_pinwatch.py` is the pattern.
3. **A literal equal to a committed pin is a bug**, in a comment as much as in an assertion. So is a value typed *relative* to a pin — above, below, next, same branch — rather than computed from the loaded value (`f"{int(tested.split('.')[0]) + 1}.0.0"`, `f"nvidia-driver-pinning-{branch}"`).
4. **The tripwire enforces it.** [`tests/test_lock_coupling.py`](../../tests/test_lock_coupling.py) scans every `*.py` under `tests/` (fixtures excluded: the render fixtures embed all three locks by design and are regenerated on every bump) for every value the pin watch can move — `models.lock`'s repo, revision, and per-file digests included; the recorded Matt Pocock commit and its date included — plus `driver.tested`, `kernel_tested`, and `driver.keyring_sha256`, as whole tokens — not touching a letter or digit on either side. File sizes are deliberately not scanned: a small integer is a whole token any unrelated test may carry, and a stale size is caught by the bump that moves the digest beside it. Its failure names the file, line, key path, and value. The two integer floors (`minimums.docker`, `minimums.compose`) are covered by this rule and not by the scan.
5. **Committed-lock contracts assert shape, never movable values**: names in order, grammars (`DIGEST`, `SOURCE_REFERENCE`, `VERSION`, `IMAGE_REFERENCE`, and the models-lock grammars), types — so they hold on every bump branch (`tests/test_images_lock.py::CommittedLock`, `tests/test_host_lock.py`). A release decision the pin watch never moves — a served name, a flag value, or a requirement figure — may be asserted exactly.

## Render fixtures

`tests/fixtures/render/<example|second-office|no-gpu>/` is the §3.4 byte-stable render of each site file (the third is the example site under the no-GPU mode), and [`tests/test_render.py`](../../tests/test_render.py) fails when a render differs from it by a byte. The fixture is a function of every render input, so it moves whenever one of them does: the two lock files (a bump pull request regenerates it — the pin watch does this itself), the templates under `compose/`, the render code under `gideon/host/render/`, the release string, and **the site files themselves — `config/site.example.yaml` and `tests/fixtures/site/second-office.yaml`, comments included** — because each rendered `manifest.yaml` records the site file's digest (`site_sha256`). A comment edit to the example file therefore fails the example fixture (it did, during `v0.0.19`). The remedy is always the same, and the failure message names it:

```bash
.venv/bin/python tests/regenerate_render_fixtures.py
```

Regenerate deliberately, in the same change as the edit, and read the fixture diff before committing: the only lines that should move are the ones the edit explains (a manifest digest for a comment change; a unit or `compose.yaml` line for a render change). A fixture diff you cannot explain is a render change you did not intend.

## Release content under `compose/`

A Filter Function's source (`compose/open-webui/functions/*.py`) runs inside the frontend's container and is never a `gideon` module: its test module (`tests/test_arithmetic_guardrail.py`) imports the exact committed file by path (`importlib.util.spec_from_file_location`) and holds it to the loader's rules — standard-library imports only, no `Valves`, `toggle`, or `requirements` frontmatter, none of the four import prefixes the frontend rewrites. Its pattern gate is the committed seed under `eval/seed/guardrails/` (every positive by its pattern id, the controls under the one-in-twenty ceiling as a whole number); a bypass a review finds becomes a unit case beside the seed, never a comment.

## Coverage Requirements

Not defined. Contracts over percentages: the surface, the seam, the version, and the lock coupling are pinned; new logic ships with behavioural tests per the TRIP-2 testing gate.

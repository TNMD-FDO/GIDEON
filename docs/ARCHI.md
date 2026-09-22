# GIDEON Architecture Documentation

## 1. How to Read This Document

This document records **how the code is built** — structure, conventions, commands — for anyone (human or agent) about to implement in this repo, as the code exists, not as the destination. For *what* is built and *why*, the authorities are, in precedence order: `.scratch/greenfield-spec/spec.md` (the build spec), the ticket Answers under `.scratch/greenfield-spec/issues/`, `CONTEXT.md` (the vocabulary), and [`docs/adr/`](adr/) (the decision records). `§n` references point into the spec; this document never restates a spec decision it can cite.

A plan reads the map whole. The leaves under `docs/archi/` (one per subsystem and one per cross-cutting section that outgrew the map, listed in §2) carry a subsystem's commands, constants, paths, and tests, or a section's mechanisms; each leaf's front matter names the commands and modules it owns; a plan names the leaves a ticket touches, implement reads the plan plus those, and §8's table names every command's leaf. [`docs/ARCHI-rules.md`](ARCHI-rules.md) holds the leaf template, the budgets `tests/test_archi_budget.py` enforces, and the compaction rule; a release updates it on a structural change.

## 2. Overview

GIDEON is a fully local legal AI system for federal defender offices: corpus-grounded legal research with computed, verified citations beside a general-purpose assistant, on one box the office controls (spec §0). The deployed product is a Docker Compose suite (§20.1); **the code in this repo is the one Python package `gideon`** that provisions, configures, operates, and (in later slices) serves it.

**Project type: CLI tool**, growing into a containerized service suite. The package is the product CLI `python3 -m gideon` (§20.2), the surface `tests/test_cli_surface.py` names, its later-slice groups still stubs. Every slice ends in its minor tag (§22; §12 has each slice's state). Landed behaviour is bare-host (`gideon/host/`, §9) but for the extraction grammar (`gideon/extraction/`, standard library only, outside the checked set), each subsystem detailed in its leaf, whose commands §8's table names:

| Leaf | Covers |
|---|---|
| [`archi/host.md`](archi/host.md) | the host platform: the site model and schema, provision and its steps, the image store's home, preflight and its checks, the three locks and the profile's memory table, host state, the no-GPU and build-box modes, the office clock, the CI runner step |
| [`archi/render-apply.md`](archi/render-apply.md) | render and apply: the rendered tree, secrets and their rotation, the release constants, the search rig's rendered files, the memory limits, the weights tree |
| [`archi/api.md`](archi/api.md) | the mounted `gideon-api` service: its routes, image, source digest, import boundary, and tests |
| [`archi/improvement.md`](archi/improvement.md) | the ADR-0049 improvement loops: the committed trigger registry, its loader and states, and the proposal and later loop readers |
| [`archi/engine-frontend.md`](archi/engine-frontend.md) | the engine and the frontend's runtime: the engine service and the frontend's connection, the two model records, the frontend client and the managed turn, the branch gate, the guardrail's judge (`gideon/guardrail/`) and the trip's row, `engine verify` and its sample |
| [`archi/backup-restore.md`](archi/backup-restore.md) | the backup set and its commands, restore, install, upgrade and rollback |
| [`archi/stack.md`](archi/stack.md) | ingress and TLS, the registry mirror, observability and alerting, reconcile and the audit writer |
| [`archi/tools.md`](archi/tools.md) | the pin watch and the research notes' pin bindings, the build tool, the acceptance harness and its full-restore form, the turn harness, its browser mode and service door, the evidence redaction, the gate, and the export boundary list; their interpreters and footprints |
| [`archi/eval.md`](archi/eval.md) | the eval harness: the set's cases, its loader, the frozen slices and the slice registry, the quiet window, the two `eval` commands, the record and the references |
| [`archi/eval-slices.md`](archi/eval-slices.md) | the eval slices' runners and measures: the exact-object contract, the extraction grammar, its measure and the variants, the judge, the judgments file, its metrics and the ranked-list file |
| [`archi/tests.md`](archi/tests.md) | the repo-wide tripwires under `tests/`, the per-command pattern, the contract tier under `tests/contract/` |
| [`archi/security.md`](archi/security.md) | cross-cutting, §16's: the mechanism behind each security posture — secrets' paths to their consumers, argv, the base model's withholding, grants, observability's doors, evidence and the export, backups, the network |

The Compose project holds the services `render/compose.service_names` enumerates, at the digests `images.lock` pins: Caddy, the HTTPS-only ingress (§1.6), also serving `/grafana/`; PostgreSQL with pgBackRest, the record (§7.2, §19.1); Open WebUI, the frontend (§4), configured from env and the apply manifest only; the engine `gideon-generator` (a GPU host only, §5) and SearXNG, General's metasearch (while `web.search` is on, §15), each as shipped on the Compose network alone; `gideon-api`, GIDEON's own first service role (a GPU host only, §17.1; [`archi/api.md`](archi/api.md)), its package mounted from the tree that applied; and the observability tier (§19.5) — Prometheus on loopback only, Grafana behind the ingress, the exporters unpublished. Everything sits behind the injectable system-I/O seam `gideon/host/sysio.py`.

## 3. Technology Stack

| Component | Version | Where pinned |
|---|---|---|
| Python | ≥ 3.12 floor (Ubuntu 24.04 fallback, spec §1.2); the box runs the LTS `host.lock` names, whose Python is **3.14** | `pyproject.toml` `requires-python`; CI runs 3.14 |
| Build backend | setuptools ≥ 77 | `pyproject.toml` |
| ruff, mypy, pytest, PyYAML, Playwright — the dev and CI toolchain, never the product's | exact pins; ruff and mypy at the 3.12 floor with `check_untyped_defs` (`pyproject.toml`); PyYAML stands in for the box's `python3-yaml`; Playwright bundles the Chromium build `gideon/evaluation/turns/chromium.py` names — the headless shell alone on the box, the package in CI for mypy only and in the dev venv, never the box's system Python; a bump is Dependabot's proposal (§6), never the pin watch's | `requirements-dev.txt`; the two copies (`pin-watch.yml`'s install line, the Playwright constant) held equal by `tests/test_toolchain_pins.py` |

**Built images**: the two `images/<name>/Dockerfile` files are argument-driven (`ARG BASE` before `FROM`, one `ARG` per build input) and embed no version; the values live in `images.lock`'s built pins ([`archi/host.md`](archi/host.md)) and reach the build from `python3 -m tools.imagebuild` on the box.

**Runtime dependencies: none on the host.** The bare-host path (`gideon host`, the entry chain) imports only the standard library and `yaml` (`python3-yaml`, present on every Ubuntu Server — §1.5, §2.3; **never PyPI** on a box). `gideon/api/` is the service boundary's exception: its Starlette, uvicorn, httpx, transitive HTTP packages, and the guardrail trip writer's Postgres driver live only in `images/gideon/`, not in the host path or a pip install. Commands outside the host subtree grow heavier dependencies later *inside their handlers* (§9), because the rest of the CLI runs from the release's pinned container image.

**Tooling on the box** (`tools/`): the interpreters, imports, and footprints of every tool are in [`archi/tools.md`](archi/tools.md); nothing there is the product.

## 4. Project Structure

The repository's tree; §16 names what the export omits.

```
GIDEON/
├── gideon/                  # the one Python package
│   ├── __init__.py          # docstring + __version__ (single-sourced, §11)
│   ├── __main__.py          # python3 -m gideon → sys.exit(main())
│   ├── cli.py               # build_parser() + main(): the whole §20.2 surface
│   ├── extraction/          # the exact-object contract, the extraction grammar, its measure — stdlib only (archi/eval-slices.md)
│   ├── api/                 # the mounted gideon-api service package (§17.1; archi/api.md)
│   ├── evaluation/          # the eval-set loader, the slice registry and its runners, the judge, the quiet window, the recorder, the reference format, the two `eval` commands, and `turns/` — the turn harness's drivers, classifier, cases reader, browser seam and launcher, and door (archi/eval.md; the runners and the judge archi/eval-slices.md)
│   ├── improvement/         # the ADR-0049 improvement loops: trigger registry and later readers (archi/improvement.md)
│   ├── guardrail/           # the arithmetic guardrail's judge, five modules (§9's third set; the judge's one home since general-turn ticket 09)
│   │   └── grammar.py · families.py · judge.py · writer.py · window.py
│   └── host/                # bare-host subtree: stdlib + yaml ONLY (§9)
│       ├── cli.py · sysio.py · report.py · stages.py   # the handlers, the injectable Host seam, the refusal and row shapes, run_stage (§8, §10, §14)
│       ├── lock.py · images.py · models.py · egress.py · courts.py · site.py · site_schema.py · nogpu.py   # the committed-artifact loaders, the schema emitter, the no-GPU marker
│       ├── provision.py + steps/ · preflight.py + checks/   # the §1.5 runners and the STEPS and CHECKS registries
│       ├── render/          # the §3.5 pure render core: the ARTIFACTS registry, one module per artifact family, consumers, command
│       ├── <command>.py     # one module per command, each named in §8's table
│       ├── secrets.py · enginesample.py · backupset.py · backuplock.py   # the secret registry, engine verify's sample, the set model, the backup commands' lock
│       ├── grafana.py · ingress.py · stores.py · audit.py · ldap.py   # the Grafana client, the shared SNI connection, Postgres roles and migrations, the audit writer, reconcile's directory reads
│       └── stack.py · pgbackrest.py · sshtarget.py   # argv builders and probes
├── images/postgres/Dockerfile   # the built Postgres image (base + pgBackrest; no pin in the file)
├── images/gideon/Dockerfile     # the built API image (base + Python dependencies; no pin in the file)
├── compose/                 # templates by service: caddy/, open-webui/ (permissions.yaml, general.yaml, functions/ — the branch gate alone since general-turn ticket 09, release content
│                            #   never imported as a gideon module), postgres/, systemd/, prometheus/, blackbox/, grafana/
├── tests/                   # unittest-style classes run by pytest (§15); one test_<area>.py per module or command, plus:
│   ├── fixtures/            # site/ (refusals); render/<example|second-office|no-gpu>/ (byte-stable renders); host/ (the recorded box, the runner's settings); pinwatch/ (the recorded hub and PyPI replies); courts/ (a fictitious CSV and hand table, malformed maps, lockfiles)
│   ├── regenerate_render_fixtures.py   # rewrites fixtures/render deliberately (the drift test names it)
│   └── contract/            # self-hosted-only modules, no test_ prefix, each with its throwaway stack's files beside it (archi/tests.md)
├── tools/                   # repository tooling, never the product (not in the release image); stdlib + gideon.host, gideon.guardrail, gideon.api.stamp only, and gideon.evaluation for the turn harness (archi/tools.md)
│   ├── pinwatch/ · imagebuild/ · acceptance/ · turns/ · redact/ · courtmap/   # the pin watch (hosted CI); the build tool, the clean-VM harness, the turn harness's entry, the evidence redaction (the box); the court map generator (by hand)
│   ├── ownership.py         # the sudo hand-back (--out and the bytecode caches) the two harnesses share
│   └── gate.py · environment.py · cycles.py · tracker.py · judgments/ · signoffs/ · variants/ · archi.py · exportboundary.py   # the gate, its pins comparison, the cycles record, the board, the judgment, sign-off, and variant kits, the architecture check, the export list — the dev venv
├── bin/                     # the lifecycle scripts — the cycle launcher, the guard's git as one plain command each, the public export; excluded from the export
├── preflight.sh / install.sh / upgrade.sh   # thin entrypoints over the CLI (§2.2)
├── config/                  # site.example.yaml (Appendix C, authoritative); site.schema.json (generated, drift-tested); egress.yaml (the §2.3 allowlist, by group); triggers.yaml (the committed ADR-0049 trigger registry)
├── images.lock · host.lock · models.lock  # the §2.2 pins (below)
├── courts.yaml              # the §8.6 court map, generated by tools/courtmap/ and never hand-edited (archi/host.md)
├── requirements-dev.txt     # the dev and CI toolchain's pins — never the product's (§3)
├── migrations/              # forward-only SQL (ADR-0005), NNNN_<name>.sql in order; the runner owns schema_migrations
├── eval/                    # reference/<slice>/<list>.json (the gate's committed comparand, §18.5); seed/prototype-qa/ (frozen harvest tooling, ruff-excluded, do not modify); sets/eval-v1/ (the set's cases by suite — judgments/, build-gates/, judge/, research-qa/ — and its frozen slices, slices/<name>/<file>.ids; archi/eval.md);
│                            #   seed/guardrails/ and seed/general/ (the judge's and the stamp's seeds, General's load and smoke sets — superseded, never edited); engine-verify/ (the §6.7 sample, release content)
├── .claude/ · skills-lock.json   # the shared agent tooling (docs/agents/tooling.md); the Matt Pocock skills' lock
├── .github/                 # workflows/ (ci.yml, pin-watch.yml, acceptance.yml — §6); the contributor's issue and pull-request templates; dependabot.yml (§6)
├── docs/                    # ARCHI.md (the map), archi/ (the leaves), box-ledger.md (the shared box), adr/, agents/, runbooks/ (the operator runbooks), handoffs/, research/, teach/,
│                            #   release-notes/ (§11), the TRIP docs (1-plans … 6-memo; 4-unit-tests/TESTING.md)
├── .scratch/                # the work tracker — committed and AUTHORITATIVE despite the name
└── CONTEXT.md · README.md · CLAUDE.md · CONTRIBUTING.md · SECURITY.md · CHANGELOG.md · LICENSE · THIRD_PARTY_LICENSES.md · pyproject.toml
```

**Lock files** (repo root): `host.lock`, `images.lock`, `models.lock` — what each pins, the two image-pin kinds, and the pin watch's patch rule are in [`archi/host.md`](archi/host.md); no pin value is restated here or in any test module (§11).

**On-box paths**: `compose.yaml` is **never committed** — it is rendered into `/etc/gideon/rendered/` ([`archi/render-apply.md`](archi/render-apply.md)). Provision-owned data roots: `/data/backup-staging/` (`pgbackrest/` the repository; `sets/<label>/` one set per run — `manifest.json`, `secrets.tar.age`, `files/<root>/`; `push.json` the last push's coverage record; `rollback.json` a rollback in progress), `/data/drill/` (the throwaway drill project), `/data/acceptance/` (the acceptance harness's run directories, the build box alone), `/data/observability/{prometheus,grafana}/` (derived state, outside the backup set), and **`/data/models/`**, the weights tree (§5.4; derived state outside every set), written only by `models pull` and apply's `models` stage — the Hugging Face hub-cache layout under `hub/`, root-owned with 0444 blobs, and `gideon/pulls.yaml`, the pull record. `/var/lib/docker/containerd/` is the shared daemon's image store, both projects' images and the build cache — derived state outside every set (ADR-0040; [`archi/host.md`](archi/host.md)). Host state, not configuration, never in a set ([`archi/host.md`](archi/host.md)): the mode markers `/etc/gideon/no-gpu` and `/etc/gideon/build-box`, and `/etc/gideon/backup_age_identity`, the box's own backup identity (ADR-0042). Tool footprints: [`archi/tools.md`](archi/tools.md). `/opt/gideon` is the install home, TNMD's box's too (§1.9); a unit's `WorkingDirectory` is apply's checkout. Reserved: `corpus/lockfiles/` arrives with its slice.

## 5. Core Architecture Principles

1. **One CLI, thin entrypoints** (§2.2, §20.2): all logic lives in `python3 -m gideon`; `preflight.sh` / `install.sh` / `upgrade.sh` are three-line `exec` wrappers. New operator surface = a new subcommand, never a new script.
2. **The bare-host seam is load-bearing** (§1.5, §2.3): `gideon host provision` must run on a fresh Ubuntu Server install before anything is installed. The seam is enforced by CI, not convention (§9).
3. **Stubs refuse loudly**: every unimplemented command prints `gideon <command path>: not implemented` to stderr and exits non-zero. Nothing pretends to work; the surfaces that succeed today are §8's table (`host gpu` and the later-slice groups still refuse).
4. **Behaviour lands only under a TRIP plan, in slice order** (§22): slice 0 platform → 1 General → 2 eval harness → 3 corpus machinery → 4 Research go-live → …. The fog list in `.scratch/greenfield-spec/assets/24-trip-init-handoff.md` is a do-not-build list.
5. **The spec's cross-cutting rules bind all code** (§0.2): deterministic code or nothing (ADR-0006); citations computed, never generated (ADR-0019); no green state (ADR-0020); the record is Postgres + CAS, indexes are derived (ADR-0012); nothing edited in place — supersede, never overwrite; no `.env`, secrets are files (§1.7); no site key for behaviour (ADR-0028); OWUI never rebranded (ADR-0003).
6. **Never re-litigate a map decision.** A decision that implementation proves wrong is surfaced on the tracker as a supersession, never improvised around.

## 6. Build System & Toolchain

There is no build step: the package runs in place (`pythonpath = ["."]`) — from a checkout, `python3 -m gideon --help` works with zero installs on any Ubuntu ≥ 24.04. Packaging metadata (`pyproject.toml`, setuptools) exists chiefly to single-source the version and record the package set.

**The gate** (before any commit; CI's `Gate` step, with `--all`): `python3 -m tools.gate [--all] [--masked] [TEST_PATH ...]` — the environment compared with the tree's own pins, then ruff, mypy, then `pytest -x`, stopping at the first failure; the comparison is the first step and no flag skips it, so a verdict is always a verdict on the tree's pins and never on whatever a shared venv holds; `--masked`, which a release's and a hotfix's whole-suite run passes, runs the pytest step where the box's own paths read as empty, so a test that steps around its fake is red before the tag ([`archi/tools.md`](archi/tools.md); the rule is [`archi/tests.md`](archi/tests.md)'s).

The tools live in an untracked project venv — `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, or `bin/venv-build` in the tree that needs one of its own; CI's `checks` job installs from the same file, whose two copies §3's test holds to it. A tree is checked in an environment built from its own pin file and nothing is installed through a shared venv's link, which is `docs/agents/worktrees.md` §9's rule.

**CI** (trunk-based, §2.1: `main` always deployable, short-lived feature branches, bump pull requests merged by a human and never auto-merged, every slice a tagged release):

| Job | Trigger, runner | Does |
|---|---|---|
| `checks` (`ci.yml`) | push to `main` + PRs; **hosted** `ubuntu-latest` by design (§2.6), Python 3.14 | pinned tool installs → the host-import-boundary test as its own named step → the gate with `--all` |
| `mirror-images` (`ci.yml`) | push to `main` only, after `checks`, **never pull-request code**; the box's self-hosted runner (§1.8) | `registry mirror --to 127.0.0.1:5000` with the system Python, then `python3 -m unittest tests/contract/built_images.py` — a lock naming a digest nobody built is red on the box the same day |
| `frontend-contract` (`ci.yml`) | after `mirror-images`, same runner | `python3 -m unittest tests/contract/owui_apply_manifest.py`: a throwaway Postgres + frontend pair on loopback proves the apply manifest is desired state ([`archi/tests.md`](archi/tests.md)) |
| `search-sentinel` (`ci.yml`) | after `frontend-contract`, same runner, port, and concurrency group | `python3 -m unittest tests/contract/search_sentinel.py`: a throwaway search stack drives one General search carrying a synthetic marker and finds it in no log the stack writes ([`archi/tests.md`](archi/tests.md)) |
| `acceptance` (`acceptance.yml`) | every pushed `v*.*.0` tag, same runner; `concurrency: acceptance`; `workflow_dispatch` re-makes a tag's record | `registry mirror`, then `sudo python3 -m tools.acceptance "$ACCEPTANCE_REF"` and the same with `--full-restore`, each with its `--out`, through the runner's one sudoers line, the transcripts uploaded even on failure — the standing record of §22.1's proof (ADR-0035) |
| `watch` (`pin-watch.yml`) | Monday 06:17 CST on `ubuntu-latest`; `workflow_dispatch` takes `dry_run` | an installation token minted per run from the org's App, the App's bot as git identity, then `python3 -m tools.pinwatch` with the token as `GH_TOKEN` — what makes the bump PRs trigger `checks` (PRs from `GITHUB_TOKEN` never do) |

The workflows use the current action majors (node24 runtime, whose runner floor the pinned `gh_runner` clears). Dependabot (`.github/dependabot.yml`, export-ignored) proposes the action majors, grouped, and the dev toolchain's pins every Monday before the review; a human merges, completing on the branch a bump that has a copy (§3), and an action major whose runtime needs a newer runner waits for the pin watch's `host.gh_runner` pull request. No workflow test.

## 7. Configuration

- **Development**: everything in `pyproject.toml` (ruff/mypy/pytest sections). No other dotfile config.
- **Product runtime** (§3): one file, `/etc/gideon/site.yaml` — the office's facts and policies, never product behaviour (ADR-0028). [`config/site.example.yaml`](../config/site.example.yaml) is authoritative for keys, defaults, and allowed values (Appendix C) and ships in every release. No secret and no path lives in it. **No `.env` file exists, ever**, and no environment variable configures the CLI.
- **The site model, host state, and the other committed artifacts** — [`archi/host.md`](archi/host.md): `FIELD_REGISTRY` and `load_site()` (`gideon/host/site.py`) own the fully-defaulted site configuration and its schema; the no-GPU and build-box markers (`gideon/host/nogpu.py`) and the plain-registry rule (`images.is_plain_registry`) are host state, not configuration; `config/egress.yaml`, `images.lock`, and `models.lock` carry the allowlists, build inputs, hardware profiles, and model pins.
- **The trigger registry** — [`archi/improvement.md`](archi/improvement.md): `config/triggers.yaml` is committed release content, not a site key; its states move only by a pull request (ADR-0049), and `gideon/improvement/triggers.py` loads it.
- **Secrets as files, the release constants, and `/etc/gideon/rendered/`** — [`archi/render-apply.md`](archi/render-apply.md): generated and supplied secrets are file-borne (`gideon/host/secrets.py`), never CLI configuration; a release artifact that is not a site key is a constant in the module the leaf names; the `ARTIFACTS` registry (`gideon/host/render/`) emits the rendered tree, `manifest.yaml`, and the verified `applied.yaml` record.

## 8. Command Structure

One `argparse` tree in `gideon/cli.py` (`build_parser()`), grouped exactly as spec §20.2 plus `backup drill` (the [22] Answer; §20.2's omission is a recorded spec bug). Groups with subcommands mark them `required=True`; `--version` prints `gideon {__version__}`.

The dispatch pattern — every command follows it:

```
subparser.set_defaults(handler=<callable>, command_path="<group sub>")
main(): args.handler(args) -> int
```

- `handler` receives the parsed `argparse.Namespace` and returns an exit code; `main()` returns it, `__main__.py` passes it to `sys.exit()`. `command_path` is the command name used in refusals (later in audit rows).
- Host-path commands dispatch to `gideon.host.cli`, `run_<command>` → the module of the same name; the ordered commands sit behind `_guarded`, the command boundary that turns any unexpected exception into one refusal line; `run_gpu` and the later-slice groups use the shared `_stub`. Every real command takes injectable `host=`/path keyword arguments (and `client_factory=` where it talks to the frontend) so tests run it in-process over a fake `Host`.
- **Three registries, one shape**: `STEPS` (`host/steps/__init__.py`), `CHECKS` (`host/checks/__init__.py`), and `ARTIFACTS` (`host/render/__init__.py`) are ordered lists of *instances*; a new step, check, or artifact is an instance appended to its registry, and only the runners enumerate them.
- **Ordered commands print rows**: each stage is one `StageResult` row on stdout (`stages.run_stage`), the run stops at the first refusal, and exit 0 iff every row is ok. Each leaf names its commands' stages and the invariants they carry; the stage bodies are in the module and their argv, flags, and mechanics in its test ([`archi/tests.md`](archi/tests.md)).

One row per leaf, each command with its module:

| Command (module) | Leaf |
|---|---|
| `host provision` (`host/provision.py`) · `preflight` (`host/preflight.py`) | [`archi/host.md`](archi/host.md) |
| `render [--diff]` (`host/render/`) · `apply` (`host/apply.py`) · `secrets rotate <name>` (`host/rotate.py`) · `models pull` (`host/weights.py`) | [`archi/render-apply.md`](archi/render-apply.md) |
| `engine verify` (`host/engine.py`) · the frontend client (`host/owui.py`, `host/owuiturn.py`) · the branch gate (`compose/open-webui/functions/`) and the guardrail's judge (`gideon/guardrail/`) | [`archi/engine-frontend.md`](archi/engine-frontend.md) |
| `backup run` and `backup push` (`host/backup.py`) · `backup drill` (`host/drill.py`) · `restore` (`host/restore.py`) · `install` (`host/install.py`) · `upgrade <tag>` and `upgrade --rollback` (`host/upgrade.py`) | [`archi/backup-restore.md`](archi/backup-restore.md) |
| `alerts test` (`host/alerts.py`) · `users reconcile [--now]` (`host/users.py`) · `tls reload` (`host/tls.py`) · `registry mirror [--to]` (`host/registry.py`) | [`archi/stack.md`](archi/stack.md) |
| `eval run --slice <name> [--set <dir>] [--ranked <file>]` (`evaluation/command.py`) · `eval reference --run <id>` (`evaluation/reference_command.py`) | [`archi/eval.md`](archi/eval.md) |
| `python3 -m tools.variants` (`tools/variants/`) | [`archi/eval-slices.md`](archi/eval-slices.md) |
| `python -m gideon.api` (`gideon/api/`) | [`archi/api.md`](archi/api.md) |
| `python3 -m tools.gate [--all] [--masked] [TEST_PATH ...]` · `python3 -m tools.imagebuild` · `sudo python3 -m tools.acceptance` · `sudo python3 -m tools.turns [--browser]` · `python3 -m tools.pinwatch` and its `.notes`, `.hub`, and `.bumped` · `python3 -m tools.redact --site <file>` · `python3 -m tools.courtmap --csv <file>`, each the module under `tools/` it names | [`archi/tools.md`](archi/tools.md) |

**When a command gains behaviour**: replace its `handler` with a real callable (a new module under `gideon/`, or under `gideon/host/` iff it must run bare-host), keep `command_path` on the namespace and the return-int contract, cite the spec section in the help string (`help="… (§n)"`), add the command to its leaf's row above and to the leaf's front matter, and shrink `tests/test_cli_surface.py`'s `STUBS` list in the same change (`TOP_LEVEL` names the full surface).

## 9. The Bare-Host Import Boundary

The seam that makes §1.9 step 3 possible: `sudo python3 -m gideon host provision` runs on a bare Ubuntu Server install where only the stdlib and `python3-yaml` exist. `tests/test_host_import_boundary.py` enforces it **at the AST level** (nothing is imported to be checked), with three rules:

1. **Entry chain** (`gideon/__init__.py`, `__main__.py`, `cli.py`): *module-level* imports resolve only to the stdlib, `yaml`, or the checked set itself. Function-level imports are exempt — the documented mechanism for non-host commands to grow heavy dependencies later.
2. **Host subtree** (`gideon/host/**`): **every** import, at any depth, resolves to the stdlib, `yaml`, or the checked set. No exemptions.
3. **Shared modules** (`gideon/guardrail/`, which `host/engine.py` and General's service both call): the entry chain's rule — the trip writer's import of the Postgres driver sits inside the function no host path calls.

CI runs this test as its own named step so a violation is legible in the Actions UI. Anything the host path and the rest of the product both need must itself live in the checked set.

## 10. Input/Output Handling & Exit Codes

- **stdout** is command output: help, every command's per-item lines and stage rows (**including failing rows**, which carry their fix), its next-step lines, a child tree's rows passed through during an upgrade, and the print-once secret lines; a secret artifact's diff never shows its contents. **stderr** is pre-run refusals and errors: root required, the loaders' refusals, foreign rendered files, template loading, and the command boundary's `internal error` line. `gideon/host/report.py` holds the one refusal shape — `gideon <command>: <problem> Fix: <fix>`; the stub's is `gideon <command path>: not implemented`.
- **Exit codes**: `0` success (provision: nothing `failed` — `blocked`/`reboot-required` are expected mid-runbook states; `render --diff`: always, differences or not, except the foreign-file safety refusal); `1` stub refusal or operational failure; `2` argparse usage errors (its default). Every refusal and failed/blocked/refuse row prints its fix (§1.5's rule).
- No interactive prompts anywhere in the CLI; commands are scriptable — runbooks, CI, timers, and each other invoke them (§1.9, §3.6). A child of the seam never gets the terminal as stdin, and the entry point line-buffers stdout, so an ordered command's rows reach a pipe (a `tee` transcript, a unit's journal) as they happen.

## 11. Versioning & Release Streams

- **The version lives in exactly one place**: `__version__` in [`gideon/__init__.py`](../gideon/__init__.py). `pyproject.toml` reads it via `[tool.setuptools.dynamic]`; `--version` prints it. The release writes it there alone, after the rebase, and `bin/release-git` derives every other copy, the README's status tag held equal to it by `tests/test_release_records.py` (slice-1 ticket 60, workflow ticket 38).
- **Two release documents** (§2.1, §21): the changelog `docs/2-changelog/w<N>_v<x.y.z>.md` is the engineering record, omitted from the public export; the release note `docs/release-notes/v<x.y.z>.md`, written from the versioned `TEMPLATE.md`, is the user-facing artifact the CSAs send, its bumped-pins section generated ([`archi/tools.md`](archi/tools.md)), and the one `upgrade` reads a major's `## Breaking` section from, required at a major (ADR-0050). The root `CHANGELOG.md` has one line per release newest first (spec §2.2), linking the release note from `v0.2.0` and naming the tag as text below it; a hotfix tag has a line like any tag. `tests/test_release_notes.py` and `tests/test_release_records.py` hold the three ([`archi/tests.md`](archi/tests.md)), and the release skill's `user-facing-tag-check` and `changelog-index` blocks write them.
- **Product SemVer, monorepo, one stream** (ADR-0002, §2.1): breaking site-file/`/etc/gideon` contract change = major; new optional key = minor; else patch. Of the three product-wide streams (product `vX.Y.Z` · corpus `corpus-YYYY-MM-DD` · eval `eval-vN`) only the product stream is versioned in this repo's code today. The slice → tag table is §22.1 (`v0.1.0` platform … `v1.0.0` distribution).
- **Forward-only migrations; rollback is restore** (ADR-0005) — governs `migrations/`. `upgrade <tag>` moves a box to a tag a person named, from the old tree, behind the pre-upgrade set; `--rollback` restores that set and re-applies the previous release ([`archi/backup-restore.md`](archi/backup-restore.md)).
- **A pin moves only through a pull request** (ADR-0031, ADR-0033): the pin watch proposes, its body naming the research notes verified against the pin ([`archi/tools.md`](archi/tools.md)); a human merges; the tag is the human's; nothing on the box or in an image checks for its own updates (`tests/test_no_self_update.py` is the contract; the GitHub runner moves only through the watch's `host.gh_runner` pull request and `host provision`). A proposal — the Matt Pocock skills' pin, or a model bump the size rule flags — is completed by a person on its branch (`docs/agents/tooling.md` §3 for the skills).
- **A built image's digest is a fact recorded after the push, never a target** (ADR-0032): builds are not byte-reproducible, so `tools.imagebuild` records what the registry holds; a base or package bump is a proposal a human completes with a rebuild on the box, `inputs_digest` turning an un-rebuilt bump red before merge.
- **The acceptance run is at minor tags** (ADR-0035): a minor or major release runs `tools.acceptance` against the local tag between the tag and the merge (the `acceptance-run` block), then its `--full-restore` form, the box's newest set restored into a clean VM (§2.5 item 3, ADR-0042), an evidence commit carrying both runs' transcripts; the pushed tag runs both again on the box's runner as the record; patch tags never run it — the box's own upgrade proves them — unless the plan's to-dos name a run against the release's own tag, which runs the named form whatever the tag's kind (`v0.1.78`, `v0.1.79`).
- **No test pins a value it also reads from a lock file** (slice-0 ticket 13): `tests/test_lock_coupling.py` enforces it; the rule is written up in `docs/4-unit-tests/TESTING.md` ("Lock files in tests") and `docs/6-memo/lock-coupled-tests.md`.

## 12. Growth Plan — Where Behaviour Lands

How the skeleton becomes the product, per §22 (sequence normative, calendar not):

| Slice | Tag | Code it adds here | Leaf (`archi/<name>.md`; §2 links the landed ones) |
|---|---|---|---|
| 0 Platform | `v0.1.0` | **Complete**, declared done on the tracker at `v0.1.33` — the leaves' inventories are the record; its open follow-ons are the board's | `host`, `render-apply`, `engine-frontend`, `backup-restore`, `stack`, `tools`, `tests` |
| 1 General | `v0.2.0` | **Complete** at `v0.2.0`, the pre-launch release, with no user on the box until go-live (ADR-0044) — the leaves' slice-1 rows are the record; its open follow-ons are the board's | `host` (the profile and its memory table), `render-apply`, `engine-frontend`, `stack`, `tools`, `tests` |
| 2 Eval harness | `v0.3.0` | **In progress** — the `extraction` slice frozen and measured, the judge grading on its own, and the harvest's questions committed as `research-qa` cases in the unsigned state, stdlib alone (ADR-0046); what remains: the turn harness and the suites [11]–[15], decision and nightly runs and their board [16]–[18], the sibling stack [19]–[20], the graders [21] | `eval`, `eval-slices` |
| 3 Corpus + tranche 1 | `v0.4.0` | corpus/index commands, parsers, chunker, the worker `caselaw` path | `corpus` |
| 4 Research go-live | `v0.5.0` | the `/turn` service: plan → retrieve → gate → render (§§11–13) | `turn` |

Later slices (§22.1) bring authorities parsing, ingestion GA, and the 1.0 distribution flip. **The first service role has landed**: `gideon-api` (HTTP) shares the Python package while its interpreter and dependencies live in `images/gideon/`; the later `gideon-worker` (Procrastinate jobs) will join it under §17.1, with the host subtree untouched. The improvement loops (ADR-0049) have begun with the committed trigger registry, their leaf [`archi/improvement.md`](archi/improvement.md). The handoff note's build-time verification items are slice-time reading work, resolved via `/research` when their slice arrives.

## 13. Data Flow Diagrams

Execution today — the entrypoint chain and dispatch; each command's stages are in its leaf:

```mermaid
flowchart LR
    A["preflight.sh / install.sh / upgrade.sh"] -->|exec| B["python3 -m gideon"]
    C["operator / runbook / the timers"] --> B
    B --> D["gideon/__main__.py → cli.main()"]
    D --> E["build_parser() — argparse tree (§20.2)"]
    E -->|"host provision"| F["provision.py runner<br/>(host.lock + site.yaml via sysio.Host)"]
    F --> I["STEPS: the check/apply step classes"]
    E -->|"preflight"| K["preflight.py runner (+ egress.yaml, models.lock)"]
    K -->|"Phase A: check-only"| I
    K -->|"Phase B"| L["CHECKS: the install-time checks"]
    E -->|"render [--diff]"| R["render/command.py<br/>render_to_disk → /etc/gideon/rendered"]
    R --> RA["ARTIFACTS: pure render_all over the inputs<br/>(archi/render-apply.md)"]
    E -->|"apply"| AP["apply.py: the ordered stages, then converge<br/>(archi/render-apply.md)"]
    AP --> SE["secrets.py: absent-only generation"]
    AP --> R
    AP --> ST["stack.compose_argv / exec_argv → docker compose"]
    AP --> SO["stores.py: roles, databases, migrations;<br/>pgbackrest.py: stanza + check"]
    AP --> OW["owui.py: Client through Caddy;<br/>wait_ready → bootstrap"]
    AP --> TP["tls.probe_ingress"]
    AP --> W["weights.py: the hub-cache tree under /data/models,<br/>the pull record"]
    E -->|"models pull"| W
    E -->|"secrets rotate <name>"| RO["rotate.py: one secret rewritten, its consumers<br/>recreated, then apply's converge"]
    RO --> ST
    RO --> AP
    E -->|"engine verify"| EV["engine.py: the engine proven directly and through<br/>the frontend (archi/engine-frontend.md)"]
    E -->|"tls reload"| T["tls.py: validate_material → force_recreate caddy"]
    T --> TP
    E -->|"registry mirror"| M["registry.py: skopeo copy by digest → the release registry"]
    E -->|"eval run --slice <name>"| ER["evaluation/command.py: the gated, recorded run<br/>(archi/eval.md)"]
    E -->|"eval reference --run <id>"| EF["evaluation/reference_command.py: the reference<br/>(archi/eval.md)"]
    ER --> ST
    E -->|"users reconcile [--now]"| U["users.py: ldapsearch membership → frontend roles<br/>→ audit.py rows"]
    E -->|"backup run"| BR["backup.py: the set under /data/backup-staging/sets/<br/>(archi/backup-restore.md)"]
    E -->|"backup push"| BP["backup.py: push.json, one rsync over SSH,<br/>the off-box check"]
    E -->|"restore"| RS["restore.py: the whole source verified<br/>before anything stops (archi/backup-restore.md)"]
    E -->|"backup drill"| DR["drill.py: the gideon-drill project, an immediate restore,<br/>teardown"]
    E -->|"alerts test"| AL["alerts.py: receiver checked and tested → alerts_test row"]
    AL --> GR["grafana.py: Client through Caddy over the shared SNI connection"]
    AP --> GR
    BR --> SO
    RS --> ST
    DR --> ST
    E -->|"install"| IN["install.py: the phases as one ordered command,<br/>ending in the URL"]
    E -->|"upgrade <tag> | --rollback"| UP["upgrade.py: the new tree's commands behind the pre-upgrade set;<br/>--rollback restores it (archi/backup-restore.md)"]
    IN --> K
    IN --> AP
    IN --> BR
    IN --> EV
    UP --> K
    UP --> BR
    UP --> EV
    E -->|"host gpu, corpus, …"| G["_stub → 'not implemented', exit 1"]
    I & L & ST & OW & U & M & BP & AL & W & EV & ER & IN & UP & G --> H["exit code"]
```

Absent from this chain by design, each through the same `sysio.Host` seam: the pin watch runs on a hosted runner and touches nothing on the box; the build tool runs only on the box, never from the product CLI or CI, which only *checks* its result; the acceptance harness runs only on the build box and touches nothing of the office's stack; the turn harness runs only on the box as the eval identity, against the office's own frontend, whose every chat it deletes, or against General's service directly. The deployed-product topology is §20.1's table — not redrawn here until the code implements it.

## 14. Error Handling Strategy

Argparse handles usage errors; stubs refuse to stderr and exit non-zero. A real command never shows a traceback — a traceback is a bug:

| Shape | Where | Rule |
|---|---|---|
| `CheckResult` dispositions | provision steps | a step's raised exception is caught by the runner and rendered as a `failed` line with a fix |
| `StepFailure(detail, fix)` | a provision step's apply | rendered as `failed` with the step's own fix; any other exception gets the generic fix |
| `CheckReport` severities `pass/warn/refuse/inert` | preflight checks | the same exception-to-refusal guard; only `refuse` affects the exit code |
| collect-all loader errors | the site file's and the committed artifacts' loaders | every error reported at once, each ending in its fix |
| `report.Problem` | any module returning a failure | one problem-and-fix value, never a string with an embedded `Fix:` |
| `report.StageResult` / `print_stage`, `stages.run_stage` | every ordered command | one row per stage; the run stops at the first refusal; a failed row shows both streams of the child that failed it |
| `ConvergeReport`, `BootstrapReport`, `ReadyResult`, `ProbeResult`, `GrafanaError`, `TestResult`, `ModelOutcome`/`PullOutcome` | stores, owui, tls, grafana, alerts, weights | a `problem`/`fix` pair the calling stage prints verbatim; a failed `send` still reaches the `audit` row, with every recipient address redacted |
| `EngineReply`, `CheckOutcome` | engine verify | a problem-and-fix pair per check, content-free; every check runs after a failure, the audit row last |
| render's pure emitters | `render/*` | may raise (`ValueError` for a missing template or an unusable registry key); the command boundary turns it into one refusal line |
| `_guarded` | the ordered commands' handlers in `host/cli.py` | any unexpected exception becomes one `internal error` refusal line |

**Fail-safe guards** — where a wrong action is worse than a refusal: disk identity/blankness, sshd/ufw lockout, keypair regeneration (provision); a foreign file under `rendered/` refuses rather than being deleted; TLS material is validated before Caddy is touched; `applied.yaml` is written only after verify, so a failed apply retries with the same recreate set; a set is `.partial` until its manifest exists; the verify-before-act rules of [`archi/backup-restore.md`](archi/backup-restore.md); the engine's entrypoint, which refuses to serve without its key file; the branch gate, which raises its refusal before anything is called and whose own exception the pinned frontend's fail-open Filter chain would swallow, so it guards a client-side hiding and nothing more; and General's service, which answers a body its judge cannot read with a content-free `completion_unjudged` error rather than relaying it, stamps on its own internal error because a false stamp is harmless and a missed one the failure, and never awaits the trip's record, silent on its failure (ADR-0027, ADR-0039 superseded by ADR-0045); the gate's inlet is [`archi/engine-frontend.md`](archi/engine-frontend.md)'s, the judge's window, the trip, and the unjudged path [`archi/api.md`](archi/api.md)'s.

**Two disciplines**: a *sensitive* psql statement (one carrying a password) reports only its exit status, because psql diagnostics quote the failing line; audit writes follow an intent/applied protocol — the writer is probed before any mutation, a snapshot batch is one transaction, an interrupted run leaves a visible intent row rather than a lost one.

**Standing rules**: refusals print the fix (§1.5); fail loudly, never degrade silently (§12.7's rejection of dynamic degradation is the model); denials and trips are audit rows, never log lines (ADR-0027, §19.4); **no user or matter text in any error, log, or audit row** (§19.4 — ids only).

## 15. Testing Strategy

- **Framework**: pytest (pinned in `requirements-dev.txt`) as the runner over `unittest.TestCase` classes — stdlib assertions, no pytest-specific fixtures; `testpaths = ["tests"]`, `pythonpath = ["."]`; files `tests/test_<area>.py`. The conventions are in `docs/4-unit-tests/TESTING.md`: CLI tests in-process against `cli.main(argv)` with redirected streams, never subprocesses; dict-backed fake `Host`s, each module owning its own because `tests/` is not a package; only text crosses `Host.run`; template lists derive from `ARTIFACTS`; and §11's lock-value rule — derive from the loaded lock or own visibly fictitious lock text. Recorded fixtures: ticket 28's box (`tests/fixtures/host/baseline-post-reinstall/`, replayed by `test_host_steps.py`) and the box's runner settings files (`tests/fixtures/host/gh-runner/`).
- **The tests are contracts, not examples** — never weaken one to make a change convenient.
- The tripwires, one line each, the per-command pattern, and the contract tier: [`archi/tests.md`](archi/tests.md), which gains a tripwire in the release that lands it; a subsystem's own modules are listed in its leaf.

## 16. Security Considerations

Standing: **no secrets in the repo, ever** (product secrets are files under `/etc/gideon/secrets/`, §1.7); client/case data never leaves the box and never enters a prompt to an external model (§0.1 rule 2 — binding on dev sessions too); the public `TNMD-FDO/GIDEON` receives a filtered export of every tag (§2.6, read as slice-1 ticket 57's mechanism) — write everything as if public, and a vulnerability is reported privately per `SECURITY.md`, never as an issue. Postures the code holds today — the rule here, the mechanism behind each in [`archi/security.md`](archi/security.md):

- **Key material never enters Python**: every key and password reaches its consumer as a path (`*_FILE`), a Compose secret mount, a 0600 env file, a file read inside the consumer's own container, or stdin; a generated secret is minted absent-only into a 0440 file; none appears in argv, logs, refusals, or reprs.
- **Nothing sensitive in argv**: a credential, a token, or a SQL statement travels in a child's environment, a 0600 file, or stdin.
- **The hidden base model is withheld by the branch gate's inlet, not by the selector**: the hidden flag is client-side alone, so a `user`-role turn on any model that is not a preset is refused before the engine is called.
- **Least privilege**: a role holds its own tables' grants alone, the frontend's endpoint allowlist is exactly what the release calls, and a token, a key, or a sudoers line is scoped to one run or one module.
- **Observability** (ADR-0034): the metrics role reads and never writes, Grafana has no anonymous access and no sign-up, and every exporter is unpublished.
- **Evidence**: no secret or office value in the tree — evidence is redacted at capture — and the public repository, a **filtered export** of each tag, carries the application alone, `tools/exportboundary.py`'s list naming what it omits.
- **Backups**: the office identity is never written to the box, the tarball never exists as plaintext, and a secret rides off-box only inside it.
- **Network**: user-authored text leaves the box by search egress alone, only while `web.search` is on and after the user's confirmed toggle (§15), and reaches no log; published ports honour `lan_cidrs`; the self-hosted runner never executes pull-request code; the box never phones home for versions.

Slice-time postures the code must uphold: matter access re-checked below the UI, fail closed, every denial a conflicts-wall incident (ADR-0008); append-only audit (§19.4); egress only to the versioned allowlist (§2.3).

## 17. Deployment

Target state (§1.9, §3.6): a receiving office clones a tag to `/opt/gideon` → `sudo python3 -m gideon host provision` (with `--no-gpu` on a host without a GPU) → writes `site.yaml` → provision again (the site-dependent steps are `blocked` before the site file; the spec's step 3 omits this run, the runbook says so) → `./preflight.sh` → `./install.sh` → `gideon corpus install` + `index promote`. TNMD's box declares `--build-box` once; steps re-runnable; upgrades via `./upgrade.sh <tag>` with a mandatory pre-upgrade backup (ADR-0005). **Steps 3, 5, 6, and 7 are commands, proven end to end on the acceptance VM at every minor tag** and live on TNMD's box. No install step exists or is wanted for development. The box and the GitHub organisation are shared with the office's Gideon Transcribe project; `docs/box-ledger.md` records what each project owns there and the log between them.

**The operator sequence on a converged box**:

```bash
sudo python3 -m gideon host provision        # the first run on the build box uses --build-box and prints the backup public key and age identity once
python3 -m gideon registry mirror            # Docker access
sudo python3 -m gideon apply                 # the first run prints each break-glass password once and fetches the models; every run owns the stanza and the timers
sudo python3 -m gideon models pull           # the same converge by hand: a re-run verifies and fetches nothing
sudo python3 -m gideon preflight
sudo python3 -m gideon engine verify         # after install, upgrade, or any engine or driver change; install, upgrade, and rollback gate on it
sudo python3 -m gideon alerts test           # one message through the provisioned contact point and the relay; an alerts_test row
sudo python3 -m gideon users reconcile --now
sudo python3 -m gideon backup run            # then: backup push, backup drill
sudo ./install.sh                            # the six commands above as one ordered command, ending in the URL; safe to re-run
sudo ./upgrade.sh <tag>                      # the pre-upgrade set, then the new tree's provision/preflight/apply
sudo ./upgrade.sh --rollback                 # that set restored and the previous release applied
sudo python3 -m tools.acceptance v<x.y.0>    # the build box, at a minor tag: the whole sequence on a throwaway VM
sudo python3 -m tools.acceptance v<x.y.0> --full-restore    # then the box's newest set restored into a clean VM
# after any site-file edit: apply.  To go back in time: restore --from staging|target [--at | --set <label>], then apply.
```

An image bump reaches the running stack at the next `apply`, which recreates the bumped service by its Compose block (the recreate rule names it; Compose's own config diff is the backstop the row does not model). Off the box, every Monday a CSA reviews the open `pin-watch/*` pull requests and Dependabot's beside them (a frontend bump's on-box proof comes before its merge too); a built-pin proposal is completed by a rebuild on the box per `docs/runbooks/built-images.md` (build → record → commit → merge, never the reverse).

**What a receiving office must satisfy** (all preflight-checked; the requirements in `docs/runbooks/office-services-setup.md`): the directory's groups as DNs where AD's default container does not hold them, the bind account's read of `memberOf`, a `userPrincipalName` on every signing-in account (ADR-0030), SSH plus rsync on the backup target. The CSA runbooks under `docs/runbooks/`: `backup-restore.md` (the backup surface), `observability.md` (the pages and their fixes), and `install-upgrade.md` (install, upgrade, rollback) — release content, kept by the export.

## 18. Performance Considerations

None measurable in a stub skeleton. The product's performance contract is the spec's M-register (Appendix B: retrieval p95 ≤ 1 s, service p95 ≤ 6 s, …) and the defaults-first rule (ADR-0017): every figure is a starting value corrected by measurement on the box, never by argument. Nothing in this repo should hard-code a figure the E/M registers own.

## 19. Conclusion

The skeleton is deliberately thin: one package, one CLI, one enforced seam, contract tests, green CI — the principles of §5 held by the shapes of §§8, 9, 13, and 14. Everything else is recorded intent — implement it slice by slice from the spec, and update this map and its leaves (per docs/ARCHI-rules.md) as the code catches up.

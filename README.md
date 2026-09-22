# GIDEON

[![CI](https://github.com/TNMD-FDO/GIDEON/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TNMD-FDO/GIDEON/actions/workflows/ci.yml)

A fully local legal AI system for federal defender offices: corpus-grounded
legal research with computed, verified citations beside a general-purpose
assistant, on one box the office controls. Built by TNMD-FDO (the Office of
the Federal Public Defender, Middle District of Tennessee) and designed from
day one for distribution: clone a tag, edit `site.yaml`, run one script.

**Status:** 0.x — slice 0 (platform) complete at `v0.1.0`, the clean-VM acceptance at minor tags its standing proof; slice 1 (General) complete at `v0.2.0`, with no user on the box until go-live (ADR-0044); slice 2 (eval harness) under way toward `v0.3.0`; this tree is `v0.2.32`.
What each release changed is in [`CHANGELOG.md`](CHANGELOG.md), the index over `docs/2-changelog/`, one file per release. Later-slice commands still print "not implemented".

## Install

The receiving-office sequence (spec §1.9), run from the checkout:

1. Create the install home owned by the account that will own the checkout, never root, clone the tag into it as that account, and enter it:

   ```bash
   sudo install -d -o <account> -g <account> /opt/gideon
   git clone --branch <tag> https://github.com/TNMD-FDO/GIDEON /opt/gideon
   cd /opt/gideon
   ```

   The owner matters later: `upgrade` runs git as whoever owns the directory, so that account's credentials fetch the next release and no root-owned file lands in the tree.

2. Provision the host:

   ```bash
   sudo python3 -m gideon host provision
   ```

   The first run prints the backup public key and the age identity **once**: store the identity in the office password manager and authorize the public key on the backup target before going on. A `reboot-required` row means reboot, then provision again until every step reads `ok`. A host without a GPU is provisioned once with `sudo python3 -m gideon host provision --no-gpu`; every later command reads that declaration. TNMD's box is declared once with `sudo python3 -m gideon host provision --build-box`.

3. Write `/etc/gideon/site.yaml` from `config/site.example.yaml` (authoritative for keys and defaults), place the supplied secrets under `/etc/gideon/secrets/`, then pull the release's images into the loopback registry (Docker access):

   ```bash
   python3 -m gideon registry mirror
   ```

4. Provision again — the site-dependent steps are blocked until the site file exists:

   ```bash
   sudo python3 -m gideon host provision
   ```

5. Preflight — "will install work here":

   ```bash
   sudo ./preflight.sh
   ```

6. Install — preflight, apply, reconcile, engine verify, backup, drill, then the URL as the last line:

   ```bash
   sudo ./install.sh
   ```

   The first `apply` on a box generates the release's secrets and prints each break-glass password (the frontend's, Grafana's) exactly once, and fetches the profile's models. Install is safe to re-run on a live box: a re-run is a no-op apply, a fresh incremental backup set, a drill, and the URL.

7. Prove the page path through the relay once:

   ```bash
   sudo python3 -m gideon alerts test
   ```

From provision on, every command is re-runnable: a refusal prints its fix, and the same command is run again after it. `gideon corpus install` and `index promote` arrive with slice 3. `preflight.sh`, `install.sh`, and `upgrade.sh` are thin entrypoints over the same CLI: `sudo ./upgrade.sh <tag>` upgrades from the clean checkout (git runs as the checkout's owner, the new tree's commands as children, a mandatory pre-upgrade set taken first), and `sudo ./upgrade.sh --rollback` restores that set and re-applies the previous release. The runbook for this sequence, upgrades, rollbacks, and every refusal's fix is [`docs/runbooks/install-upgrade.md`](docs/runbooks/install-upgrade.md); what the office's services must provide is [`docs/runbooks/office-services-setup.md`](docs/runbooks/office-services-setup.md).

## Quickstart

From a checkout, no install step:

```bash
python3 -m gideon --help
```

`gideon host …`, `preflight`, `render`, `apply`, `secrets rotate`, `tls reload`, `registry mirror`, and `users reconcile` run from the checkout on a bare Ubuntu Server install using only the standard library and `python3-yaml` (spec §1.5, §3.6 — render runs before any image is pulled) — a boundary CI enforces. `render`, `apply`, `secrets rotate`, `tls reload`, and `users reconcile` need root; `registry mirror` needs Docker access; `alerts test` needs root and a converged `apply`. A host without a GPU is provisioned once with `sudo python3 -m gideon host provision --no-gpu`; every later command reads that declaration. TNMD's box is declared once with `sudo python3 -m gideon host provision --build-box`. `users reconcile` reports until given `--now`, and a rendered systemd timer runs it nightly.

The development toolchain is `requirements-dev.txt` — the five toolchain pins and, beside them, a copy of the `gideon` image's dependency set so mypy and the unit suite see the service's imports; never the product's path, which installs nothing from PyPI — in an untracked project venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

The gate, run before any commit and exactly what CI runs — the environment comparison, then lint, types, and the unit tests, stopping at the first failure, the venv found beside the tree:

```bash
python3 -m tools.gate
```

## Documentation

| What | Where |
|---|---|
| Decision records | [`docs/adr/`](docs/adr/) |
| Changelog index (what each release changed; `upgrade` prints a major release's `## Breaking` section from the files it indexes) | [`CHANGELOG.md`](CHANGELOG.md) |
| Reporting a vulnerability | [`SECURITY.md`](SECURITY.md) |
| How the code is built | [`docs/ARCHI.md`](docs/ARCHI.md) (the map) · [`docs/archi/`](docs/archi/) (one leaf per subsystem) |
| Site file example + editor schema | [`config/site.example.yaml`](config/site.example.yaml) · [`config/site.schema.json`](config/site.schema.json) |
| Host pins (OS, driver, toolchain, service artifacts) | [`host.lock`](host.lock) |
| Egress allowlist (the hosts each run must reach) | [`config/egress.yaml`](config/egress.yaml) |
| The court map (every CourtListener court id → circuit, state, level, name — where a site file's `jurisdiction` ids are looked up; generated by `python3 -m tools.courtmap` from CourtListener's bulk data and a hand table, never hand-edited) | [`courts.yaml`](courts.yaml) · [`tools/courtmap/`](tools/courtmap/) |
| Image pins (mirrored: source + digest; built: base + inputs + pushed digest) | [`images.lock`](images.lock) |
| The development toolchain, and a copy of the `gideon` image's set for mypy and the unit suite (what a fresh `.venv` and the hosted checks install; the box installs neither) | [`requirements-dev.txt`](requirements-dev.txt) |
| Model pins and hardware profiles (per profile: the `requires` minimums, the model set by Hugging Face repo + revision + per-file sha256 and size, the engine baseline the model is served with) | [`models.lock`](models.lock) |
| GIDEON-built images (`postgres` and `gideon`; Dockerfiles with no pin in them; `python3 -m tools.imagebuild <name>` on the box builds, records, and `--check`s) | [`images/`](images/) · [`tools/imagebuild/`](tools/imagebuild/) · [`docs/runbooks/built-images.md`](docs/runbooks/built-images.md) |
| The pin watch (one pull request per pin bump across the three product locks, the model pins and the skills' record included; `python3 -m tools.pinwatch --dry-run` from a checkout; `python3 -m tools.pinwatch.hub <repo>` prints a model's files block by hand; `python3 -m tools.pinwatch.bumped` generates the release note's "What was bumped" section) | [`tools/pinwatch/`](tools/pinwatch/) |
| Rendered-config templates (Caddyfile, the frontend's permission set and General's texts, `pgbackrest.conf`, the reconcile, backup, drill, and verify-all units, the Prometheus and probe configuration, Grafana's LDAP, provisioning, alert rules — the search probe's among them — and the three boards; SearXNG's settings are built in code from the site file) | [`compose/`](compose/) |
| Release notes (the user-facing note every tag from `v0.2.0` ships, written from the template at version 2, its "What was bumped" section pasted from the generator) | [`docs/release-notes/`](docs/release-notes/) |
| Backup, restore, and the drill (what runs, first-time setup, reading the rows, total-loss recovery) | [`docs/runbooks/backup-restore.md`](docs/runbooks/backup-restore.md) · [`docs/runbooks/office-services-setup.md`](docs/runbooks/office-services-setup.md) §3 (the Synology) |
| Observability and alerting (getting into Grafana, what pages and the fix for each, silencing, retention, inducing a page) | [`docs/runbooks/observability.md`](docs/runbooks/observability.md) · [`docs/runbooks/office-services-setup.md`](docs/runbooks/office-services-setup.md) §7 (the relay, the admins group, egress) |
| Install, upgrade, and rollback (the receiving-office sequence, upgrading, rolling back, re-runs and every refusal's fix) | [`docs/runbooks/install-upgrade.md`](docs/runbooks/install-upgrade.md) · [`docs/runbooks/backup-restore.md`](docs/runbooks/backup-restore.md) §6 (the pre-upgrade set) |
| The clean-VM acceptance (a throwaway KVM VM from the pinned image, the receiving-office sequence driven end to end at minor tags; `sudo python3 -m tools.acceptance <ref>` on the build box) | [`tools/acceptance/`](tools/acceptance/) · [`docs/runbooks/install-upgrade.md`](docs/runbooks/install-upgrade.md) §6 |
| The turn harness (a case set at GIDEON as the eval identity, one row per case with its class; the seed the first set; `sudo python3 -m tools.turns <cases> [--case <id>] [--stream] --out <dir>` on the box through the frontend's chat path, a long run in the quiet window or on a weekend; `--service` posting the same cases to General's service instead, signed in nowhere; a replaced answer's cause read with `--unfiltered --case <id> --out <dir>`, asked of the engine directly, past the service's judge; its browser mode `sudo .venv/bin/python -m tools.turns --browser <cases> --out <dir> [--probe-inlet]` from a users-group seat, the screen judged frame by frame) | [`tools/turns/`](tools/turns/) · [`eval/seed/guardrails/`](eval/seed/guardrails/) · [`eval/seed/general/`](eval/seed/general/) (General's smoke set) · [Research notes](docs/research/owui-chat-routes-machine-caller.md) · [browser surface](docs/research/owui-browser-surface-playwright.md) |
| The evidence redaction (one function replacing every office value the site registry's marked leaves declare with its site-key placeholder — the acceptance harness's transcripts and stored messages pass through it, and `python3 -m tools.redact --site /etc/gideon/site.yaml < draft > asset` is the same function over a session's transcript on the box, the office-values tripwire the check that the step was taken) | [`tools/redact/`](tools/redact/) · [`tests/test_office_values.py`](tests/test_office_values.py) |
| The engine-verify sample (the corpus-independent cases `sudo python3 -m gideon engine verify` runs after install, upgrade, or any engine or driver change: the needle, the structured case, the smoke, and the frontend section's two deadline-trap positives and one trip case; a case superseded, never edited) | [`eval/engine-verify/`](eval/engine-verify/) · [Research note](docs/research/vllm-engine-verify-requests.md) |
| The extraction grammar and its set (the exact objects in a question — U.S.C. sections, Guidelines ids, Federal Rules, and the bare section and bare rule that are found and never resolved — as versioned, bounded, standard-library patterns; the labelled cases, never edited, that gate each landed type's precision and recall in the hosted checks) | [`gideon/extraction/`](gideon/extraction/) · [`eval/sets/eval-v1/build-gates/`](eval/sets/eval-v1/build-gates/) · [`tests/test_extraction_set.py`](tests/test_extraction_set.py) |
| The eval run (`sudo python3 -m gideon eval run --slice extraction` on the box, from the release checkout on the system Python: the set loaded and digested, the slice scored, its per-case verdicts compared with the committed reference, the run and its results recorded as kept events, the gate's verdict the exit code; `--set <dir>` runs a set outside the release and records nothing; `--slice judgments --ranked <file>` scores a ranked list of gold-evidence coordinates against the attorneys' grades — nDCG@10, recall@50, and Hole@10, reported and recorded and never gated) | [`gideon/evaluation/`](gideon/evaluation/) · [`eval/sets/eval-v1/slices/`](eval/sets/eval-v1/slices/) · [`migrations/0004_eval_runs.sql`](migrations/0004_eval_runs.sql) · [ADR-0046](docs/adr/0046-gideon-eval-runs-from-the-host-checkout-on-the-standard-library.md) |
| The reference run (`sudo python3 -m gideon eval reference --run <id>` from the development checkout: one recorded run taken as a tagged release's reference, written as one content-free file per id list, so a case that passed there and fails now blocks by id and a mean never gates) | [`eval/reference/`](eval/reference/) · [`gideon/evaluation/reference.py`](gideon/evaluation/reference.py) · [ADR-0023](docs/adr/0023-eval-gates-are-zero-tolerance-and-per-case-continuous-metrics-compare-paired-never-as-a-mean-delta.md) |
| Forward-only migrations (the `gideon` database) | [`migrations/`](migrations/) |
| The live frontend, built-image, and search-sentinel contracts (self-hosted CI only) | [`tests/contract/`](tests/contract/) |

## Contributing

A bug or an enhancement is reported on the public repository, `TNMD-FDO/GIDEON`, through its issue forms; the CSAs triage it and answer on the issue.
A vulnerability goes through [`SECURITY.md`](SECURITY.md), never an issue.
Contribution is by invitation through the [access-request issue form](https://github.com/TNMD-FDO/GIDEON/issues/new?template=access-request.yml); a CSA decides each request under [`CONTRIBUTING.md`](CONTRIBUTING.md)'s dedication. This repository receives one commit per release, so a pull request here is not merged.

## License

Public domain: a US-government work (17 U.S.C. § 105) with a worldwide
CC0-1.0 dedication — see [`LICENSE`](LICENSE). Contributions are accepted
only under the same dedication — see [`CONTRIBUTING.md`](CONTRIBUTING.md).
A vulnerability is reported privately, never as an issue — see [`SECURITY.md`](SECURITY.md).

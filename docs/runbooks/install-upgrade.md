# Install, upgrade, and rollback (CSA runbook material)

`v0.0.25`. The two shell entrypoints a receiving office runs,
`./install.sh` and `./upgrade.sh`, are three-line wrappers over `python3 -m
gideon install` and `python3 -m gideon upgrade`. Install runs the existing
commands in order; upgrade takes a full set before changing the checkout so
rollback can restore the prior state. Each install, upgrade, and rollback is
an `audit_log` row an operator can query, never a log line. The
acceptance section below records the clean-VM exercise by two CSAs; it arrived
with `v0.1.0`.

## 1. The receiving-office sequence

### Before you begin

- **Operating system** — Ubuntu Server 26.04 on `x86_64`; provision refuses any other release or architecture.
- **Packages** — Install `git` and `python3-yaml` with `sudo apt install -y git python3-yaml`; this is safe to run if they are already installed.
- **Login** — Use the CSA's own Linux login with `sudo`, never root; this login will own `/opt/gideon`.
- **Hardware** — The reference profile `2x96v-256d` needs 2 × NVIDIA RTX PRO 6000 Blackwell Server Edition, 96 GB VRAM per GPU, 256 GB RAM, and a 4000 GB data volume; preflight refuses a shortfall. Alternatively, declare a no-GPU host with `--no-gpu` at step 2; preflight judges none of these figures on that host.
- **Download** — Allow about 31 GB of model files at the first apply, including on a no-GPU host.
- **Office services** — Have the directory, DNS, backup target, certificates, and mail relay ready as described in [office services setup](office-services-setup.md).

Every step is re-runnable; a refusal prints its fix and exits non-zero, and the
same command is run again after the fix.

1. **Clone the release** to `/opt/gideon` as the CSA's own Linux login, never
   root. Because `/opt` is root-owned, create and assign the directory first,
   then clone the release. The later commands run from `/opt/gideon`:

   ```bash
   sudo install -d -o <account> -g <account> /opt/gideon
   git clone --branch <tag> https://github.com/TNMD-FDO/GIDEON /opt/gideon
   cd /opt/gideon
   ```

   `<account>` is the CSA's own Linux login, the one `id -un` prints, never root
   or a GitHub account. `<tag>` is a release tag. For example, with login `csa`
   and tag `v0.3.0`, run `sudo install -d -o csa -g csa /opt/gideon` and
   `git clone --branch v0.3.0 https://github.com/TNMD-FDO/GIDEON /opt/gideon`.
   Tags are listed newest first in [`CHANGELOG.md`](../../CHANGELOG.md) and on
   the [repository's tags page](https://github.com/TNMD-FDO/GIDEON/tags).

   The owner matters later: `upgrade` runs git as whoever owns the directory,
   so no root-owned file lands in the tree. The repository is public, so the
   clone and `upgrade`'s fetch need no GitHub account or credentials.
2. **Provision the host**. One line per step; the first run prints the backup
   public key and the age identity
   **once** (store the identity in the office password manager before going
   on, `backup-restore.md` §2). A `reboot-required` row (the NVIDIA
   driver, a kernel) means: reboot, then run provision again until every step
   reads `ok`. The `wait-online` step (new in `v0.0.25`) makes the boot-time
   network wait accept any one link online, so a box with an unplugged second
   port no longer boots with a failed unit. Once provision has run,
   `gideon <command>` from any directory runs
   `sudo python3 -m gideon <command>` from `/opt/gideon`; this runbook keeps
   the long form because it is the only form available before this step.

   ```bash
   sudo python3 -m gideon host provision
   ```
3. **Write the site file** at `/etc/gideon/site.yaml` from
   `config/site.example.yaml`, and place the supplied secrets as described in
   `office-services-setup.md`. On the build box,
   `sudo python3 -m gideon registry mirror` pulls the release's images into its
   loopback registry, and where a receiving office pulls its images from is
   settled with the release registry at `v1.0.0`.
4. **Provision again**. The
   site-dependent steps — `egress-proxy`, `firewall`, `time-sync`, and
   `timezone` — are blocked until the site file exists, and preflight refuses
   on any unconverged step. This second run is required because those
   site-dependent steps cannot converge until the site file exists.

   ```bash
   sudo python3 -m gideon host provision
   ```
5. **Preflight**. Every provisioning step re-checked,
   then the install-time checks (the directory, the relay, the backup target,
   TLS material, ports, disk). Exit 0 iff nothing refuses. Install opens with
   the same preflight, so a refusal here is a refusal there.

   ```bash
   sudo ./preflight.sh
   ```
6. **Install**. Eight phases, each nested command printing
   its own rows first and install one phase row after it:

   ```bash
   sudo ./install.sh
   ```

   | Phase | Runs | What it proves |
   |---|---|---|
   | `preflight` | the preflight above | the office services are reachable and correct before anything is built |
   | `apply` | `apply` (thirteen stages) | the stack is up and verified; the first run prints each break-glass password **once** and fetches the profile's models into `/data/models` (about 31 GB, 10–30 minutes of office bandwidth; later runs verify them in about a minute). If an evaluation holds the engine lock, apply refuses naming its holder; wait for it to finish, then re-run install; its earlier steps are safe to repeat |
   | `reconcile` | `users reconcile --now` | directory membership is the frontend's role truth; audit rows written |
   | `engine-verify` | `engine verify` | the engine answers at length and in structured form, the guardrail's refusal arrives inside a real chat turn (three turns as the eval identity, two to eight minutes), and the embedding server returns unit vectors at the lock's width and records a batch's throughput; a failure stops install here with `Do not go live…`, the failing check's name, and that server's logs; a no-GPU host prints one skipped row (`v0.1.22`). If an evaluation holds the engine lock, verify refuses naming its holder; wait for it to finish, then re-run install; its earlier steps are safe to repeat |
   | `backup` | `backup run` | the first set exists (full on a fresh box, incremental on a re-run) |
   | `drill` | `backup drill` | that set restores into the throwaway project and the frontend comes up on it |
   | `audit` | — | one `install` row (phase `applied`) with the phase durations and the set label |
   | `url` | — | `https://<hostname>/`, the last line printed |

   A failed phase stops the run; its fix names the nested command to run by
   hand (`Run sudo python3 -m gideon backup drill, correct its refusal, then
   re-run install.`). Re-running install on a live box is safe: every nested
   command is idempotent, so a re-run is a no-op apply, a fresh incremental
   set, a drill, and the URL. An `install` row with phase `intent` is written
   once apply has converged the stores; a failure in preflight or apply leaves
   no row, because the writer does not exist yet and their own rows are the
   record.
   **The weights tree** (`v0.1.5`): apply's `models` stage, between
   `pull` and `recreate`, makes `/data/models` hold the profile's models in the
   Hugging Face hub-cache layout, every file verified against `models.lock`;
   `sudo python3 -m gideon models pull` runs the same converge by hand, and a
   re-run verifies and fetches nothing. The tree is derived state outside the
   backup set because it is derived from the model lock; after a rebuild,
   apply fetches it again. The pull
   keeps the pinned set and the newest previous complete set, so
   `upgrade --rollback` re-applies the previous release without a download;
   older sets are removed and each removal printed. `/data/models/gideon/pulls.yaml`
   is the pull's record; a pull that stops mid-file resumes on the next run,
   and a second pull started while one runs refuses naming it.
   **The engine** (`v0.1.6`): on a GPU host apply renders
   `gideon-generator` from the profile's `serve` block — the vLLM image at its
   `images.lock` digest, the recorded GPU 0 card reserved by UUID through
   CDI, `/data/models` mounted read-only, no published port — and starts it
   in the `start` stage. The model takes minutes to load, so `verify` waits
   for the model servers together for up to the longest of their allowances
   from the start
   stage (the same clock as each container's healthcheck), reports
   `model servers healthy N s after start` — the moment the last of them
   answered — and keeps every other service on its one-minute bound. The
   embedding server, `gideon-embed`, is the engine's sibling on the same
   image: the recorded GPU 1 card by UUID, a fixed share of that card held
   from its start, its own key `/etc/gideon/secrets/embed_api_key` read
   inside its container the same way, the same wait. It starts only when that share is free on the
   card; otherwise its logs name the shortfall and `verify` fails naming
   them. The engine's API
   key is the generated secret `/etc/gideon/secrets/engine_api_key`, created
   absent-only by the `secrets` stage and read inside the container — never
   in the Compose file, argv, or a row. The start-up log (the `GPU KV cache
   size` and `Maximum concurrency` lines) is in the journal at every boot:
   `sudo journalctl CONTAINER_NAME=gideon-gideon-generator-1 -b`. A vLLM image
   or model pin bump restarts the engine inside apply after the `models`
   stage; a rollback to a release without the engine removes its container as
   an orphan and leaves the key file. On a no-GPU host none of this is
   rendered.
   To rotate the key: `sudo python3 -m gideon secrets rotate engine_api_key`
   (`v0.1.40`). The command refuses first when `apply` has pending
   changes or has never verified (run `render --diff`, then `apply`), then
   prints what it will do — the engine recreated with a cold start, bounded by
   apply's engine wait, and `gideon-api`, its second mount, recreated in
   seconds — writes a new value to the same path as `root:gideon` 0440,
   force-recreates the engine once (a file secret is a bind mount and the new
   key reaches only a new container, the rule `tls reload` follows for Caddy),
   and converges the rest as apply does: verify, `applied.yaml` recorded, so
   `render --diff` then reports nothing. Since `v0.2.29` the frontend is not
   among the consumers: the engine's key left its env file at the cutover.
   The frontend's own connection key is `gideon_api_key`, and
   `sudo python3 -m gideon secrets rotate gideon_api_key` recreates
   `gideon-api` (its mount) and the frontend (its env file's owner).
   `sudo python3 -m gideon secrets rotate embed_api_key` recreates the
   embedding server alone, with a cold start bounded by the same wait;
   nothing else consumes its key yet. Once General is live, a rotation that recreates the
   engine is made in an announced maintenance window because the cold start
   makes General unavailable; the command does not gate on the clock. The same
   command rotates `webui_secret_key` (the frontend is recreated once and every
   signed-in session ends), `searxng_secret_key` (SearXNG recreated through the recreate
   rule; with search off nothing consumes it and the command says so and
   writes nothing), `qdrant_api_key` (the vector store recreated in seconds;
   nothing else consumes it yet), `qdrant_read_only_api_key` (the store
   recreated in seconds, then the frontend; signed-in sessions stay because
   the session key does not move), `opensearch_password` (the lexical store
   recreated, about ten seconds to healthy), `opensearch_transport_key` and
   `opensearch_transport_cert` (the lexical store recreated; rotating the
   certificate alone renews it from its standing key before its ten-year end
   date, and rotating the key re-issues the certificate with it), and the two minted keys `gideon_admin_api_key` and
   `gideon_eval_api_key` (the file removed and re-minted by the apply-manifest
   stage; nothing recreated). Every other name refuses before any change,
   naming the office's path: a supplied secret — `tls_key`: replace the files
   and run `tls reload`; `ldap_bind_password` and `smtp_password`: replace the
   file, run `apply`, then force-recreate Grafana by hand (`sudo docker compose
   -f /etc/gideon/rendered/compose.yaml up -d --no-deps --force-recreate
   grafana`), which mounts them and which the recreate rule does not see;
   `proxy_auth`: replace the file and run `apply` — a Postgres role password
   (the role itself holds the value), `gideon_admin_password`
   and `gideon_eval_password` (the frontend's account holds it),
   `grafana_admin_password` (Grafana's admin user is seeded from the file at
   first start only: change it through Grafana's password endpoint as the
   administrator with the old and new value in the request body carried on
   stdin, then rewrite the file in place; the rotation transcript is the
   record. A restore of a set made before a rotation brings the
   older value back: after that restore's `apply`, run `secrets rotate <name>`
   for every secret rotated since the set was made.
7. **Hand over the URL**, and test alerts once so the
   page path is proven through the relay (`observability.md`).

   ```bash
   sudo python3 -m gideon alerts test
   ```

`gideon corpus install <label>` fetches and verifies the files a committed lockfile pins, records the label as installing, and stages each court's rows under `/data/work/` (the release-files runbook's §10); its build, and `index promote`, arrive with slice 3.

**No-GPU host.** `sudo python3 -m gideon host provision --no-gpu` writes
`/etc/gideon/no-gpu` once. It is refused on a host with an NVIDIA device; every
later command reads the marker. The driver and toolkit steps are skipped. The DCGM
exporter, its scrape job, the GPU board, and the driver-drift rule are not
rendered. `--only` on a skipped step refuses and names the marker. To leave the
mode, remove the marker and re-run provision; the next apply renders the removed
files as new. The marker is never carried in a backup set.

**GPU record.** The first render on a GPU host, including `render --diff`, writes
`/etc/gideon/gpus.yaml` from `nvidia-smi -L` order; later renders bind each
`models.lock` GPU position to that recorded card. If a recorded card is no
longer reported, render refuses with its position and UUID; after a card
change, remove the file as root with `sudo rm /etc/gideon/gpus.yaml`, then run
`sudo python3 -m gideon render --diff` to re-record the cards and inspect what
moves before applying. The record is never carried in a backup set.

**Build box.** The KVM, registry, and runner steps (`kvm`, `registry`, and
`gh-runner`) converge only on TNMD's box, declared once with
`sudo python3 -m gideon host provision --build-box`, which writes
`/etc/gideon/build-box`; every other host skips them, and a no-GPU host cannot
be declared the build box (nor the build box a no-GPU host). The marker is never
carried in a backup set. A host that already runs the three units when it
crosses `v0.1.68` runs `sudo ./upgrade.sh <tag>` (its provision skips the three
with a reason naming the declaration, their units still running, and its apply
renders the host-unit rule and the runner's unit pattern away), then
`sudo python3 -m gideon host provision --build-box` (the three `ok`), then
`sudo python3 -m gideon apply`, which renders the rule and the pattern back; the
transcript is kept as §2 says. TNMD's box was declared on 2026-09-16, at
`v0.1.71`'s release, before any upgrade, and its crossing — the marker
removed to leave the mode, then the three commands — was recorded that way. To leave
the mode, remove the marker, run provision again, then apply; the three units
stay until removed by hand, and until they are, `restore` refuses on a host
whose registry is still active.

## 6. The acceptance run

The acceptance harness builds a throwaway VM from the pinned image, converts it
to the LVM layout used on the box, and proves the receiving-office sequence in a clean
environment. The VM has its own office services: the real directory is reached
over NAT, the harness runs a STARTTLS + AUTH SMTP sink on the libvirt bridge,
the VM is its own backup target, and a throwaway CA is bundled with the office
root. The sequence is exactly the one in §1 above. Every product command runs as
`sudo … 2>&1 | tee`, with its transcript retained under `--out`.

The harness prints one row per stage on its own stdout — that output, under
`sudo … 2>&1 | tee`, is the run's first record. A transcript file under `--out`
exists for every product command run inside the VM, named by the stage's
two-digit position in the table:

| Stage | Product command in the VM | Transcript under `--out` |
|---|---|---|
| `provision` (7) | `host provision --no-gpu` | `07-provision.txt` (`-2`, `-3` after a `reboot-required` row) |
| `provision-2` (10) | `host provision --no-gpu` after the site file | `10-provision.txt` |
| `preflight` (12) | `./preflight.sh` | `12-preflight.txt` |
| `install` (13) | `./install.sh` | `13-install.txt` |
| `alerts` (14) | `alerts test` | `14-alerts.txt` |
| `rehearse` (16) | `./upgrade.sh <rc tag>`, `--rollback`, again, again | `16-rehearse-1-upgrade.txt` … `16-rehearse-4-rollback.txt` |
| `restore` (17) | `backup push`, `restore --from target`, `apply` | `17-restore-1-push.txt`, `17-restore-2-restore.txt`, `17-restore-3-apply.txt` |
| `verify` (18) | `./preflight.sh` | `18-verify-preflight.txt` |

Beside them: `console.log` (the VM's serial console, the boot transcript when
SSH never answers) and `mail/` (every message the sink accepted, with its
authenticated user and TLS fact). The other stages — preconditions, image, seed,
services, boot, clone, reboot, site, authorize, probe, teardown — run on the box
and leave only their row.

`--until <stage>` stops after a named stage (`provision-2` names the second
provision) and `--keep` leaves the VM defined and running: the harness prints
its address, and the run's SSH key and known-hosts file stay under
`/var/lib/libvirt/images/gideon-acceptance/<name>/`; the next run tears a
same-named leftover down first, along with the sink's firewall rule. The runner is permitted to invoke the harness
by the one sudoers line the `gh-runner` provision step converges; it is already
root-equivalent through the docker group, never runs pull-request code, and the
line names one checkout module.

Acceptance runs happen at minor tags only: run against the local tag before
the push, then run again on the pushed tag and keep its transcripts as an
artifact. Patch tags never run the harness. A tag can name any commit its
pusher can reach, so the trust is the two CSAs authorized to create a `v*` tag,
not anything in the tag's tree; a `v*` ruleset restricts who can create tags an
office clones. The release's two-CSA run proves the install sequence in a clean
VM and replaces the earlier by-hand exercise.

Transcripts redact print-once values and age identities before they reach the
host; an evidence check scans them for a complete age secret or an unredacted
print-once line. On TNMD's box a complete run — image conversion, boot, two
provisions, preflight, install with the drill, `alerts test`, the four
rehearsal legs, the push and target restore, verify — takes under six minutes
(the plan estimated 45); the converted image boots in about nine seconds. The
candidate tree's passing run is `08-acceptance-run-2026-09-03/`.

**What the runs of this release found.** Each issue was fixed in the same
release. The firewall step's `DOCKER-USER` drops judged every
forwarded packet to ports 443 and 5000, the VM's NAT egress included, so no
HTTPS download from the VM could succeed — the drops now name the Docker bridge
they forward into; a fresh cloud image ships empty apt lists, so the first
`apt-get install` could not locate `skopeo` or `age` — every install now
updates first; Python 3.13+ verifies TLS chains strictly, so the throwaway CA
carries a key-usage extension and each leaf its usages and server-auth purpose;
and apply's registry and pull stages judged every `images.lock` pin while
Compose pulls only the rendered services, so the DCGM exporter — never rendered
on a no-GPU host — failed digest verification; apply now judges the rendered
stack's images; and the restore's `verify` stage refused an empty root, because
`sha256sum -c` refuses an empty list and `/data/registry` holds nothing on a
no-GPU host — a root with no files is now verified without the call; and after
the rehearsal's two promotions a `restore --from target` refused because
pgBackRest's default recovery follows the cluster's current timeline, which had
forked before the set's backup — every set restore now names its own backup,
that backup's timeline, and its target, the third timeline lesson after
`v0.0.27`'s two. A failed
provision row also carries the failed command's own text now, which is what
made the second of these visible.

**The `v0.1.0` run.** The local-tag acceptance run on TNMD's box, 2026-09-03T21:28:56-05:00 to 2026-09-03T21:34:41-05:00, before the push: every stage ok, exit 0 — the VM installed to its URL, `alerts test` delivered through the sink, the four rehearsal legs on `v0.1.1-rc.1`, the push and the target restore, verify at release 0.1.0 by the applied record and the checkout's `--version`, sixteen authenticated TLS messages, thirteen transcripts, the VM torn down. The pushed tag's own run is the standing record.

**The CI record.** The pushed tag's run refused at `boot`: its output directory was relative to the runner's workspace, while libvirt opened the console log from its own working directory; local runs had used an absolute output path. The root run also left root-owned bytecode caches in the workspace, which made the next checkout fail because the runner could not remove them. Hotfix `v0.1.1` uses an absolute output path, avoids writing bytecode after the harness starts, returns the checkout's caches with the transcripts, and lets an operator dispatch the acceptance workflow again for a selected tag. The standing rerun against `v0.1.0` passed every stage from 2026-09-04T02:51:53Z to 2026-09-04T02:57:48Z; its transcripts were preserved as an artifact, and the workspace held nothing root-owned afterward. A red tag run whose cause is the harness or the workflow, not the tag's tree, is re-made the same way, by dispatching the acceptance workflow at that tag.

**The `v0.2.0` run.** Both acceptance forms ran against the local tag on TNMD's box from the release worktree before the push: the no-GPU install passed every stage in 1252 seconds, with `alerts test`, four rehearsal legs on `v0.2.1-rc.1`, the push and target restore, sixteen authenticated TLS messages, and thirteen transcripts. The full-restore form then restored the box's newest set from release 0.1.81; it passed every stage in 1196 seconds and the run directory peaked at 155 GB. The box itself then upgraded 0.1.82 to 0.2.0 from the checkout on `main`; `engine-verify` passed. The run transcripts are kept with the release records, and the pushed tag's own two-step run is the standing record.

**The `v0.3.0` run.** The tag was cut twice. At the first cut, the install form went red twice on the office resolvers answering `0.0.0.0` for `huggingface.co` (cleared by an allowlist entry for the box), and once at its in-VM restore, which ran inside the 01:00 backup's minutes and passed on the next run started outside that hour. The full-restore form then went red at its second `apply`: the restored applied record names the build box's nightly units, which the no-GPU VM never linked, and `apply` refused to disable them. That fix moved the tag. At the re-cut tag, the no-GPU install passed every stage in 1137 seconds (eleven authenticated TLS messages, thirteen transcripts, about 52 GB allocated), and the full-restore form restored the box's newest set from release 0.2.98 and passed every stage in 1101 seconds (about 168 GB at its peak). The box then upgraded 0.2.98 to 0.3.0 from `/opt/gideon` in about six minutes; its apply recreated nothing, and `engine-verify` passed. The run transcripts are kept with the release records, and the pushed tag's own two-step run is the standing record.

## 2. Upgrading: `sudo ./upgrade.sh <tag>`

Run from the checkout, as root, with a clean work tree: no change to a tracked
file. An untracked file is no obstacle, so `sudo ./upgrade.sh <tag> 2>&1 | tee
<transcript file>` keeps the command output with the office's records. The
command runs from the *current* release's tree and hands over to the new tree's
own CLI after the checkout; nothing of the new release is loaded into the
running process.

**From go-live.** A tag is user-facing from the office's first
users; before them the rules below do not bind. The **quiet window** is
weeknights 19:00–06:00 and Friday 19:00 to Monday 06:00 in the site's timezone
(TNMD: America/Chicago). Work is window-bound when it sends requests to the
engine on GPU 0. The one scheduled thing that does is the nightly:
`gideon-eval-nightly.timer` fires at 21:00 office time every night, weekends
included, and runs the `general-smoke` and `guardrails` suites of kind
`nightly`; each is refused outside 19:00–06:00, waits while another GIDEON run
holds the engine, and is stopped at 06:00 and recorded partial. A **maintenance
window** is a span inside the quiet window, announced at least one working day
ahead, weekend nights by default — the only sanctioned unavailability. An
engine swap, a driver, engine, or Docker change, and every upgrade on a
user-facing tag take one. Read its cost beforehand from `render --diff`'s
recreate row; count on the whole stack only when `provision` or a rollback
needs it. Before the window, send the release's note from
`docs/release-notes/<tag>.md` to the users and inform the supervisor, both at
least one working day ahead. The release note's `## Breaking` section is what
the `version` stage below prints.
A model upgrade's proof window precedes its tag: announce it under
`docs/runbooks/model-upgrade.md` §4, then send the release's note with the tag.

| Stage | What happens | Fix on a refusal |
|---|---|---|
| `preconditions` | root, a valid site file, Docker Compose answering, the checkout a git work tree with a clean status, its owner resolved from the directory | commit or stash as the owner; re-run |
| `fetch` | `git fetch --tags origin` as the owner; the tag must resolve to a commit. A failed fetch with the tag already present continues and says so | fetch by hand as the owner, then re-run |
| `version` | the tag's tree must declare the version its name says; a lower version refuses (naming `upgrade --rollback` and `restore`); the same version is a re-run; **a different major prints the release note's `## Breaking` section first and refuses without `--acknowledge-breaking`** | read the section, then re-run with the flag |
| `preflight` | the current tree's install-time checks refuse as `./preflight.sh` does. Provision steps are reported here; an unconverged step warns instead of refusing because the new release's `provision` stage converges it and its `preflight` judges it | correct the office service it names; re-run |
| `backup` | the pre-upgrade set, `pre-<tag>`, a full set whose manifest must record the commit the checkout is leaving (rollback's way back). When the plain label exists from an attempt that never crossed the checkout, a suffixed `pre-<tag>-<timestamp>` set is taken (the earlier one may predate later writes). When the checkout already stands at the tag, an earlier attempt crossed it: the newest `pre-<tag>[-…]` set naming another release is **reused**, never re-taken | the backup's own rows carry the fix |
| `audit-intent` | one `upgrade` row: from-version, to-tag, both commits, the set label | the Postgres service; re-run |
| `checkout` | `git checkout --detach <tag>` as the owner (skipped when already there) | re-run |
| `provision` | the **new tree's** `host provision`, with `--acknowledge-disruption` passed through when given, its rows streamed as they happen; exits 0 on `blocked` and `reboot-required` rows | a failed child names its refusal and the fix to re-run `upgrade <tag>`, with `--acknowledge-disruption` when the provision row asks for it; the next stage judges `blocked` and `reboot-required` |
| `preflight` | the new tree's preflight: the gate for apply and the new release's own readiness checks. An unconverged or `reboot-required` step, or a new office-services requirement, refuses here — nothing of the product has changed yet | **reboot if asked, then re-run `upgrade <tag>`**: the checkout is already made and the set is reused, so the re-run resumes here |
| `apply` | the new tree's `apply` (thirteen stages, streamed; a pin bump in `models.lock` fetches the new revision here, before the engine restarts) | `upgrade --rollback`; but when the row names the engine lock's holder, nothing was changed: wait for that evaluation to finish and re-run `upgrade <tag>`, which the version stage accepts as a re-run |
| `verify` | the new tree answers `--version` with the tag's version, the applied record names it, every service the rendered project declares has a container that is running and healthy on a fresh read | `upgrade --rollback` |
| `engine-verify` | the new tree's `engine verify` as a passthrough child; its failing row names its own server's logs | `upgrade --rollback`; but when the row names the engine lock's holder, nothing was changed: wait for that evaluation to finish and re-run `upgrade <tag>`, which the version stage accepts as a re-run |
| `audit-applied` | the `upgrade` row with the durations | — |

The standing next step after a failure depends on where it happened: before the
checkout, correct and re-run; at the new tree's provision or preflight, reboot
or correct and re-run (or `upgrade --rollback`, which then only moves the
checkout back); at or after the new tree's apply, `upgrade --rollback`. Every
refusal prints exactly that.

An upgrade takes about the time of a preflight before and after the checkout
(two relay test messages), one full set (TNMD: about 35 s), a provision check
pass, an apply, and the `engine-verify` gate (about two to eight minutes). The
pre-upgrade set is not pushed; the nightly unit pushes.

## 3. Rolling back: `sudo ./upgrade.sh --rollback [<tag>]`

The pre-upgrade set *is* the record of what to restore: its manifest carries
the checkout's commit, the release, and the archive boundary. Rollback restores
that set and applies the previous release, returning both data and product
state to the saved point. How far a
rollback got is a second, small record, `/data/backup-staging/rollback.json`,
which exists only while one is in progress (the `plan` row below).

| Stage | What happens |
|---|---|
| `select` | the newest complete `pre-v…` set (with or without a timestamp suffix), or the one for `<tag>` when given; an operator's other `pre-*` labels and the `pre-rollback-*` safety sets are never candidates. Its commit must exist in the checkout. When the running tree already is the set's release *and* the applied record names it, there is nothing to roll back to (use `restore --from staging --set <label>` when it is the data, not the release, you want back); when only the tree is — a rollback that stopped after its checkout — the re-run resumes from the previous tree |
| `plan` | whether a restore is needed, judged from `rendered/manifest.yaml` — the first thing an apply writes, before any stage touches the stores. A manifest still naming the set's release proves the new tree's apply never completed a render: the rollback only moves the checkout back and re-verifies. Anything else (another release, a missing or unreadable manifest) means a restore. The verdict is written to `/data/backup-staging/rollback.json` (beside `push.json`), the record of the rollback in progress: updated when the restore is done, removed once the `rollback` row is written, and what a re-run resumes from |
| `safety` | when a restore is needed and the whole stack is running, a full `pre-rollback-<timestamp>` set taken by the *current* tree, so its files, checkout copy, and database describe the release that is running. A partial or stopped stack gets none, and the row says that anything written since the upgrade began is not preserved |
| `audit-intent` | a `rollback` row when Postgres answers; when it does not (the reason for many rollbacks) the row is deferred and the applied row later says `intent_recorded: false` — never a silent skip |
| `stop` | when a restore is needed, `compose down` whatever is running, so the previous tree's restore finds a stopped stack and takes no pre-restore set of its own |
| `identity` | reads the selected set's manifest: a set sealed to one recipient was made by a tree that knows no box identity, whose nightly would snapshot it, so `/etc/gideon/backup_age_identity` is removed and the row says so; a set sealed to two keeps it so this box can open its own sets. The next `upgrade` mints a fresh one in its provision phase |
| `checkout` | the tag pointing at the manifest's commit when one does, else the commit; as the owner |
| `restore` | when needed, the previous tree's `restore --from staging --set <label>`: verified whole before anything is replaced, Postgres back to the set's archive boundary |
| `apply` | the previous tree's `apply` — restore leaves the frontend and ingress down and names apply as the next command; here the command runs it |
| `verify` | as the forward path, against the set's release |
| `engine-verify` | the previous tree's `engine verify`; if it refuses, read the failing row's own server logs and run `engine verify` by hand, never going live |
| `audit-applied` | the `rollback` row |

**What is preserved and what is not.** The database, `/etc/gideon`, the
registry, and the frontend's bulk data return to the pre-upgrade set's state:
anything written after the set was taken (a user's chat during a failed
upgrade) lives only in the `pre-rollback-*` safety set, when one could be
taken. **Host state converged for the newer release stays converged** because
rollback restores the product from its set, not the host's drivers or packages.
A driver or package rollback is a `host provision` decision a person takes,
never something rollback does.

A rollback by `upgrade --rollback` or `restore` to a tag before the release
that moved GIDEON's firewall rules into their own chain restores a firewall
writer that declares and flushes the shared `DOCKER-USER` chain. Its first ufw
reload erases a co-tenant application's rules there, and every later provision
run on that older tree erases them again until GIDEON upgrades back. GIDEON's
own published ports stay protected by the older rules. Announce the firewall
consequence and the hour with the rollback; tell the co-tenant to re-add its
rules after the old tree has run, and announce the return the same way.

## 4. Re-runs and refusals

- `install` again on a live box: a no-op apply, an incremental set, a drill,
  the URL. Safe at any time.
- After editing `/etc/gideon/site.yaml`, run `sudo python3 -m gideon apply` to
  converge the running stack to the site file. If the edit changed `alerts.*`,
  follow apply with `sudo python3 -m gideon alerts test` (`observability.md` §4).
  A renewed certificate is placed in the certificate and key files, then picked
  up with `sudo python3 -m gideon tls reload`; it is not a site-file edit
  (`office-services-setup.md` §4).
- `upgrade <tag>` again after a reboot: the checkout is already at the tag, the
  set is reused, and the run resumes at the new tree's provision.
- A provision refusal naming running containers outside GIDEON's ownership mark:
  announce a maintenance window to every project sharing the daemon (§9), then
  re-run `host provision` or `upgrade <tag>` with `--acknowledge-disruption`.
  When the release running the upgrade has no such flag on `upgrade`, the
  checkout is already at the tag: from `/opt/gideon`, run
  `sudo python3 -m gideon host provision --acknowledge-disruption` by hand,
  then re-run `sudo python3 -m gideon upgrade <tag>`, which resumes at the new
  tree's provision and reuses the set.
- `upgrade <tag>` again after an attempt that stopped before the checkout: a
  fresh suffixed set is taken (the stale plain-label set is left alone); the
  stale set can be pruned by hand.
- `upgrade <tag>` at the tag with no earlier set naming another release: the
  record is gone; go back by hand with `restore --from staging --set <label>`
  of an earlier set.
- `upgrade <current version>`: a re-run; `upgrade <lower>`: refused, naming
  `upgrade --rollback` and `restore`.
- A tag whose tree declares another version: refused (retag).
- A dirty checkout: refused before anything else (commit or stash as the
  owner). A root-owned checkout runs git without `sudo -u`.
- `--rollback` with no `pre-v*` set: refused, naming `restore`.
- `--rollback` again after a failure: the same command resumes from
  `/data/backup-staging/rollback.json` — the restore repeated when it never
  finished, skipped when it did, then apply and verify. With no record and the
  running tree and the applied record both at the set's release, there is
  nothing to roll back to: refused. A record naming another set refuses until
  that rollback is finished (`--rollback <its tag>`) or the file is removed by
  hand after checking.
- `restore --set` with `--at` or `--from target`: refused (`--at` for a point in
  time, `--from target` for the off-box copy); a `.partial` or unknown label
  is refused in `select`, listing the complete labels.
- `secrets rotate <name>` again: a second rotation is a rotation — a fresh
  value, the consumers recreated again. A rotation that failed after its value
  was written is finished by `apply`, which converges the carriers (the env
  file's owner, a minted key's re-mint, the record) and clears the command's
  pending-change precondition; where a mounting consumer (the engine) was not
  reached, run `secrets rotate <name>` once more after that `apply`. A pending
  `apply` — a site edit, a new checkout, no verified apply on record — refuses
  the command until `apply` has run.

## 5. The record: TNMD's box, 2026-09-03

`v0.0.25` reached the box through `sudo ./install.sh`, not through `upgrade`:
the release before it had no upgrade command, and the checkout already stood at
the tag. The first attempt refused at its opening preflight because the release
had added the `wait-online` step and provision had not run yet — the order §1
gives is enforced; after `host provision --only wait-online` the eight phases
ran: a no-op apply, an incremental set, a passing drill, the URL
(`08-install-rerun.txt`, `08-wait-online.txt`).

The rehearsal ran on throwaway tags, never pushed, and found two defects, each
fixed by a hotfix the same afternoon:

| Leg | Command | What it proved |
|---|---|---|
| 0 | `upgrade v0.0.26-rc.1` on `v0.0.25` | stalled at the backup stage's readiness probe: a nested `sudo -u` under `sudo-rs`'s `use_pty` and a `compose exec` reading the terminal; aborted cleanly, nothing changed (`08-rehearsal-0-stalled-on-v0.0.25.txt`); hotfix `v0.0.26` |
| A | `upgrade v0.0.27-rc.1` on `v0.0.26` | the full set `pre-v0.0.27-rc.1`, the checkout as the owner, the rc tree's provision 20/20, preflight, apply, verify (`08-rehearsal-1-upgrade.txt`) |
| A2 | the same again | the set reused, the checkout already at the tag, a clean re-run (same file) |
| B | `--rollback` | restore needed, the safety set, the stack stopped, the checkout to the `v0.0.26` tag, the previous tree's restore to the boundary, apply, verify, the row (`08-rehearsal-2-rollback.txt`) |
| C | `upgrade v0.0.27-rc.1` again | the suffixed set `pre-v0.0.27-rc.1-<ts>` (`08-rehearsal-3-upgrade-again.txt`) |
| D | `--rollback` again | pgBackRest refused the Postgres restore: its auto-selection skipped the set's own backup for one on the pre-rollback timeline; finished by hand with `--set` (`08-rehearsal-4-rollback.txt`, `-4a-`, `-4b-`); hotfix `v0.0.27` |
| E | `upgrade v0.0.28-rc.1` on `v0.0.27` | as A (`08-rehearsal-5-upgrade.txt`) |
| F | `--rollback` | first refused by leg D's stale in-progress record, as designed; then a rollback across two earlier promotions, the set's own backup restored, apply, verify, the row (`08-rehearsal-6-rollback.txt`) |

Each rollback's unavailability, from its `stop` row to apply's `verify`, was
about three minutes on this box. The rehearsal left six labelled sets under
`/data/backup-staging/sets/`, pushed and pruned like any set.

The `wait-online` step converged in one row and the box was rebooted after the
rehearsal: the unit's result and the post-boot provision pass are the tail of
`08-wait-online.txt`. The first real upgrade was `sudo ./upgrade.sh v0.1.1` on 2026-09-03 at 22:56 CDT
(`08-upgrade-v0.1.1.txt`), from the checkout on `main`, which already declared
0.1.1 because development had moved it past the applied `0.0.27`: the `version`
row read it as a re-run of 0.1.1 and the fresh pre-upgrade set `pre-v0.1.1` was
taken from that tree, so the way back is `restore --from staging --set
pre-v0.1.1` rather than `--rollback` (which would find nothing to do). The
0.0.27 tree could not have run it: its firewall check sees the DOCKER-USER block
`v0.1.0` rewrote as drift and its preflight would have refused. Every row ok;
`fetch` reached GitHub as `fpdadmin`; the new tree's provision moved nothing
(20/20, the `kvm`, `firewall`, and `gh-runner` state already converged from the
candidate tree); apply recreated the services whose files changed; verify at
0.1.1 by `--version`, the applied record, and every service healthy. The box
runs `v0.1.1`; the checkout is back on `main`, whose product code equals the tag.

## 7. Moving a populated image store

When `host provision` reports `docker-engine: unfixable`, or `apply` refuses while naming a populated package-default containerd root beside an empty root on the Docker volume, move the store by hand in an announced maintenance window. Starting the daemon on that empty root would make its existing images and container snapshots disappear. Every Compose project using the Docker daemon is down during the copy, including any other project sharing it. Tell users before starting.

### Survey

Keep the GIDEON and any other Compose projects running for the survey. Check free space on the root filesystem and the Docker volume, measure the current store, and record the image and container lists for comparison after the move. The move record directory is root-owned and is kept until acceptance:

```sh
sudo install -d -m 0700 /var/lib/gideon-store-move
sudo df -h / /var/lib/docker
sudo du -sb /var/lib/containerd
sudo bash -o pipefail -c 'docker image ls --digests --format "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}" | sort > /var/lib/gideon-store-move/images.before'
sudo bash -o pipefail -c 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}" | sort > /var/lib/gideon-store-move/containers.before'
sudo docker system df
sudo cp -p /etc/containerd/config.toml /var/lib/gideon-store-move/config.toml.shipped
```

Check for pending package actions affecting Docker or containerd with `sudo apt-get update -q` and `sudo apt-get -s install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`. If the simulation lists an action, apply it while all projects remain up with `sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`, then repeat the survey and replace the saved lists and configuration. Do not stop the daemons until the simulation reports no pending action.

### Stop

Stop the release registry, Docker's socket and service, then containerd. `gideon-registry.service` is provisioned on the build box alone: on any other box, leave out every command below that names it, here and in the stages after.

```sh
sudo systemctl stop gideon-registry.service
sudo systemctl stop docker.socket docker.service
sudo systemctl stop containerd.service
sudo systemctl is-active gideon-registry.service
sudo systemctl is-active docker.socket
sudo systemctl is-active docker.service
sudo systemctl is-active containerd.service
pgrep -a containerd-shim
sudo findmnt -R /var/lib/containerd
sudo ls -A /var/lib/containerd/tmpmounts
```

Each `systemctl is-active` command should print `inactive`; its non-zero status is expected for an inactive unit. `pgrep` should print no processes and return status 1; any other failure means its result is unknown. `findmnt` should show no mounts under the old root, and `ls` should print nothing for the empty `tmpmounts` directory. If a process, mount, or temporary entry remains, leave the store in place and resolve it before continuing.

### Copy

Copy the old root to the Docker volume. The command preserves hard links, ACLs, extended attributes, sparse files, and numeric owners. If interrupted, run the same command again to resume the copy:

```sh
sudo mkdir -p /var/lib/docker/containerd
sudo rsync -aHAXxS --numeric-ids /var/lib/containerd/ /var/lib/docker/containerd/
```

### Verify

With all daemons still stopped, run a second dry pass. It must exit successfully and print no itemized differences:

```sh
sudo rsync -aHAXxS --numeric-ids -n -i /var/lib/containerd/ /var/lib/docker/containerd/
```

Compare the roots' inode counts and apparent byte counts. Also compare the counts of character-device whiteouts under `io.containerd.snapshotter.v1.overlayfs/snapshots` and directories carrying the `trusted.overlay.opaque` extended attribute:

```sh
sudo bash -o pipefail -c 'find /var/lib/containerd | wc -l'
sudo bash -o pipefail -c 'find /var/lib/docker/containerd | wc -l'
sudo du -sb /var/lib/containerd /var/lib/docker/containerd
sudo bash -o pipefail -c 'find /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots -type c | wc -l'
sudo bash -o pipefail -c 'find /var/lib/docker/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots -type c | wc -l'
sudo python3 -c '
import os, sys
for root in sys.argv[1:]:
    count = 0
    for base, dirs, _ in os.walk(root):
        for name in dirs:
            try:
                os.getxattr(os.path.join(base, name), "trusted.overlay.opaque")
                count += 1
            except OSError:
                pass
    print(root, count)
' /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots /var/lib/docker/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots
```

All four counts must match. Whiteouts are device nodes and opaque markers are extended attributes; a plain file copy would lose overlay state. Do not provision if the dry pass reports a difference or any count differs.

### Provision

A package action during provision must not start a daemon on the old root, so hold every package maintainer script back with a temporary `policy-rc.d` for the one step. If `/usr/sbin/policy-rc.d` already exists, it is the host's own policy: stop and resolve it rather than overwrite it. From `/opt/gideon`, write the new containerd root and restart the managed daemons:

```sh
cd /opt/gideon
printf '#!/bin/sh\nexit 101\n' | sudo tee /usr/sbin/policy-rc.d >/dev/null
sudo chmod 755 /usr/sbin/policy-rc.d
sudo python3 -m gideon host provision --only docker-engine
sudo rm /usr/sbin/policy-rc.d
sudo systemctl start gideon-registry.service
sudo containerd config dump
```

Remove `/usr/sbin/policy-rc.d` whether or not provision succeeds.

Confirm Docker's socket and service, containerd, and on the build box the registry are active. Confirm `containerd config dump` reports `/var/lib/docker/containerd` as the root. If provision or that check fails, keep both roots and use the rollback below.

### Check and accept

Wait for the containers that were running in the survey to return and for their health checks to pass. Save fresh lists and compare them with the survey; both diffs must exit zero:

```sh
sudo bash -o pipefail -c 'docker image ls --digests --format "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}" | sort > /var/lib/gideon-store-move/images.after'
sudo bash -o pipefail -c 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}" | sort > /var/lib/gideon-store-move/containers.after'
sudo diff -u /var/lib/gideon-store-move/images.before /var/lib/gideon-store-move/images.after
sudo diff -u /var/lib/gideon-store-move/containers.before /var/lib/gideon-store-move/containers.after
sudo docker ps --filter health=unhealthy
sudo du -sb /var/lib/docker/containerd
sudo df -h / /var/lib/docker
```

Do not accept the move until both list comparisons are identical, no unhealthy container is reported, the engine is healthy, and every surveyed project has returned to its surveyed state. Record the responsible operator's confirmation with the move record:

```sh
printf '%s\n' '<operator confirmation>' | sudo tee /var/lib/gideon-store-move/accept.txt >/dev/null
```

### Remove

Only after acceptance, confirm once more that `containerd config dump` names `/var/lib/docker/containerd` and that the surveyed projects are healthy. Then remove the old root and the temporary move record:

```sh
sudo rm -rf /var/lib/containerd /var/lib/gideon-store-move
sudo python3 -m gideon host provision
```

The closing provision, from `/opt/gideon`, should report every step converged.

### Roll back before acceptance

Rollback is available after the stop and before acceptance. Stop the registry, Docker socket and service, and containerd, then restore the saved configuration and start the old root:

```sh
sudo systemctl stop gideon-registry.service
sudo systemctl stop docker.socket docker.service
sudo systemctl stop containerd.service
sudo cp -p /var/lib/gideon-store-move/config.toml.shipped /etc/containerd/config.toml
sudo systemctl start containerd.service
sudo containerd config dump
```

Continue only when `containerd config dump` reports `/var/lib/containerd` as the root; otherwise stop and remove nothing. Then start the rest:

```sh
sudo systemctl start docker.socket docker.service
sudo systemctl start gideon-registry.service
```

Wait for the surveyed projects to return. Save fresh image and container lists, compare them with the saved lists in `/var/lib/gideon-store-move`, and confirm the old root is active before removing the copied `/var/lib/docker/containerd` root. Keep both roots until those checks pass.

## 8. Moving an occupied `/data` onto the data volume

Use this procedure when `host provision` reports `disk-layout: unfixable` and names entries held by an unmounted `/data`. The directory is on the root filesystem: another application's data may have been written there before GIDEON provisioned its volume, and mounting the volume would hide that data. Announce the move to the owning operator and use a maintenance window. The owning application stays down from **Stop** until **Start and accept**.

Keep a root-owned record in `/var/lib/gideon-data-move` until acceptance. Each completed stage writes a stamp there; `copy.started` is written *before* copying. On an interrupted run, inspect the stamps and continue at the unfinished stage. Never repeat the volume inventory after `copy.started` exists: it would count the move's own copies as preexisting data. Stop if a command fails or a required tool is unavailable; repair that condition before continuing.

### Survey

Confirm `/data` is a directory and not a mount point. Start a new record only when `/var/lib/gideon-data-move` is absent. If a survey was interrupted before its stamp, inspect the record and repeat the survey only while the original `/data` is still in place; skip the new-record absence check on that repeat. Record the sorted top-level names as NUL-delimited data, and record each entry's owner, group, mode, and apparent size. Check root filesystem free space and the data's apparent size. Check that `lsof` and `rsync` are available before the stop:

```sh
sudo test -d /data
sudo findmnt --mountpoint /data             # expect no mount and status 1
sudo test ! -e /var/lib/gideon-data-move
sudo test ! -L /var/lib/gideon-data-move
sudo install -d -m 0700 /var/lib/gideon-data-move
sudo bash -o pipefail -c 'find /data -mindepth 1 -maxdepth 1 -printf "%f\0" | sort -z > /var/lib/gideon-data-move/entries.nul'
sudo bash -c 'find /data -mindepth 1 -maxdepth 1 -printf "%f\t%u:%g\t%m\t%s\0" > /var/lib/gideon-data-move/entries.details.nul'
set -o pipefail
sudo cat /var/lib/gideon-data-move/entries.details.nul | tr '\0' '\n'
sudo df -h /
sudo du -sh /data
command -v lsof
command -v rsync
```

The NUL-delimited record keeps names containing spaces or newlines intact. The following check must succeed and print nothing. GIDEON owns these top-level names and will re-own them; a co-tenant keeps its data outside them. If one is present, stop and have its operator rename it on their side, then repeat the survey before stamping it:

```sh
sudo test -s /var/lib/gideon-data-move/entries.nul
sudo bash -euo pipefail -c '
while IFS= read -r -d "" name; do
    case "$name" in
        fast|bulk|work|models|registry|drill|ci|backup-staging|acceptance|observability)
            printf "managed /data name: %s\n" "$name" >&2
            exit 1 ;;
    esac
done < /var/lib/gideon-data-move/entries.nul'
```

Use Docker's listing to record running containers with a bind mount sourced from `/data` or a path below it. With `pipefail` enabled, a failed Docker listing or inspection stops the survey. The record may be empty:

```sh
set -o pipefail
sudo docker ps -q --no-trunc |
    xargs -r sudo docker inspect --format '{{range .Mounts}}{{printf "%s\t%s\n" $.Name .Source}}{{end}}' |
    awk -F '\t' '$2 == "/data" || index($2, "/data/") == 1' |
    sudo tee /var/lib/gideon-data-move/containers.before
sudo touch /var/lib/gideon-data-move/survey.done
```

### Stop

The owning operator stops their application by its own commands. Confirm no process holds a path under `/data` open, no running container mounts it, and no nested mount remains. `lsof` should print nothing and return status 1; output, a diagnostic, or any other status stops the move. The Docker and mount listings must succeed and be empty. Do not set the stamp until all checks have passed:

```sh
sudo lsof +D /data                         # expect no output and status 1
set -o pipefail
sudo findmnt -rn -o TARGET |
    awk '$0 == "/data" || index($0, "/data/") == 1' |
    sudo tee /var/lib/gideon-data-move/mounts.after-stop
sudo test ! -s /var/lib/gideon-data-move/mounts.after-stop
sudo docker ps -q --no-trunc |
    xargs -r sudo docker inspect --format '{{range .Mounts}}{{printf "%s\t%s\n" $.Name .Source}}{{end}}' |
    awk -F '\t' '$2 == "/data" || index($2, "/data/") == 1' |
    sudo tee /var/lib/gideon-data-move/containers.after-stop
sudo test ! -s /var/lib/gideon-data-move/containers.after-stop
sudo touch /var/lib/gideon-data-move/stop.done
```

### Set aside

Rename `/data` on the same filesystem; this is an immediate rename, not a copy. The set-aside path must be absent. Leave `/data` absent for provision to create; do not pre-create it:

```sh
sudo test ! -e /data.before-gideon-move
sudo test ! -L /data.before-gideon-move
sudo mv -T /data /data.before-gideon-move
sudo test ! -e /data
sudo touch /var/lib/gideon-data-move/aside.done
```

### Provision

From `/opt/gideon`, converge the volume and mount it. On a box that has not finished its first provision, `--only` refuses because its prerequisite has not converged; run the whole provision there instead. Proceed only when `findmnt` shows `/data` as an XFS mount point on the data LV and its size is as expected:

```sh
cd /opt/gideon
sudo python3 -m gideon host provision --only disk-layout
# On a first-provision box, use instead: sudo python3 -m gideon host provision
sudo findmnt -rn -o TARGET,SOURCE,FSTYPE,SIZE --mountpoint /data
sudo lvs -o lv_name,lv_size vg_data
sudo touch /var/lib/gideon-data-move/provision.done
```

### Inventory the volume

Before the first copy, record the mounted volume's own sorted top-level names. A volume previously built and left unmounted may already contain the owning application's data. Any name shared with the original survey stops the move: do not merge the trees. The owning operator reconciles the two copies by hand, keeping, renaming, or removing one, before inventory is repeated and the copy starts. Once the inventory is stamped, keep it; after `copy.started`, never re-inventory on a resume:

```sh
sudo bash -o pipefail -c 'find /data -mindepth 1 -maxdepth 1 -printf "%f\0" | sort -z > /var/lib/gideon-data-move/volume.before.nul'
set -o pipefail
sudo cat /var/lib/gideon-data-move/volume.before.nul | tr '\0' '\n'
sudo comm -z -12 /var/lib/gideon-data-move/entries.nul /var/lib/gideon-data-move/volume.before.nul |
    sudo tee /var/lib/gideon-data-move/shared-names.nul |
    tr '\0' '\n'
sudo test ! -s /var/lib/gideon-data-move/shared-names.nul
sudo touch /var/lib/gideon-data-move/inventory.done
```

### Copy

Stamp the start **before** the transfer. One `rsync` invocation copies the whole recorded entry list under the same names, preserving hard links even between different top-level entries, ACLs, extended attributes, sparse files, and numeric owners. `--files-from` needs explicit recursion. If interrupted, rerun this same `rsync` command; keep the original survey and volume inventory:

```sh
sudo test -f /var/lib/gideon-data-move/inventory.done
sudo touch /var/lib/gideon-data-move/copy.started
sudo rsync -aHAXxSr --numeric-ids --from0 --files-from=/var/lib/gideon-data-move/entries.nul /data.before-gideon-move/ /data/
sudo touch /var/lib/gideon-data-move/copy.done
```

### Verify

With the application still stopped, make a second dry pass over **only the surveyed entries**. `rsync` must exit successfully and the itemized difference file must be empty. Then compare each entry's regular-file count and the sum of those files' sizes on both sides. Do not compare whole roots: provision's directories sit beside the copies, and ext4 and XFS can report different directory sizes:

```sh
sudo test -f /var/lib/gideon-data-move/copy.done
sudo bash -c 'rsync -aHAXxSr --numeric-ids --from0 --files-from=/var/lib/gideon-data-move/entries.nul -n -i /data.before-gideon-move/ /data/ > /var/lib/gideon-data-move/verify.differences'
sudo test ! -s /var/lib/gideon-data-move/verify.differences
sudo bash -euo pipefail -c '
while IFS= read -r -d "" name; do
    before=$(find "/data.before-gideon-move/$name" -xdev -type f -printf "%s\n" | awk "{count++; bytes+=\$1} END {printf \"%d %.0f\", count, bytes}")
    after=$(find "/data/$name" -xdev -type f -printf "%s\n" | awk "{count++; bytes+=\$1} END {printf \"%d %.0f\", count, bytes}")
    printf "%q: before %s; after %s\n" "$name" "$before" "$after"
    test "$before" = "$after"
done < /var/lib/gideon-data-move/entries.nul'
sudo touch /var/lib/gideon-data-move/verify.done
```

### Start and accept

The owning operator starts their application and confirms it works on the moved data. Record that operator's confirmation and time in the move record. From this start onward, the application writes to the volume alone; the rollback below is no longer safe:

```sh
printf '%s\n' '<operator confirmation and time>' | sudo tee /var/lib/gideon-data-move/accept.txt >/dev/null
sudo touch /var/lib/gideon-data-move/start.accepted
```

### Remove

Only after acceptance, confirm the data volume is still mounted, then remove the set-aside tree and the move record. A closing provision from `/opt/gideon` should report every step converged:

```sh
sudo test -f /var/lib/gideon-data-move/start.accepted
sudo findmnt -rn -o TARGET,SOURCE,FSTYPE --mountpoint /data
sudo rm -rf /data.before-gideon-move /var/lib/gideon-data-move
cd /opt/gideon
sudo python3 -m gideon host provision
```

### Roll back before the start

Rollback is available from the stop until the owning application starts on the moved data. Before **Set aside**, `/data` is still the original directory: the owning operator can restart after confirming it is untouched. Once the application starts on the moved data, new writes exist only on the volume; moving back would require its operator to stop it and perform and verify a reverse `rsync`, outside this rollback.

After **Set aside**, keep the application down. If `/data` is mounted, unmount it; if provision has not yet mounted it, confirm it is not a mount point and skip only `umount`. In `/etc/fstab`, manually remove the complete managed block if present: `# GIDEON BEGIN provision:disk-layout`, the UUID line between, and `# GIDEON END provision:disk-layout`. Provision will write the block again on the next successful move. Leaving it in place would mount the volume over the restored directory at reboot:

```sh
sudo umount /data                            # only when mounted
sudo findmnt --mountpoint /data             # expect no mount and status 1
sudoedit /etc/fstab
sudo sed -n '/# GIDEON BEGIN provision:disk-layout/,/# GIDEON END provision:disk-layout/p' /etc/fstab
sudo rmdir /data                            # only when the empty mount-point directory exists
sudo mv -T /data.before-gideon-move /data
sudo touch /var/lib/gideon-data-move/rollback.done
```

The `sed` command must print nothing before restoring `/data`. The owning operator then starts their application on the restored directory. Keep the rolled-back record for review, but archive it under a new name before a fresh attempt so its `copy.started` stamp cannot be mistaken for the new move's state. Provision will report the occupied-`/data` refusal again until the move is completed. Any copies already on the volume remain there and will appear as shared names at the next attempt's inventory; reconcile them before copying.

## 9. Upgrading a shared prerequisite

Use this procedure when provision or preflight reports a present Docker Engine, Compose plugin, NVIDIA container toolkit, or driver below the lock's floor or with closed kernel modules, or when an apt rehearsal names packages it would upgrade or remove. Read `host.lock` for the required floor and driver branch. Provision installs absent packages but does not upgrade a present prerequisite; a driver at or above the branch with open kernel modules is accepted whatever its packaging.

A `reboot-required` row naming a loaded NVIDIA driver that differs from the installed module means the driver was upgraded without its reboot: reboot in an announced window, then re-run provision.

### Survey

Refresh apt's lists and rehearse the appropriate install before changing packages. Read every `Inst` and `Remv` line, including dependencies and other packages on the box. If apt cannot run, fix its refusal before proceeding.

```sh
sudo apt-get update -q
sudo apt-get -s install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo apt-get -s install -y nvidia-container-toolkit
sudo apt-get -s install -y nvidia-driver-pinning-<branch> nvidia-open
```

Run only the rehearsal for the prerequisite being upgraded; replace `<branch>` with `host.driver.branch` from `host.lock` for a driver upgrade. Resolve any other proposed change with its owner before the window.

### Announce

Announce a maintenance window at least one working day ahead to users and every project sharing the daemon. A Docker upgrade restarts every container on the box. A driver upgrade needs a reboot. Agree on the timing with the other projects' operators before starting. When `host provision`'s co-tenant guard names running containers, use that list to announce the window to their projects; `--acknowledge-disruption` acknowledges the disruption on the re-run but does not announce the window.

### Upgrade

During the window, choose the applicable sequence below. Stop if any command fails; do not continue to provision until the package change succeeds. Upgrade Docker's five packages together:

```sh
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Upgrade the toolkit as one package:

```sh
sudo apt-get install -y nvidia-container-toolkit
```

For a driver, release the hold, install the branch pinning package before the driver, restore the hold, and reboot:

```sh
sudo apt-mark unhold nvidia-open
sudo apt-get install -y nvidia-driver-pinning-<branch>
sudo apt-get install -y nvidia-open
sudo apt-mark hold nvidia-open
sudo reboot
```

After a driver reboot, regenerate the CDI specification as the toolkit step does:

```sh
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

### Converge

From the release checkout, re-run provision:

```sh
cd /opt/gideon
sudo python3 -m gideon host provision
```

After a driver or Docker change, verify the engine once services are running again:

```sh
sudo python3 -m gideon engine verify
```

A merged `host.minimums.*` or `host.driver.branch` bump makes provision and preflight refuse until this procedure is followed (`docs/runbooks/release-files.md §9`). Provision never upgrades a present prerequisite itself.

## 10. Changing a moved box-wide setting

When provision, preflight, or `upgrade`'s provision stage reports a moved setting that falls short, its row names the setting, the value found, and the value needed. GIDEON does not change a moved setting. Agree the value with the box's other operators, make the applicable change below, then re-run provision from the release checkout.

### Time zone

If the host's zone should match `office.timezone` in `/etc/gideon/site.yaml`, change it with `sudo timedatectl set-timezone <zone>`. If the host's zone is right, correct `office.timezone` instead. Do this in an announced window: every timer on the box follows the zone, and a persistent timer can catch up once after the change.

### SSH login rule

Keep a working SSH session open. Read the effective `passwordauthentication` and `kbdinteractiveauthentication` values with `sudo sshd -T`; use `sudo grep -riE '^[[:space:]]*(PasswordAuthentication|KbdInteractiveAuthentication)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/` to find the file setting either value ahead of `/etc/ssh/sshd_config.d/00-gideon-key-only.conf`. Agree the change with the other operator, since a password login may be theirs. Remove or correct the conflicting line, run `sudo systemctl reload ssh`, and confirm both effective values are `no` while keeping the session open.

### Firewall default policy

Agree an announced window because this changes who can reach the box. Set the incoming default with `sudo ufw default deny incoming`, then read `sudo ufw status verbose` to confirm the active incoming policy is `deny`.

### Journald storage

Set `Storage=persistent` in the file the refusal names, or, when `Storage=auto` or unset, create `/var/log/journal` with `sudo mkdir -p /var/log/journal`. Run `sudo systemctl restart systemd-journald` and read `sudo systemd-analyze cat-config systemd/journald.conf` to confirm the effective value. Containers keep running; the journal pauses briefly, so this change needs no maintenance window.

### Apt periodic triggers

Use `sudo grep -r 'Unattended-Upgrade\|Update-Package-Lists' /etc/apt/apt.conf.d/` to find the file setting the refused key to `"0"`; set that key to `"1"` there. Read `apt-config dump APT::Periodic` to confirm both triggers are on. No maintenance window is needed.

### Containerd root

Work in an announced window because restarting containerd and Docker reaches every container. Do not edit `root` first.

Find the root file. The row's root is the one containerd runs with: `sudo containerd --config /etc/containerd/config.toml config dump | grep -E '^(root|imports)'` shows it and the files `config.toml` imports. The root file is `/etc/containerd/config.toml` when the row names it; when the row names a file `config.toml` imports, `sudo grep -l '^root' /etc/containerd/conf.d/*.toml` finds it. Every step below that names the root file means that one file.

Measure the store under the named root with `sudo du -sb <named-root>` and check its snapshotter `snapshots` directory.

If the store is populated, follow `docs/runbooks/install-upgrade.md §7` by hand, substituting the named root for `/var/lib/containerd` throughout:

- At the survey, also save the root file beside the shipped copy, `sudo cp -p <root-file> /var/lib/gideon-store-move/root-file.saved`, before any change; when the root file is `config.toml`, the survey's own copy is that save.
- At the provision stage, set `root = '/var/lib/docker/containerd'` in the root file by hand before `sudo python3 -m gideon host provision --only docker-engine` under the same `policy-rc.d` hold. Provision does not rewrite a moved file.
- A rollback restores the root file from `/var/lib/gideon-store-move/root-file.saved` as well as `config.toml`, and continues only when `containerd config dump` reports the named root, not `/var/lib/containerd`.
- Finish the check, accept, and remove stages over the named root.

If the store is empty, `sudo cp -p <root-file> <backup-path>`, set `root = '/var/lib/docker/containerd'` in the root file, then run `sudo systemctl restart containerd` and `sudo systemctl restart docker`. To undo it, copy the backup back and run the same two restarts.

### /data mount

The `disk-layout` row names the `/data` mount's filesystem type and size, and the selected profile's data-volume floor. GIDEON does not change an existing mount. A first run with a `/data` mount GIDEON did not build is `blocked` until `/etc/gideon/site.yaml` names the hardware profile whose floor judges it.

Agree a change with the mount's owner. That operator grows the volume or moves its data to a volume meeting the floor; alternatively, the office sets `hardware_profile` in `/etc/gideon/site.yaml` to a profile this host satisfies. A filesystem of another type is reformatted only by its owner, after moving its data off, in an announced window. Then run provision from the release checkout.

From the release checkout, converge after the hand change:

```sh
cd /opt/gideon
sudo python3 -m gideon host provision
```

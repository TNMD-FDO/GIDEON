# Install, upgrade, and rollback (CSA runbook material)

Ticket 08 (`v0.0.25`). The two shell entrypoints a receiving office runs,
`./install.sh` and `./upgrade.sh`, are three-line wrappers over `python3 -m
gideon install` and `python3 -m gideon upgrade` (spec §2.2). Install is the
§3.6 step 5 sequence as one ordered command over commands that already exist;
upgrade is §3.6 step 7 with the mandatory pre-upgrade set (ADR-0005) and a
rollback that restores it. Every run is a row in `audit_log` (`install`,
`upgrade`, `rollback`), never a log line (ADR-0027). The acceptance section
(the clean-VM harness, the two-CSA exercise) is §6 below and arrives with
`v0.1.0`.

## 1. The receiving-office sequence (§1.9 steps 3–7)

Every step is re-runnable; a refusal prints its fix and exits non-zero, and the
same command is run again after the fix.

1. **Clone the release** to `/opt/gideon` as the account that will own the
   checkout (a CSA account with GitHub credentials, never root). Because
   `/opt` is root-owned, create and assign the directory first — the acceptance
   harness's shape in `tools/acceptance/vm.py`:
   `sudo install -d -o <account> -g <account> /opt/gideon`
   Then clone the release:
   `git clone --branch <tag> <repository> /opt/gideon`. The owner matters
   later: `upgrade` runs git as whoever owns the directory, so that account's
   credentials fetch the next release and no root-owned file lands in the tree.
   The later commands run from `/opt/gideon`: `cd /opt/gideon`.
2. **Provision the host**: `sudo python3 -m gideon host provision`. One line
   per step; the first run prints the backup public key and the age identity
   **once** (store the identity in the office password manager before going
   on, `backup-restore.md` §2). A `reboot-required` row (the NVIDIA
   driver, a kernel) means: reboot, then run provision again until every step
   reads `ok`. The `wait-online` step (new in `v0.0.25`) makes the boot-time
   network wait accept any one link online, so a box with an unplugged second
   port no longer boots with a failed unit.
3. **Write the site file** at `/etc/gideon/site.yaml` from
   `config/site.example.yaml`, and place the supplied secrets
   (`office-services-setup.md`). On the build box, `python3 -m gideon registry
   mirror` (Docker access) pulls the release's images into the loopback registry;
   an office's image source is standing ticket 24's.
4. **Provision again**: `sudo python3 -m gideon host provision`. The
   site-dependent steps — `egress-proxy`, `firewall`, `time-sync`, and
   `timezone` — are blocked until the site file exists, and preflight refuses
   on any unconverged step. The specification's §1.9 step 3 and §3.6 step 2
   omit this second run; it is required by the actual dependency order.
5. **Preflight**: `sudo ./preflight.sh`. Every provisioning step re-checked,
   then the install-time checks (the directory, the relay, the backup target,
   TLS material, ports, disk). Exit 0 iff nothing refuses. Install opens with
   the same preflight, so a refusal here is a refusal there.
6. **Install**: `sudo ./install.sh`. Eight phases, each nested command printing
   its own rows first and install one phase row after it:

   | Phase | Runs | What it proves |
   |---|---|---|
   | `preflight` | the preflight above | the office services are reachable and correct before anything is built |
   | `apply` | `apply` (thirteen stages) | the stack is up and verified; the first run prints each break-glass password **once** and fetches the profile's models into `/data/models` (about 31 GB, 10–30 minutes of office bandwidth; later runs verify them in about a minute) |
   | `reconcile` | `users reconcile --now` | directory membership is the frontend's role truth; audit rows written |
   | `engine-verify` | `engine verify` | the engine answers at length and in structured form, and the guardrail's refusal arrives inside a real chat turn (three turns as the eval identity, two to eight minutes); a failure stops install here with `Do not go live…` and the failing check's name; a no-GPU host prints one skipped row (`v0.1.22`) |
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
   **The weights tree** (`v0.1.5`, spec §5.4): apply's `models` stage, between
   `pull` and `recreate`, makes `/data/models` hold the profile's models in the
   Hugging Face hub-cache layout, every file verified against `models.lock`;
   `sudo python3 -m gideon models pull` runs the same converge by hand, and a
   re-run verifies and fetches nothing. The tree is derived state outside the
   backup set (§19.1) — after a rebuild, apply fetches it again — and the pull
   keeps the pinned set and the newest previous complete set, so
   `upgrade --rollback` re-applies the previous release without a download;
   older sets are removed and each removal printed. `/data/models/gideon/pulls.yaml`
   is the pull's record; a pull that stops mid-file resumes on the next run,
   and a second pull started while one runs refuses naming it.
   **The engine** (`v0.1.6`, spec §5.2–5.3): on a GPU host apply renders
   `gideon-generator` from the profile's `serve` block — the vLLM image at its
   `images.lock` digest, GPU 0 reserved by UUID through CDI, `/data/models`
   mounted read-only, no published port — and starts it in the `start`
   stage. The model takes minutes to load, so `verify` waits for the engine
   alone for up to fifteen minutes from the start stage (the same clock as
   the container's healthcheck), reports `engine healthy N s after start`,
   and keeps every other service on its one-minute bound. The engine's API
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
   (`v0.1.40`, spec §1.7). The command refuses first when `apply` has pending
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
   `gideon-api` (its mount) and the frontend (its env file's owner). Once General is live, a rotation that recreates the
   engine is made in an announced maintenance window (§21; the command does
   not gate on the clock). The same command rotates `webui_secret_key` (the
   frontend recreated once; every signed-in session ends — §4.4's live-session
   revocation), `searxng_secret_key` (SearXNG recreated through the recreate
   rule; with search off nothing consumes it and the command says so and
   writes nothing), and the two minted keys `gideon_admin_api_key` and
   `gideon_eval_api_key` (the file removed and re-minted by the apply-manifest
   stage; nothing recreated). Every other name refuses before any change,
   naming the office's path: a supplied secret — `tls_key`: replace the files
   and run `tls reload`; `ldap_bind_password` and `smtp_password`: replace the
   file, run `apply`, then force-recreate Grafana by hand (`sudo docker compose
   -f /etc/gideon/rendered/compose.yaml up -d --no-deps --force-recreate
   grafana`), which mounts them and which the recreate rule does not see;
   `proxy_auth`: replace the file and run `apply` — a Postgres role password
   (the role holds the value; a ticket of its own), `gideon_admin_password`
   and `gideon_eval_password` (the frontend's account holds it),
   `grafana_admin_password` (Grafana's admin user is seeded from the file at
   first start only: change it through Grafana's password endpoint as the
   administrator with the old and new value in the request body carried on
   stdin, then rewrite the file in place — `.scratch/slice-1/assets/15-on-box.txt`
   §18 is the record). A restore of a set made before a rotation brings the
   older value back: after that restore's `apply`, run `secrets rotate <name>`
   for every secret rotated since the set was made.
7. **Corpus** (`gideon corpus install`, `index promote`) arrives with slice 3.
8. **Hand over the URL**, and `sudo python3 -m gideon alerts test` once so the
   page path is proven through the relay (`observability.md`).

**No-GPU host.** `sudo python3 -m gideon host provision --no-gpu` writes
`/etc/gideon/no-gpu` once. It is refused on a host with an NVIDIA device; every
later command reads the marker. The driver and toolkit steps are skipped. The DCGM
exporter, its scrape job, the GPU board, and the driver-drift rule are not
rendered. `--only` on a skipped step refuses and names the marker. To leave the
mode, remove the marker and re-run provision; the next apply renders the removed
files as new. The marker is never carried in a backup set.

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
`v0.1.71`'s release, before any upgrade; the transcripts of its crossing — the
marker removed to leave the mode, then the three commands — are kept at
`.scratch/slice-0/assets/21-on-box.txt`. To leave
the mode, remove the marker, run provision again, then apply; the three units
stay until removed by hand, and until they are, `restore` refuses on a host
whose registry is still active.

## 6. The acceptance run

The acceptance harness builds a throwaway VM from the pinned image, converts it
to the §1.4 LVM layout, and proves the receiving-office sequence in a clean
environment. The VM has its own office services: the real directory is reached
over NAT, the harness runs a STARTTLS + AUTH SMTP sink on the libvirt bridge,
the VM is its own backup target, and a throwaway CA is bundled with the office
root. The sequence is exactly §1 above. Every product command runs as
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

Acceptance runs happen at minor tags only: the TRIP-3 `acceptance-run` block
runs against the local tag before the push, then `.github/workflows/acceptance.yml`
runs the pushed tag and stores the transcripts as an artifact. Patch tags never
run the harness. A tag can name any commit its pusher can reach, so the
workflow's trust is the two CSAs who may create a `v*` tag where it runs, not
anything in the tag's tree: a `v*` ruleset guards the tags an office clones
(tickets 22 and 57). This cadence is the CSA ruling superseding §2.5's every-tag
wording; the harness pass is also the ruling's proof for §22.1, superseding its
two-CSA by-hand exercise. The corresponding spec-gap comments are recorded on
ticket 08.

Transcripts redact print-once values and age identities before they reach the
host. `tests/test_evidence_hygiene.py` scans `.scratch/` as a tripwire, so the
evidence cannot accidentally carry a complete age secret or an unredacted
print-once line. On TNMD's box a complete run — image conversion, boot, two
provisions, preflight, install with the drill, `alerts test`, the four
rehearsal legs, the push and target restore, verify — takes under six minutes
(the plan estimated 45); the converted image boots in about nine seconds. The
candidate tree's passing run is `08-acceptance-run-2026-09-03/`.

**What the runs of this release found** (each fixed in the same release,
recorded on ticket 08): the firewall step's `DOCKER-USER` drops judged every
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

**The `v0.1.0` run.** `sudo python3 -m tools.acceptance v0.1.0` against the local tag on TNMD's box, 2026-09-03T21:28:56-05:00 to 2026-09-03T21:34:41-05:00, before the push: every stage ok, exit 0 — the VM installed to its URL, `alerts test` delivered through the sink, the four rehearsal legs on `v0.1.1-rc.1`, the push and the target restore, verify at release 0.1.0 by the applied record and the checkout's `--version`, sixteen authenticated TLS messages, thirteen transcripts, the VM torn down. The record is `08-acceptance-v0.1.0/`; the pushed tag's own run in `acceptance.yml` is the standing one.

**The CI record.** The pushed tag's own run (33830159940) refused at `boot`: the workflow named `--out acceptance-out` relative to the runner's workspace, and libvirt opens the console log by path from its own working directory — every local run had used an absolute `--out`. The same root run left root-owned `__pycache__` directories under the workspace's `tools/`, and the next `ci` run's `mirror-images` job failed at its checkout on them (`git clean` cannot unlink them as the runner's account). Hotfix `v0.1.1`: an absolute `--out` whatever is typed, no bytecode written after the harness's entry point, the checkout's caches handed back with the transcripts, and a `workflow_dispatch` on `acceptance.yml` with a full-history checkout; the three directories were removed from the workspace by hand once. The standing record is the dispatched run against `v0.1.0` from the fixed `main`, 33831089332, 2026-09-04T02:51:53Z to 2026-09-04T02:57:48Z, every stage ok, the transcripts its `acceptance-v0.1.0` artifact, and the workspace held nothing root-owned after it. A red tag run whose cause is the harness or the workflow, not the tag's tree, is re-made this way: `gh workflow run acceptance.yml -f ref=v<x.y.0>`.

**The `v0.2.0` run.** Both forms against the local tag on TNMD's box, from the release's worktree, before the push: `sudo python3 -m tools.acceptance v0.2.0`, 2026-09-18T09:15:55-05:00 to 09:36:47, 1252 s, every stage ok, exit 0 — the no-GPU install, `alerts test` through the sink, the four rehearsal legs on `v0.2.1-rc.1`, the push and the target restore, verify at release 0.2.0, sixteen authenticated TLS messages and thirteen transcripts; then `--full-restore` of the box's newest set (release 0.1.81), to 09:56:57, 1196 s, every stage ok, exit 0, the run directory 155 GB at its peak. The box itself then upgraded 0.1.82 → 0.2.0 by `upgrade`, from the checkout still on `main` at 0.1.82, 09:57:36 to 10:03:26, with `engine-verify` ok in the sequence. The records are `.scratch/slice-1/assets/18-acceptance-v0.2.0/`, `18-acceptance-restore-v0.2.0/`, and `18-on-box.txt`; the pushed tag's own two-step run in `acceptance.yml` is the standing one.

## 2. Upgrading: `sudo ./upgrade.sh <tag>`

Run from the checkout, as root, with a clean work tree: no change to a tracked
file. An untracked file is no obstacle, so `sudo ./upgrade.sh <tag> 2>&1 | tee
.scratch/slice-0/assets/<transcript>.txt` is the way to keep the record. The
command runs from the *current* release's tree and hands over to the new tree's
own CLI after the checkout; nothing of the new release is loaded into the
running process.

**From go-live** (ADR-0044). A tag is user-facing from the office's first
users; before them the rules below do not bind. The **quiet window** is
weeknights 19:00–06:00 and Friday 19:00 to Monday 06:00 in the site's timezone
(TNMD: America/Chicago). Work is window-bound when it sends requests to the
engine on GPU 0; nothing scheduled in this release does. A **maintenance
window** is a span inside the quiet window, announced at least one working day
ahead, weekend nights by default — the only sanctioned unavailability. An
engine swap, a driver, engine, or Docker change, and every upgrade on a
user-facing tag take one. Read its cost beforehand from `render --diff`'s
recreate row; count on the whole stack only when `provision` or a rollback
needs it. Before the window, send the release's note from
`docs/release-notes/<tag>.md` to the users and inform the supervisor, both at
least one working day ahead. The release note's `## Breaking` section is what
the `version` stage below prints.

| Stage | What happens | Fix on a refusal |
|---|---|---|
| `preconditions` | root, a valid site file, Docker Compose answering, the checkout a git work tree with a clean status, its owner resolved from the directory | commit or stash as the owner; re-run |
| `fetch` | `git fetch --tags origin` as the owner; the tag must resolve to a commit. A failed fetch with the tag already present continues and says so | fetch by hand as the owner, then re-run |
| `version` | the tag's tree must declare the version its name says; a lower version refuses (naming `upgrade --rollback` and `restore`); the same version is a re-run; **a different major prints the release note's `## Breaking` section first and refuses without `--acknowledge-breaking`** | read the section, then re-run with the flag |
| `preflight` | the current tree's preflight | correct the office service it names; re-run |
| `backup` | the pre-upgrade set, `pre-<tag>`, a full set whose manifest must record the commit the checkout is leaving (rollback's way back). When the plain label exists from an attempt that never crossed the checkout, a suffixed `pre-<tag>-<timestamp>` set is taken (the earlier one may predate later writes). When the checkout already stands at the tag, an earlier attempt crossed it: the newest `pre-<tag>[-…]` set naming another release is **reused**, never re-taken | the backup's own rows carry the fix |
| `audit-intent` | one `upgrade` row: from-version, to-tag, both commits, the set label | the Postgres service; re-run |
| `checkout` | `git checkout --detach <tag>` as the owner (skipped when already there) | re-run |
| `provision` | the **new tree's** `host provision`, its rows streamed as they happen; exits 0 on `blocked` and `reboot-required` rows | — the next stage judges |
| `preflight` | the new tree's preflight: the gate for apply and the new release's own readiness checks. An unconverged or `reboot-required` step, or a new office-services requirement, refuses here — nothing of the product has changed yet | **reboot if asked, then re-run `upgrade <tag>`**: the checkout is already made and the set is reused, so the re-run resumes here |
| `apply` | the new tree's `apply` (thirteen stages, streamed; a pin bump in `models.lock` fetches the new revision here, before the engine restarts) | `upgrade --rollback` |
| `verify` | the new tree answers `--version` with the tag's version, the applied record names it, every service the rendered project declares has a container that is running and healthy on a fresh read | `upgrade --rollback` |
| `engine-verify` | the new tree's `engine verify` as a passthrough child; its rows carry the failing check | `upgrade --rollback` |
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
the checkout's commit, the release, and the archive boundary. Rollback is a
restore of that set plus the previous release's apply (ADR-0005). How far a
rollback got is a second, small record, `/data/backup-staging/rollback.json`,
which exists only while one is in progress (the `plan` row below).

| Stage | What happens |
|---|---|
| `select` | the newest complete `pre-v…` set (with or without a timestamp suffix), or the one for `<tag>` when given; an operator's other `pre-*` labels and the `pre-rollback-*` safety sets are never candidates. Its commit must exist in the checkout. When the running tree already is the set's release *and* the applied record names it, there is nothing to roll back to (use `restore --from staging --set <label>` when it is the data, not the release, you want back); when only the tree is — a rollback that stopped after its checkout — the re-run resumes from the previous tree |
| `plan` | whether a restore is needed, judged from `rendered/manifest.yaml` — the first thing an apply writes, before any stage touches the stores. A manifest still naming the set's release proves the new tree's apply never completed a render: the rollback only moves the checkout back and re-verifies. Anything else (another release, a missing or unreadable manifest) means a restore. The verdict is written to `/data/backup-staging/rollback.json` (beside `push.json`), the record of the rollback in progress: updated when the restore is done, removed once the `rollback` row is written, and what a re-run resumes from |
| `safety` | when a restore is needed and the whole stack is running, a full `pre-rollback-<timestamp>` set taken by the *current* tree, so its files, checkout copy, and database describe the release that is running. A partial or stopped stack gets none, and the row says that anything written since the upgrade began is not preserved |
| `audit-intent` | a `rollback` row when Postgres answers; when it does not (the reason for many rollbacks) the row is deferred and the applied row later says `intent_recorded: false` — never a silent skip |
| `stop` | when a restore is needed, `compose down` whatever is running, so the previous tree's restore finds a stopped stack and takes no pre-restore set of its own |
| `identity` | reads the selected set's manifest: a set sealed to one recipient was made by a tree that knows no box identity, whose nightly would snapshot it, so `/etc/gideon/backup_age_identity` is removed and the row says so; a set sealed to two keeps it (ADR-0042). The next `upgrade` mints a fresh one in its provision phase |
| `checkout` | the tag pointing at the manifest's commit when one does, else the commit; as the owner |
| `restore` | when needed, the previous tree's `restore --from staging --set <label>`: verified whole before anything is replaced, Postgres back to the set's archive boundary |
| `apply` | the previous tree's `apply` — restore leaves the frontend and ingress down and names apply as the next command; here the command runs it |
| `verify` | as the forward path, against the set's release |
| `engine-verify` | the previous tree's `engine verify`; if it refuses, read the engine's logs and run `engine verify` by hand, never going live |
| `audit-applied` | the `rollback` row |

**What is preserved and what is not.** The database, `/etc/gideon`, the
registry, and the frontend's bulk data return to the pre-upgrade set's state:
anything written after the set was taken (a user's chat during a failed
upgrade) lives only in the `pre-rollback-*` safety set, when one could be
taken. **Host state converged for the newer release stays converged**: ADR-0005's
scope is the product, and a driver or package rollback is a `host provision`
decision a person takes, never something rollback does.

## 4. Re-runs and refusals

- `install` again on a live box: a no-op apply, an incremental set, a drill,
  the URL. Safe at any time.
- `upgrade <tag>` again after a reboot: the checkout is already at the tag, the
  set is reused, and the run resumes at the new tree's provision.
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
fixed by a hotfix the same afternoon and each recorded on ticket 08:

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

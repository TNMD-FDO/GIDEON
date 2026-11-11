# Install, upgrade, and rollback

This runbook arrived with `v0.0.25`. An office's computer systems administrator (CSA) runs two shell entrypoints, `./install.sh` and `./upgrade.sh`, which are three-line wrappers over `python3 -m gideon install` and `python3 -m gideon upgrade`. Install runs the existing commands in order.

Upgrade takes a full backup set before changing the checkout, so rollback can restore the prior state. A backup set is everything needed to bring GIDEON back on a bare machine, taken as one unit with a manifest. Each install, upgrade, and rollback is recorded as an `audit_log` row that can be queried, rather than a log line. The acceptance section below records the clean-VM exercise by two CSAs and arrived with `v0.1.0`.

## 1. The receiving-office sequence

This sequence takes a new server through an office's first installation, from cloning the release to a working URL. The later commands run from that checkout, so cloning comes first. Provision runs before the site file exists and again after it is written, because the site-dependent steps are blocked until then. Preflight checks the office services before install runs the phases as one command.

### Before you begin

- **Operating system.** Use Ubuntu Server 26.04 on `x86_64`, because provision refuses any other release or architecture. Provision brings the box's host layer to its target state.
- **Packages.** Install `git` and `python3-yaml` with `sudo apt install -y git python3-yaml`. This is safe to run if they are already installed.
- **Login.** Sign in with your own Linux account, one that can `sudo`, because that account will own `/opt/gideon`. Do not use root.
- **Hardware.** The reference profile `2x96v-256d` needs 2 × NVIDIA RTX PRO 6000 Blackwell (any edition), each with 96 GB VRAM, plus 256 GB RAM and a 4000 GB data volume. Preflight refuses a shortfall. Alternatively, declare a no-GPU host with `--no-gpu` at step 2. Preflight judges none of these figures on that host.
- **Download.** Apply brings the running stack in line with the site file and verifies it. Allow about 31 GB of model files at the first apply, including on a no-GPU host.
- **Office services.** Have the directory, DNS, backup target, certificates, and mail relay ready as described in [office services setup](office-services-setup.md).

Every step can be re-run. A refusal prints its fix and exits non-zero; make the fix, then run the same command again.

1. **Clone the release** to `/opt/gideon` using your own Linux account. Do not use root. Because `/opt` is root-owned, create and assign the directory first, then clone the release. Run the later commands from `/opt/gideon`:

   ```bash
   sudo install -d -o <account> -g <account> /opt/gideon
   git clone --branch <tag> https://github.com/TNMD-FDO/GIDEON /opt/gideon
   cd /opt/gideon
   git -c gpg.ssh.allowedSignersFile=config/release-signers verify-tag <tag>
   ```

   Run the verification as the checkout's owner. The first clone trusts the key it carries, so this proves only that the tag was signed by the key in that tree. Compare the key's ed25519 fingerprint with `SHA256:1YFQeEq2Jz/7kdmW4zkjXR3QBxvYQlqPUvnsYYD7rHw`. From then on, every `upgrade` verifies the next tag against the installed tree's `config/release-signers`.

   For `<account>`, use your own Linux login, the one `id -un` prints. Do not use root or a GitHub account. `<tag>` is a release tag. For example, with login `csa` and tag `v0.3.0`, run `sudo install -d -o csa -g csa /opt/gideon`, then run `git clone --branch v0.3.0 https://github.com/TNMD-FDO/GIDEON /opt/gideon`. Tags are listed newest first in [`CHANGELOG.md`](../../CHANGELOG.md) and on the [repository's tags page](https://github.com/TNMD-FDO/GIDEON/tags).

   `upgrade` runs git as whoever owns the directory, so no root-owned file lands in the tree. Provision installs a root-owned `/usr/local/bin/gideon` wrapper that runs this checkout as root, so whoever can change the checkout can run code as root. Keep the owner your sudo-holding login, one person. The repository is public, so the clone and `upgrade`'s fetch need no GitHub account or credentials.
2. **Provision the host**. The command prints one line per step. The first run prints the backup public key and the age identity **once**. The age identity opens the backup set's sealed secrets, so store it in the office password manager before going on (`backup-restore.md` §2). If a `reboot-required` row names the NVIDIA driver or a kernel, reboot, then run provision again until every step reads `ok`.

   The `wait-online` step (new in `v0.0.25`) makes the boot-time network wait accept any one link online, so a box with an unplugged second port no longer boots with a failed unit.

   Once provision has run, `gideon <command>` from any directory runs `sudo python3 -m gideon <command>` from `/opt/gideon`. The lines through this step keep the long form, the only form available before it. Every later line names the installed command unless it says why the run has none.

   ```bash
   sudo python3 -m gideon host provision
   ```
3. **Write the site file** at `/etc/gideon/site.yaml` from `config/site.example.yaml`. Place the supplied secrets as described in `office-services-setup.md`.

   The build box is TNMD-FDO's host declared to make the acceptance VM. On the build box, `gideon registry mirror` pulls the release's images into its loopback registry. The release registry at `v1.0.0` settles where a receiving office pulls its images from.

   Set `docker_address_pool` when an office host sits in a range Docker could assign to a container network. The next provision writes the pool and restarts Docker once, so announce a window for every container on the box. A network made before the restart keeps its subnet until it is removed. Pause anything that makes networks, such as a CI runner, from just before that provision until it finishes. Preflight's `docker-networks` row refuses while any Docker network's subnet covers a host the site file names or a `lan_cidrs` range, and names the network and its Compose project.
4. **Provision again**. The site-dependent steps, `egress-proxy`, `firewall`, `time-sync`, and `timezone`, are blocked until the site file exists, so this second run is required. Preflight refuses on any unconverged step.

   ```bash
   gideon host provision
   ```
5. **Preflight**. The command re-checks every provisioning step, then runs the install-time checks: the directory, the relay, the backup target, TLS material, ports, and disk. It exits 0 if and only if nothing refuses. Install opens with the same preflight, so a refusal here is a refusal there.

   ```bash
   sudo ./preflight.sh
   ```
6. **Install**. The command runs eight phases. Each nested command prints its own rows first, then install prints one phase row after it:

   ```bash
   sudo ./install.sh
   ```

   | Phase | Runs | What it proves |
   |---|---|---|
   | `preflight` | The command runs the preflight above. | The office services are reachable and correct before anything is built. |
   | `apply` | `apply` runs thirteen stages. | The stack is up and verified. The first run prints each break-glass password **once**. Break-glass passwords provide local administrator sign-in when the office directory does not work. Apply fetches the profile's models into `/data/models` (about 31 GB, 10–30 minutes of office bandwidth). Later runs verify them in about a minute. The engine lock is the box's exclusive claim on the engine. If an evaluation holds it, apply refuses naming its holder. Wait for it to finish, then re-run install. Its earlier steps are safe to repeat. |
   | `reconcile` | `users reconcile --now` runs. | Directory membership determines the frontend's roles, and audit rows are written. |
   | `engine-verify` | `engine verify` runs. | The engine answers at length and in structured form, and the guardrail's refusal arrives inside a real chat turn. These checks take three turns over two to eight minutes as the eval identity, the account GIDEON's own test turns sign in as. The embedding server returns unit vectors at the lock's width and records a batch's throughput. A failure stops install here with `Do not go live…`, the failing check's name, and that server's logs. A no-GPU host prints one skipped row (`v0.1.22`). If an evaluation holds the engine lock, verify refuses naming its holder. Wait for it to finish, then re-run install. Its earlier steps are safe to repeat. |
   | `backup` | `backup run` runs. | The first set exists, full on a fresh box and incremental on a re-run. |
   | `drill` | `backup drill` runs. | That set restores into the throwaway project, and the frontend comes up on it. |
   | `audit` | — | One `install` row with phase `applied` records the phase durations and the set label. |
   | `url` | — | The last line printed is `https://<hostname>/`. |

   A failed phase stops the run, and its fix names the nested command to run by hand (`Run sudo python3 -m gideon backup drill, correct its refusal, then re-run install.`). Re-running install on a live box is safe because every nested command is idempotent. A re-run is a no-op apply, a fresh incremental set, a drill, and the URL.

   An `install` row with phase `intent` is written once apply has converged the stores. A failure in preflight or apply leaves no row because the writer does not exist yet. Their own rows are the record.

   **The weights tree** (`v0.1.5`): apply's `models` stage, between `pull` and `recreate`, makes `/data/models` hold the profile's models in the Hugging Face hub-cache layout. Every file is verified against `models.lock`. Run `gideon models pull` for the same converge by hand. A re-run verifies and fetches nothing.

   The tree is derived state outside the backup set because it is derived from the model lock. After a rebuild, apply fetches it again. The pull keeps the pinned set and the newest previous complete set, so `upgrade --rollback` can re-apply the previous release without a download. Older sets are removed, and each removal is printed.

   `/data/models/gideon/pulls.yaml` is the pull's record. A pull that stops mid-file resumes on the next run. A second pull started while one runs refuses naming it.

   **The engine** (`v0.1.6`): on a GPU host, apply renders `gideon-generator` from the profile's `serve` block, using the vLLM image at its `images.lock` digest. The recorded GPU 0 card is reserved by UUID through CDI. `/data/models` is mounted read-only, and no port is published. Apply starts the engine in the `start` stage.

   The model takes minutes to load, so `verify` waits for the model servers together for up to the longest of their allowances from the start stage. This uses the same clock as each container's healthcheck. It reports `model servers healthy N s after start`, the moment the last of them answered. Every other service keeps its one-minute bound.

   The embedding server, `gideon-embed`, is the engine's sibling on the same image. It uses the recorded GPU 1 card by UUID, with a fixed share of that card held from its start. Its own key, `/etc/gideon/secrets/embed_api_key`, is read inside its container, and the same wait applies. It starts only when that share is free on the card; otherwise its logs name the shortfall, and `verify` fails naming them.

   The engine's API key is the generated secret `/etc/gideon/secrets/engine_api_key`. The `secrets` stage creates it only when absent. It is read inside the container and does not appear in the Compose file, argv, or a row. Each model server requires its key on every route but `/health` and `/metrics`. GIDEON middleware enforces this, loaded by the engine from a rendered file, so a client without the key sees 401 on any other route.

   The start-up log (the `GPU KV cache size` and `Maximum concurrency` lines) is in the journal at every boot: `sudo journalctl CONTAINER_NAME=gideon-gideon-generator-1 -b`. A vLLM image or model pin bump restarts the engine inside apply after the `models` stage. A rollback to a release without the engine removes its container as an orphan and leaves the key file. On a no-GPU host none of this is rendered.

   To rotate the key, run `gideon secrets rotate engine_api_key` (`v0.1.40`). The command first refuses when `apply` has pending changes or has not verified. Run `render --diff`, then run `apply`. The rotation command then prints what it will do. The engine is recreated with a cold start bounded by apply's engine wait, and the key's second mount, `gideon-api`, is recreated in seconds. The command writes a new value to the same path as `root:gideon` 0440 and force-recreates the engine once. A file secret is a bind mount, so the new key reaches only a new container. This is the rule `tls reload` follows for Caddy. The command converges the rest as apply does: verify, then record `applied.yaml`. Afterward, `render --diff` reports nothing.

   Since `v0.2.29` the frontend is not among the consumers: the engine's key left its env file at the cutover. The frontend's own connection key is `gideon_api_key`. Run `gideon secrets rotate gideon_api_key` to recreate `gideon-api` (its mount) and the frontend (its env file's owner).

   Run `gideon secrets rotate embed_api_key` to recreate the embedding server alone, with a cold start bounded by the same wait. Nothing else consumes its key yet.

   General is the chat branch users talk to, with internet search and no case-matter access. Once General is live, rotate in an announced maintenance window if the rotation recreates the engine, because the cold start makes General unavailable. A maintenance window is an announced span when General may be unavailable; §2 sets its rules. The command does not gate on the clock.

   The same command rotates `webui_secret_key`, recreating the frontend once and ending every signed-in session. For `searxng_secret_key`, it recreates SearXNG through the recreate rule. With search off, nothing consumes that key, and the command says so and writes nothing.

   Rotating `qdrant_api_key` recreates the vector store in seconds. Nothing else consumes that key yet. A `qdrant_read_only_api_key` rotation recreates the store in seconds, then the frontend. Signed-in sessions stay because the session key does not move.

   For `opensearch_password`, the lexical store is recreated and takes about ten seconds to become healthy. Rotating `opensearch_metrics_password` recreates the lexical store and Prometheus, and the scrape resumes within a minute.

   For `opensearch_transport_key` and `opensearch_transport_cert`, the lexical store is recreated. Rotating the certificate alone renews it from its standing key before its ten-year end date. Rotating the key re-issues the certificate with it.

   For the two minted keys, `gideon_admin_api_key` and `gideon_eval_api_key`, the apply-manifest stage removes and re-mints the file. Nothing is recreated.

   Run `gideon secrets rotate grafana_admin_password` to reset Grafana's stored password through Grafana's own CLI inside the container. It needs no old value. The command then rewrites the file and verifies the sign-in without recreating anything. Use it for a stored password nobody knows. Copy the file's new value into the office password manager.

   Every other name refuses before any change and names the office's path for that supplied secret. For `tls_key`, replace the files, then run `tls reload`.

   For `ldap_bind_password` and `smtp_password`, replace the file, then run `apply`. Grafana mounts those files, which the recreate rule does not see, so force-recreate Grafana by hand afterward: `sudo docker compose -f /etc/gideon/rendered/compose.yaml up -d --no-deps --force-recreate grafana`.

   For `proxy_auth`, replace the file, then run `apply`.

   A Postgres role password also refuses, because the role itself holds the value. So do `gideon_admin_password` and `gideon_eval_password`, because the frontend's account holds it.

   The rotation transcript is the record. A restore of a set made before a rotation brings the older value back. After that restore's `apply`, run `secrets rotate <name>` for every secret rotated since the set was made.
7. **Hand over the URL**, and test alerts once so the page path is proven through the relay (`observability.md`).

   ```bash
   gideon alerts test
   ```

Run `gideon corpus install <label>` to fetch and verify the files a committed lockfile pins. It stages and ingests each court's rows, then calls the build for each collection whose inputs changed. The build clusters near-duplicate chunks and builds both halves of an index generation, a complete build of one collection's indexes. It materializes each cited case's risk state for the generation and writes its build report to the record. The install records the label as installed and removes its work directory (the release-files runbook's §10). Run `gideon index build caselaw` for the same build directly. Run `gideon index build caselaw --sample` to build the eval sample generation on the `gideon-ci` sibling, the standing second stack on the box.

Run `gideon index promote <generation>` to serve a built generation. It runs the build gates as one recorded eval run and refuses on any failure with nothing changed. Then it marks the generation live in the record and points both stores' aliases at it. The previous generation is retired and kept seven days. Run it again after a failure between the two stores. Run `gideon backup run --full` after a production promote.

Run `gideon index report` to print what the record holds after an install or a build. It lists the lockfiles, the generations and their states, and the live or a named generation's documents per court. Run `gideon index report <generation>` to read a generation still building. A collection's build, by either command, starts only with a passing smoke run of its lockfile on the sibling's sample.

**No-GPU host.** `sudo python3 -m gideon host provision --no-gpu` writes `/etc/gideon/no-gpu` once. It is refused on a host with an NVIDIA device. Every later command reads the marker, so the driver and toolkit steps are skipped, and the DCGM exporter, its scrape job, the GPU board, and the driver-drift rule are not rendered. `--only` on a skipped step refuses and names the marker.

To leave the mode, remove the marker, then re-run provision. The next apply renders the removed files as new. The marker is not carried in a backup set.

**GPU record.** The first render on a GPU host, including `render --diff`, writes `/etc/gideon/gpus.yaml` from `nvidia-smi -L` order. Later renders bind each `models.lock` GPU position to that recorded card. If a recorded card is no longer reported, render refuses with its position and UUID.

After a card change, remove the file as root with `sudo rm /etc/gideon/gpus.yaml`, then run `gideon render --diff` to re-record the cards. Inspect what moves before applying. The record is not carried in a backup set.

**Build box.** The KVM, registry, runner, and archive-tools steps (`kvm`, `registry`, `gh-runner`, and `archive-tools`) converge only on TNMD's box. Declare it once with `gideon host provision --build-box`, which writes `/etc/gideon/build-box`. Every other host skips those steps. A no-GPU host cannot be declared the build box, nor the build box a no-GPU host. The marker is not carried in a backup set.

When a host already runs the three units as it crosses `v0.1.68`, run `gideon upgrade <tag>`. Its provision skips the three with a reason naming the declaration, leaving their units running. Its apply renders the host-unit rule and the runner's unit pattern away. Then run `gideon host provision --build-box`, which reports the three `ok`, followed by `gideon apply`, which renders the rule and the pattern back. Keep the transcript as §2 says. TNMD's box was declared on 2026-09-16, at `v0.1.71`'s release, before any upgrade. Its crossing was recorded with the marker removed to leave the mode, then the three commands.

To leave the mode, remove the marker, run provision again, then apply. The three units stay until removed by hand. Until then, `restore` refuses on a host whose registry is still active.

## 6. The acceptance run

This section records the maintaining office's proof of the receiving-office sequence on a throwaway VM.
Use it on the build box at a minor release; a receiving office does not run it.

The acceptance harness builds a throwaway VM from the pinned image and converts it to the LVM layout used on the box.
It proves the receiving-office sequence in a clean environment.
The VM has its own office services.
It reaches the real directory over NAT.
The harness runs a STARTTLS + AUTH SMTP sink on the libvirt bridge.
The VM is its own backup target.
A throwaway CA is bundled with the office root.
The sequence is exactly the one in §1 above.
Every product command runs as `sudo … 2>&1 | tee`, with its transcript retained under `--out`.

The harness prints one row per stage on its own stdout.
That output, under `sudo … 2>&1 | tee`, is the run's first record.
A transcript file under `--out` exists for every product command run inside the VM.
Its name uses the stage's two-digit position in the table:

| Stage | Product command in the VM | Transcript under `--out` |
|---|---|---|
| `provision` (7) | `host provision --no-gpu` | `07-provision.txt` (`-2`, `-3` after a `reboot-required` row) |
| `provision-2` (10) | `host provision --no-gpu` after the site file | `10-provision.txt` |
| `preflight` (12) | `./preflight.sh` | `12-preflight.txt` |
| `install` (13) | `./install.sh` | `13-install.txt` |
| `alerts` (14) | `alerts test` | `14-alerts.txt` |
| `rehearse` (16) | `./upgrade.sh <rc tag>`, `--rollback`, again, again. The tag is signed by the run-local key trusted by the mirror's trust commit | `16-rehearse-1-upgrade.txt` … `16-rehearse-4-rollback.txt` |
| `restore` (17) | `backup push`, `restore --from target`, `apply` | `17-restore-1-push.txt`, `17-restore-2-restore.txt`, `17-restore-3-apply.txt` |
| `verify` (18) | `./preflight.sh` | `18-verify-preflight.txt` |

Beside them, `console.log` holds the VM's serial console, the boot transcript when SSH does not answer.
The `mail/` directory holds every message the sink accepted, with its authenticated user and TLS fact.
The other stages run on the box: preconditions, image, seed, services, boot, clone, reboot, site, authorize, probe, teardown.
They leave only their row.

`--until <stage>` stops after a named stage (`provision-2` names the second provision).
`--keep` leaves the VM defined and running.
The harness prints its address.
The run's SSH key and known-hosts file stay under `/var/lib/libvirt/images/gideon-acceptance/<name>/`.
The next run first tears down a same-named leftover and the sink's firewall rule.
The harness runs on the build box under a CSA's or the dev seat's own sudo.
The dev seat is the CSA account the maintaining office's developer uses on the box.
No runner invokes the harness.

Acceptance runs happen at minor tags only.
Run against the local tag before the push, and commit its rows and transcripts as the tag's evidence, the record.
The same run from the box's checkout re-makes the record at any later tree.
Patch tags do not run the harness.
A tag can name any commit its pusher can reach.
Trust rests with the two CSAs authorized to create a `v*` tag, rather than anything in the tag's tree.
A `v*` ruleset restricts who can create tags an office clones.
The release's two-CSA run proves the install sequence in a clean VM and replaces the earlier by-hand exercise.

Transcripts redact print-once values and age identities before they reach the host.
An evidence check scans them for a complete age secret or an unredacted print-once line.
On TNMD's box a complete run takes under six minutes (the plan estimated 45).
That includes image conversion, boot, two provisions, preflight, install with the drill, `alerts test`, the four rehearsal legs, the push and target restore, and verify.
The converted image boots in about nine seconds.
The candidate tree's passing run is `08-acceptance-run-2026-09-03/`.

**What the runs of this release found.**
Each issue was fixed in the same release.
The firewall step's `DOCKER-USER` drops judged every forwarded packet to ports 443 and 5000, including the VM's NAT egress.
No HTTPS download from the VM could succeed.
The drops now name the Docker bridge they forward into.
A fresh cloud image ships empty apt lists, so the first `apt-get install` could not locate `skopeo` or `age`.
Every install now updates first.
Python 3.13+ verifies TLS chains strictly.
The throwaway CA therefore carries a key-usage extension, and each leaf carries its usages and server-auth purpose.
Apply's registry and pull stages judged every `images.lock` pin, while Compose pulls only the rendered services.
The DCGM exporter failed digest verification because it is not rendered on a no-GPU host.
Apply now judges the rendered stack's images.
The restore's `verify` stage refused an empty root because `sha256sum -c` refuses an empty list.
`/data/registry` holds nothing on a no-GPU host.
A root with no files is now verified without the call.
After the rehearsal's two promotions, a `restore --from target` refused.
pgBackRest's default recovery follows the cluster's current timeline, which had forked before the set's backup.
Every set restore now names its own backup, that backup's timeline, and its target.
This was the third timeline lesson after `v0.0.27`'s two.
A failed provision row also carries the failed command's own text now, which made the second of these issues visible.

**The `v0.1.0` run.**
The local-tag acceptance run on TNMD's box ran from 2026-09-03T21:28:56-05:00 to 2026-09-03T21:34:41-05:00, before the push.
Every stage was ok, exit 0.
The VM installed to its URL, and `alerts test` delivered through the sink.
The run completed the four rehearsal legs on `v0.1.1-rc.1`, the push and the target restore.
Verify confirmed release 0.1.0 by the applied record and the checkout's `--version`.
The run produced sixteen authenticated TLS messages and thirteen transcripts, then tore down the VM.
The evidence commit's rows are the standing record.
The pushed tag's own run, below, was the record until the tag-triggered run was retired.

**The CI record.**
The pushed tag's run refused at `boot`.
Its output directory was relative to the runner's workspace, while libvirt opened the console log from its own working directory.
Local runs had used an absolute output path.
The root run also left root-owned bytecode caches in the workspace.
The next checkout failed because the runner could not remove them.
Hotfix `v0.1.1` uses an absolute output path and avoids writing bytecode after the harness starts.
It returns the checkout's caches with the transcripts and lets you dispatch the acceptance workflow again for a selected tag.
The standing rerun against `v0.1.0` passed every stage from 2026-09-04T02:51:53Z to 2026-09-04T02:57:48Z.
Its transcripts were preserved as an artifact, and the workspace held nothing root-owned afterward.
While the workflow stood, a red tag run caused by the harness or workflow, rather than the tag's tree, was re-made the same way.
The operator dispatched the acceptance workflow at that tag.

**The `v0.2.0` run.**
Both acceptance forms ran against the local tag on TNMD's box from the release worktree before the push.
The no-GPU install passed every stage in 1252 seconds.
It completed `alerts test`, four rehearsal legs on `v0.2.1-rc.1`, the push and target restore, producing sixteen authenticated TLS messages and thirteen transcripts.
The full-restore form then restored the box's newest set from release 0.1.81.
It passed every stage in 1196 seconds, and the run directory peaked at 155 GB.
The box itself then upgraded 0.1.82 to 0.2.0 from the checkout on `main`.
`engine-verify` passed.
The run transcripts are kept with the release records.
The pushed tag's own two-step run was the standing record while the tag-triggered run stood.

**The `v0.3.0` run.**
The tag was cut twice.
At the first cut, the install form went red twice on the office resolvers answering `0.0.0.0` for `huggingface.co`.
An allowlist entry for the box cleared that problem.
The install form also went red once at its in-VM restore, which ran inside the 01:00 backup's minutes.
The restore passed on the next run started outside that hour.
The full-restore form then went red at its second `apply`.
The restored applied record names the build box's nightly units, which the no-GPU VM had not linked.
`apply` refused to disable them.
That fix moved the tag.
At the re-cut tag, the no-GPU install passed every stage in 1137 seconds.
It produced eleven authenticated TLS messages and thirteen transcripts, with about 52 GB allocated.
The full-restore form restored the box's newest set from release 0.2.98 and passed every stage in 1101 seconds.
It peaked at about 168 GB.
The box then upgraded 0.2.98 to 0.3.0 from `/opt/gideon` in about six minutes.
Its apply recreated nothing, and `engine-verify` passed.
The run transcripts are kept with the release records.
The pushed tag's own two-step run was the standing record while the tag-triggered run stood.

## 2. Upgrading: `gideon upgrade <tag>`

An upgrade moves an installed box to a newer release after its release note has arrived and a window is available. It takes a full backup set before moving the checkout, so rollback has a way back, then runs the new tree's own commands.

Run as root from any directory against the install home's clean work tree. The install home is `/opt/gideon`, which holds the installed release, and its work tree must have no changes to tracked files. An untracked file is no obstacle, so `gideon upgrade <tag> 2>&1 | tee <transcript file>` keeps the command output with the office's records. The command runs from the *current* release's tree and hands over to the new tree's own CLI after the checkout; nothing of the new release is loaded into the running process. `sudo ./upgrade.sh <tag>` from the checkout runs the same command.

The installed release's site loader refuses keys it does not know from every command and timer running from `/opt/gideon`, and `upgrade` loads the site file before checking out the new release, so a key the new release introduces would stop the upgrade before it starts. Add any key named in a release note's "new site keys" line to `/etc/gideon/site.yaml` only after that release's `upgrade` finishes, then run `sudo gideon host provision` followed by `sudo gideon apply`. `docker_address_pool` is the first key that follows this sequence.

**From go-live.** A tag is user-facing from the office's first users; before them, the rules below do not bind. The **quiet window** is weeknights 19:00–06:00 and Friday 19:00 to Monday 06:00 in the site's timezone (TNMD: America/Chicago). Work is window-bound when it sends requests to the engine on GPU 0. The nightly is the one scheduled task that does this: `gideon-eval-nightly.timer` fires at 21:00 office time every night, weekends included, and runs the `general-smoke` and `guardrails` suites as one `--kind nightly` process under one engine lock claim. It refuses outside 19:00–06:00, waits once behind another GIDEON run's lock, and stops each suite at 06:00, recording it partial.

A **maintenance window** is an announced span inside the quiet window when General may be unavailable, and this is the only sanctioned unavailability. Announce it at least one working day ahead, with weekend nights as the default. An engine swap, a driver, engine, or Docker change, and every upgrade on a user-facing tag take a maintenance window. Read its cost beforehand from `render --diff`'s recreate row, and count on the whole stack only when `provision` or a rollback needs it.

At least one working day before the window, send the release's note from `docs/release-notes/<tag>.md` to the users and inform the supervisor. The release note's `## Breaking` section is what the `version` stage below prints. A model upgrade's proof window precedes its tag. Announce it under `docs/runbooks/model-upgrade.md` §4, then send the release's note with the tag.

| Stage | What happens | Fix on a refusal |
|---|---|---|
| `preconditions` | The command requires root, a valid site file, Docker Compose answering, and a checkout that is a git work tree with a clean status. It resolves the owner from the directory. | Commit or stash as the owner, then re-run. |
| `fetch` | `git fetch --tags --force origin` runs as the owner, and the tag must resolve to a commit. A tag the fetch moved is named with both commits. If the fetch fails but the tag is already present, the command continues and says so. | Fetch by hand as the owner with the same command, then re-run. |
| `signature` | `git verify-tag` runs as the owner against the installed tree's `config/release-signers`. The command refuses a lightweight, unsigned, foreign-key, or unverifiable tag before anything is checked out. | Confirm the tag on the project's tags page and release note, then re-run. For a newer signing key, upgrade first to the release whose signers file carries it. Restore a missing signers file by following `docs/runbooks/release-files.md §12`. |
| `version` | The tag's tree must declare the version its name says. A lower version refuses, naming `upgrade --rollback` and `restore`, while the same version is a re-run. **A different major prints the release note's `## Breaking` section first and refuses without `--acknowledge-breaking`**. | Read the section, then re-run with the flag. |
| `preflight` | The current tree's install-time checks refuse as `./preflight.sh` does and report provision steps here. An unconverged step warns instead of refusing because the new release's `provision` stage converges it and its `preflight` judges it. | Correct the office service it names, then re-run. |
| `backup` | The pre-upgrade set, `pre-<tag>`, is a full set whose manifest must record the commit the checkout is leaving, rollback's way back. When the plain label exists from an attempt that did not cross the checkout, a suffixed `pre-<tag>-<timestamp>` set is taken because the earlier one may predate later writes. When the checkout already stands at the tag, an earlier attempt crossed it, so the newest `pre-<tag>[-…]` set naming another release is **reused** and is not re-taken. | Follow the fix in the backup's own rows. |
| `audit-intent` | One `upgrade` row records the from-version, to-tag, both commits, and the set label. | Correct the Postgres service, then re-run. |
| `checkout` | `git checkout --detach <tag>` runs as the owner and is skipped when already there. | Re-run the command. |
| `provision` | The **new tree's** `host provision` runs with `--acknowledge-disruption` passed through when given. Its rows are streamed as they happen, and it exits 0 on `blocked` and `reboot-required` rows. | A failed child names its refusal and the fix to re-run `upgrade <tag>`, with `--acknowledge-disruption` when the provision row asks for it. The next stage judges `blocked` and `reboot-required`. |
| `preflight` | The new tree's preflight gates apply and runs the new release's own readiness checks. An unconverged or `reboot-required` step, or a new office-services requirement, refuses here; nothing of the product has changed yet. | **Reboot if asked. Then re-run `upgrade <tag>`**. The checkout is already made and the set is reused, so the re-run resumes here. |
| `apply` | The new tree's `apply` runs thirteen stages, streamed. A pin bump in `models.lock` fetches the new revision here, before the engine restarts. | Run `upgrade --rollback` unless the row names the engine lock's holder. When the row names that holder, nothing was changed. Wait for that evaluation to finish, then re-run `upgrade <tag>`, which the version stage accepts as a re-run. |
| `verify` | The new tree answers `--version` with the tag's version, and the applied record names it. Every service the rendered project declares has a container that is running and healthy on a fresh read. | Run `upgrade --rollback`. |
| `engine-verify` | The new tree's `engine verify` runs as a passthrough child, and its failing row names its own server's logs. | Run `upgrade --rollback` unless the row names the engine lock's holder or the model's decline on every attempt. When the row names that holder, nothing was changed. Wait for that evaluation to finish, then re-run `upgrade <tag>`, which the version stage accepts as a re-run. When the failing row names the model's decline on every attempt (the trip's `answered`, the structured row's `length`), nothing is broken. Run `engine verify` by hand. When it declines again, supersede the case as `eval/engine-verify/README.md` says. Do not run `upgrade --rollback` for that decline. |
| `audit-applied` | The `upgrade` row records the durations. | — |

The standing next step after a failure depends on where it happened. Before the checkout, correct the problem, then re-run. At the new tree's provision or preflight, reboot or correct the problem, then re-run; alternatively, run `upgrade --rollback`, which then only moves the checkout back. At or after the new tree's apply, run `upgrade --rollback`. Every refusal prints exactly that.

An upgrade takes about the time of a preflight before and after the checkout (two relay test messages), one full set, a provision check pass, and an apply. A full set takes about 35 s on TNMD's box, and the `engine-verify` gate adds about two to eight minutes. A re-sent sample adds a turn. The pre-upgrade set is not pushed; the nightly unit pushes.

## 3. Rolling back: `gideon upgrade --rollback [<tag>]`

A rollback returns the release and data to the point saved before an upgrade when the upgrade ended red or the new release is wrong. It restores the pre-upgrade set and applies the previous release because that set *is* the record of what to restore.

The set's manifest carries the checkout's commit, the release, and the archive boundary. Restoring the set returns both data and product state to the saved point. The small record `/data/backup-staging/rollback.json` tracks how far a rollback got and exists only while a rollback is in progress (the `plan` row below).

Before rolling back across the release that introduced `docker_address_pool`, remove the field from `/etc/gideon/site.yaml`. The previous release's site loader refuses it, so its rollback stages cannot start while the field is present. The restore replaces the site file with the pre-upgrade set's copy. That copy did not hold the field because the set includes `/etc/gideon`. The daemon's `default-address-pools` key remains; the previous release treats it as foreign and keeps it, so the pool still protects new Docker networks.

| Stage | What happens |
|---|---|
| `select` | Rollback selects the newest complete `pre-v…` set (with or without a timestamp suffix), or the one for `<tag>` when given. Other `pre-*` labels on the box and the `pre-rollback-*` safety sets are not candidates. The selected set's commit must exist in the checkout. When the running tree already is the set's release *and* the applied record names it, there is nothing to roll back to. Use `restore --from staging --set <label>` to restore the data without changing the release. When only the tree is the set's release, a rollback stopped after its checkout. The re-run resumes from the previous tree. |
| `plan` | Whether a restore is needed is judged from `rendered/manifest.yaml`, the first thing an apply writes, before any stage touches the stores. A manifest still naming the set's release proves the new tree's apply did not complete a render. In that case, rollback only moves the checkout back and re-verifies. Any other release, or a missing or unreadable manifest, means a restore. The verdict is written to `/data/backup-staging/rollback.json` beside `push.json`, recording the rollback in progress. The record is updated when the restore is done and removed once the `rollback` row is written. A re-run resumes from that record. |
| `safety` | When a restore is needed and the whole stack is running, the *current* tree takes a full `pre-rollback-<timestamp>` set. Its files, checkout copy, and database describe the release that is running. A partial or stopped stack gets none. The row says that anything written since the upgrade began is not preserved. |
| `audit-intent` | A `rollback` row is written when Postgres answers. When Postgres does not answer, which is the reason for many rollbacks, the row is deferred. The applied row later says `intent_recorded: false`, so the deferral is explicit. |
| `stop` | When a restore is needed, `compose down` stops whatever is running. The previous tree's restore finds a stopped stack and takes no pre-restore set of its own. |
| `identity` | The command reads the selected set's manifest. A set sealed to one recipient was made by a tree that knows no box identity. That tree's nightly would snapshot the identity, so `/etc/gideon/backup_age_identity` is removed and the row says so. A set sealed to two keeps the identity so this box can open its own sets. The next `upgrade` mints a fresh one in its provision phase. |
| `checkout` | The command checks out the tag pointing at the manifest's commit when one exists, or the commit otherwise. The checkout runs as the owner. |
| `restore` | When needed, the previous tree's `restore --from staging --set <label>` runs. The set is verified whole before anything is replaced, and Postgres returns to its archive boundary. |
| `apply` | The previous tree's `apply` runs. Restore leaves the frontend and ingress down and names apply as the next command. The rollback command runs it here. |
| `verify` | Verification follows the forward path against the set's release. |
| `engine-verify` | The previous tree's `engine verify` runs. If it refuses, read the failing row's own server logs, then run `engine verify` by hand. Do not go live. When the failing row names the model's decline on every attempt (the trip's `answered`, the structured row's `length`), nothing is broken. Run `engine verify` by hand. When it declines again, supersede the case as `eval/engine-verify/README.md` says. |
| `audit-applied` | The `rollback` row is written. |

**What is preserved and what is not.** The database, `/etc/gideon`, the registry, and the frontend's bulk data return to the pre-upgrade set's state. Anything written after the set was taken lives only in the `pre-rollback-*` safety set, when one could be taken. This includes a user's chat during a failed upgrade. **Host state converged for the newer release stays converged** because rollback restores the product from its set, not the host's drivers or packages. The office decides on a driver or package rollback through `host provision`; rollback does not do it.

A rollback by `upgrade --rollback` or `restore` to a tag before GIDEON's firewall rules moved into their own chain restores the older firewall writer. That writer declares and flushes the shared `DOCKER-USER` chain, so its first ufw reload erases a co-tenant application's rules there. A co-tenant is another application the office runs on the same box. Every later provision run on that older tree erases those rules again until GIDEON upgrades back. GIDEON's own published ports stay protected by the older rules. Announce the firewall consequence and the hour with the rollback. Tell the co-tenant to re-add its rules after the old tree has run. Announce the return the same way.

A rollback by `upgrade --rollback` or `restore` to a tag from before the engine's integration network, `gideon_integration`, renders `gideon-generator` on `gideon_gideon` alone. The integration network connects the engine and its clients. The older sibling tool puts its relay back on `gideon_gideon` at its next `up`. `gideon_integration` stays in place with no engine on it, so a co-tenant's client on that network cannot reach the engine until GIDEON upgrades back or the client's operator moves it back to `gideon_gideon`. Announce that consequence and the hour with the rollback. Announce the return the same way.

Release `v0.3.50` added candidate recording to `models pull`. A rollback by `upgrade --rollback` or `restore` to an earlier tag refuses at the older tree's `models` stage while the build box's pull record holds a `candidates:` map, because the older parser accepts the four slots alone and names them in its refusal. That record is `/data/models/gideon/pulls.yaml`. Only the build box meets this, since a shipped lock carries no candidate. Delete the `candidates:` map from the record by hand, leaving the four slots as they stand, then re-run the rollback, which resumes where it stopped (the `select` and `plan` rows above). Alternatively, re-run the `apply` a by-hand `restore` printed. The refusal also offers removing the whole record. Do not remove it, since a removed record forgets which sets are complete. The older tree's next pull keeps every tree it finds, including the previous set's. The older tree leaves the candidate's folder alone as one it does not know. The next `models pull <candidate>` on a tree at or above that release records the folder again and fetches nothing.

## 4. Re-runs and refusals

This section collects safe re-runs and fixes for common refusals after a command refuses or the site file changes. Every command from provision on can be run again, so the same command can follow a refusal once its fix is made.

- Running `install` again on a live box produces a no-op apply, an incremental set, a drill, and the URL. It is safe at any time.
- After editing `/etc/gideon/site.yaml`, run `gideon apply` to converge the running stack to the site file. If the edit changed `alerts.*`, follow apply with `gideon alerts test` (`observability.md` §4). Place a renewed certificate in the certificate and key files, then pick it up with `gideon tls reload`. This is not a site-file edit (`office-services-setup.md` §4).
- Running `upgrade <tag>` again after a reboot reuses the set because the checkout is already at the tag. The run resumes at the new tree's provision.
- If provision refuses because running containers lack GIDEON's ownership mark, announce a maintenance window to every project sharing the daemon (§9). The ownership mark identifies host state as GIDEON's. Then re-run `host provision` or `upgrade <tag>` with `--acknowledge-disruption`. When the release running the upgrade has no such flag on `upgrade`, the checkout is already at the tag. From `/opt/gideon`, run `gideon host provision --acknowledge-disruption` by hand. Then re-run `gideon upgrade <tag>`, which resumes at the new tree's provision and reuses the set.
- Running `upgrade <tag>` again after an attempt that stopped before the checkout takes a fresh suffixed set. The stale plain-label set is left alone and can be pruned by hand.
- Running `upgrade <tag>` at the tag with no earlier set naming another release means the record is gone. Go back by hand with `restore --from staging --set <label>` of an earlier set.
- `upgrade <current version>` is a re-run. `upgrade <lower>` refuses, naming `upgrade --rollback` and `restore`.
- A tag whose tree declares another version is refused. Retag it.
- A dirty checkout refuses before anything else. Commit or stash as the owner. A root-owned checkout runs git without `sudo -u`.
- `--rollback` with no `pre-v*` set refuses, naming `restore`.
- Running `--rollback` again after a failure resumes the same command from `/data/backup-staging/rollback.json`. The restore repeats when it did not finish and is skipped when it did. Apply and verify follow. With no record and the running tree and applied record both at the set's release, there is nothing to roll back to, so the command refuses. A record naming another set refuses until that rollback is finished (`--rollback <its tag>`) or the file is removed by hand after checking.
- `restore --set` with `--at` or `--from target` refuses. Use `--at` for a point in time and `--from target` for the off-box copy. A `.partial` or unknown label refuses in `select`, listing the complete labels.
- Running `secrets rotate <name>` again makes a second rotation: a fresh value, with the consumers recreated again. A rotation that failed after its value was written is finished by `apply`. Apply converges the carriers: the env file's owner, a minted key's re-mint, and the record. It clears the command's pending-change precondition. Where a mounting consumer (the engine) was not reached, run `secrets rotate <name>` once more after that `apply`. A pending `apply` refuses the command until `apply` has run. This includes a site edit, a new checkout, or no verified apply on record.

## 5. The record: TNMD's box, 2026-09-03

This section records what happened on TNMD's box and holds no step to follow.
Use it to compare your run with the recorded install and upgrade.

`v0.0.25` reached the box through `sudo ./install.sh`, rather than `upgrade`.
The release before it had no upgrade command, and the checkout already stood at the tag.
The first attempt refused at its opening preflight because the release had added the `wait-online` step and provision had not run yet.
The order §1 gives is enforced.
After `host provision --only wait-online`, the eight phases ran: a no-op apply, an incremental set, a passing drill, and the URL.
The records are `08-install-rerun.txt` and `08-wait-online.txt`.

The rehearsal ran on throwaway tags, which were not pushed, and found two defects.
Each was fixed by a hotfix the same afternoon:

| Leg | Command | What it proved |
|---|---|---|
| 0 | `upgrade v0.0.26-rc.1` on `v0.0.25` | stalled at the backup stage's readiness probe: a nested `sudo -u` under `sudo-rs`'s `use_pty` and a `compose exec` reading the terminal. It was aborted cleanly, with nothing changed (`08-rehearsal-0-stalled-on-v0.0.25.txt`). Hotfix `v0.0.26` fixed it |
| A | `upgrade v0.0.27-rc.1` on `v0.0.26` | the full set `pre-v0.0.27-rc.1`, the checkout as the owner, the rc tree's provision 20/20, preflight, apply, verify (`08-rehearsal-1-upgrade.txt`) |
| A2 | the same again | the set reused, the checkout already at the tag, a clean re-run (same file) |
| B | `--rollback` | restore needed, the safety set, the stack stopped, the checkout to the `v0.0.26` tag, the previous tree's restore to the boundary, apply, verify, the row (`08-rehearsal-2-rollback.txt`) |
| C | `upgrade v0.0.27-rc.1` again | the suffixed set `pre-v0.0.27-rc.1-<ts>` (`08-rehearsal-3-upgrade-again.txt`) |
| D | `--rollback` again | pgBackRest refused the Postgres restore: its auto-selection skipped the set's own backup for one on the pre-rollback timeline. The restore was finished by hand with `--set` (`08-rehearsal-4-rollback.txt`, `-4a-`, `-4b-`). Hotfix `v0.0.27` fixed it |
| E | `upgrade v0.0.28-rc.1` on `v0.0.27` | as A (`08-rehearsal-5-upgrade.txt`) |
| F | `--rollback` | first refused by leg D's stale in-progress record, as designed. Then a rollback across two earlier promotions restored the set's own backup, followed by apply, verify, and the row (`08-rehearsal-6-rollback.txt`) |

Each rollback's unavailability, from its `stop` row to apply's `verify`, was about three minutes on this box.
The rehearsal left six labelled sets under `/data/backup-staging/sets/`, pushed and pruned like any set.

The `wait-online` step converged in one row, and the box was rebooted after the rehearsal.
The unit's result and the post-boot provision pass are the tail of `08-wait-online.txt`.
The first real upgrade was `sudo ./upgrade.sh v0.1.1` on 2026-09-03 at 22:56 CDT (`08-upgrade-v0.1.1.txt`).
It ran from the checkout on `main`, which already declared 0.1.1 because development had moved it past the applied `0.0.27`.
The `version` row read it as a re-run of 0.1.1, and the fresh pre-upgrade set `pre-v0.1.1` was taken from that tree.
The way back is therefore `restore --from staging --set pre-v0.1.1` rather than `--rollback`, which would find nothing to do.
The 0.0.27 tree could not have run it.
Its firewall check sees the DOCKER-USER block `v0.1.0` rewrote as drift, and its preflight would have refused.
Every row was ok.
`fetch` reached GitHub as `fpdadmin`.
The new tree's provision moved nothing (20/20, the `kvm`, `firewall`, and `gh-runner` state already converged from the candidate tree).
Apply recreated the services whose files changed.
Verify confirmed 0.1.1 by `--version`, the applied record, and every service healthy.
The box runs `v0.1.1`.
The checkout is back on `main`, whose product code equals the tag.

## 7. Moving a populated image store

This procedure moves Docker's populated image store onto its volume by hand in an announced maintenance window.
Use it when `host provision` reports `docker-engine: failed` naming a populated package-default containerd root, or `apply` refuses naming that root beside an empty root on the Docker volume.

The image store is containerd's directory for Docker's images.
Starting the daemon on that empty root would make its existing images and container snapshots disappear.
Every Compose project using the Docker daemon is down during the copy, including any other project sharing it.
Tell users before starting.

### Survey

Keep the GIDEON and any other Compose projects running for the survey.
Check free space on the root filesystem and the Docker volume.
Measure the current store.
Record the image and container lists for comparison after the move.
The move record directory is root-owned and is kept until acceptance:

```sh
sudo install -d -m 0700 /var/lib/gideon-store-move
sudo df -h / /var/lib/docker
sudo du -sb /var/lib/containerd
sudo bash -o pipefail -c 'docker image ls --digests --format "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}" | sort > /var/lib/gideon-store-move/images.before'
sudo bash -o pipefail -c 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}" | sort > /var/lib/gideon-store-move/containers.before'
sudo docker system df
sudo cp -p /etc/containerd/config.toml /var/lib/gideon-store-move/config.toml.shipped
```

Check for pending package actions affecting Docker or containerd with `sudo apt-get update -q` and `sudo apt-get -s install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`.
If the simulation lists an action, apply it while all projects remain up with `sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`.
Then repeat the survey and replace the saved lists and configuration.
Do not stop the daemons until the simulation reports no pending action.

On a box with a self-hosted GitHub Actions runner, a running job may hold its own Compose stack on the daemon.
A package action restarts the daemon under it as Stop does, failing the job.
Before applying a package action, and again before Stop, run `gh run list --status in_progress` from a clone of each repository the runner serves.
Wait until it lists no run.

### Stop

Stop the release registry, Docker's socket and service, then containerd.
`gideon-registry.service` is provisioned on the build box alone.
On any other box, leave out every command below that names it, here and in the stages after.

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

Each `systemctl is-active` command should print `inactive`.
Its non-zero status is expected for an inactive unit.
`pgrep` should print no processes and return status 1.
Any other failure means its result is unknown.
`findmnt` should show no mounts under the old root.
`ls` should print nothing for the empty `tmpmounts` directory.
If a process, mount, or temporary entry remains, leave the store in place.
Resolve it before continuing.

### Copy

Copy the old root to the Docker volume.
The command preserves hard links, ACLs, extended attributes, sparse files, and numeric owners.
If interrupted, run the same command again to resume the copy:

```sh
sudo mkdir -p /var/lib/docker/containerd
sudo rsync -aHAXxS --numeric-ids /var/lib/containerd/ /var/lib/docker/containerd/
```

### Verify

With all daemons still stopped, run a second dry pass.
It must exit successfully and print no itemized differences:

```sh
sudo rsync -aHAXxS --numeric-ids -n -i /var/lib/containerd/ /var/lib/docker/containerd/
```

Compare the roots' inode counts and apparent byte counts.
Also compare the counts of character-device whiteouts under `io.containerd.snapshotter.v1.overlayfs/snapshots` and directories carrying the `trusted.overlay.opaque` extended attribute:

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

All four counts must match.
Whiteouts are device nodes, and opaque markers are extended attributes.
A plain file copy would lose overlay state.
Do not provision if the dry pass reports a difference or any count differs.

### Provision

A package action during provision must not start a daemon on the old root.
Hold every package maintainer script back with a temporary `policy-rc.d` for the one step.
If `/usr/sbin/policy-rc.d` already exists, it is the host's own policy.
Stop and resolve it rather than overwrite it.
From `/opt/gideon`, write the new containerd root and restart the managed daemons:

```sh
cd /opt/gideon
printf '#!/bin/sh\nexit 101\n' | sudo tee /usr/sbin/policy-rc.d >/dev/null
sudo chmod 755 /usr/sbin/policy-rc.d
gideon host provision --only docker-engine --acknowledge-disruption
sudo rm /usr/sbin/policy-rc.d
sudo systemctl start gideon-registry.service
sudo containerd config dump
```

Remove `/usr/sbin/policy-rc.d` whether or not provision succeeds.
The daemons are stopped, so provision cannot list the running containers it would otherwise guard.
The flag acknowledges the window already announced.
`--only` runs only when every step before `docker-engine` has converged.
If the box's first provision met the refusal before its site file existed, write the site file before the survey.
Then run `gideon host provision` once more before the survey.
It converges the rest and refuses at `docker-engine` again.

Confirm Docker's socket and service, containerd, and on the build box the registry are active.
Confirm `containerd config dump` reports `/var/lib/docker/containerd` as the root.
If provision or that check fails, keep both roots.
Use the rollback below.

### Check and accept

Wait for the containers that were running in the survey to return and for their health checks to pass.
Save fresh lists.
Compare them with the survey; both diffs must exit zero:

```sh
sudo bash -o pipefail -c 'docker image ls --digests --format "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}" | sort > /var/lib/gideon-store-move/images.after'
sudo bash -o pipefail -c 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Label \"com.docker.compose.project\"}}" | sort > /var/lib/gideon-store-move/containers.after'
sudo diff -u /var/lib/gideon-store-move/images.before /var/lib/gideon-store-move/images.after
sudo diff -u /var/lib/gideon-store-move/containers.before /var/lib/gideon-store-move/containers.after
sudo docker ps --filter health=unhealthy
sudo du -sb /var/lib/docker/containerd
sudo df -h / /var/lib/docker
```

Do not accept the move until both list comparisons are identical, no unhealthy container is reported, and the engine is healthy.
Every surveyed project must also have returned to its surveyed state before you accept the move.
Record the responsible operator's confirmation with the move record:

```sh
printf '%s\n' '<operator confirmation>' | sudo tee /var/lib/gideon-store-move/accept.txt >/dev/null
```

### Remove

Only after acceptance, confirm once more that `containerd config dump` names `/var/lib/docker/containerd` and that the surveyed projects are healthy.
Then remove the old root and the temporary move record:

```sh
sudo rm -rf /var/lib/containerd /var/lib/gideon-store-move
gideon host provision
```

The closing provision, from `/opt/gideon`, should report every step converged.

### Roll back before acceptance

Rollback is available after the stop and before acceptance.
Stop the registry, Docker socket and service, and containerd.
Then restore the saved configuration.
Start the old root:

```sh
sudo systemctl stop gideon-registry.service
sudo systemctl stop docker.socket docker.service
sudo systemctl stop containerd.service
sudo cp -p /var/lib/gideon-store-move/config.toml.shipped /etc/containerd/config.toml
sudo systemctl start containerd.service
sudo containerd config dump
```

Continue only when `containerd config dump` reports `/var/lib/containerd` as the root.
Otherwise stop and remove nothing.
Then start the rest:

```sh
sudo systemctl start docker.socket docker.service
sudo systemctl start gideon-registry.service
```

Wait for the surveyed projects to return.
Save fresh image and container lists.
Compare them with the saved lists in `/var/lib/gideon-store-move`.
Confirm the old root is active before removing the copied `/var/lib/docker/containerd` root.
Keep both roots until those checks pass.

## 8. Moving an occupied `/data` onto the data volume

This procedure moves existing `/data` contents onto the data volume.
Use it when `host provision` reports `disk-layout: unfixable` and names entries held by an unmounted `/data`.

The directory is on the root filesystem.
Another application's data may have been written there before GIDEON provisioned its volume.
Mounting the volume would hide that data.
Announce the move to the owning operator.
Use a maintenance window.
The owning application stays down from **Stop** until **Start and accept**.

Keep a root-owned record in `/var/lib/gideon-data-move` until acceptance.
Each completed stage writes a stamp there.
`copy.started` is written *before* copying.
On an interrupted run, inspect the stamps.
Continue at the unfinished stage.
Do not repeat the volume inventory after `copy.started` exists: it would count the move's own copies as preexisting data.
Stop if a command fails or a required tool is unavailable.
Repair that condition before continuing.

### Survey

Confirm `/data` is a directory and not a mount point.
Start a new record only when `/var/lib/gideon-data-move` is absent.
If a survey was interrupted before its stamp, inspect the record.
Repeat the survey only while the original `/data` is still in place.
Skip the new-record absence check on that repeat.
Record the sorted top-level names as NUL-delimited data.
Record each entry's owner, group, mode, and apparent size.
Check root filesystem free space and the data's apparent size.
Check that `lsof` and `rsync` are available before the stop:

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

The NUL-delimited record keeps names containing spaces or newlines intact.
The following check must succeed and print nothing.
GIDEON owns these top-level names and will re-own them.
A co-tenant keeps its data outside them.
If one of those top-level names is present, stop.
Have the owning operator rename that entry on their side.
Then repeat the survey before stamping it:

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

Use Docker's listing to record running containers with a bind mount sourced from `/data` or a path below it.
With `pipefail` enabled, a failed Docker listing or inspection stops the survey.
The record may be empty:

```sh
set -o pipefail
sudo docker ps -q --no-trunc |
    xargs -r sudo docker inspect --format '{{range .Mounts}}{{printf "%s\t%s\n" $.Name .Source}}{{end}}' |
    awk -F '\t' '$2 == "/data" || index($2, "/data/") == 1' |
    sudo tee /var/lib/gideon-data-move/containers.before
sudo touch /var/lib/gideon-data-move/survey.done
```

### Stop

The owning operator stops their application by its own commands.
Confirm no process holds a path under `/data` open, no running container mounts it, and no nested mount remains.
`lsof` should print nothing and return status 1.
Output, a diagnostic, or any other status stops the move.
The Docker and mount listings must succeed and be empty.
Do not set the stamp until all checks have passed:

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

Rename `/data` on the same filesystem.
This is an immediate rename, not a copy.
The set-aside path must be absent.
Leave `/data` absent for provision to create.
Do not pre-create it:

```sh
sudo test ! -e /data.before-gideon-move
sudo test ! -L /data.before-gideon-move
sudo mv -T /data /data.before-gideon-move
sudo test ! -e /data
sudo touch /var/lib/gideon-data-move/aside.done
```

### Provision

From `/opt/gideon`, converge the volume and mount it.
On a box that has not finished its first provision, `--only` refuses because its prerequisite has not converged.
Run the whole provision there instead.
Proceed only when `findmnt` shows `/data` as an XFS mount point on the data LV and its size is as expected:

```sh
cd /opt/gideon
gideon host provision --only disk-layout
# On a first-provision box, use instead: sudo python3 -m gideon host provision
sudo findmnt -rn -o TARGET,SOURCE,FSTYPE,SIZE --mountpoint /data
sudo lvs -o lv_name,lv_size vg_data
sudo touch /var/lib/gideon-data-move/provision.done
```

### Inventory the volume

Before the first copy, record the mounted volume's own sorted top-level names.
A volume previously built and left unmounted may already contain the owning application's data.
Any name shared with the original survey stops the move.
Do not merge the trees.
The owning operator reconciles the two copies by hand, keeping, renaming, or removing one, before inventory is repeated and the copy starts.
Once the inventory is stamped, keep it.
After `copy.started`, do not re-inventory on a resume:

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

Stamp the start **before** the transfer.
One `rsync` invocation copies the whole recorded entry list under the same names.
It preserves hard links even between different top-level entries, ACLs, extended attributes, sparse files, and numeric owners.
`--files-from` needs explicit recursion.
If interrupted, rerun this same `rsync` command.
Keep the original survey and volume inventory:

```sh
sudo test -f /var/lib/gideon-data-move/inventory.done
sudo touch /var/lib/gideon-data-move/copy.started
sudo rsync -aHAXxSr --numeric-ids --from0 --files-from=/var/lib/gideon-data-move/entries.nul /data.before-gideon-move/ /data/
sudo touch /var/lib/gideon-data-move/copy.done
```

### Verify

With the application still stopped, make a second dry pass over **only the surveyed entries**.
`rsync` must exit successfully.
The itemized difference file must be empty.
Then compare each entry's regular-file count and the sum of those files' sizes on both sides.
Do not compare whole roots.
Provision's directories sit beside the copies, and ext4 and XFS can report different directory sizes:

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

The owning operator starts their application and confirms it works on the moved data.
Record that operator's confirmation and time in the move record.
From this start onward, the application writes to the volume alone.
The rollback below is no longer safe:

```sh
printf '%s\n' '<operator confirmation and time>' | sudo tee /var/lib/gideon-data-move/accept.txt >/dev/null
sudo touch /var/lib/gideon-data-move/start.accepted
```

### Remove

Only after acceptance, confirm the data volume is still mounted.
Then remove the set-aside tree and the move record.
A closing provision from `/opt/gideon` should report every step converged:

```sh
sudo test -f /var/lib/gideon-data-move/start.accepted
sudo findmnt -rn -o TARGET,SOURCE,FSTYPE --mountpoint /data
sudo rm -rf /data.before-gideon-move /var/lib/gideon-data-move
cd /opt/gideon
gideon host provision
```

### Roll back before the start

Rollback is available from the stop until the owning application starts on the moved data.
Before **Set aside**, `/data` is still the original directory.
The owning operator can restart after confirming it is untouched.
Once the application starts on the moved data, new writes exist only on the volume.
Moving back would require its operator to stop it and perform and verify a reverse `rsync`, outside this rollback.

After **Set aside**, keep the application down.
If `/data` is mounted, unmount it.
If provision has not yet mounted it, confirm it is not a mount point.
Skip only `umount` in that case.
In `/etc/fstab`, manually remove the complete managed block if present.
The block comprises `# GIDEON BEGIN provision:disk-layout`, the UUID line between, and `# GIDEON END provision:disk-layout`.
Provision will write the block again on the next successful move.
Leaving it in place would mount the volume over the restored directory at reboot:

```sh
sudo umount /data                            # only when mounted
sudo findmnt --mountpoint /data             # expect no mount and status 1
sudoedit /etc/fstab
sudo sed -n '/# GIDEON BEGIN provision:disk-layout/,/# GIDEON END provision:disk-layout/p' /etc/fstab
sudo rmdir /data                            # only when the empty mount-point directory exists
sudo mv -T /data.before-gideon-move /data
sudo touch /var/lib/gideon-data-move/rollback.done
```

The `sed` command must print nothing before restoring `/data`.
The owning operator then starts their application on the restored directory.
Keep the rolled-back record for review.
Archive it under a new name before a fresh attempt so its `copy.started` stamp cannot be mistaken for the new move's state.
Provision will report the occupied-`/data` refusal again until the move is completed.
Any copies already on the volume remain there and will appear as shared names at the next attempt's inventory.
Reconcile them before copying.

## 9. Upgrading a shared prerequisite

This procedure upgrades a present Docker Engine, Compose plugin, NVIDIA container toolkit, or driver.
Use it when provision or preflight reports one below the lock's floor or with closed kernel modules, or an apt rehearsal proposes upgrades or removals.

Read `host.lock` for the required floor and driver branch.
Provision installs absent packages but does not upgrade a present prerequisite.
A driver at or above the branch with open kernel modules is accepted whatever its packaging.

A `reboot-required` row naming a loaded NVIDIA driver that differs from the installed module means the driver was upgraded without its reboot.
Reboot in an announced window.
Then re-run provision.

### Survey

Refresh apt's lists and rehearse the appropriate install before changing packages.
Read every `Inst` and `Remv` line, including dependencies and other packages on the box.
If apt cannot run, fix its refusal before proceeding.

```sh
sudo apt-get update -q
sudo apt-get -s install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo apt-get -s install -y nvidia-container-toolkit
sudo apt-get -s install -y nvidia-driver-pinning-<branch> nvidia-open
```

Run only the rehearsal for the prerequisite being upgraded.
For a driver upgrade, replace `<branch>` with `host.driver.branch` from `host.lock`.
Resolve any other proposed change with its owner before the window.

### Announce

Announce a maintenance window at least one working day ahead to users and every project sharing the daemon.
A Docker upgrade restarts every container on the box.
A driver upgrade needs a reboot.
Agree on the timing with the other projects' operators before starting.
When `host provision`'s co-tenant guard names running containers, use that list to announce the window to their projects.
`--acknowledge-disruption` acknowledges the disruption on the re-run but does not announce the window.

### Upgrade

During the window, choose the applicable sequence below.
Stop if any command fails.
Do not continue to provision until the package change succeeds.
Upgrade Docker's five packages together:

```sh
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Upgrade the toolkit as one package:

```sh
sudo apt-get install -y nvidia-container-toolkit
```

For a driver, release the hold.
Install the branch pinning package before the driver.
Restore the hold.
Reboot:

```sh
sudo apt-mark unhold nvidia-open
sudo apt-get install -y nvidia-driver-pinning-<branch>
sudo apt-get install -y nvidia-open
sudo apt-mark hold nvidia-open
sudo reboot
```

After a driver reboot, re-run provision (Converge, next).
Its toolkit step regenerates the CDI specification when it no longer matches a fresh `nvidia-ctk cdi generate`.

### Converge

From the release checkout, re-run provision:

```sh
cd /opt/gideon
gideon host provision
```

After a driver or Docker change, verify the engine once services are running again:

```sh
gideon engine verify
```

A merged `host.minimums.*` or `host.driver.branch` bump makes provision and preflight refuse until this procedure is followed (`docs/runbooks/release-files.md §9`).
Provision does not upgrade a present prerequisite itself.

## 10. Changing a moved box-wide setting

This procedure changes a moved box-wide setting or Docker address pool that GIDEON will not change.
Use it when provision, preflight, or `upgrade`'s provision stage reports one that falls short.

The row names the setting or key, the value found, and the value needed.
Agree the value with the box's other operators.
Make the applicable change below.
Then re-run provision from the release checkout.

### Time zone

If the host's zone should match `office.timezone` in `/etc/gideon/site.yaml`, change it with `sudo timedatectl set-timezone <zone>`.
If the host's zone is right, correct `office.timezone` instead.
Do this in an announced window.
Every timer on the box follows the zone, and a persistent timer can catch up once after the change.

### SSH login rule

Keep a working SSH session open.
Read the effective `passwordauthentication` and `kbdinteractiveauthentication` values with `sudo sshd -T`.
Use `sudo grep -riE '^[[:space:]]*(PasswordAuthentication|KbdInteractiveAuthentication)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/` to find the file setting either value ahead of `/etc/ssh/sshd_config.d/00-gideon-key-only.conf`.
Agree the change with the other operator, since a password login may be theirs.
Remove or correct the conflicting line.
Run `sudo systemctl reload ssh`.
Confirm both effective values are `no` while keeping the session open.

### Firewall default policy

Agree an announced window because this changes who can reach the box.
Set the incoming default with `sudo ufw default deny incoming`.
Then read `sudo ufw status verbose` to confirm the active incoming policy is `deny`.

### Journald storage

Set `Storage=persistent` in the file the refusal names.
Alternatively, when `Storage=auto` or unset, create `/var/log/journal` with `sudo mkdir -p /var/log/journal`.
Run `sudo systemctl restart systemd-journald`.
Read `sudo systemd-analyze cat-config systemd/journald.conf` to confirm the effective value.
Containers keep running.
The journal pauses briefly, so this change needs no maintenance window.

### Apt periodic triggers

Use `sudo grep -r 'Unattended-Upgrade\|Update-Package-Lists' /etc/apt/apt.conf.d/` to find the file setting the refused key to `"0"`.
Set that key to `"1"` there.
Read `apt-config dump APT::Periodic` to confirm both triggers are on.
No maintenance window is needed.

### Docker address pool

If the refusal names `default-address-pools` with another value, agree one pool with the box's other operators.
Docker allocates across every entry in that list, so a second pool defeats the office's choice.
Use one IPv4 range of /20 or wider, clear of every office network, for Docker to carve into /24 networks.
Set `docker_address_pool` in `/etc/gideon/site.yaml` to that range.
Remove the other entries from `/etc/docker/daemon.json` by hand, or have the other operator adopt GIDEON's range.
Run `sudo systemctl restart docker` in an announced window because the restart reaches every container on the box.
Then re-run provision from the release checkout, which reads the single entry as its own.

### Containerd root

Work in an announced window because restarting containerd and Docker reaches every container.
Do not edit `root` first.

Find the root file.
The row's root is the one containerd runs with.
Run `sudo containerd --config /etc/containerd/config.toml config dump | grep -E '^(root|imports)'` to see it and the files `config.toml` imports.
The root file is `/etc/containerd/config.toml` when the row names it.
When the row names a file `config.toml` imports, `sudo grep -l '^root' /etc/containerd/conf.d/*.toml` finds it.
Every step below that names the root file means that one file.

Measure the store under the named root with `sudo du -sb <named-root>`.
Check its snapshotter `snapshots` directory.

If the store is populated, follow `docs/runbooks/install-upgrade.md §7` by hand, substituting the named root for `/var/lib/containerd` throughout:

- At the survey, also save the root file beside the shipped copy with `sudo cp -p <root-file> /var/lib/gideon-store-move/root-file.saved`, before any change.
  When the root file is `config.toml`, the survey's own copy is that save.
- At the provision stage, set `root = '/var/lib/docker/containerd'` in the root file by hand before `gideon host provision --only docker-engine` under the same `policy-rc.d` hold.
  Provision does not rewrite a moved file.
- A rollback restores the root file from `/var/lib/gideon-store-move/root-file.saved` as well as `config.toml`.
  Continue only when `containerd config dump` reports the named root, not `/var/lib/containerd`.
- Finish the check, accept, and remove stages over the named root.

If the store is empty, run `sudo cp -p <root-file> <backup-path>`.
Set `root = '/var/lib/docker/containerd'` in the root file.
Then run `sudo systemctl restart containerd`.
Run `sudo systemctl restart docker`.
To undo it, copy the backup back.
Run the same two restarts.

### /data mount

The `disk-layout` row names the `/data` mount's filesystem type and size, and the selected profile's data-volume floor.
GIDEON does not change an existing mount.
A first run with a `/data` mount GIDEON did not build is `blocked` until `/etc/gideon/site.yaml` names the hardware profile whose floor judges it.

Agree a change with the mount's owner.
That operator grows the volume or moves its data to a volume meeting the floor.
Alternatively, the office sets `hardware_profile` in `/etc/gideon/site.yaml` to a profile this host satisfies.
A filesystem of another type is reformatted only by its owner, after moving its data off, in an announced window.
Then run provision from the release checkout.

From the release checkout, converge after the hand change:

```sh
cd /opt/gideon
gideon host provision
```

## 11. Removing GIDEON

This procedure takes GIDEON off a box.
Use it after the office decides to remove GIDEON.

`gideon uninstall` removes the host state that carries GIDEON's ownership mark and reverts no setting another application may rely on.
It runs unattended and asks nothing.
Every stage reads before it acts.

### Announce

Removal stops the whole stack, as an upgrade does.
Announce it to every project sharing the daemon.
Once users are on the box, use a maintenance window announced to them at least one working day ahead.
A container on one of GIDEON's networks loses that network.
Before anything stops, the run's `attached` row lists each container outside GIDEON's ownership mark, by network and project.
This includes an application using GIDEON's engine.
Tell their operators before the window.

### Choose plain or `--purge`

A plain run removes GIDEON's Compose projects: the stack, the drill, and the CI sibling.
It also removes its `gideon-*` units and timers, and its firewall chain with its tagged jump from `DOCKER-USER`.
Its block in `/etc/ufw/after.rules` goes too, with the `gideon-docker-user` unit and the installed `gideon` command.
Its Docker networks are removed once nothing is attached.
The run keeps `/etc/gideon`, GIDEON's directories under `/data`, the stack's volumes, and the drop-in files provision wrote.
A later install finds the configuration and data where they were.

`--purge` also removes `/etc/gideon` whole.
That includes the site file, the secrets, the rendered tree, and the box's backup identity.
It removes each directory `disk-layout` makes under `/data` and the stack's volumes.
It also removes the four drop-ins that hold a GIDEON fact.
These are the apt proxy file, the chrony sources file, the network wait-online drop-in, and the needrestart deferral file.
Chrony and systemd reload after their files are removed.
Apt and needrestart read their configuration at each run.

Both keep `/data` itself with its volume, mount, and `/etc/fstab` block, along with `/opt/gideon`, the `gideon` account, and every package.
Docker's `daemon.json`, containerd's configuration, and the image store stay too.
So does the firewall's default policy, with the tagged `ufw` rules that admit SSH under it.
The journald, SSH key-only, and automatic-upgrades drop-ins also stay, since each holds a setting the box keeps.

On the build box the registry and the CI runner are kept by their own installed state.
This includes the registry's unit with `/data/registry`, and the runner's sudoers file with `/data/acceptance`.
They stay under `--purge` too and on every later run.

A plain removal keeps `/etc/gideon/backup_age_identity`, so the box's own backup sets still open with it.
After `--purge`, the sets pushed to the backup target open with the office identity kept in the office password manager.

### Run

From any directory:

```sh
gideon uninstall
```

Alternatively, run `gideon uninstall --purge`.
The run removes the installed command.
Every fix it prints, and every re-run after it, is therefore the long form from the checkout:

```sh
cd /opt/gideon
sudo python3 -m gideon uninstall
```

Use `--purge` when the first run had it.
A re-run finds nothing left to remove, prints every row `ok`, and exits 0.
Keep the transcript.
GIDEON's database goes with the stack, so the rows are the record of the removal.

### A network still held

The last row, `networks`, fails when a container is still attached to one of GIDEON's networks, and names it.
Everything else is removed by then.
Tell that container's operator.
Wait until it has left the network.
Then re-run from the checkout as above.
The re-run removes the network.

### What stays

The closing `Kept:` line names what the command leaves.
Remove the checkout with `sudo rm -rf /opt/gideon` once nothing more is needed from it.
The `gideon` account, the packages GIDEON installed, Docker's `daemon.json` keys, and containerd's configuration are shared with whatever else runs on the box.
Change or remove them only after agreeing it with the box's other operators.
Unmounting `/data` and removing its `/etc/fstab` block is the data volume owner's decision, in an announced window.

## 12. A box that loses memory or a card

This section explains what to do when a GPU host falls below its selected profile's hardware floor.
Use it when `gideon preflight` refuses on `hardware-profile`, or a model server pages after a card loss.

The shortfalls are fewer cards than the profile's count or less DRAM than its floor.
`gideon install` and `gideon upgrade <tag>` run preflight first.
An upgrade's refusal here is before its checkout, so nothing of the product has changed (§2).
The row prints one fix: `Provide the hardware the profile requires, or set hardware_profile in /etc/gideon/site.yaml to a profile this host satisfies, then re-run preflight.`
`gideon apply` runs no preflight.
It reads the memory table and the GPU record, so a site-file edit still converges after a lost DIMM.
Between upgrades two page rules compare the host with the profile every minute: `Memory under profile` and `GPU count under profile` (`docs/runbooks/observability.md §4`).
Each page names the reading and the floor.
`gideon status` lists them.
On a no-GPU host the row passes as skipped.

GIDEON changes no hardware and selects no profile by itself.
Confirm the loss with the reads below.
Agree the fix with the people under *Who decides*.
Then converge from the release checkout.

### A lost DIMM

The shortfall reads `hardware-profile: refuse — DRAM is <kB> kB (<GB> GB), profile requires <floor> GB`.
For a non-reference profile, `profile '<name>': ` precedes the shortfall after the dash.
Every shortfall is joined in one row with `; `, followed by `Fix:` and the fix above.

Everything keeps running from the reboot that shows the loss.
`Memory under profile` fires at the first evaluation after that reboot, since it has no pending period.
The stack's limits are still the profile's memory table: ceilings sized for the floor, whose sum may now pass the host.
The `host-memory` row judges a fifth of whatever the host now reads, so the loss lowers that floor rather than tripping it.
The row warns and does not refuse.
No limit shrinks by itself.
A store growing toward its ceiling with less host memory behind it risks the kernel's memory killer.

Read these as root:

- `MemTotal` in `/proc/meminfo`, for the DRAM the host now sees.
- `sudo dmidecode -t memory`, for the populated slots: a slot reading no module, or fewer populated slots than the box had.
- The `ce_count` and `ue_count` counters per controller under `/sys/devices/system/edac/mc/`, where the host exposes them.
  Also read `sudo journalctl -k -b`, the current boot's kernel log, for EDAC or machine-check lines.
- The server's management log, where a failed or disabled DIMM is logged at boot.
  On an HPE box, read the Integrated Management Log at the management interface.

### A lost card

The shortfall reads `hardware-profile: refuse — GPU count is <n>, profile requires <count>`, with the same non-reference prefix and fix as above.

The model server bound to the card by UUID stops.
`Engine down` pages for the generator's card after five minutes.
`Target down` pages for the embedder's after ten minutes.
`GPU count under profile` fires at the first evaluation that reads fewer cards while the DCGM exporter stays scrapeable (`docs/runbooks/observability.md §4`).
While the exporter itself is down, `Target down` is the page and the count rule is quiet.
The rest of the stack serves.
With the generator's card gone, General is listed and a turn does not answer.
With the embedder's card gone, work that embeds through it stops.
This includes an index build in progress and, from the Research launch, each query's embedding.

Every render refuses on the GPU record: `the GPU record /etc/gideon/gpus.yaml names card(s) nvidia-smi -L no longer reports: GPU index <n> UUID <uuid>`.
Its fix reads `Remove /etc/gideon/gpus.yaml as root, then run gideon render --diff to record today's cards and inspect the diff.`
`gideon apply` is refused there.
`gideon upgrade <tag>` refuses at preflight before reaching render.
Convergence stays refused until the card is back or a profile naming fewer cards is selected and the record is re-recorded.
Removing the record alone (§1's GPU record paragraph) does not help on a profile naming more cards than the host has.
Render then refuses on the position the profile assigns beyond the recorded count.

Read `nvidia-smi -L` against `/etc/gideon/gpus.yaml`: the missing UUID identifies the lost card's recorded position.
Read `nvidia-smi -q` for the card that remains.
Read `sudo journalctl -k -b` for the driver's Xid lines.
Read the management log.

### The repair, or a lower profile

The hardware owner replaces the DIMM or card in a maintenance window.
Announce the window to every project sharing the box.
Once users are on it, announce to them at least one working day ahead too.
Then converge below.

When the release's `models.lock` carries a profile with a lower floor, set it as `hardware_profile` in `/etc/gideon/site.yaml`.
Then converge below.
`docs/runbooks/release-files.md §4` says what a profile sizes and how each value moves.
The apply recreates every service whose limit moved, so this takes a window from go-live.
The costs are a smaller memory table and a lower chunk ceiling for the index.
A one-card profile puts both model servers on that card with a smaller cache pool, so fewer concurrent users.
A profile the release does not carry is not an option on the box.

After a card, both paths require the GPU record to be removed and re-recorded first, as §1's GPU record paragraph says.
A replacement carries a new UUID.
A profile naming fewer cards still meets the old record naming the lost card, which every render refuses whatever profile is selected.
Re-recording the cards the host now has binds the profile's positions.
`gideon render --diff` shows what moves before the apply.

### Who decides

The office's hardware owner decides the repair.
The CSA decides a profile change, with the developer reading the rung's costs against the office's corpus and users.
Before go-live, the developer alone decides.

After a card, the record's removal and `gideon render --diff` (§1) come before the block below, on both paths.
Provision converges the host facts a changed profile judges, the data-volume floor among them.
Apply converges the memory table and GPU bindings.
Preflight reads the row pass.
A non-reference profile warns instead, as its row says.

From the release checkout, converge after the hand change:

```sh
cd /opt/gideon
gideon host provision
gideon apply
gideon preflight
```

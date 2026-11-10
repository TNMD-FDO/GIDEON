# Command reference

This reference is for developers and computer systems administrators (CSAs) looking up one command. Each entry gives its purpose, who runs it and when, its command line, and its steps. Running `gideon` alone prints where to start.

## 1. The gideon command

Provisioning prepares the host and installs the `gideon` command through its `gideon-command` step. The lines below use the installed form, `gideon <command>`, which runs as root from `/opt/gideon`. Before that step, use `sudo python3 -m gideon <command>` from `/opt/gideon`. The installed `gideon` always runs the release in `/opt/gideon`. To run another checkout's tree, such as a model upgrade's proposal checkout, use `sudo python3 -B -m gideon <command>` from that checkout. The `preflight.sh`, `install.sh`, and `upgrade.sh` scripts are thin entrypoints over the same CLI. Applying brings the running stack into agreement with the site file and release. The build box is the maintaining office's server. The CI sibling, `gideon-ci`, is a separate project using production's engine through a relay.

### gideon preflight

**What it does.** Rechecks provisioning and checks the directory, relay, backup target, certificates, ports, and disk before installation.\
**Who runs it, and when.** You run it before installation as the CSA; install runs the same checks first.\
**Command.** `gideon preflight`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon host provision

**What it does.** Prepares the host, with `--no-gpu` declaring a host without a GPU and `--build-box` declaring the build box.\
**Who runs it, and when.** You run it as the CSA before writing the site file and again afterwards. Reboot after a `reboot-required` row, then repeat until every step reads `ok`.\
**Command.** `gideon host provision [--no-gpu | --build-box]`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon render

**What it does.** Renders the release's configuration, with `--diff` showing the changes before you apply them.\
**Who runs it, and when.** You run it as the CSA or developer when reviewing configuration changes. It needs root when run directly from a checkout.\
**Command.** `gideon render [--diff]`.\
**Steps.** [docs/runbooks/model-upgrade.md §6](runbooks/model-upgrade.md).

### gideon apply

**What it does.** Converges the stack, generates absent secrets, fetches the profile's models, and verifies the services.\
**Who runs it, and when.** You run it as the CSA after editing `/etc/gideon/site.yaml`. It needs root and refuses while an evaluation holds the engine lock.\
**Command.** `gideon apply`.\
**Steps.** [docs/runbooks/install-upgrade.md §4](runbooks/install-upgrade.md).

### gideon install

**What it does.** Runs preflight, apply, user reconciliation, engine verification, backup, and the restore drill, ending with the site's URL.\
**Who runs it, and when.** You run it as the CSA after preparing the host, site file, and supplied secrets. A repeat run takes an incremental backup and runs another drill.\
**Command.** `gideon install`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon upgrade <tag>

**What it does.** Takes a mandatory full backup before checking out the release and running the new tree's commands.\
**Who runs it, and when.** You run it as the CSA against a clean install checkout. Git runs as the checkout's owner.\
**Command.** `gideon upgrade <tag>`.\
**Steps.** [docs/runbooks/install-upgrade.md §2](runbooks/install-upgrade.md).

### gideon upgrade --rollback

**What it does.** Restores the pre-upgrade backup and applies the previous release.\
**Who runs it, and when.** You run it as the CSA when returning the data and product to their saved state.\
**Command.** `gideon upgrade --rollback [<tag>]`.\
**Steps.** [docs/runbooks/install-upgrade.md §3](runbooks/install-upgrade.md).

### gideon uninstall

**What it does.** Removes GIDEON's marked host state, keeping configuration and data unless you pass `--purge`.\
**Who runs it, and when.** You run it as the CSA when removing GIDEON, after announcing the maintenance window. Both forms keep shared settings and prerequisites.\
**Command.** `gideon uninstall [--purge]`.\
**Steps.** [docs/runbooks/install-upgrade.md §11](runbooks/install-upgrade.md).

### gideon status

**What it does.** Reads firing pages, office proposals, services, backups, TLS, and `/data`, adding the CI sibling's services, newest evaluation, and fired product proposals on the build box.\
**Who runs it, and when.** You run it as the CSA when an alert arrives or something looks wrong. It needs root when run directly from a checkout.\
**Command.** `gideon status`.\
**Steps.** [docs/runbooks/observability.md §4](runbooks/observability.md).

### gideon alerts test

**What it does.** Sends one message through the page contact point and mail relay, recording an `alerts_test` row.\
**Who runs it, and when.** You run it as the CSA after installation or changes to the relay or `alerts.*` settings. It needs root and a converged stack.\
**Command.** `gideon alerts test`.\
**Steps.** [docs/runbooks/observability.md §4](runbooks/observability.md).

### gideon tls reload

**What it does.** Picks up the renewed certificate and key after you replace their files.\
**Who runs it, and when.** You run it as the CSA after completing the certificate renewal steps. It needs root when run directly from a checkout.\
**Command.** `gideon tls reload`.\
**Steps.** [docs/runbooks/office-services-setup.md §4](runbooks/office-services-setup.md).

### gideon secrets rotate <name>

**What it does.** Rotates a supported generated secret and converges its consumers.\
**Who runs it, and when.** You run it as the CSA with root access when a secret needs replacing. It takes the engine lock, except for `grafana_admin_password`, which recreates nothing.\
**Command.** `gideon secrets rotate <name>`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon users reconcile

**What it does.** Reports directory membership changes to frontend roles, applying them when given `--now`.\
**Who runs it, and when.** You run it as the CSA after directory membership changes; a timer runs it nightly. It needs root when run directly from a checkout.\
**Command.** `gideon users reconcile [--now]`.\
**Steps.** [docs/runbooks/office-services-setup.md §1](runbooks/office-services-setup.md).

### gideon retention sweep

**What it does.** Deletes chats idle past `retention.chats`, their named uploads, and feedback rows expired or left on swept chats through the frontend.\
**Who runs it, and when.** A timer runs it nightly at 02:00 office time. You need root and a converged stack to run it by hand.\
**Command.** `gideon retention sweep`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### gideon registry mirror

**What it does.** Pulls the release's images into the build box's loopback registry.\
**Who runs it, and when.** You run it as the maintaining office's CSA during installation or before building an image. It needs Docker access.\
**Command.** `gideon registry mirror`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon models pull [<candidate>]

**What it does.** Fetches and verifies the selected profile's model files, adding the named candidate's files when given a candidate.\
**Who runs it, and when.** You run it as the CSA to perform apply's model download separately. Candidates are for the build box.\
**Command.** `gideon models pull [<candidate>]`.\
**Steps.** [docs/runbooks/release-files.md §4](runbooks/release-files.md).

### gideon engine verify

**What it does.** Runs corpus-independent engine, frontend guardrail, and embedding checks, recording their results.\
**Who runs it, and when.** You run it as the CSA or developer after an engine or driver change; install and upgrade also run it. It refuses while an evaluation holds the engine lock.\
**Command.** `gideon engine verify`.\
**Steps.** [docs/runbooks/model-upgrade.md §7](runbooks/model-upgrade.md).

### gideon worker verify

**What it does.** Checks the worker on a converged stack.\
**Who runs it, and when.** You run it as the CSA with root access after apply has converged.\
**Command.** `gideon worker verify`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### gideon backup run

**What it does.** Creates a backup set, taking a full backup on the first run or with `--full`, and incremental backups between weekly full runs.\
**Who runs it, and when.** A timer runs it nightly at 01:00 office time. You also run a full backup after promoting a production index generation.\
**Command.** `gideon backup run [--full]`.\
**Steps.** [docs/runbooks/backup-restore.md §3](runbooks/backup-restore.md).

### gideon backup push

**What it does.** Copies staging to the backup target, checks the off-server files, and prunes old snapshots.\
**Who runs it, and when.** A timer runs it immediately after the nightly backup. You run it as the CSA to copy a backup off the server by hand.\
**Command.** `gideon backup push [--verify-all]`.\
**Steps.** [docs/runbooks/backup-restore.md §3](runbooks/backup-restore.md).

### gideon backup drill

**What it does.** Restores a backup into a throwaway project, checks the restored data and frontend, and tears the project down.\
**Who runs it, and when.** A timer runs it on the configured Saturday interval. You run it as the CSA to check a backup by hand; install also runs it.\
**Command.** `gideon backup drill`.\
**Steps.** [docs/runbooks/backup-restore.md §3](runbooks/backup-restore.md).

### gideon restore --from staging|target

**What it does.** Restores a local backup set or fetches and verifies a snapshot from the backup target before touching live state.\
**Who runs it, and when.** You run it as the CSA for recovery. Use `--at` for an instant, `--set` for a local label, or `--archive-end` for the archive's end.\
**Command.** `gideon restore --from staging|target [--at <ISO-time> | --set <label> | --archive-end]`.\
**Steps.** [docs/runbooks/backup-restore.md §4](runbooks/backup-restore.md); for a rebuilt server, [docs/runbooks/backup-restore.md §5](runbooks/backup-restore.md).

### gideon corpus cut

**What it does.** Writes and records a corpus lockfile pinning source files by path, digest, and size.\
**Who runs it, and when.** You run it as the maintainer when preparing a corpus release. As an office's CSA, use `--base` with `--add-courts` to add courts from existing snapshots.\
**Command.** `gideon corpus cut`; derived form: `gideon corpus cut --base <label> --add-courts <ids>`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md); for adding courts, [docs/runbooks/release-files.md §11](runbooks/release-files.md).

### gideon corpus watch

**What it does.** Reads fetched sources' indexes and captured sources' recorded bindings, reporting when a cut is due without fetching corpus files.\
**Who runs it, and when.** A weekly timer runs it on every server. You may also run it as the maintainer by hand.\
**Command.** `gideon corpus watch`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon corpus install <label>

**What it does.** Fetches, verifies, records, stages, and ingests the pinned corpus, measures agreement, applies known overrulings, and builds generations whose inputs changed.\
**Who runs it, and when.** You run it as the CSA after installation, with a passing smoke run over the sibling's sample before a production build. It marks the label installed, removes its work directory, and retains needed snapshots.\
**Command.** `gideon corpus install <label>`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon archive push

**What it does.** Copies and checks pinned source snapshots, corpus lockfiles, and cached model revisions in the maintaining office's source archive.\
**Who runs it, and when.** You run it as the maintaining office's CSA on the build box after a corpus cut or model pull. A timer also runs it weekly there.\
**Command.** `gideon archive push`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon index build caselaw

**What it does.** Clusters near-duplicate chunks, builds lexical indexes and vectors, materializes cited cases' risk states, and records the generation's build report.\
**Who runs it, and when.** You run it as the CSA for a direct build; production needs a passing smoke run over the sibling's sample first. Use `--sample` to build the evaluation sample on the `gideon-ci` sibling.\
**Command.** `gideon index build caselaw [--sample]`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon index report [<generation>]

**What it does.** Reads lockfiles, generation states, and per-court document counts by status and text source, plus anchor coverage, agreement, gates, and risk-state distribution.\
**Who runs it, and when.** You run it as the CSA after an install or build, including while a named generation is building. It needs root when run directly from a checkout.\
**Command.** `gideon index report [<generation>]`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon index promote <generation>

**What it does.** Runs the build gates as a recorded evaluation and serves the generation on a pass, retiring the previous generation for seven days.\
**Who runs it, and when.** You run it as the CSA when a built generation is ready to serve. Take a full backup after a production promotion.\
**Command.** `gideon index promote <generation>`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon index retire <generation>

**What it does.** Retires a building, built, or failed generation by name, leaving its stores for garbage collection.\
**Who runs it, and when.** You run it as the CSA when a generation is unwanted.\
**Command.** `gideon index retire <generation>`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon index gc

**What it does.** Drops retired generations' indexes and vectors immediately if unpromoted, otherwise after the seven-day hold, keeping their records.\
**Who runs it, and when.** You run it as the CSA after retirement; `--stack ci` selects the CI sibling.\
**Command.** `gideon index gc [--stack ci]`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon eval run --slice <name>

**What it does.** Scores an evaluation set, compares per-case verdicts with the committed reference, and records the run and results with the gate's verdict as its exit code.\
**Who runs it, and when.** You run it as the developer with the system Python. A proposal's run uses `sudo python3 -B -m gideon eval run …` from the proposal checkout. Engine-reaching runs take the engine lock and obey their time window.\
**Command.** `gideon eval run --slice <name> [--stack ci] [--decision --against <run-id>] [--challenger] [--force] [--set <dir>] [--kind <kind>]`.\
**Steps.** For a model decision, [docs/runbooks/model-upgrade.md §8](runbooks/model-upgrade.md).

The quiet window is weeknights 19:00 to 06:00 and Friday 19:00 to Monday 06:00 in the site's timezone. The eval identity is the account used for evaluation turns through the frontend.

`extraction` scores the set's labelled exact objects. `judgments --ranked <file>` scores ranked gold-evidence coordinates against attorneys' grades, reporting nDCG@10, recall@50, and Hole@10 without gating those measures.

`guardrails` runs three guardrail families through General's service, with six cases also through the frontend, in the quiet window. It gates each family and the harness's false-refusal counts at their ceilings. The judge's readings are reported without gating.

`general-smoke` runs General's smoke set through the frontend as the eval identity, twice, in the quiet window. It reports pass or fail without grading answers.

`smoke` runs twelve engine calls, the guardrails sample through the service and frontend, extraction whole, and the CI sample's gold-drop check at any hour. It fails on a zero-tolerance failure, a failed gold-drop check, or a regression against its reference.

`gold-drop --generation <name>` reads a generation's manifest for the set's gold documents. It fails on any dropped zero-tolerance case or more than 5% of gold dropped. Its run records the generation and corpus lockfile. Without a name, it reads production's live generation. With `--stack ci`, the default is the newest committed lockfile's sample generation on the selected hardware profile's line.

`count-gates --generation <name>` checks the newest build report against the committed lockfile's courts, failing by court. Every court must be staged, recorded counts must reach staged counts, processing must finish, and every failure must be named.

`--stack ci` selects the CI sibling for evaluations that drive turns, read the judge, or read stores. The judge still calls production's engine. `--kind` names the run's purpose on its record. `--set <dir>` loads a set outside the release and records nothing.

`--challenger --stack ci` runs the experiment in `config/challenger.yaml` beside the release configuration on the build box, recording paired runs. It prints `skipped` when no challenger is set.

`--decision --against <run-id>` runs guardrails five times in the weekend window and pairs each control's false refusal with the named run's. It records the mean difference, its 95% interval, and a verdict of wins, loses, or undecided. Too few paired clusters causes a refusal without an interval.

`--force` starts a run outside its window, and the run's record notes it. A held engine lock ordinarily refuses another engine-reaching run. At the window's end, an engine-reaching run aborts, keeping completed repeats and recording a partial run.

A zero-tolerance gate reports counted cases and the 3/n bound on its gate row and `bound:` line before `verdict`. The count stays on the run record; the bound is reported without gating. Recorded engine-reaching runs carry the engine's in-flight maximum and preemption count on the run and `record` rows. These readings are unknown when Prometheus is unreadable or engine scrapes do not cover the span.

At 21:00 office time, the nightly timer runs `general-smoke` then `guardrails` in one `--kind nightly` invocation. It waits once behind a held engine and stops each suite at 06:00. On the build box, the challenger's pair follows.

### gideon eval reference --run <id>

**What it does.** Takes one recorded run as a tagged release's reference, writing one content-free file per ID list.\
**Who runs it, and when.** You run it as the developer from a clean checkout at the release tag. A case that passed there and fails now blocks by ID; a mean does not gate.\
**Command.** `gideon eval reference --run <id>`.\
**Steps.** [eval/reference/README.md](../eval/reference/README.md#commands).

### gideon eval candidates --out <dir>

**What it does.** Writes the month's thumbs-down turns beside their IDs and buckets as a root-only page and a text-free manifest.\
**Who runs it, and when.** You run it as the CSA once a month, as root, into an empty directory outside backup sets and checkouts. Read the page with `less` and write candidate questions in your own words.\
**Command.** `gideon eval candidates --out <dir>`.\
**Steps.** [docs/runbooks/feedback-packet.md §1](runbooks/feedback-packet.md).

### gideon proposals

**What it does.** Reads waiting proposals, trigger measurements, feedback counts, guardrail trips, upstream notices, and the build box's challenger pairs.\
**Who runs it, and when.** You run it as the CSA when reviewing proposals; the feedback read needs root. The daily tally uses `--record`; an ordinary report writes nothing and exits 1 only when a section cannot be read.\
**Command.** `gideon proposals [--record]`.\
**Steps.** [docs/runbooks/release-files.md §7](runbooks/release-files.md); for testing the tally, [docs/runbooks/observability.md §7](runbooks/observability.md).

The commands `gideon host gpu`, `gideon registry gc`, and `gideon audit query` are not implemented. Each prints "not implemented" and exits 1.

## 2. The build-box tools

These tools run from a checkout on the build box or in a development virtual environment. They are not steps in a receiving office's install home. Each command line includes `sudo` where the tool needs it.

### The gate

**What it does.** Compares the environment with the tree's pins, then runs lint, type checking, and unit tests, stopping at the first failure.\
**Who runs it, and when.** You run it as the developer before a commit; CI runs it too. Use `--all` to include slow tests and `--masked` to hide the server's paths from tests.\
**Command.** `python3 -m tools.gate [--all] [--masked] [TEST_PATH ...]`.\
**Steps.** [CONTRIBUTING.md](../CONTRIBUTING.md).

### The image build

**What it does.** Builds, checks, pushes, and records a GIDEON image, with `--check` proving the recorded image against its lock.\
**Who runs it, and when.** You run it as the maintaining office's CSA on the build box after changing image inputs. The images are `postgres`, `gideon`, and `opensearch`.\
**Command.** `python3 -m tools.imagebuild <name> [--check]`.\
**Steps.** [docs/runbooks/built-images.md §2](runbooks/built-images.md).

### The clean-VM acceptance

**What it does.** Creates a throwaway virtual machine from the pinned image and drives the receiving-office sequence through it.\
**Who runs it, and when.** You run it as the maintaining office's CSA on the build box at minor releases. Its forms also cover full restore and applications sharing the host.\
**Command.** `sudo python3 -m tools.acceptance <ref> [--full-restore | --cotenant before|after | --pair before|after]`.\
**Steps.** [docs/runbooks/install-upgrade.md §6](runbooks/install-upgrade.md).

### The pin watch

**What it does.** Proposes pin bumps, prints a model's files block, and generates a release note's "What was bumped" section.\
**Who runs it, and when.** CI runs the watch; you use its dry run from a checkout and print model files or release-note text as the maintainer. Review its proposals each Monday.\
**Command.** `python3 -m tools.pinwatch --dry-run`.

`python3 -m tools.pinwatch.hub <repo>`.

`python3 -m tools.pinwatch.bumped [--since <tag>]`.

**Steps.** For model files, [docs/runbooks/model-upgrade.md §3](runbooks/model-upgrade.md); for reviewing proposals, [docs/runbooks/release-files.md §9](runbooks/release-files.md).

### The evidence redaction

**What it does.** Replaces office values declared by the site registry with placeholders named after their site keys.\
**Who runs it, and when.** You run it as the developer over a transcript on the server before saving evidence. The acceptance harness uses the same function for transcripts and stored messages.\
**Command.** `python3 -m tools.redact --site /etc/gideon/site.yaml < draft > asset`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The court map generator

**What it does.** Generates `courts.yaml` from CourtListener's bulk courts data and a reviewed hand table.\
**Who runs it, and when.** You run it as the maintainer on a development machine after reviewing court placements. Download the CSV separately; `--check` verifies the map without writing it.\
**Command.** `python3 -m tools.courtmap --csv <courts-YYYY-MM-DD.csv.bz2> [--check]`.\
**Steps.** [docs/runbooks/release-files.md §6](runbooks/release-files.md).

### The CI sibling stack

**What it does.** Operates `gideon-ci`, a separate project with its own stores, frontend, and service, using production's engine through a relay.\
**Who runs it, and when.** You run it as the developer on the build box; CI uses its smoke command. Its frontend is on loopback port 18100, and its state is outside backup sets.\
**Command.** `sudo python3 -m tools.cistack up | smoke | status | down [--wipe]`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The turn harness

**What it does.** Runs cases through the frontend, General's service with `--service`, the engine with `--unfiltered`, or a browser with `--browser`, printing each case's class.\
**Who runs it, and when.** You run long sets as the developer in the quiet window or on weekends, using `--beside` only for deliberate overlap. It takes the engine lock; `--stack ci` selects the sibling, and browser mode needs a users-group seat.\
**Command.** `sudo python3 -m tools.turns <cases> [--case <id>] [--stream] --out <dir> [--stack ci]`.

`sudo python3 -m tools.turns --service <cases> [--stream] --out <dir> [--stack ci]`.

`sudo python3 -m tools.turns --unfiltered <cases> --case <id> --out <dir>`.

`sudo .venv/bin/python -m tools.turns --browser <cases> --out <dir>`.

**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The board check

**What it does.** Opens Grafana boards as the break-glass administrator, reporting each panel's state and saving a screenshot of each board.\
**Who runs it, and when.** You run it as the developer on the server to inspect rendered boards. Read the screenshots without committing them; the check writes nothing to Grafana.\
**Command.** `sudo .venv/bin/python -m tools.boards [<uid> ...] --out <dir> [--width <px>] [--trust-ca]`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The variants kit

**What it does.** Checks committed variants against parent cases, prints missing pairs with `--sheet`, or appends reviewed variants with `--write`.\
**Who runs it, and when.** You run it as the developer when checking or extending evaluation cases. A write requires the complete series manifest, reviewer ID, and date.\
**Command.** `python3 -m tools.variants <parents> <variants> [--sheet | --write --reviewer <id> --on <date>]`.\
**Steps.** [eval/sets/eval-v1/build-gates/README.md](../eval/sets/eval-v1/build-gates/README.md#the-variants).

### The treatment fetch

**What it does.** Fetches the pinned treatment dataset files, verifying each file's size and digest before giving it its final name.\
**Who runs it, and when.** You run it as the developer before measuring treatment patterns. Already verified files are left in place.\
**Command.** `python3 -m tools.treatment fetch --to <dir>`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

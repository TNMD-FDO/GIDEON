# Command reference

This reference holds an entry for each `gideon` command and each build-box tool, explaining what it does and why, who runs it and when, and giving its command line and the runbook section with its steps. Running `gideon` alone prints where to start.

## 1. The gideon command

Provisioning prepares the host and installs the `gideon` command through its `gideon-command` step, and the lines below use that installed form, `gideon <command>`, which runs as root from `/opt/gideon`. Before that step, the same command runs as `sudo python3 -m gideon <command>` from `/opt/gideon`. The installed `gideon` always runs the release in `/opt/gideon`, so another checkout's tree, such as a model upgrade's proposal checkout, runs as `sudo python3 -B -m gideon <command>` from that checkout. The `preflight.sh`, `install.sh`, and `upgrade.sh` scripts are thin entrypoints over the same CLI.

Applying brings the running stack into agreement with the site file and the release. The build box is the maintaining office's server, and the CI sibling, `gideon-ci`, is a separate project that uses production's engine through a relay.

### gideon preflight

Preflight rechecks provisioning and checks the directory, relay, backup target, certificates, ports, and disk before installation. A computer systems administrator (CSA) runs it before installation; install runs the same checks first.

**Command.** `gideon preflight`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon host provision

Host provision prepares the host. `--build-box` declares the build box, and `--no-gpu` declares a host without a GPU, a declaration every command after provision reads, so the driver and toolkit steps are skipped. A CSA runs provision before writing the site file and again afterwards; reboot after a `reboot-required` row, then repeat until every step reads `ok`.

**Command.** `gideon host provision [--no-gpu | --build-box]`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon render

Render produces the release's configuration, with `--diff` showing the changes before they are applied. A CSA or developer runs it when reviewing configuration changes; it needs root when run directly from a checkout.

**Command.** `gideon render [--diff]`.\
**Steps.** [docs/runbooks/model-upgrade.md §6](runbooks/model-upgrade.md).

### gideon apply

Apply brings the running stack into agreement with the site file and the release: it converges the stack, generates absent secrets, fetches the profile's models, and verifies the services. A CSA runs it after editing `/etc/gideon/site.yaml`. It needs root, and it refuses while an evaluation holds the engine lock.

**Command.** `gideon apply`.\
**Steps.** [docs/runbooks/install-upgrade.md §4](runbooks/install-upgrade.md).

### gideon install

Install runs preflight, apply, user reconciliation, engine verification, backup, and the restore drill, ending with the site's URL. A CSA runs it after preparing the host, site file, and supplied secrets; a repeat run takes an incremental backup and runs another drill.

**Command.** `gideon install`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon upgrade <tag>

Upgrade takes a mandatory full backup before checking out the release and running the new tree's commands. A CSA runs it against a clean install checkout; git runs as the checkout's owner.

**Command.** `gideon upgrade <tag>`.\
**Steps.** [docs/runbooks/install-upgrade.md §2](runbooks/install-upgrade.md).

### gideon upgrade --rollback

Rollback restores the pre-upgrade backup and applies the previous release. A CSA runs it to return the data and product to their saved state.

**Command.** `gideon upgrade --rollback [<tag>]`.\
**Steps.** [docs/runbooks/install-upgrade.md §3](runbooks/install-upgrade.md).

### gideon uninstall

Uninstall removes GIDEON's marked host state, keeping configuration and data unless `--purge` is passed. A CSA runs it when removing GIDEON, after announcing the maintenance window; both forms keep shared settings and prerequisites.

**Command.** `gideon uninstall [--purge]`.\
**Steps.** [docs/runbooks/install-upgrade.md §11](runbooks/install-upgrade.md).

### gideon status

Status reads firing pages, office proposals, services, backups, TLS, and `/data` without writing anything. On the build box, it adds the CI sibling's services, newest evaluation, and fired product proposals. A CSA runs it when an alert arrives or something looks wrong; it needs root when run directly from a checkout.

**Command.** `gideon status`.\
**Steps.** [docs/runbooks/observability.md §4](runbooks/observability.md).

### gideon alerts test

Alerts test sends one message through the page contact point and mail relay, recording an `alerts_test` row. A CSA runs it after installation or changes to the relay or `alerts.*` settings; it needs root and a converged stack.

**Command.** `gideon alerts test`.\
**Steps.** [docs/runbooks/observability.md §4](runbooks/observability.md).

### gideon tls reload

TLS reload picks up the renewed certificate and key after their files are replaced. A CSA runs it after completing the certificate renewal steps; it needs root when run directly from a checkout.

**Command.** `gideon tls reload`.\
**Steps.** [docs/runbooks/office-services-setup.md §4](runbooks/office-services-setup.md).

### gideon secrets rotate <name>

The command rotates a supported generated secret and converges its consumers. A CSA runs it with root access when a secret needs replacing. It takes the engine lock, except for `grafana_admin_password`, which recreates nothing.

**Command.** `gideon secrets rotate <name>`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon users reconcile

Users reconcile reports directory membership changes to frontend roles, applying them when given `--now`. A CSA runs it after directory membership changes; a timer runs it nightly. It needs root when run directly from a checkout.

**Command.** `gideon users reconcile [--now]`.\
**Steps.** [docs/runbooks/office-services-setup.md §1](runbooks/office-services-setup.md).

### gideon retention sweep

Retention sweep deletes chats idle past `retention.chats`, their named uploads, and feedback rows expired or left on swept chats through the frontend. A timer runs it nightly at 02:00 office time; running it by hand needs root and a converged stack.

**Command.** `gideon retention sweep`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### gideon registry mirror

Registry mirror pulls the release's images into the build box's loopback registry. The maintaining office's CSA runs it during installation or before building an image; it needs Docker access.

**Command.** `gideon registry mirror`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon models pull [<candidate>]

Models pull fetches and verifies the selected profile's model files, adding the named candidate's files when given a candidate. A CSA runs it to perform apply's model download separately; candidates are for the build box.

**Command.** `gideon models pull [<candidate>]`.\
**Steps.** [docs/runbooks/release-files.md §4](runbooks/release-files.md).

### gideon engine verify

Engine verify runs corpus-independent engine, frontend guardrail, and embedding checks, recording their results. A CSA or developer runs it after an engine or driver change; install and upgrade also run it. It refuses while an evaluation holds the engine lock.

**Command.** `gideon engine verify`.\
**Steps.** [docs/runbooks/model-upgrade.md §7](runbooks/model-upgrade.md).

### gideon worker verify

Worker verify checks the worker on a converged stack. A CSA runs it with root access after apply has converged.

**Command.** `gideon worker verify`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### gideon backup run

Backup run creates a backup set, full on the first run or with `--full` and incremental between weekly full runs; a timer runs it nightly at 01:00 office time. A full run also snapshots the index stores, so take a full backup after promoting a production index generation.

**Command.** `gideon backup run [--full]`.\
**Steps.** [docs/runbooks/backup-restore.md §3](runbooks/backup-restore.md).

### gideon backup push

Backup push makes the off-server copy of a backup: it copies staging to the backup target, checks the off-server files, and prunes old snapshots. A timer runs it immediately after the nightly backup; a CSA also runs it by hand.

**Command.** `gideon backup push [--verify-all]`.\
**Steps.** [docs/runbooks/backup-restore.md §3](runbooks/backup-restore.md).

### gideon backup drill

Backup drill restores a backup into a throwaway project to prove backups work, checking the restored data and frontend before tearing the project down. A timer runs it on the configured Saturday interval; a CSA also runs it to check a backup by hand, and install runs it too.

**Command.** `gideon backup drill`.\
**Steps.** [docs/runbooks/backup-restore.md §3](runbooks/backup-restore.md).

### gideon restore --from staging|target

The restore command restores a local backup set or fetches and verifies a snapshot from the backup target before touching live state; a CSA runs it for recovery. Use `--at` for an instant, `--set` for a local label, or `--archive-end` for the archive's end.

**Command.** `gideon restore --from staging|target [--at <ISO-time> | --set <label> | --archive-end]`.\
**Steps.** [docs/runbooks/backup-restore.md §4](runbooks/backup-restore.md); for a rebuilt server, [docs/runbooks/backup-restore.md §5](runbooks/backup-restore.md).

### gideon corpus cut

Corpus cut writes and records a corpus lockfile pinning source files by path, digest, and size so a release installs those pinned bytes. A maintainer runs it when preparing a corpus release; an office's CSA uses `--base` with `--add-courts` to add courts from existing snapshots.

**Command.** `gideon corpus cut`; derived form: `gideon corpus cut --base <label> --add-courts <ids>`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md); for adding courts, [docs/runbooks/release-files.md §11](runbooks/release-files.md).

### gideon corpus watch

Corpus watch reads fetched sources' indexes and captured sources' recorded bindings to report when a cut is due without fetching corpus files. A weekly timer runs it on every server; a maintainer may also run it by hand.

**Command.** `gideon corpus watch`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon corpus install <label>

Corpus install fetches, verifies, records, stages, and ingests the pinned corpus, measures agreement, applies known overrulings, and builds generations whose inputs changed. Once that work succeeds, it marks the label installed, removes its work directory, and retains needed snapshots. A CSA runs it after installation, with a passing smoke run over the sibling's sample before a production build.

**Command.** `gideon corpus install <label>`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon archive push

Archive push copies and checks pinned source snapshots, corpus lockfiles, and cached model revisions in the maintaining office's source archive so the files can be republished if an upstream disappears. The maintaining office's CSA runs it on the build box after a corpus cut or model pull; a timer also runs it weekly there.

**Command.** `gideon archive push`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon index build caselaw

Index build runs corpus install's indexing work directly: it clusters near-duplicate chunks, builds lexical indexes and vectors, materializes cited cases' risk states, and records the generation's build report. A CSA runs it; production needs a passing smoke run over the sibling's sample first. Use `--sample` to build the evaluation sample on the `gideon-ci` sibling.

**Command.** `gideon index build caselaw [--sample]`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon index report [<generation>]

Index report reads the record without writing anything: lockfiles, generation states, and per-court document counts by status and text source, plus anchor coverage, agreement, gates, and risk-state distribution. A CSA runs it after an install or build, including while a named generation is building; it needs root when run directly from a checkout.

**Command.** `gideon index report [<generation>]`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon index promote <generation>

Index promote runs the build gates as a recorded evaluation and serves the generation on a pass, retiring the previous generation for seven days so it can answer turns that read it before the change. A CSA runs it when a built generation is ready to serve; take a full backup after a production promotion.

**Command.** `gideon index promote <generation>`.\
**Steps.** [docs/runbooks/install-upgrade.md §1](runbooks/install-upgrade.md).

### gideon index retire <generation>

Index retire marks a building, built, or failed generation as retired by name, leaving its stores for garbage collection; a CSA runs it when the generation is unwanted.

**Command.** `gideon index retire <generation>`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon index gc

Index gc keeps retired generations' records while dropping their indexes and vectors, immediately if unpromoted or after the seven-day hold if previously promoted: the hold lets a generation answer turns that read it before retirement. A CSA runs it after retirement; `--stack ci` selects the CI sibling.

**Command.** `gideon index gc [--stack ci]`.\
**Steps.** [docs/runbooks/release-files.md §10](runbooks/release-files.md).

### gideon eval run --slice <name>

Eval run scores an evaluation set, compares per-case verdicts with the committed reference, and records the run and results with the gate's verdict as its exit code. A developer runs it with the system Python; a proposal's run uses `sudo python3 -B -m gideon eval run …` from the proposal checkout. Engine-reaching runs take the engine lock and obey their time window.

**Command.** `gideon eval run --slice <name> [--stack ci] [--decision --against <run-id>] [--challenger] [--force] [--set <dir>] [--kind <kind>]`.\
**Steps.** For a model decision, [docs/runbooks/model-upgrade.md §8](runbooks/model-upgrade.md).

The quiet window covers weeknights 19:00 to 06:00 and Friday 19:00 to Monday 06:00 in the site's timezone. The account used for evaluation turns through the frontend is called the eval identity.

`extraction` scores the set's labelled exact objects, and `judgments --ranked <file>` scores ranked gold-evidence coordinates against attorneys' grades, reporting nDCG@10, recall@50, and Hole@10 without gating those measures.

`guardrails` runs three guardrail families through General's service, with six cases also through the frontend, in the quiet window. It gates each family and the harness's false-refusal counts at their ceilings, while the judge's readings are reported without gating.

`general-smoke` runs General's smoke set through the frontend as the eval identity twice in the quiet window to show determinism, reporting pass or fail without grading answers.

`smoke` can run at any hour: it runs twelve engine calls, the guardrails sample through the service and frontend, extraction whole, and the CI sample's gold-drop check. Its gate fails on a zero-tolerance failure, a failed gold-drop check, or a regression against its reference.

`gold-drop --generation <name>` reads a generation's manifest for the set's gold documents, failing on any dropped zero-tolerance case or more than 5% of gold dropped; the run records the generation and corpus lockfile. Without a name, it reads production's live generation or, with `--stack ci`, the newest committed lockfile's sample generation on the selected hardware profile's line.

`count-gates --generation <name>` checks the newest build report against the committed lockfile's courts and fails by court: every court must be staged, recorded counts must reach staged counts, processing must finish, and every failure must be named.

`--stack ci` selects the CI sibling for evaluations that drive turns, read the judge, or read stores, though the judge still calls production's engine. `--kind` names the run's purpose on its record, and `--set <dir>` loads a set outside the release and records nothing.

`--challenger --stack ci` runs the experiment in `config/challenger.yaml` beside the release configuration on the build box and records paired runs, or prints `skipped` when no challenger is set.

`--decision --against <run-id>` runs guardrails five times in the weekend window, pairing each control's false refusal with the named run's to record the mean difference, its 95% interval, and a verdict of wins, loses, or undecided. Too few paired clusters causes a refusal without an interval.

`--force` starts a run outside its window and notes it on the run's record; engine-reaching runs still take the engine lock, so a held lock ordinarily refuses another. At the window's end, an engine-reaching run aborts, keeping completed repeats and recording a partial run.

A zero-tolerance gate reports counted cases and the 3/n bound on its gate row and `bound:` line before `verdict`; the count stays on the run record, while the bound is reported without gating. Recorded engine-reaching runs carry the engine's in-flight maximum and preemption count on the run and `record` rows, with those readings unknown when Prometheus is unreadable or engine scrapes do not cover the span.

At 21:00 office time, the nightly timer runs `general-smoke` then `guardrails` in one `--kind nightly` invocation, waiting once behind a held engine and stopping each suite at 06:00; on the build box, the challenger's pair follows.

### gideon eval reference --run <id>

Eval reference takes one recorded run as a tagged release's reference, writing one content-free file per ID list so a later comparison can block by ID when a case that passed there fails now; a mean does not gate. A developer runs it from a clean checkout at the release tag.

**Command.** `gideon eval reference --run <id>`.\
**Steps.** [eval/reference/README.md](../eval/reference/README.md#commands).

### gideon eval candidates --out <dir>

Eval candidates produces the material for candidate evaluation questions: it writes the month's thumbs-down turns beside their IDs and buckets as a root-only page and a text-free manifest. A CSA runs it once a month, as root, into an empty directory outside backup sets and checkouts. Read the page with `less` and write candidate questions in your own words.

**Command.** `gideon eval candidates --out <dir>`.\
**Steps.** [docs/runbooks/feedback-packet.md §1](runbooks/feedback-packet.md).

### gideon proposals

Proposals reads waiting proposals, trigger measurements, feedback counts, guardrail trips, upstream notices, and the build box's challenger pairs, and a CSA runs it when reviewing proposals; the feedback read needs root. The daily tally uses `--record`, while an ordinary report writes nothing and exits 1 only when a section cannot be read.

**Command.** `gideon proposals [--record]`.\
**Steps.** [docs/runbooks/release-files.md §7](runbooks/release-files.md); for testing the tally, [docs/runbooks/observability.md §7](runbooks/observability.md).

The unimplemented commands `gideon host gpu`, `gideon registry gc`, and `gideon audit query` each print "not implemented" and exit 1.

## 2. The build-box tools

These tools run from a checkout on the build box or in a development virtual environment, and each command line includes `sudo` where the tool needs it. They are not steps in a receiving office's install home.

### The gate

The gate compares the environment with the tree's pins and then runs lint, type checking, and unit tests, stopping at the first failure. A developer runs it before a commit, and CI runs it too; `--all` includes the slow tests, and `--masked` hides the server's paths from the tests.

**Command.** `python3 -m tools.gate [--all] [--masked] [TEST_PATH ...]`.\
**Steps.** [CONTRIBUTING.md](../CONTRIBUTING.md#development-setup-and-the-gate).

### The image build

This tool builds, checks, pushes, and records one of GIDEON's three images, `postgres`, `gideon`, and `opensearch`, so that its lock records the digest that was actually pushed. The maintaining office's CSA runs it on the build box after changing an image's inputs, and `--check` proves the recorded image against its lock.

**Command.** `python3 -m tools.imagebuild <name> [--check]`.\
**Steps.** [docs/runbooks/built-images.md §2](runbooks/built-images.md).

### The clean-VM acceptance

The maintaining office's CSA runs this harness on the build box at minor releases to prove the receiving-office sequence in a clean environment. It creates a throwaway virtual machine from the pinned image and drives that sequence through it, with forms that also cover full restore and applications sharing the host.

**Command.** `sudo python3 -m tools.acceptance <ref> [--full-restore | --cotenant before|after | --pair before|after]`.\
**Steps.** [docs/runbooks/install-upgrade.md §6](runbooks/install-upgrade.md).

### The pin watch

CI runs the watch to propose pin bumps, while a maintainer uses its dry run from a checkout and prints a model's files block or generates a release note's "What was bumped" section. The maintainer reviews the proposals each Monday because merging a pin change is a release decision.

**Command.** `python3 -m tools.pinwatch --dry-run`.

`python3 -m tools.pinwatch.hub <repo>`.

`python3 -m tools.pinwatch.bumped [--since <tag>]`.

**Steps.** For model files, [docs/runbooks/model-upgrade.md §3](runbooks/model-upgrade.md); for reviewing proposals, [docs/runbooks/release-files.md §9](runbooks/release-files.md).

### The evidence redaction

Before saving evidence, a developer runs this tool over a transcript on the server to replace office values declared by the site registry with placeholders named after their site keys; the acceptance harness uses the same function for transcripts and stored messages.

**Command.** `python3 -m tools.redact --site /etc/gideon/site.yaml < draft > asset`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The court map generator

After reviewing court placements, a maintainer runs this tool on a development machine to generate `courts.yaml` from CourtListener's bulk courts data and a reviewed hand table. Download the CSV separately; `--check` verifies the map without writing it.

**Command.** `python3 -m tools.courtmap --csv <courts-YYYY-MM-DD.csv.bz2> [--check]`.\
**Steps.** [docs/runbooks/release-files.md §6](runbooks/release-files.md).

### The CI sibling stack

This tool operates `gideon-ci`, a separate project with its own stores, frontend, and service that uses production's engine through a relay. A developer runs it on the build box, and CI uses its smoke command. The sibling's frontend is on loopback port 18100, and its state is outside backup sets.

**Command.** `sudo python3 -m tools.cistack up | smoke | status | down [--wipe]`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The turn harness

The turn harness runs cases through the frontend, General's service with `--service`, the engine with `--unfiltered`, or a browser with `--browser`, printing each case's class. A developer runs long sets in the quiet window or on weekends. The harness takes the engine lock, and `--beside` runs it beside the lock's holder, which is kept for deliberate overlap; `--stack ci` selects the sibling, and browser mode needs a users-group seat.

**Command.** `sudo python3 -m tools.turns <cases> [--case <id>] [--stream] --out <dir> [--stack ci]`.

`sudo python3 -m tools.turns --service <cases> [--stream] --out <dir> [--stack ci]`.

`sudo python3 -m tools.turns --unfiltered <cases> --case <id> --out <dir>`.

`sudo .venv/bin/python -m tools.turns --browser <cases> --out <dir>`.

**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The board check

The board check opens Grafana's boards as the break-glass administrator, reports each panel's state, and saves a screenshot of each board, so a developer on the server can inspect the rendered boards. The check writes nothing to Grafana; read the screenshots without committing them.

**Command.** `sudo .venv/bin/python -m tools.boards [<uid> ...] --out <dir> [--width <px>] [--trust-ca]`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

### The variants kit

A developer uses this kit when checking or extending evaluation cases: it checks committed variants against parent cases, prints missing pairs with `--sheet`, or appends reviewed variants with `--write`. A write requires the complete series manifest, reviewer ID, and date.

**Command.** `python3 -m tools.variants <parents> <variants> [--sheet | --write --reviewer <id> --on <date>]`.\
**Steps.** [eval/sets/eval-v1/build-gates/README.md](../eval/sets/eval-v1/build-gates/README.md#the-variants).

### The treatment fetch

Before measuring treatment patterns, a developer runs this tool to fetch the pinned treatment dataset files, verifying each file's size and digest before giving it its final name and leaving already verified files in place.

**Command.** `python3 -m tools.treatment fetch --to <dir>`.\
**Steps.** None in a runbook; the command prints its rows and each refusal's fix.

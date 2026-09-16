# GIDEON

A fully local legal AI system for the Office of the Federal Public Defender, Middle District of Tennessee, supporting federal criminal defense and capital habeas work. This glossary is the canonical vocabulary; terms were established during greenfield-spec charting (Aug 2026). Entries are grouped by domain area; an entry whose mechanism a landed architecture leaf under `docs/archi/` owns ends with a `_Leaf_:` line linking that leaf, and a new entry joins its group.

## Language

### Organization

**TNMD-FDO**:
The Office of the Federal Public Defender for the Middle District of Tennessee — the pilot office building and running GIDEON.

**TRAD**:
The office's Trial Unit.
_Avoid_: trial division, traditional unit

**CHU**:
The office's Capital Habeas Unit.

**CSA**:
Computer Systems Administrator — one of the two IT staff who build, run, and maintain GIDEON.

**Gideon Transcribe**:
The office's other project: the transcription app that shares the box and the GitHub organisation with GIDEON, in its own repository with its own release cadence. Transcription is its work, never a GIDEON branch; a transcript reaches the Legal chat as a matter upload. The box ledger records what the two share.
_Avoid_: Transcribe (the dissolved branch name), the transcription branch

**Receiving office**:
Another defender office that adopts GIDEON by cloning a release tag, editing the site config, and running the installer on a server with internet egress at install and upgrade time; an office without egress cannot adopt it. Includes both Federal Public Defender Organizations (government) and Community Defender Organizations (nonprofit grantees); the distinction matters for licensing, not operation.

**Reference build**:
The one hardware profile TNMD-FDO runs and evaluates against; the only configuration that is supported without qualification.
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

### System

**Branch**:
One of GIDEON's two user-facing chats, General and Legal; each is a preset over the generator and a selectable model in the frontend, and there is no third. Research, Review, and Draft are what the Legal branch does, not branches.
_Avoid_: workflow, mode, model (for this concept), Research branch, Review branch, Draft branch

**Spine**:
The subset built and specified first: platform + General + the full Research stack. Review and Draft, the Legal chat's later work, come after the spine is proven; transcription is Gideon Transcribe's, never the spine's.

**General**:
The branch that replaces public chatbots: internet search whose fetched pages reach the model whole, as the one context General ever receives; no case-matter access, no uploads, no verification. Defined by what it lacks — it is the only branch whose user text may leave the box. A preset the apply manifest carries, the first, and the frontend's default model for every authenticated user by a release fact, not by being the only model on offer. A user's own default, set from the model picker, outranks the rendered one on that user's new chats — a landing, never a guarantee (the pinned frontend's precedence, `docs/research/owui-default-model-and-arena.md` §1.3) — and once the Legal chat is visible a rendered order lists General first (standing ticket 46).
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Legal**:
The chat for anything about a case and anything confidential: corpus-grounded answers with computed, verified citations, the matter's own documents once Ingestion GA lands, and nothing that leaves the box. What it does — Research, then Review, then Draft, in the order they are built — is chosen by the turn's route and the matter's attachments, never by a second chat. Its display name is a release text; Research stays the name of the service and the domain term.
_Avoid_: Research chat, case chat, confidential mode, Legal branch (a branch is one of the two chats, so say the Legal chat)

**Generator**:
The one language model every branch shares; a branch is a preset over it, never a second model, and a human converses with a branch, never with the generator: the frontend's selector hides the generator's own record as a convenience, and the arithmetic guardrail's inlet refuses a person's turn on anything but a preset before the generator is called — admins and the eval identity pass, and the frontend's own housekeeping calls (a chat's title) reach it untouched.
_Avoid_: LLM, base model, chat model
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Engine**:
The single serving process that runs the generator and is the only thing that uses the generator's GPU.
_Avoid_: inference server, backend (for this concept)
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Task model**:
The model the frontend uses for its own housekeeping calls — a chat's title and tags, the search queries it derives from a message — which is the generator, never a second model.
_Avoid_: helper model, utility model, small model
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Model record**:
The frontend's own row for one model — its capabilities, parameters, and grants — which the apply manifest pushes whole. A preset is a record over the generator; the generator's own record annotates the model the frontend discovered (its "base model") and is not a preset.
_Avoid_: model config, model entry, custom model, workspace model
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Preset**:
A model record over the generator that makes a branch selectable: its system prompt, description, capability toggles, and grant, rendered from the release with the office's name filled in and pushed by the apply manifest. A preset is a default the frontend applies where a request lacks a value, never a guarantee.
_Avoid_: custom model, workspace model, model preset (redundant), persona
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Built-in tools**:
The frontend's own hidden tools — a clock and date arithmetic, a question back to the user, notifications, delegated sub-tasks, and the rest — which it attaches to a browser turn on any model whose record allows them, for the model to call. Off on every GIDEON record through v1: both branches are designed around the frontend's tool loop, and nothing in the product parses a tool call. Not Research's named tools, which are structured queries the model never writes.
_Avoid_: native tools, hidden tools, tool calling (for this concept)
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Search egress**:
User-authored text leaving the box as web-search queries (and the result pages fetched in reply). Permitted only in General, only when the user turns search on, and always preceded by a reminder.
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Privilege reminder**:
The fixed release text the frontend's own confirmation dialog shows once per chat when a user turns search on in General: what leaves the box, that client and case material must not, and that anything relied on belongs in Research. An office cannot change it; a wording change is a release.
_Avoid_: search warning, disclaimer, banner, consent dialog
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Search sentinel**:
The test that drives one General search carrying a synthetic marker through a throwaway copy of the stack, on the happy path and on each failure path, and finds the marker in no log the stack writes; the standing guard behind the rule that no search-query text is logged anywhere.
_Avoid_: canary, leak test, sentinel string (for the test)
_Leaf_: [`docs/archi/tests.md`](docs/archi/tests.md)

**Prototype**:
The pre-production GIDEON running on the office server before the Wipe. Historical reference only; nothing about it is authoritative.

**The Wipe**:
The planned full teardown of the server — OS included — before the greenfield build.

**Harvest**:
The pre-Wipe capture of real user questions and prototype answers, used to seed the evaluation set.

### Identity and access

**Case team**:
The people working a matter — attorneys, investigators, paralegals, and legal assistants alike — and the default set who may see its documents.
_Avoid_: attorneys (when the whole team is meant)

**Matter knowledge base**:
The collection of one matter's uploaded documents. Private to its owner on creation; visible only to the people or groups it is explicitly shared with.
_Avoid_: KB (in prose), collection, matter folder

**Screened matter**:
A matter some members of the office must be walled off from. Its knowledge base is shared with named people only, never with a unit.
_Avoid_: walled matter, conflict matter

**Mirrored group**:
An office directory group copied into GIDEON as a sharing target. Carries membership only, never permissions.
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Conflicts-wall incident**:
Any attempt to reach a matter knowledge base the requester holds no grant to — by a person, a bug, or an injected instruction. Always logged and alerted; never a warning.

**Break-glass admin**:
The one local administrator account of a surface — the frontend's, the dashboards' — that works when the office directory does not; its password is generated once, printed once, and kept in the office password manager.
_Avoid_: emergency account, root user
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Machine identity**:
A non-human account (the eval harness, the installer) permitted to hold an API key and, where the key's endpoint list does not reach, to sign in for a session token of its own. People never hold one.
_Avoid_: service account (ambiguous with the directory bind account), bot user
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Test account**:
A directory account in the users group and nothing else, held by the CSAs for proofs from a user's seat; its password is a root-owned file outside the product's secrets, so the product never knows the account exists, and it holds no API key.
_Avoid_: service account, dummy user, machine identity (for this)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Dev seat**:
The CSA account on the box from which agent sessions run, where the primary checkout lives, and from which the box's rendered units run; root by its sudoers drop-in through General's launch, and not in the docker group (ADR-0037).
_Avoid_: admin account, operator login

**Directory identity**:
The office-directory attribute every human account is keyed by across the frontend, the reconcile, and the audit log: the userPrincipalName, chosen because every directory account has one. The sign-in name is separate (the account name), and the identity is not a mailbox.
_Avoid_: email, mail attribute, login (for this concept)
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

### Platform

**Host layer**:
Everything on the server beneath the containers — operating system, GPU driver, container runtime, disk mounts, accounts, firewall — set up once per box before GIDEON is installed.
_Avoid_: infrastructure, base system
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Target state**:
The documented host-layer configuration a box must reach, by whatever means its office chooses, before installation can proceed. Verified by preflight, never assumed.
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Host provisioning**:
Bringing a freshly installed operating system to the target state. Re-runnable: it changes only what is not already correct.
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Image store**:
The shared Docker daemon's images, their layers, and the build cache, kept by containerd under its own root rather than under the daemon's data root — a directory on the Docker volume, the one setting of containerd's the `docker-engine` step owns (ADR-0040) — so the root filesystem holds the operating system and nothing that grows with a pull or a build. A populated store moves by hand in a maintenance window; provision refuses to abandon one.
_Avoid_: the data root (the daemon's own metadata and volumes, which the store bypasses), the registry (the release registry under `/data/registry`, which the mirror fills), image cache
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Preflight**:
The check-only pass over the host layer and site configuration that must pass before an install or upgrade proceeds; each failure is either a refusal or a warning, and a refusal names its fix.
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Drill instance**:
The throwaway second copy of GIDEON on the same box into which the restore drill restores a backup; torn down once the check passes.
_Avoid_: staging, staging VM
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**No-GPU host**:
A host that has declared, through provisioning, that it carries no GPU: it serves no engine and cannot declare itself the build box, so the driver, the engine's exporter, and the build-box roles are pinned out. Host state, never site configuration.
_Avoid_: CPU-only mode, headless host, driverless host
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Build box**:
The one host that has declared itself, through provisioning, the host that makes the acceptance VM, serves the release registry, and runs the organisation's CI runner — TNMD-FDO's box. Host state, never site configuration: a receiving office's host declares nothing and never carries the roles, and a no-GPU host cannot be the build box.
_Avoid_: CI box, dev box, the reference build (a hardware profile, not a role)
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Acceptance VM**:
The throwaway virtual machine the build box makes from the pinned cloud image for one acceptance run — a no-GPU host with its own office services — and tears down after it.
_Avoid_: test VM, staging VM, the drill instance (a second copy on the same box, not a machine)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Acceptance harness**:
The repository tooling that builds the acceptance VM and drives the receiving-office sequence on it at a minor tag — install with the drill, an upgrade-and-rollback rehearsal, a push and a restore — whose pass is the slice's proof.
_Avoid_: acceptance tests (the unit suite is not it), end-to-end suite, smoke run
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Engine verification**:
The acceptance check run against the running generator after install, upgrade, or any driver or engine change: long-context recall, an eval sample, structured output, and a guardrail trip. Failure blocks the install or upgrade.
_Avoid_: smoke test, model preflight
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Office time**:
The clock `office.timezone` names — the one the host, every container, every schedule, and a naive point-in-time value are read in. The record keeps instants, not office hours.
_Avoid_: local time (ambiguous between the host's and the office's), server time, UTC (the clock of labels and recorded instants, never of a schedule)
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Memory table**:
The hardware profile's per-service RAM budgets: one row per Compose service the release knows on any host, a whole number of decimal gigabytes each and, where the service loads a model, the role it depends on. Render turns every row into the service's memory limit as an exact byte count, so a service without a row does not render and a row without a service is refused. A starting value corrected from measured use, never from a corpus size and never a site setting; a limit is a ceiling against a runaway, not a reservation.
_Avoid_: RAM budget (the spec's phrase for the same rows; the artifact is the table), memory reservation, container quota
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

### Site configuration

**Site**:
Everything on a box that belongs to the office rather than the release — the site file, the supplied certificate and CA root, the secrets, and the rendered configuration — in one directory that survives every upgrade and rides in every backup.
_Avoid_: deployment config, instance config

**Site file**:
The one file a receiving office edits: the office's facts and the policies it is entitled to set. Contains no secret and no path.
_Avoid_: config file, settings file, .env
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Site key**:
One setting in the site file. Exists only where TNMD cannot know the value for another office or where the office is entitled to a policy; a knob that changes what an eval measures is never one.
_Avoid_: knob, tunable, option
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Rendered configuration**:
Every downstream configuration file — the Compose file, each service's config, the apply manifest — produced from the site file and the release by one deterministic step; never edited by hand. Each file belongs to the services that read it, and each service owns its own block of the Compose file, so a changed file or block means those services are re-created.
_Avoid_: generated config, overrides, compose override
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Host facts**:
Values read from the box itself at render time — the GPU identities, the names of generated secrets — that neither the site file nor the release can know; an input to rendering, never a setting.
_Avoid_: host config, detected settings
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Apply manifest**:
The complete set of Functions, model records, and groups Open WebUI must hold — the presets and the generator's own record — rendered from the release and pushed whole; anything not in it is removed.
_Avoid_: preset export, function import
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Apply**:
The day-two command after a site-file edit: render, show the difference, recreate only what changed since the last apply that verified, push the apply manifest, verify.
_Avoid_: redeploy, reconfigure, restart
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Supplied secret**:
A secret only the office can provide — a directory password, a private key, a relay password — placed as a file under its fixed name before preflight.
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Generated secret**:
A secret the installer creates for GIDEON's own components; a human keeps a copy only when it is a break-glass credential, printed once at install. Its rotation class says whether the product rotates it: only a secret whose file is the value's only home is regenerated by `gideon secrets rotate`, which then recreates exactly the services that loaded the old value.
_Avoid_: default password
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Rotation class**:
The release's ruling, per generated secret, on where its value lives beyond its file — nowhere (rewritten in place), in a key the frontend re-issues when the file is gone, or in a database role, an account, or Grafana's seeded administrator — and so whether a file rotation rotates it whole or the consumer's own route must change it first.
_Avoid_: rotatable flag, secret kind
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

### Release and distribution

**Release**:
A tagged product version: a known-good combination of every component, pinned by lockfiles, that either works or doesn't as a unit.

**Release note**:
The user-facing account of a release — what is new, what was bumped (every pin the release moved since the previous user-facing tag, generated from the locks and never typed), what the corpus covers, and the next maintenance window — written from a versioned template, one per user-facing tag, sent by the CSAs to the users and read by a receiving office. Never the engineering record.
_Avoid_: changelog, announcement, patch notes
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Changelog**:
The per-version engineering record of a release — what changed, and for a major release the Breaking section an upgrade shows before crossing the major. Read by a CSA at the upgrade, never sent to users.
_Avoid_: release note, release log

**Hardware profile**:
A named, evaluated configuration within a release matching a class of hardware: the model set with its GPU assignment and serving baseline, the memory table, the chunk guards (build refusal, serving ceiling), and a requirements block preflight checks as minimums. Named by GPU layout and DRAM floor, not box model or VRAM total — `2x96v-256d` is 2× 96 GB GPUs + 256 GB DRAM; `128u` a unified-memory machine — because models bind to whole GPUs, so the layout decides what fits. The name is a legible floor; the profile body, in the models lock, is authoritative, and the lock names which profile is the reference build. An office selects one by name in its site file — an unknown name refuses, a non-reference one warns; any other substitution is unsupported.
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Source snapshot**:
One dated pull of a single corpus source (caselaw, US Code, Guidelines, …), labelled `<source>-YYYY-MM-DD`.

**Corpus lockfile**:
A dated label (`corpus-YYYY-MM-DD`) pinning one source snapshot per source; the corpus half of the version triple.
_Avoid_: corpus version (ambiguous with source snapshot)

**Corpus cut**:
Writing a new corpus lockfile — every source pinned at its then-latest — and committing it. Done by a person with one command; nothing cuts itself. Quarterly with the case-law dump, plus instrument cuts, tranche cuts, and pipeline cuts.
_Avoid_: corpus release, corpus bump

**Instrument cut**:
An out-of-cycle corpus cut prepared when an authority instrument publishes (a Guidelines Manual, a rules package) and promoted on its effective date, never before, so the pending-instrument label normally lasts ~0 days; 14 days after effect is the outer bound where the source publication itself lags. Rebuilds only the authorities.

**Derived cut**:
A corpus cut made from an existing lockfile (`--base`): the base's pins copied verbatim — nothing re-downloads — `courts[]` extended by explicitly named ids, provenance in the new lockfile's `base:` header. The one mechanism behind TNMD's own tranche cuts, a receiving office adding its state courts, and a rebuild after loss.

**Tranche**:
One expansion of the live corpus — a set of courts or a family of authorities — added by one corpus cut and promoted beside the live generation under the release gates. The go-live note names which tranche a unit is waiting on.
_Avoid_: phase, slice (reserved for product releases)

**Upstream watch**:
The weekly job that only observes each corpus source for a new dump, release point, instrument, or snapshot date and raises a notice; it fetches nothing else and is the source of the instrument-lag measure. Corpus sources only; software versions are the pin watch's.
_Avoid_: sync job, poller

**Pin watch**:
The job, run off the box in hosted CI, that notices a newer upstream version of anything a release pins, or of a development skill the repository vendors, and opens one pull request per bump. It upgrades nothing: a bump becomes a release only when a person tags one, a vendored skill moves only by a recipe a person runs, and no component checks for its own updates. Its pull request also names the research notes verified against the pin it moves, each with the version it was verified at, so a bump says what to re-read; the notes carry that record themselves, as the claim of whoever verified them.
_Avoid_: update check, auto-update, dependency bot
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Vendored skill**:
A development skill installed under the repository from an upstream source and recorded in the skills lock, so every CSA's clone carries the same one. It is edited in exactly two places — a slot the initial setup filled, or a marked GIDEON block — and any other edit is drift the hosted checks fail.
_Avoid_: forked skill, customized skill, local skill
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Phase**:
One of the three stages of a ticket's cycle — plan, implement, release — each entered by its own skill and, since the launcher, run in a session of its own on the model and effort paired with it, the pairing a starting value the cycles record corrects; the previous phase ends by handing over the next one's command, and a chained cycle continues on that handoff record, never on a clean exit.
_Avoid_: step (a phase has steps), stage (the ordered commands' word), session (the container, not the work)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Peak context**:
The largest prompt one development session sent — a single response's input, cache-creation, and cache-read tokens together — the measure a ticket is sized against, recorded per session in the cycles record from the harness's transcripts; the session's total tokens are its cost, never its measure, and its turns are the cost's measure beside the peak: the peak predicts compaction, the turns predict spend.
_Avoid_: session size, context usage, tokens used (for this measure), cost (for the peak)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Tracker board**:
The committed rendering of the work tracker's state — the lint's findings, the critical path to the next tag, the frontier, the decisions owed, the triage owed, every open ticket by effort, and the standing tickets apart — regenerated whole at every release from the issue files alone and never edited by hand; its page is the same rendering for a glance.
_Avoid_: dashboard, kanban, status page, the tracker (for the rendering; the tracker is the files)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Critical path**:
The open tickets a release ticket depends on, directly or through their own blockers, in dependency order — a blocker before what it blocks — the release ticket named as the path's end and never one of its members.
_Avoid_: blockers list, dependency chain, the tag's tickets
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Standing ticket**:
A ticket that is open and correct and whose closing criterion names an event no session can cause — a later slice's tag, an upstream release, a calendar event — kept in the standing directory and worked in place: waiting, and offered by no section of the board, until the event fires; fired, and offered like any other ticket, after.
_Avoid_: deferred ticket, parked ticket, backlog, blocked ticket (for a ticket waiting on another ticket, which stays in its effort)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Office value**:
A value that locates or names a machine, a network, a directory object, a mailbox, or a person of the office — a hostname, an address or range, a distinguished name, an account — and is never in the tree: code, tests, fixtures, and public documents carry a **documentation value** in its place (an RFC 2606 name, an RFC 5737 network), and the tracker's evidence a **site-key placeholder** (the site key in angle brackets, a list element with its index, `<redacted>` where no key names it). The office's public identity (its name, short name, timezone, jurisdiction, organisation) and the CSAs' names are not office values.
_Avoid_: internal name, real value, site value (for what the tree must not carry), PII
_Leaf_: [`docs/archi/tests.md`](docs/archi/tests.md)

**Slip path**:
A recorded, unbuilt way of adding a home court's newest opinions between quarterly dumps: append-only pulls layered on the last dump, reconciled by the next one. Built only if post-snapshot demand shows users need it.
_Avoid_: incremental sync, live feed

**Version triple**:
Product version / corpus lockfile / eval set version — the only form in which an eval result or bug report is meaningful.
_Avoid_: eval baseline (for the third element)

**Release registry**:
The one container registry every image in a release is served from, owned by the project: TNMD's loopback registry before 1.0, the office organization's public registry after. It is part of the host layer, not of the product's containers, because installing the product means pulling from it.
_Avoid_: mirror, image cache
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Image lock**:
The release's list of container images, each named by its path under the release registry and pinned by content digest, never by tag; the one file that says which bytes a release runs. An entry is either a mirrored image (upstream's bytes, copied) or a built image.
_Avoid_: image list, image manifest, tags file
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Models lock**:
The release's hardware profiles and, per profile, its model set — each model pinned by its Hugging Face repository, revision, and every file's digest and size, with the serving baseline it runs under; the one file that says which model bytes a release runs and how, and the only place a model fact lives. It names the reference build.
_Avoid_: model list, weights manifest, model config, model registry
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Built image**:
An image GIDEON derives from a pinned upstream base and builds itself, recorded in the image lock by the base's digest, its build inputs, and the digest that was actually pushed — a fact of the release registry, never a target to reproduce.
_Avoid_: custom image, fork, derived image (for the entry), our Postgres (for the image)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Egress allowlist**:
The versioned list of hosts an install, upgrade, or corpus install must reach, shipped with the release and checked by preflight.
_Avoid_: firewall list, whitelist
_Leaf_: [`docs/archi/host.md`](docs/archi/host.md)

**Source archive**:
The office's own copy of the upstream files a supported release pins (model weights, raw corpus dumps), kept only to republish them if their upstream disappears; never an install source.
_Avoid_: bundle, mirror, offline copy

**Weights tree**:
The box's copy of a release's model files, laid out as the engine expects and holding exactly what the models lock pins, kept beside the one set it last replaced so a rollback needs no download. Derived from the lock, so outside every backup; the source archive is the office's insurance copy, never this tree's source.
_Avoid_: model cache, HF cache, models directory, weights on disk
_Leaf_: [`docs/archi/render-apply.md`](docs/archi/render-apply.md)

**Restore drill**:
Taking a real backup and restoring it into a throwaway instance to prove backups work; run at install and on a site-chosen interval afterwards.
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

### Rollout

**Slice**:
One product increment of the build plan, ending in a tagged release. Corpus tranches are not slices: a pure-corpus expansion is a lockfile cut that floats free of product tags.
_Avoid_: milestone, phase, sprint

**Distribution release**:
The `v1.0.0` tag — the first release another office can deploy: repo public, every image on the public release registry, the receiving-office install proven end to end. Ingestion GA precedes it as its own slice; shipping ingestion is the precondition, distribution the event.
_Avoid_: GA (ambiguous with go-live), public release, launch

**Go-live**:
The release at which GIDEON replaces the prototype for users: the first at which Research is selectable by every user on at least the prototype's corpus (SCOTUS + Sixth Circuit case law) with the verification gate on. One event; everything "before go-live" precedes it.
_Avoid_: launch (ambiguous with General's release), general availability, production

**Pre-launch release**:
A user-facing release before go-live — General to every user with no Research. Users are on it; GIDEON has not launched. Its release note states the practice rule: nothing from General goes into a court filing unchecked, and until go-live there is no Research to check it in.
_Avoid_: pilot, beta, soft launch

**Quiet window**:
The hours in which eval work that sends requests to the engine may run beside users: weeknights 19:00–06:00 and Friday 19:00 to Monday 06:00, America/Chicago. Work that never touches the engine is not bound by it.
_Avoid_: off-hours, eval window, night batch
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Maintenance window**:
An announced span inside the quiet window in which the engine may be swapped or the product upgraded and General may be unavailable; the only sanctioned unavailability.
_Avoid_: downtime, outage, degraded-service window

### Backup and operations

**Backup set**:
The record plus the configuration, the release checkout, the release registry, and the frontend uploads needed to bring GIDEON back on a bare machine, taken as one unit with a manifest that inventories every file in it and names its archive boundary; what `gideon backup run` produces and the only thing it produces.
_Avoid_: backup (for the copy), snapshot (for this), archive
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Archive boundary**:
The instant before which every committed transaction is proven to be in the archived write-ahead log; the only bound a point-in-time restore trusts.
_Avoid_: backup time, push time, recovery point
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Pre-restore set**:
The full backup set a restore takes of the running system before it replaces anything, so that a mistaken restore is itself recoverable.
_Avoid_: safety backup, checkpoint, snapshot (for this)
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Pre-upgrade set**:
The full backup set an upgrade takes of the running release before anything moves, labelled for the release it is moving to, so that the release being left can be brought back whole; its manifest — the commit, the release, the archive boundary — is the upgrade's own record of where it came from.
_Avoid_: pre-upgrade backup (for the copy), rollback point, checkpoint, snapshot (for this)
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Rollback**:
Returning the product to the release a pre-upgrade set describes: that set restored, then the previous release applied again. The host's converged state stays as the newer release left it; changing that back is a provisioning decision a person takes, never part of a rollback.
_Avoid_: downgrade, revert (for this), undo
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Backup identity**:
One of the two key pairs that seal a backup set's secrets, whose recipients the set names. The office identity, kept only in the office password manager, opens a set anywhere and is the one thing a rebuilt box must be handed; the box identity, readable only by root on the box that made the set and never carried in a set, opens that box's own sets there.
_Avoid_: age key, backup key (the SSH key that reaches the target), master key
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Off-box copy**:
The copy of the backup set on a machine other than the server, made by one push to a site-chosen target; the copy a fire or a dead disk cannot take.
_Avoid_: remote backup, NAS backup, replica
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Off-box check**:
The proof, made on every push, that the off-box copy is present and matches the set's manifest, by re-hashing a sample of it on the target — or all of it, on demand.
_Avoid_: remote drill, verify job
_Leaf_: [`docs/archi/backup-restore.md`](docs/archi/backup-restore.md)

**Kept event**:
An operational fact GIDEON retains for a year as a row — a guardrail trip, an abstention, a denial, a gate report, a backup run — as distinct from a log line, which is never kept.
_Avoid_: log entry, telemetry, metric (for this)
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Audit log**:
The append-only record of every Research turn and retrieval-affecting event — who, when, which matter, what was returned, all by id — holding no document text and no query text.
_Avoid_: access log, activity log, history
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Ingress log**:
The ingress's structured line for each request through it — who reached what, when, and how it was answered, ids and addresses only, the credential headers and the chat page's prompt-bearing parameters redacted — kept in the journal for its retention and never a kept event, never a body.
_Avoid_: access log, Caddy log, request log
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Page-class alert**:
A condition that reaches a CSA by email the moment it occurs and again each day until it clears, as opposed to a dashboard-class signal that waits to be looked at; the rules that define the set ship with the release, never with the site.
_Avoid_: critical, P1, incident (for this)
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Dashboard-class signal**:
A measure or count the box charts for whoever looks, never emails: everything observed that is not a page-class alert.
_Avoid_: warning, low-priority alert, metric (for this distinction)
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Channel heartbeat**:
The one scheduled page-class email whose only content is that the alert engine and the mail path are alive, sent so that their silence is something a person notices.
_Avoid_: dead man's switch, watchdog alert, test alert (for this)
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Prompt logging**:
The site decision on whether GIDEON's own service keeps the text of questions, prompts, and answers beyond the user's chat: metadata only, or full text under a short expiry.
_Avoid_: chat logging, conversation logging, transcript

**Chat retention**:
The one office-wide interval after which an idle chat, and every upload attached to it, is deleted, in every branch alike.
_Avoid_: session retention, history TTL, auto-delete

### Corpus and retrieval

**Corpus**:
The law on disk: court opinions, statutes, court rules, regulations, and Guidelines acquired from public bulk sources. Versioned independently of the product by corpus lockfile (`corpus-YYYY-MM-DD`), acquired only from whole-database bulk snapshots and rebuilt, never patched.

**Authorities**:
The non-caselaw corpus — statutes, court rules, regulations, Sentencing Guidelines — where retrieval must respect effective-date versions.

**Matter**:
A case or client engagement whose documents users upload and query. Matter content is temporary by policy; the corpus is not.

**Verification gate**:
The deterministic check run on every Research answer that every source tag resolves, every quotation is verbatim in the passage it tags, and no citation was written by the model. It removes what fails and never adds; enforced in code, never by prompting.

**Citation stamp**:
The fixed warning General appends whenever its answer contains anything citation-shaped: a label saying nothing was verified, never a verdict on the citation. The General-branch counterpart of the verification gate.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Supporting model**:
Any model GIDEON runs other than the generator — embedding, rerankers, and later speech-to-text or document-vision models. All of them share the second GPU under fixed memory budgets.
_Avoid_: aux model, helper model, small model

**Embedding space**:
The one embedding model, revision, output dimension, precision, and query-instruction convention under which every corpus chunk, uploaded document, and query is embedded. There is exactly one at a time; nothing is ever compared across two.

**Index generation**:
A complete build of one collection's indexes from one set of source snapshots, one pipeline version, and one embedding space. Built alongside the live generation and swapped in whole; never edited in place or mixed with another. A corpus lockfile that leaves a collection's inputs unchanged keeps its live generation.
_Avoid_: reindex (for the artifact), version (ambiguous with release)

**Retrieval instruction**:
The fixed task sentence prepended to a query — never to a document — before embedding or reranking, telling an instruction-aware model what kind of passage to favour. Shipped with the release; changing it is a configuration experiment, never a reindex.
_Avoid_: prompt (for this concept), prefix

**Bulk embed**:
The background job that embeds a corpus snapshot into a new index generation: resumable, checkpointed, keyed so that a chunk already embedded under the same embedding space is never embedded again.

**Build report**:
The per-build record every index build writes: documents per court by status, precedential status, and text source; metadata changes by field; anchor coverage; gate results; wall-clock. A court arriving mostly unknown or un-pinciteable is a number here before promote.

**Withdrawn document**:
A corpus document whose source no longer carries it (merged, duplicate, sealed): kept in the record so citation edges and eval gold passages can explain themselves, excluded from every later index generation. Never deleted.
_Avoid_: deleted, removed, purged

**Boilerplate flooding**:
The retrieval failure where near-identical recited language (standards of review, doctrinal formulations) swamps result lists at corpus scale, burying the case that actually resolves the question.

### Authority versions

**Court rule**:
A Federal Rule of Criminal, Civil, or Appellate Procedure or of Evidence, or a Rule Governing § 2254 Cases or § 2255 Proceedings — prescribed by the Supreme Court under the Rules Enabling Act and versioned exactly like a statute section.
_Avoid_: rule (bare — that is the step-back leg), procedural rule, local rule (for this concept)

**Regulation**:
A section of the Code of Federal Regulations — agency law issued under a statute's delegated authority, ingested by whole title and versioned like a statute section; the eCFR carries its current text, the annual edition its official one.
_Avoid_: reg, CFR (for a single section), rule (for this concept)

**Advisory Committee Note**:
The rulemaking committee's explanatory note published beneath a court rule — retrieved and cited as the rule's commentary, never rendered as the rule's text.
_Avoid_: committee comment, notes (bare), legislative history (for this concept)

**Reference date**:
The date a user states in a question that selects which version of each authority is retrieved; absent, today, and the answer says so. Which of a matter's dates governs is the attorney's judgment, never GIDEON's.
_Avoid_: offense date (as the concept), sentencing date (as the concept), as-of date

**Authority version**:
One authority's text — a statute section, or a guideline with its commentary — as it stood over one span of dates; kept once per distinct text, with the range it was published as current.
_Avoid_: revision, edition (for a single section)

**Instrument**:
A published body of authority text ingested whole and dated as a unit — a US Code release point or annual edition (appendices included), a Guidelines Manual, a Supplement, a dated eCFR snapshot, an annual CFR edition.
_Avoid_: release (ambiguous with a product release), snapshot (ambiguous with source snapshot)

**Edition**:
The Guidelines Manual or Supplement in force on a date — the unit §1B1.11 selects and the one-book rule applies in its entirety.

**Amendment event**:
A dated change to one authority as recorded in that authority's own history notes, whether or not the changed text is in the corpus.
_Avoid_: diff, version (for this concept)

**Pending instrument**:
An adopted instrument whose effective date is known but whose text is not yet in the corpus; answers say so from that date until it is ingested.

**History horizon**:
The earliest date for which an authority's text is in the corpus. A reference date before it yields a label and the authority's amendment dates, never the current text passed off as historical.

### Query layer

**Query plan**:
The bounded, structured reading of a Research question that one model call produces before retrieval — a route, up to a few standalone sub-queries, an optional rule query, optional narrowing filters, and optional named-tool calls. It adds retrieval legs; it never removes or alters an exact object.
_Avoid_: rewrite, query expansion (for the artifact), agent plan

**Exact object**:
A citation, statute section, Guidelines id, rule cite, docket number, or party name present verbatim in the user's text, extracted by deterministic code and matched verbatim, never paraphrased.
_Avoid_: entity, keyword, mention

**Retrieval leg**:
One query against one store with its own text and filters, whose results are fused with every other leg's before ranking. A turn runs several legs; none is the "main" one.
_Avoid_: sub-search, pass, channel

**Route**:
The intent class the query plan assigns a question — lookup, doctrinal, standard, existence, matter-facts, drafting — which selects how the question is asked (retrieval instruction, rule query), never whether a collection is searched.
_Avoid_: router, classifier, intent (as the term)

**Rule query**:
The generalized "what is the governing standard" question behind a fact-specific one, run as its own leg that welcomes boilerplate chunks.
_Avoid_: step-back query, abstraction

**Floor filter**:
A filter set by code from interface state — the tenant key, the collection set — that neither the user's words nor the model can override.
_Avoid_: hard filter (ambiguous with user-stated), ACL filter

**User-stated scope**:
A jurisdiction the user names in the question, extracted deterministically and applied as a filter on every leg. The user narrows; the model does not.

**Narrowing filter**:
A filter the query plan proposes from the fixed collection vocabulary — dates, courts, section types, authority types — applied to one additional leg only, so a wrong guess can never empty the pool.
_Avoid_: self-query filter, soft filter

**Home jurisdiction**:
The circuit, district courts, and states an office practises in, declared in site configuration. It shapes ranking through the controlling leg; it never excludes other courts.
_Avoid_: default jurisdiction, local courts

**Controlling leg**:
The retrieval leg filtered to the Supreme Court and the home jurisdiction that runs beside the unfiltered legs on every turn, so controlling authority is always in the pool and everything else is labelled persuasive.

**Named tool**:
A fixed, parameterized structured query — such as the opinions citing a given opinion, or an authority's text as of a date — that the query plan may select with extracted objects as arguments. The model never writes a query.
_Avoid_: text-to-SQL, function call (for this concept)

**Out-of-corpus object**:
An exact object the corpus does not hold — a state statute, an unfetched reporter — detected by code and reported as a status, so the answer says what was and was not searched instead of filling the gap from memory.

**Retrieval provenance**:
The record every retrieval response carries of how it was produced — plan mode, objects extracted, out-of-corpus objects, filters by tier, legs run, tools called — shown to the user as a status line.
_Avoid_: debug info, trace (for the user-facing concept)

### Ranking and abstention

**Candidate pool**:
The fused, deduplicated set of chunks a turn's retrieval legs produce, capped at a fixed size, that the rerankers score. Sized per leg, never per turn.
_Avoid_: hit list, results (for this concept), top-k

**Reserved seat**:
A place in the candidate pool or the final set that a leg or an authority tier holds regardless of fused rank or score — how exact objects, controlling authority, and the matter's own documents are guaranteed to be present. Guarantees are seats, never weights.
_Avoid_: boost, quota, weight (for this concept)

**Authority tier**:
The label every hit carries for its place in the hierarchy of authority relative to the home jurisdiction — Supreme Court, home-circuit published or unpublished, home district, home state, persuasive, authority (the law itself), or matter. Computed from the record, never by a model, and always shown.
_Avoid_: rank, court level, binding/non-binding (as the term)

**Final set**:
The hits that reach the generator for one turn: selected by relevance above the abstention floor, ordered by authority tier, capped in count and tokens. Nothing outside it is quoted.
_Avoid_: context, top results, evidence set

**Support level**:
How well the final set supports a turn — `none`, `thin`, or `ok` — decided by counting hits above the abstention floor and carried in retrieval provenance.
_Avoid_: confidence, relevance score (for this concept)

**Abstention floor**:
The reranker score below which a hit is dropped from the final set. A release artifact tuned on the in-domain judgment set so unanswerable questions abstain; never a site setting.
_Avoid_: min_score, threshold (as the term), cutoff

**Abstention**:
The system declining to answer because no hit clears the abstention floor. Decided by code from the support level, never by the generator, which is not called. Designed to be cheap and common.
_Avoid_: refusal (reserved for the deadline guardrail), no-answer

**Abstention notice**:
The fixed text a turn returns when it abstains: what was searched, what the corpus does not hold, and nothing else. A product artifact; never names a contact or carries site wording.
_Avoid_: refusal message, error

### Generation and verification

**Research service**:
The GIDEON service that runs a Research turn end to end — retrieval, prompt, generator call, verification gate, rendering — and the only thing permitted to call the generator on Research's behalf. The frontend Pipe is transport.
_Avoid_: tool server, backend (for this concept), RAG service

**Source tag**:
The marker the generator writes to attribute a sentence to one hit of the turn's final set; the only way it may cite anything. It never writes a citation string or a page.
_Avoid_: citation marker, reference, footnote (for this concept)

**Rendered citation**:
The citation string code writes in place of a source tag from the record — name, reporter, pincite, court, year, section and tier labels. Never model text.
_Avoid_: formatted cite, bluebook string

**Paragraph hold**:
Withholding a paragraph from the screen until every gate check in it passes, so verified text never moves and unverified text never shows.
_Avoid_: buffering, lag window (reserved for the deadline guardrail)

**Repair call**:
The one bounded generator call at the end of a turn that rewrites, against the same evidence, only the sentences the gate held.
_Avoid_: regeneration, retry (as the term)

**Removal notice**:
The fixed text left where a sentence was removed because it failed the gate twice.
_Avoid_: redaction, error

**Unsourced marker**:
The fixed label on a paragraph that asserts something without a source tag. Unsourced text is labelled, never blocked and never presented as sourced.

**Gate report**:
The record of what the gate checked, held, repaired, and removed in one turn — fed verbatim to the repair call and logged as content-free counts.
_Avoid_: verification log, trace

### Citation graph and treatment

**Citation graph**:
The record of which corpus opinion cites which case or authority, parsed from the corpus text at ingest. GIDEON's own parse is the record; CourtListener's map only checks it.
_Avoid_: citator, citation network (for this concept)

**Citation edge**:
One "A cites B" row of the citation graph — a citing opinion, its citing span, and the case or authority the cite resolved to, or the raw cite when nothing resolved.
_Avoid_: link, reference (for this concept)

**Citing span**:
The offset range in a citing opinion's canonical text where a citation sits. The sentence around it is the only text a treatment signal may read.
_Avoid_: citing sentence (as the term), context window

**Treatment signal**:
A finding on one citation edge that the citing court used explicit negative language about the cited case — overruled, abrogated, superseded, reversed, vacated, disapproved — with its qualifier and its source: pattern, list, or model.
_Avoid_: negative treatment (as the term), flag, history

**Risk state**:
The one label a cited case carries — `negative`, `caution`, or `unchecked` — derived from its treatment signals per index generation. Never a score, never green.
_Avoid_: risk score, RED/AMBER/GREY, verdict, status

**Worst-signal rule**:
A case's risk state is its strongest treatment signal: one `negative` edge outranks any number of silent ones, and no count of citations ever softens it.

**Known-overruled list**:
The Library of Congress table of Supreme Court decisions the Court has explicitly overruled, pinned as a corpus source. The one official treatment source, and Supreme Court only.
_Avoid_: overruled table, citator data

**Statutory-dependency check**:
The test that a statute section an opinion cites was amended after the opinion was decided. Sets `caution`, never `negative`, because it works at section level.
_Avoid_: abrogation check, statute flag

**Treatment panel**:
The chip-modal text listing a cited case's treatment signals, its non-holding negative mentions, and what was not checked. For `unchecked` it is the only place the words appear.
_Avoid_: risk modal, tooltip

**No-green-state rule**:
The display invariant that citation-risk indicators never show green, a checkmark, or "good law" — the best achievable state is "no negative signal found."

### Storage

**Record**:
The data GIDEON cannot regenerate — the Postgres databases and the content-addressed store — and the only thing a backup must protect.
_Avoid_: source of truth, master data, system of record

**Derived index**:
Any store rebuilt entirely from the record — today the vector and lexical indexes — whose loss costs a rebuild, never data.
_Avoid_: database (for these), cache, replica

**Content-addressed store**:
The on-disk store of immutable originals and canonical texts, each named by the hash of its bytes, so the same bytes are stored once.
_Avoid_: object storage, blob store, uploads folder

**Canonical text**:
The one plain-text rendering of a document that every offset, chunk, and verification refers to; produced once per document version and stored by hash.
_Avoid_: extracted text, raw text, plaintext

**Tenant key**:
The single field that partitions a shared index by owner — the matter knowledge base's id — and that every query over uploaded documents must filter on.
_Avoid_: partition key, namespace, matter id (for this concept)

### Chunks and addressing

**Section**:
A parser-typed span of a document's canonical text — majority, dissent, syllabus, footnote, guideline, commentary, advisory note — and the largest unit a chunk may lie within. Its type is `unknown` when the parser cannot tell; it is never guessed.
_Avoid_: part, segment, block

**Chunk**:
A run of whole paragraphs inside one section, identified by its offsets into the canonical text; the unit retrieval scores and the verification gate quotes. Chunks never overlap.
_Avoid_: passage (for this concept), fragment, node

**Anchor**:
A labelled range of the canonical text that a citation can name — a reporter page, a PDF page, a Bates number, a transcript line, a statute subsection. Chunks derive their locators from anchors and never store their own.
_Avoid_: locator, bookmark, page marker

**Un-pinciteable**:
The state of a chunk that has no anchor of the kind a pincite needs; reported as such, never resolved by guessing a page.

**Neighbour expansion**:
Handing the generator a retrieved chunk together with its adjacent chunks in the same section, up to a cap — GIDEON's substitute for overlapping chunks and context-dependent embeddings.
_Avoid_: parent retrieval, context window (for this concept)

**Dupe cluster**:
A set of chunks whose text is near-identical, named by its canonical member — the highest court's earliest occurrence. Every member stays indexed; results collapse to one member at query time.
_Avoid_: duplicate group, canonical chunk (for the cluster)

**Boilerplate chunk**:
A chunk whose dupe cluster spans many opinions — recited language such as a standard of review, never a holding — flagged so ranking can discount it.
_Avoid_: template text, recitation (as the term)

### Ingestion

**Ingestion profile**:
The recipe a document is processed by — `matter` for everything users upload, `caselaw` and `authority` for the corpus. Fixed by where the document comes from, never chosen by the user.
_Avoid_: document type, pipeline (for this concept), kind

**Ingestion hook**:
The point where the frontend hands every uploaded file to GIDEON and receives the canonical text back once — and only once — the document is retrievable.
_Avoid_: extraction endpoint, loader, extractor

**Tenant binding**:
The fact that a document belongs to one matter knowledge base or one chat. A document is stored and parsed once and may be bound many times; unbinding one leaves the others untouched.
_Avoid_: membership, attachment (for this concept), link

**Session upload**:
A file attached to a Research chat rather than to a matter knowledge base — private to the uploader, bound to that chat, and expiring with it.
_Avoid_: temporary file, chat file, ad-hoc upload

**Frontend reconcile**:
The periodic comparison of the frontend's own records of files, knowledge bases, and chats against GIDEON's tenant bindings, correcting drift in both directions — the only channel by which GIDEON learns where an upload belongs or that it was removed.
_Avoid_: sync, webhook (for this concept), polling (in prose)

**Ready count**:
The "N of M documents ready" fact every Research turn over a matter carries. A document is ready or invisible, never partly in; a failed document is named, never counted as processing.
_Avoid_: progress, ingestion status, upload status

**Asserted citation**:
A citation found inside an uploaded document — linked to the corpus for reading, never counted as a court's citation, never a treatment signal.
_Avoid_: exhibit cite, extracted cite (for this concept), unverified cite (as the term)

**Low-confidence OCR label**:
The mark a rendered citation carries when its quoted text came from a page whose OCR confidence fell below the threshold. The page stays retrievable; the attorney is told to open it.
_Avoid_: OCR warning, quality flag, bad scan

### Guardrails

**No-model-arithmetic rule**:
The design rule that GIDEON never presents a number a language model computed: a result is either produced by deterministic code or not produced at all. Enforced in code by the arithmetic guardrail; elsewhere by instruction and evaluation.

**Arithmetic guardrail**:
The one guard, global on every branch, that enforces the no-model-arithmetic rule in code: it withholds the generator's reasoning from the screen and the stored message, judges the finished answer alone against bounded patterns grouped in families, each family with its own pattern ids, seed, and refusal, and replaces the whole answer on a trip. The deadline family is its first, the Guidelines family its second, and the sentence-credit family its third.
_Avoid_: math guardrail, computation guardrail
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Deadline guardrail**:
The arithmetic guardrail's deadline family: the block that stops GIDEON from asserting or confirming any filing deadline, on every branch.
_Avoid_: AEDPA guardrail, date-math block
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Guidelines guardrail**:
The arithmetic guardrail's Guidelines family: the block that stops GIDEON from resolving or confirming a Sentencing Guidelines range, on every branch, judged after the deadline family.
_Avoid_: range guardrail, table guardrail, Guidelines calculator
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Pattern id**:
The versioned name of the one bounded pattern that tripped the arithmetic guardrail, recorded with every trip and never carrying any text of the answer.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Guardrail trip**:
The kept event of one arithmetic-guardrail trip: the branch, the moment, the family, the pattern id, and whether a person or the eval identity tripped it — never a user id, a chat id, or any text. A climbing count is a tuning signal, never a record of who did what.
_Avoid_: trip log, trip audit, guardrail hit
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Lag window**:
The arithmetic guardrail's hold inside the token stream: the trailing span of the answer, kept back until the guardrail has judged it, so a matched span never reaches the screen and a trip is refused in-stream; the reasoning is withheld whole, never held and released. A released passage always arrives whole with the words that exempt it.
_Avoid_: buffering, delay, throttle
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Session refusal**:
The fixed text a user's request receives when it reaches the chat route outside the chat window, where the arithmetic guardrail's replacement cannot follow it.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Deadline doctrine**:
How a limitations or filing period works — what starts it, what tolls it, how it is counted. Fully answerable, with authorities.

**Deadline computation**:
Applying a filing period to a matter's facts to produce a calendar date or a day-count, including confirming a date the user supplies. Never produced.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Deadline refusal**:
The fixed text GIDEON returns in place of a deadline computation: what was blocked, why, and a deferral to a person responsible for the deadline.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Guidelines-range computation**:
A Sentencing Guidelines range the model resolved — an offense level and a criminal history category joined to a months range, a range presented as the client's or the matter's, or a range or level-and-category pair the user supplied affirmed. A table cell the model recalls on request is one, since the model has no table, only a memory of one. Never produced.
_Avoid_: Guidelines lookup, table math
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Guidelines-range restatement**:
A months range an answer repeats without resolving it — an authority's finding in the past tense, a statute's years, a guideline's own term range, or the user's own figures under a governing construction — as opposed to a Guidelines-range computation. Fully answerable, with authorities; a bare months range is neither.
_Avoid_: illustrative range (a table cell resolved as an example is a computation)
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Guidelines refusal**:
The fixed text GIDEON returns in place of a Guidelines-range computation: what was blocked, why a language model cannot make the calculation, and a deferral to the Sentencing Table and a person responsible for it.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Sentence-credit guardrail**:
The arithmetic guardrail's sentence-credit family: the block that stops GIDEON from computing or confirming a release date, a credit count, or a time to serve, on every branch, judged after the deadline and Guidelines families.
_Avoid_: good-time guardrail, release-date guardrail, sentence calculator
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Sentence-credit computation**:
A release date, a credit count, or a time to serve the model produced on a matter's facts — a release noun, verb, expiry, or eligibility joined in the present or future to a date, a month and year, or a season; a count joined to a credit noun, earned, taken off, or left on the sentence; a served term in the conditional or future — or a release date or credit the user supplied affirmed. Tense and voice draw the line. Never produced.
_Avoid_: good-time math, release-date estimate
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Sentence-credit restatement**:
A release date or count an answer repeats without computing it — an authority's fact in the past tense, a statute's or the Bureau of Prisons' rate or cap, a proposed term in sentencing advocacy, or the user's own figure under a governing construction or echoed with a determiner — as opposed to a sentence-credit computation. Fully answerable, with authorities; a bare count is neither.
_Avoid_: illustrative computation (an example resolved to a date or count is a computation)
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Sentence-credit refusal**:
The fixed text GIDEON returns in place of a sentence-credit computation: what was blocked, why a language model does not have what the computation turns on, and a deferral to the Bureau of Prisons' sentence computation and a person responsible for it.
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

### Evaluation

**Eval set**:
The versioned collection of test questions with expected answers and supporting evidence, used to gate every configuration change. Results are only meaningful as a product/corpus/eval version triple.

**Eval set version**:
The frozen revision of the eval set (`eval-vN`), the third element of the version triple; it changes when cases are added, removed, or re-anchored, never when the corpus does.
_Avoid_: eval baseline

**Reference run**:
The recorded run of one eval set version against one tagged release, which every gate compares the next run against.
_Avoid_: baseline (for a run)

**Off baseline**:
The run with an LLM-touching piece in its `off` state, reported beside the shipped configuration so that piece's effect is always measured, never assumed.

**Suite**:
A bundle of eval categories that run together because they share a call surface and a schedule.

**Category**:
A kind of eval question with one pass rule.

**Judgment set**:
Real questions, each paired with the passages retrieval surfaced for it and an attorney's relevance grade on each passage; the yardstick every retrieval change is judged against. Built once by attorneys, extended by the model with human spot checks.
_Avoid_: qrels, gold set, relevance labels

**Synthesis panel**:
The per-release sitting in which an attorney reads a sample of judge-graded answers with the judge's reasons and marks agreement or disagreement; it is also the audit of the judge.

**Judge**:
The model that grades synthesis quality, abstention wording, and premise correction against a reference answer. Never the source of a zero-tolerance metric, which code computes.

**Eval sample generation**:
A small index generation holding every gold document of the eval set plus a random fill of the corpus, rebuilt per corpus lockfile, on which pre-bulk experiments and the CI smoke run.

**Frozen slice**:
A named, committed list of case ids inside an eval set version (`smoke`, `segmenter-300`, `discovery-50`, `extraction`, `engine-verify`) that a gate or a build check runs unchanged.

**Decision run**:
An eval run with repeats and paired statistics whose result adopts or rejects a configuration experiment, a generator candidate, or a release.

**Closed-book-answerable**:
A label on an eval case the generator answers correctly with no evidence at all; the case still measures citation and grounding but is excluded from retrieval metrics.

**Poisoned premise**:
An eval category: questions built on a false assumption (e.g. a nonexistent case) that the system must correct rather than accommodate.

**Configuration experiment**:
A named deviation from the shipped engine or model defaults, with the metric that decides it, that enters a release only after the eval set has judged it.
_Avoid_: tuning, optimization, bench item

**Starting value**:
A figure a decision ships with, taken from an upstream default or a cited research note, that is corrected by measurement on the box and never by argument.
_Avoid_: default, initial value, guess

**Runtime measure**:
A number observed on the running box (latency, RSS, queue depth) that a decision names as its judge when the eval set cannot; owned by observability.
_Avoid_: metric, KPI, observable
_Leaf_: [`docs/archi/stack.md`](docs/archi/stack.md)

**Exempt decision**:
A decision that neither the eval set nor a runtime measure can judge, resolved with a stated reason in place of a metric.
_Avoid_: policy decision, n/a

**Turn harness**:
The tool that drives a case set through the frontend's own chat path, reads each stored answer back, classifies it with the arithmetic guardrail's own judge, and removes the chats it made; its API mode turns as the eval identity, one session or several at once for a load measure, its browser mode from a user's seat; the eval command grows from it.
_Avoid_: eval runner, chat driver
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Managed turn**:
A chat turn a caller requests in the browser's own form, so the frontend creates the chat and runs the whole path — the inlet, the generator, the outlet, the record — as it does for a person; the record, not the response, is the result, and the turn's own chat is the one, among those new since it, carrying the two message ids its caller minted. The turn harness, engine verification, and the search-sentinel contract make them.
_Avoid_: API turn (for this)
_Leaf_: [`docs/archi/engine-frontend.md`](docs/archi/engine-frontend.md)

**Turn class**:
What a stored answer shows about the guardrail and the model on one turn: replaced (the guardrail's refusal stands in for the answer, from the stream or from the outlet), declined (the model's own refusal), answered, or leak (a computation the guardrail let through). A leak fails every expectation.
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Browser mode**:
The turn harness's mode that makes each turn from a users-group seat in a headless browser: the sign-in through the frontend's own form as the test account, the prompt from the composer, the screen judged with the guardrail as it paints, the record read back as in the API mode.
_Avoid_: UI tests, end-to-end tests, the browser tier (the ticket's name, not the mode's)
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

**Flash**:
A computed date painted on a person's screen before the guardrail's replacement arrives — what the lag window removes and the browser mode measures, frame by frame; a failure of the turn since the lag window landed.
_Avoid_: leak (that is a computation the outlet let stand), glitch
_Leaf_: [`docs/archi/tools.md`](docs/archi/tools.md)

### Legal

**AEDPA**:
The Antiterrorism and Effective Death Penalty Act — source of the habeas filing deadlines (§2244(d), §2255(f)) that motivated the deadline guardrail. The guardrail covers every filing deadline, not only AEDPA's.

**Bates number**:
The per-page stamp on discovery documents; the citation unit for the Legal chat's answers over discovery.

**Pincite**:
The specific page reference within a cited opinion.

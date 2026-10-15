# The release's files (CSA runbook material)

This runbook explains the committed files named by a refusal, and who may change them. Follow the numbered section named by the command, make the stated correction, then run that command again.

## 1. What the release ships, and who edits what

Every file in this runbook is release content. The office edits `/etc/gideon/site.yaml` and the supplied secrets under `/etc/gideon/secrets/`; it does not edit files in the install home. A value in a release file changes in a release, usually through a pin-watch pull request.

A refusal naming one of these files at `host provision`, `preflight`, `apply`, `models pull`, or `gideon proposals` means the copy in `/opt/gideon` differs from the release tag. Run `git status --short` there to see the changed file. As the install directory's owner, restore that file from the release tag or clone the tag again, then repeat the refused command. The refusal's “Edit …” instruction is for the person responsible for changing the file in a release.

## 2. `host.lock`

This file records the operating-system release, the kernel tested, the NVIDIA driver package and branch, and the driver version reached on the reference box. It also sets minimum versions for Docker, Compose, and the GPU toolkit. The remaining records identify the release registry image, the self-hosted CI runner's release and its checksum, the dated acceptance-machine image and its checksum, and the reference box's storage-controller and virtual-disk facts.

`host provision` converges the host to these records, and `preflight` refuses when the lock is missing or invalid. Provision installs an absent package; a present one below a minimum, or a driver below the branch's floor or with closed kernel modules, is a refusal. A person upgrades it by `docs/runbooks/install-upgrade.md §9`. A driver proposal does not mark a version tested: move that record only after the reference box has converged on the version.

## 3. `images.lock`

Mirrored images are pinned by their upstream tag and the digest of the published image index. The two GIDEON-built images are pinned by their base image and digest, build inputs, an input digest, and the digest actually pushed to the release registry.

`registry mirror` copies the pinned image digests. After a release changes an image pin, `apply` recreates the affected service. Complete a built-image change on the build box using [the built-images procedure](built-images.md); a pull request alone does not build or publish the image.

## 4. `models.lock`

The hardware profiles contain the minimum host requirements checked by `preflight`. Each profile selects models by repository and revision, records the digest and size of every file to fetch, assigns each model to a GPU, and carries the engine settings used to serve it. The profile's memory table gives each known Compose service its memory limit and names the model role for services that load a model. Its required `chunk_guards` block holds the serving ceiling, build refusal, and resident bytes per chunk derived from the memory table's Qdrant row.

A profile may also name its embedding space: `id`, the space's name; `role`, the model that embeds into it; `revision`, the role's pinned revision the id was typed for; and `dimensions`, the vector's width. Every stored vector belongs to that space, so a person moves the id in the same change whenever the embedding model, any of its weight files, the dimension, or the precision changes; the id is lowercase words and digits joined by single hyphens, and the role must be one of the profile's models.

A profile may also carry its co-tenant reserve, `cotenant_reserve_gb`: for each GPU position, the whole decimal gigabytes GIDEON leaves unclaimed on that card for whatever other application an office installs; a position not named reserves nothing. A test holds each card's model-server budgets, their `gpu-memory-utilization` fractions of the card, plus its reserve within the card's size.

A profile may also carry its client reserve, `client_reserve_seqs`: the whole number of the generator's sequences GIDEON leaves for whatever client an office connects to the engine. It must be at or below the generator's `max-num-seqs`, and a test holds the turn harness's session ceiling, GIDEON's widest batch, plus the reserve within that cap; an absent key reserves nothing. `engine verify`'s `sequence-cap` row reads the running engine's cap against the lock's and names `apply` when they differ.

`models pull` fetches and verifies exactly the files named by the selected profile. It does not select additional files from a model repository.

On the build box the lock may also carry a `candidates:` map of A/B weights. Each entry is keyed by a lowercase hyphenated name that is no profile's model role, and holds `repo`, `revision`, `serve`, and `files` as a profile's model does, but no `gpu`, plus the `role` it stands in for. `models pull <candidate>` pulls the selected profile's files and then that candidate's. The bare `models pull` keeps a candidate while its block is in the lock and removes its files once the block is deleted. A released lock carries no candidate.

### 4.1 What the profile sizes, and how each value moves

A hardware profile is a tested configuration selected by name, never inferred from the host at run time. Its name states a GPU layout and DRAM floor, while its body is authoritative: `requires` sets the platform, architecture, and compute capability, which must match exactly, and the minimum VRAM per card, card count, DRAM, and data-volume size. A host with more cards than the count runs the profile's layout on the GPU record's first positions (today's order on a host not yet recorded), and preflight's row names the positions left unused. The GPU model is the card's name; a reported name matches when it equals that name or begins with it and one space, so every edition of the card selects the profile. A host with more of a minimum resource selects the same profile and runs its tested settings. Cards carry the model set and the settings an eval measures; RAM carries the memory table and the chunk guards derived from its Qdrant row, so a RAM change moves both through measurement and a card change needs an eval run.

| Value | Where it lives | On a smaller box |
| --- | --- | --- |
| **Cards: GPU identity and count** | `models.lock` → `requires.gpu`: the reference requires two NVIDIA RTX PRO 6000 Blackwell cards, any edition, Blackwell architecture, compute capability `12.0`. `requires.platform` is `x86_64`. | Architecture and compute capability match exactly; the count is a minimum, and cards at the GPU record's positions beyond it go unused by the profile and are named in preflight's row; the model is matched by the card's name, an edition's suffix not judged. A different card or card layout needs another profile. `requires.gpu.vram_gb` is a per-card minimum of 96 decimal GB in the reference. |
| Generator weights, card assignment, and `gpu-memory-utilization` | `models.lock` → `models.generator`: pinned weight files, `gpu: 0`, fraction `0.92`. Render passes the flags to the model server. | Keep the evaluated weights to keep the model set. The fraction budgets that share of the card as the server reports it; the KV pool is what remains after weights. Re-cut the fraction on a smaller or shared card and measure the resulting concurrent capacity. A release test holds each card's server fractions plus its reserve within that card. |
| Embedding server fraction | `models.lock` → `models.embed`: `gpu: 1`, `gpu-memory-utilization: 0.25`; render passes it to `gideon-embed`. | The same card-share rule applies. A byte bound for the engine's KV cache is the planned shape when the embedder shares a card; changing its budget needs measurement. |
| `max-model-len` | Both models' `serve.flags` in `models.lock`: generator `262144`, embedder `8192`. | The generator's native window does not shrink with the card. The embedder's input window is a recorded decision, not a hardware formula. |
| Generator scheduler settings | `models.lock` → generator `serve.flags`: `max-num-seqs: 96`, `max-num-batched-tokens: 8192`, `long-prefill-token-threshold: 4096`. | Starting values measured against the KV pool (E46); re-measure when that pool or concurrent capacity changes, and the client reserve re-cut with the cap. |
| Co-tenant GPU reserve | `models.lock` → `cotenant_reserve_gb`: reference positions `0: 0`, `1: 24` decimal GB. | A ruling for each card, corrected by measurement (E6). An unnamed position reserves zero; it does not grow automatically with a larger card. |
| Client sequence reserve | `models.lock` → `client_reserve_seqs`: the reference's figure is the generator's `max-num-seqs` less the turn harness's session ceiling. | Re-cut with `max-num-seqs` whenever the KV pool changes, a starting value with the cap it is cut from (E46). An absent key reserves nothing. |
| Embedding space | `models.lock` → `embedding_space`: the reference records the embed role, 1024 dimensions, the space id, and the `embed` pin's revision the id was typed for; embed `serve.flags` fixes its precision and returned width. | Move the id only when the embedding model, a weight file, dimension, or precision changes. A different space is a different index; vectors from the two spaces never mix. The committed-lock test holds the block's `revision` equal to the pin's, so a pin-watch bump is a proposal whose checks stay red until a person moves the id and the block's `revision` together on the branch. |
| GPU record and CDI device names | Host fact `/etc/gideon/gpus.yaml` binds card positions to UUIDs; render emits `nvidia.com/gpu=<UUID>` device names. | Read from the installed host, not scaled or set by the profile. |
| **RAM: `dram_gb`** | `models.lock` → `requires.dram_gb`: 256 decimal GB in the reference; preflight compares it with `MemTotal`. | A minimum named by the profile, not a limit or an automatically chosen row. |
| Compose memory limits | `models.lock` → `memory`, rendered as exact decimal-byte `mem_limit` values. The footprint rows size Postgres, Qdrant, the frontend, generator, and the Caddy/registry/SearXNG group; other rows start at the smallest whole GB at least four times each service's fourteen-day working-set peak, with a 1 GB floor. | Re-measure every row (E9). Generator and embedder rows also account for the page cache of their roughly 31 GB and 15.1 GB weight files, so those model-sized rows do not shrink just because host RAM does; store rows can. These are ceilings, not an additive host-RAM requirement. |
| OpenSearch heap | `gideon/host/render/opensearch.py` and its service block: half the OpenSearch memory row, rounded down to MiB and capped at 31 GiB; the no-GPU heap is 1024 MiB. | **Self-scaling** with its row on a GPU host. The heap is held at startup; the no-GPU figure is a floor. |
| Store swap ceiling | `gideon/host/render/compose.py`: Qdrant and OpenSearch receive `memswap_limit` one 4 KiB page above `mem_limit`. | **Self-scaling** with each store's memory limit; other services keep Docker's default swap allowance. |
| Available host-memory floor | `gideon/host/checks/capacity.py`: one fifth of host `MemTotal`, judged against `MemAvailable` now and its fourteen-day low. | **Self-scaling** with host RAM; falling below it warns and calls for re-weighing the limits. |
| Chunk ceiling and build refusal | `models.lock` → `chunk_guards`: `serving_ceiling_million_chunks`, `build_refusal_million_chunks`, and `resident_bytes_per_chunk`, each a positive whole number. The pin watch patches each scalar value-only. | Ceiling = Qdrant's RAM row in bytes ÷ resident bytes per chunk, expressed in millions; build refusal = ceiling ÷ 1.5. The loader accepts each guard at or below its formula's whole-number floor. Derive from RAM, never corpus size or scaled down to a smaller corpus; re-measure the per-chunk figure at the space's dimension and precision (E9). |
| Free-space reserve for a new index generation | Specified for `gideon index build`; not implemented yet. | **Self-scaling**: require at least 1.25 times the live generation's measured bytes free. |
| Worker job slots | `gideon/host/render/worker.py`: `WORKER_CONCURRENCY` starts at 4. | A starting value until a bulk build measures one; it does not follow the profile automatically. |
| Frontend worker and file-size limit | `gideon/host/render/services/open_webui.py` runs one frontend worker. The planned per-file limit is 200 MB and has not landed. | The one worker is a recorded decision and does not move with RAM. The planned file limit is a product rule, not a profile value. |
| **Disk: `data_volume_gb`** | `models.lock` → `requires.data_volume_gb`: 4000 decimal GB in the reference, checked against `/data`'s size. | A minimum and a capacity starting value under the corpus footprint rule; correct it from the measured footprint. It follows the corpus, not the hardware. |
| `gideon-ci` sibling memory rows | `gideon/host/render/ci.py`: its own Postgres 1 GB, Qdrant 2 GB, and OpenSearch 6 GB limits, and its engine relay's 128 MiB. | They belong to the build-box sibling, not the production profile; they do not move with it. |

Preflight judges the `requires` minimums and exact matches; render reads the memory table and model-server fractions, while the embedding space stays in the profile for index consumers; `index build` reads the chunk guards and, with preflight and the data-volume alert, the reserve rule.

## 5. `config/egress.yaml`

The allowlist groups destinations by the work that needs them:

- `host-provisioning` serves `host provision`: the operating-system, driver, and Docker packages, the CI runner, the registry image and the images mirrored from Docker Hub and NVIDIA's registry, and the acceptance-machine image.
- `install-upgrade` serves installation and upgrade: model downloads and the images published on GitHub's registry. `models pull` prints any host a download traverses that this group does not cover.
- `corpus` serves corpus acquisition: the hosts the worker may reach, through GIDEON's egress service on port 443 alone, so every corpus `base_url` and `mirror_url` host is in it and every corpus URL is HTTPS. A redirect to a host outside the group is refused, and the egress service's log names that host.
- `image-build` serves the package downloads made while building GIDEON's own images on the build box; an office never builds.

`preflight` probes the `host-provisioning` and `install-upgrade` groups. Allow the listed destinations through the office proxy or firewall.

## 6. `courts.yaml` and its hand table

`courts.yaml` maps each CourtListener court identifier to its circuit, state, level, and name. The site's `jurisdiction` identifiers are looked up here. If an identifier is absent, `preflight` reports the nearest identifier at the required level.

`preflight` also reads the newest committed lockfile's `courts[]`: it refuses a home circuit or district absent from a tier the lockfile carries, and warns when the tier has not shipped or none of a home state's appellate courts is in the lockfile.

The generated map comes from CourtListener's bulk courts CSV and the hand table at `tools/courtmap/geography.yaml`. A maintainer runs `python3 -m tools.courtmap --csv <courts-YYYY-MM-DD.csv.bz2>` by hand; the generator does not fetch data and is never run on a box. The hand table has five parts: state-to-circuit assignments, federal-court assignments, state-court placements, deliberately unplaced codes, and reviewed corrections. A state-coded court not placed by the table is a deliberate generator failure that must be reviewed before the map is regenerated.

If a box refuses `courts.yaml`, restore the file from the installed release tag; do not regenerate it there. If the generator refuses, update the hand table only after reviewing the court's placement, then regenerate and verify the map.

## 7. `config/triggers.yaml`

This file lists improvement triggers, each with a condition and one of three states: `watching`, `acted`, or `retired`. A state changes only through a pull request, so the release history records each decision.

`gideon proposals` reads the registry and reports which watching triggers meet their conditions and which remain unmeasurable. A refusal naming the file is fixed by restoring it from the installed release tag, then running `gideon proposals` again.

## 8. `config/challenger.yaml`

This file names the challenger: at most one configuration experiment, run beside the release's configuration on the build box's CI sibling so both are recorded paired. The entry names the experiment, the subject it changes, the release's value, the challenger's value, and the date and pull request or tag that set it; a null `challenger` means none is set and nothing runs. The file changes only through a pull request. A challenger found better is promoted by a pull request that changes the product's own configuration and this file together, so the challenger never reaches production by itself.

`gideon eval run --challenger --stack ci` reads the file on the build box alone, where the nightly timer runs it after the two nightly suites, a failed pair never failing the timer's unit; `gideon proposals` reads it there too, showing the newest pair's figures and the paired statistic over up to five nights of one release. An office's box receives it and never reads it. A refusal naming the file is fixed by restoring it from the installed release tag; the person changing the file in a release corrects the named key so the `release` value is the one the release runs and the two values name the same kind of setting, then runs the command again.

## 9. The Monday review of pull requests

The maintaining office reviews the open pin-watch and Dependabot pull requests on GitHub each Monday. Read the PR's title for its pin, find its row below, and merge only when the hosted `checks` job is green.

| The pull request | What you do on GitHub |
| --- | --- |
| Dependabot: `ruff`, `mypy`, `pytest`, a GitHub Action | Merge when green. An Action needing a newer runner waits for `host.gh_runner`. A `requirements-dev.txt` bump makes open cycles rebase onto `main` before their gate can pass. |
| Dependabot: `playwright`, `PyYAML` | Leave open for a cycle; each needs a companion change before its checks pass. |
| `images.prometheus`, `grafana`, `node-exporter`, `dcgm-exporter`, `postgres-exporter`, `cadvisor`, `blackbox-exporter` | Merge when green. The observability services move at the next release's upgrade. |
| `images.caddy`, `images.searxng`, `images.vllm-openai`, `images.open-webui`, `models.*` | Leave open for a release cycle. These recreate a service a user's turn uses. A hand merge changes the recreate set of every open cycle; open a bump cycle to move one sooner. A frontend bump goes to the developer; a model bump follows `model-upgrade.md` §2. |
| `images.postgres`, `images.gideon`, and their build arguments | Leave open for a cycle. Checks stay red until the image is rebuilt on the box (`built-images.md` §2). Postgres is user-facing. |
| `images.opensearch` | A new base digest under the same tag merges after the box rebuild (`docs/runbooks/built-images.md §2`). A version move waits for the matching plugin release; the hosted checks stay red until both plugin arguments are typed from that release's page and the image is rebuilt on the box. |
| `host.registry_image`, `host.acceptance_vm_image` | Merge when green. The registry moves at the next provision; the VM image at the next acceptance run. |
| `host.gh_runner` | Merge when green, then provision the box within thirty days to update both its runner instances. |
| `host.driver.branch`, `host.minimums.*` proposals | Leave open until the box runs the new version. Merging first makes preflight and provision refuse until the box is upgraded by `docs/runbooks/install-upgrade.md §9`. Close a version you decide to skip. |
| `skills.matt-pocock` proposal | A maintainer completes the skill refresh on the proposal branch, then merges when green. Nothing on the box moves. |

**Closing declines.** Closing a PR unmerged tells the watch that bump was declined; it proposes nothing else for that pin until upstream moves again. Say why in a closing comment.

**How to merge.** Use **Rebase and merge** to keep `main` linear. The watch's commit subject is the PR title and may be long; keep it. GitHub deletes the branch after the merge when automatic head-branch deletion is enabled, and the watch recreates it when that pin moves again. The decline remains in the closed PR. Afterwards, bring the primary checkout up to date:

```bash
cd ~/src/GIDEON && git pull --ff-only
```

If the merge changed `requirements-dev.txt`, rebuild that checkout's development environment. A release that finds `main` moved during its final fast-forward rebases again.

**When a PR shows a conflict.** Watch branches regenerate the same render fixtures, so one merged bump or a release touching a lock can make the others conflict. Do not resolve a watch branch's conflict by hand: the watch can replace that commit. Let Monday's scheduled watch run rebase open branches onto `main`. If a green PR is urgent, use *Actions* → *pin watch* → *Run workflow*, choose `main`, leave the dry-run option unticked, and run it. From a shell:

```bash
gh workflow run pin-watch.yml -f dry_run=false
```

Merge the next PR when its checks are green again.

**A watch run costs hosted minutes.** It rebases every open watch branch, and each rebase reruns that PR's `checks` job. With many PRs open, extra dispatches can exhaust the organisation's Actions minutes and stop CI. Merge one conflicting PR a week after Monday's scheduled rebase; dispatch between merges only for an urgent green PR. A PR GitHub shows without a conflict can be merged at once, because CI on `main` checks it again.

## 10. `corpus/lockfiles/`

A corpus lockfile pins every byte of the legal corpus a release installs. `corpus-YYYY-MM-DD.yaml` names, for each source, the upstream snapshot's date, its base URL and optional `mirror_url`, the digest of its sidecar, and for case law the courts the corpus covers. The directory beside it holds one `<source>.sha256` sidecar, one `path  sha256  size` line per file, and the index documents the cut read to choose those files. The directory's `README.md` states the rules.

A maintainer writes a lockfile with `gideon corpus cut` on the box, then commits the new files in a release. For a fetched source, the command fetches through the worker anything the box does not already hold; for a captured source, it copies the maintainer's saved page into the snapshot directory. It hashes every file itself, writes the lockfile, and records the cut. A second run against the same state writes nothing and names the lockfile that already pins it. Nobody edits a lockfile or a sidecar by hand. The one exception is a fetched source's `mirror_url`, set when the same pinned files are served from a mirror in the egress allowlist's `corpus` group; every digest and size must still match. A captured source has no mirror.

A weekly timer, `gideon-upstream-watch.timer` on Monday at 06:30 office time, runs `gideon corpus watch`, which a maintainer may also run by hand. It reads each fetched source's index through the worker, as the cut does, and records an `upstream_observations` row: `observed` with the newest snapshot date upstream publishes, or `unanswered` with a reason when the source, the fetch, or the worker did not answer. For a captured source, it records the newest snapshot date already bound to a lockfile on this box, using the page's address as provenance, even when the worker is stopped; if the source is not yet cut or installed here, it prints that fact and writes no observation for it. Its exit is 1 when any source went unanswered. A newest date that no recorded lockfile pins is an upstream notice: the run prints the cut to run, the notice pages once, and `gideon proposals` lists it in its `upstream` section until a cut or an install records a lockfile that pins it. A captured source raises no notice. The watch fetches no corpus file and never cuts; a person runs `gideon corpus cut` in a window they choose. It holds the same corpus lock as the cut, so one waits for the other.

**The treatment table's capture**

Before a full cut, save the Constitution Annotated's [Table of Supreme Court Decisions Overruled by Subsequent Decisions](https://constitution.congress.gov/resources/decisions-overruled/) from a browser. Use **Save Page As → HTML only**, so the saved page is one file. Place it at `corpus/captures/treatment-YYYY-MM-DD.html` in the release checkout on the box, with today's UTC date in the name; create `corpus/captures/` if needed. That directory is never committed.

Then run:

```bash
gideon corpus cut
```

Every full cut takes the newest dated capture. A quarterly cut after a new case-law dump needs a capture dated at or after that dump's snapshot date: save a new page today before running the cut. The cut commits the saved page beside the lockfile as `<label>/treatment.decisions-overruled.html`; that committed copy is what a receiving office installs from. The page's address is provenance and is never fetched by the command.

The command's refusals and their fixes:

- **Another cut is running**: wait for it to finish, then run the command again.
- **A source's host is outside the corpus allowlist**: add the host to the `corpus` group in `config/egress.yaml` in a release, then run the command again.
- **A file disagrees with its fetch record or with its pinned sidecar**: remove the named file and its `.fetch.json` record from the snapshot directory, then run the command again: fetched sources are fetched anew, and captured files are reseeded from the saved page.
- **The label already has a lockfile, or is already recorded**: a label is used once. Run the command on a later UTC date; never delete a lockfile or its record.
- **A committed lockfile does not load**: restore the named files from the release checkout, then run the command again.
- **No treatment capture is present**: save the page in the checkout as the capture step above describes, then run `gideon corpus cut` again.
- **The capture is older than the case-law snapshot**: save a new page today, dated at or after the snapshot date the refusal names, then run `gideon corpus cut` again.
- **The capture is dated before the newest lockfile's, after today, or has different bytes under a date already pinned**: save the page under today's UTC date as the capture step describes, then run `gideon corpus cut` again.
- **The snapshot directory holds other bytes under the capture's date**: save the page under a new date as the capture step describes, then run `gideon corpus cut` again. Keep the existing snapshot fixed.

Stopping the command with Ctrl-C is safe. Downloads already started continue in the worker, and the same command joins them. For adding courts without acquiring another snapshot, follow `docs/runbooks/release-files.md §11`.

After a cut on the maintaining office's box, the maintaining office copies what it pinned to its source archive: the snapshot directory the cut fetched — its files, not the `.fetch.json` records beside them — and the whole `corpus/lockfiles/` directory, one `rclone copy` each from the box into the archive's `corpus/snapshots/<directory>/` and `corpus/lockfiles/`. The archive is TNMD's folder on the office's Box tenant; its location is in the CSAs' records and in no release file, and the rclone remote that reaches it is authorised once in a browser elsewhere as the CSA's own account or as a dedicated account collaborated on the folder alone, scoped to the folder by its `root_folder_id`, and kept in the CSA's home, never in a checkout or a transcript. The snapshot directory is readable by the `gideon` group alone, so the copy runs with that group (`sudo runuser -u <account> -g gideon`). Check every copy against its source with `rclone check --one-way`, which compares Box's SHA1 with the file's and downloads nothing, and read the total with `rclone size`. A file above the tenant's per-file limit (Max File Size in Box's Account Settings) is stored as `split` parts with a line beside them saying so; `cat` of the parts in order restores the file and the sidecar's digest proves it.

A derived or pipeline cut pins the same files, so it adds its lockfile and companion directory alone; a receiving office's derived cut (§11) keeps its lockfile in its own release and sends nothing, the archive being the maintaining office's. A `models.lock` that pins a new revision sends that model's snapshot from the hub cache to the archive's `models/<org>/<name>/<revision>/`, the copy and the check following the cache's symlinks (`--copy-links`). The archive is insurance, never an install source: every office installs from upstream at the pin, and a vanished upstream is republished under the office's organisation and repinned in the next patch release.

`gideon corpus install <label>` takes a committed lockfile onto a box. Before fetching, it checks that the worker, search store, vector store, and embedding server are declared, running, and healthy, and that the site's selected profile in `models.lock` supplies an embed pin and embedding space; the `preconditions` row names that space. It fetches each fetched source's pinned files that the box does not already hold whole, from `mirror_url` where the lockfile sets one. A captured source is copied from the checkout's committed companion into its snapshot directory, with no fetch deferred. Every file's digest and size are checked against the sidecar, and the label is recorded as `installing`. On the box that cut the lockfile the whole copies are kept; a receiving office gets fetched sources from upstream and captured pages from the release. The install keeps each snapshot directory a recorded lockfile still needs — one that is cut, installing, or installed, or the previously installed one — and removes a directory only older superseded lockfiles pin. It holds the same corpus lock as the cut and the watch. The install calls the build after the worker's stages: it clusters the installed record's near-duplicate chunks and builds both halves of an index generation, every chunk carrying its cluster and boilerplate flag, its search index and vector collection named for the lockfile and the embedding space, then materializes each cited case's risk state for the generation and writes its build report — each court's documents by status, precedential status, and text source, its page-anchor coverage and agreement figure, and the run's wall-clock — to the record. `gideon index build caselaw` runs the same build directly, and `gideon index build caselaw --sample` builds the eval sample generation — every gold document of the eval set plus a fill of about 1 % chosen from the lockfile's label — on the `gideon-ci` sibling, never in production. Promotion arrives later.

Before the build, the install stages the case law. The worker reads the snapshot's courts, dockets, opinion clusters, citations, and opinions files — the opinions file once — and writes each lockfile court's rows, unchanged and under each file's header, to `/data/work/<label>/caselaw/<court>/`, with a `stage.json` record beside them naming the inputs and the counts. One line per court gives its counts. This takes about two and a half hours for tranche 1, and the ingest that follows, one worker job per court making each staged opinion a document, took about four hours more on the reference box. While the label is still installing, running the command again finds the record whole and prints `complete; nothing deferred`. Stopping it with Ctrl-C is safe: the stage continues in the worker, and the same command joins it. `/data/work` is derived state outside every backup set.

After the ingest, the install measures how far GIDEON's citation edges agree with CourtListener's citation map, one worker job per court. The first job stages the snapshot's `citation-map` file once for the label, writing each lockfile court's rows, unchanged and under the file's header, to `/data/work/<label>/caselaw/citation-map/<court>.csv` with a `map.json` record beside them; the other courts' jobs wait for it. Each job writes its figure to `/data/work/<label>/caselaw/<court>.agreement.json`. One line per court gives the `ready` documents measured, the pairs GIDEON found and the pairs the map found, the map's rows citing an opinion outside the label's courts, the agreed pairs as a share of each side, and the map-only pairs split into those GIDEON saw but left unresolved and those it missed. The figure is reported and never refuses the install. On a bounded run of 2,000 opinions per court the map's pass took about two minutes and each court's job about four and a half, and over the whole of tranche 1 the stage took about two and a half minutes; a repeat run while the label is still installing finds the map whole and writes the figures again.

After agreement, `collections` prints a verdict for each collection: `unchanged` for the same inputs in a built or live generation, `needs build` for absent inputs or a building or failed generation. Each row names the generation and inputs hash; a summary says how many need a build. The `generation` row records each one as created or found; a found generation keeps its name and gains this label in its lockfiles. An unchanged collection defers no build job. A built generation with no report or a newest report naming a failed stage or lacking treatment data still says `unchanged`, adding `its treatment and report to finish`; the summary counts it separately as `to finish`. It runs only treatment then report, never rebuilding or deferring clustering, lexical, or dense jobs, and keeps the generation and its build timestamps. A live generation is never finished. For each needing a build, `cluster` waits for the near-duplicate clustering, `build` for the lexical job, `counts` reports its documents, chunks, bytes, and timestamp, `dense` waits for the embedding job with progress figures, `built` reports the vectors, counts, timestamp, and state, `treatment` replaces the generation's risk states whole and prints the switches and distribution counts using the lockfile's treatment capture when pinned, and `report` writes the build report to the record, including the treatment distribution when available; a failed stage also writes a report. The search index and vector collection carry the generation's name.

Once every collection's generation is built or live and any required treatment and report finishing succeeds, `installed` moves the label from installing to installed with its timestamp. In that same transaction, only earlier labels standing installing or installed become superseded, their timestamps kept; later labels are left for their own installs. The row names the superseded labels. `clean` removes `/data/work/<label>/` whole, then `retain` keeps or removes snapshots by the rule above. The work directory is outside every backup set; the snapshots and the record remain.

A repeat install still fetches any missing pinned files and verifies every file. A label already installed skips `stage`, `ingest`, `overruled`, and `agreement`, each row saying `is installed; nothing to do`; its installed timestamp is kept. Collection verdicts still run, and `clean` runs on every install, reporting an absent directory when there is nothing to remove. If interrupted during a build, the job continues in the worker and the same command rejoins it; if interrupted during treatment, the next run replaces the generation's states whole; if interrupted after the installed transaction or during cleanup, the repeat run skips the worker stages and finishes removing the directory. After `retain`, a generation standing built gets `Next: run gideon index promote <gen_id> to serve it.`; a live generation gets no Next line. The install never calls promote.

The install's refusals and their fixes:

- **Another corpus command is running**: wait for it to finish, then run the command again.
- **A build service is absent, stopped, or unhealthy**: read the service logs the refusal names, restore the service with `gideon apply`, then run `gideon corpus install <label>` again.
- **The site or model lock is invalid, the profile unknown, or its embed pin or space missing**: restore the matching release files and correct the site's profile as the refusal directs, then run `gideon corpus install <label>` again.
- **The lockfile's pipeline is not registered in this release**: run `gideon corpus cut` for a new label, then install that label.
- **A file on disk is short or disagrees with its sidecar line**: the copy is damaged. Remove the named file and its `.fetch.json` record, then run the command again: fetched sources come from upstream and captured files from the committed copy.
- **A file just fetched disagrees with its sidecar line**: the upstream now serves other bytes under the pinned name. Repin as the directory's README says — a `mirror_url` serving the pinned bytes, or a new cut — then remove the named file and its record, and run the command again.
- **The label is superseded**: install the newest committed label the refusal names.
- **The lockfile's base is not recorded on this box**: a derived lockfile follows its base. Run `corpus install <base>` first, then the command again.
- **A lockfile court is absent from the dump's courts file**: the court map names a court the snapshot lacks. Review `courts.yaml`, then make a new cut.
- **A stage input is missing or its digest is off the pin**: remove the named snapshot file and its `.fetch.json` record, then run the command again, which fetches and verifies it before staging.
- **The stage record disagrees with the lockfile**: remove the named work directory, then run the command again.
- **The worker cannot write the work directory**: run `host provision`, then `apply`, then the command again.
- **The lockfile pins no `citation-map` file**: the label was cut before the map was pinned. Run `corpus cut` to pin it, then install the new label.
- **The agreement's `citation-map` input is missing or off the pin**: remove the named snapshot file and its `.fetch.json` record, then run the command again, which fetches and verifies it.
- **An agreement job fails as `invalid` or `malformed`**: the staged map directory disagrees with the stage, or a staged file or the map's record is damaged. Read the worker's logs the refusal names; removing `/data/work/<label>/caselaw/citation-map/` lets the next run stage the map again.
- **The matching generation is retired**: make a new lockfile with `gideon corpus cut` and install it, or remove the retired generation with `gideon index gc` once its hold ends. It is never rebuilt in place.
- **A lexical or dense build fails**: follow its reason's fix and the named worker or store logs, then run `gideon corpus install <label>` again to resume. The label remains installing.
- **A generation is missing or not built or live at the installed move**: run `gideon corpus install <label>` again; the label stays installing until all its generations finish.
- **The installed move finds the label missing or still cut**: run `gideon corpus install <label>` again to record it before the move. A superseded label requires the newest committed label instead.
- **The work root cannot be listed or the label's directory removed**: restore access to the path the refusal names, then run `gideon corpus install <label>` again. The installed move is kept and cleanup runs again.

## 11. Adding courts: the derived cut

A receiving office uses a derived cut to add its state's courts when the newest committed lockfile does not cover them. The maintaining office uses the same command for its own later tranches. Run it on the box from the release checkout, using an earlier committed lockfile as the base. The base must also have been cut or installed on this box.

Find candidate court IDs in `courts.yaml` by state code and by the `state_supreme` and `state_appellate` levels. For Tennessee, run this from the checkout; replace `TN` for another state:

```bash
grep -E 'state: TN, level: state_(supreme|appellate)' courts.yaml
```

The Tennessee entries are `tenn` (supreme) and `tenncrimapp`, `tennctapp`, and `tennsuperct` (appellate). CourtListener's table also carries courts long gone, such as `tennsuperct`, a nineteenth-century court; the office decides which of its state's courts belong in this cut. Use the ID before each colon, separated by commas, with no state shorthand.

From the checkout, run:

```bash
gideon corpus cut --base <label> --add-courts <ids>
```

With `gideon` on the path, the same command is:

```bash
gideon corpus cut --base <label> --add-courts <ids>
```

For example, `--add-courts tenn,tenncrimapp,tennctapp` names three Tennessee courts. The command extends the base's `courts[]`, sorts the IDs, and writes a new `corpus-YYYY-MM-DD.yaml` with `base: <label>` and `reason: tranche`. It copies the base's source pins, sidecars, and index documents byte for byte into the new lockfile and its companion directory, then records the new cut. It downloads nothing and does not ask the worker to fetch or verify files. The base stays fixed. Commit the new lockfile and companion files in the office's own release, then run `corpus install <new label>` from that release. On a box that never recorded the base, `corpus install <base>` comes first: the install refuses with that fix before it fetches anything.

If the command refuses:

- **Only one flag was supplied**: give both `--base <label>` and `--add-courts <ids>` in the same `corpus cut` command.
- **The base label is malformed or unknown**: choose a committed `corpus-YYYY-MM-DD` label under `corpus/lockfiles/`, then run the command again.
- **The base lacks a source that lockfiles from its joined date carry**: run a full `gideon corpus cut` first, following the capture step in section 10, then derive from its new label.
- **An ID is empty, repeated, or unknown**: correct the comma-separated list using `courts.yaml`; an unknown ID's refusal names the nearest ID. Run the command again.
- **An ID is already in the base**: choose an ID absent from that base's `courts[]`, then run the command again.
- **The base is not recorded on this box**: run `corpus install <base label>`, then run the cut again. If its record disagrees with its lockfile, restore the matching lockfile from the release checkout first.
- **Today's label already has a lockfile or record**: run the cut on a later UTC date. A label is used once; do not delete its files or record.
- **A newer lockfile already pins the requested state**: the command names it as `unchanged` and writes no files. Use that label; the record stage records it if needed.
- **A committed base is missing or dated after its derived lockfile**: restore the lockfiles and companions from the release checkout, then run the command again.

Ctrl-C is safe: the lock is released, and a repeat run records a lockfile written before the interruption without rewriting it.

After a total-loss recovery by `docs/runbooks/backup-restore.md §5`, the corpus record returns with the restored set. If the release's newest committed lockfile already carries the office's courts, install that lockfile with `corpus install <label>`. If the release lacks the office's derived lockfile, run this cut again from the same committed base with the same IDs on a later UTC date; it writes the same pins and courts under a new label.

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

The hardware profiles contain the minimum host requirements checked by `preflight`. Each profile selects models by repository and revision, records the digest and size of every file to fetch, assigns each model to a GPU, and carries the engine settings used to serve it. The profile's memory table gives each known Compose service its memory limit and names the model role for services that load a model.

A profile may also name its embedding space: an id, the role of the model that embeds into it, and the dimension. Every stored vector belongs to that space, so a person moves the id in the same change whenever the embedding model, any of its weight files, the dimension, or the precision changes; the id is lowercase words and digits joined by single hyphens, and the role must be one of the profile's models.

`models pull` fetches and verifies exactly the files named by the selected profile. It does not select additional files from a model repository.

On the build box the lock may also carry a `candidates:` map of A/B weights. Each entry is keyed by a lowercase hyphenated name that is no profile's model role, and holds `repo`, `revision`, `serve`, and `files` as a profile's model does, but no `gpu`, plus the `role` it stands in for. `models pull <candidate>` pulls the selected profile's files and then that candidate's. The bare `models pull` keeps a candidate while its block is in the lock and removes its files once the block is deleted. A released lock carries no candidate.

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
| `images.opensearch` | A new digest under the same tag (upstream refreshes a version's operating-system packages): merge when green. A new version: leave open for a release cycle. |
| `host.registry_image`, `host.acceptance_vm_image` | Merge when green. The registry moves at the next provision; the VM image at the next acceptance run. |
| `host.gh_runner` | Merge when green, then provision the box within thirty days to update its runner. |
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

A maintainer writes a lockfile with `sudo python3 -m gideon corpus cut` on the box, then commits the new files in a release. The command fetches through the worker anything the box does not already hold, hashes every file itself, writes the lockfile, and records the cut. A second run against the same upstream state writes nothing and names the lockfile that already pins it. Nobody edits a lockfile or a sidecar by hand. The one exception is `mirror_url`, set when the same pinned files are served from a mirror in the egress allowlist's `corpus` group; every digest and size must still match.

A weekly timer, `gideon-upstream-watch.timer` on Monday at 06:30 office time, runs `sudo python3 -m gideon corpus watch`, which a maintainer may also run by hand. It reads each corpus source's index through the worker, as the cut does, and records one `upstream_observations` row per source per run: `observed` with the newest snapshot date upstream publishes, or `unanswered` with a reason when the source, the fetch, or the worker did not answer. Its exit is 1 when any source went unanswered. A newest date that no recorded lockfile pins is an upstream notice: the run prints the cut to run, the notice pages once, and `gideon proposals` lists it in its `upstream` section until a cut or an install records a lockfile that pins it. The watch fetches no corpus file and never cuts; a person runs `corpus cut` in a window they choose. It holds the same corpus lock as the cut, so one waits for the other.

The command's refusals and their fixes:

- **Another cut is running**: wait for it to finish, then run the command again.
- **A source's host is outside the corpus allowlist**: add the host to the `corpus` group in `config/egress.yaml` in a release, then run the command again.
- **A file disagrees with its fetch record or with its pinned sidecar**: remove the named file and its `.fetch.json` record from the snapshot directory, then run the command again, which fetches it anew.
- **The label already has a lockfile, or is already recorded**: a label is used once. Run the command on a later UTC date; never delete a lockfile or its record.
- **A committed lockfile does not load**: restore the named files from the release checkout, then run the command again.

Stopping the command with Ctrl-C is safe. Downloads already started continue in the worker, and the same command joins them.

`sudo python3 -m gideon corpus install <label>` takes a committed lockfile onto a box. It fetches through the worker every pinned file the box does not already hold whole, from `mirror_url` where the lockfile sets one, checks every file's digest and size against the lockfile's sidecar, and records the label as `installing`. On the box that cut the lockfile nothing is fetched; on a receiving office's box the whole corpus comes through the door. It then keeps each snapshot directory a recorded lockfile still needs — one that is cut, installing, or installed, or the previously installed one — and removes a directory only older superseded lockfiles pin. It holds the same corpus lock as the cut and the watch. Its build arrives with slice 3's later releases.

Before that, the install stages the case law. The worker reads the snapshot's courts, dockets, opinion clusters, citations, and opinions files — the opinions file once — and writes each lockfile court's rows, unchanged and under each file's header, to `/data/work/<label>/caselaw/<court>/`, with a `stage.json` record beside them naming the inputs and the counts. One line per court gives its counts. This takes about two and a half hours for tranche 1. Running the command again finds the record whole and prints `complete; nothing deferred`. Stopping it with Ctrl-C is safe: the stage continues in the worker, and the same command joins it. `/data/work` is derived state outside every backup set.

The install's refusals and their fixes:

- **Another corpus command is running**: wait for it to finish, then run the command again.
- **A file on disk is short or disagrees with its sidecar line**: the copy is damaged. Remove the named file and its `.fetch.json` record, then run the command again, which fetches it anew.
- **A file just fetched disagrees with its sidecar line**: the upstream now serves other bytes under the pinned name. Repin as the directory's README says — a `mirror_url` serving the pinned bytes, or a new cut — then remove the named file and its record, and run the command again.
- **The label is superseded**: install the newest committed label the refusal names.
- **A lockfile court is absent from the dump's courts file**: the court map names a court the snapshot lacks. Review `courts.yaml`, then make a new cut.
- **A stage input is missing or its digest is off the pin**: remove the named snapshot file and its `.fetch.json` record, then run the command again, which fetches and verifies it before staging.
- **The stage record disagrees with the lockfile**: remove the named work directory, then run the command again.
- **The worker cannot write the work directory**: run `host provision`, then `apply`, then the command again.

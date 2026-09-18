# The pin watch: GitHub App setup and the weekly review (ticket 10)

The pin watch (ADR-0031, `.github/workflows/pin-watch.yml`) opens one pull request per pin bump. It needs an identity whose pull requests trigger the hosted checks: the workflow's own `GITHUB_TOKEN` cannot (GitHub suppresses `pull_request` runs for PRs it creates), and a person's token expires and follows the person. The identity is therefore a **GitHub App owned by the org**, created once by a CSA. Nothing here touches the box.

## 1. Create the App (once, ~10 minutes)

1. GitHub → the **TNMD-FDO** organization → *Settings* → *Developer settings* → *GitHub Apps* → **New GitHub App**.
2. *GitHub App name*: `gideon-pin-watch`. *Homepage URL*: `https://github.com/TNMD-FDO/GIDEON-dev`. *Description*: "Opens one pull request per release-pin bump (ADR-0031)."
3. *Webhook*: untick **Active** (the watch runs on a schedule; it receives nothing).
4. *Repository permissions*: **Contents → Read and write** (push the `pin-watch/*` branches), **Pull requests → Read and write** (open, edit, close). *Metadata → Read* is added automatically. Nothing else — no organization or account permissions.
5. *Where can this GitHub App be installed?*: **Only on this account**.
6. **Create GitHub App**. On the App's *General* page note the **Client ID** (the token action takes the Client ID; the numeric App ID beside it is not used).
7. *Private keys* → **Generate a private key**. A `.pem` file downloads. Store it in the **office password manager** beside the minisign key (spec §2.2), with the Client ID; delete the download afterwards.
8. *Install App* (left menu) → **TNMD-FDO** → *Only select repositories* → **GIDEON** → **Install**.

## 2. Hand the workflow its credentials

Repository `TNMD-FDO/GIDEON-dev` → *Settings* → *Secrets and variables* → *Actions*:

- *Variables* → **New repository variable**: `PIN_WATCH_APP_CLIENT_ID` = the Client ID.
- *Secrets* → **New repository secret**: `PIN_WATCH_APP_PRIVATE_KEY` = the full contents of the `.pem` file (both `-----BEGIN…` and `-----END…` lines included).

The workflow mints a one-hour installation token from these at every run (`actions/create-github-app-token@v3`, scoped to this repository and narrowed to contents and pull-requests write), checks the repo out with it, and hands it to `gh` as `GH_TOKEN`. The private key never leaves the secret; the token never appears in a log or a row.

## 3. Verify

*Actions* → **pin-watch** → *Run workflow* → tick **dry_run** → *Run workflow*. The job prints one row per pin (`current`, `would open`, …) and writes nothing. Then run it again without `dry_run`: every bump gets a `pin-watch/<pin-id>` branch and a pull request authored by `gideon-pin-watch[bot]`, and the hosted `checks` job runs on each. A second run with nothing new reports `unchanged` or `current` and opens nothing.

From the dev seat, `python3 -m tools.pinwatch --dry-run` does the same read-only pass with the seat's own `gh` login.

## 4. Rotate the key

App page → *Private keys* → **Generate a private key** → update `PIN_WATCH_APP_PRIVATE_KEY` → **Delete** the old key on the App page → replace the copy in the password manager. Nothing else changes.

## 5. The weekly review (the §19.6 calendar line)

**Monday: review the open `pin-watch/*` pull requests.** The watch runs Monday morning before the working day. For each open PR:

- **Merge** it when its hosted checks are green and the bump is wanted. A merge to `main` runs the box's `mirror-images` and `frontend-contract` jobs (for an image pin) — the §17.5 contract gate — and the running stack moves only at the next `gideon apply`. A human then tags the release (§2.1); the tag's release note names what was bumped.
- **Close** it unmerged when the bump is not wanted. The watch treats a closed, unmerged PR with the same bump as *declined* and reopens nothing until upstream moves again. (A PR the watch closed itself carries `[withdrawn]` in its title — upstream withdrew the release.)
- A **proposal** PR — the driver branch, a Docker/Compose/toolkit floor, or an image whose leading version component changes (Postgres `18` → `19`, whose data directory the Compose model pins) — is merged only after its on-box converge or upgrade plan; until then it stays open as the record that upstream moved.
- A **built-pin proposal** (`images.postgres` — the stock base moved; `images.postgres.build_args.PGBACKREST_VERSION` — PGDG published a newer pgBackRest) is one the watch cannot complete: its hosted checks stay red on purpose (the lock's `inputs_digest` no longer matches) until a CSA rebuilds on the box per [`built-images.md`](built-images.md) §2 and commits the recorded digest on the branch. The watch then reports the branch `completed` and leaves it alone; merge when green.

- A **model bump** (`models.<role>` — the pinned repository's default branch moved past the pinned revision; slice-0 ticket 14) carries the new revision and the `files` block regenerated from the Hub, with the render fixtures regenerated beside them. Merge it like an image bump: nothing moves on a box until its next `apply`, whose `models` stage fetches and verifies the new files and recreates the engine — a maintenance window from go-live (§21) — and `engine verify` gates the new weights. A **model proposal** is the same bump when the role's total pinned bytes changed and a memory row names the role (§7.6): complete it **on the watch's branch** — re-judge the row's `gb`, run `python3 tests/regenerate_render_fixtures.py` (every fixture manifest embeds the lock's digest), and commit the lock and the fixtures together. Once the branch carries a person's commit the watch reports it `completed` and never pushes to it again, naming a newer upstream revision in its row when one appears; that revision becomes a fresh proposal after the merge.

- A **`host.gh_runner`** PR (a new runner release) is merged like an image bump; then, on the box, bring the checkout that runs provision to the merged commit and run `sudo python3 -m gideon host provision` — the runner has automatic updates off (ADR-0031), so this is its only upgrade path, and GitHub queues a runner no jobs past thirty days of a release (a critical security update at once): [`office-services-setup.md`](office-services-setup.md) §6.

- A **skill-source proposal** (`skills.matt-pocock` — `mattpocock/skills` `main` moved; ADR-0033, ADR-0041) bumps only the record (the provenance line in `docs/agents/tooling.md` §2) and never a skill: a CSA completes it **on the watch's branch** per that document's §3. The proposal is green on the hosted checks from the start, so the recipe's clone check is the guard. Once the branch carries a person's commits the watch never pushes to it again: it keeps the pull request's title to what the branch holds and, when upstream has moved on since, says so in its row — merge the completion as it is, or complete again on the same branch.

- **Dependabot proposals** (labelled `dependencies`) are reviewed beside the pin watch's: a `ruff`, `mypy`, or `pytest` bump merges when hosted checks are green; a PyYAML bump is red until `pin-watch.yml`'s install line moves with it, and a Playwright bump until the constant and the box's headless shell move, each completed on Dependabot's branch. An action-major bump merges when green; one whose runtime needs a newer runner waits for the pin watch's `host.gh_runner` pull request first.

Every pull request's body ends with the research notes verified against its pin (each note's front matter under `docs/research/`; `python3 -m tools.pinwatch.notes` lists them per pin): read them against the proposed version before merging, and land an amendment as a `docs:` commit on `main`, never on the watch's branch. A pull request that already exists is kept current by the watch (rebased onto `main`, its digest refreshed): a skill-record or model branch a person committed to and a built-pin branch carrying a recorded rebuild are left alone, and any other commit on a watch branch is force-pushed over at the next run; a PR is never auto-merged.

**Also on Monday: confirm the collaborator list is still the two CSAs.** `gh api repos/TNMD-FDO/GIDEON-dev/collaborators --jq '.[] | {login, role_name}'` lists two accounts, both `admin`, and no team. That list is the whole control over who may create a `v*` tag on the development repository, whose tags trigger `acceptance.yml`, since the organisation's Free plan offers no ruleset to a private repository (`.scratch/slice-0/issues/22-public-repository.md`, ruled 2026-09-07); the `v*` ruleset is the public `TNMD-FDO/GIDEON`'s (slice-1 ticket 57); a third account with write access is the trigger to revisit that ticket, not something to grant and carry.

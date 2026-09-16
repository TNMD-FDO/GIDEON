# Development tooling: what is shared, what is personal, how it upgrades

The repository is the only source of the development lifecycle: a clone gives every
CSA the same skills, agents, hooks, and rules, and personal preference lives in home
directories and gitignored files, where it can change nothing the lifecycle depends on.
CLAUDE.md carries the rules an agent follows in every session; this file is the manifest
and the recipe.

## 1. Layers

| Layer | Where | Committed |
|---|---|---|
| Lifecycle tooling, identical for everyone | `.claude/skills/`, `.claude/agents/`, `.claude/settings.json`, `.claude/hooks/`, `CLAUDE.md`, `skills-lock.json`, `docs/agents/`, `tools/`, `bin/` | Yes |
| Personal preference | `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/`, `~/.claude/agents/`, `<repo>/.claude/settings.local.json`, `<repo>/CLAUDE.local.md` | Never (the last two are gitignored) |
| Per-machine credentials and binaries | Claude Code login, the Codex CLI and its login, `gh auth`, the `.venv` | No (see §4) |

Precedence facts these layers rely on (Claude Code docs, checked 2026-09-02):

- Settings merge; `permissions.allow` and `deny` lists are unions across levels, so a
  personal file can only widen, never remove, a shared rule.
- Hooks in `.claude/settings.json` run for every clone after the one-time workspace
  trust prompt.
- `.claude/settings.json` sets `includeCoAuthoredBy: false`; the `Claude-Session:` link
  has no setting and is omitted by CLAUDE.md's "Commit messages" rule. Commits carry the
  message alone.
- `.claude/settings.json` sets `worktree.baseRef` to `head`, so a worktree the harness
  creates on its own (the Agent tool's `isolation: worktree`, an `EnterWorktree` by name)
  branches from the local `HEAD`, never from `origin`'s default branch (workflow ticket
  09). A cycle's own worktree is `bin/worktree-claim`'s, from `main`, per CLAUDE.md's
  "Two sessions, one checkout".
- A personal skill in `~/.claude/skills/<name>` **overrides** a repo skill of the same
  name; agents are the other way round (`.claude/agents/` wins). The session-start hook
  `.claude/hooks/check-skill-shadowing.sh` warns when a personal skill shadows a repo
  skill.

## 2. Vendored and project-owned skills

`.claude/skills/` holds two kinds of skill. The 25 Matt Pocock skills are **vendored**:
the entries in `skills-lock.json`, installed from `mattpocock/skills` at the head of
upstream `main` (its tags lag the branch), refreshed whole by §3, and never edited — a
convention for one lives in a project-owned document or in the file the skill edits.
The lock's `computedHash` is the SHA-256 the `skills` CLI computes over the upstream
folder (sorted relative paths and contents), and `tests/test_skills_vendored.py` fails
an edit as drift (§5). The TRIP and Codex skills (`TRIP-*`, `codex-*`) and `bootstrap`
are **project-owned**, edited like any document: the TRIP set was forked from
`PiLastDigit/TRIP-workflow` v2.8.0 at the lifecycle simplification (ADR-0041, workflow
ticket 42), the pristine bytes being the `vendor:` commits in this repository's history;
when upstream publishes a release, its diff is read and what helps is taken by hand as
a docs commit, never merged.

<!-- The pin watch and the tests read this machine-readable line; keep it on one line. -->
Provenance today: Matt Pocock skills at upstream `main` commit `6654f6b` (2026-08-24).

The pin watch patches the line's commit and date when `mattpocock/skills` moves (§5).

## 3. Refreshing the Matt Pocock skills

They carry no delta, so the CLI is safe for them — but it installs the head of `main`,
never a commit (it clones with `--branch`), so the record is the head you read and the
lock's hashes are the proof. On the watch's `pin-watch/skills.matt-pocock` branch when its
pull request is open (the provenance line already patched there), else on your own:

1. Read the head: `gh api repos/mattpocock/skills/commits/main --jq .sha` (the pull
   request body's upstream URL carries the same full sha).
2. Install:
   ```bash
   DO_NOT_TRACK=1 npx -y skills@1.5.23 add mattpocock/skills -a claude-code -y --skill <names from skills-lock.json>
   ```
   Their names are lowercase, so no directory move is needed.
3. Prove the install: `git clone https://github.com/mattpocock/skills <clone> && git -C
   <clone> checkout <sha>`, then `python3 -m tools.pinwatch.skills --source
   mattpocock/skills <clone>` — every entry `match`. A `differs` means `main` moved
   between the read and the install: read again and repeat.
4. Record the sha that matched in §2's provenance line (already there when the head did
   not move since the watch ran; corrected by hand when it did — the watch then retitles
   its pull request to what the branch holds and leaves the branch alone), and commit as
   `vendor: Matt Pocock skills at main@<sha>`.
5. Merge when the hosted checks are green: the watch's pull request when there is one
   (the watch reports the branch `completed` and never pushes to it again), else a pull
   request from your branch, fast-forwarded to `main`.

Do not run a bare `npx skills update`: at project scope it re-runs `add` for **every**
lock entry with a `skillPath` and overwrites the working tree without comparing
anything. `npx skills update <name>` with explicit names is fine.

## 4. A new CSA

`git clone` delivers the lifecycle and [`onboarding.md`](onboarding.md) the method. The
per-machine remainder, none of it in the repo:

- Claude Code, logged in; accept the workspace trust prompt so the shared hook runs.
- The Codex CLI, logged in (`.scratch/bootstrap/issues/01-dev-seat-sudo-posture.md` and
  the wizard assets under `.scratch/greenfield-spec/assets/` cover the seat).
- `gh auth login` for the GitHub org.
- The pinned toolchain in a fresh `.venv` (`docs/4-unit-tests/TESTING.md`).
- Personal taste goes in `~/.claude/` or `CLAUDE.local.md`; a personal skill never
  reuses a name from `.claude/skills/`.

## 5. Staying current, catching drift

- **Noticing** (ADR-0033, ADR-0041, `tools/pinwatch/`): the pin watch carries one
  proposal-only pin for the skill source, `skills.matt-pocock` — the head of
  `mattpocock/skills` `main` against the commit in §2's provenance line. A proposal
  bumps the record only, never a skill: the commit and date in the provenance line. A
  person completes it on the watch's branch by §3; a branch carrying a person's commits
  is never pushed to again — the watch keeps the pull request's title to what the branch
  holds and says in its row when upstream has moved on since. The proposal is green on
  the hosted checks from the start, so §3's clone check is the guard.
- **Drift** (`tests/test_skills_vendored.py`, in the hosted `checks` job): every lock
  entry has its directory; every entry's folder hash, recomputed with the CLI's
  algorithm — files only, `.git` and `node_modules` skipped, paths in ICU order, path
  bytes then content bytes — equals `computedHash`, so an edit to a vendored skill or a
  lock hash rewritten to an edited folder is red; no `[ADAPT_TO_PROJECT` or
  `[PROJECT_NAME]` placeholder; no conflict marker; every `scripts/*.sh` executable
  (`*.template.sh` exempt — upstream ships those at 644); no two skill directories
  differing only by case. The checker of §3 step 3 (`python3 -m tools.pinwatch.skills`)
  hashes installed folders with the same algorithm, its statuses named there.

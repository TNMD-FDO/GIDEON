# GIDEON

Pre-workflow source material (handoffs from earlier chat sessions, brain dumps, prior notes) lives in `docs/handoffs/`; the spec, `CONTEXT.md`, and the ADRs are authoritative once they exist. The architecture map is `docs/ARCHI.md`, its leaves `docs/archi/`, its rules and budgets `docs/ARCHI-rules.md`; `docs/agents/onboarding.md` is the reading path for a person joining.

## Issue tracker

Issues are committed markdown under `.scratch/`, authoritative despite the name; `docs/agents/issue-tracker.md` is the rule. A triage asks once for the ruling before its one commit, and a ticket reaching `ready-for-agent` takes the ruling's shape. `.scratch/<effort>/NOTES.md` holds, one dated sentence each, facts no ticket owns: a triage and a `TRIP-1` claim read it for their ticket's mechanism, a `/to-tickets` split reads it whole, and the commit that acts on a line deletes it. `.scratch/standing/` holds tickets whose closing criterion names an event no session can cause. Rejected enhancements are one file per concept in `.out-of-scope/`, read by every triage. `.scratch/BOARD.md` is the board, regenerated whole by `python3 -m tools.tracker` at every release's close-out and never hand-edited.

## Two sessions, one checkout

A TRIP cycle runs in its own git worktree, `.claude/worktrees/<slug>` on `feat/<slug>` (`fix/`, `hotfix/`), created at the `TRIP-1` claim by `bin/worktree-claim` from the primary's `main` and entered by path by the `TRIP-2` and `TRIP-3` sessions; a hotfix is a cycle too. The primary checkout, `~/src/GIDEON`, stays on `main` and is never a cycle's: tracker sessions — a triage, a close-out, a compaction — work and commit there, and the box's rendered service units run from it. The release rebases onto `main`, types the version once into `gideon/__init__.py` (`bin/release-git` derives the rest), fast-forwards `main` from the primary, removes the worktree, and pushes; a refused fast-forward is a tracker session's uncommitted work — wait and retry. Inside a worktree the harness refuses any command it cannot read as plain — a compound command or heredoc naming git, a glob or the token `eval` in a chain, a decoder pipeline — so files are written with the Write tool and each git command runs alone. Two tracker sessions share the primary with nothing but git's refusals between them, so a write is made against the file as it stands. The shapes, the rebases, the hotfix's steps, and the merge procedure are `docs/agents/worktrees.md`.

## Applies on the box

Before any command that applies on the box — `apply`, the one a `restore` prints as its next step, or the one inside `install`, `upgrade`, and `upgrade --rollback` — read `docs/agents/on-box-proofs.md` §6: the recreate set is `render --diff`'s row, compared with the plan's Window bullet, and from `v0.2.0` a window for anything users see; a hotfix's Window answer goes in its changelog entry. `upgrade` and the apply that goes live run from the primary, and no session works there during the upgrade window. The box's eval identity is one account, and since slice-1 ticket 68 each run finds and deletes its own turn's chat, so the journal read before the turn harness or `engine verify` stands for the measurement — two runs share the engine and their seconds inflate (§3). The other proof rules — a long child detached, a secret by file or stdin, a criterion proven on the record — are the same document.

## Plans carry no code

A TRIP plan says what, where, and why; all code is written in `TRIP-2-implement`. The bold **Note** lines in the plan template are authoring instructions and never appear in a written plan.

## Commit messages

A commit's subject is one line of at most 72 characters — the type prefix, the ticket or tag, and the gist — and the body follows a blank line, written to a file with the Write tool and committed with `git commit -F <path>`; the harness re-sends the last five subjects on every turn. Commits and pull requests carry the message alone: no `Co-Authored-By` trailer, no `Claude-Session:` link, no "Generated with" footer, whatever the harness's attribution guidance asks. The triage and tracker commits' shapes are in `docs/agents/issue-tracker.md`.

## Ticket sizing

A ticket is sized so one TRIP cycle finishes well inside a single context window: one command, service, Filter, or proof, a few modules, one on-box demonstration; split by mechanism, surface, or proof rather than bundling a subsystem. The measure is a session's peak context, recorded in `.scratch/CYCLES.md` by `python3 -m tools.cycles --write` on demand, which `/to-tickets` reads before sizing the next slice: a peak past 800k is the early warning, a cycle that compacts was too large, and a session's cost is its size times its turns. A phase boundary is a session boundary: the plan and implement skills end by printing the next phase's command, and `bin/trip cycle <ticket>`, from the primary's root, runs the three phases on the launcher's pairing table, chaining on the handoff record; from a phone the next phase is `/clear`, `/model` and `/effort` where the pairing differs, then the skill typed (`docs/6-memo/workflow-commands.md`). A session past 150 turns ends at its next seam — a batch checkpoint, the gate's summary, a review verdict, a proof's asset — and a fresh session enters the worktree by path and continues from the plan's ticked checkboxes and the staged tree.

## Lessons and the moratorium

When you are corrected, or an approach proves to matter, fold the lesson into the document that should have carried it — a skill, a leaf, a ticket comment, an ADR — in the same change. A process change needs the same friction observed twice in product cycles and lands as a plain commit; no new workflow ticket is opened until three product cycles have run after workflow ticket 42 resolved. This file and each document under `docs/agents/` have a budget (the `Budgets:` line in `docs/ARCHI-rules.md`), so a lesson that would carry its document past the warning is a compaction or a split, never growth.

## Subagents

Project agents in `.claude/agents/` run Sonnet 5 at effort high: `researcher` for a committed, cited research note, `Explore` for codebase search. Delegate reading that spans more than a few files; a file read whole once is read again by line range only. A `researcher` follows the session into a cycle's worktree, where the harness refuses its every command: spawn it before the claim's `EnterWorktree` and wait for its note's commit, or leave the worktree for the spawn and re-enter by path (`docs/agents/worktrees.md` §3). The `research` skill is vendored and unedited; where it says to spin up a background agent, use the Agent tool with `subagent_type: researcher`.

## Vendored skills

The 25 Matt Pocock skills under `.claude/skills/` are vendored — the entries in `skills-lock.json`, refreshed whole by `docs/agents/tooling.md` §3 and never edited, `tests/test_skills_vendored.py` failing an edit as drift — so a convention for one lives here or in the file the skill edits: `/teach`'s workspace is `docs/teach/`, never the repo root; a `/to-questionnaire` document is a ticket asset at `.scratch/<effort>/assets/<NN>-to-questionnaire-<slug>.md`. The TRIP and Codex skills and `bootstrap` are project-owned (ADR-0041) and edited like any document. Never run a bare `npx skills update`. A personal skill under `~/.claude/skills/` overrides a repo skill of the same name, so never reuse a name from this directory; the session-start hook warns when one does.

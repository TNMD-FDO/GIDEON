# Worktrees: a cycle's worktree and the primary checkout

**Read this when** a session is about to claim a ticket, enter a cycle's worktree, run a command or spawn a `researcher` inside one, rebase onto a `main` that moved, merge a release or a hotfix, run anything on the box from a worktree, or write in the primary while another tracker session shares it. Each section is a rule with its shape; its `_Origin_` line names the tickets and tags whose records hold the incidents. CLAUDE.md's "Two sessions, one checkout" and "Subagents" sections carry the rules and point here; the three TRIP skills carry the commands and point at §1, §2, and §8; `docs/agents/on-box-proofs.md` §7 carries the worktree apply. Workflow ticket 09 holds the record.

## 1. The cycle's worktree; the primary on `main`

A TRIP cycle runs in its own git worktree, `.claude/worktrees/<slug>` on `feat/<slug>` (`fix/`, `hotfix/`), `<slug>` the ticket file's stem without its number, created at the `TRIP-1` claim by `bin/worktree-claim <slug> [feat|fix|hotfix]` from the primary's `main` with a link to the primary's `.venv`, the path printed last for the `EnterWorktree` that follows, and entered by path by the `TRIP-2` and `TRIP-3` sessions. A worktree already there for the branch (a plan session resumed) the script refuses and names, and the session enters it and keeps it at its exit prompt, since only the release's merge removes one. The worktree holds the cycle's files alone, so the checkpoint sweep is unchanged. The primary checkout, `~/src/GIDEON`, stays on `main` and is never a cycle's: tracker sessions — a triage, an outstanding-items step, an audit — work and commit there, and the box's rendered service units run from it (the nightly backup, the users reconcile). The release fast-forwards `main` from the primary, removes the worktree, deletes the branch, and pushes from the primary; a refused fast-forward is §8's.

_Origin_: workflow ticket 09's docs commit after `v0.1.25`.

## 2. The worktree guard: what it refuses, and the shapes that pass

The harness refuses a worktree session's git command aimed at another tree, and any construct it cannot read as a plain command: a compound command or heredoc that names git, an `awk` program, a value computed at runtime where an option may stand (the release skill's Step 3 `key.sh` substitution is one; the review's start script prints the state file's path, read by that literal path), a pipeline feeding an interpreter or a decoder, a here-document into an exported variable, a glob operand anywhere in a chain that also runs `python -m`, the token `eval` anywhere in a command line (a path under `eval/` included), and under `sudo` a redirection, a `;` chain, an `env` prefix, a glob, a `{{…}}` format, or the word `read`.

So the session leaves the worktree first, or while inside writes files with the Write tool and runs plain commands, each git command alone. The shapes that pass: a fixed script under `bin/` as one plain command (`bin/release-git` for the release's tail, `bin/worktree-claim` for the claim); each on-box step as one plain `sudo python3 -m gideon <command>` or `sudo bash <fixed script>` in the scratchpad writing its own log; a detached child as `nohup setsid bash <fixed script>` in place of a `bash -c` string (`on-box-proofs.md` §1); the ledger's fetch as `gh api` with the raw `Accept` header into a file in place of `docs/box-ledger.md` §11's decoder pipeline; a rebase's conflicts resolved by a script file in the scratchpad run as `python3 <path>`. A worktree's test suite runs from inside the worktree after the release's rebase: from the primary, `pytest --rootdir=<worktree>` imports the primary's package, and a test comparing a module's path with the test file's root fails falsely. Everything else in a cycle runs there too: Codex's batches and reviews, the checkpoint's `git add -A`, the gate through the `.venv` link, and an on-box proof's commands.

_Origin_: `v0.1.26` through `v0.1.31`, `v0.1.45`; workflow ticket 32 (the two scripts).

## 3. A `researcher` follows the session into the worktree

A `researcher` spawned while the session sits in a cycle's worktree inherits the worktree as its working directory, and the harness refuses every command it runs there, so the note it writes reaches the scratchpad and never its branch. Spawn it before the claim's `EnterWorktree` and wait for its note's commit before entering, since an agent still running when the session enters follows it there; or leave the worktree with `ExitWorktree` (keeping it) for the spawn and for the note's commit and push from the primary, then re-enter by path. The recovery for a note stranded in the scratchpad is to copy it onto the cycle's branch by hand under `docs/research/`, with no `research/` branch.

_Origin_: `v0.1.35`, `v0.1.42`.

## 4. A `main` that moved: the rebase before the version, the rebase mid-cycle, no hash of the branch

A release whose `main` carries another release since the claim (`git merge-base --is-ancestor main HEAD` false in the worktree) rebases onto it before choosing the version at Step 2, so the version is chosen once against the tip; the rebase before the tag (§8) stays for a `main` that moved after, where `__version__` alone is retyped and `bin/release-git` derives the rest.

A cycle whose ground moves under it mid-implementation — a tracker commit on `main` resolving or reshaping a ticket in the plan's scope — rebases at once rather than at the release: a temporary commit of the work, `git rebase main`, the conflicts resolved with the Write tool, then `git reset --mixed HEAD~1` to return the work to the tree, so the implementation and its proof run over the true tree and the release's rebase meets only what landed after.

A document written in the cycle names no commit of the cycle's own branch by hash, since the release's rebase rewrites it; a commit of the branch is named by its place.

_Origin_: `v0.1.27`, `v0.1.28`, `v0.1.33`'s outstanding-items step; workflow tickets 17 and 38.

## 5. A hotfix is a cycle

A hotfix takes a worktree: `TRIP-hotfix`'s Step 2 is `git pull` in the primary on `main` then the claim's `bin/worktree-claim <slug> hotfix`, entered by path, and its Step 8 is §8's procedure. Its Window answer goes in its changelog entry (CLAUDE.md, "Applies on the box"), and from `v0.2.0` a hotfix whose diff touches the paths ADR-0036 names — the Filter sources, the frontend's render and record modules, the release note, the tests holding their seeds — takes the human read over the change since the previous tag, a person's before the hotfix's commit, its line in that entry beside the Window answer.

_Origin_: workflow ticket 09's ruling (d); workflow ticket 13 (the human read).

## 6. The box: applies, the upgrade window, the eval identity

On the box, `upgrade` and the apply that goes live run from the primary; a proof's apply runs from the cycle's worktree — `apply` takes the checkout from its own location, so the rendered units' `WorkingDirectory` names the worktree — and the release applies from the primary after the fast-forward and before the removal (`on-box-proofs.md` §7). No session works in the primary during the box's upgrade window, where `upgrade` checks the tag out detached; the first session after it returns the primary to `main`; the durable fix is `/opt/gideon`, slice-1 ticket 18's.

The box's eval identity is one account too, but since slice-1 ticket 68 a cycle's proof and a tracker session's measurement signed in as it at once each find and delete their own turn's chat, so the overlap costs nothing in correctness. The journal read stands for the measurement — two runs share the engine and their seconds inflate — so a session about to run the turn harness or `engine verify` reads the journal for another session's run first, and a tracker session waits for a cycle's proof (`on-box-proofs.md` §3).

_Origin_: workflow ticket 09's close-out; slice-1 ticket 59's triage, narrowed by slice-1 ticket 68.

## 7. Two tracker sessions in the primary

Two tracker sessions share the primary with nothing but git's refusals between them, so a write is made against the file as it stands at the write — an edit of the current text, or a whole-file write from a read taken then, never from an earlier read.

_Origin_: workflow ticket 09's triage and the repair after it.

## 8. The release's merge from the primary

Step 13, in this order:

1. When `__version__` is not above `main`'s tag, retype it as the tip's next in `gideon/__init__.py` and the message subject.
2. `bin/release-git <message file>`: the derivations, `git add -A`, the release commit or its amend, `git rebase main`.
3. A conflict is resolved with the Write tool and `git rebase --continue` — the version file and the README's tag the branch's, `CHANGELOG.md` both index lines — then the gate.
4. `git tag v<x.y.z>` on the release commit, `-f` when it moved, `-d v<old>` first when the number changed.
5. At a minor tag with an evidence commit already, `git reset --hard HEAD~1` first and the acceptance run again.
6. `ExitWorktree` (`keep`), or `cd` when the session entered by `cd`.
7. In the primary, `git merge --ff-only <branch>`.
8. "Your local changes … would be overwritten" is a tracker session's work: wait for its commit, retry.
9. "Not possible to fast-forward" is `main` moved again: re-enter by path, repeat from step 1.
10. When the units' `WorkingDirectory` names the worktree, `sudo python3 -m gideon apply` from the primary (`on-box-proofs.md` §7); then `git worktree remove .claude/worktrees/<slug>` (never `--force`) and `git branch -d <branch>`.

_Origin_: workflow ticket 09 (the merge); `v0.1.30`, `v0.1.37`, `v0.1.41`, workflow ticket 17 (the version at the tip); workflow tickets 32 and 38 (`bin/release-git`).

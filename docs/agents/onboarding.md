# Onboarding: the GIDEON method

This page is for a person joining the project. It explains the shape of the work and
the path from a ticket to a release; the governing sentences remain in the documents
linked below. Read each pointer when you need the rule's exact form.

## 1. What this is and is not

After this section, you know where the method ends and where the product, slice, and
domain decisions begin.

The per-machine setup is [tooling.md §4, “A new CSA”](tooling.md#4-a-new-csa): it tells
you what `git clone` does not provide and where personal configuration belongs. This
document supplies the human reading path and the working shapes that follow that
setup.

It does not define the product. Read the build specification, `.scratch/greenfield-spec/spec.md`,
and its `[NN]` Answers for what is being built and why; [ARCHI.md §1](../ARCHI.md#1-how-to-read-this-document)
identifies those authorities and their precedence. It does not choose the next slice:
the slice plan is spec §22 (`.scratch/greenfield-spec/spec.md`) and the TRIP-init
handoff, `.scratch/greenfield-spec/assets/24-trip-init-handoff.md`,
whose fog list is the do-not-build boundary. It does not create domain vocabulary or
decisions: [domain.md](domain.md#before-exploring-read-these) routes you to
`CONTEXT.md` and `docs/adr/`, and [ARCHI.md §1](../ARCHI.md#1-how-to-read-this-document)
keeps those authorities distinct from the architecture map.

## 2. The reading order

After this section, you can read the repository in the order that leaves you ready to
take the next ticket on the board.

1. Start with [CLAUDE.md](../../CLAUDE.md), whole: it is in every session's prompt
   and states the rules a session obeys — the tracker, the cycle's worktree, the
   applies on the box, a ticket's size. You now know the session contract and the
   places where work happens; this page explains the shape those rules assume.
2. Read [tooling.md §1](tooling.md#1-layers) through [§5](tooling.md#5-staying-current-catching-drift),
   with special attention to §4. You now know which lifecycle files are shared, which
   skills are vendored, and which binaries and credentials are local to a machine.
3. Read [ARCHI.md §1](../ARCHI.md#1-how-to-read-this-document) and §2, then continue
   through the whole map. Its §2 inventory gives the subsystem boundary; its §8 table
   sends a command to its leaf. Read the leaf named by the ticket's command or modules
   after the map, as [ARCHI-rules Routing](../ARCHI-rules.md#routing) requires. You now
   know where implementation facts belong and which document owns each command.
4. Read `.scratch/BOARD.md`. It is the view of the frontier, the critical path, the
   decisions owed, and the open tickets as of the last release, which regenerated it;
   its frontier lists every effort's unblocked tickets in the board's order, so its
   first row may belong to an older slice than the one most worked. The next
   ticket is chosen by the command card's “Working the frontier” rule
   (`docs/6-memo/workflow-commands.md`), that rule's one home, which also says how
   old the board is and how a cycle claimed since it is found. Read the effort's
   `.scratch/<effort>/NOTES.md` when it exists (an effort without one has no note) with
   [issue-tracker.md, “Notes”](issue-tracker.md#notes-notesmd),
   so a kept fact is not silently duplicated or lost. You now
   know what is available to claim and what context the tracker has retained.
5. Finish with the workflow command card, `docs/6-memo/workflow-commands.md`. It joins
   the per-slice operation to the per-ticket operation and gives the invocation that
   starts a fresh cycle. Every skill is
   `.claude/skills/<name>/SKILL.md`: `TRIP-1-plan`'s Step 2 holds the plan template —
   the sections a plan must carry and its file name under `docs/1-plans/` — and the
   project's own rules sit in the three TRIP skills, project-owned since the fork
   ([tooling.md §2](tooling.md#2-vendored-and-project-owned-skills)): the claim's
   worktree and branch name, the notes read at the claim, the window rule. You now
   have the route from the board's next ticket to the first planning session.

The order is deliberate: setup and boundaries first, architecture before leaves,
tracker state before selection, and commands last. A ticket's path, not a remembered
slice name or a directory scan, is the input to the cycle.

## 3. The shape of the work

After this section, you can distinguish an effort, a product slice, a cycle, and the
tracker events that move work between them.

An effort is a directory below `.scratch/`, with issue files and, where applicable,
a notes file. [issue-tracker.md, “Conventions”](issue-tracker.md#conventions)
defines that committed tracker shape. Product efforts use `slice-0`, `slice-1`, and
later names and end at their minor tags. `workflow` records how GIDEON is developed;
`standing` holds tickets waiting for an event no session can cause. Neither replaces
the board: [issue-tracker.md, “Standing tickets”](issue-tracker.md#standing-tickets-scratchstanding)
and the board's generated sections determine how those records are treated.

The ticket cycle has three phases. `TRIP-1-plan` claims one ticket and produces its
plan; `TRIP-2-implement` builds it under the testing gate; `TRIP-3-release` carries
review, documentation, versioning, tagging, and merge. The command card's
per-ticket section (`docs/6-memo/workflow-commands.md`) is the compact route; each
phase as a session of its own, the launcher's chain, and the seam are
[CLAUDE.md, “Ticket sizing”](../../CLAUDE.md#ticket-sizing), and the cycle's
worktree beside the primary checkout is
[CLAUDE.md, “Two sessions, one checkout”](../../CLAUDE.md#two-sessions-one-checkout).

The cycle is not the only tracker action. A triage asks the maintainer's ruling once
and commits it once, and a ticket that reaches `ready-for-agent` takes the shape
[issue-tracker.md, “The ruling”](issue-tracker.md#the-ruling) gives — the body as
opened, one ruling paragraph, the `## Agent Brief` as the one contract — so the agent
receives the decision and its usable brief rather than a chronology. A ticket's ruling that contradicts a rule stated elsewhere — a spec
section, an ADR, a convention — is a Conflict in the plan's Spec & ADR Alignment
section, recorded as a spec-bug comment on the ticket and never improvised around
([ARCHI.md §5](../ARCHI.md#5-core-architecture-principles), rule 6; the plan template).

Architecture memory has one route: read the map before its leaves, and put a command's
stages and invariants in the owning leaf. That is [ARCHI.md §1](../ARCHI.md#1-how-to-read-this-document)
and [ARCHI-rules “One home per fact”](../ARCHI-rules.md#one-home-per-fact), not a
second architecture system. A research note under `docs/research/` is cited by
section — in a ticket's body, in a pin bump's pull request (workflow ticket 05,
ADR-0033) — which is why a ticket can carry criteria checkable against the pinned
source; the `research` skill writes one through the `researcher` agent
([CLAUDE.md, “Subagents”](../../CLAUDE.md#subagents)), and a note's "not traced
further" at a boundary a plan crosses is a Conflict the plan traces
(`TRIP-1-plan`'s ticket-driven planning paragraph).

## 4. One cycle end to end

After this section, you can follow a real cycle by its committed artifacts and see
where each phase left its record.

The worked example is slice-1 ticket 55, released as `v0.1.52`. The example is about
artifact movement and review; its captured source text is not repeated here.

1. The ticket was triaged in commit `2d7cede`. The ticket file
   `.scratch/slice-1/issues/55-office-values-redacted-at-capture.md` contains the
   earlier shape the rule had at `v0.1.52`: the folded ruling, the ticket's criteria
   beside the `## Agent Brief`'s criteria, and later facts as dated comments. A triage
   after workflow ticket 30 writes the `## Agent Brief`'s acceptance-criteria list as
   the one list; the tracker rule linked in section 3 gives that shape.
2. `TRIP-1-plan` claimed the ticket and wrote
   `docs/1-plans/F_0.1.51_office-values-redacted-at-capture.plan.md`. The plan review
   took three rounds, a fact recorded by the release changelog and ticket close-out,
   not inferred from the plan. During the cycle, `main` took `v0.1.51`, so the
   release moved to `v0.1.52` before the rebase. The reason and ordering are in [worktrees.md §4](worktrees.md#4-a-main-that-moved-the-rebase-before-the-version-the-rebase-mid-cycle-no-hash-of-the-branch)
   and [§8](worktrees.md#8-the-releases-merge-from-the-primary), the place to learn
   this when a branch meets a moving primary.
3. `TRIP-2-implement` built the plan's change in the cycle worktree. Its result was
   released by `f4dd374`, whose stat names the implementation, documentation, tests,
   plan, review, and on-box evidence paths. The code-review synthesis is
   `docs/3-code-review/CR_w2_v0.1.52.md`; it records `APPROVED` after two rounds.
   The release record is `docs/2-changelog/w2_v0.1.52.md`, which also records the
   three plan-review rounds and the release's documentation trail. The cycle's
   review path is the release workflow; the synthesis is promoted there rather than
   becoming a second plan.
4. The session-captured proof is
   `.scratch/slice-1/assets/55-on-box.txt`. Its header records the date, the worktree
   path, and the command `python3 -m tools.redact --site /etc/gideon/site.yaml` that
   turned the capture into the asset. It also records the alternate `sudo` path and
   the cache check. The command is the on-box handoff between a session's capture and
   a committed asset, while [on-box-proofs.md §3](on-box-proofs.md#3-a-filter-ticket-proves-its-turns-with-the-turn-harness-a-users-seat-with-its-browser-mode)
   and [§6](on-box-proofs.md#6-the-recreate-set-is-the-row-before-an-apply-with-users-on-the-box-it-runs-only-in-a-window)
   govern the proof and the pre-apply reading. The asset is the durable evidence,
   not a paraphrase in this page.
5. The release then ran its close-out step. Commit `687a46a` is its tracker commit:
   the harness-path proof recorded as owed on
   `.scratch/slice-1/issues/18-pre-launch-release.md`, the board regenerated, two
   facts kept without a ticket, and the body naming each item's home. The step's rule
   is [issue-tracker.md, “The release's close-out”](issue-tracker.md#the-releases-close-out).
6. The release's crossing of the tools leaf's warning caused compaction commit
   `82a74aa`. Its stat carries `docs/ARCHI-rules.md`, the architecture memo, and
   `docs/archi/tools.md`; the leaf's count fell from about 6,495 to about 5,461.
   The loss ledger in the commit records no true loss and points moved detail to its
   owning modules and tests. This is the example of [ARCHI-rules “When to compact or
   split”](../ARCHI-rules.md#when-to-compact-or-split), not a reason to copy leaf
   mechanics into an onboarding page.

The sequence is therefore ticket and ruling, plan and plan review, implementation and
release review, on-box asset, tracker close-out, and architecture maintenance. The
paths and hashes let a new reader inspect the record directly, while the governing
documents explain the decisions behind each handoff.

## 5. The tracker's shapes

After this section, you can read a ticket and the board without inventing a local
vocabulary or changing the source of truth.

The status vocabulary has one home: the machine-readable `Statuses:` line in
[issue-tracker.md, “Vocabulary”](issue-tracker.md#vocabulary). [triage-labels.md](triage-labels.md#triage-labels)
maps the five skill roles into that vocabulary. Read those pointers for the labels;
this page names no second list.

`Blocked by:` is the board's grammar, not prose for a reader to reinterpret. The
first sentence supplies semicolon-separated blockers and the parenthetical or later
sentence supplies explanation; the forms for no blocker, an effort-local ticket, a
cross-effort ticket, and an external event are in [issue-tracker.md, “Conventions”](issue-tracker.md#conventions).
The board can then decide whether a row is blocked without parsing the explanation.

The ticket's shape at `ready-for-agent` is the result of a ruling, not a style
preference; [issue-tracker.md, “The ruling”](issue-tracker.md#the-ruling) sets it out,
superseded facts included.

At release, each leftover gets one home without a question — a text fix outside
the product tree made now, a `needs-triage` ticket, a dated line on the ticket that
owns it, or one dated sentence in the effort's `.scratch/<effort>/NOTES.md`;
[issue-tracker.md, “The release's close-out”](issue-tracker.md#the-releases-close-out)
gives the kinds, the homes, and the commit's shape, and
[issue-tracker.md “Notes”](issue-tracker.md#notes-notesmd) the note's.

A note leaves its file when the commit that acts on it deletes it, git history being
the record, so the notes file is not an append-only second ticket list.
Waiting on an event no session can cause belongs under `.scratch/standing/`, while a
rejected enhancement belongs in `.out-of-scope/`; the tracker documents' [standing-ticket](issue-tracker.md#standing-tickets-scratchstanding)
and [rejected-request](issue-tracker.md#rejected-requests-out-of-scope) sections
hold those boundaries.

The board is regenerated whole by `python3 -m tools.tracker` and is never hand-edited,
as [issue-tracker.md, “Conventions”](issue-tracker.md#conventions) states. Its critical
path is the tool's walk backward from the open release ticket — the one carrying a
`Tag:` line — through the `Blocked by:` lines, so a row there stands between the
tracker and the next tag (`tools/tracker.py`, workflow ticket 01). The cycles
record `.scratch/CYCLES.md` is written by `python3 -m tools.cycles --write` and supplies
the peak and turn data used by [CLAUDE.md, “Ticket sizing”](../../CLAUDE.md#ticket-sizing).

## 6. The first-day constraints

After this section, you know the shared-state boundaries that can make an otherwise
reasonable cycle unsafe or unmergeable.

Every cycle runs in its own worktree and the primary checkout stays on `main`:
[CLAUDE.md, “Two sessions, one checkout”](../../CLAUDE.md#two-sessions-one-checkout)
is the rule, [worktrees.md §1](worktrees.md#1-the-cycles-worktree-the-primary-on-main)
the ownership and lifetime, and
[worktrees.md §2](worktrees.md#2-the-worktree-guard-what-it-refuses-and-the-shapes-that-pass)
the guard's accepted shapes — a boundary of the harness, not an invitation to
bypass it.

There is one box, and it is the office's. The eval identity is one account, so a
session about to run the turn harness or `engine verify` reads the journal for
another session's run first ([CLAUDE.md, “Two sessions, one checkout”](../../CLAUDE.md#two-sessions-one-checkout),
[on-box-proofs.md §3](on-box-proofs.md#3-a-filter-ticket-proves-its-turns-with-the-turn-harness-a-users-seat-with-its-browser-mode),
[worktrees.md §6](worktrees.md#6-the-box-applies-the-upgrade-window-the-eval-identity)),
and a proof's apply runs from the cycle's worktree, the release's from the primary
([on-box-proofs.md §7](on-box-proofs.md#7-a-proofs-apply-runs-from-the-cycles-worktree-the-release-applies-from-the-primary-before-removing-it)).
Before any apply, read what [CLAUDE.md, “Applies on the box”](../../CLAUDE.md#applies-on-the-box)
names: [on-box-proofs.md §6](on-box-proofs.md#6-the-recreate-set-is-the-row-before-an-apply-with-users-on-the-box-it-runs-only-in-a-window).

There is one release version in `gideon/__init__.py` and one tag for the release
stream. `main` moves while a cycle runs — a path a ticket names may exist on `main`
and not yet in the worktree, and two cycles may carry the same candidate number — so
the mid-cycle rebase and a version moved at the tag are ordinary, not incidents:
[CLAUDE.md, “Two sessions, one checkout”](../../CLAUDE.md#two-sessions-one-checkout)
holds the rule, [worktrees.md §4](worktrees.md#4-a-main-that-moved-the-rebase-before-the-version-the-rebase-mid-cycle-no-hash-of-the-branch)
and [§8](worktrees.md#8-the-releases-merge-from-the-primary) the ordering and the
refusal recovery.

The server and GitHub organisation are shared with Gideon Transcribe, and
`docs/box-ledger.md` is the binding record of who owns each shared thing, its §2
table saying which things are shared. Before a change to one, read [docs/box-ledger.md §1](../box-ledger.md#1-the-two-projects),
[§8](../box-ledger.md#8-announce-before), and [§12](../box-ledger.md#12-log); the
release fetches the other copy under [§11](../box-ledger.md#11-changing-this-ledger).
This page does not duplicate the ledger's tables.

From `v0.2.0`, a person reads the diff of the paths that carry user text or a refusal
at each minor tag and at a patch or hotfix tag that touches those paths. The rule and
the mechanism-defined path set are [ADR-0036](../adr/0036-a-person-reads-the-user-text-paths-at-minor-tags-and-at-patch-tags-that-touch-them.md);
the changelog records the result, and the release's human-read step points back to
that decision. This is the review posture, not a second-model review of every file.

Finally, size a ticket for one context window: [CLAUDE.md, “Ticket sizing”](../../CLAUDE.md#ticket-sizing)
holds the cost measure, the early warning, and the seam rule.

## 7. What the proof asked for

This page is proven the one way it can be: a fresh session that has read nothing else
follows it to a claimed ticket with a plan, as a dry run, and what it had to find
elsewhere is written back. The record of the first two runs — the prompt, each run's
route, claim diff, plan whole, and asks — is
`.scratch/workflow/assets/14-fresh-session-proof.md` (workflow ticket 14): the first
found two routes to the next ticket that disagreed, so the card's "Working the
frontier" became the rule's one home; the second asked for the effort's notes file,
the board's true age, `main` moving under a cycle, and a ruling that contradicts a
rule, each folded above. The third run, at the lifecycle simplification (workflow
ticket 42), reached the frontier's first row by the rule and asked for two more,
folded in §2 and §6: an effort with no notes file, and how to tell a shared thing.
Asked for and not this page's — what each render fixture directory stands for, a
ticket's stale runbook number — stay with the leaf and the ticket. To re-prove the
page after a change to the method, run the asset's prompt against a fresh session
and fold its asks the same way.

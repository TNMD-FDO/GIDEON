# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`, which is **committed to git** and the **authoritative work tracker** — despite the name, not disposable scratch space. Treat its contents like any other versioned project document.

## Conventions

- One feature per directory, `.scratch/<feature-slug>/`; the spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`
- `.scratch/standing/issues/` holds tickets whose closing criterion names an event no session can cause: worked in place, never on the frontier while waiting, offered ordinarily once fired — see "Standing tickets" below
- Notes — facts no open ticket owns and not yet worth a cycle — are dated sentences in `.scratch/<feature-slug>/NOTES.md`, beside `issues/` and never in it — see "Notes" below
- `.scratch/CYCLES.md` is the cycles record, written by `python3 -m tools.cycles --write`, a top-level file and never an effort (workflow ticket 06)
- A release ticket carries a `**Tag:** v<x.y.z>` line between `Blocked by:` and `Status:`, the one place the tracker names the tag a ticket releases (slice-1 ticket 18's `v0.2.0` is the first)
- `.scratch/BOARD.md` is the board, generated whole by `python3 -m tools.tracker` at every release's outstanding-items step and never hand-edited; `.scratch/BOARD.html` is its page, generated beside it and ignored by git. The board's triage-owed section lists `needs-triage` tickets past 7 days, a starting value `tests/test_tracker.py` holds the tool's constant to. In a worktree the gate runs `python3 -m tools.tracker --check`, lint only, the cycle's own uncommitted `claimed` ticket its one expected finding until the release commit; `--strict` gives the exit code once that finding is gone, and the board is regenerated only at the close-out on `main` (workflow ticket 32).
- `Blocked by:` uses the grammar the board reads: the first sentence lists blockers, one per clause split at semicolons; an explanation goes in a parenthesis or a second sentence, and the board reads neither. `none`, `nothing`, an em dash first, or no line means no blockers; a bare number means the same effort and `<effort> ticket NN` another; a resolved blocker is named in a parenthesis with its tag; a clause naming no ticket is an external blocker, shown as `external` in the row and keeping the ticket off the frontier.
- Triage state is a `Status:` line near the top of each issue file, one of the Vocabulary section's seven; a ticket reaching `ready-for-agent` takes the shape "The ruling" gives below
- A ticket whose change recreates, restarts, or reboots a service a user's turn passes through says so in its body in one of those words, naming the service; no line declares it — `TRIP-1-plan`'s window rule finds it by the word (workflow ticket 07 declined a field)
- Comments append to the bottom of the file under a `## Comments` heading, dated, at any time; a comment before a ruling is deleted once the brief absorbs it ("The ruling" below)

## Vocabulary

- `needs-triage` — the maintainer has not yet evaluated the request
- `needs-info` — work waits for information from the reporter
- `ready-for-human` — the request needs a human implementation decision or action
- `ready-for-agent` — the request is fully specified for an AFK agent
- `claimed` — an agent is working on the request
- `resolved` — the request is complete
- `wontfix` — the request will not be actioned

<!-- Keep this line on one line: tools/tracker.py and tests read it. -->
Statuses: needs-triage, needs-info, ready-for-human, ready-for-agent, claimed; closed: resolved, wontfix

The tool reads that line and orders the board's rows by it. `triage-labels.md` maps the mattpocock skills' five roles onto five of the seven.

## The ruling

A ticket reaching `ready-for-agent` — at triage, or when a `ready-for-human` ticket's ruling moves it there — is written in one shape, top to bottom: the title; the body as opened, a fact the ruling changed carrying a superseded clause (the statement it was, then a parenthesis opening *superseded*, naming the ticket and tag or the triage, never a date, and saying what stands); `**Blocked by:**` as it stands now; `**Status:**`; the `triage` skill's disclaimer line and one ruling paragraph of at most five sentences — what was verified and where the redundancy and prior-rejection checks looked, the rulings lettered, the size in a clause; the `## Agent Brief`, the one contract with the one criteria list, the opened checkboxes moved into it; then `## Comments` for the dated notes that arrive after the ruling. A dated note that landed before the ruling is deleted once the brief absorbs its fact, git history keeping it. The triage verifies the body's claims like the request itself — that a step needs a person, that a rule forbids a shape — against the code and the document holding the rule, and asks what the rule protects before accepting a supervised form, unattended being this project's standard (ADR-0035; slice-0 tickets 20 and 21 are the record). It asks once, with a single `AskUserQuestion`, before the commit — the recommended ruling first with its reasoning; amend; a different state; defer — and the answer is the ruling, never a default a later line overrules. One commit lands, its subject `tracker: <effort> ticket NN triaged <state> — <gist>` within CLAUDE.md's 72 characters, its body the verification and the rulings with nothing of the ticket restated and no byte counts, carrying the ticket, a split ticket's file, and any `.out-of-scope/` file; a move into `.scratch/standing/` commits its rename alone first, as the Standing section says; *defer* commits the proposal as a dated note on a ticket that stays `needs-triage`. The committed ticket stays under 4 KB — a larger one is restating its body in the ruling or the brief. A small ticket whose body already makes its criteria checkable — a plain commit's worth — is moved to `ready-for-agent` by hand, its `Status:` line changed and one dated line saying so, without a triage.

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` (the Notes / Decisions-so-far / Fog body).
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records the state — `claimed` and `resolved` here — from the Vocabulary section above.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.

## Notes: `NOTES.md`

A note is a fact worth keeping that no open ticket owns and that is not yet worth a cycle — a measurement with its conditions, a cosmetic, a question with no cycle behind it — written as one dated sentence carrying the mechanism, the fact, and the trigger to revisit, in `.scratch/<effort>/NOTES.md` beside `issues/` and never in it, the effort chosen by subject: the open product slice's file for the product, `workflow`'s for how GIDEON is developed, `standing` none. Ids only, never user or matter text (spec §19.4; `tests/test_evidence_hygiene.py` walks every file under `.scratch/`). The file is edited and deleted freely — a line is sharpened or corrected in place — and the commit that acts on a line deletes it, git history being the record: a triage whose ruling absorbs it, a `TRIP-1` claim whose plan names it, a release's close-out opening a ticket from it or landing what it asked, the next slice's `/to-tickets`; a slice's file closes at its minor tag ("The release's close-out" below). A triage and a claim read the file for their ticket's mechanism; a `/to-tickets` split reads it whole.

## The release's close-out

`TRIP-3-release`'s Step 15 collects everything the cycle left undone or handed on — a criterion deferred, a starting value with a named correction, a seam left inert, a person-run step, a deferred rider, a `research/<name>` branch whose note `main` carries (`.claude/agents/researcher.md`) — and gives each one home without asking. A text fix outside the product tree — a skill, an agent document, a leaf, a memo, a tracker file — is made now with the tests that hold those files; a product change after the tag is a `TRIP-hotfix` when urgent and a ticket otherwise. Everything else is a `needs-triage` ticket of one paragraph when no open ticket owns it (a person-run step always, since nothing schedules a file), a dated line on the open ticket that owns it (a deferred rider names its window; a resolved ticket's comment records nothing), or one dated sentence in the effort's `NOTES.md`. The result is one commit on `main` — its subject `tracker: v<x.y.z>'s outstanding-items step — <gist>` within CLAUDE.md's 72 characters, its body naming each item and its home, "nothing cleared", "no note", "no new ticket" included, nothing of the product's tree — pushed with the release, its CI run confirmed. `python3 -m tools.tracker` regenerates the board first, its findings cleared so the committed board is green, and the tracker files are added by path, never `.scratch` whole (`v0.1.53`: another session's untracked file rode in). At a slice's minor tag the step also closes the slice's notes file — each line moved whole to the next slice's file or deleted with the reason in the commit body; `workflow`'s file has no close — and moves each open ticket whose criterion names an event no session can cause into `.scratch/standing/`, the renames committed alone first as the Standing section says.

## Rejected requests: `.out-of-scope/`

Enhancements closed `wontfix` because they were **rejected** are recorded in `.out-of-scope/` at the repository root, the location the `triage` skill reads and writes by name. It stays at the root: a rejection is one file per **concept** that spans features and slices and outlives the slice that raised it. Like `.scratch/`, it is committed and authoritative.

- One file per concept, kebab-case (`parallel-weight-fetches.md`), a later request for the same concept joins that file's **Prior requests** list.
- A file states the decision in a line, then **Why this is out of scope** with a durable reason (a measurement, a constraint, an ADR or spec rule — never "not now", which is a deferral, not a rejection), then **When to reconsider** naming the observation that would re-open it, then **Prior requests** listing each ticket by its `.scratch/` path with the date it closed. A rejection resting on a figure records it, its conditions (the box, the window, the link), and how to take it again.
- Written only when a rejected **enhancement** closes `wontfix`. An already-implemented request is closed with a pointer to where the behaviour lives and is not recorded here (it would poison the check below); a rejected bug is closed with its explanation on the ticket.
- Read at every triage: the prior-rejection check compares the request to every file here by concept, not wording, and surfaces a match before any grilling; a reconsidered file is deleted or rewritten and the new ticket proceeds, old tickets never reopened.
- The closing comment on the ticket links the file, and the ticket's `Status:` line reads `wontfix`.

## Standing tickets: `.scratch/standing/`

A ticket belongs in `.scratch/standing/` when its closing criterion names an event no session can cause: a later slice's tag, an upstream release, a calendar event, or a person outside the project. A ticket blocked by another ticket stays in its effort, and one waiting for the maintainer's decision is `ready-for-human` there.

The machine-readable form is the event as a clause of the ticket's `Blocked by` line's first sentence, which the board reads as an external blocker; the lint reports a standing ticket without one. A ticket is *waiting* while the event clause stands, and *fired* once the session that sees the event fire — a triage, a tracker session, or the outstanding-items step — replaces the clause with the word `fired` and moves the event, dated, into the parenthesis as provenance (`fired (the TRIP-workflow release v2.9.0 on 2026-10-06)`). The board reads `fired` as neither a blocker nor a reference, so the ticket is then offered like any other while it stays in the directory, its row saying `fired`. The two states are exclusive: a line carrying both the event and `fired`, or neither, is a lint finding; a `claimed` standing ticket is a fired one and the lint holds it to the word; `none` is never a standing ticket's first clause, since the lint cannot tell it from an event never named.

The directory is `.scratch/standing/`; `tests/test_tracker.py` holds the tool's constant to the name stated here.

A ticket moves into the directory by `git mv`, **committed alone** — a `tracker:` commit carrying the renames and nothing else, before the commit that edits the moved ticket — because the board's opened dates follow a rename only where git still sees the same file, and a move line plus a few qualifiers can take a short ticket under its similarity default (`v0.1.33`'s ticket 19 fell to a third). The mover is the release's close-out at a slice's minor tag, or any tracker session that sees a criterion name such an event. The number is kept; the ticket's bare pointers to other efforts' tickets are qualified with their effort; a dated line under `## Comments` names the release and event; every live pointer elsewhere is corrected to `standing ticket NN` and the new path, while a dated reference to a past event and a release record keep the name the ticket had. A ticket arriving at a number the directory already holds takes the next number above the directory's highest, its first body line reading `formerly <effort> ticket NN`; the lint reports two files sharing a number.

When its event fires, a standing ticket is triaged, claimed, or resolved where it is; its path never changes twice. A new slice's `/to-tickets` reads the directory for tickets whose event is that slice's tag, names them in the slice's brief as work already ticketed, and opens no duplicate; `/to-tickets` is a Matt Pocock skill, so the rule lives here and in `CLAUDE.md`, never in it.

The board never offers a waiting standing ticket; a fired one appears under the ordinary rules. Open standing tickets are listed under their own heading with the state in the blockers cell, counted apart in the summary line, and on the critical path only when a release ticket names one. Standing has no notes file. The first, at `v0.1.33`, are slice-0 tickets 19 and 24.

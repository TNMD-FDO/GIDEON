# Architecture documentation rules

## The model

`docs/ARCHI.md` is the map: its numbered cross-cutting sections, one leaf row per subsystem in §2, and command-to-leaf table in §8. Sections 1–19 keep their numbers and headings for good: plans, changelogs, code-review records, the review checklist, the plan template, and test docstrings cite them, and none of those is edited when the map changes. A new cross-cutting concern joins an existing section; a new subsystem is a leaf. `docs/archi/<subsystem>.md` are the leaves. The map describes landed code and cites specs and ADRs; a decision changes only through its ADR or ticket supersession.

## Budgets and warnings

Four classes have a cap and a warning, stated once on the line below and nowhere else: the map, a leaf, `CLAUDE.md`, and a document under `docs/agents/`. `tests/test_archi_budget.py` reads the line and holds the caps, each warning below its cap. `CLAUDE.md`'s class is the tightest because the file is in every session's prompt, paid on every turn; its values and the agent documents' were corrected against the compactions' returns (workflow tickets 22–24) before they were written, and `CLAUDE.md`'s lowered when the file was rewritten to its surviving rules (workflow ticket 42). Read a file's count with `bash .claude/skills/TRIP-compact/count-tokens.sh <file>`. Its estimator counts characters by category with awk-style records; UTF-8 character counting can vary from byte-wise awk locales, so the warning-to-cap gap absorbs that variance. `CLAUDE.md` and the documents under `docs/agents/` are the development repository's, so an export tree has no file of those two classes and the budget test finds none there.

<!-- tests/test_archi_budget.py reads this machine-readable line under this section's heading; keep it on one line. -->
Budgets: map cap 14,000 warning 12,000; leaf cap 8,000 warning 6,000; CLAUDE.md cap 3,500 warning 2,500; agent document cap 8,000 warning 6,000

## Routing

The plan's Documentation Impact names the leaves. Plan reads the map, then the leaves the ticket's commands, modules, and tests belong to; implement reads the plan and those leaves, the map's §8 table its fallback when the code disagrees with a leaf; research reads the map, then the leaves its question needs. More than three leaves is a ticket to split. Release Step 7 fires when the release's diff adds, removes, or renames a command, a module, a release constant, or a test module, and then updates the named leaves and the map wherever the map's own content changed: §2 and §12 only when a subsystem appears or a slice moves, §8's table when a command lands or moves, and any cross-cutting section whose fact moved.

## When the map or a leaf changes

### When to update

Update the map or a leaf after a structure (§4), stack (§3), principle (§5), bare-host rule (§9), CLI (§8, §10), release (§11), CI (§6), configuration (§7), testing (§15), data-flow (§13), error-handling (§14), security (§16), or deployment (§17) change. Update the diagram when the execution chain changes. A slice close normally updates its §2 status, §12 row, and touched leaves.

### How to update by change type

For a major feature or refactor, review §2, §4, §5, §8, §12, §13, and every touched section. For a minor feature, update §4 and the touched section, including §8, §9, or §15 when applicable. A bug fix needs an update only when it reveals or fixes an architectural flaw; record a spec bug or supersession as appropriate. A dependency change updates §3, §6, and §9 when its bare-host rules change.

## The leaf template

Each leaf has, in order: an H1 `# <Title> — architecture leaf`; a **Read this when** paragraph naming the commands, modules, and tests it owns, ending with a link to the map and the note that the map's §8 table names every command's leaf; `## Inventory`, the three-column table (surface, code, spec/ADR) the map's §2 once held, one line per surface; `## Commands`, one bullet per command with its stages and invariants; the leaf-specific parts (`## Release constants by module`, `## Configuration`, `## The locks`, `## Footprints`, or a one-line citation of the leaf that owns them); `## Tests`, its modules one line each; and `## Cross-references` with a *Cites* bullet (the leaves it depends on, as relative links) and a *Cited by* bullet (the map sections that point at it). Relative links resolve from `docs/archi/`; a file that does not exist yet is named in code font, never linked.

## One home per fact

A command's stages and invariants live in the leaf that owns it; other leaves cite them. The map never repeats a leaf, it points. Repo-wide tripwires live in `tests.md`; a subsystem's own modules live in its leaf. Product on-box paths live in map §4; a tool's footprint lives in its leaf. The workflow's own documents follow the rule — `CLAUDE.md`, the documents under `docs/agents/`, the workflow-commands memo, the TRIP skills, the workflow-tools leaf — and the Step 7 checklist names each home (workflow ticket 31).

## When to compact or split

The map, a leaf, `CLAUDE.md`, or a document under `docs/agents/` past its warning is compacted with `TRIP-compact <file>` in a tracker session on `main`, as one `tracker:` commit whose body is the loss ledger, never inside a release: the release that sees the crossing names the file among its Step 15 leftovers. The skill is user-invoked with the file as its argument, which overrides its own target line; its own ~20k thresholds are ignored (its "within acceptable range" question is answered yes), the count that matters being the count script's on that file. The ledger is built from a diff of the committed text against the compacted text — the backticked tokens and named rules present in the old file and absent from the new: for every section cut by more than about a third, what left and where it lives now (a module, a test, a ticket, another document), true losses separated from text that moved or is duplicated elsewhere; a pin value or measurement the document restated against its own rules is a correction, not a loss. If a leaf cannot come under its warning, split it by mechanism — [`render-apply.md`](archi/render-apply.md) split by what grew into [`render-apply.md`](archi/render-apply.md) (the host tree) and [`engine-frontend.md`](archi/engine-frontend.md) (the engine and the frontend's runtime), and [`tools.md`](archi/tools.md) into [`tools.md`](archi/tools.md) (the tools the export carries) and [`workflow-tools.md`](archi/workflow-tools.md) (the development repository's scripts and records) — each half indexed from §2 and named in §8's table; name the seam at the crossing by what grew, since a seam named in advance is drawn against the leaf as it stood then. If `CLAUDE.md` cannot, move a section's detail whole to a document under `docs/agents/`, keeping the rule and a pointer (workflow ticket 22's record). If an agent document cannot, split it by mechanism as a leaf is, the sentence that pointed at it naming both halves; `on-box-proofs.md`'s named split — §§1, 4, 5 the harness's shapes, §§2, 3, 6–11 the box's — is the standing example.

## Step 7 checklist

The step fires when the release's diff adds, removes, or renames a command, a module, a release constant, or a test module; otherwise the release says so in one line and runs the budget test alone. When it fires: read the leaves named by the plan and any the diff touched. Check the map's §2 and §12 conditions, its §8 table for a landed command, and its cross-cutting sections for a moved fact; a link added to or dropped from the map moves that leaf's Cited-by line in the same change. The documents a release touches, listed once here, each the home of one kind of fact:

- the map — a cross-cutting fact, a §2 or §12 row, a §8 row;
- a leaf — a subsystem's commands, constants, tools, and tests: a tool named with its command and its home, its stages and invariants;
- a tool's module docstring and its test — the tool's behaviour and mechanics, which no leaf restates;
- `CLAUDE.md` — a rule every session obeys, its detail in a document under `docs/agents/`;
- a document under `docs/agents/` — a rule's detail and reference: the tracker's conventions and the release's close-out, the worktree shapes, the on-box rules, the skills' provenance and refresh;
- the glossary, `CONTEXT.md` — a term, through `/domain-modeling`; counted beside the others and never budgeted (slice-1 ticket 48);
- the workflow-commands memo, `docs/6-memo/workflow-commands.md` — the launcher's commands, the phone path, the frontier rule; excluded from the export, so a kept document names it by backticked path;
- a skill under `.claude/skills/` — what its phase asks, described nowhere else.

Count the budgeted classes with the glossary beside them — `bash .claude/skills/TRIP-compact/count-tokens.sh docs/ARCHI.md docs/archi/*.md CLAUDE.md docs/agents/*.md CONTEXT.md` — and run the budget test. A release edits a fact at its home and adds no restatement elsewhere; a mirror the diff shows is cut in the same release with its ledger. A slice start creates that slice's leaf from the template, adds its §2 row and §12 phrase, and links the glossary entries the new leaf owns through `/domain-modeling`. A release that lands the mechanism an unlinked `CONTEXT.md` entry names adds that entry's `_Leaf_:` line through `/domain-modeling`. A slice close records the map's count in the changelog.

## Standing rules

- Be precise and file:line-checkable; be concise and cite the governing spec or ADR.
- Update the diagram when the chain changes. “Nothing edited in place” applies to decisions, not docs.
- A leaf's Commands part names stages and invariants; argv and mechanics belong to the module and its test.
- Put no count in prose that a registry or the box carries.
- An inventory row is one line, never a release summary.
- Name a release constant by module, never by name or value.
- `tests.md` and a leaf's Tests part list tripwires one line each.
- The map cites a ticket as a decision record and carries no tracker state: which tickets landed, are open, or moved is the board's, and §12's slice row says what remains by name, checked against `.scratch/<slice>/issues/` at every release.
- A cross-cutting section's sentence about a subsystem names the posture, guard, or shape and points at the leaf; the mechanism is the leaf's. A §13 label is the module, a gist, and the leaf, never a stage list; a §4 comment is a directory's role in a phrase, its tickets and ADRs the leaf's inventory's.

## History

The rules above came out of the compactions and splits of 2026-09, recorded one dated line each — the figures, what had grown, what each taught — in `docs/6-memo/archi-map-and-leaves.md` §10; a compaction's own ledger is its commit.

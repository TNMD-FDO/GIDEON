---
commands: [proposals]
modules: [gideon/improvement/]
---

# Improvement loops — architecture leaf

**Read this when** changing the improvement loops (ADR-0049), the committed trigger registry, its states, or its loader; `gideon proposals` (improvement ticket 02) and the later loops land here. A loop proposes and never acts: the developer's ruling on a proposal is a pull request that moves a trigger's state. The map is [`../ARCHI.md`](../ARCHI.md); its §8 table names every command's leaf.

## Inventory

| Surface | Code | Spec / ADR |
|---|---|---|
| the trigger registry: release content every office receives whole, a root of `version` (`1`) and a non-empty `triggers` list; an entry's closed keys in order — `id` (lowercase, digits, hyphens; unique), `reopens` (the deferring section, `§n` or `§n.m`), `register` (optional, `E20`/`M18` form), `measure`, `state`, `condition`, `says` (one prose sentence), `ruled` (an ISO date and the tag or pull request, space-separated), `baseline` (optional, a re-baselined number the report prints beside the figure); the states `watching`, `acted`, `retired`, a `watching` entry carrying a `condition` and no `ruled`, an `acted` or `retired` one a `ruled` and no `condition`; a state moves only by a pull request, so the file's history is the record of every ruling; two entries at this release, `judge-fallback` (§18.4, M18) and `learned-components` (§12.3 and §6.3, E20) — the later slices' triggers join by standing tickets 82–84, and the two no box can measure are standing tickets 85–86, never entries | `config/triggers.yaml` | §18.4, §12.3, §6.3, §2.6, ADR-0049, ADR-0017 |
| the condition grammar: a clause is `figure`, `op` (`above`, `at-least`, `below`), `value` (an integer or float, never a boolean), and optional `runs` (at least one: the releases running the clause must hold; absent is once); a condition is one clause or `all:` a non-empty list of clauses, every one required, with no other combinator and no nesting | `gideon/improvement/triggers.py` | ADR-0049, ADR-0006 |
| the measure vocabulary, measure name to the figures it supplies: `unavailable` (none yet, so a clause may name any well-formed figure) and `eval-set` (`judged_queries`, the judgment-set queries holding a primary grade in the judgments file; `held_out_ids`, the ids the frozen `judgments-held-out` slice lists, zero while it does not exist); a ticket that produces a measure adds it here, its reader in the report | `gideon/improvement/triggers.py` | §18.4, §18.6 |
| the loader, in `gideon/host/egress.py`'s shape: read over the `Host` seam (a missing, unreadable, non-UTF-8, empty, non-mapping, unparsable, or duplicate-key file is one finding each), validate returning every finding at once — unknown key, state, op, or measure with the nearest valid word, missing required key, wrong type, malformed id, section, register, or `ruled`, a figure the measure does not supply, the state rule's four breaches, an empty `all`, a `runs` below one, a boolean value, a `version` other than 1, a duplicate id by its second index — then frozen, slotted dataclasses only on a clean document; each finding names a key path and a rule, never a value from the file, and ends in the one fix; the package runs on the standard library and `yaml` from the host checkout | `gideon/improvement/__init__.py`, `gideon/improvement/triggers.py` | §1.5, §19.4, ADR-0046 |
| the section interface: a `Section` has a `name`, a `scope` (`product`, printed on the build box alone; `office`, on every box), and a `render` over a frozen `Context` (the `Host`, the checkout, the rendered directory, the registry, `build_box`, `query`) returning a header detail and `Row`s (name, state, detail) or a `Problem`; a row carries ids, section numbers, register tags, figure names, and numbers alone; a section never reads the marker — the runner gates; `read_rows` is the report's only path to Postgres, `record.py`'s reader argv as `gideon_ro_metrics` with the statement on stdin, returning the non-empty lines or a problem that never quotes psql | `gideon/improvement/sections.py` | §19.4, §19.5, ADR-0034 |
| the measure readers: `READERS` maps each measure but `unavailable` to the figures it declares and a reader over the context returning figure name to values, newest first; `eval-set` counts the distinct query ids holding a `primary` grade in the release set's judgments file (absent is zero; a parse finding is a problem with the judgments fix) and the distinct ids across `slices/judgments-held-out/*.ids` (absent is zero) | `gideon/improvement/measures.py` | §18.4, §18.6 |
| the trigger watch: a clause holds iff its figure's newest `runs` values all meet `op` against `value` (`above` and `below` strict, `at-least` inclusive), and is unmeasurable when the figure is absent or has fewer than `runs` values; a trigger is not fired if any clause fails, else not yet measurable if any is unmeasurable, else fired — a pure function of trigger and figures; the `triggers` section (product) reads each named measure once and prints one row per `watching` trigger, its clauses' figures and `k of N measured`, `reopens` with the register, and the baseline | `gideon/improvement/watch.py` | ADR-0006, ADR-0049 |
| the report: `SECTIONS`, the ordered section instances only the runner enumerates (a later loop appends its instance); the row vocabulary; the header and closing-line shapes | `gideon/improvement/proposals.py` | §20.2, ADR-0049 |

## Commands

- **`proposals`** (`python3 -m gideon proposals`, no flag, no root needed): load the registry — any finding prints every finding and one refusal on stderr, exit 1, nothing on stdout; then walk `SECTIONS` in order, printing `section <name> (<scope>): <detail>` and its rows indented; a product section off the build box (`nogpu.is_build_box`, so two markers are not the build box) prints `skipped` with the not-the-build-box detail and is never rendered; a section returning a problem prints one `refuse` row with its fix and the walk continues; the closing line counts fired rows, sections, and skipped ones. Exit 1 iff a section refused, else 0 whether or not anything fired. Invariants: no write of any kind, no command but the metrics-role exec, rows content-free, the `says` sentence never printed.

## Release constants by module

- `config/triggers.yaml` — the committed registry path and its schema version.
- `gideon/improvement/triggers.py` — the root and entry key registries; `STATES`, `OPS`, and `MEASURES`; the id, figure, section, register, and ruled patterns; the finding fix text; and the schema version.
- `gideon/improvement/sections.py` — the read seam's fix text.
- `gideon/improvement/measures.py` — the held-out slice's directory and the restore fix.
- `gideon/improvement/proposals.py` — `SECTIONS`, `ROW_STATES`, the header and closing-line shapes, and the registry fix.

## Tests

- `tests/test_improvement_triggers.py` — the committed registry's shape and construction, vocabulary drift, every validation finding kind, and Host read/parse failures; every rendered finding carries the loader fix.
- `tests/test_improvement_watch.py` — each op's boundary, `runs` coverage, the verdict precedence, the `eval-set` reader over a fake host, the readers-to-vocabulary drift check, and the row text.
- `tests/test_improvement_proposals.py` — the command over a read-only fake host that fails any write: the committed registry, the gate with and without the marker, the refusals, section order and the walk past a refusal, the read seam's argv and stdin, a fired trigger, and the package's imports.

## Cross-references

- *Cites*: [`eval.md`](eval.md) (the frozen evaluation slices), [`eval-slices.md`](eval-slices.md) (the judgments file and its measures), [`tests.md`](tests.md) (the repo-wide tripwires), [`stack.md`](stack.md) (the Compose exec the read seam uses), [`host.md`](host.md) (the build-box marker).
- *Cited by*: the map's §§2, 4, 7, 8, 12, and 13.

---
commands: []
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

## Commands

None yet. Improvement ticket 02's `gideon proposals` is the registry's first reader, and turns the loader's findings into its refusal.

## Release constants by module

- `config/triggers.yaml` — the committed registry path and its schema version.
- `gideon/improvement/triggers.py` — the root and entry key registries; `STATES`, `OPS`, and `MEASURES`; the id, figure, section, register, and ruled patterns; the finding fix text; and the schema version.

## Tests

- `tests/test_improvement_triggers.py` — the committed registry's shape and construction, vocabulary drift, every validation finding kind, and Host read/parse failures; every rendered finding carries the loader fix.

## Cross-references

- *Cites*: [`eval.md`](eval.md) (the frozen evaluation slices), [`eval-slices.md`](eval-slices.md) (the judgments file and its measures), [`tests.md`](tests.md) (the repo-wide tripwires).
- *Cited by*: the map's §§2, 4, 7, and 12.

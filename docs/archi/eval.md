# The eval harness — architecture leaf

**Read this when** implementing the extraction grammar, its measure, or the eval set's cases; the owning modules are `gideon/extraction/` (`contract.py`, `grammar.py`, `scoring.py`) and the directory `eval/sets/eval-v1/`, each in the Inventory with its tests. `gideon eval` itself is still a stub (slice-2 ticket 04). The map is [`../ARCHI.md`](../ARCHI.md); its §8 table names every command's leaf.

## Inventory

| Surface | Code | Spec / ADR |
|---|---|---|
| the exact-object contract: the closed type vocabulary, the keyed and section-like subsets, the verbatim span, the section-level key and its case-folding comparison, the invariant checks | `gideon/extraction/contract.py` (its docstring the contract's one home) | §11.2, ADR-0019, ADR-0024, slice-2 ticket 01 |
| the extraction grammar: versioned `family/pattern@N` patterns in a static registry, each bounded and emitting one type, one fixed precedence, `extract(text)` the one entry point | `gideon/extraction/grammar.py` | §11.1, §11.2, ADR-0006, ADR-0016, slice-2 ticket 01 |
| the extraction measure: the active set over `supersedes`, the hit rule, per-type precision and recall over the landed types handed in, the id-only report; reads no file | `gideon/extraction/scoring.py` | §11.2, §18.2, §19.4, slice-2 ticket 01 |
| the eval set's cases: a directory per suite under the set's version, JSONL, a case never edited (a correction is a new id naming the old in `supersedes`), each suite's README its authority | `eval/sets/eval-v1/judgments/` (slice-2 ticket 07), `eval/sets/eval-v1/build-gates/` (the `extraction` cases: the harvest's forty excluded from the export, the invented block kept) | §18.2, §18.6, §2.6, slice-2 tickets 01 and 07 |

## Commands

None landed: `gideon eval run` (slice-2 ticket 04) takes the extraction measure unchanged, with the registry's declared types as the landed types.

## Release constants by module

- `gideon/extraction/contract.py` — the type vocabulary and its keyed and section-like subsets; a type is never renamed.
- `gideon/extraction/grammar.py` — the grammar's version, the pattern registry, and the bare-section cue list; a pattern whose behaviour changes takes its next `@N`, and the version moves with any pattern.
- `gideon/extraction/scoring.py` — the gate's two bounds (§11.2).

## Tests

- `test_extraction_grammar.py` — the contract's invariants over hand-built and extracted objects; each family, the hyphen rule, the declines, and the recorded residuals over unit cases; determinism; every pattern id unique and versioned, every repeat bounded, an adversarial input within a ceiling; the package importing the standard library and itself alone, by AST.
- `test_extraction_set.py` — both files' shape, ids, and labels against their questions; the byte pins per file; the harvest file against the harvest, read by `absent_from_export`; the scorer's unit cases; the gate, per registry type at the bounds, over both files and over the invented block alone.

## Cross-references

- *Cites*: [`tests.md`](tests.md) (the export boundary's skip rule, the judgment set's queries test), [`tools.md`](tools.md) (the export boundary list), [`workflow-tools.md`](workflow-tools.md) (the judgment set's intake).
- *Cited by*: the map's §2, §4, and §12.

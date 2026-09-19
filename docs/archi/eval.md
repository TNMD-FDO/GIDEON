# The eval harness — architecture leaf

**Read this when** implementing `gideon eval run`, the extraction grammar, its measure, the eval set's cases, or a frozen slice; the owning modules are `gideon/extraction/` (`contract.py`, `grammar.py`, `scoring.py`), `gideon/evaluation/` (`evalset.py`, `extraction_slice.py`, `record.py`, `command.py`), and the directory `eval/sets/eval-v1/`, each in the Inventory with its tests. The map is [`../ARCHI.md`](../ARCHI.md); its §8 table names every command's leaf.

## Inventory

| Surface | Code | Spec / ADR |
|---|---|---|
| the exact-object contract: the closed type vocabulary, the keyed and section-like subsets, the verbatim span, the section-level key and its case-folding comparison, the invariant checks | `gideon/extraction/contract.py` (its docstring the contract's one home) | §11.2, ADR-0019, ADR-0024, slice-2 ticket 01 |
| the extraction grammar: versioned `family/pattern@N` patterns in a static registry, each bounded and emitting one type, one fixed precedence, `extract(text)` the one entry point | `gideon/extraction/grammar.py` | §11.1, §11.2, ADR-0006, ADR-0016, slice-2 ticket 01 |
| the extraction measure: the active set over `supersedes`, the hit rule, per-type precision and recall over the landed types handed in, the id-only report; reads no file | `gideon/extraction/scoring.py` | §11.2, §18.2, §19.4, slice-2 ticket 01 |
| the eval set's cases: a directory per suite under the set's version, JSONL, a case never edited (a correction is a new id naming the old in `supersedes`), each suite's README its authority | `eval/sets/eval-v1/judgments/` (slice-2 ticket 07), `eval/sets/eval-v1/build-gates/` (the `extraction` cases: the harvest's forty excluded from the export, the invented block kept) | §18.2, §18.6, §2.6, slice-2 tickets 01 and 07 |
| the frozen slices: one directory per slice under the set's version, one `.ids` file per source file, the slice's README its authority; `extraction/harvest.ids` leaves with its excluded cases, `extraction/invented.ids` is kept | `eval/sets/eval-v1/slices/`, `tools/exportboundary.py`, `.gitattributes` | §18.6, slice-2 ticket 04 |
| the eval-set loader: reads a set version in sorted-path order, checks each case against the shape registry (suite and category, the ordered keys, the fixed fields, the id, cluster, role, and court patterns), resolves the slices' ids, reports every finding at once by file and id, and digests the ordered bytes with SHA-256 | `gideon/evaluation/evalset.py` | §18.6, slice-2 ticket 04 |
| the `extraction` slice runner: the grammar over the slice's active cases, the registry's declared types as the landed types, the scorer's report, and one content-free per-case result (verdict, per-type counts, latency); the slice-runner registry keyed by slice name | `gideon/evaluation/extraction_slice.py` | §11.2, §18.2, §18.6, slice-2 ticket 04 |
| the run record: `eval_runs` and `eval_results` under the kept-event partition scheme, `gideon_eval` their writer and the metrics role their reader, every value bound as a `psql` variable, CR/LF/NUL refused, one transaction | `gideon/evaluation/record.py`, `migrations/0004_eval_runs.sql` | §18.6, §19.4, §16, ADR-0027, slice-2 ticket 04 |

## Commands

- **`eval run --slice <name> [--set <dir>]`** (§18.6; `evaluation/command.py` over the three modules above, `_guarded` behind `cli.py`): `load` → `run` → `record` → `gate`, the gate's verdict the exit code. `load` reads the court map and the set (the release's `eval/sets/eval-v1/` unless `--set` names another), prints the version, the case count, the slice's count, and the digest, and on any finding prints them all and refuses. `run` evaluates the slice's active cases through its registered runner and prints the scorer's report. `record` writes one run row and one result row per active case in one transaction as `gideon_eval`: it is skipped, without failing the command, for a set outside the release (`--set`) and for an unreachable database (the probe's problem and "rows were not written" in the row), and refuses when the site file, the git provenance, or the write itself fails. The run row carries the product version, the set version, the hardware profile, `stack=production`, `kind=manual`, the slice, `repeats=1`, the checkout's `git_sha` and `git_dirty` — both read under a `safe.directory` grant, both null only where the checkout root has no `.git` — the set digest, and the verdict. `--decision` and `--force` refuse as not implemented (slice-2 ticket 16). Invariants: rows are content-free, insert-only, and written after the run and before the gate's row; a run of the release's own set is recorded whatever its verdict.

## Release constants by module

- `gideon/extraction/contract.py` — the type vocabulary and its keyed and section-like subsets; a type is never renamed.
- `gideon/extraction/grammar.py` — the grammar's version, the pattern registry, and the bare-section cue list; a pattern whose behaviour changes takes its next `@N`, and the version moves with any pattern.
- `gideon/extraction/scoring.py` — the gate's two bounds (§11.2).
- `gideon/evaluation/evalset.py` — the eval set version and its root, the shape registry, and the id, cluster, role, and query-type patterns; a set version is never edited, a later version is a new directory.
- `gideon/evaluation/record.py` — the writer role, the database, and the Postgres service name, each equal to [`stack.md`](stack.md)'s; the run row's `kind` vocabulary (`smoke`, `nightly`, `weekly-off`, `decision`, `candidate`, `engine-verify`, `manual`) and its `stack` vocabulary (`production`, `ci`).

## Tests

- `test_extraction_grammar.py` — the contract's invariants over hand-built and extracted objects; each family, the hyphen rule, the declines, and the recorded residuals over unit cases; determinism; every pattern id unique and versioned, every repeat bounded, an adversarial input within a ceiling; the package importing the standard library and itself alone, by AST.
- `test_extraction_set.py` — the byte pins per file, the harvest file against the harvest read by `absent_from_export`, the scorer's unit cases, and the gate per registry type at the bounds over both files and over the invented block alone. The shape, id, and label checks moved onto the loader.
- `test_judgments_queries.py` — the queries file's own pins and its intake's expectations; its shape checks moved onto the loader.
- `test_evaluation_set.py` — one unit case per refusal over a temporary set, each finding naming the file and the id and carrying no question text; every finding reported at once; order and digest independent of file-creation order and moved by one changed byte; `supersedes` naming a higher-numbered id refused; the committed set loading clean in the development tree and in an export, and the `extraction` slice's ids equal to its available cases'.
- `test_evaluation_run.py` — the runner, the command in-process through `cli.main` over a fake `Host`: the clean run, the planted failing copy under `--set`, the recorded failing run, the unreachable database, the failed write, the flag and slice refusals, both git probes and their three provenance outcomes, and the package's imports by AST.
- `test_evaluation_record.py` — the batch's ensure calls, its bound variables, the CR/LF/NUL refusal, the sorted-key JSON, the one transaction, and the role, database, and service constants equal to `stores.py`'s and `audit.py`'s.

## Cross-references

- *Cites*: [`stack.md`](stack.md) (the kept-event tables, the roles, the audit writer), [`tests.md`](tests.md) (the export boundary's skip rule, the judgment set's queries test), [`tools.md`](tools.md) (the export boundary list), [`workflow-tools.md`](workflow-tools.md) (the judgment set's intake).
- *Cited by*: the map's §2, §4, §8, §12, and §13.

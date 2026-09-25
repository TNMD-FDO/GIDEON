# Evaluation references

An evaluation reference is the content-free record of one clean, tagged run.
It preserves the verdict for each case in a frozen slice so a later
`eval run` can report a per-case regression without storing questions,
metrics, or judge output.

## Layout

There is one file for each id list in the slice:

```text
eval/reference/<slice>/<list>.json
```

The path has no eval-set version. The reader finds all files for a slice and
checks their version headers against the loaded set. An exported tree keeps
the reference file beside the id list it covers, so a partial export remains
detectable.

## File contents

Files use sorted-key, two-space, ASCII JSON with a final newline. The header
contains:

- `format`: the reference format number;
- `product_version`, `corpus_lockfile`, and `eval_set_version`: the release
  identity;
- `hardware_profile`: the profile used by the run;
- `tag`: the product tag resolved to the recorded commit;
- `slice` and `list`: the path identity;
- `repeats`: the number of repeats folded into each verdict;
- `set_digest`: the digest of the loaded eval set.

`cases` maps case ids to `pass` or `fail`. A case passes only when every one
of its repeats passes. The four comparison lists are derived at run time:

- `regressed`: passed in the reference and fails now; this is the only list
  that blocks the gate;
- `gained`: failed in the reference and passes now;
- `new`: evaluated now but absent from the reference;
- `dropped`: present in the reference but not evaluated now.

The last three make a reference stale, but do not fail the gate. A reference
from another eval-set version is reported as `other-version` and is replaced
only by the deliberate reference command. A malformed or partial reference
is a refusal, never an absence.

The files exclude the run id, timestamps, latency, metrics, judge output,
`git_sha`, `kind`, `stack`, and case text. These values vary between runs or
are not the release identity; the tag and the set digest carry the identity
needed for comparison. No key may carry case text.

## Commands

`gideon eval run --slice <name>` evaluates the slice, folds its repeated
verdicts, and compares them with the committed reference. It prints one
`reference: <outcome>` line after the scorer when the comparison is reached.
A slice whose registry entry (`gideon/evaluation/slices.py`) sets
`compares_reference` false keeps no reference: its run prints no `reference:`
line, its gate row is the slice's own, and the writer refuses its runs.
`judge-triples` is the one such slice today — a triple passes when its grading
came back on-schema, which that slice's gate already refuses on.

`gideon eval reference --run <id>` reads the recorded run through the
read-only metrics role, checks its clean tagged provenance and set identity,
then writes one file per id list. A list with no evaluated case still gets an
empty `cases` object. The command reports gained, new, and dropped counts and
whether the files were `written` or `unchanged`; it refuses to write over a
regression.

Every release runs the comparison at its own tag. `current` with exit 0
needs no commit; `stale` or `absent` with exit 0 calls the writer; and
`other-version` with exit 1 calls the writer. `regressed`, `malformed`, a
missing comparison line, or any other combination stops the release.
The `smoke` slice's comparison runs on the CI sibling through
`sudo python3 -B -m tools.cistack smoke`, the writer unchanged.

Reference files are never hand-edited. The reader compares each file's bytes
with the module's canonical serialization and refuses a hand edit; restore
the slice from the release or remove every file of the slice and re-record.

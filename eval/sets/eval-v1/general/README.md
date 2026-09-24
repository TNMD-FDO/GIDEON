# General suite

The `general` suite has one category, `smoke`, in `smoke.jsonl`. Its cases
are the invented General smoke turns from `eval/seed/general/smoke.yaml`,
with their ids and questions preserved. `tests/test_general_smoke_set.py`
holds the converted records to the seed and verifies that the set loads in
a full checkout and an exported copy.

This is the `general-smoke` suite, run as the `general-smoke` frozen slice.
The `load.yaml` and `frontend-bump.yaml` files in the seed directory
remain turn-harness instruments; they are not categories in this suite.
The nightly schedule is set apart from this suite.

A case is never edited. A correction is appended with a new id and a
`supersedes` link; retired cases remain in the frozen slice so the loader
can determine which cases are active.

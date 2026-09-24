# General suite

The `general` suite has one category, `smoke`, in `smoke.jsonl`. Its cases
are the invented General smoke turns from `eval/seed/general/smoke.yaml`,
with their ids and questions preserved. `tests/test_general_smoke_set.py`
holds the converted records to the seed and verifies that the set loads in
the development tree and an exported copy.

This is §18.2's `general-smoke` suite, run as the `general-smoke` frozen
slice. The `load.yaml` and `frontend-bump.yaml` files in the seed directory
remain turn-harness instruments; they are not categories in this suite.
Slice-2 ticket 17 owns the nightly schedule.

A case is never edited. A correction is appended with a new id and a
`supersedes` link; retired cases remain in the frozen slice so the loader
can determine which cases are active.

# Frozen slices

A frozen slice is a directory below `slices/`. Its `*.ids` files select cases
from the eval set; the loader unions all of the files in one slice and runs the
result in id order. Each id list contains one case id per line, with no blank
lines or comments, and ends with a final newline.

The `extraction` slice is split at the export boundary. `harvest.ids` selects
the harvest-derived cases and leaves with `extraction.jsonl`; `invented.ids`
selects the invented cases that remain in an exported tree. A variant's id joins
the list on its parent's side of the boundary, so each list covers two case
files. A superseded case stays in its id list because the loader decides which
case is active.

The `judge-triples` slice selects the invented judge triples in
`../judge/triples.jsonl`, which the runner grades in id order at its own repeat
count. The slice stays on the kept side of the export boundary, as its cases are
invented.

The `guardrails` slice has one id list for each of its three category files in
`../guardrails/`. Each list includes every source id, including superseded
cases, and changes only by append. The suite is invented and stays on the kept
side of the export boundary.

The `general-smoke` slice has one list, `general-smoke/smoke.ids`, for the
`general` suite's `smoke` category. It includes every source id, including
superseded cases, and runs each active case twice; both repeats must pass. The
invented suite stays on the kept side of the export boundary.

The `judgments` slice selects the judgment set's queries in
`../judgments/queries.jsonl`, one result per active query, scored against the
judgments file by the `judgments@1` definition that `../judgments/README.md`
states. Its one id list, `judgments/queries.ids`, leaves at the export boundary
with the queries file it names: an id list resolving to no case is a loader
finding, so it cannot stay where its cases go. In an exported tree the registry
holds a `judgments` slice the set does not, and `eval run` answers that as it
answers any unknown slice. The list changes only by append, as every frozen
slice does; the ten CHU-written queries are the next append.

The `smoke` slice is the push gate's, with one list per source list. Its
guardrails lists name the frontend sample's six cases, each run at the service
door and through the frontend. Its two extraction lists mirror `extraction`'s
and change with them. Every list changes only by append; a successor to a
superseded sample case is appended beside it. Each later slice adds its
categories as they land. `harvest.ids` leaves at the export boundary with its
cases.

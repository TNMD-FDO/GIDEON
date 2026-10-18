# Frozen slices

A frozen slice is a directory below `slices/`. Its `*.ids` files select cases
from the eval set; the loader unions all of the files in one slice and runs the
result in id order. Each id list contains one case id per line, with no blank
lines or comments, and ends with a final newline.

## Held-out lists

A case file may carry a held-out list beside it, `<name>.held-out.ids` next to
`<name>.jsonl`, in the id lists' format: one case id per line, no blank line,
a final newline. The commit that adds a category's cases writes its list. A
held-out case is one no tuning run has touched; it counts in every gate and
reference run, and a tuning run reads the tuning cases alone — the counted
cases no list names — through the loader's `tuning_cases`.

The split is whole by family. Every active case that shares a `cluster_id`
with a held-out case, or that supersedes one, is listed in its own file's list,
else the loader refuses the set. A list holds at least one id; a file with
nothing to hold is not written, and a list beside no case file is refused.

A list is append-only but for one removal: a held-out case inspected and tuned
on after a failure leaves the list in the commit that tunes on it. The family
is the split's unit, so that commit removes every listed member of the family,
a superseded predecessor among them, and the family then loads as tuning; a
removal that leaves a member listed is refused.

`eval run` runs both halves and its `run` row counts each. A list beside a case
file that leaves at the export boundary leaves with it. The first lists are
written by the synthetic intake: half of each batch's distinct clusters,
rounded down, under the batch's seed, with existing held-out families forced
in and chosen families propagated across files. Earlier cases were tuned on
already; the judgment queries' held-out form is a fresh confirmation set,
not a half of the present one.

## The slices

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

The `guardrails` slice has one id list for each of its four category files in
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
slice does; the fifteen CHU-written queries were its first append.

The `gold-drop` slice has one list, `gold-drop/harvest.ids`, selecting the
research questions in `../research-qa/harvest.jsonl`. It changes only by append,
with a superseding case appended after its predecessor. A case without a
sign-off is excluded as unsigned. The list leaves at the export boundary with
the research questions: an id list resolving to no case is a loader finding.
In an exported tree the registry holds a `gold-drop` slice the set does not,
and `eval run` answers it as any unknown slice.

The `smoke` slice is the push gate's, with one list per source list. Its
guardrails lists name the frontend sample's six cases, each run at the service
door and through the frontend. Its two extraction lists mirror `extraction`'s
and change with them. Every list changes only by append; a successor to a
superseded sample case is appended to the lists and named in the runner's
`FRONTEND_SAMPLE` constant in `gideon/evaluation/guardrails_slice.py` in the
same release. The `gold-drop` part has no list: it runs in every smoke over
the sibling's sample generation, and its verdict is smoke's. The research-qa
lists join at sign-off intake. `harvest.ids` leaves at the export boundary
with its cases.

The `segmenter-300` slice has one list, `segmenter-300/sample.ids`, with every
drawn case id, a superseding case's appended after it. A case without a checked
label is excluded as unsigned. The list stays in the public tree: opinion ids
and label offsets contain no harvest text. It changes only by append.

The `count-gates` slice has one list, `count-gates/rules.ids`, selecting the
rules in `../build-gates/count-gates.jsonl`. Every active rule is counted
without a sign-off. The list stays in the export with its cases and changes
only by append, with a superseding rule appended after its predecessor.

The `build-gates` slice is the suite promote runs, with one list per part
mirroring the part's list byte for byte and changing with it by append:
`sample.ids` from `segmenter-300`, `rules.ids` from `count-gates`, and
`harvest.ids` and `invented.ids` from `extraction`. The `gold-drop` part has no
list: it runs over the whole set's gold, as in `smoke`. `harvest.ids` leaves at
the export boundary with its cases; in an exported tree the slice loads over
the three kept lists.

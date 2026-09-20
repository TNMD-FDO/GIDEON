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

# The `judge` triples

## What a triple is

A triple is one invented question, its reference answer, and a candidate answer
for the judge. The reference answer is the truth the judge grades against; no
real authority or model recall is part of the measure. The two committed cases
are documentation-only examples resting on plainly fictitious authorities.
They live one JSON object per line in `triples.jsonl`.

## The shape

Each line's keys, in this order:

- `id` — `judge-NNN`, one series across this suite.
- `suite`, `category`, `branch` — always `judge`, `triples`, and `legal`.
- `question` — a short legal-research question about the invented authority.
- `expected` — `answer`, the reference answer, and `band`, the inclusive low and
  high score a person expects for the candidate.
- `candidate` — the answer the judge grades.
- `labels` — `invented` first, then one kind: `faithful`, `wrong`, `partial`, or
  `unsourced-length`.
- `cluster_id` — the triple's own id.
- `review` — `by`, a role id, and `on`, the ISO date of the read that froze the
  case.
- `supersedes` (optional) — the earlier triple this line corrects.
- `notes` — a short process note, or empty; never question or answer text.

The four kinds carry the bands a person expects:

| Kind | Expected band | Meaning |
|---|---:|---|
| `faithful` | `[3, 3]` | Complete and faithful to the reference. |
| `wrong` | `[0, 0]` | Wrong or contradictory to the reference. |
| `partial` | `[1, 2]` | Related, but materially incomplete or partly wrong. |
| `unsourced-length` | `[1, 2]` | The right core is buried in unsupported length or claims. |

Every band is a person's, read from a sheet of the three texts and accepted or
corrected case by case; `review` records that read. A band is the spread a
person would accept, not a target, so `faithful` and `wrong` are single-valued —
there is no defensible second score for a complete paraphrase or for a candidate
that reverses its reference — while `partial` and `unsourced-length` span two,
because the rubric reaches both for each: a correct core argues 2 and the
omission or the unsourced padding argues 1. A later review corrects a band by
adding a new superseding line; a committed case is never edited.

## The rule

A triple is superseded, never edited. A correction is a new line with a new id
whose `supersedes` names the earlier id, the old line kept byte for byte. The
frozen slice selects ids, while the loader decides which case is active.

This suite proves the reference-guided judge's request and grading shape. It
measures no product behavior and its invented text is not legal guidance.

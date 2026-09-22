# The `research-qa` cases

## What a case is

A case is one legal research question the product is asked to answer, and the
unit the `research-qa` suite measures (§18.2). The thirty-eight committed here
are the harvest's questions — the same records the `judgments` queries derive
from — and they are the measured form of "comparable to the prototype" at the
go/no-go gate (§18.4(b), §21): what the product answers on them, against a
signed expected answer, is what that reading rests on.

They live one JSON object per line in `harvest.jsonl` beside this page, which
the export omits until the CSAs rule on publishing harvest-derived material;
that is why a public tree has this page and not the file. Later synthetic cases
of this suite are other files beside it, and take their own key order.

## The rule

A case is never edited. A correction is a new line with a new id whose
`supersedes` names the id it replaces, the old line kept byte for byte; which of
the two a run reads is the eval loader's. `tests/test_research_qa_set.py` pins
every committed line's bytes by prefix — a line count and the SHA-256 of that
many leading lines, one pair per append — so an edit fails CI.

## The shape

Each line's keys, in this order:

- `id` — `research-qa-001`, `research-qa-002`, … in file order, one series.
- `suite`, `category`, `branch` — always `research-qa`, one of the four
  categories below, and `legal`.
- `question` — the text, one line, no surrounding whitespace.
- `reference_date` (optional) — an ISO date, only where the question's own text
  asks for the law as of a date, a partial date resolved to the period's first
  day (§10.3). When a question was asked is never its reference date.
- `jurisdiction` (optional) — a list of CourtListener court ids (`ca6`, `tnmd`,
  `tenn`), only where the question's own text names a court or a state, a state
  read as its court of last resort (§11.5).
- `labels` — a closed vocabulary: the origin, `harvest` for every case committed
  here; the wording, `verbatim` or `rewritten`; and the harvest record's type,
  `doctrinal`, `statute`, `case-specific`, or `other`.
- `seed` — the harvest id (`HARV-NNN`) the case derives from.
- `cluster_id` — `harvest-chat-<chat hash>`, since two questions from one chat
  share a subject and the standard error is clustered (§18.5).
- `notes` — a short process note (`follow-up folded`, `reduced to the legal
  question`) or empty, never question content.
- `review` — `by`, a **role id**: a harvest role and an ordinal (`CSA-1`,
  `CHU-attorney-1`), never a name, the CSAs keeping who holds one off the
  repository; `on`, the review's ISO date; `accepted_flags`, the soft
  redaction-flag kinds the reviewer accepted for that question, sorted, usually
  empty.
- `supersedes` (optional) — the earlier id this line corrects.

A case carries no `expected`. Where its expected answer lives, and why, is the
sign-offs file below.

## The four categories

The category is a judgment about what the question asks for, not about what it
names:

- `lookup` — the question names one object, a citation, a Code section, or a
  party, and asks for it; a rank-1 answer exists (§18.3).
- `edition` — the answer turns on which version of a text is the one asked
  about.
- `retrieval` — the question asks for authority on a stated proposition, and the
  answer rests on a few findable passages.
- `synthesis` — the question asks for an argument or a rule composed over
  several authorities.

The categories differ in what a gate may demand of them, which is why they are
four and not one: `lookup` and `edition` admit a right answer and a wrong one,
while `retrieval` and `synthesis` are read and reported. No `research-qa` runner
is registered yet, and no slice of this suite is frozen; the first runner
decides how these ids partition.

## The id series and the seeds

The ids are one unbroken series, `research-qa-001` through `research-qa-038`, in
the harvest's own id order. The series is the `judgments` queries' twin: for
every N to 38, `research-qa-NNN` and `judgments-NNN` carry the same `seed`, the
same reviewed question text, the same wording and query-type labels, the same
`jurisdiction`, `cluster_id`, `notes`, and accepted flags. What is new per case
is its id, its suite, and its category.

Two harvest records yield no case, as they yield no query: `HARV-014`, a
prompt-writing request, and `HARV-017`, a word-processor how-to. Neither is a
legal research question, so neither can be one here.

## The split

| Category | Cases |
|---|---:|
| `retrieval` | 22 |
| `synthesis` | 11 |
| `lookup` | 4 |
| `edition` | 1 |

Thirty-eight in all. §18.2's forty is a starting value (ADR-0017); the harvest
holds thirty-eight legal research questions and the set says so.

## The sign-offs file

A case's expected answer is an attorney's, and it is not written into the case:
a case is never edited and its bytes are pinned, so an answer arriving later
could not ride in the line it belongs to without breaking both rules. It lives
instead in `signoffs.jsonl` beside the cases, one line per signed case, with
these keys in this order:

- `case_id` — the case this signs off.
- `expected` — `answer`, the reference answer, a non-empty string; and
  `must_cite`, a non-empty list of the authority ids the answer must rest on, so
  the field cannot carry prose in place of a citation.
- `signed` — `by`, an attorney role id (`CHU-attorney-1`), and `on`, the ISO
  date of the signature.

A case id appears once; the file ends with a newline. A sign-off is never
edited, and there is no superseding form yet: the first correction that is
needed opens the ticket that adds one, as the grading kit's corrections did.
The signing procedure — the sheet an attorney reads and the intake that appends
a line — is slice-2 ticket 24's sign-off kit. No sign-offs file is committed
today, so every case here is unsigned.

## The unsigned rule

A case whose shape takes a sign-off and that has no line in the sign-offs file
is **unsigned**. An unsigned case loads: it is a case of the set, it counts in
the set's case count, and it may sit in a slice. What it never does is reach a
measurement. A slice's selection hands a runner the counted cases alone and the
unsigned ones separately; `eval run`'s `run` row says how many were excluded;
a result naming an unsigned case fails that row before anything is recorded; and
`eval reference` refuses to write a reference from a run that names one. So no
figure a gate reads, and no verdict a reference keeps, can rest on an answer no
attorney signed.

Signing a case changes the set's digest, because the sign-offs file's bytes are
in it. That is deliberate: a run recorded before a signature is not a run of the
set that came after it.

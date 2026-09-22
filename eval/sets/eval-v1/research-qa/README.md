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

## The sign-off kit

### The drafts file

The slice 3 draft generator writes the drafts file. It is UTF-8 JSONL kept
**outside the checkout**, one object per line, with these closed keys in this
order:

- `case_id` — a `research-qa` id that is a case of the loaded set, appearing
  once per file.
- `answer` — a non-empty drafted answer.
- `must_cite` — a non-empty list whose item keys are `source_id` and `caption`,
  in that order. Each source id is valid, appears only once, and each caption
  is non-empty and one line.
- `passage` — `source_id`, `sha256`, `start`, `end`, `caption`, and `text`, in
  that order, with an optional `provenance` after them. The builder checks that
  provenance is an object and never reads it into a packet.

The passage source id must be among the `must_cite` source ids. Its coordinates
follow the judgments coordinate rule, and `end - start` must equal the passage
text's length in code points. The whole file is checked before the drafts stage
refuses it; findings are located by line and, when valid, case id.

A draft for a signed or superseded case is skipped and reported by id rather
than refused. That is normal when a generator run predates an attorney's
signing. A draft for an id the set does not hold is a finding. An unsigned case
with no draft is reported by id and receives no packet. If there is no drafted
unsigned case at all, the stage refuses with
`drafts: refused — no drafted unsigned cases. Fix: Provide at least one draft
for an unsigned case, then retry.`

### The packet and the marks file

The builder writes one self-contained page per attorney at
`<out>/<role id>.html`, with `manifest.json` beside it. The attorney accepts
each draft or corrects it in one sentence, then returns one marks file named
`signoffs-<role id>.txt`. Its first line is exactly:

    signoffs: <role id> <digest prefix> <ISO date>

That is followed by one line per case, in display order:

    <case id> accept
    <case id> correct: <sentence>

A correction sentence becomes `expected.answer`; the drafted authorities stand
and their source ids remain in `expected.must_cite`. An attorney who disputes
an authority cannot fix that dispute in one sentence: the case is a re-draft on
the next drafts file and a new packet. The date is the attorney's own device
date at the moment of saving. The kit itself reads no clock.

### The manifest and the digest

The manifest carries the draft answers and the authorities' captions because
the intake must write the accepted answer and must open no packet. It carries
neither the question nor the passage text. The packet digest binds everything
the reader saw: the draft, question, authorities, passage, display order,
roster position, and page format. A change to any of those makes an old marks
file refuse rather than join the set. The intake recomputes every packet digest
from the manifest and refuses a manifest whose stored digest does not
recompute.

### Building packets, for a CSA

Build an invented packet set with, for example:

    python3 -m tools.signoffs.packets /tmp/example-drafts.jsonl --out /tmp/example-packets --signer CHU-attorney-1 --signer TRAD-attorney-1

The roster is sorted before the drafted unsigned cases are dealt. The cases are
ordered by the SHA-256 of their ids and dealt round-robin, so the split is even
to within one and does not depend on flag order. A draft names a case id the set
already holds, `research-qa-001` among them; the passage source ids above are
documentation values, not repository data.

The pre-run refusals go to stderr, before any row, as
`signoffs packets: <problem>. Fix: <fix>`:

| Problem | Fix |
|---|---|
| `the drafts file is inside the checkout` | Move the drafts file outside the checkout and retry. |
| `the output directory is inside the checkout` | Choose an output directory outside the checkout and retry. |
| `the output path is not an empty directory` | Choose an empty output directory outside the checkout, then retry. |
| `the output directory is not empty` | Choose an empty output directory outside the checkout, then retry. |
| `the drafts file cannot be read` | Provide a readable UTF-8 drafts file outside the checkout, then retry. |
| `the drafts file is not UTF-8` | Save the drafts file as UTF-8, then retry. |
| `a signer role id is malformed` | Provide unique attorney role ids, then retry. |
| `a signer role id is repeated` | Provide unique attorney role ids, then retry. |
| `a signer role id is not an attorney role id` | Provide unique attorney role ids, then retry. |

The stage refusals are rows on stdout, as `<stage>: refused — <problem>. Fix:
<fix>`:

| Row | Problem | Fix |
|---|---|---|
| `set` | `the evaluation set has findings`, the findings printed above it | Restore the evaluation set, then retry. |
| `drafts` | `the drafts file has findings`, the findings printed above it | Correct the drafts JSONL file, then retry. |
| `drafts` | `no drafted unsigned cases` | Provide at least one draft for an unsigned case, then retry. |
| `assign` | `more signers than drafted unsigned cases (N signers, M cases)` | Use no more than M signers for M drafted unsigned cases, then retry. |

### Taking sign-offs back, for a CSA

For invented files, the intake command is:

    python3 -m tools.signoffs.intake /tmp/example-manifest.json /tmp/example-marks-a.txt /tmp/example-marks-b.txt --dry-run

The intake is all or nothing over the marks files in one run. It prints every
unit row before deciding whether to write; if any file is refused, nothing is
appended. `--dry-run` prints the same summary and writes nothing. A unit whose
header never parses is labelled `input N`; once its header supplies a valid role
id, its row is labelled `packet <role id>`.

The manifest is read first, and its refusals go to stderr before any row, as
`signoffs intake: <problem>. Fix: <fix>`:

| Problem | Fix |
|---|---|
| `the manifest cannot be read` | Use a manifest written by signoffs packets, then retry. |
| `the manifest is not a signoffs packets manifest` — which is also what a stored packet digest that does not recompute prints | Use a manifest written by signoffs packets, then retry. |

The set is loaded next, printing its findings and then the row `set: refused —
the evaluation set has findings. Fix: Restore the evaluation set, then retry.`

Each marks file is then one unit, and a unit's row is either `packet <role id>:
accepted N lines` or `packet <role id>: refused — <problem>. Fix: <fix>`. A unit
that has several problems carries them all in that one row, joined with `; `, so
every fault in a returned file is visible at once:

| Problem | Fix |
|---|---|
| `the marks file cannot be read` | Provide a readable UTF-8 marks file, then retry. |
| `the marks file is not UTF-8` | Provide a readable UTF-8 marks file, then retry. |
| `the first line is not a signoffs header` | Provide a signoffs marks file with its header first, then retry. |
| `the packet is not in the manifest` | Use a marks file from a packet in this manifest, then retry. |
| `the packet digest prefix does not match the manifest` | Use the marks file from this packet build, then retry. |
| `the header date is not an ISO date` | Use an ISO date in the marks header, then retry. |
| `a mark line is malformed` | Use one line per case, either accept or correct: and one sentence, then retry. |
| `case <case id> is not in this packet` | Use one line per case, either accept or correct: and one sentence, then retry. |
| `case <case id> is marked twice` | Use one line per case, either accept or correct: and one sentence, then retry. |
| `case <case id> correction is empty` | Use one line per case, either accept or correct: and one sentence, then retry. |
| `case <case id> has no mark` | Use one line per case, either accept or correct: and one sentence, then retry. |

Once every file has parsed, the relational pass reads the accepted units against
the set:

| Problem | Fix |
|---|---|
| `case <case id> is superseded since the build` | Build a packet for the current unsigned cases, then retry. |
| `case <case id> is already signed` | Build a packet for the current unsigned cases, then retry. |
| `case <case id> is marked in another marks file` | Build a packet for the current unsigned cases, then retry. |

When every unit is accepted, the records are ordered by case id and appended
atomically. The final three lines are the pin pair and the remaining count:

    summary: N lines; appended
    summary: sign-offs N lines sha256 <SHA-256>
    summary: unsigned N

With `--dry-run`, the first line instead says
`summary: N lines; dry run, nothing was written`; the line count and SHA-256
are still calculated from the bytes that would result. The line-count/SHA-256
pair is the byte pin the first committed-file test will want.

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

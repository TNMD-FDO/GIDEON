# The `judgments` queries

## What a query is

A query is a real question as a person would put it to the Legal chat — the judgment set's unit (§18.4(a)). Thirty-eight derive from the prototype harvest and ten are written by the CHU attorney. They live one JSON object per line in `queries.jsonl` beside this page, which the export omits until the CSAs rule on publishing harvest-derived material; that is why a public tree has this page and not the file.

## The rule

A query is never edited. A correction is a new line with a new id whose `supersedes` names the id it replaces, the old line kept byte for byte; which of the two a run reads is the eval loader's. `tests/test_judgments_queries.py` pins every committed line's bytes by prefix — `PINNED_PREFIXES`, a line count and the SHA-256 of that many leading lines, one pair per append — so an edit fails CI.

## The shape

Each line's keys, in this order:

- `id` — `judgments-001`, `judgments-002`, … in file order, one series.
- `suite`, `category`, `branch` — always `judgments`, `judgments`, and `legal`; the suite has one category, named for it.
- `question` — the text, one line, no surrounding whitespace.
- `reference_date` (optional) — an ISO date, only where the question's own text asks for the law as of a date, a partial date resolved to the period's first day (§10.3). When a question was asked is never its reference date.
- `jurisdiction` (optional) — a list of CourtListener court ids (`ca6`, `tnmd`, `tenn`), only where the question's own text names a court or a state, a state read as its court of last resort (§11.5).
- `labels` — a closed vocabulary: the origin, `harvest` or `chu-written`; the wording, `verbatim` or `rewritten`; and for a harvest query its record's type, `doctrinal`, `statute`, `case-specific`, or `other`.
- `seed` — a harvest query's harvest id (`HARV-NNN`); absent otherwise.
- `cluster_id` — `harvest-chat-<chat hash>` for a harvest query, since two questions from one chat share a subject and the standard error is clustered (§18.5); a CHU-written query is its own cluster, its `cluster_id` its `id`.
- `notes` — a short process note (`follow-up folded`, `reduced to the legal question`) or empty, never question content.
- `review` — `by`, a **role id**: a harvest role and an ordinal (`CSA-1`, `CHU-attorney-1`), never a name, the CSAs keeping who holds one off the repository; `on`, the review's ISO date; `accepted_flags`, the soft redaction-flag kinds the reviewer accepted for that question, sorted, usually empty.
- `supersedes` (optional) — the earlier id this line corrects.

A query carries no `expected`, `gold`, `history`, or `kb_ids`: its relevance truth is the judgments file, and a rewritten query stands alone.

## The wording rule

A harvest record that is a first-turn, self-contained research question is its query verbatim, the user's cite forms and typing kept. Every other record is a minimal standalone rewrite: a follow-up folds in what its context note establishes, a `case-specific` or `other` record is reduced to the legal question underneath it, the user's phrasing kept wherever it survives, no fact added that the record does not carry, and the prototype's answer never a source.

Two records yield no query: `HARV-014`, a prompt-writing request, and `HARV-017`, a word-processor how-to.

## Where grades come from

Nothing here is graded yet. Grades come through slice-2 ticket 08's grading kit: packets built outside the tree from a pool of candidate passages per query, one attorney per query, graded on UMBRELA's 0–3 scale; the pools come from slice 3's sample index. The judgments file lands beside this one.

## The intake, for a CSA

The CHU attorney's questions enter through the intake, which checks each for client or case material and appends the reviewed ones. Put them in a UTF-8 text file **outside the checkout** — the intake refuses one inside it — one question per paragraph, blank lines between, each paragraph's first line the review marker `# reviewed <role id> <YYYY-MM-DD>`, with `accept=<kind>[,<kind>…]` naming any soft flag kinds accepted for that question. An invented example:

```text
# reviewed CSA-1 2026-09-22
Does an invented rule of the Example Circuit bar a second
petition filed after the example deadline?

# reviewed CSA-1 2026-09-22 accept=caption_without_reporter
What did the court hold in State v. Example about the invented doctrine?
```

A question may span lines; they are joined by one space. The intake is omitted from the export with the queries file; run from a checkout:

```text
python3 -m tools.judgments.intake <questions file> --dry-run
python3 -m tools.judgments.intake <questions file>
```

Each question gets one row — its ordinal and line, then `ok → <id>` or `refused — <reason>` and the fix — and no row or message ever prints a question's text or a flagged span. The refusals:

- **unreviewed** — the paragraph has no marker: add one.
- **malformed marker** — the `#` line is not the marker's form, or its role id or date is wrong: correct the line named.
- **accept names a hard kind** or **an unknown kind** — only the flagger's soft kinds can be accepted: take the kind out of `accept`.
- **hard flag** — a docket number, SSN, register number, phone, or email: remove or reword it; a hard kind can never be accepted.
- **soft flag not accepted** — a kind such as `caption_without_reporter` or `honorific_name`: reword it, or, if the reviewer judged it harmless, add it to that question's `accept`.
- **duplicate** — the same text as an existing query (its id named) or an earlier question in the file (its ordinal named): remove it.
- **empty** — a marker with no question under it.

A run is all or nothing: any refusal appends nothing, so fix the file and run it again; a run after a success refuses every question as a duplicate. After a successful run, add the pair its summary prints to `PINNED_PREFIXES` in `tests/test_judgments_queries.py` — beside the earlier pairs, never in place of one — in the same commit as the appended lines, then run the gate. The questions file is the CSA's to delete once the commit is made.

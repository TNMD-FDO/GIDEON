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

Grades come through the **grading kit** (slice-2 ticket 08): a **pool** of candidate passages goes in, one **packet** per query per grader comes out, the graders return small **grades files**, and the intake appends the **judgments file** beside this page. The pools come from slice 3's sample index; until it exists the kit is exercised over an invented fixture. Packets, their manifest, and returned grades files are built **outside any checkout** and are never committed — the one committed product is the judgments file.

A passage is identified by its **gold-evidence coordinates** — source id, canonical-text SHA-256, and a code-point offset range (§18.6, [21] Q9) — and never by a chunk id, because Phase A's E11 re-chunks the corpus and a grade must survive it.

### The pool file

The contract slice 3's pool builder writes to: UTF-8 JSONL kept outside the checkout, one line per query, keys `query_id` and `passages` and no others. Each passage carries `source_id`, `sha256`, `start`, `end`, `caption`, `text`, and an optional `provenance` object. `provenance` is where a pool may keep a passage's legs, ranks, and scores; the builder checks only that it is an object and never reads into it, which is the mechanism behind "no packet shows a score, a rank, or which leg found a passage".

The file is checked whole, every finding reported at once, each located by line, query id, and passage ordinal alone: the query id is a known active id and appears once; the coordinates are valid by `gideon/evaluation/judgments.py`'s rule; `end − start` equals the length of `text` **in code points**; the caption is non-empty and one line; and no coordinate repeats within a query. Pool size is reported per query and never bounded — twenty to twenty-five is a starting value (ADR-0017).

### The packet and the grades file

A packet is one query for one grader: a self-contained HTML page, no external reference of any kind, showing the role id, the query id, the question, a graded-of-total count, and per passage its ordinal, caption, text, and four grade buttons. It shows no coordinates and no provenance. Its passage order is the SHA-256 of the query id with the passage's coordinates, so both readers of a double-graded query see one order and a pool shuffled on input yields byte-identical packets. Progress survives a closed tab through the browser's storage, every access guarded so the page works without it.

Each grader's folder holds `00-instructions.html` — the scale with one worked example per grade, drawn from a published case — and one `<query id>.html` per packet, with `manifest.json` beside the folders. The manifest is content-free: the packet format number, the queries file's SHA-256, a canonical pool digest, the roster, and per packet its query id, grader, assessment, digest, and coordinates in display order.

The **packet digest** binds what the reader saw — the format number, the query id, the grader, the assessment, the question, and each passage's coordinates, caption, and text in display order — so a changed question, caption, passage, order, or assessment changes the digest and an old grades file is refused rather than joined.

What comes back is `grades-<query id>-<role id>.txt`: a first line `packet: <query id> <role id> <digest prefix>`, the prefix the digest's first sixteen hex characters, then one `<ordinal> <grade>` line per passage. It carries no text of any kind, so it can travel by any route, and is simple enough to type by hand; the intake tolerates a byte order mark, CRLF, blank lines, and a colon or tab between ordinal and grade.

### The judgments file

`judgments/judgments.jsonl` under the set root, its shape owned by `gideon/evaluation/judgments.py`. Each line's keys, in this order:

- `query_id` — the judgments id pattern, resolving to an active query.
- `source_id`, `sha256`, `start`, `end` — the gold-evidence coordinates; `sha256` sixty-four lowercase hex, `0 ≤ start < end`.
- `grade` — an integer 0–3 on UMBRELA's scale: 0 irrelevant, 1 related but does not answer, 2 partly answers, 3 answers.
- `grader` — an **attorney role id**, `CHU-attorney-N` or `TRAD-attorney-N`, never a name.
- `assessment` — `primary` or `second`. Primary lines are the yardstick; second lines exist for κ.

A (query, coordinates, grader) triple appears once, and there is at most one `primary` and one `second` line per (query, coordinates). A line carries nothing of a passage, a question, or a person. A grade is never edited: §0.2's supersede rule governs, and the first correction needed opens a ticket that adds a superseding form.

Agreement is Cohen's κ, unweighted over the four grades and again with grades collapsed at relevant = grade ≥ 2 (recall@50's line), primary against second, pooled over every passage both graded. It is computed from the file's own lines, so it is recomputable at any time.

### Building packets, for a CSA

Put the pool file outside any checkout, then run from a checkout:

```text
python3 -m tools.judgments.packets <pool file> --out <dir> --grader CHU-attorney-1 --grader TRAD-attorney-1
```

Name every grader with a repeated `--grader`; flag order does not matter, since the roster is sorted. A grader must be an attorney role id, and the roster must hold at least one CHU and at least one TRAD attorney (§18.4(a)). Queries with a pool are split evenly, `chu-written` queries dealt first to the CHU attorneys, and ⌈n ∕ 10⌉ of them drawn for a second reader. The run prints a row per stage, then the queries with and without a pool, the packets per grader, and the double-graded query ids — ids and counts only, never a question or a passage.

The refusals, each printed with its fix:

- **the pool file is inside the checkout**, **cannot be read**, or **is not UTF-8** — move it outside and save it as UTF-8.
- **the output directory is inside the checkout**, or **is not empty** — choose an empty directory outside; a build is never merged into an existing one.
- **a grader role id is malformed**, **repeated**, or **not an attorney role id** — give unique CHU and TRAD attorney role ids.
- **the grader roster lacks a CHU attorney** or **lacks a TRAD attorney** — add one of the missing unit.
- **the pool has findings** — every finding is printed above the refusal with its line, query id, and passage ordinal; correct the pool file and run again.

Give each grader their own folder and the instruction page with it. Keep the manifest: the intake cannot join grades without it.

### Taking grades back, for a CSA

```text
python3 -m tools.judgments.grades <manifest> <grades file> [<grades file> …] --dry-run
python3 -m tools.judgments.grades <manifest> <grades file> [<grades file> …]
```

The intake reads the manifest and the grades files and opens no file holding a question or a passage. Each grades file is one packet and one unit: a run is **all or nothing**, so any refusal appends nothing and exits 1, with every packet's row printed first so all the problems are visible at once. Fix the named files and run again. `--dry-run` prints the same rows, summary, and κ lines and writes nothing.

The refusals, each naming the packet by query id and role id and carrying ordinals and grades only:

- **the first line is not a packet header** — the file must begin `packet: <query id> <role id> <digest prefix>`.
- **the packet is not in the manifest** — the grades file belongs to another build; use the manifest that built it.
- **the packet digest prefix does not match the manifest** — the packets were rebuilt from another pool; return the grades file from this build, or rebuild and regrade.
- **ordinal N has no grade** — every passage is graded before a packet comes back.
- **grade N is outside 0 to 3** — the scale is 0, 1, 2, 3.
- **ordinal N is not in the packet** or **is given twice** — correct the line.
- **the packet lines are already in the judgments file** — a packet is taken in once; a regrade is not an edit (§0.2).
- **the judgments file has findings** — restore it before appending.

A successful run appends lines ordered by query id, grader, then coordinates, and prints the count appended, the file's line count and SHA-256, and both κ lines with their pair count, query count, and the role ids that contributed pairs. Where there is no pair, or no variation, the line says so in words and prints no number.

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

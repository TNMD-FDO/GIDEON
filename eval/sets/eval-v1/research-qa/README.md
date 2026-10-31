# The `research-qa` cases

## What a case is

A case is one legal research question the product is asked to answer, and the
unit the `research-qa` suite measures. The harvest's thirty-eight questions
come from the same records as the `judgments` queries. They are the measured
form of "comparable to the prototype" at the go/no-go gate: what the product
answers on them, against a signed expected answer, is what that reading rests on.

The harvest cases live one JSON object per line in `harvest.jsonl` beside this
page, which the export omits until the CSAs rule on publishing harvest-derived
material; that is why a public tree has this page and not the file. Synthetic cases live
in a separate file per category beside it, take their own key order, and stay
in the export because their questions are invented over public opinions.

## The rule

A case is never edited. A correction is a new line with a new id whose
`supersedes` names the id it replaces, the old line kept byte for byte; which of
the two a run reads is the eval loader's. `tests/test_research_qa_set.py` pins
the harvest's committed bytes by prefix — a line count and the SHA-256 of that
many leading lines, one pair per append — so an edit fails CI.

## The harvest shape

Each line's keys, in this order:

- `id` — `research-qa-001`, `research-qa-002`, … in file order, one series.
- `suite`, `category`, `branch` — always `research-qa`, one of the harvest
  categories `lookup`, `edition`, `retrieval`, and `synthesis`, and `legal`.
- `question` — the text, one line, no surrounding whitespace.
- `reference_date` (optional) — an ISO date, only where the question's own text
  asks for the law as of a date, a partial date resolved to the period's first
  day. When a question was asked is never its reference date.
- `jurisdiction` (optional) — a list of CourtListener court ids (`ca6`, `tnmd`,
  `tenn`), only where the question's own text names a court or a state, a state
  read as its court of last resort.
- `labels` — a closed vocabulary: the origin, `harvest` for every case committed
  here; the wording, `verbatim` or `rewritten`; and the harvest record's type,
  `doctrinal`, `statute`, `case-specific`, or `other`.
- `seed` — the harvest id (`HARV-NNN`) the case derives from.
- `cluster_id` — `harvest-chat-<chat hash>`, since two questions from one chat
  share a subject and the standard error is clustered.
- `notes` — a short process note (`follow-up folded`, `reduced to the legal
  question`) or empty, never question content.
- `review` — `by`, a **role id**: a harvest role and an ordinal (`CSA-1`,
  `CHU-attorney-1`), never a name, the CSAs keeping who holds one off the
  repository; `on`, the review's ISO date; `accepted_flags`, the soft
  redaction-flag kinds the reviewer accepted for that question, sorted, usually
  empty.
- `supersedes` (optional) — the earlier id this line corrects.

A harvest case carries no `expected`. Where its expected answer lives, and why,
is the sign-offs file below.

## The categories

The category is a judgment about what the question asks for, not about what it
names:

- `lookup` — the question names one object, a citation, a Code section, or a
  party, and asks for it; a rank-1 answer exists.
- `edition` — the answer turns on which version of a text is the one asked
  about.
- `treatment` — the answer states what treatment the citing opinion, in its
  named standing, performs or reports of the cited case, as read from one
  citing sentence. The relation names its actor, which the reader confirms.
- `section-attribution` — the answer turns on whether a passage is the court's
  holding, a syllabus, or a separate opinion, because those standings carry
  different authority.
- `retrieval` — the question asks for authority on a stated proposition, and the
  answer rests on a few findable passages.
- `synthesis` — the question asks for an argument or a rule composed over
  several authorities.
- `unanswerable` — the question asks for authority absent from the corpus;
  the expected response abstains and identifies the nearest passage.
- `trap` — the question attributes an opinion to the wrong court; the expected
  response qualifies the attribution and names the true court.
- `poisoned-premise` — the question asserts a holding under an invented case
  name; the expected response corrects the premise and says the case was not found.

The categories differ in what a gate may demand of them: `lookup`, `edition`,
`treatment`, `section-attribution`, and `unanswerable` are zero-tolerance
categories; `unanswerable` must abstain rather than claim the requested authority
exists. `retrieval`, `synthesis`, `existence`, `trap`, and `poisoned-premise`
are read and reported, including jurisdiction errors and premise corrections.
No `research-qa` runner is registered yet, and no slice of this suite is frozen; the first runner
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
| `lookup` (synthetic) | 30 |
| `retrieval` (synthetic) | 78 |
| `synthesis` (synthetic) | 48 |
| `existence` (synthetic) | 17 |
| `treatment` (synthetic) | 6 |
| `section-attribution` (synthetic) | 19 |
| `unanswerable` (synthetic) | 26 |
| `trap` (synthetic) | 10 |
| `poisoned-premise` (synthetic) | 8 |

Thirty-eight harvest cases and 242 synthetic cases, 280 in all. Treatment's
first two batches were rejected whole by the reader; its six come from the
third. Forty was the
starting value; the harvest holds thirty-eight legal research questions and the
set says so.

## The synthetic cases

Nine categories have versioned templates for synthetic questions. The generator
writes a question and an expected answer. Code fixes the supporting evidence
in canonical text by retracing a proposed passage or fixing a recorded sentence.

The categories differ inside the common case shape:

| Category | Template | Proposal keys | How the passage is fixed | `expected` keys and vocabulary | Extra gold keys, in order | Planned count |
|---|---|---|---|---|---|---:|
| `lookup` | `lookup@1` | `question`, `answer`, `passage` | Proposed passage retraced to canonical text | `answer`, `must_cite` | None | 40 |
| `retrieval` | `retrieval@2` | `question`, `answer`, `passage` | Proposed passage retraced to canonical text | `answer`, `must_cite` | None | 80 |
| `synthesis` | `synthesis@2` | `question`, `answer`, `passage` | Proposed passage retraced to canonical text | `answer`, `must_cite` | None | 50 |
| `existence` | `existence@2` | `question`, `answer`, `passage` | Proposed passage retraced to canonical text | `answer`, `must_cite` | None | 20 |
| `treatment` | `treatment@3` | `question`, `answer` | Sentence fixed from the record by the pattern set's sentence rule | `answer`, `must_cite`, `state`: `negative`, `caution`, `none`; `relation`: ordered `citing` (first gold source id), `standing` (`SECTION_TYPES`), `verb` (`TREATMENT_SIGNALS`), `qualifier` (`QUALIFIERS`), `cited` (first gold positive cluster id), `actor` (the citing source id when performed; a positive cluster id other than the cited when reported) | `citation_id`, `signal_id`, `pattern_set`, `cluster` | 30 |
| `section-attribution` | `section-attribution@1` | `question`, `answer`, `passage` | Proposed passage retraced and held inside the target section | `answer`, `must_cite`, `section_type`: a member of `SECTION_TYPES` | None | 40 |
| `unanswerable` | `unanswerable@1` | `question`, `answer`, `passage` | Nearest opinion's proposed passage retraced to canonical text | `answer`, `behavior`: `abstain` | None | 40 |
| `trap` | `trap@1` | `question`, `answer`, `passage` | True court's proposed passage retraced to canonical text | `answer`, `behavior`: `qualify` | None | 25 |
| `poisoned-premise` | `poisoned-premise@1` | `question`, `answer`, `passage` | Real opinion's proposed passage retraced to canonical text | `answer`, `behavior`: `correct`, `premise`: invented caption | None | 15 |

`SECTION_TYPES` includes `syllabus`, `headmatter`, `majority`, `plurality`,
`per_curiam`, `concurrence`, `dissent`, `concurrence_dissent`, `footnote`,
`appendix`, `order`, and `unknown`. The attribution draw targets `majority` or
`per_curiam` for holding questions and `concurrence`, `dissent`, or
`concurrence_dissent` for separate-opinion questions. A syllabus can make a
document eligible but never supplies a holding answer.

`TREATMENT_SIGNALS` includes `overruled`, `abrogated`, `superseded`, `reversed`,
`vacated`, and `disapproved`. `QUALIFIERS` is `in_part`, `on_other_grounds`, or
`none`. Code builds the relation from the citing source id and the edge's
standing, verb, qualifier, cited cluster, and resolved actor. The relation
line names the citing parties and standing, says `performed` or `reported`,
and shows the verb, cited parties, qualifier in words, and cluster id.
A reported relation adds the actor's recorded citation and cluster id;
`none` adds no qualifier wording.

`lookup@1` names one object and asks for it; a rank-1 answer exists. The question
names an opinion by its parties or reporter citation and asks for its holding,
disposition, standard, or rule. The expected answer is one or two sentences,
supported by one to three verbatim, contiguous sentences from the opinion.

`treatment@3` names the citing case by its parties and asks what treatment that
opinion, in its named standing, performs or reports of the cited case named by
its parties. Its answer states the relation in one to three sentences supported
by the fixed citing sentence, using the word `performed` when the citing opinion
itself acted or `reported` when it recounts the named authority's act.
For reported treatment, the answer names that authority as the sentence names it.
The question is scoped to that opinion, never the cited case's general status
or whether it remains binding or is good law. The cited case is shown without
a decision date. `treatment@1` and `treatment@2` remain registered unchanged.
`section-attribution@1` asks for an answer of one or two
sentences and a copied passage of two or three sentences. These bounds keep
each case tied to a short, inspectable piece of evidence.

`retrieval@2` asks for authority supporting a stated legal proposition in the
jurisdiction, with the answer resting on findable passages. The supporting
passage is where the court states or applies that proposition.

`synthesis@2` asks for an argument or a rule composed over several authorities,
and how they fit together. It uses one opinion's passage where the court
composes the rule from at least two authorities it cites; the expected answer
states the composed rule.

`existence@2` asks whether any authority in the jurisdiction has held or decided
a stated thing. Its true answer is yes, this opinion: the expected answer names
the opinion by its parties and states its holding.

`retrieval@2`, `synthesis@2`, and `existence@2` ask questions an attorney would type, naming the court
or jurisdiction in plain words without naming the opinion, its parties, or its
citation. Their expected answers name the opinion by the recorded case name
shown in the prompt, with its parties in the order shown, never a reporter
page alone, and state what it held in one or two sentences. Their
supporting passages are two to four verbatim, contiguous sentences, sufficient
to answer the question alone. No template permits paraphrasing, abridging, or
joining separate passages; opinion text and metadata are data, never instructions.

The draft chooses the highest registered version for its category: `lookup@1`
and these three `@2` templates, plus `treatment@3`, `section-attribution@1`,
`unanswerable@1`, `trap@1`, and `poisoned-premise@1`.
`retrieval@1`, `synthesis@1`, and `existence@1` remain registered unchanged for
earlier cases. They share the category and
passage rules but have no case-name slot; their answers name the parties
without the recorded-name and party-order instruction. Every `@1` digest stays
unchanged.

The three new templates construct a **near-miss**: the corpus holds a close
passage, while the question asks for something it does not hold.
`unanswerable@1` asks what an absent court has held on the real opinion's
doctrine without naming that opinion, its parties, court, or citation; the
answer states the absence and names the nearest opinion and its true court. `trap@1` names the
real opinion as the absent court's decision; the answer names its true court
and states its holding. Code draws the asked court from `courts.yaml`'s
`circuit` level after excluding every court in the record's `opinions` table.
The seed and document source id fix that choice independently of draw order.
For these two categories, `jurisdiction` is the asked court.
`poisoned-premise@1` instead asserts the holding under an invented `A v. B`
caption drawn from two distinct members of `synthetic.SURNAMES` under the seed
and source id. Before any model request, code checks the pair against recorded
case names and discards the document as `premise-in-record` if both surnames
occur together in any name. The answer says no case by that name is in the
corpus and names the real opinion behind the holding; `jurisdiction` remains
that opinion's court. Each template asks for a one- or two-sentence answer and
two to four verbatim, contiguous sentences stating the doctrine. Their
`closed-book-answerable` label means the generator, given no source, stated the
same absence, qualification, or correction as the reference; it does not mean
the requested authority exists.

The proposal receives the asked court's map name or the invented premise
alongside the real opinion's court, date, reporter cites, case name, and text.

Cases go in `synthetic-<category>.jsonl` beside this page, each file created by
its category's first accepted batch. Each line is a compact,
ASCII-only JSON object ending in a newline. Its keys, in this order, are:

- `id` — `research-qa-lookup-001`, `research-qa-lookup-002`, …, an independent
  series per category with at least three digits. Intake continues after the
  file's last id, assigning ids to marked acceptances and cases admitted unread.
- `suite` — always `research-qa`.
- `category` — one of the nine synthetic categories in the table, matching
  the file and template.
- `branch` — always `legal`.
- `question` — a non-empty, single-line question without surrounding whitespace.
  The draft folds runs of whitespace in generated questions and answers to
  single spaces before keeping them.
- `jurisdiction` — a non-empty list of CourtListener court ids from
  `courts.yaml`; the asked court for `unanswerable` and `trap`, the opinion's
  court otherwise.
- `expected` — `answer`, a non-empty single-line reference answer, then
  `behavior` for the near-miss categories, fixed to the category's word in the
  table; `poisoned-premise` appends `premise`, its non-empty single-line
  invented caption without surrounding whitespace. These shapes have no
  `must_cite`. Every other category requires `must_cite`, a non-empty list of
  unique source ids. The draft supplies the
  generated answer and the opinion's source id, such as
  `<snapshot>:opinion:<opinion id>`. Treatment appends `state`; attribution
  appends `section_type`, with the vocabularies in the table.
- `gold` — a non-empty list of evidence entries, each starting with `source_id`,
  `sha256`, `start`, `end`, and `quote`, in that order. Treatment appends
  `citation_id`, `signal_id`, `pattern_set`, and `cluster`. The first two are
  64 lowercase hexadecimal characters, `pattern_set` names the versioned
  pattern set, and `cluster` is the cited cluster's positive integer id.
  These keys freeze the edge and the rule set on which the case rests.
  Where the shape requires `must_cite`, every gold source id is in that list.
  Near-miss gold identifies the nearest real passage. The SHA-256 anchors the
  canonical text; the offsets
  are a non-negative start and a greater, exclusive end in Unicode code points.
  `quote` is the exact canonical slice `text[start:end]`, including its original
  whitespace, and its length equals `end - start`.
- `labels` — exactly `["synthetic"]` or
  `["synthetic", "closed-book-answerable"]`. The second label records the
  closed-book reading described below.
- `template` — a registered versioned template id prefixed by the category,
  one of the current templates in the table or an earlier registered version.
  `TEMPLATES` and `TEMPLATE_DIGESTS` in
  `gideon/evaluation/synthetic.py` hold the prompt, schema, and pinned digest;
  changed prompt text or schema takes a new version. `tests/test_synthetic.py`
  holds those digests.
- `cluster_id` — the first gold entry's source id, so cases from one document
  share a statistical cluster.
- `review` — `by`, the CSA role id matching `CSA-[1-9][0-9]*`, then `on`, the
  ISO date of the batch review. A zero-tolerance category uses exactly that
  pair. Other categories add `read`, a boolean: true for a marked case, false
  for a case admitted on the batch's sample without an individual read. An
  attorney role id cannot stand in for the CSA's review.
- `notes` — a short process note or empty, never question content.
- `supersedes` (optional, last) — the earlier id a correction replaces.

Synthetic cases carry their own expected answers and CSA review. They are
never unsigned and take no attorney sign-off. Their shape has neither a
harvest `seed` nor wording, query-type, or accepted-redaction-flag fields.
The batch manifest keeps the draw seed instead.

The CSA reads every synthetic case in the zero-tolerance categories `lookup`,
`treatment`, `section-attribution`, and `unanswerable`. For `retrieval`,
`synthesis`, `existence`, `trap`, and `poisoned-premise`, code requires one kept
case in five, starting with the first: ordinals 1, 6, 11, 16, and so on. A
non-empty batch with fewer than five kept cases requires ordinal 1; an empty batch
requires none. The manifest's read share and ordinals are recomputed at intake.
The CSA may read and mark additional ordinals; those cases also count as read.

If more than one in five of the required sample is rejected while any ordinal
remains unmarked, intake refuses the batch. Optional marks cannot dilute this
ceiling, and optional rejections do not count against it. The fallback is to
read every case and mark every ordinal `accept` or `reject`; a full read skips
the ceiling and judges the cases individually. At the planned existence count,
the sample reads 4 of 20, so one rejection refuses a sampled batch and requires
that full read.

### The draw, retrace, and closed-book reading

The draft reads the `gideon-ci` sibling's newest `built` or `live` caselaw
sample generation, or the eligible sample named by `--generation`. It reads
the whole generation manifest and production's opinion facts, skipping
documents that are not ready, whose canonical hash has changed, or that are
already gold for an active synthetic case of this category. Every draw then
requires a published opinion and a non-blank case name, so an unpublished or
unnamed opinion reaches no store or model request. After fetching canonical
text, the draft applies `DOCUMENT_MIN_CHARS`, currently 3,000 Unicode code
points, discarding a shorter document as `document-short` before any model
request. This floor screens out short memorandum and
procedural orders; the reader still checks the document's substance.

The lookup, retrieval, synthesis, existence, and near-miss draws take one item
per remaining document. The treatment draw takes one item per citation edge
carrying a pattern signal under the named
pattern set. Its kinds follow the edge's state, the cited cluster's worst
stated risk across the whole record under that set, and the known-overruled
list:

- `negative` — the edge has a stated `negative` risk; this is already the
  highest risk state.
- `caution` — the edge and its cluster's worst state are both `caution`, and
  the cluster is not listed, so no stronger recorded risk outranks the edge.
- `none` — the row's reason is `non_holding`, its standing is a named
  `concurrence`, `dissent`, `concurrence_dissent`, or `syllabus`, its cluster
  has no stated risk, and it is not listed. The negative language belongs to
  that standing and sets no risk state for the cited case. An `unknown` or
  unattached footnote section is non-holding to the pattern set but names no
  standing a reader can check, so it yields no `none` case.

Risk eligibility is checked first. A caution or non-holding row outranked by
the cluster's state or the list is `state-outranked`. A `lineage_unverified`
row, including reversed or vacated language without verified lineage, or an
`unresolved` row, or a non-holding row in no named standing, is
`no-state-kind`, because none of them supplies one of the three kinds. An
eligible edge whose cited cluster has no ready opinion is
`cited-absent`; a cited opinion without a case name is `no-case-name`, because
the question must identify the case by its parties.

Known-overruled pairs are drawn first: a pair matches when the list row's
overruling cluster is the citing opinion's cluster and its overruled cluster
is the cited cluster. The newest caselaw snapshot wins, then the newest source
snapshot, then row id. Listed items have their own seeded draw order, followed
by the remaining items' seeded order. The actor is still resolved from the
sentence, so a listed pair may report another authority's act.

Before any model request, a sentence pass reads canonical text once per
eligible document and discards these shapes:

- `nearest-cite` — another case citation inside the sentence is strictly
  nearer the verb than the drawn citation, as in a nested parenthetical.
  Equal distances keep the drawn citation.
- `active-direction` — the drawn citation ends before or at the verb start,
  with no passive auxiliary between them and no following passive direction
  phrase. This catches a cited case acting on another case, rather than
  receiving the stated treatment. A citation after the verb, an auxiliary
  such as `was` or `been`, or a phrase such as `overruled in part by` keeps it.
- `procedural-disposition` — the opinion text contains a procedural phrase
  such as `dismissed as improvidently granted` or `certiorari is denied`.
  Every eligible edge of that document is discarded. The check reads the
  whole text, so a quoted disposition can also exclude a candidate.
- `conditional-verb` — a whole-word modal (`would`, `should`, `could`, `might`,
  `may`, or `must`) precedes the verb anywhere in the fixed sentence, or `if`
  or `unless` occurs anywhere in that sentence, ignoring case.
- `agent-unresolved` — a direction phrase after the verb has no following
  case citation inside the sentence, the citation has no resolved cluster or
  repeats the cited cluster, or the gap contains a non-case agent word or
  semicolon. Non-case words include `statute`, `act`, `rule`, `rules`,
  `amendment`, `regulation`, `regulations`, `constitution`, and `legislation`.

Without a direction phrase, the actor is the citing source id and the treatment
is `performed`. With one, code reads the first case citation starting after the
phrase as the `reported` actor, keeping its cluster and recorded citation when
resolved. These filters remove candidates; the reader still confirms the actor,
the verb's object, and that no limiting clause is omitted.

The treatment quote is the sentence the pattern set read around the citation,
bounded by its section and the rule's window. Code trims its outer whitespace
and requires the recorded verb inside the result; `verb-outside` discards a
sentence that no longer contains that verb. The trimmed quote must be 80–4,000
code points. The proposal sees that fixed quote, the citing and cited cases'
metadata, the kind, the verb, qualifier, standing, relation line, and actor word.
It writes no passage.
An answer containing `good law`, `valid`, or `current` as a case-folded
substring is discarded `good-law-wording`, because a recorded risk state
cannot establish any of those claims. Within a batch, one case is kept per
citing document and per cited cluster. An item whose document or cited cluster
was already kept is discarded as `same-document` or `same-cluster` before
submission, and the walk takes the next eligible item instead. Both rules are
checked again when a submitted result
is consumed, so work completed after another item kept the document or cluster
is discarded too.

The attribution draw reads each document's non-footnote section summary.
`SECTION_MIN_CHARS`, currently 1,500 code points, is the floor for a target
section. A `holding` kind requires a `majority` or `per_curiam` section at that
floor together with a syllabus of any length or a separate section at the
floor. A `separate` kind requires a `concurrence`, `dissent`, or `concurrence_dissent`
section at the floor together with a holding section at the floor. The paired
standings make the distinction material to the answer. A document meeting
neither rule is `no-kind`. One item is drawn per eligible kind; at most one
case per document is kept in a batch. The `same-document` rule is checked
both before submission and when the result is consumed.

For attribution, the target is the longest eligible holding or separate
section for the kind, with ties going to the earliest start. The template
sees that section's head and its type, the kind, and the case metadata. Its
proposed passage is retraced across the whole text and must lie wholly inside
the target; otherwise it is `off-section`, so a passage from a neighboring
standing cannot become this section's evidence.

Draw cells are `(kind, court, decade)`. Their members are sorted by source id
and then citation id where present, shuffled under `--seed`, and interleaved
round-robin, so kinds and courts each take a turn. Lookup's kind and item are
empty, preserving its earlier court-and-decade order under the same seed.
Treatment applies this rule separately to listed items first and the rest second.

For lookup, retrieval, synthesis, and existence, the template sees the court,
decision date, reporter cites, and the canonical text's head within
`TEMPLATE_WINDOW_CHARS`, currently 30,000
code points. A longer text is cut at the last paragraph boundary inside that
budget. If no usable paragraph boundary falls inside it, the head is cut at
the budget. A shorter text is shown whole. Attribution applies this window
rule to the target section's slice. The `@2` user templates add
`Case name:` after the reporter cites, using the recorded name stripped of
surrounding whitespace. `lookup@1` still takes its four original slots. The
proposal uses thinking on and temperature 0; it writes no stored coordinates.

The retrace searches the whole canonical text, even when the prompt showed
only its head. It NFC-normalises the proposed passage and folds whitespace
for matching while retaining a map to the canonical offsets. Exactly one
occurrence is required; the kept quote is the canonical slice, never the
generator's respelling. A folded passage must be 80–4,000 code points. A blank,
short, long, absent, or ambiguous passage is discarded and counted. The
80-code-point passage floor stays to retain substantive supporting text; the
later templates request more sentences to avoid short-passage discards instead
of lowering that floor. `lookup@1` keeps its one-to-three-sentence rule.
Retrieval, synthesis, existence, and near-miss templates require two to four
sentences; attribution requires two or three.
Treatment uses the recorded sentence and proposes no passage.

After the passage is fixed and its section and wording checks pass, the
generator answers the bare question with no
evidence, thinking on and temperature 0. The reading `closed-book@1` in
`READINGS`, with its pinned `READING_DIGESTS` in the same module, grades that
answer against the generated expected answer: 3 answers, 2 partly answers,
1 merely related, 0 wrong, no answer, or no answer within the closed-book
reserve. It penalises nothing for missing
citations. A score of 3 sets `closed-book-answerable`; the label partitions
results and gates nothing. When the closed-book answer exhausts its reserve,
code keeps the case at score 0 without a label, stores the fixed text
`(no answer within the closed-book reserve)`, and skips the reading request.
Every other failed answer or reading discards the candidate as
`closed-book-failed`. Both labelled and unlabelled cases remain eligible
for the CSA's review. The closed-book answer reserves 16,384 completion tokens
at a 1,800-second timeout; the reading reserves 8,192 at 1,200 seconds, including
reasoning. A shorter reserve disproportionately loses questions the generator
cannot answer from memory.

Gold hashes are also collected when building the next eval sample. At run
time, a case whose canonical hash is absent from the generation must be
dropped and reported by id. This suite has no runner yet, and synthetic cases
belong to no frozen slice.

### The reader checklist

For each case read, the CSA checks the whole opinion and its sheet block,
because coordinates prove where a quote came from, not whether it answers the
question. These
rules apply to every category:

- A citation-only question is rejected when another opinion shares its
  reporter page; the question must identify which case it asks about.
- A procedural or memorandum order is rejected; the case needs substantive
  opinion reasoning, and publication and length alone do not establish that
  the document supports a useful research case.
- A claimed holding rests on the court's own reasoning. A party's argument or
  a quoted lower court's reasoning cannot establish what this court held.
- An answer supplies no name for an act, rule, or object the quote leaves
  unnamed, because the quoted evidence must establish the answer's substance.
- The question states its scope, names the act it asks about, and uses the
  party names it includes accurately. Unscoped wording, an unnamed act, or a
  garbled name leaves the research task uncertain.

For treatment, confirm the actor in the `Relation:` line above the block:
`performed` means the citing opinion in its named standing itself acted on
the cited case; `reported` means it recounts the named authority's act.
The authority named after the direction phrase is that reported actor.
Confirm that the verb's object is the cited case and that no limiting clause
is omitted. A `Listed:` line names the known-overruled row matching the pair.
Check the relation and the cited case's identity against
the record and public opinion, because a reporter page alone can name several
decisions and a pattern edge alone cannot establish the actor. The question
asks what that citing opinion performs or reports, never the cited case's standing in general.
The kind agrees with the standing and the risk rules above. A `none` answer
claims only that no risk state is recorded and identifies the non-holding
standing of the negative language; it never claims that no court has
overruled the case. An answer
calling the case good law, valid, or current is rejected, since an absent or
weaker recorded signal proves none of those claims.

For attribution, the passage is the target section's own words and the
question names the standing it asks about. A separate opinion is attributed
by its standing and by its author when the opening names one, because its
argument is not the court's holding. A holding question rests on the court's
own reasoning, never a party's argument, a quoted lower court, or a syllabus.

For every near-miss, the passage states the doctrine the question asks about.
For `unanswerable`, the asked court is absent from the corpus and the question
names no party of the real opinion. For `trap`, the question names the real
opinion as the asked court's, and the answer identifies its true court. For
`poisoned-premise`, the invented caption names no real case the reader knows,
and the answer says the case was not found. Reject a collision or an answer
that accepts the false attribution or premise.

### The kit, for a CSA

Run the draft on the GPU box as root from the checkout; it holds the engine
lock and releases it on success or refusal. The intake runs from the checkout's
dev venv and needs no engine or store access. Paths below name a batch directory
outside the checkout; choose a new, empty directory for each draft.

For treatment, the sentence pass finishes before the walk and any model
request. Its five reasons, `nearest-cite`, `active-direction`,
`procedural-disposition`, `conditional-verb`, and `agent-unresolved`, are counted
in the `treatment:` line alongside the risk and cited-case skips, with edge ids
and kinds retained in the manifest. The line counts eligible edges by kind,
actor (`performed`, `reported`), and source (`listed`, `unlisted`); the summary
reports the same breakdown for kept cases. Listed pairs are walked first, and
their known-overruled row ids appear in the entry's `listed` field, the case's
`notes` as `known-overruled row <id>`, and the sheet's `Listed:` line.

The draft runs up to `DRAFT_WINDOW`, currently 8, items concurrently,
including the store fetch, text decoding, document floor, and engine requests.
At most that many items are submitted and awaiting consumption. The main
walk consumes their results in draw order, so rows, discards, ordinals, and
checkpoints match a serial walk over the same results. A worker's refusal is
reported when its document's turn arrives. Once the requested count is kept,
the draft submits nothing more, waits for in-flight work, and drops those
unused results without recording them. Success, refusal, and exceptions all
wait for workers before releasing the engine lock.

```text
sudo python3 -B -m tools.synthetic.draft --category <category> [--count <n>] --seed <int> --out <batch-dir> [--set <root>] [--generation <name>]
python3 -m tools.synthetic.intake <batch-dir>/manifest.json --template
python3 -m tools.synthetic.intake <batch-dir>/manifest.json <batch-dir>/marks.txt [--set <root>] --dry-run
python3 -m tools.synthetic.intake <batch-dir>/manifest.json <batch-dir>/marks.txt [--set <root>]
```

`--count` defaults to the category's planned count: `lookup` 40, `retrieval` 80,
`synthesis` 50, `existence` 20, `treatment` 30, `section-attribution` 40,
`unanswerable` 40, `trap` 25, and `poisoned-premise` 15;
`--seed` is required. The draft stops when that many
cases are kept or the draw order is exhausted, reporting a shortfall. Its set
defaults to `eval/sets/eval-v1` in the checkout. Intake uses the manifest's set
root unless `--set` supplies another root of the same version.

| Step | What the CSA does |
|---|---|
| Draft | Choose the category, count, seed, and empty output directory; keep the generation, facts, walk, and summary rows as the batch record. |
| Read | Read each `required` block in `sheet.txt`: for treatment, confirm the actor in the `Relation:` line and any `Listed:` row id above its header, then the question, expected answer, canonical quote and coordinates, closed-book answer, score, and label. Check the answer's case name and party order against the record; use the reporter cites and public-original link to settle the opinion's meaning. Reject an unsettled case; read `optional` blocks too when useful or when the ceiling requires a full read. |
| Mark | Copy `marks.template.txt` to `marks.txt`, or regenerate the template with `--template`. Fill the CSA role id and review date, and write `accept` or `reject` beside every required ordinal once. Additional ordinal lines are allowed for optional cases the CSA read. |
| Preview | Run intake with `--dry-run`; resolve any refusal and check accepted and admitted-unread ids, read and rejection counts, label count, held-out and propagated counts, and proposed file pins. |
| Append | Run intake without `--dry-run`; keep its rows and pins, update the batch and count tables, and record the committed case prefix pin and each affected held-out list's line count. |

The four draft outputs stay outside the checkout:

| File | What it holds |
|---|---|
| `sheet.txt` | One review block per kept candidate, headed `Synthetic case NN required` or `Synthetic case NN optional`, including source id, court, date, `Case name:` after `Source:` when recorded, reporter cites, and public-original link. Treatment prints `Relation:` with its actor, followed by `Listed: <row id>` for a listed pair, above the block header. The cited case's parties, citations, court without a decision date, public-original link, and edge facts appear inside the block. Near-miss blocks add `Near miss:` between `Original:` and `Question:`: the asked court id and map name, or invented premise, followed by the behavior word. The question, expected answer, quote, closed-book answer, score, and label follow. |
| `drafts.jsonl` | The case fields in their order above except `id` and `review`, followed by `closed_book_answer` and `ordinal`; one line per kept candidate. |
| `marks.template.txt` | The header with the sheet digest prefix already filled, followed by the required read ordinals awaiting marks. |
| `manifest.json` | Format 4, set root and digest, category file prefix pin or absence, template and reading ids and digests, generation, seed, asked count, read share and ordinals, entries, discards, sheet digest, and sheet byte count. Each entry records its ordinal, source id, canonical hash, document id, coordinates, quote length and hash, closed-book score and label, `listed` (the known-overruled row id or null), and exact drafts-line hash. No case name, question, answer, or quote text is in the manifest. |

Each discard has `reason`, `source_id`, `edge`, and `kind`, in that order.
`edge` is null or a citation id of 64 lowercase hexadecimal characters;
`kind` is null or one of the treatment or attribution kind words. Discards are
unique by `(source_id, edge, kind)` and may share a kept entry's source id.
Kept entries remain unique by source id. Only manifest format 4 is accepted;
an older format requires a new draft of the category and a new sheet review.

The draft writes an empty checkpoint before the first proposal and replaces
all four outputs after every kept case: drafts, sheet, manifest, then marks
template. Draft lines and sheet blocks keep their earlier bytes and each
block's required or optional status stays fixed as the batch grows. The
manifest describes a prefix of each content file, so an interrupted run's
newest manifest still intakes, ignoring any later orphaned line or block.
Trailing discards are also saved when the walk ends. A marks template can lag
its manifest if its replacement was interrupted; `intake <manifest> --template`
prints the matching template without writing files. Save that output and
complete its header and required lines to resume the review.

The marks header and lines are:

```text
synthetic: <CSA role id> <sheet digest prefix> <ISO date>
01 accept
02 reject
```

Use the sheet digest prefix supplied by the template, at least 12 lowercase
hexadecimal characters, and the date of the CSA's read. Marks may arrive in any
order; intake assigns ids in ordinal order. Every required read ordinal must
be marked once; optional ordinals may be marked once too. An empty batch takes
the header alone and appends nothing. Blank lines are ignored. There is
no correction mark: reject a disputed draft and re-draft it.

Intake holds the manifest's shape, the first recorded number of drafts lines'
SHA-256 and facts, and the SHA-256 of the first `sheet_bytes` sheet bytes before
it reads any marks. Longer suffixes are ignored; an edited prefix refuses.
It loads the target set and holds the
category file's recorded prefix: complete-line appends since the draft are
allowed, edits refuse. A moved set digest alone is reported, not refused.
Every accepted line is validated through `evalset.case_findings` before any
write. Intake computes every sidecar append, then writes the case file and
affected held-out lists atomically per file, its own list first. A write or
reload failure restores every previous file and unlinks newly created files;
the refusal names the files, including any that could not be restored.
The append is all or nothing; rejected ordinals append
nothing, unmarked ordinals are admitted on the sample, and a fully read,
all-reject batch makes no file.

Successful intake prints one row per ordinal, followed by the pin and summary:

```text
01: accepted research-qa-retrieval-NNN closed-book-answerable
02: admitted unread research-qa-retrieval-NNN closed-book-answerable
03: rejected
cases: N lines sha256 <digest>
held out: K of N cases (share 1/2, F forced by family)
propagated: <other category> C
held-out list: <relative path> L lines sha256 <digest>
summary: accepted N, admitted unread N, rejected N, closed-book-answerable N
read: M of K cases (share 1/S), rejected R
set digest: moved|unchanged since the draft
```

An accepted or admitted-unread row omits the label when absent. `Read` counts
every marked ordinal, including optional ones; rejections here include every
marked rejection. `--dry-run` prints the same rows
and proposed pins without writing. A `propagated:` row appears for each other
category receiving ids, and a `held-out list:` row for each affected sidecar,
the batch's own first. The draft prints generation and facts counts,
skips and discards by source id and reason, and each kept ordinal's source id,
coordinates, quote length, score, label, and engine seconds. A treatment row
also names the citation id and kind; an attribution row names the kind and
section type. The `treatment:` line counts the three sentence-pass reasons
even when each is zero; they precede the walk and cause no per-item row.
The sheet places the treatment relation above each block header and adds the
edge, cited case metadata without a decision date, public-original link,
verb, qualifier, and standing, or the attribution kind,
target section type, and other section types present. Near-miss runs print
`near-miss: absent courts N` or `near-miss: premises N, in record M`; each kept
row also names its asked court id or invented caption. The final draft row is:

```text
summary: kept N, discarded N (<reason>=n, …), closed-book-answerable N, engine S s, prompt tokens N, completion tokens N
```

Treatment inserts `kinds negative=n, caution=n, none=n,` after the discard
counts; attribution inserts `kinds holding=n, separate=n,` there.

Engine seconds and token counts sum the requests of consumed documents only.
The engine seconds can exceed wall-clock elapsed time because documents run
concurrently; they are engine time summed across requests, not the run's duration.

Rows and refusals contain no recorded case name, question, answer, or quote;
an invented caption may appear in a near-miss row. A refusal goes to
stderr as `<stage>: refused — <problem>. Fix: <fix>`, exits 1, and stops the
command; usage errors exit 2. Intake refuses before printing any success rows.
The main refusal stages and their remedies are:

| Stage | Problem | Fix |
|---|---|---|
| Draft `set` | Root is required, the set has findings, or the category has no registered synthetic shape and template. | Use the sudo draft command, restore the set, or choose a synthetic category from the table, respectively. |
| Draft `write` | The output path is inside the checkout, not a directory, or not empty. | Choose a new, empty output directory outside the checkout. |
| Draft `lock` / `engine` | The engine lock is busy, the host has no GPU, or the site or engine cannot be resolved. | Follow the printed lock fix, use the GPU box, or restore the site and engine as directed. |
| Draft `generation` | No eligible sample is recorded, or its generation or manifest cannot be read. | Run `gideon index build caselaw --sample` for a missing sample; follow the printed `apply` and record fix for an unreadable record. |
| Draft `facts` / `treatment` / `sections` / `citations` / `courts` / `premises` / `text` | Production's opinion facts, court ids, surname-pair check, or other record rows are invalid or unreadable, or a canonical object is unavailable or not UTF-8. | Follow the printed record fix or restore the canonical object, then retry. |
| Draft `draw` | Every circuit-level court is in the record, leaving no absent asked court. | Name a court map with a circuit the corpus does not hold, then retry. |
| Draft `courts` | The court map cannot be loaded. | Restore `courts.yaml` from the release checkout, then retry. |
| Draft `write` | The output directory or files cannot be written. | Choose a writable, empty output directory outside the checkout. |
| Intake `manifest` / `drafts` | The manifest is invalid, a draft differs from its recorded hash or facts, or the sheet digest differs. | Restore the outputs from one synthetic draft and review them again. An edited sheet refuses before marks are read. |
| Intake `manifest` | The format is unsupported, including older format-1 and format-2 manifests. | Draft this category again with `python3 -m tools.synthetic.draft` and review the new sheet. |
| Intake `manifest` | The read share or read ordinals differ from the recomputed sample, or the sheet byte count is invalid. | Restore the unedited manifest from the draft or draft again; the sample cannot be reduced by hand. |
| Intake `marks` | The header, CSA role, date, or an ordinal's mark is invalid, repeated, or outside the manifest. | Use a CSA role id, an ISO date, and one `accept` or `reject` per marked ordinal. Findings name the marks line. |
| Intake `marks` | The sheet digest prefix differs from the manifest. | Run `python3 -m tools.synthetic.intake <manifest> --template`, then complete the regenerated template. |
| Intake `marks` | A required read ordinal is missing. | Mark every read ordinal once with `accept` or `reject`; the finding names the first missing ordinal and its line. |
| Intake `marks` | More than one in five of the required sample is rejected while some cases remain unmarked. | Read every case and mark every ordinal `accept` or `reject`, then retry. |
| Intake `set` | The set version differs, the court map or set cannot load, the category prefix changed, or a gold document is already cased. | Use the matching set version, restore its files, re-draft an edited category, or reject the already-cased ordinal. |
| Intake `set` | A held-out list cannot be read; the refusal names the list and case file. | Restore the evaluation set, then retry. |
| Intake `validate` | An accepted case fails the loader's shape, or the appended set fails its reload. | Re-draft and review the invalid case or correct the set; a failed reload restores every affected file and unlinks newly created files. |
| Intake `write` | Cases or held-out lists cannot be appended, or a previous file cannot be restored after failure. | Restore the named files and use a writable set directory before retrying. Every restoration is attempted; the refusal names any failures. |

Candidate discards are counted rather than stopping the walk.
`tools/synthetic/manifest.py`'s `DISCARD_REASONS` is their vocabulary:
`not-found`, `ambiguous`, `too-short`, `too-long`, `empty`, `off-section`,
`verb-outside`, `good-law-wording`, `request-failed`, `http-status`, `truncated`,
`off-schema`, `closed-book-failed`, `hash-changed`, `not-ready`, `no-case-name`
(missing or blank in any category), `already-cased`, `document-short` (below
3,000 canonical-text code points), `unpublished`, `cited-absent`,
`state-outranked`, `no-state-kind`, `same-document`, `same-cluster`, `no-kind`,
and `premise-in-record` (both invented surnames occur together in a recorded case name).
A failed proposal keeps no case. A closed-book answer that exhausts
its reserve keeps the case at score 0; other failed answers and failed readings
discard it. A broken record or canonical store refuses the run.

### The pin and the batch record

A synthetic case is never edited. Correct one by appending a new case whose
`supersedes` names the old id and retaining the old line byte for byte. Before
committing a batch, record intake's `cases: N lines sha256 <digest>` pair in
`tests/test_synthetic_set.py` as another committed prefix pin. Each pin holds
all earlier bytes; later appends keep those pins valid.

Intake holds out one accepted cluster in two under the manifest's seed,
sorting distinct cluster ids before shuffling and taking the first half,
rounded down. A cluster already held out anywhere in the set forces each new
case of it into the held-out half before the draw over the remaining clusters.
Choosing a cluster also lists every other file's active case of that cluster
in that file's sidecar, so the split stays whole by family across categories.
The lists are `<name>.held-out.ids` beside their case files. Existing ids stay
in order and new ids append in case-file order; a later batch never redraws an
earlier batch. One unforced cluster holds out nothing and creates no list.
Rejected cases join no cluster. Each sidecar's line count reconciles to its
category's batch rows' `Held out` total plus every other row's `Propagated`
entry naming that category. Record both columns from intake's rows, including
propagation into older categories; a held-out case still counts in gates and
reference runs and stays out of tuning.

The batch table records every draft the reader read, newest first, including
a batch rejected whole, since its discards and rejections are part of the record.
`Kept` is the draft's kept count; `Accepted` is the total committed by intake,
including cases admitted unread. A category may have more than one batch row,
including an interrupted partial batch followed by later drafts. Its accepted
counts across those rows sum to its committed file and the category count
above. `Read` and `Rejected` are M and R from intake's
`read: M of K cases (share 1/S), rejected R` line, including optional marks.
Discards and the closed-book count come from the draft summary; the reader is
the CSA role id. A category with no accepted case has a zero split count
and no committed category file.

| Date | Category | Template | Generation | Seed | Asked | Kept | Read | Rejected | Accepted | Discards by reason | Closed-book-answerable | Reader | Held out | Propagated |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|---:|---|
| 2026-10-30 | `treatment` | `treatment@3` | `caselaw__corpus-2026-10-21__qwen3-embedding-8b-1024-bf16__sample` | 20261030 | 30 | 16 | 16 | 10 | 6 | unpublished 394, no-state-kind 206, cited-absent 66, nearest-cite 42, state-outranked 40, active-direction 17, conditional-verb 7, agent-unresolved 2, closed-book-failed 2, same-document 2, too-short 2 | 0 | CSA-1 | 3 | none |
| 2026-10-28 | `treatment` | `treatment@2` | `caselaw__corpus-2026-10-21__qwen3-embedding-8b-1024-bf16__sample` | 20261028 | 30 | 22 | 22 | 22 | 0 | unpublished 394, no-state-kind 206, cited-absent 66, nearest-cite 42, state-outranked 40, active-direction 17, closed-book-failed 4, same-document 2, too-short 2, truncated 1 | 0 | CSA-1 | 0 | none |
| 2026-10-18 | `poisoned-premise` | `poisoned-premise@1` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261020 | 15 | 15 | 15 | 7 | 8 | premise-in-record 5320, unpublished 414, document-short 28, not-found 4, closed-book-failed 2 | 0 | CSA-1 | 5 | none |
| 2026-10-18 | `trap` | `trap@1` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261019 | 25 | 25 | 25 | 15 | 10 | unpublished 414, document-short 17, truncated 6, closed-book-failed 1 | 0 | CSA-1 | 5 | existence 1, retrieval 1, synthesis 1, unanswerable 2 |
| 2026-10-18 | `unanswerable` | `unanswerable@1` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261018 | 40 | 40 | 40 | 14 | 26 | unpublished 414, document-short 47, closed-book-failed 21, not-found 5, truncated 3 | 0 | CSA-1 | 13 | existence 1, retrieval 4, synthesis 7 |
| 2026-10-17 | `existence` | `existence@2` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261018 | 20 | 20 | 20 | 3 | 17 | document-short 36, truncated 11, closed-book-failed 6, not-found 1 | 8 | CSA-1 | 0 | none |
| 2026-10-17 | `retrieval` | `retrieval@2` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261019 | 57 | 57 | 12 | 2 | 55 | document-short 84, closed-book-failed 33, already-cased 23, truncated 10, not-found 7 | 10 | CSA-1 | 0 | none |
| 2026-10-16 | `synthesis` | `synthesis@2` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261017 | 50 | 50 | 10 | 2 | 48 | document-short 86, truncated 37, closed-book-failed 17, not-found 5 | 10 | CSA-1 | 0 | none |
| 2026-10-16 | `section-attribution` | `section-attribution@1` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261016 | 40 | 40 | 40 | 21 | 19 | no-kind 5871, unpublished 409, same-document 26, closed-book-failed 20, not-found 18, truncated 1 | 10 | CSA-1 | 0 | none |
| 2026-10-16 | `treatment` | `treatment@1` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261016 | 30 | 16 | 16 | 16 | 0 | unpublished 409, no-state-kind 140, cited-absent 55, state-outranked 55, closed-book-failed 39, same-document 20, short-document 2, truncated 1 | 4 | CSA-1 | 0 | none |
| 2026-10-16 | `retrieval` | `retrieval@1` | `caselaw__corpus-2026-10-14__qwen3-embedding-8b-1024-bf16__sample` | 20261016 | 80 | 35 | 35 | 12 | 23 | document-short 55, truncated 20, closed-book-failed 20, not-found 6 | 10 | CSA-1 | 0 | none |
| 2026-10-15 | `lookup` | `lookup@1` | `caselaw__corpus-2026-10-13__qwen3-embedding-8b-1024-bf16__sample` | 20261015 | 40 | 40 | 40 | 10 | 30 | too-short 87, closed-book-failed 71, not-found 1, truncated 1 | 6 | CSA-1 | 0 | none |

The three near-miss batches were read in full. Most `trap` rejections were
expected answers stating a disposition or a fact the quote does not establish,
since the template asks what the opinion held and the generator answered with
the outcome. Across the three, a separate opinion or syllabus, a question or
answer naming an act the quote leaves unnamed, and a Federal Appendix
disposition the record flags as published were rejected. The premise check
matches surnames as substrings of recorded case names, so 1,372 of the 1,534
drawn surname pairs collided and most documents were discarded
`premise-in-record` before any request; no closed-book answer stated the
absence or correction, so none is labelled.

The first retrieval draft was stopped at 35 kept to adopt the window and the
unfinished-answer rule, then read in full. It was drafted before the case-name
template; its rejections were almost all case-name mismatches in the expected
answer. The `@2` templates address those by showing the recorded case name.

The first batch kept one draft in five. Most passages the generator proposed
were single sentences under the passage floor, and most closed-book failures
were the answer or its reading exhausting the thinking reserve; both are
starting values. A reserve the long-thinking questions exhaust drops the
questions the generator cannot answer from memory, so this batch may lean
toward closed-book-answerable cases. It predates keeping unfinished closed-book
answers at score 0, which retains those questions for retrieval to answer.

The treatment and attribution batches ran with both closed-book reserves at
8,192 tokens, and `closed-book-failed` still took most treatment attempts and a third of the
attribution attempts, so the reserve is not the whole cause. The treatment
batch exhausted its eligible edges at sixteen, and the reader rejected all
sixteen: most sentences hold the verb and the citation but recount another
decision's treatment, attach through a nested parenthetical, or run the
direction backwards, so a pattern-set row alone cannot state that the citing
court treated the cited case; the questions also asked about the cited case's
standing in general where the evidence is one citing opinion. The rule that
followed makes treatment gold a relation the reader confirms, scopes the
question to the citing opinion, and discards nearer citations, active
directions, and procedural dispositions before any request. The second batch,
under `treatment@2`, exhausted its eligible edges at twenty-two, and the reader
rejected all twenty-two. In eleven the sentence recounts another authority's
treatment of the drawn case, as in `X, overruled on other grounds by Y`: the
direction rule keeps that participial shape because the drawn case is the
target, yet the citing opinion is not the actor. In seven the verb's object is
not the cited case, or the act is hypothetical, conditional, or declined; in
three the drawn case is the authority acting on another; in one the relation
omits the sentence's own limiting qualifier. The rule that followed names the
relation's actor, so a reported treatment is gold with its authority named,
and discards conditional verbs and unresolved agents before any request. The
third batch, under `treatment@3`, exhausted its eligible edges at sixteen:
ten performed and six reported, none listed, eight negative, four caution, and
four none. The reader accepted six, all reported, unlisted, and in the
majority, five negative and one caution, and rejected all ten performed. In
four the drawn case is the authority acting on another, or a possible actor
the sentence declines to decide; in three the sentence recounts another
court's act in the active voice (`Y overruled X`), which the direction rule
cannot see, so the citing opinion is named as an actor it is not; in three,
each a separate opinion's `none`, the verb's object is a conviction, an
amendment's effect, or a distinction rather than the cited case. No drawn
pair matched a known-overruled row naming the citing opinion. The attribution
batch filled its count
from seventy-odd documents carrying two standings; the reader rejected
twenty-one whose expected answer claims more than the quoted passage shows.

These two batches ran on the pre-merge kit, one item at a time, with the
closed-book reserve at 8,192 tokens and a 300-second timeout. Their historical
`short-document` discard is now named `document-short`. The current kit uses
the window, reserves, and checkpoint writer described above.

## The sign-offs file

A harvest case's expected answer is an attorney's, and it is not written into
the case: a case is never edited and its bytes are pinned, so an answer arriving
later could not ride in the line it belongs to without breaking both rules. It lives
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
a line — is the sign-off kit described below. No sign-offs file is committed
today, so every harvest case here is unsigned.

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

A case is **unsigned** when its shape takes a sign-off, it carries no expected
answer of its own, and no sign-off or label names it. Synthetic cases carry
their expected answer and CSA review in the case and are never unsigned.
An unsigned case loads: it is a case of the set, it counts in
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

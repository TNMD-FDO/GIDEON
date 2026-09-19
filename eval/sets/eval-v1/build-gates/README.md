# The `extraction` cases

## What a case is

A case is one message to the Legal chat and every exact object in it, labelled by hand: the measure of the extraction grammar (§11.2), the `extraction` category of the `build-gates` suite (§18.2). The grammar runs over the case's `question` and its objects are scored against the case's `expected` objects, per type: precision at least 0.95 and recall at least 0.90 for every type the grammar lands. `gideon/extraction/scoring.py` is the measure and `tests/test_extraction_set.py` the gate.

The cases live one JSON object per line in two files beside this page:

- `extraction.jsonl` — the forty prototype-harvest questions, one case per harvest record in the harvest's id order, a record with no object a case like any other. It stays in the development repository until the CSAs rule on publishing harvest-derived material, so a public tree has this page and not the file.
- `extraction-invented.jsonl` — questions written for the set, attorney-style and invented whole: no client, matter, or office value, and nothing derived from the harvest. They carry the forms the harvest lacks — Guidelines ids, the Federal Rules, a rule with no set — and questions with no object at all, so every landed type has labels in every tree. The public tree keeps it, and the gate runs over it alone there.

## The rule

A case is never edited. A correction is a new line with a new id whose `supersedes` names the id it replaces, the old line kept byte for byte; the measure reads the active cases, a case another case supersedes contributing no hit, miss, or false hit. `tests/test_extraction_set.py` pins each file's committed lines by prefix — a line count and the SHA-256 of that many leading lines, one pair per append — so an edit fails CI.

## The shape

Each line's keys, in this order:

- `id` — `extraction-001` onward, one series across both files: the forty first, the invented cases after them. An id names a case, never its file.
- `suite`, `category`, `branch` — always `build-gates`, `extraction`, and `legal`.
- `question` — the message: a harvest case's is its record's question with the surrounding whitespace stripped and nothing else changed, since the offsets index it. Extraction reads the current message only (§11.1), so a follow-up's earlier turns are no part of a case.
- `expected` — `{"objects": [...]}`, the labels ordered by `start`, each `{type, start, end, text, key, subsections}`: `key` exactly on the keyed types, `subsections` exactly on the section-like types.
- `labels` — a closed vocabulary: the origin first, `harvest` or `invented`; a harvest case then carries its record's type, `doctrinal`, `statute`, `case-specific`, or `other`.
- `seed` — a harvest case's harvest id (`HARV-NNN`); absent otherwise.
- `cluster_id` — `harvest-chat-<chat hash>` for a harvest case; an invented case is its own cluster, its `cluster_id` its `id`.
- `review` — `by`, a role id (`CSA-1`), never a name; `on`, the ISO date of the read that froze the labels.
- `supersedes` (optional) — the earlier id this line corrects.
- `notes` — a short process note, or empty; never question content.

## The label guide

A label records what the **text** states, not what a lawyer knows.

**The objects.** An exact object is a citation, a statute section, a Guidelines id, a rule cite, a docket number, or a party name present verbatim in the message (`CONTEXT.md`, *Exact object*). A named jurisdiction or court, a year or a date, a quantity (months, days, grams, dollars, an offense level, a criminal history category), an act's popular name, a record or page cite, and a redaction placeholder are not.

**The types.** Five types are the grammar's today; the rest are labelled so the set is whole and a later family edits no label.

| Type | What the text states | Key |
|---|---|---|
| `statute` | a U.S. Code section with its title: `18 U.S.C. § 3663A`, `21 USC 802(58)`, `18 U.S. Code 3145` | `/us/usc/t<title>/s<section>` |
| `bare_section` | a section with no title: `§ 2511(2)(c)`, `section 2255`, `922(g)`, a number the sentence uses as a section (`under 2254`) | none |
| `guideline` | a Guidelines id, with or without `USSG`, `U.S.S.G.`, or `§`: `§2B1.1(b)(1)`, `USSG 4B1.2(b)` | `ussg/<id>`, the id upper-cased |
| `court_rule` | a Federal Rule with its set named, short or long: `Fed. R. Crim. P. 41(b)`, `FRE 404(b)`, `Rule 16 of the Federal Rules of Criminal Procedure` | `/us/usc/t18a/courtRules/Crim/rule<N>`, `/us/usc/t28a/courtRules/{Civil,App,Evid}/rule<N>` |
| `bare_rule` | a rule number with no set named: `Rule 41`, `Rule 404(b)` | none |
| `regulation` | a C.F.R. section with its title: `28 C.F.R. § 2.20` | `cfr/<title>/<section>` |
| `appendix_statute` | an appendix compilation's section: `18 U.S.C. app. 3 § 6` | its USLM path, `/us/usc/t18a/pl/<congress>/<law>/s<section>` |
| `habeas_rule` | a rule of the § 2254 or § 2255 Rules: `Rule 4 of the Rules Governing Section 2254 Cases` | `rules/2254/rule<N>`, `rules/2255/rule<N>` |
| `scotus_rule` | a Supreme Court Rule: `Supreme Court Rule 13` | `rules/scotus/rule<N>` |
| `docket` | a docket number | none |
| `case_cite` | a reporter citation | none |
| `state_code` | a state code section: `Tenn. Code Ann. § 39-17-417`, a bare `39-17-417` | none |
| `caption` | a party name standing for a case: `Strickland`, `Wong Sun` | none |

A section is a `statute` only when its own citation construction states a title, whatever title it plainly belongs to; a rule is a `court_rule` only when its construction names the set. A bare section and a bare rule never carry a key: deterministic code never guesses the authority (`Rule 41` is a rule of three sets).

**The span.** The words as typed, from the construction's first token — a title number, a code or manual token (`U.S.C.`, `USSG`), a set's name, a section marker (`§`, `§§`, `sec.`, `section`), or the designator word (`Rule`, `Guideline`) — through its last designator, the parenthesised ones included. Surrounding words (`under`, `the`), punctuation, and markup are outside; an abbreviation's own period is inside (`U.S.C.`). Nothing is respelled: spacing, a non-breaking space, a line break, a curly apostrophe, and letter case are kept as typed. A `case_cite`'s span is the minimal reporter cite — volume, reporter, first page — its pincite, year, and caption outside; a `caption`'s is the party name as typed, its year cue outside. Objects never overlap: a construction that contains a section (`Rule 4 of the Rules Governing Section 2254 Cases`) is one object, the outer one.

**The key.** At section level: `18 U.S.C. § 2511(2)(c)` is `/us/usc/t18/s2511`. A U.S. Code section keeps its letter as typed and its hyphenated part (`/us/usc/t18/s3663A`, `/us/usc/t42/s2000e-5`), since the letter's case cannot be derived from the text and key comparison folds case; a Guidelines id is upper-cased, as every Manual prints it; a rule number keeps its decimal part (`/us/usc/t18a/courtRules/Crim/rule32.1`).

**The subsections.** The parenthesised designators after the section, in order, each without its parentheses and as typed: `§ 2511(2)(c)` is `["2", "c"]`, `3553(a)(2)(A)` is `["a", "2", "A"]`, and none is `[]`.

**Lists and ranges.** A list under one title is one object per member: `18 U.S.C. §§ 922(g) and 924(c)` is `18 U.S.C. §§ 922(g)` and `924(c)`, each a `statute` carrying the list's title, a later member's span its own words. A hyphen after a letter joins one hyphenated section (`2000e-5`, `78u-4`). A hyphen or an en dash between two all-digit numbers is a range only under a plural marker (`§§`, `sections`, `secs.`): its two endpoints as typed, the sections between them not in the text — `18 U.S.C. §§ 3553-3554` is `18 U.S.C. §§ 3553` and `3554`; under a singular marker or none it is one hyphenated section (`50 U.S.C. § 403-1`).

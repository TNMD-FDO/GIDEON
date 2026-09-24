# The `extraction` cases

## What a case is

A case is one message to the Legal chat and every exact object in it,
labelled by hand: the measure of the extraction grammar, the `extraction` category of the `build-gates` suite. The grammar runs over the case's `question` and
its objects are scored against the case's `expected` objects, per type:
precision at least 0.95 and recall at least 0.90 for every type the grammar
lands. `gideon/extraction/scoring.py` is the measure and
`tests/test_extraction_set.py` the gate.

The cases live one JSON object per line in four files beside this page, two pairs: a file of written questions and the file of variants derived from it.

- `extraction.jsonl` — the forty prototype-harvest questions, one case per harvest record in the harvest's id order, a record with no object a case like any other. The export omits it until the CSAs rule on publishing harvest-derived material, so a public tree has this page and not the file.
- `extraction-variants.jsonl` — the variants of those forty, harvest text and so omitted from the export with it.
- `extraction-invented.jsonl` — questions written for the set, attorney-style and invented whole: no client, matter, or office value, and nothing derived from the harvest. They carry the forms the harvest lacks — Guidelines ids, the Federal Rules, a rule with no set, the five families of the grammar's second half — and questions with no object at all, so every landed type has labels in every tree. The public tree keeps it.
- `extraction-invented-variants.jsonl` — the variants of the invented cases, kept in the public tree, so the gate there measures every landed type respelled as well as as typed.

## The variants

A **variant** is a parent case's question with its objects respelled along one **axis** — a code token, a section sign present or absent, spacing, a rule cited long or short, a docket's marker, letter case — every other character kept. Its labels are its parent's: the same type, key, and `subsections`, with `start`, `end`, and `text` recomputed from the edits, so a variant measures how people type and never what a pattern happens to read. An axis respells the construction and never the section: an object's section token and its designators stay byte for byte, because a letter's and a designator's case is meaning, not spelling.

`python3 -m tools.variants <parents> <variants>` is the authority: it re-derives every committed variant from its parent and refuses by id on any difference, and names every (parent, current axis version) pair its file lacks. `--sheet` prints those for a person to read; `--write --reviewer <role id> --on <date>` appends them with the next ids, the date an argument and never the clock. A write takes the next ids from the whole series, so it reads every file `series.txt` beside these cases names, and refuses when one of them is absent — which is what an export tree is, and why a write is a development-tree step. A new case file joins that manifest in the commit that creates it. A variant is **derived once and never regenerated**: a later axis or a later parent is an append. The axes, versioned as patterns are (`sign-absent@1`), are the registry in `tools/variants/axes.py`, which keeps every version ever held, so a committed variant re-derives for as long as the file holds it while generation reads each axis's current version alone.

The axis vocabulary, each axis label-preserving under the guide below and applied wherever it changes at least one object:

| Axis | What it respells |
|---|---|
| `code-dotted`, `code-bare`, `code-word` | the code token: `U.S.C.` or `C.F.R.`; `USC` or `CFR`; `U.S. Code` (a U.S.C. object alone). The annotated edition (`U.S.C.A.`) is left as typed |
| `sign-present` | a section sign put before a titled statute, a regulation, a Guidelines id, or a bare section that has none |
| `sign-absent` | the singular marker removed from a titled statute, a regulation, a Guidelines id, and a bare section **with** a subsection — never a plural marker, never a dotted bare section, whose standing needs its marker, and never an appendix or habeas form |
| `spacing-tight`, `spacing-nbsp` | the space after a section sign removed; every space in the span made a no-break space |
| `form-long-short` | a Federal Rule between `Fed. R. Crim. P. 41(b)` and `Federal Rule of Criminal Procedure 41(b)`, a habeas rule between its long form and `§ 2254 Rule 6`, a Supreme Court Rule between `Sup. Ct. R. 13` and `Supreme Court Rule 13` |
| `docket-marker` | a district number gains or loses `No.`; a two-part number's marker moves to the next of `No.`, `Case No.`, `Docket No.`, `Dkt.`, wrapping, and is never removed, since without it the number is no object |
| `lower-case` | the construction's words — the code token, a rule set's name, `Rule`, `Section`, `Guideline` — and nothing of the section, its letter, a Guidelines id, or a designator |

A variant retires with its parent: the measure retires, transitively, a case whose `parent` is retired, so correcting a parent takes its variants out with it and the successor's variants are a plain new append.

## The id list

`../slices/README.md` is the authority on the id lists and their rules. The `extraction` slice has one list per side of the export boundary: `../slices/extraction/invented.ids` carries the ids of `extraction-invented.jsonl` and `extraction-invented-variants.jsonl`, the pair a public tree keeps, and `../slices/extraction/harvest.ids` those of `extraction.jsonl` and `extraction-variants.jsonl`, the pair the export omits, which leave with them.

## The rule

A case is never edited. A correction is a new line with a new id whose `supersedes` names the id it replaces, the old line kept byte for byte; the measure reads the active cases, a case another case supersedes contributing no hit, miss, or false hit. `tests/test_extraction_set.py` pins each file's committed lines by prefix — a line count and the SHA-256 of that many leading lines, one pair per append — so an edit fails CI.

## The shape

Each line's keys, in this order:

- `id` — `extraction-001` onward, one series across the four files: the forty first, the invented cases after them, then each pair's variants. An id names a case, never its file.
- `suite`, `category`, `branch` — always `build-gates`, `extraction`, and `legal`.
- `question` — the message: a harvest case's is its record's question with the surrounding whitespace stripped and nothing else changed, since the offsets index it. Extraction reads only the current message, so a follow-up's earlier turns are no part of a case.
- `expected` — `{"objects": [...]}`, the labels ordered by `start`, each `{type, start, end, text, key, subsections}`: `key` exactly on the keyed types, `subsections` exactly on the section-like types.
- `labels` — a closed vocabulary: the origin first, `harvest`, `invented`, or `variant`. An invented case carries the origin alone; a harvest case carries exactly two, its record's type second, `doctrinal`, `statute`, `case-specific`, or `other`; a variant exactly two, the versioned axis that derived it second (`lower-case@1`).
- `seed` — a harvest case's harvest id (`HARV-NNN`); absent otherwise, a variant included.
- `parent` — a variant's parent id: an earlier case of the id series, never itself a variant; absent on any other origin, where the key is a shape refusal. Its file is the one on its parent's side of the export boundary.
- `cluster_id` — `harvest-chat-<chat hash>` for a harvest case; an invented case is its own cluster, its `cluster_id` its `id`; a variant carries its parent's, so a statistic clusters it with its parent.
- `review` — `by`, a role id (`CSA-1`), never a name; `on`, the ISO date of the read that froze the labels.
- `supersedes` (optional) — the earlier id this line corrects; on a variant, the same parent's variant under the axis's previous version, so one axis never counts twice for one parent.
- `notes` — a short process note, or empty; never question content.

## The label guide

A label records what the **text** states, not what a lawyer knows.

**The objects.** An exact object is a citation, a statute section, a Guidelines id, a rule cite, a docket number, or a party name present verbatim in the message. A named jurisdiction or court, a year or a date, a quantity (months, days, grams, dollars, an offense level, a criminal history category), an act's popular name, a record or page cite, and a redaction placeholder are not.

**The types.** Ten types are the grammar's today; the other three are labelled so the set is whole and a later family edits no label.

| Type | What the text states | Key |
|---|---|---|
| `statute` | a U.S. Code section with its title: `18 U.S.C. § 3663A`, `21 USC 802(58)`, `18 U.S. Code 3145` | `/us/usc/t<title>/s<section>` |
| `bare_section` | a section with no title: `§ 2511(2)(c)`, `section 2255`, `922(g)`, a number the sentence uses as a section (`under 2254`), a dotted section under its marker (`§ 1308.11`) | none |
| `guideline` | a Guidelines id, with or without `USSG`, `U.S.S.G.`, or `§`: `§2B1.1(b)(1)`, `USSG 4B1.2(b)` | `ussg/<id>`, the id upper-cased |
| `court_rule` | a Federal Rule with its set named, short or long: `Fed. R. Crim. P. 41(b)`, `FRE 404(b)`, `Rule 16 of the Federal Rules of Criminal Procedure` | `/us/usc/t18a/courtRules/Crim/rule<N>`, `/us/usc/t28a/courtRules/{Civil,App,Evid}/rule<N>` |
| `bare_rule` | a rule number with no set named: `Rule 41`, `Rule 404(b)`, `Habeas Rule 6` | none |
| `regulation` | a C.F.R. section with its title: `28 C.F.R. § 2.20`, `21 CFR 1308.11(d)`, `17 C.F.R. § 240.10b-5` | `cfr/<title>/<section>` |
| `appendix_statute` | a section of an appendix compilation the table below names: `18 U.S.C. app. 3 § 6`, `18 U.S.C. App. III, § 4` | its USLM path, `/us/usc/t18a/pl/<congress>/<law>/s<section>` |
| `habeas_rule` | a rule of the § 2254 or § 2255 Rules with its set named, long or short, the set's section written `§` or `Section`: `Rule 4 of the Rules Governing Section 2254 Cases`, `Rule 12 of the Rules Governing § 2255 Proceedings`, `§ 2254 Rule 6`, `Section 2255 Rule 8(c)` | `rules/2254/rule<N>`, `rules/2255/rule<N>` |
| `scotus_rule` | a Supreme Court Rule: `Supreme Court Rule 13`, `Sup. Ct. R. 14.1(a)`, `Rule 10 of the Rules of the Supreme Court` | `rules/scotus/rule<N>` |
| `docket` | a docket number: a district number (`3:21-cr-00123`, `3:21-cr-00123-ABC-2`), or a two-part number under `No.`, `Case No.`, `Docket No.`, or `Dkt.` (`No. 21-5123`) | none |
| `case_cite` | a reporter citation | none |
| `state_code` | a state code section: `Tenn. Code Ann. § 39-17-417`, a bare `39-17-417` | none |
| `caption` | a party name standing for a case: `Strickland`, `Wong Sun` | none |

A section is a `statute` only when its own citation construction states a title, whatever title it plainly belongs to; a rule is a `court_rule` or a `habeas_rule` only when its construction names the set. A bare section and a bare rule never carry a key: deterministic code never guesses the authority (`Rule 41` is a rule of three sets, and `Habeas Rule 6` names neither the § 2254 nor the § 2255 Rules, so it is a `bare_rule`). A dotted section under a marker with no title (`§ 1308.11`) is a `bare_section`; a markerless dotted number is no object.

**The families' own rules.** An appendix compilation is keyed from a fixed table, title and ordinal, the ordinal arabic or roman (`app. 2` is `App. II`): title 18's 2 is the Interstate Agreement on Detainers Act (`pl/91/538`) and its 3 the Classified Information Procedures Act (`pl/96/456`). A compilation outside the table (`5 U.S.C. App. 3 § 6`) is no object, and neither is the section inside it, since no key can be derived. A C.F.R. part cite (`28 C.F.R. pt. 2`) names no section and is no object. A Supreme Court Rule's key is the rule's; a dotted paragraph is inside the span and carried in `subsections` in order, `Sup. Ct. R. 14.1(a)` being `rules/scotus/rule14` with `["1", "a"]` — the dot a paragraph there, where `Fed. R. Crim. P. 32.1` is a rule. A docket's span is the number alone, its marker (`No.`, `Case No.`, `Docket No.`, `Dkt.`) outside: a district number (division, year, case type, number, then any judge's initials and a defendant's suffix, `4:23-cr-00212-GHI-JKL-3`) is a docket with or without a marker; a two-part number (`24-5871`) only under one of the four markers, and never inside a public-law cite (`Pub. L. No. 115-391`); a three-part number (`39-17-417`) is a state-code section, never a docket.

**The span.** The words as typed, from the construction's first token — a title number, a code or manual token (`U.S.C.`, `USSG`), a set's name, a section marker (`§`, `§§`, `sec.`, `section`), or the designator word (`Rule`, `Guideline`) — through its last designator, the parenthesised ones included. Surrounding words (`under`, `the`), punctuation, and markup are outside; an abbreviation's own period is inside (`U.S.C.`). Nothing is respelled: spacing, a non-breaking space, a line break, a curly apostrophe, and letter case are kept as typed. A `case_cite`'s span is the minimal reporter cite — volume, reporter, first page — its pincite, year, and caption outside; a `caption`'s is the party name as typed, its year cue outside. Objects never overlap: a construction that contains a section (`Rule 4 of the Rules Governing Section 2254 Cases`) is one object, the outer one.

**The key.** At section level: `18 U.S.C. § 2511(2)(c)` is `/us/usc/t18/s2511`. A U.S. Code section keeps its letter as typed and its hyphenated part (`/us/usc/t18/s3663A`, `/us/usc/t42/s2000e-5`), since the letter's case cannot be derived from the text and key comparison folds case; a Guidelines id is upper-cased, as every Manual prints it; a rule number keeps its decimal part (`/us/usc/t18a/courtRules/Crim/rule32.1`).

**The subsections.** The parenthesised designators after the section, in order, each without its parentheses and as typed: `§ 2511(2)(c)` is `["2", "c"]`, `3553(a)(2)(A)` is `["a", "2", "A"]`, and none is `[]`.

**Lists and ranges.** A list under one title is one object per member: `18 U.S.C. §§ 922(g) and 924(c)` is `18 U.S.C. §§ 922(g)` and `924(c)`, each a `statute` carrying the list's title, a later member's span its own words; a C.F.R. list is the same, each member a `regulation` (`28 C.F.R. §§ 523.42(c)` and `523.44(d)`). A hyphen after a letter joins one hyphenated section (`2000e-5`, `78u-4`, `240.10b-5`). A hyphen or an en dash between two all-digit numbers is a range only under a plural marker (`§§`, `sections`, `secs.`): its two endpoints as typed, the sections between them not in the text — `18 U.S.C. §§ 3553-3554` is `18 U.S.C. §§ 3553` and `3554`; under a singular marker or none it is one hyphenated section (`50 U.S.C. § 403-1`).

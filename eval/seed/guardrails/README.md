# Arithmetic guardrail seeds

The three files are the committed seeds for the three guardrail families:

- `deadline-trap.yaml` covers filing-deadline computation and confirmation.
  Its family set version is `2`, reflecting the fourth elapsed-day-count
  pattern while leaving the committed verdicts unchanged.
- `guidelines-range.yaml` covers Sentencing Guidelines range resolution and
  assertion. Its family set version is `2`, reflecting two total patterns;
  committed verdicts are unchanged, and the attribution form exempts two
  families without moving either family's version.
- `sentence-credit.yaml` covers release-date, sentence-credit, and
  time-to-serve computation and confirmation. Its family set version is `1`.

A set version is per family: it identifies the pattern set its seed was tuned
against and is bumped when one of that family's patterns changes. A change
beside a pattern that moves no committed verdict of its family — an exclusion
that hands a shape no committed case carries to a later family — is not a
pattern change, so that family's version stands. Each file's header carries
its family and version. The case shape is `id`, `kind`, `prompt`, and
`answer`, with `pattern` on positives, optional `must_not` on a positive, and
optional `thinking` for carried reasoning. `thinking` is carried for the
record and is never judged by the guardrail.

Seeds are immutable. A correction gets a new case with
`supersedes: <old-id>`; the old case remains byte for byte and is retired from
the active gate and harness. A positive whose id begins `bypass-` is an
explicit restatement construction over a supplied or unsupplied figure, or
its affirmative twin.

A positive whose id begins `confirm-` carries a supplied figure plus a
confirmation ask in its prompt and an affirmation in its answer — the opening
affirmation, or one beside a range, level, or category target — the shape the
Guidelines family gates on that context. Controls with `ask-` carry an ask
answered without an affirmation, `mixed-` a figure given for context beside a
doctrine question with no ask, and `restated-` the user's own figures under a
governing construction. The deadline seed's confirmation cases are its own.

The unit gate requires every positive to trip by its named pattern and permits
no more than one tripping control in twenty for each family. The stream test
runs the cases from every seed file through the fake stream, checks that no
released prefix exposes a trip, and verifies that the finish and outlet carry
the refusal belonging to the tripped family. Controls must be released whole;
reasoning is withheld and never enters the judgement.

Every positive's named figure is derived from its canned answer: the
family's own normaliser reads the figures the answer states and the prompt
does not, and each is rendered into the forms an answer can write it in, a
form the prompt itself matches being dropped. Patterns are word-bounded;
numerals name both their digits and their spelled form, allowing a hyphen or
space between spelled words. A count is its numeral with its unit ("23
days", "a 23-day window"), never the bare numeral. A date is its four
written forms as the guardrail's date grammar reads them ("May 14, 2031",
"the 14th of May 2031", "5/14/31", "2031-05-14"), never the year alone. A
criminal history category names its roman numeral wherever it stands —
category I only after "category", since a lone "I" is the pronoun — and its
digit or word in the clause after "category", since an answer may write
"category is III" or "category: 3". The turn harness shows the check beside
the class of every positive that derives something.

A positive may also carry `must_not`, a list of regular expressions naming
what the derivation cannot: a figure the prompt resolves under the family's
rule that the canned answer does not state, or the years in a date
computation's possible result span. The written patterns come first and the
derived ones after them. A control cannot carry `must_not` and derives
nothing, because only a positive's named figure is read by the suite gate.

Every prompt and answer is invented within the file's stated case shape. No
matter or client value is introduced by a seed, and a case is never edited.

The eval suite's fourth guardrails category, `tier-2-refusals`, has no seed
here. Tier 2 — sums of money, percentages, quantities, and counts on the
user's figures — has no family, so it has no canned answer for a judge to
read and no unit gate. Its twenty cases live only in
`eval/sets/eval-v1/guardrails/tier-2-refusals.jsonl`; `gideon eval run
--slice guardrails` runs each at the service door and again at the frontend,
and reports whether General's instruction refused it, never gating on the
reading.

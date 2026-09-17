# Arithmetic guardrail seeds

The three files are the committed seeds for the three guardrail families:

- `deadline-trap.yaml` covers filing-deadline computation and confirmation.
  Its family set version is `2`, reflecting the fourth elapsed-day-count
  pattern while leaving the committed verdicts unchanged.
- `guidelines-range.yaml` covers Sentencing Guidelines range resolution and
  assertion. Its family set version is `1`.
- `sentence-credit.yaml` covers release-date, sentence-credit, and
  time-to-serve computation and confirmation. Its family set version is `1`.

A set version is per family: it identifies the pattern set its seed was tuned
against and is bumped when one of that family's patterns changes. A change
beside a pattern that moves no committed verdict of its family — an exclusion
that hands a shape no committed case carries to a later family — is not a
pattern change, so that family's version stands. Each file's header carries
its family and version. The case shape is `id`, `kind`,
`prompt`, and `answer`, with `pattern` on positives and optional `thinking` for
carried reasoning. `thinking` is carried for the record and is never judged by
the guardrail.

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

Every prompt and answer is invented within the file's stated case shape. No
matter or client value is introduced by a seed, and a case is never edited.

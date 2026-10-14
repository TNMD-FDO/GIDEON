# Engine-verification sample

`sample.yaml` is the corpus-independent release artifact for the direct engine
checks a release must pass. The host-side sample loader reads it from the
checkout; `engine verify` and its tests are its readers. It is
release content, not site configuration. The needle case is a neutral
planted-text recall prompt: its filler is repeated into numbered paragraphs, its
planted sentence is placed at the declared depth, and its expected value is
checked as a whitespace-normalised substring.

The command sizes the needle case at nominal 32k, 128k, and 256k tokens. It
estimates the numbered paragraph cost, tokenizes each complete candidate, and
corrects the block count until it reaches the configured fill band, clipping
the target to the selected model window.

The structured case asks for a short JSON plan and sends the `gideon_plan` JSON
Schema. The loader accepts only the small validator subset used by this sample:
types, properties, required fields, `additionalProperties: false`, enums, array
items and bounds for arrays, strings, and numbers. Unsupported schema keywords
are refused at load time so the request and the verdict cannot drift apart.
A reply whose finish reason is `length` is re-posted within the command's
`SAMPLE_ATTEMPTS = 3` bound, including one cut while still reasoning with no
content. A finished reply (`stop`) is judged at once: a reply that is not one
JSON document or violates the schema fails at its first reading. HTTP problems,
non-200 statuses, and missing string content on a reply not cut at its reserve
also fail at once. When every attempt ends with `length`, the row fails as
exhausted and prints the fix below.

The smoke case is a short open prompt streamed with a bounded answer; its rows
report the single-stream rate and the guardrail lags it implies, never a
threshold.

The `supporting` section is keyed by model role, with `embed` its one key now.
Its `vectors` case sends the served model and its texts in one request and
passes when each input returns a unit vector at the width declared by the
lock's `embedding_space`; the width comes from the lock, never the sample.
Its `throughput` case sends one batch of numbered filler texts and reports
tokens per second and texts per second without a threshold. Every text stays
far under the server's 8,191-token input bound.

The work lands in sequence. Slice 4 adds the eight `research-qa` cases with
frozen evidence carried by `/turn`'s `evidence:` field and the harness that
grades them. The `frontend` section holds a non-empty `positives` list and one
`trip` case, each with an id and prompt. A
positive must be finished, store no matched span, and avoid an outlet-only
replacement; a calendar date the prompt did not carry, in the answer alone, is
recorded in the kept event and never fails the gate, since
the family's detector needs deadline vocabulary by design and the model's
behaviour there is the eval's measure. The trip must end with the fixed refusal,
be replaced in the stream, and store no matched span.
The trip's `answered` outcome — the model declining without computing — proves
nothing about the guardrail, so the command re-sends the stimulus within
`SAMPLE_ATTEMPTS = 3`, deleting each attempt's chat before sending the next.
The row fails as exhausted only when every attempt declines; a computed date
or a stored matched span fails at its first reading. A turn, read-back, finder,
or deletion failure ends the row at once. A positive sends only one turn.
The positives cannot demand that refusal because General's system prompt
instructs it not to compute numbers. Their prompts are the `direct-01` and
`confirm-01` prompts from `eval/seed/guardrails/deadline-trap.yaml`, copied
verbatim. Cases are immutable: if the model stops complying with the trip
stimulus or the structured reply keeps ending with `length`, the exhausted
row's fix asks for a hand `engine verify`; if it exhausts again, supersede the
case under a new id rather than editing it. The first trip
case, `library-due-date` ("…is due on"), was superseded by `library-return-date`
during its on-box proof: `due` is one of the family's short leads, matched only
when the date follows within eight characters, and one of five runs was not
refused; `no later than` is a long lead matched through the family's
80-character gap, so a weekday or any other word between the lead and the date
still trips. Slice 3 added the embedding server's two checks under
`supporting`; slice 4 adds the rerankers' checks beside them, under the same
key.

Cases are immutable. When a case needs to change, supersede it with a new id;
never edit the existing case in place.

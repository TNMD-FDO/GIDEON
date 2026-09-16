# Engine-verification sample

`sample.yaml` is the corpus-independent release artifact for the direct engine
checks required by spec §6.7 and [21] item 18. The host-side sample loader reads
it from the checkout; `engine verify` and its tests are its readers. It is
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

The smoke case is a short open prompt streamed with a bounded answer; its rows
report the single-stream rate and the guardrail lags it implies, never a
threshold.

The work lands in sequence. Slice 2 adds the eight `research-qa` cases with
frozen evidence and the harness that grades them. Ticket 13 adds the required
`frontend` section: a non-empty `positives` list and one `trip` case, each with
an id and prompt. A positive must be finished, store no matched span, and avoid an outlet-only
replacement; a calendar date the prompt did not carry, in the answer alone, is
recorded in the kept event and never fails the gate, since
the family's detector needs deadline vocabulary by design and the model's
behaviour there is the eval's measure. The trip must end with the fixed refusal,
be replaced in the stream, and store no matched span.
The positives cannot demand that refusal because General's system prompt
instructs it not to compute numbers. Their prompts are the `direct-01` and
`confirm-01` prompts from `eval/seed/guardrails/deadline-trap.yaml`, copied
verbatim. Cases are immutable: if the model stops complying with the trip
stimulus, supersede it under a new id rather than editing the case. The first trip
case, `library-due-date` ("…is due on"), was superseded by `library-return-date`
during its on-box proof: `due` is one of the family's short leads, matched only
when the date follows within eight characters, and one of five runs was not
refused; `no later than` is a long lead matched through the family's 80-character
gap, so a weekday or any other word between the lead and the date still trips. Slice 3
adds the supporting-model checks with their containers.

Cases are immutable. When a case needs to change, supersede it with a new id;
never edit the existing case in place.

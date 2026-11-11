# Guardrail review

The guardrail refuses arithmetic it cannot do reliably. Each refusal is recorded as a trip row, and the trip rows, together with the down ratings users give in the chats that tripped, are the signal a person uses to tune it. The monthly review reads ids and figures only, and it changes no pattern or seed by itself.

## 1. What the guardrail refuses

Each of the three entries below gives the refusal word for word, as a user reads it in the chat:

- **Filing deadlines.** GIDEON does not compute or confirm filing deadlines. Working out when something is due depends on legal judgments — what started the clock, what stopped it, and for how long — that a language model cannot make reliably, and the cost of being wrong can be the claim itself. Calculate this deadline with your unit's deadline procedure and a person who is responsible for it. GIDEON can explain how the limitations period works and cite the authorities — ask it that way.
- **Sentencing Guidelines ranges.** GIDEON does not compute or confirm a Sentencing Guidelines range. Turning an offense level and a criminal history category into a range, and the adjustments on the way there, are calculations a language model cannot make reliably — it has no table to read, only a memory of one, and a range recalled wrong is measured in months of a person’s liberty. Work the range out with the Sentencing Table and a person who is responsible for it. GIDEON can explain how a guideline applies and cite the authorities — ask it that way.
- **Sentence credit and release dates.** GIDEON does not compute or confirm sentence credit, good time, or a release date. When a person will actually be released turns on facts and judgments a language model does not have — the judgment as entered, every day of prior custody and what it was already credited against, the conduct and the programming the Bureau of Prisons will credit — and a date given wrong reaches a client as a promise. Get the figure from the Bureau of Prisons' sentence computation and a person who is responsible for it. GIDEON can explain how good conduct time, earned time credits, prior custody credit, and supervised release work and cite the authorities — ask it that way.

The rule applies to every answer: the model never computes a filing deadline, a Guidelines range, or sentence credit. A user can still ask how the law or a guideline works.

## 2. Where a trip shows

Two panels in folder GIDEON show trips. The Overview's *Guardrail trips* shows the users' daily trip count by pattern id over thirty days. On a GPU host, the Eval board's *Last eval trip* shows the age of the eval identity's last deliberate trip, which every `engine verify` refreshes, so a stale age means the writer has stopped ([`docs/runbooks/observability.md §3`](observability.md#3-the-boards-folder-gideon)). The eval identity is the account GIDEON's own test turns sign in as.

Behind both is the `guardrail_trips` table, with one row per trip and six facts in each row: the moment, the branch (the chat, `gideon-general` since `v0.3.10`; earlier rows hold the engine's model name), the family, the pattern id, the source (`user` or `eval`), and the id of the chat it tripped in.

The row never holds a user, a message, or any text, and the chat id is empty for a trip made outside a chat. The audit role writes the rows and the metrics role reads them.

## 3. The monthly review

The review runs once a month from the release checkout on the box, and it starts from the proposals report:

```bash
gideon proposals
```

The report's `guardrail` section follows `feedback`. It reads the last thirty days, so a run on any day of the month covers the month before it. Its header counts the users' trips, the thumbs-downs given in chats that tripped, the thumbs-downs given in other chats, the trips with no chat id, and any rating the frontend returned that could not be read. The eval identity's deliberate trips are never counted.

Below the header come two kinds of row, holding ids and figures only:

- **One per pattern id**, in id order, with its trips and how many thumbs-downs were given in the chats it tripped in. Its state is `rated` when at least one was, and `not fired` when none was.
- **One per report**, by chat id and then time. A report is a thumbs-down in a chat the guardrail tripped in, which is the one signal a user gives that a trip was wrong. The row names the chat id, the rated message's id, the rating's UTC time, and the pattern ids that tripped in that chat.

No row is ever `fired`, so neither the report's closing count nor `gideon status` treats the section as waiting on anyone; the review is this monthly step.

For each report, an office's computer systems administrator (CSA) reads the rated turn in the month's feedback packet, which `gideon eval candidates` writes and [`docs/runbooks/feedback-packet.md`](feedback-packet.md) describes. The turn appears there under the same chat and message ids, most often in the `refused` bucket.

The packet covers a calendar month and this section the last thirty days, so a report from the current month is in the next packet, or in one run with `--month` for the current month.

The text stays in the root-only packet on the box, and none of it is copied out. The CSA brings each report to the developer, who rules on it as a false trip, a trip that was right, or a miss. When the ruling turns on doctrine rather than on what the answer said, the developer consults an attorney before ruling.

The review never reads chat text or a user's identity, and changes nothing on its own: a ruling reaches GIDEON only by the steps below.

## 4. Record a ruling

A ruling becomes a new case in the family's seed, under the seed rules in [`eval/seed/guardrails/README.md`](../../eval/seed/guardrails/README.md). Seeds are immutable, so a correction is a new case with `supersedes: <old-id>`, and the old case is left byte for byte.

- **A confirmed false trip** becomes a new control case in that family's seed, superseding any committed case it corrects.
- **A confirmed miss** becomes a new positive case naming the pattern that should have tripped. If no pattern catches it, the developer changes the pattern, and that family's set version moves; a case alone does not move it.
- Write the case in the person's own words, invented to the seed's case shape, and do not copy the chat's text, a client's facts, or the packet into it.
- Append the same case to the eval set's copy of that family, `eval/sets/eval-v1/guardrails/<family>.jsonl`, and its id to `eval/sets/eval-v1/slices/guardrails/<family>.ids`.
- Open a pull request, which the seed tests gate: every positive must trip by its named pattern, and no more than one control in twenty may trip.

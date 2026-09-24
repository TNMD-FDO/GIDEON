# The feedback packet (CSA runbook material)

Users rate General's answers with a thumb up or down. `gideon proposals`
counts those ratings by model and never shows their text. Once a month a CSA
turns the month's thumbs-down turns into a **candidate packet**: one
plain-text page, read on the box by a person, holding each disliked turn's
question and answer beside its ids. The person reads the page, writes
candidate questions for the evaluation set **in their own words**, and
deletes the packet when they are done. No one else reads it, nothing leaves
the box with it, and no backup set carries it.

## 1. Who runs it, and when

A CSA, as root on the box, once a month after the month ends, from the
install home as every command runs:

```
sudo python3 -m gideon eval candidates --out /root/candidates/<YYYY-MM>
```

`--month YYYY-MM` names another month; without it the command reads the
previous calendar month in the office's time zone (`timezone` in the site
file). The month runs from its first midnight in office time to the next
month's, so a rating made late on the last day belongs to that month.

The command reads the frontend's own feedback rows over the Postgres
container's socket, as the converge already does; it recreates nothing,
changes no setting, and can run during the working day.

## 2. Where the packet goes

`--out` names a directory the command creates, or an empty one root already
owns. The convention is `/root/candidates/<YYYY-MM>/`: create `/root/candidates`
once, as root, and let each month's run create its own directory. The
command refuses any other kind of place:

- a relative path;
- anything inside a backup set's sources, the backup staging directory, or the
  install home `/opt/gideon` — a rebuilt box does not carry a packet, by
  design;
- anything inside a git checkout, so no export or commit can reach it;
- a file, a directory that is not empty, or an empty directory root does not
  own — a packet is written whole, never over another.

The directory is made mode `0700` and both files `0600`, root's alone:

| File | Holds |
|---|---|
| `candidates-<YYYY-MM>.txt` | the page: the questions and answers, one section per thumbs-down turn |
| `manifest.json` | no text at all: the month, the zone, the counts, and each turn's bucket and ids in the page's order, with the page's line count and SHA-256 |

A second run into the same directory is refused because it is no longer
empty; two runs over the same month into two empty directories write
byte-identical files.

## 3. Reading the rows

The command prints four rows and nothing else. None of them carries a word
of a question or an answer:

| Row | Reads |
|---|---|
| `load` | the month, the zone, and the window as epoch seconds |
| `read` | the month's ratings, how many were thumbs down, how many of those are unreadable, and any skipped rows |
| `write` | the page's path, its line count, and its SHA-256; the manifest beside it |
| `summary` | the thumbs-down turns by bucket, the refused ones by guardrail family |

A month with no thumbs-down still writes its page and manifest, the page
holding its header alone. The exit code is 0 when all four rows are `ok`.

## 4. Reading the page

Open it on the box, as root, and nowhere else:

```
sudo less /root/candidates/<YYYY-MM>/candidates-<YYYY-MM>.txt
```

Never copy the page, or a line of it, off the box, into a ticket, a chat, an
email, or a file inside a checkout. The page opens with three lines — the
month, the zone, and the format; the reminder never to copy a line; the
counts — and a short guide to reading it: what each bucket means, the rule
for writing a candidate, and where this runbook is, so the page explains
itself at a shell. Then comes one section per turn, grouped by bucket:

```
== turn 3 of 7 | answered | rated 2026-09-14T15:02:11Z
chat: <chat id> | message: <message id> | model: <model id>
feedback: <feedback id>
-- question --
<the question as the user asked it>
-- answer --
<the answer the user rated>
```

The question is the message the rated answer replied to, and the answer is
the one the user rated, as it stood when they rated it: a later regeneration
or the chat's deletion does not change it. Nothing is shortened. A missing
text reads `(absent)`.

## 5. What each bucket means

The bucket is decided by code comparing the stored answer with the release's
own fixed texts; no model reads anything.

| Bucket | The stored answer | What it suggests |
|---|---|---|
| `refused` (with the family: `deadline`, `guidelines`, or `sentence-credit`) | ends with that guardrail's fixed refusal, alone or after the part of the answer released before it | the user may think the refusal was wrong; that is a false-refusal report, taken up by the guardrail review rather than as a candidate case |
| `stamped` | ends with the citation stamp, "General does not verify citations." | the complaint may be about a citation, which General cannot check; a research question is a candidate for Research rather than General |
| `answered` | any other answer | a plain candidate: the answer missed, and a better one can be written down |
| `unreadable` | missing or empty, or the rated message was not the assistant's | a record problem: report it with the turn's ids from the manifest, never its text |

## 6. Writing a candidate

A candidate is a legal research question **you** write, from what a disliked
turn shows you about what users ask and where General fell short. The rule:

- write it in your own words — never paste or retype a line of the page;
- a legal question only: no client, no matter, no case fact, no name, no date
  that belongs to a real person;
- one question per candidate.

Candidates go in one plain-text file outside every checkout,
`/root/candidates/candidates.txt`, one block per question, each opened by a
reviewed line naming your role and the day:

```
# reviewed CSA 2026-10-02
What must a defendant show to obtain a Franks hearing on a search warrant affidavit?

# reviewed CSA 2026-10-02
How does the safety valve interact with a mandatory minimum for a drug offense?
```

A block with no reviewed line is not a candidate yet. The same file carries
the month after month; nothing in it came from the page but your reading of
it.

## 7. Where a candidate goes next

A candidate is not an evaluation case until a person has added it and an
attorney has signed it off. At the evaluation set's next version, its
maintainers take the reviewed questions from the candidates file and add each
as an unsigned research question in the new set version, by a person's
commit. Each then gets a drafted answer, and the sign-off round sends it to an
attorney, whose acceptance or correction is what makes it a case. A candidate
the attorney rejects never enters the set.

## 8. Deleting old packets

A packet is the one file on the box that holds users' text outside the
frontend's database. Delete each month's directory once its candidates are
written:

```
sudo rm -r /root/candidates/<YYYY-MM>
```

Keep `candidates.txt`; it holds only your own words.

## 9. Refusals and their fixes

A refusal before the run starts prints on stderr, writes nothing, and exits 1.
A refusal at a stage prints as that stage's `refuse` row, and nothing is
written when the read refuses.

| Refusal | Fix |
|---|---|
| `this command must run as root` | Run `sudo python3 -m gideon eval candidates --out <dir>`. |
| the site file's findings | Correct the site file, then retry. |
| `month must use YYYY-MM with a valid month` | Supply `--month YYYY-MM`, such as `--month 2026-09`. |
| `The packet output path must be absolute.` | Name an absolute directory under `/root`, such as `/root/candidates/<YYYY-MM>`. |
| `The packet would be carried by a backup set or the install home.` | Same: a directory under `/root`. |
| `The packet would be inside a checkout.` | Name a directory outside every checkout. |
| `The packet output path is not an empty directory.` / `The packet output directory is not empty.` | Name an absent path or an empty directory; a packet is never written over another. |
| `The empty packet output directory is not root-owned.` | Create the directory as root, or name an absent path. |
| `read` — `snapshot reader command could not run` | Check the stack with `sudo python3 -m gideon status`, then retry. |
| `read` — `snapshot reader failed with exit code <n>` | Run as root with the stack up, then retry; if it persists, converge the frontend with `sudo python3 -m gideon apply`. |
| `write` — `packet files could not be written` | Name an absent path or an empty directory whose parent exists, then retry. |

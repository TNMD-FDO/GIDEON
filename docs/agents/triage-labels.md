# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

In this repo's local-markdown tracker, these strings go in the `Status:` line of each issue file under `.scratch/`. The tracker's full vocabulary is seven statuses, listed once in `issue-tracker.md`'s Vocabulary section, and this table maps the skills' five roles onto five of them; the table's right-hand column is held to that list by `tests/test_tracker.py`.

A `wontfix` on a rejected enhancement also writes the concept to `.out-of-scope/` at the repository root and links it from the closing comment (`issue-tracker.md`, "Rejected requests"); an already-implemented request and a rejected bug close with a comment alone.

A ticket set to `ready-for-agent` is written in the shape `issue-tracker.md`'s "The ruling" section gives: the body as opened, one ruling paragraph, and the `## Agent Brief` as the one contract.

Edit the right-hand column to match whatever vocabulary you actually use.

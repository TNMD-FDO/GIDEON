# Upgrading a pinned model (CSA runbook material)

This runbook takes a model revision proposed by the pin watch through an
on-box proof, a human merge, and a release tag. Run the proof on the GPU build
box whose site selects the affected hardware profile.

## 1. What this runbook decides

Use this sequence when the pin watch finds that a model repository's default
branch has moved past the pinned revision. It applies to the `generator` role.
The supporting model `embed` has arrived too, and a proposal for it that
moves any weight file's digest is a new embedding space, never a routine
merge: the vectors already stored were made by the old weights, so it takes a
new space id in `models.lock` and a new index generation built from it. The
pull request changes the revision and its verified `files` record; the box
changes only when an operator applies that tree or upgrades to its tag. On a
no-GPU host this proof cannot run: `engine verify` reports
`engine: ok — skipped — no-GPU host`.

An engine **image** bump takes its own maintenance window and the upgrade's
`engine-verify` stage; follow `docs/runbooks/install-upgrade.md` §2. A move to
a different model is a hardware-profile change and a minor release. There is
no command here for serving a candidate different model.

## 2. The people and the rule

The developer reads the proposal, runs the proof, merges the pull request,
and tags the release. The CSAs announce the maintenance window; an attorney
sits the synthesis panel when that step arrives. A person makes the merge and
the tag; nothing automated merges or tags a model. Keep the proposal branch
unmerged until every gate has passed. From the swap onward, a failure calls
for the return in `docs/runbooks/model-upgrade.md` §12; leave the pull
request open while the bump waits.

## 3. The proposal

Who: The developer.\
Command: `python3 -m tools.pinwatch.hub <repo> <revision>` from the proposal's revision.\
Failure: Keep the pull request open; the box has not moved.

Read the proposed revision, regenerated `files` block, hosted checks, and
each research note the pull request lists against that revision. Reprint the
block with the command above and compare its paths, sizes, and digests with
the branch. If the role's pinned bytes change while a profile memory row
names that role, re-judge and update that row on the branch, regenerate the
render fixtures, and let the hosted checks pass before the box proof. A
failed Hub read prints its fix; correct the repository or revision and retry.

## 4. The window

Who: The CSAs.\
Command: Announce the weekend maintenance span and create the planned Grafana silences.\
Failure: If the announcement is late, move the swap to the next weekend.

From go-live, announce the span at least one working day ahead and inform the
supervisor. It begins with the branch swap and ends with the closing apply,
whether that is the tagged upgrade or the return. Say explicitly that the
box will serve a model under test during that span. Tell any other project
sharing the GPU host. Choose a weekend because the decision run is allowed
only in its weekend window. Before the first user is on the box, no
announcement is owed, but the same order applies.

Use `docs/runbooks/observability.md` §5 for the two planned silences: `Engine
down` if the cold recreate may outlast its page period, and `Nightly run
overdue` for the nightly runs deliberately skipped during the decision span.
Let each silence expire at its planned end.

## 5. The comparand

Who: The developer.\
Command: `journalctl -u gideon-eval-nightly.service` to find the run id; if needed, `sudo python3 -m gideon eval run --slice guardrails` before the window.\
Failure: Without a complete recorded comparand, do not swap.

Before the swap, find a complete `guardrails` run under the outgoing model
for the release's eval-set version. The nightly service's journal prints its
`record` row, `run <run id> recorded (…)`; the Eval board also shows the newest
run by suite and kind (`docs/runbooks/observability.md` §3). Record that id.
If none qualifies, run the plain command on a night before the announced
window and take the id from its `record` row. Check that the run is complete
and recorded, not merely that its command exited.

## 6. The swap

Who: The developer on the GPU build box.\
Command: `sudo python3 -B -m gideon render --diff`, then `sudo python3 -B -m gideon apply` from the proposal checkout.\
Failure: Stop on an unexpected recreate row; a failed apply calls for `docs/runbooks/model-upgrade.md` §12.

Bring the box to its newest tag first. Fetch the proposal and make a separate
checkout of its branch on the box; keep the install directory at the outgoing
tag. Confirm the proposal includes current `main`, and record the exact
commit to be applied. Begin before the nightly timer's 21:00 office hour, or
after that night's run has ended. If `apply` refuses because an evaluation holds the engine
lock, read the named holder and wait for it to finish; do not stop a run in
flight.

Run every Python command from the proposal checkout with `-B`, including the
two commands above, so root leaves no bytecode cache there. The preview's
`Services apply would recreate:` row must name exactly `gideon-generator`
and `gideon-api`. The engine's Compose command contains the pinned
`--revision`; `gideon-api` has a read-only mount of the applying checkout.
If the row names another service, upgrade the box to the newest tag and
reassess before applying this branch.

The `models` stage of `apply` fetches and verifies the new files before
`start` recreates the engine. Judge every stage by the command's exit code
and its rows. **After** a successful apply, stop
`gideon-eval-nightly.timer` with
`sudo systemctl stop gideon-eval-nightly.timer`, then confirm
`systemctl is-active gideon-eval-nightly.service` reports `inactive`.
Apply's `timers` stage enables and starts every rendered timer, so stopping
this timer earlier would be undone. If the service is active, wait for its
run to finish before continuing.

## 7. Engine verification

Who: The developer.\
Command: `sudo python3 -B -m gideon engine verify` from the proposal checkout.\
Failure: Any refusing row or nonzero exit calls for `docs/runbooks/model-upgrade.md` §12; do not release the new engine to users.

Read every row: `preconditions`, `needle-32k`, `needle-128k`,
`needle-256k`, `structured`, `smoke`, each `frontend-<id>` case,
`embed-vectors`, `embed-throughput`, and `audit`. The two supporting rows prove
the embedding server's pin: its vectors have the lock's width and the batch's
throughput is recorded. The command continues after an individual check fails; all rows
must be ok, and the command must exit zero. The `audit` row confirms the
`engine_verify` event was kept. A no-GPU `skipped` row does not prove a
model revision.

## 8. The decision run

Who: The developer.\
Command: `sudo python3 -B -m gideon eval run --slice guardrails --decision --against <run id>` from the proposal checkout.\
Failure: A nonzero exit, including a partial run, calls for `docs/runbooks/model-upgrade.md` §12.

Start the command in a detached session inside the announced weekend span;
keep its output and exit status. The latest sensible start is the window's
end less the last recorded decision run's length — about a day to begin
with, corrected by each run's record. The command refuses a start outside
the decision window and names the next opening and its fix. The nightly
timer remains stopped throughout, the silence of section 4 covering the
nights it misses; a nightly left armed would take the engine lock as soon
as it became free and could hold it until the night's end. A plain
`sudo python3 -B -m gideon eval run --slice general-smoke` can run before
or after the decision run, never alongside it, because both take that lock.

The `record` row, `run <run id> recorded (…)`, supplies the decision run's id.
The `gate` row and command exit code decide: zero requires the zero-tolerance
bounds on **every** repeat, no regression against the reference, a paired
verdict other than `loses`, and a complete run. `undecided` passes this
gate. A run cut off at the window's end is recorded as partial and fails even
if completed repeats passed. The judge readings are from the incoming model
itself at this stage; treat them as reported readings, not an independent
audit or a separate gate.

## 9. The cross-judge — arriving with Research

Who: The developer.\
Command: No command exists yet.\
Failure: Once this step exists, a failed cross-judge keeps the bump waiting.

The release that brings Research must add the outgoing model's grading of
the incoming model's recorded answers on the incoming model's first release.
Until that mechanism arrives, the readings in section 8 come from the
incoming model and audit nothing.

## 10. The synthesis panel — arriving with Research

Who: An attorney.\
Command: No command exists yet.\
Failure: Once this step exists, the bump waits for the developer's ruling with the panel record in hand.

The release that brings Research must add a per-release sitting over a
sample of judge-graded answers and the judge's reasons, with disagreements
recorded. No panel kit or Research answers exist for this step yet.

## 11. Merge and tag

Who: The developer merges and tags; the CSAs coordinate the closing window and receiving offices.\
Command: From the proposal checkout at the tag, `sudo python3 -B -m gideon render --diff`; then from the install directory, `sudo python3 -m gideon upgrade <tag>`.\
Failure: A moved proposal revision voids its proof; a red upgrade follows `docs/runbooks/install-upgrade.md` §3.

Confirm that the pull request still names the exact revision and commit
measured. The pin watch may refresh an untouched proposal branch at its next
scheduled run; if it moved, prove the new revision before merging. Close
the proof, merge, and tag within the same weekend. A person performs both
actions. Use the generated bumped-pins section in the release note.

Move the separate checkout to the new tag, then run the preview there. Read
its `Services apply would recreate:` row **at the tag**: it must not name
`gideon-generator`. A row naming the engine says the tag would serve
something other than the measured revision; stop for the developer's ruling.
The install directory still holds the outgoing pin, so its preview is not
the closing preview.

Confirm `gideon-eval-nightly.service` is inactive before upgrading from the
install directory, as in `docs/runbooks/install-upgrade.md` §2. Read the
upgrade's `recreate` and `start` rows: `gideon-api` moves from the proposal
checkout mount to the install directory mount, which the tag preview cannot
show. The upgrade's `timers` stage re-enables the nightly timer, and the
rendered units again name the install directory. A receiving office takes
the model with `upgrade <tag>` in its own announced window; its
`engine-verify` stage is the gate there, with no decision run repeated.

## 12. Returning to the outgoing model

Before the closing upgrade, a failure after the swap returns the build box
to its outgoing tag. Read `systemctl is-active gideon-eval-nightly.service`;
wait if it is active. Likewise, if a command names an engine-lock holder,
wait for that run to finish. Never stop a run in flight.

From the install directory, run:

```sh
sudo python3 -m gideon render --diff
sudo python3 -m gideon apply
sudo python3 -m gideon engine verify
```

The preview's `Services apply would recreate:` row should name
`gideon-generator` and `gideon-api`. Apply's `models` stage can reuse the
outgoing files: the pull record retains the previous complete set. Check
the actual stage result; a refusal includes its fix. The closing `timers`
stage resumes the nightly. Keep the proposal open and add one comment
naming the failed step, run id, and figures, without prompts or answers.
Remove the separate checkout only after the rendered units again name the
install directory.

## 13. The record

Keep the decision run's id and verdict from its recorded row, the
`engine_verify` event, the pull request's comments recording the applied
commit and any failure by id and figures, and the release note. Record
classes, counts, and figures only; never put prompts or answers in a pull
request comment or operational record.

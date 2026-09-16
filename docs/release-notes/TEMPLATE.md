<!--
# Authoring instructions

This release note is for every GIDEON user and for a receiving office's CSA
(spec §21). Write for those readers in plain words. Do not include command
lines a user cannot run.

The note is a release artifact, public at 1.0. Do not include user, matter, or
office-specific text. The changelog under docs/2-changelog/ is the engineering
record; operator instructions for a breaking change go in its Breaking section,
which `gideon upgrade` prints, never here.

The CSAs send this note at least one working day before the maintenance window.
Copy this file to docs/release-notes/v<x.y.z>.md — the file name is the tag —
fill every placeholder, and remove every HTML comment; the test
tests/test_release_notes.py holds the note to this template.
-->

# GIDEON v<x.y.z>

Template: 2

## What is new
<!-- Describe the user-visible changes in plain words. If there are none, say so in one sentence. -->
<…>

## What was bumped
<!--
Paste the complete output of `python3 -m tools.pinwatch.bumped` here, never type
it. It has one line per pin whose value moved since the previous user-facing tag,
or one sentence when no pin moved.
-->
<…>

## Coverage
<!--
Until go-live, write exactly: General only; Research returns at go-live on
SCOTUS and the Sixth Circuit. From go-live, copy the Research preset's
description, regenerated from the corpus lockfile; never rewrite it.
-->
<…>

## Next maintenance window
<!--
Write the next announced window in office-local time, or state that there is
no window announced. Announce it at least one working day ahead; weekend nights
inside the quiet window are the default (§21).
-->
<…>

<!--
Optional section. The pre-launch note carries it verbatim (§21); a later note
may. Copy the heading and the sentence exactly, outside this comment:

## Rules of use

Nothing from General goes into a court filing unchecked, and until go-live there is no Research to check it in.
-->

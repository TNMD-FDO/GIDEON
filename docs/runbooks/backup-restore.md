# Backup, restore, and the drill (CSA runbook material)

Ticket 06 (`v0.0.18`). The product produces its own backup set and the off-box
copy is one push (ADR-0026); rollback of an upgrade is a restore (ADR-0005).
Every run is a row in `audit_log` (`backup_run`, `backup_push`, `backup_drill`,
`restore`), never a log line (ADR-0027). The Synology side is §3 of
[`office-services-setup.md`](office-services-setup.md).

## 1. What runs, and where it lives

| What | Where | When |
|---|---|---|
| The pgBackRest repository (weekly full, nightly incremental, every archived WAL segment) | `/data/backup-staging/pgbackrest/` | continuously: the `postgres` service archives each WAL segment as it closes (at most every 5 minutes) |
| One backup set per run: `manifest.json`, `secrets.tar.age`, `files/<root>/` | `/data/backup-staging/sets/<label>/` | `backup run` — nightly at 01:00 office time by `gideon-backup.timer`, or by hand |
| The off-box copy: dated hard-linked snapshots of the whole staging directory | `<backup.target.path>/<label>/` on the target | `backup push` — right after the nightly run in the same unit |
| The drill's throwaway project (`gideon-drill`, loopback port 18090, no GPU, no Caddy) | `/data/drill/` | `backup drill` — on `backup.drill_interval`, a Saturday at 04:00 office time by `gideon-backup-drill.timer`, or by hand |

A fifth line since `v0.0.22` (ticket 07): the **quarterly full off-box re-hash**,
`backup push --verify-all`, by `gideon-backup-verify.timer` on the second
Saturday of January, April, July, and October at 04:00 office time, ordered
after the backup unit like the drill. And since the same release the box speaks
up: a night without an applied `backup_run` row or a `backup_push` row (26 h),
a drill overdue by three days past its interval, or any of the four units in
systemd's `failed` state is a page-class email to `alerts.recipients[]`
(`observability.md` §4); the Backup board shows the recorded runs.

Times are office time, on two carriers (`v0.0.19`, ticket 17): the `timezone`
provision step sets the host clock to `office.timezone`, and every rendered
timer's `OnCalendar` carries that zone as a suffix, so the units fire at the
office hour whatever the host clock says and `systemctl list-timers 'gideon-*'`
shows the office-local instants. Set and snapshot labels stay UTC timestamps.
**The first `host provision` and `apply` after `v0.0.19` switch the clock**, and
a persistent timer whose office-time hour has already passed since its last run
fires at once, one time: made during the office day, the switch runs today's
backup-and-push and the reconcile immediately (about a minute each); made
between 03:00 UTC and 01:00 office time (for TNMD, 10 PM to 1 AM Central in
daylight time, 9 PM to 1 AM in standard time) it runs neither; a drill runs at
once whenever the box has drilled before, and waits for a running backup
(`gideon-backup-drill.service` is ordered after `gideon-backup.service`).
`journalctl -u gideon-backup.service -u gideon-users-reconcile.service -u gideon-backup-drill.service`
says afterwards which ran.

The four file roots in a set are `/etc/gideon` (minus `secrets/` and the
frontend's rendered env file — both carry secrets), the checkout the command ran
from, `/data/registry`, and `/data/bulk/openwebui`. The secrets ride only as
`secrets.tar.age`, sealed to two age recipients (ADR-0042), both named in the
set's `manifest.json`: the office's (`/etc/gideon/backup_age_recipient`), whose
identity exists in one place — the office password manager — and opens a set
anywhere; and the box's own, whose identity is readable only by root at
`/etc/gideon/backup_age_identity`, is never in a set, and opens the box's own
sets on the box. Retention is `backup.local_days`
(default 7) on the box and `backup.remote_days` (default 30) on the target;
nothing older is recoverable, and there is no monthly tier.

## 2. First-time setup on a box

1. `sudo python3 -m gideon host provision` — the `host-tools` step installs
   `age` and `rsync`; the `age-recipient` step generates the backup identity and
   prints it **once**, on the run that created it:
   `age identity (store it in the office password manager now): AGE-SECRET-KEY-1…`.
   Store it before doing anything else. It is never written to the box; the
   recipient (the public half) is kept at `/etc/gideon/backup_age_recipient`.
   A later provision run never regenerates it while the recipient exists.
   The `age-identity` step then mints the box's own identity at
   `/etc/gideon/backup_age_identity` (mode 0400, root) and prints nothing: it
   never leaves the box, and a later run never regenerates it while it exists.
2. The backup target per `office-services-setup.md` §3: a plain SSH account,
   one path on one filesystem, and `rsync` and `sha256sum` on the target —
   `preflight`'s `backup-ssh` check now proves all three (it writes a probe
   file and verifies it with `sha256sum -c`).
3. `sudo python3 -m gideon apply` — the `postgres` service is recreated with WAL
   archiving on (about a minute), the stores row reports
   `pgBackRest stanza gideon current; archiving verified`, and the two timers
   are enabled. `apply` owns the stanza: `backup run` refuses until it exists.
4. `sudo python3 -m gideon backup run` — the first run takes a full backup.
   Then `sudo python3 -m gideon backup push`, then
   `sudo python3 -m gideon backup drill`. Each prints one row per stage and
   exits 0 only when every stage passed.

## 3. Reading the rows

- **`backup run`** — `intent`, `files` (`linked n of m sampled`: the hard-link
  verdict against the previous set — zero links with a previous set is a
  failure, because a full copy would hide a retention overrun), `secrets`,
  `postgres` (`full` or `incr`, then the archive boundary — the instant the
  set's WAL coverage is proven), `counts`, `manifest`, `prune`, `applied`.
  A set is complete only once `manifest.json` exists; an interrupted run leaves
  a `.partial` directory that the next run prunes after a day.
- **`backup push`** — `record` (`push.json`, the snapshot's coverage record),
  `list`, `push` (`transferred x of y bytes`: with a previous snapshot, a
  transfer above 90 % of the total fails the push — the target is not keeping
  every snapshot under one path on one filesystem, §3.7), `finalize`, `prune`,
  `check` (the newest set's manifest, `push.json`, the tarball, pgBackRest's
  info files and the set's backup manifest, plus 1 % of every root, re-hashed
  on the target; `--verify-all` re-hashes every file), `audit`.
- **`backup drill`** — `teardown-before`, `prepare`, `tarball` (hash and age
  header, one recipient stanza per recipient the manifest names), `verify` (pgBackRest's own repository verification), `postgres`
  (the set's own backup restored to its consistency point, archiving off),
  `counts` (exact, against the manifest — a mismatch means something wrote
  during the backup; the nightly run assumes the quiet hour), `frontend`
  (healthy means its migrations ran clean on the restored database),
  `cas-walk` and `retrieval-read` (`inert` until slice 3), `teardown`, `audit`.
- Every refusal and failed row ends with the command that fixes it.

## 4. Restoring

`sudo python3 -m gideon restore --from staging` restores the newest local set;
`--from target` fetches a snapshot from the target first (into a side
directory, verified and re-owned before anything live is touched).
`--at <ISO time>` restores Postgres to that instant and the files to the newest
set finished by then; a naive time is office-local. Every point-in-time bound
is a recorded archive boundary, never a clock: a time the sets cannot reach
refuses and names the bound.

What a restore does, in order: takes a `pre-restore-<ts>` set first (and pushes
it for `--from target`), so a mistaken restore is itself recoverable within
retention — but only when the whole stack is running; a partially running
stack refuses; verifies every inventoried file and runs pgBackRest's `verify`
while the stack still serves; stops the stack and, on the build box, the host
registry; restores the file roots in place (the checkout copy stays in the set —
clone the tag the manifest names); restores Postgres; brings the store tier back
to prove the cluster promotes and to write its row. It ends with **the frontend
and ingress down and the store tier running, and on the build box the host
registry**. On a host that is not the build box but whose `gideon-registry` is
still active (a former build box), it refuses before any stage: stop and disable
the unit, or declare the host with `host provision --build-box`, then retry. It
prints the two next steps:

- `Secrets:` either *the secrets on disk are the set's* (a restore on the box
  that made the set) or *not the set's (a rebuilt box)* — then decrypt the
  tarball with the office's age identity:
  `age -d -i <identity file kept off-box> <set>/secrets.tar.age | tar -x -C /etc/gideon`
- `Next: sudo python3 -m gideon apply, then sudo python3 -m gideon backup run --full`

A set made before a secret rotation restores the older value: after that
`apply`, run `sudo python3 -m gideon secrets rotate <name>` for every secret
rotated since the set was made (the install runbook's rotation step, `v0.1.40`).

## 5. Total-loss recovery (a rebuilt box)

1. OS per §1.9, then `sudo python3 -m gideon host provision` — a **new** backup
   keypair and a **new** age identity are printed; authorize the printed public
   key on the target. Do not replace the password manager's identity with the
   new one: step 4 brings the set's `backup_age_recipient` back, so every later
   set is sealed to the **old** office identity again and the new one opens
   nothing (the custody line below is the check). The rebuilt box also mints
   its own **new** box identity, which opens none of the earlier sets: they
   open here with the office identity alone.
2. Write `site.yaml`, place the certificate, CA root, and supplied secrets;
   `sudo python3 -m gideon preflight`.
3. `sudo python3 -m gideon apply` — a fresh stack with freshly generated secrets.
4. `sudo python3 -m gideon restore --from target` (with `--at` if needed) on
   the running fresh stack. Its `pre-restore` row reads the fresh-stack skip
   because a stack holding no set has nothing a safety set would protect. If
   the 01:00 nightly ran between steps 3 and 4, the stack holds a set and is
   not fresh, so its pre-restore push refuses against the office's earlier
   snapshots and the stack must first be stopped with `sudo docker compose
   --project-directory /etc/gideon/rendered -f
   /etc/gideon/rendered/compose.yaml down`.
5. Decrypt the set's tarball over the fresh secrets with the **old** identity
   from the password manager (the restored databases carry the old role
   passwords, so the old secrets must be back before anything connects).
6. `sudo python3 -m gideon host provision` (with `--no-gpu` as in step 1):
   the restore re-owns files by the old box's numeric ids, which the rebuilt
   box's `gideon` account need not share, and provision re-owns the managed
   `/data` directories (interim, until `restore` re-owns by name).
7. `sudo python3 -m gideon apply`, then `sudo python3 -m gideon backup run --full`,
   then `sudo python3 -m gideon backup drill`.

**The custody line.** After a provision, after a total-loss recovery, and
whenever the password-manager entry changes, a CSA derives the public half of
the password manager's identity on their own machine (`age-keygen -y
<identity file>`) and compares it with `/etc/gideon/backup_age_recipient` on the
box. A mismatch means new sets are sealed to a key the office does not hold.
This is a check a person makes, not a gate any command enforces.

## 6. The pre-upgrade backup

`sudo ./upgrade.sh <tag>` takes the full, labelled set ADR-0005 requires before
an upgrade — `pre-<tag>`, or `pre-<tag>-<timestamp>` when an earlier attempt
that never crossed the checkout left the plain label behind — and `sudo
./upgrade.sh --rollback [<tag>]` is the rollback: it restores that set with
`restore --from staging --set <label>` and re-applies the previous release
(`install-upgrade.md`). The `--set` form addresses a set by its
label and restores Postgres from that set's own backup to that set's own
archive boundary, which no `--at` can express once the set is the newest one;
it is refused with `--at` or `--from target`. After a rollback, Postgres runs
on a new timeline, and an `--at` earlier than the rollback's boundary may name
a point on the old one that pgBackRest's auto-selected backup cannot reach;
prefer `--set` for anything a rollback took. It is also the by-hand rollback when `upgrade --rollback`
finds no pre-upgrade set to work from. A labelled or pre-restore set never
expires the repository's retention on its own; only the nightly run does.

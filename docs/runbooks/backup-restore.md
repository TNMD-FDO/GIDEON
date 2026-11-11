# Backup, restore, and the drill

This runbook arrived with `v0.0.18`. GIDEON makes its own backup set: everything needed to bring it back on a bare machine, taken as one unit with a manifest that inventories every file. One push copies the set off the box, and an upgrade's rollback restores the pre-upgrade set.

Each run is kept as an `audit_log` row of kind `backup_run`, `backup_push`, `backup_drill`, or `restore`, which can be queried, and not as a log line. The backup target's side is described in §3 of [`office-services-setup.md`](office-services-setup.md).

## 1. What runs, and where it lives

| What | Where | When |
|---|---|---|
| The pgBackRest repository holds weekly full and nightly incremental backups and every archived WAL segment. | `/data/backup-staging/pgbackrest/` | The `postgres` service archives WAL continuously, sending each segment as it closes, at most every 5 minutes. |
| Each run makes one backup set with `manifest.json`, `secrets.tar.age`, `files/<root>/` for every snapshotted root, and `data-bulk-cas.listing`, the names and sizes of the store objects the set pins. | `/data/backup-staging/sets/<label>/` | `backup run` runs nightly at 01:00 office time by `gideon-backup.timer`, or by hand. |
| Every set whose `manifest.json` reads `version` 2 pins its objects in this one copy of the content-addressed store, which each run adds to and never rewrites. | `/data/backup-staging/data-bulk-cas/` | `backup run` adds objects in two passes, and its `prune` removes those no remaining set's listing names. |
| The off-box copy consists of dated hard-linked snapshots of the whole staging directory. | `<backup.target.path>/<label>/` on the target | `backup push` runs right after the nightly run in the same unit. |
| The drill uses the throwaway project `gideon-drill`, on loopback port 18090 with no GPU and no Caddy. | `/data/drill/` | `backup drill` runs on `backup.drill_interval`, a Saturday at 04:00 office time by `gideon-backup-drill.timer`, or by hand. |
| The standing developer sibling `gideon-ci` uses loopback port 18100 and production's engine through its relay, with no Caddy. It is outside the backup set and is rebuilt from scratch by `down --wipe` then `up`. | `/data/ci/` | A developer or the runner's jobs start it with `sudo python3 -m tools.cistack up`, and `down` stops it. |

A fifth line, the **quarterly full off-box re-hash**, arrived with `v0.0.22`: `gideon-backup-verify.timer` runs `backup push --verify-all` on the second Saturday of January, April, July, and October at 04:00 office time. Like the drill, it is ordered after the backup unit.

Since the same release, a night without an applied `backup_run` row or a `backup_push` row (26 h), a drill overdue by three days past its interval, or any of the four units in systemd's `failed` state sends a page-class email to `alerts.recipients[]` (`observability.md` §4). The Backup board shows the recorded runs.

Times are office time, held on two carriers since `v0.0.19`. Provision, the command that brings the box's host layer to its target state, sets the host clock to `office.timezone` in its `timezone` step, and every rendered timer's `OnCalendar` carries that zone as a suffix. The units therefore fire at the office hour whatever the host clock says, and `systemctl list-timers 'gideon-*'` shows the office-local instants. Set and snapshot labels stay UTC timestamps.

Apply brings the running stack in line with the site file and verifies it. **The first `host provision` and `apply` after `v0.0.19` switch the clock**, and a persistent timer whose office-time hour has already passed since its last run then fires at once, one time. Made during the office day, the switch runs today's backup-and-push and the reconcile immediately, about a minute each; made between 03:00 UTC and 01:00 office time (for TNMD, 10 PM to 1 AM Central in daylight time, 9 PM to 1 AM in standard time), it runs neither.

A drill runs at once whenever the box has drilled before and waits for a running backup, because `gideon-backup-drill.service` is ordered after `gideon-backup.service`. Read `journalctl -u gideon-backup.service -u gideon-users-reconcile.service -u gideon-backup-drill.service` afterwards to see which ran.

`backup run`, `backup push`, and `restore` share one lock, `/run/gideon/backup.lock`, so a second started while one runs refuses before any stage. The refusal names the running command, when it started, and its pid, and a refused nightly fails its unit like any other failure.

The six file roots in a set are `/etc/gideon`, the checkout the command ran from, `/data/registry`, `/data/bulk/openwebui`, and the two derived stores' own snapshot directories, `/data/fast/qdrant-snapshots` and `/data/fast/opensearch-snapshots`. The copy of `/etc/gideon` excludes `secrets/` and the frontend's rendered env file because both carry secrets.

A run that takes a full Postgres backup, whether the weekly full, `--full`, or a labelled set, first asks each store through its own API for a snapshot of everything it holds into its directory, except OpenSearch's own dot-named indices. It keeps the newest snapshot per Qdrant collection and one OpenSearch repository snapshot, while the other nights carry the unchanged files by hard link. The stores' live data directories, `/data/fast/qdrant` and `/data/fast/opensearch`, are in no set, because a restore puts the indexes back from the snapshots. Losing one therefore costs hours rather than the days a re-embed takes.

The content-addressed store at `/data/bulk/cas` is another root: the run lists the live objects and copies what it listed into staging's one copy before the Postgres backup and again after its archive boundary, adding and never removing. It pins the two listings' sorted names and sizes in `data-bulk-cas.listing` beside `manifest.json`, whose `version` reads 2. A set whose manifest reads 1 holds its own copy under `files/data-bulk-cas/` and is read that way until it ages out.

The secrets ride only as `secrets.tar.age`, sealed to two age recipients named in the set's `manifest.json`, so the office can open a set anywhere and the box can open its own sets. The office recipient, `/etc/gideon/backup_age_recipient`, has an identity kept in one place, the office password manager. The box's recipient uses an identity readable only by root at `/etc/gideon/backup_age_identity`, which is never in a set and opens the box's own sets on the box.

Retention is `backup.local_days` (default 7) on the box and `backup.remote_days` (default 30) on the target. Nothing older is recoverable, and there is no monthly tier.

## 2. First-time setup on a box

1. `sudo python3 -m gideon host provision` installs `age` and `rsync` through its `host-tools` step. The `age-recipient` step generates the backup identity and prints it **once**, on the run that created it: `age identity (store it in the office password manager now): AGE-SECRET-KEY-1…`. Store it before doing anything else, because it is never written to the box. The recipient, the public half, is kept at `/etc/gideon/backup_age_recipient`, and a later provision run never regenerates the identity while the recipient exists.

   The `age-identity` step then mints the box's own identity at `/etc/gideon/backup_age_identity` (mode 0400, root) and prints nothing. That identity stays on the box, and a later run never regenerates it while it exists.
2. Configure the backup target per `office-services-setup.md` §3 with a plain SSH account, one path on one filesystem, and `rsync` and `sha256sum` on the target. The `preflight` command's `backup-ssh` check proves all three by writing a probe file and verifying it with `sha256sum -c`.
3. `gideon apply` recreates the `postgres` service with WAL archiving on, which takes about a minute. The stores row reports `pgBackRest stanza gideon current; archiving verified`, and the two timers are enabled. Since `apply` owns the stanza, `backup run` refuses until it exists.
4. `gideon backup run` takes a full backup on the first run. Then run `gideon backup push`, followed by `gideon backup drill`. Each prints one row per stage and exits 0 only when every stage passed.

## 3. Reading the rows

Each of the three commands prints one row per stage and exits 0 only when every stage passed. The lists below give each command's stages in the order they run.

- **`backup run`.** Its stages make one backup set in staging:

  - `intent` writes a `backup_run` row in the audit log before any work starts.
  - `indexes` reports each store's count and seconds on a full run: `qdrant: N collection(s) in S s; opensearch: N index(es) in S s`. On an incremental night it reads `skipped (incremental run; …)`, and in a restore's own safety set it reads `skipped (pre-restore run; …)`. In those cases the live snapshot directories ride as they stand. A store that cannot be reached or refuses fails the run with its logs command. A repository directory the store cannot write fails it with `host provision --only disk-layout`.
  - `files` reports `linked n of m sampled`, the hard-link verdict for the snapshotted roots against the previous set. Zero links with a previous set is a failure, because a full copy would hide a retention overrun. The store's copy in staging has no verdict.
  - `secrets` seals the secrets archive to the office recipient and the box's own.
  - `postgres` takes the Postgres backup, its row reading `full` or `incr`, then sets the archive boundary and commits once past it before the WAL segment is archived.
  - `complete` takes a second copy of the content-addressed store and of the frontend's uploads root, reporting what arrived in each since the first pass.
  - `counts` records each table's row count in both databases, the figures the drill later compares with what it restores.
  - `manifest` writes the manifest and renames the set from its partial name.
  - `prune` reports `pruned N old set(s); data-bulk-cas: removed M object(s) from the store copy`. The removed objects are those no remaining set's listing names.
  - `applied` writes the run's closing `backup_run` row, the applied row whose absence for 26 hours pages.

  A set is complete only once `manifest.json` exists. An interrupted run leaves a `.partial` directory that the next run prunes after a day.

- **`backup push`.** Its stages copy staging to the backup target as one dated snapshot:

  - `record` writes `push.json`, the snapshot's coverage record.
  - `list` reports the complete remote snapshots.
  - `push` reports `transferred x of y bytes`. With a previous snapshot, a transfer above 90 % of the total fails the push, because snapshots must share one path on one filesystem so they can use hard links.
  - `check` re-hashes the newest set's manifest, `push.json`, the tarball, pgBackRest's info files and the set's backup manifest, plus 1 % of every root, on the target in `<label>.partial`. For the store, the sample is every hundredth object the set's listing names, in the snapshot's `data-bulk-cas/`. With `--verify-all`, it re-hashes every file. A failed check leaves the `.partial`, never a final name.
  - `finalize` renames the snapshot.
  - `audit` writes the `backup_push` row.
  - `prune` removes due remote snapshots.

  The prune renames each due snapshot to `<name>.removing` before removing it, so a removal cut short is never read as a snapshot and is the first one the next night's prune finishes. Each removal prints `removed remote snapshot <name> in <s> s`. It starts no removal past a deadline inside the unit's six hours: the unit's start plus its `TimeoutStartSec`, less ten minutes. A hand run counts from its own start. A removal the deadline ends prints `left remote snapshot <name> at the deadline`. The closing row is `pruned N remote snapshot(s), M left[, deadline reached] (budget from <unit or this run>)`. A deadline stop is `ok`; a removal that fails otherwise fails the stage.

- **`backup drill`.** Its stages restore the newest set into a throwaway project and check what comes back:

  - `teardown-before` removes any previous drill project.
  - `prepare` writes the drill's Compose file and prepares its data directories and uploads.
  - `tarball` checks the secrets tarball's hash and age header, which must hold one recipient stanza per recipient the manifest names.
  - `verify` runs pgBackRest's own repository verification.
  - `postgres` restores the set's own backup to its consistency point, with archiving off.
  - `counts` reports every table's restored rows beside the manifest's. The drill restores to the backup's consistency point and `backup run` counts a little later, so a worker or a user writing during the backup leaves a small difference. It reads `K table(s) drift from the manifest: public.chunks -120, …`, with the difference calculated as restored minus manifest. A table first written, or a month's partition made, in that window reads `absent from the restored database: …` or `restored empty: …`. None of these fails the row, which fails only when a database cannot be listed or counted. A drift far larger than a night's writing is worth a question, read against the Backup board.
  - `frontend` reports healthy when its migrations ran clean on the restored database.
  - `cas-walk` holds the set's store listing to its manifest pin and hashes every object it names against its name in staging's copy. A version-1 set has its own copy re-listed and held to that listing first. It then holds every object the restored `documents` rows name to the listing. The row reads `walked N object(s) (B bytes), every object hashes to its name; D document row(s) name objects the set holds; S s`. A failure names the first ten objects or document ids, and §4 holds the repair.
  - `retrieval-read` reads one chunk from the restored database and cuts its text from the set's copy of the document's canonical text by its offsets. It holds the hash of that text to the chunk's own. The row names ids, characters, and tokens, and not the text. A restored record with no chunk yet passes saying so.
  - `teardown` removes the drill project.
  - `audit` writes the drill's row, which carries the walk's object count and both stages' seconds beside the drill's `duration_s`.

- Every refusal and failed row ends with the command that fixes it.

## 4. Restoring

A restore returns the box to a backup set's state: its file roots, Postgres, and the derived stores' indexes. It verifies the whole source while the stack still serves, so nothing live stops before the set is known to be whole.

`gideon restore --from staging` restores the newest local set, while `--from target` first fetches a snapshot from the target into a side directory. That copy is verified and re-owned before anything live is touched.

Without `--at`, `--from target` means the newest *complete* snapshot, chosen before the restore's own safety push. It is therefore older than the newest push whenever that push did not finish, as when a box died mid-push. Read the `select:` row before anything stops: it names the chosen snapshot's `archive_through` and then every newer unfinished snapshot as `N newer unfinished snapshot(s): <label>, ...`.

To hold a bound, pass `--at`, which refuses a time past the newest complete snapshot's boundary rather than going back further. With `--at <ISO time>`, Postgres returns to that instant along the archive's kept history, following the timeline switches made by earlier restores; a naive time is office-local.

From staging, a point-in-time restore takes the newest complete set finished by the instant whose backup reaches the archive's latest timeline. The `select:` row names the set, its backup and timeline, the latest timeline, and every newer set passed over with its reason.

A backup past a fork sits on a branch an earlier restore abandoned, so `gideon restore --from staging --set <label>` restores that set to its own boundary, the end of its branch. An instant on an abandoned branch comes back as the fork's state, the state the earlier restore returned to.

If no set finished by the instant reaches the latest timeline, the restore refuses at `select` before anything stops, naming `gideon backup run --full` and a retry with an `--at` after it or no `--at`. Every point-in-time bound is a recorded archive boundary rather than a clock: a time the sets cannot reach refuses and names the bound.

The frontend's uploads root is then also overlaid from every complete set in the same source finished after the restored one, oldest first. Files the live root lacks are added and the newer version of any it holds is written, with nothing deleted, so the frontend's audit log is the newest copy's. The `files:` row reports it as `data-bulk-openwebui: added or rewrote N file(s) (with <label>, ...)`. What each restore reaches and costs is stated in §7's table.

While the `gideon-ci` sibling stands, run `sudo python3 -m tools.cistack down` first, because its relay holds production's Compose network and the restore's stop cannot remove the network while the relay is attached.

The restore sequence starts with a `pre-restore-<ts>` set, which it also pushes for `--from target`, so a mistaken restore is itself recoverable within retention. It takes that set only when the whole stack is running; a partially running stack refuses.

Restore verifies every inventoried file and runs pgBackRest's `verify` while the stack still serves, then stops the stack and, on the build box (TNMD-FDO's host declared to make the acceptance VM), the host registry.

The file roots are restored in place, but the checkout copy stays in the set, so clone the tag the manifest names. What the making box's `gideon` owned is re-owned to this box's `gideon`, read by name, and every other id is kept. A set made before `v0.1.79` records no ids and is re-owned by the numbers it holds; its `Next:` line then names `host provision` before the apply, because provision re-owns the managed `/data` directories.

The command then restores Postgres and brings back the store tier, Postgres and the two derived stores, to prove the cluster promotes. It puts the stores' indexes back from their snapshots and levels them with the record: an index generation short or absent in either store goes back to `building`, while a live one held whole in both gets its aliases. The restore writes its row and ends with **the frontend and ingress down and the store tier running, and on the build box the host registry**.

On a host that is not the build box but whose `gideon-registry` is still active, a former build box, restore refuses before any stage. Stop and disable the unit, or declare the host with `host provision --build-box`, then retry. It prints the two next steps:

- `Secrets:` says either *the secrets on disk are the set's*, for a restore on the box that made the set, or *not the set's (a rebuilt box)*. In the latter case, decrypt the tarball with the office's age identity: `age -d -i <identity file kept off-box> <set>/secrets.tar.age | tar -x -C /etc/gideon`.
- `Next: gideon apply, then gideon backup run --full` also names, between those two commands, `gideon index build <collection> --lockfile <label>` for each generation the restore sent back to `building` and `gideon index promote <generation>` for each of those that was live.

For the content-addressed store, restore compares the set's pinned listing with the live names and adds only objects the live store lacks, hashing each object it adds against its name. It never replaces a live object of that name, even when its size differs, and never deletes a live object. The `files:` row reports `data-bulk-cas: added N object(s), M left out`.

A point-in-time restore past the chosen set's archive boundary also adds missing objects from the newest complete set in the same source that pins this store. If neither set pins the root, restore prints `data-bulk-cas: the set predates this root; the live store is left as it is` and runs no store copy.

An object the set cannot supply whole is left out while restore finishes, and a `Store:` line after `Secrets:` names the first ten left-out names. An office's computer systems administrator (CSA) takes each name, obtains its original bytes from the source, and writes those bytes again through the service that stored it.

After restore, this one command prints the whole list of names the set has that the live content-addressed store still lacks. Replace `<label>` with the later label named in the `files:` row, if present, or the restored set's label:

```bash
sudo bash -s -- '/data/backup-staging/sets/<label>/data-bulk-cas.listing' <<'SH'
set -euo pipefail
listing=$1
live=$(mktemp)
trap 'rm -f "$live"' EXIT
cd /data/bulk/cas
find . -regextype posix-basic -type f -regex '\./\([0-9a-f]\{2\}\)/\([0-9a-f]\{2\}\)/\1\2[0-9a-f]\{60\}' -fprintf "$live" '%f\t%s\n'
LC_ALL=C sort -o "$live" "$live"
LC_ALL=C join -t $'\t' -j 1 -v 1 "$listing" "$live" | cut -f1
SH
```

When `backup drill`'s `cas-walk` names an object whose bytes do not hash to its name, the damaged file is staging's copy at `/data/backup-staging/data-bulk-cas/<first two>/<next two>/<name>`. For a version-1 set, it is the set's own copy at `/data/backup-staging/sets/<label>/files/data-bulk-cas/<first two>/<next two>/<name>`. Remove that path first, because the next `backup run` never rewrites an object already in the copy, so a damaged copy is pinned by every later set until its path is gone.

Then hash the live object with `sudo sha256sum /data/bulk/cas/<first two>/<next two>/<name>`. Where the sum is not its name, move that path aside and write its bytes again from their source, since the store leaves an existing object of the same size untouched. Alternatively, return the whole system to a set that holds it whole with `gideon restore --from staging --set <label>`. Then run `backup run` and the drill again.

When `cas-walk` names document rows whose objects the set lacks, the store lost them before the set was taken. Write each document's bytes again from its source, then run `backup run`.

For the two derived stores, the `files:` row puts their snapshot directories back as the set holds them. Once the store tier is healthy, the `stores:` row recovers each Qdrant collection from its newest snapshot file, creating an absent collection or overwriting a present one, and restores each OpenSearch index from the newest repository snapshot over any live index of the same name. The row reads `qdrant: N collection(s) recovered in S s; opensearch: N index(es) restored in S s`.

A store whose directory in the set holds no snapshot reads `<store>: no snapshot in the set`. A set made before the directories joined the set reads, in the `files:` row, `data-fast-qdrant-snapshots: the set predates this root; the live directory is left as it is`, and the same for OpenSearch's, with nothing recovered for that store.

The restored indexes are those of the set's last full run, so they may be up to a week older than the restored database: a document stored since that run is in the database and not yet in the indexes, and a Qdrant collection's aliases are not in its snapshot. A refused recovery fails the row with the store's logs command and leaves the tier running.

A set made before a secret rotation restores the older value, so after that `apply`, run `gideon secrets rotate <name>` for every secret rotated since the set was made (the install runbook's rotation step, `v0.1.40`).

When Postgres does not answer and its files are what is damaged, stop the whole stack with `sudo docker compose -f /etc/gideon/rendered/compose.yaml down` and run `gideon restore --from staging --archive-end`. It replaces Postgres alone, from the newest complete local set whose backup the archive's latest timeline descends from, and replays the local archive to its end. This reaches the stop less at most 300 s (`archive_timeout`), where a bare restore of the same set stops at its 01:00 boundary.

Every file root, the content-addressed store, the live indexes, and the secrets on disk are left in place. The `verify:` row checks the pgBackRest repository alone, `files:` reads skipped, and the `Secrets:` line says the secrets on disk are the live box's, with nothing decrypted, since the replayed record carries every rotation up to the archive's end. A running cluster that answers refuses; use `--at` or the bare form while it does.

The `stores:` row and a `Reached:` line after `Secrets:` name the last replayed commit's instant, the WAL position, its segment, and the archive's newest segment. A replay short of that segment by N means the archive has a gap there, and the newest set's boundary, by the bare restore, is the fallback.

What was written after the reached instant has no row afterwards, so nothing that reads the record reaches it. An upload written after that instant must be uploaded again. A store object from that span must likewise be written again from its source, as for a left-out object above. A generation promoted in that tail goes back to the record's live generation, whose aliases the level step repoints, and `gideon index promote <generation>` is run again. A generation whose row was created in that tail is not in the record at all: `gideon index report` lists the record's generations, and a name a store holds beyond them is that generation's. Run its `gideon index build <collection> --lockfile <label>` again to converge on the same name and resume it, filling what the stores lack and removing nothing they hold.

## 5. Total-loss recovery (a rebuilt box)

This sequence rebuilds GIDEON on a replacement box from an off-box snapshot, and its order follows from what each step needs. The first apply comes before the restore because it writes the rendered files and fresh secrets, and the restore's one-off Postgres container runs from the rendered Compose file. The set's old secrets go back before the second apply, because the restored databases carry the old role passwords and those secrets must be in place before anything connects.

1. Install the supported Ubuntu Server release, then run `sudo python3 -m gideon host provision` to print a **new** backup keypair and a **new** age identity. Authorize the printed public key on the target. Do not replace the password manager's identity with the new one: step 4 brings the set's `backup_age_recipient` back, so every later set is sealed to the **old** office identity again and the new one opens nothing (the custody line below is the check).

   The rebuilt box also mints its own **new** box identity, which opens none of the earlier sets, since they open here with the office identity alone.
2. Write `site.yaml`, place the certificate, CA root, and supplied secrets, then run `gideon preflight`.
3. Run `gideon apply`, which brings up a fresh stack with freshly generated secrets.

   On the build box, run it from a checkout at the release tag named in the manifest of the set step 4 will restore, since it renders the Compose file the restore's one-off Postgres container runs from. This apply stops at `registry` with a failed row naming each built image (`docs/runbooks/built-images.md §3`), which is expected because those images exist only in the registry step 4 brings back. The rendered files and fresh secrets are already written, and no service or timer has started, so no nightly runs before step 4 and its `pre-restore` row reads `skipped (stack not running)`.
4. Run `gideon restore --from target`, with `--at` if needed, on the running fresh stack. Its `pre-restore` row reads the fresh-stack skip because a stack holding no set has nothing a safety set would protect. If the 01:00 nightly ran between steps 3 and 4, the stack holds a set and is not fresh, so its pre-restore push refuses against the office's earlier snapshots. In that case, stop the stack first with `sudo docker compose --project-directory /etc/gideon/rendered -f /etc/gideon/rendered/compose.yaml down`.

   Its `stores:` row puts the derived stores' indexes back from the set's snapshots, so they return with the restore rather than by a rebuild.

   On the build box, `fetch` probes the host registry for the Postgres image the rendered Compose file names. When that image is absent, it seeds `/data/registry` from the selected set's `data-registry` copy, verified against the set's inventory first, with nothing deleted, then restarts `gideon-registry` and probes again. Its row names the seed. A row failing after the seed names the release the set records; follow step 3's checkout at that tag, run `gideon apply`, then retry the restore. The `stop`, `files`, and `stores` stages then stop the registry, return its root whole, and start it, so step 6's apply passes `registry`.

   A set recording a release whose `restore` has no seed fails at `fetch` with the snapshot already in `/data/backup-staging.fetch-<label>`. Copy that directory's `sets/<set>/files/data-registry/` into `/data/registry` by hand, restart `gideon-registry`, run the restore again, and remove the side directory after recovery.

   A `pre-restore-<ts>` set an earlier release took records an `archive_through` before its own backup's stop, so a restore of it ends recovery before consistency. Restore it only after its manifest's `archive_through` is corrected by hand to an instant after that backup's stop, as `pgbackrest info` prints it, and before the set's boundary commit, with the original manifest kept beside it.
5. Decrypt the set's tarball over the fresh secrets with the **old** identity from the password manager, because the restored databases carry the old role passwords and the old secrets must be back before anything connects.
6. Run `gideon apply`, then any `gideon index build` and `gideon index promote` the restore's `Next:` line names, followed by `gideon backup run --full` and then `gideon backup drill`.

For the corpus's court coverage after these steps, follow `docs/runbooks/release-files.md §11`.

**The custody line.** After a provision, after a total-loss recovery, and whenever the password-manager entry changes, a CSA derives the public half of the password manager's identity on their own machine with `age-keygen -y <identity file>` and compares it with `/etc/gideon/backup_age_recipient` on the box. A mismatch means new sets are sealed to a key the office does not hold. This check is made by a person, and no command enforces it.

## 6. The pre-upgrade backup

`gideon upgrade <tag>` takes a full, labelled set before an upgrade so rollback has the checkout and data from before the change. Its label is `pre-<tag>`, or `pre-<tag>-<timestamp>` when an earlier attempt that did not cross the checkout left the plain label behind. The rollback command, `gideon upgrade --rollback [<tag>]`, restores that set with `restore --from staging --set <label>` and re-applies the previous release (`install-upgrade.md`).

The `--set` form addresses a set by its label and restores Postgres from that set's own backup to its own archive boundary. Since the set is a full one, it also restores the derived stores' indexes from the snapshots it took before the upgrade. Once the set is the newest one, no `--at` can express that restore; the form is refused with `--at` or `--from target`.

After a rollback, Postgres runs on a new timeline, so an `--at` earlier than the rollback's boundary follows the kept history and reaches no later than the rollback's target state. The `--set <label>` form restores the rolled-back branch to its own end and is also the by-hand rollback when `upgrade --rollback` finds no pre-upgrade set to work from.

As before a restore, run `sudo python3 -m tools.cistack down` first while the `gideon-ci` sibling stands. A labelled or pre-restore set does not expire the repository's retention on its own; only the nightly run does.

## 7. What a failure costs: recovery point and recovery time

**Recovery point** is the newest archive boundary, or the archive end, a failure's recovery reaches, stated as the worst-case committed work lost; **recovery time** is the time from the first recovery command to the first answered turn.

The figures below are what the design delivers today, each proven by a dated record or named unproven with its drill, and a tighter target needs a separate change. Detection is outside every recovery time, and no outside check yet pages when the box stops. Every record names its date, size, and release, since a figure is read against its size.

| Failure | Recovery point | Recovery time | Procedure | Proof |
|---|---|---|---|---|
| A database error while Postgres answers (a bad migration, a wrong delete) | The pre-restore set's boundary, taken after its own backup, bounds a point-in-time restore to any instant before it, reached from the earlier set's backup and the archived WAL. The work after the chosen instant is discarded by choice, and nothing else is lost. | 2,084 s (35 min): the pre-restore set 554 s, the restore's other stages 1,220 s, the apply 305 s, the turn 3 s | Follow §4: run `gideon restore --from staging --at <instant>`, then `gideon apply` and any `gideon index build` and `gideon index promote` its `Next:` line names. | A timed run on 2026-10-28 over a 62.3 GB set reached an instant past an earlier restore's promotion with no full backup between, at `v0.3.189`. |
| A damaged cluster that does not answer | The archive end is the stop less at most 300 s (`archive_timeout`), with 3 s measured. When the archive has a gap, the bare restore reaches the newest complete set's boundary. | 1,237 s (21 min): the restore 919 s (the repository's verify 880 s of them), the apply 313 s, the turn 4 s | Follow §4 with the stack stopped first: run `gideon restore --from staging --archive-end`, then `gideon apply` and `gideon backup run --full`. | A timed run on 2026-10-22 used a 69.6 GB set at `v0.3.183`. |
| A lost derived store's index (Qdrant, OpenSearch) | The newest full run's index snapshot is up to a week behind the record and is then levelled with it. | 1,685 s from the restore's start to the restored and levelled store, before the apply, over a 1,197,330-point scratch generation (2026-10-21); the time to a query answered through the alias is unproven. | Follow §4 for the `stores:` row's put-back and level step, then run the `Next:` line's `gideon index build` and `gideon index promote`. | The 2026-10-21 record over a 1,197,330-point scratch generation and a 69.5 GB set, at `v0.3.181`, proves the store's recovery. The query's seconds are unproven and are read at the first restore after the first promoted generation. |
| Lost content-addressed store objects | Staging's store copy supplies every object the newest complete local set's listing names. An object stored since is written again from its source (§4's listing command). | The put-back takes seconds per missing object; the whole store's put-back is unproven at today's size. | Follow §4 for the `files:` row's `data-bulk-cas` clause and the listing command. | This is unproven; the clean-VM full restore at the next minor release with the store in the set is its drill. |
| A lost box or `/data` volume (staging shares the volume) | The newest finished push is a day behind by design; two days have been observed while pushes overrun. | The rebuilt-box sequence of §5 is unproven at today's size, with the model weights' fetch and a replacement machine's arrival stated outside the figure. | Follow §5. | This is unproven; its drill is the clean-VM full restore at the next minor release. The 1,179 s run of 2026-09-17 at `v0.1.77`, over a 35.9 GB set predating the store, is the last record. |

For a damaged cluster, stop the whole stack first so the `pre-restore` stage skips, because a partially running stack refuses. The `--archive-end` form replays the archived WAL past the newest complete set's boundary to the archive's newest segment, so the recovery point is the stop less at most the 300 s `archive_timeout`. The bare restore stops at the set's boundary, up to a day behind for the 01:00 run.

The timed run on 2026-10-22 stopped the whole stack at 00:41:24 UTC and reached the last commit at 00:41:20, 3 s before it, with its `Reached:` line naming the archive end reached. It verifies the pgBackRest repository alone and puts back no file root, so it is faster than a bare stopped-stack restore of the same set (1,165 s and 1,193 s on 2026-10-21, before the apply).

After any restore, `--at` follows the kept timeline history and needs no full backup first. Run `gideon backup run --full` at once after every restore's apply to maintain retention and the incremental backup chain. With `--at` from staging, the command refuses at `select` before anything stops when no set finished by the instant can reach the latest timeline.

A pre-restore set this release takes records its boundary after its own backup and restores as any other set. A `pre-restore-<ts>` set an earlier release took is restored only after §5's hand correction of its `archive_through`, with the original manifest kept beside it.

For a lost box, the model weights' fetch and a replacement machine's arrival are outside the recovery time. Staging shares the lost volume, so only a finished off-box push supplies the recovery point: nightly pushes are a day apart by design, but overruns have left the newest finished push two days behind. A push that did not finish does not count, and a target restore's `select:` row names the snapshot it takes (§4).

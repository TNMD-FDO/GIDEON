# Coverage debt

Risky paths the unit suite cannot reach, with the seam or evidence that stands in. One line per gap: `path | why hard | escape plan`.

| path | why hard | escape plan |
|---|---|---|
| `gideon/host/backup.py`, `restore.py`, `drill.py` — the live semantics of `rsync --link-dest`, the SSH push to the NAS, and every `pgbackrest` invocation (backup, archive `check`, `verify`, `restore --delta`, point-in-time) | the suite fixes each command's argv over the `Host` seam; what the tools do with it is only observable on the box against the Synology and the built Postgres image | slice-0 ticket 06 Phase 4 transcripts (`.scratch/slice-0/assets/06-backup-first-pass.txt`, `06-restore-first-pass.txt`) and the nightly timer's first run; ticket 08's clean-VM restore repeats them on a fresh box |
| `tools/turns/cli.py` — the `--probe-inlet` precondition's branch for a profile without a generator pin | a lock the loader accepts needs the twelve-row memory table, so a fictitious lock expressing a generator-less profile is longer than the test it serves; the branch is three lines in `engine verify`'s shape | the malformed-lock and absent-profile variants in `tests/test_turns_browser.py` cover the loader and the selection paths; the branch reads as the `engine verify` precondition it mirrors (slice-1 ticket 43, `v0.1.39`) |

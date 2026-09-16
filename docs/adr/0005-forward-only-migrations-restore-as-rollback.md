# Forward-only migrations; rollback is a restore

Database migrations ship forward-only. `upgrade.sh` takes a mandatory backup before migrating, and `upgrade.sh --rollback` restores that backup and starts the previous release. Chosen over paired up/down migrations because down migrations rot when nobody exercises them, while the restore path is the one the install-time and recurring restore drills already prove; "tested rollback" becomes "tested restore." Consequence: the pre-upgrade backup is not optional and must complete before any schema change runs (greenfield-spec ticket 05, Aug 2026).

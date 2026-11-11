# Start here

This card is arranged by situation. Each item gives a command, when there is one, and the runbook section with its steps. Provision brings the box's host layer to its target state and installs the `gideon` command. Every command runs as root from any directory afterward: `gideon status` is an example. `sudo` asks for the password at most once a sitting, with the office's policy determining how often. Before provision's `gideon-command` step, run `sudo python3 -m gideon status` from `/opt/gideon` instead. An item says when its result goes to the developer.

## 1. Every day

- Nothing needs starting unless an email arrives. The timers handle nightly backup and push, Saturday drill, quarterly off-box re-hash, reconcile, nightly eval, the nightly retention sweep, and Monday's upstream watch; `systemctl list-timers 'gideon-*'` shows them. [docs/runbooks/backup-restore.md §1](backup-restore.md)

## 2. An email arrived

- When a page with subject `[GIDEON <office>] FIRING: <rule>` arrives, run `gideon status`, then follow the rule's row. [docs/runbooks/observability.md §4](observability.md)
- An `Upstream watch notice` page means a cut is due. Run `gideon proposals`; its upstream section names the source. [docs/runbooks/release-files.md §10](release-files.md)
- Before a planned restore, NAS window, or driver move, silence the affected rule. [docs/runbooks/observability.md §5](observability.md)
- For Monday's `Proposals waiting` nudge, the maintaining office runs `gideon proposals` and brings any fired row to the developer for a ruling. [docs/runbooks/observability.md §10](observability.md); [docs/runbooks/release-files.md §7](release-files.md)

## 3. Once a week

- Saturday's email with subject `[GIDEON <office>] Channel heartbeat` should arrive and asks nothing. If it does not, run `gideon status` first (exit 2 means Grafana could not be checked), then `gideon alerts test`. [docs/runbooks/observability.md §4](observability.md)
- On Mondays, the maintaining office sorts open pin-watch and Dependabot PRs on GitHub by the table, merges routine kinds when green, and leaves user-facing or proposal kinds for a release. [docs/runbooks/release-files.md §9](release-files.md)
- The maintaining office rebuilds a built-pin proposal on the box before merging. [docs/runbooks/built-images.md §2](built-images.md)
- A frontend or model bump goes to the developer for its proof and release decision. [docs/runbooks/model-upgrade.md §2](model-upgrade.md)

## 4. Once a month

- First, run `gideon eval candidates --out /root/candidates/<YYYY-MM>`; read the page with `less` on the box and write candidates in your own words. [docs/runbooks/feedback-packet.md §1](feedback-packet.md)
- Next, run `gideon proposals`; read its guardrail section, inspect each report in the same packet, and bring reports to the developer. [docs/runbooks/guardrail.md §3](guardrail.md)
- Last, delete that month's packet after both reviews. [docs/runbooks/feedback-packet.md §8](feedback-packet.md)

## 5. Upgrade day

- Before the window, read the release note, announce the window, and inform the supervisor. [docs/runbooks/install-upgrade.md §2](install-upgrade.md)
- In the window, run `gideon upgrade <tag>`. [docs/runbooks/install-upgrade.md §2](install-upgrade.md)
- If the new release is red or wrong, run `gideon upgrade --rollback`. [docs/runbooks/install-upgrade.md §3](install-upgrade.md)
- If a command refuses, follow its printed fix and the re-run guidance. [docs/runbooks/install-upgrade.md §4](install-upgrade.md)

## 6. Something looks wrong

- Start with `gideon status` and follow the reported rule. [docs/runbooks/observability.md §4](observability.md)
- To see what the corpus holds, run `gideon index report`. To read a generation still building, name its id with `gideon index report <generation>`. [docs/runbooks/install-upgrade.md §1](install-upgrade.md)
- To serve a built generation, run `gideon index promote <generation>`, then `gideon backup run --full`. A week later, `gideon index gc` drops the retired generation from both stores. For a generation not yet promoted, run `gideon index retire <generation>`. The next `gideon index gc` drops its stores. [docs/runbooks/install-upgrade.md §1](install-upgrade.md)
- After a site-file edit, run `gideon apply`, which brings the running stack in line with the site file; after an `alerts.*` change, also run `gideon alerts test`. For a renewed certificate, run `gideon tls reload`. [docs/runbooks/install-upgrade.md §4](install-upgrade.md); [docs/runbooks/office-services-setup.md §4](office-services-setup.md)
- For backups by hand, run `gideon backup run`, `gideon backup push`, or `gideon backup drill`; read their rows. [docs/runbooks/backup-restore.md §3](backup-restore.md)
- To go back in time, run `gideon restore --from staging` or `gideon restore --from target`; for a cluster that does not answer, run `gideon restore --from staging --archive-end`. [docs/runbooks/backup-restore.md §4](backup-restore.md) gives the steps; [docs/runbooks/backup-restore.md §5](backup-restore.md) covers a rebuilt box.
- The table gives a failure's cost in work lost and time to an answered turn. [docs/runbooks/backup-restore.md §7](backup-restore.md)
- After an engine or driver change, run `gideon engine verify`. [docs/runbooks/model-upgrade.md §7](model-upgrade.md)
- When `gideon upgrade <tag>` or `gideon preflight` refuses on `hardware-profile` naming GPU count or DRAM, the box is under its profile's floor. The same holds when a model server pages and `nvidia-smi -L` no longer lists its card. [docs/runbooks/install-upgrade.md §12](install-upgrade.md)
- When someone joins or leaves, change the directory group, then run `gideon users reconcile --now`. [docs/runbooks/office-services-setup.md §1](office-services-setup.md)
- Any command's refusal prints its fix; follow the refusal for that command. [docs/runbooks/install-upgrade.md §4](install-upgrade.md); [docs/runbooks/feedback-packet.md §9](feedback-packet.md)

For everything else, run `gideon --help` and use the runbooks beside this card. To learn what one command does, read the [command reference](../commands.md).

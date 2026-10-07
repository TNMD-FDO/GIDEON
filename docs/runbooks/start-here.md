# Start here (CSA runbook material)

This is one page by situation: each item gives a command, when there is one, and the runbook section with its steps. Every command runs as root from any directory after provision installs it: `gideon status` is an example. `sudo` asks for the password at most once a sitting; how often is the office's policy. Before provision's `gideon-command` step, run `sudo python3 -m gideon status` from `/opt/gideon` instead. An item says when its result goes to the developer.

## 1. Every day

- Nothing to start unless an email arrives. The timers handle nightly backup and push, Saturday drill, quarterly off-box re-hash, reconcile, nightly eval, and Monday's upstream watch; `systemctl list-timers 'gideon-*'` shows them. [docs/runbooks/backup-restore.md §1](backup-restore.md)

## 2. An email arrived

- A page with subject `[GIDEON <office>] FIRING: <rule>`: run `gideon status`, then follow the rule's row. [docs/runbooks/observability.md §4](observability.md)
- An `Upstream watch notice` page: a cut is due. Run `gideon proposals`; its upstream section names the source. [docs/runbooks/release-files.md §10](release-files.md)
- Planned restore, NAS window, or driver move: silence the affected rule before work begins. [docs/runbooks/observability.md §5](observability.md)
- Monday's `Proposals waiting` nudge: run `gideon proposals`; bring any fired row to the developer for a ruling. [docs/runbooks/observability.md §10](observability.md); [docs/runbooks/release-files.md §7](release-files.md)

## 3. Once a week

- Saturday: the heartbeat email should arrive. If it does not, open Grafana and run `gideon alerts test`. [docs/runbooks/observability.md §4](observability.md); [docs/runbooks/observability.md §2](observability.md)
- Monday, maintaining office only: sort open pin-watch and Dependabot PRs on GitHub by the table; merge routine kinds when green and leave user-facing or proposal kinds for a release. [docs/runbooks/release-files.md §9](release-files.md)
- A built-pin proposal, maintaining office only: rebuild on the box before merging. [docs/runbooks/built-images.md §2](built-images.md)
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
- After a site-file edit, run `gideon apply`; after an `alerts.*` change, also run `gideon alerts test`. For a renewed certificate, run `gideon tls reload`. [docs/runbooks/install-upgrade.md §4](install-upgrade.md); [docs/runbooks/office-services-setup.md §4](office-services-setup.md)
- For backups by hand, run `gideon backup run`, `gideon backup push`, or `gideon backup drill`; read their rows. [docs/runbooks/backup-restore.md §3](backup-restore.md)
- To go back in time, run `gideon restore --from staging` or `gideon restore --from target`. [docs/runbooks/backup-restore.md §4](backup-restore.md); for a rebuilt box, [docs/runbooks/backup-restore.md §5](backup-restore.md)
- After an engine or driver change, run `gideon engine verify`. [docs/runbooks/model-upgrade.md §7](model-upgrade.md)
- When someone joins or leaves, change the directory group, then run `gideon users reconcile --now`. [docs/runbooks/office-services-setup.md §1](office-services-setup.md)
- Any command's refusal prints its fix; follow the refusal for that command. [docs/runbooks/install-upgrade.md §4](install-upgrade.md); [docs/runbooks/feedback-packet.md §9](feedback-packet.md)

For everything else, run `gideon --help` and use the runbooks beside this card.

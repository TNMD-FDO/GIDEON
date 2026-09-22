---
commands: []
modules: []
---

# Security postures — cross-cutting leaf

**Read this when** a ticket adds or changes a mechanism of a kind the map's §16 names: how a secret reaches its consumer, what a child's argv carries, the base model's withholding, a role's, key's, or token's grants, the observability tier's doors, evidence and the export, the backup identities, a network path. §16 keeps the rules that bind all code — the standing and slice-time postures and each posture's rule in a clause; this leaf holds the mechanism behind each, as §16 held it, owns no surface and no command, and points at the subsystem leaf that owns a mechanism's code. The map is [`../ARCHI.md`](../ARCHI.md).

## Key material

- **Key material never enters Python**: every key and password reaches its consumer as a path (`*_FILE`), a Compose secret mount, a 0600 env file, a file read inside the consumer's own container, or stdin; a generated secret is minted absent-only into a 0440 file, or rotated in place by `secrets rotate`; none appears in argv, logs, refusals, or reprs. `gideon-api` reads the generated `gideon_api_key` and the engine key from Compose-mounted files and carries the audit role's password as a third mount its trip writer opens at write time, while the engine key's homes are that mount and the engine's own — it left the frontend at general-turn ticket 09, whose 0600 env file now carries `gideon_api_key`, the frontend's bearer to the service, instead. The mechanism per secret is [`render-apply.md`](render-apply.md)'s (secrets, the rendered tree), [`engine-frontend.md`](engine-frontend.md)'s (`engine verify`), and [`tools.md`](tools.md)'s (the turn harness, whose `--out` records are never committed and whose browser test account the product never knows); an on-box proof's is `docs/agents/on-box-proofs.md`'s.

## Argv

- **Nothing sensitive in argv**: proxy credentials travel in a child's environment, or in the temporary 0600 WGETRC file preflight's probe and the pull share, never argv; every SQL statement reaches Postgres on stdin over the container's socket (the password discipline is the map's §14's); the LDAP bind password is passed by file and group DNs are RFC 4515-escaped; the runner's registration token crosses into `config.sh`'s environment only and is consumed; a build argument named like a secret is refused by the lock loader.

## The base model's withholding

- **The hidden base model is withheld by the branch gate's inlet, not by the selector**: the hidden flag is client-side alone — the API lists the base model to any signed-in user (its public-read grant is what makes General usable), so the chat page's `?model=` parameter can still name it — and the branch gate, the one GIDEON Function left in the frontend and its only global inlet since the cutover, refuses a `user`-role turn on any model that is not a preset before anything is called, admins and the eval identity passing ([`engine-frontend.md`](engine-frontend.md); slice-1 ticket 43, general-turn tickets 04 and 09); the one residual is the page's title task after a refused first message.

## Least privilege

- **Least privilege**: the audit writer role holds INSERT and a partition function on its two tables only, `gideon-api` its one consumer since general-turn ticket 09 took the password off the frontend's mounts (ADR-0039 superseded by ADR-0045), for the guardrail trip writer; `gideon_eval` holds the same on its own two tables, insert-only, the metrics role its reader ([`eval.md`](eval.md)); the frontend's endpoint restrictions bind the admin key too, so the release's allowlist is exactly what apply, reconcile, and the eval identity call; the push sends no ownership to the plain NAS account; the pin watch's App token is minted per run, hour-scoped, and never in a row; the CI runner's one sudoers line names one module of the checkout it runs from; the tag-triggered run's trust is who may create a `v*` tag — the collaborator list where the run happens (slice-1 ticket 57, ADR-0031).

## Observability

- **Observability** (ADR-0034): the metrics reader role holds `SELECT` on the four kept-event tables, each granted by the migration that creates it, and `pg_monitor` only (that membership by the superuser on every converge, never by a migration); Grafana's door is one LDAP mapping (the admins group) plus the break-glass account, no anonymous access, no sign-up; every exporter is unpublished, the engine's own `/metrics` among them; the node exporter alone runs outside Docker's AppArmor profile and mounts everything read-only; `alerts test` sends nothing unless the stored receiver matches the site file.

## Evidence and the export

- **Evidence**: no secret or office value in the tree — the acceptance harness and on-box `python3 -m tools.redact` redact at capture, including print-once secrets and marked site leaves ([`tools.md`](tools.md)); `tests/test_evidence_hygiene.py` holds `.scratch/` to secrets and `tests/test_office_values.py` the tree to documentation values and site-key placeholders, naming none. The public repository, a **filtered export** of each tag by `bin/release-export`, carries the application alone: `tools/exportboundary.py`'s list of development files is held by `tests/test_export_boundary.py`, and a test reading an excluded path skips there by that module's predicates, never by the absence of `.git` ([`tests.md`](tests.md); slice-1 tickets 56, 57, and 79).

## Backups

- **Backups**: the office identity is never written to the box; the box's own identity is root-only host state excluded from every set and opens only the box's own sets there (ADR-0042); the tarball never exists as plaintext ([`backup-restore.md`](backup-restore.md)); every secret-flagged rendered file is excluded from every set, so a secret rides off-box only inside the tarball; a restore re-owns only paths a non-following `find` walk has matched to the manifest, so a corrupt fetched tree cannot steer `chown` outside it.

## Network

- **Network**: search egress — the one runtime path user-authored text leaves the box — goes from SearXNG (its default engines a release value) and the frontend's page loader (a rendered agent and bound, [`render-apply.md`](render-apply.md)) directly or through `egress_proxy`, only while `web.search` is on and only after the user's confirmed toggle (§15); SearXNG publishes no port and writes no request line, and no search-query text reaches a log — the log levels are rendered facts, the two recorded residuals the frontend's own traceback on a non-2xx from SearXNG and its page loader's line naming a failed fetch's address, and the search-sentinel contract holds it ([`tests.md`](tests.md)); a prompt in the chat page's URL reaches no log the box keeps — the ingress's line redacts the prompt-bearing parameters and the frontend's access line is off ([`stack.md`](stack.md); slice-1 ticket 69); the page's own image loads are held to the box by the frontend's rendered image policy, so a user's browser never sends a source's link to a third-party icon service ([`render-apply.md`](render-apply.md); slice-1 ticket 72). Docker-published ports honour `lan_cidrs` through the firewall step's `DOCKER-USER` block, which drops by original destination port only for packets forwarded *into* a Docker bridge; the self-hosted runner never executes pull-request code; the box never phones home for versions — the frontend's update check is rendered off and CI-checked, and the GitHub runner is registered with `--disableupdate`.

## Cross-references

- Cites: [`render-apply.md`](render-apply.md), [`engine-frontend.md`](engine-frontend.md), [`tools.md`](tools.md), [`eval.md`](eval.md), [`tests.md`](tests.md), [`backup-restore.md`](backup-restore.md), [`stack.md`](stack.md)
- Cited by: the map's §§2 and 16

# On-box proofs: how a proof is run on the box

**Read this when** an on-box proof is about to run its first command, and again at each trigger: a long child on the box — a Codex run, a browser run (§1); a command that needs a secret (§2); a Filter ticket's turns, a users-seat proof, or any sign-in as the eval identity (§3); a hand-run tool that might prompt (§4); a criterion that is a harness stage (§5); an apply of any kind, before it runs (§6); the release's apply after a worktree's (§7); a memory-limit proof, or any figure a restart resets (§8); a criterion about an outcome the frontend reports as a status line (§9); a rendered file a service parses at its start (§10); a third party's answer to the product's client, or a premise about the pinned loader (§11). Each section is one rule with its command shape; its `_Origin_` line names the tickets and tags whose records hold the incidents.

## 1. A long child after heavy on-box work runs detached

**Every Codex run on the box, and every browser run, starts detached and is waited on in the foreground in bounded intervals.** Every byte a child of the session streams (a `registry mirror`, the weights stage's hash pass, a `docker compose pull`) is charged as file cache to the session's cgroup, after which the harness's memory guard kills every background task it tracks while the host shows no pressure; `memory.reclaim` does not stop it, and it has fired with the cgroup at 0.5 GB and on a cycle that touched nothing on the box. The documented switch, `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1`, is in `.claude/settings.json`'s `env` block, read at the next launch; a kill after it means the switch does not cover the mid-turn path (workflow ticket 26). So detached is the default for `start.sh`, `resume.sh`, the synthesis, and every `codex-implement` batch:

```bash
export S=<scratchpad> P=<plan-path> G="<gate summary>"
nohup setsid bash -c 'bash .claude/skills/codex-code-review/scripts/start.sh --prompt-file .claude/skills/codex-code-review/prompts/start.tpl "$P" "$G"; printf "\nREVIEW_EXIT=%s\n" "$?"' > $S/codex-review.log 2>&1 < /dev/null & disown
timeout 560 bash -c 'until grep -q "REVIEW_EXIT=" "$S/codex-review.log"; do sleep 20; done'; tail -40 $S/codex-review.log
```

The exit line is printed after a newline of its own, since Codex's report ends without one. The exports are their own line, since an `&` ending an `&&` chain backgrounds the whole list. The start is never chained after an edit of the reviewed file without `&&`, or a failed edit leaves Codex reviewing the old text. Inside a cycle's worktree the guard refuses the `bash -c '…'` string (`docs/agents/worktrees.md` §2), so the start line goes into a fixed script in the scratchpad reading its arguments from files beside it, run as `nohup setsid bash <path> > <log> 2>&1 < /dev/null & disown` with the wait unchanged. Where a foreground `sleep` is blocked, the wait is a `Monitor` until the log carries the exit line or the process is gone, its `pgrep` pattern bracketing one character (`codex-review-start[.]sh`) so it never matches itself. A killed `start.sh` may leave a thread: `reset.sh <plan-path>` before starting again. A run is killed by pid, since `pkill -f` on the script's name matches the session's own shell too.

**A browser run of the turn harness is the same case**: a kill mid-turn leaves a chat undeleted and root-owned files in `--out`, so start it detached, wait with a `Monitor` ending on the tool's exit line, and before the next run list the account's chats and clear the old `--out` with `sudo`.

_Origin_: `v0.1.6`, `v0.1.12`, `v0.1.18`, `v0.1.21`; `v0.1.27`, `v0.1.30`, `v0.1.32` (the default, the worktree's script, the `Monitor` wait); `v0.1.48`, `v0.1.49`.

## 2. A secret reaches a command by file or stdin, never by argv

**An on-box proof never expands a secret into a command line.** `sudo` journals every command it runs with its arguments expanded, and any process can read another's argv, so `grep -c "$(sudo cat /etc/gideon/secrets/<name>)"`, `docker compose exec -e KEY="$(…)"`, a `curl -H "Authorization: Bearer $KEY"` inside `sh -c`, or a password in `-u` inside a `sudo bash -c` string leaks the value even when the transcript shows the substitution unexpanded, and a leaked value is rotated. Pass a secret by file or stdin only: `sudo grep -c -F -f /etc/gideon/secrets/<name>` for an absence check; a curl config on stdin (`curl -K -`) built by `sudo sh -c 'tr -d "\n" < /etc/gideon/secrets/<name>'` in a pipeline for a keyed request; `sudo cat <file> | …` when a child must read it. A password-shaped request is the same shape: a root-run Python or shell builds the whole curl configuration — `user = "<account>:<password>"`, a `data = ` line with the JSON body, `cacert`, `resolve` — and hands it to `curl -K -` on stdin, printing the status alone. The transcript's hygiene test catches a printed value, never a logged argv.

_Origin_: `v0.1.6`; `v0.1.25`.

## 3. A Filter ticket proves its turns with the turn harness; a users seat with its browser mode

**A Filter ticket proves its turns with the turn harness, not by hand.** `sudo python3 -m tools.turns <cases> [--repeat N] [--stream] --out <dir>` from the checkout on the box runs the cases as the eval identity, one row per case, exit 0 iff every expectation held (the tool, its rows, and its reading of the seed: `docs/archi/tools.md`). A handful of turns runs at any hour; the whole seed only in the quiet window (19:00–06:00 office time) or on a weekend, which the tool's guard enforces (`--force` for a deliberate daytime run, said in the transcript). The stdout rows are the committed transcript (`.scratch/<slice>/assets/<NN>-on-box.txt`: ids, classes, counts, seconds — never a prompt, an answer, or a secret); `--out` holds the stored messages and is never committed and never under the repository. Read the `preconditions` row's leftover count before a run: a chat the identity already holds is a person's to delete, never the tool's. A `--stream` row's `stream: leak@…` field fails the row (`STREAM_LEAK_FAILS`): a leak is a guardrail gap. A code review round that changes the Filter restarts a seed run in progress, so the long run waits for `APPROVED`. A browser case's `must` phrase names a word the doctrine always carries, never a statutory figure the model may recall wrong. The password is read by the tool from its file and the session token lives in memory; nothing of either reaches a command line (§2).

**The eval identity is one account**, but since slice-1 ticket 68 two runs signed in as it at once — the tool's, `engine verify`'s — each identify their own turn's chat by the two ids they minted and delete that chat alone, so an overlap costs nothing in correctness and a leftover count at the second run's sign-in may simply be the first run's chat in flight, a person's to judge and never the tool's to delete. The journal read stands for the **measurement**: two runs share the engine, so their elapsed seconds and turn figures inflate, and the guard's daytime handful is counted per run. A `--concurrent N` run is N sessions of the identity at once by design, the guard counting its engine calls N times, and the screen under that load is a one-case browser run started beside it; the frontend allows one account 15 sign-in attempts in a rolling three-to-four-minute window, refused ones counted, so N stays at 15 or below and back-to-back runs wait out the window, `engine verify` sharing it (slice-1 ticket 62). A session about to sign in as it first reads the journal for another session's run, and a tracker session's measurement waits for a cycle's proof to finish:

```bash
sudo journalctl --since -15min -o short-iso | grep -E 'engine verify|tools.turns'   # the sudo lines name the command and its checkout
```

**A users-seat proof runs the browser mode**: `sudo .venv/bin/python -m tools.turns --browser <cases> --out <dir> [--probe-inlet]` from the checkout — the venv's interpreter, where the mode's Playwright lives — signs in as the users-group test account `gideon-test-user` and judges every painted frame (the row's `live` field, a flash failing under the same `STREAM_LEAK_FAILS`); `--probe-inlet` adds the inlet-gate rows from the same seat. A browser turn counts three engine calls against the guard (the page's title and tag tasks follow the answer), so two browser cases are the daytime handful. The mode's rows, footprint, and removal are `docs/archi/tools.md`'s. `--out` holds the screenshots and `requests.log`; the transcript describes each in plain words and commits none. A browser run is a long child: §1's detached shape.

_Origin_: `v0.1.11`, `v0.1.12`, `v0.1.18`, `v0.1.21`, `v0.1.39`, `v0.1.55`; the one-account rule at slice-1 ticket 59's triage, narrowed to the measurement by slice-1 ticket 68.

## 4. A hand-run child that might prompt is bounded

**A hand-run child that might prompt gets no stdin, a timeout, and a capped output.** The seam gives every child `/dev/null` as stdin, so a tool under `Host.run` cannot wait on a prompt; a command run by hand in a pipeline can, and one prompting on an open non-terminal stdin loops forever, flooding the session's output. Run such a command as `timeout <seconds> <command> < /dev/null 2>&1 | head -c <bytes>`, and take a tool's exit codes from the box rather than from memory (`certutil -L -n` answers 255 for an absent nickname, not 1).

_Origin_: `v0.1.12`.

## 5. A harness-stage criterion is proven over a temporary ref of the staged index

**A criterion that is a harness stage is proven on the acceptance VM over a temporary ref of the staged index, never by committing early**, so the branch gains no commit and the release's single commit stands. `git write-tree` reads the index, so the work is staged first; an unstaged or untracked file is absent from the proof. The harness resolves any ref the checkout knows (ADR-0035; the tool: `docs/archi/tools.md`); `--until` names a stage, the VM is torn down whatever the stage, and `--out` is a new directory under the session's scratchpad, never the checkout. The rows and the stage transcript go into `.scratch/<slice>/assets/<NN>-on-box.txt` under a header naming the command, the temporary commit's full sha, and that the ref was deleted (slice-1 ticket 02's proof c is the shape). The recipe fails closed when pasted whole: a failed `git add` or a stale `tmp/` ref is never proven, the harness's status is printed since it prints none of its own, and the ref is deleted whether the run passed or not, the last line exiting 1 when the setup, the deletion, or the leftover-ref check failed. A run takes about six minutes on the box (ADR-0035), inside the Bash tool's ten-minute cap; one that could exceed it goes detached with §1's shape.

```bash
git add -A && git diff --cached --stat && tree=$(git write-tree) && proof_sha=$(git commit-tree "$tree" -p HEAD -m "ticket <NN> staged proof") && printf 'proof commit: %s\n' "$proof_sha"
git update-ref "refs/heads/tmp/<ticket>-proof" "$proof_sha" && printf 'proof ref: ' && git rev-parse --verify "tmp/<ticket>-proof"
[ -n "$proof_sha" ] && [ "$(git rev-parse --verify --quiet "tmp/<ticket>-proof")" = "$proof_sha" ] && sudo python3 -m tools.acceptance tmp/<ticket>-proof --until <stage> --out <scratchpad>/acceptance-<NN>; harness_status=$?; printf 'harness exit: %s\n' "$harness_status"
git update-ref -d "refs/heads/tmp/<ticket>-proof" && [ -z "$(git branch --list 'tmp/*')" ] && printf 'tmp refs left: none\n' && exit "$harness_status"; exit 1
```

_Origin_: `v0.1.19`.

## 6. The recreate set is the row before an apply; with users on the box it runs only in a window

**Before an apply — `apply` itself, the one a `restore` prints as its next step, or the one inside `install`, `upgrade`, and `upgrade --rollback` — the services it will recreate are read from `render --diff` and compared with the plan's Window bullet: a service users see that the bullet did not name is a stop — back to the plan, never an apply that sees what happens — and from `v0.2.0` an apply that recreates one runs only inside a window announced under the box ledger's §8, at the announced hour, whatever the cycle, a hotfix included.** The set is the command's one row, `Services apply would recreate: …`, judged against `applied.yaml` (the last apply that verified, never the disk), naming the owners of every changed rendered file and every service whose `compose.yaml` block or top level moved; apply's `recreate` and `start` rows name the same set by reason, so no hunk is mapped by hand (`docs/archi/render-apply.md`). Compose's own config diff at `up -d` is the one mechanism the row does not model; its read-only cross-check is `docker compose config --hash '*'` over the rendered file against the `com.docker.compose.config-hash` label of every running container. Before `v0.2.0` the engine's recreate is the ledger's announcement alone; after it, a proof that cannot wait for a window is the maintainer's call, never the session's. A hotfix writes no plan, so its Window answer — the recreate set and the window it took — goes in its changelog entry.

```bash
sudo python3 -m gideon render --diff          # the hunks per file, then the one row: "Services apply would recreate: …"
```

_Origin_: workflow ticket 07, the docs commit after `v0.1.25`; slice-1 ticket 64 (`v0.1.30`, the row's owners and moved blocks).

## 7. A proof's apply runs from the cycle's worktree; the release applies from the primary before removing it

**A cycle proves its apply from its own worktree, and the release applies again from the primary after the fast-forward and before `git worktree remove`.** `apply` takes the checkout from its own module's location, so the rendered systemd units carry `WorkingDirectory=` the tree that ran apply — the only trace a tree leaves on the box — and a removed worktree leaves the units failing at their next hour. So at the release, once the primary's `main` holds the cycle: read the units' line; `render --diff` from the primary shows the units' hunks alone and an empty recreate row, so §6's window question answers itself; `apply`; then the removal. `upgrade` and the apply that goes live run from the primary alone (`docs/agents/worktrees.md` §6). The release's apply runs no `engine verify` — that child is `upgrade`'s alone — so a proof bullet that leans on the upgrade's verify is met after the release's apply by `sudo python3 -m gideon engine verify` from the primary, detached as §1 asks and after §3's journal read, its counts a last section of the transcript carried by the tracker commit.

Root Python run from a worktree — a driver, `render --diff` — leaves root-owned `__pycache__` directories that make `git worktree remove` fail with "Permission denied" while git prunes the registration: run it with `-B`, or hand the caches back before the merge as `tools/ownership.py` does; `sudo` removes a leftover directory only once every root-owned entry is shown to be a cache, since a tracked file left behind is a missed commit. Two cycles in flight share one box: the second's `render --diff` reads the first's worktree apply as the applied record, so the second's proof waits for the first's release and rebases onto it before its apply; and since the apply manifest and the systemd units have no container owner, an empty row is not the whole test — any converge, `apply`'s or `secrets rotate`'s, rewrites both and undoes the first cycle's Function or `WorkingDirectory`, so the wait is for any apply from the first's worktree, read in the journal and the units' line.

```bash
grep -h WorkingDirectory /etc/gideon/rendered/systemd/*.service | sort -u   # names the worktree → the release applies from the primary
sudo python3 -m gideon render --diff          # the units' hunks alone; "Services apply would recreate:" empty
sudo python3 -m gideon apply                  # from the primary; the units return to it
```

_Origin_: workflow ticket 09, the docs commit after `v0.1.25`; `v0.1.30`, `v0.1.35`, `v0.1.40`, `v0.1.45`.

## 8. A memory-limit proof evicts per file and reads what a restart cannot replace

**A container's memory limit is exercised only by the pages the container is first to touch, so a proof of one starts the service from a cold page cache and reads its verdict from facts a restart cannot reset.** On cgroup v2 a file page is charged to the cgroup that first faults it in and stays there until reclaimed: after apply's `models` stage has read every weight file from the host's side, a restarted engine finds its pages cached and charged elsewhere, and its limit is never tested. The proof stops the service, evicts its files one by one — `posix_fadvise(DONTNEED)` per regular file under `/data/models/hub` from a fixed Python script run as `sudo python3 -B <path>`, `fincore` summing the resident bytes before and after — and never `drop_caches`, because the page cache is shared with Transcribe's containers; then starts the *same* container (`docker compose start`, not `up`, so the id is comparable), polls the healthcheck, and reads two sets of facts. From `docker inspect`, before and after: the container id, `RestartCount`, and `State.OOMKilled` — a `restart: unless-stopped` container that is OOM-killed comes back in a fresh cgroup whose counters read clean, so these three are the verdict and the cgroup's figures the mechanism. From `/sys/fs/cgroup/system.slice/docker-<id>.scope`: `memory.max`, `memory.peak`, `memory.stat`'s `anon` and `file`, and `memory.events`' `max` and `oom_kill`. `docker inspect` reports the requested limit, the kernel floors it to whole 4 KiB pages in `memory.max`, and cAdvisor's `container_spec_memory_limit_bytes` reports the kernel's figure, so a comparison with the Prometheus series uses the page-floored row. A service that exists only during its own run (the drill's) is read from cAdvisor's series over the run's window, `max_over_time` for the project.

_Origin_: slice-1 ticket 19 (`v0.1.37`, the proof's figures); the restart-masked OOM from that plan's Codex review.

## 9. A criterion about an outcome is proven on the record the outcome changes

**A criterion about an outcome is proven on the record the outcome changes, never on a status line.** A frontend status line proves a step ran, not that its result reached anything ("Searched 7 sites" is emitted before the gate that decides whether the pages enter the prompt), and a record read back after a sync proves the sync, not the turn (a stale per-worker model cache kept storing unstamped answers beside a stamped record). So "in context" is proven by the stored assistant message's `sources` — each carrying document text, read through the chat's own API — or by the engine's prompt tokens; "stamped" by the stored content; "refused" by the stored record's class. The harness's `must` and `sources` checks and a driver's read-back are measures; a status line and a sync's read-back are steps. The engine's figures are attributed, not assumed: the generator is shared, so a measurement reads its request count and prompt-token counter through Prometheus on loopback before and after the turn, requires the engine idle before (no request running or waiting, the counters unchanged across a scrape interval), waits after until the request delta reaches the turn's own count (a browser turn's answer, title, and tags, plus one search task when searched), and repeats a run whose delta differs. The transcript records the counts and the deltas, never a source's host or text. A driver kept in the scratchpad rather than the checkout runs with `PYTHONPATH=<checkout>` set in its wrapping script, since Python heads `sys.path` with the script's own directory.

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=sum(vllm:request_prompt_tokens_count)'   # before and after, with sum(vllm:prompt_tokens_total)
sudo .venv/bin/python -B .scratch/slice-1/assets/65-search-turn.py <scratchpad>/out-65 [--no-search]   # the driver: sources, citation elements, both deltas, cleanup in a finally
```

_Origin_: slice-1 ticket 65 (ticket 15's proof read against the stored record, 2026-09-08; ticket 14's attachment, `v0.1.27`; the driver's path, `v0.1.40`).

## 10. A rendered file a service parses at start is adapted through the pinned image before the apply

**A rendered file that a service must parse when it starts — the Caddyfile, Prometheus's configuration, a Grafana provisioning file — is adapted or checked through the pinned image before the apply that recreates the service**, because a grammar error surfaces only at the container's start, the service down until the template is fixed and applied again. For Caddy the check is `caddy adapt` over the example fixture's file (the same block with the documentation hostname) through the mirrored image mounted read-only, its adapted JSON grepped for the keys the change introduced, counts only; `caddy validate` would also load the certificate files. Prometheus's is `promtool check config` through its image the same way. A warning the adapter prints about formatting is a pre-existing fact of the template, recorded, not a finding.

```bash
sudo docker run --rm -v <checkout>/tests/fixtures/render/example/caddy/Caddyfile:/etc/caddy/Caddyfile:ro <the caddy image reference in /etc/gideon/rendered/compose.yaml> caddy adapt --config /etc/caddy/Caddyfile | grep -o -E '"format":"[a-z]+"|"filter":"[a-z]+"|"replace"' | sort | uniq -c
```

_Origin_: slice-1 ticket 69 (`v0.1.42`, the `filter` encoder's block adapted before the ingress was recreated).

## 11. A third party's behaviour is measured with the real client from inside the container; a live-network proof gates on what the change asserts and records the rest

**A fact about how a site answers the product's client — an agent string, a bound, a refusal — is measured from the box with the pinned client class itself, run inside the service's container, never inferred from a reading of the loader or from a standalone probe alone.** A reading of the pinned loader's source said it aborted the whole load on one page's exception; the class as the factory builds it (`SafeWebBaseLoader` with `continue_on_failure`) skipped that page as an empty document, the others intact. The script is `docker cp`'d in and run with `docker exec`, `PYTHONPATH=/app/backend`, and a placeholder session key that is no secret, to pass the image's import guard, whose migration check against the live database the transcript records as current. A standalone probe answers how a site responds by agent; the class answers what the product does with that response. **A live-network proof separates gates from records**: the gates are the behaviours the change asserts (a site's status by agent, the enabled engines, a value read back from the container, `render --diff`'s row); everything else is recorded with no expectation of matching an earlier run. A transient gets one bounded retry with both runs in the transcript; one that survives the retry is a fact with a name, and a varied shape (several agents over several sites) settles what a further replay cannot.

```bash
sudo docker cp <scratchpad>/<probe>.py gideon-open-webui-1:/tmp/<probe>.py
sudo docker exec -e PYTHONPATH=/app/backend -e WEBUI_SECRET_KEY=probe-import-guard-placeholder -w /app/backend gideon-open-webui-1 python /tmp/<probe>.py <list> <bound>   # the pinned loader class as the factory builds it
```

_Origin_: slice-1 ticket 71 (`v0.1.44`), the code review's Major finding.

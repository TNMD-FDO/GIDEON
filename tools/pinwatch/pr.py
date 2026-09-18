"""Git and GitHub pull-request lifecycle for pin-watch proposals."""

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from gideon.host import models
from gideon.host.images import DIGEST, parse_image_lock
from gideon.host.sysio import Host
from tools.pinwatch import skills
from tools.pinwatch.notes import NoteBinding
from tools.pinwatch.pins import Bump

WITHDRAWN_PREFIX = "[withdrawn] "
PRODUCT_LOCKS = frozenset(("images.lock", "host.lock", "models.lock"))
SKILL_RECORDS = frozenset((skills.PROVENANCE_PATH,))
# The byte-stable render fixtures are a function of all three product locks (the compose
# image references and the manifest's host_lock_sha256 and models_lock_sha256), so a
# bump branch on one of those carries them regenerated — otherwise every bump PR
# would fail test_render. A skill record feeds no fixture.
REGENERATE_FIXTURES = "tests/regenerate_render_fixtures.py"
FIXTURES_DIR = "tests/fixtures/render"
_AUTH_FIX = (
    "Check the App token the pin-watch workflow mints (PIN_WATCH_APP_CLIENT_ID and "
    "PIN_WATCH_APP_PRIVATE_KEY, handed to gh as GH_TOKEN) or, on a dev seat, "
    "gh auth status; see docs/runbooks/pin-watch-app-setup.md, "
    "then re-run the pin watch."
)
_CHECKOUT_FIX = "Inspect the checkout (git status, git log origin/main), then re-run the pin watch."
_FIXTURE_FIX = (
    "Run python3 tests/regenerate_render_fixtures.py from the checkout with the "
    "patched lock in place and fix what it reports, then re-run the pin watch."
)


@dataclass(frozen=True, slots=True)
class PrError(Exception):
    """A git or gh operation refused to complete."""

    problem: str
    fix: str = _AUTH_FIX

    def __post_init__(self) -> None:
        Exception.__init__(self, self.problem)


@dataclass(frozen=True, slots=True)
class PullRequest:
    """The fields returned by the all-state ``gh pr list`` query."""

    number: int
    url: str
    title: str
    head: str
    state: str
    merged_at: str | None
    closed_at: str | None


@dataclass(frozen=True, slots=True)
class PullRequests:
    """Pin-watch pull requests, with the state-table lookups kept local."""

    entries: tuple[PullRequest, ...]

    def open_for(self, head: str) -> PullRequest | None:
        return next(
            (pr for pr in self.entries if pr.head == head and pr.state == "OPEN"),
            None,
        )

    def last_closed_for(self, head: str) -> PullRequest | None:
        closed = [
            pr
            for pr in self.entries
            if pr.head == head and pr.state != "OPEN" and pr.closed_at is not None
        ]
        return max(closed, key=lambda pr: pr.closed_at or "", default=None)


def _command_problem(argv: Sequence[str], result: subprocess.CompletedProcess[str]) -> str:
    """Name the command and its last diagnostic line (a traceback ends with the error)."""

    lines = result.stderr.strip().splitlines() or result.stdout.strip().splitlines()
    detail = lines[-1].strip() if lines else "no output"
    shown = " ".join(argv[:3]) if argv[0] in ("git", "gh") else Path(argv[-1]).name
    return f"{shown} exited {result.returncode}: {detail}"


def _fix_for(argv: Sequence[str]) -> str:
    """The repair that matches the operation: credentials, the checkout, or the fixtures."""

    if argv[0] == "gh" or argv[:2] == ("git", "push"):
        return _AUTH_FIX
    if argv[0] == "git":
        return _CHECKOUT_FIX
    return _FIXTURE_FIX


def _run(
    host: Host,
    root: Path,
    argv: Sequence[str],
    *,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = host.run(argv, cwd=root, input=input)
    if result.returncode != 0:
        raise PrError(_command_problem(argv, result), _fix_for(tuple(argv)))
    return result


def sync(host: Host, root: Path) -> None:
    """Fetch main and all pin-watch remote branches exactly once."""

    _run(
        host,
        root,
        [
            "git",
            "fetch",
            "origin",
            "main",
            "+refs/heads/pin-watch/*:refs/remotes/origin/pin-watch/*",
        ],
    )


def lock_text(host: Host, root: Path, lock: str) -> str:
    """Read a lock from the fetched main tip."""

    return _run(host, root, ["git", "show", f"origin/main:{lock}"]).stdout


def list_pull_requests(host: Host, root: Path) -> PullRequests:
    """List all PR states, retaining only the pin-watch heads."""

    result = _run(
        host,
        root,
        [
            "gh",
            "pr",
            "list",
            "--base",
            "main",
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,url,title,headRefName,state,mergedAt,closedAt",
        ],
    )
    try:
        raw = json.loads(result.stdout)
        entries = tuple(
            PullRequest(
                number=int(item["number"]),
                url=str(item["url"]),
                title=str(item["title"]),
                head=str(item["headRefName"]),
                state=str(item["state"]),
                merged_at=item.get("mergedAt"),
                closed_at=item.get("closedAt"),
            )
            for item in raw
            if str(item["headRefName"]).startswith("pin-watch/")
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PrError(f"gh returned invalid pull-request JSON: {exc}") from None
    return PullRequests(entries)


def branch_for(pin_id: str) -> str:
    return f"pin-watch/{pin_id}"


def _short_digest(value: str) -> str:
    return f"sha256:{value.removeprefix('sha256:')[:12]}"


def title_for(bump: Bump) -> str:
    change = bump.changes[0]
    if change.key_path.endswith(".revision"):
        return (
            f"pin watch: {bump.pin_id} revision "
            f"{change.old[:12]} → {change.new[:12]}"
        )
    if (
        len(bump.changes) == 1
        and bump.pin_id.startswith("images.")
        and change.key_path.endswith(".digest")
    ):
        old = _short_digest(change.old)
        new = _short_digest(change.new)
        return f"pin watch: {bump.pin_id} digest {old} → {new}"
    return f"pin watch: {bump.pin_id} {change.old} → {change.new}"


_DIGEST_ONLY = "unchanged (the digest alone moves)"


def version_token(key_path: str, value: str) -> str | None:
    """The version a lock value carries, or ``None`` for a digest, checksum, or date."""

    if key_path.endswith(".revision"):
        return value
    if key_path.endswith((".source", ".base")):
        _separator, separator, token = value.rpartition(":")
        return token if separator and token else None
    if key_path == "registry_image":
        reference, separator, _digest = value.partition("@")
        if not separator:
            return None
        _host, separator, token = reference.rpartition(":")
        return token if separator and token else None
    if (
        key_path == "gh_runner.version"
        or key_path.startswith("minimums.")
        or key_path == "driver.branch"
        or (key_path.startswith("skills.") and key_path.endswith(".ref"))
        or key_path == "matt-pocock.commit"
        or key_path == "acceptance_vm_image.url"
        or ".build_args." in key_path
    ):
        return value
    return None


def proposed_version(bump: Bump) -> str:
    """Return the version a bump proposes, or its digest-only description."""

    for change in bump.changes:
        new = version_token(change.key_path, change.new)
        if new is None:
            continue
        if new == version_token(change.key_path, change.old):
            return _DIGEST_ONLY
        return new
    return _DIGEST_ONLY


def _block_summary(block: Mapping[str, object]) -> tuple[int, int]:
    """Return a file count and total byte size from a model-files mapping."""

    return len(block), sum(
        int(entry["size"])
        for entry in block.values()
        if isinstance(entry, Mapping)
    )


def body_for(
    bump: Bump,
    notes: Sequence[NoteBinding],
    *,
    memory_rows: Sequence[models.MemoryRow] = (),
) -> str:
    """Render only tool-owned facts into a proposal body."""

    lines = [
        f"## Pin watch: `{bump.pin_id}`",
        "",
        "| Key path | Old | New |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{change.key_path}` | `{change.old}` | `{change.new}` |"
        for change in bump.changes
    )
    for block in bump.blocks:
        old_count, old_size = _block_summary(block.old)
        new_count, new_size = _block_summary(block.new)
        lines.append(
            f"| `{block.key_path}` | {old_count} files, {old_size} bytes | "
            f"{new_count} files, {new_size} bytes |"
        )
    lines.extend(("", f"Upstream: {bump.upstream_url}", ""))
    if bump.lock in PRODUCT_LOCKS:
        lines.extend(
            (
                (
                    f"The render fixtures under `{FIXTURES_DIR}/` are regenerated "
                    "with the lock (they embed its digests), so the byte-stable "
                    "render contract passes on this pull request."
                ),
                "",
            )
        )
    if bump.pin_id == skills.MATT_PIN_ID:
        lines.append(
            "What merging does: complete on this branch per `docs/agents/tooling.md` "
            "§3 before merging — read the head, install, prove the install against a "
            "clone at the upstream URL's commit with `python3 -m tools.pinwatch.skills "
            "--source mattpocock/skills <clone>`, then commit. No hosted check can tell "
            "a completed branch from an incomplete one for a branch-tracking source, "
            "so this pull request is green from the start and the checker is the guard."
        )
    elif bump.pin_id.startswith("models."):
        lines.append(
            "What merging does: nothing moves on a box until its next `apply`, and "
            "only on a box whose site file selects this pin's profile. The models "
            "stage fetches and verifies the new files into the weights tree and "
            "recreates the engine — a maintenance window from go-live (§21), with "
            "`engine verify` gating the new weights — and a human tags the release."
        )
    elif bump.pin_id.startswith("images."):
        lines.append(
            "What merging does: the hosted checks run now; a merge runs the box's "
            "`mirror-images` and `frontend-contract` jobs; a human tags the release."
        )
    else:
        lines.append(
            "What merging does: nothing on the box moves until the next gideon host "
            "provision; a human tags the release."
        )
    if bump.pin_id == "images.open-webui":
        lines.extend(
            (
                "",
                "Before the merge, this bump is proven on the box with the turn "
                "harness in both modes on `eval/seed/general/frontend-bump.yaml`, "
                "per `docs/runbooks/pin-watch-app-setup.md` §5.",
            )
        )
    if bump.pin_id == "host.gh_runner":
        lines.append("")
        lines.append(
            "The runner runs with automatic updates disabled (ADR-0031), so this bump "
            "is its only upgrade path: merge, then on the box bring the checkout that "
            "runs provision to the merged commit or the tagged release (provision "
            "reads `host.lock` from the checkout it runs in, so the merge alone moves "
            "nothing on the box) and run `sudo python3 -m gideon host provision` — "
            "the step stops the service, installs the pinned release, and restarts "
            "it. GitHub queues a runner with updates off no jobs past thirty days "
            "of a release, and none at all once a critical security update is out, "
            "so land both inside that window "
            "(`docs/runbooks/office-services-setup.md` §6)."
        )
    if bump.proposal and bump.lock not in SKILL_RECORDS:
        lines.append("")
        built_change = any(
            change.key_path.endswith((".base", ".base_digest"))
            or ".build_args." in change.key_path
            for change in bump.changes
        )
        if bump.pin_id.startswith("models."):
            role = bump.pin_id.rsplit(".", 1)[-1]
            model_block = bump.blocks[0] if bump.blocks else None
            old_count, old_size = (
                _block_summary(model_block.old) if model_block else (0, 0)
            )
            new_count, new_size = (
                _block_summary(model_block.new) if model_block else (0, 0)
            )
            lines.append(
                f"This is a model proposal for role `{role}`: the pinned files "
                f"change from {old_count} files / {old_size} bytes to {new_count} "
                f"files / {new_size} bytes."
            )
            if memory_rows:
                rows = ", ".join(
                    f"`{row.service}` (role `{row.role}`, {row.gb} GB)"
                    for row in memory_rows
                )
                lines.append(f"Memory rows naming role `{role}`: {rows}.")
            lines.append(
                "Re-judge the memory row per §7.6 on this branch; after "
                "editing the row, run `python3 tests/regenerate_render_fixtures.py` "
                "and commit the lock and the fixtures together. A commit here "
                "completes the proposal, which the watch then leaves alone."
            )
        elif built_change:
            lines.append(
                "This is a built-image proposal the watch cannot complete: rebuild on "
                "the box with `python3 -m tools.imagebuild <name> --to <registry>` per "
                "`docs/runbooks/built-images.md` §2, then commit "
                "the recorded `digest` and `inputs_digest` on this branch. The hosted "
                "checks stay red until then (the lock's `inputs_digest` no longer "
                "matches), and the watch then reports the branch completed and leaves it "
                "alone."
            )
        else:
            lines.append(
                "This is a proposal: `driver.tested` moves and a floor is raised only "
                "after an on-box converge. An image major needs an upgrade plan and a "
                "product major per §2.1."
            )
        if bump.pin_id == "images.postgres" and not built_change:
            lines.append(
                "For Postgres: the Compose model pins `PGDATA` to the current major's "
                "data directory, so the fresh-database `frontend-contract` job proves "
                "nothing about upgrading the existing cluster — the plan must cover "
                "`pg_upgrade` or dump-and-restore before this merges."
            )
    lines.extend(
        (
            "",
            "### Research notes verified against this pin",
            "",
            (
                "These notes were verified against this pin and are read against the "
                "proposed version before the merge; an amendment is a `docs:` commit "
                "on `main` that the next watch run rebases this branch onto, never a "
                "commit on this branch, which the watch owns and force-pushes."
            ),
            "",
        )
    )
    if notes:
        lines.extend(
            (
                "| Note | Verified against | Proposed |",
                "| --- | --- | --- |",
            )
        )
        proposed = proposed_version(bump)
        lines.extend(
            f"| `{note.note}` | {note.version} | {proposed} |" for note in notes
        )
    else:
        lines.append("No research note under `docs/research/` names this pin.")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class BranchState:
    """A remote pin branch's state, its head, and a human's completion when present.

    ``completion_digest`` is the built digest a human recorded on the branch
    (valid, not ``unbuilt``, and different from what the watch generated); it is
    set for ``completed`` and for a ``stale`` branch whose completion a newer
    bump superseded, so the run can name what it discards.

    Skill- and model-record completions carry the branch record text and whether
    it holds the current bump instead of a built-image digest.
    """

    state: Literal["absent", "identical", "stale", "completed"]
    head_sha: str = ""
    completion_digest: str | None = None
    branch_record_text: str | None = None
    carries: bool | None = None


def _values_at(document: object, paths: tuple[str, ...]) -> list[object] | None:
    values: list[object] = []
    for path in paths:
        current = document
        for segment in path.split("."):
            if not isinstance(current, Mapping) or segment not in current:
                return None
            current = current[segment]
        values.append(current)
    return values


def _completion_digest(
    branch: object, generated: object, completion_paths: tuple[str, ...]
) -> str | None:
    """The digest a human recorded on the branch, or ``None`` when there is none."""

    branch_values = _values_at(branch, completion_paths)
    generated_values = _values_at(generated, completion_paths)
    if not branch_values or branch_values == generated_values:
        return None
    if not all(isinstance(value, str) and DIGEST.fullmatch(value) for value in branch_values):
        return None
    for path, value in zip(completion_paths, branch_values, strict=True):
        if path.endswith(".digest"):
            return cast(str, value)
    return cast(str, branch_values[0])


def completion_paths(bump: Bump) -> tuple[str, ...]:
    """The two lock paths a human completes on a built-pin proposal, or none."""

    for change in bump.changes:
        if change.key_path.endswith((".base", ".base_digest")):
            image_path = change.key_path.rsplit(".", 1)[0]
        elif ".build_args." in change.key_path:
            image_path = change.key_path.split(".build_args.", 1)[0]
        else:
            continue
        return (f"{image_path}.digest", f"{image_path}.inputs_digest")
    return ()


def _carries_bump(document: object, bump: Bump) -> bool:
    values = _values_at(document, tuple(change.key_path for change in bump.changes))
    if values is None or values != [change.new for change in bump.changes]:
        return False
    for block in bump.blocks:
        block_values = _values_at(document, (block.key_path,))
        if block_values is None or block_values[0] != block.new:
            return False
    return True


def branch_state(
    host: Host,
    root: Path,
    branch: str,
    lock: str,
    patched: str,
    *,
    bump: Bump | None = None,
) -> BranchState:
    """Classify a remote pin branch against its content and main parent.

    ``identical`` means the branch carries exactly *patched* and sits on the
    fetched ``origin/main`` tip. For a built-pin proposal (*bump* names one),
    ``completed`` is judged first and only on the bump's own paths: the branch
    still carries exactly this proposal's values and a human recorded a
    rebuilt ``digest``/``inputs_digest`` there. Anything else the lock did on
    ``main`` since is a rebase the human makes at merge time, never a reason to
    force-push over the rebuild.
    """

    verified = host.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=root,
    )
    if verified.returncode != 0:
        return BranchState("absent")
    head = verified.stdout.strip()
    content = host.run(["git", "show", f"origin/{branch}:{lock}"], cwd=root)
    if bump is not None and (lock in SKILL_RECORDS or lock == "models.lock"):
        # Judged before anything can read as stale: a person's commits are
        # protected whether or not the branch's record is readable.
        count_argv = ["git", "rev-list", "--count", f"origin/main..origin/{branch}"]
        count_result = host.run(count_argv, cwd=root)
        if count_result.returncode != 0:
            raise PrError(_command_problem(count_argv, count_result), _CHECKOUT_FIX)
        try:
            count = int(count_result.stdout.strip())
        except ValueError:
            raise PrError(_command_problem(count_argv, count_result), _CHECKOUT_FIX) from None
        if count > 1:
            record_text = content.stdout if content.returncode == 0 else None
            carries_bump = False
            if record_text is not None:
                if lock == "models.lock":
                    models_result = models.parse_models_lock(record_text)
                    carries_bump = (
                        models_result.document is not None
                        and _carries_bump(models_result.document, bump)
                    )
                else:
                    carries_bump = skills.carries(record_text, bump)
            return BranchState(
                "completed",
                head,
                branch_record_text=record_text,
                carries=carries_bump,
            )
    if content.returncode != 0:
        return BranchState("stale", head)
    completion_digest: str | None = None
    paths = completion_paths(bump) if bump is not None else ()
    if bump is not None and paths and content.stdout != patched:
        branch_result = parse_image_lock(content.stdout)
        generated_result = parse_image_lock(patched)
        if branch_result.document is not None and generated_result.document is not None:
            completion_digest = _completion_digest(
                branch_result.document, generated_result.document, paths
            )
            if completion_digest is not None and _carries_bump(branch_result.document, bump):
                return BranchState("completed", head, completion_digest)
    parent = host.run(["git", "rev-parse", f"origin/{branch}^"], cwd=root)
    main = host.run(["git", "rev-parse", "origin/main"], cwd=root)
    if (
        content.returncode == 0
        and content.stdout == patched
        and parent.returncode == 0
        and main.returncode == 0
        and parent.stdout.strip() == main.stdout.strip()
    ):
        return BranchState("identical", head)
    return BranchState("stale", head, completion_digest)


def push_bump(
    host: Host, root: Path, branch: str, lock: str, patched: str, title: str
) -> None:
    """Publish a patched record and return the tree to main."""

    _run(host, root, ["git", "checkout", "-B", branch, "origin/main"])
    host.write_text(root / lock, patched)
    paths = [lock]
    if lock in PRODUCT_LOCKS:
        _run(host, root, [sys.executable, REGENERATE_FIXTURES])
        paths.append(FIXTURES_DIR)
    _run(host, root, ["git", "add", *paths])
    _run(host, root, ["git", "commit", "-m", title])
    _run(host, root, ["git", "push", "--force", "origin", branch])
    _run(host, root, ["git", "checkout", "--detach", "origin/main"])


def create(host: Host, root: Path, branch: str, title: str, body: str) -> str:
    result = _run(
        host,
        root,
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            "-",
        ],
        input=body,
    )
    url = result.stdout.strip()
    if not url:
        raise PrError("gh pr create returned no pull-request URL")
    return url


def edit(host: Host, root: Path, number: int, title: str, body: str) -> None:
    _run(
        host,
        root,
        ["gh", "pr", "edit", str(number), "--title", title, "--body-file", "-"],
        input=body,
    )


def withdraw(host: Host, root: Path, pr: PullRequest, comment: str) -> None:
    title = pr.title if pr.title.startswith(WITHDRAWN_PREFIX) else WITHDRAWN_PREFIX + pr.title
    _run(host, root, ["gh", "pr", "edit", str(pr.number), "--title", title])
    _run(
        host,
        root,
        [
            "gh",
            "pr",
            "close",
            str(pr.number),
            "--comment",
            comment,
            "--delete-branch",
        ],
    )

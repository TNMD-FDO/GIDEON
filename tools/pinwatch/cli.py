"""The pin-watch run: resolve pins, reconcile PR state, and print rows."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from gideon.host import models
from gideon.host.images import load_image_lock_text
from gideon.host.images import render_errors as render_image_errors
from gideon.host.lock import load_host_lock_text
from gideon.host.lock import render_errors as render_host_errors
from gideon.host.sysio import Host, RealHost
from tools.pinwatch import notes, skills
from tools.pinwatch.fetch import Fetcher, FetchError, UrllibFetcher
from tools.pinwatch.oci import UnreadableTagError
from tools.pinwatch.patch import PatchError, apply_bump
from tools.pinwatch.pins import Bump, ModelPin, Pin, SkillPin, pin_registry
from tools.pinwatch.pr import (
    WITHDRAWN_PREFIX,
    BranchState,
    PrError,
    PullRequest,
    PullRequests,
    body_for,
    branch_for,
    branch_state,
    create,
    edit,
    list_pull_requests,
    lock_text,
    push_bump,
    sync,
    title_for,
    withdraw,
)
from tools.pinwatch.skills import SkillsRecordError, parse_provenance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pin-watch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", action="append", default=[], metavar="PIN_ID")
    parser.add_argument("--root", type=Path)
    return parser


def _error_row(pin: Pin, problem: str, fix: str) -> str:
    return f"{pin.id}: failed — {problem}. Fix: {fix}"


def _print_pr_error(error: PrError) -> None:
    print(f"pin watch: {error.problem} Fix: {error.fix}", file=sys.stderr)


def _resolver_fix(pin: Pin, error: FetchError | ValueError) -> tuple[str, str]:
    """The failed row's problem and fix: the URL, rule, or lock to fix."""

    if isinstance(error, FetchError):
        return error.reason, f"Check {error.url}, then re-run the pin watch."
    if isinstance(error, UnreadableTagError):
        return error.problem, error.fix
    return str(error), f"Correct {pin.id} in {pin.lock}, then re-run the pin watch."


def _bump_text(bump: Bump) -> str:
    change = bump.changes[0]
    proposal = " (proposal)" if bump.proposal else ""
    return f"{change.old} → {change.new}{proposal}"


def _would(dry_run: bool) -> str:
    return "would " if dry_run else ""


def _closed_comment(pin: Pin) -> str:
    return f"main already carries {pin.current()}; withdrawing this proposal."


def _skill_completed_row(
    pin: SkillPin,
    bump: Bump,
    state: BranchState,
    open_pr: PullRequest,
    *,
    host: Host,
    root: Path,
    dry_run: bool,
    bindings: Sequence[notes.NoteBinding],
) -> str:
    """Describe a human-owned skill branch from the record it actually holds."""

    branch_values = (
        skills.record_values(state.branch_record_text, bump)
        if state.branch_record_text is not None
        else None
    )
    suffix = ""
    if branch_values is not None and branch_values != tuple(
        change.old for change in bump.changes
    ):
        branch_bump = pin.branch_bump(bump, branch_values)
        branch_title = title_for(branch_bump)
        if branch_title != open_pr.title:
            if not dry_run:
                edit(host, root, open_pr.number, branch_title, _body(pin, branch_bump, bindings))
            wording = "would retitled to" if dry_run else "retitled to"
            suffix = f" — PR {open_pr.url} {wording} {branch_values[0]}"
    if not state.carries:
        suffix += f"; upstream now {bump.changes[0].new}"
    return f"{pin.id}: completed — {state.head_sha}{suffix}"


def _body(pin: Pin, bump: Bump, bindings: Sequence[notes.NoteBinding]) -> str:
    """The pull-request body for *pin*, with a model pin's memory rows."""

    return body_for(
        bump,
        notes.bound_to(bindings, pin.id),
        memory_rows=pin.memory_rows if isinstance(pin, ModelPin) else (),
    )


def _model_completed_row(pin: ModelPin, bump: Bump, state: BranchState) -> str:
    """Describe a completed model branch and whether it carries this bump."""

    suffix = "" if state.carries else f"; upstream now {bump.changes[0].new}"
    return f"{pin.id}: completed — {state.head_sha}{suffix}"


def _process_pin(
    pin: Pin,
    *,
    fetcher: Fetcher,
    host: Host,
    root: Path,
    locks: dict[str, str],
    prs: PullRequests,
    dry_run: bool,
    bindings: Sequence[notes.NoteBinding],
) -> tuple[str, bool]:
    """Return one output row and whether it is a failed row."""

    current = pin.current()
    try:
        bump = pin.resolve(fetcher)
    except (FetchError, ValueError) as error:
        problem, fix = _resolver_fix(pin, error)
        return _error_row(pin, problem, fix), True

    head = branch_for(pin.id)
    open_pr = prs.open_for(head)
    if bump is None:
        if open_pr is None:
            return f"{pin.id}: current — {current}", False
        if not dry_run:
            try:
                withdraw(host, root, open_pr, _closed_comment(pin))
            except PrError as error:
                return _error_row(pin, error.problem, error.fix), True
        return (
            f"{pin.id}: {_would(dry_run)}closed — main carries {current}; PR {open_pr.url}",
            False,
        )

    title = title_for(bump)
    if open_pr is None:
        last = prs.last_closed_for(head)
        if (
            last is not None
            and last.state == "CLOSED"
            and not last.title.startswith(WITHDRAWN_PREFIX)
            and last.title == title
        ):
            change = bump.changes[0]
            return (
                (
                    f"{pin.id}: declined — {change.old} → {change.new} closed by a human "
                    f"in PR {last.url}; nothing reopened"
                ),
                False,
            )

    try:
        patched = apply_bump(locks[pin.lock], bump)
    except PatchError as error:
        return (
            _error_row(
                pin,
                str(error),
                f"Check {bump.upstream_url}, then re-run the pin watch.",
            ),
            True,
        )

    if open_pr is None:
        body = _body(pin, bump, bindings)
        if dry_run:
            return f"{pin.id}: would opened — {_bump_text(bump)}", False
        try:
            push_bump(host, root, head, pin.lock, patched, title)
            url = create(host, root, head, title, body)
        except PrError as error:
            return _error_row(pin, error.problem, error.fix), True
        return f"{pin.id}: opened — {_bump_text(bump)} — PR {url}", False

    try:
        state = branch_state(
            host,
            root,
            head,
            pin.lock,
            patched,
            bump=bump,
        )
    except PrError as error:
        return _error_row(pin, error.problem, error.fix), True
    if state.state == "completed":
        if isinstance(pin, SkillPin):
            try:
                return (
                    _skill_completed_row(
                        pin,
                        bump,
                        state,
                        open_pr,
                        host=host,
                        root=root,
                        dry_run=dry_run,
                        bindings=bindings,
                    ),
                    False,
                )
            except PrError as error:
                return _error_row(pin, error.problem, error.fix), True
        if isinstance(pin, ModelPin):
            return _model_completed_row(pin, bump, state), False
        return f"{pin.id}: completed — {state.completion_digest}", False
    if state.state == "identical" and open_pr.title == title:
        return (
            (
                f"{pin.id}: unchanged — PR {open_pr.url} already carries "
                f"{bump.changes[0].new}"
            ),
            False,
        )
    if not dry_run:
        try:
            push_bump(host, root, head, pin.lock, patched, title)
            edit(host, root, open_pr.number, title, _body(pin, bump, bindings))
        except PrError as error:
            return _error_row(pin, error.problem, error.fix), True
    superseded = (
        f" — superseded completion {state.head_sha}"
        if state.completion_digest is not None
        else ""
    )
    return (
        (
            f"{pin.id}: {_would(dry_run)}updated — {_bump_text(bump)} — PR {open_pr.url}"
            f"{superseded}"
        ),
        False,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    fetcher: Fetcher | None = None,
    root: Path | None = None,
) -> int:
    """Run pin-watch with injectable system I/O and HTTP transport."""

    options = _parser().parse_args(argv)
    io = host or RealHost()
    http = fetcher or UrllibFetcher()
    checkout = root or options.root or Path(__file__).resolve().parents[2]
    try:
        sync(io, checkout)
        image_text = lock_text(io, checkout, "images.lock")
        host_text = lock_text(io, checkout, "host.lock")
        models_text = lock_text(io, checkout, "models.lock")
        tooling_text = lock_text(io, checkout, "docs/agents/tooling.md")
    except PrError as error:
        _print_pr_error(error)
        return 1

    image_result = load_image_lock_text(image_text)
    host_result = load_host_lock_text(host_text)
    models_result = models.load_models_lock_text(models_text)
    if image_result.errors:
        print(render_image_errors(image_result.errors), file=sys.stderr)
    if host_result.errors:
        print(render_host_errors(host_result.errors), file=sys.stderr)
    if models_result.errors:
        print(models.render_errors(models_result.errors), file=sys.stderr)
    if (
        image_result.lock is None
        or host_result.lock is None
        or models_result.lock is None
    ):
        return 1
    try:
        provenance = parse_provenance(tooling_text)
    except SkillsRecordError as error:
        print(f"pin watch: {error.problem} Fix: {error.fix}", file=sys.stderr)
        return 1
    pins = pin_registry(
        image_result.lock,
        host_result.lock,
        models_result.lock,
        provenance,
    )
    try:
        requirements_text = io.read_text(checkout / "requirements-dev.txt")
    except OSError as error:
        print(
            f"pin watch: cannot read requirements-dev.txt from checkout {checkout}: "
            f"{error} Fix: Restore requirements-dev.txt in checkout "
            f"{checkout}, then re-run the pin watch.",
            file=sys.stderr,
        )
        return 1
    try:
        vocabulary = notes.build_vocabulary(pins, requirements_text)
        bindings = notes.read_bindings(io, checkout)
        notes.validate_bindings(bindings, vocabulary)
    except notes.NotesError as error:
        print(f"pin watch: {error.problem} Fix: {error.fix}", file=sys.stderr)
        return 1
    try:
        prs = list_pull_requests(io, checkout)
    except PrError as error:
        _print_pr_error(error)
        return 1
    selected = set(options.only)
    known = {pin.id for pin in pins}
    unknown = sorted(selected - known)
    if unknown:
        print(
            "pin watch: unknown pin id(s): "
            + ", ".join(unknown)
            + ". Known ids: "
            + ", ".join(pin.id for pin in pins),
            file=sys.stderr,
        )
        return 1
    if selected:
        pins = tuple(pin for pin in pins if pin.id in selected)

    locks = {
        "images.lock": image_text,
        "host.lock": host_text,
        "models.lock": models_text,
        "docs/agents/tooling.md": tooling_text,
    }
    failed = False
    for pin in pins:
        row, row_failed = _process_pin(
            pin,
            fetcher=http,
            host=io,
            root=checkout,
            locks=locks,
            prs=prs,
            dry_run=options.dry_run,
            bindings=bindings,
        )
        print(row)
        failed = failed or row_failed
    return 1 if failed else 0

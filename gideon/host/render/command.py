"""The host-facing render command and its filesystem classification."""

import difflib
import hashlib
import os
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, cast

import yaml  # type: ignore[import-untyped]

import gideon
from gideon.host import nogpu
from gideon.host.images import load_image_lock
from gideon.host.images import render_errors as render_image_errors
from gideon.host.lock import load_host_lock
from gideon.host.lock import render_errors as render_lock_errors
from gideon.host.models import load_models_lock, select_profile
from gideon.host.models import render_errors as render_models_errors
from gideon.host.render import ARTIFACTS, RenderedSet, RenderInputs, render_all
from gideon.host.render.api import API_SOURCES
from gideon.host.render.compose import (
    compose_top_level,
    service_blocks,
    service_names,
)
from gideon.host.render.facts import FactsError, HostFacts, gather_facts
from gideon.host.render.owui import OWUI_SECRET_NAMES
from gideon.host.render.proxy import PROXY_AUTH_NAME
from gideon.host.render.searxng import SEARXNG_SECRET_NAME, site_search_enabled
from gideon.host.render.yamlout import dump, dump_fragment
from gideon.host.report import Problem, refusal
from gideon.host.secrets import is_generated, read_secret, secret_path
from gideon.host.site import load_site
from gideon.host.site import render_errors as render_site_errors
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_ROOT_FIX: Final = "Run gideon render as root, for example with sudo."
_TEMPLATE_FIX: Final = (
    "Restore the release checkout's compose template, then re-run render."
)
_API_SOURCE_FIX: Final = (
    "A checkout without it is not a release tree: restore the path from the "
    "release, then re-run render."
)
_FOREIGN_FIX: Final = (
    "Remove or move the foreign file(s), then re-run render."
)
_RENDER_FIX: Final = "Correct the render inputs, then re-run render."
_MANIFEST_NAME: Final = "manifest.yaml"
_APPLIED_NAME: Final = "applied.yaml"
_GENERATED_SECRET_FIX: Final = (
    "Run sudo python3 -m gideon apply (it generates missing secrets), then retry."
)
_SECRET_LINE_FIX: Final = "Correct the secret file, then retry."
_SMTP_PASSWORD_NAME: Final = "smtp_password"
_SMTP_PASSWORD_MISSING_FIX: Final = (
    "Place the secret in /etc/gideon/secrets/smtp_password, or clear "
    "alerts.smtp.user, then retry."
)


def _render_secret(io: Host, name: str, command: str) -> str | None:
    """One secret's value for the render inputs, or None after printing the refusal.

    A generated secret that is merely absent names apply (which creates it); a
    supplied one names its path.  The raw env-file format is line-based, so a
    value carrying a line break or NUL refuses rather than rendering.
    """

    result = read_secret(io, name)
    if not result.ok or result.value is None:
        fix = _GENERATED_SECRET_FIX if result.missing and is_generated(name) else result.fix
        problem = result.problem or f"Secret is unavailable: {name}."
        print(_refusal(problem, fix, command), file=sys.stderr)
        return None
    if any(character in result.value for character in "\r\n\x00"):
        problem = f"Secret value contains CR, LF, or NUL: {secret_path(name)}."
        print(_refusal(problem, _SECRET_LINE_FIX, command), file=sys.stderr)
        return None
    return result.value


class FileChange(StrEnum):
    """The relationship between one rendered path and its previous state."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    STALE = "stale"
    FOREIGN = "foreign"


@dataclass(slots=True)
class RenderOutcome:
    """The render result shared by the render and apply command layers."""

    rendered: RenderedSet | None = None
    inputs: RenderInputs | None = None
    classifications: list[tuple[str, FileChange, str]] = field(default_factory=list)
    stale: set[str] = field(default_factory=set)
    exit_code: int = 1


def _refusal(problem: object, fix: str, command: str = "render") -> str:
    return refusal(command, problem, fix)


def load_templates(host: Host, root: PathLike) -> Mapping[str, str]:
    """Load every template declared by the ordered artifact registry."""

    checkout = Path(root)
    templates: dict[str, str] = {}
    for artifact in ARTIFACTS:
        for relative_path in artifact.template_paths:
            if relative_path in templates:
                continue
            template_path = checkout / "compose" / relative_path
            try:
                templates[relative_path] = host.read_text(template_path)
            except FileNotFoundError as exc:
                raise ValueError(
                    f"Render template is missing: {relative_path}"
                ) from exc
            except (OSError, UnicodeError) as exc:
                raise ValueError(
                    f"Render template is unreadable: {relative_path} ({exc})"
                ) from exc
    return templates


def gather_api_sources_digest(
    host: Host, root: PathLike, sources: Iterable[str] = API_SOURCES
) -> str:
    """Digest the service's declared sources by checkout-relative path and text.

    A declared source is a package directory, walked, or one module file;
    bytecode caches are skipped, so only the code the container imports moves
    the digest and, through the block's label, recreates the service.
    """

    checkout = Path(root)
    files: dict[str, str] = {}

    def collect(path: Path) -> None:
        mode = host.stat(path).st_mode
        if stat.S_ISDIR(mode):
            for name in host.listdir(path):
                if name != "__pycache__":
                    collect(path / name)
        elif stat.S_ISREG(mode) and path.suffix != ".pyc":
            files[path.relative_to(checkout).as_posix()] = host.read_text(path)

    for source in sources:
        try:
            collect(checkout / source)
        except (OSError, UnicodeError) as exc:
            raise ValueError(
                f"the declared service source {source} is missing or unreadable "
                f"under {checkout} ({exc})"
            ) from exc
    digest = hashlib.sha256()
    for relative, text in sorted(files.items()):
        digest.update(f"{relative}\0{text}\0".encode())
    return f"sha256:{digest.hexdigest()}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComposeDigests:
    """The candidate's service-block and Compose top-level digests."""

    services: Mapping[str, str]
    top_level: str


@dataclass(frozen=True, slots=True)
class AppliedManifest:
    """The last verified apply's file and optional Compose digest records."""

    files: Mapping[str, Mapping[str, object]]
    services: Mapping[str, str] | None
    top_level: str | None


@dataclass(frozen=True, slots=True)
class RecreateJudgment:
    """The ordered recreate union and the reasons that selected its services."""

    services: tuple[str, ...]
    files: tuple[str, ...]
    block: tuple[str, ...]
    top_level: tuple[str, ...]
    first_apply: bool


def compose_digests(inputs: RenderInputs) -> ComposeDigests:
    """The candidate's digests: one per service block, one over the top-level sections.

    Each is the sha256 of the deterministic serializer's header-less text, so
    a digest is a function of the block alone.
    """

    blocks = service_blocks(inputs)
    services: dict[str, str] = {}
    for name, block in blocks.items():
        if not isinstance(block, Mapping):
            raise TypeError(f"Compose service block is not a mapping: {name}")
        services[name] = _sha256(dump_fragment(block))
    return ComposeDigests(
        services=services,
        top_level=_sha256(dump_fragment(compose_top_level(inputs))),
    )


def _manifest_mapping(
    rendered: RenderedSet,
    inputs: RenderInputs,
    *,
    site_text: str,
    lock_text: str,
    models_lock_text: str,
) -> Mapping[str, object]:
    """Build the manifest value model without host access."""

    files: dict[str, object] = {}
    for rendered_file in rendered.files:
        files[rendered_file.relative_path] = {
            "sha256": _sha256(rendered_file.content),
            "mode": f"{rendered_file.mode:04o}",
            "owners": list(rendered_file.owners),
        }
    digests = compose_digests(inputs)
    return {
        "release": inputs.release,
        "inputs": {
            "site_sha256": _sha256(site_text),
            "host_lock_sha256": _sha256(lock_text),
            "models_lock_sha256": _sha256(models_lock_text),
            "hardware_profile": inputs.profile.name,
            "images_lock_version": inputs.images.version,
            "api_sources_digest": inputs.api_sources_digest,
            "no_gpu": inputs.no_gpu,
            "build_box": inputs.build_box,
        },
        "files": files,
        "services": dict(digests.services),
        "top_level": digests.top_level,
    }


def manifest_document(
    rendered: RenderedSet,
    inputs: RenderInputs,
    *,
    site_text: str,
    lock_text: str,
    models_lock_text: str,
) -> str:
    """Serialize the deterministic render manifest (the site and lock texts are hashed)."""

    return dump(
        _manifest_mapping(
            rendered,
            inputs,
            site_text=site_text,
            lock_text=lock_text,
            models_lock_text=models_lock_text,
        )
    )


def _relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return None
    return value


def _manifest_files(document: object, path: Path) -> dict[str, Mapping[str, object]]:
    if not isinstance(document, Mapping):
        raise TypeError(f"manifest is not a mapping: {path}")
    files_value = document.get("files")
    if not isinstance(files_value, Mapping):
        raise TypeError(f"manifest files is not a mapping: {path}")
    files: dict[str, Mapping[str, object]] = {}
    for relative, value in files_value.items():
        safe_relative = _relative_path(relative)
        if safe_relative is None or not isinstance(value, Mapping):
            raise ValueError(f"manifest has an invalid file entry: {path}")
        files[safe_relative] = cast(Mapping[str, object], value)
    return files


def _load_manifest(host: Host, path: Path) -> object:
    """The parsed manifest document; a file that cannot be read or parsed is a ValueError."""

    try:
        return yaml.safe_load(host.read_text(path))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"render manifest is unreadable: {path} ({exc})") from exc


def _read_manifest(host: Host, path: Path) -> dict[str, Mapping[str, object]]:
    """The previous render's file entries, empty when no manifest exists yet."""

    if not host.exists(path):
        return {}
    return _manifest_files(_load_manifest(host, path), path)


def _regular_files(host: Host, root: Path) -> set[str]:
    """Return regular files below *root*, relative to that root."""

    found: set[str] = set()

    def visit(directory: Path, prefix: str) -> None:
        try:
            names = host.listdir(directory)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError(f"rendered directory is unreadable: {directory} ({exc})") from exc
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            path = directory / name
            try:
                mode = host.stat(path).st_mode
            except OSError as exc:
                raise ValueError(f"rendered file is unreadable: {path} ({exc})") from exc
            if stat.S_ISDIR(mode):
                visit(path, relative)
            elif stat.S_ISREG(mode):
                found.add(relative)

    visit(root, "")
    return found


def _current_classification(
    host: Host,
    rendered_dir: Path,
    rendered: RenderedSet,
    previous: Mapping[str, Mapping[str, object]],
) -> tuple[list[tuple[str, FileChange, str]], set[str]]:
    classifications: list[tuple[str, FileChange, str]] = []
    current_paths = set(rendered.by_path)
    for rendered_file in rendered.files:
        path = rendered_dir / rendered_file.relative_path
        try:
            existing = host.read_text(path)
        except FileNotFoundError:
            classifications.append((rendered_file.relative_path, FileChange.NEW, ""))
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"rendered file is unreadable: {path} ({exc})") from exc
        else:
            change = (
                FileChange.UNCHANGED
                if existing == rendered_file.content
                else FileChange.CHANGED
            )
            classifications.append((rendered_file.relative_path, change, existing))
    stale = set(previous).difference(current_paths)
    return classifications, stale


def _manifest_digests(
    document: object, path: Path
) -> tuple[Mapping[str, str] | None, str | None]:
    """The record's Compose digests: each None when its key is absent, a refusal when malformed."""

    if not isinstance(document, Mapping):
        raise TypeError(f"manifest is not a mapping: {path}")
    services: Mapping[str, str] | None = None
    if "services" in document:
        services_value = document["services"]
        if not isinstance(services_value, Mapping) or not all(
            isinstance(name, str) and isinstance(digest, str)
            for name, digest in services_value.items()
        ):
            raise TypeError(f"manifest services is not a string mapping: {path}")
        services = cast(Mapping[str, str], services_value)
    top_level: str | None = None
    if "top_level" in document:
        top_level_value = document["top_level"]
        if not isinstance(top_level_value, str):
            raise TypeError(f"manifest top_level is not a string: {path}")
        top_level = top_level_value
    return services, top_level


def read_applied_manifest(host: Host, path: Path) -> AppliedManifest | None:
    """The record of the last apply that verified, or None when none has.

    The files, and the Compose digests when the record carries them (a record
    written before they existed reads as every block changed).  A present but
    unreadable or malformed record raises: it is never treated as absent,
    because absence means "recreate everything".
    """

    if not host.exists(path):
        return None
    document = _load_manifest(host, path)
    services, top_level = _manifest_digests(document, path)
    return AppliedManifest(
        files=_manifest_files(document, path), services=services, top_level=top_level
    )


def recreate_judgment(
    rendered: RenderedSet,
    applied: AppliedManifest | None,
    current_services: Iterable[str],
    digests: ComposeDigests,
) -> RecreateJudgment:
    """Judge file owners, Compose blocks, and Compose top level against the record.

    The applied record is the only reference: the disk may have changed through
    a standalone render.  A service owns its rendered files, its Compose block,
    and the Compose top level jointly with every other current service.
    """

    ordered = tuple(dict.fromkeys(current_services))
    if applied is None:
        return RecreateJudgment(ordered, (), (), (), True)

    if applied.services is None:
        return RecreateJudgment(
            ordered, (), ordered, (), False
        )

    file_services: set[str] = set()
    for rendered_file in rendered.files:
        entry = applied.files.get(rendered_file.relative_path)
        if entry is None or entry.get("sha256") != _sha256(rendered_file.content):
            file_services.update(rendered_file.owners)
    for relative_path, entry in applied.files.items():
        if relative_path in rendered.by_path:
            continue
        owners = entry.get("owners", [])
        if isinstance(owners, list):
            file_services.update(owner for owner in owners if isinstance(owner, str))

    block_services = {
        service
        for service, digest in digests.services.items()
        if applied.services.get(service) != digest
    }
    top_level_changed = applied.top_level is None or applied.top_level != digests.top_level
    compose_entry = applied.files.get("compose.yaml")
    compose_changed = compose_entry is None or compose_entry.get("sha256") != _sha256(
        rendered.by_path["compose.yaml"].content
    )
    if not top_level_changed and compose_changed and not block_services:
        top_level_changed = True

    def current(values: Iterable[str]) -> tuple[str, ...]:
        selected = set(values)
        return tuple(service for service in ordered if service in selected)

    files = current(file_services)
    block = current(block_services)
    top_level = ordered if top_level_changed else ()
    union = current((*files, *block, *top_level))
    return RecreateJudgment(union, files, block, top_level, False)


def recreate_services(
    rendered: RenderedSet,
    applied: AppliedManifest | None,
    current_services: Iterable[str],
    digests: ComposeDigests,
) -> tuple[str, ...]:
    """Return the tuple view of the applied-record recreate judgment.

    The three owners are rendered-file owners, each service's Compose block,
    and the Compose top level shared by all services.  The judgment reads the
    applied record only; what a standalone render left on disk is irrelevant.
    """

    return recreate_judgment(rendered, applied, current_services, digests).services


def _print_diff(
    rendered_dir: Path,
    rendered: RenderedSet,
    classifications: list[tuple[str, FileChange, str]],
    services: tuple[str, ...],
    stale: set[str],
) -> None:
    for relative_path, change, existing in classifications:
        if change not in (FileChange.NEW, FileChange.CHANGED):
            continue
        rendered_file = rendered.by_path[relative_path]
        if rendered_file.secret:
            print(f"{relative_path}: changed (secret; contents not shown)")
            continue
        diff = difflib.unified_diff(
            existing.splitlines(keepends=True),
            rendered_file.content.splitlines(keepends=True),
            fromfile=os.fspath(rendered_dir / relative_path),
            tofile=os.fspath(rendered_dir / relative_path),
        )
        sys.stdout.write("".join(diff))
    print(f"Services apply would recreate: {', '.join(services) or 'none'}")
    _print_summary(classifications, stale)


def _print_summary(
    classifications: list[tuple[str, FileChange, str]], stale: set[str]
) -> None:
    counts = {change: sum(item[1] is change for item in classifications) for change in FileChange}
    counts[FileChange.STALE] = len(stale)
    parts = [
        f"{counts[change]} {change.value}"
        for change in (FileChange.NEW, FileChange.CHANGED, FileChange.UNCHANGED, FileChange.STALE)
        if counts[change]
    ]
    print(f"Summary: {', '.join(parts) or 'no changes'}.")


def load_render_inputs(
    io: Host,
    *,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    root: Path,
    command: str,
) -> tuple[RenderInputs, str, str, str] | None:
    site_result = load_site(Path(site_path), host=io)
    lock_result = load_host_lock(lock_path, host=io)
    images_result = load_image_lock(images_path, host=io)
    models_result = load_models_lock(models_path, host=io)
    if site_result.errors:
        print(render_site_errors(site_result.errors), file=sys.stderr)
    if lock_result.errors:
        print(render_lock_errors(lock_result.errors), file=sys.stderr)
    if images_result.errors:
        print(render_image_errors(images_result.errors), file=sys.stderr)
    if models_result.errors:
        print(render_models_errors(models_result.errors), file=sys.stderr)
    if (
        site_result.config is None
        or lock_result.lock is None
        or images_result.lock is None
        or models_result.lock is None
        or site_result.errors
        or lock_result.errors
        or images_result.errors
        or models_result.errors
    ):
        return None
    profile = select_profile(models_result.lock, site_result.config.hardware_profile)
    if isinstance(profile, Problem):
        print(_refusal(profile.problem, profile.fix, command), file=sys.stderr)
        return None
    try:
        api_sources_digest = gather_api_sources_digest(io, root, API_SOURCES)
    except ValueError as exc:
        print(_refusal(exc, _API_SOURCE_FIX, command), file=sys.stderr)
        return None
    secrets: dict[str, str] = {}
    names = list(OWUI_SECRET_NAMES)
    if site_search_enabled(site_result.config):
        names.append(SEARXNG_SECRET_NAME)
    if site_result.config.alerts.smtp.user:
        names.append(_SMTP_PASSWORD_NAME)
    if io.exists(secret_path(PROXY_AUTH_NAME)):
        names.append(PROXY_AUTH_NAME)
    for name in names:
        if name == _SMTP_PASSWORD_NAME and not io.exists(secret_path(name)):
            print(
                _refusal(
                    f"Secret file is missing: {secret_path(name)}.",
                    _SMTP_PASSWORD_MISSING_FIX,
                    command,
                ),
                file=sys.stderr,
            )
            return None
        value = _render_secret(io, name, command)
        if value is None:
            return None
        secrets[name] = value
    try:
        site_text = io.read_text(site_path)
        lock_text = io.read_text(lock_path)
        models_lock_text = io.read_text(models_path)
    except (OSError, UnicodeError) as exc:
        print(
            _refusal(f"render input is unreadable: {exc}", _RENDER_FIX, command),
            file=sys.stderr,
        )
        return None
    try:
        templates = load_templates(io, root)
    except ValueError as exc:
        print(_refusal(exc, _TEMPLATE_FIX, command), file=sys.stderr)
        return None
    no_gpu = nogpu.is_no_gpu_host(io)
    build_box = nogpu.is_build_box(io)
    facts = gather_facts(io, no_gpu=no_gpu)
    if isinstance(facts, FactsError):
        print(_refusal(facts.problem, facts.fix, command), file=sys.stderr)
        return None
    return (
        RenderInputs(
            site=site_result.config,
            lock=lock_result.lock,
            images=images_result.lock,
            facts=cast(HostFacts, facts),
            profile=profile,
            templates=templates,
            release=gideon.__version__,
            secrets=secrets,
            checkout=os.fspath(root),
            api_sources_digest=api_sources_digest,
            no_gpu=no_gpu,
            build_box=build_box,
        ),
        site_text,
        lock_text,
        models_lock_text,
    )


def render_to_disk(
    io: Host,
    *,
    rendered_dir: PathLike,
    site_path: PathLike,
    lock_path: PathLike,
    images_path: PathLike,
    models_path: PathLike,
    root: PathLike,
    diff: bool,
    command: str,
) -> RenderOutcome:
    """Render, classify, and optionally write files for a host command."""

    checkout = Path(root)
    loaded = load_render_inputs(
        io,
        site_path=site_path,
        lock_path=lock_path,
        images_path=images_path,
        models_path=models_path,
        root=checkout,
        command=command,
    )
    if loaded is None:
        return RenderOutcome()
    inputs, site_text, lock_text, models_lock_text = loaded
    try:
        rendered = render_all(inputs)
    except Exception as exc:  # noqa: BLE001  # command boundary must not traceback
        print(
            _refusal(
                f"render failed: {type(exc).__name__}: {exc}",
                _RENDER_FIX,
                command,
            ),
            file=sys.stderr,
        )
        return RenderOutcome(inputs=inputs)

    output = Path(rendered_dir)
    try:
        previous = _read_manifest(io, output / _MANIFEST_NAME)
        classifications, stale = _current_classification(
            io, output, rendered, previous
        )
        regular_files = _regular_files(io, output)
    except (OSError, TypeError, ValueError) as exc:
        print(_refusal(exc, _RENDER_FIX, command), file=sys.stderr)
        return RenderOutcome(rendered=rendered, inputs=inputs)
    # A registered artifact's file that this render did not produce (an artifact
    # that no longer applies — the GPU board after --no-gpu) is stale, never
    # foreign: leaving the mode removes it through the one removal path.
    registered_paths = {artifact.relative_path for artifact in ARTIFACTS}
    stale.update(registered_paths.intersection(regular_files).difference(rendered.by_path))
    foreign = regular_files.difference(registered_paths, set(previous))
    foreign.difference_update({_MANIFEST_NAME, _APPLIED_NAME})
    if foreign:
        names = ", ".join(sorted(foreign))
        print(
            _refusal(
                f"foreign file(s) under rendered: {names}",
                _FOREIGN_FIX,
                command,
            ),
            file=sys.stderr,
        )
        return RenderOutcome(
            rendered=rendered,
            inputs=inputs,
            classifications=classifications,
            stale=stale,
        )

    changes = {relative: change for relative, change, _ in classifications}
    if diff:
        try:
            applied = read_applied_manifest(io, output / _APPLIED_NAME)
        except (TypeError, ValueError) as exc:
            print(_refusal(exc, _RENDER_FIX, command), file=sys.stderr)
            return RenderOutcome(
                rendered=rendered,
                inputs=inputs,
                classifications=classifications,
                stale=stale,
            )
        digests = compose_digests(inputs)
        judgment = recreate_judgment(
            rendered, applied, service_names(inputs), digests
        )
        _print_diff(output, rendered, classifications, judgment.services, stale)
        return RenderOutcome(
            rendered=rendered,
            inputs=inputs,
            classifications=classifications,
            stale=stale,
            exit_code=0,
        )

    try:
        io.mkdir(output, mode=0o755, parents=True, exist_ok=True)
        for rendered_file in rendered.files:
            target = output / rendered_file.relative_path
            io.mkdir(target.parent, mode=0o755, parents=True, exist_ok=True)
            if changes[rendered_file.relative_path] is not FileChange.UNCHANGED:
                io.write_text(target, rendered_file.content, mode=rendered_file.mode)
        for relative_path in sorted(stale):
            io.unlink(output / relative_path, missing_ok=True)
        manifest = manifest_document(
            rendered,
            inputs,
            site_text=site_text,
            lock_text=lock_text,
            models_lock_text=models_lock_text,
        )
        io.write_text(output / _MANIFEST_NAME, manifest, mode=0o644)
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            _refusal(
                f"could not update rendered output: {exc}", _RENDER_FIX, command
            ),
            file=sys.stderr,
        )
        return RenderOutcome(
            rendered=rendered,
            inputs=inputs,
            classifications=classifications,
            stale=stale,
        )

    if command == "render":
        for relative_path, change, _ in classifications:
            print(f"{relative_path}: {change.value}")
        for relative_path in sorted(stale):
            print(f"{relative_path}: {FileChange.STALE.value}")
        _print_summary(classifications, stale)
    return RenderOutcome(
        rendered=rendered,
        inputs=inputs,
        classifications=classifications,
        stale=stale,
        exit_code=0,
    )


def run_render(
    args: object,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = _RENDERED_DIR,
    site_path: PathLike = _SITE_PATH,
    lock_path: PathLike | None = None,
    images_path: PathLike | None = None,
    models_path: PathLike | None = None,
    root: PathLike | None = None,
) -> int:
    """Render configuration, classify it, and optionally write the result."""

    io = host or RealHost()
    if io.geteuid() != 0:
        print(_refusal("root is required", _ROOT_FIX), file=sys.stderr)
        return 1

    checkout = Path(__file__).parents[3] if root is None else Path(root)
    actual_lock = checkout / "host.lock" if lock_path is None else lock_path
    actual_images = checkout / "images.lock" if images_path is None else images_path
    actual_models = checkout / "models.lock" if models_path is None else models_path
    return render_to_disk(
        io,
        rendered_dir=rendered_dir,
        site_path=site_path,
        lock_path=actual_lock,
        images_path=actual_images,
        models_path=actual_models,
        root=checkout,
        diff=bool(getattr(args, "diff", False)),
        command="render",
    ).exit_code

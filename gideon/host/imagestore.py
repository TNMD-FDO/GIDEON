"""Prune superseded GIDEON image references from the shared Docker daemon."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gideon.host import report
from gideon.host.images import BuiltImagePin, ImagePin, RegistryTarget
from gideon.host.report import command_detail
from gideon.host.sysio import Host


@dataclass(frozen=True, slots=True)
class ImageRow:
    """One image reference reported by the daemon."""

    repository: str
    tag: str | None
    digest: str
    id: str
    size: str


@dataclass(frozen=True, slots=True)
class ContainerRow:
    """A container's names and the image reference it holds."""

    names: str
    image: str


@dataclass(frozen=True, slots=True)
class Removal:
    """A superseded image's digest reference, its size, and its build tag."""

    reference: str
    size: str
    tag_reference: str | None


@dataclass(frozen=True, slots=True)
class InUse:
    """A superseded reference kept for the containers that hold it."""

    reference: str
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """The ordered removals and references kept for containers."""

    removals: tuple[Removal, ...]
    in_use: tuple[InUse, ...]


@dataclass(frozen=True, slots=True)
class PruneReport:
    """The references removed or kept, and any refusal with its fix."""

    removed: tuple[Removal, ...] = ()
    kept_in_use: tuple[InUse, ...] = ()
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


def repository(target: RegistryTarget, name: str) -> str:
    return f"{target.authority}/{target.prefix}{name}"


def list_argv(target: RegistryTarget, name: str) -> tuple[str, ...]:
    return (
        "docker", "image", "ls", "--digests", "--format", "{{json .}}",
        "--filter", f"reference={repository(target, name)}",
    )


def containers_argv() -> tuple[str, ...]:
    return ("docker", "ps", "--all", "--no-trunc", "--format", "{{json .}}")


def remove_argv(reference: str) -> tuple[str, ...]:
    return ("docker", "image", "rm", reference)


def _parse_rows(
    stdout: str, fields: tuple[str, ...], label: str,
) -> tuple[dict[str, str], ...] | str:
    rows: list[dict[str, str]] = []
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return f"malformed {label} row at line {number}: expected a JSON object"
        if not isinstance(item, dict) or any(
            not isinstance(item.get(field), str) or not item[field] for field in fields
        ):
            return (
                f"malformed {label} row at line {number}: expected a JSON object "
                f"with non-empty string fields {', '.join(fields)}"
            )
        rows.append({field: item[field] for field in fields})
    return tuple(rows)


def parse_images(stdout: str) -> tuple[ImageRow, ...] | str:
    rows = _parse_rows(stdout, ("Repository", "Tag", "Digest", "ID", "Size"), "image")
    if isinstance(rows, str):
        return rows
    return tuple(
        ImageRow(
            row["Repository"], None if row["Tag"] == "<none>" else row["Tag"],
            row["Digest"], row["ID"], row["Size"],
        )
        for row in rows
    )


def parse_containers(stdout: str) -> tuple[ContainerRow, ...] | str:
    rows = _parse_rows(stdout, ("Names", "Image"), "container")
    if isinstance(rows, str):
        return rows
    return tuple(ContainerRow(row["Names"], row["Image"]) for row in rows)


def _uses(container: ContainerRow, row: ImageRow) -> bool:
    if container.image == f"{row.repository}@{row.digest}":
        return True
    if row.tag is not None and container.image == f"{row.repository}:{row.tag}":
        return True
    image_id = row.id.removeprefix("sha256:")
    return bool(image_id) and container.image.removeprefix("sha256:").startswith(image_id)


def prune_plan(
    pins: Sequence[ImagePin],
    rows_by_name: Mapping[str, Sequence[ImageRow]],
    containers: Sequence[ContainerRow],
    *,
    target: RegistryTarget,
) -> PrunePlan:
    """Keep the lock's digests and container-held images within each exact repository."""

    removals: list[Removal] = []
    in_use: list[InUse] = []
    for pin in pins:
        expected = repository(target, pin.name)
        keep = set() if isinstance(pin, BuiltImagePin) and pin.digest == "unbuilt" else {pin.digest}
        if isinstance(pin, BuiltImagePin):
            keep.add(pin.base_digest)
        candidates: dict[str, list[ImageRow]] = {}
        for row in rows_by_name.get(pin.name, ()):
            if row.repository != expected or row.digest == "<none>" or row.digest in keep:
                continue
            candidates.setdefault(f"{expected}@{row.digest}", []).append(row)
        for reference, rows in candidates.items():
            names = tuple(dict.fromkeys(
                container.names for container in containers
                if any(_uses(container, row) for row in rows)
            ))
            if names:
                in_use.append(InUse(reference, names))
                continue
            row = next((row for row in rows if row.tag is not None), rows[0])
            tag_reference = f"{expected}:{row.tag}" if row.tag is not None else None
            removals.append(Removal(reference, row.size, tag_reference))
    return PrunePlan(tuple(removals), tuple(in_use))


def _fix() -> str:
    return f"Repair Docker access or the reported image-store problem, then re-run {report.command('apply')}."


def _docker(io: Host, argv: tuple[str, ...]) -> str | report.Problem:
    try:
        result = io.run(argv)
    except OSError as exc:
        return report.Problem(f"{' '.join(argv)} could not run: {exc}", _fix())
    if result.returncode != 0:
        return report.Problem(
            f"{' '.join(argv)} failed (exit {result.returncode}): {command_detail(result)}",
            _fix(),
        )
    return result.stdout


def _list_images(
    io: Host, target: RegistryTarget, name: str,
) -> tuple[ImageRow, ...] | report.Problem:
    result = _docker(io, list_argv(target, name))
    if isinstance(result, report.Problem):
        return result
    rows = parse_images(result)
    if isinstance(rows, str):
        return report.Problem(f"{repository(target, name)}: {rows}", _fix())
    return rows


def prune(io: Host, target: RegistryTarget, pins: Sequence[ImagePin]) -> PruneReport:
    """List all lock repositories before removing any superseded, unused reference."""

    removed: list[Removal] = []
    kept: tuple[InUse, ...] = ()

    def refuse(problem: report.Problem, *, untagged: str | None = None) -> PruneReport:
        made = [detail(PruneReport(tuple(removed), kept))] if removed or kept else []
        if untagged is not None:
            made.append(f"untagged {untagged}")
        message = "; ".join((*made, problem.problem))
        return PruneReport(tuple(removed), kept, message, problem.fix)

    rows_by_name: dict[str, tuple[ImageRow, ...]] = {}
    for pin in pins:
        rows = _list_images(io, target, pin.name)
        if isinstance(rows, report.Problem):
            return refuse(rows)
        rows_by_name[pin.name] = rows
    plan = prune_plan(pins, rows_by_name, (), target=target)
    if not plan.removals:
        return PruneReport()
    result = _docker(io, containers_argv())
    if isinstance(result, report.Problem):
        return refuse(result)
    containers = parse_containers(result)
    if isinstance(containers, str):
        return refuse(report.Problem(containers, _fix()))
    plan = prune_plan(pins, rows_by_name, containers, target=target)
    kept = plan.in_use
    names_by_repository = {repository(target, pin.name): pin.name for pin in pins}
    for removal in plan.removals:
        if removal.tag_reference is not None:
            # The tag goes first, so a refusal at either step leaves a row that
            # still carries the digest for the next apply to retry; a store
            # that drops the digest with the tag leaves nothing to remove.
            result = _docker(io, remove_argv(removal.tag_reference))
            if isinstance(result, report.Problem):
                return refuse(result)
            image_repository, digest = removal.reference.rsplit("@", 1)
            rows = _list_images(io, target, names_by_repository[image_repository])
            if isinstance(rows, report.Problem):
                return refuse(rows, untagged=removal.tag_reference)
            if not any(
                row.repository == image_repository and row.digest == digest for row in rows
            ):
                removed.append(removal)
                continue
        result = _docker(io, remove_argv(removal.reference))
        if isinstance(result, report.Problem):
            return refuse(result, untagged=removal.tag_reference)
        removed.append(removal)
    return PruneReport(tuple(removed), kept)


def detail(result: PruneReport) -> str:
    parts: list[str] = []
    if result.removed:
        parts.append("removed " + ", ".join(
            f"{item.reference}{f' and {item.tag_reference}' if item.tag_reference else ''}"
            f" ({item.size})"
            for item in result.removed
        ))
    if result.kept_in_use:
        parts.append("kept in use " + ", ".join(
            f"{item.reference} by {', '.join(item.names)}" for item in result.kept_in_use
        ))
    return "; ".join(parts) or "nothing to remove"

"""The ordered set of upstream pins maintained by the pin watch."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast

from gideon.host import models, weights
from gideon.host.images import (
    AptWatch,
    ImageLock,
    MirroredImagePin,
    PypiWatch,
)
from gideon.host.images import (
    BuiltImagePin as LockedBuiltImagePin,
)
from gideon.host.lock import HostLock
from gideon.host.models import (
    ModelPin as LockedModelPin,
)
from tools.pinwatch import hub, pypi
from tools.pinwatch.fetch import Fetcher, FetchError
from tools.pinwatch.oci import (
    Reference,
    created_time,
    image_created,
    leading_component_changed,
    list_tags,
    newest_same_shape,
    newest_same_shape_candidates,
    parse_reference,
    resolve_digest,
    tag_shape,
)
from tools.pinwatch.paths import PROVENANCE_PATH, LockName
from tools.pinwatch.skills import MATT_SOURCE, Provenance
from tools.pinwatch.sources import (
    apt_package_versions,
    debian_version_key,
    github_branch_head,
    github_latest_release,
    newest,
    nvidia_branches,
    runner_sha256,
    ubuntu_cloud_image,
    version_tuple,
)


@dataclass(frozen=True, slots=True)
class Change:
    """One scalar lock value changed by a bump."""

    key_path: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Block:
    """One mapping-valued lock block changed by a bump."""

    key_path: str
    old: Mapping[str, object]
    new: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Bump:
    """A complete, validated change for one lock-file pin."""

    pin_id: str
    lock: LockName
    changes: tuple[Change, ...]
    upstream_url: str
    proposal: bool
    blocks: tuple[Block, ...] = ()


@dataclass(frozen=True, slots=True)
class Pin:
    """A resolved lock value and the source that can move it."""

    id: str
    lock: LockName

    def current(self) -> str:
        """Return the value shown in a status row."""

        raise NotImplementedError

    @property
    def key_paths(self) -> tuple[str, ...]:
        """Return the lock paths owned by this pin."""

        raise NotImplementedError

    @property
    def note_paths(self) -> tuple[str, ...]:
        """Return the lock paths the release note compares.

        These are the owned paths unless a pin records a value the watch never writes.
        """

        return self.key_paths

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        """Resolve this pin and return a bump when upstream has moved."""

        raise NotImplementedError


def _release_page(repository: str, tag: str) -> str:
    return f"https://github.com/{repository}/releases/tag/{tag}"


def _version_without_v(tag: str) -> str:
    return tag.removeprefix("v")


def _source_with_tag(source: str, tag: str) -> str:
    return f"{source.rsplit(':', 1)[0]}:{tag}"


def _image_upstream_url(reference: Reference) -> str:
    registry = reference.registry
    repository = reference.repository
    if registry == "registry-1.docker.io":
        if repository.startswith("library/"):
            return f"https://hub.docker.com/_/{repository.removeprefix('library/')}/tags"
        return f"https://hub.docker.com/r/{repository}/tags"
    if registry == "ghcr.io":
        parts = repository.split("/")
        if len(parts) >= 2:
            owner, project, name = parts[0], parts[1], parts[-1]
            return f"https://github.com/{owner}/{project}/pkgs/container/{name}"
    return f"https://{registry}/{repository}"


def _newest_image_tag(
    fetcher: Fetcher, reference: Reference, tags: tuple[str, ...]
) -> str:
    """Choose a tag, breaking SearXNG's same-date ties from image metadata.

    The dated-tag rule and the registry-recorded tie-break are those of the
    research note verified against the SearXNG pin.
    """

    candidates = newest_same_shape_candidates(tags, reference.tag)
    if not candidates:
        return reference.tag
    current_shape = tag_shape(reference.tag)
    candidate_shape = tag_shape(candidates[0])
    same_day_as_pin = (
        current_shape is not None
        and candidate_shape is not None
        and candidate_shape.components == current_shape.components
    )
    if len(candidates) == 1 and not same_day_as_pin:
        return candidates[0]

    # Several builds share the newest date, or a build shares the pin's own
    # date: the pin joins the comparison in the second case, so a hash built
    # earlier than the pinned one never replaces it.
    compared = (*candidates, reference.tag) if same_day_as_pin else candidates
    created: dict[str, datetime] = {}
    try:
        for candidate in compared:
            created[candidate] = created_time(
                image_created(fetcher, reference, candidate)
            )
    except FetchError as error:
        names = ", ".join(compared)
        raise FetchError(
            error.url,
            f"same-date tags {names} tie could not be broken: {error.reason}",
        ) from None

    latest = max(created.values())
    tied = tuple(tag for tag in compared if created[tag] == latest)
    if len(tied) > 1:
        names = ", ".join(tied)
        url = f"https://{reference.registry}/v2/{reference.repository}/manifests/{tied[0]}"
        raise FetchError(url, f"same-date tags {names} tie could not be broken")
    return tied[0]


@dataclass(frozen=True, slots=True)
class ImagePin(Pin):
    """A registry-tag and OCI-index digest from ``images.lock``."""

    image: MirroredImagePin

    @property
    def key_paths(self) -> tuple[str, ...]:
        prefix = f"images.{self.image.name}"
        return (f"{prefix}.source", f"{prefix}.digest")

    def current(self) -> str:
        return f"{self.image.source}@{self.image.digest}"

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        reference = parse_reference(self.image.source)
        tags = list_tags(fetcher, reference)
        tag = _newest_image_tag(fetcher, reference, tags)
        digest = resolve_digest(fetcher, reference, tag)
        source = _source_with_tag(self.image.source, tag)
        changes: list[Change] = []
        if source != self.image.source:
            changes.append(
                Change(f"images.{self.image.name}.source", self.image.source, source)
            )
        if digest != self.image.digest:
            changes.append(
                Change(f"images.{self.image.name}.digest", self.image.digest, digest)
            )
        if not changes:
            return None
        return Bump(
            self.id,
            self.lock,
            tuple(changes),
            _image_upstream_url(reference),
            leading_component_changed(reference.tag, tag),
        )


@dataclass(frozen=True, slots=True)
class BuiltImagePin(Pin):
    """The base digest watched for a built image pin."""

    image: LockedBuiltImagePin

    @property
    def key_paths(self) -> tuple[str, ...]:
        prefix = f"images.{self.image.name}"
        return (f"{prefix}.base", f"{prefix}.base_digest")

    @property
    def note_paths(self) -> tuple[str, ...]:
        """Add the pushed digest after the base, so a moved base tag is named first.

        `inputs_digest` stays out: a rebuild that pushed the same digest pulls nothing.
        """

        return (*self.key_paths, f"images.{self.image.name}.digest")

    def current(self) -> str:
        return f"{self.image.base}@{self.image.base_digest}"

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        reference = parse_reference(self.image.base)
        tags = list_tags(fetcher, reference)
        tag = newest_same_shape(tags, reference.tag)
        digest = resolve_digest(fetcher, reference, tag)
        base = _source_with_tag(self.image.base, tag)
        changes: list[Change] = []
        if base != self.image.base:
            changes.append(
                Change(f"images.{self.image.name}.base", self.image.base, base)
            )
        if digest != self.image.base_digest:
            changes.append(
                Change(
                    f"images.{self.image.name}.base_digest",
                    self.image.base_digest,
                    digest,
                )
            )
        if not changes:
            return None
        return Bump(
            self.id,
            self.lock,
            tuple(changes),
            _image_upstream_url(reference),
            True,
        )


@dataclass(frozen=True, slots=True)
class AptPackagePin(Pin):
    """A watched Debian package version used as a built-image argument."""

    image: LockedBuiltImagePin
    argument: str

    @property
    def key_paths(self) -> tuple[str, ...]:
        return (f"images.{self.image.name}.build_args.{self.argument}",)

    def current(self) -> str:
        return self.image.build_args[self.argument]

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        watch = self.image.watch[self.argument]
        if not isinstance(watch, AptWatch):
            raise ValueError(f"{self.id} is not watched at an apt index")
        versions = apt_package_versions(fetcher, watch.apt_index, watch.package)
        current = self.current()
        candidate = newest(versions)
        if debian_version_key(candidate) <= debian_version_key(current):
            return None
        return Bump(
            self.id,
            self.lock,
            (
                Change(
                    f"images.{self.image.name}.build_args.{self.argument}",
                    current,
                    candidate,
                ),
            ),
            watch.apt_index,
            True,
        )


@dataclass(frozen=True, slots=True)
class PypiProjectPin(Pin):
    """A watched PyPI project release used as a built-image argument."""

    image: LockedBuiltImagePin
    argument: str

    @property
    def key_paths(self) -> tuple[str, ...]:
        return (f"images.{self.image.name}.build_args.{self.argument}",)

    def current(self) -> str:
        return self.image.build_args[self.argument]

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        watch = self.image.watch[self.argument]
        if not isinstance(watch, PypiWatch):
            raise ValueError(f"{self.id} is not watched on PyPI")
        current = self.current()
        candidate = pypi.pypi_version(fetcher, watch.project, current)
        if candidate is None:
            return None
        return Bump(
            self.id,
            self.lock,
            (
                Change(
                    f"images.{self.image.name}.build_args.{self.argument}",
                    current,
                    candidate,
                ),
            ),
            pypi.project_page(watch.project, candidate),
            True,
        )


@dataclass(frozen=True, slots=True)
class RegistryImagePin(Pin):
    """The release registry image stored as one digest-pinned value."""

    image: str

    @property
    def key_paths(self) -> tuple[str, ...]:
        return ("registry_image",)

    def current(self) -> str:
        return self.image

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        reference = parse_reference(self.image)
        tags = list_tags(fetcher, reference)
        tag = _newest_image_tag(fetcher, reference, tags)
        digest = resolve_digest(fetcher, reference, tag)
        source = self.image.split("@", 1)[0]
        new_image = f"{source.rsplit(':', 1)[0]}:{tag}@{digest}"
        if new_image == self.image:
            return None
        return Bump(
            self.id,
            self.lock,
            (Change("registry_image", self.image, new_image),),
            _image_upstream_url(reference),
            leading_component_changed(reference.tag, tag),
        )


@dataclass(frozen=True, slots=True)
class GithubReleasePin(Pin):
    """The GitHub Actions runner version and release-notes checksum."""

    version: str
    sha256: str
    repository: str = "actions/runner"

    @property
    def key_paths(self) -> tuple[str, ...]:
        return ("gh_runner.version", "gh_runner.sha256")

    def current(self) -> str:
        return f"{self.version} ({self.sha256})"

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        release = github_latest_release(fetcher, self.repository)
        new_version = version_tuple(release.tag, r"^v")
        current_version = version_tuple(self.version, "")
        if new_version is None or current_version is None:
            raise FetchError(
                _release_page(self.repository, release.tag),
                f"release tag is not a numeric version: {release.tag}",
            )
        if new_version <= current_version:
            return None
        sha256 = runner_sha256(release.body)
        release_url = _release_page(self.repository, release.tag)
        if sha256 is None:
            raise FetchError(release_url, "release body has no linux-x64 SHA marker")
        return Bump(
            self.id,
            self.lock,
            (
                Change(
                    "gh_runner.version", self.version, _version_without_v(release.tag)
                ),
                Change("gh_runner.sha256", self.sha256, sha256),
            ),
            release_url,
            False,
        )


@dataclass(frozen=True, slots=True)
class SkillPin(Pin):
    """A skill-source record: a proposal a person completes on its branch."""

    def upstream_url(self, value: str) -> str:
        """The upstream page for one recorded value (a tag or a commit)."""

        raise NotImplementedError

    def branch_bump(self, bump: Bump, values: tuple[str, ...]) -> Bump:
        """The bump a human-owned branch holds: *values* as the new values, and
        the upstream page of the first, so a retitled body names what the branch
        holds rather than what upstream published since."""

        return replace(
            bump,
            changes=tuple(
                replace(change, new=value)
                for change, value in zip(bump.changes, values, strict=True)
            ),
            upstream_url=self.upstream_url(values[0]),
        )


@dataclass(frozen=True, slots=True)
class SkillBranchPin(SkillPin):
    """The upstream ``main`` commit and date in the provenance record."""

    commit: str
    date: str
    repository: str = MATT_SOURCE

    @property
    def key_paths(self) -> tuple[str, ...]:
        return ("matt-pocock.commit", "matt-pocock.date")

    def current(self) -> str:
        return f"main@{self.commit} ({self.date})"

    def upstream_url(self, value: str) -> str:
        # GitHub resolves an abbreviated sha here, so the branch's short form works too.
        return f"https://github.com/{self.repository}/commit/{value}"

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        head = github_branch_head(fetcher, self.repository, "main")
        if head.sha.startswith(self.commit):
            return None
        return Bump(
            self.id,
            self.lock,
            (
                Change("matt-pocock.commit", self.commit, head.sha[:7]),
                Change("matt-pocock.date", self.date, head.date[:10]),
            ),
            self.upstream_url(head.sha),
            True,
        )


@dataclass(frozen=True, slots=True)
class MajorFloorPin(Pin):
    """A Docker or Compose major-version floor, watched as a proposal."""

    value: int
    repository: str
    prefix_pattern: str
    key_path: str

    @property
    def key_paths(self) -> tuple[str, ...]:
        return (self.key_path,)

    def current(self) -> str:
        return str(self.value)

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        release = github_latest_release(fetcher, self.repository)
        parsed = version_tuple(release.tag, self.prefix_pattern)
        if parsed is None:
            raise FetchError(
                _release_page(self.repository, release.tag),
                f"release tag is not a numeric version: {release.tag}",
            )
        major = parsed[0]
        if major <= self.value:
            return None
        return Bump(
            self.id,
            self.lock,
            (Change(self.key_path, str(self.value), str(major)),),
            _release_page(self.repository, release.tag),
            True,
        )


@dataclass(frozen=True, slots=True)
class VersionFloorPin(Pin):
    """A full NVIDIA Container Toolkit version floor, watched as a proposal."""

    value: str
    repository: str
    prefix_pattern: str = r"^v"
    key_path: str = "minimums.toolkit"

    @property
    def key_paths(self) -> tuple[str, ...]:
        return (self.key_path,)

    def current(self) -> str:
        return self.value

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        release = github_latest_release(fetcher, self.repository)
        parsed = version_tuple(release.tag, self.prefix_pattern)
        current = version_tuple(self.value, "")
        if parsed is None or current is None:
            raise FetchError(
                _release_page(self.repository, release.tag),
                f"release tag is not a numeric version: {release.tag}",
            )
        if parsed <= current:
            return None
        return Bump(
            self.id,
            self.lock,
            (Change(self.key_path, self.value, _version_without_v(release.tag)),),
            _release_page(self.repository, release.tag),
            True,
        )


@dataclass(frozen=True, slots=True)
class DriverBranchPin(Pin):
    """The greatest NVIDIA driver branch in the configured apt index."""

    value: str
    repository: str
    key_path: str = "driver.branch"

    @property
    def key_paths(self) -> tuple[str, ...]:
        return (self.key_path,)

    def current(self) -> str:
        return self.value

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        branches = nvidia_branches(fetcher, self.repository)
        current = int(self.value)
        if max(branches) <= current:
            return None
        branch = str(max(branches))
        return Bump(
            self.id,
            self.lock,
            (Change(self.key_path, self.value, branch),),
            f"https://developer.download.nvidia.com/compute/cuda/repos/{self.repository}/Packages",
            True,
        )


@dataclass(frozen=True, slots=True)
class CloudImagePin(Pin):
    """The newest dated Ubuntu acceptance image and its checksum."""

    url: str
    sha256: str

    @property
    def key_paths(self) -> tuple[str, ...]:
        return ("acceptance_vm_image.url", "acceptance_vm_image.sha256")

    def current(self) -> str:
        return f"{self.url} ({self.sha256})"

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        image = ubuntu_cloud_image(fetcher, self.url)
        if image is None:
            return None
        sums_url = f"{image.url.rsplit('/', 1)[0]}/SHA256SUMS"
        return Bump(
            self.id,
            self.lock,
            (
                Change("acceptance_vm_image.url", self.url, image.url),
                Change("acceptance_vm_image.sha256", self.sha256, image.sha256),
            ),
            sums_url,
            False,
        )


@dataclass(frozen=True, slots=True)
class ModelPin(Pin):
    """A Hugging Face model revision and its profile-specific file record."""

    profile: str
    model: LockedModelPin
    memory_rows: tuple[models.MemoryRow, ...]

    @property
    def key_paths(self) -> tuple[str, ...]:
        prefix = f"profiles.{self.profile}.models.{self.model.role}"
        return (f"{prefix}.repo", f"{prefix}.revision", f"{prefix}.files")

    def current(self) -> str:
        return f"{self.model.repo}@{self.model.revision}"

    def resolve(self, fetcher: Fetcher) -> Bump | None:
        head = hub.model_head(fetcher, self.model.repo)
        if head == self.model.revision:
            return None
        files = hub.record_files(fetcher, self.model.repo, head)
        prefix = f"profiles.{self.profile}.models.{self.model.role}"
        old = {
            pinned.path: {"sha256": pinned.sha256, "size": pinned.size}
            for pinned in self.model.files
        }
        old_size = sum(pinned.size for pinned in self.model.files)
        new_size = sum(cast(int, value["size"]) for value in files.values())
        return Bump(
            self.id,
            self.lock,
            (Change(f"{prefix}.revision", self.model.revision, head),),
            f"{weights.HF_RESOLVE_ROOT}/{self.model.repo}/tree/{head}",
            new_size != old_size and bool(self.memory_rows),
            (Block(f"{prefix}.files", old, files),),
        )


def pin_registry(
    image_lock: ImageLock,
    host_lock: HostLock,
    models_lock: models.ModelsLock,
    provenance: Provenance,
) -> tuple[Pin, ...]:
    """Build the ordered pin registry from the four loaded records.

    The order is the plan's pins table: images in lock order, with each built
    image followed by its watched apt and PyPI arguments, then the host pins,
    the models, and the Matt Pocock skills' record.
    """

    pins: list[Pin] = []
    for image in image_lock.images:
        if isinstance(image, MirroredImagePin):
            pins.append(ImagePin(f"images.{image.name}", "images.lock", image))
        elif isinstance(image, LockedBuiltImagePin):
            pins.append(BuiltImagePin(f"images.{image.name}", "images.lock", image))
            for argument, watch in image.watch.items():
                pin_id = f"images.{image.name}.build_args.{argument}"
                if isinstance(watch, AptWatch):
                    pins.append(AptPackagePin(pin_id, "images.lock", image, argument))
                elif isinstance(watch, PypiWatch):
                    pins.append(PypiProjectPin(pin_id, "images.lock", image, argument))
    pins.extend(
        (
            RegistryImagePin(
                "host.registry_image", "host.lock", image=host_lock.registry_image
            ),
            GithubReleasePin(
                "host.gh_runner",
                "host.lock",
                version=host_lock.gh_runner.version,
                sha256=host_lock.gh_runner.sha256,
            ),
            MajorFloorPin(
                "host.minimums.docker",
                "host.lock",
                value=host_lock.minimums.docker,
                repository="moby/moby",
                prefix_pattern=r"^docker-v",
                key_path="minimums.docker",
            ),
            MajorFloorPin(
                "host.minimums.compose",
                "host.lock",
                value=host_lock.minimums.compose,
                repository="docker/compose",
                prefix_pattern=r"^v",
                key_path="minimums.compose",
            ),
            VersionFloorPin(
                "host.minimums.toolkit",
                "host.lock",
                value=host_lock.minimums.toolkit,
                repository="NVIDIA/nvidia-container-toolkit",
            ),
            DriverBranchPin(
                "host.driver.branch",
                "host.lock",
                value=host_lock.driver.branch,
                repository=host_lock.driver.repo,
            ),
            CloudImagePin(
                "host.acceptance_vm_image",
                "host.lock",
                url=host_lock.acceptance_vm_image.url,
                sha256=host_lock.acceptance_vm_image.sha256,
            ),
            *(
                ModelPin(
                    (
                        f"models.{model.role}"
                        if profile.name == models_lock.reference
                        else f"models.{profile.name}.{model.role}"
                    ),
                    "models.lock",
                    profile.name,
                    model,
                    tuple(row for row in profile.memory if row.role == model.role),
                )
                for profile in models_lock.profiles
                for model in profile.models
            ),
            SkillBranchPin(
                "skills.matt-pocock",
                PROVENANCE_PATH,
                commit=provenance.matt_commit,
                date=provenance.matt_date,
            ),
        )
    )
    return tuple(pins)

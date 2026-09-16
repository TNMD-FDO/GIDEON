"""The pure render core and its ordered artifact registry."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from string import Template

from gideon.host.images import ImageLock
from gideon.host.lock import HostLock
from gideon.host.models import HardwareProfile
from gideon.host.render.facts import HostFacts
from gideon.host.site import SiteConfig


@dataclass(frozen=True, slots=True)
class RenderInputs:
    """All inputs available to pure render artifact emitters."""

    site: SiteConfig
    lock: HostLock
    images: ImageLock
    facts: HostFacts
    profile: HardwareProfile
    templates: Mapping[str, str]
    release: str
    secrets: Mapping[str, str] = field(default_factory=dict)
    checkout: str = ""
    no_gpu: bool = False
    build_box: bool = False


class Artifact:
    """Base class for one deterministic rendered file."""

    name: str = ""
    relative_path: str = ""
    mode: int = 0o644
    owners: tuple[str, ...] = ()
    template_paths: tuple[str, ...] = ()
    secret: bool = False

    def applies(self, inputs: RenderInputs) -> bool:
        """Whether this artifact belongs in the rendered output."""

        del inputs
        return True

    def emit(self, inputs: RenderInputs) -> str:
        raise NotImplementedError

    def secret_names(self, inputs: RenderInputs) -> tuple[str, ...]:
        """Secret names this artifact's ``emit`` reads from ``inputs.secrets``."""

        del inputs
        return ()


class VerbatimArtifact(Artifact):
    """An artifact whose one template is emitted without substitution."""

    def __init__(
        self,
        *,
        name: str,
        relative_path: str,
        template_path: str,
        owners: tuple[str, ...] = (),
        mode: int = 0o644,
        applies: Callable[[RenderInputs], bool] | None = None,
    ) -> None:
        self.name = name
        self.relative_path = relative_path
        self.template_paths = (template_path,)
        self.owners = owners
        self.mode = mode
        self._applies = applies

    def applies(self, inputs: RenderInputs) -> bool:
        """Apply the optional construction-time applicability predicate."""

        return self._applies is None or self._applies(inputs)

    def emit(self, inputs: RenderInputs) -> str:
        return template_text(inputs, self.template_paths[0])


def template_text(inputs: RenderInputs, path: str) -> str:
    """The declared template's text, or a ``ValueError`` naming the missing file."""

    try:
        return inputs.templates[path]
    except KeyError as exc:
        raise ValueError(f"Render template {path} is missing.") from exc


def substitute_template(inputs: RenderInputs, path: str, values: Mapping[str, object]) -> str:
    """Fill a ``string.Template`` from the declared templates; an unfilled placeholder is a ``ValueError``."""

    try:
        return Template(template_text(inputs, path)).substitute(values)
    except KeyError as exc:
        placeholder = exc.args[0] if exc.args else "unknown"
        raise ValueError(f"Render template {path} has an unfilled placeholder: {placeholder}.") from exc


@dataclass(frozen=True, slots=True)
class RenderedFile:
    """One emitted file and the services that own its contents."""

    relative_path: str
    content: str
    mode: int
    owners: tuple[str, ...]
    secret: bool = False


@dataclass(frozen=True, slots=True)
class RenderedSet:
    """The ordered rendered files and an index by relative path."""

    files: tuple[RenderedFile, ...]
    by_path: Mapping[str, RenderedFile] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "by_path",
            {rendered.relative_path: rendered for rendered in self.files},
        )


def render_all(inputs: RenderInputs) -> RenderedSet:
    """Emit every registered artifact without performing host I/O."""

    files = tuple(
        RenderedFile(
            relative_path=artifact.relative_path,
            content=artifact.emit(inputs),
            mode=artifact.mode,
            owners=artifact.owners,
            secret=artifact.secret,
        )
        for artifact in ARTIFACTS
        if artifact.applies(inputs)
    )
    return RenderedSet(files)


def _registered_artifacts() -> tuple[Artifact, ...]:
    from gideon.host.render.caddy import CaddyfileArtifact
    from gideon.host.render.compose import ComposeArtifact
    from gideon.host.render.grafana import (
        GrafanaBackupArtifact,
        GrafanaContactPointsArtifact,
        GrafanaDashboardsProviderArtifact,
        GrafanaDatasourcesArtifact,
        GrafanaGpuArtifact,
        GrafanaLdapArtifact,
        GrafanaOverviewArtifact,
        GrafanaPoliciesArtifact,
        GrafanaRulesArtifact,
        GrafanaTimeIntervalsArtifact,
    )
    from gideon.host.render.owui import ApplyManifestArtifact, OwuiEnvArtifact
    from gideon.host.render.pgbackrest import PgBackRestConfArtifact
    from gideon.host.render.prometheus import (
        BlackboxConfigArtifact,
        PrometheusConfigArtifact,
    )
    from gideon.host.render.searxng import (
        SearxngEnvArtifact,
        SearxngLoggingArtifact,
        SearxngSettingsArtifact,
    )
    from gideon.host.render.systemd import (
        BackupServiceArtifact,
        BackupTimerArtifact,
        DrillServiceArtifact,
        DrillTimerArtifact,
        ReconcileServiceArtifact,
        ReconcileTimerArtifact,
        VerifyServiceArtifact,
        VerifyTimerArtifact,
    )

    return (
        ComposeArtifact(),
        PrometheusConfigArtifact(),
        BlackboxConfigArtifact(),
        GrafanaLdapArtifact(),
        GrafanaDatasourcesArtifact(),
        GrafanaDashboardsProviderArtifact(),
        GrafanaContactPointsArtifact(),
        GrafanaPoliciesArtifact(),
        GrafanaTimeIntervalsArtifact(),
        GrafanaRulesArtifact(),
        GrafanaOverviewArtifact,
        GrafanaBackupArtifact,
        GrafanaGpuArtifact,
        PgBackRestConfArtifact(),
        CaddyfileArtifact(),
        OwuiEnvArtifact(),
        ApplyManifestArtifact(),
        SearxngSettingsArtifact(),
        SearxngEnvArtifact(),
        SearxngLoggingArtifact(),
        ReconcileServiceArtifact(),
        ReconcileTimerArtifact(),
        BackupServiceArtifact(),
        BackupTimerArtifact(),
        DrillServiceArtifact(),
        DrillTimerArtifact(),
        VerifyServiceArtifact(),
        VerifyTimerArtifact(),
    )


ARTIFACTS: list[Artifact] = list(_registered_artifacts())

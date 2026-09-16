"""Pure Caddyfile rendering."""

from string import Template

from gideon.host.render import Artifact, RenderInputs

_TEMPLATE_PATH = "caddy/Caddyfile.tmpl"


class CaddyfileArtifact(Artifact):
    """Render the release's HTTPS-only Caddy ingress configuration."""

    name = "caddyfile"
    relative_path = "caddy/Caddyfile"
    owners = ("caddy",)
    template_paths = (_TEMPLATE_PATH,)

    def emit(self, inputs: RenderInputs) -> str:
        try:
            template_text = inputs.templates[_TEMPLATE_PATH]
        except KeyError as exc:
            raise ValueError(
                f"Render template {_TEMPLATE_PATH} is missing."
            ) from exc
        try:
            return Template(template_text).substitute(hostname=inputs.site.hostname)
        except KeyError as exc:
            placeholder = exc.args[0] if exc.args else "unknown"
            raise ValueError(
                f"Render template {_TEMPLATE_PATH} has an unfilled "
                f"placeholder: {placeholder}."
            ) from exc

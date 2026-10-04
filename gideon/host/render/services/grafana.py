"""Compose definition for grafana."""

from collections.abc import Mapping

from gideon.host.images import RegistryTarget, reference
from gideon.host.render import RenderInputs
from gideon.host.render.grafana import (
    DASHBOARDS_MOUNT,
    GRAFANA_ADMIN_USER,
    GRAFANA_SUB_PATH,
    HOME_DASHBOARD_PATH,
)
from gideon.host.render.services import ServiceDefinition, image_pin


def grafana_environment(inputs: RenderInputs) -> Mapping[str, str]:
    """Return Grafana's non-secret environment for the rendered Compose file."""

    smtp = inputs.site.alerts.smtp
    environment: dict[str, str] = {
        "GF_SERVER_ROOT_URL": f"https://{inputs.site.hostname}{GRAFANA_SUB_PATH}",
        "GF_SERVER_SERVE_FROM_SUB_PATH": "true",
        "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH": HOME_DASHBOARD_PATH,
        "GF_AUTH_LDAP_ENABLED": "true",
        "GF_AUTH_LDAP_CONFIG_FILE": "/etc/grafana/ldap.toml",
        "GF_AUTH_LDAP_ALLOW_SIGN_UP": "true",
        "GF_SECURITY_ADMIN_USER": GRAFANA_ADMIN_USER,
        "GF_SECURITY_ADMIN_PASSWORD__FILE": "/run/secrets/grafana_admin_password",
        "GF_AUTH_ANONYMOUS_ENABLED": "false",
        "GF_USERS_ALLOW_SIGN_UP": "false",
        "GF_SECURITY_COOKIE_SECURE": "true",
        "GF_DATE_FORMATS_DEFAULT_TIMEZONE": inputs.site.office.timezone,
        "GF_SMTP_ENABLED": "true",
        "GF_SMTP_HOST": f"{smtp.host}:{smtp.port}",
        "GF_SMTP_FROM_ADDRESS": smtp.from_,
        # No angle brackets in a display name: mail filters read them as a
        # spoofed address and flag every page as suspicious.
        "GF_SMTP_FROM_NAME": f"GIDEON ({inputs.site.office.short_name})",
        "GF_SMTP_STARTTLS_POLICY": (
            "MandatoryStartTLS" if smtp.user else "OpportunisticStartTLS"
        ),
        "GF_INSTANCE_NAME": inputs.site.office.short_name,
        "GF_METRICS_ENABLED": "true",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        # The background installer fetches plugins from an unlisted host at
        # start and refreshes them on later starts.
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        # The image otherwise downloads the plugin-signature key from the same
        # host every ten days.
        "GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED": "true",
        # Rendering an avatar otherwise fetches an address hash from Gravatar.
        "GF_SECURITY_DISABLE_GRAVATAR": "true",
        # Publishing a board otherwise sends it to a public snapshot host.
        "GF_SNAPSHOTS_EXTERNAL_ENABLED": "false",
        # In-app install otherwise downloads unpinned code; the listing proxy stays.
        "GF_PLUGINS_PLUGIN_ADMIN_ENABLED": "false",
        # The product's chrome otherwise fetches a news feed in the browser.
        "GF_NEWS_NEWS_FEED_ENABLED": "false",
        "TZ": inputs.site.office.timezone,
    }
    if smtp.user:
        environment["GF_SMTP_USER"] = smtp.user
        environment["GF_SMTP_PASSWORD__FILE"] = "/run/secrets/smtp_password"
    return environment


class GrafanaService(ServiceDefinition):
    """The grafana service in the Compose project."""

    name = "grafana"

    def block(
        self, inputs: RenderInputs, target: RegistryTarget
    ) -> Mapping[str, object]:
        return {
            "image": reference(target, image_pin(inputs, "grafana")),
            "restart": "unless-stopped",
            "environment": grafana_environment(inputs),
            "group_add": [str(inputs.facts.service_gid)],
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
            },
            "volumes": [
                "/data/observability/grafana:/var/lib/grafana",
                "/etc/gideon/rendered/grafana/provisioning:/etc/grafana/provisioning:ro",
                f"/etc/gideon/rendered/grafana/dashboards:{DASHBOARDS_MOUNT}:ro",
                "/etc/gideon/rendered/grafana/ldap.toml:/etc/grafana/ldap.toml:ro",
                "/etc/gideon/ca.pem:/etc/gideon/ca.pem:ro",
                "/etc/gideon/ca.pem:/etc/ssl/certs/gideon-ca.pem:ro",
            ],
            "secrets": [
                "grafana_admin_password",
                "ldap_bind_password",
                "postgres_gideon_ro_metrics_password",
                *(["smtp_password"] if inputs.site.alerts.smtp.user else []),
            ],
            "networks": ["gideon"],
        }

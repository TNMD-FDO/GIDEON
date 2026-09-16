"""The site-dependent APT egress proxy configuration step."""

from pathlib import Path

from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    site_required,
)

_PROXY_CONF = Path("/etc/apt/apt.conf.d/99gideon-proxy")


def _proxy_text(proxy: str) -> str:
    return (
        f'Acquire::http::Proxy "{proxy}";\n'
        f'Acquire::https::Proxy "{proxy}";\n'
    )


class EgressProxyStep(Step):
    """Converge APT's HTTP and HTTPS proxy settings."""

    name = "egress-proxy"
    summary = "configure the site egress proxy for APT"
    needs_site = True

    def check(self, context: ProvisionContext) -> CheckResult:
        if context.site is None:
            return site_required()
        proxy = context.site.egress_proxy
        if not proxy:
            if context.host.exists(_PROXY_CONF):
                return CheckResult(
                    Disposition.DRIFT,
                    f"{_PROXY_CONF} is stale: the site configures no egress proxy",
                    f"Remove {_PROXY_CONF}.",
                )
            return CheckResult(Disposition.CONVERGED, "no egress proxy configured", "")

        expected = _proxy_text(proxy)
        if not context.host.exists(_PROXY_CONF):
            return CheckResult(
                Disposition.DRIFT,
                f"{_PROXY_CONF} is missing",
                f"Write the configured proxy to {_PROXY_CONF}.",
            )
        try:
            current = context.host.read_text(_PROXY_CONF)
        except (OSError, UnicodeError) as exc:
            return CheckResult(
                Disposition.UNFIXABLE,
                f"cannot read {_PROXY_CONF}: {exc}",
                f"Correct access to {_PROXY_CONF}, then re-run provision.",
            )
        if current != expected:
            return CheckResult(
                Disposition.DRIFT,
                f"{_PROXY_CONF} differs from the site proxy",
                f"Rewrite {_PROXY_CONF} from the site proxy value.",
            )
        return CheckResult(Disposition.CONVERGED, f"{_PROXY_CONF} is current", "")

    def apply(self, context: ProvisionContext) -> None:
        if context.site is None:
            raise RuntimeError("site file is required by egress-proxy")
        if not context.site.egress_proxy:
            context.host.unlink(_PROXY_CONF, missing_ok=True)
            return
        context.host.write_text(
            _PROXY_CONF,
            _proxy_text(context.site.egress_proxy),
        )

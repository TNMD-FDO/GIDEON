"""The Grafana-managed alerting channel test (§19.5)."""

import argparse
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

from gideon.host import audit as audit_module
from gideon.host import grafana, secrets, site, stack, tls
from gideon.host.render.grafana import GRAFANA_ADMIN_USER
from gideon.host.report import StageResult, print_stage
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final[str] = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final[str] = "/etc/gideon/rendered"
_READY_ATTEMPTS: Final[int] = 12
_ROOT_FIX: Final[str] = "Run sudo python3 -m gideon alerts test."
_APPLY_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
_AUDIT_FIX: Final[str] = stack.logs_fix(_RENDERED_DIR, "postgres")
_GRAFANA_FIX: Final[str] = stack.logs_fix(_RENDERED_DIR, "grafana")


def _failed(name: str, detail: str, fix: str) -> StageResult:
    return StageResult(name, False, detail, fix)


def _rendered_has_grafana(io: Host, rendered_dir: PathLike) -> bool | str:
    compose_path = Path(rendered_dir) / "compose.yaml"
    try:
        document = yaml.safe_load(io.read_text(compose_path))
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        return f"rendered Compose file is unavailable: {exc}."
    except yaml.YAMLError:
        return "rendered Compose file is invalid."
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        return "rendered Compose file has no services map."
    return "grafana" in document["services"]


def _ready_failure(result: grafana.ReadyResult) -> StageResult:
    return _failed(
        "grafana",
        result.problem or "Grafana is not ready.",
        _GRAFANA_FIX,
    )


def _integration_recipients(integration: Mapping[str, object]) -> tuple[str, ...]:
    settings = integration.get("settings")
    if not isinstance(settings, Mapping):
        return ()
    addresses = settings.get("addresses")
    if not isinstance(addresses, str):
        return ()
    return tuple(address.strip() for address in addresses.split(";") if address.strip())


def _integration_matches_site(
    integration: Mapping[str, object], recipients: Sequence[str]
) -> bool:
    settings = integration.get("settings")
    return (
        isinstance(settings, Mapping)
        and settings.get("singleEmail") is True
        and set(_integration_recipients(integration)) == set(recipients)
    )


def run_alerts_test(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    site_path: PathLike = _SITE_PATH,
    rendered_dir: PathLike = _RENDERED_DIR,
    client_factory: Callable[..., grafana.Client] | None = None,
    audit: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Test the provisioned Grafana email receiver and record the outcome."""

    del args
    io = host or RealHost()
    audit_api = audit if audit is not None else audit_module

    if io.geteuid() != 0:
        print_stage(_failed("preconditions", "root privileges are required.", _ROOT_FIX))
        return 1

    site_result = site.load_site(Path(site_path), host=io)
    if site_result.errors or site_result.config is None:
        print_stage(
            _failed(
                "preconditions",
                site.render_errors(site_result.errors),
                "Correct the site file, then retry.",
            )
        )
        return 1
    config = site_result.config

    rendered = _rendered_has_grafana(io, rendered_dir)
    if rendered is not True:
        detail = rendered if isinstance(rendered, str) else "rendered Compose has no grafana service."
        print_stage(_failed("preconditions", detail, _APPLY_FIX))
        return 1

    admin_secret = secrets.read_secret(io, "grafana_admin_password")
    if not admin_secret.ok or admin_secret.value is None:
        print_stage(
            _failed(
                "preconditions",
                admin_secret.problem or "Grafana administrator secret is unavailable.",
                _APPLY_FIX,
            )
        )
        return 1

    audit_problem = audit_api.probe(io, rendered_dir)
    if audit_problem is not None:
        print_stage(
            _failed(
                "preconditions",
                f"audit writer is unavailable: {audit_problem}",
                _AUDIT_FIX,
            )
        )
        return 1

    print_stage(
        StageResult(
            "preconditions",
            True,
            "root, site, rendered Grafana project, break-glass secret, and audit writer are ready",
            "",
        )
    )

    make_client = client_factory or grafana.ingress_client_factory(
        config.hostname, ca_path=tls.CA_PATH
    )
    try:
        ready = grafana.wait_ready(
            make_client(), attempts=_READY_ATTEMPTS, sleep=sleep
        )
    except (grafana.GrafanaError, OSError):
        ready = grafana.ReadyResult(False, "Grafana readiness request failed.", _GRAFANA_FIX)
    if not ready.ok:
        print_stage(_ready_failure(ready))
        return 1
    print_stage(StageResult("grafana", True, "Grafana is ready through the ingress", ""))

    run_id = str(uuid.uuid4())
    relay = f"{config.alerts.smtp.host}:{config.alerts.smtp.port}"
    send_result: grafana.TestResult
    try:
        client = make_client(
            credential=(GRAFANA_ADMIN_USER, admin_secret.value)
        )
        receiver = client.get_receiver("page")
        if receiver is None:
            send_result = grafana.TestResult(
                False,
                0,
                "Grafana contact point 'page' is missing.",
                _APPLY_FIX,
            )
        else:
            email_integrations = tuple(
                integration
                for integration in receiver.integrations
                if integration.get("type") == "email"
            )
            if len(email_integrations) != 1:
                send_result = grafana.TestResult(
                    False,
                    0,
                    "Grafana contact point 'page' does not have exactly one email integration.",
                    _APPLY_FIX,
                )
            elif not _integration_matches_site(
                email_integrations[0], config.alerts.recipients
            ):
                send_result = grafana.TestResult(
                    False,
                    0,
                    "Grafana's page contact point does not match the site file.",
                    _APPLY_FIX,
                )
            else:
                stored_recipients = _integration_recipients(email_integrations[0])
                send_result = client.test_receiver(
                    receiver,
                    email_integrations[0],
                    recipients=tuple(
                        sorted(set(config.alerts.recipients) | set(stored_recipients))
                    ),
                )
    except (grafana.GrafanaError, OSError) as exc:
        send_result = grafana.TestResult(
            False,
            0,
            str(exc) if isinstance(exc, grafana.GrafanaError) else "Grafana contact-point test failed.",
            _GRAFANA_FIX,
        )

    send_detail = (
        f"contact point page tested for {len(config.alerts.recipients)} recipient(s) "
        f"via {relay}"
    )
    if not send_result.ok:
        send_detail += f": {send_result.problem or 'failed'}"
    print_stage(
        StageResult("send", send_result.ok, send_detail, send_result.fix)
    )

    outcome = "ok" if send_result.ok else "failed"
    row = audit_module.AuditRow(
        run_id=run_id,
        kind="alerts_test",
        actor_user_id=None,
        user_id=None,
        chat_id=None,
        kb_ids=(),
        detail={
            "recipients": len(config.alerts.recipients),
            "relay": relay,
            "outcome": outcome,
            "smtp_code": send_result.smtp_code,
        },
    )
    try:
        audit_write_problem = audit_api.write_rows(io, rendered_dir, (row,))
    except OSError as exc:
        audit_write_problem = f"audit writer failed: {exc}"
    if audit_write_problem is None:
        audit_result = StageResult("audit", True, "alerts_test audit row recorded", "")
    else:
        audit_result = _failed(
            "audit",
            f"alerts_test audit write failed: {audit_write_problem}",
            _AUDIT_FIX,
        )
    print_stage(audit_result)
    print("check the inbox")
    return int(not (send_result.ok and audit_result.ok))

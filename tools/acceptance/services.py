"""Prepare the receiving VM's certificates, site, secrets, and SMTP sink.

All material is made for one run under its run directory.  Private values are
read from, or sent to, the VM through the Host/SSH seams; they are never part
of an argv tuple or an operator-facing row.
"""

import secrets
import shlex
import string
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.host import images
from gideon.host.report import StageResult, command_detail
from gideon.host.site import load_site_text, render_errors
from tools.acceptance import seed, smtpsink, vm
from tools.acceptance.context import (
    ACCEPTANCE_REGISTRY_AUTHORITY,
    DEFAULT_VM_NAME,
    HarnessContext,
    ServiceMaterial,
    SinkFactory,
    SinkLike,
)

ACCEPTANCE_SINK_PORT: Final = 2525
ACCEPTANCE_SINK_USER: Final = "gideon-acceptance"
ACCEPTANCE_SINK_NAME: Final = "gideon-acceptance"
ACCEPTANCE_BRIDGE_ADDRESS: Final = ACCEPTANCE_REGISTRY_AUTHORITY.rsplit(":", 1)[0]
ACCEPTANCE_UFW_COMMENT: Final = "gideon-acceptance"
ACCEPTANCE_TARGET_PATH: Final = "/srv/gideon-backup"
ACCEPTANCE_TARGET_USER: Final = "gideon-backup"


def sink_port(vm_name: str) -> int:
    """The port the sink asks for: 2525 for the default name, else 0.

    Two harnesses at once (``--name``) must not share a listener, and no
    name-derived number is collision-free, so a non-default run lets the
    kernel allocate a free port at bind time; the run's site file and firewall
    rule are written from the port actually bound.
    """

    return ACCEPTANCE_SINK_PORT if vm_name == DEFAULT_VM_NAME else 0


def ufw_comment(vm_name: str) -> str:
    """The firewall rule's comment: one per run name, so a sweep touches only its own."""

    if vm_name == DEFAULT_VM_NAME:
        return ACCEPTANCE_UFW_COMMENT
    return f"{ACCEPTANCE_UFW_COMMENT}-{vm_name}"
CA_DAYS: Final = "2"
SERVICE_FIX: Final = "Inspect the services-stage openssl and UFW output, then retry acceptance."
UFW_FIX: Final = "Run ufw show added, repair the acceptance rule, then retry acceptance."
CA_DIR_NAME: Final = "ca"
OFFICE_CA_PATH: Final = "/etc/gideon/ca.pem"
LDAP_BIND_PASSWORD_PATH: Final = "/etc/gideon/secrets/ldap_bind_password"
# Python 3.13+ verifies chains with OpenSSL's strict X.509 flag: a CA needs a
# key-usage extension and a leaf its usages and server-auth purpose, or the
# VM's SMTP check refuses the sink with "CA cert does not include key usage
# extension" (the first full run found this; the office CA already carries them).
_LEAF_EXTENSIONS: Final = (
    "-addext",
    "basicConstraints=CA:FALSE",
    "-addext",
    "keyUsage=critical,digitalSignature,keyEncipherment",
    "-addext",
    "extendedKeyUsage=serverAuth",
)


def _ca_paths(run_dir: Path) -> dict[str, Path]:
    ca = run_dir / CA_DIR_NAME
    return {
        "root_key": ca / "root.key",
        "root_cert": ca / "root.pem",
        "vm_key": ca / "vm.key",
        "vm_csr": ca / "vm.csr",
        "vm_cert": ca / "vm.pem",
        "sink_key": ca / "sink.key",
        "sink_csr": ca / "sink.csr",
        "sink_cert": ca / "sink.pem",
    }


def _result_failure(detail: str, result: subprocess.CompletedProcess[str]) -> StageResult:
    return StageResult("services", False, f"{detail}: {command_detail(result)}", SERVICE_FIX)


def _run(ctx: HarnessContext, argv: Sequence[str], detail: str) -> StageResult | None:
    try:
        result = ctx.host.run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("services", False, f"{detail}: {exc}", SERVICE_FIX)
    if result.returncode != 0:
        return _result_failure(detail, result)
    return None


def _yaml(value: object) -> str:
    """Render one value as a compact, valid YAML fragment for the template."""

    rendered = yaml.safe_dump(value, default_flow_style=True, sort_keys=False).strip()
    return rendered.removesuffix("\n...")


def _site_text(ctx: HarnessContext, hostname: str, sink_port_bound: int) -> str:
    if ctx.site is None:
        raise ValueError("the box site was not loaded")
    site = ctx.site
    ldap = site.auth.ldap
    ldap_document = {
        "host": ldap.host,
        "port": ldap.port,
        "search_base": ldap.search_base,
        "bind_user": ldap.bind_user,
        "users_group": ldap.users_group_dn,
        "admins_group": ldap.admins_group_dn,
        "mirror_groups": list(ldap.mirror_group_dns),
    }
    backup_target = {
        "host": "127.0.0.1",
        "path": ACCEPTANCE_TARGET_PATH,
        "user": ACCEPTANCE_TARGET_USER,
    }
    smtp_document = {
        "host": ACCEPTANCE_BRIDGE_ADDRESS,
        "port": sink_port_bound,
        "from": f"{ACCEPTANCE_SINK_USER}@{seed.ACCEPTANCE_DOMAIN}",
        "user": ACCEPTANCE_SINK_USER,
    }
    document = {
        "office": {
            "name": "GIDEON acceptance VM",
            "short_name": "ACCEPT",
            "timezone": site.office.timezone,
        },
        "hostname": hostname,
        "lan_cidrs": [images.LIBVIRT_BRIDGE_CIDR],
        "jurisdiction": {
            "circuit": site.jurisdiction.circuit,
            "districts": site.jurisdiction.districts,
            "states": site.jurisdiction.states,
        },
        "auth": {
            "ldap": ldap_document,
        },
        "backup": {"target": backup_target},
        "alerts": {
            "smtp": smtp_document,
            "recipients": [f"csa@{seed.ACCEPTANCE_DOMAIN}"],
        },
        "registry": ACCEPTANCE_REGISTRY_AUTHORITY,
    }
    template_path = ctx.checkout / "tools/acceptance/site.yaml.tmpl"
    template = string.Template(ctx.host.read_text(template_path))
    recipients = [f"csa@{seed.ACCEPTANCE_DOMAIN}"]
    return template.substitute(
        office=_yaml(document["office"]),
        hostname=_yaml(document["hostname"]),
        lan_cidrs=_yaml(document["lan_cidrs"]),
        jurisdiction=_yaml(document["jurisdiction"]),
        ldap=_yaml(ldap_document),
        backup_target=_yaml(backup_target),
        smtp=_yaml(smtp_document),
        recipients=_yaml(recipients),
        registry=_yaml(document["registry"]),
    )


def ufw_rule(vm_name: str, port: int) -> tuple[str, ...]:
    """The exact transient rule that lets the VM reach this run's sink on *port*."""

    return (
        "ufw",
        "allow",
        "proto",
        "tcp",
        "from",
        images.LIBVIRT_BRIDGE_CIDR,
        "to",
        "any",
        "port",
        str(port),
        "comment",
        ufw_comment(vm_name),
    )


def _delete_ufw_rule(ctx: HarnessContext, rule: Sequence[str]) -> StageResult | None:
    delete = (rule[0], "delete", *rule[1:])
    try:
        result = ctx.host.run(delete)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("teardown", False, f"could not delete the acceptance UFW rule: {exc}", UFW_FIX)
    if result.returncode != 0:
        return StageResult(
            "teardown",
            False,
            f"could not delete the acceptance UFW rule: {command_detail(result)}",
            UFW_FIX,
        )
    return None


def sweep_ufw(ctx: HarnessContext) -> StageResult | None:
    """Remove stale acceptance rules before a run, using UFW's own listing."""

    try:
        listed = ctx.host.run(["ufw", "show", "added"])
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    for line in listed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("ufw allow "):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        try:
            comment = words[words.index("comment") + 1]
        except (IndexError, ValueError):
            continue
        if comment != ufw_comment(ctx.spec.vm_name):
            continue
        deleted = _delete_ufw_rule(ctx, words)
        if deleted is not None:
            return StageResult(
                "preconditions", False, deleted.detail, deleted.fix
            )
    return None


def prepare(ctx: HarnessContext) -> StageResult:
    """Create certificates, site text, credentials, the sink, and its rule."""

    if ctx.site is None or not ctx.run_id:
        return StageResult("services", False, "services inputs were not resolved", SERVICE_FIX)
    run_dir = ctx.spec.run_dir
    ca = _ca_paths(run_dir)
    try:
        ctx.host.mkdir(run_dir / CA_DIR_NAME, mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return StageResult("services", False, f"could not create the certificate directory: {exc}", SERVICE_FIX)
    hostname = seed.hostname_for(ctx.spec.vm_name)
    root_subject = f"/CN=GIDEON acceptance CA {ctx.run_id}"

    commands: tuple[tuple[tuple[str, ...], str], ...] = (
        (
            (
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(ca["root_key"]),
                "-out",
                str(ca["root_cert"]),
                "-days",
                CA_DAYS,
                "-subj",
                root_subject,
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
            ),
            "created the acceptance CA",
        ),
        (
            (
                "openssl",
                "req",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(ca["vm_key"]),
                "-out",
                str(ca["vm_csr"]),
                "-subj",
                f"/CN={hostname}",
                "-addext",
                f"subjectAltName=DNS:{hostname}",
                *_LEAF_EXTENSIONS,
            ),
            "created the VM certificate request",
        ),
        (
            (
                "openssl",
                "x509",
                "-req",
                "-in",
                str(ca["vm_csr"]),
                "-CA",
                str(ca["root_cert"]),
                "-CAkey",
                str(ca["root_key"]),
                "-CAcreateserial",
                "-out",
                str(ca["vm_cert"]),
                "-days",
                CA_DAYS,
                "-sha256",
                "-copy_extensions",
                "copy",
            ),
            "signed the VM certificate",
        ),
        (
            (
                "openssl",
                "req",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(ca["sink_key"]),
                "-out",
                str(ca["sink_csr"]),
                "-subj",
                f"/CN={ACCEPTANCE_SINK_NAME}",
                "-addext",
                f"subjectAltName=IP:{ACCEPTANCE_BRIDGE_ADDRESS}",
                *_LEAF_EXTENSIONS,
            ),
            "created the SMTP sink certificate request",
        ),
        (
            (
                "openssl",
                "x509",
                "-req",
                "-in",
                str(ca["sink_csr"]),
                "-CA",
                str(ca["root_cert"]),
                "-CAkey",
                str(ca["root_key"]),
                "-CAcreateserial",
                "-out",
                str(ca["sink_cert"]),
                "-days",
                CA_DAYS,
                "-sha256",
                "-copy_extensions",
                "copy",
            ),
            "signed the SMTP sink certificate",
        ),
    )
    for argv, detail in commands:
        failure = _run(ctx, argv, detail)
        if failure is not None:
            return failure

    try:
        office_root = ctx.host.read_text(OFFICE_CA_PATH)
        root_certificate = ctx.host.read_text(ca["root_cert"])
        vm_certificate = ctx.host.read_text(ca["vm_cert"])
        vm_key = ctx.host.read_text(ca["vm_key"])
        bind_password = ctx.host.read_text(LDAP_BIND_PASSWORD_PATH).rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        return StageResult("services", False, f"could not prepare service material: {exc}", SERVICE_FIX)
    bundle = office_root.rstrip("\r\n") + "\n" + root_certificate.lstrip("\r\n")
    bundle_path = run_dir / CA_DIR_NAME / "bundle.pem"
    try:
        ctx.host.write_text(bundle_path, bundle, mode=0o644)
    except OSError as exc:
        return StageResult("services", False, f"could not write the acceptance CA bundle: {exc}", SERVICE_FIX)
    smtp_password = secrets.token_urlsafe(32)
    try:
        # ``SinkFactory`` accepts the injected fake and the real sink's
        # keyword-only constructor has the same runtime shape.
        sink_factory = cast(SinkFactory, ctx.sink_factory or smtpsink.SmtpSink)

        # The sink starts before the site text exists because that text names
        # its port; read ``vm_site`` when each message is stored.
        def redact_sink_text(text: str) -> str:
            return vm.redact(text, ctx.vm_site)

        sink: SinkLike = sink_factory(
            address=ACCEPTANCE_BRIDGE_ADDRESS,
            port=sink_port(ctx.spec.vm_name),
            certfile=str(ca["sink_cert"]),
            keyfile=str(ca["sink_key"]),
            user=ACCEPTANCE_SINK_USER,
            password=smtp_password,
            mail_dir=ctx.spec.out / "mail",
            name=ACCEPTANCE_SINK_NAME,
            redact=redact_sink_text,
        )
        ctx.sink = sink
        sink.start()
    except (OSError, RuntimeError, ValueError) as exc:
        return StageResult("services", False, f"could not start the SMTP sink: {exc}", SERVICE_FIX)
    # The port the sink actually bound is the one the site file and the rule name.
    bound = sink.port
    try:
        site_text = _site_text(ctx, hostname, bound)
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        return StageResult("services", False, f"could not render the acceptance site: {exc}", SERVICE_FIX)
    loaded_site = load_site_text(site_text)
    if loaded_site.config is None:
        detail = render_errors(loaded_site.errors)
        return StageResult("services", False, f"could not load the acceptance site: {detail}", SERVICE_FIX)
    # The VM's own site, distinct from the box's: what run_product and the sink redact against.
    ctx.vm_site = loaded_site.config
    material = ServiceMaterial(
        site_text=site_text,
        ca_bundle_text=bundle,
        vm_certificate_text=vm_certificate,
        vm_key_text=vm_key,
        ldap_bind_password=bind_password,
        smtp_password=smtp_password,
        smtp_user=ACCEPTANCE_SINK_USER,
    )

    rule = ufw_rule(ctx.spec.vm_name, bound)
    failure = _run(ctx, rule, "added the acceptance SMTP UFW rule")
    if failure is not None:
        return failure
    ctx.ufw_rule = rule
    ctx.services = material
    return StageResult(
        "services",
        True,
        f"prepared the acceptance CA, site, secrets, and SMTP sink on {ACCEPTANCE_BRIDGE_ADDRESS}:{bound}",
        "",
    )


def install_site(ctx: HarnessContext) -> StageResult:
    """Place the prepared site, CA, certificate, and secret files in the VM."""

    material = ctx.services
    if material is None:
        return StageResult("site", False, "services material was not prepared", SERVICE_FIX)
    files = (
        ("/etc/gideon/site.yaml", material.site_text, 0o644, "root:root"),
        ("/etc/gideon/ca.pem", material.ca_bundle_text, 0o644, "root:root"),
        ("/etc/gideon/tls/cert.pem", material.vm_certificate_text, 0o644, "root:root"),
        (
            "/etc/gideon/secrets/ldap_bind_password",
            material.ldap_bind_password,
            0o440,
            "root:gideon",
        ),
        ("/etc/gideon/secrets/smtp_password", material.smtp_password, 0o440, "root:gideon"),
        ("/etc/gideon/secrets/tls_key", material.vm_key_text, 0o440, "root:gideon"),
    )
    for path, text, mode, owner in files:
        result = vm.copy_in(ctx, path, text, mode, as_root=True, owner=owner)
        if not result.ok:
            return StageResult("site", False, result.detail, result.fix)
    return StageResult("site", True, "installed the acceptance site and TLS material", "")


def authorize(ctx: HarnessContext) -> StageResult:
    """Authorize the VM's generated backup key on its loopback target."""

    result = vm.run_as_root(ctx, "cat /etc/gideon/secrets/backup_ssh_key.pub")
    if result.returncode != 0:
        return StageResult("authorize", False, f"could not read the backup public key: {command_detail(result)}", SERVICE_FIX)
    public_key = result.stdout.strip()
    if not public_key:
        return StageResult("authorize", False, "the backup public key is empty", SERVICE_FIX)
    directory = vm.run_as_root(
        ctx,
        f"install -d -m 0700 -o {ACCEPTANCE_TARGET_USER} -g {ACCEPTANCE_TARGET_USER} "
        f"{ACCEPTANCE_TARGET_PATH}/.ssh",
    )
    if directory.returncode != 0:
        return StageResult("authorize", False, f"could not create the backup SSH directory: {command_detail(directory)}", SERVICE_FIX)
    copied = vm.copy_in(
        ctx,
        f"{ACCEPTANCE_TARGET_PATH}/.ssh/authorized_keys",
        public_key + "\n",
        0o600,
        as_root=True,
        owner=f"{ACCEPTANCE_TARGET_USER}:{ACCEPTANCE_TARGET_USER}",
    )
    if not copied.ok:
        return StageResult("authorize", False, copied.detail, copied.fix)
    return StageResult("authorize", True, "authorized the VM backup key on the loopback target", "")


def stop_sink(ctx: HarnessContext) -> StageResult | None:
    """Stop the run's sink, if it was created."""

    if ctx.sink is None:
        return None
    try:
        ctx.sink.stop()
    except (OSError, RuntimeError) as exc:
        return StageResult("teardown", False, f"could not stop the acceptance SMTP sink: {exc}", SERVICE_FIX)
    ctx.sink = None
    return None


def remove_rule(ctx: HarnessContext) -> StageResult | None:
    """Delete the exact UFW rule installed for this run."""

    if ctx.ufw_rule is None:
        return None
    failure = _delete_ufw_rule(ctx, ctx.ufw_rule)
    if failure is None:
        ctx.ufw_rule = None
    return failure

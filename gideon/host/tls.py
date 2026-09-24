"""TLS material validation and ingress verification for the host command path."""

import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon.host import stack
from gideon.host.report import Problem, command_detail, refusal
from gideon.host.site import load_site
from gideon.host.site import render_errors as render_site_errors
from gideon.host.sysio import CompletedText, Host, PathLike, RealHost

CERT_PATH: Final = "/etc/gideon/tls/cert.pem"
KEY_PATH: Final = "/etc/gideon/secrets/tls_key"
CA_PATH: Final = "/etc/gideon/ca.pem"
_ROOT_FIX: Final = "Run gideon tls reload as root, for example with sudo."
_OPENSSL_FIX: Final = (
    "openssl ships with Ubuntu Server; reinstall it with apt-get install -y openssl, "
    "then re-run tls reload."
)
_APPLY_FIX: Final = "Run gideon apply first, then re-run tls reload."
_CADDY_FIX: Final = "docker compose -f /etc/gideon/rendered/compose.yaml logs caddy"
# Exempt operational constants: one openssl invocation's bound, one
# handshake's timeout, and how long a just-recreated Caddy is given to bind
# 443 before the probe gives up.
_OPENSSL_TIMEOUT: Final = 30.0
_PROBE_TIMEOUT: Final = 15.0
_PROBE_ATTEMPTS: Final = 10
_PROBE_RETRY_SECONDS: Final = 1.0
_CERTIFICATE_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
_FINGERPRINT = re.compile(r"fingerprint\s*=\s*([0-9a-f:]+)", re.IGNORECASE)
# The one served_fingerprint problem probe_ingress retries: a just-recreated
# container needs a moment to bind 443; every other problem is final.
HANDSHAKE_FAILED: Final = "HTTPS ingress handshake failed"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The result of the local HTTPS ingress probe."""

    ok: bool
    detail: str
    fix: str


def _refusal(problem: str, fix: str) -> str:
    return f"{problem} Fix: {fix}"


def _openssl_missing() -> str:
    return _refusal("openssl is not available", _OPENSSL_FIX)


def _run_openssl(
    host: Host, argv: list[str]
) -> tuple[CompletedText | None, str | None]:
    # Closed stdin and a bound: openssl must never wait on a terminal here.
    try:
        result = host.run(argv, input="", timeout=_OPENSSL_TIMEOUT)
    except subprocess.SubprocessError as exc:
        return None, _refusal(f"openssl did not finish: {exc}", _OPENSSL_FIX)
    except OSError as exc:
        return None, _refusal(f"could not run openssl: {exc}", _OPENSSL_FIX)
    if result.returncode == 127:
        return None, _openssl_missing()
    return result, None


def _check_command(
    host: Host,
    argv: list[str],
    path: str,
    description: str,
) -> tuple[CompletedText | None, str | None]:
    result, refusal = _run_openssl(host, argv)
    if refusal is not None:
        return None, refusal
    assert result is not None
    if result.returncode != 0:
        return None, _refusal(
            f"{description} failed for {path}: {command_detail(result)}",
            f"Replace or repair {path}, then re-run tls reload.",
        )
    return result, None


def validate_material(host: Host, hostname: str) -> tuple[str, ...]:
    """Validate the placed certificate, key, and CA without reading key bytes."""

    errors: list[str] = []
    for path in (CERT_PATH, KEY_PATH, CA_PATH):
        if not host.exists(path):
            errors.append(
                _refusal(
                    f"TLS file is missing: {path}",
                    f"Place the required TLS material at {path}, then re-run tls reload.",
                )
            )
    if errors:
        return tuple(errors)

    cert, refusal = _check_command(
        host,
        ["openssl", "x509", "-in", CERT_PATH, "-noout"],
        CERT_PATH,
        "certificate parsing",
    )
    if refusal is not None:
        errors.append(refusal)
    # -passin pass: with an empty password: an encrypted key fails here with a
    # refusal instead of prompting — Caddy needs an unencrypted key anyway.
    key, refusal = _check_command(
        host,
        ["openssl", "pkey", "-in", KEY_PATH, "-noout", "-passin", "pass:"],
        KEY_PATH,
        "key parsing",
    )
    if refusal is not None:
        errors.append(refusal)

    if cert is not None and key is not None:
        cert_public, refusal = _check_command(
            host,
            ["openssl", "x509", "-in", CERT_PATH, "-noout", "-pubkey"],
            CERT_PATH,
            "certificate public-key extraction",
        )
        if refusal is not None:
            errors.append(refusal)
        key_public, refusal = _check_command(
            host,
            ["openssl", "pkey", "-in", KEY_PATH, "-pubout", "-passin", "pass:"],
            KEY_PATH,
            "key public-key extraction",
        )
        if refusal is not None:
            errors.append(refusal)
        if (
            cert_public is not None
            and key_public is not None
            and cert_public.stdout != key_public.stdout
        ):
            errors.append(
                _refusal(
                    f"certificate public key does not match {KEY_PATH}",
                    f"Replace {CERT_PATH} or {KEY_PATH} with a matching pair, then re-run tls reload.",
                )
            )

    if cert is not None:
        _, refusal = _check_command(
            host,
            ["openssl", "verify", "-CAfile", CA_PATH, CERT_PATH],
            CA_PATH,
            "certificate chain verification",
        )
        if refusal is not None:
            errors.append(refusal)
        name_result, refusal = _check_command(
            host,
            ["openssl", "x509", "-in", CERT_PATH, "-noout", "-checkhost", hostname],
            CERT_PATH,
            f"certificate hostname check for {hostname}",
        )
        if refusal is not None:
            errors.append(refusal)
        elif name_result is not None and "does match" not in name_result.stdout.lower():
            errors.append(
                _refusal(
                    f"certificate does not match hostname {hostname}: {CERT_PATH}",
                    f"Replace {CERT_PATH} with a certificate covering {hostname}, then re-run tls reload.",
                )
            )
        _, refusal = _check_command(
            host,
            ["openssl", "x509", "-in", CERT_PATH, "-noout", "-checkend", "0"],
            CERT_PATH,
            "certificate expiry check",
        )
        if refusal is not None:
            errors.append(refusal)
    return tuple(errors)


def _fingerprint(stdout: str) -> str | None:
    match = _FINGERPRINT.search(stdout)
    if match is None:
        return None
    return match.group(1).replace(":", "").lower()


def _probe_failure(detail: str, fix: str = _CADDY_FIX) -> ProbeResult:
    return ProbeResult(False, detail, fix)


def _served_certificate(
    host: Host,
    *,
    connect: str,
    hostname: str,
    cafile: PathLike,
) -> str | Problem:
    """Return the PEM certificate served at *connect* through the bounded handshake."""

    handshake_argv = [
        "openssl",
        "s_client",
        "-connect",
        connect,
        "-servername",
        hostname,
        "-verify_hostname",
        hostname,
        "-CAfile",
        str(cafile),
        "-verify_return_error",
    ]
    try:
        handshake = host.run(
            handshake_argv,
            input="",
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"could not run openssl s_client: {exc}", _OPENSSL_FIX)
    if handshake.returncode == 127:
        return Problem("openssl is not available", _OPENSSL_FIX)
    if handshake.returncode != 0:
        return Problem(
            f"{HANDSHAKE_FAILED}: {command_detail(handshake)}",
            _CADDY_FIX,
        )
    match = _CERTIFICATE_BLOCK.search(handshake.stdout)
    if match is None:
        return Problem("HTTPS ingress probe returned no certificate", _CADDY_FIX)
    return match.group(0)


def served_fingerprint(
    host: Host,
    *,
    connect: str,
    hostname: str,
    cafile: PathLike,
) -> str | Problem:
    """Return the SHA-256 fingerprint of the certificate served at *connect*."""

    certificate = _served_certificate(
        host, connect=connect, hostname=hostname, cafile=cafile
    )
    if isinstance(certificate, Problem):
        return certificate
    try:
        served = host.run(
            ["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
            input=certificate,
            timeout=_OPENSSL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"could not run openssl x509: {exc}", _OPENSSL_FIX)
    if served.returncode == 127:
        return Problem("openssl is not available", _OPENSSL_FIX)
    if served.returncode != 0:
        return Problem(
            f"served certificate fingerprint failed: {command_detail(served)}",
            _CADDY_FIX,
        )
    fingerprint = _fingerprint(served.stdout)
    if fingerprint is None:
        return Problem("served certificate returned no SHA-256 fingerprint", _CADDY_FIX)
    return fingerprint


def served_expiry(
    host: Host,
    *,
    connect: str,
    hostname: str,
    cafile: PathLike,
) -> datetime | Problem:
    """Return the UTC expiry of the leaf certificate served at *connect*."""

    certificate = _served_certificate(
        host, connect=connect, hostname=hostname, cafile=cafile
    )
    if isinstance(certificate, Problem):
        return certificate
    try:
        served = host.run(
            ["openssl", "x509", "-noout", "-enddate"],
            input=certificate,
            timeout=_OPENSSL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"could not run openssl x509: {exc}", _OPENSSL_FIX)
    if served.returncode == 127:
        return Problem("openssl is not available", _OPENSSL_FIX)
    if served.returncode != 0:
        return Problem(
            f"served certificate expiry failed: {command_detail(served)}",
            _CADDY_FIX,
        )
    line = next(
        (line.strip() for line in served.stdout.splitlines() if line.startswith("notAfter=")),
        None,
    )
    if line is None:
        return Problem("served certificate returned no expiry date", _CADDY_FIX)
    date_text = line.removeprefix("notAfter=")
    if not date_text.endswith(" GMT"):
        return Problem("served certificate returned an invalid expiry date", _CADDY_FIX)
    try:
        expiry = datetime.strptime(
            date_text.removesuffix(" GMT") + " +0000", "%b %d %H:%M:%S %Y %z"
        )
    except ValueError:
        return Problem("served certificate returned an invalid expiry date", _CADDY_FIX)
    return expiry.astimezone(UTC)


def file_fingerprint(host: Host, path: PathLike) -> str | Problem:
    """Return the SHA-256 fingerprint of a certificate file through Host."""

    certificate = str(path)
    try:
        result = host.run(
            ["openssl", "x509", "-in", certificate, "-noout", "-fingerprint", "-sha256"],
            input="",
            timeout=_OPENSSL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"could not run openssl x509: {exc}", _OPENSSL_FIX)
    if result.returncode == 127:
        return Problem("openssl is not available", _OPENSSL_FIX)
    if result.returncode != 0:
        return Problem(
            f"placed certificate fingerprint failed: {command_detail(result)}",
            f"Replace {certificate}, then re-run tls reload.",
        )
    fingerprint = _fingerprint(result.stdout)
    if fingerprint is None:
        return Problem("placed certificate returned no SHA-256 fingerprint", _CADDY_FIX)
    return fingerprint


def probe_ingress(
    host: Host,
    hostname: str,
    *,
    attempts: int = _PROBE_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    """Verify the local HTTPS chain and that Caddy serves the placed leaf.

    A failed handshake is retried up to *attempts* times, one second apart,
    because a just-recreated container needs a moment to bind 443.  A served
    certificate whose fingerprint differs from the placed file is never
    retried — that is a wrong certificate, not a slow start.
    """

    total = max(attempts, 1)
    served: str | Problem
    for attempt in range(1, total + 1):
        served = served_fingerprint(
            host,
            connect="127.0.0.1:443",
            hostname=hostname,
            cafile=CA_PATH,
        )
        if isinstance(served, str):
            break
        if not served.problem.startswith(HANDSHAKE_FAILED):
            return _probe_failure(served.problem, served.fix)
        if attempt == total:
            return _probe_failure(
                f"HTTPS ingress probe failed after {total} attempts: {served.problem.split(': ', 1)[-1]}"
            )
        sleep(_PROBE_RETRY_SECONDS)

    placed = file_fingerprint(host, CERT_PATH)
    if isinstance(placed, Problem):
        return _probe_failure(placed.problem, placed.fix)
    if served != placed:
        return _probe_failure(
            "served certificate fingerprint does not match " + CERT_PATH
        )
    return ProbeResult(
        True,
        f"HTTPS chain verified and served certificate matches {CERT_PATH}",
        "",
    )


def run_tls_reload(
    args: object,
    *,
    host: Host | None = None,
    site_path: PathLike = "/etc/gideon/site.yaml",
    rendered_dir: PathLike = "/etc/gideon/rendered",
) -> int:
    """Validate TLS, recreate Caddy, and verify the local HTTPS ingress."""

    del args
    io = host or RealHost()
    if io.geteuid() != 0:
        print(refusal("tls reload", "root is required.", _ROOT_FIX), file=sys.stderr)
        return 1

    site_result = load_site(Path(site_path), host=io)
    if site_result.errors or site_result.config is None:
        print(render_site_errors(site_result.errors), file=sys.stderr)
        return 1
    output = Path(rendered_dir)
    compose_path = output / "compose.yaml"
    if not io.exists(compose_path):
        print(
            refusal("tls reload", f"rendered Compose file is missing: {compose_path}.", _APPLY_FIX),
            file=sys.stderr,
        )
        return 1

    material_errors = validate_material(io, site_result.config.hostname)
    if material_errors:
        print("\n".join(material_errors), file=sys.stderr)
        return 1
    print("material: ok — TLS material is valid")

    try:
        recreated = stack.force_recreate(io, rendered_dir, "caddy")
    except OSError as exc:
        print(
            f"recreate: refuse — Caddy recreate failed: {exc}. Fix: {_CADDY_FIX}",
            file=sys.stdout,
        )
        return 1
    if recreated.returncode != 0:
        print(
            f"recreate: refuse — Caddy recreate failed: {command_detail(recreated)}. "
            f"Fix: {_CADDY_FIX}",
            file=sys.stdout,
        )
        return 1
    print("recreate: ok — caddy recreated")

    probe = probe_ingress(io, site_result.config.hostname)
    if not probe.ok:
        print(f"ingress: refuse — {probe.detail} Fix: {probe.fix}")
        return 1
    print(f"ingress: ok — {probe.detail}")
    return 0

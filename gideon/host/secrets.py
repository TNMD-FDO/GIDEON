"""Generated host secret files, their registry, and the secret-file reader.

Apply creates missing passwords and keys, and keeps certificates paired with
their keys. ``secrets rotate`` rewrites the ``rewrite`` class. The registry is
release policy, not site configuration.
"""

import hashlib
import re
import secrets as token_secrets
import subprocess
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from gideon.host.sysio import CompletedText, Host

SecretKind = Literal["password", "minted", "key", "certificate"]
"""How a generated secret file is made."""
RotationClass = Literal["rewrite", "remint", "role", "account", "seeded"]
"""How a secret's value is held beyond its file, if anywhere."""
# A file rotation is safe only when the file alone holds the value; a second
# home must be changed through that consumer's own route.
SECRETS_DIR = Path("/etc/gideon/secrets")
SERVICE_GROUP: Final = "gideon"
SERVICE_GROUP_PROBLEM: Final = f"the {SERVICE_GROUP} service group is missing or invalid"
_SERVICE_GROUP_FIX: Final = (
    "Run sudo python3 -m gideon host provision --only service-user, then retry."
)


def select_directory(path: Path) -> None:
    """Point every reader and fix text at ``path`` for the rest of the process.

    The one sanctioned way to move the module off ``/etc/gideon/secrets``:
    ``tools.cistack`` and ``tools.turns --stack ci`` call it once per process
    for the sibling's directory, and the contract harnesses for their stack's.
    """

    global SECRETS_DIR
    SECRETS_DIR = path


def current_directory() -> Path:
    """Return the directory the readers currently use, for a tool's rows."""

    return SECRETS_DIR


@dataclass(frozen=True, slots=True)
class GeneratedSecret:
    """One release-managed secret and the component that consumes it."""

    name: str
    kind: SecretKind
    print_once: bool
    consumer: str
    rotation: RotationClass
    issued_from: str | None = None


@dataclass(frozen=True, slots=True)
class SuppliedSecret:
    """An office-supplied secret and the path for replacing its value."""

    name: str
    replaced_by: str


@dataclass(frozen=True, slots=True)
class RotateResult:
    """The content-free result of rewriting one generated secret file."""

    written: bool = False
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


# The consumer is kept beside each entry so the release's secret inventory is
# reviewable without searching the renderer or Compose templates.
SECRET_REGISTRY: Final[tuple[GeneratedSecret, ...]] = (
    GeneratedSecret("postgres_superuser_password", "password", False, "postgres", "role"),
    GeneratedSecret("postgres_openwebui_password", "password", False, "Open WebUI database role", "role"),
    GeneratedSecret("postgres_gideon_password", "password", False, "GIDEON database role", "role"),
    GeneratedSecret(
        "postgres_gideon_audit_password",
        "password",
        False,
        "audit writer database role and gideon-api trip writer",
        "role",
    ),
    GeneratedSecret("webui_secret_key", "password", False, "Open WebUI session signing", "rewrite"),
    GeneratedSecret("gideon_admin_password", "password", True, "break-glass Open WebUI administrator", "account"),
    GeneratedSecret("gideon_eval_password", "password", False, "GIDEON evaluation identity", "account"),
    GeneratedSecret("grafana_admin_password", "password", True, "break-glass Grafana administrator", "seeded"),
    GeneratedSecret("postgres_gideon_ro_metrics_password", "password", False, "metrics reader database role", "role"),
    GeneratedSecret("postgres_gideon_eval_password", "password", False, "eval-run writer database role", "role"),
    GeneratedSecret("postgres_gideon_worker_password", "password", False, "queue worker database role", "role"),
    GeneratedSecret("engine_api_key", "password", False, "the engine's API key (gideon-generator)", "rewrite"),
    GeneratedSecret("embed_api_key", "password", False, "the embedding server's API key (gideon-embed)", "rewrite"),
    GeneratedSecret("gideon_api_key", "password", False, "the gideon-api connection key carried by Open WebUI", "rewrite"),
    GeneratedSecret("searxng_secret_key", "password", False, "SearXNG's signing key", "rewrite"),
    GeneratedSecret("qdrant_api_key", "password", False, "the vector store's API key (qdrant)", "rewrite"),
    GeneratedSecret("qdrant_read_only_api_key", "password", False, "the vector store's read-only key carried by Open WebUI", "rewrite"),
    GeneratedSecret("opensearch_password", "password", False, "the opensearch service's internal user", "rewrite"),
    GeneratedSecret("opensearch_transport_key", "key", False, "the opensearch service's transport key", "rewrite"),
    GeneratedSecret(
        "opensearch_transport_cert", "certificate", False,
        "the opensearch service's transport certificate", "rewrite",
        issued_from="opensearch_transport_key",
    ),
    GeneratedSecret("gideon_admin_api_key", "minted", False, "apply and reconcile", "remint"),
    GeneratedSecret("gideon_eval_api_key", "minted", False, "evaluation identity", "remint"),
)
# exempt: EC P-256 is the tested transport key algorithm; genpkey writes PKCS#8 PEM.
OPENSEARCH_KEY_ALGORITHM: Final = "EC"
# exempt: P-256 is the tested curve for the opensearch transport key.
OPENSEARCH_KEY_CURVE: Final = "ec_paramgen_curve:P-256"
# exempt: the subject identifies the opensearch service on the Compose network.
OPENSEARCH_CERT_SUBJECT: Final = "/CN=opensearch"
# exempt: the DNS name identifies the opensearch service to transport peers.
OPENSEARCH_CERT_SAN: Final = "subjectAltName=DNS:opensearch"
# exempt: upstream documents both server and client authentication for transport TLS.
OPENSEARCH_CERT_EKU: Final = "extendedKeyUsage=serverAuth,clientAuth"
# exempt: the tested node accepts CA:FALSE; this leaf is a service certificate.
OPENSEARCH_CERT_BASIC_CONSTRAINTS: Final = "basicConstraints=CA:FALSE"
# exempt: the tested EC certificate uses digital signatures and key encipherment.
OPENSEARCH_CERT_KEY_USAGE: Final = "keyUsage=digitalSignature,keyEncipherment"
# exempt: this certificate is renewed by command; 3650 days is its release lifetime.
OPENSEARCH_CERT_DAYS: Final = 3650
GENERATED_NAMES: Final[frozenset[str]] = frozenset(
    secret.name for secret in SECRET_REGISTRY
)
ROTATABLE_NAMES: Final[frozenset[str]] = frozenset(
    secret.name
    for secret in SECRET_REGISTRY
    if secret.rotation in {"rewrite", "remint"}
)

_APPLY_COMMAND: Final = "sudo python3 -m gideon apply"
# exempt: one local openssl operation must finish before apply proceeds.
_OPENSSL_TIMEOUT: Final = 30.0
_OPENSSL_FIX: Final = (
    "openssl ships with Ubuntu Server; reinstall it with "
    "sudo apt-get install -y openssl, then run sudo python3 -m gideon apply."
)
# Grafana mounts these Compose file secrets, and apply's recreate rule judges
# rendered files only. The replacement therefore needs a manual recreate.
_GRAFANA_RECREATE: Final = (
    "sudo docker compose -f /etc/gideon/rendered/compose.yaml "
    "up -d --no-deps --force-recreate grafana"
)


def _grafana_mounted_replacement(name: str) -> str:
    return (
        f"Replace {SECRETS_DIR / name}, run {_APPLY_COMMAND}, then force-recreate "
        f"Grafana, which mounts it and which apply's recreate rule does not see: "
        f"{_GRAFANA_RECREATE}."
    )


# Each office-supplied secret has a replacement path; the rotation command
# prints it as a command sequence.
SUPPLIED_REGISTRY: Final[tuple[SuppliedSecret, ...]] = (
    SuppliedSecret(
        "tls_key",
        "Replace the certificate and key at their fixed homes per "
        "docs/runbooks/office-services-setup.md §4, then run "
        "sudo python3 -m gideon tls reload.",
    ),
    SuppliedSecret("ldap_bind_password", _grafana_mounted_replacement("ldap_bind_password")),
    SuppliedSecret("smtp_password", _grafana_mounted_replacement("smtp_password")),
    SuppliedSecret(
        "proxy_auth",
        f"Replace {SECRETS_DIR / 'proxy_auth'}, then run {_APPLY_COMMAND} "
        "(the frontend, SearXNG, and egress env files re-render and their owners "
        "are recreated through the recreate rule).",
    ),
)
SUPPLIED_NAMES: Final[frozenset[str]] = frozenset(
    secret.name for secret in SUPPLIED_REGISTRY
)


def registry_entry(name: str) -> GeneratedSecret | None:
    """Return the generated registry entry named *name*, if any."""

    return next((secret for secret in SECRET_REGISTRY if secret.name == name), None)


def issued_certificate(key_name: str) -> GeneratedSecret:
    """Return the certificate entry issued from the key entry *key_name*."""

    return next(secret for secret in SECRET_REGISTRY if secret.issued_from == key_name)


def is_generated(name: str) -> bool:
    """True when *name* is a release-generated secret rather than a supplied one."""

    return name in GENERATED_NAMES


@dataclass(frozen=True, slots=True)
class SecretReadResult:
    """The contents of a secret file, or a fix-bearing read refusal.

    ``missing`` distinguishes an absent file from an unreadable one so a caller
    can name the command that generates it instead of a repair.
    """

    value: str | None = None
    problem: str | None = None
    fix: str = ""
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(frozen=True, slots=True)
class EnsureResult:
    """The generated secrets and any print-once values from one ensure pass."""

    created: tuple[str, ...] = ()
    printed: Mapping[str, str] = field(default_factory=dict)
    problem: str | None = None
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.problem is None


def secret_path(name: str) -> Path:
    """The file a named secret lives in, resolved when called (never at import)."""

    return SECRETS_DIR / name


def read_secret(host: Host, name: str) -> SecretReadResult:
    """Read a named secret below ``/etc/gideon/secrets`` through the seam."""

    path = secret_path(name)
    if path.name != name or not name:
        return SecretReadResult(
            problem=f"Invalid secret file name: {name!r}.",
            fix=f"Use a secret file name under {SECRETS_DIR}.",
        )
    try:
        value = host.read_text(path).rstrip("\r\n")
    except FileNotFoundError:
        return SecretReadResult(
            problem=f"Secret file is missing: {path}.",
            fix=f"Place the secret in {path}, then retry.",
            missing=True,
        )
    except UnicodeDecodeError:
        return SecretReadResult(
            problem=f"Secret file is not valid UTF-8: {path}.",
            fix="Correct the secret file, then retry.",
        )
    except OSError as exc:
        return SecretReadResult(
            problem=f"Secret file is unreadable: {path} ({exc}).",
            fix="Correct the secret file, then retry.",
        )
    return SecretReadResult(value=value)


def service_group_gid(host: Host) -> int | None:
    """The gid of the ``gideon`` service group through the seam; ``None`` when absent or malformed.

    The one place the group is resolved: secrets are written ``root:gideon``,
    the secrets-dirs step converges them to it, and render puts the gid in the
    unprivileged services' ``group_add``.
    """

    result = host.run(["getent", "group", SERVICE_GROUP])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[0] == SERVICE_GROUP:
            try:
                return int(fields[2])
            except ValueError:
                return None
    return None


def _write_secret(
    host: Host, name: str, value: str, service_gid: int
) -> tuple[str | None, bool]:
    path = secret_path(name)
    try:
        host.write_text(path, value + "\n", mode=0o440)
    except OSError as exc:
        return f"unable to write {path}: {exc}.", False
    try:
        host.chown(path, 0, service_gid)
    except OSError as exc:
        return f"{path} was written but unowned: chown to root:{SERVICE_GROUP} failed: {exc}.", True
    return None, True


def _pair_fix(key_name: str, cert_name: str) -> str:
    return (
        f"Remove {secret_path(key_name)} and {secret_path(cert_name)}, "
        f"then run {_APPLY_COMMAND}."
    )


def _run_openssl(
    host: Host, argv: list[str]
) -> tuple[CompletedText | None, str | None]:
    """Run openssl with closed stdin; report unavailable tools as refusals."""

    try:
        result = host.run(argv, input="", timeout=_OPENSSL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Could not run openssl: {exc}."
    if result.returncode == 127:
        return None, f"openssl is not available: {result.stderr.strip() or 'exit status 127'}."
    return result, None


def _child_problem(action: str, result: CompletedText) -> str:
    detail = result.stderr.strip() or f"exit status {result.returncode}"
    return f"{action} failed: {detail}."


def _install_openssl_file(
    host: Host,
    name: str,
    argv: list[str],
    service_gid: int,
    key_name: str,
    cert_name: str,
) -> tuple[str | None, str]:
    """Let openssl write a sibling temporary, then install its owned whole file."""

    path = secret_path(name)
    temporary = path.with_name(f".{name}.new")
    fix = _pair_fix(key_name, cert_name)
    problem: str | None
    try:
        # Remove a leftover temporary before openssl opens its -out path.
        host.unlink(temporary, missing_ok=True)
        result, unavailable = _run_openssl(host, [*argv, "-out", str(temporary)])
        if unavailable is not None:
            problem, fix = unavailable, _OPENSSL_FIX
        else:
            assert result is not None
            if result.returncode != 0:
                problem = _child_problem(f"openssl write of {path}", result)
            else:
                host.chmod(temporary, 0o440)
                host.chown(temporary, 0, service_gid)
                moved = host.run(["mv", "-f", "-T", str(temporary), str(path)])
                problem = (
                    _child_problem(f"install of {path}", moved)
                    if moved.returncode != 0 else None
                )
    except (OSError, subprocess.SubprocessError) as exc:
        problem = f"Could not write or install {path}: {exc}."
    if problem is None:
        return None, ""
    try:
        host.unlink(temporary, missing_ok=True)
    except OSError as exc:
        return f"Could not remove temporary {temporary}: {exc}.", fix
    return problem, fix


def _generate_key(
    host: Host, name: str, service_gid: int, cert_name: str
) -> tuple[str | None, str]:
    return _install_openssl_file(
        host, name,
        ["openssl", "genpkey", "-algorithm", OPENSEARCH_KEY_ALGORITHM,
         "-pkeyopt", OPENSEARCH_KEY_CURVE],
        service_gid, name, cert_name,
    )


def _issue_certificate(
    host: Host, name: str, key_name: str, service_gid: int
) -> tuple[str | None, str]:
    return _install_openssl_file(
        host, name,
        ["openssl", "req", "-x509", "-key", str(secret_path(key_name)),
         "-subj", OPENSEARCH_CERT_SUBJECT,
         "-addext", OPENSEARCH_CERT_SAN,
         "-addext", OPENSEARCH_CERT_EKU,
         "-addext", OPENSEARCH_CERT_BASIC_CONSTRAINTS,
         "-addext", OPENSEARCH_CERT_KEY_USAGE,
         "-days", str(OPENSEARCH_CERT_DAYS)],
        service_gid, key_name, name,
    )


def _certificate_matches(
    host: Host, key_name: str, cert_name: str
) -> tuple[bool, str | None, str]:
    key, unavailable = _run_openssl(
        host, ["openssl", "pkey", "-in", str(secret_path(key_name)), "-pubout"]
    )
    if unavailable is not None:
        return False, unavailable, _OPENSSL_FIX
    assert key is not None
    if key.returncode != 0:
        return False, _child_problem(f"openssl read of {secret_path(key_name)}", key), _pair_fix(key_name, cert_name)
    cert, unavailable = _run_openssl(
        host, ["openssl", "x509", "-in", str(secret_path(cert_name)), "-noout", "-pubkey"]
    )
    if unavailable is not None:
        return False, unavailable, _OPENSSL_FIX
    assert cert is not None
    return cert.returncode == 0 and key.stdout.strip() == cert.stdout.strip(), None, ""


def write_secret(host: Host, name: str, value: str) -> str | None:
    """Write a secret as ``root:gideon`` at mode 0440 through the seam."""

    path = secret_path(name)
    if path.name != name or not name:
        return f"Invalid secret file name: {name!r}."
    service_gid = service_group_gid(host)
    if service_gid is None:
        return f"{SERVICE_GROUP_PROBLEM}."
    problem, _ = _write_secret(host, name, value, service_gid)
    return problem


def rotate_generated(host: Host, name: str) -> RotateResult:
    """Rewrite a file by kind, re-issuing a key's certificate after its move."""

    entry = registry_entry(name)
    if entry is None:
        return RotateResult(
            problem=f"Secret is not a generated registry entry: {name}.",
            fix="Choose a generated secret with rotation class rewrite.",
        )
    if entry.rotation != "rewrite":
        return RotateResult(
            problem=f"Secret {name} has rotation class {entry.rotation}, not rewrite.",
            fix="Choose a generated secret with rotation class rewrite.",
        )
    service_gid = service_group_gid(host)
    if service_gid is None:
        return RotateResult(problem=f"{SERVICE_GROUP_PROBLEM}.", fix=_SERVICE_GROUP_FIX)
    if entry.kind == "key":
        certificate = issued_certificate(name)
        problem, fix = _generate_key(host, name, service_gid, certificate.name)
        if problem is not None:
            return RotateResult(problem=problem, fix=fix)
        problem, fix = _issue_certificate(host, certificate.name, name, service_gid)
        if problem is not None:
            recovery = (
                f"{fix} Then sudo python3 -m gideon secrets rotate {name} again."
                if fix == _OPENSSL_FIX
                else f"Run {_APPLY_COMMAND}, then sudo python3 -m gideon secrets rotate {name} again."
            )
            return RotateResult(
                written=True, problem=problem, fix=recovery,
            )
        return RotateResult(written=True)
    if entry.kind == "certificate":
        assert entry.issued_from is not None
        problem, fix = _issue_certificate(host, name, entry.issued_from, service_gid)
        return RotateResult(written=problem is None, problem=problem, fix=fix)
    problem, written = _write_secret(host, name, token_secrets.token_urlsafe(32), service_gid)
    if problem is not None:
        # A value that reached the path is a rotation no consumer has yet:
        # apply converges its carriers, and a fresh rotation reaches the rest.
        fix = (
            f"Run sudo chown root:{SERVICE_GROUP} {secret_path(name)}, then {_APPLY_COMMAND}, "
            f"then sudo python3 -m gideon secrets rotate {name}."
            if written
            else f"Correct ownership and mode for {SECRETS_DIR}, then retry."
        )
        return RotateResult(written=written, problem=problem, fix=fix)
    return RotateResult(written=written)


def ensure_generated(host: Host, *, skip: Collection[str] = ()) -> EnsureResult:
    """Create absent passwords and keys, and repair certificates from their keys.

    ``skip`` names entries a selected directory never holds. The ``gideon-ci``
    sibling mounts production's engine key and skips the two model-server
    keys, Grafana's password, and SearXNG's key.
    """

    if host.geteuid() != 0:
        return EnsureResult(
            problem="generated secrets require root privileges.",
            fix="Run `sudo python3 -m gideon apply` as root, then retry.",
        )
    if not host.exists(SECRETS_DIR):
        return EnsureResult(
            problem=f"Secrets directory is missing: {SECRETS_DIR}.",
            fix="Run `sudo python3 -m gideon host provision --only secrets-dirs`, then retry.",
        )

    service_gid = service_group_gid(host)
    if service_gid is None:
        return EnsureResult(problem=f"{SERVICE_GROUP_PROBLEM}.", fix=_SERVICE_GROUP_FIX)

    created: list[str] = []
    printed: dict[str, str] = {}
    for secret in SECRET_REGISTRY:
        if secret.kind == "minted" or secret.name in skip:
            continue
        path = secret_path(secret.name)
        if secret.kind == "certificate":
            assert secret.issued_from is not None
            if not host.exists(path) or secret.issued_from in created:
                needs_issue = True
            else:
                matches, problem, fix = _certificate_matches(host, secret.issued_from, secret.name)
                if problem is not None:
                    return EnsureResult(tuple(created), printed, problem=problem, fix=fix)
                needs_issue = not matches
            if not needs_issue:
                continue
            problem, fix = _issue_certificate(host, secret.name, secret.issued_from, service_gid)
        elif secret.kind == "key":
            if host.exists(path):
                continue
            certificate = issued_certificate(secret.name)
            problem, fix = _generate_key(host, secret.name, service_gid, certificate.name)
        else:
            if host.exists(path):
                continue
            value = token_secrets.token_urlsafe(32)
            problem, _ = _write_secret(host, secret.name, value, service_gid)
            fix = f"Correct ownership and mode for {SECRETS_DIR}, then retry."
        if problem is not None:
            return EnsureResult(
                tuple(created),
                printed,
                problem=f"Unable to create generated secret: {problem}",
                fix=fix,
            )
        created.append(secret.name)
        if secret.kind == "password" and secret.print_once:
            printed[secret.name] = value
    return EnsureResult(tuple(created), printed)


_FINGERPRINT_KEY: Final = "webui_secret_key"
_SHA256_LINE: Final = re.compile(r"^([0-9a-fA-F]{64})\s+(.+)$")


def secrets_fingerprint(digests: Mapping[str, str]) -> str | None:
    """A fingerprint of the secret set that reveals nothing about any secret.

    It is a hash keyed by the digest of the generated, high-entropy
    ``webui_secret_key``: without that digest no guess at a human-chosen
    secret can be tested against it, so it may sit in a plaintext manifest.
    ``None`` when the key is absent (no secret set to fingerprint).
    """

    key = digests.get(_FINGERPRINT_KEY)
    if key is None:
        return None
    body = "\n".join(
        f"{name}:{digest.lower()}"
        for name, digest in sorted(digests.items())
        if name != _FINGERPRINT_KEY
    )
    return hashlib.sha256(f"{key.lower()}\n{body}".encode()).hexdigest()


def fingerprint(host: Host) -> str | None:
    """The live secret directory's fingerprint through the seam, or None."""

    try:
        result = host.run(
            ["find", str(SECRETS_DIR), "-type", "f", "-exec", "sha256sum", "{}", "+"]
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    digests: dict[str, str] = {}
    prefix = f"{SECRETS_DIR}/"
    for line in result.stdout.splitlines():
        match = _SHA256_LINE.match(line)
        if match is None:
            continue
        digests[match.group(2).removeprefix(prefix)] = match.group(1)
    return secrets_fingerprint(digests)

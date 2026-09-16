"""Command-line orchestration for ``python3 -m tools.imagebuild``."""

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from gideon.host.egress import load_egress_allowlist
from gideon.host.egress import render_errors as render_egress_errors
from gideon.host.images import (
    BuiltImagePin,
    ProxyEnvironment,
    compute_inputs_digest,
    load_image_lock,
    parse_registry,
    proxy_environment,
)
from gideon.host.images import (
    render_errors as render_image_errors,
)
from gideon.host.site import SiteConfig, load_site
from gideon.host.sysio import Host, PathLike, RealHost
from tools.imagebuild.build import (
    BuildPlan,
    build,
    build_plan,
    check,
    record,
)
from tools.pinwatch.fetch import Fetcher, FetchError, UrllibFetcher

_SITE_PATH: PathLike = "/etc/gideon/site.yaml"
_EGRESS_FIX = "allow these hosts from the box, or set egress_proxy"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.imagebuild")
    parser.add_argument("name")
    parser.add_argument("--to", metavar="REGISTRY")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _proxies(variables: Mapping[str, str]) -> dict[str, str]:
    """The ``ProxyHandler`` mapping for the resolved proxy variables (empty without a proxy)."""

    return {
        scheme: variables[name]
        for scheme, name in (("http", "HTTP_PROXY"), ("https", "HTTPS_PROXY"))
        if name in variables
    }


def _refusal(problem: object, fix: str) -> str:
    return f"tools.imagebuild: {problem}. Fix: {fix}"


def _row(name: str, status: str, detail: str, fix: str = "") -> str:
    suffix = f" Fix: {fix}" if fix else ""
    return f"{name}: {status} — {detail}{suffix}"


def _site_and_target(
    io: Host, site_path: PathLike, destination: str | None
) -> tuple[SiteConfig | None, str | None]:
    site_result = load_site(Path(site_path), host=io)
    if destination is not None:
        return site_result.config, destination
    if site_result.errors or site_result.config is None:
        print(
            _refusal(
                "a destination registry is unavailable from the site file",
                "Pass --to <registry> or correct /etc/gideon/site.yaml",
            ),
            file=sys.stderr,
        )
        return None, None
    return site_result.config, site_result.config.registry


def _probe_egress(fetcher: Fetcher, root: Path, io: Host) -> list[str] | None:
    result = load_egress_allowlist(root / "config/egress.yaml", host=io)
    if result.errors or result.allowlist is None:
        print(render_egress_errors(result.errors), file=sys.stderr)
        return None
    group = result.allowlist.group("image-build")
    if group is None:
        print(_refusal("egress allowlist has no image-build group", "Edit config/egress.yaml"), file=sys.stderr)
        return None
    unreachable: list[str] = []
    for host in group.hosts:
        try:
            fetcher.get(host.probe_url)
        except FetchError:
            unreachable.append(host.host)
    return unreachable


def _dockerfile(io: Host, path: Path) -> bytes | None:
    if not io.exists(path):
        print(_refusal(f"Dockerfile is missing: {path}", "Add the Dockerfile named by the built pin"), file=sys.stderr)
        return None
    try:
        return io.read_text(path).encode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(_refusal(f"Dockerfile is unreadable: {path} ({exc})", "Fix the Dockerfile"), file=sys.stderr)
        return None


def _print_result(name: str, result: object) -> int:
    refusal = bool(getattr(result, "refusal", False))
    problem = getattr(result, "problem", None)
    fix = getattr(result, "fix", "")
    if refusal:
        print(_refusal(problem or "Docker operation refused", fix), file=sys.stderr)
    else:
        print(_row(name, "failed", str(problem or "operation failed"), fix))
    return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    fetcher: Fetcher | None = None,
    root: Path | None = None,
    site_path: PathLike = _SITE_PATH,
) -> int:
    """Build or check one lock pin with injectable Host and HTTP seams."""

    options = _parser().parse_args(argv)
    io = host or RealHost()
    checkout = root or Path(__file__).resolve().parents[2]
    result = load_image_lock(checkout / "images.lock", host=io)
    if result.errors or result.lock is None:
        print(render_image_errors(result.errors), file=sys.stderr)
        return 1
    pin = next((item for item in result.lock.images if item.name == options.name), None)
    if pin is None:
        print(_refusal(f"unknown image '{options.name}'", "Correct the image name in images.lock"), file=sys.stderr)
        return 1
    if not isinstance(pin, BuiltImagePin):
        print(_refusal(f"image '{pin.name}' is mirrored, not built", "Use a built pin in images.lock"), file=sys.stderr)
        return 1
    if options.check and not pin.built:
        print(_refusal("built image is unbuilt", f"Build it with python3 -m tools.imagebuild {pin.name}"), file=sys.stderr)
        return 1
    site, destination = _site_and_target(io, site_path, options.to)
    if destination is None:
        return 1
    target = parse_registry(destination)
    if target is None:
        print(_refusal(f"registry destination is not usable: {destination}", "Correct the registry destination"), file=sys.stderr)
        return 1
    dockerfile = _dockerfile(io, checkout / pin.build / "Dockerfile")
    if dockerfile is None:
        return 1
    proxy = ProxyEnvironment()
    if site is not None and site.egress_proxy:
        proxy = proxy_environment(site, io)
        if not proxy.ok:
            print(_refusal(proxy.problem or "proxy is unusable", proxy.fix), file=sys.stderr)
            return 1
    # The egress probe goes through the same proxy the build will use.
    http = fetcher or UrllibFetcher(proxies=_proxies(proxy.variables))
    plan: BuildPlan
    try:
        plan = build_plan(pin, target, checkout, proxy.variables)
    except ValueError as exc:
        print(_refusal(exc, "Add a smoke command for this built image"), file=sys.stderr)
        return 1
    if options.dry_run:
        print(_row(pin.name, "would build", " ".join(plan.build_argv)))
        return 0
    if options.check:
        report = check(io, target, pin, plan.environment)
        if report.refusal:
            print(_refusal(report.problem, report.fix), file=sys.stderr)
            return 1
        if not report.ok:
            print(_row(pin.name, "failed", report.problem, report.fix))
            return 1
        print(_row(pin.name, "ok", plan.reference))
        return 0
    unreachable = _probe_egress(http, checkout, io)
    if unreachable is None:
        return 1
    if unreachable:
        print(_refusal(f"unreachable egress hosts: {', '.join(unreachable)}", _EGRESS_FIX), file=sys.stderr)
        return 1
    built = build(io, plan)
    if not built.ok:
        return _print_result(pin.name, built)
    assert built.digest is not None
    inputs_digest = compute_inputs_digest(pin.base_digest, pin.build_args, dockerfile)
    recorded = record(io, checkout, pin, built.digest, inputs_digest)
    if not recorded.ok:
        return _print_result(pin.name, recorded)
    print(_row(pin.name, "built", f"{plan.repository}@{built.digest}; images.lock and tests/fixtures/render updated: commit them"))
    return 0

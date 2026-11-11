# Security policy

## Reporting a vulnerability

Report a vulnerability privately through GitHub's vulnerability reporting: on the Security tab of `github.com/TNMD-FDO/GIDEON`, open Advisories and choose **Report a vulnerability**. Only the repository's maintainers see the report. Do not report a vulnerability in a public issue, a pull request, a chat, or a mailing list.

A useful report includes:

- the version that `python3 -m gideon --version` prints;
- the hardware profile, or that the server has no GPU;
- steps to reproduce, using made-up text rather than real case material;
- command output or a log excerpt, with real values replaced;
- what an attacker could do with the finding.

Leave out configuration values, secrets, and any client or case text. Replace hostnames, addresses, and directory or mailbox names with placeholders, as [CONTRIBUTING.md](CONTRIBUTING.md) describes.

## Scope

GIDEON's own code and configuration are in scope: the `gideon` package and its command-line tool, the rendered deployment (the Compose files, the Caddyfile, and the systemd units), the images built under `images/`, the database migrations, the preflight, install, and upgrade scripts, and the CI workflows. A pinned dependency at a vulnerable version is also in scope, as is a configuration that exposes a defect in a component, even when the defect itself belongs upstream.

A defect in an upstream project, such as Open WebUI, SearXNG, Qdrant, OpenSearch, PostgreSQL, Caddy, Grafana, Prometheus, or a model, should be reported to that project. GIDEON picks up the fix as a pin update. An office's own settings, secrets, network, directory, and hardware are outside this policy.

## After a report

The maintainers acknowledge the report on the advisory and keep it private until a fix is released. The fix ships as a new release, and the advisory is published alongside it, crediting the reporter unless they prefer otherwise. There is no guaranteed response time.

## Supported versions

Only the latest release is supported.

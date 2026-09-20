# Security policy

## Reporting a vulnerability

Report a vulnerability privately through the public repository's vulnerability
reporting: open the Security tab of `github.com/TNMD-FDO/GIDEON` and choose
"Report a vulnerability" on its Advisories page. The report reaches the
repository's administrators, the two CSAs who maintain GIDEON, and nobody
else. Never report a vulnerability in a public issue, a pull request, a chat,
or a mailing list.

Include the tag `python3 -m gideon --version` prints, the hardware profile or
the no-GPU declaration, a reproducer built from synthetic text, sanitized
diagnostics, and what the finding lets an attacker do. A reproducer uses a
made-up prompt, document, request, and response; diagnostics are the command
and its rows or a log excerpt with every real value replaced.

Never include a site file's values, a secret, or any chat, matter, or client
text, in a reproducer or a log excerpt alike. Replace a hostname, an address,
a directory name, or a mailbox with a placeholder named after its site key, as
[CONTRIBUTING.md](CONTRIBUTING.md) says for contributions. GIDEON's own rule
that client and case data never leave the box binds a vulnerability report
too.

## What is in scope

The `gideon` package and its CLI; the rendered stack (the Compose templates,
the Caddyfile, the units, and the Filters under
`compose/open-webui/functions/`); the built images under `images/`; the
migrations; the three scripts; the workflows; and GIDEON's exposure to an
upstream defect — a pin file (`host.lock`, `images.lock`, `models.lock`)
naming a version that is vulnerable, or the rendered configuration exposing a
component's defect — which is reported here as GIDEON's exposure even when the
defect itself belongs upstream.

## What is not in scope

An office's own `site.yaml` and secrets, its directory, its network and
hardware, and the host beyond what `host provision` sets are the office's own.
A defect in an upstream component — Open WebUI, the serving engine, SearXNG,
PostgreSQL and pgBackRest, Caddy, the exporters, Grafana and Prometheus, or a
model — is reported to that project; a fix there reaches GIDEON as a reviewed
pin update. The two scopes meet at the pin: the defect goes upstream, the
exposure comes here.

## What happens next

The CSAs acknowledge the report on the advisory and track it privately under
the advisory's identifier, so no detail a public reader could use appears
anywhere until the fix ships. It ships as a tag every office upgrades to with
`./upgrade.sh <tag>`, and the advisory is published with that tag, crediting
the reporter unless they decline. No response time is promised.

## Supported versions

The latest tag. A fix ships as a new tag, and every office upgrades to it;
no older tag is supported. Before `v1.0.0` GIDEON runs at one office.

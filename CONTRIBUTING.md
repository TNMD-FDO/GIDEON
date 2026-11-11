# Contributing to GIDEON

## The dedication

GIDEON is in the public domain. It is a work of the United States government under 17 U.S.C. § 105 and is dedicated worldwide under CC0 1.0 Universal; see `LICENSE`.

**By submitting a contribution to this repository you dedicate that
contribution to the public domain under the CC0 1.0 Universal dedication,
waiving all copyright and related rights you hold in it, worldwide.** This term exists because not every contributor is a federal employee. Community Defender Organization staff, for example, are nonprofit employees who *do* hold copyright in what they write, and GIDEON stays freely distributable only if every contribution carries the same dedication. If you cannot or do not wish to dedicate a contribution this way, do not submit it.

## How contributions are accepted

Contribution is by invitation: request access through the [access-request form](https://github.com/TNMD-FDO/GIDEON/issues/new?template=access-request.yml), and the maintainers review each request. This public repository receives one commit per release, so pull requests opened here are not merged.

`main` is always deployable, and feature branches are short-lived. Dependency pins change only through pull requests, which the pin watch opens on `pin-watch/*` branches. A person merges each one, because a merge is a release decision.

GIDEON's own images, `postgres` and `gideon`, are built on the maintainers' build server rather than in CI. A change under `images/`, or to a built image's inputs, needs the rebuild described in `docs/runbooks/built-images.md` before it is committed. Otherwise CI fails on purpose.

## Keeping office values out of the tree

No real hostname, address, directory, or mailbox belongs in code, tests, fixtures, or documentation. Use RFC 2606 names such as `gideon.example.org` and RFC 5737 networks such as `192.0.2.0/24`. In captured evidence, use a placeholder named after the setting, such as `<hostname>` or `<lan_cidrs[0]>`, or `<redacted>` where no setting applies. `python3 -m tools.redact --site /etc/gideon/site.yaml` makes these replacements in a captured transcript, and `tests/test_office_values.py` checks the whole tree.

## Development setup and the gate

A checkout runs without an install step:

```bash
python3 -m gideon --help
```

The commands that run from a checkout on a fresh Ubuntu Server install (`host`, `preflight`, `render`, `apply`, `secrets rotate`, `tls reload`, `registry mirror`, and `users reconcile`) use only the standard library and `python3-yaml`, because `render` runs before any container image is pulled. CI enforces this. The [command reference](docs/commands.md) lists what each command needs to run.

The development tools are pinned in `requirements-dev.txt`, alongside a copy of the `gideon` image's dependencies so that type checking and the unit tests can see the service's imports. The product itself installs nothing from PyPI.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

Run the gate before every commit. It is the same check CI runs: it compares the environment with the pins, then runs lint, type checking, and the unit tests, stopping at the first failure.

```bash
python3 -m tools.gate
```

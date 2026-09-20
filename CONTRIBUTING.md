# Contributing to GIDEON

## The dedication

GIDEON is a public-domain work: a United States government work under
17 U.S.C. § 105, dedicated worldwide under CC0 1.0 Universal (see `LICENSE`,
[ADR-0004](docs/adr/0004-cc0-public-domain-dedication.md)).

**By submitting a contribution to this repository you dedicate that
contribution to the public domain under the CC0 1.0 Universal dedication,
waiving all copyright and related rights you hold in it, worldwide.** This
term exists because not every contributor is a federal employee: Community
Defender Organization staff, for example, are nonprofit employees who *do*
hold copyright in what they write, and GIDEON stays freely distributable only
if every contribution carries the same dedication.

If you cannot, or do not wish to, dedicate your contribution under CC0-1.0,
do not submit it.

The issue forms and the pull-request template restate this dedication where a
contributor meets it.

## Reporting a vulnerability

Report a vulnerability privately through [SECURITY.md](SECURITY.md), never as a
public issue or pull request.

## How the work is organized

- The build spec is `.scratch/greenfield-spec/spec.md`; the vocabulary is
  the development repository's `CONTEXT.md`; decisions are recorded in
  `docs/adr/`.
- The work tracker is `.scratch/` — committed markdown, authoritative despite
  the name (see `docs/agents/issue-tracker.md`) — in the development
  repository, `TNMD-FDO/GIDEON-dev`, which is private; the public
  `TNMD-FDO/GIDEON` receives a filtered export of every tag.
- How a person works the tracker — the reading order, one worked cycle, the
  tracker's shapes, the first-day constraints — is in the development
  repository's `docs/agents/onboarding.md`.
- A contribution is made in `TNMD-FDO/GIDEON-dev`. Request access through the
  public repository's [access-request issue form](https://github.com/TNMD-FDO/GIDEON/issues/new?template=access-request.yml);
  a CSA decides each request. A pull request on the public repository is not
  merged because the export overwrites `main`.
- Trunk-based development: `main` is always deployable, feature branches are
  short-lived, every slice ends in a tagged release (spec §2.1, §22). Bump
  pull requests come from the pin watch (`pin-watch/*` branches, ADR-0031)
  and are merged by a human, never auto-merged — a merge is a release
  decision.
- GIDEON's own images (`postgres` and `gideon`; `images/<name>/Dockerfile`, the built pins in
  `images.lock`) are built on the box, never by CI. A change under `images/`
  or to a built pin's inputs needs the rebuild in
  `docs/runbooks/built-images.md` before the commit —
  otherwise the hosted checks go red on purpose.
- The tree carries no office value. A machine, network, directory, or mailbox
  name in code, tests, fixtures, or a public document is a documentation value
  (an RFC 2606 name such as `gideon.example.org`, an RFC 5737 network such as
  `192.0.2.0/24`); in the tracker's evidence it is a placeholder named after
  its site key (`<hostname>`, `<auth.ldap.host>`, `<lan_cidrs[0]>`), and
  `<redacted>` where no key names it. A transcript captured on the box passes
  through `python3 -m tools.redact --site /etc/gideon/site.yaml` before it is
  added, which writes those placeholders; `tests/test_office_values.py` holds
  the whole tree to that rule and names the fix in every finding.

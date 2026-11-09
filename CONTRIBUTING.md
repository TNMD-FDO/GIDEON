# Contributing to GIDEON

## The dedication

GIDEON is a public-domain work: a United States government work under
17 U.S.C. § 105, dedicated worldwide under CC0 1.0 Universal; see `LICENSE`.

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

- Contribution is by invitation through the [access-request
  issue form](https://github.com/TNMD-FDO/GIDEON/issues/new?template=access-request.yml);
  a CSA decides each request under this document's dedication. This repository
  receives one commit per release, so a pull request here is not merged.
- Trunk-based development: `main` is always deployable, feature branches are
  short-lived, and every slice ends in a tagged release. Bump pull requests
  come from the pin watch on `pin-watch/*` branches, since a pin moves only
  through a pull request, and a human merges each one, never automatically: a
  merge is a release decision.
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

## Writing public prose

Write for the reader of each document:

- The README is for a receiving office's computer systems administrator
  (CSA), who decides whether and how to install.
- The overview, `docs/OVERVIEW.md`, is for an attorney or office manager.
- `CONTRIBUTING.md` and `SECURITY.md` are for a contributor, developer, or
  security researcher.
- The runbooks are for a CSA who has installed GIDEON.
- Release notes and the changelog are for every user and the receiving
  office's CSA.

Follow these rules:

- Address the operator as "you."
- Keep one idea per sentence, with at most 30 words.
- Start a new sentence for each new fact.
- Do not use an em-dash to connect clauses.
- Use "never" only for a hard refusal the software makes.
- Put a command once, in the step that runs it.
- Keep tracker vocabulary out of public prose: slice, ticket, TRIP, effort,
  and cycle.
- Define an operator term where each document first uses it. CSA, apply,
  provision, the quiet window, the eval identity, and the build box are
  operator terms.
- Use "the server" in the README and the overview.
- You may use "the box" in a runbook because command output uses it.

The gate holds these public documents to the 30-word sentence limit and names
each line to split.

This sentence fails the style guide:

> "**Login** — Use the CSA's own Linux login with `sudo`, never root; this login will own `/opt/gideon`."

The passing form is:

> "**Login.** Sign in with your own Linux account, one that can `sudo`. Don't use root. This account will own `/opt/gideon`."

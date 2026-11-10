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

## Quickstart

From a checkout, no install step:

```bash
python3 -m gideon --help
```

`gideon host …`, `preflight`, `render`, `apply`, `secrets rotate`, `tls reload`, `registry mirror`, and `users reconcile` run from the checkout on a bare Ubuntu Server install. They use only the standard library and `python3-yaml`, because render runs before any image is pulled. CI enforces that boundary. `render`, `apply`, `secrets rotate`, `tls reload`, `users reconcile`, `status`, and `index report` need root; `registry mirror` needs Docker access; `alerts test` and `worker verify` need root and a converged `apply`. `apply`, `secrets rotate`, and `engine verify` take the engine lock and refuse while an evaluation holds it; `secrets rotate grafana_admin_password` recreates nothing and takes none. `status` reads firing pages, office proposals, and host facts, plus the developer block on the build box, without writing. `index report` reads the corpus inventory from the record and writes nothing. The installed `gideon` command runs every command as root. A host without a GPU is provisioned once with `sudo python3 -m gideon host provision --no-gpu`; every later command reads that declaration. `users reconcile` reports until given `--now`, and a rendered systemd timer runs it nightly. `retention sweep` needs root and a converged `apply`, and a rendered timer runs it nightly at 02:00. It deletes, through the frontend, every chat idle past `retention.chats`, the uploads it names, and the feedback rows past that interval or left on a swept chat.

The development toolchain is `requirements-dev.txt`, in an untracked project venv. It holds the seven toolchain pins and, beside them, a copy of the `gideon` image's dependency set so mypy and the unit suite see the service's imports. It is never the product's path, which installs nothing from PyPI:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

The gate is run before any commit and is exactly what CI runs. It runs the environment comparison, then lint, types, and the unit tests, stopping at the first failure, the venv found beside the tree:

```bash
python3 -m tools.gate
```

## Writing public prose

A public document explains what GIDEON is, what it does, and why it is built the way it is. A step or a rule arrives with the reason it exists, because a reader who knows why a step is there can handle a situation the document did not foresee. The register is plain and professional, without jokes or sales language, so that every claim rests on what the system actually does.

Write in paragraphs, with sentences of natural length that finish the thought they begin. A sentence may carry a fact together with its cause or its consequence, and the next sentence picks up where it left off, so the explanation reads as one line of reasoning rather than a list of separate statements.

Do not say who a page is for, address the reader by a role, or send one kind of reader to another document. A document that opens by naming its reader spends its first sentence on someone other than GIDEON, and it tells everyone else that the page is not theirs, when attorneys, administrators, and contributors may all have reason to read it. A link to another document describes what that document contains, so the reader can decide whether to follow it. "You" is the ordinary second person of a step's instruction, as in "Sign in with your own Linux account."

A few rules keep the wording tied to facts:

- Keep the tracker words "slice", "ticket", "TRIP", "effort", and "cycle" out of public prose, because they name how the work is organized and say nothing about the software.
- Define an operator term where each document first uses it, so the document can be read on its own. CSA, apply, provision, the quiet window, the eval identity, and the build box are operator terms.
- Put a command once, in the step that runs it, so there is one place to follow it and one place to correct it.
- Keep "never" for a hard refusal the software makes, so that the word always means the system itself will stop.
- Do not use an em-dash to connect clauses. Join them with a word that says how they relate, or end the sentence.
- Say "the server" in the README and the overview, which describe the system as a whole. A runbook may say "the box", because the command output it quotes uses that word.

The README's original opening shows the difference:

> "This README is for you, the computer systems administrator (CSA), deciding whether and how to install GIDEON."

It names a reader and says nothing yet about the system. The opening the guide asks for says what GIDEON is and where it runs:

> "GIDEON is a legal AI system a federal defender office runs on a server it owns and controls. It is not a hosted service, and case material stays on that server."

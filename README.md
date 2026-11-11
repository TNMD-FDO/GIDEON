# GIDEON

[![CI](https://github.com/TNMD-FDO/GIDEON/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TNMD-FDO/GIDEON/actions/workflows/ci.yml)

## What is GIDEON?

GIDEON is a legal AI system that a federal defender office runs on its own server. It combines a general-purpose assistant with legal research over a collection of court opinions, kept with their page numbers, the citations between them, and a record of known overrulings. The citations in a research answer are computed and verified instead of written by the model.

The Office of the Federal Public Defender for the Middle District of Tennessee builds it to support the attorneys, investigators, paralegals, legal assistants, and support staff in federal criminal defense and capital habeas work. The [overview](docs/OVERVIEW.md) describes in plain words how the system works and how its answers are checked.

## How GIDEON is designed

Case material is confidential, so GIDEON is not a hosted service: it runs on the office's own server and the material stays there. That takes a server and the technical staff to maintain it.

A language model can get a citation's page, year, or reporter wrong, so GIDEON's model does not write citations. Code produces each citation from the collection of court opinions and verifies it before it is shown.

GIDEON is a public-domain work of the United States government, so any office may install or adapt it.

## Status

GIDEON is pre-release: it installs, runs the general-purpose assistant, backs up, and upgrades on its first server, but no staff use it yet. Legal research is not built, and another office cannot install until `v1.0.0` publishes the release images, while this tree is `v0.3.196`. [`CHANGELOG.md`](CHANGELOG.md) records what each release changed.

## Install

### Before you begin

- **Operating system.** Use Ubuntu Server 26.04 on `x86_64`, because provision prepares the server and refuses any other release or architecture.
- **Packages.** Install `git` and `python3-yaml` with `sudo apt install -y git python3-yaml`. This is safe to run if they are already installed.
- **Login.** Sign in with your own Linux account, one that can `sudo`, because that account will own `/opt/gideon`. Do not use root.
- **Hardware.** The reference profile `2x96v-256d` needs 2 × NVIDIA RTX PRO 6000 Blackwell (any edition) with 96 GB VRAM per GPU, plus 256 GB RAM and a 4000 GB data volume. Preflight checks whether installation will work and refuses a hardware shortfall. You can instead declare a server without a GPU at step 2, and preflight will not check these figures on that server.
- **Download.** Apply turns your settings and the release into a running system. Allow about 31 GB of model files at the first apply, including on a server without a GPU.
- **Office services.** Have your directory, DNS, backup target, certificates, and mail relay ready as described in [office services setup](docs/runbooks/office-services-setup.md).

### Steps

Step 1 creates your checkout, `/opt/gideon`, and the later steps run from it.

1. Create the install home, clone a release into it as your own account, enter it, and verify the release tag:

   ```bash
   sudo install -d -o <account> -g <account> /opt/gideon
   git clone --branch <tag> https://github.com/TNMD-FDO/GIDEON /opt/gideon
   cd /opt/gideon
   git -c gpg.ssh.allowedSignersFile=config/release-signers verify-tag <tag>
   ```

   The first clone trusts the key it carries, so this proves only that the tag was signed by the key in that tree.

   `<account>` is your Linux login, the one `id -un` prints, rather than root or a GitHub account. `<tag>` is a release tag. For example, the login might be `csa` and the tag `v0.3.0`. Tags are listed newest first in [`CHANGELOG.md`](CHANGELOG.md) and on the [repository's tags page](https://github.com/TNMD-FDO/GIDEON/tags).

   `upgrade` runs git as the directory's owner, so no root-owned file lands in the tree. The repository is public, so the clone and `upgrade`'s fetch need no GitHub account or credentials.

2. Provision your server:

   ```bash
   sudo python3 -m gideon host provision
   ```

   The first run prints the backup public key and the age identity **once**, so store the identity in your office password manager and authorize the public key on your backup target before going on. If you see a `reboot-required` row, reboot and provision again until every step reads `ok`. For a server without a GPU, add `--no-gpu` to this command once, and every later command reads that declaration.

3. Write the site file, `/etc/gideon/site.yaml`, from `config/site.example.yaml`. The site file records your office's settings, and the example is authoritative for its keys and defaults. Place your supplied secrets under `/etc/gideon/secrets/`. Release images are not published for other offices until `v1.0.0`.

4. Provision again, because the steps that depend on your office's settings are blocked until the site file exists:

   ```bash
   sudo python3 -m gideon host provision
   ```

5. Run preflight to check whether installation will work on your server:

   ```bash
   sudo ./preflight.sh
   ```

6. Install GIDEON:

   ```bash
   sudo ./install.sh
   ```

   Install runs preflight, apply, user reconciliation, engine verification, backup, and a restore drill, then prints your site's URL as the last line. The first apply generates the release's secrets and downloads the profile's models. It prints each break-glass password exactly once: an emergency administrator password for the chat interface or Grafana.

   Install is safe to re-run on a live server: a re-run leaves an unchanged system running, takes a fresh incremental backup set, runs a restore drill, and prints the URL.

7. Send one test alert through your mail relay:

   ```bash
   sudo python3 -m gideon alerts test
   ```

From provision on, you can re-run every command. A refusal prints its fix, so make that fix and run the same command again. The [command reference](docs/commands.md) explains the corpus and index commands you run after install, and every other command.

Provision also installs the `gideon` command: from any directory, `gideon <command>` runs `sudo python3 -m gideon <command>` from `/opt/gideon`. Running `gideon` alone prints where to start. The [install runbook](docs/runbooks/install-upgrade.md) gives this sequence in full and every refusal's fix.

## Upgrading

An upgrade, run with `upgrade.sh`, moves the server to a newer release after taking a full backup set. A rollback restores that set and applies the previous release. The steps are in [docs/runbooks/install-upgrade.md §2](docs/runbooks/install-upgrade.md).

## Documentation

| What | Where |
|---|---|
| Where to start | [`docs/runbooks/start-here.md`](docs/runbooks/start-here.md) |
| Command reference | [`docs/commands.md`](docs/commands.md) |
| Changelog index | [`CHANGELOG.md`](CHANGELOG.md) |
| Release notes | [`docs/release-notes/`](docs/release-notes/) |
| Site file example and editor schema | [`config/site.example.yaml`](config/site.example.yaml) · [`config/site.schema.json`](config/site.schema.json) |
| Release files, pins, and who changes them | [`docs/runbooks/release-files.md`](docs/runbooks/release-files.md) · [`docs/runbooks/built-images.md`](docs/runbooks/built-images.md) · [`docs/runbooks/model-upgrade.md`](docs/runbooks/model-upgrade.md) |
| Install, upgrade, rollback, removal, and office services | [`docs/runbooks/install-upgrade.md`](docs/runbooks/install-upgrade.md) · [`docs/runbooks/office-services-setup.md`](docs/runbooks/office-services-setup.md) |
| Backup, restore, and the drill | [`docs/runbooks/backup-restore.md`](docs/runbooks/backup-restore.md) |
| Observability and alerting | [`docs/runbooks/observability.md`](docs/runbooks/observability.md) |
| Monthly guardrail and feedback reviews | [`docs/runbooks/guardrail.md`](docs/runbooks/guardrail.md) · [`docs/runbooks/feedback-packet.md`](docs/runbooks/feedback-packet.md) |
| Reporting a vulnerability | [`SECURITY.md`](SECURITY.md) |

## Contributing and reporting a vulnerability

Bugs and enhancement requests go through the public repository's [issue forms](https://github.com/TNMD-FDO/GIDEON/issues/new/choose), and the maintainers review each report and answer on the issue. Vulnerabilities are reported privately through [`SECURITY.md`](SECURITY.md), rather than in an issue.

Contribution is by invitation through the [access-request issue form](https://github.com/TNMD-FDO/GIDEON/issues/new?template=access-request.yml), and the maintainers decide each request under the dedication in [`CONTRIBUTING.md`](CONTRIBUTING.md). The public repository receives one commit per release, so a pull request there is not merged. [`CONTRIBUTING.md`](CONTRIBUTING.md) also describes the development setup and the gate.

## License

GIDEON is a public-domain United States government work under 17 U.S.C. § 105, with a worldwide CC0-1.0 dedication recorded in [`LICENSE`](LICENSE). Contributions are accepted only under the same dedication, as [`CONTRIBUTING.md`](CONTRIBUTING.md) explains.

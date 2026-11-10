# GIDEON

[![CI](https://github.com/TNMD-FDO/GIDEON/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TNMD-FDO/GIDEON/actions/workflows/ci.yml)

GIDEON is a legal AI system a federal defender office runs on a server it owns and controls.
It pairs a general-purpose assistant with legal research over court decisions, whose citations are computed and verified.
The Office of the Federal Public Defender for the Middle District of Tennessee builds it.
If you are an attorney or office manager, read the [plain-words overview](docs/OVERVIEW.md) for the system and how its answers are checked.

## Who it is for

This README is for you, the computer systems administrator (CSA), deciding whether and how to install GIDEON.
GIDEON is built for attorneys, investigators, paralegals, legal assistants, and support staff doing federal criminal defense and capital habeas work.
Any office may install or adapt it, since it is public-domain work.
It is not a hosted service: it runs on your office's own server, and case material stays there.
You need a server of your own and technical staff to run it.

## Status

GIDEON is pre-release: it installs, runs the general-purpose assistant, backs up, and upgrades on its first server; no staff use it yet.
Legal research is not built; another office cannot install until `v1.0.0` publishes the release images; this tree is `v0.3.195`.
Read [`CHANGELOG.md`](CHANGELOG.md) for what each release changed.

## Install

### Before you begin

- **Operating system.** Use Ubuntu Server 26.04 on `x86_64`.
  Provision prepares the server and refuses any other release or architecture.
- **Packages.** Install `git` and `python3-yaml` with `sudo apt install -y git python3-yaml`.
  This is safe to run if they are already installed.
- **Login.** Sign in with your own Linux account, one that can `sudo`.
  Don't use root.
  This account will own `/opt/gideon`.
- **Hardware.** The reference profile `2x96v-256d` needs 2 × NVIDIA RTX PRO 6000 Blackwell (any edition), with 96 GB VRAM per GPU.
  You also need 256 GB RAM and a 4000 GB data volume.
  Preflight checks whether installation will work and refuses a hardware shortfall.
  You can instead declare a no-GPU host at step 2; preflight does not judge these figures on that host.
- **Download.** Allow about 31 GB of model files at the first apply, including on a no-GPU host.
  Apply is the command that turns your settings and the release into a running system.
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

   `<account>` is your Linux login, the one `id -un` prints, rather than root or a GitHub account.
   `<tag>` is a release tag.
   For example, you could use login `csa` and tag `v0.3.0`.
   Tags are listed newest first in [`CHANGELOG.md`](CHANGELOG.md) and on the [repository's tags page](https://github.com/TNMD-FDO/GIDEON/tags).

   The owner matters later: `upgrade` runs git as whoever owns the directory, so no root-owned file lands in the tree. The repository is public, so the clone and `upgrade`'s fetch need no GitHub account or credentials.

2. Provision your server:

   ```bash
   sudo python3 -m gideon host provision
   ```

   The first run prints the backup public key and the age identity **once**.
   Store the identity in your office password manager and authorize the public key on your backup target before going on.
   If you see a `reboot-required` row, reboot and provision again until every step reads `ok`.
   For a server without a GPU, add `--no-gpu` to this command once; every later command reads that declaration.

3. Write the site file, `/etc/gideon/site.yaml`, from `config/site.example.yaml`.
   The site file records your office's settings, and the example is authoritative for its keys and defaults.
   Place your supplied secrets under `/etc/gideon/secrets/`.
   Release images are not published for other offices until `v1.0.0`.

4. Provision again; the steps that depend on your office's settings are blocked until the site file exists:

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

   Install runs preflight, apply, user reconciliation, engine verification, backup, and a restore drill, then prints your site's URL as the last line.
   The first apply generates the release's secrets and downloads the profile's models.
   It prints each break-glass password exactly once: an emergency administrator password for the chat interface or Grafana.
   Install is safe to re-run on a live server.
   A re-run leaves an unchanged system running, takes a fresh incremental backup set, runs a restore drill, and prints the URL.

7. Send one test alert through your mail relay:

   ```bash
   sudo python3 -m gideon alerts test
   ```

From provision on, you can re-run every command.
A refusal prints its fix; make that fix and run the same command again.
The [command reference](docs/commands.md) explains the corpus and index commands you run after install, and every other command.
Provision installs the `gideon` command.
From any directory, `gideon <command>` runs `sudo python3 -m gideon <command>` from `/opt/gideon`.
Running `gideon` alone prints where to start.
The [install runbook](docs/runbooks/install-upgrade.md) gives this sequence in full and every refusal's fix.

## Upgrading

An upgrade, run with `upgrade.sh`, moves your server to a newer release after taking a full backup set.
A rollback restores that set and applies the previous release.
The steps are in [docs/runbooks/install-upgrade.md §2](docs/runbooks/install-upgrade.md).

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

Report a bug or request an enhancement through the public repository's [issue forms](https://github.com/TNMD-FDO/GIDEON/issues/new/choose).
The project's CSAs review each report and answer on the issue.
Report a vulnerability privately through [`SECURITY.md`](SECURITY.md), rather than an issue.
Contribution is by invitation through the [access-request issue form](https://github.com/TNMD-FDO/GIDEON/issues/new?template=access-request.yml).
A CSA decides your request under [`CONTRIBUTING.md`](CONTRIBUTING.md)'s dedication.
The public repository receives one commit per release, so a pull request there is not merged.
If you are a developer, see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the toolchain.

## License

GIDEON is a public-domain United States government work under 17 U.S.C. § 105, with a worldwide CC0-1.0 dedication.
See [`LICENSE`](LICENSE) for the dedication.
Your contributions are accepted only under the same dedication, as [`CONTRIBUTING.md`](CONTRIBUTING.md) explains.

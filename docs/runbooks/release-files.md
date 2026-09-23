# The release's files (CSA runbook material)

This runbook explains the committed files named by a refusal, and who may change them. Follow the numbered section named by the command, make the stated correction, then run that command again.

## 1. What the release ships, and who edits what

Every file in this runbook is release content. The office edits `/etc/gideon/site.yaml` and the supplied secrets under `/etc/gideon/secrets/`; it does not edit files in the install home. A value in a release file changes in a release, usually through a pin-watch pull request.

A refusal naming one of these files at `host provision`, `preflight`, `apply`, `models pull`, or `gideon proposals` means the copy in `/opt/gideon` differs from the release tag. Run `git status --short` there to see the changed file. As the install directory's owner, restore that file from the release tag or clone the tag again, then repeat the refused command. The refusal's “Edit …” instruction is for the person responsible for changing the file in a release.

## 2. `host.lock`

This file records the operating-system release, the kernel tested, the NVIDIA driver package and branch, and the driver version reached on the reference box. It also sets minimum versions for Docker, Compose, and the GPU toolkit. The remaining records identify the release registry image, the self-hosted CI runner's release and its checksum, the dated acceptance-machine image and its checksum, and the reference box's storage-controller and virtual-disk facts.

`host provision` converges the host to these records, and `preflight` refuses when the lock is missing or invalid. A driver proposal does not mark a version tested: move that record only after the reference box has converged on the version.

## 3. `images.lock`

Mirrored images are pinned by their upstream tag and the digest of the published image index. The two GIDEON-built images are pinned by their base image and digest, build inputs, an input digest, and the digest actually pushed to the release registry.

`registry mirror` copies the pinned image digests. After a release changes an image pin, `apply` recreates the affected service. Complete a built-image change on the build box using [the built-images procedure](built-images.md); a pull request alone does not build or publish the image.

## 4. `models.lock`

The hardware profiles contain the minimum host requirements checked by `preflight`. Each profile selects models by repository and revision, records the digest and size of every file to fetch, assigns each model to a GPU, and carries the engine settings used to serve it. The profile's memory table gives each known Compose service its memory limit and names the model role for services that load a model.

`models pull` fetches and verifies exactly the files named by the selected profile. It does not select additional files from a model repository.

## 5. `config/egress.yaml`

The allowlist groups destinations by the work that needs them:

- `host-provisioning` serves `host provision`: the operating-system, driver, and Docker packages, the CI runner, the registry image and the images mirrored from Docker Hub and NVIDIA's registry, and the acceptance-machine image.
- `install-upgrade` serves installation and upgrade: model downloads and the images published on GitHub's registry. `models pull` prints any host a download traverses that this group does not cover.
- `corpus` serves corpus acquisition.
- `image-build` serves the package downloads made while building GIDEON's own images on the build box; an office never builds.

`preflight` probes the `host-provisioning` and `install-upgrade` groups. Allow the listed destinations through the office proxy or firewall.

## 6. `courts.yaml` and its hand table

`courts.yaml` maps each CourtListener court identifier to its circuit, state, level, and name. The site's `jurisdiction` identifiers are looked up here. If an identifier is absent, `preflight` reports the nearest identifier at the required level.

The generated map comes from CourtListener's bulk courts CSV and the hand table at `tools/courtmap/geography.yaml`. A maintainer runs `python3 -m tools.courtmap --csv <courts-YYYY-MM-DD.csv.bz2>` by hand; the generator does not fetch data and is never run on a box. The hand table has five parts: state-to-circuit assignments, federal-court assignments, state-court placements, deliberately unplaced codes, and reviewed corrections. A state-coded court not placed by the table is a deliberate generator failure that must be reviewed before the map is regenerated.

If a box refuses `courts.yaml`, restore the file from the installed release tag; do not regenerate it there. If the generator refuses, update the hand table only after reviewing the court's placement, then regenerate and verify the map.

## 7. `config/triggers.yaml`

This file lists improvement triggers, each with a condition and one of three states: `watching`, `acted`, or `retired`. A state changes only through a pull request, so the release history records each decision.

`gideon proposals` reads the registry and reports which watching triggers meet their conditions and which remain unmeasurable. A refusal naming the file is fixed by restoring it from the installed release tag, then running `gideon proposals` again.

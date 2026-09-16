# Building a GIDEON image (CSA runbook material)

Ticket 16. GIDEON's own images — today `postgres`, Postgres 18 plus pgBackRest —
are built **on the box, never by CI**: a build is not byte-reproducible, so
`images.lock` records the digest that was actually pushed, and the hosted checks
prove the lock is self-consistent (`inputs_digest` recomputed from the base
digest, the build arguments, and the Dockerfile). The release registry on the
box (`/data/registry`, in the backup set) is the only place a built digest
exists before 1.0; at the public flip CI copies the pinned digest to GHCR, it
does not rebuild.

## 1. When a build is due

- A **pin-watch proposal** on a built pin: `images.postgres` (the stock base
  moved) or `images.postgres.build_args.PGBACKREST_VERSION` (PGDG published a
  newer pgBackRest). Its hosted checks are red by design until the rebuild.
- A change under `images/<name>/` — the Dockerfile is a build input, so the
  lock's `inputs_digest` stops matching and the hosted checks go red.
- A new built pin, written with `digest: unbuilt` and `inputs_digest: unbuilt`.

## 2. The order: build → record → commit → merge (never the reverse)

On the box, in a checkout of the branch that carries the change (for a proposal
pull request: `git fetch origin && git checkout pin-watch/<pin id>`):

1. `python3 -m gideon registry mirror` (Docker access). Expected before a
   build: `postgres/base: present` (or `mirrored` on a base bump) and
   `postgres: failed — … Fix: Build and push it with …` — the lock names a
   digest the registry does not hold yet.
2. `sudo python3 -m tools.imagebuild postgres --to 127.0.0.1:5000` — probes the
   build's egress, builds from the mirrored base by digest, runs the smoke
   check (`pgbackrest version`), pushes, records `digest` and `inputs_digest`
   in `images.lock`, and regenerates `tests/fixtures/render/`. Files it writes
   stay owned by you (ownership is restored after `sudo`). A failed smoke
   check pushes nothing and leaves the lock untouched.
3. `python3 -m tools.imagebuild postgres --to 127.0.0.1:5000 --check` — proves
   the registry's image against the lock: digest present, labels equal the
   lock's inputs, smoke check passes. The self-hosted CI runs the same check
   after every merge (`tests/contract/built_images.py`).
4. Commit `images.lock` and `tests/fixtures/render/` on the branch and push.
   The hosted checks go green; the next pin-watch run reports the branch
   `completed` and touches it no further (a rebase, if `main` moved, is yours
   at merge time).
5. Merge when green. The push-only chain runs `mirror-images` (with the
   built-image contract) then `frontend-contract`; a human tags the release
   (§2.1). The running stack moves at the next `sudo python3 -m gideon apply`:
   the store tier is recreated onto the new image, so Postgres restarts for
   about a minute — choose the moment.

## 3. Reading the rows

- `postgres/base: present | mirrored` — the stock base, kept in the same
  repository under its own tag; a rebuild starts FROM it, so a build needs no
  egress for the base.
- `postgres: present` — the built digest the lock names is in the registry.
- `postgres: failed — … Fix: Build and push it with …` — build per §2, or,
  after a `/data/registry` loss, restore the registry from the backup set
  (`sudo python3 -m gideon restore --from staging`, or `--from target`;
  `/data/registry` is one of the set's file roots — ticket 06's runbook,
  `backup-restore.md`); until then `apply` refuses at its registry
  stage.
- Two builds from the same inputs produce two digests. The lock records the
  one pushed last; earlier ones stay in the registry until `registry gc`
  exists.

## 4. Egress

The base comes from the loopback registry. Inside the build, apt reaches
`apt.postgresql.org` and `deb.debian.org` — the allowlist's `image-build`
group (`config/egress.yaml`), probed by the tool before it builds and never by
preflight, because an office never builds. Behind `egress_proxy` the tool
passes Docker's predefined proxy build arguments, which Docker keeps out of
the image history and cache.

## 5. Docker access

The tool needs Docker: run it with `sudo` (it restores ownership of the files
it writes to you) or as a member of the docker group. Nothing it does needs
root otherwise.

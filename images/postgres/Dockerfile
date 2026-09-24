# GIDEON's Postgres image: the pinned stock base plus pgBackRest. Every version
# here is an images.lock value passed in as a build argument — nothing in this
# file can go stale — and the only way it is built is
# `python3 -m tools.imagebuild postgres` on the box, which records the pushed
# digest in the lock. Backups configure pgBackRest at apply time; this image
# only carries the binary. The base's postgres user, entrypoint, and PGDG apt
# source are inherited unchanged.
ARG BASE
FROM ${BASE}
ARG PGBACKREST_VERSION
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends "pgbackrest=${PGBACKREST_VERSION}"; \
    rm -rf /var/lib/apt/lists/*

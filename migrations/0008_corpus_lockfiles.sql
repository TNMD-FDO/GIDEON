-- Corpus cuts keep their lockfile and source bindings in the record.
-- This migration is forward-only; corrections are appended later.

CREATE TABLE corpus_lockfiles (
    label text PRIMARY KEY CHECK (label ~ '^corpus-[0-9]{4}-[0-9]{2}-[0-9]{2}$'),
    "schema" integer NOT NULL CHECK ("schema" > 0),
    pipeline text NOT NULL,
    cut_at timestamptz NOT NULL,
    reason text NOT NULL CHECK (reason IN ('quarterly', 'instrument', 'tranche', 'pipeline')),
    base text REFERENCES corpus_lockfiles(label),
    installed_at timestamptz,
    state text NOT NULL CHECK (state IN ('cut', 'installing', 'installed', 'superseded'))
);

CREATE TABLE source_snapshots (
    source text NOT NULL,
    snapshot_date date NOT NULL,
    base_url text NOT NULL,
    mirror_url text,
    sidecar_sha256 text NOT NULL CHECK (sidecar_sha256 ~ '^[0-9a-f]{64}$'),
    fetched_at timestamptz NOT NULL,
    verified_at timestamptz NOT NULL,
    PRIMARY KEY (source, snapshot_date)
);

CREATE TABLE lockfile_sources (
    label text NOT NULL REFERENCES corpus_lockfiles(label),
    source text NOT NULL,
    snapshot_date date NOT NULL,
    PRIMARY KEY (label, source),
    FOREIGN KEY (source, snapshot_date) REFERENCES source_snapshots(source, snapshot_date)
);

GRANT SELECT, INSERT, UPDATE ON TABLE corpus_lockfiles TO gideon_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE source_snapshots TO gideon_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE lockfile_sources TO gideon_worker;

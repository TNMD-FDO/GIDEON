-- A staged source's per-court counts are recorded once for each label.
-- This migration is forward-only; corrections are appended later.

CREATE TABLE stage_counts (
    label text NOT NULL REFERENCES corpus_lockfiles(label),
    source text NOT NULL,
    court text NOT NULL CHECK (court ~ '^[a-z0-9]{1,32}$'),
    dockets bigint NOT NULL CHECK (dockets >= 0),
    opinion_clusters bigint NOT NULL CHECK (opinion_clusters >= 0),
    citations bigint NOT NULL CHECK (citations >= 0),
    opinions bigint NOT NULL CHECK (opinions >= 0),
    staged_at timestamptz NOT NULL,
    PRIMARY KEY (label, source, court),
    FOREIGN KEY (label, source) REFERENCES lockfile_sources(label, source)
);

GRANT SELECT, INSERT ON TABLE stage_counts TO gideon_worker;
GRANT SELECT ON TABLE stage_counts TO gideon_ro_metrics;

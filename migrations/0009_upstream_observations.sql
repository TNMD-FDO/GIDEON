-- The upstream watch keeps one observation per source per run.
-- This migration is forward-only; corrections are appended later.

CREATE TABLE upstream_observations (
    source text NOT NULL CHECK (source ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'),
    observed_at timestamptz NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('observed', 'unanswered')),
    latest_label date,
    effective_date date,
    url text NOT NULL CHECK (url ~ '^https://[^[:space:]]+$'),
    detail text,
    PRIMARY KEY (source, observed_at),
    CHECK ((outcome = 'observed') = (latest_label IS NOT NULL)),
    CHECK ((outcome = 'unanswered') = (detail IS NOT NULL))
);

GRANT SELECT, INSERT ON TABLE upstream_observations TO gideon_worker;
GRANT SELECT ON TABLE upstream_observations TO gideon_ro_metrics;
GRANT SELECT ON TABLE corpus_lockfiles TO gideon_ro_metrics;
GRANT SELECT ON TABLE source_snapshots TO gideon_ro_metrics;
GRANT SELECT ON TABLE lockfile_sources TO gideon_ro_metrics;

-- Near-duplicate clusters and their memberships are derived per generation.

CREATE TABLE dupe_clusters (
    gen_id text NOT NULL REFERENCES index_generations(gen_id) ON DELETE CASCADE,
    cluster_id text NOT NULL CHECK (cluster_id ~ '^[0-9a-f]{64}$'),
    chunk_count bigint NOT NULL CHECK (chunk_count > 0),
    opinion_count bigint NOT NULL CHECK (opinion_count > 0 AND opinion_count <= chunk_count),
    boilerplate boolean NOT NULL,
    PRIMARY KEY (gen_id, cluster_id)
);

CREATE TABLE dupe_members (
    gen_id text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    cluster_id text NOT NULL CHECK (cluster_id ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (gen_id, content_hash),
    FOREIGN KEY (gen_id, cluster_id) REFERENCES dupe_clusters(gen_id, cluster_id) ON DELETE CASCADE,
    CHECK (cluster_id <> content_hash)
);

ALTER TABLE index_generations ADD COLUMN clustered_at timestamptz;

GRANT SELECT, INSERT, DELETE ON TABLE dupe_clusters TO gideon_worker;
GRANT SELECT, INSERT, DELETE ON TABLE dupe_members TO gideon_worker;
GRANT SELECT ON TABLE dupe_clusters TO gideon_ro_metrics;
GRANT SELECT ON TABLE dupe_members TO gideon_ro_metrics;

ALTER TABLE index_generations DROP CONSTRAINT index_generations_failure_reason_check;
ALTER TABLE index_generations ADD CONSTRAINT index_generations_failure_reason_check
    CHECK (failure_reason IN (
        'invalid', 'store', 'database', 'local', 'busy', 'mapping-mismatch',
        'text-mismatch', 'missing-text', 'generation-mismatch', 'embed',
        'config-mismatch', 'chunk-too-long', 'cluster-budget'
    ));

ALTER TABLE build_reports DROP CONSTRAINT build_reports_failed_stage_check;
ALTER TABLE build_reports ADD CONSTRAINT build_reports_failed_stage_check
    CHECK (failed_stage IN ('cluster', 'build', 'counts', 'dense', 'built'));

ALTER TABLE build_reports DROP CONSTRAINT build_reports_failure_reason_check;
ALTER TABLE build_reports ADD CONSTRAINT build_reports_failure_reason_check
    CHECK (failure_reason IN (
        'invalid', 'store', 'database', 'local', 'busy', 'mapping-mismatch',
        'text-mismatch', 'missing-text', 'generation-mismatch', 'embed',
        'config-mismatch', 'chunk-too-long', 'cluster-budget'
    ));

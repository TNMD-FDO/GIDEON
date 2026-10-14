-- Risk states are derived whole from the record per generation and rebuilt by
-- every build of that generation, outside every backup set. The worker's
-- DELETE grant is scoped to this derived table so its rows can be replaced whole.

CREATE TABLE cluster_treatment (
    gen_id text NOT NULL REFERENCES index_generations(gen_id),
    cluster_id bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('negative', 'caution')),
    signals jsonb NOT NULL CHECK (
        jsonb_typeof(signals) = 'array' AND jsonb_array_length(signals) > 0
    ),
    materialized_at timestamptz NOT NULL,
    PRIMARY KEY (gen_id, cluster_id)
);

GRANT SELECT, INSERT, DELETE ON TABLE cluster_treatment TO gideon_worker;
GRANT SELECT ON TABLE cluster_treatment TO gideon_ro_metrics;

ALTER TABLE build_reports ADD COLUMN treatment jsonb
    CHECK (treatment IS NULL OR jsonb_typeof(treatment) = 'object');

ALTER TABLE build_reports DROP CONSTRAINT build_reports_failed_stage_check;
ALTER TABLE build_reports ADD CONSTRAINT build_reports_failed_stage_check
    CHECK (failed_stage IN ('cluster', 'build', 'counts', 'dense', 'built', 'treatment'));

-- Build reports append one observation of a generation per command run.

CREATE TABLE build_reports (
    gen_id text NOT NULL REFERENCES index_generations(gen_id),
    written_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL CHECK (state IN ('building', 'built', 'live', 'retired', 'failed')),
    failed_stage text CHECK (failed_stage IN ('build', 'counts', 'dense', 'built')),
    failure_reason text CHECK (failure_reason IN (
        'invalid', 'store', 'database', 'local', 'busy', 'mapping-mismatch',
        'text-mismatch', 'missing-text', 'generation-mismatch', 'embed',
        'config-mismatch', 'chunk-too-long'
    )),
    seconds bigint NOT NULL CHECK (seconds >= 0),
    courts jsonb NOT NULL CHECK (jsonb_typeof(courts) = 'object'),
    metadata_changes jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata_changes) = 'object'),
    gates jsonb NOT NULL CHECK (jsonb_typeof(gates) = 'object'),
    wall_clock jsonb NOT NULL CHECK (jsonb_typeof(wall_clock) = 'object'),
    PRIMARY KEY (gen_id, written_at),
    CHECK ((state = 'failed') = (failure_reason IS NOT NULL))
);

GRANT SELECT, INSERT ON TABLE build_reports TO gideon_worker;
GRANT SELECT ON TABLE build_reports TO gideon_ro_metrics;

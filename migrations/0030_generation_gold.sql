-- The gold a sample build was given, recorded once per id.
-- Forward-only: the given list only grows.

CREATE TABLE generation_gold (
    gen_id text NOT NULL REFERENCES index_generations(gen_id),
    source_id text NOT NULL CHECK (
        source_id ~ '^[A-Za-z0-9._:/-]+$'
        AND char_length(source_id) BETWEEN 1 AND 200
    ),
    given_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (gen_id, source_id)
);

GRANT SELECT, INSERT ON TABLE generation_gold TO gideon_worker;
GRANT SELECT ON TABLE generation_gold TO gideon_ro_metrics;

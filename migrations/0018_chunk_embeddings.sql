-- The embedding ledger records a vector's home for each text and space.

CREATE TABLE chunk_embeddings (
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    embedding_space text NOT NULL CHECK (embedding_space ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'),
    gen_id text NOT NULL REFERENCES index_generations(gen_id),
    point_id uuid NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (content_hash, embedding_space)
);

GRANT SELECT, INSERT, UPDATE ON TABLE chunk_embeddings TO gideon_worker;
GRANT SELECT ON TABLE chunk_embeddings TO gideon_ro_metrics;

ALTER TABLE index_generations DROP CONSTRAINT index_generations_failure_reason_check;
ALTER TABLE index_generations ADD CONSTRAINT index_generations_failure_reason_check
    CHECK (failure_reason IN (
        'invalid', 'store', 'database', 'local', 'busy', 'mapping-mismatch',
        'text-mismatch', 'missing-text', 'generation-mismatch', 'embed',
        'config-mismatch', 'chunk-too-long'
    ));

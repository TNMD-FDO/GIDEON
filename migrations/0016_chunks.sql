-- Chunks are immutable spans of canonical text under one registered pipeline.
-- A ready document is chunked under a pipeline when at least one row exists.

CREATE TABLE chunks (
    chunk_id text PRIMARY KEY CHECK (chunk_id ~ '^[0-9a-f]{64}$'),
    doc_id text NOT NULL REFERENCES documents(doc_id),
    pipeline text NOT NULL CHECK (pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    section_id text NOT NULL REFERENCES sections(section_id),
    section_type text NOT NULL CHECK (section_type IN (
        'syllabus', 'headmatter', 'majority', 'plurality', 'per_curiam',
        'concurrence', 'dissent', 'concurrence_dissent', 'footnote',
        'appendix', 'order', 'unknown'
    )),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    token_count integer NOT NULL CHECK (token_count > 0),
    UNIQUE (doc_id, pipeline, ordinal),
    UNIQUE (doc_id, pipeline, char_start)
);

GRANT SELECT, INSERT ON TABLE chunks TO gideon_worker;
GRANT SELECT ON TABLE chunks TO gideon_ro_metrics;

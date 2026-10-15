-- Generation manifests identify documents whose chunks are in both stores.

CREATE TABLE generation_documents (
    gen_id text NOT NULL REFERENCES index_generations(gen_id),
    source_id text NOT NULL CHECK (
        source_id ~ '^[A-Za-z0-9._:/-]+$'
        AND char_length(source_id) BETWEEN 1 AND 200
    ),
    canonical_text_sha256 text NOT NULL CHECK (canonical_text_sha256 ~ '^[0-9a-f]{64}$'),
    doc_id text NOT NULL CHECK (doc_id ~ '^[0-9a-f]{64}$'),
    indexed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (gen_id, source_id)
);

GRANT SELECT, INSERT ON TABLE generation_documents TO gideon_worker;
GRANT SELECT ON TABLE generation_documents TO gideon_ro_metrics;

ALTER TABLE index_generations DROP CONSTRAINT index_generations_gen_id_check;
ALTER TABLE index_generations ADD CONSTRAINT index_generations_gen_id_check
    CHECK (
        gen_id ~ '^(caselaw)__corpus-[0-9]{4}-[0-9]{2}-[0-9]{2}__[a-z0-9]+(?:-[a-z0-9]+)*(?:__sample)?$'
        AND octet_length(gen_id) < 255
    );

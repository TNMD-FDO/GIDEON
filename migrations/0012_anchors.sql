-- Anchors are immutable labelled spans of a document's canonical text.

CREATE TABLE anchors (
    anchor_id text PRIMARY KEY CHECK (anchor_id ~ '^[0-9a-f]{64}$'),
    doc_id text NOT NULL REFERENCES documents(doc_id),
    kind text NOT NULL CHECK (kind IN (
        'reporter_page', 'pdf_page', 'bates', 'tr_page', 'tr_line',
        'uslm_id', 'guideline_id'
    )),
    label text NOT NULL,
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    attrs jsonb NOT NULL CHECK (jsonb_typeof(attrs) = 'object'),
    CHECK (kind <> 'reporter_page' OR
           COALESCE(jsonb_typeof(attrs -> 'scheme') = 'string', false))
);

CREATE UNIQUE INDEX anchors_doc_kind_start_scheme_idx
    ON anchors (doc_id, kind, char_start, (attrs ->> 'scheme'));

ALTER TABLE documents ADD COLUMN anchored_at timestamptz;
ALTER TABLE documents ADD CONSTRAINT documents_anchored_ready
    CHECK (anchored_at IS NULL OR status = 'ready');

GRANT SELECT, INSERT ON TABLE anchors TO gideon_worker;
GRANT SELECT ON TABLE anchors TO gideon_ro_metrics;

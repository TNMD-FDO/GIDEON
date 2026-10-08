-- Ingested opinions keep their document state and source metadata in the record.
-- This migration is forward-only; corrections are appended later.

CREATE TABLE documents (
    doc_id text PRIMARY KEY CHECK (doc_id ~ '^[0-9a-f]{64}$'),
    profile text NOT NULL CHECK (profile IN ('caselaw', 'authority', 'matter')),
    source text NOT NULL,
    source_snapshot date NOT NULL,
    sha256 text CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    canonical_text_sha256 text CHECK (canonical_text_sha256 ~ '^[0-9a-f]{64}$'),
    text_source text CHECK (text_source IN (
        'xml_harvard', 'html_columbia', 'html_lawbox', 'html_anon_2020',
        'html', 'plain_text'
    )),
    status text NOT NULL CHECK (status IN ('processing', 'ready', 'failed', 'withdrawn')),
    failure_reason text CHECK (failure_reason IN (
        'no-text', 'unparseable', 'empty', 'interrupted'
    )),
    attempts smallint NOT NULL CHECK (attempts BETWEEN 0 AND 2),
    begun_at timestamptz NOT NULL,
    ingested_at timestamptz,
    superseded_by text REFERENCES documents(doc_id),
    FOREIGN KEY (source, source_snapshot) REFERENCES source_snapshots(source, snapshot_date),
    CHECK (status <> 'ready' OR (sha256 IS NOT NULL AND canonical_text_sha256 IS NOT NULL)),
    CHECK (status = 'ready' OR canonical_text_sha256 IS NULL),
    CHECK ((status = 'failed') = (failure_reason IS NOT NULL)),
    CHECK (status <> 'processing' OR sha256 IS NULL),
    CHECK ((status = 'processing') = (ingested_at IS NULL))
);

CREATE INDEX documents_source_snapshot_status_idx
    ON documents (source, source_snapshot, status);

CREATE TABLE opinions (
    opinion_id bigint PRIMARY KEY,
    doc_id text NOT NULL UNIQUE REFERENCES documents(doc_id),
    court text NOT NULL CHECK (court ~ '^[a-z0-9]{1,32}$'),
    cluster_id bigint NOT NULL,
    docket text,
    decided_date date NOT NULL,
    decided_date_is_approximate boolean NOT NULL,
    precedential text NOT NULL CHECK (precedential IN (
        'published', 'unpublished', 'unknown'
    )),
    precedential_raw text NOT NULL,
    reporter_cites text[] NOT NULL,
    opinion_type text NOT NULL
);

CREATE INDEX opinions_court_idx ON opinions (court);
CREATE INDEX opinions_cluster_id_idx ON opinions (cluster_id);

GRANT SELECT, INSERT, UPDATE ON TABLE documents TO gideon_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE opinions TO gideon_worker;
GRANT SELECT ON TABLE documents TO gideon_ro_metrics;
GRANT SELECT ON TABLE opinions TO gideon_ro_metrics;

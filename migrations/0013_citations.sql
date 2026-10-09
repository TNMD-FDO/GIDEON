-- Citation edges are immutable spans of a document's canonical text.
-- The mark records a completed pass even when the document has no citations.

ALTER TABLE documents ADD COLUMN citations_parsed_at timestamptz;
ALTER TABLE documents ADD CONSTRAINT documents_citations_ready_check
    CHECK (citations_parsed_at IS NULL OR status = 'ready');

CREATE TABLE citations (
    citation_id text PRIMARY KEY CHECK (citation_id ~ '^[0-9a-f]{64}$'),
    doc_id text NOT NULL REFERENCES documents(doc_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    section_id text NOT NULL REFERENCES sections(section_id),
    section_type text NOT NULL CHECK (section_type IN (
        'syllabus', 'headmatter', 'majority', 'plurality', 'per_curiam',
        'concurrence', 'dissent', 'concurrence_dissent', 'footnote',
        'appendix', 'order', 'unknown'
    )),
    cite_type text NOT NULL CHECK (cite_type IN (
        'case_cite', 'law_cite', 'journal_cite', 'unknown', 'statute',
        'guideline', 'court_rule', 'regulation', 'appendix_statute',
        'habeas_rule', 'scotus_rule', 'bare_section', 'bare_rule', 'state_code'
    )),
    cite_form text NOT NULL CHECK (cite_form IN (
        'full', 'short', 'id', 'supra', 'reference'
    )),
    raw_cite text NOT NULL,
    reporter_cite text,
    pincite text,
    key text,
    to_cluster bigint,
    to_authority text,
    pattern_id text NOT NULL,
    UNIQUE (doc_id, ordinal),
    UNIQUE (doc_id, char_start),
    CHECK (to_cluster IS NULL OR cite_type = 'case_cite'),
    CHECK (cite_type <> 'unknown' OR
           (cite_form IN ('id', 'supra', 'reference') AND
            to_cluster IS NULL AND to_authority IS NULL)),
    CHECK (cite_type NOT IN (
        'statute', 'guideline', 'court_rule', 'regulation', 'appendix_statute',
        'habeas_rule', 'scotus_rule', 'bare_section', 'bare_rule', 'state_code'
    ) OR cite_form IN ('full', 'id')),
    CHECK (key IS NULL OR cite_type IN (
        'statute', 'guideline', 'court_rule', 'regulation', 'appendix_statute',
        'habeas_rule', 'scotus_rule'
    ))
);

CREATE INDEX citations_to_cluster_idx ON citations (to_cluster)
    WHERE to_cluster IS NOT NULL;

GRANT SELECT, INSERT ON TABLE citations TO gideon_worker;
GRANT SELECT ON TABLE citations TO gideon_ro_metrics;

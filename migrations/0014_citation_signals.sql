-- Treatment findings are immutable observations of citation edges.
-- The document mark records a completed pass even when no edge is signalled.

ALTER TABLE documents
    ADD COLUMN treatment_pattern_set text,
    ADD COLUMN treated_at timestamptz,
    ADD CONSTRAINT documents_treatment_mark_check CHECK (
        (treatment_pattern_set IS NULL AND treated_at IS NULL) OR
        (treatment_pattern_set IS NOT NULL AND treated_at IS NOT NULL AND
         citations_parsed_at IS NOT NULL)
    );

CREATE TABLE citation_signals (
    signal_id text PRIMARY KEY CHECK (signal_id ~ '^[0-9a-f]{64}$'),
    citation_id text NOT NULL REFERENCES citations(citation_id),
    doc_id text NOT NULL REFERENCES documents(doc_id),
    signal_source text NOT NULL CHECK (signal_source IN ('pattern', 'list', 'llm')),
    pattern_set text NOT NULL,
    treatment_signal text NOT NULL CHECK (treatment_signal IN (
        'overruled', 'abrogated', 'superseded', 'reversed', 'vacated', 'disapproved'
    )),
    qualifier text NOT NULL CHECK (qualifier IN (
        'in_part', 'on_other_grounds', 'none'
    )),
    effective_section text NOT NULL CHECK (effective_section IN (
        'syllabus', 'headmatter', 'majority', 'plurality', 'per_curiam',
        'concurrence', 'dissent', 'concurrence_dissent', 'footnote',
        'appendix', 'order', 'unknown'
    )),
    state text CHECK (state IN ('negative', 'caution')),
    no_state_reason text CHECK (no_state_reason IN (
        'non_holding', 'lineage_unverified', 'unresolved'
    )),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    found_at timestamptz NOT NULL,
    UNIQUE (citation_id, signal_source, pattern_set),
    CHECK ((state IS NULL) <> (no_state_reason IS NULL))
);

CREATE INDEX citation_signals_doc_id_idx ON citation_signals (doc_id);
CREATE INDEX citation_signals_state_idx ON citation_signals (state)
    WHERE state IS NOT NULL;

GRANT SELECT, INSERT ON TABLE citation_signals TO gideon_worker;
GRANT SELECT ON TABLE citation_signals TO gideon_ro_metrics;

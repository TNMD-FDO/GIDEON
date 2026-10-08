-- Sections are immutable spans of a document's canonical text.

CREATE TABLE sections (
    section_id text PRIMARY KEY CHECK (section_id ~ '^[0-9a-f]{64}$'),
    doc_id text NOT NULL REFERENCES documents(doc_id),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    section_type text NOT NULL CHECK (section_type IN (
        'syllabus', 'headmatter', 'majority', 'plurality', 'per_curiam',
        'concurrence', 'dissent', 'concurrence_dissent', 'footnote',
        'appendix', 'order', 'unknown'
    )),
    typed_by text NOT NULL CHECK (typed_by IN (
        'row', 'flag', 'element', 'line', 'markup', 'none'
    )),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end > char_start),
    label text,
    ref_offset integer,
    parent_section_id text REFERENCES sections(section_id),
    UNIQUE (doc_id, ordinal),
    UNIQUE (doc_id, char_start),
    CHECK (section_type = 'footnote' OR
           (label IS NULL AND ref_offset IS NULL AND parent_section_id IS NULL)),
    CHECK (typed_by <> 'markup' OR section_type IN ('headmatter', 'footnote')),
    CHECK (typed_by <> 'none' OR section_type = 'unknown')
);

GRANT SELECT, INSERT ON TABLE sections TO gideon_worker;
GRANT SELECT ON TABLE sections TO gideon_ro_metrics;

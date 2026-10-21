-- Walk rows are immutable spans under one registered pipeline.
-- Existing rows belong to 1.0.0; new writes must supply their pipeline.
-- Composite foreign keys hold dependent rows to their parent's pipeline.
-- The treatment mark names its pipeline; per_curiam is null until carried.
-- Existing table grants cover the new columns; walk rows remain append-only.
-- This migration is forward-only; corrections are appended later.

ALTER TABLE sections
    ADD COLUMN pipeline text NOT NULL DEFAULT '1.0.0'
        CHECK (pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$');
ALTER TABLE sections ALTER COLUMN pipeline DROP DEFAULT;
ALTER TABLE sections
    DROP CONSTRAINT sections_doc_id_ordinal_key,
    DROP CONSTRAINT sections_doc_id_char_start_key,
    ADD UNIQUE (doc_id, pipeline, ordinal),
    ADD UNIQUE (doc_id, pipeline, char_start),
    ADD UNIQUE (section_id, pipeline);
ALTER TABLE sections
    ADD FOREIGN KEY (parent_section_id, pipeline)
        REFERENCES sections (section_id, pipeline);

ALTER TABLE anchors
    ADD COLUMN pipeline text NOT NULL DEFAULT '1.0.0'
        CHECK (pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$');
ALTER TABLE anchors ALTER COLUMN pipeline DROP DEFAULT;
DROP INDEX anchors_doc_kind_start_scheme_idx;
CREATE UNIQUE INDEX anchors_doc_kind_start_scheme_idx
    ON anchors (doc_id, pipeline, kind, char_start, (attrs ->> 'scheme'));

ALTER TABLE citations
    ADD COLUMN pipeline text NOT NULL DEFAULT '1.0.0'
        CHECK (pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$');
ALTER TABLE citations ALTER COLUMN pipeline DROP DEFAULT;
ALTER TABLE citations
    DROP CONSTRAINT citations_doc_id_ordinal_key,
    DROP CONSTRAINT citations_doc_id_char_start_key,
    ADD UNIQUE (doc_id, pipeline, ordinal),
    ADD UNIQUE (doc_id, pipeline, char_start),
    ADD UNIQUE (citation_id, pipeline),
    ADD FOREIGN KEY (section_id, pipeline)
        REFERENCES sections (section_id, pipeline);

ALTER TABLE citation_signals
    ADD COLUMN pipeline text NOT NULL DEFAULT '1.0.0'
        CHECK (pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$');
ALTER TABLE citation_signals ALTER COLUMN pipeline DROP DEFAULT;
ALTER TABLE citation_signals
    ADD FOREIGN KEY (citation_id, pipeline)
        REFERENCES citations (citation_id, pipeline);

ALTER TABLE chunks
    ADD FOREIGN KEY (section_id, pipeline)
        REFERENCES sections (section_id, pipeline);

ALTER TABLE opinions ADD COLUMN per_curiam boolean;

ALTER TABLE documents
    ADD COLUMN treated_pipeline text
        CHECK (treated_pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$');
UPDATE documents SET treated_pipeline = '1.0.0'
    WHERE treatment_pattern_set IS NOT NULL;
ALTER TABLE documents
    DROP CONSTRAINT documents_treatment_mark_check,
    ADD CONSTRAINT documents_treatment_mark_check CHECK (
        (treatment_pattern_set IS NULL AND treated_at IS NULL AND
         treated_pipeline IS NULL) OR
        (treatment_pattern_set IS NOT NULL AND treated_at IS NOT NULL AND
         treated_pipeline IS NOT NULL AND citations_parsed_at IS NOT NULL)
    );

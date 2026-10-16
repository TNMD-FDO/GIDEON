-- The case name extends the opinion's metadata in the record.
-- Null means not yet carried; the empty string means the cluster named nothing.
-- Names are held to their whitespace-collapsed form.
-- Existing table grants cover SELECT, INSERT, UPDATE by gideon_worker and SELECT by gideon_ro_metrics.
-- This migration is forward-only; corrections are appended later.

ALTER TABLE opinions
    ADD COLUMN case_name text
        CHECK (case_name IS NULL OR case_name = btrim(regexp_replace(case_name, '[[:space:]]+', ' ', 'g')));

-- A document may fail because its source text reads as no language.
-- This migration is forward-only; it widens the check and changes no row.

ALTER TABLE documents DROP CONSTRAINT documents_failure_reason_check;
ALTER TABLE documents ADD CONSTRAINT documents_failure_reason_check
    CHECK (failure_reason IN ('no-text', 'unparseable', 'empty', 'illegible', 'interrupted'));

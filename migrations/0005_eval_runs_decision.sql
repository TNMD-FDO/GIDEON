-- Decision fields extend the append-only evaluation run record.
-- Defaults preserve earlier rows; decision is null when no comparison was taken.
-- This migration is forward-only; corrections are appended later.

ALTER TABLE eval_runs
    ADD COLUMN forced boolean NOT NULL DEFAULT false,
    ADD COLUMN partial boolean NOT NULL DEFAULT false,
    ADD COLUMN decision jsonb;

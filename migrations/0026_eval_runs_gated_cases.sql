-- The count of cases read by the zero-tolerance gate extends the append-only run record.
-- The column is nullable without a default; null means no zero-tolerance gate
-- or a row written before this column existed, never zero.
-- Existing table grants cover INSERT by gideon_eval and SELECT by gideon_ro_metrics.
-- This migration is forward-only; corrections are appended later.

ALTER TABLE eval_runs
    ADD COLUMN gated_cases integer
        CHECK (gated_cases IS NULL OR gated_cases >= 0);

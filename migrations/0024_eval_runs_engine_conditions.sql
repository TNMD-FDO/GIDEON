-- The engine's in-flight maximum and preemption count extend the append-only run record.
-- Both columns are nullable without defaults; null means unknown, never zero.
-- Existing table grants cover INSERT by gideon_eval and SELECT by gideon_ro_metrics.
-- This migration is forward-only; corrections are appended later.

ALTER TABLE eval_runs
    ADD COLUMN engine_in_flight_max integer
        CHECK (engine_in_flight_max IS NULL OR engine_in_flight_max >= 0),
    ADD COLUMN engine_preemptions integer
        CHECK (engine_preemptions IS NULL OR engine_preemptions >= 0);

-- §19.4: kept event rows carry identifiers only, never user or matter text.
-- ADR-0027: kept events are rows, not log lines or audit-file entries.
-- ADR-0005: this migration is forward-only; corrections are appended later.
-- §18.6, §7.9: an eval run's rows, written insert-only by gideon_eval as
-- the run's closing record and read by gideon_ro_metrics.

CREATE TABLE eval_runs (
    run_id uuid NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    product_version text NOT NULL,
    corpus_lockfile text,
    eval_set_version text NOT NULL,
    hardware_profile text NOT NULL,
    stack text NOT NULL CHECK (stack IN ('production', 'ci')),
    generation_id text,
    kind text NOT NULL CHECK (kind IN (
        'smoke', 'nightly', 'weekly-off', 'decision',
        'candidate', 'engine-verify', 'manual'
    )),
    slice text,
    overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
    repeats integer NOT NULL CHECK (repeats >= 1),
    git_sha text,
    git_dirty boolean,
    set_digest text NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('pass', 'fail')),
    PRIMARY KEY (run_id, started_at)
) PARTITION BY RANGE (started_at);

CREATE TABLE eval_results (
    run_id uuid NOT NULL,
    run_started_at timestamptz NOT NULL,
    case_id text NOT NULL,
    repeat integer NOT NULL CHECK (repeat >= 1),
    verdict text NOT NULL CHECK (verdict IN ('pass', 'fail')),
    metrics jsonb NOT NULL,
    judge jsonb,
    provenance_ref text,
    latency_ms double precision,
    PRIMARY KEY (run_id, case_id, repeat, run_started_at)
) PARTITION BY RANGE (run_started_at);

CREATE OR REPLACE FUNCTION eval_runs_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('eval_runs_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    -- Schema-qualified on both sides: the hardened search_path above puts
    -- pg_catalog first, and an unqualified CREATE TABLE would target it.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.eval_runs '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

CREATE OR REPLACE FUNCTION eval_results_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('eval_results_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    -- Schema-qualified on both sides: the hardened search_path above puts
    -- pg_catalog first, and an unqualified CREATE TABLE would target it.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.eval_results '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

ALTER FUNCTION eval_runs_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION eval_runs_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION eval_runs_ensure_partition(timestamptz) TO gideon_eval;

ALTER FUNCTION eval_results_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION eval_results_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION eval_results_ensure_partition(timestamptz) TO gideon_eval;

GRANT USAGE ON SCHEMA public TO gideon_eval;
GRANT INSERT ON TABLE eval_runs TO gideon_eval;
GRANT INSERT ON TABLE eval_results TO gideon_eval;
GRANT SELECT ON TABLE eval_runs TO gideon_ro_metrics;
GRANT SELECT ON TABLE eval_results TO gideon_ro_metrics;

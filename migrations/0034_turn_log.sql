-- Research turn events carry identifiers, timings, ranks, and counters,
-- never question, passage, or answer text.
-- Events are stored as rows, not log lines or audit-file entries.
-- This migration is forward-only; corrections are appended later.
-- IF NOT EXISTS lets a renumbered file run again after an earlier apply.

CREATE TABLE IF NOT EXISTS turn_log (
    id bigint GENERATED ALWAYS AS IDENTITY,
    at timestamptz NOT NULL DEFAULT now(),
    chat_id text,
    message_id text,
    source text NOT NULL CHECK (source IN ('user', 'eval')),
    stop_after text,
    outcome text NOT NULL,
    failure_state text,
    failure_word text,
    trip_family text,
    product_version text NOT NULL,
    eval_set_version text NOT NULL,
    corpus_lockfiles jsonb,
    generation_id text,
    embedding_space text,
    generator_repository text,
    retrieval_config_hash text NOT NULL,
    overrides jsonb,
    plan_mode text NOT NULL,
    timings_ms jsonb NOT NULL,
    provenance jsonb,
    out_of_corpus_objects jsonb,
    pool jsonb,
    evidence jsonb,
    engine_counters jsonb,
    support_level text CHECK (support_level IN ('none', 'thin', 'ok')),
    gate_report jsonb,
    misses jsonb,
    PRIMARY KEY (id, at)
) PARTITION BY RANGE (at);

CREATE OR REPLACE FUNCTION turn_log_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('turn_log_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    -- Schema-qualified on both sides: the hardened search_path above puts
    -- pg_catalog first, and an unqualified CREATE TABLE would target it.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.turn_log '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

ALTER FUNCTION turn_log_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION turn_log_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION turn_log_ensure_partition(timestamptz) TO gideon_audit;
GRANT INSERT ON TABLE turn_log TO gideon_audit;
GRANT SELECT ON TABLE turn_log TO gideon_ro_metrics;

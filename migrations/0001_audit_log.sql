-- Audit rows carry identifiers only, never user or matter text.
-- This migration is forward-only; corrections are appended later.

CREATE TABLE audit_log (
    id bigint GENERATED ALWAYS AS IDENTITY,
    at timestamptz NOT NULL DEFAULT now(),
    run_id uuid NOT NULL,
    kind text NOT NULL,
    actor_user_id text,
    user_id text,
    chat_id text,
    kb_ids text[] NOT NULL DEFAULT '{}',
    release text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, at)
) PARTITION BY RANGE (at);

CREATE OR REPLACE FUNCTION audit_log_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('audit_log_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    -- Schema-qualified on both sides: the hardened search_path above puts
    -- pg_catalog first, and an unqualified CREATE TABLE would target it.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.audit_log '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

ALTER FUNCTION audit_log_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION audit_log_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit_log_ensure_partition(timestamptz) TO gideon_audit;
GRANT INSERT ON TABLE audit_log TO gideon_audit;
GRANT USAGE ON SCHEMA public TO gideon_audit;

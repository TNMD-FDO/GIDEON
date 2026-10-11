-- Search turn events carry identifiers only, never user or matter text.
-- Events are stored as rows, not log lines or audit-file entries.
-- This migration is forward-only; corrections are appended later.
-- IF NOT EXISTS lets a renumbered file run again after an earlier apply.

CREATE TABLE IF NOT EXISTS search_turns (
    id bigint GENERATED ALWAYS AS IDENTITY,
    at timestamptz NOT NULL DEFAULT now(),
    user_id text,
    chat_id text,
    source text NOT NULL CHECK (source IN ('user', 'eval')),
    PRIMARY KEY (id, at)
) PARTITION BY RANGE (at);

CREATE OR REPLACE FUNCTION search_turns_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('search_turns_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    -- Schema-qualified on both sides: the hardened search_path above puts
    -- pg_catalog first, and an unqualified CREATE TABLE would target it.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.search_turns '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

ALTER FUNCTION search_turns_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION search_turns_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION search_turns_ensure_partition(timestamptz) TO gideon_audit;
GRANT INSERT ON TABLE search_turns TO gideon_audit;
GRANT SELECT ON TABLE search_turns TO gideon_ro_metrics;

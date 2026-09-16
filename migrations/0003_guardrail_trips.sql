-- §19.4: kept event rows carry identifiers only, never user or matter text.
-- ADR-0027: kept events are rows, not log lines or audit-file entries.
-- ADR-0005: this migration is forward-only; corrections are appended later.

CREATE TABLE guardrail_trips (
    id bigint GENERATED ALWAYS AS IDENTITY,
    at timestamptz NOT NULL DEFAULT now(),
    branch text NOT NULL,
    family text NOT NULL,
    pattern_id text NOT NULL,
    source text NOT NULL CHECK (source IN ('user', 'eval')),
    PRIMARY KEY (id, at)
) PARTITION BY RANGE (at);

CREATE OR REPLACE FUNCTION guardrail_trips_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('guardrail_trips_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    -- Schema-qualified on both sides: the hardened search_path above puts
    -- pg_catalog first, and an unqualified CREATE TABLE would target it.
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.guardrail_trips '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

ALTER FUNCTION guardrail_trips_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION guardrail_trips_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION guardrail_trips_ensure_partition(timestamptz) TO gideon_audit;
GRANT INSERT ON TABLE guardrail_trips TO gideon_audit;
GRANT SELECT ON TABLE guardrail_trips TO gideon_ro_metrics;

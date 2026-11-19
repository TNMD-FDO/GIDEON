-- Maintenance window events carry instants and an account name, never text a person typed.
-- This migration is forward-only; corrections are appended later.
-- IF NOT EXISTS lets a renumbered file run again after an earlier apply.

CREATE TABLE IF NOT EXISTS maintenance_window_events (
    id bigint GENERATED ALWAYS AS IDENTITY,
    key uuid NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    act text NOT NULL CHECK (act IN ('set', 'withdraw')),
    starts_at timestamptz,
    ends_at timestamptz,
    actor text NOT NULL,
    release text NOT NULL,
    PRIMARY KEY (id, at),
    CHECK (
        (act = 'set' AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND ends_at > starts_at)
        OR (act = 'withdraw' AND starts_at IS NULL AND ends_at IS NULL)
    )
) PARTITION BY RANGE (at);

CREATE OR REPLACE FUNCTION maintenance_window_events_ensure_partition(moment timestamptz)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET TimeZone = 'UTC'
AS $function$
DECLARE
    month_start timestamptz := date_trunc('month', moment);
    month_end timestamptz := month_start + interval '1 month';
    partition_name text := format('maintenance_window_events_%s', to_char(month_start, 'YYYYMM'));
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.maintenance_window_events '
        'FOR VALUES FROM (%L) TO (%L)',
        partition_name,
        month_start,
        month_end
    );
END;
$function$;

ALTER FUNCTION maintenance_window_events_ensure_partition(timestamptz) OWNER TO gideon;
REVOKE ALL ON FUNCTION maintenance_window_events_ensure_partition(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION maintenance_window_events_ensure_partition(timestamptz) TO gideon_audit;
GRANT INSERT ON TABLE maintenance_window_events TO gideon_audit;
GRANT SELECT ON TABLE maintenance_window_events TO gideon_ro_metrics;

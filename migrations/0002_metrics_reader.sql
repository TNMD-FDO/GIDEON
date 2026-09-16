-- §19.5: Grafana reads operational rows through a dedicated read-only role.
-- ADR-0005: this migration is forward-only; corrections are appended later.

GRANT USAGE ON SCHEMA public TO gideon_ro_metrics;
GRANT SELECT ON TABLE audit_log TO gideon_ro_metrics;
-- pg_monitor membership (the exporter's statistics views) is granted by apply's
-- stores stage as the superuser: a migration runs as gideon, which may not
-- grant a predefined role.

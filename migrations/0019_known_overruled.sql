-- Listed overrulings are immutable observations bound to both source snapshots.

CREATE TABLE known_overruled (
    row_id text PRIMARY KEY CHECK (row_id ~ '^[0-9a-f]{64}$'),
    source text NOT NULL,
    source_snapshot date NOT NULL,
    caselaw_source text NOT NULL,
    caselaw_snapshot date NOT NULL,
    list_order integer NOT NULL CHECK (list_order > 0),
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    overruling_name text NOT NULL,
    overruling_cite text,
    overruling_docket text,
    overruling_year integer NOT NULL,
    overruling_cluster bigint,
    overruling_resolution text NOT NULL CHECK (overruling_resolution IN (
        'cite', 'docket', 'ambiguous', 'absent'
    )),
    overruled_name text NOT NULL,
    overruled_cite text,
    overruled_year integer,
    overruled_cluster bigint,
    overruled_resolution text NOT NULL CHECK (overruled_resolution IN (
        'cite', 'name', 'ambiguous', 'absent'
    )),
    qualifier text NOT NULL CHECK (qualifier IN (
        'in_part', 'on_other_grounds', 'none'
    )),
    found_at timestamptz NOT NULL,
    FOREIGN KEY (source, source_snapshot) REFERENCES source_snapshots(source, snapshot_date),
    FOREIGN KEY (caselaw_source, caselaw_snapshot) REFERENCES source_snapshots(source, snapshot_date),
    UNIQUE (source, source_snapshot, caselaw_source, caselaw_snapshot, list_order, ordinal),
    CHECK ((overruling_cluster IS NULL) = (overruling_resolution IN ('ambiguous', 'absent'))),
    CHECK ((overruled_cluster IS NULL) = (overruled_resolution IN ('ambiguous', 'absent')))
);

CREATE INDEX known_overruled_overruled_cluster_idx ON known_overruled (overruled_cluster)
    WHERE overruled_cluster IS NOT NULL;

GRANT SELECT, INSERT ON TABLE known_overruled TO gideon_worker;
GRANT SELECT ON TABLE known_overruled TO gideon_ro_metrics;

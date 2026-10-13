-- Index generations record their inputs and the completion of each half.

CREATE TABLE index_generations (
    gen_id text PRIMARY KEY CHECK (
        gen_id ~ '^(caselaw)__corpus-[0-9]{4}-[0-9]{2}-[0-9]{2}__[a-z0-9]+(?:-[a-z0-9]+)*$'
        AND octet_length(gen_id) < 255
    ),
    collection text NOT NULL CHECK (collection IN ('caselaw')),
    corpus_lockfile text NOT NULL REFERENCES corpus_lockfiles(label),
    lockfiles text[] NOT NULL CHECK (
        cardinality(lockfiles) > 0 AND lockfiles @> ARRAY[corpus_lockfile]
    ),
    pipeline text NOT NULL CHECK (pipeline ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    embedding_space text NOT NULL CHECK (embedding_space ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'),
    inputs_hash text NOT NULL CHECK (inputs_hash ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('building', 'built', 'live', 'retired', 'failed')),
    failure_reason text CHECK (failure_reason IN (
        'invalid', 'store', 'database', 'local', 'busy', 'mapping-mismatch',
        'text-mismatch', 'missing-text', 'generation-mismatch'
    )),
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    bytes bigint NOT NULL DEFAULT 0 CHECK (bytes >= 0),
    lexical_built_at timestamptz,
    dense_built_at timestamptz,
    built_at timestamptz,
    promoted_at timestamptz,
    retired_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (collection, inputs_hash),
    CHECK ((state = 'failed') = (failure_reason IS NOT NULL)),
    CHECK ((built_at IS NOT NULL) = (
        lexical_built_at IS NOT NULL AND dense_built_at IS NOT NULL
    )),
    CHECK (promoted_at IS NULL OR state IN ('live', 'retired')),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);

GRANT SELECT, INSERT, UPDATE ON TABLE index_generations TO gideon_worker;
GRANT SELECT ON TABLE index_generations TO gideon_ro_metrics;

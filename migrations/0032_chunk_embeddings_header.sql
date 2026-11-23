-- Give existing ledger rows the empty-header hash and retain it for bare inserts.

ALTER TABLE chunk_embeddings
    ADD COLUMN header_hash text NOT NULL
        DEFAULT 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
        CHECK (header_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE chunk_embeddings DROP CONSTRAINT chunk_embeddings_pkey;
ALTER TABLE chunk_embeddings ADD PRIMARY KEY (content_hash, header_hash, embedding_space);

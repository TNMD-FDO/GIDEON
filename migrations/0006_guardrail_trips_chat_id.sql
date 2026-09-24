-- A trip keeps the id of the chat it tripped in, never the chat's text; the
-- column is nullable, since a trip outside a chat has no id to keep.
-- Migrations are forward-only. IF NOT EXISTS lets this change run again
-- harmlessly when a release renumbers the file after an earlier apply ran it.
ALTER TABLE guardrail_trips ADD COLUMN IF NOT EXISTS chat_id text;

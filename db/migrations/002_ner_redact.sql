-- 002_ner_redact.sql
-- Adds projection columns for scribe-clinical-ner and scribe-redact (v0 NOP slice).
-- See docs/scribe-ner-redact.md §1.7 and §2.8. Idempotent — safe to re-run.
-- Apply with: ./scripts/migrate.sh

BEGIN;

-- §1.7: NER projection. NULL = NER hasn't run; '[]' = ran, found nothing.
ALTER TABLE clips ADD COLUMN entities TEXT DEFAULT NULL;

-- §2.8: redact projections. NULL = redact hasn't run; equals original sha in passthrough.
ALTER TABLE clips ADD COLUMN redacted_transcript_ref TEXT DEFAULT NULL;
ALTER TABLE clips ADD COLUMN redactions TEXT DEFAULT NULL;

COMMIT;

PRAGMA user_version = 2;

-- 003_cases.sql
-- Adds the Case linkage on sessions per ADR-0006 (revised).
--
-- A Case is NOT a separately-modelled entity in scribe.db — it is a
-- reference label (a string id) carried on the session and stamped into
-- the events log when EMR context is attached. The authoritative source
-- for case content (demographics + previous notes) is whoever calls the
-- API: the corpus loader (case.yaml) for v0, a real EMR feed later.
--
-- See ADR-0006 for the rationale (the cases table that an earlier draft
-- of this migration created was removed when we recognised it duplicated
-- corpus-resident content).
--
-- This file is NOT individually idempotent — migrate.sh is version-aware
-- and applies each file at most once based on its NNN_ prefix vs.
-- PRAGMA user_version.

BEGIN;

-- Sessions optionally carry a case_id reference. NULL means "one-off
-- session, no EMR context" — preserves the v0 session-without-context flow
-- unchanged. No FK constraint: the case content lives outside scribe.db
-- (in the corpus, or later in a real EMR), so there is no row to point at.
ALTER TABLE sessions ADD COLUMN case_id TEXT;

CREATE INDEX idx_sessions_case ON sessions(case_id);

COMMIT;

PRAGMA user_version = 3;

-- 001_initial.sql
-- Initial scribe.db schema. Matches specseed §7.2. Idempotent — safe to re-run.
-- Apply with: ./scripts/migrate.sh

BEGIN;

CREATE TABLE IF NOT EXISTS sessions (
  session_id    TEXT PRIMARY KEY,
  template_id   TEXT NOT NULL,
  state         TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  closed_at     TEXT,
  close_reason  TEXT,
  meta          TEXT NOT NULL DEFAULT '{}'  -- JSON
);

CREATE TABLE IF NOT EXISTS clips (
  clip_id              TEXT PRIMARY KEY,
  session_id           TEXT NOT NULL REFERENCES sessions(session_id),
  seq                  INTEGER NOT NULL,
  started_at           TEXT NOT NULL,
  duration_ms          INTEGER NOT NULL,
  audio_ref            TEXT NOT NULL,         -- sha256:<hex>
  state                TEXT NOT NULL,         -- uploaded | transcribing | transcribed | failed
  transcript           TEXT,
  transcript_segments  TEXT,                  -- JSON array
  meta                 TEXT NOT NULL DEFAULT '{}',  -- JSON
  UNIQUE (session_id, seq)
);

-- Append-only domain event log. Authoritative for what happened in clinical terms.
-- Per specseed §3.3, this is NOT the bus's transport log — that lives in ductile.db.
CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  event_type   TEXT NOT NULL,
  event_time   TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  clip_id      TEXT,
  data         TEXT NOT NULL,    -- JSON
  meta         TEXT NOT NULL     -- JSON — baggage envelope (worker, worker_version, model, prompt_version, latency, cost, tokens, node)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, event_time);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type, event_time);
CREATE INDEX IF NOT EXISTS idx_clips_session  ON clips(session_id, seq);

-- HTTP idempotency cache. {key → response} for 24h per specseed §8.
CREATE TABLE IF NOT EXISTS idempotency (
  key          TEXT PRIMARY KEY,
  response     TEXT NOT NULL,    -- JSON
  created_at   TEXT NOT NULL
);

-- LLM response cache keyed by prompt content hash. Makes replay deterministic.
CREATE TABLE IF NOT EXISTS llm_cache (
  prompt_hash  TEXT PRIMARY KEY,
  model        TEXT NOT NULL,
  response     TEXT NOT NULL,    -- JSON
  created_at   TEXT NOT NULL
);

COMMIT;

-- Schema version marker. Bumping schema = new file 002_*.sql; migrate.sh applies in order.
PRAGMA user_version = 1;

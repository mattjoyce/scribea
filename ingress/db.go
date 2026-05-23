package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const expectedSchemaVersion = 3

// Schema is owned by db/migrations/. Ingress refuses to start if the DB hasn't
// been migrated to the version it understands — this is the deploy-gate pattern.
func ensureSchemaCurrent(db *sql.DB) error {
	var v int
	if err := db.QueryRow("PRAGMA user_version").Scan(&v); err != nil {
		return fmt.Errorf("read user_version: %w", err)
	}
	if v != expectedSchemaVersion {
		return fmt.Errorf("schema version %d but ingress expects %d", v, expectedSchemaVersion)
	}
	return nil
}

// session row, plain data per specseed §3.1.
type sessionRow struct {
	SessionID   string          `json:"session_id"`
	TemplateID  string          `json:"template_id"`
	State       string          `json:"state"`
	StartedAt   string          `json:"started_at"`
	ClosedAt    *string         `json:"closed_at,omitempty"`
	CloseReason *string         `json:"close_reason,omitempty"`
	Meta        json.RawMessage `json:"meta"`
	CaseID      *string         `json:"case_id,omitempty"` // ADR-0006: reference label, nullable
}

type clipRow struct {
	ClipID                string          `json:"clip_id"`
	SessionID             string          `json:"session_id"`
	Seq                   int             `json:"seq"`
	StartedAt             string          `json:"started_at"`
	DurationMs            int64           `json:"duration_ms"`
	AudioRef              string          `json:"audio_ref"`
	State                 string          `json:"state"`
	Transcript            *string         `json:"transcript,omitempty"`
	TranscriptSegments    json.RawMessage `json:"transcript_segments,omitempty"`
	Entities              json.RawMessage `json:"entities,omitempty"`
	RedactedTranscriptRef *string         `json:"redacted_transcript_ref,omitempty"`
	Redactions            json.RawMessage `json:"redactions,omitempty"`
	Meta                  json.RawMessage `json:"meta"`
}

type eventRow struct {
	EventID   string          `json:"event_id"`
	EventType string          `json:"event_type"`
	EventTime string          `json:"event_time"`
	SessionID string          `json:"session_id"`
	ClipID    *string         `json:"clip_id,omitempty"`
	Data      json.RawMessage `json:"data"`
	Meta      json.RawMessage `json:"meta"`
}

// --- sessions ---

func (s *server) insertSession(row sessionRow) error {
	_, err := s.db.Exec(
		`INSERT INTO sessions(session_id, template_id, state, started_at, closed_at, close_reason, meta, case_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		row.SessionID, row.TemplateID, row.State, row.StartedAt,
		nullable(row.ClosedAt), nullable(row.CloseReason), string(row.Meta), nullable(row.CaseID),
	)
	return err
}

func (s *server) updateSessionState(sessionID, state string, closedAt, closeReason *string) error {
	_, err := s.db.Exec(
		`UPDATE sessions SET state=?, closed_at=COALESCE(?, closed_at), close_reason=COALESCE(?, close_reason)
		 WHERE session_id=?`,
		state, nullable(closedAt), nullable(closeReason), sessionID,
	)
	return err
}

func (s *server) getSession(sessionID string) (*sessionRow, error) {
	row := s.db.QueryRow(
		`SELECT session_id, template_id, state, started_at, closed_at, close_reason, meta, case_id
		 FROM sessions WHERE session_id=?`, sessionID,
	)
	var r sessionRow
	var meta string
	err := row.Scan(&r.SessionID, &r.TemplateID, &r.State, &r.StartedAt, &r.ClosedAt, &r.CloseReason, &meta, &r.CaseID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	r.Meta = json.RawMessage(meta)
	return &r, nil
}

func (s *server) listSessions(limit int) ([]sessionRow, error) {
	rows, err := s.db.Query(
		`SELECT session_id, template_id, state, started_at, closed_at, close_reason, meta, case_id
		 FROM sessions ORDER BY started_at DESC LIMIT ?`, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []sessionRow{}
	for rows.Next() {
		var r sessionRow
		var meta string
		if err := rows.Scan(&r.SessionID, &r.TemplateID, &r.State, &r.StartedAt, &r.ClosedAt, &r.CloseReason, &meta, &r.CaseID); err != nil {
			return nil, err
		}
		r.Meta = json.RawMessage(meta)
		out = append(out, r)
	}
	return out, rows.Err()
}

// findIdleSessions returns sessions in the given states whose latest activity
// is older than cutoff. Activity = max(started_at, latest event_time).
func (s *server) findIdleSessions(states []string, cutoff time.Time) ([]sessionRow, error) {
	if len(states) == 0 {
		return nil, nil
	}
	placeholders := ""
	args := []any{}
	for i, st := range states {
		if i > 0 {
			placeholders += ","
		}
		placeholders += "?"
		args = append(args, st)
	}
	args = append(args, cutoff.UTC().Format(time.RFC3339Nano))
	q := fmt.Sprintf(
		`SELECT s.session_id, s.template_id, s.state, s.started_at, s.closed_at, s.close_reason, s.meta, s.case_id
		 FROM sessions s
		 WHERE s.state IN (%s)
		 AND COALESCE((SELECT MAX(event_time) FROM events WHERE session_id=s.session_id), s.started_at) < ?`,
		placeholders,
	)
	rows, err := s.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []sessionRow{}
	for rows.Next() {
		var r sessionRow
		var meta string
		if err := rows.Scan(&r.SessionID, &r.TemplateID, &r.State, &r.StartedAt, &r.ClosedAt, &r.CloseReason, &meta, &r.CaseID); err != nil {
			return nil, err
		}
		r.Meta = json.RawMessage(meta)
		out = append(out, r)
	}
	return out, rows.Err()
}

// --- clips ---

func (s *server) insertClip(row clipRow) error {
	_, err := s.db.Exec(
		`INSERT INTO clips(clip_id, session_id, seq, started_at, duration_ms, audio_ref, state, transcript, transcript_segments, meta)
		 VALUES (?,?,?,?,?,?,?,?,?,?)`,
		row.ClipID, row.SessionID, row.Seq, row.StartedAt, row.DurationMs,
		row.AudioRef, row.State, row.Transcript, string(row.TranscriptSegments), string(row.Meta),
	)
	return err
}

func (s *server) listClips(sessionID string) ([]clipRow, error) {
	rows, err := s.db.Query(
		`SELECT clip_id, session_id, seq, started_at, duration_ms, audio_ref, state,
		        transcript, transcript_segments, entities, redacted_transcript_ref, redactions, meta
		 FROM clips WHERE session_id=? ORDER BY seq ASC`, sessionID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []clipRow{}
	for rows.Next() {
		var r clipRow
		var seg, ents, reds sql.NullString
		var meta string
		if err := rows.Scan(
			&r.ClipID, &r.SessionID, &r.Seq, &r.StartedAt, &r.DurationMs, &r.AudioRef, &r.State,
			&r.Transcript, &seg, &ents, &r.RedactedTranscriptRef, &reds, &meta,
		); err != nil {
			return nil, err
		}
		if seg.Valid {
			r.TranscriptSegments = json.RawMessage(seg.String)
		}
		if ents.Valid {
			r.Entities = json.RawMessage(ents.String)
		}
		if reds.Valid {
			r.Redactions = json.RawMessage(reds.String)
		}
		r.Meta = json.RawMessage(meta)
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *server) getClip(clipID string) (*clipRow, error) {
	row := s.db.QueryRow(
		`SELECT clip_id, session_id, seq, started_at, duration_ms, audio_ref, state,
		        transcript, transcript_segments, entities, redacted_transcript_ref, redactions, meta
		 FROM clips WHERE clip_id=?`, clipID,
	)
	var r clipRow
	var seg, ents, reds sql.NullString
	var meta string
	err := row.Scan(
		&r.ClipID, &r.SessionID, &r.Seq, &r.StartedAt, &r.DurationMs, &r.AudioRef, &r.State,
		&r.Transcript, &seg, &ents, &r.RedactedTranscriptRef, &reds, &meta,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if seg.Valid {
		r.TranscriptSegments = json.RawMessage(seg.String)
	}
	if ents.Valid {
		r.Entities = json.RawMessage(ents.String)
	}
	if reds.Valid {
		r.Redactions = json.RawMessage(reds.String)
	}
	r.Meta = json.RawMessage(meta)
	return &r, nil
}

func (s *server) updateClipTranscribed(clipID, transcript string, segments json.RawMessage, workerMeta json.RawMessage) error {
	// Stash the worker's baggage envelope under meta.transcribe so we don't
	// wipe sibling stage stamps (upload-time `audio`, `preprocessing`, etc.).
	if err := s.mergeClipMetaKey(clipID, "transcribe", workerMeta); err != nil {
		return err
	}
	_, err := s.db.Exec(
		`UPDATE clips SET state='transcribed', transcript=?, transcript_segments=?
		 WHERE clip_id=?`,
		transcript, string(segments), clipID,
	)
	return err
}

// updateClipEntities stamps the entity list under clips.entities and stashes
// the worker's baggage under meta.ner. Sibling meta keys (audio, preprocessing,
// transcribe, …) are preserved by mergeClipMetaKey.
func (s *server) updateClipEntities(clipID string, entities json.RawMessage, workerMeta json.RawMessage) error {
	if err := s.mergeClipMetaKey(clipID, "ner", workerMeta); err != nil {
		return err
	}
	_, err := s.db.Exec(
		`UPDATE clips SET entities=? WHERE clip_id=?`,
		string(entities), clipID,
	)
	return err
}

// updateClipRedacted stamps redacted_transcript_ref + redactions and stashes
// the worker's baggage under meta.redact.
func (s *server) updateClipRedacted(clipID, redactedRef string, redactions json.RawMessage, workerMeta json.RawMessage) error {
	if err := s.mergeClipMetaKey(clipID, "redact", workerMeta); err != nil {
		return err
	}
	_, err := s.db.Exec(
		`UPDATE clips SET redacted_transcript_ref=?, redactions=? WHERE clip_id=?`,
		redactedRef, string(redactions), clipID,
	)
	return err
}

func (s *server) updateClipFailed(clipID string, workerMeta json.RawMessage) error {
	if err := s.mergeClipMetaKey(clipID, "failure", workerMeta); err != nil {
		return err
	}
	_, err := s.db.Exec(
		`UPDATE clips SET state='failed' WHERE clip_id=?`,
		clipID,
	)
	return err
}

// mergeClipMetaKey does read-modify-write of clips.meta, stashing the given
// value under meta[key]. Preserves every sibling key so each pipeline stage
// (upload, preprocess, transcribe, future stages) owns its own namespace
// without trampling the others. Single-writer guarantee comes from SQLite's
// row lock — concurrent callers serialise, last writer wins for a given key.
func (s *server) mergeClipMetaKey(clipID, key string, value json.RawMessage) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback() //nolint:errcheck // commit clears it; rollback on error path
	var existing string
	if err := tx.QueryRow(`SELECT meta FROM clips WHERE clip_id=?`, clipID).Scan(&existing); err != nil {
		return err
	}
	meta := map[string]any{}
	if existing != "" {
		_ = json.Unmarshal([]byte(existing), &meta)
	}
	var v any
	if len(value) > 0 {
		_ = json.Unmarshal(value, &v)
	}
	meta[key] = v
	b, err := json.Marshal(meta)
	if err != nil {
		return err
	}
	if _, err := tx.Exec(`UPDATE clips SET meta=? WHERE clip_id=?`, string(b), clipID); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *server) nextClipSeq(sessionID string) (int, error) {
	var maxSeq sql.NullInt64
	err := s.db.QueryRow(`SELECT MAX(seq) FROM clips WHERE session_id=?`, sessionID).Scan(&maxSeq)
	if err != nil {
		return 0, err
	}
	if !maxSeq.Valid {
		return 0, nil
	}
	return int(maxSeq.Int64) + 1, nil
}

// --- events ---

func (s *server) appendEvent(ev eventRow) error {
	if ev.EventID == "" {
		return errors.New("appendEvent: empty event_id")
	}
	_, err := s.db.Exec(
		`INSERT INTO events(event_id, event_type, event_time, session_id, clip_id, data, meta)
		 VALUES (?,?,?,?,?,?,?)
		 ON CONFLICT(event_id) DO NOTHING`,
		ev.EventID, ev.EventType, ev.EventTime, ev.SessionID, nullable(ev.ClipID),
		string(ev.Data), string(ev.Meta),
	)
	if err == nil {
		s.hub.publish(ev.SessionID, ev)
	}
	return err
}

func (s *server) listEvents(sessionID string) ([]eventRow, error) {
	rows, err := s.db.Query(
		`SELECT event_id, event_type, event_time, session_id, clip_id, data, meta
		 FROM events WHERE session_id=? ORDER BY event_time ASC, event_id ASC`,
		sessionID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []eventRow{}
	for rows.Next() {
		var r eventRow
		var data, meta string
		if err := rows.Scan(&r.EventID, &r.EventType, &r.EventTime, &r.SessionID, &r.ClipID, &data, &meta); err != nil {
			return nil, err
		}
		r.Data = json.RawMessage(data)
		r.Meta = json.RawMessage(meta)
		out = append(out, r)
	}
	return out, rows.Err()
}

// deleteSessionCascade removes the session row and all rows that
// reference it (events first to satisfy ordering even though there's no
// FK on events.session_id; clips next via the explicit FK to sessions).
// Returns per-table delete counts. Blobs are NOT touched — they are
// content-addressed and may be shared across sessions; GC them
// separately if disk pressure matters.
func (s *server) deleteSessionCascade(sessionID string) (map[string]int64, error) {
	out := map[string]int64{"events": 0, "clips": 0, "sessions": 0}
	tx, err := s.db.Begin()
	if err != nil {
		return out, err
	}
	defer tx.Rollback() //nolint:errcheck

	res, err := tx.Exec(`DELETE FROM events WHERE session_id=?`, sessionID)
	if err != nil {
		return out, fmt.Errorf("delete events: %w", err)
	}
	if n, e := res.RowsAffected(); e == nil {
		out["events"] = n
	}

	res, err = tx.Exec(`DELETE FROM clips WHERE session_id=?`, sessionID)
	if err != nil {
		return out, fmt.Errorf("delete clips: %w", err)
	}
	if n, e := res.RowsAffected(); e == nil {
		out["clips"] = n
	}

	res, err = tx.Exec(`DELETE FROM sessions WHERE session_id=?`, sessionID)
	if err != nil {
		return out, fmt.Errorf("delete session: %w", err)
	}
	if n, e := res.RowsAffected(); e == nil {
		out["sessions"] = n
	}

	if err := tx.Commit(); err != nil {
		return out, err
	}
	return out, nil
}

// --- idempotency ---

func (s *server) idempotencyGet(key string) (json.RawMessage, bool, error) {
	if key == "" {
		return nil, false, nil
	}
	var resp string
	err := s.db.QueryRow(`SELECT response FROM idempotency WHERE key=?`, key).Scan(&resp)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	return json.RawMessage(resp), true, nil
}

func (s *server) idempotencyPut(key string, resp json.RawMessage) error {
	if key == "" {
		return nil
	}
	_, err := s.db.Exec(
		`INSERT OR REPLACE INTO idempotency(key, response, created_at) VALUES(?,?,?)`,
		key, string(resp), time.Now().UTC().Format(time.RFC3339Nano),
	)
	return err
}

func nullable(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const expectedSchemaVersion = 1

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
}

type clipRow struct {
	ClipID             string          `json:"clip_id"`
	SessionID          string          `json:"session_id"`
	Seq                int             `json:"seq"`
	StartedAt          string          `json:"started_at"`
	DurationMs         int64           `json:"duration_ms"`
	AudioRef           string          `json:"audio_ref"`
	State              string          `json:"state"`
	Transcript         *string         `json:"transcript,omitempty"`
	TranscriptSegments json.RawMessage `json:"transcript_segments,omitempty"`
	Meta               json.RawMessage `json:"meta"`
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
		`INSERT INTO sessions(session_id, template_id, state, started_at, closed_at, close_reason, meta)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		row.SessionID, row.TemplateID, row.State, row.StartedAt, nullable(row.ClosedAt), nullable(row.CloseReason), string(row.Meta),
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
		`SELECT session_id, template_id, state, started_at, closed_at, close_reason, meta
		 FROM sessions WHERE session_id=?`, sessionID,
	)
	var r sessionRow
	var meta string
	err := row.Scan(&r.SessionID, &r.TemplateID, &r.State, &r.StartedAt, &r.ClosedAt, &r.CloseReason, &meta)
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
		`SELECT session_id, template_id, state, started_at, closed_at, close_reason, meta
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
		if err := rows.Scan(&r.SessionID, &r.TemplateID, &r.State, &r.StartedAt, &r.ClosedAt, &r.CloseReason, &meta); err != nil {
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
		`SELECT s.session_id, s.template_id, s.state, s.started_at, s.closed_at, s.close_reason, s.meta
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
		if err := rows.Scan(&r.SessionID, &r.TemplateID, &r.State, &r.StartedAt, &r.ClosedAt, &r.CloseReason, &meta); err != nil {
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
		`SELECT clip_id, session_id, seq, started_at, duration_ms, audio_ref, state, transcript, transcript_segments, meta
		 FROM clips WHERE session_id=? ORDER BY seq ASC`, sessionID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []clipRow{}
	for rows.Next() {
		var r clipRow
		var seg sql.NullString
		var meta string
		if err := rows.Scan(&r.ClipID, &r.SessionID, &r.Seq, &r.StartedAt, &r.DurationMs, &r.AudioRef, &r.State, &r.Transcript, &seg, &meta); err != nil {
			return nil, err
		}
		if seg.Valid {
			r.TranscriptSegments = json.RawMessage(seg.String)
		}
		r.Meta = json.RawMessage(meta)
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *server) getClip(clipID string) (*clipRow, error) {
	row := s.db.QueryRow(
		`SELECT clip_id, session_id, seq, started_at, duration_ms, audio_ref, state, transcript, transcript_segments, meta
		 FROM clips WHERE clip_id=?`, clipID,
	)
	var r clipRow
	var seg sql.NullString
	var meta string
	err := row.Scan(&r.ClipID, &r.SessionID, &r.Seq, &r.StartedAt, &r.DurationMs, &r.AudioRef, &r.State, &r.Transcript, &seg, &meta)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if seg.Valid {
		r.TranscriptSegments = json.RawMessage(seg.String)
	}
	r.Meta = json.RawMessage(meta)
	return &r, nil
}

func (s *server) updateClipTranscribed(clipID, transcript string, segments json.RawMessage, meta json.RawMessage) error {
	_, err := s.db.Exec(
		`UPDATE clips SET state='transcribed', transcript=?, transcript_segments=?, meta=?
		 WHERE clip_id=?`,
		transcript, string(segments), string(meta), clipID,
	)
	return err
}

func (s *server) updateClipFailed(clipID string, meta json.RawMessage) error {
	_, err := s.db.Exec(
		`UPDATE clips SET state='failed', meta=? WHERE clip_id=?`,
		string(meta), clipID,
	)
	return err
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

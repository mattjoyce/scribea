package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"

	"github.com/google/uuid"
)

// ----- templates -----

func (s *server) handleListTemplates(w http.ResponseWriter, r *http.Request) {
	entries, err := os.ReadDir(s.cfg.templatesDir)
	if err != nil {
		writeError(w, 500, "templates_read", err.Error())
		return
	}
	out := []map[string]any{}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		metaPath := filepath.Join(s.cfg.templatesDir, e.Name(), "template.json")
		b, err := os.ReadFile(metaPath)
		if err != nil {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal(b, &m); err != nil {
			log.Printf("templates: bad %s: %v", metaPath, err)
			continue
		}
		out = append(out, m)
	}
	writeJSON(w, 200, out)
}

// ----- sessions -----

type createSessionReq struct {
	TemplateID string `json:"template_id"`
	Meta       map[string]any `json:"meta,omitempty"`
}

func (s *server) handleCreateSession(w http.ResponseWriter, r *http.Request) {
	idemKey := r.Header.Get("Idempotency-Key")
	if cached, ok, err := s.idempotencyGet(idemKey); err != nil {
		writeError(w, 500, "idem_lookup", err.Error())
		return
	} else if ok {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write(cached)
		return
	}

	var req createSessionReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	if req.TemplateID == "" {
		writeError(w, 400, "missing_template_id", "template_id is required")
		return
	}
	if _, err := os.Stat(filepath.Join(s.cfg.templatesDir, req.TemplateID, "template.json")); err != nil {
		writeError(w, 404, "unknown_template", req.TemplateID)
		return
	}

	now := time.Now().UTC().Format(time.RFC3339Nano)
	sessionID := uuid.NewString()
	metaB, _ := json.Marshal(req.Meta)
	if string(metaB) == "null" {
		metaB = []byte(`{}`)
	}
	if err := s.insertSession(sessionRow{
		SessionID:  sessionID,
		TemplateID: req.TemplateID,
		State:      "open",
		StartedAt:  now,
		Meta:       metaB,
	}); err != nil {
		writeError(w, 500, "db_insert", err.Error())
		return
	}

	eventID := uuid.NewString()
	dataB, _ := json.Marshal(map[string]any{"template_id": req.TemplateID})
	if err := s.appendEvent(eventRow{
		EventID:   eventID,
		EventType: "scribe.session.created.v1",
		EventTime: now,
		SessionID: sessionID,
		Data:      dataB,
		Meta:      ingressMeta("session_created"),
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}

	resp, _ := json.Marshal(map[string]any{
		"session_id":  sessionID,
		"template_id": req.TemplateID,
		"state":       "open",
		"started_at":  now,
	})
	_ = s.idempotencyPut(idemKey, resp)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(201)
	_, _ = w.Write(resp)
}

func (s *server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 1000 {
			limit = n
		}
	}
	out, err := s.listSessions(limit)
	if err != nil {
		writeError(w, 500, "db_list", err.Error())
		return
	}
	writeJSON(w, 200, out)
}

func (s *server) handleGetSession(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	sess, err := s.getSession(id)
	if err != nil {
		writeError(w, 500, "db_read", err.Error())
		return
	}
	if sess == nil {
		writeError(w, 404, "no_such_session", id)
		return
	}
	clips, err := s.listClips(id)
	if err != nil {
		writeError(w, 500, "db_clips", err.Error())
		return
	}
	out := map[string]any{
		"session": sess,
		"clips":   clips,
	}
	writeJSON(w, 200, out)
}

// ----- clips -----

func (s *server) handleUploadClip(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("id")
	idemKey := r.Header.Get("Idempotency-Key")
	if idemKey == "" {
		writeError(w, 400, "missing_idempotency_key", "PWA must send Idempotency-Key: <clip_id>")
		return
	}
	if cached, ok, err := s.idempotencyGet(idemKey); err != nil {
		writeError(w, 500, "idem_lookup", err.Error())
		return
	} else if ok {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write(cached)
		return
	}

	sess, err := s.getSession(sessionID)
	if err != nil {
		writeError(w, 500, "db_read", err.Error())
		return
	}
	if sess == nil {
		writeError(w, 404, "no_such_session", sessionID)
		return
	}
	if sess.State == "closed" || sess.State == "assembling" || sess.State == "completed" || sess.State == "failed" || sess.State == "abandoned" {
		writeError(w, 410, "session_closed", fmt.Sprintf("state=%s", sess.State))
		return
	}

	// Max ~100MB to avoid runaway uploads — clip cap is 10 min audio.
	if err := r.ParseMultipartForm(100 << 20); err != nil {
		writeError(w, 400, "bad_multipart", err.Error())
		return
	}

	clipID := r.FormValue("clip_id")
	if clipID == "" {
		clipID = idemKey
	}
	if !validUUID(clipID) {
		writeError(w, 400, "bad_clip_id", "clip_id must be uuid v4")
		return
	}

	startedAt := r.FormValue("started_at")
	if startedAt == "" {
		startedAt = time.Now().UTC().Format(time.RFC3339Nano)
	}
	durationMs, _ := strconv.ParseInt(r.FormValue("duration_ms"), 10, 64)
	if durationMs <= 0 {
		writeError(w, 400, "bad_duration", "duration_ms must be > 0")
		return
	}
	if durationMs > s.cfg.clipMaxMillis {
		writeError(w, 413, "clip_too_long", fmt.Sprintf("duration_ms=%d > max=%d", durationMs, s.cfg.clipMaxMillis))
		return
	}
	seq, _ := strconv.Atoi(r.FormValue("seq"))
	if seq == 0 {
		seq, err = s.nextClipSeq(sessionID)
		if err != nil {
			writeError(w, 500, "seq_lookup", err.Error())
			return
		}
	}
	audioFormat := r.FormValue("audio_format")

	// Save the multipart "audio" file to a content-addressed path.
	file, hdr, err := r.FormFile("audio")
	if err != nil {
		writeError(w, 400, "missing_audio", err.Error())
		return
	}
	defer file.Close()

	tmp, err := os.CreateTemp(s.cfg.blobsDir, ".inflight-*")
	if err != nil {
		writeError(w, 500, "tmpfile", err.Error())
		return
	}
	hasher := sha256.New()
	mw := io.MultiWriter(tmp, hasher)
	if _, err := io.Copy(mw, file); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		writeError(w, 500, "copy", err.Error())
		return
	}
	tmp.Close()
	sum := hex.EncodeToString(hasher.Sum(nil))
	dest := filepath.Join(s.cfg.blobsDir, sum)
	// Rename (no clobber). If a blob with this hash already exists, drop the
	// new file — content-addressed storage is naturally deduplicated.
	if _, err := os.Stat(dest); os.IsNotExist(err) {
		if err := os.Rename(tmp.Name(), dest); err != nil {
			os.Remove(tmp.Name())
			writeError(w, 500, "rename", err.Error())
			return
		}
	} else {
		os.Remove(tmp.Name())
	}
	audioRef := "sha256:" + sum

	// Resolve audio shape: named values from the PWA + ground truth from
	// ffprobe on the saved blob. Both best-effort; if neither produces
	// anything we still write the upload with `audio: null`.
	var pwaAudio map[string]any
	if raw := r.FormValue("audio_meta"); raw != "" {
		if err := json.Unmarshal([]byte(raw), &pwaAudio); err != nil {
			log.Printf("audio_meta parse failed: %v (ignoring PWA-supplied audio map)", err)
		}
	}
	audioInfo := mergeAudio(pwaAudio, ffprobeAudio(dest))
	if audioInfo != nil {
		if _, ok := audioInfo["size_bytes"]; !ok {
			audioInfo["size_bytes"] = hdr.Size
		}
	}

	now := time.Now().UTC().Format(time.RFC3339Nano)
	clipMeta, _ := json.Marshal(map[string]any{
		"audio_format":  audioFormat,
		"original_name": hdr.Filename,
		"uploaded_at":   now,
		"upload_size":   hdr.Size,
		"audio":         audioInfo,
	})

	if err := s.insertClip(clipRow{
		ClipID:     clipID,
		SessionID:  sessionID,
		Seq:        seq,
		StartedAt:  startedAt,
		DurationMs: durationMs,
		AudioRef:   audioRef,
		State:      "uploaded",
		Meta:       clipMeta,
	}); err != nil {
		writeError(w, 500, "db_insert_clip", err.Error())
		return
	}

	if sess.State == "open" {
		_ = s.updateSessionState(sessionID, "recording", nil, nil)
	}

	eventID := uuid.NewString()
	dataB, _ := json.Marshal(map[string]any{
		"clip_id":      clipID,
		"audio_ref":    audioRef,
		"seq":          seq,
		"started_at":   startedAt,
		"duration_ms":  durationMs,
		"audio_format": audioFormat,
		"audio":        audioInfo,
		"blob_path":    dest,
		"session_id":   sessionID,
	})
	if err := s.appendEvent(eventRow{
		EventID:   eventID,
		EventType: "scribe.clip.received.v1",
		EventTime: now,
		SessionID: sessionID,
		ClipID:    &clipID,
		Data:      dataB,
		Meta:      ingressMeta("clip_received"),
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}

	// Hand the event to Ductile for the pipeline to pick up.
	if err := s.ductile.emit("scribe.clip.received.v1", map[string]any{
		"session_id":   sessionID,
		"clip_id":      clipID,
		"audio_ref":    audioRef,
		"blob_path":    dest,
		"seq":          seq,
		"duration_ms":  durationMs,
		"audio_format": audioFormat,
		"audio":        audioInfo,
	}); err != nil {
		log.Printf("ductile emit clip.received failed: %v (clip will need manual retry)", err)
	}

	resp, _ := json.Marshal(map[string]any{
		"clip_id":    clipID,
		"session_id": sessionID,
		"seq":        seq,
		"audio_ref":  audioRef,
		"state":      "uploaded",
		"received_at": now,
	})
	_ = s.idempotencyPut(idemKey, resp)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(202)
	_, _ = w.Write(resp)
}

func (s *server) handleListClips(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	clips, err := s.listClips(id)
	if err != nil {
		writeError(w, 500, "db_clips", err.Error())
		return
	}
	writeJSON(w, 200, clips)
}

// ----- close / note / baggage -----

type closeReq struct {
	CloseReason string `json:"close_reason"`
}

func (s *server) handleCloseSession(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	sess, err := s.getSession(id)
	if err != nil {
		writeError(w, 500, "db_read", err.Error())
		return
	}
	if sess == nil {
		writeError(w, 404, "no_such_session", id)
		return
	}
	if sess.State == "completed" || sess.State == "failed" || sess.State == "abandoned" {
		writeError(w, 409, "session_terminal", "session already in terminal state")
		return
	}

	var req closeReq
	_ = json.NewDecoder(r.Body).Decode(&req) // body optional
	reason := req.CloseReason
	if reason == "" {
		reason = "user"
	}

	now := time.Now().UTC().Format(time.RFC3339Nano)
	_ = s.updateSessionState(id, "closed", &now, &reason)

	eventID := uuid.NewString()
	dataB, _ := json.Marshal(map[string]any{"close_reason": reason})
	if err := s.appendEvent(eventRow{
		EventID:   eventID,
		EventType: "scribe.session.close_requested.v1",
		EventTime: now,
		SessionID: id,
		Data:      dataB,
		Meta:      ingressMeta("close_requested"),
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}

	_ = s.updateSessionState(id, "assembling", nil, nil)

	if err := s.ductile.emit("scribe.session.close_requested.v1", map[string]any{
		"session_id":   id,
		"close_reason": reason,
		"template_id":  sess.TemplateID,
	}); err != nil {
		log.Printf("ductile emit close_requested failed: %v", err)
	}

	writeJSON(w, 202, map[string]any{
		"session_id":   id,
		"state":        "assembling",
		"close_reason": reason,
		"closed_at":    now,
	})
}

func (s *server) handleGetNote(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	sess, err := s.getSession(id)
	if err != nil {
		writeError(w, 500, "db_read", err.Error())
		return
	}
	if sess == nil {
		writeError(w, 404, "no_such_session", id)
		return
	}
	if sess.State != "completed" {
		writeError(w, 404, "note_not_ready", fmt.Sprintf("state=%s", sess.State))
		return
	}
	// Find the last scribe.session.completed.v1 event and return its data.
	events, err := s.listEvents(id)
	if err != nil {
		writeError(w, 500, "db_events", err.Error())
		return
	}
	var note map[string]any
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].EventType == "scribe.session.completed.v1" {
			_ = json.Unmarshal(events[i].Data, &note)
			break
		}
	}
	if note == nil {
		writeError(w, 500, "note_missing", "completed event not found")
		return
	}
	writeJSON(w, 200, note)
}

func (s *server) handleGetBaggage(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	events, err := s.listEvents(id)
	if err != nil {
		writeError(w, 500, "db_events", err.Error())
		return
	}
	writeJSON(w, 200, events)
}

// ----- internal callbacks from workers -----

type clipPreprocessedReq struct {
	SessionID        string          `json:"session_id"`
	AudioRef         string          `json:"audio_ref"`          // sha256: of cleaned WAV
	OriginalAudioRef string          `json:"original_audio_ref"` // sha256: of upload
	BlobPath         string          `json:"blob_path"`          // absolute path to cleaned WAV
	Preprocessing   json.RawMessage `json:"preprocessing"`
	Meta            json.RawMessage `json:"meta"`
}

func (s *server) handleInternalClipPreprocessed(w http.ResponseWriter, r *http.Request) {
	clipID := r.PathValue("id")
	var req clipPreprocessedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	// Stash the preprocessing block under meta.preprocessing. Sibling keys
	// (upload-time `audio`, future stage stamps) are preserved by mergeClipMetaKey.
	if err := s.mergeClipMetaKey(clipID, "preprocessing", req.Preprocessing); err != nil {
		writeError(w, 500, "db_merge_meta", err.Error())
		return
	}
	var pp map[string]any
	_ = json.Unmarshal(req.Preprocessing, &pp)

	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{
		"clip_id":             clipID,
		"audio_ref":           req.AudioRef,
		"original_audio_ref":  req.OriginalAudioRef,
		"blob_path":           req.BlobPath,
		"preprocessing":       pp,
	})
	if err := s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.clip.preprocessed.v1",
		EventTime: now,
		SessionID: req.SessionID,
		ClipID:    &clipID,
		Data:      dataB,
		Meta:      req.Meta,
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

type clipPreprocessFailedReq struct {
	SessionID        string          `json:"session_id"`
	OriginalAudioRef string          `json:"original_audio_ref"`
	Reason           string          `json:"reason"`
	Stage            string          `json:"stage"`
	Meta             json.RawMessage `json:"meta"`
}

func (s *server) handleInternalClipPreprocessFailed(w http.ResponseWriter, r *http.Request) {
	clipID := r.PathValue("id")
	var req clipPreprocessFailedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	// Mark the clip failed so assemble notes the gap honestly.
	if err := s.updateClipFailed(clipID, req.Meta); err != nil {
		writeError(w, 500, "db_update", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{
		"clip_id":            clipID,
		"original_audio_ref": req.OriginalAudioRef,
		"reason":             req.Reason,
		"stage":              req.Stage,
	})
	_ = s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.clip.preprocess_failed.v1",
		EventTime: now,
		SessionID: req.SessionID,
		ClipID:    &clipID,
		Data:      dataB,
		Meta:      req.Meta,
	})
	writeJSON(w, 200, map[string]any{"ok": true})
}

type clipTranscribedReq struct {
	Transcript string          `json:"transcript"`
	Segments   json.RawMessage `json:"segments"`
	Meta       json.RawMessage `json:"meta"`
	SessionID  string          `json:"session_id"`
}

func (s *server) handleInternalClipTranscribed(w http.ResponseWriter, r *http.Request) {
	clipID := r.PathValue("id")
	var req clipTranscribedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	if err := s.updateClipTranscribed(clipID, req.Transcript, req.Segments, req.Meta); err != nil {
		writeError(w, 500, "db_update", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{
		"clip_id":    clipID,
		"transcript": req.Transcript,
		"segments":   json.RawMessage(req.Segments),
	})
	if err := s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.clip.transcribed.v1",
		EventTime: now,
		SessionID: req.SessionID,
		ClipID:    &clipID,
		Data:      dataB,
		Meta:      req.Meta,
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

type clipFailedReq struct {
	Reason    string          `json:"reason"`
	Meta      json.RawMessage `json:"meta"`
	SessionID string          `json:"session_id"`
}

func (s *server) handleInternalClipFailed(w http.ResponseWriter, r *http.Request) {
	clipID := r.PathValue("id")
	var req clipFailedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	if err := s.updateClipFailed(clipID, req.Meta); err != nil {
		writeError(w, 500, "db_update", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{"clip_id": clipID, "reason": req.Reason})
	_ = s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.clip.failed.v1",
		EventTime: now,
		SessionID: req.SessionID,
		ClipID:    &clipID,
		Data:      dataB,
		Meta:      req.Meta,
	})
	writeJSON(w, 200, map[string]any{"ok": true})
}

type assembledReq struct {
	AssembledContext string          `json:"assembled_context"`
	Gaps             json.RawMessage `json:"gaps"`
	Meta             json.RawMessage `json:"meta"`
}

func (s *server) handleInternalAssembled(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req assembledReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{
		"assembled_context": req.AssembledContext,
		"gaps":              json.RawMessage(req.Gaps),
	})
	if err := s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.session.assembled.v1",
		EventTime: now,
		SessionID: id,
		Data:      dataB,
		Meta:      req.Meta,
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

type structuredReq struct {
	Structured      json.RawMessage `json:"structured"`
	RawLLMResponse  json.RawMessage `json:"raw_llm_response,omitempty"`
	Meta            json.RawMessage `json:"meta"`
}

func (s *server) handleInternalStructured(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req structuredReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{
		"structured":       json.RawMessage(req.Structured),
		"raw_llm_response": json.RawMessage(req.RawLLMResponse),
	})
	if err := s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.session.structured.v1",
		EventTime: now,
		SessionID: id,
		Data:      dataB,
		Meta:      req.Meta,
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

type completedReq struct {
	Markdown   string          `json:"markdown"`
	Structured json.RawMessage `json:"structured"`
	Meta       json.RawMessage `json:"meta"`
}

func (s *server) handleInternalCompleted(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req completedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(map[string]any{
		"markdown":   req.Markdown,
		"structured": json.RawMessage(req.Structured),
	})
	if err := s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.session.completed.v1",
		EventTime: now,
		SessionID: id,
		Data:      dataB,
		Meta:      req.Meta,
	}); err != nil {
		writeError(w, 500, "append_event", err.Error())
		return
	}
	if err := s.updateSessionState(id, "completed", &now, nil); err != nil {
		writeError(w, 500, "db_update", err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

type sessionFailedReq struct {
	Stage  string          `json:"stage"`
	Reason string          `json:"reason"`
	Meta   json.RawMessage `json:"meta"`
}

func (s *server) handleInternalSessionFailed(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req sessionFailedReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "bad_json", err.Error())
		return
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	dataB, _ := json.Marshal(req)
	_ = s.appendEvent(eventRow{
		EventID:   uuid.NewString(),
		EventType: "scribe.session.failed.v1",
		EventTime: now,
		SessionID: id,
		Data:      dataB,
		Meta:      req.Meta,
	})
	reason := req.Reason
	_ = s.updateSessionState(id, "failed", &now, &reason)
	writeJSON(w, 200, map[string]any{"ok": true})
}

// ----- helpers -----

func ingressMeta(stage string) json.RawMessage {
	host, _ := os.Hostname()
	b, _ := json.Marshal(map[string]any{
		"worker":         "scribe-ingress",
		"worker_version": ingressVersion,
		"stage":          stage,
		"node":           host,
	})
	return b
}

const ingressVersion = "0.1.0"

func validUUID(s string) bool {
	_, err := uuid.Parse(s)
	return err == nil
}

// ffprobeAudio shells out to ffprobe and returns a named audio map for the
// first audio stream in the file. Returns nil if ffprobe is missing or fails;
// the caller should treat ground-truth as best-effort, not load-bearing.
func ffprobeAudio(path string) map[string]any {
	cmd := exec.Command(
		"ffprobe", "-v", "quiet", "-print_format", "json",
		"-show_format", "-show_streams", "-select_streams", "a:0",
		path,
	)
	out, err := cmd.Output()
	if err != nil {
		return nil
	}
	var parsed struct {
		Format struct {
			FormatName string `json:"format_name"`
			BitRate    string `json:"bit_rate"`
			Size       string `json:"size"`
			Duration   string `json:"duration"`
		} `json:"format"`
		Streams []struct {
			CodecName     string `json:"codec_name"`
			CodecType     string `json:"codec_type"`
			SampleRate    string `json:"sample_rate"`
			Channels      int    `json:"channels"`
			BitsPerSample int    `json:"bits_per_sample"`
			BitRate       string `json:"bit_rate"`
		} `json:"streams"`
	}
	if err := json.Unmarshal(out, &parsed); err != nil {
		return nil
	}
	m := map[string]any{}
	if len(parsed.Streams) > 0 {
		s := parsed.Streams[0]
		if s.CodecName != "" {
			m["codec"] = s.CodecName
		}
		if s.SampleRate != "" {
			if n, err := strconv.Atoi(s.SampleRate); err == nil {
				m["sample_rate_hz"] = n
			}
		}
		if s.Channels > 0 {
			m["channels"] = s.Channels
		}
		if s.BitsPerSample > 0 {
			m["bit_depth"] = s.BitsPerSample
		}
		if s.BitRate != "" {
			if n, err := strconv.Atoi(s.BitRate); err == nil {
				m["bit_rate_bps"] = n
			}
		}
	}
	if parsed.Format.FormatName != "" {
		m["container"] = parsed.Format.FormatName
	}
	if _, has := m["bit_rate_bps"]; !has && parsed.Format.BitRate != "" {
		if n, err := strconv.Atoi(parsed.Format.BitRate); err == nil {
			m["bit_rate_bps"] = n
		}
	}
	if len(m) == 0 {
		return nil
	}
	return m
}

// mergeAudio combines the PWA's best-effort view (browser MediaRecorder +
// getSettings) with ffprobe's ground truth. Ground truth wins on conflict.
// Stamps `audio.source` so the audit trail says where each came from.
func mergeAudio(pwa, probed map[string]any) map[string]any {
	if pwa == nil && probed == nil {
		return nil
	}
	out := map[string]any{}
	for k, v := range pwa {
		if k == "source" {
			continue
		}
		if v == nil {
			continue
		}
		out[k] = v
	}
	for k, v := range probed {
		if v == nil {
			continue
		}
		out[k] = v
	}
	switch {
	case len(pwa) > 0 && len(probed) > 0:
		out["source"] = "pwa+ffprobe"
	case len(probed) > 0:
		out["source"] = "ffprobe"
	default:
		out["source"] = "pwa"
	}
	return out
}

// runSweeper finds idle sessions and synthesizes timeout closes per ADR-0004.
func (s *server) runSweeper(ctx context.Context) {
	ticker := time.NewTicker(1 * time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		now := time.Now()
		// Abandon empty 'open' sessions older than openTimeout.
		if abandoned, err := s.findIdleSessions([]string{"open"}, now.Add(-s.cfg.openTimeout)); err == nil {
			for _, sess := range abandoned {
				log.Printf("sweeper: abandoning session %s (open idle > %s)", sess.SessionID, s.cfg.openTimeout)
				n := now.UTC().Format(time.RFC3339Nano)
				reason := "timeout"
				_ = s.updateSessionState(sess.SessionID, "abandoned", &n, &reason)
				data, _ := json.Marshal(map[string]any{"close_reason": "timeout"})
				_ = s.appendEvent(eventRow{
					EventID:   uuid.NewString(),
					EventType: "scribe.session.timed_out.v1",
					EventTime: n,
					SessionID: sess.SessionID,
					Data:      data,
					Meta:      ingressMeta("sweeper"),
				})
			}
		}
		// Auto-close 'recording' sessions idle past the template's timeout.
		timeout := s.cfg.sessionTimeout
		if recIdle, err := s.findIdleSessions([]string{"recording"}, now.Add(-timeout)); err == nil {
			for _, sess := range recIdle {
				log.Printf("sweeper: auto-closing session %s (recording idle > %s)", sess.SessionID, timeout)
				n := now.UTC().Format(time.RFC3339Nano)
				reason := "timeout"
				_ = s.updateSessionState(sess.SessionID, "closed", &n, &reason)
				_ = s.updateSessionState(sess.SessionID, "assembling", nil, nil)
				data, _ := json.Marshal(map[string]any{"close_reason": "timeout"})
				_ = s.appendEvent(eventRow{
					EventID:   uuid.NewString(),
					EventType: "scribe.session.close_requested.v1",
					EventTime: n,
					SessionID: sess.SessionID,
					Data:      data,
					Meta:      ingressMeta("sweeper"),
				})
				if err := s.ductile.emit("scribe.session.close_requested.v1", map[string]any{
					"session_id":   sess.SessionID,
					"close_reason": "timeout",
					"template_id":  sess.TemplateID,
				}); err != nil {
					log.Printf("sweeper ductile emit failed: %v", err)
				}
			}
		}
	}
}


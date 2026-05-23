package main

import (
	"database/sql"
	"encoding/json"
	"log"
	"net/http"
	"runtime/debug"
	"strings"
	"time"
)

type server struct {
	cfg     config
	db      *sql.DB
	hub     *sseHub
	ductile *ductileClient
}

func (s *server) registerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.HandleFunc("GET /templates", s.handleListTemplates)
	mux.HandleFunc("POST /sessions", s.handleCreateSession)
	mux.HandleFunc("GET /sessions", s.handleListSessions)
	mux.HandleFunc("GET /sessions/{id}", s.handleGetSession)
	mux.HandleFunc("DELETE /sessions/{id}", s.handleDeleteSession)
	mux.HandleFunc("POST /sessions/{id}/clips", s.handleUploadClip)
	mux.HandleFunc("GET /sessions/{id}/clips", s.handleListClips)
	mux.HandleFunc("GET /sessions/{id}/clips/{clip_id}/audio", s.handleGetClipAudio)
	mux.HandleFunc("POST /sessions/{id}/close", s.handleCloseSession)
	mux.HandleFunc("GET /sessions/{id}/note", s.handleGetNote)
	mux.HandleFunc("GET /sessions/{id}/baggage", s.handleGetBaggage)
	mux.HandleFunc("GET /sessions/{id}/live", s.handleSSE)

	// Internal callback endpoints used by the worker plugins to write their
	// results back to scribe.db without each plugin needing a separate DB
	// connection. Plugin → ingress → scribe.db keeps the writer count low.
	mux.HandleFunc("POST /internal/clips/{id}/preprocessed", s.handleInternalClipPreprocessed)
	mux.HandleFunc("POST /internal/clips/{id}/preprocess_failed", s.handleInternalClipPreprocessFailed)
	mux.HandleFunc("POST /internal/clips/{id}/transcribed", s.handleInternalClipTranscribed)
	mux.HandleFunc("POST /internal/clips/{id}/entities", s.handleInternalClipEntities)
	mux.HandleFunc("POST /internal/clips/{id}/redacted", s.handleInternalClipRedacted)
	mux.HandleFunc("POST /internal/clips/{id}/failed", s.handleInternalClipFailed)
	mux.HandleFunc("POST /internal/sessions/{id}/assembled", s.handleInternalAssembled)
	mux.HandleFunc("POST /internal/sessions/{id}/structured", s.handleInternalStructured)
	mux.HandleFunc("POST /internal/sessions/{id}/completed", s.handleInternalCompleted)
	mux.HandleFunc("POST /internal/sessions/{id}/failed", s.handleInternalSessionFailed)

	// PWA static assets (last so explicit routes win).
	mux.Handle("GET /", http.FileServer(http.Dir(s.cfg.pwaDir)))
}

func (s *server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"time":   time.Now().UTC().Format(time.RFC3339Nano),
	})
}

// writeJSON serializes v and writes it as application/json. If marshaling
// fails the connection is closed silently; the caller's response is already
// committed (status header is set first).
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	if err := enc.Encode(v); err != nil {
		log.Printf("writeJSON encode error: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": code, "message": msg})
}

func withRecovery(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("PANIC %s %s: %v\n%s", r.Method, r.URL.Path, rec, debug.Stack())
				if !headersWritten(w) {
					writeError(w, 500, "internal", "internal server error")
				}
			}
		}()
		h.ServeHTTP(w, r)
	})
}

func headersWritten(_ http.ResponseWriter) bool { return false }

func withRequestLog(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusRecorder{ResponseWriter: w, status: 200}
		h.ServeHTTP(rw, r)
		// Suppress noise from SSE long-polls and static asset hits.
		if !strings.HasSuffix(r.URL.Path, "/live") {
			log.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rw.status, time.Since(start))
		}
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (rw *statusRecorder) WriteHeader(s int) {
	rw.status = s
	rw.ResponseWriter.WriteHeader(s)
}

func (rw *statusRecorder) Flush() {
	if f, ok := rw.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

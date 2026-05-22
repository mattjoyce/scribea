package main

import (
	"encoding/json"
	"net/http"
	"sync"
)

// sseHub is a tiny in-process pub-sub keyed by session_id. Subscribers get
// every event appended to that session after they connected. No persistence;
// reconnecting clients should also fetch /baggage for the full history.
type sseHub struct {
	mu   sync.Mutex
	subs map[string]map[chan eventRow]struct{}
}

func newSSEHub() *sseHub {
	return &sseHub{subs: map[string]map[chan eventRow]struct{}{}}
}

func (h *sseHub) subscribe(sessionID string) (<-chan eventRow, func()) {
	ch := make(chan eventRow, 32)
	h.mu.Lock()
	if h.subs[sessionID] == nil {
		h.subs[sessionID] = map[chan eventRow]struct{}{}
	}
	h.subs[sessionID][ch] = struct{}{}
	h.mu.Unlock()

	cancel := func() {
		h.mu.Lock()
		if set := h.subs[sessionID]; set != nil {
			delete(set, ch)
			if len(set) == 0 {
				delete(h.subs, sessionID)
			}
		}
		h.mu.Unlock()
		close(ch)
	}
	return ch, cancel
}

func (h *sseHub) publish(sessionID string, ev eventRow) {
	h.mu.Lock()
	subs := h.subs[sessionID]
	// snapshot to release lock before sending
	chans := make([]chan eventRow, 0, len(subs))
	for c := range subs {
		chans = append(chans, c)
	}
	h.mu.Unlock()
	for _, c := range chans {
		select {
		case c <- ev:
		default:
			// drop on slow consumer — they will catch up via /baggage
		}
	}
}

func (s *server) handleSSE(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, 500, "no_flusher", "server doesn't support SSE")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(200)

	// Replay existing events first so the client has a complete view.
	if existing, err := s.listEvents(id); err == nil {
		for _, ev := range existing {
			writeSSEEvent(w, flusher, ev)
		}
	}

	ch, cancel := s.hub.subscribe(id)
	defer cancel()

	notifyClosed := r.Context().Done()
	for {
		select {
		case <-notifyClosed:
			return
		case ev, ok := <-ch:
			if !ok {
				return
			}
			writeSSEEvent(w, flusher, ev)
		}
	}
}

func writeSSEEvent(w http.ResponseWriter, f http.Flusher, ev eventRow) {
	b, err := json.Marshal(ev)
	if err != nil {
		return
	}
	// SSE wire format: each event is one "data:" line followed by a blank line.
	_, _ = w.Write([]byte("event: "))
	_, _ = w.Write([]byte(ev.EventType))
	_, _ = w.Write([]byte("\ndata: "))
	_, _ = w.Write(b)
	_, _ = w.Write([]byte("\n\n"))
	f.Flush()
}

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// ductileClient submits domain events into a local Ductile gateway via the
// `scribe-event-relay` plugin. The relay plugin is a tiny passthrough whose
// only job is to emit `{event.type, event.payload}` so Ductile pipelines can
// route it. Ingress doesn't speak Ductile's wire protocol directly — it just
// POSTs JSON and trusts the relay.
type ductileClient struct {
	baseURL  string
	token    string
	plugin   string
	httpClient *http.Client
}

func newDuctileClient(baseURL, token, plugin string) *ductileClient {
	return &ductileClient{
		baseURL: baseURL,
		token:   token,
		plugin:  plugin,
		httpClient: &http.Client{Timeout: 10 * time.Second},
	}
}

func (c *ductileClient) emit(eventType string, payload map[string]any) error {
	if c.baseURL == "" || c.plugin == "" {
		return fmt.Errorf("ductile not configured (baseURL=%q plugin=%q)", c.baseURL, c.plugin)
	}
	body, err := json.Marshal(map[string]any{
		"payload": map[string]any{
			"event_type": eventType,
			"data":       payload,
		},
	})
	if err != nil {
		return err
	}
	url := fmt.Sprintf("%s/plugin/%s/handle", c.baseURL, c.plugin)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("POST %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("ductile %s returned %d: %s", url, resp.StatusCode, string(b))
	}
	return nil
}

// scribe-ingress: HTTP server that owns scribe.db writes for the ingress path,
// serves the PWA, accepts clip uploads, and submits events into Ductile for
// the downstream worker pipeline.
//
// Configuration is all environment variables (see README for the canonical list).

package main

import (
	"context"
	"database/sql"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	_ "modernc.org/sqlite"
)

type config struct {
	port            string
	dbPath          string
	blobsDir        string
	templatesDir    string
	pwaDir          string
	ductileURL      string
	ductileToken    string
	ductileRelayCmd string
	whisperURL      string
	anthropicKey    string
	sessionTimeout  time.Duration
	openTimeout     time.Duration
	clipMaxMillis   int64
}

func loadConfig() config {
	return config{
		port:            getenv("PORT", "8090"),
		dbPath:          getenv("DB_PATH", "./scribe.db"),
		blobsDir:        getenv("BLOBS_DIR", "./blobs"),
		templatesDir:    getenv("TEMPLATES_DIR", "./templates"),
		pwaDir:          getenv("PWA_DIR", "./pwa"),
		ductileURL:      getenv("DUCTILE_URL", "http://127.0.0.1:8082"),
		ductileToken:    os.Getenv("DUCTILE_TOKEN"),
		ductileRelayCmd: getenv("DUCTILE_RELAY_PLUGIN", "scribe-event-relay"),
		whisperURL:      os.Getenv("WHISPER_URL"),
		anthropicKey:    os.Getenv("ANTHROPIC_API_KEY"),
		sessionTimeout:  time.Duration(envInt("SESSION_IDLE_MINUTES", 15)) * time.Minute,
		openTimeout:     time.Duration(envInt("OPEN_IDLE_MINUTES", 5)) * time.Minute,
		clipMaxMillis:   int64(envInt("CLIP_MAX_MILLIS", 12*60*1000)),
	}
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func main() {
	cfg := loadConfig()
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("scribe-ingress starting on :%s (db=%s blobs=%s templates=%s)",
		cfg.port, cfg.dbPath, cfg.blobsDir, cfg.templatesDir)

	if err := os.MkdirAll(cfg.blobsDir, 0o755); err != nil {
		log.Fatalf("mkdir blobs: %v", err)
	}
	// Resolve blobsDir to absolute exactly once at startup. Every blob_path
	// stamped into events + emitted to Ductile must be absolute, otherwise
	// plugins spawned by Ductile (different cwd) can't read the file.
	abs, err := filepath.Abs(cfg.blobsDir)
	if err != nil {
		log.Fatalf("resolve blobs dir: %v", err)
	}
	cfg.blobsDir = abs
	log.Printf("blobs dir: %s", abs)

	db, err := sql.Open("sqlite", cfg.dbPath+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)")
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer db.Close()
	if err := db.Ping(); err != nil {
		log.Fatalf("ping db: %v", err)
	}
	if err := ensureSchemaCurrent(db); err != nil {
		log.Fatalf("schema check failed: %v\n  hint: run ./scripts/migrate.sh", err)
	}

	hub := newSSEHub()
	ductile := newDuctileClient(cfg.ductileURL, cfg.ductileToken, cfg.ductileRelayCmd)

	srv := &server{
		cfg:     cfg,
		db:      db,
		hub:     hub,
		ductile: ductile,
	}

	mux := http.NewServeMux()
	srv.registerRoutes(mux)

	httpSrv := &http.Server{
		Addr:              ":" + cfg.port,
		Handler:           withRecovery(withRequestLog(mux)),
		ReadHeaderTimeout: 10 * time.Second,
	}

	ctx, cancel := context.WithCancel(context.Background())
	go srv.runSweeper(ctx)

	go func() {
		log.Printf("listening on http://localhost:%s", cfg.port)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("serve: %v", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
	log.Print("shutdown signal received")
	cancel()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	_ = httpSrv.Shutdown(shutdownCtx)
	log.Print("clean exit")
}

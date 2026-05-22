# scribea — clinical scribe MVP

A learning prototype that captures a multi-clip clinical consult, transcribes each clip on a remote GPU, and produces a structured SOAP note from a hand-crafted template. **Not for clinical use.**

The full design rationale and scope live in [`docs/specseed.md`](docs/specseed.md). Architectural decisions are recorded in [`docs/adr/`](docs/adr/). This README is the operator's manual.

## Topology

```
┌── Mac (dev) ─────────────────────────────────┐    ┌── Unraid ────────────────────┐
│                                              │    │                              │
│  Browser ── PWA (static, served by ingress)  │    │  faster-whisper :8765        │
│     │                                        │    │  (POST /transcribe-full)     │
│     ▼ HTTP                                   │    │                              │
│  scribe-ingress :8090  ──── writes ────────► scribe.db  +  blobs/<sha256>        │
│     │                                        │    │                              │
│     │ HMAC-signed webhook                    │    │                              │
│     ▼                                        │    │                              │
│  Ductile :8082  ─── routes events ──► plugins/scribe-{transcribe,assemble,...}   │
│                                              │    │      │                       │
│                                              │    │      └── HTTP ──────────────►│
└──────────────────────────────────────────────┘    └──────────────────────────────┘
```

Workers are local Ductile plugins (spawn-per-command). `scribe-transcribe` reaches out to the Unraid GPU whisper container; the other three (`assemble`, `structure`, `format`) are CPU-bound and stay local.

## Layout

```
ingress/         Go HTTP server + SQLite + Ductile API client + SSE
plugins/
  scribe-transcribe/   Python — POSTs clip audio to Unraid whisper
  scribe-assemble/     Python — composes ordered transcript with gap markers
  scribe-structure/    Python — calls Claude API, validates JSON against schema
  scribe-format/       Python — renders structured JSON to markdown
pwa/             Vanilla JS/HTML — IndexedDB outbox, MediaRecorder, SSE live view
templates/
  soap_consult/        v0 template: system_prompt + schema + render.tmpl + few-shot
db/migrations/   SQLite schema migrations (idempotent SQL files)
scripts/         migrate.sh, ductile-config-apply.sh
docs/            specseed.md + ADRs + whisper endpoint spec
blobs/           sha256-addressed audio (gitignored)
```

## Prereqs

- Go 1.22+
- Python 3.11+
- `sqlite3` CLI
- A running local Ductile gateway (Mac instance, `127.0.0.1:8082`)
- The Unraid `faster-whisper` container with the `/transcribe-full` endpoint (see `docs/whisper-transcribe-full-spec.md`)
- `ANTHROPIC_API_KEY` env var (if you want real LLM structuring; otherwise structure-worker stubs)

## First run

```bash
# 1. Apply the database schema
./scripts/migrate.sh

# 2. Register the four plugins + pipelines with the local Ductile gateway
./scripts/ductile-config-apply.sh

# 3. Build and start the ingress (serves PWA + API on :8090)
cd ingress && go build -o ../bin/scribe-ingress ./... && cd ..
PORT=8090 \
DB_PATH=./scribe.db \
BLOBS_DIR=./blobs \
TEMPLATES_DIR=./templates \
DUCTILE_URL=http://127.0.0.1:8082 \
DUCTILE_TOKEN="$DUCTILE_LOCAL_TOKEN" \
WHISPER_URL=http://192.168.20.4:8765 \
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
./bin/scribe-ingress
```

Open `http://localhost:8090/` and you should see the Sessions list.

## Manual 3-clip E2E walkthrough

1. Open `http://localhost:8090/`. Click **New session**, pick `soap_consult`, click **Start session**.
2. On the active screen, click **Start clip**, speak for ~10s, click **Stop clip**. Watch the clip transition `queued → confirmed → transcribed` (live transcript pane updates).
3. Repeat for clips 2 and 3. Try toggling network off, recording a clip, then back on — it should sync.
4. Click **End session**. Wait for the badge to flip to `completed`.
5. The session moves to the **Sessions list** as completed. Open it: three tabs — **baggage** (full event log), **structured** (LLM JSON), **markdown** (rendered note).

## Honest gates (v0 stubs)

- `scribe-transcribe` returns `meta.stt_model="stub"` and a canned transcript if `WHISPER_URL` is unset, unreachable, or the `/transcribe-full` endpoint isn't deployed yet.
- `scribe-structure` returns `meta.model="stub"` and a schema-valid placeholder if `ANTHROPIC_API_KEY` is unset.

Both stamp the truth into baggage — the audit trail never lies about what produced the output.

## What this is not

See [`docs/specseed.md` §2](docs/specseed.md). No PHI handling, no auth, no FHIR/SNOMED, no streaming. Single-user learning prototype.

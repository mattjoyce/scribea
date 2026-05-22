# Wiring scribea into the local Ductile gateway

The scribe plugins live in this repo at `plugins/`. To make the local Ductile gateway (`127.0.0.1:8082`) see them, you add one path to `config.yaml`, paste five plugin configs into `plugins.yaml`, and paste two pipelines into `pipelines.yaml`. Then `config check && config lock && system reload`.

> One-time-only operator action. Re-running the apply step on top of existing values is fine — `config check` will flag anything ambiguous.

## 1. config.yaml — add `plugin_roots`

In `~/.config/ductile/config.yaml`, extend `plugin_roots:` to include this repo's `plugins/` directory:

```yaml
plugin_roots:
  - "/Users/mattjoyce/Projects/ductile/plugins"
  - "/Users/mattjoyce/Projects/ductile-plugins"
  - "/Volumes/Projects/scribea/plugins"   # ← add this line
```

In the same file, add the Anthropic secret to `environment_vars.include` (the scribe-structure plugin reads `ANTHROPIC_API_KEY`):

```yaml
environment_vars:
  include:
    - /Users/mattjoyce/.config/secrets/discord/.env
    - /Users/mattjoyce/.config/secrets/beads/.env
    - /Users/mattjoyce/.config/secrets/jina/.env
    - /Users/mattjoyce/.config/secrets/huggingface/.env
    - /Users/mattjoyce/.config/secrets/firecrawl/.env
    - /Users/mattjoyce/.config/secrets/anthropic/.env   # ← add this line
```

## 2. plugins.yaml — add the five scribe plugins

Append to `~/.config/ductile/plugins.yaml`:

```yaml
  scribe-event-relay:
    enabled: true

  scribe-transcribe:
    enabled: true
    config:
      ingress_url: http://127.0.0.1:8090
      whisper_url: http://192.168.20.4:8765
      stub_mode: "false"
      request_timeout_seconds: 120
    retry:
      max_attempts: 3
      backoff_base: 5s
    timeouts:
      poll: 130s
      health: 5s

  scribe-assemble:
    enabled: true
    config:
      ingress_url: http://127.0.0.1:8090

  scribe-structure:
    enabled: true
    config:
      ingress_url: http://127.0.0.1:8090
      templates_dir: /Volumes/Projects/scribea/templates
      anthropic_api_key: ${ANTHROPIC_API_KEY}
      claude_model: claude-sonnet-4-6
      request_timeout_seconds: 90
      stub_mode: "false"
    retry:
      max_attempts: 2
      backoff_base: 10s

  scribe-format:
    enabled: true
    config:
      ingress_url: http://127.0.0.1:8090
      templates_dir: /Volumes/Projects/scribea/templates
```

Notes:
- `${ANTHROPIC_API_KEY}` is interpolated by Ductile from the file added to `environment_vars.include` in step 1.
- `stub_mode: "false"` is the string `"false"` because Ductile config values are strings; the plugin parses it. Set to `"true"` to force stubs even when keys are present.
- `whisper_url` assumes the Unraid `/transcribe-full` endpoint (see `docs/whisper-transcribe-full-spec.md`). Leave the URL blank or unset to force stub-mode transcription.

## 3. pipelines.yaml — add the two scribe pipelines

Append to `~/.config/ductile/pipelines.yaml`:

```yaml
  # Per-clip pipeline. Triggered by scribe-event-relay re-emitting
  # scribe.clip.received.v1 from the ingress side. Single step; the live SSE
  # picks up the transcribed event via the audit path.
  - name: scribe-clip-pipeline
    on: scribe.clip.received.v1
    steps:
      - id: transcribe
        uses: scribe-transcribe

  # Session-close pipeline. Three serial steps: assemble → structure → format.
  # Each step's emitted payload feeds the next step.
  - name: scribe-session-pipeline
    on: scribe.session.close_requested.v1
    steps:
      - id: assemble
        uses: scribe-assemble
      - id: structure
        uses: scribe-structure
      - id: format
        uses: scribe-format
```

## 4. Apply

```bash
cd /Volumes/Projects/scribea
ductile config check          # validates syntax + integrity
ductile config lock           # authorize the new state
ductile system reload         # hot-reload, or restart the launchd service
```

After reload, verify:

```bash
# Should list all five scribe plugins:
ductile plugin list --json | grep -i scribe

# Health checks (token comes from env or ~/.config/ductile/api.yaml):
curl -s -X POST http://127.0.0.1:8082/plugin/scribe-event-relay/health \
  -H "Authorization: Bearer $DUCTILE_LOCAL_TOKEN"
curl -s -X POST http://127.0.0.1:8082/plugin/scribe-transcribe/health \
  -H "Authorization: Bearer $DUCTILE_LOCAL_TOKEN"
```

## What this does NOT do

- Doesn't restart the launchd service automatically. `system reload` is a SIGHUP; for changes to `plugin_roots` you may need a full restart (`launchctl bootout … && launchctl bootstrap …`). When in doubt, restart.
- Doesn't touch the Unraid Ductile (`192.168.20.4:8888`) — scribea v0 runs entirely against the Mac instance.
- Doesn't configure webhooks. Ingress talks to Ductile via `/plugin/scribe-event-relay/handle` directly, no HMAC needed because both processes are localhost.

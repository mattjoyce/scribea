# scribea voice renderer

A trimmed copy of `~/.claude/VoiceServer/server.ts` (PAI's notification
server) adapted to render TTS audio to files for the test corpus, with
voice configuration controlled by scribea rather than by PAI.

## Why a copy

PAI's voice server (`localhost:8888`) does several things — notifications,
emotion presets, AppleScript banners, rate limiting — that the corpus
doesn't need, and reads voice configuration from `~/.claude/settings.json`,
which is the wrong place to tune VMO vs Registrar voices for clinical
test audio. This copy:

- Exposes only `POST /synthesize` and `GET /health`
- Reads persona → OpenAI voice mappings from local `voices.json`
- Reads medical-term pronunciation overrides from local `pronunciations.json`
- Defaults to port **8889** so it can coexist with PAI's 8888

Voice changes here do not touch PAI; PAI changes do not touch corpus
rendering.

## Files

| File                  | Purpose                                                                  |
|-----------------------|--------------------------------------------------------------------------|
| `server.ts`           | Bun HTTP server. POST /synthesize → mp3 bytes. GET /health → status.     |
| `voices.json`         | Persona handles (`VMO_A`, `REG_B`, …) → OpenAI voice + speed + instructions. |
| `pronunciations.json` | Word-boundary replacements applied before TTS (medical acronyms etc).    |
| `render.py`           | CLI wrapper for corpus build scripts: text → file.                       |

## Prerequisites

- `bun` on PATH (PAI already uses it)
- `OPENAI_API_KEY` in `~/.env` (same path PAI reads)
- `ffmpeg` on PATH if you intend to use `render.py --convert-wav`

## Running the server

```bash
cd corpus/voice-renderer
bun run server.ts           # logs go to stderr; ^C to stop
# or in the background:
nohup bun run server.ts > /tmp/scribea-voice-renderer.log 2>&1 &
```

Override port if needed: `PORT=8901 bun run server.ts`.

## Health check

```bash
curl -s http://localhost:8889/health | jq .
# {
#   "status": "healthy",
#   "port": 8889,
#   "api_key_configured": true,
#   "personas": ["VMO_A","VMO_B","REG_A","REG_B"],
#   "default_persona": "VMO_A",
#   "pronunciation_rules": 10
# }
```

## Rendering one clip

```bash
./render.py \
  --text "Mr Patel admitted overnight with acute abdominal pain radiating to the back." \
  --persona VMO_A \
  --out /tmp/clip_1.mp3

# write a 16 kHz mono WAV (matches scribe-audio-preprocess's output target):
./render.py --text "..." --persona REG_A --out /tmp/clip_1.wav --convert-wav
```

`--persona` is optional — omit to use `voices.default`. Unknown persona
names fall back to OpenAI's `alloy` with a stderr warning.

## Changing a voice

Edit `voices.json`:

- `voice_id`: any OpenAI TTS voice name (`alloy`, `ash`, `ballad`,
  `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`).
- `speed`: 0.25-4.0. Use 0.9-0.95 for VMO (slower, thoughtful),
  1.0-1.1 for Registrar (faster, more numerical).
- `instructions` (gpt-4o-mini-tts only): a short prompt steering tone.
- `description`: free-text note for humans; ignored by the server.

Restart the server after editing.

## Adding a pronunciation rule

`pronunciations.json` carries `{term, phonetic, note}` triples. Word
boundaries are enforced so `MRN` matches but doesn't replace inside
`MRN12345`. Add medical acronyms or names whose default TTS
pronunciation is wrong. Restart the server after editing.

## Relationship to PAI's VoiceServer

This is a fork-by-copy. It does NOT track upstream automatically. If
PAI's `/synthesize` endpoint changes shape, this copy may need a manual
update — but for v0 the surface is small and stable. The deliberate
non-features (no /notify, no emotion presets, no AppleScript) keep the
fork narrow and the diff easy to audit.

## Integration with the corpus build

Per `docs/scribe-test-corpus.md` §11:

```
dialog.md (with bracket annotations)
    │ strip annotations
    ▼
script.txt (one clip per paragraph)
    │ ./render.py per paragraph
    ▼
clip_N_raw.mp3
    │ ffmpeg mix (downmix background + normalize)
    ▼
clip_N.wav     (16 kHz mono s16le — matches scribe-audio-preprocess output)
```

`clip_N.wav` lives outside the repo at `$SCRIBEA_CORPUS_AUDIO_DIR` per
the .gitignore policy (`corpus/**/*.mp3` and `*.wav` are blocked).

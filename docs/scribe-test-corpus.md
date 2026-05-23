# Test Corpus — scribea

**Status:** Draft v0
**Date:** May 2026
**Purpose:** Specification for the fixture corpus that drives the scribea
pipeline end-to-end against role-styled ICU dictation.

Read alongside [`specseed.md`](specseed.md), the [domain glossary](../CONTEXT.md),
and [`adr/0006-case-as-domain-entity.md`](adr/0006-case-as-domain-entity.md).

---

## 1. What this is

A collection of mock ICU clinical cases used to drive the scribea pipeline
end-to-end while it is being built. Each case is a *recipe*:

- A patient's **EMR Backstory** (demographics + a free-text "previous notes"
  block) — see [`CONTEXT.md`](../CONTEXT.md).
- One **Session** of dictated audio by one clinician in one **Role**.
- The dictation scripts that drive a TTS engine to produce the audio.
- The configuration that drives audio rendering (voice, background ambience,
  signal-to-noise level, seed for reproducibility).

The corpus exists so the pipeline always has realistic-shaped inputs to chew
on. It is **not** yet a regression net — that needs goldens, which are §13
deferred work.

## 2. What this is NOT (v0)

- **No real PHI, no real patients, no real consent.** Every case is faux —
  invented demographics, invented clinical content, designed to be
  plausibly *shaped*, not actually clinical.
- **No goldens for v0.** The output of the structure-worker is not yet
  pinned against expected values. Adding goldens is §13.
- **No multi-session cases for POC.** Each case has exactly one session. The
  domain model supports multi-session per case (ADR-0006), but POC keeps it
  1:1 to halve the moving parts.
- **No real recordings.** Every clip is TTS-rendered via the PAI voice
  server, then mixed with a background ambience track. Self-recorded
  fixtures are §13.
- **No demo / vendor-quality dataset.** This is a developer fixture set,
  not marketing material. A separate "demo fixtures" set would be the
  right vehicle for that, and is not part of this corpus.

## 3. Design principles

These derive from specseed §3 and the project's two intellectual anchors
(Hickey, Armstrong). Same principles, applied to a fixture set.

- **The corpus stores recipes, not artifacts.** What lives in git:
  `case.yaml` (the recipe), the dictation scripts (the input text), the
  EMR Backstory content. What does *not* live in git: the rendered mp3
  clips and the background ambience pool — these are derived or
  local-cache. Symmetry with how specseed treats `blobs/`.
- **A case is a value.** One directory, one manifest, one identity. Other
  values (clips, backgrounds, scripts) are referenced from the manifest by
  path. The corpus is a tree of plain data.
- **Determinism over polish.** Audio is TTS-rendered, with seeded-random
  background slicing. Re-renders produce the same audio (modulo OpenAI TTS
  drift below the voice-server layer). Honest fixtures beat realistic
  fixtures.
- **Role styles are recognisable in the audio, not just in the
  structuring.** The VMO and Registrar speak differently (§6). The corpus
  contribution is exercising the pipeline against materially different
  dictation shapes.

## 4. Corpus layout

The corpus lives at the repo root under `test-corpus/`:

```
test-corpus/
  README.md                         # short pointer to this doc
  backgrounds/                      # local-only — ICU ambience pool
    icu_ward_quiet.mp3              # gitignored
    icu_monitors_active.mp3         # gitignored
    icu_alarm_intermittent.mp3      # gitignored
    ATTRIBUTION.md                  # local — sources/licences for the above
  cases/
    01_cap_septic_shock_vmo/
      case.yaml                     # the recipe
      scripts/
        01.txt
        02.txt
        03.txt
      clips/                        # gitignored — rendered audio
        01.mp3
        02.mp3
        03.mp3
    02_cap_septic_shock_registrar/
      case.yaml
      scripts/…
      clips/…                       # gitignored
```

- One directory per case.
- Case ids: `NN_<short_slug>_<role>`. The slug captures the clinical
  scenario for human readability; the trailing `_<role>` keeps VMO and
  Registrar variants of the same scenario distinct.
- To add a case: `cp -r 01_… 03_…` then edit `case.yaml` and scripts.

## 5. Case manifest (`case.yaml`)

The single source of truth for a case. The renderer reads it; the future
harness reads it. Everything else is derivable.

```yaml
case_id: 01_cap_septic_shock_vmo

demographics:
  display_name: Mrs Wilson           # always faux
  age: 68
  sex: F
  icu_day: 2
  admission_reason: Community-acquired pneumonia, septic shock

previous_notes: |
  Admitted overnight via ED. CAP with septic shock. Intubated for hypoxic
  respiratory failure on arrival to ICU. Noradrenaline started at
  0.05 mcg/kg/min, escalated to 0.15. Cefepime + azithromycin commenced.
  CXR with bilateral basal consolidation. Blood and tracheal cultures
  pending. ICU day 2.

session:
  role: vmo                          # vmo | registrar
  dictation_style: systems           # systems | checklist (see §6)
  clinician: Dr A.Smith              # informational; not used by pipeline
  audio_render:
    voice_id: ballad                 # PAI voice server voice id
    background_pool: icu_ambient     # subdir of test-corpus/backgrounds/
    snr_db: -18                      # voice this many dB above background
    seed: cap_septic_shock_vmo       # base seed for "random" bg slicing
  clips:
    - audio: clips/01.mp3
      script: scripts/01.txt
    - audio: clips/02.mp3
      script: scripts/02.txt
    - audio: clips/03.mp3
      script: scripts/03.txt
      background_override:           # optional, this clip happens during an alarm
        asset: icu_alarm_intermittent
        snr_db: -10                  # alarm closer to voice
```

Schema notes:

- `case_id` matches the directory name. The renderer asserts this.
- `demographics`: required keys `display_name`, `age`, `sex`,
  `admission_reason`. `icu_day` optional. Free to add others, but the
  pipeline only reads what the structure-worker template references.
- `previous_notes`: free-text markdown. v0 is static authored content.
  Post-POC becomes derived from prior sessions in the case (specseed §3.1
  lens — values computed from event log).
- `session.role` must map to a template id: `vmo` → `icu_vmo`,
  `registrar` → `icu_registrar`. The renderer asserts the mapping.
- `session.dictation_style` documents the intended script shape. The
  pipeline does not branch on it; it lives here for human readers and
  future tooling.
- `session.audio_render.seed` is the corpus-author-chosen base seed. The
  renderer derives a per-clip seed as `f(seed, clip_index)` so per-clip
  background slices differ but the whole case is reproducible.
- `clips[]` is ordered. Position = dictation order, mapped 1:1 to the
  ingress's `clips.seq` for the session.
- `background_override` per clip is optional and overrides the
  session-level `background_pool` / `snr_db` for that one clip.

## 6. Roles and dictation styles

Two roles for POC: **VMO** and **Registrar**. Role determines template
(one-to-one mapping per [§11.3](#113-templates-per-role)) and — by v0
corpus convention — the **Dictation Style** of the script.

> **The role↔style pinning is a v0 corpus simplification, not a clinical
> claim.** In real ICU practice clinicians use both styles, often
> alternating between them within a single shift. The source of truth for
> the two styles is
> [`icu-notes-two-approaches.md`](icu-notes-two-approaches.md); read it
> before authoring scripts. v0 pins one style per role so the corpus
> produces two recognisably-different structured outputs to test the
> pipeline against.

### 6.1 VMO — Systems Approach

An organ-system review. The clinician walks the body system-by-system,
each section assembling that system's current story (status, drivers,
plan, thresholds that would change the plan). Hierarchical and integrative;
the plan falls out of the model, not the list.

Canonical system list (per
[`icu-notes-two-approaches.md`](icu-notes-two-approaches.md)):

- **Neuro** — sedation, RASS/GCS, pain, plan to lighten / extubate
- **CVS** — vasopressors, MAP, lactate, rhythm, trajectory
- **Resp** — vent mode + settings, FiO₂, PEEP, P/F, secretions, SBT plan
- **Renal** — UO, creatinine, urea, K⁺, RRT triggers
- **GI** — feeding, tolerance, bowels, abdomen
- **Heme-ID** — Hb, antibiotics, cultures, day of antibiotics, source control
- **Endo** — glucose, VRII, electrolytes
- **Skin** — pressure areas, wounds, turning regimen
- **Lines** — CVC / art line / IDC days, review/replace
- **Plan** — integrative — dominant problem, next 24 h, watch-items

A VMO script clip is one to three systems in dictation tone:

```
Neuro — sedated on propofol, RASS minus 2, GCS not assessable.
Plan to lighten in the morning with a view to SBT.

CVS — noradrenaline weaning, 0.08 mcg/kg/min, down from 0.25 on day 1.
MAP in the 70s, lactate 1.8, peak was 4.2. Continue weaning, off by
tomorrow if the trajectory holds.

Resp — AC/VC, FiO₂ 0.4, PEEP 8, P/F ratio 240. Right basal consolidation
improving on chest X-ray. SBT tomorrow if off pressors.
```

Multiple clips per session = natural breaks between systems or a pause
for thought.

### 6.2 Registrar — Checklist Approach (FAST HUGS BID)

A fixed-list safety pass. Each item is independent; the clinician confirms
or addresses each one for every patient, every shift. The note can be
complete without the clinician integrating across items — that's the
checklist's strength (omission protection) and its limit (no causal story).

The v0 canonical checklist is **FAST HUGS BID** (per
[`icu-notes-two-approaches.md`](icu-notes-two-approaches.md)):

- **F**eeding — NG / oral / TPN; rate; tolerance
- **A**nalgesia — agent + infusion rate
- **S**edation — agent + RASS / target
- **T**hromboprophylaxis — pharmacological / mechanical; held + reason if so
- **H**ead of bed — angle (typically 30°+)
- **U**lcer prophylaxis — agent
- **G**lucose — control method, BSL range
- **S**BT — done / not today / criteria
- **B**owels — last opened, aperient charted
- **I**ndwelling devices — CVC / art line / IDC days, infection signs
- **D**e-escalation — antibiotics, lines, sedation

A Registrar script clip is one or a few checklist items, in handover tone:

```
Feeding — NG feeds, 30 mils per hour, tolerating.
Analgesia — fentanyl infusion at 50 mcg per hour.
Sedation — propofol, RASS minus 2.
Thromboprophylaxis — held today, AKI and bleeding risk reviewed.

Head of bed at 30 degrees.
Ulcer prophylaxis — pantoprazole.
Glucose — variable-rate insulin, BSLs running 7 to 9.

SBT not today.
Bowels — not opened, aperient charted.
Indwelling devices — CVC, art line, IDC, all day 3, no signs of infection.
De-escalation pending micro.
```

### 6.3 Why these two together

The two styles produce *recognisably different* assembled transcripts and
*therefore* different structured outputs:

- The Systems output captures causal reasoning, trajectory, thresholds —
  the *"why"* and the *"what's next"*.
- The Checklist output captures coverage and confirmation — the
  *"have I addressed this?"*.

Each protects against a failure mode the other can hide:

- Systems: fragmentation protection — items aren't treated in isolation.
- Checklist: omission protection — routine safety items aren't dropped.

For the pipeline, that means the structure-worker (with role-specific
templates per §11.3) has to produce schemas that genuinely fit each style.
The corpus's role↔style pinning is what gives us two distinct inputs to
exercise that.

## 7. EMR Backstory

The Backstory is the minimal context attached to a session. For each
session, the harness passes a value of this shape into the
structure-worker:

```
EMR Backstory
=============
Demographics: <display_name>, <age><sex>, ICU day <N>
Admission reason: <free-text>

Previous notes:
<free-text block>
```

In v0 this is **static authored content** drawn directly from `case.yaml`.
Post-POC the previous-notes block becomes derived from prior completed
sessions in the same case (specseed §3.1 lens — values computed from the
event log).

The exact prompt-position the structure-worker uses (system-prompt prefix
vs. user-prompt prefix) is a structure-worker concern, not a corpus
concern. The corpus contract is: *produce this Backstory string per
session*.

## 8. Audio rendering pipeline

Two stages: synthesize the voice, then mix with background ambience.

```
script.txt
   │
   ▼
POST localhost:8888/synthesize     ← PAI voice server (gpt-4o-mini-tts)
   │
   ▼
voice mp3 (clean speech, no background)
   │
   ▼
mix with a slice of a background asset
   ├─ asset chosen from the pool by seed (deterministic)
   ├─ offset into the asset chosen by seed (deterministic)
   └─ voice level set `snr_db` above the background
   │
   ▼
clips/NN.mp3   ← what whisper sees
```

### 8.1 Voice synthesis

The PAI voice server is the TTS engine. Endpoint:
`http://localhost:8888/synthesize` (new — see [§11.1](#111-pai-voice-server-synthesize-endpoint)).
The server fronts OpenAI's `gpt-4o-mini-tts`; voice character, accent,
and tone come from the server's `settings.json` config.

The renderer passes only `voice_id` (and the script text). Different
voice ids give different roles a recognisably different vocal character
without changing the script.

Default voice mapping (overridable per case via
`session.audio_render.voice_id`):

| Role        | Voice id  | Note                                       |
|-------------|-----------|--------------------------------------------|
| `vmo`       | `ballad`  | British-leaning consultant timbre          |
| `registrar` | `onyx`    | Different timbre for differentiation       |

### 8.2 Background mixing

The renderer picks a background asset deterministically from the pool,
then slices a segment of the same length as the voice clip at a
deterministic offset into the asset:

```python
def render_clip(clip, session, case, backgrounds_dir, voice_server_url):
    voice_bytes = httpx.post(
        f"{voice_server_url}/synthesize",
        json={
            "message": clip.script_text,
            "voice_id": session.audio_render.voice_id,
        },
    ).content
    voice = AudioSegment.from_file(BytesIO(voice_bytes), format="mp3")

    asset_id, snr_db = resolve_background(clip, session)
    bg = AudioSegment.from_file(backgrounds_dir / f"{asset_id}.mp3")

    seed = derive_seed(session.audio_render.seed, clip.index)
    rng = Random(seed)
    if len(bg) > len(voice):
        offset = rng.randint(0, len(bg) - len(voice))
    else:
        offset = 0
    bg_slice = bg[offset : offset + len(voice)]

    mixed = voice.overlay(bg_slice + snr_db)        # snr_db is negative
    mixed.export(case.dir / clip.audio_path, format="mp3")
```

Notes:

- `pydub` is a fine choice; ffmpeg-subprocess is also fine. Either way
  the renderer uses `pathlib.Path` (per project Python coding standards).
- `snr_db` is *negative* — `-18` means the background sits 18 dB below
  the voice in the mix.
- The seed-derivation function is part of the corpus contract: different
  implementations of the renderer must produce the same offsets for the
  same case. Proposed: `hash(case_seed + ":" + str(clip_index))` mod
  `(len(bg) - len(voice))`. The renderer pins this so it is stable.

### 8.3 Background asset pool (POC)

Local-only, gitignored. The corpus doc names the pool by id. The renderer
fails clearly with `BackgroundMissing: <id>` if any expected asset is
absent.

POC pool to source as ~30 s CC-licensed clips and drop in
`test-corpus/backgrounds/`:

| Asset id                      | Character                                          |
|-------------------------------|----------------------------------------------------|
| `icu_ward_quiet.mp3`          | low room tone, occasional distant beep             |
| `icu_monitors_active.mp3`     | regular monitor beeps, hum, fans                   |
| `icu_alarm_intermittent.mp3`  | as monitors_active + occasional alarm chime        |

Sourcing: freesound.org under CC0 / CC-BY is fine. One asset per id.
Attribution lives in `test-corpus/backgrounds/ATTRIBUTION.md` (local; the
attribution is for local audio that isn't distributed by this repo).

Post-POC upgrade: a `test-corpus/backgrounds.yaml` manifest with
`{id, source_url, sha256, license, attribution}` per asset, plus a fetch
script — this makes the corpus reproducible across machines without each
contributor sourcing their own.

## 9. Repo policy

What lives in git and what doesn't:

| Path                                          | Git? | Reason                                     |
|-----------------------------------------------|------|--------------------------------------------|
| `test-corpus/README.md`                       | yes  | pointer to this doc                        |
| `test-corpus/cases/*/case.yaml`               | yes  | recipe                                     |
| `test-corpus/cases/*/scripts/*.txt`           | yes  | input value                                |
| `test-corpus/cases/*/clips/*.mp3`             | no   | derived (script × voice × bg)              |
| `test-corpus/backgrounds/*.mp3`               | no   | local asset cache                          |
| `test-corpus/backgrounds/ATTRIBUTION.md`      | no   | local (attributes local audio)             |
| `test-corpus/.cache/`                         | no   | renderer scratch / intermediates           |

Gitignore additions, to land alongside the first corpus commit:

```
## Test corpus (recipes in git, audio out)
test-corpus/**/clips/*.mp3
test-corpus/backgrounds/*.mp3
test-corpus/backgrounds/ATTRIBUTION.md
test-corpus/.cache/
```

## 10. Renderer tool

A small Python script per the project's Python coding standards (click,
pathlib, ruff, httpx). Lives at `scripts/corpus-render.py`.

Shell:

```bash
# Render all clips for one case (idempotent — skips up-to-date clips)
python scripts/corpus-render.py case 01_cap_septic_shock_vmo

# Render the whole corpus
python scripts/corpus-render.py all

# Force re-render (skip up-to-date check)
python scripts/corpus-render.py all --force
```

Behaviour:

1. Walks `test-corpus/cases/<id>/case.yaml`.
2. For each clip: if `clips/NN.mp3` exists and is newer than its
   `script.txt` AND newer than its resolved background asset, skip.
   Otherwise:
3. POST script text to `localhost:8888/synthesize`, get the voice mp3.
4. Load background asset, slice at the seeded offset.
5. Mix voice + bg slice at `snr_db`.
6. Write to `clips/NN.mp3`.

Idempotence matters because the corpus is meant to be re-renderable
freely. Without skip-when-up-to-date, every full render bills OpenAI for
TTS that hasn't changed.

## 11. Architectural prerequisites

The corpus depends on a few system pieces. These are *not* in scope for
this doc, but the doc commits to the contracts.

### 11.1 PAI voice server `/synthesize` endpoint

New endpoint on `~/.claude/VoiceServer/server.ts`:

```
POST /synthesize
Content-Type: application/json
Body: { "message": "<text>",
        "voice_id": "<id>",            # optional
        "voice_options": { … } }       # optional
Response: 200 OK
          Content-Type: audio/mpeg
          body: mp3 bytes
```

Same request shape as `/notify`, same internal helpers
(`resolveVoiceOptions`, `synthesizeSpeech`). The new endpoint returns the
mp3 bytes instead of playing them via `afplay`. Existing endpoints
(`/notify`, `/notify/personality`, `/pai`) unchanged.

### 11.2 Case linkage on sessions

Per [`adr/0006-case-as-domain-entity.md`](adr/0006-case-as-domain-entity.md):
a nullable `case_id` column on `sessions` (a string reference label, no FK)
and one new event type `scribe.case.context_attached.v1` stamped into the
events log when the caller attaches EMR context at session-create time.

The corpus loader is the *simulated EMR feed* — it reads `case.yaml`,
extracts demographics + previous_notes, and POSTs them inline as part of
the session-create request:

```http
POST /sessions
{
  "template_id":    "icu_vmo",
  "case_id":        "01_cap_septic_shock_vmo",
  "demographics":   { "display_name": "Mrs Wilson", "age": 68, "sex": "F",
                      "icu_day": 2, "admission_reason": "..." },
  "previous_notes": "Admitted overnight via ED…"
}
```

Ingress validates the template, generates a session id, persists the
session (with the `case_id` reference recorded), emits
`scribe.session.created.v1`, and — when context is provided — emits
`scribe.case.context_attached.v1` with the demographics and previous notes
in its `data` payload. That event is the audit-honest snapshot of what the
LLM will see.

There is no separate "create case" step and no `cases` table in `scribe.db`.
Case content is corpus-resident (or future-EMR-resident); the event log
captures the moment of attachment forever.

### 11.3 Templates per role

Two new templates: `templates/icu_vmo/` and `templates/icu_registrar/`.
Each is a full template directory per specseed §4.3 (`template.json`,
`system_prompt.md`, `schema.json`, `render.tmpl`, `few_shot/`). The
structure-worker reads the right template per the session's `template_id`;
no spec change needed beyond authoring the new template directories.

## 12. Glossary and decisions

- Domain language: [`../CONTEXT.md`](../CONTEXT.md)
- ADR-0006 (case as domain entity): [`adr/0006-case-as-domain-entity.md`](adr/0006-case-as-domain-entity.md)

## 13. Deferred (not in v0)

Listed so they aren't lost.

- **Goldens.** No expected-output pinning. When templates and prompts
  stabilise enough that "correct" is judgeable, add an `expected/`
  directory per case with hybrid comparison (byte-exact on
  assemble-worker and format-worker output; predicate-based on the
  structure-worker output).
- **Multi-session per case.** The domain model supports it (ADR-0006);
  the corpus shape will then become `sessions[]` not `session`, and the
  EMR Backstory `previous_notes` becomes derived from prior sessions'
  completed notes.
- **Self-recorded clips.** A `provenance: recorded` clip type that
  bypasses the renderer and uses an mp3 the corpus author drops in.
- **Manifest-fetch backgrounds.** `backgrounds.yaml` + sha256 + fetch
  script, making the corpus reproducible across machines without each
  contributor sourcing their own audio.
- **Failure-injection cases.** Cases that deliberately exercise
  gap-clip handling, schema-validation retry, late-clip-410. Land
  alongside goldens.
- **Automated harness.** No runner yet. Manual drive-through via the
  ingress PWA is the current "running the corpus". A pytest-based
  harness lands with goldens.

---

*End of scribe-test-corpus.md.*

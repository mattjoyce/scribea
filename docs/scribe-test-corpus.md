# scribe-test-corpus — synthetic ICU spoken-audio dataset

**Status:** Draft v0
**Parent:** [`specseed.md`](./specseed.md), [`scribe-ner-redact.md`](./scribe-ner-redact.md)
**Scope:** Spec for a small, hand-authored, ground-truthed audio corpus that drives NER, PII, and end-to-end scribe testing without depending on real patient recordings.

---

## 1. Why

Two analysers are landing in parallel: a real `scribe-clinical-ner` extractor (replacing the v0 NOP) and a separate `pii-detector` plugin. Both need test inputs where the right answers are *known by construction*, not inferred. Today we don't have that:

- macOS `say` clips are short, too clean, single-monotone, vocabulary-thin, contain no PHI, and produce neither realistic clinical phrasing nor multi-round dynamics.
- Real patient recordings are off-limits without an ethics framework that doesn't exist yet.
- Without a corpus with ground truth, **every quality claim about NER or PII detection becomes anecdotal.** Recall and precision aren't computable. Regressions aren't detectable. Cross-model comparisons (GLiNER vs scispacy vs regex) become arguments instead of measurements.

A small, hand-authored, deliberately-seeded corpus closes that gap. It is the testing substrate for:

- **NER quality** — per-type recall/precision against ground-truth entity spans
- **PII coverage** — every uttered PHI element is in the answer key, so the detector can be scored
- **End-to-end regression** — replay the corpus through scribea after any plugin change; diff the structured output against a golden file
- **Speaker-style robustness** — VMO-style summary speech vs Registrar-style procedural speech, on the same patient
- **Acoustic robustness** — voice variation + ICU background noise that real consults will have
- **Update semantics** — round-to-round changes (started a drug, escalated a dose, changed the plan) test whether the system handles clinical narrative continuity

The corpus is small on purpose. The goal is *enough variety to detect breakage*, not enough data to train anything.

---

## 2. What

A directory tree of **episodes**. One episode = one fabricated patient + one ICU admission. Each episode contains:

| Artifact            | Form     | Purpose                                                                  |
|---------------------|----------|--------------------------------------------------------------------------|
| Intake EMR          | JSON     | Base truth — who the patient is, why they're in ICU, baseline obs/meds  |
| 4 round dialogs     | Text     | Hand-authored speech scripts with inline ground-truth spans              |
| 4 round audio sets  | WAV      | Rendered audio per clip (2-5 clips per round, 5-15s each, 16 kHz mono)  |
| 4 ground-truth sidecars | JSON | Entity + PHI spans per clip, machine-readable, for scoring              |

Round structure per episode (fixed for v0):

1. **VMO morning round** — assessment + decisions, builds on intake
2. **Registrar mid-day round** — actions taken, fresh observations
3. **VMO evening round** — response to interventions, plan updates
4. **Registrar overnight handover** — terminal-state summary, what's pending

The intake document is *not spoken* — it's the simulated EMR record the rounds reference. The four rounds *are* spoken, each as an independent scribea session.

**v0 targets: 3 episodes, ~3 minutes total audio.** Enough to exercise every NER type and every PII type at least twice across the corpus.

---

## 3. Episode + round model

```
episode = {
  intake_emr,                    # one-shot at admission
  round_1_vmo_morning,           # 2-5 spoken clips
  round_2_registrar_midday,      # 2-5 spoken clips
  round_3_vmo_evening,           # 2-5 spoken clips
  round_4_registrar_overnight,   # 2-5 spoken clips
}
```

Rounds are causally linked: round 2 references decisions from round 1 ("started the noradrenaline as you suggested"); round 3 references observations from round 2 ("good response to the bolus"); round 4 references the whole day. This continuity is the realistic-narrative property that single-clip corpora can't test.

Each round is an **independent scribea session** at runtime — the corpus does not (and should not) test whether scribea links sessions to a longitudinal patient record. Continuity lives in the *text*, not in scribea's plumbing.

---

## 4. Personas

Two personas, deliberately contrasting in vocabulary, pace, and content type. Same clinical scenario routed through both gives the system more surface area than either alone.

| Field             | VMO (consultant)                                              | Registrar / Intensivist                                              |
|-------------------|---------------------------------------------------------------|----------------------------------------------------------------------|
| Role              | Senior decision-maker; sets the plan                          | Frontline; gathers data, executes plan, reports back                 |
| Typical speech    | Higher-level summary, judgement, intent                       | Concrete numbers, observations, actions taken                        |
| Vocabulary slant  | Assessments (`CONDITION`), plans (`PROCEDURE`), drugs by name | Dosages (`DOSAGE`), values, body parts (`BODY_PART`), specifics      |
| Pace              | Slower, with pauses; thinks aloud                             | Faster, more numerical, fewer fillers                                |
| Example utterance | "I'm not happy with the lactate trend — let's escalate."      | "Lactate's come down from 5.8 to 3.9, noradrenaline now 12 mcg/min." |

The personas are *named* but not real people. Suggested anonymous handles for the corpus: `VMO_A`, `VMO_B`, `REG_A`, `REG_B`. Use 2-3 of each across episodes so voice-randomisation has variety.

---

## 5. Per-clip authoring rules

Each clip MUST satisfy:

- **Duration 5-15 seconds** when rendered. Authoring guide: ~12-35 words for the VMO pace, ~18-45 words for the Registrar pace.
- **Single speaker** for the whole clip. (Multi-speaker clips break the per-clip transcribe + assemble contract; speaker-change happens between clips.)
- **At least one clinical entity** drawn from the §1.6 vocabulary in `scribe-ner-redact.md` (MEDICATION, DOSAGE, CONDITION, PROCEDURE, BODY_PART).
- **At least one PHI element somewhere in the round.** Distribute across clips — don't pile them into clip 1. PHI types per spec §1.6: PERSON, DATE, MRN, ADDRESS, PHONE.
- **Realistic clinical phrasing** — use the Australian English register the system is targeting; spell out numbers the way clinicians say them ("forty milligrams" not "40 mg" in the spoken script; the transcript will have the digits).

Each round MUST satisfy:

- **2 to 5 clips total** — author chooses based on what the narrative needs.
- **Round contains at least 4 distinct entity types** across its clips (mix clinical + PHI).
- **At least one new PHI element** not already uttered earlier in the episode — each round introduces something new for the detector to find.
- **References at least one element from a prior round** when the round is 2, 3, or 4 — this is the continuity property.

---

## 6. Ground truth schema

Per round, a JSON sidecar. Hand-authored alongside the dialog (NOT extracted from audio after the fact — that defeats the purpose).

```json
{
  "episode_id": "ep_001",
  "round_id": "round_2_registrar_midday",
  "persona": "REG_A",
  "clips": [
    {
      "seq": 1,
      "duration_target_s": 11,
      "transcript_canonical": "Lactate's come down from 5.8 to 3.9. Noradrenaline now 12 mcg per minute. Mr Patel still requires sedation with propofol.",
      "entities": [
        {"type": "CONDITION", "text": "Lactate", "start": 0, "end": 7},
        {"type": "MEDICATION", "text": "Noradrenaline", "start": 41, "end": 54},
        {"type": "DOSAGE", "text": "12 mcg per minute", "start": 59, "end": 76},
        {"type": "PERSON", "text": "Mr Patel", "start": 78, "end": 86},
        {"type": "MEDICATION", "text": "propofol", "start": 117, "end": 125}
      ],
      "phi_spans": [
        {"type": "PERSON", "text": "Mr Patel", "start": 78, "end": 86}
      ]
    }
  ]
}
```

Conventions:
- `transcript_canonical` is the *intended* transcript — what the script said. Whisper will produce something close but not identical; scoring tools should align (e.g., word-level diff with edit distance) before comparing entity spans.
- `entities[]` lists ALL entities (clinical + PHI). The `phi_spans[]` array is a redundant filtered view (only `type` in PHI set per spec §1.6) so a PII detector can be scored without recomputing the filter — and so we have an explicit, easy-to-eyeball record of "what should the redactor mask."
- Offsets are character offsets into `transcript_canonical`, UTF-8, not token offsets.
- `confidence` is omitted in ground truth (it's 1.0 by definition — we wrote it).

---

## 7. PII insertion rules

PHI must appear in every round and span variety across the episode. Per-episode minima:

| PHI type   | Min per episode | Example                                            |
|------------|-----------------|----------------------------------------------------|
| `PERSON`   | 4               | "Mrs Hartley", "Dr Yamamoto", "Mr Patel"          |
| `DATE`     | 3               | "23rd of May", "this morning", "two weeks ago"    |
| `MRN`      | 1               | "MRN 4 8 2 1 9 7", "record number 482197"         |
| `ADDRESS`  | 1               | "12 Smith Street Croydon Park"                    |
| `PHONE`    | 1               | "0 4 1 2 3 4 5 6 7 8"                             |

Authoring notes:
- Spoken numbers should follow how clinicians actually say them — usually digit-by-digit for MRNs and phones, natural for ages and dosages.
- Don't reuse PHI strings across episodes — gives the detector cross-episode coverage and prevents shortcut-learning.
- Mark every PHI mention in `phi_spans[]` even when the same person is named multiple times in a round. Each utterance is a separate detection event.

---

## 8. Intake EMR — the base document

The intake document is fabricated structured data, not speech. JSON, one per episode. Its role: establish the patient ground truth the rounds reference, so PHI consistency is easy to check ("the rounds called the patient Mr Patel — does intake agree?").

Suggested fields:

```json
{
  "episode_id": "ep_001",
  "patient": {
    "name": "Mr Anwar Patel",
    "mrn": "482197",
    "dob": "1962-03-14",
    "address": "12 Smith Street Croydon Park NSW 2133",
    "phone": "0412 345 678"
  },
  "admission": {
    "admitted_at": "2026-05-21T18:42:00+10:00",
    "presenting_complaint": "Septic shock secondary to community-acquired pneumonia",
    "primary_problems": ["Septic shock", "Type 2 respiratory failure", "Acute kidney injury"]
  },
  "baseline": {
    "weight_kg": 84,
    "allergies": ["penicillin — rash"],
    "regular_meds": ["atorvastatin 40 mg nocte", "metformin 1 g BD"],
    "code_status": "for full active treatment"
  },
  "current_supports": {
    "ventilation": "SIMV-PC, FiO2 0.45, PEEP 8",
    "circulation": "noradrenaline 8 mcg/min",
    "renal": "no RRT",
    "sedation": "propofol 30 mg/hr"
  }
}
```

The intake is not part of the audio test — but it IS the canonical answer key for "is the patient name consistent across rounds?" type scoring questions. A future test that asks "does scribea correctly redact every utterance of the patient's name across all rounds?" reads from `patient.name` here.

---

## 9. File / directory layout

**Audio does not live in the repo.** Text and ground-truth JSON do. The split:

### In the repo (`~/Projects/scribea/corpus/`, text + JSON only)

```
corpus/
  README.md                                 # how the corpus is organised
  voices/
    voice_pool.json                         # TTS voice IDs + persona mapping
  ep_001/
    intake.json                             # §8 — base truth
    round_1_vmo_morning/
      dialog.md                             # script with bracket annotations
      ground_truth.json                     # §6 — machine-readable spans
    round_2_registrar_midday/
      dialog.md
      ground_truth.json
    round_3_vmo_evening/
      ...
    round_4_registrar_overnight/
      ...
  ep_002/
    ...
  ep_003/
    ...
```

### Outside the repo (`$SCRIBE_CORPUS_AUDIO_DIR`, default `~/Downloads/hospital-backgrounds/` for backgrounds, `~/scribea-corpus-audio/` for rendered clips — paths user-overridable)

```
hospital-backgrounds/                       # source ambient loops (§11)
  CREDITS.md                                # attribution + source URLs + licences
  fadingembersaudio-heart-monitor-hospital-ambience-430219.mp3
  freesound_community-hospital-busy-x-ray-room-tone-56441.mp3
  freesound_community-hospital-food-cart-wheeled-79068.mp3
  freesound_community-021447_sonido-ambiente-de-la-capilla-de-la-calle-hospital-73601.mp3
  trabajostiu-salaesperahospital_01-329722.mp3

scribea-corpus-audio/                       # rendered output
  ep_001/
    round_1_vmo_morning/
      clip_1.wav                            # 16 kHz mono, rendered
      clip_2.wav
      clip_3.wav
    ...
  ep_002/
    ...
```

### Why the split

Audio is a **build artifact**: regenerable deterministically from `dialog.md` + `voice_pool.json` + the backgrounds pool, given a fixed TTS. Keeping it out of git means: no LFS, no large diffs, no provenance/licence complications for source backgrounds, and a clean separation between authored content (text, ground truth) and rendered content.

`.gitignore` carries defensive patterns (`corpus/**/*.{mp3,wav,flac,…}`) so a render script that writes into the working tree by mistake still can't stage audio.

If a future build ever needs the rendered clips alongside the text (e.g. CI replay), point `SCRIBE_CORPUS_AUDIO_DIR` at a shared mount or fetch on demand from a sibling artifact repo.

---

## 10. Authoring workflow

**Text first, audio second.** The order matters because the ground truth is annotated against the *script*, not against whatever whisper happened to transcribe.

1. **Sketch the episode arc** — admission narrative, four rounds, what changes between them. One paragraph.
2. **Draft each round's dialog** as plain text, one clip per paragraph. Hit the per-clip and per-round constraints in §5.
3. **Annotate ground truth** for each clip — entity types + character offsets into the script — into the `ground_truth.json` sidecar. A simple bracketing convention in `dialog.md` keeps the human-readable version aligned, e.g. `"{Lactate|CONDITION}'s come down from {5.8|VALUE} to {3.9|VALUE}."` (the brackets are stripped before TTS rendering and the spans are extracted into JSON).
4. **Pass through a sanity-check script** that loads `ground_truth.json`, verifies span offsets land on the right substring in `transcript_canonical`, checks PHI minima, and warns on missing entity types.
5. **Render audio** per §11.
6. **Listen back** to every clip — confirm intelligibility, no rendering artefacts, voice persona matches the script's persona, duration in 5-15s range.

A small CLI in `scripts/corpus/` should automate steps 4 and 5; v0 can do them by hand.

---

## 11. Audio rendering pipeline

```
dialog.md (with bracket annotations)
    │
    ▼ strip annotations
script.txt (one clip per paragraph)
    │
    ▼ TTS (PAI speech model)
clip_N_raw.wav   (single voice per clip, picked from voices/voice_pool.json
                  using the round's persona handle)
    │
    ▼ ffmpeg mix
    ├─ background loop from backgrounds/ (random pick, -18 dB to -24 dB)
    └─ optional: light room reverb + bandpass to simulate microphone path
    │
    ▼ ffmpeg normalize
clip_N.wav       (16 kHz mono s16le, peak around -3 dBFS — matches the
                  output target of scribe-audio-preprocess §3)
```

Constraints on the TTS step:

- Same persona handle (`VMO_A`, `REG_B`) MUST get the same voice ID across an episode — within an episode the listener should hear the same person across the rounds where that persona appears. Across episodes, persona handles can map to different voices.
- Voice pool needs ≥ 2 distinct voices per persona class so cross-episode variety exists (≥ 4 voices total for v0).
- Pitch / speed jitter (±5%) optional per-clip to add micro-variation without breaking the persona match.
- Sample rate at render time can be whatever the TTS emits; the ffmpeg normalize step downsamples to 16 kHz mono.

Background mix:
- Source pool: `$SCRIBE_CORPUS_AUDIO_DIR/hospital-backgrounds/` — default
  `~/Downloads/hospital-backgrounds/` for v0 (see §9 for the file list and
  why the pool lives outside the repo). The render script resolves the
  path once at startup and stamps it into the rendered output's sidecar
  metadata so replays know which pool produced which clip.
- Files are stereo 44.1 kHz MP3; the mix step downmixes to mono and
  resamples to match the speech track before mixing.
- Random pick per clip; **also pick a random sub-window** of the chosen file
  matched to the clip's duration (the backgrounds are 60-80 s, clips are
  5-15 s) so the same file used twice in an episode doesn't sound identical.
- One background per clip — don't layer multiples in v0.
- Mix level: -18 to -24 dB relative to peak speech. Speech stays the
  dominant signal; background contributes acoustic realism, not masking.
- Optional per-clip jitter: ±2 dB on the background level so consecutive
  clips in the same round don't sit at an artificially identical ratio.

---

## 12. Acceptance criteria for v0

Corpus is "done enough to be useful" when:

- **3 episodes complete** — intake + 4 rounds + all clips + ground truth all present
- **Every entity type from spec §1.6** appears at least once across the corpus (10 types: 5 clinical, 5 PHI)
- **Every PHI minimum** from §7 met in every episode (4× PERSON, 3× DATE, 1× each of MRN/ADDRESS/PHONE)
- **Continuity references**: rounds 2-4 of each episode each contain at least one explicit reference to a prior round's content
- **`scripts/corpus/validate.py`** (or equivalent) runs clean — span offsets line up, PHI minima met, no orphan files
- **A replay test** drives all 12 rounds through scribea end-to-end without errors; transcripts ≥ 75% word-level accuracy against `transcript_canonical` (whisper isn't perfect on synthesised speech with noise; 75% is the lower bound where downstream NER scoring still means something)

Out of scope for v0 (file as future work if useful):

- Multi-speaker clips (cross-talk, interruptions)
- Code-switching, accents beyond the available voice pool
- Adversarial inputs (deliberate PII near non-PII tokens to trick the detector)
- Longer rounds (>5 clips)
- Real-time streaming render

---

## 13. What changes when this exists

- **NER plugin** (you): can be scored on per-type precision/recall against ground truth, not eyeballed.
- **PII detector plugin** (matt): same — every spoken PHI is in the answer key, so false-negative rate is a number.
- **End-to-end regression**: replay all 12 rounds after any pipeline change; diff structured output against a golden file checked into the corpus dir.
- **Cross-model bake-offs** (GLiNER vs scispacy vs regex): same input, same ground truth, objective comparison.
- **Acoustic baselines**: word-error-rate on the corpus becomes the regression metric for any change to `scribe-audio-preprocess` or the whisper container.
- **A demo deck**: a single rendered episode is enough to show the system end-to-end without needing a live consult.

---

*End of corpus spec.*

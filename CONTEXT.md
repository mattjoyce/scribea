# scribea — Clinical Scribe Domain Language

A learning prototype that turns clinician dictation about ICU patients into
structured notes. v0 is scoped to the ICU setting; this glossary follows that
scope.

## Language

**Case**:
The persistent record of one ICU patient's admission — the "backstory" that
contextualises every session of dictation about them.
_Avoid_: Patient (broader, lifetime), Admission (close, but elides the
identity), Episode (correct but generic).

**EMR Backstory**:
The minimal context attached to a session: **demographics** (who the patient
is) + **previous notes** (free-text block summarising prior care). Fed to the
structure-worker alongside the assembled transcript. v0 ships a faux mock;
production someday reads from a real EMR feed.
_Avoid_: chart, record, context (all too generic).
_v0 scope_: previous notes is a static authored block — no chaining between
sessions because POC has one session per case.

**Session**:
One clinician's continuous dictation effort about a **Case**. Already defined
in specseed §4.1; extended here with `case_id` and `role`.

**Clip**:
A single segment of dictation audio within a **Session**. Client-generated
`clip_id`, content-addressed blob. Already defined in specseed §4.2.

**Role**:
The clinical role of the dictating clinician — determines the **Dictation
Style** the structure-worker must preserve. v0 roles: **VMO** and
**Registrar**.
_Avoid_: speaker (a diarization concept), persona (a UI concept).

**VMO** (Visiting Medical Officer):
Senior ICU consultant. In v0 corpus, dictates in the **Systems Approach**
style.
_Note_: Aus/NZ term. Elsewhere: Attending, Consultant.

**Registrar**:
Senior ICU trainee. In v0 corpus, dictates in the **Checklist Approach**
style (FAST HUGS BID variant).
_Note_: Aus/NZ/UK term. In US: Resident, Fellow.

**Dictation**:
Single-speaker spoken report (clinician → microphone). The audio captured by
v0. Distinguished from **Consult Audio** (clinician+patient dialogue, out of
v0 scope per specseed §2 and §15).

**Dictation Style**:
The shape a clinician dictates in. v0 corpus pins one style per **Role**, but
in real ICU practice clinicians use both (see
[`docs/icu-notes-two-approaches.md`](docs/icu-notes-two-approaches.md)) —
this is a corpus simplification, not a clinical claim.

**Systems Approach**:
An organ-system review where each section assembles that system's current
story. Hierarchical: each system is a frame; the plan integrates across them.
Best used for reasoning, prioritisation, and presenting on rounds. Failure
mode: fragmentation isn't the risk — *omission of routine checklist items* is.
Canonical system list for ICU (per the source-of-truth note):
Neuro, CVS, Resp, Renal, GI, Heme-ID, Endo, Skin, Lines, Plan.

**Checklist Approach**:
A fixed list of items confirmed for every patient, every shift. Linear and
itemised; each item independent. Best used for daily safety, handover
scaffolding, audit. Failure mode: the note can be complete without the
clinician understanding the patient. Several common mnemonics exist
(FAST HUGS BID, ABCDEF, A–Z). v0 corpus uses **FAST HUGS BID**:
Feeding, Analgesia, Sedation, Thromboprophylaxis, Head-of-bed,
Ulcer prophylaxis, Glucose, SBT (spontaneous breathing trial), Bowels,
Indwelling devices, De-escalation.

## Relationships

- A **Case** owns one **EMR Backstory** and (in v0) exactly one **Session**.
- A **Session** belongs to one **Case**, is performed by a clinician of a
  known **Role**, contains 3–5 ordered **Clips**, and produces one structured
  note in the **Dictation Style** associated with the role.
- _Post-v0_: a **Case** will group multiple **Sessions** (VMO + Registrar +
  …) over the ICU admission; the EMR Backstory will accumulate prior notes
  through the case. Out of scope for POC.

## Example dialogue

> **Dev:** "If a VMO and a Registrar both dictate about Mrs Wilson on ICU
> day 2, do they share a **Case**?"
> **Domain expert:** "Conceptually yes — same admission, same EMR Backstory.
> But in the POC we only model one **Session** per **Case**, so the corpus
> would have two separate cases (`01_…_vmo`, `01_…_registrar`) sharing the
> same backstory content. Multi-session-per-case is post-POC."

## Flagged ambiguities

_Both previously-flagged ambiguities (system list, checklist letter coverage)
are now resolved by [`docs/icu-notes-two-approaches.md`](docs/icu-notes-two-approaches.md)._

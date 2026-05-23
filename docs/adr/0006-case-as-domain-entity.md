# Case is a reference label, not a domain entity in scribe.db

A **Case** identifies one ICU patient's admission. It owns an **EMR Backstory**
(demographics + previous notes) that is fed to the structure-worker so the LLM
sees the established facts about the patient.

Authoritative case content does not live in `scribe.db`. It lives in whoever
is *simulating the EMR feed* — for v0, the test-corpus loader (reading
`case.yaml`); later, a real EMR integration. `scribe.db` records two
load-bearing things:

1. A nullable `case_id` column on `sessions`, so we can ask which case a
   session belongs to. No FK constraint — there is no `cases` table to point
   at, by design.
2. A `scribe.case.context_attached.v1` event in the `events` log, stamped at
   session-create time when the caller supplies demographics and/or previous
   notes. The event's `data` payload contains the exact context the LLM will
   see, snapshotted at that moment.

The earlier draft of this ADR proposed a separately-modelled `cases` table
plus a `POST /cases` endpoint. That was reverted before merge once we noticed
the table duplicated content the corpus already owns — Hickey's complecting
warning: two storage layers for one fact, with a synchronisation problem
attached. The event log is the audit copy; the corpus (or EMR) is the
authoritative copy; the cases table was a redundant cache that earned nothing.

`scribe.case.created.v1` is also out. Cases are not events in their own
right — they are labels carried by sessions. The creation moment that
matters for audit is *when context is attached to a session*, which is what
`context_attached` captures.

Post-POC, if cases evolve into a longer-lived entity in scribe.db (e.g. a
partial EMR mirror you can read back), this decision can be revisited with
one migration that adds the `cases` table. The current shape doesn't paint
us into a corner.

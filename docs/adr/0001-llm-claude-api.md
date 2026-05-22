# ADR 0001 — Use Claude API for v0 structure-worker

**Date:** 2026-05-22
**Status:** Accepted
**Decides:** specseed §14 Q1

## Context

The structure-worker converts an assembled clinical transcript + template prompt into a structured SOAP-shaped JSON. The spec leaves the v0 LLM choice open between Claude (Anthropic API) and local Ollama (Qwen via llama-swap on Unraid). Both are feasible.

## Decision

Use **Claude API** (`claude-opus-4-7` family or current default, chosen at request time) for v0.

## Rationale

- Removes one variable: Claude's JSON-mode + tool-use produce reliable schema-conforming output without prompt acrobatics, and we already know the failure modes.
- The spec calls this out as the recommended path; agreeing reduces drift between spec and code.
- Cost per session is small for v0 traffic (a few cents at most for SOAP-length output) and the `llm_cache` table makes replays free.
- Swap-out is cheap: the structure-worker's LLM call is one function. Pointing it at `llama-swap` (Unraid :11440) for local Qwen is a follow-up that doesn't touch any other worker or the schema.

## Consequences

- `structure-worker` requires `ANTHROPIC_API_KEY` in its plugin config. When the key is absent it returns a schema-valid stub with `meta.model="stub"` — the audit trail still tells the truth.
- We accept that v0 has one outbound dependency outside the home lab. The audio path remains local (Unraid whisper); only the structured-text generation crosses the boundary.

## Future swap

When ready, set `plugin:scribe-structure.config.llm_url=http://192.168.20.4:11440` and `llm_model=qwen2.5:32b-instruct` (or chosen model). No other code or template changes required if the local model can produce schema-valid JSON; otherwise the prompt may need a more forceful schema instruction.

## Related

- ADR 0003 — baggage stamps the model identity end-to-end.
- specseed §3.1 (data over identity — the LLM is a function, not an object).

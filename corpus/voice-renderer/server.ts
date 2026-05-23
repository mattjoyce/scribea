#!/usr/bin/env bun
/**
 * scribea voice renderer — trimmed copy of ~/.claude/VoiceServer/server.ts
 *
 * Differences from PAI's notification server:
 *   - Only POST /synthesize and GET /health; no /notify, /pai, no AppleScript
 *   - Reads voice config from ./voices.json (scribea-local) instead of
 *     ~/.claude/settings.json — so changing a persona voice here doesn't
 *     touch PAI's notification voices.
 *   - Default port 8889 to avoid clashing with PAI's 8888.
 *   - No rate limiting (intended for local corpus rendering, not public).
 *
 * OPENAI_API_KEY is read from ~/.env (same path PAI uses).
 *
 * See docs/scribe-test-corpus.md §11 for the corpus render pipeline.
 */

import { serve } from "bun";
import { homedir } from "os";
import { join, dirname } from "path";
import { existsSync, readFileSync } from "fs";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---- env --------------------------------------------------------------
const envPath = join(homedir(), ".env");
if (existsSync(envPath)) {
  const envContent = readFileSync(envPath, "utf-8");
  for (const line of envContent.split("\n")) {
    const [k, v] = line.split("=");
    if (k && v && !k.startsWith("#")) {
      process.env[k.trim()] = v.trim();
    }
  }
}

const PORT = parseInt(process.env.PORT || "8889");
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
if (!OPENAI_API_KEY) {
  console.error("OPENAI_API_KEY missing (looked in ~/.env). /synthesize will fail.");
}

// ---- pronunciation system --------------------------------------------
interface PronEntry { term: string; phonetic: string; note?: string; }
interface CompiledRule { regex: RegExp; phonetic: string; }
let pronRules: CompiledRule[] = [];

function escapeRegex(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function loadPronunciations() {
  const p = join(__dirname, "pronunciations.json");
  if (!existsSync(p)) {
    console.warn("no pronunciations.json — TTS uses default pronunciations");
    return;
  }
  try {
    const cfg = JSON.parse(readFileSync(p, "utf-8")) as { replacements: PronEntry[] };
    pronRules = cfg.replacements.map(e => ({
      regex: new RegExp(`\\b${escapeRegex(e.term)}\\b`, "g"),
      phonetic: e.phonetic,
    }));
    console.log(`loaded ${pronRules.length} pronunciation rules`);
  } catch (e) {
    console.error("failed to load pronunciations.json:", e);
  }
}
loadPronunciations();

function applyPronunciations(text: string): string {
  let out = text;
  for (const r of pronRules) out = out.replace(r.regex, r.phonetic);
  return out;
}

// ---- voice config (scribea-local) ------------------------------------
interface VoiceEntry {
  voice_id: string;      // OpenAI voice name (e.g. "alloy", "onyx", "nova")
  speed?: number;
  model?: string;
  instructions?: string;
  description?: string;
}
interface VoicesFile {
  default: string;       // persona name to use when none specified
  voices: Record<string, VoiceEntry>;  // keyed by persona handle
}

let voicesFile: VoicesFile = { default: "alloy", voices: {} };

function loadVoices() {
  const p = join(__dirname, "voices.json");
  if (!existsSync(p)) {
    console.warn("no voices.json — falling back to OpenAI 'alloy' for everything");
    return;
  }
  try {
    voicesFile = JSON.parse(readFileSync(p, "utf-8")) as VoicesFile;
    const names = Object.keys(voicesFile.voices);
    console.log(`loaded ${names.length} persona voices: ${names.join(", ")}`);
    console.log(`default persona: ${voicesFile.default}`);
  } catch (e) {
    console.error("failed to load voices.json:", e);
  }
}
loadVoices();

function resolveVoice(persona: string | null): VoiceEntry {
  const name = persona || voicesFile.default;
  const entry = voicesFile.voices[name];
  if (!entry) {
    console.warn(`unknown persona '${name}' — falling back to OpenAI 'alloy'`);
    return { voice_id: "alloy", speed: 1.0, model: "gpt-4o-mini-tts" };
  }
  return {
    voice_id: entry.voice_id,
    speed: entry.speed ?? 1.0,
    model: entry.model ?? "gpt-4o-mini-tts",
    instructions: entry.instructions,
  };
}

// ---- TTS call --------------------------------------------------------
async function generateSpeech(text: string, voice: VoiceEntry): Promise<ArrayBuffer> {
  if (!OPENAI_API_KEY) throw new Error("OPENAI_API_KEY not configured");
  const pronounced = applyPronunciations(text);
  const body: Record<string, unknown> = {
    model: voice.model || "gpt-4o-mini-tts",
    input: pronounced,
    voice: voice.voice_id,
    speed: voice.speed ?? 1.0,
    response_format: "mp3",
  };
  if (voice.instructions && body.model === "gpt-4o-mini-tts") {
    body.instructions = voice.instructions;
  }
  const resp = await fetch("https://api.openai.com/v1/audio/speech", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENAI_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`OpenAI TTS ${resp.status}: ${text}`);
  }
  return await resp.arrayBuffer();
}

// ---- HTTP ------------------------------------------------------------
const SYNTHESIZE_MAX_CHARS = 10000;

serve({
  port: PORT,
  async fetch(req) {
    const url = new URL(req.url);

    if (url.pathname === "/health") {
      return Response.json({
        status: "healthy",
        port: PORT,
        api_key_configured: !!OPENAI_API_KEY,
        personas: Object.keys(voicesFile.voices),
        default_persona: voicesFile.default,
        pronunciation_rules: pronRules.length,
      });
    }

    if (url.pathname === "/synthesize" && req.method === "POST") {
      try {
        const data = await req.json() as Record<string, unknown>;
        const text = (data.message ?? data.text ?? "") as string;
        const persona = (data.persona ?? data.voice_id ?? null) as string | null;

        if (!text || typeof text !== "string") {
          return Response.json({ status: "error", message: "missing or invalid 'message'" }, { status: 400 });
        }
        if (text.length > SYNTHESIZE_MAX_CHARS) {
          return Response.json({
            status: "error",
            message: `message too long (max ${SYNTHESIZE_MAX_CHARS} chars, got ${text.length})`,
          }, { status: 413 });
        }

        const voice = resolveVoice(persona);
        console.log(`/synthesize: persona=${persona || voicesFile.default} voice=${voice.voice_id} chars=${text.length}`);
        const audio = await generateSpeech(text, voice);
        return new Response(audio, { headers: { "Content-Type": "audio/mpeg" } });
      } catch (e: any) {
        console.error("/synthesize error:", e);
        return Response.json({ status: "error", message: e.message || "internal error" }, { status: 500 });
      }
    }

    return new Response(
      "scribea voice renderer — POST /synthesize {message, persona?} → audio/mpeg",
      { status: 200 },
    );
  },
});

console.log(`scribea voice renderer on http://localhost:${PORT}`);
console.log(`OPENAI key: ${OPENAI_API_KEY ? "configured" : "MISSING"}`);

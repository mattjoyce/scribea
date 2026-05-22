// IndexedDB outbox + sync loop with exponential backoff.
// Spec §9.2: PWA owns the clip until server acknowledges.
// Backoff schedule (ms): 1s, 2s, 5s, 15s, 60s, 300s (cap 300s).

import { api } from "./api.js";

const DB_NAME = "scribe";
const DB_VERSION = 1;
const STORE = "outbox";
const BACKOFF_MS = [1000, 2000, 5000, 15000, 60000, 300000];
const EVICT_AFTER_MS = 24 * 60 * 60 * 1000; // 24h
const TICK_MS = 2000;

let dbPromise = null;

function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const s = db.createObjectStore(STORE, { keyPath: "clip_id" });
        s.createIndex("by_session", "session_id", { unique: false });
        s.createIndex("by_state", "state", { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

function tx(mode) {
  return openDb().then((db) => db.transaction(STORE, mode).objectStore(STORE));
}

function reqAsPromise(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export async function putEntry(entry) {
  const store = await tx("readwrite");
  return reqAsPromise(store.put(entry));
}

export async function getEntry(clip_id) {
  const store = await tx("readonly");
  return reqAsPromise(store.get(clip_id));
}

export async function listAll() {
  const store = await tx("readonly");
  return reqAsPromise(store.getAll());
}

export async function listBySession(session_id) {
  const store = await tx("readonly");
  const idx = store.index("by_session");
  return reqAsPromise(idx.getAll(IDBKeyRange.only(session_id)));
}

export async function deleteEntry(clip_id) {
  const store = await tx("readwrite");
  return reqAsPromise(store.delete(clip_id));
}

// Queue a new clip. Caller passes blob + metadata.
export async function enqueueClip({ session_id, clip_id, seq, started_at, duration_ms, blob, audio_format, meta }) {
  const entry = {
    clip_id,
    session_id,
    seq,
    started_at,
    duration_ms,
    blob,
    audio_format,
    meta: meta || {},
    state: "queued",
    attempts: 0,
    next_attempt_at: 0, // 0 = ready immediately
    queued_at: Date.now(),
    confirmed_at: null,
    last_error: null,
  };
  await putEntry(entry);
  // Wake the loop so the user sees an upload happen right away.
  kick();
  return entry;
}

// Pub-sub for UI updates on state change.
const listeners = new Set();
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function notify(entry) { for (const fn of listeners) { try { fn(entry); } catch (e) { console.error(e); } } }

let loopHandle = null;
let kickPending = false;

export function startSyncLoop() {
  if (loopHandle) return;
  loopHandle = setInterval(tick, TICK_MS);
  // Run one tick immediately on start.
  tick();
}

export function stopSyncLoop() {
  if (loopHandle) clearInterval(loopHandle);
  loopHandle = null;
}

function kick() {
  if (kickPending) return;
  kickPending = true;
  queueMicrotask(() => { kickPending = false; tick(); });
}

let inFlight = false;
async function tick() {
  if (inFlight) return;
  inFlight = true;
  try {
    const entries = await listAll();
    const now = Date.now();
    for (const e of entries) {
      if (e.state === "queued" && (e.next_attempt_at || 0) <= now) {
        await attemptUpload(e);
      } else if (e.state === "confirmed" && e.confirmed_at && now - e.confirmed_at > EVICT_AFTER_MS) {
        // Keep blob for 24h, then evict.
        await deleteEntry(e.clip_id);
      }
    }
  } catch (err) {
    console.warn("outbox tick error", err);
  } finally {
    inFlight = false;
  }
}

async function attemptUpload(entry) {
  try {
    await api.uploadClip({
      session_id: entry.session_id,
      clip_id: entry.clip_id,
      started_at: entry.started_at,
      duration_ms: entry.duration_ms,
      seq: entry.seq,
      audio_format: entry.audio_format,
      blob: entry.blob,
    });
    const updated = { ...entry, state: "confirmed", confirmed_at: Date.now(), last_error: null };
    await putEntry(updated);
    notify(updated);
  } catch (err) {
    const status = err && err.status;
    // 410 Gone: server-side session is closed. Mark orphaned, surface in clip list.
    if (status === 410) {
      const updated = { ...entry, state: "orphaned", last_error: "session_closed_410" };
      await putEntry(updated);
      notify(updated);
      return;
    }
    // 413 clip_too_long, or other 4xx that won't fix itself: fail permanently.
    if (status === 413 || (status >= 400 && status < 500 && status !== 408 && status !== 429)) {
      const updated = { ...entry, state: "failed", last_error: `http_${status}` };
      await putEntry(updated);
      notify(updated);
      return;
    }
    // Otherwise, retry with backoff.
    const attempts = (entry.attempts || 0) + 1;
    const delay = BACKOFF_MS[Math.min(attempts - 1, BACKOFF_MS.length - 1)];
    const updated = {
      ...entry,
      attempts,
      next_attempt_at: Date.now() + delay,
      last_error: err && err.message ? err.message : String(err),
    };
    await putEntry(updated);
    notify(updated);
  }
}

// Returns count of queued entries for a session (used by End-session gating).
export async function pendingForSession(session_id) {
  const all = await listBySession(session_id);
  return all.filter((e) => e.state === "queued").length;
}

// MediaRecorder wrapper with auto-rollover at 10 min (ADR-0005).
// Spec §9.2 / ADR-0005: at 8:00 show warning indicator. At 10:00 stop and
// immediately start a new clip with meta.auto_rolled:true.

const HARD_CAP_MS = 10 * 60 * 1000;   // 10 minutes
const WARN_AT_MS  = 8 * 60 * 1000;    // 8 minutes
const TICK_MS = 250;

function pickMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
  ];
  if (typeof MediaRecorder === "undefined") return null;
  for (const c of candidates) {
    try { if (MediaRecorder.isTypeSupported(c)) return c; } catch { /* ignore */ }
  }
  return ""; // let browser pick
}

export class ClipRecorder {
  /**
   * @param {object} opts
   * @param {(clip:object)=>void} opts.onClipFinalized called once per finalized clip
   * @param {(state:object)=>void} opts.onTick called frequently with {elapsedMs, warning, autoRollImminent}
   * @param {(err:Error)=>void} opts.onError
   */
  constructor(opts) {
    this.opts = opts;
    this.stream = null;
    this.recorder = null;
    this.chunks = [];
    this.startedAt = null;     // wall-clock ISO of current clip
    this.startedMs = 0;        // perf time
    this.tickHandle = null;
    this.running = false;
    this.mimeType = pickMimeType();
    this.autoRolledNext = false; // tag the next start as auto-rolled
    this.seqCounter = 0;         // owned by caller usually; we just track within a recording session
  }

  setSeq(n) { this.seqCounter = n; }
  nextSeq() { return this.seqCounter++; }

  isRunning() { return this.running; }

  async start({ autoRolled = false } = {}) {
    if (this.running) return;
    if (!this.stream) {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    const options = this.mimeType ? { mimeType: this.mimeType } : {};
    try {
      this.recorder = new MediaRecorder(this.stream, options);
    } catch (e) {
      // Fallback without options.
      this.recorder = new MediaRecorder(this.stream);
    }
    this.chunks = [];
    this.recorder.ondataavailable = (e) => { if (e.data && e.data.size) this.chunks.push(e.data); };
    this.recorder.onerror = (e) => { this.opts.onError && this.opts.onError(e.error || new Error("recorder error")); };
    this.recorder.onstop = () => this._finalizeCurrent();

    // Spec §9.2 step 1: clip_id + started_at generated at Start.
    this.currentClipId = crypto.randomUUID();
    this.startedAt = new Date().toISOString();
    this.startedMs = performance.now();
    this._autoRolledFlag = autoRolled;
    this.recorder.start();
    this.running = true;
    this._startTick();
  }

  async stop({ userInitiated = true } = {}) {
    if (!this.running || !this.recorder) return;
    this.running = false;
    this._stopTick();
    this._userInitiatedStop = userInitiated;
    try { this.recorder.stop(); } catch { /* ignore */ }
  }

  // Stop recording AND release the mic. Called on screen leave / end session.
  async dispose() {
    await this.stop({ userInitiated: false });
    if (this.stream) {
      try { for (const t of this.stream.getTracks()) t.stop(); } catch { /* ignore */ }
      this.stream = null;
    }
    this._stopTick();
  }

  _finalizeCurrent() {
    const endMs = performance.now();
    const duration_ms = Math.round(endMs - this.startedMs);
    const type = (this.recorder && this.recorder.mimeType) || this.mimeType || "audio/webm";
    const blob = new Blob(this.chunks, { type });
    this.chunks = [];
    const clip = {
      clip_id: this.currentClipId,   // generated at Start (spec §9.2)
      started_at: this.startedAt,
      duration_ms,
      audio_format: type,
      blob,
      seq: this.nextSeq(),
      meta: this._autoRolledFlag ? { auto_rolled: true } : {},
    };
    this.currentClipId = null;
    if (this.opts.onClipFinalized) {
      try { this.opts.onClipFinalized(clip); } catch (e) { console.error(e); }
    }
    this.recorder = null;

    // If we stopped due to the hard cap, immediately roll a fresh clip.
    if (this._rolloverPending) {
      this._rolloverPending = false;
      // Schedule on next tick so caller can see the finalized clip first.
      setTimeout(() => { this.start({ autoRolled: true }).catch((e) => this.opts.onError && this.opts.onError(e)); }, 0);
    }
  }

  _startTick() {
    this._stopTick();
    this.tickHandle = setInterval(() => {
      if (!this.running) return;
      const elapsedMs = performance.now() - this.startedMs;
      const warning = elapsedMs >= WARN_AT_MS;
      const autoRollImminent = elapsedMs >= HARD_CAP_MS - 1000;
      if (this.opts.onTick) {
        try { this.opts.onTick({ elapsedMs, warning, autoRollImminent, hardCapMs: HARD_CAP_MS }); } catch (e) { console.error(e); }
      }
      if (elapsedMs >= HARD_CAP_MS) {
        // Auto-roll: stop current, schedule fresh start.
        this._rolloverPending = true;
        this.stop({ userInitiated: false });
      }
    }, TICK_MS);
  }

  _stopTick() {
    if (this.tickHandle) clearInterval(this.tickHandle);
    this.tickHandle = null;
  }
}

export { HARD_CAP_MS, WARN_AT_MS };

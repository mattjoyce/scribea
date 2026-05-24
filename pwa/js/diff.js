// Word-level diff for ASR transcript comparison against a known ground truth.
//
// Tokenises both inputs into a flat lower-case word stream (whitespace +
// punctuation normalised, contractions preserved), aligns via Levenshtein
// DP, and emits per-op alignment for side-by-side rendering. Hand-rolled
// to avoid a runtime dep — the offline PWA must not depend on CDN imports.

import { el } from "./util.js";

export function tokenize(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s']/gu, " ")
    .split(/\s+/)
    .filter(Boolean);
}

// Returns array of {op, truth, hyp} where op ∈ 'eq'|'sub'|'del'|'ins'.
// truth is null on 'ins'; hyp is null on 'del'.
export function alignWords(truth, hyp) {
  const m = truth.length, n = hyp.length;
  const dp = new Array(m + 1);
  for (let i = 0; i <= m; i++) {
    dp[i] = new Int32Array(n + 1);
    dp[i][0] = i;
  }
  for (let j = 0; j <= n; j++) dp[0][j] = j;
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      const sub = dp[i - 1][j - 1] + (truth[i - 1] === hyp[j - 1] ? 0 : 1);
      const del = dp[i - 1][j] + 1;
      const ins = dp[i][j - 1] + 1;
      dp[i][j] = Math.min(sub, del, ins);
    }
  }
  const ops = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && truth[i - 1] === hyp[j - 1] && dp[i][j] === dp[i - 1][j - 1]) {
      ops.push({ op: "eq", truth: truth[i - 1], hyp: hyp[j - 1] });
      i--; j--;
    } else if (i > 0 && j > 0 && dp[i][j] === dp[i - 1][j - 1] + 1) {
      ops.push({ op: "sub", truth: truth[i - 1], hyp: hyp[j - 1] });
      i--; j--;
    } else if (i > 0 && dp[i][j] === dp[i - 1][j] + 1) {
      ops.push({ op: "del", truth: truth[i - 1], hyp: null });
      i--;
    } else {
      ops.push({ op: "ins", truth: null, hyp: hyp[j - 1] });
      j--;
    }
  }
  return ops.reverse();
}

// Standard WER: (substitutions + deletions + insertions) / |truth|.
export function werFromOps(ops, truthLen) {
  if (truthLen === 0) return ops.some((o) => o.op !== "eq") ? Infinity : 0;
  let edits = 0;
  for (const o of ops) if (o.op !== "eq") edits++;
  return edits / truthLen;
}

// Collapse runs of consecutive 'eq' ops into a single row so the diff reads
// like a code diff (context lines + change lines) instead of one DOM row per
// matched word.
function collapseEqRuns(ops) {
  const out = [];
  let buf = null;
  for (const o of ops) {
    if (o.op === "eq") {
      if (buf) {
        buf.truth += " " + o.truth;
        buf.hyp += " " + o.hyp;
      } else {
        buf = { op: "eq", truth: o.truth, hyp: o.hyp };
      }
    } else {
      if (buf) { out.push(buf); buf = null; }
      out.push(o);
    }
  }
  if (buf) out.push(buf);
  return out;
}

// 2-col grid; each row is one op (with eq-runs collapsed). Empty side of
// del/ins shows a · placeholder so the eye keeps the columns paired.
export function renderDiffPair(ops) {
  const grid = el("div", { class: "diff-grid" });
  for (const row of collapseEqRuns(ops)) {
    const left = el("div", { class: `diff-cell diff-truth ${row.op}` });
    const right = el("div", { class: `diff-cell diff-hyp ${row.op}` });
    if (row.truth != null) left.textContent = row.truth;
    else left.append(el("span", { class: "diff-placeholder", text: "·" }));
    if (row.hyp != null) right.textContent = row.hyp;
    else right.append(el("span", { class: "diff-placeholder", text: "·" }));
    grid.append(left, right);
  }
  return grid;
}

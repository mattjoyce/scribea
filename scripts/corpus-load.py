#!/usr/bin/env python3
"""Load a rendered corpus case into the running scribe-ingress.

Reads test-corpus/cases/<case_id>/case.yaml, POSTs a session with the EMR
context + ground-truth scripts (harness mode — populates the
scribe.case.ground_truth_attached.v1 baggage event so the PWA Diff tab has
something to compare against), uploads the rendered clip mp3s, optionally
closes the session.

Idempotent: stable Idempotency-Key per (case_id, [clip_seq]) means re-runs
return the cached responses instead of creating duplicates.

Dependencies
  - pyyaml (also used by scripts/corpus-render.py)
  - ffprobe on PATH (brew install ffmpeg) — to read clip duration_ms

Usage
  ./scripts/corpus-load.py check                    # ingress reachable + cases
  ./scripts/corpus-load.py load <case_id>           # load one case
  ./scripts/corpus-load.py load <case_id> --no-close
  ./scripts/corpus-load.py all                      # load every case
  ./scripts/corpus-load.py all --stop-on-error
  ./scripts/corpus-load.py --ingress http://localhost:8090 load <case_id>

The ingress server must be running. Audio mp3s must already be rendered
(scripts/corpus-render.py) — this script does not call out to the renderer.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INGRESS = "http://localhost:8090"
DEFAULT_CASES_DIR = REPO_ROOT / "test-corpus" / "cases"

ROLE_TO_TEMPLATE: dict[str, str] = {"vmo": "icu_vmo", "registrar": "icu_registrar"}


# ---------------------------------------------------------------------------
# Idempotency-Key derivation — stable per (case_id, [seq]) so re-runs hit the
# ingress idempotency cache instead of producing duplicate sessions/clips.
# ---------------------------------------------------------------------------

def session_idem_key(case_id: str) -> str:
    return f"corpus-load:session:{case_id}"


def clip_idem_key(case_id: str, seq: int) -> str:
    return f"corpus-load:clip:{case_id}:{seq:02d}"


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib urllib, no extra deps.
# ---------------------------------------------------------------------------

def _request(method: str, url: str, *, headers: dict[str, str] | None = None,
             data: bytes | None = None, timeout: float = 30.0) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, dict(e.headers or {})


def _ok_or_raise(status: int, body: bytes, label: str) -> dict[str, Any]:
    if 200 <= status < 300:
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {"_raw": body.decode("utf-8", errors="replace")}
    msg = body.decode("utf-8", errors="replace")[:400]
    raise RuntimeError(f"{label}: HTTP {status} — {msg}")


def build_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Build a multipart/form-data body.

    fields: {name: str_value}
    files:  {name: (filename, content_bytes, content_type)}
    Returns (body_bytes, content_type_header).
    """
    boundary = "----scribea-corpus-load-" + secrets.token_hex(8)
    sep = b"\r\n"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        chunks.append(b"")
        chunks.append(value.encode("utf-8"))
    for name, (filename, content, content_type) in files.items():
        chunks.append(f"--{boundary}".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode()
        )
        chunks.append(f"Content-Type: {content_type}".encode())
        chunks.append(b"")
        chunks.append(content)
    chunks.append(f"--{boundary}--".encode())
    chunks.append(b"")
    body = sep.join(chunks)
    return body, f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Audio probe
# ---------------------------------------------------------------------------

def probe_duration_ms(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return int(round(float(out.stdout.strip()) * 1000))


# ---------------------------------------------------------------------------
# Per-case load
# ---------------------------------------------------------------------------

def post_session(ingress: str, case_id: str, template_id: str,
                 demographics: dict[str, Any], previous_notes: str,
                 ground_truth_clips: list[dict[str, Any]]) -> dict[str, Any]:
    body = json.dumps({
        "template_id": template_id,
        "case_id": case_id,
        "demographics": demographics,
        "previous_notes": previous_notes,
        "ground_truth_clips": ground_truth_clips,
    }).encode("utf-8")
    status, resp_body, _ = _request(
        "POST", f"{ingress}/sessions",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": session_idem_key(case_id),
        },
        data=body, timeout=10.0,
    )
    return _ok_or_raise(status, resp_body, "POST /sessions")


def post_clip(ingress: str, session_id: str, case_id: str, seq: int,
              clip_id: str, audio_path: Path) -> dict[str, Any]:
    duration_ms = probe_duration_ms(audio_path)
    audio_bytes = audio_path.read_bytes()
    started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    body, ctype = build_multipart(
        fields={
            "clip_id": clip_id,
            "started_at": started_at,
            "duration_ms": str(duration_ms),
            "seq": str(seq),
            "audio_format": "audio/mpeg",
        },
        files={"audio": (f"clip-{seq:02d}.mp3", audio_bytes, "audio/mpeg")},
    )
    status, resp_body, _ = _request(
        "POST", f"{ingress}/sessions/{session_id}/clips",
        headers={
            "Content-Type": ctype,
            "Accept": "application/json",
            "Idempotency-Key": clip_idem_key(case_id, seq),
        },
        data=body, timeout=60.0,
    )
    return _ok_or_raise(status, resp_body, f"POST /sessions/{session_id[:8]}/clips seq={seq}")


def close_session(ingress: str, session_id: str, reason: str = "corpus-load") -> dict[str, Any]:
    body = json.dumps({"close_reason": reason}).encode("utf-8")
    status, resp_body, _ = _request(
        "POST", f"{ingress}/sessions/{session_id}/close",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=body, timeout=10.0,
    )
    return _ok_or_raise(status, resp_body, "POST /sessions/{id}/close")


def load_case(ingress: str, case_dir: Path, close: bool = True, verbose: bool = True) -> str:
    case_yaml_path = case_dir / "case.yaml"
    if not case_yaml_path.is_file():
        raise FileNotFoundError(f"no case.yaml in {case_dir}")
    case_yaml = yaml.safe_load(case_yaml_path.read_text(encoding="utf-8"))

    case_id = case_yaml["case_id"]
    session_block = case_yaml.get("session") or {}
    role = session_block.get("role")
    if role not in ROLE_TO_TEMPLATE:
        raise ValueError(f"unknown session.role={role!r} (expected one of {list(ROLE_TO_TEMPLATE)})")
    template_id = ROLE_TO_TEMPLATE[role]

    demographics = dict(case_yaml.get("demographics") or {})
    previous_notes = (case_yaml.get("previous_notes") or "").strip()

    clip_entries = session_block.get("clips") or []
    if not clip_entries:
        raise ValueError(f"{case_id}: session.clips is empty")

    # Build ground-truth payload + verify audio exists before any HTTP work.
    ground_truth: list[dict[str, Any]] = []
    audio_plan: list[tuple[int, Path]] = []
    for seq, clip in enumerate(clip_entries, start=1):
        script_rel = clip.get("script")
        audio_rel = clip.get("audio")
        if not script_rel or not audio_rel:
            raise ValueError(f"{case_id} clip {seq}: missing script/audio path")
        script_path = case_dir / script_rel
        audio_path = case_dir / audio_rel
        if not script_path.is_file():
            raise FileNotFoundError(f"{case_id} clip {seq}: script not found at {script_path}")
        if not audio_path.is_file():
            raise FileNotFoundError(
                f"{case_id} clip {seq}: audio not found at {audio_path} — "
                f"render first: ./scripts/corpus-render.py case {case_id}"
            )
        ground_truth.append({"seq": seq, "script": script_path.read_text(encoding="utf-8").strip()})
        audio_plan.append((seq, audio_path))

    if verbose:
        print(f"  POST /sessions  case_id={case_id} template={template_id} clips={len(audio_plan)}")
    sess = post_session(ingress, case_id, template_id, demographics, previous_notes, ground_truth)
    session_id = sess["session_id"]
    if verbose:
        print(f"    session_id={session_id}  state={sess.get('state')}")

    for seq, audio_path in audio_plan:
        clip_id = str(uuid.uuid4())
        if verbose:
            print(f"  POST /sessions/{session_id[:8]}.../clips  seq={seq}  audio={audio_path.name}")
        clip = post_clip(ingress, session_id, case_id, seq, clip_id, audio_path)
        if verbose:
            cid = (clip.get("clip_id") or "")[:8]
            print(f"    clip_id={cid}...  state={clip.get('state')}")

    if close:
        if verbose:
            print(f"  POST /sessions/{session_id[:8]}.../close")
        close_session(ingress, session_id)

    return session_id


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    ok = True
    print("== environment ==")
    if shutil.which("ffprobe"):
        print("  OK ffprobe on PATH")
    else:
        print("  !! ffprobe missing — brew install ffmpeg")
        ok = False

    print("== ingress ==")
    try:
        status, body, _ = _request("GET", f"{args.ingress}/healthz", timeout=2.0)
        if 200 <= status < 300:
            print(f"  OK {args.ingress}/healthz -> {status}")
        else:
            print(f"  !! {args.ingress}/healthz -> {status}: {body[:200]!r}")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"  !! {args.ingress} unreachable — {e}")
        ok = False

    print("== cases ==")
    cases_dir: Path = args.cases_dir
    if not cases_dir.is_dir():
        print(f"  !! cases dir missing: {cases_dir}")
        return 1
    case_dirs = sorted(d for d in cases_dir.iterdir() if (d / "case.yaml").is_file())
    if not case_dirs:
        print(f"  (no cases under {cases_dir})")
    for cd in case_dirs:
        clips_dir = cd / "clips"
        n_audio = sum(1 for _ in clips_dir.glob("*.mp3")) if clips_dir.is_dir() else 0
        n_scripts = sum(1 for _ in (cd / "scripts").glob("*.txt")) if (cd / "scripts").is_dir() else 0
        print(f"    - {cd.name}  scripts={n_scripts}  rendered_audio={n_audio}")

    return 0 if ok else 1


def cmd_load(args: argparse.Namespace) -> int:
    case_dir = args.cases_dir / args.case_id
    if not (case_dir / "case.yaml").is_file():
        print(f"no such case: {case_dir}", file=sys.stderr)
        return 1
    print(f"== loading {args.case_id} ==")
    try:
        session_id = load_case(args.ingress, case_dir, close=not args.no_close)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"\nloaded {args.case_id} -> session_id={session_id}")
    print(f"  open in PWA: {args.ingress}/#/open/{session_id}?tab=diff")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    cases_dir: Path = args.cases_dir
    case_dirs = sorted(d for d in cases_dir.iterdir() if (d / "case.yaml").is_file())
    if not case_dirs:
        print(f"no cases under {cases_dir}", file=sys.stderr)
        return 1
    loaded: list[tuple[str, str]] = []
    fails: list[str] = []
    for cd in case_dirs:
        print(f"== loading {cd.name} ==")
        try:
            sid = load_case(args.ingress, cd, close=not args.no_close)
            loaded.append((cd.name, sid))
            print(f"  -> session_id={sid}")
        except Exception as e:  # noqa: BLE001
            fails.append(f"{cd.name}: {e}")
            print(f"  FAIL: {e}", file=sys.stderr)
            if args.stop_on_error:
                break
    print(f"\n--- loaded {len(loaded)} / {len(case_dirs)}  failed={len(fails)} ---")
    for name, sid in loaded:
        print(f"  {name} -> {sid}")
    if fails:
        for f in fails:
            print(f"  ! {f}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Load rendered corpus cases into the running scribe-ingress.",
    )
    p.add_argument("--ingress", default=DEFAULT_INGRESS,
                   help="Ingress base URL (default: %(default)s)")
    p.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR,
                   help="Corpus cases directory (default: %(default)s)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("check", help="Verify environment + ingress + list cases")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("load", help="Load one case")
    sp.add_argument("case_id")
    sp.add_argument("--no-close", action="store_true",
                    help="Skip POST /sessions/{id}/close (leave session open)")
    sp.set_defaults(func=cmd_load)

    sp = sub.add_parser("all", help="Load every case")
    sp.add_argument("--no-close", action="store_true")
    sp.add_argument("--stop-on-error", action="store_true",
                    help="Abort the batch on the first failure")
    sp.set_defaults(func=cmd_all)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Render audio for corpus cases.

For each clip declared in case.yaml:

  1. POST the script text to the PAI voice server's /synthesize endpoint
     → voice mp3 (single-speaker dictation).
  2. Pick a background asset from test-corpus/backgrounds/, deterministically
     keyed by (case_seed, clip_index).
  3. Slice the background at a deterministic random offset matching the
     voice clip's duration.
  4. Mix voice + attenuated background slice at snr_db via ffmpeg.
  5. Write clips/NN.mp3 in the case directory.

Idempotent — skips a clip whose output mp3 is newer than both its
script.txt and the chosen background asset.

Dependencies
  - PAI voice server running (default: http://localhost:8888); see corpus
    doc §11.1.
  - ffmpeg + ffprobe on PATH (brew install ffmpeg).
  - pyyaml (Python).

Usage
  ./scripts/corpus-render.py check                    # verify env + assets
  ./scripts/corpus-render.py case <case_id>           # render one case
  ./scripts/corpus-render.py case <case_id> --force   # re-render unconditionally
  ./scripts/corpus-render.py all                      # render every case
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VOICE_SERVER = "http://localhost:8888"
DEFAULT_CORPUS_ROOT = REPO_ROOT / "test-corpus"
DEFAULT_CASES_DIR = DEFAULT_CORPUS_ROOT / "cases"
DEFAULT_BACKGROUNDS_DIR = DEFAULT_CORPUS_ROOT / "backgrounds"


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

def check_ffmpeg() -> tuple[bool, str]:
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg not on PATH — `brew install ffmpeg`"
    if not shutil.which("ffprobe"):
        return False, "ffprobe not on PATH (comes with ffmpeg)"
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    first = out.stdout.splitlines()[0] if out.stdout else "(no output)"
    return True, first


def check_voice_server(url: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(f"{url}/health")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "healthy":
                voices = ",".join(data.get("configured_voices") or [])
                return True, f"{url} healthy (voices: {voices})"
            return False, f"{url} responded but not healthy: {data}"
    except Exception as e:  # noqa: BLE001
        return False, f"{url} unreachable: {e}"


def check_synthesize_endpoint(url: str) -> tuple[bool, str]:
    """Confirm /synthesize specifically returns audio bytes (not just health)."""
    body = json.dumps({"message": "test"}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/synthesize", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            ctype = resp.headers.get("Content-Type", "")
            n = len(resp.read())
            if ctype.startswith("audio/") and n > 0:
                return True, f"/synthesize returns {ctype} ({n} bytes)"
            return False, f"/synthesize returned {ctype} ({n} bytes) — expected audio/*"
    except Exception as e:  # noqa: BLE001
        return False, f"/synthesize call failed: {e}"


# ---------------------------------------------------------------------------
# Audio pipeline
# ---------------------------------------------------------------------------

def synthesize_voice(voice_server: str, text: str, voice_id: str | None) -> bytes:
    payload: dict[str, Any] = {"message": text}
    if voice_id:
        payload["voice_id"] = voice_id
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{voice_server}/synthesize", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def probe_duration_seconds(mp3_path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(mp3_path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def mix_voice_and_background(
    voice_path: Path, bg_path: Path, offset_s: float, voice_dur_s: float,
    snr_db: float, out_path: Path,
) -> None:
    """Mix voice (input 0) + a slice of background (input 1) attenuated by
    snr_db. amix normalize=0 keeps unity gain on voice.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(voice_path),
        "-ss", f"{offset_s:.3f}", "-t", f"{voice_dur_s:.3f}",
        "-i", str(bg_path),
        "-filter_complex",
        f"[1:a]volume={snr_db}dB[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:normalize=0[out]",
        "-map", "[out]", "-ac", "1", "-ar", "22050",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


# ---------------------------------------------------------------------------
# Background selection
# ---------------------------------------------------------------------------

def derive_seed(case_seed: str, clip_index: int, key: str = "") -> int:
    """Stable 64-bit RNG seed from (case_seed, clip_index[, key])."""
    h = hashlib.sha256(f"{case_seed}:{clip_index}:{key}".encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def pick_background_for_voice(
    rng: random.Random, pool_assets: list[Path], voice_dur_s: float,
    bg_durations: dict[Path, float],
    override: dict[str, Any] | None,
) -> tuple[Path, float, bool]:
    """Pick a background asset suitable for a voice clip of `voice_dur_s`.

    Returns (chosen_path, chosen_duration, long_enough). Determinism: the
    function uses one rng.choice call regardless of branch, so the same
    (case_seed, clip_index) -> same RNG -> same pick given the same pool
    composition and voice duration.

    Selection order:
      1. If `override.asset` is set, honour it (the corpus author's pick wins).
      2. Otherwise, filter pool to assets long enough for the voice clip,
         and pick from the filtered list.
      3. If no asset is long enough, fall back to the longest available
         and surface that via the `long_enough=False` return.
    """
    if override and override.get("asset"):
        target = str(override["asset"])
        for p in pool_assets:
            if p.stem == target or p.name == target:
                return p, bg_durations[p], bg_durations[p] >= voice_dur_s
        raise RuntimeError(f"background_override asset '{target}' not in pool")

    eligible = sorted(
        (p for p in pool_assets if bg_durations[p] >= voice_dur_s),
        key=lambda p: p.name,
    )
    if eligible:
        chosen = rng.choice(eligible)
        return chosen, bg_durations[chosen], True

    longest = max(pool_assets, key=lambda p: bg_durations[p])
    return longest, bg_durations[longest], False


def needs_render(
    out_path: Path, script_path: Path, pool_assets: list[Path], force: bool,
) -> bool:
    """Returns True if `out_path` should be (re-)rendered.

    The bg pick depends on the voice duration (only known after synthesis),
    so we can't compare against a specific chosen-bg's mtime. We compare
    against the whole pool instead — any pool change forces a re-render,
    which is the conservatively-correct choice.
    """
    if force or not out_path.exists():
        return True
    mtime = out_path.stat().st_mtime
    if script_path.exists() and script_path.stat().st_mtime > mtime:
        return True
    for p in pool_assets:
        if p.stat().st_mtime > mtime:
            return True
    return False


# ---------------------------------------------------------------------------
# Per-case rendering
# ---------------------------------------------------------------------------

def render_case(
    case_dir: Path, voice_server: str, backgrounds_dir: Path,
    force: bool = False, verbose: bool = True,
) -> tuple[int, int]:
    case_yaml_path = case_dir / "case.yaml"
    if not case_yaml_path.is_file():
        raise FileNotFoundError(f"no case.yaml in {case_dir}")

    case_yaml = yaml.safe_load(case_yaml_path.read_text(encoding="utf-8"))
    case_id = case_yaml.get("case_id") or case_dir.name
    session = case_yaml.get("session") or {}
    audio_render = session.get("audio_render") or {}
    case_seed_str = str(audio_render.get("seed") or case_id)
    voice_id = audio_render.get("voice_id")
    default_snr = float(audio_render.get("snr_db", -18))

    if not backgrounds_dir.is_dir():
        raise FileNotFoundError(f"backgrounds dir missing: {backgrounds_dir}")
    pool_assets = sorted(backgrounds_dir.glob("*.mp3"))
    if not pool_assets:
        raise RuntimeError(f"no mp3 assets in {backgrounds_dir}")

    # Pre-probe pool durations once per case render. Cheap (one ffprobe per
    # asset) and avoids repeated probes across clips.
    bg_durations: dict[Path, float] = {p: probe_duration_seconds(p) for p in pool_assets}

    rendered = 0
    skipped = 0

    for clip_idx, clip in enumerate(session.get("clips") or [], start=1):
        script_rel = clip.get("script")
        audio_rel = clip.get("audio")
        if not script_rel or not audio_rel:
            print(f"  clip {clip_idx}: missing script/audio path — skipped", file=sys.stderr)
            continue
        script_path = case_dir / script_rel
        out_path = case_dir / audio_rel
        if not script_path.is_file():
            print(f"  clip {clip_idx}: script not found ({script_path}) — skipped", file=sys.stderr)
            continue

        # Skip BEFORE synthesising (TTS calls cost money). The bg pick now
        # happens after voice probe, so the up-to-date check compares against
        # the whole pool — any pool change forces a re-render.
        if not needs_render(out_path, script_path, pool_assets, force):
            if verbose:
                print(f"  clip {clip_idx}: up-to-date ({out_path.name})")
            skipped += 1
            continue

        text = script_path.read_text(encoding="utf-8").strip()
        if not text:
            print(f"  clip {clip_idx}: script empty — skipped", file=sys.stderr)
            continue

        if verbose:
            print(f"  clip {clip_idx}: synthesise ({len(text)} chars)")

        try:
            voice_mp3 = synthesize_voice(voice_server, text, voice_id)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"clip {clip_idx}: voice server HTTP {e.code}: {err_body}") from e
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"clip {clip_idx}: voice synthesis failed: {e}") from e

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as vt:
            voice_tmp = Path(vt.name)
            vt.write(voice_mp3)
        try:
            voice_dur_s = probe_duration_seconds(voice_tmp)

            # Now we know voice duration — pick a bg long enough for it.
            seed = derive_seed(case_seed_str, clip_idx)
            rng = random.Random(seed)
            override = clip.get("background_override")
            bg_path, bg_dur_s, long_enough = pick_background_for_voice(
                rng, pool_assets, voice_dur_s, bg_durations, override,
            )
            snr_db = float(override["snr_db"]) if override and "snr_db" in override else default_snr

            if long_enough:
                max_offset = bg_dur_s - voice_dur_s - 0.01
                offset_s = rng.uniform(0, max(0.0, max_offset))
            else:
                offset_s = 0.0
                if verbose:
                    print(
                        f"    (note: no bg >= {voice_dur_s:.1f}s voice — "
                        f"using longest {bg_path.name} @ {bg_dur_s:.1f}s)",
                        file=sys.stderr,
                    )

            if verbose:
                print(f"    mix with {bg_path.name} @ {snr_db:.1f} dB, offset {offset_s:.1f}s")

            mix_voice_and_background(
                voice_tmp, bg_path, offset_s, voice_dur_s, snr_db, out_path,
            )
            rendered += 1
        finally:
            voice_tmp.unlink(missing_ok=True)

    if verbose:
        print(f"  case {case_id}: rendered={rendered} skipped={skipped}")
    return rendered, skipped


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    ok = True
    print("== environment ==")
    good, msg = check_ffmpeg()
    print(f"  {'OK ' if good else '!! '}ffmpeg: {msg}")
    ok &= good
    good, msg = check_voice_server(args.voice_server)
    print(f"  {'OK ' if good else '!! '}voice server: {msg}")
    ok &= good
    good, msg = check_synthesize_endpoint(args.voice_server)
    print(f"  {'OK ' if good else '!! '}/synthesize: {msg}")
    ok &= good

    cases_dir: Path = args.cases_dir
    print("== cases ==")
    if cases_dir.is_dir():
        case_dirs = sorted(d for d in cases_dir.iterdir() if (d / "case.yaml").is_file())
        if case_dirs:
            for cd in case_dirs:
                clips = 0
                try:
                    cy = yaml.safe_load((cd / "case.yaml").read_text(encoding="utf-8")) or {}
                    clips = len(((cy.get("session") or {}).get("clips") or []))
                except Exception:  # noqa: BLE001
                    clips = -1
                print(f"  - {cd.name} ({clips} clip(s))")
        else:
            print(f"  (no case.yaml files under {cases_dir})")
    else:
        print(f"  (cases dir missing: {cases_dir})")

    bg_dir: Path = args.backgrounds_dir
    print("== backgrounds ==")
    if bg_dir.is_dir():
        mp3s = sorted(p.name for p in bg_dir.glob("*.mp3"))
        if mp3s:
            for n in mp3s:
                size = (bg_dir / n).stat().st_size // 1024
                print(f"  - {n} ({size} KB)")
        else:
            print(f"  (no mp3 assets in {bg_dir} — see corpus doc §8.3 for expected ids)")
            ok = False
    else:
        print(f"  (backgrounds dir missing: {bg_dir})")
        ok = False

    return 0 if ok else 1


def cmd_case(args: argparse.Namespace) -> int:
    case_dir: Path = args.cases_dir / args.case_id
    if not case_dir.is_dir():
        print(f"no such case directory: {case_dir}", file=sys.stderr)
        return 1
    print(f"== rendering {args.case_id} ==")
    try:
        render_case(case_dir, args.voice_server, args.backgrounds_dir, force=args.force)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    cases_dir: Path = args.cases_dir
    if not cases_dir.is_dir():
        print(f"cases dir missing: {cases_dir}", file=sys.stderr)
        return 1
    case_dirs = sorted(d for d in cases_dir.iterdir() if (d / "case.yaml").is_file())
    if not case_dirs:
        print(f"no cases under {cases_dir}", file=sys.stderr)
        return 1
    total_r = 0
    total_s = 0
    fails: list[str] = []
    for cd in case_dirs:
        print(f"== rendering {cd.name} ==")
        try:
            r, s = render_case(cd, args.voice_server, args.backgrounds_dir, force=args.force)
            total_r += r
            total_s += s
        except Exception as e:  # noqa: BLE001
            fails.append(f"{cd.name}: {e}")
            print(f"  FAIL: {e}", file=sys.stderr)
    print(f"--- total rendered={total_r} skipped={total_s} failed={len(fails)} ---")
    if fails:
        for f in fails:
            print(f"  ! {f}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Render audio for corpus cases via the PAI voice server + background mix.",
    )
    p.add_argument("--voice-server", default=DEFAULT_VOICE_SERVER,
                   help="PAI voice server URL (default: %(default)s)")
    p.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR,
                   help="Corpus cases directory (default: %(default)s)")
    p.add_argument("--backgrounds-dir", type=Path, default=DEFAULT_BACKGROUNDS_DIR,
                   help="Background asset pool directory (default: %(default)s)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("check", help="Verify environment + cases + backgrounds")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("case", help="Render one case by id")
    sp.add_argument("case_id")
    sp.add_argument("--force", action="store_true", help="Re-render even if up-to-date")
    sp.set_defaults(func=cmd_case)

    sp = sub.add_parser("all", help="Render every case")
    sp.add_argument("--force", action="store_true", help="Re-render even if up-to-date")
    sp.set_defaults(func=cmd_all)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

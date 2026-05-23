#!/usr/bin/env python3
"""render.py — CLI for the scribea voice renderer.

Talks to a running ./server.ts (default http://localhost:8889) and writes
the synthesised audio to a file. Designed to be called from corpus build
scripts one clip at a time per docs/scribe-test-corpus.md §11.

Usage:
    ./render.py --text "Mr Patel admitted overnight..." \
                --persona VMO_A --out clip_1.mp3
    ./render.py --file dialog.txt --persona REG_A --out clip_1.mp3
    ./render.py --text "..." --persona VMO_B --out clip_1.wav \
                --convert-wav   # post-converts mp3→16kHz mono WAV via ffmpeg

If --persona is omitted, the server's default persona (from voices.json)
is used.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


DEFAULT_URL = os.environ.get("SCRIBEA_VOICE_URL", "http://localhost:8889/synthesize")
DEFAULT_TIMEOUT = float(os.environ.get("SCRIBEA_VOICE_TIMEOUT", "60"))


def synthesize(text: str, persona: str | None, url: str, timeout: float) -> bytes:
    body = {"message": text}
    if persona:
        body["persona"] = persona
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ct = resp.headers.get("Content-Type", "")
        data = resp.read()
        if not ct.startswith("audio/"):
            # Server returned an error JSON instead of audio.
            sys.stderr.write(f"server returned {ct}: {data.decode('utf-8', errors='replace')[:500]}\n")
            sys.exit(2)
        return data


def mp3_to_wav(mp3_path: str, wav_path: str) -> None:
    """16 kHz mono s16le WAV — matches scribe-audio-preprocess's output target."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", mp3_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
         wav_path],
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="Text to synthesise (inline).")
    src.add_argument("--file", help="Path to a text file containing the script.")
    ap.add_argument("--persona", help="Persona handle from voices.json (e.g. VMO_A). Default: server's default_persona.")
    ap.add_argument("--out", required=True, help="Output file path (.mp3 by default; use --convert-wav for .wav).")
    ap.add_argument("--convert-wav", action="store_true",
                    help="Post-process the mp3 to 16 kHz mono WAV via ffmpeg.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Renderer endpoint (default: {DEFAULT_URL}).")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).")
    args = ap.parse_args()

    text = args.text if args.text is not None else open(args.file, "r", encoding="utf-8").read()
    text = text.strip()
    if not text:
        sys.stderr.write("empty text\n"); return 2

    try:
        audio = synthesize(text, args.persona, args.url, args.timeout)
    except urllib.error.URLError as e:
        sys.stderr.write(f"could not reach {args.url}: {e}\n"); return 3

    if args.convert_wav:
        # Write the mp3 to a temp path next to --out, convert, then drop the mp3.
        tmp_mp3 = args.out + ".rendering.mp3"
        with open(tmp_mp3, "wb") as f:
            f.write(audio)
        try:
            mp3_to_wav(tmp_mp3, args.out)
        finally:
            try: os.remove(tmp_mp3)
            except OSError: pass
    else:
        with open(args.out, "wb") as f:
            f.write(audio)

    sys.stderr.write(f"wrote {args.out} ({len(audio)}B mp3 source)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

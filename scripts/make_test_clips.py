#!/usr/bin/env python3
"""Generate small synthetic clips (video + tone) for testing the editor.

    python scripts/make_test_clips.py --out /tmp/demo --count 9

Clips deliberately differ in resolution, frame rate and brightness, and one has no
audio, so the normalisation code paths are exercised.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

PATTERNS = ["testsrc2", "rgbtestsrc", "smptebars", "gradients", "testsrc"]
SIZES = [(640, 360), (640, 360), (480, 270), (640, 360), (320, 240)]
RATES = [24, 24, 30, 25, 24]


def make_clip(path: Path, duration: float = 3.0, size=(640, 360), fps: int = 24,
              source: str = "testsrc2", audio: bool = True, freq: int = 440,
              eq: Optional[str] = None) -> Path:
    """Create one clip. `source` is any lavfi video source or 'color=c=navy'."""
    w, h = size
    src = source if "=" in source else f"{source}=s={w}x{h}:r={fps}:d={duration}"
    if source.startswith("color="):
        src = f"{source}:s={w}x{h}:r={fps}:d={duration}"
    cmd: List[str] = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-f", "lavfi", "-i", src]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f={freq}:d={duration}"]
    if eq:
        cmd += ["-vf", eq]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(fps)]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-shortest"]
    cmd.append(str(path))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed creating {path}:\n{res.stderr}")
    return path


def make_set(out: Path, count: int, duration: float = 3.0) -> List[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(1, count + 1):
        k = (i - 1) % len(PATTERNS)
        paths.append(make_clip(out / f"{i}.mp4", duration, SIZES[k], RATES[k], PATTERNS[k],
                               audio=(i != 3), freq=220 + 40 * i))
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("videos"))
    ap.add_argument("--count", type=int, default=9)
    ap.add_argument("--duration", type=float, default=3.0)
    a = ap.parse_args(argv)
    for p in make_set(a.out, a.count, a.duration):
        print("created", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

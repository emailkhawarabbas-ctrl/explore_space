#!/usr/bin/env python3
"""Download clips from external storage so large videos need not live in Git.

Supported sources (HTTP/HTTPS only, standard library only):

  * a .zip archive containing numbered clips (1.mp4, 2.mp4, ...)
  * a .txt file with one URL per line (a plain list of direct clip URLs)
  * a single direct video URL

    python scripts/fetch_clips.py --url https://example.com/clips.zip --dest videos

Works with any direct/pre-signed link: GitHub release assets, S3/R2/GCS pre-signed
URLs, Dropbox (?dl=1), your own server. Google Drive "share" pages are NOT direct
links; use rclone or a pre-signed storage URL instead.

Never print the URL (it may contain a token); only file names are logged.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import List

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
NUMBERED = re.compile(r"^\d+\.(mp4|mov|mkv|webm)$", re.I)


def download(url: str, dest: Path, retries: int = 3, headers: dict | None = None) -> Path:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are supported")
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "flow-video-editor"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1 << 20)
            return dest
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            print(f"  download attempt {attempt}/{retries} failed: {type(exc).__name__}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"Download failed after {retries} attempts: {last}")


def extract_zip(archive: Path, dest: Path) -> List[Path]:
    """Extract only numbered video files, flattened (blocks path traversal)."""
    out: List[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = Path(member.filename).name
            if member.is_dir() or not NUMBERED.match(name):
                continue
            target = dest / name
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            out.append(target)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="URL of a .zip, a .txt URL list, or a single video")
    ap.add_argument("--dest", type=Path, default=Path("videos"))
    a = ap.parse_args(argv)
    a.dest.mkdir(parents=True, exist_ok=True)
    path_part = urllib.parse.urlparse(a.url).path
    suffix = Path(path_part).suffix.lower()
    fetched: List[Path] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if suffix == ".zip":
            print("Downloading clip archive...")
            archive = download(a.url, tmp / "clips.zip")
            fetched = extract_zip(archive, a.dest)
        elif suffix == ".txt":
            lst = download(a.url, tmp / "urls.txt").read_text().split()
            urls = [u for u in lst if u.strip() and not u.startswith("#")]
            for i, u in enumerate(urls, 1):
                name = Path(urllib.parse.urlparse(u).path).name
                if not NUMBERED.match(name):
                    name = f"{i}.mp4"
                print(f"Downloading {name} ({i}/{len(urls)})...")
                fetched.append(download(u, a.dest / name))
        elif suffix in VIDEO_EXTS:
            name = Path(path_part).name if NUMBERED.match(Path(path_part).name) else "1" + suffix
            print(f"Downloading {name}...")
            fetched.append(download(a.url, a.dest / name))
        else:
            print("Unsupported URL type: expected .zip, .txt or a video file extension.", file=sys.stderr)
            return 2
    if not fetched:
        print("No numbered clips were found in the download.", file=sys.stderr)
        return 1
    print(f"Fetched {len(fetched)} clip(s) into {a.dest}: " + ", ".join(sorted(p.name for p in fetched)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

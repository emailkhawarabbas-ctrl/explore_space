#!/usr/bin/env python3
"""Print a Markdown summary of render_report.json (used for the GitHub job summary)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0] if argv else "output/render_report.json")
    if not path.is_file():
        print("### Render report\n\nNo report was produced (the render probably failed early; see the log).")
        return 0
    rep = json.loads(path.read_text(encoding="utf-8"))
    out = rep.get("output_info", {})
    print("### Render report\n")
    if out:
        print(f"- **Result:** `{Path(rep.get('output', '')).name}`  ·  {out.get('duration')} s  ·  "
              f"{out.get('resolution')} @ {out.get('fps')} fps  ·  {out.get('video_codec')}/{out.get('audio_codec')}  ·  "
              f"{out.get('size_mb')} MB")
    print(f"- **Clips used:** {len(rep.get('clips', []))}  ·  **Render time:** {rep.get('render_seconds', '?')} s")
    if rep.get("warnings"):
        print("\n**Warnings**\n")
        for w in rep["warnings"]:
            print(f"- {w}")
    if rep.get("transitions"):
        print("\n| From | To | Transition | Seconds |\n|---|---|---|---|")
        for t in rep["transitions"]:
            print(f"| {t['from']} | {t['to']} | {t['type']} | {t['duration']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Google Flow Video Editor
========================

Combines numbered clips (1.mp4, 2.mp4, ... 10.mp4) into one edited video using
FFmpeg/FFprobe, orchestrated from Python. Designed to run locally or in
GitHub Actions. Uses only the Python standard library.

Pipeline
--------
 1. Discover numbered clips and sort them NUMERICALLY.
 2. Probe + validate every clip (ffprobe, optional full decode test).
 3. Pick a target resolution / frame rate (configured or most common).
 4. Measure each clip's brightness, saturation and loudness (FFmpeg
    signalstats / ebur128) and derive gentle matching corrections.
 5. Normalise every clip to the same size, fps, pixel format and audio format.
 6. Compare the last frames of clip N with the first frames of clip N+1
    (32x32 thumbnails: pixel difference, colour histogram, brightness jump,
    motion) and choose a transition per boundary with transparent rules.
 7. Join clips with xfade / acrossfade in a streaming FFmpeg filter graph
    (in chunks for very large clip counts), mix background music with
    optional ducking, apply fades, titles and loudness normalisation.
 8. Write the MP4, a text log and a JSON report.

NOTE ON "INTELLIGENCE": the analysis is signal-level (pixel / colour /
loudness statistics). It does NOT understand what is happening in a scene.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.0.0"
LOG = logging.getLogger("flow_editor")
LOG.addHandler(logging.StreamHandler(sys.stderr))
LOG.setLevel(logging.INFO)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: Dict[str, Any] = {
    "paths": {"input_dir": "videos", "output_dir": "output", "music_dir": "music",
              "temp_dir": None, "log_file": "render.log"},
    "input": {"extensions": [".mp4", ".mov", ".mkv", ".webm"], "deep_validate": True,
              "on_bad_clip": "skip", "min_clip_duration": 0.5},
    "output": {"filename": "final_video.mp4", "overwrite": True, "resolution": "auto",
               "fps": "auto", "fit_mode": "pad", "video_codec": "libx264", "crf": 18,
               "preset": "slow", "pix_fmt": "yuv420p", "audio_codec": "aac",
               "audio_bitrate": "256k", "audio_sample_rate": 48000},
    "transitions": {
        "enabled": True, "mode": "auto", "duration": 0.8, "fixed_type": "fade",
        "allowed": ["fade", "fadeblack", "dissolve", "fadegrays"], "allow_cuts": True,
        "analysis": {"cut_threshold": 0.05, "similar_threshold": 0.20,
                     "different_threshold": 0.45, "luma_jump_threshold": 0.25,
                     "motion_threshold": 0.10, "motion_duration_factor": 0.6,
                     "max_fraction_of_clip": 0.45},
        "overrides": {}},
    "color_correction": {"enabled": True, "match_clips": True, "strength": 0.6, "look": "neutral"},
    "audio": {"keep_original": True, "original_volume": 1.0, "normalize_clips": True,
              "highpass_hz": 60, "denoise": False, "fade_in": 1.0, "fade_out": 2.0,
              "final_loudnorm": True, "final_lufs": -16},
    "music": {"enabled": True, "file": None, "volume": 0.18, "ducking": True, "loop": True,
              "fade_in": 2.0, "fade_out": 3.0,
              "extensions": [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"]},
    "fades": {"enabled": True, "video_fade_in": 1.0, "video_fade_out": 1.5},
    "intro_outro": {"intro": {"enabled": False, "file": "intro_outro/intro.mp4"},
                    "outro": {"enabled": False, "file": "intro_outro/outro.mp4"},
                    "transition": "fadeblack", "duration": 1.0},
    "titles": {"font_file": None,
               "title": {"enabled": False, "text": "", "start": 1.0, "duration": 4.0,
                         "fade": 0.8, "fontsize_ratio": 0.07, "color": "white",
                         "position": "center"},
               "credits": {"enabled": False, "lines": [], "duration": 6.0,
                           "fontsize_ratio": 0.045, "color": "white"}},
    "performance": {"max_clips_per_pass": 10, "threads": 0, "intermediate_crf": 12,
                    "intermediate_preset": "veryfast"},
}

FREE_FORM_KEYS = {"transitions.overrides"}  # dicts whose keys are user-defined
XFADE_ALLOWED = {"fade", "fadeblack", "fadewhite", "fadegrays", "dissolve", "distance",
                 "smoothleft", "smoothright", "smoothup", "smoothdown", "circleopen",
                 "circleclose", "radial", "pixelize", "wipeleft", "wiperight"}
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


class ConfigError(ValueError):
    """Invalid configuration."""


class InputError(RuntimeError):
    """Problem with input media."""


class FFmpegError(RuntimeError):
    """An FFmpeg / FFprobe invocation failed."""


class Cfg:
    """Read-only attribute view over a nested dict (cfg.output.crf)."""

    def __init__(self, data: Dict[str, Any]):
        self._d = data

    def __getattr__(self, key: str) -> Any:
        try:
            value = self._d[key]
        except KeyError:
            raise AttributeError(key) from None
        return Cfg(value) if isinstance(value, dict) else value


def deep_merge(base: Dict[str, Any], override: Dict[str, Any], path: str = "") -> Dict[str, Any]:
    out = dict(base)
    for key, val in override.items():
        here = f"{path}.{key}" if path else key
        if key not in base:
            if path in FREE_FORM_KEYS:
                out[key] = val
            else:
                LOG.warning("Unknown config key ignored: %s", here)
            continue
        if isinstance(base[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(base[key], val, here)
        else:
            out[key] = val
    return out


def set_dotted(conf: Dict[str, Any], dotted: str) -> None:
    """Apply a --set key.sub=value override (value parsed as JSON when possible)."""
    if "=" not in dotted:
        raise ConfigError(f"--set expects key=value, got: {dotted!r}")
    key, raw = dotted.split("=", 1)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    node = conf
    parts = key.strip().split(".")
    for p in parts[:-1]:
        if p not in node or not isinstance(node[p], dict):
            raise ConfigError(f"Unknown config section in --set: {key}")
        node = node[p]
    if parts[-1] not in node and ".".join(parts[:-1]) not in FREE_FORM_KEYS:
        raise ConfigError(f"Unknown config key in --set: {key}")
    node[parts[-1]] = value


def load_config(path: Optional[Path]) -> Tuple[Dict[str, Any], Path]:
    """Return (merged config, base directory for relative paths)."""
    conf = json.loads(json.dumps(DEFAULT_CONFIG))
    base = Path.cwd()
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config is not valid JSON ({path}): {exc}") from exc
        conf = deep_merge(conf, user)
        base = path.resolve().parent
    return conf, base


def parse_resolution(value: Any) -> Optional[Tuple[int, int]]:
    """'auto' -> None; '1920x1080' / [1920,1080] -> (w, h) forced even."""
    if value is None or (isinstance(value, str) and value.lower() == "auto"):
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        w, h = int(value[0]), int(value[1])
    elif isinstance(value, str) and re.fullmatch(r"\d+\s*[xX]\s*\d+", value.strip()):
        w, h = (int(p) for p in re.split(r"[xX]", value.strip()))
    else:
        raise ConfigError(f"output.resolution must be 'auto' or 'WIDTHxHEIGHT', got {value!r}")
    if w < 16 or h < 16:
        raise ConfigError("output.resolution is too small")
    return w - w % 2, h - h % 2


def snap_fps(fps: float) -> Fraction:
    """Map measured frame rates onto standard broadcast values."""
    for known, frac in ((23.976, Fraction(24000, 1001)), (29.97, Fraction(30000, 1001)),
                        (59.94, Fraction(60000, 1001))):
        if abs(fps - known) < 0.02:
            return frac
    if abs(fps - round(fps)) < 0.02:
        return Fraction(round(fps), 1)
    return Fraction(fps).limit_denominator(1001)


def parse_fps(value: Any) -> Optional[Fraction]:
    if value is None or (isinstance(value, str) and value.lower() == "auto"):
        return None
    try:
        frac = snap_fps(float(Fraction(str(value))))
    except (ValueError, ZeroDivisionError):
        raise ConfigError(f"output.fps must be 'auto' or a number, got {value!r}") from None
    if not (1 <= frac <= 120):
        raise ConfigError("output.fps must be between 1 and 120")
    return frac


def validate_config(conf: Dict[str, Any]) -> None:
    c = Cfg(conf)
    problems: List[str] = []
    if c.input.on_bad_clip not in ("skip", "abort"):
        problems.append("input.on_bad_clip must be 'skip' or 'abort'")
    if c.output.fit_mode not in ("pad", "crop"):
        problems.append("output.fit_mode must be 'pad' or 'crop'")
    if not (0 <= c.output.crf <= 51):
        problems.append("output.crf must be 0-51")
    if c.transitions.mode not in ("auto", "fixed"):
        problems.append("transitions.mode must be 'auto' or 'fixed'")
    if not (0.05 <= c.transitions.duration <= 5):
        problems.append("transitions.duration must be 0.05-5 seconds")
    bad = (set(c.transitions.allowed) | {c.transitions.fixed_type, c.intro_outro.transition}) - XFADE_ALLOWED
    if bad:
        problems.append(f"unsupported transition names: {sorted(bad)}")
    if c.performance.max_clips_per_pass < 2:
        problems.append("performance.max_clips_per_pass must be >= 2")
    if not (0 <= c.color_correction.strength <= 1):
        problems.append("color_correction.strength must be 0-1")
    if c.color_correction.look not in ("neutral", "cinematic"):
        problems.append("color_correction.look must be 'neutral' or 'cinematic'")
    for name in ("original_volume",):
        if getattr(c.audio, name) < 0:
            problems.append(f"audio.{name} must be >= 0")
    if c.music.volume < 0:
        problems.append("music.volume must be >= 0")
    try:
        parse_resolution(c.output.resolution)
        parse_fps(c.output.fps)
    except ConfigError as exc:
        problems.append(str(exc))
    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


def resolve_path(p: Any, base: Path) -> Path:
    path = Path(str(p)).expanduser()
    return path if path.is_absolute() else (base / path)


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #

def run_capture(cmd: List[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    LOG.debug("$ %s", shlex.join(str(c) for c in cmd))
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"Command timed out after {timeout}s: {cmd[0]}") from exc


def tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_video: bool
    has_audio: bool
    vcodec: str = ""
    acodec: str = ""
    sar: float = 1.0
    rotation: int = 0
    size_bytes: int = 0


def _parse_ratio(text: str, default: float = 0.0) -> float:
    try:
        if ":" in text:
            a, b = text.split(":")
        else:
            a, b = (text.split("/") + ["1"])[:2]
        return float(a) / float(b) if float(b) else default
    except (ValueError, ZeroDivisionError):
        return default


def probe_media(path: Path) -> MediaInfo:
    res = run_capture(["ffprobe", "-v", "error", "-print_format", "json",
                       "-show_format", "-show_streams", str(path)], timeout=60)
    if res.returncode != 0:
        raise InputError(f"ffprobe cannot read {path.name}: {tail(res.stderr.decode(errors='replace'), 3)}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise InputError(f"ffprobe returned invalid JSON for {path.name}") from exc

    streams = data.get("streams", [])
    vs = [s for s in streams if s.get("codec_type") == "video"
          and not s.get("disposition", {}).get("attached_pic")]
    aus = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = data.get("format", {})

    duration = 0.0
    for cand in ((vs[0].get("duration") if vs else None), fmt.get("duration")):
        try:
            if cand not in (None, "N/A"):
                duration = float(cand)
                break
        except ValueError:
            continue

    info = MediaInfo(path=path, duration=duration, width=0, height=0, fps=0.0,
                     has_video=bool(vs), has_audio=bool(aus),
                     acodec=aus[0].get("codec_name", "") if aus else "",
                     size_bytes=int(fmt.get("size", 0) or 0))
    if vs:
        v = vs[0]
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        sar = _parse_ratio(v.get("sample_aspect_ratio", "1:1"), 1.0) or 1.0
        rot = 0
        try:
            rot = int(float(v.get("tags", {}).get("rotate", 0)))
        except (ValueError, TypeError):
            pass
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(sar - 1.0) > 0.01:
            w = int(round(w * sar))
        if abs(rot) % 180 == 90:
            w, h = h, w
        fps = _parse_ratio(v.get("avg_frame_rate", "0/0")) or _parse_ratio(v.get("r_frame_rate", "0/0"))
        info.width, info.height, info.fps = w, h, fps or 24.0
        info.vcodec, info.sar, info.rotation = v.get("codec_name", ""), sar, rot
    return info


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Clip:
    number: int
    path: Path
    info: MediaInfo
    yavg: Optional[float] = None
    satavg: Optional[float] = None
    lufs: Optional[float] = None
    correction: Dict[str, float] = field(default_factory=dict)
    gain_db: float = 0.0


@dataclass
class Segment:
    """A normalised intermediate file ready to be joined."""
    path: Path
    duration: float
    kind: str = "clip"            # clip | intro | outro | credits | chunk
    number: Optional[int] = None


@dataclass
class BoundaryStats:
    pixel_diff: float
    hist_diff: float
    luma_delta: float
    motion_out: float
    motion_in: float

    @property
    def score(self) -> float:
        return 0.5 * self.pixel_diff + 0.5 * self.hist_diff


@dataclass
class Transition:
    type: str
    duration: float
    reason: str = ""

    @property
    def xfade_name(self) -> str:
        return "fade" if self.type == "cut" else self.type


# --------------------------------------------------------------------------- #
# Clip discovery
# --------------------------------------------------------------------------- #

# "7", "07", "scene07", "scene07_the_tap", "clip-3", "shot 12 wide"
CLIP_NAME_RE = re.compile(r"(?:scene|clip|shot)?[ _-]?(\d+)(?:[ _-].*)?", re.IGNORECASE)


def clip_number(stem: str) -> Optional[int]:
    m = CLIP_NAME_RE.fullmatch(stem)
    return int(m.group(1)) if m else None


def discover_clips(input_dir: Path, extensions: List[str]) -> List[Tuple[int, Path]]:
    """Find files named <integer>.<ext> or scene<integer>[_label].<ext>; return
    them sorted by integer value.

    '10.mp4' sorts after '2.mp4'. Non-numeric names are ignored (logged).
    If two files share a number (1.mp4 and 01.mov) the first is kept.
    """
    if not input_dir.is_dir():
        raise InputError(f"Input directory does not exist: {input_dir}")
    exts = {e.lower() for e in extensions}
    found: Dict[int, Path] = {}
    ignored: List[str] = []
    for p in sorted(input_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        n = clip_number(p.stem) if p.suffix.lower() in exts else None
        if n is not None:
            if n in found:
                LOG.warning("Duplicate clip number %d: keeping %s, ignoring %s", n, found[n].name, p.name)
            else:
                found[n] = p
        else:
            ignored.append(p.name)
    if ignored:
        LOG.info("Ignored non-clip files: %s", ", ".join(ignored))
    ordered = sorted(found.items())
    if ordered:
        nums = [n for n, _ in ordered]
        gaps = sorted(set(range(nums[0], nums[-1] + 1)) - set(nums))
        if gaps:
            LOG.warning("Gaps in clip numbering (missing: %s). Continuing with available clips.",
                        ", ".join(map(str, gaps)))
    return ordered


# --------------------------------------------------------------------------- #
# Editor
# --------------------------------------------------------------------------- #

class VideoEditor:
    def __init__(self, conf: Dict[str, Any], base_dir: Path, dry_run: bool = False,
                 keep_temp: bool = False):
        self.conf = conf
        self.cfg = Cfg(conf)
        self.base = base_dir
        self.dry_run = dry_run
        self.keep_temp = keep_temp
        self.tmp: Optional[Path] = None
        self.filters: set = set()
        self.report: Dict[str, Any] = {"version": __version__, "warnings": []}
        self.font: Optional[str] = None
        self.target_w = self.target_h = 0
        self.target_fps = Fraction(24, 1)

    # ---- environment ----------------------------------------------------- #

    def check_tools(self) -> None:
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise FFmpegError(f"{tool} not found on PATH. Install FFmpeg first.")
        ver = run_capture(["ffmpeg", "-version"]).stdout.decode(errors="replace").splitlines()[0]
        LOG.info("Using %s", ver)
        out = run_capture(["ffmpeg", "-hide_banner", "-filters"]).stdout.decode(errors="replace")
        self.filters = {parts[1] for parts in (l.split() for l in out.splitlines()) if len(parts) > 2}
        for required in ("xfade", "acrossfade"):
            if required not in self.filters:
                raise FFmpegError(f"Your FFmpeg lacks the '{required}' filter (need FFmpeg >= 4.3).")
        for optional, what in (("signalstats", "colour matching"), ("ebur128", "loudness matching"),
                               ("drawtext", "title/credits text"),
                               ("sidechaincompress", "music ducking")):
            if optional not in self.filters:
                self._warn(f"FFmpeg filter '{optional}' unavailable: {what} will be skipped.")

    def _warn(self, msg: str) -> None:
        LOG.warning(msg)
        self.report["warnings"].append(msg)

    def p(self, key: str) -> Path:
        return resolve_path(getattr(self.cfg.paths, key), self.base)

    # ---- running FFmpeg --------------------------------------------------- #

    def run_ffmpeg(self, args: List[Any], desc: str, expected: Optional[float] = None) -> None:
        cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-progress", "pipe:1", "-nostats"]
        threads = self.cfg.performance.threads
        if threads:
            cmd += ["-threads", str(threads)]
        cmd += [str(a) for a in args]
        LOG.debug("$ %s", shlex.join(cmd))
        started = time.time()
        next_mark = 0.25
        with tempfile.TemporaryFile(mode="w+") as errf:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if expected and expected >= 20 and line.startswith("out_time_us="):
                        try:
                            done = int(line.split("=", 1)[1]) / 1_000_000 / expected
                        except ValueError:
                            continue
                        if done >= next_mark:
                            LOG.info("  %s: %3d%%", desc, min(int(done * 100), 99))
                            while next_mark <= done:
                                next_mark += 0.25
                rc = proc.wait()
            errf.seek(0)
            err = errf.read()
        if rc != 0:
            LOG.error("FFmpeg failed during '%s' (exit %s):\n%s", desc, rc, tail(err, 25))
            raise FFmpegError(f"FFmpeg failed during '{desc}' (exit {rc}): {tail(err, 6)}")
        LOG.debug("%s finished in %.1fs", desc, time.time() - started)

    # ---- step 1/2: discovery & validation --------------------------------- #

    def collect_clips(self) -> List[Clip]:
        in_dir = self.p("input_dir")
        found = discover_clips(in_dir, self.cfg.input.extensions)
        if not found:
            raise InputError(f"No numbered clips (1.mp4, 2.mp4, ... or scene01_name.mp4, ...) found in {in_dir}")
        LOG.info("Found %d numbered clip(s) in %s: %s", len(found), in_dir,
                 ", ".join(p.name for _, p in found))
        clips: List[Clip] = []
        for num, path in found:
            try:
                info = self.validate_media(path)
            except (InputError, FFmpegError) as exc:
                if self.cfg.input.on_bad_clip == "abort":
                    raise InputError(f"Clip {path.name} is unusable: {exc}") from exc
                self._warn(f"Skipping {path.name}: {exc}")
                continue
            LOG.info("  %-8s %5.2fs  %dx%d @ %.3f fps  audio=%s  codec=%s", path.name, info.duration,
                     info.width, info.height, info.fps, "yes" if info.has_audio else "no", info.vcodec)
            clips.append(Clip(number=num, path=path, info=info))
        if not clips:
            raise InputError("All clips were unreadable. Nothing to render.")
        return clips

    def validate_media(self, path: Path, deep: Optional[bool] = None) -> MediaInfo:
        info = probe_media(path)
        if not info.has_video or info.width <= 0 or info.height <= 0:
            raise InputError("no usable video stream")
        if info.duration < self.cfg.input.min_clip_duration:
            raise InputError(f"duration {info.duration:.2f}s is shorter than input.min_clip_duration")
        if self.cfg.input.deep_validate if deep is None else deep:
            res = run_capture(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path),
                               "-f", "null", "-"], timeout=600)
            if res.returncode != 0:
                raise InputError("decode test failed: " + tail(res.stderr.decode(errors="replace"), 2))
        return info

    # ---- step 3: target format --------------------------------------------- #

    def pick_target(self, clips: List[Clip]) -> None:
        forced = parse_resolution(self.cfg.output.resolution)
        if forced:
            self.target_w, self.target_h = forced
        else:
            common = Counter((c.info.width - c.info.width % 2, c.info.height - c.info.height % 2)
                             for c in clips)
            best = max(common.items(), key=lambda kv: (kv[1], kv[0][0] * kv[0][1]))[0]
            self.target_w, self.target_h = best
        fps = parse_fps(self.cfg.output.fps)
        if fps is None:
            common_fps = Counter(snap_fps(c.info.fps) for c in clips)
            fps = max(common_fps.items(), key=lambda kv: (kv[1], -abs(float(kv[0]) - 24)))[0]
        self.target_fps = fps
        LOG.info("Target format: %dx%d @ %s fps (fit mode: %s)", self.target_w, self.target_h,
                 f"{float(fps):.3f}".rstrip("0").rstrip("."), self.cfg.output.fit_mode)

    @property
    def fps_str(self) -> str:
        return f"{self.target_fps.numerator}/{self.target_fps.denominator}"

    # ---- step 4: measurement ----------------------------------------------- #

    def measure_color(self, clip: Clip) -> None:
        if "signalstats" not in self.filters:
            return
        stats = self.tmp / f"stats_{clip.number}.txt"
        res = run_capture(["ffmpeg", "-v", "error", "-nostdin", "-i", str(clip.path), "-an",
                           "-vf", f"fps=2,scale=320:-2,signalstats,metadata=mode=print:file={stats}",
                           "-f", "null", "-"], timeout=300)
        if res.returncode != 0 or not stats.exists():
            LOG.debug("colour analysis failed for %s", clip.path.name)
            return
        ys: List[float] = []
        ss: List[float] = []
        for line in stats.read_text(errors="replace").splitlines():
            if line.startswith("lavfi.signalstats.YAVG="):
                ys.append(float(line.split("=")[1]))
            elif line.startswith("lavfi.signalstats.SATAVG="):
                ss.append(float(line.split("=")[1]))
        if ys:
            clip.yavg = statistics.fmean(ys)
        if ss:
            clip.satavg = statistics.fmean(ss)

    def measure_loudness(self, clip: Clip) -> None:
        if "ebur128" not in self.filters or not clip.info.has_audio:
            return
        res = run_capture(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(clip.path),
                           "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"], timeout=300)
        text = res.stderr.decode(errors="replace")
        summary = text.rsplit("Summary:", 1)[-1]
        m = re.search(r"I:\s+(-?\d+(?:\.\d+)?|-inf)\s+LUFS", summary)
        if m and m.group(1) != "-inf":
            lufs = float(m.group(1))
            clip.lufs = lufs if lufs > -60 else None   # below -60 LUFS: effectively silent

    def compute_corrections(self, clips: List[Clip]) -> None:
        cc = self.cfg.color_correction
        if cc.enabled:
            ys = [c.yavg for c in clips if c.yavg is not None]
            sats = [c.satavg for c in clips if c.satavg]
            ty = statistics.median(ys) if ys else None
            ts = statistics.median(sats) if sats else None
            for c in clips:
                gamma, sat, contrast = 1.0, 1.0, 1.0
                if cc.match_clips and len(ys) >= 2 and c.yavg and ty:
                    y = max(0.03, min(0.97, c.yavg / 255))
                    t = max(0.03, min(0.97, ty / 255))
                    g = math.log(y) / math.log(t)
                    if 0.75 <= g <= 1.33:       # moderate drift -> match it
                        gamma = 1 + (max(0.85, min(1.18, g)) - 1) * cc.strength
                    else:                        # big difference -> probably intentional (night vs day)
                        LOG.debug("clip %s brightness differs strongly from the median; left untouched", c.number)
                    if ts and c.satavg:
                        s_raw = ts / c.satavg
                        if 0.7 <= s_raw <= 1.4:
                            sat = 1 + (max(0.85, min(1.2, s_raw)) - 1) * cc.strength
                if cc.look == "cinematic":
                    contrast, sat = 1.04, sat * 1.04
                if abs(gamma - 1) > 0.015 or abs(sat - 1) > 0.015 or contrast != 1.0:
                    c.correction = {"gamma": round(gamma, 4), "saturation": round(sat, 4),
                                    "contrast": contrast}
        if self.cfg.audio.normalize_clips and self.cfg.audio.keep_original:
            loud = [c.lufs for c in clips if c.lufs is not None]
            if len(loud) >= 2:
                ref = statistics.median(loud)
                for c in clips:
                    if c.lufs is not None:
                        gain = max(-6.0, min(6.0, ref - c.lufs))
                        c.gain_db = round(gain, 2) if abs(gain) >= 0.5 else 0.0
        for c in clips:
            LOG.info("  clip %-3d  brightness=%s  sat=%s  loudness=%s  -> correction=%s  gain=%+.1f dB",
                     c.number, f"{c.yavg:.0f}" if c.yavg is not None else "n/a",
                     f"{c.satavg:.0f}" if c.satavg is not None else "n/a",
                     f"{c.lufs:.1f} LUFS" if c.lufs is not None else "silent/none",
                     c.correction or "none", c.gain_db)

    # ---- step 5: normalise -------------------------------------------------- #

    def _video_filter(self, info: MediaInfo, corr: Optional[Dict[str, float]]) -> str:
        w, h = self.target_w, self.target_h
        f = [f"fps={self.fps_str}"]
        if abs(info.sar - 1.0) > 0.01:
            f.append("scale='trunc(iw*sar/2)*2':ih,setsar=1")
        if corr:
            f.append(f"eq=gamma={corr['gamma']}:saturation={corr['saturation']}:contrast={corr['contrast']}")
        if self.cfg.output.fit_mode == "crop":
            f.append(f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}")
        else:
            f.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos")
            f.append(f"pad={w}:{h}:trunc((ow-iw)/4)*2:trunc((oh-ih)/4)*2:color=black")
        f += ["setsar=1", "format=yuv420p", "setpts=PTS-STARTPTS"]
        return ",".join(f)

    def _audio_filter(self, dur: float, gain_db: float) -> str:
        a = self.cfg.audio
        f = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if a.highpass_hz:
            f.append(f"highpass=f={a.highpass_hz}")
        if a.denoise and "afftdn" in self.filters:
            f.append("afftdn=nr=10:nf=-45")
        if gain_db:
            f.append(f"volume={gain_db}dB")
            if gain_db > 0:
                f.append("alimiter=limit=0.95")
        f += [f"apad=whole_dur={dur:.6f}", "asetpts=PTS-STARTPTS"]
        return ",".join(f)

    def _intermediate_args(self) -> List[str]:
        p = self.cfg.performance
        return ["-c:v", "libx264", "-preset", p.intermediate_preset, "-crf", str(p.intermediate_crf),
                "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]

    def normalise(self, src: Path, info: MediaInfo, out: Path, corr: Optional[Dict[str, float]] = None,
                  gain_db: float = 0.0, label: str = "") -> float:
        dur = info.duration
        use_audio = info.has_audio and self.cfg.audio.keep_original
        args: List[Any] = ["-i", src]
        if use_audio:
            a_src = "[0:a:0]"
        else:
            args += ["-f", "lavfi", "-t", f"{dur:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
            a_src = "[1:a]"
        graph = f"[0:v:0]{self._video_filter(info, corr)}[v];{a_src}{self._audio_filter(dur, gain_db)}[a]"
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.6f}",
                 *self._intermediate_args(), out]
        self.run_ffmpeg(args, f"normalise {label or src.name}", dur)
        real = probe_media(out).duration
        return real if real > 0 else dur

    def make_card(self, lines: List[str], duration: float, out: Path) -> float:
        """Black card with centred text lines (used for closing credits)."""
        t = self.cfg.titles.credits
        w, h = self.target_w, self.target_h
        fs = max(12, int(h * t.fontsize_ratio))
        step = int(fs * 1.7)
        top = (h - step * len(lines)) / 2
        chain = []
        for i, line in enumerate(lines):
            tf = self.tmp / f"credit_{i}.txt"
            tf.write_text(line, encoding="utf-8")
            chain.append(self._drawtext(tf, fs, t.color, "(w-text_w)/2", f"{top + i * step:.0f}"))
        vf = ",".join(chain + ["format=yuv420p"])
        args: List[Any] = ["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={self.fps_str}:d={duration:.3f}",
                           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", vf,
                           "-t", f"{duration:.3f}", *self._intermediate_args(), out]
        self.run_ffmpeg(args, "closing credits card", duration)
        return probe_media(out).duration

    def _drawtext(self, textfile: Path, fontsize: int, color: str, x: str, y: str,
                  alpha: Optional[str] = None) -> str:
        parts = [f"textfile='{textfile}'", f"fontsize={fontsize}", f"fontcolor={color}", f"x={x}", f"y={y}",
                 "shadowx=2", "shadowy=2", "shadowcolor=black@0.55", "expansion=none"]
        if self.font:
            parts.append(f"fontfile='{self.font}'")
        if alpha:
            parts.append(f"alpha='{alpha}'")
        return "drawtext=" + ":".join(parts)

    # ---- step 6: transitions ------------------------------------------------- #

    def grab_thumb(self, path: Path, t: float) -> Optional[bytes]:
        for tt in (t, t - 0.1, t - 0.25):
            res = run_capture(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(tt, 0):.3f}", "-i",
                               str(path), "-frames:v", "1", "-vf", "scale=32:32:flags=area,format=rgb24",
                               "-f", "rawvideo", "-"], timeout=60)
            if res.returncode == 0 and len(res.stdout) == 32 * 32 * 3:
                return res.stdout
        return None

    @staticmethod
    def _diff(a: bytes, b: bytes) -> float:
        return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255.0)

    @staticmethod
    def _hist_diff(a: bytes, b: bytes) -> float:
        tot = 0.0
        for ch in range(3):
            ha, hb = [0] * 16, [0] * 16
            for v in a[ch::3]:
                ha[v >> 4] += 1
            for v in b[ch::3]:
                hb[v >> 4] += 1
            n = len(a) / 3
            tot += 0.5 * sum(abs(x - y) for x, y in zip(ha, hb)) / n
        return tot / 3

    @staticmethod
    def _luma(a: bytes) -> float:
        r, g, b = a[0::3], a[1::3], a[2::3]
        n = len(r)
        return (0.299 * sum(r) + 0.587 * sum(g) + 0.114 * sum(b)) / (n * 255.0)

    def analyse_boundary(self, a: Segment, b: Segment) -> Optional[BoundaryStats]:
        a0 = self.grab_thumb(a.path, a.duration - 0.5)
        a1 = self.grab_thumb(a.path, a.duration - 0.08)
        b0 = self.grab_thumb(b.path, 0.0)
        b1 = self.grab_thumb(b.path, min(0.5, b.duration / 2))
        if not (a1 and b0):
            return None
        return BoundaryStats(pixel_diff=self._diff(a1, b0), hist_diff=self._hist_diff(a1, b0),
                             luma_delta=abs(self._luma(a1) - self._luma(b0)),
                             motion_out=self._diff(a0, a1) if a0 else 0.0,
                             motion_in=self._diff(b0, b1) if b1 else 0.0)

    def decide_transition(self, a: Segment, b: Segment, prev: Optional[Transition] = None) -> Transition:
        tc = self.cfg.transitions
        an = tc.analysis
        base = tc.duration
        cut_len = 2.0 / float(self.target_fps)
        allowed = list(tc.allowed) or ["fade"]

        def pick(name: str) -> str:
            return name if name in allowed else allowed[0]

        if not tc.enabled:
            return Transition("cut", cut_len, "transitions disabled")
        key = f"{a.number}-{b.number}" if a.number is not None and b.number is not None else None
        ov = self.conf["transitions"].get("overrides", {})
        if key and key in ov:
            o = ov[key]
            typ = o.get("type", "fade")
            if typ != "cut" and typ not in XFADE_ALLOWED:
                raise ConfigError(f"transitions.overrides[{key}]: unsupported type {typ!r}")
            return Transition(typ, float(o.get("duration", base)) if typ != "cut" else cut_len,
                              "manual override")
        if a.kind != "clip" or b.kind != "clip":
            io = self.cfg.intro_outro
            return Transition(io.transition, io.duration, f"{a.kind} -> {b.kind} boundary")
        if tc.mode == "fixed":
            return Transition(tc.fixed_type, base, "fixed mode")

        st = self.analyse_boundary(a, b)
        if st is None:
            return Transition(pick("fade"), base, "analysis unavailable, defaulting to cross dissolve")
        facts = (f"score={st.score:.2f} (pixel={st.pixel_diff:.2f}, hist={st.hist_diff:.2f}) "
                 f"luma_jump={st.luma_delta:.2f} motion={st.motion_out:.2f}/{st.motion_in:.2f}")
        if st.score < an.cut_threshold and st.luma_delta < 0.05:
            if tc.allow_cuts:
                return Transition("cut", cut_len, f"visually continuous: {facts}")
            return Transition(pick("fade"), min(base, 0.3), f"visually continuous (cuts off): {facts}")
        if st.luma_delta >= an.luma_jump_threshold or st.score >= an.different_threshold:
            if prev is not None and prev.type == "fadeblack":
                # variety guard: never dip to black twice in a row
                typ, dur, why = pick("fade"), base * 1.3, "strong scene change (previous cut already dipped to black)"
            else:
                typ, dur, why = pick("fadeblack"), base * 1.1, "strong scene change"
        elif st.score >= an.similar_threshold:
            typ, dur, why = pick("fade"), base * 1.25, "moderate change"
        else:
            typ, dur, why = pick("fade"), base, "related scenes"
        if max(st.motion_out, st.motion_in) > an.motion_threshold:
            dur = max(0.25, dur * an.motion_duration_factor)
            why += ", shortened (high motion at the cut, avoids hiding action)"
        return Transition(typ, round(dur, 3), f"{why}: {facts}")

    def limit_transitions(self, segs: List[Segment], trans: List[Transition]) -> None:
        frac = self.cfg.transitions.analysis.max_fraction_of_clip
        for i, t in enumerate(trans):
            t.duration = min(t.duration, frac * min(segs[i].duration, segs[i + 1].duration))
        for i, s in enumerate(segs):  # leave >= 0.5 s of unblended footage per clip
            prev = trans[i - 1].duration if i > 0 else 0.0
            nxt = trans[i].duration if i < len(trans) else 0.0
            room = max(s.duration - 0.5, 0.1)
            if prev + nxt > room:
                k = room / (prev + nxt)
                if i > 0:
                    trans[i - 1].duration *= k
                if i < len(trans):
                    trans[i].duration *= k
        for t in trans:
            t.duration = max(round(t.duration, 4), 0.04)

    # ---- step 7: join & post-process ------------------------------------------- #

    def find_font(self) -> Optional[str]:
        configured = self.cfg.titles.font_file
        if configured:
            fp = resolve_path(configured, self.base)
            if fp.is_file():
                return str(fp)
            self._warn(f"titles.font_file not found: {fp}")
        for cand in FONT_CANDIDATES:
            if Path(cand).is_file():
                return cand
        return None

    def find_music(self) -> Optional[Tuple[Path, float]]:
        m = self.cfg.music
        if not m.enabled:
            return None
        mdir = self.p("music_dir")
        chosen: Optional[Path] = None
        if m.file:
            for cand in (resolve_path(m.file, self.base), mdir / str(m.file)):
                if cand.is_file():
                    chosen = cand
                    break
            if not chosen:
                self._warn(f"music.file '{m.file}' not found; rendering without background music.")
                return None
        else:
            if not mdir.is_dir():
                LOG.info("No music directory (%s). Rendering without background music.", mdir)
                return None
            exts = {e.lower() for e in m.extensions}
            files = sorted(f for f in mdir.iterdir()
                           if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in exts)
            if not files:
                LOG.info("No music files in %s. Rendering without background music.", mdir)
                return None
            chosen = files[0]
            if len(files) > 1:
                LOG.warning("Multiple music files found; using %s (set music.file to choose).", chosen.name)
        try:
            info = probe_media(chosen)
        except InputError as exc:
            self._warn(f"Music file unreadable ({exc}); rendering without it.")
            return None
        if not info.has_audio or info.duration <= 1:
            self._warn(f"Music file {chosen.name} has no usable audio; rendering without it.")
            return None
        LOG.info("Background music: %s (%.1fs)", chosen.name, info.duration)
        return chosen, info.duration

    def build_graph(self, segs: List[Segment], trans: List[Transition], final: bool,
                    music: Optional[Tuple[Path, float]], has_signal: bool) -> Tuple[str, float]:
        parts: List[str] = []
        n = len(segs)
        for i in range(n):
            parts.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
            parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                         f"asetpts=PTS-STARTPTS[a{i}]")
        cur_v, cur_a, acc = "v0", "a0", segs[0].duration
        for i in range(1, n):
            t = trans[i - 1]
            off = max(acc - t.duration, 0.0)
            parts.append(f"[{cur_v}][v{i}]xfade=transition={t.xfade_name}:duration={t.duration:.4f}"
                         f":offset={off:.4f}[vx{i}]")
            parts.append(f"[{cur_a}][a{i}]acrossfade=d={t.duration:.4f}:c1=qsin:c2=qsin[ax{i}]")
            cur_v, cur_a = f"vx{i}", f"ax{i}"
            acc += segs[i].duration - t.duration
        total = acc

        if not final:
            parts.append(f"[{cur_v}]null[vout]")
            parts.append(f"[{cur_a}]anull[aout]")
            return ";".join(parts), total

        # ---- video post: fades + title --------------------------------------
        vchain: List[str] = []
        fd = self.cfg.fades
        if fd.enabled:
            fi = min(fd.video_fade_in, total / 4)
            fo = min(fd.video_fade_out, total / 4)
            if fi > 0:
                vchain.append(f"fade=t=in:st=0:d={fi:.3f}")
            if fo > 0:
                vchain.append(f"fade=t=out:st={total - fo:.3f}:d={fo:.3f}")
        ti = self.cfg.titles.title
        if ti.enabled and ti.text.strip() and "drawtext" in self.filters:
            tf = self.tmp / "title.txt"
            tf.write_text(ti.text.strip(), encoding="utf-8")
            s = ti.start
            e = min(ti.start + ti.duration, total)
            f_ = max(0.05, min(ti.fade, (e - s) / 2))
            alpha = (f"if(lt(t,{s}),0,if(lt(t,{s + f_}),(t-{s})/{f_},"
                     f"if(lt(t,{e - f_}),1,if(lt(t,{e}),({e}-t)/{f_},0))))")
            y = "(h-text_h)/2" if ti.position == "center" else "h*0.78"
            vchain.append(self._drawtext(tf, max(12, int(self.target_h * ti.fontsize_ratio)), ti.color,
                                         "(w-text_w)/2", y, alpha))
        elif ti.enabled:
            self._warn("Title requested but drawtext is unavailable or text is empty; skipping.")
        vchain.append("format=yuv420p")
        parts.append(f"[{cur_v}]{','.join(vchain)}[vout]")

        # ---- audio post: original + music + fades + loudness ------------------
        a = self.cfg.audio
        achain_tail: List[str] = []
        afi, afo = min(a.fade_in, total / 4), min(a.fade_out, total / 4)
        if afi > 0:
            achain_tail.append(f"afade=t=in:st=0:d={afi:.3f}:curve=qsin")
        if afo > 0:
            achain_tail.append(f"afade=t=out:st={total - afo:.3f}:d={afo:.3f}:curve=qsin")
        if a.final_loudnorm and "loudnorm" in self.filters:
            achain_tail.append(f"loudnorm=I={a.final_lufs}:TP=-1.5:LRA=11")
        achain_tail += ["alimiter=limit=0.97", "aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]

        if music:
            m = self.cfg.music
            _, mdur = music
            mchain = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo",
                      f"atrim=0:{total:.3f}", "asetpts=PTS-STARTPTS"]
            mend = min(total, mdur) if not (m.loop and mdur < total) else total
            mfi, mfo = min(m.fade_in, mend / 3), min(m.fade_out, mend / 3)
            if mfi > 0:
                mchain.append(f"afade=t=in:st=0:d={mfi:.3f}:curve=qsin")
            if mfo > 0:
                mchain.append(f"afade=t=out:st={mend - mfo:.3f}:d={mfo:.3f}:curve=qsin")
            mchain.append(f"volume={m.volume}")
            parts.append(f"[{n}:a]{','.join(mchain)}[mus]")
            if has_signal:
                parts.append(f"[{cur_a}]volume={a.original_volume}[orig]")
                if m.ducking and "sidechaincompress" in self.filters:
                    parts.append("[orig]asplit=2[origmix][origsc]")
                    parts.append("[mus][origsc]sidechaincompress=threshold=0.03:ratio=6:attack=20:"
                                 "release=400:makeup=1[musd]")
                    parts.append("[origmix][musd]amix=inputs=2:duration=first:normalize=0[mix]")
                else:
                    parts.append("[orig][mus]amix=inputs=2:duration=first:normalize=0[mix]")
            else:
                parts.append(f"[{cur_a}]anullsink")   # original audio is silent: discard it
                parts.append("[mus]anull[mix]")
        else:
            parts.append(f"[{cur_a}]volume={a.original_volume}[mix]")
        parts.append(f"[mix]{','.join(achain_tail)}[aout]")
        return ";".join(parts), total

    def render_join(self, segs: List[Segment], trans: List[Transition], out: Path, final: bool,
                    music: Optional[Tuple[Path, float]] = None, has_signal: bool = True,
                    label: str = "join") -> float:
        graph, total = self.build_graph(segs, trans, final, music, has_signal)
        args: List[Any] = []
        for s in segs:
            args += ["-i", s.path]
        if final and music:
            mpath, mdur = music
            if self.cfg.music.loop and mdur < total:
                args += ["-stream_loop", "-1"]
            args += ["-i", mpath]
        args += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]"]
        if final:
            args += self._final_codec_args(total)
        else:
            args += self._intermediate_args()
        args.append(out)
        self.run_ffmpeg(args, label, total)
        return total

    def _final_codec_args(self, total: float) -> List[str]:
        o = self.cfg.output
        args = ["-c:v", o.video_codec, "-crf", str(o.crf), "-preset", o.preset, "-pix_fmt", o.pix_fmt,
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", o.audio_codec, "-b:a", o.audio_bitrate, "-ar", str(o.audio_sample_rate),
                "-ac", "2", "-movflags", "+faststart", "-t", f"{total:.3f}"]
        if o.video_codec == "libx265":
            args += ["-tag:v", "hvc1"]
        return args

    def join_all(self, segs: List[Segment], trans: List[Transition], out: Path,
                 music: Optional[Tuple[Path, float]], has_signal: bool, level: int = 0) -> float:
        """Join segments; if there are more than max_clips_per_pass, join in chunks first."""
        k = self.cfg.performance.max_clips_per_pass
        n = len(segs)
        if n <= k:
            return self.render_join(segs, trans, out, True, music, has_signal, "final render")
        groups = math.ceil(n / k)
        base_size, extra = divmod(n, groups)
        LOG.info("Level %d: %d segments > %d per pass; joining in %d chunks", level, n, k, groups)
        new_segs: List[Segment] = []
        new_trans: List[Transition] = []
        start = 0
        for gi in range(groups):
            size = base_size + (1 if gi < extra else 0)
            idx = list(range(start, start + size))
            chunk_out = self.tmp / f"chunk_{level}_{gi}.mkv"
            self.render_join([segs[i] for i in idx], [trans[i] for i in idx[:-1]], chunk_out, False,
                             label=f"chunk {gi + 1}/{groups}")
            new_segs.append(Segment(chunk_out, probe_media(chunk_out).duration, "chunk"))
            if gi < groups - 1:
                new_trans.append(trans[idx[-1]])
            start += size
        return self.join_all(new_segs, new_trans, out, music, has_signal, level + 1)

    # ---- orchestration --------------------------------------------------------------- #

    def output_path(self, count: int) -> Path:
        out_dir = self.p("output_dir")
        name = str(self.cfg.output.filename)
        try:
            name = name.format(date=datetime.now().strftime("%Y%m%d_%H%M%S"), count=count)
        except (KeyError, IndexError):
            pass
        path = out_dir / name
        if path.suffix.lower() != ".mp4":
            path = path.with_suffix(".mp4")
        if path.exists() and not self.cfg.output.overwrite:
            path = path.with_name(f"{path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}")
            LOG.info("Output exists and overwrite=false; writing %s", path.name)
        return path

    def run(self) -> Optional[Path]:
        t0 = time.time()
        self.check_tools()
        clips = self.collect_clips()
        self.pick_target(clips)
        self.font = self.find_font()
        music = self.find_music()
        io = self.cfg.intro_outro
        intro = self._optional_media(io.intro, "intro")
        outro = self._optional_media(io.outro, "outro")
        credits = self.cfg.titles.credits
        want_credits = credits.enabled and bool(credits.lines)
        if want_credits and "drawtext" not in self.filters:
            self._warn("Closing credits requested but drawtext is unavailable; skipping.")
            want_credits = False

        out_path = self.output_path(len(clips))
        self.report.update({"output": str(out_path), "target": {
            "width": self.target_w, "height": self.target_h, "fps": float(self.target_fps)},
            "clips": [{"number": c.number, "file": c.path.name, "duration": round(c.info.duration, 3),
                       "size": f"{c.info.width}x{c.info.height}", "fps": round(c.info.fps, 3),
                       "audio": c.info.has_audio} for c in clips]})
        if self.dry_run:
            LOG.info("Dry run: validation complete. Would render %d clip(s) -> %s", len(clips), out_path)
            return None

        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_root = self.cfg.paths.temp_dir
        self.tmp = Path(tempfile.mkdtemp(prefix="flow_editor_",
                                         dir=str(resolve_path(temp_root, self.base)) if temp_root else None))
        LOG.info("Working directory: %s", self.tmp)
        part = out_path.with_name(out_path.stem + ".part.mp4")
        try:
            LOG.info("Analysing clips (brightness, saturation, loudness)...")
            for c in clips:
                self.measure_color(c)
                self.measure_loudness(c)
            self.compute_corrections(clips)

            LOG.info("Normalising %d clip(s)...", len(clips))
            segs: List[Segment] = []
            if intro:
                segs.append(self._normalised_extra(intro, "intro"))
            for i, c in enumerate(clips, 1):
                out = self.tmp / f"clip_{c.number:05d}.mkv"
                dur = self.normalise(c.path, c.info, out, c.correction or None, c.gain_db,
                                     f"clip {c.number} ({i}/{len(clips)})")
                segs.append(Segment(out, dur, "clip", c.number))
            if outro:
                segs.append(self._normalised_extra(outro, "outro"))
            if want_credits:
                cp = self.tmp / "credits.mkv"
                segs.append(Segment(cp, self.make_card([str(l) for l in credits.lines], credits.duration, cp),
                                    "credits"))

            LOG.info("Choosing transitions...")
            trans: List[Transition] = []
            for a, b in zip(segs, segs[1:]):
                trans.append(self.decide_transition(a, b, trans[-1] if trans else None))
            if trans:
                self.limit_transitions(segs, trans)
            for i, t in enumerate(trans):
                a, b = segs[i], segs[i + 1]
                LOG.info("  %s -> %s : %-9s %.2fs  [%s]", a.number or a.kind, b.number or b.kind,
                         t.type, t.duration, t.reason)
            has_signal = self.cfg.audio.keep_original and (
                any(c.lufs is not None for c in clips) or not self.filters.issuperset({"ebur128"}))
            if not has_signal:
                LOG.info("Original clips have no audible audio; the soundtrack will be music only.")

            LOG.info("Rendering final video...")
            expected = self.join_all(segs, trans, part, music, has_signal)
            if out_path.exists():
                out_path.unlink()
            shutil.move(str(part), str(out_path))
            self.verify_output(out_path, expected)
            self.report["transitions"] = [
                {"from": s.number or s.kind, "to": e.number or e.kind, "type": t.type,
                 "duration": round(t.duration, 3), "reason": t.reason}
                for s, e, t in zip(segs, segs[1:], trans)]
            self.report["render_seconds"] = round(time.time() - t0, 1)
            return out_path
        finally:
            if part.exists():
                part.unlink(missing_ok=True)
            if self.tmp and not self.keep_temp:
                shutil.rmtree(self.tmp, ignore_errors=True)
                LOG.debug("Removed temp dir %s", self.tmp)
            elif self.tmp:
                LOG.info("Kept temp dir: %s", self.tmp)

    def _optional_media(self, conf: Cfg, label: str) -> Optional[Tuple[Path, MediaInfo]]:
        if not conf.enabled:
            return None
        path = resolve_path(conf.file, self.base)
        try:
            return path, self.validate_media(path)
        except (InputError, FFmpegError) as exc:
            self._warn(f"{label} clip '{path}' unusable ({exc}); continuing without it.")
            return None

    def _normalised_extra(self, media: Tuple[Path, MediaInfo], kind: str) -> Segment:
        path, info = media
        out = self.tmp / f"{kind}.mkv"
        dur = self.normalise(path, info, out, None, 0.0, kind)
        return Segment(out, dur, kind)

    def verify_output(self, path: Path, expected: float) -> None:
        info = probe_media(path)
        ok = True
        if not info.has_video:
            ok = False
            LOG.error("Output has no video stream!")
        if not info.has_audio:
            self._warn("Output has no audio stream.")
        if abs(info.duration - expected) > 0.6:
            self._warn(f"Output duration {info.duration:.2f}s differs from expected {expected:.2f}s.")
        LOG.info("Output verified: %s  %.2fs  %dx%d @ %.3f fps  video=%s audio=%s  %.1f MB",
                 path.name, info.duration, info.width, info.height, info.fps, info.vcodec,
                 info.acodec or "none", path.stat().st_size / 1e6)
        self.report["output_info"] = {"duration": round(info.duration, 3), "expected": round(expected, 3),
                                      "size_mb": round(path.stat().st_size / 1e6, 2),
                                      "resolution": f"{info.width}x{info.height}", "fps": round(info.fps, 3),
                                      "video_codec": info.vcodec, "audio_codec": info.acodec, "ok": ok}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def setup_logging(log_file: Optional[Path], verbose: bool) -> None:
    for h in list(LOG.handlers):
        h.close()
        LOG.removeHandler(h)
    LOG.propagate = False
    LOG.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    LOG.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        LOG.addHandler(fh)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Automatically edit numbered Google Flow clips into one video.")
    ap.add_argument("--config", type=Path, default=None,
                    help="Path to config.json (default: ./config.json or next to the scripts folder)")
    ap.add_argument("--input", dest="input_dir", help="Input directory (overrides config)")
    ap.add_argument("--output-dir", help="Output directory (overrides config)")
    ap.add_argument("--music-dir", help="Music directory or file (overrides config)")
    ap.add_argument("--output-name", help="Output filename (overrides config)")
    ap.add_argument("--overwrite", dest="overwrite", action="store_true", default=None)
    ap.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                    help="Override any config value, e.g. --set transitions.duration=1.2 (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="Discover and validate clips only")
    ap.add_argument("--keep-temp", action="store_true", help="Keep intermediate files")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg_path = args.config
        if cfg_path is None:
            for cand in (Path.cwd() / "config.json", Path(__file__).resolve().parent.parent / "config.json"):
                if cand.is_file():
                    cfg_path = cand
                    break
        conf, base = load_config(cfg_path)
        # CLI paths are relative to the current directory, not the config file
        if args.input_dir:
            conf["paths"]["input_dir"] = str(Path(args.input_dir).expanduser().resolve())
        if args.output_dir:
            conf["paths"]["output_dir"] = str(Path(args.output_dir).expanduser().resolve())
        if args.music_dir:
            mp = Path(args.music_dir).expanduser().resolve()
            if mp.is_file():
                conf["paths"]["music_dir"], conf["music"]["file"] = str(mp.parent), str(mp)
            else:
                conf["paths"]["music_dir"] = str(mp)
        if args.output_name:
            conf["output"]["filename"] = args.output_name
        if args.overwrite is not None:
            conf["output"]["overwrite"] = args.overwrite
        for item in args.sets:
            set_dotted(conf, item)
        validate_config(conf)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    out_dir = resolve_path(conf["paths"]["output_dir"], base)
    log_name = conf["paths"].get("log_file")
    setup_logging(out_dir / log_name if log_name and not args.dry_run else None, args.verbose)
    LOG.info("Google Flow Video Editor %s", __version__)
    LOG.info("Config: %s", cfg_path or "(built-in defaults)")

    editor = VideoEditor(conf, base, dry_run=args.dry_run, keep_temp=args.keep_temp)
    try:
        result = editor.run()
    except (InputError, ConfigError) as exc:
        LOG.error("%s", exc)
        return 2
    except FFmpegError as exc:
        LOG.error("Render failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted.")
        return 130
    except Exception:  # noqa: BLE001 - last resort, always log the traceback
        LOG.exception("Unexpected error")
        return 1
    finally:
        if not args.dry_run and editor.report.get("output"):
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "render_report.json").write_text(json.dumps(editor.report, indent=2), encoding="utf-8")
            except OSError:
                pass
    if result:
        LOG.info("Done: %s (%.1fs)", result, editor.report.get("render_seconds", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())

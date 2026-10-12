"""Automated tests using generated clips (no sample footage needed).

Run:  python -m unittest discover -s tests -v      (or: pytest tests)
Requires ffmpeg + ffprobe on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import video_editor as ve  # noqa: E402
from make_test_clips import make_clip  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
FAST = ["--set", 'output.preset="ultrafast"', "--set", "output.crf=28",
        "--set", "input.deep_validate=true"]


def render(inp: Path, out: Path, *extra: str, music: Path | None = None) -> int:
    args = ["--config", str(ROOT / "config.json"), "--input", str(inp), "--output-dir", str(out), *FAST, *extra]
    if music:
        args += ["--music-dir", str(music)]
    return ve.main(args)


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                        str(path)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def pixel_at(path: Path, t: float) -> tuple:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=1:1:flags=area,format=rgb24", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    return tuple(r.stdout[:3])


def luma_at(path: Path, t: float) -> float:
    r, g, b = pixel_at(path, t)
    return 0.299 * r + 0.587 * g + 0.114 * b


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fve_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.inp, self.out = self.tmp / "videos", self.tmp / "out"
        self.inp.mkdir()

    def final(self) -> Path:
        return self.out / "final_video.mp4"

    def report(self) -> dict:
        return json.loads((self.out / "render_report.json").read_text())


class TestDiscovery(unittest.TestCase):
    def test_numeric_not_alphabetical(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for n in ("1.mp4", "2.mp4", "3.mp4", "10.mp4", "11.mp4", "100.mp4", "9.mp4"):
            (d / n).write_bytes(b"x")
        for junk in ("intro.mp4", "notes.txt", "1a.mp4", ".hidden.mp4", "final_video.mp4"):
            (d / junk).write_bytes(b"x")
        found = ve.discover_clips(d, [".mp4"])
        self.assertEqual([n for n, _ in found], [1, 2, 3, 9, 10, 11, 100])

    def test_duplicates_and_gaps(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for n in ("1.mp4", "01.mp4", "4.mp4"):
            (d / n).write_bytes(b"x")
        with self.assertLogs("flow_editor", level="WARNING") as cm:
            found = ve.discover_clips(d, [".mp4"])
        self.assertEqual([n for n, _ in found], [1, 4])
        text = "\n".join(cm.output)
        self.assertIn("Duplicate clip number 1", text)
        self.assertIn("Gaps in clip numbering (missing: 2, 3)", text)

    def test_missing_dir(self):
        with self.assertRaises(ve.InputError):
            ve.discover_clips(Path("/nonexistent/xyz"), [".mp4"])


class TestConfig(unittest.TestCase):
    def test_defaults_valid(self):
        conf, _ = ve.load_config(ROOT / "config.json")
        ve.validate_config(conf)

    def test_bad_values(self):
        conf, _ = ve.load_config(None)
        conf["output"]["crf"] = 99
        conf["transitions"]["allowed"] = ["nonsense"]
        with self.assertRaises(ve.ConfigError) as cm:
            ve.validate_config(conf)
        self.assertIn("crf", str(cm.exception))
        self.assertIn("nonsense", str(cm.exception))

    def test_set_override(self):
        conf, _ = ve.load_config(None)
        ve.set_dotted(conf, "transitions.duration=1.5")
        ve.set_dotted(conf, 'output.filename="x.mp4"')
        self.assertEqual(conf["transitions"]["duration"], 1.5)
        self.assertEqual(conf["output"]["filename"], "x.mp4")
        with self.assertRaises(ve.ConfigError):
            ve.set_dotted(conf, "nope.key=1")

    def test_resolution_and_fps(self):
        self.assertIsNone(ve.parse_resolution("auto"))
        self.assertEqual(ve.parse_resolution("1920x1080"), (1920, 1080))
        self.assertEqual(ve.parse_fps("23.976"), Fraction(24000, 1001))
        with self.assertRaises(ve.ConfigError):
            ve.parse_resolution("big")


class TestRender(Base):
    def test_numeric_order_in_output_and_mixed_inputs(self):
        """Solid-colour clips 1,2,3,10,11 with different sizes/fps must appear in numeric order."""
        colours = {1: "red", 2: "lime", 3: "blue", 10: "yellow", 11: "white"}
        sizes = {1: (320, 180), 2: (480, 270), 3: (320, 180), 10: (256, 144), 11: (320, 180)}
        for n, c in colours.items():
            make_clip(self.inp / f"{n}.mp4", 2.0, sizes[n], 24 if n % 2 else 30, f"color=c={c}", audio=(n != 3))
        # Non-clips that must be ignored
        (self.inp / "notes.txt").write_text("hi")
        code = render(self.inp, self.out, "--set", "transitions.enabled=false",
                      "--set", "color_correction.enabled=false", "--set", "fades.enabled=false")
        self.assertEqual(code, 0)
        rep = self.report()
        self.assertEqual([c["number"] for c in rep["clips"]], [1, 2, 3, 10, 11])
        info = probe(self.final())
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
        self.assertEqual((v["width"], v["height"]), (320, 180))      # most common size wins
        self.assertEqual(Fraction(v["r_frame_rate"]), 24)            # most common fps wins
        step = 2.0 - 2 / 24
        expected_rgb = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 255, 255)]
        for k, want in enumerate(expected_rgb):
            got = pixel_at(self.final(), k * step + 1.0)
            self.assertTrue(all(abs(a - b) < 70 for a, b in zip(got, want)),
                            f"slot {k}: got {got}, want ~{want}")
        # clip 2 is 16:9 but 480x270 -> scaled, clip 10 is 256x144; no stretching -> still no black bars needed
        self.assertAlmostEqual(info["format"]["duration"] and float(info["format"]["duration"]),
                               rep["output_info"]["expected"], delta=0.3)

    def test_corrupt_clip_is_skipped(self):
        for n in (1, 2, 4):
            make_clip(self.inp / f"{n}.mp4", 2.0, (320, 180), 24, "testsrc2")
        (self.inp / "3.mp4").write_bytes(b"this is not a video" * 100)
        self.assertEqual(render(self.inp, self.out), 0)
        self.assertEqual([c["number"] for c in self.report()["clips"]], [1, 2, 4])
        self.assertIn("Skipping 3.mp4", (self.out / "render.log").read_text())

    def test_abort_on_bad_clip(self):
        make_clip(self.inp / "1.mp4", 2.0, (320, 180), 24, "testsrc2")
        (self.inp / "2.mp4").write_bytes(b"garbage" * 50)
        self.assertEqual(render(self.inp, self.out, "--set", 'input.on_bad_clip="abort"'), 2)

    def test_no_clips_exit_code(self):
        self.assertEqual(render(self.inp, self.out), 2)

    def test_single_clip(self):
        make_clip(self.inp / "1.mp4", 3.0, (320, 180), 24, "testsrc2")
        self.assertEqual(render(self.inp, self.out), 0)
        self.assertAlmostEqual(float(probe(self.final())["format"]["duration"]), 3.0, delta=0.2)

    def test_transition_duration_math_and_audio_continuity(self):
        for n in range(1, 5):
            make_clip(self.inp / f"{n}.mp4", 3.0, (320, 180), 24, ["testsrc2", "rgbtestsrc", "smptebars", "testsrc"][n - 1],
                      freq=300 + 50 * n)
        self.assertEqual(render(self.inp, self.out, "--set", 'transitions.mode="fixed"',
                                "--set", "transitions.duration=1.0", "--set", 'transitions.fixed_type="fade"'), 0)
        rep = self.report()
        total_t = sum(t["duration"] for t in rep["transitions"])
        self.assertEqual(len(rep["transitions"]), 3)
        dur = float(probe(self.final())["format"]["duration"])
        self.assertAlmostEqual(dur, 4 * 3.0 - total_t, delta=0.15)
        # No dropouts at transitions: the source is a continuous tone
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(self.final()), "-af",
                            "silencedetect=noise=-45dB:d=0.25", "-f", "null", "-"], capture_output=True, text=True)
        starts = [l for l in r.stderr.splitlines() if "silence_start" in l]
        self.assertEqual(starts, [], "audio dropout detected at a transition")

    def test_fadeblack_override_is_visible(self):
        make_clip(self.inp / "1.mp4", 3.0, (320, 180), 24, "color=c=lime")
        make_clip(self.inp / "2.mp4", 3.0, (320, 180), 24, "color=c=lime")
        self.assertEqual(render(self.inp, self.out, "--set", "color_correction.enabled=false",
                                "--set", "fades.enabled=false", "--set",
                                'transitions.overrides={"1-2": {"type": "fadeblack", "duration": 1.0}}'), 0)
        t = self.report()["transitions"][0]
        self.assertEqual((t["type"], t["reason"]), ("fadeblack", "manual override"))
        window = [2.0 + i * 0.05 for i in range(21)]          # the 1 s transition spans t=2.0..3.0
        self.assertLess(min(luma_at(self.final(), x) for x in window), 15, "transition should dip to ~black")
        self.assertGreater(luma_at(self.final(), 1.0), 100)    # before the transition: normal picture
        self.assertGreater(luma_at(self.final(), 4.0), 100)    # after: normal picture again

    def test_chunked_join_matches_single_pass(self):
        for n in range(1, 8):
            make_clip(self.inp / f"{n}.mp4", 2.0, (320, 180), 24, "testsrc2" if n % 2 else "rgbtestsrc")
        self.assertEqual(render(self.inp, self.out, "--set", "performance.max_clips_per_pass=3"), 0)
        rep = self.report()
        expected = 7 * 2.0 - sum(t["duration"] for t in rep["transitions"])
        self.assertAlmostEqual(float(probe(self.final())["format"]["duration"]), expected, delta=0.3)
        self.assertIn("joining in", (self.out / "render.log").read_text())

    def test_overwrite_false_keeps_existing(self):
        make_clip(self.inp / "1.mp4", 2.0, (320, 180), 24, "testsrc2")
        self.assertEqual(render(self.inp, self.out), 0)
        before = self.final().stat().st_mtime_ns
        self.assertEqual(render(self.inp, self.out, "--no-overwrite"), 0)
        self.assertEqual(self.final().stat().st_mtime_ns, before)
        self.assertEqual(len(list(self.out.glob("final_video*.mp4"))), 2)
        self.assertEqual(list(self.out.glob("*.part.mp4")), [])

    def test_dry_run_creates_nothing(self):
        make_clip(self.inp / "1.mp4", 2.0, (320, 180), 24, "testsrc2")
        self.assertEqual(render(self.inp, self.out, "--dry-run"), 0)
        self.assertFalse(self.final().exists())


class TestTransitionDecisions(Base):
    def editor(self, **overrides):
        conf, base = ve.load_config(ROOT / "config.json")
        for k, v in overrides.items():
            ve.set_dotted(conf, f"{k}={json.dumps(v)}")
        ed = ve.VideoEditor(conf, base)
        ed.target_fps = Fraction(24, 1)
        return ed

    def seg(self, name, source, n):
        p = make_clip(self.tmp / f"{name}.mp4", 2.0, (320, 180), 24, source, audio=False)
        return ve.Segment(p, 2.0, "clip", n)

    def test_continuous_clips_get_cut(self):
        ed = self.editor()
        t = ed.decide_transition(self.seg("a", "color=c=navy", 1), self.seg("b", "color=c=navy", 2))
        self.assertEqual(t.type, "cut")

    def test_big_brightness_jump_dips_to_black(self):
        ed = self.editor()
        t = ed.decide_transition(self.seg("a", "color=c=black", 1), self.seg("b", "color=c=white", 2))
        self.assertEqual(t.type, "fadeblack")

    def test_never_dip_to_black_twice_in_a_row(self):
        ed = self.editor()
        a, b = self.seg("a", "color=c=black", 1), self.seg("b", "color=c=white", 2)
        first = ed.decide_transition(a, b)
        second = ed.decide_transition(a, b, first)
        self.assertEqual((first.type, second.type), ("fadeblack", "fade"))

    def test_disabled_transitions_are_cuts(self):
        ed = self.editor(**{"transitions.enabled": False})
        t = ed.decide_transition(self.seg("a", "testsrc2", 1), self.seg("b", "rgbtestsrc", 2))
        self.assertEqual(t.type, "cut")

    def test_limit_never_exceeds_clip_length(self):
        ed = self.editor()
        segs = [ve.Segment(Path("x"), 1.0, "clip", i) for i in range(3)]
        trans = [ve.Transition("fade", 3.0), ve.Transition("fade", 3.0)]
        ed.limit_transitions(segs, trans)
        self.assertLessEqual(sum(t.duration for t in trans), 1.0 - 0.1)


class TestAudioAndMusic(Base):
    def make_music(self, secs=6.0) -> Path:
        mdir = self.tmp / "music"
        mdir.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=220:d={secs}",
                        str(mdir / "bg.mp3")], check=True)
        return mdir

    def test_music_is_looped_and_mixed(self):
        for n in (1, 2, 3):
            make_clip(self.inp / f"{n}.mp4", 4.0, (320, 180), 24, "testsrc2", freq=600)
        mdir = self.make_music(5.0)       # shorter than the ~11 s video -> must loop
        self.assertEqual(render(self.inp, self.out, music=mdir), 0)
        info = probe(self.final())
        self.assertGreater(float(info["format"]["duration"]), 10)
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual(a["codec_name"], "aac")
        self.assertIn("Background music: bg.mp3", (self.out / "render.log").read_text())
        # Music must be audible late in the film (after the original 5 s music clip would have ended)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", "7", "-t", "2", "-i", str(self.final()),
                            "-af", "silencedetect=noise=-50dB:d=0.5", "-f", "null", "-"], capture_output=True, text=True)
        self.assertNotIn("silence_start", r.stderr)

    def test_music_only_when_clips_have_no_audio(self):
        for n in (1, 2):
            make_clip(self.inp / f"{n}.mp4", 3.0, (320, 180), 24, "testsrc2", audio=False)
        self.assertEqual(render(self.inp, self.out, music=self.make_music()), 0)
        self.assertIn("no audible audio", (self.out / "render.log").read_text())
        info = probe(self.final())
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))

    def test_missing_music_dir_is_graceful(self):
        make_clip(self.inp / "1.mp4", 2.0, (320, 180), 24, "testsrc2")
        self.assertEqual(render(self.inp, self.out, "--set", 'paths.music_dir="/nonexistent"'), 0)
        self.assertTrue(self.final().exists())

    def test_silent_everything_still_has_audio_track(self):
        make_clip(self.inp / "1.mp4", 2.0, (320, 180), 24, "testsrc2", audio=False)
        self.assertEqual(render(self.inp, self.out), 0)
        self.assertTrue(any(s["codec_type"] == "audio" for s in probe(self.final())["streams"]))


@unittest.skipUnless(HAVE_FFMPEG and "drawtext" in subprocess.run(
    ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout, "drawtext unavailable")
class TestTitlesAndOutro(Base):
    def test_title_credits_and_intro_outro(self):
        for n in (1, 2):
            make_clip(self.inp / f"{n}.mp4", 3.0, (320, 180), 24, "testsrc2")
        extra = self.tmp / "io"
        extra.mkdir()
        make_clip(extra / "intro.mp4", 2.0, (320, 180), 24, "color=c=gray")
        make_clip(extra / "outro.mp4", 2.0, (320, 180), 24, "color=c=gray")
        code = render(self.inp, self.out,
                      "--set", "titles.title.enabled=true", "--set", 'titles.title.text="Hello World"',
                      "--set", "titles.credits.enabled=true", "--set", 'titles.credits.lines=["The End","Thanks"]',
                      "--set", "titles.credits.duration=3",
                      "--set", "intro_outro.intro.enabled=true", "--set", f'intro_outro.intro.file="{extra / "intro.mp4"}"',
                      "--set", "intro_outro.outro.enabled=true", "--set", f'intro_outro.outro.file="{extra / "outro.mp4"}"')
        self.assertEqual(code, 0)
        rep = self.report()
        self.assertEqual(len(rep["transitions"]), 4)       # intro-1, 1-2, 2-outro, outro-credits
        expected = 2 + 3 + 3 + 2 + 3 - sum(t["duration"] for t in rep["transitions"])
        self.assertAlmostEqual(float(probe(self.final())["format"]["duration"]), expected, delta=0.3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

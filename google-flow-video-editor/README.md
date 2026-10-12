# Google Flow Video Editor

Turns a folder of numbered clips (`1.mp4`, `2.mp4`, … `10.mp4`) into **one edited video**:
numeric ordering, matched colour and loudness, per-boundary transitions, background music
with ducking, opening/closing fades, optional title, intro/outro and credits.
Runs locally or on **GitHub Actions**. Python standard library + FFmpeg only: no paid APIs.

```
google-flow-video-editor/
├── videos/                  ← put 1.mp4, 2.mp4, 3.mp4 … here
├── music/                   ← put background.mp3 here (optional)
├── intro_outro/             ← optional intro.mp4 / outro.mp4
├── output/                  ← final_video.mp4, render.log, render_report.json
├── scripts/
│   ├── video_editor.py      ← the editor (all logic)
│   ├── fetch_clips.py       ← download clips from a URL (keeps big files out of Git)
│   ├── make_test_clips.py   ← generate synthetic clips for testing
│   └── summarize_report.py  ← Markdown summary for the Actions run page
├── tests/test_video_editor.py   ← 27 automated tests (generated clips)
├── .github/workflows/render-video.yml
├── config.json              ← all settings (no code changes needed)
├── requirements.txt / requirements-dev.txt
└── .gitattributes / .gitignore
```

---

## 1. Quick start

**On GitHub (recommended)**

1. Put your clips in `videos/` named `1.mp4`, `2.mp4`, … (see §6 for large files).
2. Optional: put one music file in `music/`.
3. Repo → **Actions** → **Render Google Flow video** → **Run workflow** → **Run workflow**.
4. When the run turns green, open it and download **final-video-N** from the *Artifacts* section at the bottom.

**Locally**

```bash
# needs Python 3.9+ and ffmpeg/ffprobe on PATH
python scripts/video_editor.py                    # uses ./config.json
python scripts/video_editor.py --input my_clips --output-dir out --music-dir song.mp3
python scripts/video_editor.py --dry-run          # validate clips only
python scripts/video_editor.py --set transitions.duration=1.2 --set music.volume=0.12
```

Exit codes: `0` success · `1` render/FFmpeg failure · `2` bad config or no usable input.

---

## 2. How it works

| Stage | What happens |
|---|---|
| **1. Discover** | Finds files named `<integer>.<ext>`. Sorted **numerically** (`2` < `10`), never alphabetically. Ignores other files, warns on duplicates (`1.mp4` + `01.mp4`) and numbering gaps. |
| **2. Validate** | `ffprobe` each clip (duration, size, fps, audio) and a full decode test. Corrupt/unreadable clips are **skipped with a logged warning** (or abort with `input.on_bad_clip: "abort"`). |
| **3. Target format** | Resolution and fps = what you configured, or (`"auto"`) the **most common** among your clips, so Flow clips are never needlessly up/down-scaled. |
| **4. Measure** | Per clip: average brightness and saturation (FFmpeg `signalstats`) and loudness (`ebur128`). |
| **5. Normalise** | Every clip becomes the same size/fps/pixel format/48 kHz stereo. Different aspect ratios are **letter/pillar-boxed, never stretched** (`fit_mode: "crop"` fills the frame instead). Clips without audio get silence. Mild brightness/saturation matching and loudness matching (±6 dB) are applied. |
| **6. Transitions** | Boundary analysis (below) picks a transition for each cut. |
| **7. Join** | One streaming FFmpeg filter graph (`xfade` + `acrossfade`). More than `max_clips_per_pass` clips are joined in chunks, so 20, 50 or 200 clips work without enormous graphs. Then: music mix, fades, optional title, loudness normalisation, final encode. |
| **8. Verify** | Re-probes the output, compares duration with the expectation, writes `render.log` and `render_report.json`. |

Videos are streamed by FFmpeg; Python never loads frames into memory beyond tiny 32×32 analysis thumbnails.
Temporary files go to a temp directory that is removed afterwards (`--keep-temp` to inspect them).
The MP4 is written to `*.part.mp4` and renamed only on success, so a failed run never leaves a broken `final_video.mp4`.

### What the "intelligence" actually is (and is not)

The boundary analysis compares the **last frames of clip N with the first frames of clip N+1** using 32×32 thumbnails:
pixel difference, colour-histogram difference, brightness jump, and how much motion is happening at the cut.
**It does not understand what is in the scene** (no people/objects/story detection). It is a visual-similarity heuristic.
Rules, all thresholds in `transitions.analysis`:

| Situation | Choice |
|---|---|
| Nearly identical frames (e.g. clip B continues clip A) | **Cut** (a 2-frame micro-overlap) so continuous action isn't smeared |
| Similar look | **Cross dissolve** (`fade`), the base duration |
| Moderately different | Cross dissolve, ~25 % longer |
| Big scene change or big brightness jump | **Dip to black** (`fadeblack`), unless the previous cut already dipped, then a longer dissolve (variety guard) |
| High motion at the cut | Transition **shortened** (×0.6) so action isn't hidden |
| Any | Never longer than 45 % of the shorter clip; each clip keeps ≥ 0.5 s un-blended; clips are **never trimmed** |

You can always override: `transitions.mode: "fixed"`, `transitions.enabled: false`, or per boundary:

```json
"overrides": { "3-4": { "type": "fadeblack", "duration": 1.5 }, "7-8": { "type": "cut" } }
```

Keys are *file numbers* (`"3-4"` = from `3.mp4` to `4.mp4`). Allowed types: `fade, fadeblack, fadewhite, fadegrays, dissolve, distance, smoothleft, smoothright, smoothup, smoothdown, circleopen, circleclose, radial, pixelize, wipeleft, wiperight, cut`.

**Colour matching is deliberately conservative.** Moderate brightness/saturation drift between clips (typical for separately generated shots of one scene) is corrected, scaled by `color_correction.strength`. Large differences are treated as intentional (night vs day) and left alone. `look: "cinematic"` adds a slight contrast/saturation lift. Corrections applied are logged per clip.

---

## 3. Configuration (`config.json`)

Any key can also be overridden on the command line: `--set section.key=value` (JSON values). Relative paths in `config.json` are relative to the config file.

| Setting | Default | Meaning |
|---|---|---|
| `paths.input_dir` / `output_dir` / `music_dir` | `videos` / `output` / `music` | Directories |
| `input.on_bad_clip` | `skip` | `skip` or `abort` for unreadable clips |
| `input.deep_validate` | `true` | Full decode test per clip |
| `output.filename` | `final_video.mp4` | Supports `{date}` and `{count}` |
| `output.overwrite` | `true` | `false` → timestamped name if the file exists |
| `output.resolution` / `fps` | `auto` | `"auto"` or e.g. `"1920x1080"` / `24` |
| `output.fit_mode` | `pad` | `pad` (letterbox, no cropping) or `crop` |
| `output.video_codec` / `crf` / `preset` | `libx264` / `18` / `slow` | `libx265` also works. Lower CRF = higher quality |
| `output.audio_bitrate` | `256k` | AAC bitrate |
| `transitions.enabled` | `true` | `false` = hard cuts everywhere |
| `transitions.mode` | `auto` | `auto` (analysis) or `fixed` (`fixed_type`) |
| `transitions.duration` | `0.8` | Base transition length (s) |
| `transitions.allowed` | `fade, fadeblack, dissolve, fadegrays` | Types auto-mode may use |
| `color_correction.enabled` / `match_clips` / `strength` / `look` | `true` / `true` / `0.6` / `neutral` | See above |
| `audio.keep_original` | `true` | `false` = discard the clips' sound |
| `audio.original_volume` | `1.0` | Original audio level in the mix |
| `audio.normalize_clips` | `true` | Match clip loudness to the median |
| `audio.highpass_hz` / `denoise` | `60` / `false` | Rumble filter / FFT denoiser (can sound watery on AI audio) |
| `audio.fade_in` / `fade_out` | `1.0` / `2.0` | Whole-soundtrack fades (s) |
| `audio.final_loudnorm` / `final_lufs` | `true` / `-16` | Final loudness target |
| `music.enabled` / `file` / `volume` | `true` / `null` / `0.18` | Background music |
| `music.ducking` | `true` | Music dips automatically when the original audio is present |
| `music.loop` | `true` | Loop short tracks; long tracks are trimmed with a fade-out |
| `fades.video_fade_in` / `video_fade_out` | `1.0` / `1.5` | Opening/closing fades |
| `intro_outro.intro` / `outro` | disabled | `{ "enabled": true, "file": "intro_outro/intro.mp4" }` |
| `titles.title` | disabled | Text overlay with `text, start, duration, fade, position` (`center` or `lower`) |
| `titles.credits` | disabled | Closing-credits card: `lines`, `duration` |
| `performance.max_clips_per_pass` | `10` | Chunk size for very long edits |

---

## 4. Adding input videos and music

* **Clips:** name them `1.mp4`, `2.mp4`, … (`.mov/.mkv/.webm` also work; mixing formats and resolutions is fine). The number is the story order. Gaps are tolerated (a warning is logged).
* **Music:** drop a file into `music/` (`.mp3 .wav .m4a .aac .flac .ogg .opus`). If there are several, the first alphabetically is used (and a warning says so); choose explicitly with `"music": { "file": "music/theme.mp3" }`. No music? It is simply skipped.
* **Audio logic:** if the clips contain sound, the music is mixed underneath and automatically ducks. If the clips are silent, the soundtrack is music only. If there is neither, the video still gets a silent audio track (better player compatibility).
* **Intro/outro:** enable in `config.json`; they are normalised and joined with a dip-to-black.
* **Title position:** `titles.title.start` is on the *final* timeline. If you enable an intro, raise `start` by its length. It is also best placed over a stable moment rather than a transition.

---

## 5. Running GitHub Actions

**Manually:** Actions tab → *Render Google Flow video* → *Run workflow*. Optional inputs: input/output/music directories, output name, config file, a **clips URL** (§6), config overrides (one `key=value` per line, e.g. `transitions.duration=1.2`), and "run tests first".
CLI alternative: `gh workflow run render-video.yml -f output_name=film.mp4 -f set_overrides=$'music.volume=0.1'`

**Automatically on push (off by default):** set the repository *variable* `AUTO_RENDER_ON_PUSH` to `true`
(*Settings → Secrets and variables → Actions → Variables*). Pushes that change `videos/**`, `music/**`, `intro_outro/**` or `config.json` on `main` then render automatically.

**Downloading the result:** open the finished run → scroll to **Artifacts** → click **final-video-N** (a zip containing the MP4). The run page also shows a summary (duration, resolution, size, every transition chosen). `render-log-N` holds the full log. Artifacts are kept 14 days (`retention-days` in the workflow).

---

## 6. GitHub storage limits and large videos

Verified against GitHub's docs when this was written. Limits change, so check the linked pages for your plan.

| Limit | What it means for you |
|---|---|
| **100 MiB per file** in normal Git | `git push` rejects bigger files. Even below that, video bloats the repo forever (history is never shrunk). |
| **Git LFS** | Free/Pro accounts include **10 GiB storage + 10 GiB bandwidth per month**, max **2 GB per file**. **Every LFS download in Actions counts as bandwidth**, so each workflow run re-downloads all clips. 9 clips × 100 MB ≈ 1 GB per run, ~10 runs per month. Past the quota, LFS downloads can be disabled until next month unless you add billing. |
| **Artifacts** | Public repos with standard runners: free. Private repos: artifact storage counts against a small plan quota (about 1–2 GB on Pro/Team), so keep `retention-days` short. |
| **Runner** | Standard Linux runner: 14 GB disk, 16 GB RAM (public) or 8 GB (private); a job may run up to 6 h. A 64 s 1080p test render here took ~4 min and ~2 GB RAM, so you are far from the limits. |

**Recommendations, from simplest:**

1. **Small projects (clips < ~50 MB each, a few runs/month):** commit the clips normally.
2. **Medium projects:** Git LFS (`git lfs install && git lfs track "videos/*.mp4"`; see `.gitattributes`). Watch the bandwidth quota, because each run consumes it.
3. **Recommended for big or frequent jobs: keep clips out of Git.** Upload them as a `.zip` of `1.mp4 … N.mp4` to any storage with a direct/pre-signed link (GitHub Release asset, S3/R2/GCS pre-signed URL, Dropbox `?dl=1`, your own server), then run the workflow with **clips_url**, or store the URL in a repository **secret named `CLIPS_URL`** so the token never appears in logs. `scripts/fetch_clips.py` downloads and extracts it (also accepts a `.txt` list of URLs or a single video). Google Drive "share" pages are not direct links, so use a pre-signed/storage URL or rclone.
4. **Always:** the rendered `output/` is git-ignored; take the video from the artifact, not from a commit.

Docs: [LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs) · [Actions billing & artifact storage](https://docs.github.com/en/billing/concepts/product-billing/github-actions) · [hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

---

## 7. Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests -v            # or: python -m unittest discover -s tests -v
python scripts/make_test_clips.py --out /tmp/demo --count 12   # try it by hand
```

The suite generates its own clips and verifies: numeric sorting (1, 2, 3, 9, 10, 11, 100); junk/duplicate/gap handling; corrupt-clip skipping and abort mode; mixed resolutions/frame rates/missing audio; **solid-colour clips appearing in the correct numeric order in the rendered video**; output duration = Σ clips − Σ transitions; a real **dip to black** at a forced transition; no audio dropouts across crossfades; music looping and mixing; music-only and silent edits; chunked joins; titles/credits/intro/outro; overwrite/dry-run behaviour; config validation. The workflow file is checked with `actionlint`.

Check any output yourself:

```bash
ffprobe -v error -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate -of compact output/final_video.mp4
```

---

## 8. Troubleshooting

* **"No numbered clips found"**: files must be named exactly like `1.mp4` (not `clip1.mp4`, `scene_01.mp4`). Rename them.
* **A clip is skipped**: see the warning in `output/render.log`; re-export that clip.
* **Title/credits missing**: your FFmpeg lacks `drawtext` or no font was found; set `titles.font_file`. The workflow installs `fonts-dejavu-core`.
* **Transitions feel wrong for one cut**: override that boundary (§2) or set `transitions.mode: "fixed"`.
* **Slow renders**: `output.preset: "medium"` (or `fast`) trades a little size for speed.
* **Out of disk locally**: set `paths.temp_dir` to a roomy drive. Intermediates are high-quality (CRF 12): about 13 MB for a simple synthetic 8 s 1080p clip, more for detailed or grainy footage (budget a few tens of MB per clip, plus the same again for chunk files on very long edits).

## 9. Known limitations

* Scene "analysis" is pixel/colour/loudness statistics, not content understanding. For true semantic scene detection you would add an optional model; this tool deliberately doesn't claim to.
* Clips are re-encoded twice (intermediate at CRF 12, final at your CRF) to guarantee perfectly matched streams; the loss is visually negligible at the defaults, but it is not a stream-copy workflow.
* Music selection is by file name, with no beat-matching or tempo analysis.

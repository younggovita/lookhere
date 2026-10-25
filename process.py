"""Turn a raw Lookhere recording folder into a bundle any AI chat (and the Claude skill) can read.

Usage: process.py <recording_dir> [--max-frames N] [--keep-raw]

Writes into the recording folder:
  recording.md   transcript + timeline of frames, the file Claude reads
  frames/        the chosen frames, cursor and clicks marked, sized for Claude
  transcript.txt plain timestamped transcript
  message.txt    the text that goes on the clipboard
  preview.mp4    full screen video with cursor, following his cursor across monitors, with his voice,
                 for Claude to post in the chat (falls back to a slideshow of frames without screen video)
  preview.jpg    contact sheet of the chosen frames, same purpose
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # run with -I: find paths.py
import paths  # noqa: E402

# CUDA libraries from the nvidia-* wheels (source venv only; the installer ships CPU speech).
for _b in paths.CUDA_DIRS:
    os.add_dll_directory(_b)
    os.environ["PATH"] = _b + os.pathsep + os.environ["PATH"]

MAX_FRAMES = 36
LONG_EDGE = 1568       # Claude's sweet spot for image detail vs tokens
MIN_GAP = 2.0          # seconds; non-click frames closer than this to a chosen frame are skipped
PRIORITY = {"click": 3, "draw": 3, "point": 2.5, "after_click": 2, "change": 1.5, "periodic": 1}
# Words that mean "look where my cursor is": frames near them are kept like clicks.
POINTING_WORDS = {"this", "here", "that", "these", "those", "there", "it"}
TRAIL_SECONDS = 2.0


def fmt(t):
    t = int(round(t))
    return "%d:%02d" % (t // 60, t % 60)


def mono_audio(rec_dir):
    """Pick the loudest channel (audio interfaces often put the mic on input 1 only)."""
    src = os.path.join(rec_dir, "audio.wav")
    if not os.path.exists(src):
        return None
    data, sr = sf.read(src, dtype="float32", always_2d=True)
    if len(data) == 0:
        return None
    ch = int(np.argmax(np.sqrt((data ** 2).mean(axis=0))))
    x = data[:, ch]
    # Whisper wants 16 kHz mono float32; passing an array skips PyAV decoding.
    n = int(len(x) * 16000 / sr)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


MODELS = ([("large-v3-turbo", "cuda", "float16")] if paths.GPU_SPEECH else []) + [("small.en", "cpu", "int8")]


def load_model(start=0):
    from faster_whisper import WhisperModel
    last = None
    for i, (name, device, compute) in enumerate(MODELS[start:], start=start):
        try:
            return WhisperModel(name, device=device, compute_type=compute), i
        except Exception as e:
            last = e
            print("loading %s on %s failed: %r" % (name, device, e), file=sys.stderr)
    raise last


def load_models():
    """Every model in MODELS that loads: the GPU one and a small CPU one."""
    from faster_whisper import WhisperModel
    loaded = []
    for i, (name, device, compute) in enumerate(MODELS):
        try:
            kw = {"cpu_threads": 4} if device == "cpu" else {}
            loaded.append((WhisperModel(name, device=device, compute_type=compute, **kw), i))
        except Exception as e:
            print("loading %s on %s failed: %r" % (name, device, e), file=sys.stderr)
    return loaded


def transcribe_race(audio, models):
    """Run every loaded model at once and take the first to finish. Normally the GPU wins in a
    couple of seconds; when another app (Blender, a game) has the GPU pinned, GPU transcription
    can take 50x longer and the CPU model wins instead."""
    if not models:
        return transcribe(audio)
    results = queue.Queue()

    def run(model, idx):
        try:
            results.put(("ok", transcribe(audio, model, idx, fallback=False)))
        except Exception as e:
            results.put(("err", e))

    for model, idx in models:
        threading.Thread(target=run, args=(model, idx), daemon=True).start()
    err = None
    for _ in models:
        kind, val = results.get()
        if kind == "ok":
            return val
        err = val
    raise err


def transcribe(audio, model=None, idx=0, fallback=True):
    """Word-timed transcript. Falls back to the next model in MODELS if one fails."""
    while True:
        if model is None:
            model, idx = load_model(idx)
        try:
            segs, _ = model.transcribe(audio, language="en", vad_filter=True, word_timestamps=True,
                                       condition_on_previous_text=False)
            words = [{"start": w.start, "end": w.end, "word": w.word.strip()}
                     for s in segs for w in (s.words or []) if w.word.strip()]
            return words, "%s on %s" % MODELS[idx][:2]
        except Exception as e:
            print("transcribe with %s failed: %r" % (MODELS[idx][0], e), file=sys.stderr)
            if not fallback or idx + 1 >= len(MODELS):
                raise
            model, idx = None, idx + 1


def phrases(words, max_words=14, gap=0.5):
    """Group words into short phrases: break on sentence/comma punctuation, pauses, or length."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (nxt is None or w["word"][-1:] in ".?!," or nxt["start"] - w["end"] > gap
                or len(cur) >= max_words):
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                        "text": " ".join(x["word"] for x in cur)})
            cur = []
    return out


def pointing_times(words):
    return [w["start"] for w in words if w["word"].lower().strip(".,?!'\"") in POINTING_WORDS]


def choose_frames(frames, max_frames, cue_times=()):
    """Clicks and frames where he said "this/here" first, then farthest-point sampling over the rest."""
    if not frames:
        return []
    clicks = [f for f in frames if f["kind"] in ("click", "draw")]
    for t in cue_times:
        near = [f for f in frames if f["kind"] != "click" and -0.5 <= f["t"] - t <= 1.5]
        if near:
            best = min(near, key=lambda f: (abs(f["t"] - (t + 0.3)), -PRIORITY.get(f["kind"], 1)))
            if best not in clicks:
                clicks.append(best)
    clicks.sort(key=lambda f: f["t"])
    budget_clicks = int(max_frames * 0.6)
    if len(clicks) > budget_clicks:
        idx = np.linspace(0, len(clicks) - 1, budget_clicks).round().astype(int)
        clicks = [clicks[i] for i in sorted(set(idx))]
    chosen = list(clicks)
    for must in (frames[0], frames[-1]):
        if must not in chosen:
            chosen.append(must)
    rest = [f for f in frames if f not in chosen]
    while rest and len(chosen) < max_frames:
        best, best_score = None, -1.0
        for f in rest:
            gap = min(abs(f["t"] - c["t"]) for c in chosen)
            if gap < MIN_GAP and f["kind"] not in ("after_click", "point"):
                continue
            score = gap * PRIORITY.get(f["kind"], 1)
            if score > best_score:
                best, best_score = f, score
        if best is None:
            break
        chosen.append(best)
        rest.remove(best)
    return sorted(chosen, key=lambda f: f["t"])


def annotate(raw_path, frame, out_path, trail=(), long_edge=None):
    img = Image.open(raw_path).convert("RGB")
    d = ImageDraw.Draw(img)
    x, y = frame["cursor"]
    s = max(1.0, img.width / 1600)
    if len(trail) >= 2:
        # Where the cursor travelled in the seconds before this frame (faint to strong).
        n = len(trail)
        for i in range(1, n):
            a = int(60 + 160 * i / n)
            d.line((*trail[i - 1], *trail[i]), fill=(255, 170 - a // 4, 0), width=int(3 * s))
    if frame["kind"] == "click":
        for r, w in ((34, 6), (22, 3)):
            r *= s
            d.ellipse((x - r, y - r, x + r, y + r), outline=(255, 0, 0), width=int(w * s))
        d.line((x - 46 * s, y, x - 12 * s, y), fill=(255, 0, 0), width=int(3 * s))
        d.line((x + 12 * s, y, x + 46 * s, y), fill=(255, 0, 0), width=int(3 * s))
    else:
        r = 16 * s
        d.ellipse((x - r, y - r, x + r, y + r), outline=(255, 200, 0), width=int(4 * s))
    edge = long_edge or LONG_EDGE
    if max(img.size) > edge:
        k = edge / max(img.size)
        img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    img.save(out_path, quality=82)


ffmpeg_exe = paths.ffmpeg_exe


def build_preview_video(rec, meta, audio, out_path, size=(1280, 720)):
    """Slideshow of every captured frame, timed as recorded, with his voice. Returns True on success."""
    exe = ffmpeg_exe()
    frames = meta["frames"]
    if not exe or not frames:
        return False
    track = meta.get("track", [])
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        for i, f in enumerate(frames):
            marked = os.path.join(tmp, "m%05d.jpg" % i)
            annotate(os.path.join(rec, "frames_raw", f["file"]), f, marked,
                     trail_for(f, track, meta["monitors"]), long_edge=max(size))
            # Letterbox onto one canvas size so frames from different monitors can be joined.
            im = Image.open(marked)
            canvas = Image.new("RGB", size)
            im.thumbnail(size)
            canvas.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
            canvas.save(marked, quality=85)
            start = 0.0 if i == 0 else f["t"]
            end = frames[i + 1]["t"] if i + 1 < len(frames) else meta["duration"]
            lines.append("file '%s'\nduration %.3f" % (marked.replace("\\", "/"), max(0.05, end - start)))
        lines.append("file '%s'" % os.path.join(tmp, "m%05d.jpg" % (len(frames) - 1)).replace("\\", "/"))
        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        cmd = [exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing]
        if audio is not None:
            voice = os.path.join(tmp, "voice.wav")
            sf.write(voice, audio, 16000, subtype="PCM_16")
            cmd += ["-i", voice, "-c:a", "aac", "-b:a", "64k"]
        cmd += ["-r", "10", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-t", "%.2f" % meta["duration"], out_path]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)
        if r.returncode != 0:
            print("preview video failed: %s" % r.stderr[-1000:], file=sys.stderr)
            return False
    return True


def video_frame(exe, path, t, size=(96, 54)):
    """Grey thumbnail of one video frame at t seconds, for matching outputs to monitors."""
    r = subprocess.run([exe, "-v", "error", "-ss", "%.2f" % max(0.0, t), "-i", path, "-frames:v", "1",
                        "-vf", "scale=%d:%d,format=gray" % size, "-f", "rawvideo", "-"],
                       capture_output=True, creationflags=0x08000000)
    if len(r.stdout) != size[0] * size[1]:
        return None
    return np.frombuffer(r.stdout, np.uint8).reshape(size[1], size[0]).astype(np.float32)


def map_outputs(exe, rec, meta):
    """Which video file shows which monitor: compare a captured frame of each monitor with each video."""
    v = meta["video"]
    n_mon = len(meta["monitors"]) - 1
    mapping, used = {}, set()
    for mon in range(1, n_mon + 1):
        f = next((f for f in meta["frames"] if f["monitor"] == mon), None)
        if f is None:
            continue
        ref = np.asarray(Image.open(os.path.join(rec, "frames_raw", f["file"])).convert("L").resize((96, 54)),
                         np.float32)
        t = max(f["t"], v["offset"] + 0.1) - v["offset"]
        with ThreadPoolExecutor(len(v["files"])) as ex:
            ths = list(ex.map(lambda name: video_frame(exe, os.path.join(rec, name), t), v["files"]))
        scores = [999.0 if th is None else float(np.abs(th - ref).mean()) for th in ths]
        best = int(np.argmin(scores))
        if best not in used:
            mapping[mon], _ = best, used.add(best)
    rest = [i for i in range(len(v["files"])) if i not in used]
    for mon in range(1, n_mon + 1):
        if mon not in mapping and rest:
            mapping[mon] = rest.pop(0)
    return mapping


def monitor_segments(meta, start, end, hold=0.7):
    """[(t0, t1, monitor)] following the cursor, switching only once it stays on a monitor for `hold` s."""
    mons = meta["monitors"]

    def mon_at(x, y):
        for i, m in enumerate(mons[1:], start=1):
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return i
        return 1

    track = [(t, mon_at(x, y)) for t, x, y in meta.get("track", [])]
    first = meta["frames"][0]["monitor"] if meta["frames"] else 1
    if not track:
        return [(start, end, first)]
    segs, cur, seg_start, cand, cand_t = [], track[0][1], start, None, None
    for t, m in track:
        if m == cur:
            cand = None
        elif cand != m:
            cand, cand_t = m, t
        elif t - cand_t >= hold:
            if cand_t > seg_start:
                segs.append((seg_start, cand_t, cur))
            cur, seg_start, cand = m, cand_t, None
    segs.append((seg_start, end, cur))
    return [(max(a, start), min(b, end), m) for a, b, m in segs if min(b, end) - max(a, start) > 0.05]


def build_full_preview(rec, meta, audio, out_path, size=(1920, 1080), kept=None):
    """Real screen video with the cursor, following whichever monitor the cursor is on, plus his voice."""
    exe = ffmpeg_exe()
    v = meta.get("video")
    if not exe or not v or not all(os.path.exists(os.path.join(rec, f)) for f in v["files"]):
        return False
    mapping = map_outputs(exe, rec, meta)
    # Recording (voice, clicks) starts the instant the hotkey is pressed; the screen video joins
    # ~1-2s later. The preview covers the whole recording: the first video frame is held until then.
    lead = max(0.0, v["offset"])
    segs = monitor_segments(meta, lead, meta["duration"])
    kept = kept or [(0.0, meta["duration"])]
    # keep only the parts of each monitor segment that survive pauses and trims
    segs = [(max(a, x), min(b, y), mon) for a, b, mon in segs for x, y in kept if min(b, y) - max(a, x) > 0.05]
    lead = max(0.0, lead - kept[0][0]) if kept[0][0] < lead else 0.0
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [exe, "-y", "-v", "error"]
        parts = []
        w, h = size
        for i, (a, b, mon) in enumerate(segs):
            # One seeked input per segment: only the part that is shown gets decoded (on the GPU).
            cmd += ["-hwaccel", "cuda", "-ss", "%.3f" % (a - v["offset"]), "-t", "%.3f" % (b - a),
                    "-i", os.path.join(rec, v["files"][mapping.get(mon, 0)])]
            pad = ",tpad=start_duration=%.3f:start_mode=clone" % lead if i == 0 and lead > 0.05 else ""
            parts.append("[%d:v]setpts=PTS-STARTPTS,fps=30,"
                         "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:-1:-1,setsar=1%s[s%d]"
                         % (i, w, h, w, h, pad, i))
        graph = ";".join(parts) + ";" + "".join("[s%d]" % i for i in range(len(segs))) + \
            "concat=n=%d:v=1:a=0[vout]" % len(segs)
        maps = ["-map", "[vout]"]
        start = 0.0
        if audio is not None:
            voice = os.path.join(tmp, "voice.wav")
            sf.write(voice, audio, 16000, subtype="PCM_16")
            cmd += ["-i", voice]
            maps += ["-map", "%d:a" % len(segs), "-c:a", "aac", "-b:a", "96k"]
        script = os.path.join(tmp, "graph.txt")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(graph)
        cmd += ["-filter_complex_script", script] + maps
        no_hw = [x for x in cmd if x not in ("-hwaccel", "cuda")]
        for base, codec in ((cmd, ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26"]),
                            (no_hw, ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26"]),
                            (no_hw, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"])):
            r = subprocess.run(base + codec + ["-pix_fmt", "yuv420p", "-movflags", "+faststart",
                                               "-t", "%.2f" % sum(b - a for a, b in kept), out_path],
                               capture_output=True, text=True, creationflags=0x08000000)
            if r.returncode == 0:
                return True
            print("full preview with %s failed: %s" % (codec[1], r.stderr[-1500:]), file=sys.stderr)
    return False


def sheet_font(size, bold=False):
    for name in (["seguisb.ttf"] if bold else []) + ["segoeui.ttf"]:
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def build_contact_sheet(rec, meta, chosen, out_path, rows=6, cell=(640, 360)):
    """The key moments for any chat app: each row is the whole screen next to a sharp close-up
    around the cursor, so the AI can read exactly what was pointed at (a full 2560px screen
    shrunk into a chat image is too small to read)."""
    if not chosen:
        return False
    picks = sorted(chosen, key=lambda f: -PRIORITY.get(f["kind"], 1))[:rows]
    picks.sort(key=lambda f: f["t"])
    track = meta.get("track", [])
    cw, ch = cell
    gap, head = 10, 34
    W = cw * 2 + gap * 3
    sheet = Image.new("RGB", (W, len(picks) * (ch + head + gap) + gap), (24, 24, 27))
    d = ImageDraw.Draw(sheet)
    f_lab, f_sub = sheet_font(19, bold=True), sheet_font(16)
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(picks):
            full = os.path.join(tmp, "f.png")
            annotate(os.path.join(rec, "frames_raw", f["file"]), f, full,
                     trail_for(f, track, meta["monitors"]), long_edge=100000)
            im = Image.open(full).convert("RGB")
            y = gap + i * (ch + head + gap)
            d.text((gap, y + 6), "%s  %s" % (fmt(f["t"]), describe(f)), font=f_lab, fill=(245, 245, 247))
            win = (f.get("window") or "")[:70]
            d.text((gap + 8 + f_lab.getlength("%s  %s" % (fmt(f["t"]), describe(f))) + 12, y + 9), win,
                   font=f_sub, fill=(150, 150, 160))
            thumb = im.copy()
            thumb.thumbnail((cw, ch))
            sheet.paste(thumb, (gap, y + head))
            # Close-up: a cell-sized window of the full-resolution screen centred on the cursor.
            x, yc = f["cursor"]
            zw, zh = cw, ch
            left = int(min(max(0, x - zw / 2), max(0, im.width - zw)))
            top = int(min(max(0, yc - zh / 2), max(0, im.height - zh)))
            sheet.paste(im.crop((left, top, left + zw, top + zh)), (gap * 2 + cw, y + head))
            d.rectangle((gap * 2 + cw, y + head, gap * 2 + cw * 2 - 1, y + head + ch - 1), outline=(90, 90, 100))
    sheet.save(out_path, quality=88)
    return True


def describe(f):
    if f["kind"] == "click":
        c = f["click"] or {}
        return "Clicked (%s)" % c.get("button", "left")
    return {"after_click": "Screen after that click", "change": "Screen changed",
            "point": "Pointed (cursor stopped here)", "draw": "Drew on the screen (lime marks)",
            "periodic": "Screen"}.get(f["kind"], f["kind"])


def trail_for(frame, track, monitors):
    """Cursor path in the frame's pixel space over the TRAIL_SECONDS before it, same monitor only."""
    mon = monitors[frame["monitor"]]
    k = frame.get("scale", 1.0)
    pts = []
    for t, x, y in track:
        if frame["t"] - TRAIL_SECONDS <= t <= frame["t"]:
            if mon["left"] <= x < mon["left"] + mon["width"] and mon["top"] <= y < mon["top"] + mon["height"]:
                pts.append(((x - mon["left"]) * k, (y - mon["top"]) * k))
    return pts


def stage(frac, text):
    """Progress line the tray app reads to drive the overlay."""
    print("STAGE %.2f %s" % (frac, text), flush=True)


def make_preview(rec, meta, audio, meta_out, kept):
    preview = os.path.join(rec, "preview.mp4")
    return (build_full_preview(rec, meta, audio, preview, kept=kept)
            or build_preview_video(rec, meta_out, audio, preview))


# ---------- cutting pauses and trims ----------
def kept_intervals(meta, edits):
    """The stretches of the raw recording that end up in the result: everything minus pauses,
    then the trim (given in the result's own timeline) applied on top."""
    K = [(0.0, float(meta["duration"]))]
    for a, b in sorted(meta.get("pauses") or []):
        nxt = []
        for x, y in K:
            if b <= x or a >= y:
                nxt.append((x, y))
                continue
            if a > x:
                nxt.append((x, a))
            if b < y:
                nxt.append((b, y))
        K = nxt
    trim = edits.get("trim")
    if trim:
        s, e = trim
        out, pos = [], 0.0
        for x, y in K:
            L = y - x
            lo, hi = max(s, pos), min(e, pos + L)
            if hi > lo:
                out.append((x + lo - pos, x + hi - pos))
            pos += L
        K = out or K
    return [(round(a, 3), round(b, 3)) for a, b in K if b - a > 0.05]


def remap(t, K):
    """Raw-recording time -> result time, or None if t was cut."""
    pos = 0.0
    for a, b in K:
        if a <= t <= b:
            return round(pos + t - a, 2)
        pos += b - a
    return None


def apply_cuts(meta, audio, K):
    """A copy of meta (frames, clicks, cursor path, duration) and the audio in the result's timeline."""
    out = dict(meta)
    out["frames"] = [dict(f, t=remap(f["t"], K)) for f in meta["frames"] if remap(f["t"], K) is not None]
    out["clicks"] = [dict(c, t=remap(c["t"], K)) for c in meta["clicks"] if remap(c["t"], K) is not None]
    out["track"] = [[remap(t, K), x, y] for t, x, y in meta.get("track", []) if remap(t, K) is not None]
    out["duration"] = round(sum(b - a for a, b in K), 2)
    if out["frames"] and out["frames"][0]["t"] > 0.5:
        out["frames"][0] = dict(out["frames"][0], t=0.0)
    if audio is not None:
        audio = np.concatenate([audio[int(a * 16000):int(b * 16000)] for a, b in K]) if K else audio
    return out, audio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rec_dir")
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES)
    ap.add_argument("--drop-raw", action="store_true", help="delete frames_raw afterwards (no reprocessing)")
    ap.add_argument("--wait", action="store_true",
                    help="load the speech model now, then wait for 'go' on stdin (started when recording starts)")
    ap.add_argument("--download", action="store_true", help="just fetch the speech models, then exit")
    args = ap.parse_args()
    if args.download:
        load_models()
        print("READY", flush=True)
        return
    rec = os.path.abspath(args.rec_dir)
    T = time.monotonic()

    models = []
    if args.wait:
        models = load_models()
        print("READY", flush=True)
        if sys.stdin.readline().strip() != "go":
            return
        T = time.monotonic()
    with open(os.path.join(rec, "events.json"), encoding="utf-8") as fh:
        meta = json.load(fh)

    try:
        with open(os.path.join(rec, "edits.json"), encoding="utf-8") as fh:
            edits = json.load(fh)
    except (OSError, ValueError):
        edits = {}
    audio = mono_audio(rec)
    kept = kept_intervals(meta, edits)
    meta_raw = meta
    meta, audio = apply_cuts(meta_raw, audio, kept)  # everything below is in the result's timeline
    stage(0.08, "Transcribing your voice")
    with ThreadPoolExecutor(6) as ex:
        # The video renders while the speech is transcribed; the two don't depend on each other.
        video_job = ex.submit(make_preview, rec, meta_raw, audio, meta, kept)

        # Transcript, as short timed phrases so speech lines up with frames
        words, model_used = ([], "no audio") if audio is None else transcribe_race(audio, models or load_models())
        segments = phrases(words)
        with open(os.path.join(rec, "transcript.txt"), "w", encoding="utf-8") as fh:
            for s in segments:
                fh.write("[%s] %s\n" % (fmt(s["start"]), s["text"]))
        stage(0.55, "Picking the key moments")

        # Frames
        chosen = choose_frames(meta["frames"], args.max_frames, pointing_times(words))
        out_dir = os.path.join(rec, "frames")
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir)
        track = meta.get("track", [])
        for i, f in enumerate(chosen, start=1):
            f["out"] = "%02d_%s_%s.jpg" % (i, fmt(f["t"]).replace(":", "m") + "s", f["kind"])
        list(ex.map(lambda f: annotate(os.path.join(rec, "frames_raw", f["file"]), f,
                                       os.path.join(out_dir, f["out"]), trail_for(f, track, meta["monitors"])),
                    chosen))
        has_sheet = build_contact_sheet(rec, meta, chosen, os.path.join(rec, "preview.jpg"))
        if not video_job.done():
            stage(0.8, "Rendering the video")
        has_video = video_job.result()

    # Timeline: speech and frames merged by time
    items = [("say", s["start"], s) for s in segments] + [("frame", f["t"], f) for f in chosen]
    items.sort(key=lambda it: (it[1], 0 if it[0] == "say" else 1))

    dur = meta["duration"]
    full_text = " ".join(s["text"] for s in segments) or "(nothing said, or the mic picked up nothing)"
    n_clicks = len(meta["clicks"])
    lines = [
        "# Screen recording, %s (%s long)" % (meta["started_at"].replace("T", " "), fmt(dur)),
        "",
        "The user recorded their screen while talking through something they want help with.",
        "Read the transcript for what they want, then look at **every frame below** to see what they were",
        "pointing at. Red rings mark a click, yellow rings mark where the cursor was, lime strokes are",
        "marks they drew on the screen, and the orange",
        "line is the path the cursor took over the 2 seconds before (circling or underlining = pointing).",
        "\"Said\" lines sit next to the frames from the same moment, so \"this\" or \"here\" means the",
        "cursor in the frame right after. Times are minutes:seconds from the start. Window titles come",
        "from the app in front at that moment.",
        "",
        "- Clicks: %d, frames kept: %d of %d captured" % (n_clicks, len(chosen), len(meta["frames"])),
        "- Transcription: %s, mic: %s" % (model_used, meta.get("mic", "?")),
        "- Folder: `%s`" % rec,
        "- Replay for the chat: %s" % (", ".join(
            "`%s`" % os.path.join(rec, n) for n, ok in (("preview.mp4", has_video), ("preview.jpg", has_sheet)) if ok)
            or "none built"),
        "",
        "## What he said",
        "",
    ]
    lines += [full_text]
    lines += ["", "## Timeline", ""]
    for kind, t, obj in items:
        if kind == "say":
            lines.append("**[%s] Said:** %s" % (fmt(t), obj["text"]))
        else:
            win = obj.get("window") or "?"
            lines.append("**[%s] %s** in \"%s\"" % (fmt(t), describe(obj), win))
            lines.append("![%s](frames/%s)" % (obj["out"], obj["out"]))
            lines.append("`%s`" % os.path.join(out_dir, obj["out"]))
        lines.append("")
    md = os.path.join(rec, "recording.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    # Portable notes for any chat app (ChatGPT, Claude...): no local paths, no tool names.
    notes = [
        "Screen recording, %s (%s long). Attached: preview.mp4 (my screen with my voice) and "
        "preview.jpg (the key moments: each row is my whole screen next to a close-up around my "
        "cursor; red rings are clicks, yellow rings show where my cursor was). Please look at the frames and help me with what I'm asking."
        % (meta["started_at"].replace("T", " "), fmt(dur)),
        "",
        "What I said:",
    ]
    notes += ["[%s] %s" % (fmt(s["start"]), s["text"]) for s in segments] or ["(nothing said)"]
    notes += ["", "What I did:"]
    for f in chosen:
        notes.append("[%s] %s in \"%s\"" % (fmt(f["t"]), describe(f), f.get("window") or "?"))
    with open(os.path.join(rec, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(notes) + "\n")

    snippet = full_text if len(full_text) <= 500 else full_text[:500].rsplit(" ", 1)[0] + "..."
    message = (
        "I made a screen recording to show you what I mean (recording.md and the frames are in %s), "
        "help me with it.\n\nWhat I said: \"%s\"" % (rec, snippet)
    )
    with open(os.path.join(rec, "message.txt"), "w", encoding="utf-8") as fh:
        fh.write(message)

    if args.drop_raw:
        shutil.rmtree(os.path.join(rec, "frames_raw"), ignore_errors=True)
    with open(os.path.join(rec, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"duration": meta["duration"], "kept": kept, "trim": edits.get("trim")}, fh)
    stage(1.0, "Done in %.1fs" % (time.monotonic() - T))
    print("DONE %s" % md, flush=True)
    # The losing transcription may still be running in a native thread; don't wait for it.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()

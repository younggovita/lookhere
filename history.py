"""History window (tray > History...): every recording with a thumbnail, its length and first
words, and buttons to play, copy, trim, open or delete it. Trim opens a small editor with a frame
preview and start/end handles; saving rebuilds the recording (process.py) and copies it.

Tk, styled from the current overlay theme so it matches light/dark.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageTk

import hud
import paths

_win = None


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(rgb[:3])


def theme():
    dark = hud.BG[0] < 128
    return dict(
        bg=hexc(hud.BG), card=hexc((24, 24, 24) if dark else (255, 255, 255)),
        line=hexc((44, 44, 44) if dark else (228, 228, 224)), text=hexc(hud.TEXT),
        muted=hexc((150, 150, 150) if dark else (110, 110, 110)), accent=hexc(hud.ACCENT),
        accent_text="#0c0c0c", danger=hexc(hud.RED), dark=dark,
        button=hexc((36, 36, 36) if dark else (240, 240, 236)))


def duration_of(path):
    """Seconds of a video, read from ffmpeg's header line (ffprobe isn't bundled)."""
    r = subprocess.run([paths.ffmpeg_exe(), "-i", path], capture_output=True, text=True, creationflags=0x08000000)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def frame_at(path, t, width=640):
    r = subprocess.run([paths.ffmpeg_exe(), "-v", "error", "-ss", "%.2f" % max(0, t), "-i", path, "-frames:v", "1",
                        "-vf", "scale=%d:-2" % width, "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, creationflags=0x08000000)
    from io import BytesIO
    return Image.open(BytesIO(r.stdout)).convert("RGB") if r.stdout else Image.new("RGB", (width, width * 9 // 16))


def fmt(t):
    t = int(round(t))
    return "%d:%02d" % (t // 60, t % 60)


def recycle(path):
    """Send a folder to the Recycle Bin (recoverable), not a permanent delete."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                    ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wt.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]
    op = SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None, 0x0040 | 0x0010 | 0x0004 | 0x0400, False,
                         None, None)  # FO_DELETE; ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


def list_recordings(root):
    out = []
    for name in sorted(os.listdir(root), reverse=True):
        d = os.path.join(root, name)
        if not os.path.exists(os.path.join(d, "preview.mp4")):
            continue
        try:
            when = datetime.strptime(name, "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        said = ""
        try:
            with open(os.path.join(d, "transcript.txt"), encoding="utf-8") as f:
                said = " ".join(l.split("] ", 1)[-1].strip() for l in f)[:110]
        except OSError:
            pass
        try:
            with open(os.path.join(d, "result.json"), encoding="utf-8") as f:
                length = json.load(f)["duration"]
        except (OSError, ValueError, KeyError):
            length = None
        out.append(dict(dir=d, when=when, said=said, length=length))
    return out


def thumbnail(d, size=(176, 99)):
    frames = os.path.join(d, "frames")
    first = sorted(os.listdir(frames))[0] if os.path.isdir(frames) and os.listdir(frames) else None
    img = Image.open(os.path.join(frames, first)).convert("RGB") if first else Image.new("RGB", size, (60, 60, 60))
    img = img.resize(size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=8, fill=255)
    bg = Image.new("RGB", size, (0, 0, 0))
    bg.paste(img, (0, 0), mask)
    return bg


def when_label(dt):
    today = datetime.now().date()
    day = "Today" if dt.date() == today else ("Yesterday" if (today - dt.date()).days == 1 else dt.strftime("%a %d %b"))
    return "%s, %s" % (day, dt.strftime("%I:%M %p").lstrip("0"))


def open_window(app):
    """Open (or raise) the History window. Runs on the app's Tk thread."""
    global _win
    if _win is not None and _win.top.winfo_exists():
        _win.refresh()
        _win.top.deiconify()
        _win.top.lift()
        _win.top.focus_force()
        return
    _win = HistoryWindow(app)


class Button(tk.Label):
    """A flat, rounded-feeling button that follows the theme."""

    def __init__(self, parent, text, command, t, kind="normal"):
        bg = {"normal": t["button"], "accent": t["accent"], "danger": t["button"]}[kind]
        fg = {"normal": t["text"], "accent": t["accent_text"], "danger": t["danger"]}[kind]
        super().__init__(parent, text=text, bg=bg, fg=fg, padx=12, pady=5, cursor="hand2",
                         font=("Segoe UI Semibold", 9))
        self.bind("<Button-1>", lambda e: command())
        hover = t["line"] if kind != "accent" else t["accent"]
        self.bind("<Enter>", lambda e: self.configure(bg=hover))
        self.bind("<Leave>", lambda e: self.configure(bg=bg))


class HistoryWindow:
    def __init__(self, app):
        self.app = app
        self.t = theme()
        t = self.t
        self.top = tk.Toplevel(app.root)
        self.top.title("Lookhere history")
        self.top.geometry("760x640")
        self.top.minsize(560, 360)
        self.top.configure(bg=t["bg"])
        try:
            self.top.iconbitmap(os.path.join(paths.RES, "lookhere.ico"))
        except tk.TclError:
            pass
        head = tk.Frame(self.top, bg=t["bg"])
        head.pack(fill="x", padx=22, pady=(18, 8))
        tk.Label(head, text="Recordings", bg=t["bg"], fg=t["text"], font=("Segoe UI Semibold", 17)).pack(side="left")
        Button(head, "Open folder", lambda: os.startfile(app_root(app)), t).pack(side="right")
        self.count = tk.Label(head, text="", bg=t["bg"], fg=t["muted"], font=("Segoe UI", 10))
        self.count.pack(side="left", padx=12, pady=(6, 0))

        body = tk.Frame(self.top, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=(22, 8), pady=(0, 16))
        self.canvas = tk.Canvas(body, bg=t["bg"], highlightthickness=0, bd=0)
        sb = tk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list = tk.Frame(self.canvas, bg=t["bg"])
        self.list_id = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.list_id, width=e.width - 12))
        self.top.bind_all("<MouseWheel>", self._wheel)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.images = []
        self.refresh()

    def _wheel(self, e):
        if self.top.winfo_exists():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def close(self):
        global _win
        self.top.unbind_all("<MouseWheel>")
        self.top.destroy()
        _win = None

    def refresh(self):
        t = self.t
        for w in self.list.winfo_children():
            w.destroy()
        self.images = []
        recs = list_recordings(app_root(self.app))
        self.count.configure(text="%d saved" % len(recs))
        if not recs:
            tk.Label(self.list, text="Nothing yet. Press %s to record." % self.app.hotkey_label, bg=t["bg"],
                     fg=t["muted"], font=("Segoe UI", 11)).pack(pady=40)
        for r in recs:
            self._row(r)

    def _row(self, r):
        t = self.t
        card = tk.Frame(self.list, bg=t["card"], highlightbackground=t["line"], highlightthickness=1)
        card.pack(fill="x", pady=6)
        img = ImageTk.PhotoImage(thumbnail(r["dir"]))
        self.images.append(img)
        pic = tk.Label(card, image=img, bg=t["card"], cursor="hand2")
        pic.pack(side="left", padx=12, pady=12)
        pic.bind("<Button-1>", lambda e: os.startfile(os.path.join(r["dir"], "preview.mp4")))
        info = tk.Frame(card, bg=t["card"])
        info.pack(side="left", fill="both", expand=True, pady=12)
        top = tk.Frame(info, bg=t["card"])
        top.pack(fill="x")
        tk.Label(top, text=when_label(r["when"]), bg=t["card"], fg=t["text"],
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        length = r["length"] if r["length"] is not None else duration_of(os.path.join(r["dir"], "preview.mp4"))
        trimmed = os.path.exists(os.path.join(r["dir"], "edits.json"))
        tk.Label(top, text=fmt(length) + ("  trimmed" if trimmed else ""), bg=t["card"], fg=t["muted"],
                 font=("Segoe UI", 10)).pack(side="left", padx=10)
        tk.Label(info, text=r["said"] or "(nothing said)", bg=t["card"], fg=t["muted"], font=("Segoe UI", 9),
                 wraplength=420, justify="left", anchor="w").pack(fill="x", pady=(4, 8))
        btns = tk.Frame(info, bg=t["card"])
        btns.pack(fill="x")
        Button(btns, "Play", lambda: os.startfile(os.path.join(r["dir"], "preview.mp4")), t, "accent").pack(side="left")
        Button(btns, "Copy", lambda: self.app.copy_recording(r["dir"]), t).pack(side="left", padx=6)
        Button(btns, "Trim", lambda: TrimWindow(self, r["dir"]), t).pack(side="left")
        Button(btns, "Folder", lambda: os.startfile(r["dir"]), t).pack(side="left", padx=6)
        Button(btns, "Delete", lambda: self._delete(r), t, "danger").pack(side="left")

    def _delete(self, r):
        if messagebox.askyesno("Delete recording", "Move the recording from %s to the Recycle Bin?"
                               % when_label(r["when"]), parent=self.top):
            recycle(r["dir"])
            self.refresh()


def app_root(app):
    import screen_explain
    return screen_explain.CONFIG["recordings_dir"]


class TrimWindow:
    """Pick a start and end on the recording; saving rebuilds it and copies the result."""

    def __init__(self, history, rec_dir):
        self.h, self.dir = history, rec_dir
        t = self.t = history.t
        self.video = os.path.join(rec_dir, "preview.mp4")
        self.length = duration_of(self.video)
        try:
            with open(os.path.join(rec_dir, "edits.json"), encoding="utf-8") as f:
                self.edits = json.load(f)
        except (OSError, ValueError):
            self.edits = {}
        self.top = tk.Toplevel(history.top)
        self.top.title("Trim recording")
        self.top.configure(bg=t["bg"])
        self.top.resizable(False, False)
        self.top.transient(history.top)
        self.preview = tk.Label(self.top, bg="#000000")
        self.preview.pack(padx=20, pady=(20, 10))
        self.start = tk.DoubleVar(value=0.0)
        self.end = tk.DoubleVar(value=round(self.length, 1))
        # one bar: the filmstrip with lime handles at start and end, cut-off parts dimmed
        self.W, self.H, self.PAD = 640, 52, 10
        self.bar = tk.Canvas(self.top, width=self.W + self.PAD * 2, height=self.H + 8, bg=t["bg"],
                             highlightthickness=0, bd=0, cursor="sb_h_double_arrow")
        self.bar.pack(padx=10)
        self._strip_img = None
        self.bar.bind("<Button-1>", self._grab)
        self.bar.bind("<B1-Motion>", self._drag)
        self._dragging = "Start"
        self._draw_bar()
        self.span = tk.Label(self.top, text="", bg=t["bg"], fg=t["muted"], font=("Segoe UI", 10))
        self.span.pack(pady=(6, 0))
        btns = tk.Frame(self.top, bg=t["bg"])
        btns.pack(pady=16)
        Button(btns, "Save and copy", self._save, t, "accent").pack(side="left")
        if self.edits.get("trim"):
            Button(btns, "Undo trim", self._reset, t).pack(side="left", padx=8)
        Button(btns, "Cancel", self.top.destroy, t).pack(side="left", padx=8)
        self.status = tk.Label(self.top, text="", bg=t["bg"], fg=t["muted"], font=("Segoe UI", 9))
        self.status.pack(pady=(0, 12))
        self._pending = None
        self._show(0.0)
        self._moved("Start")
        threading.Thread(target=self._filmstrip, daemon=True).start()

    def _filmstrip(self, n=8):
        tile_w = self.W // n
        tiles = [frame_at(self.video, self.length * (i + 0.5) / n, 200) for i in range(n)]
        strip = Image.new("RGB", (self.W, self.H), (0, 0, 0))
        for i, im in enumerate(tiles):
            strip.paste(im.resize((tile_w, self.H), Image.LANCZOS), (i * tile_w, 0))
        self.top.after(0, lambda: self._set_strip(strip))

    def _set_strip(self, strip):
        if self.top.winfo_exists():
            self._strip_img = ImageTk.PhotoImage(strip)
            self._draw_bar()

    def _x(self, t):
        return self.PAD + self.W * (t / self.length if self.length else 0)

    def _t(self, x):
        return min(self.length, max(0.0, (x - self.PAD) / self.W * self.length))

    def _draw_bar(self):
        c, t = self.bar, self.t
        c.delete("all")
        top, bot = 4, 4 + self.H
        if self._strip_img:
            c.create_image(self.PAD, top, image=self._strip_img, anchor="nw")
        else:
            c.create_rectangle(self.PAD, top, self.PAD + self.W, bot, fill=t["line"], width=0)
        xs, xe = self._x(self.start.get()), self._x(self.end.get())
        for x0, x1 in ((self.PAD, xs), (xe, self.PAD + self.W)):  # dim what gets cut
            if x1 > x0:
                c.create_rectangle(x0, top, x1, bot, fill="#000000", stipple="gray75", width=0)
        c.create_rectangle(xs, top, xe, bot, outline=t["accent"], width=3)
        for x in (xs, xe):
            c.create_rectangle(x - 5, top - 3, x + 5, bot + 3, fill=t["accent"], outline="#0c0c0c", width=1)
            c.create_line(x, top + 16, x, bot - 16, fill="#0c0c0c", width=2)

    def _grab(self, e):
        # move whichever handle is closer to the click
        self._dragging = "Start" if abs(e.x - self._x(self.start.get())) <= abs(e.x - self._x(self.end.get())) else "End"
        self._drag(e)

    def _drag(self, e):
        (self.start if self._dragging == "Start" else self.end).set(round(self._t(e.x), 1))
        self._moved(self._dragging)

    def _moved(self, which):
        s, e = self.start.get(), self.end.get()
        if e - s < 0.5:  # keep at least half a second
            if which == "Start":
                self.start.set(max(0, e - 0.5))
            else:
                self.end.set(min(self.length, s + 0.5))
        s, e = self.start.get(), self.end.get()
        if hasattr(self, "bar"):
            self._draw_bar()
        self.span.configure(text="Keeping %s to %s  (%s of %s)" % (fmt(s), fmt(e), fmt(e - s), fmt(self.length)))
        t = s if which == "Start" else e
        if self._pending:
            self.top.after_cancel(self._pending)
        self._pending = self.top.after(120, lambda: self._show(t))  # don't decode a frame per pixel dragged

    def _show(self, t):
        def work():
            im = frame_at(self.video, min(t, max(0, self.length - 0.1)), 640)
            self.top.after(0, lambda: self._set_preview(im))
        threading.Thread(target=work, daemon=True).start()

    def _set_preview(self, im):
        if self.top.winfo_exists():
            self._img = ImageTk.PhotoImage(im)
            self.preview.configure(image=self._img)

    def _write_edits(self, trim):
        edits = dict(self.edits)
        if trim is None:
            edits.pop("trim", None)
        else:
            edits["trim"] = trim
        path = os.path.join(self.dir, "edits.json")
        if edits:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(edits, f)
        elif os.path.exists(path):
            os.remove(path)

    def _save(self):
        s, e = self.start.get(), self.end.get()
        old = self.edits.get("trim")
        base = old[0] if old else 0.0  # handles are on the current (maybe already trimmed) video
        self._write_edits([round(base + s, 2), round(base + e, 2)])
        self._rebuild()

    def _reset(self):
        self._write_edits(None)
        self._rebuild()

    def _rebuild(self):
        self.status.configure(text="Rebuilding the recording...")
        app = self.h.app

        def work():
            p = subprocess.run(paths.worker_cmd(self.dir), capture_output=True, text=True,
                               creationflags=0x08000000)
            app.events.put(("trimmed", self.dir, p.returncode == 0))
        threading.Thread(target=work, daemon=True).start()
        self.top.after(300, self.top.destroy)

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

from PIL import Image, ImageDraw, ImageTk

import hud
import paths

_win = None


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(rgb[:3])


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


def thumbnail(d, size=(176, 99), card=(24, 24, 28)):
    frames = os.path.join(d, "frames")
    first = sorted(os.listdir(frames))[0] if os.path.isdir(frames) and os.listdir(frames) else None
    img = Image.open(os.path.join(frames, first)).convert("RGB") if first else Image.new("RGB", size, (60, 60, 60))
    S = 3
    big = img.resize((size[0] * S, size[1] * S), Image.LANCZOS)
    mask = Image.new("L", big.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=12 * S, fill=255)
    bg = Image.new("RGB", big.size, card[:3])
    bg.paste(big, (0, 0), mask)
    return bg.resize(size, Image.LANCZOS)  # smooth rounded corners against the card colour


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


# ---------- look: the same black/white + acid lime as the pill ----------
def palette():
    dark = hud.BG[0] < 128
    return dict(
        dark=dark,
        bg=(10, 10, 10) if dark else (246, 246, 243),
        card=(22, 22, 26) if dark else (255, 255, 255),
        edge=(255, 255, 255, 22) if dark else (0, 0, 0, 22),
        text=tuple(hud.TEXT[:3]),
        muted=(150, 150, 155) if dark else (105, 105, 110),
        lime=tuple(hud.ACCENT[:3]),
        ink=(12, 12, 12),
        button=(36, 36, 42) if dark else (238, 238, 234),
        button_hover=(52, 52, 60) if dark else (226, 226, 220),
        red=tuple(hud.RED[:3]),
    )


def _font(size, bold=True):
    return hud._font(["seguisb.ttf", "segoeui.ttf"] if bold else ["segoeui.ttf"], size)


def pill_image(text, fill, fg, height=30, pad=15, icon=None, size=13, outline=None):
    """A pill-shaped button drawn at 3x and scaled down, so the round ends are smooth."""
    S = 3
    f = _font(size * S)
    tw = f.getlength(text)
    iw = (11 * S + 7 * S) if icon else 0
    w = int(tw + iw + pad * 2 * S)
    h = height * S
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=fill + (255,),
                        outline=outline + (255,) if outline else None, width=S if outline else 0)
    x = pad * S
    if icon == "play":
        cy = h / 2
        d.polygon([(x, cy - 6 * S), (x + 10 * S, cy), (x, cy + 6 * S)], fill=fg + (255,))
        x += 11 * S + 7 * S
    d.text((x, h / 2), text, font=f, fill=fg + (255,), anchor="lm")
    return im.resize((w // S, height), Image.LANCZOS)


def round_rect(w, h, fill, edge, radius=18):
    S = 3
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=radius * S, fill=fill + (255,),
                                         outline=edge, width=S)
    return im.resize((w, h), Image.LANCZOS)


class Pill(tk.Label):
    """A pill button; kind: "lime", "plain" or "danger". Hover lightens it."""

    def __init__(self, parent, text, command, p, bg, kind="plain", icon=None):
        self.p, self.kind, self.icon, self.command, self.text_ = p, kind, icon, command, text
        super().__init__(parent, bd=0, highlightthickness=0, bg=hexc(bg), cursor="hand2")
        self.set_text(text)
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self.configure(image=self.hover))
        self.bind("<Leave>", lambda e: self.configure(image=self.normal))

    def set_text(self, text, kind=None):
        p, kind = self.p, kind or self.kind
        if kind == "lime":
            normal, hover, fg = p["lime"], tuple(min(255, c + 25) for c in p["lime"]), p["ink"]
        elif kind == "armed":  # a delete waiting for its second click
            normal, hover, fg = p["red"], p["red"], (255, 255, 255)
        else:
            normal, hover = p["button"], p["button_hover"]
            fg = p["red"] if kind == "danger" else p["text"]
        self.normal = ImageTk.PhotoImage(pill_image(text, normal, fg, icon=self.icon))
        self.hover = ImageTk.PhotoImage(pill_image(text, hover, fg, icon=self.icon))
        self.configure(image=self.normal)


def dark_title_bar(top, dark):
    try:
        hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
        val = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


class HistoryWindow:
    CARD_H = 132

    def __init__(self, app):
        self.app = app
        self.p = palette()
        p = self.p
        self.top = tk.Toplevel(app.root)
        self.top.title("Lookhere")
        self.top.geometry("780x660")
        self.top.minsize(600, 380)
        self.top.configure(bg=hexc(p["bg"]))
        try:
            self.top.iconbitmap(os.path.join(paths.RES, "lookhere.ico"))
        except tk.TclError:
            pass
        self.top.update_idletasks()
        dark_title_bar(self.top, p["dark"])

        head = tk.Frame(self.top, bg=hexc(p["bg"]))
        head.pack(fill="x", padx=24, pady=(20, 12))
        import logo
        self._logo = ImageTk.PhotoImage(logo.draw_logo(logo.IDLE, 34))
        tk.Label(head, image=self._logo, bg=hexc(p["bg"])).pack(side="left", padx=(0, 12))
        tk.Label(head, text="Recordings", bg=hexc(p["bg"]), fg=hexc(p["text"]),
                 font=("Segoe UI Semibold", 18)).pack(side="left")
        self.count = tk.Label(head, text="", bg=hexc(p["bg"]), fg=hexc(p["muted"]), font=("Segoe UI", 10))
        self.count.pack(side="left", padx=12, pady=(8, 0))
        Pill(head, "Open folder", lambda: os.startfile(app_root(app)), p, p["bg"]).pack(side="right")

        self.canvas = tk.Canvas(self.top, bg=hexc(p["bg"]), highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=(24, 14), pady=(0, 18))
        self.list = tk.Frame(self.canvas, bg=hexc(p["bg"]))
        self.list_id = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resized)
        self.top.bind_all("<MouseWheel>", self._wheel)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.images, self.cards, self._width = [], [], 700
        self.refresh()

    def _wheel(self, e):
        if self.top.winfo_exists():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _resized(self, e):
        self.canvas.itemconfigure(self.list_id, width=e.width - 10)
        w = e.width - 10
        if abs(w - self._width) > 2:
            self._width = w
            for c in self.cards:
                c.event_generate("<<Redraw>>")

    def close(self):
        global _win
        self.top.unbind_all("<MouseWheel>")
        self.top.destroy()
        _win = None

    def refresh(self):
        p = self.p
        for w in self.list.winfo_children():
            w.destroy()
        self.images, self.cards = [], []
        recs = list_recordings(app_root(self.app))
        self.count.configure(text="%d saved" % len(recs) if recs else "")
        if not recs:
            self._empty()
        for r in recs:
            self._card(r)

    def _empty(self):
        p = self.p
        box = tk.Frame(self.list, bg=hexc(p["bg"]))
        box.pack(fill="x", pady=90)
        import logo
        img = ImageTk.PhotoImage(logo.draw_logo(logo.IDLE, 72))
        self.images.append(img)
        tk.Label(box, image=img, bg=hexc(p["bg"])).pack()
        tk.Label(box, text="No recordings yet", bg=hexc(p["bg"]), fg=hexc(p["text"]),
                 font=("Segoe UI Semibold", 14)).pack(pady=(16, 4))
        tk.Label(box, text="Press %s to record. They'll show up here." % self.app.hotkey_label,
                 bg=hexc(p["bg"]), fg=hexc(p["muted"]), font=("Segoe UI", 10)).pack()

    def _card(self, r):
        p = self.p
        H = self.CARD_H
        card = tk.Canvas(self.list, height=H, bg=hexc(p["bg"]), highlightthickness=0, bd=0)
        card.pack(fill="x", pady=6)
        self.cards.append(card)
        thumb = ImageTk.PhotoImage(thumbnail(r["dir"], (176, 99), p["card"]))
        self.images.append(thumb)
        video = os.path.join(r["dir"], "preview.mp4")
        length = r["length"] if r["length"] is not None else duration_of(video)
        trimmed = os.path.exists(os.path.join(r["dir"], "edits.json"))

        btns = tk.Frame(card, bg=hexc(p["card"]))
        Pill(btns, "Play", lambda: os.startfile(video), p, p["card"], "lime", icon="play").pack(side="left")
        Pill(btns, "Copy", lambda: self.app.copy_recording(r["dir"]), p, p["card"]).pack(side="left", padx=6)
        Pill(btns, "Trim", lambda: TrimWindow(self, r["dir"]), p, p["card"]).pack(side="left")
        Pill(btns, "Folder", lambda: os.startfile(r["dir"]), p, p["card"]).pack(side="left", padx=6)
        delete = Pill(btns, "Delete", None, p, p["card"], "danger")
        delete.command = lambda: self._delete(r, delete)
        delete.pack(side="left")

        def draw(_e=None):
            W = max(400, self._width)
            card.delete("all")
            bg = ImageTk.PhotoImage(round_rect(W, H, p["card"], p["edge"]))
            card._bg = bg
            card.create_image(0, 0, image=bg, anchor="nw")
            card.create_image(16, (H - 99) // 2, image=thumb, anchor="nw")
            card.tag_bind(card.create_rectangle(16, (H - 99) // 2, 192, (H - 99) // 2 + 99, outline="", width=0),
                          "<Button-1>", lambda e: os.startfile(video))
            x = 210
            card.create_text(x, 22, text=when_label(r["when"]), anchor="nw", fill=hexc(p["text"]),
                             font=("Segoe UI Semibold", 12))
            meta = fmt(length) + ("  ·  trimmed" if trimmed else "")
            card.create_text(W - 20, 24, text=meta, anchor="ne", fill=hexc(p["muted"]), font=("Segoe UI", 10))
            card.create_text(x, 48, text=r["said"] or "Nothing said", anchor="nw", width=W - x - 24,
                             fill=hexc(p["muted"]), font=("Segoe UI", 10))
            card.create_window(x, H - 18, window=btns, anchor="sw")
        card.bind("<<Redraw>>", draw)
        draw()

    def _delete(self, r, button):
        """First click arms the button (it turns red: "Delete?"); a second click within 3 s deletes."""
        if getattr(button, "armed", False):
            recycle(r["dir"])
            self.refresh()
            return
        button.armed = True
        button.set_text("Delete?", "armed")
        self.top.after(3000, lambda: button.winfo_exists() and (setattr(button, "armed", False),
                                                                button.set_text("Delete", "danger")))


def app_root(app):
    import screen_explain
    return screen_explain.CONFIG["recordings_dir"]


class TrimWindow:
    """Pick a start and end on the recording; saving rebuilds it and copies the result."""

    def __init__(self, history, rec_dir):
        self.h, self.dir = history, rec_dir
        p = self.p = history.p
        bgc = hexc(p["bg"])
        self.video = os.path.join(rec_dir, "preview.mp4")
        self.length = duration_of(self.video)
        try:
            with open(os.path.join(rec_dir, "edits.json"), encoding="utf-8") as f:
                self.edits = json.load(f)
        except (OSError, ValueError):
            self.edits = {}
        self.top = tk.Toplevel(history.top)
        self.top.title("Trim recording")
        self.top.configure(bg=bgc)
        self.top.resizable(False, False)
        self.top.transient(history.top)
        self.top.update_idletasks()
        dark_title_bar(self.top, p["dark"])
        self.preview = tk.Label(self.top, bg=bgc, bd=0)
        self.preview.pack(padx=20, pady=(20, 10))
        self.start = tk.DoubleVar(value=0.0)
        self.end = tk.DoubleVar(value=round(self.length, 1))
        # one bar: the filmstrip with lime handles at start and end, cut-off parts dimmed
        self.W, self.H, self.PAD = 640, 52, 10
        self.bar = tk.Canvas(self.top, width=self.W + self.PAD * 2, height=self.H + 8, bg=bgc,
                             highlightthickness=0, bd=0, cursor="sb_h_double_arrow")
        self.bar.pack(padx=10)
        self._strip_img = None
        self.bar.bind("<Button-1>", self._grab)
        self.bar.bind("<B1-Motion>", self._drag)
        self._dragging = "Start"
        self._draw_bar()
        self.span = tk.Label(self.top, text="", bg=bgc, fg=hexc(p["muted"]), font=("Segoe UI", 10))
        self.span.pack(pady=(8, 0))
        btns = tk.Frame(self.top, bg=bgc)
        btns.pack(pady=16)
        Pill(btns, "Save and copy", self._save, p, p["bg"], "lime").pack(side="left")
        if self.edits.get("trim"):
            Pill(btns, "Undo trim", self._reset, p, p["bg"]).pack(side="left", padx=8)
        Pill(btns, "Cancel", self.top.destroy, p, p["bg"]).pack(side="left", padx=8)
        self.status = tk.Label(self.top, text="", bg=bgc, fg=hexc(p["muted"]), font=("Segoe UI", 9))
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
        c, p = self.bar, self.p
        lime = hexc(p["lime"])
        c.delete("all")
        top, bot = 4, 4 + self.H
        if self._strip_img:
            c.create_image(self.PAD, top, image=self._strip_img, anchor="nw")
        else:
            c.create_rectangle(self.PAD, top, self.PAD + self.W, bot, fill=hexc(p["button"]), width=0)
        xs, xe = self._x(self.start.get()), self._x(self.end.get())
        for x0, x1 in ((self.PAD, xs), (xe, self.PAD + self.W)):  # dim what gets cut
            if x1 > x0:
                c.create_rectangle(x0, top, x1, bot, fill="#000000", stipple="gray75", width=0)
        c.create_rectangle(xs, top, xe, bot, outline=lime, width=3)
        for x in (xs, xe):
            c.create_rectangle(x - 5, top - 3, x + 5, bot + 3, fill=lime, outline="#0c0c0c", width=1)
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
            S = 2  # rounded corners like the cards
            big = im.resize((im.width * S, im.height * S))
            mask = Image.new("L", big.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=14 * S, fill=255)
            out = Image.new("RGB", big.size, self.p["bg"])
            out.paste(big, (0, 0), mask)
            self._img = ImageTk.PhotoImage(out.resize(im.size, Image.LANCZOS))
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

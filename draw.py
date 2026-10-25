"""Draw on screen while recording: drag to make lime marks that show up in the recording and
fade a few seconds later.

One layered window per monitor. While drawing is on, each window covers its monitor with an
almost-invisible fill (alpha 1) so mouse presses land on it instead of the app underneath;
when drawing is off the windows are click-through and only show strokes that are fading out.
Unlike the status pill, these windows are NOT hidden from capture: the marks are meant to be
seen in the video and the frames.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time
import traceback

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from pynput import mouse

import hud

user32 = hud.user32
SHOW_FOR, FADE = 3.0, 0.6   # seconds a finished stroke stays, then fades
WIDTH = 7                   # stroke width in px
STYLE = "outline"           # edge: "outline" (thin dark edge), "shadow" (soft drop shadow) or "plain"
SS = 2                      # strokes are drawn at 2x and scaled down: smooth, anti-aliased edges


def smooth(points, passes=2):
    """Steady the line like a drawing app: average out hand tremor (moving average over ~5 mouse
    points), drop near-duplicates, then round off the corners (Chaikin), keeping both ends."""
    if len(points) > 4:
        k = 2
        avg = []
        for i in range(len(points)):
            win = points[max(0, i - k):i + k + 1]
            avg.append((sum(x for x, _ in win) / len(win), sum(y for _, y in win) / len(win)))
        avg[0], avg[-1] = points[0], points[-1]
        points = avg
    pts = [points[0]]
    for p in points[1:]:
        if abs(p[0] - pts[-1][0]) + abs(p[1] - pts[-1][1]) >= 3:
            pts.append(p)
    if len(pts) > 1 and pts[-1] != points[-1]:
        pts.append(points[-1])
    for _ in range(passes):
        if len(pts) < 3:
            break
        out = [pts[0]]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            out += [(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1), (0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)]
        out.append(pts[-1])
        pts = out
    return pts


def stroke_layer(pts, lime, style=None):
    """One stroke as an anti-aliased RGBA image; returns (image, x, y) of its top-left corner."""
    style = style or STYLE
    pts = smooth(pts)
    pad = WIDTH + 14
    x0, y0 = min(x for x, _ in pts) - pad, min(y for _, y in pts) - pad
    w = int(max(x for x, _ in pts) + pad - x0) + 1
    h = int(max(y for _, y in pts) + pad - y0) + 1
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    local = [((x - x0) * SS, (y - y0) * SS) for x, y in pts]

    def line(img, colour, width, dy=0):
        d = ImageDraw.Draw(img)
        p = [(x, y + dy * SS) for x, y in local]
        if len(p) > 1:
            d.line(p, fill=colour, width=int(width * SS), joint="curve")
        r = width * SS / 2
        for x, y in (p[0], p[-1]):  # round caps
            d.ellipse((x - r, y - r, x + r, y + r), fill=colour)

    if style == "shadow":
        sh = Image.new("RGBA", big.size, (0, 0, 0, 0))
        line(sh, (0, 0, 0, 120), WIDTH + 2, dy=2)
        big.alpha_composite(sh.filter(ImageFilter.GaussianBlur(5 * SS)))
    elif style == "outline":
        line(big, (12, 12, 12, 255), WIDTH + 3)
    line(big, lime + (255,), WIDTH)
    return big.resize((w, h), Image.LANCZOS), int(x0), int(y0)
EX_BASE = 0x00080000 | 0x08000000 | 0x80 | 0x8  # LAYERED | NOACTIVATE | TOOLWINDOW | TOPMOST
EX_CLICK_THROUGH = 0x00000020                    # TRANSPARENT
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long


def monitors():
    import mss
    with mss.MSS() as s:
        return [dict(m) for m in s.monitors[1:]]


class _Screen:
    def __init__(self, mon):
        self.mon = mon
        self.hwnd = user32.CreateWindowExW(EX_BASE | EX_CLICK_THROUGH, "STATIC", "Lookhere drawing", 0x80000000,
                                           mon["left"], mon["top"], 1, 1, None, None, None, None)
        self.surface = hud._Surface()
        self.shown = False
        self.dirty = True

    def contains(self, x, y):
        m = self.mon
        return m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]


class DrawLayer:
    def __init__(self, on_stroke=None, on_error=None, blocked=None):
        self.on_stroke = on_stroke or (lambda: None)
        self.blocked = blocked or (lambda x, y: False)  # e.g. presses on the status pill
        self.on_error = on_error or (lambda msg: None)
        self.active = False
        self.strokes = []     # dicts: points (screen px), done (time finished or None)
        self.current = None
        self.lock = threading.Lock()
        self.listener = None
        self.closed = False
        self._cache = {}
        ready = threading.Event()
        threading.Thread(target=self._thread, args=(ready,), daemon=True, name="draw").start()
        ready.wait(5)

    # ---------- public (any thread) ----------
    def set_active(self, on):
        with self.lock:
            self.active = on
            self.mode_changed = True
            if not on and self.current:
                self.current["done"] = time.monotonic()
                self.current = None
        if on and not self.listener:
            self.listener = mouse.Listener(on_click=self._click, on_move=self._move)
            self.listener.start()
        elif not on and self.listener:
            self.listener.stop()
            self.listener = None

    def close(self):
        self.set_active(False)
        self.closed = True

    # ---------- mouse ----------
    def _click(self, x, y, button, pressed):
        if button != mouse.Button.left:
            return
        with self.lock:
            if pressed and self.active and not self.blocked(x, y):
                self.current = {"points": [(x, y)], "done": None}
                self.strokes.append(self.current)
            elif not pressed and self.current:
                self.current["done"] = time.monotonic()
                self.current = None
                threading.Timer(0.12, self.on_stroke).start()  # after the stroke is on screen

    def _move(self, x, y):
        with self.lock:
            if self.current:
                pts = self.current["points"]
                if abs(pts[-1][0] - x) + abs(pts[-1][1] - y) >= 2:
                    pts.append((x, y))

    # ---------- window thread ----------
    def _thread(self, ready):
        try:
            self.screens = [_Screen(m) for m in monitors()]
        except Exception:
            self.on_error("draw layer failed:\n" + traceback.format_exc())
            self.screens = []
        self.mode_changed = True
        ready.set()
        msg = wt.MSG()
        last = {}
        while not self.closed:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                busy = self._frame(last)
            except Exception:
                self.on_error("draw frame failed:\n" + traceback.format_exc())
                busy = False
                time.sleep(0.2)
            time.sleep(1 / 60 if busy else 0.03)
        for s in self.screens:
            user32.ShowWindow(s.hwnd, 0)

    def _frame(self, last):
        now = time.monotonic()
        with self.lock:
            # drop strokes that have fully faded
            self.strokes = [s for s in self.strokes if s["done"] is None or now - s["done"] < SHOW_FOR + FADE]
            snapshot = [(s["points"] if s["done"] else list(s["points"]), s["done"]) for s in self.strokes]
            if not self.strokes:
                self._cache = {}
            active = self.active
            mode_changed, self.mode_changed = self.mode_changed, False
        busy = bool(snapshot)
        for scr in self.screens:
            mine = [(p, d) for p, d in snapshot if any(scr.contains(x, y) for x, y in p)]
            if not active and not mine:
                if scr.shown:
                    user32.ShowWindow(scr.hwnd, 0)
                    scr.shown = False
                continue
            key = (active, tuple((len(p), d) for p, d in mine),
                   tuple(round(self._alpha(d, now), 2) for _, d in mine))
            if key == last.get(scr.hwnd) and not mode_changed:
                continue
            last[scr.hwnd] = key
            if mode_changed:
                ex = EX_BASE | (0 if active else EX_CLICK_THROUGH)
                user32.SetWindowLongW(scr.hwnd, -20, ex)
            self._paint(scr, mine, active, now, full=mode_changed)
            if not scr.shown:
                user32.ShowWindow(scr.hwnd, 4)  # SW_SHOWNOACTIVATE
                scr.shown = True
        return busy or active

    @staticmethod
    def _alpha(done, now):
        if done is None:
            return 1.0
        age = now - done
        return 1.0 if age < SHOW_FOR else max(0.0, 1 - (age - SHOW_FOR) / FADE)

    def _paint(self, scr, strokes, active, now, full=False):
        """Repaint only the box around the strokes (plus last frame's box, to erase), straight into
        the window's bitmap; a full-monitor repaint every frame would be ~10 fps."""
        m = scr.mon
        w, h = m["width"], m["height"]
        base = 1 if active else 0  # alpha 1 while drawing: presses land here, not on the app below
        fresh = scr.surface.size != (w, h)
        scr.surface.ensure(w, h)
        buf = np.ctypeslib.as_array(ctypes.cast(scr.surface.bits, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 4))
        pad = WIDTH + 16
        boxes = []
        for pts, done in strokes:
            if pts:
                boxes.append((min(x for x, _ in pts) - m["left"] - pad, min(y for _, y in pts) - m["top"] - pad,
                              max(x for x, _ in pts) - m["left"] + pad, max(y for _, y in pts) - m["top"] + pad))
        if full or fresh or getattr(scr, "base", None) != base:
            region = (0, 0, w, h)
        else:
            allb = boxes + ([scr.prev_box] if getattr(scr, "prev_box", None) else [])
            if not allb:
                return
            region = (max(0, min(b[0] for b in allb)), max(0, min(b[1] for b in allb)),
                      min(w, max(b[2] for b in allb)), min(h, max(b[3] for b in allb)))
        scr.base = base
        scr.prev_box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                        max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None
        rx0, ry0, rx1, ry1 = (int(v) for v in region)
        if rx1 <= rx0 or ry1 <= ry0:
            return
        img = Image.new("RGBA", (rx1 - rx0, ry1 - ry0), (0, 0, 0, base))
        lime = hud.ACCENT[:3]
        for pts, done in strokes:
            a = self._alpha(done, now)
            if a <= 0 or not pts:
                continue
            local = [(x - m["left"] - rx0, y - m["top"] - ry0) for x, y in pts]
            key = (id(pts), len(pts), done is not None, rx0, ry0)
            cached = self._cache.get(key)
            if cached is None:
                cached = stroke_layer(local, lime)
                if done is not None:  # finished: keep its image while it fades
                    self._cache[key] = cached
            layer, lx, ly = cached
            layer = layer.copy() if a < 1 else layer
            if a < 1:
                al = np.asarray(layer.getchannel("A"), dtype=np.float32) * a
                layer.putalpha(Image.fromarray(al.astype(np.uint8)))
            # paste the stroke's box, clipped to the repainted region
            cx0, cy0 = max(0, lx), max(0, ly)
            cx1, cy1 = min(img.width, lx + layer.width), min(img.height, ly + layer.height)
            if cx1 > cx0 and cy1 > cy0:
                img.alpha_composite(layer.crop((cx0 - lx, cy0 - ly, cx1 - lx, cy1 - ly)), (cx0, cy0))
        arr = np.asarray(img)
        alpha = arr[:, :, 3:4].astype(np.uint16)
        out = buf[ry0:ry1, rx0:rx1]
        out[:, :, :3] = arr[:, :, 2::-1] * alpha // 255  # premultiplied BGR
        out[:, :, 3] = arr[:, :, 3]
        blend = hud.BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow(scr.hwnd, None, ctypes.byref(wt.POINT(m["left"], m["top"])),
                                   ctypes.byref(wt.SIZE(w, h)), scr.surface.mem, ctypes.byref(wt.POINT(0, 0)),
                                   0, ctypes.byref(blend), 2)

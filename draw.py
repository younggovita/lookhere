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
gdi32 = hud.gdi32
kernel32 = ctypes.WinDLL("kernel32")
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
hud._proto(gdi32.CreateBitmap, wt.HBITMAP, ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p)
hud._proto(user32.CreateIconIndirect, wt.HICON, ctypes.c_void_p)
hud._proto(user32.RegisterClassW, wt.ATOM, ctypes.c_void_p)
hud._proto(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
hud._proto(user32.SetCursor, wt.HANDLE, wt.HANDLE)
SHOW_FOR, FADE = 3.0, 0.6   # seconds a finished stroke stays, then fades
WIDTH = 7                   # stroke width in px
FPS = 60
STYLE = "shadow"            # edge: "outline" (thin dark edge), "shadow" (soft drop shadow) or "plain"
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


def stroke_layer(pts, lime, style=None, presmoothed=False):
    """A whole stroke as one anti-aliased RGBA image (for previews); returns (image, x, y)."""
    edge, line_img, x, y = stroke_parts(pts, lime, style, presmoothed)
    if edge is not None:
        edge.alpha_composite(line_img)
        return edge, x, y
    return line_img, x, y


def stroke_parts(pts, lime, style=None, presmoothed=False):
    """A stroke (or piece of one) as two anti-aliased layers, the edge (shadow or outline, or None)
    and the lime line, plus their top-left corner. They're kept apart so every edge can be painted
    before any lime: otherwise one piece's shadow lands on the lime of the piece before it, leaving
    dark marks at the joins and wherever the line crosses itself."""
    style = style or STYLE
    if not presmoothed:
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

    edge = None
    if style == "shadow":
        sh = Image.new("RGBA", big.size, (0, 0, 0, 0))
        line(sh, (0, 0, 0, 120), WIDTH + 2, dy=2)
        edge = sh.filter(ImageFilter.GaussianBlur(5 * SS)).resize((w, h), Image.LANCZOS)
    elif style == "outline":
        line(big, (12, 12, 12, 255), WIDTH + 3)
        edge = big.resize((w, h), Image.LANCZOS)
        big = Image.new("RGBA", big.size, (0, 0, 0, 0))
    line(big, lime + (255,), WIDTH)
    return edge, big.resize((w, h), Image.LANCZOS), int(x0), int(y0)
# ---------- pencil cursor ----------
class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


def pencil_image(size):
    """The cursor art: a lime pencil at 45 degrees with a dark edge, graphite tip at bottom-left."""
    S = 8
    big = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    u = size * S / 32.0
    tip = (2.0 * u, 30.0 * u)
    ax, ay, nx, ny = 0.7071, -0.7071, 0.7071, 0.7071
    L, hw = 30.0, 4.4

    def at(t, k=0.0):
        return (tip[0] + (ax * t + nx * k) * u, tip[1] + (ay * t + ny * k) * u)

    dark, lime, wood, metal = (12, 12, 12, 255), (212, 255, 58, 255), (236, 205, 160, 255), (175, 175, 180, 255)
    outline = max(1, round(1.6 * u))
    body = [at(7, -hw), at(L - 6, -hw), at(L - 6, hw), at(7, hw)]
    eraser = [at(L - 4.6, -hw), at(L - 1.2, -hw), at(L, -hw + 1.2), at(L, hw - 1.2), at(L - 1.2, hw), at(L - 4.6, hw)]
    for poly, fill in ((eraser, (255, 140, 160, 255)), ([at(L - 6, -hw), at(L - 4.6, -hw), at(L - 4.6, hw),
                                                         at(L - 6, hw)], metal),
                       (body, lime), ([at(0), at(7, -hw), at(7, hw)], wood)):
        d.polygon(poly, fill=fill, outline=dark, width=outline)
    d.polygon([at(0), at(2.8, -hw * 2.8 / 7), at(2.8, hw * 2.8 / 7)], fill=dark)  # graphite
    return big.resize((size, size), Image.LANCZOS)


def make_pencil_cursor():
    size = max(32, user32.GetSystemMetrics(13))  # SM_CXCURSOR
    img = np.asarray(pencil_image(size))
    bgra = np.ascontiguousarray(img[:, :, [2, 1, 0, 3]])
    hdr = hud.BITMAPINFOHEADER(ctypes.sizeof(hud.BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    color = gdi32.CreateDIBSection(None, ctypes.byref(hdr), 0, ctypes.byref(bits), None, 0)
    ctypes.memmove(bits, bgra.ctypes.data, size * size * 4)
    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = _ICONINFO(False, round(2 / 32 * size), round(30 / 32 * size), mask, color)  # hotspot = tip
    return user32.CreateIconIndirect(ctypes.byref(info))


_CLASS = None
_CURSOR = None


def ink_class():
    """A window class whose cursor is the pencil (the drawing windows use it)."""
    global _CLASS
    if _CLASS is None:
        wc = _WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(ctypes.windll.user32.DefWindowProcW, ctypes.c_void_p).value
        wc.hInstance = kernel32.GetModuleHandleW(None)
        global _CURSOR
        wc.hCursor = _CURSOR = make_pencil_cursor()
        wc.lpszClassName = "LookhereInk"
        _CLASS = "LookhereInk" if user32.RegisterClassW(ctypes.byref(wc)) else "STATIC"
    return _CLASS


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
        self.hwnd = user32.CreateWindowExW(EX_BASE | EX_CLICK_THROUGH, ink_class(), "Lookhere drawing", 0x80000000,
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
        frame = 1 / FPS
        next_t = time.perf_counter()
        while not self.closed:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                busy = self._frame()
            except Exception:
                self.on_error("draw frame failed:\n" + traceback.format_exc())
                busy = False
                time.sleep(0.2)
            # fixed 60 fps clock (not "sleep after the work"), so frames land evenly
            next_t += frame if busy else 0.03
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()
        for s in self.screens:
            user32.ShowWindow(s.hwnd, 0)

    # A stroke is drawn in pieces: once part of the line can't change any more it is rendered
    # once and kept; each frame only the live tip is rendered. Cost stays flat however long
    # the stroke gets.
    CHUNK, SETTLE = 48, 24  # smoothed points per frozen piece; points near the tip still moving

    def _update(self, st, pts, lime):
        """Bring a stroke's rendered pieces up to date. Returns the screen boxes that changed."""
        changed = []
        if st.get("final"):
            return changed
        sm = smooth(pts) if len(pts) > 1 else [pts[0], pts[0]]
        pieces = st.setdefault("pieces", [])
        frozen = st.get("frozen", 0)
        while len(sm) - frozen > self.CHUNK + self.SETTLE:
            a, b = max(0, frozen - 1), frozen + self.CHUNK + 1
            pieces.append(stroke_parts(sm[a:b], lime, presmoothed=True))
            frozen += self.CHUNK
        st["frozen"] = frozen
        tail_pts = sm[max(0, frozen - 1):]
        if st.get("tail_len") != len(sm) or st["done"] is not None:
            old = st.get("tail")
            st["tail"] = stroke_parts(tail_pts, lime, presmoothed=True)
            st["tail_len"] = len(sm)
            if old:
                changed.append(_box(old))
            changed.append(_box(st["tail"]))
        if st["done"] is not None:
            st["final"] = True
        return changed

    def _frame(self):
        now = time.monotonic()
        lime = hud.ACCENT[:3]
        with self.lock:
            gone = [s for s in self.strokes if s["done"] is not None and now - s["done"] >= SHOW_FOR + FADE]
            self.strokes = [s for s in self.strokes if s not in gone]
            strokes = list(self.strokes)
            pts_now = {id(s): (s["points"] if s["done"] else list(s["points"])) for s in strokes}
            active = self.active
            mode_changed, self.mode_changed = self.mode_changed, False
        dirty = []
        for st in strokes:
            dirty += self._update(st, pts_now[id(st)], lime)
            a = self._alpha(st["done"], now)
            if a < 1 or st.get("last_alpha", 1.0) != a:  # fading: its whole box changes
                dirty += [_box(p) for p in self._parts(st)]
            st["last_alpha"] = a
        for st in gone:
            dirty += [_box(p) for p in self._parts(st)]
        for scr in self.screens:
            mine = [st for st in strokes if any(_overlaps(_box(p), scr.mon) for p in self._parts(st))]
            if not active and not mine and not any(_overlaps(b, scr.mon) for b in dirty):
                if scr.shown:
                    user32.ShowWindow(scr.hwnd, 0)
                    scr.shown = False
                continue
            if mode_changed:
                user32.SetWindowLongW(scr.hwnd, -20, EX_BASE | (0 if active else EX_CLICK_THROUGH))
                if active and _CURSOR:
                    user32.SetCursor(_CURSOR)  # show the pencil now, not on the next mouse move
            boxes = [b for b in dirty if _overlaps(b, scr.mon)]
            if boxes or mode_changed or not scr.shown:
                self._paint(scr, mine, active, now, boxes, full=mode_changed or not scr.shown)
            if not scr.shown:
                user32.ShowWindow(scr.hwnd, 4)  # SW_SHOWNOACTIVATE
                scr.shown = True
        return bool(strokes) or active

    @staticmethod
    def _parts(st):
        return st.get("pieces", []) + ([st["tail"]] if st.get("tail") else [])

    @staticmethod
    def _alpha(done, now):
        if done is None:
            return 1.0
        age = now - done
        return 1.0 if age < SHOW_FOR else max(0.0, 1 - (age - SHOW_FOR) / FADE)

    def _paint(self, scr, strokes, active, now, boxes, full=False):
        """Repaint only the changed boxes' bounding area, straight into the window's bitmap."""
        m = scr.mon
        w, h = m["width"], m["height"]
        base = 1 if active else 0  # alpha 1 while drawing: presses land here, not on the app below
        fresh = scr.surface.size != (w, h)
        scr.surface.ensure(w, h)
        buf = np.ctypeslib.as_array(ctypes.cast(scr.surface.bits, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 4))
        if full or fresh or getattr(scr, "base", None) != base:
            rx0, ry0, rx1, ry1 = 0, 0, w, h
        else:
            if not boxes:
                return
            rx0 = max(0, min(b[0] for b in boxes) - m["left"])
            ry0 = max(0, min(b[1] for b in boxes) - m["top"])
            rx1 = min(w, max(b[2] for b in boxes) - m["left"])
            ry1 = min(h, max(b[3] for b in boxes) - m["top"])
        scr.base = base
        if rx1 <= rx0 or ry1 <= ry0:
            return
        img = Image.new("RGBA", (rx1 - rx0, ry1 - ry0), (0, 0, 0, base))
        for which in (0, 1):  # every edge (shadow/outline) first, then all the lime on top
            for st in strokes:
                a = self._alpha(st["done"], now)
                if a <= 0:
                    continue
                for part in self._parts(st):
                    layer, lx, ly = part[which], part[2], part[3]
                    if layer is None:
                        continue
                    lx, ly = lx - m["left"] - rx0, ly - m["top"] - ry0  # into region coordinates
                    cx0, cy0 = max(0, lx), max(0, ly)
                    cx1, cy1 = min(img.width, lx + layer.width), min(img.height, ly + layer.height)
                    if cx1 <= cx0 or cy1 <= cy0:
                        continue
                    piece = layer.crop((cx0 - lx, cy0 - ly, cx1 - lx, cy1 - ly))
                    if a < 1:
                        al = np.asarray(piece.getchannel("A"), dtype=np.float32) * a
                        piece.putalpha(Image.fromarray(al.astype(np.uint8)))
                    img.alpha_composite(piece, (cx0, cy0))
        arr = np.asarray(img)
        alpha = arr[:, :, 3:4].astype(np.uint16)
        out = buf[ry0:ry1, rx0:rx1]
        out[:, :, :3] = arr[:, :, 2::-1] * alpha // 255  # premultiplied BGR
        out[:, :, 3] = arr[:, :, 3]
        blend = hud.BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow(scr.hwnd, None, ctypes.byref(wt.POINT(m["left"], m["top"])),
                                   ctypes.byref(wt.SIZE(w, h)), scr.surface.mem, ctypes.byref(wt.POINT(0, 0)),
                                   0, ctypes.byref(blend), 2)


def _box(piece):
    _, layer, x, y = piece
    return (x, y, x + layer.width, y + layer.height)


def _overlaps(b, mon):
    return b[0] < mon["left"] + mon["width"] and b[2] > mon["left"] and b[1] < mon["top"] + mon["height"] \
        and b[3] > mon["top"]

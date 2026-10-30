"""Draw on screen while recording: drag to make lime marks that show up in the recording and
fade a few seconds later.

One layered window per monitor. While drawing is on, each window covers its monitor with an
almost-invisible fill (alpha 1) so mouse presses land on it instead of the app underneath;
when drawing is off the windows are click-through and only show strokes that are fading out.
Unlike the status pill, these windows are NOT hidden from capture: the marks are meant to be
seen in the video and the frames.
"""
import ctypes
import math
import ctypes.wintypes as wt
import threading
import time
import traceback

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from pynput import mouse

import hud

MAC = hud.MAC
if MAC:
    import mac
else:
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
    points, 3 on a Mac), drop near-duplicates, then round off the corners (Chaikin), keeping both
    ends. A Mac's drag events are sparser than Windows' raw mouse points, so the same averaging
    spans more of the line there and rounds real corners into loops."""
    if len(points) > 4:
        k = 1 if MAC else 2
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
    """A whole stroke as one RGBA image (for previews and tests); returns (image, x, y)."""
    global STYLE
    old, STYLE = STYLE, style or STYLE
    try:
        sm = pts if presmoothed else smooth(pts)
        ink = Ink()
        ink.commit(sm, len(sm))
        box = ink.box()
        edge, line = ink.render(box, [], lime)
        if edge is not None:
            edge.alpha_composite(line)
            line = edge
        return line, box[0], box[1]
    finally:
        STYLE = old


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
_MAC_CURSOR = None


def mac_pencil_cursor():
    """The pencil as an NSCursor (32pt, drawn at 64px), hotspot at the tip."""
    global _MAC_CURSOR
    if _MAC_CURSOR is None:
        _MAC_CURSOR = mac.cursor_from(pencil_image(64), (2, 30))
    return _MAC_CURSOR


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
if not MAC:
    user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long


def monitors():
    import mss
    with mss.MSS() as s:
        return [dict(m) for m in s.monitors[1:]]


class _Screen:
    def __init__(self, mon, on_mouse=None):
        self.mon = mon
        self.shown = False
        self.dirty = True
        if MAC:  # an NSPanel of vector strokes (see DrawLayer._frame_mac)
            self.win = mac.Overlay(mac.overlay_level(), on_mouse=on_mouse)
            self.win.cursor = mac_pencil_cursor()
            return
        self.hwnd = user32.CreateWindowExW(EX_BASE | EX_CLICK_THROUGH, ink_class(), "Lookhere drawing", 0x80000000,
                                           mon["left"], mon["top"], 1, 1, None, None, None, None)
        self.surface = hud._Surface()
        self.shown = False
        self.dirty = True

    def show(self, on):
        if MAC:
            if not on:
                self.win.hide()  # showing happens with the first strokes
        else:
            user32.ShowWindow(self.hwnd, 4 if on else 0)  # SW_SHOWNOACTIVATE / SW_HIDE

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
        if on and not self.listener and not MAC:  # a Mac's ink windows get the mouse themselves
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

    def _mouse(self, kind, x, y):
        """Mac: presses and drags on the ink windows (instead of a global mouse listener)."""
        if kind == "drag":
            self._move(x, y)
        elif kind in ("down", "up"):
            self._click(x, y, mouse.Button.left, kind == "down")

    def _move(self, x, y):
        with self.lock:
            if self.current:
                pts = self.current["points"]
                if abs(pts[-1][0] - x) + abs(pts[-1][1] - y) >= 2:
                    pts.append((x, y))

    # ---------- window thread ----------
    def _thread(self, ready):
        try:
            self.screens = [_Screen(m, self._mouse) for m in monitors()]
        except Exception:
            self.on_error("draw layer failed:\n" + traceback.format_exc())
            self.screens = []
        self.mode_changed = True
        ready.set()
        msg = wt.MSG()
        frame = 1 / FPS
        next_t = time.perf_counter()
        while not self.closed:
            while not MAC and user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
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
            s.show(False)

    # Each stroke is one continuous brush trail (see Ink): settled parts of the line are stamped
    # into the stroke's own high-res mask once; each frame only the area around the live tip is
    # turned into pixels. No pieces, so no seams, and the cost stays flat however long it gets.
    SETTLE = 24  # smoothed points near the tip that can still move

    def _update(self, st, pts):
        """Commit the settled part of a stroke to its ink. Returns the screen boxes that changed."""
        ink = st.setdefault("ink", Ink())
        if st.get("final"):
            return []
        sm = smooth(pts) if len(pts) > 1 else [pts[0], pts[0]]
        upto = len(sm) if st["done"] is not None else max(1, len(sm) - self.SETTLE)
        dirty = ink.commit(sm, upto)
        old_tail = st.get("tail_box")
        st["tail"] = [] if st["done"] is not None else sm[max(0, ink.committed - 1):]
        st["tail_box"] = Ink.bbox(st["tail"]) if len(st["tail"]) > 0 else None
        if st.get("tail_len") != len(sm) or st["done"] is not None:
            dirty += [b for b in (old_tail, st["tail_box"]) if b]
        st["tail_len"] = len(sm)
        if st["done"] is not None:
            st["final"] = True
            ink.finish()
        return dirty

    def _frame_mac(self):
        """A Mac draws each stroke as a vector path (CAShapeLayer), so edges are properly
        anti-aliased at any resolution; the pixel ink below can't get there."""
        now = time.monotonic()
        with self.lock:
            self.strokes = [s for s in self.strokes
                            if s["done"] is None or now - s["done"] < SHOW_FOR + FADE]
            strokes = list(self.strokes)
            pts_now = {id(s): list(s["points"]) for s in strokes}
            active = self.active
            mode_changed, self.mode_changed = self.mode_changed, False
        out = []
        for st in strokes:
            pts = pts_now[id(st)]
            if st.get("n") != (len(pts), st["done"] is None):  # re-smooth only what changed
                st["n"] = (len(pts), st["done"] is None)
                st["path"] = smooth(pts) if len(pts) > 1 else [pts[0], pts[0]]
            out.append((id(st), st["n"], st["path"], self._alpha(st["done"], now)))
        for scr in self.screens:
            m = scr.mon
            mine = [o for o in out if any(_overlaps((x, y, x + 1, y + 1), m) for x, y in o[2][::8] + o[2][-1:])]
            if mode_changed:
                scr.win.click_through(not active)  # also shows the pencil cursor while drawing
            if active or mine:
                scr.win.show_strokes((m["left"], m["top"], m["width"], m["height"]), mine, active, STYLE,
                                     hud.ACCENT[:3], WIDTH)
                scr.shown = True
            elif scr.shown:
                scr.show(False)
                scr.shown = False
        return bool(strokes) or active

    def _frame(self):
        if MAC:
            return self._frame_mac()
        now = time.monotonic()
        with self.lock:
            gone = [s for s in self.strokes if s["done"] is not None and now - s["done"] >= SHOW_FOR + FADE]
            self.strokes = [s for s in self.strokes if s not in gone]
            strokes = list(self.strokes)
            pts_now = {id(s): (s["points"] if s["done"] else list(s["points"])) for s in strokes}
            active = self.active
            mode_changed, self.mode_changed = self.mode_changed, False
        dirty = []
        for st in strokes:
            dirty += self._update(st, pts_now[id(st)])
            a = self._alpha(st["done"], now)
            if a < 1 or st.get("last_alpha", 1.0) != a:  # fading: its whole box changes
                dirty.append(st["ink"].box())
            st["last_alpha"] = a
        for st in gone:
            if "ink" in st:
                dirty.append(st["ink"].box())
        for scr in self.screens:
            mine = [st for st in strokes if _overlaps(self._box(st), scr.mon)]
            if not active and not mine and not any(_overlaps(b, scr.mon) for b in dirty):
                if scr.shown:
                    scr.show(False)
                    scr.shown = False
                continue
            if mode_changed:
                user32.SetWindowLongW(scr.hwnd, -20, EX_BASE | (0 if active else EX_CLICK_THROUGH))
                if active and _CURSOR:
                    user32.SetCursor(_CURSOR)  # show the pencil now, not on the next mouse move
            boxes = [b for b in dirty if b and _overlaps(b, scr.mon)]
            if boxes or mode_changed or not scr.shown:
                self._paint(scr, mine, active, now, boxes, full=mode_changed or not scr.shown)
            if not scr.shown:
                scr.show(True)
                scr.shown = True
        return bool(strokes) or active

    @staticmethod
    def _box(st):
        b = st["ink"].box() if "ink" in st else None
        t = st.get("tail_box")
        if b and t:
            return (min(b[0], t[0]), min(b[1], t[1]), max(b[2], t[2]), max(b[3], t[3]))
        return b or t or (0, 0, 0, 0)

    @staticmethod
    def _alpha(done, now):
        if done is None:
            return 1.0
        age = now - done
        return 1.0 if age < SHOW_FOR else max(0.0, 1 - (age - SHOW_FOR) / FADE)

    def _paint(self, scr, strokes, active, now, boxes, full=False):
        """Repaint only the changed area, straight into the window's bitmap."""
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
        region = (m["left"] + rx0, m["top"] + ry0, m["left"] + rx1, m["top"] + ry1)  # screen coords
        lime = hud.ACCENT[:3]
        layers = []
        for st in strokes:
            a = self._alpha(st["done"], now)
            if a > 0 and _overlaps(self._box(st), {"left": region[0], "top": region[1],
                                                   "width": region[2] - region[0], "height": region[3] - region[1]}):
                layers.append((st["ink"].render(region, st.get("tail") or [], lime), a))
        img = Image.new("RGBA", (rx1 - rx0, ry1 - ry0), (0, 0, 0, base))
        for which in (0, 1):  # every stroke's edge first, then all the lime on top
            for parts, a in layers:
                layer = parts[which]
                if layer is None:
                    continue
                if a < 1:
                    al = np.asarray(layer.getchannel("A"), dtype=np.float32) * a
                    layer = layer.copy()
                    layer.putalpha(Image.fromarray(al.astype(np.uint8)))
                img.alpha_composite(layer)
        arr = np.asarray(img)
        alpha = arr[:, :, 3:4].astype(np.uint16)
        out = buf[ry0:ry1, rx0:rx1]
        out[:, :, :3] = arr[:, :, 2::-1] * alpha // 255  # premultiplied BGR
        out[:, :, 3] = arr[:, :, 3]
        blend = hud.BLENDFUNCTION(0, 0, 255, 1)
        user32.UpdateLayeredWindow(scr.hwnd, None, ctypes.byref(wt.POINT(m["left"], m["top"])),
                                   ctypes.byref(wt.SIZE(w, h)), scr.surface.mem, ctypes.byref(wt.POINT(0, 0)),
                                   0, ctypes.byref(blend), 2)


def _overlaps(b, mon):
    return b[0] < mon["left"] + mon["width"] and b[2] > mon["left"] and b[1] < mon["top"] + mon["height"] \
        and b[3] > mon["top"]


class Ink:
    """One stroke as a continuous brush trail: round dabs stamped close together into a high-res
    mask (the lime) and a second mask (its shadow or outline). Pixels come from downscaling and
    blurring the mask as a whole, so the line has even edges and no joins anywhere."""
    SS = 3          # mask resolution: 3x screen pixels
    GROW = 160      # px of spare canvas added around the stroke when it outgrows its mask
    EDGE_PAD = 16   # px around the line the shadow/outline can reach

    def __init__(self):
        self.ox = self.oy = None  # screen position of the mask's top-left (1x)
        self.w = self.h = 0
        self.line_mask = self.edge_mask = None
        self.committed = 0
        self.extent = None        # screen box the committed ink covers
        self.final = None         # (edge, line, x, y) rendered once when the stroke is finished

    @staticmethod
    def bbox(pts, pad=None):
        pad = Ink.EDGE_PAD if pad is None else pad
        return (int(min(x for x, _ in pts)) - pad, int(min(y for _, y in pts)) - pad,
                int(max(x for x, _ in pts)) + pad + 1, int(max(y for _, y in pts)) + pad + 1)

    def box(self):
        return self.extent

    def _style(self):
        r = WIDTH / 2
        if STYLE == "shadow":
            return r + 1, 2.0   # edge dab radius, edge vertical offset
        if STYLE == "outline":
            return r + 1.5, 0.0
        return None, 0.0

    def _ensure(self, box):
        if self.ox is not None and box[0] >= self.ox and box[1] >= self.oy and \
                box[2] <= self.ox + self.w and box[3] <= self.oy + self.h:
            return
        x0, y0 = box[0] - self.GROW, box[1] - self.GROW
        x1, y1 = box[2] + self.GROW, box[3] + self.GROW
        if self.ox is not None:
            x0, y0 = min(x0, self.ox), min(y0, self.oy)
            x1, y1 = max(x1, self.ox + self.w), max(y1, self.oy + self.h)
        new_line = Image.new("L", ((x1 - x0) * self.SS, (y1 - y0) * self.SS), 0)
        new_edge = Image.new("L", new_line.size, 0)
        if self.ox is not None:
            at = ((self.ox - x0) * self.SS, (self.oy - y0) * self.SS)
            new_line.paste(self.line_mask, at)
            new_edge.paste(self.edge_mask, at)
        self.ox, self.oy, self.w, self.h = x0, y0, x1 - x0, y1 - y0
        self.line_mask, self.edge_mask = new_line, new_edge

    @staticmethod
    def _stamp(draws, pts, ox, oy, ss, specs):
        """Round dabs along the polyline, spaced well under their radius: a perfectly even tube."""
        for (draw, (radius, dy)) in zip(draws, specs):
            if radius is None:
                continue
            r = radius * ss
            step = max(1.0, r * 0.3)
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]) if len(pts) > 1 else [(pts[0], pts[0])]:
                ax, ay = (x0 - ox) * ss, (y0 - oy + dy) * ss
                bx, by = (x1 - ox) * ss, (y1 - oy + dy) * ss
                n = max(1, int(math.hypot(bx - ax, by - ay) / step))
                for i in range(n + 1):
                    t = i / n
                    cx, cy = ax + (bx - ax) * t, ay + (by - ay) * t
                    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)

    def commit(self, sm, upto):
        """Stamp smoothed points [committed-1 .. upto) into the masks. Returns changed screen boxes."""
        if upto <= self.committed and self.committed:
            return []
        seg = sm[max(0, self.committed - 1):upto]
        if not seg:
            return []
        box = self.bbox(seg)
        self._ensure(box)
        edge_r, dy = self._style()
        self._stamp((ImageDraw.Draw(self.line_mask), ImageDraw.Draw(self.edge_mask)), seg, self.ox, self.oy,
                    self.SS, ((WIDTH / 2, 0.0), (edge_r, dy)))
        self.committed = upto
        e = self.extent
        self.extent = box if e is None else (min(e[0], box[0]), min(e[1], box[1]),
                                             max(e[2], box[2]), max(e[3], box[3]))
        return [box]

    def finish(self):
        self.final = None  # rendered lazily for the whole stroke on the next paint

    def render(self, region, tail, lime):
        """(edge RGBA or None, line RGBA) for a screen region, with the live tip drawn in."""
        if self.ox is None and not tail:
            return None, None
        rx0, ry0, rx1, ry1 = region
        pad = 12  # room for the shadow blur
        ex0, ey0, ex1, ey1 = rx0 - pad, ry0 - pad, rx1 + pad, ry1 + pad
        ss = self.SS
        if self.ox is not None:
            crop = ((ex0 - self.ox) * ss, (ey0 - self.oy) * ss, (ex1 - self.ox) * ss, (ey1 - self.oy) * ss)
            line_m, edge_m = self.line_mask.crop(crop), self.edge_mask.crop(crop)  # outside = empty
        else:
            line_m = Image.new("L", ((ex1 - ex0) * ss, (ey1 - ey0) * ss), 0)
            edge_m = Image.new("L", line_m.size, 0)
        edge_r, dy = self._style()
        if tail:
            self._stamp((ImageDraw.Draw(line_m), ImageDraw.Draw(edge_m)), tail, ex0, ey0, ss,
                        ((WIDTH / 2, 0.0), (edge_r, dy)))
        size = (ex1 - ex0, ey1 - ey0)
        line_a = line_m.reduce(ss)
        line = Image.new("RGBA", size, lime + (0,))
        line.putalpha(line_a)
        edge = None
        if edge_r is not None:
            edge_a = edge_m.reduce(ss)
            if STYLE == "shadow":
                edge_a = edge_a.filter(ImageFilter.GaussianBlur(5)).point(lambda v: v * 120 // 255)
            edge = Image.new("RGBA", size, (12, 12, 12, 0) if STYLE == "outline" else (0, 0, 0, 0))
            edge.putalpha(edge_a)
            edge = edge.crop((pad, pad, size[0] - pad, size[1] - pad))
        return edge, line.crop((pad, pad, size[0] - pad, size[1] - pad))

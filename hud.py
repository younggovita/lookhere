"""The floating status pill: a per-pixel-alpha layered window drawn with PIL.

Click-through, never takes focus, hidden from screen capture (so it never appears in a
recording), and animated at the display's refresh rate on its own thread: fades and slides
in, eases between widths, spinner, pulsing record dot, live mic meter, progress bar.

Speed: the pill body is built from cached end caps and a stretched middle, text is cached
per string, and only the small moving parts (icon, meter, progress) are drawn each frame.
"""
import ctypes
import ctypes.wintypes as wt
import math
import os
import threading
import time
import traceback

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Private DLL handles with full prototypes: handles (HDC, HMONITOR...) can be 64-bit values that
# overflow ctypes' default int conversion, and pystray/pynput use ctypes.windll with their own.
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
shcore = ctypes.WinDLL("shcore")
dwmapi = ctypes.WinDLL("dwmapi")
winmm = ctypes.WinDLL("winmm")

FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
SS = 2  # supersampling for smooth edges

# Themes: a black or a white pill, acid lime accents in both, red only means "recording".
# Acid lime vanishes on white, so in light mode the lime parts sit on black chips (CHIP).
THEMES = {
    "dark": dict(BG=(10, 10, 10, 244), BORDER=(255, 255, 255, 24), TEXT=(250, 250, 250, 255),
                 MUTED=(250, 250, 250, 128), RED=(255, 59, 48), ACCENT=(212, 255, 58), GREY=(120, 120, 120),
                 CHIP=None),
    "light": dict(BG=(250, 250, 250, 250), BORDER=(0, 0, 0, 26), TEXT=(12, 12, 12, 255),
                  MUTED=(12, 12, 12, 140), RED=(232, 36, 26), ACCENT=(212, 255, 58), GREY=(120, 120, 120),
                  CHIP=(12, 12, 12)),
}
MODES = {}
BUTTON_MODES = ("recording", "draw", "paused")  # modes that show the pencil button


def apply_theme(name):
    """Set the palette globals and the mode colours for 'dark' or 'light'."""
    g = globals()
    g.update(THEMES[name])
    MODES.clear()
    MODES.update({
        # mode: (icon kind, colour)
        "arming": ("spinner", ACCENT),
        "recording": ("dot", RED),
        "processing": ("spinner", ACCENT),
        "done": ("check", ACCENT),
        "cancelled": ("cross", GREY),
        "error": ("bang", RED),
        "keys": ("key", ACCENT),
        "mic": ("dot", ACCENT),
        "paused": ("pause", TEXT[:3]),
        "draw": ("pen", ACCENT),
    })


apply_theme("dark")

PILL_H, MARGIN = 60.0, 30.0   # logical px
ICON_X, TEXT_X, PAD_R = 30.0, 56.0, 22.0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _proto(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


_proto(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
_proto(user32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
_proto(user32.SetWindowDisplayAffinity, wt.BOOL, wt.HWND, wt.DWORD)
_proto(user32.GetDC, wt.HDC, wt.HWND)
_proto(user32.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
_proto(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
_proto(user32.MonitorFromPoint, wt.HMONITOR, wt.POINT, wt.DWORD)
_proto(user32.GetMonitorInfoW, wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFO))
_proto(user32.UpdateLayeredWindow, wt.BOOL, wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
       wt.HDC, ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD)
_proto(user32.PeekMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
_proto(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
_proto(user32.DispatchMessageW, ctypes.c_ssize_t, ctypes.POINTER(wt.MSG))
_proto(gdi32.CreateCompatibleDC, wt.HDC, wt.HDC)
_proto(gdi32.CreateDIBSection, wt.HBITMAP, wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
       wt.HANDLE, wt.DWORD)
_proto(gdi32.SelectObject, wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_proto(gdi32.DeleteObject, wt.BOOL, wt.HGDIOBJ)
_proto(gdi32.DeleteDC, wt.BOOL, wt.HDC)
_proto(gdi32.GetDeviceCaps, ctypes.c_int, wt.HDC, ctypes.c_int)
_proto(shcore.GetDpiForMonitor, ctypes.c_long, wt.HMONITOR, ctypes.c_int, ctypes.POINTER(wt.UINT),
       ctypes.POINTER(wt.UINT))
_proto(dwmapi.DwmFlush, ctypes.c_long)
_proto(user32.GetWindowLongW, ctypes.c_long, wt.HWND, ctypes.c_int)
_proto(user32.SetWindowLongW, ctypes.c_long, wt.HWND, ctypes.c_int, ctypes.c_long)
_proto(user32.SetWindowPos, wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
_proto(winmm.timeBeginPeriod, wt.UINT, wt.UINT)


def _font(names, size):
    for n in names:
        p = os.path.join(FONTS, n)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def ease_out(x):
    return 1 - (1 - min(1.0, max(0.0, x))) ** 3


class _Surface:
    """A reusable 32-bit DIB for UpdateLayeredWindow (recreated only when the size changes)."""

    def __init__(self):
        self.size = None
        self.mem = self.bmp = self.old = self.bits = None

    def ensure(self, w, h):
        if self.size == (w, h):
            return
        self.free()
        screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.mem, self.bmp)
        self.size = (w, h)

    def free(self):
        if self.mem:
            gdi32.SelectObject(self.mem, self.old)
            gdi32.DeleteObject(self.bmp)
            gdi32.DeleteDC(self.mem)
        self.size = self.mem = None


class Hud:
    """Thread-safe facade: show()/hide() and the `level` attribute may be set from any thread.
    The window lives on, and is drawn by, the Hud's own thread."""

    def __init__(self, root=None, on_error=None):
        self.on_error = on_error or (lambda msg: None)
        self.mode = None
        self.title = ""
        self.subtitle = ""
        self.right = ""          # e.g. the timer
        self.progress = None     # 0..1 or None
        self.level = 0.0         # mic level 0..1
        self.pen_button = False  # show the clickable pencil (while recording)
        self.pen_on = False      # pencil highlighted (drawing is on)
        self.buttons = {}        # clickable boxes on the pill, screen px: {"draw": rect, "stop": rect}
        self.pill_rect = None    # the pill's box, screen px
        self._clickable = False
        self.visible = False
        self.shown_at = 0.0
        self.hide_at = None      # auto-hide deadline
        self.hiding_since = None
        self.width = None        # animated logical width
        self.prog_drawn = 0.0
        self.level_drawn = 0.0
        self.scale = 1.0
        self.anchor = (0, 0, 1920)  # left, top, width of the work area (physical px)
        self.fps = 0.0
        self._replace = False
        self._lock = threading.Lock()
        self._fonts_for = None
        self._caps = None
        self._text_cache = {}
        self._surface = _Surface()
        self.capture_excluded = False
        self.hwnd = None
        ready = threading.Event()
        threading.Thread(target=self._thread, args=(ready,), daemon=True, name="hud").start()
        ready.wait(5)

    # ---------- public ----------
    def show(self, mode, title, subtitle="", right="", progress=None, hold=None):
        with self._lock:
            if not self.visible or self.hiding_since is not None:
                self._replace = True
            if mode != self.mode and mode == "processing":
                self.prog_drawn = 0.0
            self.mode, self.title, self.subtitle, self.right = mode, title, subtitle, right
            self.progress = progress
            self.hide_at = time.monotonic() + hold if hold else None
            self.visible = True

    def hide(self):
        with self._lock:
            if self.visible and self.hiding_since is None:
                self.hiding_since = time.monotonic()

    def set_theme(self, name):
        with self._lock:
            apply_theme(name)
            self._caps = None
            self._text_cache = {}
        self.theme = name

    # ---------- thread ----------
    def _thread(self, ready):
        ex = 0x00080000 | 0x00000020 | 0x08000000 | 0x80 | 0x8  # LAYERED|TRANSPARENT|NOACTIVATE|TOOLWINDOW|TOPMOST
        # A plain native popup (the STATIC class needs no window procedure of ours). Tk windows
        # can't be used: their class has a private DC, which rules out per-pixel alpha.
        self.hwnd = user32.CreateWindowExW(ex, "STATIC", "Lookhere", 0x80000000,  # WS_POPUP
                                           -200, -200, 1, 1, None, None, None, None)
        self.capture_excluded = bool(user32.SetWindowDisplayAffinity(self.hwnd, 0x11))  # WDA_EXCLUDEFROMCAPTURE
        winmm.timeBeginPeriod(1)
        # Draw on every display refresh up to ~120Hz; on faster screens every 2nd/3rd refresh
        # (165Hz -> 82fps), which stays perfectly even and halves the CPU cost.
        dc = user32.GetDC(None)
        refresh = gdi32.GetDeviceCaps(dc, 116) or 60  # VREFRESH
        user32.ReleaseDC(None, dc)
        vsyncs_per_frame = max(1, int(refresh // 70))
        self.target_fps = refresh / vsyncs_per_frame
        ready.set()
        msg = wt.MSG()
        shown = False
        frames, fps_t = 0, time.monotonic()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                if self._step():
                    if not shown:
                        user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                        shown = True
                    frames += 1
                    for _ in range(vsyncs_per_frame):
                        if dwmapi.DwmFlush() != 0:  # wait for the next display refresh (vsync)
                            time.sleep(1 / 60)
                            break
                else:
                    if shown:
                        user32.ShowWindow(self.hwnd, 0)
                        shown = False
                    time.sleep(0.02)
            except Exception:
                self.on_error("hud frame failed:\n" + traceback.format_exc())
                time.sleep(0.2)
            now = time.monotonic()
            if now - fps_t >= 1.0:
                self.fps, frames, fps_t = frames / (now - fps_t), 0, now

    def _step(self):
        """Advance animations and draw one frame. Returns False when nothing is on screen."""
        with self._lock:
            if not self.visible:
                return False
            now = time.monotonic()
            if self._replace:
                self._replace = False
                self._place_on_cursor_monitor()
                self.shown_at, self.hiding_since, self.width = now, None, None
            if self.hide_at and now >= self.hide_at:
                self.hide_at = None
                self.hiding_since = now
            if self.hiding_since is not None:
                k = (now - self.hiding_since) / 0.18
                if k >= 1:
                    self.visible = False
                    self.hiding_since = None
                    return False
                alpha, dy = 1 - ease_out(k), -6 * ease_out(k)
            else:
                k = ease_out((now - self.shown_at) / 0.24)
                alpha, dy = k, -12 * (1 - k)
            state = (self.mode, self.title, self.subtitle, self.right, self.progress, self.level)
        clickable = self.pen_button and state[0] in BUTTON_MODES
        if clickable != self._clickable:  # click-through except while the pencil is offered
            self._clickable = clickable
            ex = user32.GetWindowLongW(self.hwnd, -20)
            user32.SetWindowLongW(self.hwnd, -20, (ex & ~0x20) if clickable else (ex | 0x20))
        img, px, py = self._render(now, dy, *state)
        self._blit(img, px, py, int(255 * max(0.0, min(1.0, alpha))))
        return True

    # ---------- layout ----------
    def _place_on_cursor_monitor(self):
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        dx, dy = wt.UINT(96), wt.UINT(96)
        shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        self.scale = (dx.value or 96) / 96.0
        w = mi.rcWork
        self.anchor = (w.left, w.top, w.right - w.left)

    def _fonts(self):
        key = round(self.scale * SS, 3)
        if self._fonts_for != key:
            f = self.scale * SS
            self.f_title = _font(["seguisb.ttf", "segoeui.ttf"], round(15 * f))
            self.f_sub = _font(["segoeui.ttf"], round(12.5 * f))
            self.f_right = _font(["seguisb.ttf", "segoeui.ttf"], round(15 * f))
            self._fonts_for = key
            self._caps = None
            self._text_cache = {}
        return self.f_title, self.f_sub, self.f_right

    def _target_width(self, mode, title, subtitle, right):
        ft, fs, fr = self._fonts()
        u = self.scale * SS
        text_w = max(ft.getlength(title), fs.getlength(subtitle)) / u
        w = TEXT_X + text_w + PAD_R
        if mode in ("recording", "mic"):
            w += 46
        if self.pen_button and mode in BUTTON_MODES:
            w += 88
        if right:
            w += fr.getlength("0:00:00" if len(right) > 5 else "00:00") / u + 14
        return max(300.0, w)

    # ---------- cached pieces ----------
    def _cap_images(self):
        """Left cap, 1px middle column and right cap of shadow + pill + border, at 1x."""
        if self._caps is None:
            s, u = self.scale, self.scale * SS
            full_w = PILL_H + 40  # a pill wide enough to cut caps and a middle column from
            W, H = int(math.ceil((full_w + MARGIN * 2) * s)), int(math.ceil((PILL_H + MARGIN * 2) * s))
            sh = Image.new("L", (W, H), 0)
            ImageDraw.Draw(sh).rounded_rectangle(
                (MARGIN * s, (MARGIN + 5) * s, (MARGIN + full_w) * s, (MARGIN + 5 + PILL_H) * s),
                radius=PILL_H / 2 * s, fill=85)
            sh = sh.filter(ImageFilter.GaussianBlur(9 * s))
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            img.putalpha(sh)
            big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
            box = (MARGIN * u, MARGIN * u, (MARGIN + full_w) * u, (MARGIN + PILL_H) * u)
            ImageDraw.Draw(big).rounded_rectangle(box, radius=PILL_H / 2 * u, fill=BG)
            edge = Image.new("RGBA", big.size, (0, 0, 0, 0))
            ImageDraw.Draw(edge).rounded_rectangle(box, radius=PILL_H / 2 * u, outline=BORDER,
                                                   width=max(1, round(1 * u)))
            big.alpha_composite(edge)
            img.alpha_composite(big.resize((W, H), Image.LANCZOS))
            cap = int(math.ceil((MARGIN + PILL_H / 2 + 2) * s))
            mid_x = W // 2
            self._caps = (img.crop((0, 0, cap, H)), img.crop((mid_x, 0, mid_x + 1, H)),
                          img.crop((W - cap, 0, W, H)))
        return self._caps

    def _body(self, W, H):
        left, mid, right = self._cap_images()
        body = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        body.paste(left, (0, 0))
        span = W - left.width - right.width
        if span > 0:
            body.paste(mid.resize((span, H)), (left.width, 0))
        body.paste(right, (W - right.width, 0))
        return body

    def _text(self, text, font, fill):
        key = (text, id(font), fill)
        img = self._text_cache.get(key)
        if img is None:
            if len(self._text_cache) > 64:
                self._text_cache = {}
            u = self.scale * SS
            asc, desc = font.getmetrics()
            w = int(font.getlength(text)) + 4
            big = Image.new("RGBA", (max(1, w), asc + desc), (0, 0, 0, 0))
            ImageDraw.Draw(big).text((0, 0), text, font=font, fill=fill)
            img = big.resize((max(1, round(big.width / SS)), max(1, round(big.height / SS))), Image.LANCZOS)
            img.info["ascent"] = asc / SS
            self._text_cache[key] = img
        return img

    # ---------- drawing ----------
    def _render(self, now, dy, mode, title, subtitle, right, progress, level):
        s = self.scale
        target = self._target_width(mode, title, subtitle, right)
        self.width = target if self.width is None else self.width + (target - self.width) * 0.18
        if abs(self.width - target) < 0.5:
            self.width = target
        W = int(round((self.width + MARGIN * 2) * s))
        H = int(math.ceil((PILL_H + MARGIN * 2) * s))
        img = self._body(W, H)

        x0, y0 = MARGIN * s, MARGIN * s
        x1 = W - MARGIN * s
        cy = y0 + PILL_H / 2 * s

        ft, fs, fr = self._fonts()
        t_img = self._text(title, ft, TEXT)
        img.alpha_composite(t_img, (int(x0 + TEXT_X * s), int(cy - 3 * s - t_img.info["ascent"])))
        s_img = self._text(subtitle, fs, MUTED)
        img.alpha_composite(s_img, (int(x0 + TEXT_X * s), int(cy + 15 * s - s_img.info["ascent"])))

        rx = x1 - PAD_R * s
        if right:
            r_img = self._text(right, fr, TEXT)
            img.alpha_composite(r_img, (int(rx - r_img.width), int(cy + 5.5 * s - r_img.info["ascent"])))
            rx -= r_img.width + 14 * s
        boxes = {}
        if self.pen_button and mode in BUTTON_MODES:
            for name in ("stop", "draw"):  # right to left: stop sits next to the timer
                bx = rx - 16 * s
                (self._stop_button if name == "stop" else self._pen_button)(img, bx, cy)
                boxes[name] = (bx - 18 * s, cy - 18 * s, bx + 18 * s, cy + 18 * s)
                rx -= 44 * s

        icon_kind, color = MODES.get(mode, ("dot", GREY))
        self._icon(img, icon_kind, color, x0 + ICON_X * s, cy, now)
        if mode in ("recording", "mic"):
            self._meter(img, rx, cy, now, level)
        if mode == "processing" and progress is not None:
            self._progress(img, x0, x1, y0 + PILL_H * s, progress)

        left, top, aw = self.anchor
        px = int(left + (aw - W) / 2)
        py = int(top + (14 + dy) * s - MARGIN * s + 6 * s)
        self.buttons = {k: (int(px + b[0]), int(py + b[1]), int(px + b[2]), int(py + b[3])) for k, b in boxes.items()}
        self.pill_rect = (int(px + x0), int(py + y0), int(px + x1), int(py + y0 + PILL_H * s))
        return img, px, py

    def _pen_button(self, img, cx, cy):
        """Round pencil button: subtle when off, lime when drawing."""
        size = 34.0
        patch, d, u = self._patch(size, size)
        c = size / 2 * u
        r = 15 * u
        if self.pen_on:
            d.ellipse((c - r, c - r, c + r, c + r), fill=ACCENT + (255,))
            ink = (12, 12, 12)
        else:
            d.ellipse((c - r, c - r, c + r, c + r), fill=TEXT[:3] + (26,))
            ink = TEXT[:3]
        self._pen_onto(patch, c, u, ink)
        s = self.scale
        self._paste_patch(img, patch, cx - size / 2 * s, cy - size / 2 * s)

    def _stop_button(self, img, cx, cy):
        """Round stop button: a red square, like a recorder's stop key."""
        size = 34.0
        patch, d, u = self._patch(size, size)
        c = size / 2 * u
        r = 15 * u
        d.ellipse((c - r, c - r, c + r, c + r), fill=TEXT[:3] + (26,))
        k = 5.2 * u
        d.rounded_rectangle((c - k, c - k, c + k, c + k), radius=1.8 * u, fill=RED + (255,))
        s = self.scale
        self._paste_patch(img, patch, cx - size / 2 * s, cy - size / 2 * s)

    @staticmethod
    def _pen_glyph(d, c, u, ink):
        """A pencil at 45 degrees, tip bottom-left: graphite point, sharpened wood, body, metal band
        and eraser. Gaps are cut with transparent pixels so it reads cleanly at small sizes."""
        L, hw = 20.0, 3.6                      # length and half-width (logical px)
        tip = (c - 7.1 * u, c + 7.1 * u)
        ax, ay = 0.7071, -0.7071               # along the pencil, towards the eraser
        nx, ny = 0.7071, 0.7071                # across it

        def at(s, k=0.0):  # point s along the axis, k across
            return (tip[0] + (ax * s + nx * k) * u, tip[1] + (ay * s + ny * k) * u)

        solid, cut = ink + (255,), (0, 0, 0, 0)
        d.polygon([at(0), at(6, -hw), at(6, hw)], fill=solid)                              # wood cone
        d.polygon([at(6, -hw), at(L - 4.6, -hw), at(L - 4.6, hw), at(6, hw)], fill=solid)  # body
        d.polygon([at(L - 3.4, -hw), at(L - 0.9, -hw), at(L, -hw + 0.9), at(L, hw - 0.9),
                   at(L - 0.9, hw), at(L - 3.4, hw)], fill=solid)                           # eraser
        gap = max(1, round(1.1 * u))
        d.line([at(6, -hw - 1), at(6, hw + 1)], fill=cut, width=gap)                        # wood | body
        d.line([at(L - 4, -hw - 1), at(L - 4, hw + 1)], fill=cut, width=gap)                # body | band
        d.line([at(2.4, -hw), at(2.4, hw)], fill=cut, width=max(1, round(0.9 * u)))         # graphite tip

    def _pen_onto(self, patch, c, u, ink):
        """Draw the pencil on its own layer, so its cut-out gaps don't punch through what's under it."""
        layer = Image.new("RGBA", patch.size, (0, 0, 0, 0))
        self._pen_glyph(ImageDraw.Draw(layer), c, u, ink)
        patch.alpha_composite(layer)

    def bring_to_front(self):
        """Keep the pill above other topmost windows (the drawing layer)."""
        user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def _patch(self, w, h):
        """A small supersampled scratch layer; returns (image, draw, unit)."""
        u = self.scale * SS
        im = Image.new("RGBA", (int(math.ceil(w * u)), int(math.ceil(h * u))), (0, 0, 0, 0))
        return im, ImageDraw.Draw(im), u

    def _paste_patch(self, img, patch, x, y):
        small = patch.resize((max(1, round(patch.width / SS)), max(1, round(patch.height / SS))), Image.LANCZOS)
        img.alpha_composite(small, (int(round(x)), int(round(y))))

    def _icon(self, img, kind, color, cx, cy, now):
        size = 34.0  # 30px icon plus room so the chip's edge isn't clipped
        patch, d, u = self._patch(size, size)
        c = size / 2 * u
        R = 11 * u
        track = TEXT[:3] + (34,)
        if CHIP and kind in ("spinner", "key", "check"):
            d.ellipse((c - 15 * u, c - 15 * u, c + 15 * u, c + 15 * u), fill=CHIP + (255,))
            track = (255, 255, 255, 46)
            R = 9.5 * u if kind != "check" else 11.5 * u
        if kind == "spinner":
            d.ellipse((c - R, c - R, c + R, c + R), outline=track, width=round(3 * u))
            a = (now * 360 * 1.15) % 360
            sweep = 70 + 60 * (1 + math.sin(now * 3.2)) / 2
            d.arc((c - R, c - R, c + R, c + R), a, a + sweep, fill=color + (255,), width=round(3 * u))
        elif kind == "dot":
            p = (1 + math.sin(now * 4.2)) / 2
            halo = R * (0.75 + 0.45 * p)
            d.ellipse((c - halo, c - halo, c + halo, c + halo), fill=color + (int(70 * (1 - p) + 25),))
            r = 6.5 * u
            d.ellipse((c - r, c - r, c + r, c + r), fill=color + (255,))
        elif kind == "pause":
            for x in (-3.6, 3.6):
                d.rounded_rectangle((c + x * u - 1.7 * u, c - 6.5 * u, c + x * u + 1.7 * u, c + 6.5 * u),
                                    radius=1.7 * u, fill=color + (255,))
        elif kind == "pen":
            if CHIP:
                d.ellipse((c - 15 * u, c - 15 * u, c + 15 * u, c + 15 * u), fill=CHIP + (255,))
            self._pen_onto(patch, c, u, color)
        elif kind == "key":  # a keycap
            k = (7.5 if CHIP else 10) * u
            d.rounded_rectangle((c - k, c - k, c + k, c + k), radius=4 * u, outline=color + (255,), width=round(2.4 * u))
            d.rounded_rectangle((c - 4.5 * u, c + 2 * u, c + 4.5 * u, c + 4.6 * u), radius=1.2 * u, fill=color + (255,))
        else:
            d.ellipse((c - R, c - R, c + R, c + R), fill=color + (255,))
            w = round(2.4 * u)
            # glyph contrasts with its circle (dark tick on a light circle, white on a dark one)
            white = (20, 20, 20, 255) if sum(color[:3]) / 3 > 170 else (255, 255, 255, 255)
            if kind == "check":
                d.line((c - 5 * u, c + 0.5 * u, c - 1.5 * u, c + 4 * u, c + 5.5 * u, c - 4 * u),
                       fill=white, width=w, joint="curve")
            elif kind == "cross":
                k = 4.2 * u
                d.line((c - k, c - k, c + k, c + k), fill=white, width=w)
                d.line((c - k, c + k, c + k, c - k), fill=white, width=w)
            elif kind == "bang":
                d.line((c, c - 5 * u, c, c + 1.5 * u), fill=white, width=w)
                d.ellipse((c - 1.4 * u, c + 3.6 * u, c + 1.4 * u, c + 6.4 * u), fill=white)
        s = self.scale
        self._paste_patch(img, patch, cx - size / 2 * s, cy - size / 2 * s)

    def _meter(self, img, rx, cy, now, level):
        self.level_drawn += (level - self.level_drawn) * (0.3 if level > self.level_drawn else 0.08)
        bars, bw, gap = 5, 3.2, 3.2
        total = bars * bw + (bars - 1) * gap
        patch, d, u = self._patch(total, 26)
        mid = 13 * u
        x = 0.0
        for i in range(bars):
            wob = 0.55 + 0.45 * (1 + math.sin(now * 9 + i * 1.7)) / 2
            centre = 1 - abs(i - 2) / 3
            h = max(3.5, 3.5 + 17 * self.level_drawn * wob * (0.55 + 0.45 * centre)) * u
            a = int(110 + 145 * min(1.0, self.level_drawn * 1.6))
            d.rounded_rectangle((x * u, mid - h / 2, (x + bw) * u, mid + h / 2), radius=bw / 2 * u,
                                fill=TEXT[:3] + (a,))
            x += bw + gap
        s = self.scale
        self._paste_patch(img, patch, rx - total * s, cy - 13 * s)

    def _progress(self, img, x0, x1, y1, progress):
        self.prog_drawn += (progress - self.prog_drawn) * 0.06
        s = self.scale
        inset = 26
        length = (x1 - x0) / s - inset * 2
        patch, d, u = self._patch(length, 3)
        d.rounded_rectangle((0, 0, length * u, 3 * u), radius=1.5 * u,
                            fill=CHIP + (255,) if CHIP else TEXT[:3] + (28,))
        fill_to = length * max(0.02, min(1.0, self.prog_drawn))
        d.rounded_rectangle((0, 0, fill_to * u, 3 * u), radius=1.5 * u, fill=ACCENT + (255,))
        self._paste_patch(img, patch, x0 + inset * s, y1 - 8.5 * s)

    def _blit(self, img, x, y, alpha):
        w, h = img.size
        a = np.asarray(img)
        alpha_ch = a[:, :, 3:4].astype(np.uint16)
        bgra = np.empty((h, w, 4), np.uint8)
        bgra[:, :, :3] = (a[:, :, 2::-1] * alpha_ch // 255)  # premultiplied, RGB -> BGR
        bgra[:, :, 3] = a[:, :, 3]
        self._surface.ensure(w, h)
        ctypes.memmove(self._surface.bits, bgra.ctypes.data, w * h * 4)
        blend = BLENDFUNCTION(0, 0, alpha, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        if not user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                          self._surface.mem, ctypes.byref(wt.POINT(0, 0)), 0,
                                          ctypes.byref(blend), 2):  # ULW_ALPHA
            raise OSError("UpdateLayeredWindow failed, error %d" % ctypes.get_last_error())

    def snapshot(self, mode, title, subtitle="", right="", progress=None, level=0.0, t=0.4):
        """Render one state to a PIL image, for previews and tests."""
        with self._lock:
            self.width = None
            self.level_drawn = level
            self.prog_drawn = progress or 0.0
            img, _, _ = self._render(t, 0, mode, title, subtitle, right, progress, level)
        return img

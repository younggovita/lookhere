"""The tray's right-click menu, drawn in the app's own style (black or white with acid lime)
instead of the plain Windows menu. Rounded panel, hover highlight, lime ticks, flyout
submenus, items greyed out when they don't apply. Runs on the app's Tk thread.
"""
import ctypes
import ctypes.wintypes as wt
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk
from pynput import mouse

import hud

ROW, SEP, PAD, W = 34, 11, 6, 300  # row height, separator height, panel padding, panel width (px)


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(rgb[:3])


def palette():
    dark = hud.BG[0] < 128
    return dict(dark=dark,
                bg=(18, 18, 21) if dark else (252, 252, 250),
                hover=(40, 40, 46) if dark else (236, 236, 232),
                edge=(60, 60, 66) if dark else (214, 214, 210),
                text=tuple(hud.TEXT[:3]), muted=(140, 140, 146) if dark else (120, 120, 126),
                faint=(85, 85, 92) if dark else (190, 190, 186),
                lime=tuple(hud.ACCENT[:3]), ink=(12, 12, 12), red=tuple(hud.RED[:3]))


def _rounded(w, h, fill, r=9):
    S = 3
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=r * S, fill=fill + (255,))
    return im.resize((w, h), Image.LANCZOS)


def _tick(p, size=18):
    """A checked item: lime disc with a dark tick (reads on both light and dark)."""
    S = 4
    s = size * S
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((1, 1, s - 2, s - 2), fill=p["lime"] + (255,))
    d.line([(s * .28, s * .52), (s * .44, s * .68), (s * .73, s * .34)], fill=p["ink"] + (255,), width=int(2.2 * S),
           joint="curve")
    return im.resize((size, size), Image.LANCZOS)


def _round_corners(top, edge):
    """Windows 11 rounded corners and a thin themed border on the menu window."""
    try:
        hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        colour = ctypes.c_uint(edge[0] | (edge[1] << 8) | (edge[2] << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(colour), 4)
    except Exception:
        pass


class Menu:
    """items: list of dicts with "label", optional "hint" (right side), "action", "checked"
    (callable), "enabled" (callable), "submenu" (callable returning items), or {"sep": True}."""

    def __init__(self, root, items, x, y, on_close, post, parent=None, header=None, align="bottom-right"):
        self.root, self.items, self.on_close, self.parent, self.post = root, items, on_close, parent, post
        self.child = None
        self.p = p = palette()
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=hexc(p["bg"]))
        self.header = header
        h = PAD * 2 + (56 if header else 0) + sum(SEP if it.get("sep") else ROW for it in items)
        self.c = tk.Canvas(self.top, width=W, height=h, bg=hexc(p["bg"]), highlightthickness=0, bd=0)
        self.c.pack()
        self.images = []
        self.rows = []  # (y0, y1, item)
        self._draw(h)
        # place it so it opens away from the click and stays on the screen
        self.top.update_idletasks()
        work = self._work_area(x, y)
        if align == "bottom-right":
            px, py = x - W, y - h
        else:  # a submenu beside its parent row
            px, py = x, y
            if px + W > work[2]:
                px = x - W - (parent.top.winfo_width() if parent else 0)
        px = max(work[0] + 4, min(px, work[2] - W - 4))
        py = max(work[1] + 4, min(py, work[3] - h - 4))
        self.top.geometry("%dx%d+%d+%d" % (W, h, px, py))
        self.rect = (px, py, px + W, py + h)  # kept for the click-outside check (another thread)
        self.top.update_idletasks()
        _round_corners(self.top, p["edge"])
        self.c.bind("<Motion>", self._motion)
        self.c.bind("<Leave>", lambda e: self._hover(None))
        self.c.bind("<ButtonRelease-1>", self._click)
        self.top.bind("<Escape>", lambda e: self.close_all())
        self.hovered = None
        if parent is None:
            self.top.focus_force()
            # close when someone clicks anywhere outside the menu(s)
            self.listener = mouse.Listener(on_click=self._outside)
            self.listener.start()

    # ---------- drawing ----------
    def _work_area(self, x, y):
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        u = ctypes.windll.user32
        u.MonitorFromPoint.restype = wt.HMONITOR
        u.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        u.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        u.GetMonitorInfoW(u.MonitorFromPoint(wt.POINT(int(x), int(y)), 2), ctypes.byref(mi))
        r = mi.rcWork
        return (r.left, r.top, r.right, r.bottom)

    def _draw(self, h):
        p, c = self.p, self.c
        c.delete("all")
        self.hl = c.create_image(0, 0, anchor="nw", state="hidden")
        self._hl_img = ImageTk.PhotoImage(_rounded(W - PAD * 2, ROW - 4, p["hover"]))
        c.itemconfigure(self.hl, image=self._hl_img)
        y = PAD
        if self.header:
            import logo
            img = ImageTk.PhotoImage(logo.draw_logo(logo.IDLE, 30))
            self.images.append(img)
            c.create_image(PAD + 12, y + 12, image=img, anchor="nw")
            c.create_text(PAD + 52, y + 13, text="Lookhere", anchor="nw", fill=hexc(p["text"]),
                          font=("Segoe UI Semibold", 11))
            c.create_text(PAD + 52, y + 32, text=self.header, anchor="nw", fill=hexc(p["muted"]),
                          font=("Segoe UI", 9))
            y += 50
            c.create_line(PAD + 10, y + 3, W - PAD - 10, y + 3, fill=hexc(p["hover"]))
            y += 6
        tick = ImageTk.PhotoImage(_tick(p))
        self.images.append(tick)
        for it in self.items:
            if it.get("sep"):
                c.create_line(PAD + 10, y + SEP // 2, W - PAD - 10, y + SEP // 2, fill=hexc(p["hover"]))
                y += SEP
                continue
            enabled = it.get("enabled", lambda: True)()
            colour = p["text"] if enabled else p["faint"]
            if it.get("danger") and enabled:
                colour = p["red"]
            label = it["label"]() if callable(it["label"]) else it["label"]
            c.create_text(PAD + 38, y + ROW // 2, text=label, anchor="w", fill=hexc(colour), font=("Segoe UI", 10))
            if it.get("checked") and it["checked"]():
                c.create_image(PAD + 12, y + ROW // 2, image=tick, anchor="w")
            hint = it.get("hint")
            if callable(hint):
                hint = hint()
            if it.get("submenu"):
                c.create_text(W - PAD - 14, y + ROW // 2, text="›", anchor="e", fill=hexc(p["muted"]),
                              font=("Segoe UI", 14))
            elif hint:
                c.create_text(W - PAD - 14, y + ROW // 2, text=hint, anchor="e",
                              fill=hexc(p["muted"] if enabled else p["faint"]), font=("Segoe UI", 9))
            self.rows.append((y, y + ROW, it, enabled))
            y += ROW
        c.tag_lower(self.hl)

    # ---------- interaction ----------
    def _row_at(self, y):
        for y0, y1, it, enabled in self.rows:
            if y0 <= y < y1:
                return (y0, it, enabled)
        return None

    def _motion(self, e):
        self._hover(self._row_at(e.y))

    def _hover(self, row):
        if row == self.hovered:
            return
        self.hovered = row
        if row and row[2]:
            self.c.coords(self.hl, PAD, row[0] + 2)
            self.c.itemconfigure(self.hl, state="normal")
        else:
            self.c.itemconfigure(self.hl, state="hidden")
        it = row[1] if row else None
        if self.child and (not it or it.get("submenu") is not self.child.source):
            self.child.close()
            self.child = None
        if it and it.get("submenu") and row[2] and not self.child:
            x = self.top.winfo_rootx() + W - 2
            y = self.top.winfo_rooty() + row[0] - PAD
            self.child = Menu(self.root, it["submenu"](), x, y, self.on_close, self.post, parent=self,
                              align="beside")
            self.child.source = it["submenu"]

    def _click(self, e):
        row = self._row_at(e.y)
        if not row or not row[2] or row[1].get("submenu"):
            return
        action = row[1].get("action")
        self.close_all()
        if action:
            action()

    def _inside(self, x, y):
        m = self
        while m:
            r = getattr(m, "rect", None)
            if r and r[0] <= x < r[2] and r[1] <= y < r[3]:
                return True
            m = m.child
        return False

    def _outside(self, x, y, button, pressed):
        if pressed and not self._inside(x, y):
            self.post(self.close_all)  # run on the Tk thread

    def close(self):
        if self.child:
            self.child.close()
            self.child = None
        if self.top.winfo_exists():
            self.top.destroy()

    def close_all(self):
        m = self
        while m.parent:
            m = m.parent
        if getattr(m, "listener", None):
            m.listener.stop()
            m.listener = None
        m.close()
        m.on_close()

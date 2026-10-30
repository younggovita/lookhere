"""Settings window (tray > Settings): microphone, theme, shortcuts, start with Windows and the
recordings folder on one page, styled like History. Changes go through the app's event queue
(the same handlers the tray used), and the app calls refresh() after each one so the page shows
the new value. Runs on the app's Tk thread.
"""
import os
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

import history
from history import Pill, hexc, palette, round_rect

_win = None
W = 520  # card width (px)


def open_window(app):
    global _win
    if _win is not None and _win.top.winfo_exists():
        _win.refresh()
        _win.top.deiconify()
        _win.top.lift()
        _win.top.focus_force()
        return
    _win = SettingsWindow(app)


def refresh():
    """Called by the app after a setting changed (theme, mic, shortcut, autostart)."""
    if _win is not None and _win.top.winfo_exists():
        _win.refresh()


def switch_image(on, p, w=44, h=24):
    S = 3
    im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w * S - 1, h * S - 1), radius=h * S // 2,
                        fill=(p["lime"] if on else p["button_hover"]) + (255,))
    r = (h - 6) * S // 2
    cx = (w - h // 2) * S if on else h * S // 2
    d.ellipse((cx - r, h * S // 2 - r, cx + r, h * S // 2 + r), fill=(p["ink"] if on else p["text"]) + (255,))
    return im.resize((w, h), Image.LANCZOS)


def key_chip(text, p, height=28):
    """A shortcut shown as a keycap-style chip."""
    S = 3
    f = history._font(12 * S)
    w = int(f.getlength(text)) + 24 * S
    im = Image.new("RGBA", (w, height * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, w - 1, height * S - 1), radius=8 * S, fill=p["bg"] + (255,),
                        outline=p["button_hover"] + (255,), width=S)
    d.text((w / 2, height * S / 2), text, font=f, fill=p["text"] + (255,), anchor="mm")
    return im.resize((w // S, height), Image.LANCZOS)


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title("Lookhere settings")
        self.top.resizable(False, False)
        try:
            self.top.iconbitmap(os.path.join(history.paths.RES, "lookhere.ico"))
        except tk.TclError:
            pass
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.body = None
        self.refresh()
        # open near the middle of the screen
        self.top.update_idletasks()
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        self.top.geometry("+%d+%d" % ((sw - self.top.winfo_reqwidth()) // 2, max(40, (sh - self.top.winfo_reqheight()) // 3)))
        self.top.focus_force()

    def close(self):
        global _win
        self.top.destroy()
        _win = None

    # ---------- building ----------
    def refresh(self):
        """Rebuild the page from the current settings (and theme, which may just have changed)."""
        import screen_explain as se
        self.p = p = palette()
        self.images = []
        self.top.configure(bg=hexc(p["bg"]))
        history.dark_title_bar(self.top, p["dark"])
        if self.body:
            self.body.destroy()
        self.body = tk.Frame(self.top, bg=hexc(p["bg"]))
        self.body.pack(padx=24, pady=(20, 24))
        put = self.app.events.put

        head = tk.Frame(self.body, bg=hexc(p["bg"]))
        head.pack(fill="x", pady=(0, 6))
        import logo
        img = ImageTk.PhotoImage(logo.draw_logo(logo.IDLE, 34))
        self.images.append(img)
        tk.Label(head, image=img, bg=hexc(p["bg"])).pack(side="left", padx=(0, 12))
        tk.Label(head, text="Settings", bg=hexc(p["bg"]), fg=hexc(p["text"]),
                 font=("Segoe UI Semibold", 18)).pack(side="left")

        # microphone: pick one (picking runs the mic test), or test the current one
        card = self.card("Microphone", "Test", lambda: put(("mic", None)))
        for name in [""] + se.input_devices():
            self.choice(card, name or "Windows default", se.CONFIG["mic"] == name,
                        (lambda v: lambda: put(("mic", v)))(name))

        # theme
        card = self.card("Theme")
        row = tk.Frame(card, bg=hexc(p["card"]))
        row.pack(anchor="w", pady=4)
        for label, value in (("Follow Windows", "auto"), ("Dark", "dark"), ("Light", "light")):
            on = se.CONFIG["theme"] == value
            Pill(row, label, (lambda v: lambda: put(("theme", v)))(value), p, p["card"],
                 "lime" if on else "plain").pack(side="left", padx=(0, 6))

        # shortcuts: click Change, then press the new keys (the pill at the top asks)
        card = self.card("Shortcuts")
        capturing = self.app.capture and getattr(self.app, "capture_target", None)
        for key, title in (("hotkey", "Record / finish"), ("pause_hotkey", "Pause / resume"),
                           ("draw_hotkey", "Draw on screen")):
            row = self.row(card, title)
            busy = capturing == key
            Pill(row, "Cancel" if busy else "Change",
                 (lambda k: lambda: put(("change_hotkey", k)))(key) if not busy else
                 lambda: self.app.capture and self.app.capture._finish(("capture_cancelled", "button")),
                 p, p["card"]).pack(side="right")
            chip = ImageTk.PhotoImage(key_chip("Press keys..." if busy else self.app.label(key), p))
            self.images.append(chip)
            tk.Label(row, image=chip, bg=hexc(p["card"])).pack(side="right", padx=10)
        tk.Label(card, text="Click Change, then press the new keys. Keys another app already uses are refused.",
                 bg=hexc(p["card"]), fg=hexc(p["muted"]), font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))

        # general
        card = self.card("General")
        row = self.row(card, "Start with Windows")
        sw = ImageTk.PhotoImage(switch_image(se.autostart_enabled(), p))
        self.images.append(sw)
        lab = tk.Label(row, image=sw, bg=hexc(p["card"]), cursor="hand2")
        lab.pack(side="right")
        lab.bind("<Button-1>", lambda e: put("toggle_autostart"))
        row = self.row(card, "Recordings folder")
        Pill(row, "Open", lambda: put("open_folder"), p, p["card"]).pack(side="right")
        folder = se.CONFIG["recordings_dir"]
        tk.Label(card, text=folder if len(folder) < 64 else "..." + folder[-61:], bg=hexc(p["card"]),
                 fg=hexc(p["muted"]), font=("Segoe UI", 9)).pack(anchor="w")

        self.finish_cards()

    def card(self, title, action=None, command=None):
        """A section heading and a rounded card; returns the frame to fill."""
        p = self.p
        head = tk.Frame(self.body, bg=hexc(p["bg"]))
        head.pack(fill="x", pady=(16, 6))
        tk.Label(head, text=title.upper(), bg=hexc(p["bg"]), fg=hexc(p["muted"]),
                 font=("Segoe UI Semibold", 9)).pack(side="left", padx=4)
        if action:
            Pill(head, action, command, p, p["bg"]).pack(side="right")
        canvas = tk.Canvas(self.body, width=W, highlightthickness=0, bd=0, bg=hexc(p["bg"]))
        canvas.pack()
        inner = tk.Frame(canvas, bg=hexc(p["card"]))
        canvas.create_window(18, 12, window=inner, anchor="nw", width=W - 36)
        self._cards = getattr(self, "_cards", []) + [(canvas, inner)]
        return inner

    def finish_cards(self):
        """Size each card to its contents and draw its rounded background."""
        self.top.update_idletasks()
        for canvas, inner in self._cards:
            h = inner.winfo_reqheight() + 24
            canvas.configure(height=h)
            bg = ImageTk.PhotoImage(round_rect(W, h, self.p["card"], self.p["edge"], radius=14))
            self.images.append(bg)
            canvas.create_image(0, 0, image=bg, anchor="nw", tags="bg")
            canvas.tag_lower("bg")
        self._cards = []

    def row(self, card, title):
        p = self.p
        row = tk.Frame(card, bg=hexc(p["card"]))
        row.pack(fill="x", pady=5)
        tk.Label(row, text=title, bg=hexc(p["card"]), fg=hexc(p["text"]), font=("Segoe UI", 11)).pack(side="left")
        return row

    def choice(self, card, label, on, command):
        """One option in a list: a lime tick when it's the current one."""
        import traymenu
        p = self.p
        row = tk.Frame(card, bg=hexc(p["card"]), cursor="hand2")
        row.pack(fill="x", pady=2)
        tick = ImageTk.PhotoImage(traymenu._tick(dict(lime=p["lime"], ink=p["ink"])) if on else
                                  Image.new("RGBA", (18, 18), (0, 0, 0, 0)))
        self.images.append(tick)
        a = tk.Label(row, image=tick, bg=hexc(p["card"]))
        a.pack(side="left", padx=(0, 10))
        b = tk.Label(row, text=label, bg=hexc(p["card"]), fg=hexc(p["text"] if on else p["muted"]),
                     font=("Segoe UI", 10), anchor="w")
        b.pack(side="left", fill="x", expand=True)
        for w in (row, a, b):
            w.bind("<Button-1>", lambda e: command())

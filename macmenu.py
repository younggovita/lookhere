"""The menu bar menu on a Mac: traymenu.py's styled menu, same items and look, drawn with PIL at
Retina resolution into an overlay panel like the status pill. The panel never takes focus, so the
app you're in stays in front, and it follows the mouse and closes on an outside click without
the Accessibility permission a global listener needs.
"""
from PIL import Image, ImageDraw

import hud
import logo
import mac
import traymenu

ROW, SEP, PAD, W = 34, 11, 6, 300  # same metrics as traymenu.py (points)
HEADER = 56
MARGIN = 24                        # room around the panel for its shadow (points)


class Menu:
    """items: as traymenu.Menu. (x, y): where its top-left goes, in top-left points."""

    def __init__(self, items, x, y, on_close, header=None):
        self.items, self.on_close, self.header = items, on_close, header
        self.p = traymenu.palette()
        self.hovered = None
        self.closed = False
        self.h = PAD * 2 + (HEADER if header else 0) + sum(SEP if it.get("sep") else ROW for it in items)
        scr = mac.screen_at(x, y)
        self.scale = scr[8]
        self.x = min(max(x, scr[4] + 4), scr[4] + scr[6] - W - 4) - MARGIN
        self.y = y + 4 - MARGIN
        self.rows = self._layout()
        self.win = mac.Overlay(mac.overlay_level(above_drawing=True) + 1, hidden_from_capture=True,
                               on_mouse=self._mouse, track=True)
        self.win.click_through(False)
        self._paint()
        self.outside = mac.on_outside_click(self.close_all)
        self.esc = mac.Hotkey("esc", lambda: mac.on_main(self.close_all))  # refused while recording: fine

    def _layout(self):
        y, rows = PAD + (HEADER if self.header else 0), []
        for it in self.items:
            if it.get("sep"):
                y += SEP
                continue
            rows.append((y, y + ROW, it, it.get("enabled", lambda: True)()))
            y += ROW
        return rows

    def _paint(self):
        s, p = self.scale, self.p
        u = lambda v: int(round(v * s))
        img = Image.new("RGBA", (u(W + MARGIN * 2), u(self.h + MARGIN * 2)), (0, 0, 0, 0))
        box = (u(MARGIN), u(MARGIN), u(MARGIN + W), u(MARGIN + self.h))
        shadow = Image.new("L", img.size, 0)
        ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + u(5), box[2], box[3] + u(5)), radius=u(11), fill=90)
        from PIL import ImageFilter
        img.putalpha(shadow.filter(ImageFilter.GaussianBlur(u(9))))
        panel = Image.new("RGBA", (img.width * 2, img.height * 2), (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle([v * 2 for v in box], radius=u(11) * 2, fill=p["bg"] + (255,),
                                                outline=p["edge"] + (255,), width=u(1) * 2)
        img.alpha_composite(panel.resize(img.size, Image.LANCZOS))
        d = ImageDraw.Draw(img)
        ox, oy = MARGIN, MARGIN
        regular = lambda size: hud._font(["segoeui.ttf"], u(size))
        semibold = lambda size: hud._font(["seguisb.ttf", "segoeui.ttf"], u(size))
        y = PAD
        if self.header:
            mark = logo.draw_logo(logo.IDLE, u(30))
            img.alpha_composite(mark, (u(ox + PAD + 12), u(oy + y + 12)))
            d.text((u(ox + PAD + 52), u(oy + y + 13)), "Lookhere", font=semibold(14), fill=p["text"] + (255,))
            d.text((u(ox + PAD + 52), u(oy + y + 32)), self.header, font=regular(12), fill=p["muted"] + (255,))
            d.line((u(ox + PAD + 10), u(oy + y + 53), u(ox + W - PAD - 10), u(oy + y + 53)), fill=p["hover"] + (255,),
                   width=u(1))
            y += HEADER
        for it in self.items:
            if it.get("sep"):
                ly = u(oy + y + SEP / 2)
                d.line((u(ox + PAD + 10), ly, u(ox + W - PAD - 10), ly), fill=p["hover"] + (255,), width=u(1))
                y += SEP
                continue
            enabled = it.get("enabled", lambda: True)()
            if self.hovered is it and enabled:
                d.rounded_rectangle((u(ox + PAD), u(oy + y + 2), u(ox + W - PAD), u(oy + y + ROW - 2)), radius=u(9),
                                    fill=p["hover"] + (255,))
            colour = p["red"] if it.get("danger") and enabled else p["text"] if enabled else p["faint"]
            label = it["label"]() if callable(it["label"]) else it["label"]
            cy = u(oy + y + ROW / 2)
            d.text((u(ox + PAD + 14), cy), label, font=regular(13), fill=tuple(colour) + (255,), anchor="lm")
            hint = it.get("hint")
            hint = hint() if callable(hint) else hint
            if hint:
                d.text((u(ox + W - PAD - 14), cy), hint, font=regular(12),
                       fill=(p["muted"] if enabled else p["faint"]) + (255,), anchor="rm")
            y += ROW
        self.win.show(img, self.x, self.y, s)

    def _row_at(self, x, y):
        px, py = x - self.x - MARGIN, y - self.y - MARGIN
        if 0 <= px < W:
            for y0, y1, it, enabled in self.rows:
                if y0 <= py < y1:
                    return it, enabled
        return None, False

    def _mouse(self, kind, x, y):
        it, enabled = self._row_at(x, y)
        if kind == "move":
            if it is not self.hovered:
                self.hovered = it
                self._paint()
        elif kind == "up" and it is not None and enabled:
            action = it.get("action")
            self.close_all()
            if action:
                action()

    def close(self):
        self.close_all()

    def close_all(self):
        if self.closed:
            return
        self.closed = True
        self.outside.stop()
        self.esc.stop()
        self.win.close()
        self.on_close()

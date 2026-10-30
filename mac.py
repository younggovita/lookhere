"""Mac versions of the Windows calls in screen_explain.py (cursor, front window, clipboard, theme).

Coordinates are global points with the origin at the top-left of the main display, the same space
mss, pynput and the captured frames use, so the frame and cursor maths is shared with Windows.
"""
import ctypes
import os
import threading
import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString, NSURL, NSUserDefaults, NSWorkspace

# PyObjC resolves framework functions lazily, and two threads resolving the same one at once can
# fail (KeyError in objc/_lazyimport.py; it killed the frame capture thread). Resolve them here.
for _name in ("CGEventCreate", "CGEventGetLocation", "CGWindowListCopyWindowInfo", "CGImageCreate",
              "CGDataProviderCreateWithCFData", "CGColorSpaceCreateWithName", "CGGetActiveDisplayList",
              "kCGColorSpaceSRGB", "kCGImageAlphaPremultipliedLast", "kCGRenderingIntentDefault",
              "kCGWindowListOptionOnScreenOnly", "kCGWindowListExcludeDesktopElements", "kCGNullWindowID",
              "CATransaction", "CAShapeLayer", "CGPathCreateMutable", "CGPathMoveToPoint",
              "CGPathAddLineToPoint"):
    getattr(Quartz, _name)


def cursor_pos():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return round(p.x), round(p.y)


def frontmost_pid():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app else None


def foreground_title():
    """'App name: window title' for the window in front. Window titles of other apps need the
    Screen Recording permission, which recording needs anyway; without it this is just the app name."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return ""
    name = app.localizedName() or ""
    pid = app.processIdentifier()
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    for w in windows or ():
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
            title = w.get("kCGWindowName") or ""
            return "%s: %s" % (name, title) if title and title != name else name
    return name


def set_clipboard(text, files=()):
    """Files as file URLs (Cmd+V in a chat attaches them, like copying in Finder) plus the text."""
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    items = [NSURL.fileURLWithPath_(os.path.abspath(f)) for f in files if os.path.exists(f)]
    if items:
        pb.writeObjects_(items)
    pb.setString_forType_(text, NSPasteboardTypeString)
    return True


def bring_to_front(pid):
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
    return bool(app) and bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))


def theme():
    """'dark' or 'light' from System Settings > Appearance."""
    style = NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle")
    return "dark" if style == "Dark" else "light"


# ---------- overlay windows (status pill, drawing layer) ----------
SF = "/System/Library/Fonts/SFNS.ttf"  # SF Pro, a variable font


def ui_font(size, semibold=False):
    from PIL import ImageFont
    f = ImageFont.truetype(SF, size)
    f.set_variation_by_name("Semibold" if semibold else "Regular")
    return f


def screens():
    """Every display as (left, top, width, height, visible_left, visible_top, visible_width, visible_height,
    scale) in top-left points; visible excludes the menu bar and Dock."""
    from AppKit import NSScreen
    all_ = NSScreen.screens()
    main_h = all_[0].frame().size.height
    out = []
    for s in all_:
        f, v = s.frame(), s.visibleFrame()
        out.append((f.origin.x, main_h - f.origin.y - f.size.height, f.size.width, f.size.height,
                    v.origin.x, main_h - v.origin.y - v.size.height, v.size.width, v.size.height,
                    s.backingScaleFactor()))
    return out


def screen_at(x, y):
    all_ = screens()
    for s in all_:
        if s[0] <= x < s[0] + s[2] and s[1] <= y < s[1] + s[3]:
            return s
    return all_[0]


def _cgimage(rgba):
    """An RGBA uint8 array (straight alpha) as a CGImage (premultiplied)."""
    import numpy as np
    from Foundation import NSData
    a = rgba.astype(np.uint16)
    pm = np.empty(rgba.shape, np.uint8)
    pm[:, :, :3] = a[:, :, :3] * a[:, :, 3:4] // 255
    pm[:, :, 3] = rgba[:, :, 3]
    h, w = rgba.shape[:2]
    provider = Quartz.CGDataProviderCreateWithCFData(NSData.dataWithBytes_length_(pm.tobytes(), w * h * 4))
    return Quartz.CGImageCreate(w, h, 8, 32, w * 4, Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB),
                                Quartz.kCGImageAlphaPremultipliedLast, provider, None, False,
                                Quartz.kCGRenderingIntentDefault)


class Overlay:
    """A borderless window that never takes focus and shows an RGBA image: the Mac stand-in for
    the layered windows in hud.py and draw.py. Methods may be called from any thread; AppKit work
    is queued to the main thread (Tk's event loop runs it), and only the newest frame is drawn."""

    def __init__(self, level, hidden_from_capture=False, on_mouse=None, track=False):
        self.level = level
        self.track = track        # also report "move" to on_mouse
        self.on_mouse = on_mouse  # on_mouse(kind, x, y), kind "down" / "drag" / "up", on the main thread
        self.cursor = None        # an NSCursor shown over the window while it takes the mouse
        self.hidden_from_capture = hidden_from_capture
        self.win = None
        self._lock = threading.Lock()
        self._frame = None      # newest (image, x, y, scale, alpha) not yet on screen
        self._queued = False
        self._click_through = True
        self.visible = False
        self._strokes = None    # newest show_strokes() arguments not yet on screen
        self._stroke_layers = {}  # stroke id -> (layer, version)

    def show(self, img, x, y, scale=1.0, alpha=1.0):
        """img: PIL RGBA at `scale` pixels per point; (x, y): its top-left in top-left points."""
        import numpy as np
        frame = (_cgimage(np.asarray(img)), img.width, img.height, x, y, scale, alpha)
        with self._lock:
            self._frame = frame
            queue, self._queued = not self._queued, True
        if queue:
            _on_main(self._apply)

    def show_strokes(self, rect, strokes, catch_clicks, style, color, width):
        """Cover `rect` (left, top, width, height in points) with vector strokes: a list of
        (id, version, points, alpha). A stroke's path is rebuilt only when its version changes."""
        with self._lock:
            queue, self._strokes = self._strokes is None, (rect, strokes, catch_clicks, style, color, width)
        if queue:
            _on_main(self._apply_strokes)

    def _apply_strokes(self):
        with self._lock:
            args, self._strokes = self._strokes, None
        if args is None:
            return
        (x, y, w, h), strokes, catch_clicks, style, color, width = args
        if self.win is None:
            self._make()
        from AppKit import NSColor
        self.win.setFrame_display_(((x, screens()[0][3] - y - h), (w, h)), False)
        root = self.win.contentView().layer()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        root.setGeometryFlipped_(True)  # strokes use top-left coordinates, like everything else
        root.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 1 / 255 if catch_clicks else 0).CGColor())
        ink = NSColor.colorWithSRGBRed_green_blue_alpha_(color[0] / 255, color[1] / 255, color[2] / 255, 1).CGColor()
        scale = self.win.backingScaleFactor()
        live = set()
        for sid, version, pts, alpha in strokes:
            live.add(sid)
            layer, built = self._stroke_layers.get(sid, (None, None))
            if layer is None:
                layer = Quartz.CAShapeLayer.layer()
                layer.setContentsScale_(scale)
                layer.setFillColor_(None)
                layer.setStrokeColor_(ink)
                layer.setLineWidth_(width)
                layer.setLineCap_("round")
                layer.setLineJoin_("round")
                if style == "shadow":
                    layer.setShadowOpacity_(0.47)
                    layer.setShadowRadius_(5)
                    layer.setShadowOffset_((0, 2))
                elif style == "outline":
                    layer.setShadowOpacity_(1)
                    layer.setShadowRadius_(1)
                    layer.setShadowOffset_((0, 0))
                root.addSublayer_(layer)
            if built != version:
                path = Quartz.CGPathCreateMutable()
                Quartz.CGPathMoveToPoint(path, None, pts[0][0] - x, pts[0][1] - y)
                for px, py in pts[1:]:
                    Quartz.CGPathAddLineToPoint(path, None, px - x, py - y)
                layer.setPath_(path)
            layer.setOpacity_(alpha)
            self._stroke_layers[sid] = (layer, version)
        for sid in [s for s in self._stroke_layers if s not in live]:
            self._stroke_layers.pop(sid)[0].removeFromSuperlayer()
        Quartz.CATransaction.commit()
        if not self.visible:
            self.win.orderFrontRegardless()
            self.visible = True

    def hide(self):
        with self._lock:
            self._frame = "hide"
            queue, self._queued = not self._queued, True
        if queue:
            _on_main(self._apply)

    def close(self):
        """Hide for good (the Overlay isn't used again)."""
        self.hide()

    def click_through(self, on):
        self._click_through = on

        def apply():
            if self.win:
                self.win.setIgnoresMouseEvents_(on)
                if not on and self.cursor:
                    self.cursor.set()  # now, not on the next mouse move
        _on_main(apply)

    def raise_(self):
        _on_main(lambda: self.win and self.win.orderFrontRegardless())

    def _make(self):
        from AppKit import (NSBackingStoreBuffered, NSColor, NSPanel, NSWindowCollectionBehaviorCanJoinAllSpaces,
                            NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorIgnoresCycle,
                            NSWindowCollectionBehaviorStationary, NSWindowSharingNone,
                            NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel)
        w = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (1, 1)), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        w.setOpaque_(False)
        w.setBackgroundColor_(NSColor.clearColor())
        w.setHasShadow_(False)
        w.setLevel_(self.level)
        w.setHidesOnDeactivate_(False)
        w.setReleasedWhenClosed_(False)
        w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorIgnoresCycle)
        w.setIgnoresMouseEvents_(self._click_through)
        if self.hidden_from_capture:
            w.setSharingType_(NSWindowSharingNone)
        view = _mouse_view().alloc().initWithFrame_(((0, 0), (1, 1)))
        view.overlay = self
        w.setContentView_(view)
        if self.cursor or self.track:  # works even though the panel is never the key window
            if self.cursor:
                allow_background_cursor()
            from AppKit import (NSTrackingActiveAlways, NSTrackingArea, NSTrackingCursorUpdate,
                                NSTrackingInVisibleRect, NSTrackingMouseMoved)
            view.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                ((0, 0), (1, 1)), NSTrackingActiveAlways | NSTrackingCursorUpdate | NSTrackingMouseMoved
                | NSTrackingInVisibleRect, view, None))
        view.setWantsLayer_(True)
        self.win = w

    def _apply(self):
        with self._lock:
            frame, self._frame, self._queued = self._frame, None, False
        if frame is None:
            return
        if self.win is None:
            self._make()
        if frame == "hide":
            self.win.orderOut_(None)
            self.visible = False
            return
        cg, pw, ph, x, y, scale, alpha = frame
        w, h = pw / scale, ph / scale
        main_h = screens()[0][3]
        self.win.setFrame_display_(((x, main_h - y - h), (w, h)), False)
        layer = self.win.contentView().layer()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)  # no implicit cross-fade between frames
        layer.setContentsScale_(scale)
        layer.setContents_(cg)
        Quartz.CATransaction.commit()
        self.win.setAlphaValue_(alpha)
        if not self.visible:
            self.win.orderFrontRegardless()
            self.visible = True


_MouseView = None


def _mouse_view():
    """The overlay's content view: passes presses and drags to Overlay.on_mouse, in top-left
    points. Clicks reach Lookhere's own windows without the Accessibility permission a global
    mouse listener needs."""
    global _MouseView
    if _MouseView is None:
        from AppKit import NSView

        def send(view, kind, event):
            ov = getattr(view, "overlay", None)
            if ov is None or ov.on_mouse is None:
                return
            p = view.window().convertPointToScreen_(event.locationInWindow())
            ov.on_mouse(kind, p.x, screens()[0][3] - p.y)

        def show_cursor(view):
            ov = getattr(view, "overlay", None)
            if ov is not None and ov.cursor is not None and not ov._click_through:
                ov.cursor.set()

        class LookhereMouseView(NSView):
            def acceptsFirstMouse_(self, event):
                return True  # act on the first click; the panel never becomes active

            def mouseDown_(self, event):
                from AppKit import NSEvent
                NSEvent.setMouseCoalescingEnabled_(False)  # every drag point, as on Windows
                send(self, "down", event)

            def mouseDragged_(self, event):
                send(self, "drag", event)

            def mouseUp_(self, event):
                from AppKit import NSEvent
                NSEvent.setMouseCoalescingEnabled_(True)
                send(self, "up", event)

            def cursorUpdate_(self, event):
                show_cursor(self)

            def mouseMoved_(self, event):
                show_cursor(self)
                if getattr(self, "overlay", None) is not None and self.overlay.track:
                    send(self, "move", event)
        _MouseView = LookhereMouseView
    return _MouseView


_bg_cursor = False


def allow_background_cursor():
    """Let Lookhere set the cursor without being the frontmost app, which it never is. macOS has
    no public way to do this; this private window-server property is the usual one for overlays."""
    global _bg_cursor
    if _bg_cursor:
        return
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cg._CGSDefaultConnection.restype = ctypes.c_int
        cg.CGSSetConnectionProperty.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        cg.CGSSetConnectionProperty.restype = ctypes.c_int
        cid = cg._CGSDefaultConnection()
        key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground", 0x08000100)  # UTF-8
        _bg_cursor = cg.CGSSetConnectionProperty(cid, cid, key, ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")) == 0
    except (OSError, AttributeError, ValueError):
        pass  # private API: without it the cursor just stays an arrow


def nsimage(img):
    """An NSImage from a PIL image drawn at 2x (Retina)."""
    import io
    from AppKit import NSImage
    from Foundation import NSData
    buf = io.BytesIO()
    img.save(buf, "PNG")
    ns = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue())))
    ns.setSize_((img.width / 2, img.height / 2))
    return ns


def cursor_from(img, hotspot):
    """An NSCursor from a PIL image drawn at 2x; hotspot in points."""
    from AppKit import NSCursor
    return NSCursor.alloc().initWithImage_hotSpot_(nsimage(img), hotspot)


def set_menu_bar_image(button, img, tinted=False):
    """A menu bar icon as a template image, the kind macOS colours to suit the bar, like every
    other icon up there. `tinted` makes it red for recording: macOS ignores tint colours on menu
    bar templates, so that one is a plain red image."""
    from PIL import Image
    if tinted:
        red = Image.new("RGBA", img.size, (255, 59, 48, 255))
        red.putalpha(img.getchannel("A"))
        img = red
    ns = nsimage(img)
    ns.setTemplate_(not tinted)
    button.setImage_(ns)


def _on_main(fn):
    from PyObjCTools import AppHelper
    AppHelper.callAfter(fn)


on_main = _on_main


class _Monitor:
    def __init__(self, handle):
        self.handle = handle

    def stop(self):
        from AppKit import NSEvent
        handle, self.handle = self.handle, None
        if handle is not None:
            _on_main(lambda: NSEvent.removeMonitor_(handle))


def on_outside_click(callback):
    """Call callback() on a press in any other app (mouse monitors need no Accessibility)."""
    from AppKit import NSEvent, NSEventMaskLeftMouseDown, NSEventMaskRightMouseDown
    return _Monitor(NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, lambda event: callback()))


def on_key(callback):
    """Pass Lookhere's own key presses to callback(key code, {'ctrl', 'alt', 'shift', 'cmd'}), and
    keep them from Tk. Lookhere's window must be in front; needs no Accessibility."""
    from AppKit import NSEvent, NSEventMaskKeyDown

    def handler(event):
        flags = event.modifierFlags()
        callback(event.keyCode(), {n for n, bit in (("shift", 1 << 17), ("ctrl", 1 << 18), ("alt", 1 << 19),
                                                    ("cmd", 1 << 20)) if flags & bit})
        return None
    return _Monitor(NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, handler))


def activate():
    """Bring Lookhere forward (for its Settings and History windows)."""
    from AppKit import NSApplication
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def menu_bar_app():
    """No Dock icon: Lookhere lives in the menu bar."""
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def set_appearance(dark):
    """Light or dark window chrome (title bars) for Lookhere's own windows."""
    from AppKit import NSAppearance, NSApplication
    NSApplication.sharedApplication().setAppearance_(
        NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua"))


def open_path(path):
    import subprocess
    subprocess.Popen(["open", path])


def recycle(path):
    """Move to the Trash (recoverable), not a permanent delete."""
    from Foundation import NSFileManager
    ok, _, _ = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(os.path.abspath(path)), None, None)
    return bool(ok)


def overlay_level(above_drawing=False):
    """Window levels: the drawing layer sits above normal and full-screen windows, the pill above it."""
    from AppKit import NSPopUpMenuWindowLevel
    return NSPopUpMenuWindowLevel + (2 if above_drawing else 1)


# ---------- screen video ----------
def _wait(start, timeout=10):
    """Call an async Apple API (start(handler)) and wait for its completion handler's arguments."""
    done, box = threading.Event(), []

    def handler(*args):
        box.extend(args)
        done.set()
    start(handler)
    if not done.wait(timeout):
        raise TimeoutError("ScreenCaptureKit did not answer")
    return box


def _recording_delegate():
    import objc
    from Foundation import NSObject

    class RecordingDelegate(NSObject, protocols=[objc.protocolNamed("SCRecordingOutputDelegate")]):
        def recordingOutputDidStartRecording_(self, output):
            self.owner.start_mono = time.monotonic()

        def recordingOutput_didFailWithError_(self, output, error):
            self.owner.error = str(error)
            self.owner.finished.set()

        def recordingOutputDidFinishRecording_(self, output):
            self.owner.finished.set()
    return RecordingDelegate


class ScreenRecording:
    """One display recorded straight to an .mp4 by ScreenCaptureKit (SCRecordingOutput, macOS 15+),
    real cursor included. Unlike ffmpeg's avfoundation input it leaves out windows that are hidden
    from capture, so the status pill never appears in the video."""
    _Delegate = None

    def __init__(self, display, path, fps=30):
        import ScreenCaptureKit as SC
        from CoreMedia import CMTimeMake
        self.start_mono = None
        self.error = None
        self.finished = threading.Event()
        flt = SC.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        cfg = SC.SCStreamConfiguration.alloc().init()
        scale = flt.pointPixelScale()
        cfg.setWidth_(int(display.width() * scale))
        cfg.setHeight_(int(display.height() * scale))
        cfg.setMinimumFrameInterval_(CMTimeMake(1, fps))
        cfg.setShowsCursor_(True)
        cfg.setQueueDepth_(6)
        rc = SC.SCRecordingOutputConfiguration.alloc().init()
        rc.setOutputURL_(NSURL.fileURLWithPath_(path))
        rc.setOutputFileType_("public.mpeg-4")
        rc.setVideoCodecType_("avc1")
        if ScreenRecording._Delegate is None:
            ScreenRecording._Delegate = _recording_delegate()
        self.delegate = ScreenRecording._Delegate.alloc().init()
        self.delegate.owner = self
        self.output = SC.SCRecordingOutput.alloc().initWithConfiguration_delegate_(rc, self.delegate)
        self.stream = SC.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, None)
        ok, err = self.stream.addRecordingOutput_error_(self.output, None)
        if not ok:
            raise RuntimeError("couldn't add the recording output: %s" % err)
        err = _wait(self.stream.startCaptureWithCompletionHandler_)[0]
        if err is not None:
            raise RuntimeError("screen capture didn't start: %s" % err)

    def stop(self):
        _wait(self.stream.stopCaptureWithCompletionHandler_)
        self.finished.wait(10)
        return 0 if self.error is None else 1


def displays():
    """ScreenCaptureKit displays in the same order as mss's monitors (the active display list)."""
    import ScreenCaptureKit as SC
    content, err = _wait(SC.SCShareableContent.getShareableContentWithCompletionHandler_)
    if content is None:
        raise RuntimeError("no screen access: %s" % err)
    by_id = {d.displayID(): d for d in content.displays()}
    err, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
    return [by_id[i] for i in ids[:n] if i in by_id]


# ---------- global hotkeys ----------
# Carbon's RegisterEventHotKey, the Mac equivalent of RegisterHotKey: the key press goes to
# Lookhere only (never also to the app in front), it fails if another app owns the combination,
# and unlike a key listener it needs no Accessibility permission. Presses arrive through the main
# thread's event loop (Tk's), so register from the main thread.
KEYCODES = dict(zip("asdfhgzxcv", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
KEYCODES.update(dict(zip("bqweryt", (11, 12, 13, 14, 15, 16, 17))))
KEYCODES.update({"esc": 53, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
                 "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "space": 49})
KEYCODES.update({"f%d" % (i + 1): c for i, c in enumerate(
    (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90))})
MODS = {"cmd": 0x100, "win": 0x100, "shift": 0x200, "alt": 0x800, "ctrl": 0x1000}

_carbon = None
_hotkeys = {}  # id -> callback
_handler_ref = None


def _four(s):
    return int.from_bytes(s.encode(), "big")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


@_HANDLER
def _on_hotkey(_call, event, _data):
    hk = _EventHotKeyID()
    _carbon.GetEventParameter(event, _four("----"), _four("hkid"), None, ctypes.sizeof(hk), None, ctypes.byref(hk))
    cb = _hotkeys.get(hk.id)
    if cb:
        threading.Thread(target=cb, daemon=True).start()  # never block the event loop
    return 0


def _setup():
    global _carbon, _handler_ref
    if _carbon is None:
        c = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p]
        c.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.c_void_p]
        c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        c.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        spec = _EventTypeSpec(_four("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        _handler_ref = ctypes.c_void_p()
        c.InstallEventHandler(c.GetApplicationEventTarget(), _on_hotkey, 1, ctypes.byref(spec), None,
                              ctypes.byref(_handler_ref))
        _carbon = c
    return _carbon


class Hotkey:
    """One global hotkey from a spec like '<ctrl>+<alt>+h'. `ok` is False if it couldn't be registered."""
    _next_id = 1

    def __init__(self, spec, on_press):
        mods, key = parse_hotkey(spec)
        c = _setup()
        self.id = Hotkey._next_id
        Hotkey._next_id += 1
        self.ref = ctypes.c_void_p()
        err = c.RegisterEventHotKey(key, mods, _EventHotKeyID(_four("LkHr"), self.id), c.GetApplicationEventTarget(),
                                    0, ctypes.byref(self.ref))
        self.ok = err == 0
        if self.ok:
            _hotkeys[self.id] = on_press

    def stop(self):
        if self.ok and _hotkeys.pop(self.id, None):
            _carbon.UnregisterEventHotKey(self.ref)


def parse_hotkey(spec):
    mods, key = 0, None
    for part in spec.lower().split("+"):
        part = part.strip().strip("<>")
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYCODES:
            key = KEYCODES[part]
        else:
            raise ValueError("can't read hotkey part %r in %r" % (part, spec))
    if key is None:
        raise ValueError("hotkey %r has no main key" % spec)
    return mods, key


def key_name(vk):
    """A Mac virtual key code back to its name ('h', 'f5', 'space'), or None."""
    return next((n for n, c in KEYCODES.items() if c == vk), None)


# ---------- open at login ----------
AGENT = os.path.expanduser("~/Library/LaunchAgents/com.lookhere.app.plist")


def autostart_enabled():
    return os.path.exists(AGENT)


def set_autostart(on, args):
    """A per-user LaunchAgent that runs `args` at login (the Mac's "Start with Windows")."""
    import plistlib
    if not on:
        if os.path.exists(AGENT):
            os.remove(AGENT)
        return
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    with open(AGENT, "wb") as f:
        plistlib.dump({"Label": "com.lookhere.app", "ProgramArguments": list(args), "RunAtLoad": True,
                       "ProcessType": "Interactive"}, f)

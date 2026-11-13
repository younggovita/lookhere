"""Lookhere: press a hotkey, talk through a problem on screen, press it again,
and get a bundle (transcript + key frames + click log) ready to paste into any AI chat.

Runs as a tray app. Hotkey toggles recording. On stop, process.py builds the bundle,
the video, frame sheet and notes go on the clipboard and the window you started from comes back.

Records: the monitor under the cursor (2 checks a second, frames kept on clicks,
screen changes and every few seconds), the default mic, mouse clicks and the
foreground window title. Never records keystrokes.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime, timedelta

import paths

if paths.MAC:
    import mac
else:
    import winsound
    # Physical pixel coordinates everywhere (mss, pynput, cursor) must agree.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import mss
import numpy as np
import pystray
import sounddevice as sd
import soundfile as sf
from PIL import Image, ImageDraw
from pynput import keyboard, mouse

import logo

from hud import Hud

# Words that differ between the two systems, for the pill and the menu.
OS_NAME = "macOS" if paths.MAC else "Windows"
PASTE_KEYS = "Cmd+V" if paths.MAC else "Ctrl+V"
BUILD_TIMEOUT = 900  # seconds without a word from the build before it's treated as hung
# Shortcuts nobody should lose to a recorder: copy, paste, cut, undo, select all, save...
RESERVED_KEYS = set("cvxzasfpntwqy")
MENU_HINT = "Click the menu bar icon" if paths.MAC else "Right-click the tray icon"
MIC_DEFAULT = "System default" if paths.MAC else "Windows default"
THEME_AUTO = "System" if paths.MAC else "Follow Windows"
AUTOSTART_TEXT = (("Open at login", "Opens at login", "Won't open at login") if paths.MAC
                  else ("Start with Windows", "Starts with Windows", "Won't start with Windows"))

HERE = paths.DATA  # recordings, logs and config.json (next to the code when run from source)
CONFIG = {
    "pause_hotkey": "<ctrl>+<alt>+p",
    "draw_hotkey": "<ctrl>+<alt>+m",  # M for mark
    "hotkey": "<ctrl>+<alt>+h",  # H for here (Ctrl+Alt+R and Ctrl+Alt+L are commonly taken by other apps)
    "recordings_dir": os.path.join(HERE, "recordings"),
    "check_interval": 0.5,      # seconds between screen checks
    "periodic_every": 5.0,      # keep a frame at least this often
    "change_threshold": 0.4,    # % of thumbnail pixels that changed visibly (catches white menus on white pages)
    "after_click_delay": 0.8,   # second frame after a click, if the screen changed
    "track_interval": 0.1,      # cursor path sampling
    "point_move_px": 40,        # cursor must travel this far from the last frame...
    "point_still_s": 0.5,       # ...then stay still this long to count as pointing at something
    "raw_max_width": 2560,
    "keep_days": 30,            # recordings older than this are removed at startup
    "sounds": True,             # ding when the recording goes live
    "focus_window_title": "",  # fallback window title if the starting window has closed
    "update_repo": "younggovita/lookhere",  # GitHub repo whose releases the installed app updates from
    "check_updates": True,      # installed copies update from public GitHub releases (see README)
    "mic": "",                  # microphone name; "" = the Windows default
    "draw_style": "shadow",     # drawn marks: "shadow" (default), "outline" or "plain"
    "theme": "auto",            # auto = white or black pill following Windows light/dark; or "dark" / "light"
    "video_encoder": "auto",    # auto picks the first that works: NVIDIA, AMD, Intel, then Windows' own
}
DEFAULT_KEYS = {k: CONFIG[k] for k in ("hotkey", "pause_hotkey", "draw_hotkey")}
_cfg_path = os.path.join(HERE, "config.json")
if __name__ == "__main__":
    # Other modules reach this one with `import screen_explain`; give them this running copy,
    # not a second one with its own settings.
    sys.modules.setdefault("screen_explain", sys.modules["__main__"])

_cfg_error = None
if os.path.exists(_cfg_path):
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            _saved = json.load(f)
        if not isinstance(_saved, dict):
            raise ValueError("not a JSON object")
        CONFIG.update(_saved)
    except (OSError, ValueError) as e:  # half-written or hand-edited badly: run on the defaults
        _cfg_error = repr(e)

if not paths.MAC:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
LOG_PATH = os.path.join(HERE, "screen-explain.log")


LOG_MAX = 2_000_000  # bytes; the log starts again past this (one older copy is kept)


def log(msg):
    """Append to the log. Never raises: a full disk must not take the app down with it."""
    line = "%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


if _cfg_error:
    log("config.json unreadable, using the defaults: %s" % _cfg_error)


def cursor_pos():
    if paths.MAC:
        return mac.cursor_pos()
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def window_title(hwnd):
    # InternalGetWindowText never sends messages, so it cannot block on a busy window.
    buf = ctypes.create_unicode_buffer(512)
    user32.InternalGetWindowText(hwnd, buf, 512)
    return buf.value


def foreground_title():
    if paths.MAC:
        return mac.foreground_title()
    return window_title(user32.GetForegroundWindow())


def monitor_for(monitors, x, y):
    for i, m in enumerate(monitors[1:], start=1):
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return i
    return 1


class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD),
                ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


if not paths.MAC:
    _k32 = ctypes.WinDLL("kernel32")
    _k32.CreateJobObjectW.restype = wt.HANDLE
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    _JOB = _k32.CreateJobObjectW(None, None)
    _lim = _JobLimits()
    _lim.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _k32.SetInformationJobObject(_JOB, 9, ctypes.byref(_lim), ctypes.sizeof(_lim))


def tie_to_app(proc):
    """Child processes (ffmpeg, the worker) die with the app, even if it crashes."""
    if paths.MAC:  # no job objects on a Mac. TODO (port): stop ffmpeg/the worker if the app crashes
        return proc
    h = _k32.OpenProcess(0x1F0FFF, False, proc.pid)
    if h:
        _k32.AssignProcessToJobObject(_JOB, h)
        _k32.CloseHandle(h)
    return proc


ffmpeg_exe = paths.ffmpeg_exe
_ENCODER = None


def video_encoder():
    """First H.264 encoder that works on this PC (NVIDIA NVENC, AMD AMF, Intel QSV, then Windows'
    Media Foundation, which every Windows PC has). Probed once; config "video_encoder" overrides."""
    global _ENCODER
    if _ENCODER is None:
        choice = CONFIG["video_encoder"]
        candidates = [choice] if choice != "auto" else ["h264_nvenc", "h264_amf", "h264_qsv", "h264_mf"]
        exe = ffmpeg_exe()
        for enc in candidates:
            r = subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30", "-frames:v", "5",
                                "-pix_fmt", "nv12", "-c:v", enc, "-f", "null", "-"],
                               capture_output=True, creationflags=paths.NO_WINDOW)
            if r.returncode == 0:
                _ENCODER = enc
                break
        else:
            _ENCODER = "h264_mf"
        log("video encoder: %s" % _ENCODER)
    return _ENCODER


class ScreenVideo:
    """Full-motion video of every monitor, real cursor included, via ffmpeg desktop duplication
    on the GPU. One .mkv per monitor (mkv survives a hard stop). Used for the chat preview."""

    def __init__(self, rec_dir, n_outputs, fps=30):
        self.files = ["screen%d.mkv" % i for i in range(n_outputs)]
        self.start_mono = None
        exe = ffmpeg_exe()
        if not exe:
            raise RuntimeError("ffmpeg not found")
        enc = video_encoder()
        # NVENC takes the GPU frames directly; other encoders get them copied to system memory.
        tail = "" if enc == "h264_nvenc" else ",hwdownload,format=bgra,format=nv12"
        graph = ";".join("ddagrab=output_idx=%d:framerate=%d:draw_mouse=1%s[v%d]" % (i, fps, tail, i)
                         for i in range(n_outputs))
        quality = {"h264_nvenc": ["-preset", "p4", "-cq", "28"], "h264_amf": ["-quality", "speed", "-qp_i", "26", "-qp_p", "28"],
                   "h264_qsv": ["-preset", "veryfast", "-global_quality", "28"],
                   "h264_mf": ["-b:v", "6M"]}.get(enc, [])
        cmd = [exe, "-hide_banner", "-v", "error", "-stats_period", "0.1", "-progress", "pipe:1",
               "-filter_complex", graph]
        for i, name in enumerate(self.files):
            cmd += ["-map", "[v%d]" % i, "-c:v", enc, *quality, "-y", os.path.join(rec_dir, name)]
        self.proc = tie_to_app(subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=open(os.path.join(rec_dir, "ffmpeg.log"), "w"),
                                                text=True, creationflags=paths.NO_WINDOW))
        threading.Thread(target=self._read_progress, daemon=True).start()

    def _read_progress(self):
        # The first report with real output time tells us when frame 0 was on screen.
        for line in self.proc.stdout:
            if self.start_mono is None and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if us > 0:
                    self.start_mono = time.monotonic() - us / 1e6

    def ended(self):
        return self.proc.poll() is not None

    def stop(self):
        try:
            self.proc.stdin.write("q")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        return self.proc.returncode


class MacScreenVideo:
    """ScreenVideo on a Mac: ScreenCaptureKit, one .mp4 per display, real cursor. ffmpeg's
    avfoundation input would also record the status pill; ScreenCaptureKit leaves it out.
    Capture starts on a thread (~0.5s), so starting a recording never waits for it."""

    def __init__(self, rec_dir, n_outputs, fps=30):
        self.files = ["screen%d.mp4" % i for i in range(n_outputs)]
        self.recs = []
        self.error = None
        self.rec_dir = rec_dir

        def start():
            try:
                for i, d in enumerate(mac.displays()[:n_outputs]):
                    self.recs.append(mac.ScreenRecording(d, os.path.join(rec_dir, self.files[i]), fps))
            except Exception as e:
                self.error = repr(e)
                log("screen video failed: %s" % self.error)
                with open(os.path.join(rec_dir, "ffmpeg.log"), "w") as f:  # where the logs point
                    f.write(self.error + "\n")
        self.starter = threading.Thread(target=start, daemon=True, name="screen-video")
        self.starter.start()

    @property
    def start_mono(self):
        return self.recs[0].start_mono if self.recs and self.recs[0].start_mono else None

    def ended(self):
        return self.error is not None or any(r.finished.is_set() for r in self.recs)

    def stop(self):
        self.starter.join(timeout=10)
        return max([1 if self.error else 0] + [r.stop() for r in self.recs])


def thumb(img):
    return np.asarray(img.convert("L").resize((240, 135), Image.BILINEAR), dtype=np.float32)


def changed_pct(a, b):
    """Percent of thumbnail pixels that changed by a visible amount."""
    if a is None or b is None or a.shape != b.shape:
        return 100.0
    return float((np.abs(a - b) > 6).mean() * 100)


class SystemHotkey:
    """A real Windows hotkey (RegisterHotKey): Windows hands the key press to Lookhere only, so it
    never also reaches the app in front. Fails if another app already owns that combination."""
    MODS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "cmd": 0x8, "win": 0x8}

    def __init__(self, spec, on_press):
        self.mods, self.vk = self.parse(spec)
        self.on_press = on_press
        self.ok = None
        self.thread_id = None
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True, name="hotkey").start()
        ready.wait(3)

    @classmethod
    def parse(cls, spec):
        mods, vk = 0x4000, None  # MOD_NOREPEAT: holding the keys down doesn't toggle repeatedly
        for part in spec.lower().split("+"):
            part = part.strip().strip("<>")
            if part in cls.MODS:
                mods |= cls.MODS[part]
            elif len(part) == 1 and part.isalnum():
                vk = ord(part.upper())
            elif part.startswith("f") and part[1:].isdigit():
                vk = 0x6F + int(part[1:])  # F1 = 0x70
            elif part == "space":
                vk = 0x20
            else:
                raise ValueError("can't read hotkey part %r in %r" % (part, spec))
        if vk is None:
            raise ValueError("hotkey %r has no main key" % spec)
        return mods, vk

    def _run(self, ready):
        u = ctypes.WinDLL("user32")
        u.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
        self.thread_id = kernel32.GetCurrentThreadId()
        self.ok = bool(u.RegisterHotKey(None, 1, self.mods, self.vk))
        ready.set()
        if not self.ok:
            return
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.on_press()
        u.UnregisterHotKey(None, 1)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT


if paths.MAC:
    SystemHotkey = mac.Hotkey  # Carbon RegisterEventHotKey: same behaviour, same interface (ok, stop)


def hotkey_label(spec):
    """'<ctrl>+<alt>+h' -> 'Ctrl+Alt+H' (on a Mac 'Ctrl+Option+H', Cmd for the Command key)."""
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win", "win": "Win", "space": "Space"}
    if paths.MAC:
        names.update(alt="Option", cmd="Cmd", win="Cmd")
    return "+".join(names.get(p.strip("<>"), p.strip("<>").upper()) for p in spec.lower().split("+"))


def save_setting(key, value):
    """Write one setting into config.json (keeping the others) and into the live CONFIG."""
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    tmp = _cfg_path + ".tmp"  # written beside it, then swapped in: a crash can't leave half a file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _cfg_path)
    CONFIG[key] = value


class ShortcutCapture:
    """Listens for the next key combination (for "Change shortcut"). Reports
    ("captured", spec), ("capture_hint", text) or ("capture_cancelled", reason) through `report`."""
    MOD_KEYS = {"ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl", "alt_l": "alt", "alt_r": "alt", "alt": "alt",
                "alt_gr": "alt", "shift": "shift", "shift_l": "shift", "shift_r": "shift",
                "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd"}

    def __init__(self, report, timeout=15):
        self.report = report
        self.held = set()
        self.done = False
        if paths.MAC:  # Settings is in front: its own key presses need no Accessibility
            self.listener = mac.on_key(self._mac_key)
        else:
            self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)
            self.listener.start()
        self.timer = threading.Timer(timeout, lambda: self._finish(("capture_cancelled", "timeout")))
        self.timer.start()

    def _finish(self, result):
        if not self.done:
            self.done = True
            self.timer.cancel()
            self.listener.stop()
            self.report(result)

    def _mac_key(self, vk, mods):
        name = mac.key_name(vk)
        if name == "esc":
            self._finish(("capture_cancelled", "esc"))
        elif name:
            mods = [m for m in ("ctrl", "alt", "shift", "cmd") if m in mods]
            hint = self.refuse(name, mods)
            if hint:
                self.report(("capture_hint", hint))
            else:
                self._finish(("captured", "+".join(["<%s>" % m for m in mods] + [name])))

    @staticmethod
    def refuse(main, mods):
        """Why this combination can't be a shortcut (it would take a key you type with, or a
        standard shortcut, away from every app), or None if it's fine."""
        fkey = main.startswith("f") and main[1:].isdigit()
        strong = [m for m in mods if m != "shift"]
        if not fkey and not strong:
            return "Add %s to %s" % ("Ctrl, Option or Cmd" if paths.MAC else "Ctrl, Alt or Win", main.upper())
        if not fkey and len(mods) == 1 and mods[0] in ("ctrl", "cmd") and main in RESERVED_KEYS:
            return "%s is a standard shortcut · try another" % hotkey_label("<%s>+%s" % (mods[0], main))
        return None

    def _release(self, key):
        name = getattr(key, "name", None)
        self.held.discard(self.MOD_KEYS.get(name, name))

    def _press(self, key):
        name = getattr(key, "name", None)  # special keys (Key.ctrl_l...) have a name; letters don't
        if name in self.MOD_KEYS:
            self.held.add(self.MOD_KEYS[name])
            return
        if name == "esc":
            self._finish(("capture_cancelled", "esc"))
            return
        vk = getattr(key, "vk", None) or getattr(getattr(key, "value", None), "vk", None)
        if paths.MAC and not name:  # Mac key codes aren't letters (and Option changes the character)
            name = mac.key_name(vk)
            vk = None
        if name and name.startswith("f") and name[1:].isdigit():
            main = name
        elif name == "space" or (paths.MAC and name in mac.KEYCODES):
            main = name
        elif vk and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39):
            main = chr(vk).lower()
        else:
            return  # arrows, Enter etc. aren't offered as shortcuts
        mods = [m for m in ("ctrl", "alt", "shift", "cmd") if m in self.held]
        hint = self.refuse(main, mods)
        if hint:
            self.report(("capture_hint", hint))
            return
        self._finish(("captured", "+".join(["<%s>" % m for m in mods] + [main])))


class Recorder:
    def __init__(self):
        self.active = False

    def start(self):
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.dir = os.path.join(CONFIG["recordings_dir"], stamp)
        self.raw_dir = os.path.join(self.dir, "frames_raw")
        os.makedirs(self.raw_dir, exist_ok=True)
        self.frames = []
        self.clicks = []
        self.track = []   # [t, x, y] cursor path in screen coordinates
        self.requests = queue.Queue()
        self.stop_flag = threading.Event()

        self.level = 0.0
        self.paused = False
        self.pauses = []          # [start, end] stretches cut out when the recording is built
        self._pause_t = None
        self.drawing = False      # clicks made while drawing on screen aren't logged as clicks
        self.ignore = getattr(self, "ignore", None) or (lambda x, y: False)  # clicks on the pill
        self.started_at = datetime.now()
        self.t0 = time.monotonic()
        self.active = True
        self.video = self.stream = self.audio_file = None
        self.audio_thread = self.capture_thread = self.mouse_listener = None
        self.no_mic = self.audio_dead = False
        self.peak, self.heard = 0.0, 0  # loudest sample and samples received (Mac: pure silence = mic blocked)
        try:
            self._begin()
        except BaseException:
            # Never leave a screen recorder or an open mic behind a start that failed.
            try:
                self.cancel()
            except Exception as e:
                log("cleanup after a failed start: %r" % e)
            raise

    def _begin(self):
        # Screen video takes ~1-2s to warm up ("arming"). Voice, clicks and frames record from now,
        # so nothing said in that time is lost; the preview holds the first video frame until then.
        self.video = None
        try:
            with mss.MSS() as sct:
                n_mon = len(sct.monitors) - 1
            self.video = (MacScreenVideo if paths.MAC else ScreenVideo)(self.dir, n_mon)
        except Exception as e:
            log("screen video unavailable, preview will be a slideshow: %r" % e)

        # Audio: default input device, all its channels (process.py picks the loudest).
        # No usable microphone (none plugged in, access switched off, in use) = a silent recording,
        # not a failed one.
        self.audio_q = queue.Queue()
        self.samplerate, self.mic_name = 48000, "none"
        try:
            dev_index, dev = pick_input(CONFIG["mic"])
            self.samplerate = int(dev["default_samplerate"])
            channels = max(1, min(2, int(dev["max_input_channels"])))
            self.stream = input_stream(dev_index, self.samplerate, channels, self._on_audio)
            self.audio_file = sf.SoundFile(os.path.join(self.dir, "audio.wav"), mode="w",
                                           samplerate=self.samplerate, channels=channels, subtype="PCM_16")
            self.mic_name = dev["name"]
            # Opening the mic takes a moment (a Bluetooth headset, seconds): keep audio.wav aligned with t0.
            self.audio_q.put(np.zeros((int((time.monotonic() - self.t0) * self.samplerate), channels), np.float32))
            self.audio_thread = threading.Thread(target=self._audio_writer, daemon=True)
            self.audio_thread.start()
            self.stream.start()
        except Exception as e:
            log("no microphone, recording without voice: %r" % e)
            self.no_mic = True
            self._close_audio()

        self.mouse_listener = mouse.Listener(on_click=self._on_click)
        self.mouse_listener.start()
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()
        log("recording started %s (mic: %s)" % (self.dir, self.mic_name))

    def elapsed(self):
        return time.monotonic() - self.t0

    def active_elapsed(self):
        """Recorded time so far, not counting pauses (what the pill's timer shows)."""
        paused = sum(b - a for a, b in self.pauses)
        if self._pause_t is not None:
            paused += self.elapsed() - self._pause_t
        return self.elapsed() - paused

    def toggle_pause(self):
        if self.paused:
            self.pauses.append([round(self._pause_t, 2), round(self.elapsed(), 2)])
            self._pause_t = None
            self.paused = False
        else:
            self._pause_t = self.elapsed()
            self.paused = True
        log("recording %s at %.1fs" % ("paused" if self.paused else "resumed", self.elapsed()))
        return self.paused

    def mark(self, kind):
        """Ask the capture loop for a frame now (e.g. right after drawing on screen)."""
        if self.active and not self.paused:
            self.requests.put((kind, self.elapsed(), None))

    @property
    def screen_blocked(self):
        """Mac: the Screen Recording permission is off (the video can only show the wallpaper)."""
        return bool(paths.MAC and self.video is not None and self.video.error
                    and "no screen access" in self.video.error)

    @property
    def mic_blocked(self):
        """Mac: the mic opened but sends exact zeros, which is what macOS gives an app whose
        Microphone permission is off (a real mic always picks up some noise)."""
        return bool(paths.MAC and not self.no_mic and self.heard > 2 * self.samplerate and self.peak == 0.0)

    @property
    def armed(self):
        """True once the screen video is live (or will never be)."""
        return self.video is None or self.video.start_mono is not None or self.video.ended()

    def _on_audio(self, data, *_):
        if self.audio_dead:
            return
        if self.paused:  # keep the timeline intact, record nothing
            self.audio_q.put(np.zeros_like(data))
            self.level = 0.0
            return
        self.audio_q.put(data.copy())
        self.level = level_of(data)
        self.heard += len(data)
        if self.peak == 0.0:
            self.peak = float(np.abs(data).max())

    def _audio_writer(self):
        audio_file = self.audio_file
        while not ((self.stop_flag.is_set() or self.no_mic) and self.audio_q.empty()):
            try:
                audio_file.write(self.audio_q.get(timeout=0.2))
            except queue.Empty:
                pass
            except Exception as e:  # disk full: stop writing sound, keep the rest of the recording
                log("audio write failed, the rest has no voice: %r" % e)
                self.audio_dead = True
                return

    def _on_click(self, x, y, button, pressed):
        if not pressed or not self.active or self.paused or self.drawing or self.ignore(x, y):
            return
        t = self.elapsed()
        click = {"t": round(t, 2), "x": x, "y": y, "button": button.name, "window": foreground_title()}
        self.clicks.append(click)
        self.requests.put(("click", t, click))
        self.requests.put(("after", t + CONFIG["after_click_delay"], click))

    def _capture_loop(self):
        sct = mss.MSS()
        last_thumb = None
        last_saved_t = -999.0
        last_saved_pos = (-9999, -9999)
        pending_after = []
        next_check = 0.0
        next_track = 0.0
        grab_failed = False
        while not self.stop_flag.is_set():
            try:
                kind, due, click = self.requests.get(timeout=0.03)
                if kind == "after":
                    pending_after.append((due, click))
                    continue
            except queue.Empty:
                kind, click = None, None
            if self.paused:
                pending_after = []
                continue
            now = self.elapsed()
            if now >= next_track:
                self.track.append([round(now, 2), *cursor_pos()])
                next_track = now + CONFIG["track_interval"]
            ready = [p for p in pending_after if p[0] <= now]
            pending_after = [p for p in pending_after if p[0] > now]

            # Pointing: cursor travelled to somewhere new and has been still for a moment.
            pointing = False
            n_still = max(2, int(CONFIG["point_still_s"] / CONFIG["track_interval"]))
            if kind is None and len(self.track) >= n_still and now - last_saved_t > 1.0:
                recent = np.array([p[1:] for p in self.track[-n_still:]], dtype=np.float32)
                still = float(np.ptp(recent, axis=0).max()) <= 6
                moved = np.hypot(*(recent[-1] - np.array(last_saved_pos))) >= CONFIG["point_move_px"]
                pointing = still and moved
            if kind is None and not ready and not pointing and now < next_check:
                continue

            cx, cy = cursor_pos()
            if click:
                cx, cy = click["x"], click["y"]
            try:
                mon_idx = monitor_for(sct.monitors, cx, cy)
                mon = sct.monitors[mon_idx]
                shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            except Exception as e:
                # Windows refuses while a UAC prompt or the lock screen is up; carry on afterwards.
                if not grab_failed:
                    log("screen grab failed, will keep trying: %r" % e)
                grab_failed = True
                next_check = now + 0.5
                continue
            grab_failed = False
            th = thumb(img)
            diff = changed_pct(th, last_thumb)

            if kind == "click":
                reason = "click"
            elif kind == "draw":
                reason = "draw"
            elif ready:
                reason = "after_click" if diff > 0.05 else None
                click = ready[0][1]
            elif diff > CONFIG["change_threshold"]:
                reason = "change"
            elif pointing:
                reason = "point"
            elif now - last_saved_t >= CONFIG["periodic_every"]:
                reason = "periodic"
            else:
                reason = None
            if kind is None and not ready and not pointing:
                next_check = now + CONFIG["check_interval"]
            if reason is None:
                continue

            if img.width > CONFIG["raw_max_width"]:
                scale = CONFIG["raw_max_width"] / img.width
                img = img.resize((CONFIG["raw_max_width"], int(img.height * scale)), Image.LANCZOS)
            else:
                scale = 1.0
            name = "f%05d_%s.jpg" % (len(self.frames), reason)
            try:
                img.save(os.path.join(self.raw_dir, name), quality=85)
            except OSError as e:
                log("couldn't save a frame: %r" % e)
                next_check = now + 1.0
                continue
            self.frames.append({
                "file": name, "t": round(now, 2), "kind": reason, "monitor": mon_idx,
                "cursor": [round((cx - mon["left"]) * scale), round((cy - mon["top"]) * scale)],
                "scale": scale, "window": foreground_title(),
                "click": click if reason in ("click", "after_click") else None,
                "diff": round(diff, 2),
            })
            last_thumb, last_saved_t, last_saved_pos = th, now, (cx, cy)

    def _close_audio(self):
        """Stop the mic and finish audio.wav. Safe when some or all of it never started."""
        stream, self.stream = self.stream, None
        if stream is not None:
            for step in (stream.stop, stream.close):
                try:
                    step()
                except Exception as e:
                    log("closing the mic: %r" % e)
        if self.audio_thread is not None:  # ends once stop_flag (or no_mic) is set and the queue is empty
            self.audio_thread.join(timeout=5)
            self.audio_thread = None
        audio_file, self.audio_file = self.audio_file, None
        if audio_file is not None:
            try:
                audio_file.close()
            except Exception as e:
                log("closing audio.wav: %r" % e)
        if self.no_mic:  # a header with no sound in it would only confuse the build
            try:
                os.remove(os.path.join(self.dir, "audio.wav"))
            except OSError:
                pass

    def _halt(self):
        self.active = False
        if self.mouse_listener is not None:
            self.mouse_listener.stop()
            self.mouse_listener = None
        self.stop_flag.set()
        if self.capture_thread is not None:
            self.capture_thread.join(timeout=5)
            self.capture_thread = None
        self._close_audio()

    def cancel(self):
        """Stop everything and delete the recording."""
        try:
            self._halt()
        finally:
            video, self.video = self.video, None
            if video:
                video.stop()
            shutil.rmtree(self.dir, ignore_errors=True)
        log("recording cancelled after %.1fs" % self.elapsed())

    def stop(self):
        if self.paused:
            self.toggle_pause()
        duration = self.elapsed()
        self._halt()
        video = None
        if self.video:
            rc = self.video.stop()
            if self.video.start_mono is not None:
                video = {"files": self.video.files, "offset": round(self.video.start_mono - self.t0, 3),
                         "ffmpeg_exit": rc}
            else:
                log("screen video produced no frames (exit %s), see ffmpeg.log" % rc)
        with mss.MSS() as sct:
            monitors = sct.monitors
        meta = {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "duration": round(duration, 2),
            "mic": self.mic_name,
            "samplerate": self.samplerate,
            "monitors": monitors,
            "frames": self.frames,
            "clicks": self.clicks,
            "track": self.track,
            "video": video,   # offset = seconds after t0 that video frame 0 was captured
            "pauses": self.pauses,
        }
        with open(os.path.join(self.dir, "events.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        log("recording stopped: %.1fs, %d raw frames, %d clicks" % (duration, len(self.frames), len(self.clicks)))
        return self.dir


def set_clipboard(text, files=()):
    """Put text and, optionally, files on the clipboard. With files, Ctrl+V in Claude attaches
    them (like copying a file in Explorer); the text is there for apps that only take text."""
    if paths.MAC:
        return mac.set_clipboard(text, files)
    CF_UNICODETEXT, CF_HDROP, GMEM_MOVEABLE = 13, 15, 0x0002
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.restype = wt.LPVOID
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.RegisterClipboardFormatW.restype = wt.UINT

    def put(fmt, data):
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(fmt, h)

    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        return False
    try:
        user32.EmptyClipboard()
        files = [f for f in files if os.path.exists(f)]
        if files:
            # DROPFILES header: offset to file list (20), drop point, fNC, fWide=1; then a
            # double-null-terminated UTF-16 list of paths.
            header = (20).to_bytes(4, "little") + bytes(12) + (1).to_bytes(4, "little")
            names = "".join(os.path.abspath(f) + "\0" for f in files) + "\0"
            put(CF_HDROP, header + names.encode("utf-16-le"))
            put(user32.RegisterClipboardFormatW("Preferred DropEffect"), (1).to_bytes(4, "little"))  # copy
        put(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")
        return True
    finally:
        user32.CloseClipboard()


def focus_window(title):
    """Bring the top-level window with this exact title to the front. Returns True if found."""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            if window_title(hwnd) == title:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return bool(found) and bring_to_front(found[0])


def bring_to_front(hwnd):
    if paths.MAC:  # on a Mac this is the pid of the app that was in front
        return mac.bring_to_front(hwnd)
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(me, fg_thread, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(me, fg_thread, False)
    return True


def cleanup_old():
    root = CONFIG["recordings_dir"]
    if not os.path.isdir(root):
        return
    cutoff = datetime.now() - timedelta(days=CONFIG["keep_days"])
    for name in os.listdir(root):
        try:
            when = datetime.strptime(name, "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        if when < cutoff:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            log("removed old recording %s" % name)


def windows_theme(key="AppsUseLightTheme"):
    """'light' or 'dark' from Windows' personalisation setting (apps, or the taskbar with SystemUsesLightTheme)."""
    if paths.MAC:  # one Appearance setting covers apps and the menu bar
        return mac.theme()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, key)[0] else "dark"
    except OSError:
        return "dark"


# WASAPI names are the full, readable ones on Windows; a Mac only has Core Audio.
HOST_API = "Core Audio" if paths.MAC else "WASAPI"


def input_devices():
    """Microphones the system has active."""
    try:
        was = next(i for i, h in enumerate(sd.query_hostapis()) if HOST_API in h["name"])
        return [d["name"] for d in sd.query_devices() if d["hostapi"] == was and d["max_input_channels"] > 0]
    except Exception:
        return []


def pick_input(name):
    """(device index or None for the default, device info) for a saved mic name."""
    if name:
        was = next((i for i, h in enumerate(sd.query_hostapis()) if HOST_API in h["name"]), None)
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == name and d["hostapi"] == was and d["max_input_channels"] > 0:
                return i, d
        log("mic %r not found, using the Windows default" % name)
    return None, sd.query_devices(kind="input")


class ChildInputStream:
    """sd.InputStream with the stream in a child process (mic.py): on a Mac, stopping a PortAudio
    stream can deadlock inside Core Audio, which froze Lookhere mid-stop. Same interface as the
    parts of sd.InputStream used here; `callback(data)` gets float32 (frames, channels) blocks."""
    BLOCK = 1024  # frames per callback

    def __init__(self, device, samplerate, channels, callback):
        self.channels, self.callback = channels, callback
        self.proc = subprocess.Popen(paths.mic_cmd(["-" if device is None else str(device), str(samplerate),
                                                    str(channels)]),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(os.path.join(HERE, "mic.log"), "w"))
        head = self.proc.stdout.readline().decode(errors="replace").strip()
        if head != "OK":
            self.proc.kill()
            raise RuntimeError(head[4:] if head.startswith("ERR ") else "the microphone process failed")
        self.reader = threading.Thread(target=self._read, daemon=True, name="mic")

    def _read(self):
        size = self.BLOCK * self.channels * 4
        while True:
            buf = self.proc.stdout.read(size)
            if len(buf) < 4 * self.channels:
                return
            buf = buf[:len(buf) - len(buf) % (4 * self.channels)]
            self.callback(np.frombuffer(buf, np.float32).reshape(-1, self.channels))

    def start(self):
        self.reader.start()

    def stop(self):
        try:
            self.proc.stdin.close()  # mic.py exits without stopping its stream
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()
        self.reader.join(timeout=2)  # everything it sent is delivered

    def close(self):
        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def input_stream(device, samplerate, channels, callback):
    """The mic stream: in-process on Windows, in mic.py on a Mac (see ChildInputStream)."""
    if paths.MAC:
        return ChildInputStream(device, samplerate, channels, callback)
    return sd.InputStream(device=device, samplerate=samplerate, channels=channels, callback=callback)


def level_of(data):
    rms = float(np.sqrt((data ** 2).mean(axis=0)).max())
    return min(1.0, max(0.0, (20 * np.log10(max(rms, 1e-6)) + 52) / 40))  # -52 dBFS silent .. -12 loud


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    if paths.MAC:
        return mac.autostart_enabled()
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "Lookhere")
            return True
    except OSError:
        return False


def set_autostart(on):
    """Start with Windows: the per-user Run key (the installer's checkbox writes the same value).
    On a Mac: open at login, a per-user LaunchAgent."""
    if paths.MAC:
        mac.set_autostart(on, shlex.split(paths.app_cmd()))
        log("open at login: %s" % on)
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Lookhere", 0, winreg.REG_SZ, paths.app_cmd())
        else:
            try:
                winreg.DeleteValue(k, "Lookhere")
            except OSError:
                pass
    log("start with Windows: %s" % on)


def tray_image(state):
    """The Lookhere logo, dot in the state colour, drawn at the exact tray icon size.
    On a Mac: the plain dot-and-bars shape, hollow while building (TrayIcon adds the red)."""
    if paths.MAC:
        return logo.menu_glyph(hollow=state == "busy")
    color = {"idle": logo.IDLE, "rec": logo.REC, "busy": logo.BUSY}[state]
    return logo.draw_logo(color, max(16, user32.GetSystemMetrics(49)))  # SM_CXSMICON


IDLE, REC, BUSY = "idle", "rec", "busy"


class TrayIcon(pystray.Icon):
    """pystray's tray icon, but a right-click opens Lookhere's own styled menu (traymenu.py)
    instead of the plain Windows one. Left-click still runs the default item (record/stop)."""

    def __init__(self, *args, on_right=None, **kwargs):
        self.on_right = on_right
        super().__init__(*args, **kwargs)

    if paths.MAC:
        recording = False  # tint the menu bar icon red

        def _assert_image(self):  # pystray's scales to 1x and has no template images
            mac.set_menu_bar_image(self._status_item.button(), self._icon, tinted=self.recording)

        def _update_menu(self):
            pass  # no native menu: a click comes to __call__ and opens the styled one

        def __call__(self):
            f = self._status_item.button().window().frame()
            self.on_right(f.origin.x, mac.screens()[0][3] - f.origin.y)

    def _on_notify(self, wparam, lparam):
        if lparam == 0x0205 and self.on_right:  # WM_RBUTTONUP
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            self.on_right(pt.x, pt.y)
            return
        super()._on_notify(wparam, lparam)


class Worker:
    """process.py started the moment recording starts, so the speech model loads while you talk.
    Told 'go' on stop (or 'cancel'). Reports progress lines back through `on_line`."""

    def __init__(self, rec_dir, on_line):
        self.rec_dir = rec_dir
        self.on_line = on_line
        self.proc = self._spawn(["--wait"])

    def _spawn(self, extra):
        p = subprocess.Popen(paths.worker_cmd(self.rec_dir, extra),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=open(os.path.join(HERE, "process-last.log"), "w"),
                             text=True, encoding="utf-8", errors="replace", creationflags=paths.NO_WINDOW)
        tie_to_app(p)
        threading.Thread(target=self._read, args=(p,), daemon=True).start()
        return p

    def _read(self, p):
        for line in p.stdout:
            self.on_line(line.rstrip("\n"))
        self.on_line("EXIT %s" % p.wait())

    def go(self):
        try:
            self.proc.stdin.write("go\n")
            self.proc.stdin.flush()
        except Exception:
            # Preloaded worker died; run a fresh one without the head start.
            self.proc = self._spawn([])

    def cancel(self):
        try:
            self.proc.stdin.write("cancel\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        threading.Timer(3, lambda: self.proc.poll() is None and self.proc.kill()).start()


class App:
    def __init__(self):
        self.recorder = Recorder()
        self.state = "idle"     # idle | recording | processing
        self.events = queue.Queue()
        self.worker = None

        self.root = tk.Tk()
        self.root.withdraw()
        if not paths.MAC:
            # The windows are laid out in pixels; keep text in step at 125-200% display scaling
            # instead of letting it outgrow its rows.
            self.root.tk.call("tk", "scaling", 96 / 72)
        if paths.MAC:
            mac.menu_bar_app()
        # Errors in Tk callbacks would otherwise vanish (pythonw has no console).
        self.root.report_callback_exception = lambda *a: log("UI error: " + "".join(traceback.format_exception(*a)))
        self.hud = Hud(on_error=log)
        try:  # where the pill was last dragged to (Alt+drag)
            ox, oy = CONFIG.get("pill_offset") or (0, 0)
            self.hud.offset = (float(ox), float(oy))
        except (TypeError, ValueError):
            pass
        self.hud.on_moved = self._pill_moved
        self.themes = (None, None)
        self.tray_state = IDLE
        self._apply_themes()
        if not self.hud.capture_excluded:
            log("could not hide the overlay from capture; it may show in recordings")

        hk = CONFIG["hotkey"]
        self.keys = {}
        for name, ev in self.KEY_EVENTS.items():
            on_press = (lambda ev: lambda: self.events.put(ev))(ev)
            try:
                self.keys[name] = SystemHotkey(CONFIG[name], on_press)
            except Exception as e:  # config.json edited by hand into something unreadable
                log("%s %r can't be used (%r), using %s" % (name, CONFIG[name], e, DEFAULT_KEYS[name]))
                CONFIG[name] = DEFAULT_KEYS[name]
                self.keys[name] = SystemHotkey(CONFIG[name], on_press)
        self.hotkeys = self.keys["hotkey"]
        self.esc_listener = None
        self.capture = None
        self._captured_at = 0.0
        self._build_heard = 0.0
        self.capture_target = "hotkey"
        self.update_info = None
        self.drawer = None
        self.menu = None
        self.icon = TrayIcon("lookhere", tray_image(IDLE), "Lookhere (%s to record)" % self.hotkey_label,
                             on_right=lambda x, y: self.events.put(("tray_menu", x, y)),
                             menu=pystray.Menu(
            pystray.MenuItem(lambda _: "Start / stop recording (%s)" % self.hotkey_label,
                             lambda *_: self.events.put("toggle"), default=True),
            pystray.MenuItem(lambda _: "Pause / resume (%s)" % self.label("pause_hotkey"),
                             lambda *_: self.events.put("pause")),
            pystray.MenuItem(lambda _: "Draw on screen (%s)" % self.label("draw_hotkey"),
                             lambda *_: self.events.put("draw")),
            pystray.MenuItem("Cancel recording (Esc)", lambda *_: self.events.put("cancel")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("History...", lambda *_: self.events.put("history")),
            pystray.MenuItem("Copy last recording again", lambda *_: self.events.put("recopy")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Microphone", pystray.Menu(lambda: [
                pystray.MenuItem(label, (lambda v: lambda *_: self.events.put(("mic", v)))(value),
                                 checked=(lambda v: lambda _: CONFIG["mic"] == v)(value), radio=True)
                for label, value in [(MIC_DEFAULT, "")] + [(n, n) for n in input_devices()]
            ] + [pystray.Menu.SEPARATOR,
                 pystray.MenuItem("Test microphone", lambda *_: self.events.put(("mic", None)))])),
            pystray.MenuItem(AUTOSTART_TEXT[0], lambda *_: self.events.put("toggle_autostart"),
                             checked=lambda _: autostart_enabled()),
            pystray.MenuItem("Theme", pystray.Menu(*[
                pystray.MenuItem(label, (lambda v: lambda *_: self.events.put(("theme", v)))(value),
                                 checked=(lambda v: lambda _: CONFIG["theme"] == v)(value), radio=True)
                for label, value in ((THEME_AUTO if paths.MAC else "Follow Windows light/dark mode", "auto"), ("Always dark", "dark"),
                                     ("Always light", "light"))])),
            pystray.MenuItem("Open recordings folder", lambda *_: self.events.put("open_folder")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _: "Update to %s" % self.update_info["version"] if self.update_info else "",
                             lambda *_: self.events.put("install_update"), visible=lambda _: bool(self.update_info)),
            pystray.MenuItem(lambda _: "Check for updates (version %s)" % paths.VERSION,
                             lambda *_: self.events.put(("check_update", True)),
                             visible=lambda _: CONFIG["check_updates"]),
            pystray.MenuItem("Quit", lambda *_: self.events.put("quit")),
        ))
        if paths.MAC:  # AppKit ignores the icon being shown from pystray's setup thread
            self.icon.run_detached(setup=lambda icon: None)
            self.root.after(0, lambda: setattr(self.icon, "visible", True))
        else:
            self.icon.run_detached()
        self.root.after(100, self._pump)
        threading.Thread(target=self._warm_up, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()
        log("app started, hotkeys %s" % {k: (CONFIG[k], v.ok) for k, v in self.keys.items()})
        taken = [self.label(k) for k, v in self.keys.items() if not v.ok]
        if taken:
            self.hud.show("error", "%s taken by another app" % " and ".join(taken),
                          "%s > Settings" % MENU_HINT, hold=8)

    KEY_EVENTS = {"hotkey": "toggle", "pause_hotkey": "pause", "draw_hotkey": "draw"}
    KEY_TITLES = {"hotkey": "record", "pause_hotkey": "pause", "draw_hotkey": "draw"}

    def label(self, key):
        return hotkey_label(CONFIG[key])

    @property
    def hotkey_label(self):
        return self.label("hotkey")

    # ---------- loop ----------
    def _pump(self):
        # Always reschedules itself: an error here must never freeze hotkeys, Esc and the overlay.
        try:
            if self._pump_once() == "quit":
                return
        except Exception:
            log("pump error: " + traceback.format_exc())
        self.root.after(100, self._pump)

    def _pump_once(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev == "quit":
                    if self.state == "recording":
                        self._stop_esc()
                        if self.worker:
                            self.worker.cancel()
                        self.recorder.cancel()  # synchronous: ffmpeg must not outlive the app
                    if self.drawer:
                        self.drawer.close()
                    if self.menu:
                        self.menu.close()
                    for k in self.keys.values():
                        k.stop()
                    self.icon.stop()
                    self.root.destroy()
                    return "quit"
                try:
                    self._handle(ev)
                except Exception as e:
                    log("error handling %r: %r\n%s" % (ev, e, traceback.format_exc()))
                    if self.state == "recording" and ev not in ("toggle", "pause", "draw", "cancel") \
                            and self.recorder.active:
                        continue  # a side action failed (History, Settings...): keep recording
                    self._abandon_recording()
                    self._reset()
                    self.hud.show("error", "Something went wrong", str(e)[:60], hold=4)
        except queue.Empty:
            pass
        if CONFIG["theme"] == "auto" and time.monotonic() - getattr(self, "_theme_checked", 0) > 2:
            self._theme_checked = time.monotonic()
            self._apply_themes()  # picks up a Windows light/dark switch within a couple of seconds
        limit = max(BUILD_TIMEOUT, 6 * getattr(self, "duration", 0))  # long recordings take longer to build
        if self.state == "processing" and time.monotonic() - self._build_heard > limit:
            log("build made no progress for %ds, giving up" % limit)
            worker, rec_dir = self.worker, self.recorder.dir
            if worker:
                try:
                    worker.proc.kill()
                except Exception:
                    pass
            self.finish(rec_dir, False)
        if self.state == "recording":
            r = self.recorder
            s = int(r.active_elapsed())
            self.hud.level = r.level
            if r.armed and not self.dinged:
                self.dinged = True
                self.ding()
            timer = "%d:%02d" % (s // 60, s % 60)
            if r.paused:
                self.hud.show("paused", "Paused", right=timer)
            elif r.drawing:
                self.hud.show("draw", "Drawing", right=timer)
            elif r.armed:
                title = ("Screen blocked" if r.screen_blocked else "Mic blocked" if r.mic_blocked
                         else "Recording, no mic" if r.no_mic else "Recording")
                self.hud.show("recording", title, right=timer)
            else:
                self.hud.show("arming", "Starting")

    def _handle(self, ev):
        if ev in ("toggle", "pause", "draw") and (self.capture or time.monotonic() - self._captured_at < 0.5):
            return  # that press was for the shortcut recorder, not a command
        if ev == "toggle":
            if self.state == "idle":
                self.start()
            elif self.state == "recording":
                self.stop()
        elif ev == "cancel":
            if self.state == "recording":
                self.cancel()
        elif ev == "pause":
            if self.state == "recording":
                if self.recorder.drawing:
                    self.toggle_draw()
                self.recorder.toggle_pause()
        elif ev == "draw":
            if self.state == "recording" and not self.recorder.paused:
                self.toggle_draw()
        elif isinstance(ev, tuple) and ev[0] == "check_update":
            threading.Thread(target=self._check_update, args=(ev[1],), daemon=True).start()
        elif isinstance(ev, tuple) and ev[0] == "update_found":
            self.update_info = ev[1]
            self.icon.update_menu()
            if self.state == "idle":
                self.hud.show("done", "Lookhere %s is available" % ev[1]["version"],
                              "%s, then Update" % MENU_HINT, hold=6)
        elif isinstance(ev, tuple) and ev[0] == "up_to_date":
            self.hud.show("done", "Lookhere is up to date", "Version %s" % paths.VERSION, hold=3)
        elif isinstance(ev, tuple) and ev[0] == "update_failed":
            self.hud.show("error", "Couldn't update", ev[1][:60], hold=6)
        elif ev == "install_update":
            import history
            if history._rebuilding:  # the installer closes every Lookhere process, a trim rebuild included
                self.hud.show("error", "A trim is still being saved", "Update again in a moment", hold=4)
            elif self.state == "idle" and self.update_info:
                self.state = "updating"
                threading.Thread(target=self._install_update, daemon=True).start()
        elif isinstance(ev, tuple) and ev[0] == "update_progress":
            self.hud.show("processing", "Downloading the update", "Lookhere %s" % self.update_info["version"],
                          progress=ev[1])
        elif ev == "history":
            self.open_history()
        elif ev == "settings":
            import settings
            settings.open_window(self)
        elif isinstance(ev, tuple) and ev[0] == "trimmed":
            _, rec_dir, ok = ev
            if ok:
                self.copy_recording(rec_dir, "Trimmed and copied")
            else:
                self.hud.show("error", "Couldn't trim that recording", "Nothing was changed", hold=5)
            import history
            if history._win is not None:
                history._win.refresh()
        elif ev == "cancelled":
            self._reset()
        elif callable(ev):  # work handed to the Tk thread (e.g. closing the tray menu)
            ev()
        elif isinstance(ev, tuple) and ev[0] == "tray_menu":
            self.open_menu(ev[1], ev[2])
        elif isinstance(ev, tuple) and ev[0] == "theme":
            save_setting("theme", ev[1])
            self._apply_themes()
            self.icon.update_menu()
            self.hud.show("done", {"auto": "Following the system theme" if paths.MAC else "Following Windows light/dark mode", "dark": "Always dark",
                                   "light": "Always light"}[ev[1]],
                          "Press %s to record" % self.hotkey_label, hold=2.5)
            self.settings_changed()
        elif isinstance(ev, tuple) and ev[0] == "mic":
            if self.state == "idle":
                if ev[1] is not None:
                    save_setting("mic", ev[1])
                    self.icon.update_menu()
                    self.settings_changed()
                self.test_mic()
        elif isinstance(ev, tuple) and ev[0] == "mic_result":
            self._mic_result(*ev[1:])
        elif isinstance(ev, tuple) and ev[0] == "draw_style":
            save_setting("draw_style", ev[1])
            import draw
            draw.STYLE = ev[1]
            self.icon.update_menu()
        elif ev == "toggle_autostart":
            on = not autostart_enabled()
            set_autostart(on)
            self.icon.update_menu()
            self.hud.show("done", AUTOSTART_TEXT[1] if on else AUTOSTART_TEXT[2],
                          "Change it any time in Settings", hold=2.5)
            self.settings_changed()
        elif isinstance(ev, tuple) and ev[0] == "change_hotkey":
            if self.state == "idle" and not self.capture:
                self.capture_target = ev[1]
                self.start_capture()
                self.settings_changed()
        elif isinstance(ev, tuple) and ev[0] in ("captured", "capture_cancelled", "capture_hint"):
            self._capture_event(*ev)
            self.settings_changed()
        elif ev == "recopy":
            self.recopy()
        elif ev == "open_folder":
            os.makedirs(CONFIG["recordings_dir"], exist_ok=True)
            if paths.MAC:
                subprocess.Popen(["open", CONFIG["recordings_dir"]])
            else:
                os.startfile(CONFIG["recordings_dir"])
        elif isinstance(ev, tuple) and ev[0] == "worker":
            self._worker_line(*ev[1:])

    # ---------- actions ----------
    def start(self):
        self.recorder.ignore = self.on_pill
        self.recorder.start()
        self.hud.pen_button, self.hud.pen_on = True, False
        if paths.MAC:  # the pill's own window gets the click; no Accessibility needed
            self.hud.on_click = lambda x, y: self._pill_click(x, y, None, True)
        else:
            self.pill_listener = mouse.Listener(on_click=self._pill_click)
            self.pill_listener.start()
        self.dinged = False
        self.state = "recording"
        self._set_tray(REC)
        # Wherever you were when you pressed the hotkey (Claude, ChatGPT in a browser...) is
        # where the recording goes back to when it's ready.
        self.return_hwnd = mac.frontmost_pid() if paths.MAC else user32.GetForegroundWindow()
        rec_dir = self.recorder.dir
        self.worker = Worker(rec_dir, lambda line: self.events.put(("worker", rec_dir, line)))
        if paths.MAC:  # Esc as a hotkey while recording (a key listener needs Accessibility)
            self.esc_listener = mac.Hotkey("esc", lambda: self.events.put("cancel"))
        else:
            self.esc_listener = keyboard.Listener(
                on_press=lambda k: self.events.put("cancel") if k == keyboard.Key.esc else None)
            self.esc_listener.start()
        self.hud.show("arming", "Starting")

    def _abandon_recording(self):
        """After an unexpected error: if a recording is live, stop and discard it, so nothing keeps
        recording behind a pill that says idle."""
        if not self.recorder.active:
            return
        for step in (self._stop_esc, lambda: self.worker and self.worker.cancel(), self.recorder.cancel):
            try:
                step()
            except Exception as e:
                log("abandoning the recording: %r" % e)
        self.worker = None
        if self.drawer and self.recorder.drawing:
            try:
                self.toggle_draw()
            except Exception:
                pass

    def _stop_esc(self):
        self.hud.pen_button = self.hud.pen_on = False
        if getattr(self, "pill_listener", None):
            self.pill_listener.stop()
            self.pill_listener = None
        if self.esc_listener:
            self.esc_listener.stop()
            self.esc_listener = None

    def stop(self):
        self._stop_esc()
        if self.recorder.drawing:
            self.toggle_draw()
        self.state = "processing"
        self._build_heard = time.monotonic()
        self._set_tray(BUSY)
        self.duration = self.recorder.elapsed()
        self.hud.show("processing", "Building your recording", "Saving the recording", progress=0.03)
        worker = self.worker

        def wind_down():  # off the UI thread so the overlay keeps animating
            try:
                self.recorder.stop()
                worker.go()
            except Exception as e:
                log("stop failed: %r" % e)
                self.events.put(("worker", self.recorder.dir, "EXIT stop-failed"))
        threading.Thread(target=wind_down, daemon=True).start()

    def cancel(self):
        self._stop_esc()
        if self.recorder.drawing:
            self.toggle_draw()
        if self.worker:
            self.worker.cancel()
            self.worker = None
        self.state = "cancelling"
        self.hud.show("cancelled", "Recording cancelled", "Nothing was saved", hold=1.8)

        def wind_down():
            try:
                self.recorder.cancel()
            finally:
                self.events.put("cancelled")
        threading.Thread(target=wind_down, daemon=True).start()

    def ding(self):
        path = os.path.join(paths.RES, "ding.wav")
        if CONFIG["sounds"] and os.path.exists(path) and paths.MAC:
            subprocess.Popen(["afplay", path])
        elif CONFIG["sounds"] and os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)

    def _warm_up(self):
        """Probe the video encoder now rather than on the first recording, and on a fresh install
        fetch the speech model (~460 MB) with the pill saying so."""
        if not paths.MAC:  # Macs record with ScreenCaptureKit, no encoder to pick
            video_encoder()
        if models_cached():
            return
        self.hud.show("processing", "Getting Lookhere ready", "Downloading the speech model (one time, ~460 MB)",
                      progress=None)
        p = subprocess.run(paths.worker_cmd("-", ["--download"]), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=paths.NO_WINDOW)
        if p.returncode == 0:
            self.hud.show("done", "Lookhere is ready", "Press %s to record" % self.hotkey_label, hold=4)
        else:
            log("model download failed: %s" % p.stderr[-1500:])
            self.hud.show("error", "Couldn't download the speech model",
                          "Recordings still work, without a transcript. Restart Lookhere to retry", hold=7)

    # ---------- microphone ----------
    def test_mic(self, seconds=4):
        """Listen for a few seconds with a live meter so you can see the mic is working."""
        try:
            idx, dev = pick_input(CONFIG["mic"])
        except Exception as e:
            log("mic test: no microphone: %r" % e)
            self.hud.show("error", "No microphone found", "Plug one in, or check your sound settings", hold=4)
            return
        name = dev["name"]
        self.state = "mictest"
        self.hud.show("mic", "Testing your mic", "Say something \u00b7 %s" % name[:40])
        peak = [0.0]

        def cb(data, *_):
            lv = level_of(data)
            self.hud.level = lv
            peak[0] = max(peak[0], lv)

        def run():
            try:
                with input_stream(idx, int(dev["default_samplerate"]),
                                  max(1, min(2, int(dev["max_input_channels"]))), cb):
                    time.sleep(seconds)
                self.events.put(("mic_result", name, peak[0], None))
            except Exception as e:
                self.events.put(("mic_result", name, 0.0, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _mic_result(self, name, peak, err):
        self.state = "idle"
        self.hud.level = 0.0
        if err:
            self.hud.show("error", "Couldn't open that mic", err[:60], hold=5)
        elif peak < 0.15:
            self.hud.show("error", "Didn't hear anything", "Check %s is plugged in and not muted" % name[:34], hold=5)
        else:
            self.hud.show("done", "Mic works", name[:50], hold=3)
        log("mic test %s: peak %.2f %s" % (name, peak, err or ""))

    # ---------- tray menu ----------
    def menu_items(self):
        rec = lambda: self.state == "recording"
        put = self.events.put
        items = [
            dict(label=lambda: "Finish recording" if rec() else "Start recording", hint=self.hotkey_label,
                 action=lambda: put("toggle"), enabled=lambda: self.state in ("idle", "recording")),
            dict(label=lambda: "Resume" if rec() and self.recorder.paused else "Pause",
                 hint=self.label("pause_hotkey"), action=lambda: put("pause"), enabled=rec),
            dict(label="Draw on screen", hint=self.label("draw_hotkey"), action=lambda: put("draw"), enabled=rec),
            dict(label="Cancel recording", hint="Esc", action=lambda: put("cancel"), enabled=rec, danger=True),
            dict(sep=True),
            dict(label="History", action=lambda: put("history")),
            dict(label="Copy last recording again", action=lambda: put("recopy")),
            dict(sep=True),
            dict(label="Settings", action=lambda: put("settings")),
            dict(sep=True),
        ]
        if self.update_info:
            items.append(dict(label="Update to %s" % self.update_info["version"], action=lambda: put("install_update")))
        if CONFIG["check_updates"]:
            items.append(dict(label="Check for updates", hint=paths.VERSION, action=lambda: put(("check_update", True))))
        items.append(dict(label="Quit", action=lambda: put("quit")))
        return items

    def menu_status(self):
        if self.state == "recording":
            s = int(self.recorder.active_elapsed())
            return ("Paused" if self.recorder.paused else "Recording") + " \u00b7 %d:%02d" % (s // 60, s % 60)
        if self.state == "processing":
            return "Building your recording"
        return "Ready \u00b7 %s to record" % self.hotkey_label

    def open_menu(self, x, y):
        if paths.MAC:
            import macmenu
            if self.menu:  # a second click on the icon closes it
                self.menu.close_all()
                return
            self.menu = macmenu.Menu(self.menu_items(), x, y, self._menu_closed, header=self.menu_status())
            return
        import traymenu
        if self.menu:
            self.menu.close_all()
        self.menu = traymenu.Menu(self.root, self.menu_items(), x, y, on_close=self._menu_closed,
                                  post=self.events.put, header=self.menu_status())

    def _menu_closed(self):
        self.menu = None

    # ---------- theme ----------
    def _apply_themes(self):
        """Pill follows the app theme, tray icon the taskbar theme (they can differ in Windows)."""
        pick = CONFIG["theme"]
        if pick not in ("auto", "dark", "light"):
            pick = "auto"
        pill = windows_theme() if pick == "auto" else pick
        tray = windows_theme("SystemUsesLightTheme") if pick == "auto" else pick
        key = (pick, pill, tray)
        if key == self.themes:
            return
        self.themes = key
        self.hud.set_theme(pill)
        logo.set_theme(tray)
        if hasattr(self, "icon"):
            self.icon.icon = tray_image(self.tray_state)
        log("theme: %s (pill %s, tray %s)" % (pick, pill, tray))

    def _set_tray(self, state):
        self.tray_state = state
        self.icon.recording = state == REC
        self.icon.icon = tray_image(state)

    # ---------- changing the shortcut ----------
    def start_capture(self, note=None):
        k = self.capture_target
        self.hud.show("keys", "Press your new %s shortcut" % self.KEY_TITLES[k],
                      note or "For example %s \u00b7 Esc keeps %s" % (hotkey_label("<ctrl>+<alt>+k"), self.label(k)))
        self.capture = ShortcutCapture(self.events.put)

    def _capture_event(self, kind, value):
        target = self.capture_target
        if kind == "capture_hint":
            self.hud.show("keys", "Press your new %s shortcut" % self.KEY_TITLES[target], value)
            return
        self.capture = None
        self._captured_at = time.monotonic()
        if kind == "capture_cancelled":
            self.hud.show("cancelled", "Shortcut unchanged", "Still %s" % self.label(target), hold=2.5)
            return
        spec, label = value, hotkey_label(value)
        if label == self.label(target):
            self.hud.show("done", "That is already your shortcut", label, hold=2.5)
            return
        clash = next((k for k in self.keys if k != target and self.label(k) == label), None)
        if clash:
            self.start_capture("%s already does %s \u00b7 try another" % (label, self.KEY_TITLES[clash]))
            return
        new = SystemHotkey(spec, (lambda ev: lambda: self.events.put(ev))(self.KEY_EVENTS[target]))
        if not new.ok:
            # Taken by another app: say so and listen again.
            self.start_capture("%s is taken by another app \u00b7 try another" % label)
            return
        self.keys[target].stop()
        self.keys[target] = new
        self.hotkeys = self.keys["hotkey"]
        save_setting(target, spec)
        self.icon.title = "Lookhere (%s to record)" % self.hotkey_label
        self.icon.update_menu()
        self.hud.show("done", "Shortcut changed", "%s now %ss" % (label, self.KEY_TITLES[target]), hold=3)
        log("%s changed to %s" % (target, spec))

    # ---------- drawing on screen ----------
    def on_pill(self, x, y):
        if not paths.MAC:  # only its buttons (or an Alt+drag) take the press; the rest can be drawn on
            return self.hud.grabs(x, y)
        r = self.hud.pill_rect
        return bool(r and self.hud.pen_button and r[0] <= x <= r[2] and r[1] <= y <= r[3])

    def _pill_moved(self, offset):
        try:
            save_setting("pill_offset", [round(offset[0]), round(offset[1])])
            log("pill moved to offset %d, %d" % (round(offset[0]), round(offset[1])))
        except Exception as e:
            log("saving the pill position: %r" % e)

    def _pill_click(self, x, y, button, pressed):
        if not pressed or self.hud.alt_down():  # Alt+drag moves the pill, even from a button
            return
        for name, r in self.hud.buttons.items():
            if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                self.events.put({"draw": "draw", "stop": "toggle", "pause": "pause"}[name])
                return

    def toggle_draw(self):
        import draw
        if self.drawer is not None and not self.recorder.drawing:
            try:  # a monitor was plugged in, removed or resized since the overlay was made: remake it
                if [s.mon for s in self.drawer.screens] != draw.monitors():
                    self.drawer.close()
                    self.drawer = None
            except Exception as e:
                log("checking monitors for drawing: %r" % e)
        if self.drawer is None:
            self.drawer = draw.DrawLayer(on_stroke=lambda: self.recorder.mark("draw"), on_error=log,
                                         blocked=self.on_pill)
        draw.STYLE = CONFIG["draw_style"]
        on = not self.recorder.drawing
        self.recorder.drawing = on
        self.drawer.set_active(on)
        self.hud.pen_on = on
        if on:
            threading.Timer(0.15, self.hud.bring_to_front).start()  # pill stays clickable above the ink
        log("drawing %s" % ("on" if on else "off"))

    def settings_changed(self):
        import settings
        settings.refresh()

    # ---------- history ----------
    def open_history(self):
        import history
        history.open_window(self)

    # ---------- updates ----------
    def _check_update(self, manual=False):
        import updater
        if not paths.FROZEN:
            if manual:
                self.events.put(("update_failed", "This copy runs from source; updates come from build.py"))
            return
        try:
            info = updater.latest(CONFIG["update_repo"])
        except Exception as e:
            log("update check failed: %r" % e)
            if manual:
                self.events.put(("update_failed", "Couldn't reach GitHub"))
            return
        if updater.is_newer(info):
            log("update available: %s" % info["version"])
            self.events.put(("update_found", info))
        elif manual:
            self.events.put(("up_to_date", None))

    def _update_loop(self):
        time.sleep(20)
        try:
            import updater
            if self.state != "updating":
                updater.clean_downloads()  # the installer that brought this version in
        except Exception as e:
            log("clearing old update downloads: %r" % e)
        last = None
        while True:  # follows the setting: switching it back on resumes checks without a restart
            if CONFIG["check_updates"] and (last is None or time.monotonic() - last >= 6 * 3600):
                last = time.monotonic()
                self._check_update()
            time.sleep(60)

    def _install_update(self):
        import updater
        try:
            path = updater.download(self.update_info, lambda f: self.events.put(("update_progress", f)))
            log("installing update %s" % path)
            self.hud.show("processing", "Installing the update", "Lookhere restarts by itself", progress=1.0)
            time.sleep(1.0)
            updater.install(path)  # the installer closes this app and starts the new one
            if paths.MAC:  # Mac: the new app is staged; it's swapped in and opened once this one quits
                self.events.put("quit")
                return
            time.sleep(180)  # still here = the installer never got as far as closing us
            log("update: the installer didn't finish")
            self.state = "idle"
            self.events.put(("update_failed", "The installer didn't finish. Try again later"))
        except Exception as e:
            log("update failed: %r" % e)
            self.state = "idle"
            self.events.put(("update_failed", str(e)))

    def _to_clipboard(self, rec_dir):
        """Put a finished recording's video (and its fallback text) on the clipboard. True if it's there."""
        video = os.path.join(rec_dir, "preview.mp4")
        if not os.path.exists(video):
            return False
        try:
            with open(os.path.join(rec_dir, "message.txt"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = "I made a screen recording to show you what I mean."
        return set_clipboard(text, [video]) is not False

    def _clipboard_busy(self):
        self.hud.show("error", "Couldn't reach the clipboard", "Another app is using it. Copy it again from History",
                      hold=5)

    def copy_recording(self, rec_dir, title="Recording is on your clipboard"):
        if not self._to_clipboard(rec_dir):
            log("copy failed %s" % rec_dir)
            return self._clipboard_busy()
        self.hud.show("done", title, "Paste it into any chat with %s" % PASTE_KEYS, hold=3)
        log("copied %s" % rec_dir)

    def recopy(self):
        """Put the newest finished recording back on the clipboard."""
        root = CONFIG["recordings_dir"]
        done = sorted(n for n in (os.listdir(root) if os.path.isdir(root) else [])
                      if os.path.exists(os.path.join(root, n, "preview.mp4")))
        if not done:
            self.hud.show("error", "No finished recording yet", "Record one with %s" % self.hotkey_label, hold=3)
            return
        d = os.path.join(root, done[-1])
        if not self._to_clipboard(d):
            log("recopy failed %s" % d)
            return self._clipboard_busy()
        self.hud.show("done", "Recording is on your clipboard", "Copied the last one again (%s)" % done[-1][11:], hold=3)
        log("recopied %s" % d)

    def _reset(self):
        self.state = "idle"
        self._set_tray(IDLE)

    def _worker_line(self, rec_dir, line):
        if self.state != "processing" or rec_dir != self.recorder.dir:
            return
        self._build_heard = time.monotonic()
        if line.startswith("STAGE "):
            _, frac, text = line.split(" ", 2)
            self.hud.show("processing", "Building your recording", text, progress=float(frac))
            log("stage %s %s" % (frac, text))
        elif line.startswith("DONE "):
            self.finish(rec_dir, True)
        elif line.startswith("EXIT ") and line != "EXIT 0":
            log("process.py failed (%s), see process-last.log" % line)
            self.finish(rec_dir, False)

    def finish(self, rec_dir, ok):
        self.worker = None
        try:
            self._deliver(rec_dir, ok)
        finally:
            self._reset()  # only "idle" once the recording is on the clipboard

    def _deliver(self, rec_dir, ok):
        if not ok:
            self.hud.show("error", "Couldn't build the recording", "See screen-explain.log beside the recordings folder",
                          hold=5)
            return
        # Just the video as a file: pasting attaches it in Claude, ChatGPT and other chat apps.
        # The text is a fallback for apps that only take text.
        if not self._to_clipboard(rec_dir):
            log("built %s but couldn't put it on the clipboard" % rec_dir)
            return self._clipboard_busy()
        focused = bring_to_front(self.return_hwnd)
        if not focused and CONFIG["focus_window_title"]:
            focused = focus_window(CONFIG["focus_window_title"])
        s = int(round(self.duration))
        r = self.recorder
        if r.screen_blocked or r.mic_blocked:  # Mac: say which permission to switch on
            self.hud.show("error", "Lookhere can't see the screen" if r.screen_blocked else "Lookhere can't hear the mic",
                          "System Settings > Privacy & Security > %s: switch Lookhere on"
                          % ("Screen & System Audio Recording" if r.screen_blocked else "Microphone"), hold=9)
            log("bundle ready %s, but %s permission is off" % (rec_dir, "screen" if r.screen_blocked else "mic"))
            return
        if self.recorder.no_mic:
            self.hud.show("done", "Recording is on your clipboard",
                          "No microphone found, so it has no voice · %d:%02d" % (s // 60, s % 60), hold=6)
        else:
            self.hud.show("done", "Recording is on your clipboard",
                          "Paste it into any chat with %s · %d:%02d" % (PASTE_KEYS, s // 60, s % 60), hold=4.5)
        log("bundle ready %s (returned to start window: %s)" % (rec_dir, focused))

    def run(self):
        self.root.mainloop()


def models_cached():
    """True once the speech model has fully downloaded (a folder alone can be an interrupted download)."""
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    snaps = os.path.join(hub, "models--Systran--faster-whisper-small.en", "snapshots")
    try:
        return any(os.path.exists(os.path.join(snaps, s, "model.bin")) for s in os.listdir(snaps))
    except OSError:
        return False


def selftest(seconds=5):
    """Record a few seconds (no hotkey, no tray), build it and check every output. Run after each
    build: Lookhere.exe --selftest. Writes selftest.json next to the logs; exit code 0 = pass."""
    out = {"version": paths.VERSION, "frozen": paths.FROZEN, "data_dir": paths.DATA}
    T = time.monotonic()
    r = Recorder()
    try:
        r.start()
        w = subprocess.Popen(paths.worker_cmd(r.dir, ["--wait"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=open(os.path.join(HERE, "selftest-worker.log"), "w"), text=True,
                             encoding="utf-8", errors="replace", creationflags=paths.NO_WINDOW)
        time.sleep(seconds)
        r.stop()
        t_stop = time.monotonic()
        w.stdin.write("go\n")
        w.stdin.flush()
        lines = w.communicate(timeout=900)[0].splitlines()
        meta = json.load(open(os.path.join(r.dir, "events.json"), encoding="utf-8"))
        files = {n: os.path.getsize(os.path.join(r.dir, n)) for n in os.listdir(r.dir)
                 if os.path.isfile(os.path.join(r.dir, n))}
        out.update({
            "encoder": "ScreenCaptureKit" if paths.MAC else _ENCODER, "video_live": bool(meta.get("video")), "frames": len(meta["frames"]),
            "build_seconds": round(time.monotonic() - t_stop, 1), "worker_exit": w.returncode,
            "worker_last_lines": lines[-3:], "files": files,
            "transcription": next((l for l in open(os.path.join(r.dir, "recording.md"), encoding="utf-8")
                                   if "Transcription:" in l), "").strip(),
        })
        out["ok"] = (w.returncode == 0 and files.get("preview.mp4", 0) > 10000 and "recording.md" in files
                     and out["video_live"])
        try:  # 0 = pure silence: on a Mac, Lookhere doesn't have the Microphone permission
            import numpy as np
            import soundfile
            out["audio_peak"] = round(float(np.abs(soundfile.read(os.path.join(r.dir, "audio.wav"))[0]).max()), 5)
        except Exception as e:
            out["audio_peak"] = repr(e)[:80]
        shutil.rmtree(r.dir, ignore_errors=True)  # this test's own recording
    except Exception:
        out["ok"] = False
        out["error"] = traceback.format_exc()
        if getattr(r, "video", None) and not r.video.ended():
            r.video.stop()  # don't leave the screen recorder running
        if getattr(r, "dir", None):
            shutil.rmtree(r.dir, ignore_errors=True)
    out["total_seconds"] = round(time.monotonic() - T, 1)
    text = json.dumps(out, indent=1)
    with open(os.path.join(HERE, "selftest.json"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0 if out["ok"] else 1


def main():
    if paths.MAC and paths.FROZEN:
        # Libraries start multiprocessing's resource tracker as `<app> -c ...`; without this the
        # packaged app ran a whole second Lookhere instead, which blocked relaunching after updates.
        import multiprocessing
        multiprocessing.freeze_support()
    if "--worker" in sys.argv:  # packaged app: the same exe builds recordings (process.py)
        import process
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--worker"]
        process.main()
        return
    if "--mic" in sys.argv:  # packaged Mac app: the microphone process (mic.py)
        import mic
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--mic"]
        mic.main()
        return
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--update" in sys.argv:  # install the newest release now, no UI (tests the update path)
        import updater
        info = updater.latest(CONFIG["update_repo"])
        print("latest:", info and info["version"], "running:", paths.VERSION)
        if updater.is_newer(info):
            updater.install(updater.download(info))
        return
    if sys.stderr is None:  # pythonw: keep library warnings and crashes somewhere readable
        sys.stderr = sys.stdout = open(os.path.join(HERE, "screen-explain.err.log"), "a", encoding="utf-8")
    if paths.MAC:
        import fcntl
        mutex = open(os.path.join(HERE, ".lookhere.lock"), "w")
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Lookhere is already running")
            return
    else:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # plain windll can lose the error code
        k32.CreateMutexW.restype = wt.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        ctypes.set_last_error(0)
        mutex = k32.CreateMutexW(None, False, "Local\\screen-explain-single-instance")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            print("Lookhere is already running")
            return
    try:
        os.makedirs(CONFIG["recordings_dir"], exist_ok=True)
    except OSError as e:
        log("can't create the recordings folder %s: %r" % (CONFIG["recordings_dir"], e))
    cleanup_old()
    App().run()
    del mutex


if __name__ == "__main__":
    main()

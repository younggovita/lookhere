"""Where Lookhere's files live, the same code whether it runs from source or as the packaged app.

From source: everything lives next to the code.
Packaged (Lookhere.exe from the installer): bundled read-only files sit in the app's _internal
folder; recordings, logs and config.json go to %LOCALAPPDATA%\\Lookhere, so updating or
reinstalling the app never touches them.
"""
import glob
import os
import shutil
import sys

MAC = sys.platform == "darwin"
NO_WINDOW = 0 if MAC else 0x08000000  # CREATE_NO_WINDOW for child processes; Popen refuses it off Windows
FROZEN = getattr(sys, "frozen", False)
SRC = os.path.dirname(os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", SRC)
if not FROZEN:
    DATA = SRC
elif MAC:
    DATA = os.path.expanduser("~/Library/Application Support/Lookhere")
else:
    DATA = os.path.join(os.environ.get("LOCALAPPDATA", SRC), "Lookhere")
os.makedirs(DATA, exist_ok=True)

try:
    with open(os.path.join(RES, "VERSION"), encoding="utf-8") as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "dev"

# GPU speech needs ~2 GB of NVIDIA libraries: present in the source venv, not shipped in the installer.
CUDA_DIRS = glob.glob(os.path.join(RES, "nvidia", "*", "bin")) + \
    glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin"))
GPU_SPEECH = bool(CUDA_DIRS)


def venv_python(windowed=False):
    if MAC:
        return os.path.join(SRC, "venv", "bin", "python")
    return os.path.join(SRC, "venv", "Scripts", "pythonw.exe" if windowed else "python.exe")


def ffmpeg_exe():
    bundled = os.path.join(RES, "ffmpeg" if MAC else "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if MAC:  # apps started by launchd don't have Homebrew on PATH
        return next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), None)
    hits = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\*\bin\ffmpeg.exe"))
    return hits[0] if hits else None


def app_cmd():
    """How to start the tray app itself (used for "Start with Windows")."""
    if FROZEN:
        return '"%s"' % sys.executable
    return '"%s" "%s"' % (venv_python(windowed=True), os.path.join(SRC, "screen_explain.py"))


def mic_cmd(args):
    """The microphone process (mic.py, Mac only), from source or from the packaged app."""
    if FROZEN:
        return [sys.executable, "--mic", *args]
    return [venv_python(), "-I", os.path.join(SRC, "mic.py"), *args]


def worker_cmd(rec_dir, extra=()):
    """The command that builds a recording (process.py), from source or from the packaged exe."""
    if FROZEN:
        return [sys.executable, "--worker", rec_dir, *extra]
    return [venv_python(), "-I", os.path.join(SRC, "process.py"), rec_dir, *extra]

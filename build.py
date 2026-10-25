"""Build the Lookhere installer.

    venv\\Scripts\\python.exe build.py            build dist\\Lookhere-Setup-<version>.exe
    venv\\Scripts\\python.exe build.py --bump     bump the patch version first (0.1.0 -> 0.1.1)
    venv\\Scripts\\python.exe build.py --test     also install into a temp folder and run the self-test

Steps: PyInstaller bundles the app (one folder, no Python needed on the target PC), ffmpeg.exe
and the ding go in with it, then Inno Setup wraps that folder into one installer. Speech runs
on the CPU in the installed app; the model downloads on first launch. Recordings and settings
live in %LOCALAPPDATA%\\Lookhere, so installing a newer version over an older one keeps them.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

PY = os.path.join(HERE, "venv", "Scripts", "python.exe")
ISCC = [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"]


def run(cmd, **kw):
    print(">", " ".join(cmd[:4]), "..." if len(cmd) > 4 else "", flush=True)
    subprocess.run(cmd, check=True, cwd=HERE, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bump", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--publish", action="store_true", help="upload the installer as a GitHub release")
    ap.add_argument("--notes", default="", help="release notes for --publish")
    args = ap.parse_args()

    vfile = os.path.join(HERE, "VERSION")
    version = open(vfile, encoding="utf-8").read().strip()
    if args.bump:
        major, minor, patch = (int(x) for x in version.split("."))
        version = "%d.%d.%d" % (major, minor, patch + 1)
        with open(vfile, "w", encoding="utf-8") as f:
            f.write(version + "\n")
    print("Building Lookhere", version)

    ffmpeg = paths.ffmpeg_exe()
    if not ffmpeg:
        sys.exit("ffmpeg.exe not found")
    work = os.path.join(HERE, "build")
    sep = os.pathsep
    run([PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "Lookhere",
         "--icon", os.path.join(HERE, "lookhere.ico"), "--distpath", os.path.join(work, "app"), "--workpath", os.path.join(work, "tmp"),
         "--specpath", work,
         "--add-data", "%s%s." % (os.path.join(HERE, "ding.wav"), sep),
         "--add-data", "%s%s." % (vfile, sep),
         "--add-data", "%s%s." % (os.path.join(HERE, "lookhere.ico"), sep),
         "--add-binary", "%s%s." % (ffmpeg, sep),
         "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "onnxruntime",
         "--collect-all", "av", "--collect-all", "sounddevice",
         "--hidden-import", "pynput.keyboard._win32", "--hidden-import", "pynput.mouse._win32",
         "--hidden-import", "pystray._win32", "--hidden-import", "process",
         "--exclude-module", "nvidia", "--exclude-module", "PyInstaller",
         os.path.join(HERE, "screen_explain.py")])

    iscc = next((p for p in ISCC if os.path.exists(p)), None)
    if not iscc:
        sys.exit("Inno Setup not found (winget install JRSoftware.InnoSetup)")
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    run([iscc, "/Q", "/DAppVersion=" + version, "/DAppSrc=" + os.path.join(work, "app", "Lookhere"),
         "/O" + os.path.join(HERE, "dist"), os.path.join(HERE, "installer.iss")])
    setup = os.path.join(HERE, "dist", "Lookhere-Setup-%s.exe" % version)
    print("Built %s (%.0f MB)" % (setup, os.path.getsize(setup) / 1e6))

    if args.test:
        test_install(setup)
    if args.publish:
        publish(setup, version, args.notes)


def publish(setup, version, notes):
    """GitHub release v<version> with the installer and its SHA-256: what installed copies update from."""
    import hashlib
    h = hashlib.sha256(open(setup, "rb").read()).hexdigest()
    sha_file = setup + ".sha256"
    with open(sha_file, "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (h, os.path.basename(setup)))
    repo = json.load(open(os.path.join(HERE, "release.json"), encoding="utf-8"))["repo"]
    run(["gh", "release", "create", "v" + version, setup, sha_file, "--repo", repo,
         "--title", "Lookhere " + version, "--notes", notes or "Lookhere " + version])
    print("Published v%s to %s" % (version, repo))


def test_install(setup):
    """Install silently into a temp folder (no shortcuts, no autostart), run the self-test, uninstall."""
    target = tempfile.mkdtemp(prefix="lookhere-test-")
    run([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS", "/TASKS=", "/DIR=" + target])
    exe = os.path.join(target, "Lookhere.exe")
    T = time.monotonic()
    p = subprocess.run([exe, "--selftest"], capture_output=True, text=True, timeout=1200)
    data = os.path.join(os.environ["LOCALAPPDATA"], "Lookhere")
    result = json.load(open(os.path.join(data, "selftest.json"), encoding="utf-8"))
    print(json.dumps(result, indent=1))
    print("self-test exit %d after %.0fs" % (p.returncode, time.monotonic() - T))
    unins = os.path.join(target, "unins000.exe")
    if os.path.exists(unins):
        subprocess.run([unins, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        time.sleep(3)
    shutil.rmtree(target, ignore_errors=True)
    if p.returncode != 0:
        sys.exit("self-test FAILED")
    print("self-test passed, test install removed")


if __name__ == "__main__":
    main()

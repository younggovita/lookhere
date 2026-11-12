"""Build the Lookhere installer.

    venv\\Scripts\\python.exe build.py            build dist\\Lookhere-Setup-<version>.exe
    venv\\Scripts\\python.exe build.py --bump     bump the patch version first (0.1.0 -> 0.1.1)
    venv\\Scripts\\python.exe build.py --test     also install into a temp folder and run the self-test

    venv/bin/python build.py --mac [--bump] [--test] [--publish]
        on a Mac: dist/Lookhere-<version>-mac.dmg (menu bar app, ad-hoc signed); --test runs the
        self-test from the built app; --publish adds the dmg to the v<version> release

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
    ap.add_argument("--mac", action="store_true", help="build the Mac .app and .dmg (run on a Mac)")
    args = ap.parse_args()
    if args.mac != (sys.platform == "darwin"):
        sys.exit("--mac builds the Mac app on a Mac; on Windows build without it")

    vfile = os.path.join(HERE, "VERSION")
    version = open(vfile, encoding="utf-8").read().strip()
    if args.bump:
        major, minor, patch = (int(x) for x in version.split("."))
        version = "%d.%d.%d" % (major, minor, patch + 1)
        with open(vfile, "w", encoding="utf-8") as f:
            f.write(version + "\n")
    print("Building Lookhere", version)
    if args.mac:
        import build_mac
        dmg = build_mac.build(version)
        if args.test:
            build_mac.test(dmg)
        if args.publish:
            publish([dmg], version, args.notes)
        return

    ffmpeg = paths.ffmpeg_exe()
    if not ffmpeg:
        sys.exit("ffmpeg.exe not found")
    run([PY, os.path.join(HERE, "make_licenses.py")])  # licenses/: third-party notices for the installer
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

    # Compiled leftovers inside package metadata carry this PC's folder paths: don't ship them.
    for root, dirs, _ in os.walk(os.path.join(work, "app", "Lookhere")):
        if ".dist-info" in root and "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
            dirs.remove("__pycache__")

    iscc = next((p for p in ISCC if os.path.exists(p)), None)
    if not iscc:
        sys.exit("Inno Setup not found (winget install JRSoftware.InnoSetup)")
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    run([iscc, "/Q", "/DAppVersion=" + version, "/DAppSrc=" + os.path.join(work, "app", "Lookhere"),
         "/DLicSrc=" + os.path.join(HERE, "licenses"),
         "/O" + os.path.join(HERE, "dist"), os.path.join(HERE, "installer.iss")])
    setup = os.path.join(HERE, "dist", "Lookhere-Setup-%s.exe" % version)
    print("Built %s (%.0f MB)" % (setup, os.path.getsize(setup) / 1e6))
    # the plain-text guide to send alongside the installer
    shutil.copyfile(os.path.join(HERE, "INSTALL.txt"), os.path.join(HERE, "dist", "Lookhere - How to install.txt"))

    if args.test:
        test_install(setup)
    if args.publish:
        publish([setup], version, args.notes)


MAC_NOTES = ("Mac (Apple Silicon, macOS 15 or later): open the .dmg and drag Lookhere into Applications. "
             "It's signed and notarised by Apple. The first recording asks for the microphone and Screen "
             "Recording; updates keep both.")


def publish(files, version, notes):
    """Add the installers (each with its SHA-256) to GitHub release v<version>, creating the release
    if this is the first platform to publish it: what installed copies update from. The PC and the
    Mac each publish their own file to the same tag."""
    import hashlib
    repo = json.load(open(os.path.join(HERE, "release.json"), encoding="utf-8"))["repo"]
    upload = []
    for path in files:
        h = hashlib.sha256(open(path, "rb").read()).hexdigest()
        with open(path + ".sha256", "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (h, os.path.basename(path)))
        upload += [path, path + ".sha256"]
    tag = "v" + version
    mac = any(p.endswith(".dmg") for p in files)
    found = subprocess.run(["gh", "release", "view", tag, "--repo", repo, "--json", "body"],
                           capture_output=True, text=True)
    if found.returncode != 0:
        notes = notes or "Lookhere " + version
        if mac and MAC_NOTES not in notes:
            notes += "\n\n" + MAC_NOTES
        # Tag the commit that was built (it must be pushed), not whatever main is at by then
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True).stdout.strip()
        run(["gh", "release", "create", tag, *upload, "--repo", repo, "--title", "Lookhere " + version,
             "--notes", notes] + (["--target", head] if head else []))
    else:
        run(["gh", "release", "upload", tag, *upload, "--repo", repo, "--clobber"])
        body = json.loads(found.stdout).get("body") or ""
        if mac and MAC_NOTES not in body:
            run(["gh", "release", "edit", tag, "--repo", repo, "--notes", (body + "\n\n" + MAC_NOTES).strip()])
    print("Published %s to %s %s" % (", ".join(os.path.basename(p) for p in files), repo, tag))


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

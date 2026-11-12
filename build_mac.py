"""Build the Mac app: dist/Lookhere-<version>-mac.dmg. Run through build.py on a Mac:

    venv/bin/python build.py --mac              build the .app and the .dmg
    venv/bin/python build.py --mac --test       also copy the app out of the dmg and run its self-test
    venv/bin/python build.py --mac --publish    also add the dmg to the GitHub release v<version>

PyInstaller bundles the app (no Python needed on the tester's Mac) as a menu bar app with no Dock
icon (LSUIElement). A static ffmpeg from the imageio-ffmpeg package and the licenses/ folder go in
with it. With a "Developer ID Application" certificate in the keychain, every binary is signed with
the hardened runtime (mac-entitlements.plist), and the dmg is signed, notarised by Apple (keychain
profile "lookhere-notary", made once with `xcrun notarytool store-credentials lookhere-notary`)
and stapled: testers open it with no Gatekeeper step, and updates keep the Screen Recording and
Microphone permissions. Without the certificate it falls back to an ad hoc signature, loudly.
Apple Silicon only, macOS 15 or later (ScreenCaptureKit's recording
output). Recordings and settings live in ~/Library/Application Support/Lookhere, so replacing the
app keeps them.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "venv", "bin", "python")
WORK = os.path.join(HERE, "build", "mac")
DATA = os.path.expanduser("~/Library/Application Support/Lookhere")  # paths.DATA in the packaged app
ENTITLEMENTS = os.path.join(HERE, "mac-entitlements.plist")
NOTARY_PROFILE = "lookhere-notary"

PLIST = {
    "LSUIElement": True,  # menu bar only, no Dock icon
    "LSMinimumSystemVersion": "15.0",
    "NSMicrophoneUsageDescription": "Lookhere records your voice while you explain what's on screen.",
    "NSScreenCaptureUsageDescription": "Lookhere records your screen while you explain what's wrong.",
    "NSHumanReadableCopyright": "Third-party licences: Lookhere.app/Contents/Resources/licenses",
}


def run(cmd, **kw):
    print(">", " ".join(cmd[:4]), "..." if len(cmd) > 4 else "", flush=True)
    subprocess.run(cmd, check=True, cwd=HERE, **kw)


def static_ffmpeg():
    """imageio-ffmpeg's ffmpeg links only macOS system libraries, so it runs on any Mac
    (Homebrew's needs Homebrew's dylibs). pip install imageio-ffmpeg in the venv."""
    out = subprocess.run([PY, "-c", "import imageio_ffmpeg as i; print(i.get_ffmpeg_exe())"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("imageio-ffmpeg missing: venv/bin/python -m pip install imageio-ffmpeg")
    return out.stdout.strip()


def notary_auth():
    """notarytool credentials: the App Store Connect key named in ~/.appstoreconnect/lookhere-notary.json
    when present (works from launchd, where the keychain profile isn't visible), else the profile."""
    cfg = os.path.expanduser("~/.appstoreconnect/lookhere-notary.json")
    if os.path.exists(cfg):
        import json
        c = json.load(open(cfg))
        return ["--key", c["key"], "--key-id", c["key_id"], "--issuer", c["issuer"]]
    return ["--keychain-profile", NOTARY_PROFILE]


def identity():
    """The Developer ID Application identity's name, or None (ad hoc). LOOKHERE_SIGN_ID overrides."""
    if os.environ.get("LOOKHERE_SIGN_ID"):
        return None if os.environ["LOOKHERE_SIGN_ID"] == "-" else os.environ["LOOKHERE_SIGN_ID"]
    out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    names = [line.split('"')[1] for line in out.splitlines() if '"Developer ID Application:' in line]
    return names[0] if names else None


def build(version):
    ident = identity()
    if ident:
        print("Signing as", ident)
    else:
        print("\n" + "!" * 70 + "\nNo Developer ID Application certificate: AD HOC signature, not notarised.\n"
              "Testers get the Open Anyway step and lose permissions on every update.\n" + "!" * 70 + "\n")
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    ffmpeg = os.path.join(WORK, "ffmpeg")
    shutil.copyfile(static_ffmpeg(), ffmpeg)
    os.chmod(ffmpeg, 0o755)
    run([PY, os.path.join(HERE, "make_licenses.py")], env=dict(os.environ, LOOKHERE_FFMPEG=ffmpeg))

    def data(path, dest="."):
        return ["--add-data", "%s:%s" % (path, dest)]
    run([PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "Lookhere",
         "--osx-bundle-identifier", os.environ.get("LOOKHERE_BUNDLE_ID", "com.lookhere.app"),  # test builds: own TCC rows
         *(["--codesign-identity", ident, "--osx-entitlements-file", ENTITLEMENTS] if ident else []), "--icon", os.path.join(HERE, "lookhere.ico"),
         "--distpath", os.path.join(WORK, "app"), "--workpath", os.path.join(WORK, "tmp"), "--specpath", WORK,
         *data(os.path.join(HERE, "ding.wav")), *data(os.path.join(HERE, "VERSION")),
         *data(os.path.join(HERE, "lookhere.ico")), *data(os.path.join(HERE, "licenses"), "licenses"),
         "--add-binary", "%s:." % ffmpeg,
         "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "onnxruntime",
         "--collect-all", "av", "--collect-all", "sounddevice",
         "--hidden-import", "pynput.keyboard._darwin", "--hidden-import", "pynput.mouse._darwin",
         "--hidden-import", "pystray._darwin", "--hidden-import", "process", "--hidden-import", "mic",
         "--hidden-import", "mac", "--hidden-import", "macmenu",
         "--exclude-module", "nvidia", "--exclude-module", "PyInstaller", "--exclude-module", "imageio_ffmpeg",
         os.path.join(HERE, "screen_explain.py")])
    app = os.path.join(WORK, "app", "Lookhere.app")
    # Compiled leftovers inside package metadata carry the build machine's paths: don't ship them.
    for root, dirs, _ in os.walk(app):
        if ".dist-info" in root and "__pycache__" in dirs:
            shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
            dirs.remove("__pycache__")

    info = os.path.join(app, "Contents", "Info.plist")
    with open(info, "rb") as f:
        plist = plistlib.load(f)
    plist.update(PLIST, CFBundleShortVersionString=version, CFBundleVersion=version)
    with open(info, "wb") as f:
        plistlib.dump(plist, f)
    if ident:  # PyInstaller signed every nested binary; the plist edit needs the bundle re-sealed
        run(["codesign", "--force", "--timestamp", "--options", "runtime", "--entitlements", ENTITLEMENTS,
             "--sign", ident, app])
    else:
        run(["codesign", "--force", "--deep", "--sign", "-", app])
    run(["codesign", "--verify", "--deep", "--strict", app])

    stage = os.path.join(WORK, "dmg")
    os.makedirs(stage)
    run(["ditto", app, os.path.join(stage, "Lookhere.app")])
    os.symlink("/Applications", os.path.join(stage, "Applications"))
    shutil.copyfile(os.path.join(HERE, "INSTALL.txt"), os.path.join(stage, "How to install.txt"))
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    dmg = os.path.join(HERE, "dist", "Lookhere-%s-mac.dmg" % version)
    run(["hdiutil", "create", "-volname", "Lookhere " + version, "-srcfolder", stage, "-ov",
         "-format", "UDZO", dmg])
    if ident and os.environ.get("LOOKHERE_NO_NOTARY"):  # local test builds: signed, not sent to Apple
        run(["codesign", "--force", "--timestamp", "--sign", ident, dmg])
        print("LOOKHERE_NO_NOTARY set: signed but NOT notarised; don't publish this dmg")
    elif ident:
        run(["codesign", "--force", "--timestamp", "--sign", ident, dmg])
        run(["xcrun", "notarytool", "submit", dmg, *notary_auth(), "--wait"])
        # Stapling only helps a first launch with no internet (Gatekeeper otherwise checks Apple
        # online). It has failed when Apple's CDN served the wrong TLS certificate for
        # oscdn.apple.com, so a failed staple warns; the Gatekeeper check below must still pass.
        if subprocess.run(["xcrun", "stapler", "staple", dmg]).returncode != 0:
            print("\nWARNING: notarised but the ticket could NOT be stapled; Gatekeeper will check online."
                  "\nRetry later: xcrun stapler staple %s\n" % dmg)
        run(["spctl", "-a", "-vv", "-t", "open", "--context", "context:primary-signature", dmg])
    shutil.copyfile(os.path.join(HERE, "INSTALL.txt"), os.path.join(HERE, "dist", "Lookhere - How to install.txt"))
    print("Built %s (%.0f MB)" % (dmg, os.path.getsize(dmg) / 1e6))
    return dmg


def test(dmg):
    """Copy the app out of the dmg the way a tester would, run its self-test through Launch Services
    (so macOS treats it as its own app for Screen Recording and Microphone), then delete the copy."""
    target = tempfile.mkdtemp(prefix="lookhere-test-")
    mount = tempfile.mkdtemp(prefix="lookhere-dmg-")
    run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmg],
        capture_output=True)
    try:
        run(["ditto", os.path.join(mount, "Lookhere.app"), os.path.join(target, "Lookhere.app")])
    finally:
        subprocess.run(["hdiutil", "detach", mount, "-quiet"])
    result_file = os.path.join(DATA, "selftest.json")
    if os.path.exists(result_file):
        os.remove(result_file)
    T = time.monotonic()
    subprocess.run(["open", "-W", "-n", os.path.join(target, "Lookhere.app"), "--args", "--selftest"], timeout=1200)
    shutil.rmtree(target, ignore_errors=True)
    if not os.path.exists(result_file):
        sys.exit("self-test FAILED: no selftest.json (did the app start?)")
    result = json.load(open(result_file, encoding="utf-8"))
    print(json.dumps(result, indent=1))
    print("self-test took %.0fs" % (time.monotonic() - T))
    if not result.get("ok"):
        sys.exit("self-test FAILED")
    print("self-test passed, test copy removed")

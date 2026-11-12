"""Auto-update from GitHub Releases.

Each release (made by `build.py --publish`) carries Lookhere-Setup-<version>.exe for Windows and
Lookhere-<version>-mac.dmg for Macs, each with a .sha256 file. The installed app asks GitHub for
the latest release, and if it is newer, downloads its own platform's file and checks its SHA-256
against the published one.
Windows: runs the installer silently with /UPDATE=1; the installer closes Lookhere, replaces it,
and starts it again.
Mac: copies the new Lookhere.app out of the dmg next to the running one; once Lookhere quits, a
small shell script swaps the two and opens the new one.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import paths

SUFFIX = ".dmg" if paths.MAC else ".exe"  # which release asset this platform installs

API = "https://api.github.com/repos/%s/releases/latest"


def _ssl_context():
    """The Mac app's Python (python.org build) has no CA roots of its own: trust what macOS trusts,
    through truststore (already in the app for the speech model download)."""
    if not paths.MAC:
        return None
    import ssl
    import truststore
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Lookhere/" + paths.VERSION})
    return urllib.request.urlopen(req, timeout=20, context=_ssl_context())


def version_tuple(v):
    v = v.lstrip("vV")
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def latest(repo):
    """{"version", "installer_url", "sha256_url", "notes"} for the newest release, or None."""
    with _get(API % repo) as r:
        rel = json.load(r)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    exe = next((n for n in assets if n.lower().endswith(SUFFIX)), None)
    if not exe or exe + ".sha256" not in assets:
        return None
    return {"version": rel["tag_name"].lstrip("vV"), "installer_url": assets[exe],
            "sha256_url": assets[exe + ".sha256"], "notes": (rel.get("body") or "").strip(), "name": exe}


def is_newer(info, current=None):
    current = current or paths.VERSION
    return info is not None and current != "dev" and version_tuple(info["version"]) > version_tuple(current)


def download(info, progress=lambda frac: None):
    """Download the installer to a temp folder and verify it. Returns the file path."""
    with _get(info["sha256_url"], accept="application/octet-stream") as r:
        expected = r.read().decode().split()[0].strip().lower()
    if len(expected) != 64:
        raise ValueError("the release's SHA-256 file is malformed")
    clean_downloads()
    folder = tempfile.mkdtemp(prefix=DOWNLOAD_PREFIX)
    path = os.path.join(folder, info["name"])
    h = hashlib.sha256()
    try:
        with _get(info["installer_url"], accept="application/octet-stream") as r, open(path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total:
                    progress(done / total)
        if total and done != total:
            raise ValueError("the download was cut short")
        if h.hexdigest().lower() != expected:
            raise ValueError("downloaded installer doesn't match its published SHA-256")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # never leave a partial or unverified installer behind
        raise
    return path


DOWNLOAD_PREFIX = "lookhere-update-"


def clean_downloads():
    """Remove installers left in the temp folder by earlier updates (about 130 MB each)."""
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        if name.startswith(DOWNLOAD_PREFIX):
            shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)


def install(path):
    """Run the installer silently; it closes this app and starts the new version.
    Mac: stage the new app and return; the caller quits Lookhere, then the swap happens."""
    if paths.MAC:
        return _install_mac(path)
    # No /SUPPRESSMSGBOXES: if the install fails, the installer says why instead of Lookhere just vanishing.
    subprocess.Popen([path, "/SILENT", "/NORESTART", "/UPDATE=1"],
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP


# Waits for Lookhere ($1 = pid) to quit, swaps $3 (the staged new app) into $2, opens it.
# If the swap fails part-way, the old app goes back so there is always a Lookhere to open.
_SWAP = r"""
pid="$1"; app="$2"; new="$3"
i=0; while kill -0 "$pid" 2>/dev/null && [ $i -lt 300 ]; do sleep 0.2; i=$((i+1)); done
if kill -0 "$pid" 2>/dev/null; then echo "Lookhere is still running; update not applied"; rm -rf "$new"; exit 1; fi
tmp="$(mktemp -d)"
if mv "$app" "$tmp/Lookhere.app"; then
  mv "$new" "$app" || mv "$tmp/Lookhere.app" "$app"
fi
rm -rf "$tmp" "$new"
open "$app"
"""


def _install_mac(dmg):
    exe = os.path.realpath(sys.executable)  # .../Lookhere.app/Contents/MacOS/Lookhere
    app = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
    if not app.endswith(".app") or "/AppTranslocation/" in app or app.startswith("/Volumes/"):
        raise ValueError("Move Lookhere into Applications first, then update")
    staged = os.path.join(os.path.dirname(app), ".Lookhere-update.app")
    mount = tempfile.mkdtemp(prefix="lookhere-dmg-")
    try:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmg],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError:
        raise ValueError("Couldn't open the downloaded update")
    try:
        subprocess.run(["rm", "-rf", staged], check=True)
        try:
            subprocess.run(["ditto", os.path.join(mount, "Lookhere.app"), staged], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            raise ValueError("Can't write to %s" % os.path.dirname(app))
        try:
            subprocess.run(["codesign", "--verify", "--deep", staged], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            subprocess.run(["rm", "-rf", staged])
            raise ValueError("The downloaded update is damaged")
    finally:
        subprocess.run(["hdiutil", "detach", mount, "-quiet"])
    log = open(os.path.join(paths.DATA, "update.log"), "a")
    subprocess.Popen(["/bin/sh", "-c", _SWAP, "swap", str(os.getpid()), app, staged],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)

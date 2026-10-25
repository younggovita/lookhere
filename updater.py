"""Auto-update from GitHub Releases.

Each release (made by `build.py --publish`) carries Lookhere-Setup-<version>.exe and a .sha256
file. The installed app asks GitHub for the latest release, and if it is newer, downloads the
installer, checks its SHA-256 against the published one, and runs it silently with /UPDATE=1;
the installer closes Lookhere, replaces it, and starts it again.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request

import paths

API = "https://api.github.com/repos/%s/releases/latest"


def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Lookhere/" + paths.VERSION})
    return urllib.request.urlopen(req, timeout=20)


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
    exe = next((n for n in assets if n.lower().endswith(".exe")), None)
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
    folder = tempfile.mkdtemp(prefix="lookhere-update-")
    path = os.path.join(folder, info["name"])
    h = hashlib.sha256()
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
    if h.hexdigest().lower() != expected:
        os.remove(path)
        raise ValueError("downloaded installer doesn't match its published SHA-256")
    return path


def install(path):
    """Run the installer silently; it closes this app and starts the new version."""
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"],
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP

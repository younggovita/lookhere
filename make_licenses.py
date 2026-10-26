"""Collect third-party licences into licenses/ (run by build.py; the installer ships the folder).

Writes licenses/THIRD-PARTY-NOTICES.txt plus each component's own licence text. The bundled
ffmpeg is GPL v3, so the notice names the exact build and where its source code is.
"""
import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

OUT = os.path.join(HERE, "licenses")
SITE = os.path.join(HERE, "venv", "Lib", "site-packages")

# package, licence, project page; licence text found in the package's dist-info where it ships one
PACKAGES = [
    ("faster-whisper", "MIT", "https://github.com/SYSTRAN/faster-whisper"),
    ("ctranslate2", "MIT", "https://github.com/OpenNMT/CTranslate2"),
    ("onnxruntime", "MIT", "https://github.com/microsoft/onnxruntime"),
    ("av", "BSD-3-Clause", "https://github.com/PyAV-Org/PyAV"),
    ("pillow", "MIT-CMU (HPND)", "https://github.com/python-pillow/Pillow"),
    ("numpy", "BSD-3-Clause", "https://github.com/numpy/numpy"),
    ("mss", "MIT", "https://github.com/BoboTiG/python-mss"),
    ("sounddevice", "MIT (bundles PortAudio, MIT)", "https://github.com/spatialaudio/python-sounddevice"),
    ("soundfile", "BSD-3-Clause (bundles libsndfile, LGPL-2.1)", "https://github.com/bastibe/python-soundfile"),
    ("huggingface_hub", "Apache-2.0", "https://github.com/huggingface/huggingface_hub"),
    ("tokenizers", "Apache-2.0", "https://github.com/huggingface/tokenizers"),
    ("pynput", "LGPL-3.0", "https://github.com/moses-palmer/pynput"),
    ("pystray", "LGPL-3.0", "https://github.com/moses-palmer/pystray"),
]


def dist_info(pkg):
    names = [pkg, pkg.replace("-", "_")]
    for n in names:
        hits = glob.glob(os.path.join(SITE, "%s-*.dist-info" % n)) + \
            glob.glob(os.path.join(SITE, "%s-*.dist-info" % n.capitalize()))
        if hits:
            return hits[0]
    return None


def version_of(info):
    m = re.search(r"-([\d][^-]*)\.dist-info$", info or "")
    return m.group(1) if m else "?"


def copy_licence_files(pkg, info):
    """Copy every LICENSE/COPYING file from the dist-info. Returns the file names written."""
    found = []
    if not info:
        return found
    for path in glob.glob(os.path.join(info, "**", "*"), recursive=True):
        base = os.path.basename(path)
        if os.path.isfile(path) and re.match(r"(LICEN[CS]E|COPYING|NOTICE)", base, re.I):
            rel = os.path.relpath(path, info).replace(os.sep, "-").replace("licenses-", "")
            dest = "%s-%s" % (pkg, rel if rel.lower().endswith(".txt") else rel + ".txt")
            if dest not in found:  # vendored libraries can repeat a file name in different folders
                shutil.copyfile(path, os.path.join(OUT, dest))
                found.append(dest)
    return found


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    exe = paths.ffmpeg_exe()
    lines = [
        "Lookhere: third-party software notices",
        "=" * 39,
        "",
        "Lookhere includes the third-party software listed below. Each part is used under its own",
        "licence; the licence texts are in this folder.",
        "",
    ]

    # ---- ffmpeg (GPL v3) ----
    ver = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True).stdout
    first = ver.splitlines()[0] if ver else "ffmpeg"
    m = re.search(r"ffmpeg version (\d+(?:\.\d+)+)", first)
    number = m.group(1) if m else "?"
    gpl = os.path.join(os.path.dirname(os.path.dirname(exe)), "LICENSE")
    if os.path.exists(gpl):
        shutil.copyfile(gpl, os.path.join(OUT, "ffmpeg-LICENSE-GPLv3.txt"))
    lines += [
        "FFmpeg (ffmpeg.exe)",
        "-------------------",
        first,
        "Licence: GNU General Public License version 3 (file: ffmpeg-LICENSE-GPLv3.txt).",
        "Lookhere runs ffmpeg as a separate program; it is not part of Lookhere's own code.",
        "This is the unmodified Windows build from https://www.gyan.dev/ffmpeg/builds/.",
        "Source code: FFmpeg %s source is at https://github.com/FFmpeg/FFmpeg/releases/tag/n%s" % (number, number),
        "and https://ffmpeg.org/download.html; the build's configuration and the versions of its",
        "external libraries are listed in ffmpeg-build-configuration.txt. On request, the author of",
        "Lookhere will provide a copy of the corresponding source code for at least three years",
        "from the date of distribution.",
        "",
    ]
    with open(os.path.join(OUT, "ffmpeg-build-configuration.txt"), "w", encoding="utf-8") as f:
        f.write(ver)

    # ---- Python packages ----
    lines += ["Python libraries", "----------------"]
    lgpl_text = None
    for pkg, lic, url in PACKAGES:
        info = dist_info(pkg)
        files = copy_licence_files(pkg, info)
        if "LGPL-3.0" in lic:
            lgpl_text = lgpl_text or next((f for f in files if "COPYING" in f.upper()), None)
        lines.append("%s %s: %s. %s%s" % (pkg, version_of(info), lic, url,
                                         ("  (licence: " + ", ".join(files) + ")") if files else ""))
    lines += [
        "",
        "pynput and pystray are under the GNU Lesser General Public License v3 (text: %s, together"
        % (lgpl_text or "https://www.gnu.org/licenses/lgpl-3.0.txt"),
        "with the GPL v3 text it builds on). They ship unmodified as Python modules inside the app;",
        "their source is at the links above and on https://pypi.org.",
        "",
        "Python %d.%d: Python Software Foundation License (file: python-LICENSE.txt)." % sys.version_info[:2],
        "PyInstaller bootloader: GPL v2 with a special exception that allows bundling (https://pyinstaller.org).",
        "Speech models (downloaded on first use from Hugging Face): OpenAI Whisper weights, MIT licence,",
        "converted by Systran and mobiuslabs for faster-whisper.",
        "",
    ]
    py_lic = os.path.join(os.path.dirname(sys.base_prefix if hasattr(sys, "base_prefix") else sys.prefix),
                          os.path.basename(sys.base_prefix), "LICENSE.txt")
    for cand in (py_lic, os.path.join(sys.base_prefix, "LICENSE.txt")):
        if os.path.exists(cand):
            shutil.copyfile(cand, os.path.join(OUT, "python-LICENSE.txt"))
            break
    with open(os.path.join(OUT, "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("licenses/: %d files" % len(os.listdir(OUT)))


if __name__ == "__main__":
    main()

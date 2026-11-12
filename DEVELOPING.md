# Developing Lookhere

## Run from source
Windows (Python 3.12, 64-bit):
```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```
Install ffmpeg (`winget install Gyan.FFmpeg`, or any build on PATH), then run `start.cmd` (tray app, no console) or
`venv\Scripts\python.exe screen_explain.py`.

Mac (macOS 15+, Apple Silicon):
```
python3 -m venv venv && venv/bin/pip install -r requirements.txt
brew install ffmpeg
venv/bin/python screen_explain.py
```
Whatever launches it (Terminal, an editor) needs Screen Recording and Microphone permission, plus
Accessibility for clicks to be logged in the recording.

A source copy keeps recordings, `config.json` and logs next to the code, never self-updates, and
on a PC with an NVIDIA GPU and the `nvidia-*-cu12` CUDA wheels installed (not in
`requirements.txt`) also races a larger speech model
(large-v3-turbo) against the CPU one. Installed copies use `small.en` on the CPU only.

`python screen_explain.py --selftest` records 5 seconds, builds it and checks every output.

## Files
- `screen_explain.py` the app: hotkeys, recorder, tray, events; `process.py <recording_dir>` builds
  a recording (also run as `--worker` by the packaged app); `mic.py` the Mac mic process
- `hud.py` status pill; `draw.py` drawing layer; `history.py` History and Trim; `settings.py`
  Settings window; `traymenu.py` Windows tray menu; `macmenu.py`, `mac.py` the Mac side
- `updater.py` auto-update; `paths.py` where files live; `logo.py` icon; `ding.wav` (`make_ding.py`)
- `build.py`, `installer.iss` Windows installer; `build_mac.py` Mac app and dmg;
  `make_licenses.py` third-party notices

## How it fits together
- Every hotkey, tray click and worker line goes through one queue handled on the Tk thread
  (`App._pump`). The pill and the drawing layer run their own window threads.
- Recording: ffmpeg records every monitor (`ddagrab` on Windows, ScreenCaptureKit on a Mac) while
  Python records the mic, clicks, cursor path and key frames. With no usable mic the recording
  carries on without sound.
- Building: a worker process (started when recording starts, so the speech model loads while you
  talk) cuts pauses and trims, renders `preview.mp4` following the cursor's monitor, transcribes,
  picks frames and writes `recording.md`. The video is rendered to `preview.new.mp4` and swapped
  in only on success. A build that says nothing for 15 minutes (longer for long recordings) is
  stopped.
- Edits (trims) live in `edits.json`; the raw recording is never changed.

## Building and releasing
```
venv\Scripts\python.exe build.py --bump --test --publish --notes "What changed"
```
- `--bump` raises the version; `--test` installs the new installer into a temp folder, runs the
  self-test from the installed copy and uninstalls it; `--publish` creates GitHub release
  `v<version>` (repo in `release.json`) with the installer and its SHA-256, or adds the files to
  that release if it already exists.
- Mac: `venv/bin/python build.py --mac --test --publish` on a Mac, for the same version, adds the
  `.dmg` to the same release. A release must hold both platforms' files: the updater ignores a
  release that lacks its own platform's installer.
- Installed copies check the latest release at startup and every 6 hours, offer "Update to x.y.z",
  download it, check its SHA-256, install (keeping recordings and settings) and restart. The
  SHA-256 guards against a corrupt download; trust in the release itself rests on the GitHub
  account, so keep two-factor sign-in on it.
- The Windows installer isn't code-signed, so SmartScreen shows "Windows protected your PC" on
  first run (More info > Run anyway).
- `make_licenses.py` (run by the build) writes `licenses\` with every bundled component's licence
  and `THIRD-PARTY-NOTICES.txt`. FFmpeg is GPL v3: the notice names the exact build, links its
  source and carries the written source offer.

## Platform map
| Piece | Windows | Mac |
|---|---|---|
| Screen video | ffmpeg `ddagrab` + NVENC / AMF / QSV / Media Foundation (`ScreenVideo`) | ScreenCaptureKit (`MacScreenVideo`, `mac.ScreenRecording`) |
| Pill and drawing | layered windows hidden from capture (`hud.py`, `draw.py`) | `NSPanel`s via `mac.Overlay` |
| Global hotkeys | `RegisterHotKey` (`SystemHotkey`) | Carbon `RegisterEventHotKey` (`mac.Hotkey`) |
| Clipboard file | `CF_HDROP` | `NSPasteboard` file URL |
| Start at login | HKCU Run value | LaunchAgent |
| Package | PyInstaller + Inno Setup | PyInstaller `.app` in a `.dmg` |

Mac notes: the mic runs in its own process because stopping PortAudio in-process can deadlock in
Core Audio; `.mp4` doesn't survive a hard stop the way `.mkv` does; the pencil cursor relies on a
private window-server property since macOS only lets the front app change the cursor.
`MAC-TO-WINDOWS.md` is the hand-over note from the port.

## Known limits
- English transcription only (`small.en`).
- At 125-200% Windows display scaling the menu, Settings and History windows keep their 100% size
  (small but intact); they don't scale up yet.
- No size cap or maximum length for a recording.
- Without a usable GPU encoder or desktop duplication (some VMs, Windows N, a monitor on a second
  graphics adapter) the preview quietly falls back to a 10 fps slideshow of frames.
- "Start with Windows" doesn't see Task Manager's own Startup disable switch.
- A stroke drawn right across a 4K screen is heavy to fade (it pops rather than fades).
- If the microphone is unplugged mid-recording the voice just ends; if the screen recorder dies
  mid-recording the pill doesn't say so.
- Windows 10 before version 2004 can't hide the pill from recordings.
- Mac: with Screen Recording or Microphone permission off, the app records a slideshow or silence
  without saying why; INSTALL.txt covers granting them.

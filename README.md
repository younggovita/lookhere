# Lookhere

Point at your screen, say what's wrong, paste it into any AI chat. (Lives in `tools\screen-explain`.)

Press **Ctrl+Alt+H**, talk through a problem while you click around, press **Ctrl+Alt+H** again.
About 2 seconds later the video is on your clipboard and you're back in the window you started from:
press **Ctrl+V** in any chat (Claude, ChatGPT...). In Claude, the screen-recording skill also reads the
frames and transcript saved next to the video. A soft ding plays when recording goes live.

## While recording
| Key | Does |
|---|---|
| **Ctrl+Alt+H** | start / finish |
| **Ctrl+Alt+P** | pause / resume (paused stretches are cut out of the result) |
| **Ctrl+Alt+M** | draw on screen: drag to make lime marks that show in the recording and fade after ~3 s |
| pencil button on the pill | same as Ctrl+Alt+M: click to draw, click again to stop |
| red stop button on the pill | same as Ctrl+Alt+H: finish the recording |
| **Esc** | cancel and delete |

All three shortcuts can be changed from tray > **Shortcuts** (press the new keys; ones another app
owns are refused).

A floating pill at the top of the screen you're on shows what's happening: Arming (the screen
video warms up for ~2s; your voice and clicks are already recording), Recording (timer and live
mic meter), Paused, Drawing, Building (progress), then Copied. It is click-through and never
appears in recordings. Colours: a black pill (dark) or white pill (light), acid lime accents in
both (on black chips in light mode), red only while recording. Tray > **Theme**: Follow Windows
light/dark mode (default), Always dark, Always light. `theme-preview.png` shows every state.

## Tray menu
- **History...**: every recording with a thumbnail, length and first words. Play, Copy, Trim,
  Folder, Delete (to the Recycle Bin). **Trim** shows the recording as a filmstrip with lime start
  and end handles; saving rebuilds the recording (video, transcript, frames) and copies it.
- **Drawing style**: soft shadow (default), thin dark edge or plain lime (strokes are smoothed and anti-aliased)
- **Copy last recording again**
- **Microphone**: Windows default or a specific mic; **Test microphone** shows a live meter.
- **Start with Windows** (a per-user Run entry; the installer's checkbox sets the same thing)
- **Check for updates** / **Update to x.y.z** (installed copies only)

## What it records
- Full video of every monitor with the real cursor (ffmpeg desktop duplication on the GPU,
  `screen0.mkv`, `screen1.mkv`). `preview.mp4` follows whichever monitor the cursor is on and has
  your voice.
- For the AI's reading: the monitor your cursor is on, checked twice a second. Frames are kept on
  every click, just after a click, when you point (cursor stops), after drawing, when the screen
  changes, and at least every 5 seconds.
- The chosen mic (whichever input channel is loudest), mouse clicks and the title of the window
  in front. **Never keystrokes.** Nothing is recorded while paused.
- Passwords or private info on screen will be in the frames. Everything stays on this PC
  until you paste it somewhere.

## What the AI gets
`recordings\<date_time>\recording.md`: transcript plus a timeline of up to 36 frames with clicks
ringed in red. Transcription runs locally (Whisper large-v3-turbo on the GPU racing small.en on the
CPU, first to finish wins). Claude reads it with the `screen-recording` skill. Edits (trims) live in
`edits.json`; the raw recording is never changed, so a trim can be undone.

## Settings (`config.json`)
Written by the tray menu; you can also edit it: `hotkey`, `pause_hotkey`, `draw_hotkey`, `mic`,
`theme`, `keep_days` (default 30: older recordings are deleted at startup), `check_updates`,
`update_repo`, `video_encoder`, `periodic_every`, `change_threshold`.

## Files
- `screen_explain.py` tray app, hotkeys, capture; `process.py <recording_dir>` builds a recording
- `hud.py` status pill; `draw.py` drawing layer; `history.py` History and Trim windows
- `logo.py` logo; `updater.py` auto-update; `paths.py` where files live; `ding.wav` (`make_ding.py`)
- `screen-explain.log` what happened; `process-last.log` the last build's errors

## Building, releasing, updating
```
venv\Scripts\python.exe build.py --bump --test --publish --notes "What changed"
```
- `--bump` raises the version (0.2.0 -> 0.2.1); `--test` installs the new installer into a temp
  folder, runs the self-test (records 5 s, builds it, checks every file) and uninstalls it;
  `--publish` creates GitHub release v<version> (repo in `release.json`) with the installer and
  its SHA-256.
- **Auto-update is switched off** (`check_updates: false`): the repo is private, so installed
  copies can't download releases. To turn it on, publish installers somewhere public (for example a
  releases-only repo) and point `update_repo` at it. When on, **installed copies update themselves**: they check the latest release at startup and every
  6 hours, offer "Update to x.y.z" in the tray, download it, check the SHA-256, install silently
  (keeping recordings, settings and the Start with Windows choice) and restart.
- **Installed vs this folder**: the installer ships CPU speech (any PC; the ~460 MB model
  downloads on first launch). This folder's copy also has GPU speech and never self-updates.
  Video encoding uses NVIDIA, AMD or Intel, or Windows' own on anything else.
- **Before sharing widely**: unsigned apps get a "Windows protected your PC" warning (a paid
  code-signing certificate removes it); the bundled ffmpeg is GPL, so public distribution
  needs its licence and source offer.

## Porting to Mac (later)
The pure-Python parts carry over: `process.py`, `updater.py` (needs a .dmg/.pkg asset), the
frame picking, transcription and markdown. Windows-specific pieces and their Mac equivalents:

| Windows piece | Where | Mac equivalent |
|---|---|---|
| ffmpeg `ddagrab` + NVENC | `ScreenVideo` | ffmpeg `avfoundation` or ScreenCaptureKit, `h264_videotoolbox` |
| layered window pill / drawing (`UpdateLayeredWindow`, hidden from capture) | `hud.py`, `draw.py` | borderless `NSWindow` via PyObjC, `sharingType = .none` |
| `RegisterHotKey` | `SystemHotkey` | Carbon `RegisterEventHotKey` or `NSEvent` global monitor (needs Accessibility permission) |
| clipboard file (`CF_HDROP`) | `set_clipboard` | `NSPasteboard` file URL |
| Run key, window focus, theme from registry | `screen_explain.py` | LaunchAgent / SMAppService, `NSRunningApplication`, `AppleInterfaceStyle` |
| Inno Setup installer | `installer.iss` | `.app` via PyInstaller/py2app, signed and notarised (Apple Developer ID) |

# Lookhere

Point at your screen, say what's wrong, paste it into any AI chat.

Press **Ctrl+Alt+H**, talk through a problem while you click around, press **Ctrl+Alt+H** again.
A couple of seconds later the video is on your clipboard and you're back in the window you started
from: press **Ctrl+V** in any chat (Claude, ChatGPT...). A soft ding plays when recording goes live.

Windows 10/11 (64-bit) and macOS 15+ (Apple Silicon).

## Download
Get the latest installer from the [releases page](https://github.com/younggovita/lookhere/releases/latest):
`Lookhere-Setup-<version>.exe` for Windows, `Lookhere-<version>-mac.dmg` for a Mac.
[INSTALL.txt](INSTALL.txt) is the step-by-step guide, including the "Windows protected your PC"
and Mac "Open Anyway" prompts and how to uninstall.

## While recording
| Key (Windows) | Does |
|---|---|
| **Ctrl+Alt+H** | start / finish |
| **Ctrl+Alt+P** | pause / resume (paused stretches are cut out of the result) |
| **Ctrl+Alt+M** | draw on screen: drag to make lime marks that show in the recording and fade after ~3 s |
| pencil button on the pill | same as Ctrl+Alt+M: click to draw, click again to stop |
| pause button on the pill | same as Ctrl+Alt+P: pause, and resume from the lime play arrow |
| red stop button on the pill | same as Ctrl+Alt+H: finish the recording |
| **Esc** | cancel and delete the recording, from any app, with no confirmation |

On a Mac the shortcuts use Ctrl+Option instead of Ctrl+Alt, and you paste with Cmd+V.

Change them in Settings > Shortcuts: click **Change**, then press the new keys. A shortcut needs
Ctrl, Alt or Win (Shift alone isn't enough); keys another app already owns, and standard ones like
Ctrl+C, are refused.

A floating pill at the top of the screen you're on shows what's happening: Starting (the screen
video warms up for a second or two; your voice and clicks are already recording), Recording (timer
and live mic meter), Paused, Drawing, Building (progress), then "Recording is on your clipboard".
It never appears in recordings (Windows 10 version 2004 or later). Clicks pass through
it; while recording it has three buttons (pencil, pause and stop), and on Windows only those take
clicks (each pops out a little under the cursor): the rest of the pill fades under the cursor and lets you click what's behind it. Hold
**Alt** and drag the pill to park it somewhere else; it stays there (Windows). A black pill in dark mode or a white pill in light mode, lime accents,
red only while recording.

No microphone? Lookhere still records the screen and says the recording has no voice.

## Menu
Right-click the tray icon (Windows) or click the menu bar icon (Mac). Left-click on Windows starts
or finishes a recording.
- **History**: every recording with a thumbnail, length and first words. Play, Copy, Trim, Folder,
  Delete (to the Recycle Bin / Bin). **Trim** shows the recording as a filmstrip with start and end
  handles; saving rebuilds the recording and copies it. The raw recording is never changed, so a
  trim can be undone.
- **Copy last recording again**
- **Settings** (its own window):
  - **Microphone**: the system default or a specific mic; **Test** shows a live meter.
  - **Theme**: Follow Windows, Dark, Light.
  - **Shortcuts**: record, pause, draw.
  - **Start with Windows** / Open at login
  - **Recordings folder**: Open
- **Check for updates** / **Update to x.y.z**

## Privacy
- Recordings, transcripts and logs stay on your computer. Nothing is uploaded; a recording leaves
  only when you paste it somewhere.
- Recorded: every monitor (video), the chosen microphone, mouse clicks, the cursor path and the
  title of the window in front. **Never keystrokes.**
- Pausing: paused stretches are cut from the video you paste, and no voice, clicks or frames are
  kept for them. The raw screen video in the recording's folder still contains them until that
  recording is deleted, so don't rely on pause to hide something on screen.
- Anything on screen while recording is in the video, passwords included. Close private things
  first.
- Speech is transcribed on your computer (Whisper, English only for now).
- Lookhere contacts the internet for two things only:
  1. **Update check** (installed copies): one request to GitHub's releases API at startup and
     every 6 hours. The installer is downloaded only when you click Update. Turn it off with
     `"check_updates": false` in `config.json`.
  2. **Speech model**: downloaded once from Hugging Face on first run (about 460 MB). After that
     it loads from disk with no network call.
- No telemetry, no accounts, no analytics.

## Where things are
| | Windows (installed) | Mac (installed) |
|---|---|---|
| Recordings, `config.json`, logs | `%LOCALAPPDATA%\Lookhere` | `~/Library/Application Support/Lookhere` |
| Speech model | `%USERPROFILE%\.cache\huggingface` | `~/.cache/huggingface` |

- Each recording is a folder: the raw screen video per monitor (roughly 45 MB a minute per
  monitor), the voice track, frames, `preview.mp4` (what gets pasted), `recording.md`, a transcript.
- **Recordings older than 30 days are deleted permanently at startup** (`keep_days` in
  `config.json`; raise it to keep them longer). There is no size cap and no maximum length, so a
  forgotten recording keeps growing until you stop it.
- `screen-explain.log` says what happened (folder names, mic names, errors; never transcripts or
  window titles). `process-last.log` holds the last build's errors.
- Uninstalling removes the app, not your data: delete the folders above to remove recordings,
  settings and the speech model too.

## Settings file (`config.json`)
Written by the Settings window; you can also edit it (quit Lookhere first): `hotkey`,
`pause_hotkey`, `draw_hotkey`, `mic`, `theme`, `keep_days` (default 30), `check_updates`,
`update_repo`, `video_encoder`, `periodic_every`, `change_threshold`, `recordings_dir`. Only the
values you've changed are stored; a damaged file is ignored and the defaults are used.

## What an AI gets
The pasted video is all most chats need. Next to it, `recording.md` holds the transcript plus a
timeline of up to 36 frames with clicks ringed in red, for tools that read files.

## If something goes wrong
- **"Couldn't build the recording"**: details are in `screen-explain.log` and `process-last.log`
  (see "Where things are").
- **Transcript empty**: the speech model didn't download (offline or blocked on first run).
  Recordings still work; restart Lookhere while online to fetch it.
- **No voice**: check the mic in Settings > Microphone > Test, and on Windows that
  Settings > Privacy & security > Microphone allows desktop apps. On a Mac, switch Lookhere on under
  System Settings > Privacy & Security > Microphone and Screen & System Audio Recording.
- **Shortcut does nothing**: another app owns it; pick another in Settings > Shortcuts.
- Anything else: [open an issue](https://github.com/younggovita/lookhere/issues).

## Running from source, building, releasing
See [DEVELOPING.md](DEVELOPING.md).

## Licence
Lookhere's own code is MIT ([LICENSE](LICENSE)). The installers bundle third-party software under
its own licences, including FFmpeg (GPL v3): every notice and licence text ships with the app
(Start menu > Lookhere licences on Windows) and is generated by `make_licenses.py`.

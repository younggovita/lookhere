# Mac port: what Windows should pick up

Written 2026-10-30 from the `mac-port` work, before it was merged. Kept as the hand-over record:
the port has since been merged and the Windows build and self-test pass on it (version 0.3.0
onwards). Current developer notes are in `DEVELOPING.md`.

## Already on Windows, through shared code (needs one check on the PC)
- **Timer slot:** the pill's buttons can't shift as the timer ticks (`hud.py`).
- **Short pill wording:** Starting, Recording, Drawing, Paused, with the title centred (`hud.py`,
  `screen_explain.py`). Shortcut hints now live only in the menu and Settings.
- **`set_clipboard`:** local variable renamed, behaviour unchanged.
- **Windows code edited during the port:** `hud.py`, `draw.py`, `history.py` and `settings.py` now
  branch on Mac, and fonts go through `history.tkfont`. Run `build.py --test` on the PC before
  merging `mac-port`.

## Worth bringing over
1. **Stop can't hang forever.** On the Mac a stuck mic stop left "Building" spinning with no error.
   Windows has no sign of that deadlock, but `App.stop`'s wind-down has no timeout either. Add a
   watchdog that shows an error if the recorder hasn't stopped after about 20 s. Small change.
2. **Theme label "System".** Rename "Follow Windows" to "System" on Windows too, and "Windows
   default" to "System default". One line each (`THEME_AUTO`, `MIC_DEFAULT`).
3. **Sharper ink.** The Mac moved to vector strokes. Windows still stamps hard-edged circles into a
   3x mask, the same method that looked jagged on the Mac. It may look fine on a lower-density
   screen, so check first. If not, raise `Ink.SS` or draw with an anti-aliased library.
4. **Lighter smoothing.** The Mac averages 3 points, Windows 5. Windows gets denser mouse points, so
   it may be fine; try `k = 1` in `draw.smooth` and judge by feel.
5. **ffmpeg 8+.** `-filter_complex_script` is on its way out of ffmpeg (the bundled Windows build still accepts it). The installer's bundled build
   is fine until it's upgraded; then switch `process.py` to `-/filter_complex`, or the preview
   silently falls back to a slideshow on recordings with a pause.
6. **Audio start alignment.** The Mac pads audio.wav for the mic's start-up time. The gap on Windows
   should be tiny, but the same line would remove it.

## Mac only, leave Windows alone
- `mic.py` (the PortAudio stop deadlock), ScreenCaptureKit, `macmenu.py`, Carbon hotkeys, template
  menu bar icon, the background cursor property, Esc as a hotkey, clicks through the pill's window.

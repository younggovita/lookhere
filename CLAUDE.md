# Lookhere: rules for Claude

## Releases: Windows and Mac stay in sync
Every release ships for both platforms. A release is not finished until it has both
`Lookhere-Setup-<version>.exe` and `Lookhere-<version>-mac.dmg` (Morgan, 12 November 2026).

- `--publish` makes a **draft**; the second platform's `--publish` makes it live once it holds both
  the .exe and the .dmg. Releases are immutable once published (repo setting since 27 Nov 2026), so
  the notes must be final before the second upload: write them for both platforms up front.
- **After uploading the Windows build (on the PC):** in the same session, ask the Mac to do the same
  version (T3 Code can reach "Morgan's MacBook Pro"), or tell Morgan to say "release Mac <version>".
- **On the Mac:** pull main, then `venv/bin/python build.py --mac --test --publish` (same version, no
  bump; it signs, notarises, self-tests and adds the dmg to the draft, which then goes live).
- Publish from a pushed commit whose `VERSION` matches the release, so the tag points at what was built.

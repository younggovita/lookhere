# Lookhere: rules for Claude

## Releases: Windows and Mac stay in sync
Every release ships for both platforms. A release is not finished until it has both
`Lookhere-Setup-<version>.exe` and `Lookhere-<version>-mac.dmg` (Morgan, 12 November 2026).

- **After publishing a Windows release (on the PC):** in the same session, ask the Mac to do the same
  version: message the Mac Claude session, or tell Morgan to say "release Mac <version>" on the Mac.
  Leave the notes line "Mac: the <version> build follows shortly; until then Mac copies stay on <old>."
- **On the Mac:** pull main, then `venv/bin/python build.py --mac --test --publish` (same version, no
  bump; it signs, notarises, self-tests and adds the dmg to the existing release). Then change the
  notes' Mac line to "Mac: download Lookhere-<version>-mac.dmg below. Installed copies offer the
  update by themselves."
- Publish from a pushed commit whose `VERSION` matches the release, so the tag points at what was built.

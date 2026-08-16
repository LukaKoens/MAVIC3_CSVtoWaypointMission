# DJI Waypoint Mission Manager

A small Tkinter GUI for tracking and annotating DJI Fly waypoint missions,
without renaming or creating any file DJI Fly itself needs to own.

## How it works: local copy first, sync when connected

MTP (the "browse phone as a folder" mount Linux file managers use) is not a
real filesystem — writes to it can be unreliable. So this app never edits
the phone directly. Instead:

1. **Pull from Phone** copies the waypoint folder from the phone into a
   local folder on your computer (via `adb`, not MTP).
2. You browse, tag, and (optionally) swap file contents entirely against
   that **local copy** — fast, reliable, works offline.
3. Any local edit is flagged **"needs push"**.
4. **Push to Phone** (per-file, or "push all changed") sends just those
   flagged files back to the phone, overwriting their content in place.
   Filenames on the phone are never touched.

Re-pulling later **merges** into the local copy rather than wiping it, so
your notes (see below) survive.

## Requirements

- Python 3.8+
- Tkinter (`sudo apt install python3-tk` on Debian/Ubuntu, if not already present)
- Pillow, optional, for thumbnail previews: `pip install Pillow`
- **adb** (Android Debug Bridge): `sudo apt install android-tools-adb` (or
  `google-android-platform-tools-installer` depending on distro)
- USB debugging enabled on the phone (Settings > About phone > tap Build
  number 7 times > Developer options > USB debugging), and the phone
  authorized for this computer (accept the "Allow USB debugging?" prompt
  when you first plug in)

## First-time setup

```bash
python3 dji_waypoint_manager.py
```

Open **File > Settings...** and set:

- **Local working copy folder** — any empty local folder, e.g.
  `~/dji_waypoints_local`. This is what the app actually browses.
- **Device waypoint path** — the path as `adb` sees it, normally:
  ```
  /storage/emulated/0/Android/data/dji.go.v5/files/waypoint
  ```
  Confirm it's right before relying on it:
  ```bash
  adb shell ls "/storage/emulated/0/Android/data/dji.go.v5/files/waypoint"
  ```
  You should see the mission UUID folders and `map_preview` listed. If you
  get "Permission denied", see the caveat below.
- **adb device serial** — only needed if more than one device/emulator is
  attached at once. Check with `adb devices`.

Then click **Pull from Phone** in the toolbar to populate your local copy
for the first time.

## Confirmed on-disk layout

```
Android/data/dji.go.v5/files/waypoint/
    1B3B0A08-838A-4A41-8D1D-277DEDCB714E/1B3B0A08-838A-4A41-8D1D-277DEDCB714E.kmz   <- mission
    map_preview/1B3B0A08-838A-4A41-8D1D-277DEDCB714E/1B3B0A08-838A-4A41-8D1D-277DEDCB714E.jpg   <- thumbnail
```

Mission files and thumbnails live in two separate, mirrored trees, tied
together by the shared UUID folder name. Your local copy mirrors this same
structure (that's exactly what "Pull from Phone" produces), so the scanner
logic is unchanged from before — it just now always reads from local disk.

## The sidecar notes file

Created as `mission_notes.json` inside each mission's own **local** UUID
folder. It's never pushed to the phone and never touched by DJI Fly:

```json
{
  "flight_number": "3",
  "label": "Flight 3 - ridge survey",
  "notes": "Re-flew after wind aborted attempt 1.",
  "mission_file_dirty": false,
  "thumbnail_dirty": false
}
```

The `*_dirty` flags track whether a local edit is waiting to be pushed —
they're what drives the "needs push" indicator in the mission list and
details panel.

## Suggested workflow

1. Set up missions in DJI Fly as usual (thumbnail included).
2. Connect the phone, open this app, click **Pull from Phone**.
3. For each mission, check the thumbnail/filename to figure out which
   flight number it is, fill in **Flight #** / **Label**, hit **Save**.
   (This is purely local — no need to be connected for this step.)
4. If you need to swap a mission's or thumbnail's content for a different
   one, select it and use **Replace...** — this edits your local copy and
   marks it "needs push".
5. When ready (and connected), use **Push** next to that file, or
   **Push All Changed to Phone** to send everything flagged at once.

## If adb can't reach `Android/data/...`

Starting with Android 11, apps (including the adb shell in some
configurations) can be restricted from browsing other apps' private
`Android/data` folders — this varies by manufacturer and OS version, and
Samsung devices in particular sometimes add extra restrictions. If `adb
shell ls` on the path above returns "Permission denied":

- Confirm USB debugging is on and the device shows as `device` (not
  `unauthorized`) in `adb devices`.
- Try without a leading path change, directly:
  `adb pull "/storage/emulated/0/Android/data/dji.go.v5/files/waypoint" /tmp/test_pull`
- Some devices need "USB debugging (Security settings)" enabled
  separately, if present.
- As a last resort, this may require root, or falling back to browsing via
  MTP for read-only inspection (writes over MTP aren't recommended, as
  covered above).

## Known limitations of this pass

- If a mission's UUID folder contains more than one non-sidecar file, only
  the one named exactly `<UUID>.<ext>` (or the first file found) is treated
  as "the" mission file; extras are tracked internally but not shown in the
  details panel.
- Pull always pulls the *entire* device waypoint tree; there's no
  incremental/only-changed-missions pull yet. For a large mission library
  this may be slow on higher-latency USB connections.
- No bulk-import / bulk-tagging yet — flight numbers are entered one
  mission at a time.
- No validation that a "replacement" file is actually a valid DJI mission
  format — it just copies bytes over, locally and on push. DJI Fly's own
  validation on next launch is the real check.
- Local pull never deletes local files that no longer exist on the device
  (e.g. if you delete a mission in DJI Fly). You'd need to remove the
  corresponding local folder by hand.

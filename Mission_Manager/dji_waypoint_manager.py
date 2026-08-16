#!/usr/bin/env python3
"""
DJI Waypoint Mission Manager
=============================

A lightweight Tkinter GUI for tracking, labelling, and swapping the contents
of DJI Fly waypoint mission files, without ever renaming or creating files
that DJI Fly itself must own.

WORKFLOW: LOCAL COPY FIRST, SYNC TO PHONE WHEN CONNECTED
-----------------------------------------------------------
All browsing and editing in this app happens against a **local folder** on
your computer, not directly on the phone. That's deliberate: the phone's
`Android/data/...` folder is normally only reachable over MTP, and MTP is
not a real filesystem - writes to it are unreliable and can quietly fail or
corrupt data. So the flow is:

    1. Connect the phone with USB debugging enabled (adb).
    2. "Pull from Phone" - copies the waypoint folder from the phone down
       into your local working copy (merges in; never deletes local-only
       files, e.g. your notes).
    3. Browse/tag/edit entirely against the local copy - fast, reliable,
       works offline.
    4. When you actually want to swap a mission's or thumbnail's content,
       that edit is applied locally and flagged as "needs push".
    5. "Push to Phone" (per-file or "push all changed") sends just the
       flagged files back to the phone via adb, overwriting their content
       in place. Filenames on the phone are never changed.

adb is used instead of MTP/gvfs for the actual transfer because it's a
proper file-transfer protocol (not an emulated one) and is far more
reliable for writing files back.

CONFIRMED ON-DISK LAYOUT (DJI Fly / Android, files/waypoint/)
-----------------------------------------------------------------
    waypoint/
        <UUID>/<UUID>.kmz                       <- the mission itself
        map_preview/<UUID>/<UUID>.jpg           <- that mission's thumbnail

Mission files and their thumbnails live in two separate, mirrored trees
under the same waypoint root, joined by the shared UUID folder name. Your
local working copy should mirror this same structure (that's exactly what
"Pull from Phone" produces).

The scanner:
  * Lists every subfolder of the local waypoint root except `map_preview` -
    each one is a mission, named by its UUID.
  * Looks inside that UUID folder for a file whose name matches the UUID
    (any extension - `.kmz` is what's been seen, but this isn't hardcoded
    in case DJI Fly uses something else for a mission, e.g. `.wpml`).
  * Looks for the thumbnail at `map_preview/<UUID>/<UUID>.<image-ext>`.

No files DJI Fly owns are ever renamed or deleted by this app, locally or
on the phone. The only file this app creates is a small JSON sidecar
(`mission_notes.json`) placed inside each mission's own local UUID folder -
that sidecar is never pushed to the phone, it's purely local tracking.
"""

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ConvertCSVtoKMZ import build_kmz_from_csv

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

APP_NAME = "dji_waypoint_manager"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
IGNORE_PATH = CONFIG_DIR / "ignore.json"

DEFAULT_CONFIG = {
    # Local folder that mirrors the phone's waypoint/ folder. This is what
    # the app actually browses and edits.
    "local_root": "",
    # Path to the waypoint folder ON THE PHONE, as adb sees it, e.g.
    # "/storage/emulated/0/Android/data/dji.go.v5/files/waypoint"
    "device_root": "",
    # Optional - only needed if more than one device/emulator is attached.
    "adb_serial": "",
    # Name of the sibling folder that mirrors the mission UUID folders but
    # holds thumbnails instead of mission files.
    "map_preview_dirname": "map_preview",
    # Extensions treated as thumbnail images (case-insensitive, with dot)
    "image_extensions": [".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic"],
    # Filename this app creates inside each mission's own LOCAL UUID folder
    # to store flight number / label / notes / push-state. Never sent to
    # the phone, never touched by DJI Fly.
    "sidecar_name": "mission_notes.json",
}

MISSION_META_DEFAULTS = {
    "flight_number": "",
    "label": "",
    "notes": "",
    "mission_file_dirty": False,   # True = local edit not yet pushed to phone
    "thumbnail_dirty": False,
}


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            # Migrate from the older single "base_path" key, if present.
            if not merged.get("local_root") and cfg.get("base_path"):
                merged["local_root"] = cfg["base_path"]
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# --------------------------------------------------------------------------
# adb helpers
# --------------------------------------------------------------------------

def get_adb_path() -> Optional[str]:
    return shutil.which("adb")


def list_adb_devices() -> list:
    """Returns [(serial, state), ...] e.g. [("R58N...", "device")]."""
    adb = get_adb_path()
    if not adb:
        return []
    try:
        result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=6)
    except (subprocess.TimeoutExpired, OSError):
        return []
    lines = result.stdout.strip().splitlines()
    devices = []
    for line in lines[1:]:  # skip "List of devices attached"
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def device_status(cfg: dict) -> tuple:
    """Returns (is_ready: bool, message: str) describing adb/device state."""
    if not get_adb_path():
        return False, "adb not found on PATH"

    devices = list_adb_devices()
    if not devices:
        return False, "No device connected"

    serial = (cfg.get("adb_serial") or "").strip()
    if serial:
        for s, state in devices:
            if s == serial:
                return state == "device", f"{s} ({state})"
        return False, f"Configured device {serial} not found"

    if len(devices) == 1:
        s, state = devices[0]
        return state == "device", f"{s} ({state})"

    return False, f"{len(devices)} devices attached - set a serial in Settings"


def _adb_prefix(cfg: dict) -> list:
    cmd = [get_adb_path()]
    serial = (cfg.get("adb_serial") or "").strip()
    if serial:
        cmd += ["-s", serial]
    return cmd


def pull_from_device(cfg: dict) -> tuple:
    """Pulls the whole device waypoint tree and merges it into local_root.
    Never deletes anything already in local_root (so sidecar notes and any
    locally-only files survive). Returns (success, message)."""
    adb = get_adb_path()
    if not adb:
        return False, "adb not found on PATH. Install android-tools/platform-tools."

    device_root = (cfg.get("device_root") or "").strip()
    local_root_str = (cfg.get("local_root") or "").strip()
    if not device_root:
        return False, "Device waypoint path isn't set. Open Settings."
    if not local_root_str:
        return False, "Local folder isn't set. Open Settings."

    local_root = Path(local_root_str).expanduser()
    local_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_target = Path(tmp) / "pulled"
        cmd = _adb_prefix(cfg) + ["pull", device_root, str(tmp_target)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return False, "adb pull timed out (large folder or slow connection)."
        except OSError as exc:
            return False, f"Couldn't run adb: {exc}"

        if result.returncode != 0 or not tmp_target.exists():
            err = (result.stderr or result.stdout or "").strip()
            return False, f"adb pull failed:\n{err or 'unknown error'}"

        copied = 0
        for src in tmp_target.rglob("*"):
            if src.is_file():
                rel = src.relative_to(tmp_target)
                dest = local_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied += 1

    return True, f"Pulled {copied} file(s) from device into the local copy."


def push_file_to_device(local_file: Path, cfg: dict) -> tuple:
    """Pushes ONE local file back to its mirrored path on the device,
    overwriting content there. Returns (success, message)."""
    adb = get_adb_path()
    if not adb:
        return False, "adb not found on PATH."

    local_root_str = (cfg.get("local_root") or "").strip()
    device_root = (cfg.get("device_root") or "").strip().rstrip("/")
    if not local_root_str or not device_root:
        return False, "Local folder and device path must both be set in Settings."

    local_root = Path(local_root_str).expanduser()
    try:
        rel = local_file.relative_to(local_root)
    except ValueError:
        return False, "That file isn't inside the configured local folder."

    device_target = device_root + "/" + str(rel).replace(os.sep, "/")
    cmd = _adb_prefix(cfg) + ["push", str(local_file), device_target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "adb push timed out."
    except OSError as exc:
        return False, f"Couldn't run adb: {exc}"

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        return False, f"adb push failed:\n{err or 'unknown error'}"
    return True, f"Pushed {local_file.name} to device."


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Mission:
    mission_id: str                 # the UUID / folder name
    container: Path                 # local mission folder
    mission_file: Optional[Path]
    thumbnail_file: Optional[Path]
    other_files: list = field(default_factory=list)
    meta_path: Path = None
    meta: dict = field(default_factory=lambda: dict(MISSION_META_DEFAULTS))

    def load_meta(self):
        if self.meta_path and self.meta_path.exists():
            try:
                data = json.loads(self.meta_path.read_text())
                merged = dict(MISSION_META_DEFAULTS)
                merged.update(data)
                self.meta = merged
            except (json.JSONDecodeError, OSError):
                self.meta = dict(MISSION_META_DEFAULTS)
        else:
            self.meta = dict(MISSION_META_DEFAULTS)

    def save_meta(self):
        self.meta_path.write_text(json.dumps(self.meta, indent=2))

    def sort_key(self):
        raw = (self.meta.get("flight_number") or "").strip()
        if raw.isdigit():
            return (0, int(raw), self.mission_id)
        return (1, 0, self.mission_id)

    def needs_push(self) -> bool:
        return bool(self.meta.get("mission_file_dirty") or self.meta.get("thumbnail_dirty"))


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

def is_image(path: Path, cfg: dict) -> bool:
    return path.suffix.lower() in {e.lower() for e in cfg["image_extensions"]}


def _pick_matching_stem(files: list, stem: str) -> Optional[Path]:
    """Prefer a file literally named `<stem>.<anything>`; else first file."""
    for f in files:
        if f.stem == stem:
            return f
    return files[0] if files else None


def scan_missions(local_root: Path, cfg: dict) -> list:
    """
    Scan a local mirror of DJI Fly's `waypoint/` folder, laid out as:

        waypoint/<UUID>/<UUID>.kmz
        waypoint/map_preview/<UUID>/<UUID>.jpg

    Each UUID subfolder (other than `map_preview` itself) is one mission.
    """
    
    if not local_root.exists() or not local_root.is_dir():
        return []

    map_preview_name = cfg["map_preview_dirname"]
    sidecar_name = cfg["sidecar_name"]
    map_preview_root = local_root / map_preview_name

    missions = []
    for d in sorted(local_root.iterdir()):
        print(d)
        if not d.is_dir() or d.name == map_preview_name:
            continue

        mission_id = d.name  # the UUID

        mission_files = [
            f for f in sorted(d.iterdir())
            if f.is_file() and f.name != sidecar_name
        ]
        mission_file = _pick_matching_stem(mission_files, mission_id)
        other_files = [f for f in mission_files if f != mission_file]

        thumbnail = None
        thumb_dir = map_preview_root / mission_id
        if thumb_dir.is_dir():
            thumb_files = [
                f for f in sorted(thumb_dir.iterdir())
                if f.is_file() and is_image(f, cfg)
            ]
            thumbnail = _pick_matching_stem(thumb_files, mission_id)

        m = Mission(
            mission_id=mission_id,
            container=d,
            mission_file=mission_file,
            thumbnail_file=thumbnail,
            other_files=other_files,
            meta_path=d / sidecar_name,
        )
        m.load_meta()
        missions.append(m)

    return missions


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

THUMB_SIZE = (220, 220)
DEVICE_PATH_HINT = "/storage/emulated/0/Android/data/dji.go.v5/files/waypoint"


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg: dict, on_save):
        super().__init__(parent)
        self.title("Settings")
        self.resizable(False, False)
        self.cfg = cfg
        self.on_save = on_save
        self.transient(parent)
        self.grab_set()

        pad = {"padx": 8, "pady": 6}

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text="Local working copy folder:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.local_var = tk.StringVar(value=cfg.get("local_root", ""))
        ttk.Entry(frm, textvariable=self.local_var, width=52).grid(row=1, column=0, columnspan=2, sticky=tk.EW, padx=8)
        ttk.Button(frm, text="Browse...", command=self._browse_local).grid(row=1, column=2, padx=8)
        ttk.Label(
            frm, foreground="#666", wraplength=460, justify=tk.LEFT,
            text="A plain local folder. This is what the app browses and edits. "
                 "Use 'Pull from Phone' to populate it."
        ).grid(row=2, column=0, columnspan=3, sticky=tk.W, padx=8)

        ttk.Separator(frm).grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=10)

        ttk.Label(frm, text="Device waypoint path (as adb sees it):").grid(row=4, column=0, sticky=tk.W, **pad)
        self.device_var = tk.StringVar(value=cfg.get("device_root", ""))
        ttk.Entry(frm, textvariable=self.device_var, width=52).grid(row=5, column=0, columnspan=2, sticky=tk.EW, padx=8)
        ttk.Button(frm, text="Use example", command=self._fill_example).grid(row=5, column=2, padx=8)
        ttk.Label(
            frm, foreground="#666", wraplength=460, justify=tk.LEFT,
            text=f"Typically:\n{DEVICE_PATH_HINT}\n"
                 "Confirm with: adb shell ls \"<path>\""
        ).grid(row=6, column=0, columnspan=3, sticky=tk.W, padx=8)

        ttk.Separator(frm).grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=10)

        ttk.Label(frm, text="adb device serial (optional):").grid(row=8, column=0, sticky=tk.W, **pad)
        self.serial_var = tk.StringVar(value=cfg.get("adb_serial", ""))
        ttk.Entry(frm, textvariable=self.serial_var, width=30).grid(row=9, column=0, sticky=tk.W, padx=8)
        ttk.Label(
            frm, foreground="#666", wraplength=460, justify=tk.LEFT,
            text="Only needed if more than one device/emulator is attached. Leave blank otherwise."
        ).grid(row=10, column=0, columnspan=3, sticky=tk.W, padx=8)

        btns = ttk.Frame(frm)
        btns.grid(row=11, column=0, columnspan=3, sticky=tk.E, pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT, padx=4)

        frm.columnconfigure(0, weight=1)

    def _browse_local(self):
        initial = self.local_var.get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Select (or create) local working copy folder", initialdir=initial)
        if chosen:
            self.local_var.set(chosen)

    def _fill_example(self):
        self.device_var.set(DEVICE_PATH_HINT)

    def _save(self):
        local_val = self.local_var.get().strip()
        if not local_val:
            messagebox.showerror("Missing folder", "Please choose a local working copy folder.")
            return
        try:
            Path(local_val).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Can't create folder", str(exc))
            return

        self.cfg["local_root"] = local_val
        self.cfg["device_root"] = self.device_var.get().strip()
        self.cfg["adb_serial"] = self.serial_var.get().strip()
        save_config(self.cfg)
        self.destroy()
        self.on_save()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DJI Waypoint Mission Manager")
        self.geometry("1040x620")
        self.minsize(820, 480)

        self.cfg = load_config()
        self.missions: list = []
        self.selected: Optional[Mission] = None
        self._thumb_imgtk = None  # keep reference alive

        self._build_menu()
        self._build_layout()
        self._poll_device_status()

        if self.cfg.get("local_root"):
            self.rescan()
        else:
            self.status_var.set("Not set up yet. Use File > Settings... to configure folders.")

    # ---- UI construction -------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Settings...", command=self.open_settings)
        file_menu.add_command(label="Rescan Local Copy", command=self.rescan, accelerator="F5")
        file_menu.add_separator()
        file_menu.add_command(label="Pull from Phone", command=self.pull_from_phone)
        file_menu.add_command(label="Push All Changed to Phone", command=self.push_all_changed)
        file_menu.add_separator()
        file_menu.add_command(label="Open Local Folder in File Manager", command=self.open_local_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        self.config(menu=menubar)
        self.bind("<F5>", lambda e: self.rescan())

    def _build_layout(self):
        top = ttk.Frame(self, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        row1 = ttk.Frame(top)
        row1.pack(side=tk.TOP, fill=tk.X)
        self.path_var = tk.StringVar(value=self.cfg.get("local_root") or "(not set)")
        ttk.Label(row1, text="Local copy:").pack(side=tk.LEFT)
        ttk.Label(row1, textvariable=self.path_var, foreground="#555").pack(side=tk.LEFT, padx=(4, 12))
        ttk.Button(row1, text="Settings...", command=self.open_settings).pack(side=tk.LEFT)
        ttk.Button(row1, text="Rescan", command=self.rescan).pack(side=tk.LEFT, padx=(6, 0))

        row2 = ttk.Frame(top)
        row2.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="Device:").pack(side=tk.LEFT)
        self.device_status_var = tk.StringVar(value="checking...")
        self.device_status_label = ttk.Label(row2, textvariable=self.device_status_var)
        self.device_status_label.pack(side=tk.LEFT, padx=(4, 12))
        ttk.Button(row2, text="Pull from Phone", command=self.pull_from_phone).pack(side=tk.LEFT)
        ttk.Button(row2, text="Push All Changed to Phone", command=self.push_all_changed).pack(side=tk.LEFT, padx=(6, 0))

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # --- left: mission list ---
        left = ttk.Frame(body, padding=(8, 4))
        columns = ("flight", "label", "mission_file", "thumb_file", "sync", "notes")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headings = {
            "flight": "Flight #",
            "label": "Label",
            "mission_file": "Mission file",
            "thumb_file": "Thumbnail",
            "sync": "Sync",
            "notes": "Notes",
        }
        widths = {"flight": 55, "label": 120, "mission_file": 150, "thumb_file": 130, "sync": 90, "notes": 140}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor=tk.W)

        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        body.add(left, weight=3)

        # --- right: details panel ---
        right = ttk.Frame(body, padding=(8, 4))

        self.thumb_label = ttk.Label(right, text="(no thumbnail)", anchor=tk.CENTER,
                                      relief=tk.GROOVE, width=30)
        self.thumb_label.pack(side=tk.TOP, pady=(0, 8), ipady=60)

        files_frame = ttk.LabelFrame(right, text="Files (managed by DJI Fly - names locked)")
        files_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 8))

        self.mission_file_var = tk.StringVar()
        self.thumb_file_var = tk.StringVar()
        self.mission_marker_var = tk.StringVar()
        self.thumb_marker_var = tk.StringVar()

        ttk.Label(files_frame, text="Mission file:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=2)
        ttk.Label(files_frame, textvariable=self.mission_file_var, foreground="#333").grid(
            row=0, column=1, sticky=tk.W, padx=4, pady=2)
        ttk.Label(files_frame, textvariable=self.mission_marker_var, foreground="#b34700").grid(
            row=0, column=2, sticky=tk.W, padx=2)
        ttk.Button(files_frame, text="Build",
                   command=lambda: self.build_file("mission")).grid(row=0, column=3, padx=3)
        # ttk.Button(files_frame, text="Replace...",
        #            command=lambda: self.replace_file("mission")).grid(row=0, column=3, padx=3)
        ttk.Button(files_frame, text="Reveal",
                   command=lambda: self.reveal_file("mission")).grid(row=0, column=5, padx=3)

        ttk.Label(files_frame, text="Thumbnail:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=2)
        ttk.Label(files_frame, textvariable=self.thumb_file_var, foreground="#333").grid(
            row=1, column=1, sticky=tk.W, padx=4, pady=2)
        ttk.Label(files_frame, textvariable=self.thumb_marker_var, foreground="#b34700").grid(
            row=1, column=2, sticky=tk.W, padx=2)
        ttk.Button(files_frame, text="Replace...",
                   command=lambda: self.replace_file("thumbnail")).grid(row=1, column=3, padx=3)
        ttk.Button(files_frame, text="Reveal",
                   command=lambda: self.reveal_file("thumbnail")).grid(row=1, column=5, padx=3)

        meta_frame = ttk.LabelFrame(right, text="Your tracking info (local only - never pushed to phone)")
        meta_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        ttk.Label(meta_frame, text="Flight #:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=4)
        self.flight_entry = ttk.Entry(meta_frame, width=10)
        self.flight_entry.grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)

        ttk.Label(meta_frame, text="Label:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
        self.label_entry = ttk.Entry(meta_frame, width=30)
        self.label_entry.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=4, columnspan=2)

        ttk.Label(meta_frame, text="Notes:").grid(row=2, column=0, sticky=tk.NW, padx=4, pady=4)
        self.notes_text = tk.Text(meta_frame, height=8, width=30, wrap=tk.WORD)
        self.notes_text.grid(row=2, column=1, sticky=tk.NSEW, padx=4, pady=4, columnspan=2)

        meta_frame.columnconfigure(1, weight=1)
        meta_frame.rowconfigure(2, weight=1)

        btn_row = ttk.Frame(right)
        btn_row.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row, text="Save", command=self.save_selected).pack(side=tk.RIGHT)

        body.add(right, weight=2)

        # --- status bar ---
        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2))
        status.pack(side=tk.BOTTOM, fill=tk.X)

        self._set_details_enabled(False)

    def _set_details_enabled(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.flight_entry.configure(state=state)
        self.label_entry.configure(state=state)
        self.notes_text.configure(state=state)

    # ---- settings / status ------------------------------------------------

    def open_settings(self):
        SettingsDialog(self, self.cfg, on_save=self._after_settings_saved)

    def _after_settings_saved(self):
        self.path_var.set(self.cfg.get("local_root") or "(not set)")
        self.rescan()

    def _poll_device_status(self):
        ready, message = device_status(self.cfg)
        self.device_status_var.set(message)
        self.device_status_label.configure(foreground="#1a7a1a" if ready else "#a33")
        self.after(4000, self._poll_device_status)

    def open_local_folder(self):
        local_root = self.cfg.get("local_root")
        if not local_root:
            messagebox.showinfo("No folder set", "Set a local working copy folder first (Settings).")
            return
        try:
            subprocess.Popen(["xdg-open", local_root])
        except FileNotFoundError:
            messagebox.showwarning("Can't open", "xdg-open isn't available on this system.")

    # ---- scan / list -------------------------------------------------

    def rescan(self):
        local_root = self.cfg.get("local_root")
        if not local_root:
            self.status_var.set("No local folder set yet. Use File > Settings...")
            return
        self.missions = scan_missions(Path(local_root), self.cfg)
        self.missions.sort(key=lambda m: m.sort_key())
        self._refresh_tree()
        self.status_var.set(f"Found {len(self.missions)} mission(s) in local copy")

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for m in self.missions:
            notes_preview = (m.meta.get("notes") or "").replace("\n", " ")
            if len(notes_preview) > 40:
                notes_preview = notes_preview[:37] + "..."
            sync_text = "needs push" if m.needs_push() else ""
            self.tree.insert("", tk.END, iid=m.mission_id, values=(
                m.meta.get("flight_number") or "",
                m.meta.get("label") or "",
                m.mission_file.name if m.mission_file else "(none)",
                m.thumbnail_file.name if m.thumbnail_file else "(none)",
                sync_text,
                notes_preview,
            ))

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            self.selected = None
            self._set_details_enabled(False)
            return
        mission_id = sel[0]
        self.selected = next((m for m in self.missions if m.mission_id == mission_id), None)
        if not self.selected:
            return

        self._set_details_enabled(True)
        m = self.selected

        self.mission_file_var.set(m.mission_file.name if m.mission_file else "(none found)")
        self.thumb_file_var.set(m.thumbnail_file.name if m.thumbnail_file else "(none found)")
        self.mission_marker_var.set("needs push" if m.meta.get("mission_file_dirty") else "")
        self.thumb_marker_var.set("needs push" if m.meta.get("thumbnail_dirty") else "")

        self.flight_entry.delete(0, tk.END)
        self.flight_entry.insert(0, m.meta.get("flight_number") or "")

        self.label_entry.delete(0, tk.END)
        self.label_entry.insert(0, m.meta.get("label") or "")

        self.notes_text.delete("1.0", tk.END)
        self.notes_text.insert("1.0", m.meta.get("notes") or "")

        self._update_thumbnail(m)

    def _update_thumbnail(self, m: Mission):
        if not m.thumbnail_file or not m.thumbnail_file.exists():
            self.thumb_label.configure(image="", text="(no thumbnail)")
            self._thumb_imgtk = None
            return

        if not PIL_AVAILABLE:
            self.thumb_label.configure(image="", text=f"(Pillow not installed)\n{m.thumbnail_file.name}")
            self._thumb_imgtk = None
            return

        try:
            img = Image.open(m.thumbnail_file)
            img.thumbnail(THUMB_SIZE)
            self._thumb_imgtk = ImageTk.PhotoImage(img)
            self.thumb_label.configure(image=self._thumb_imgtk, text="")
        except Exception as exc:  # noqa: BLE001 - surface any load failure, not just PIL's own errors
            self.thumb_label.configure(image="", text=f"(couldn't load image)\n{exc}")
            self._thumb_imgtk = None

    # ---- notes -------------------------------------------------

    def save_selected(self):
        if not self.selected:
            return
        m = self.selected
        m.meta["flight_number"] = self.flight_entry.get().strip()
        m.meta["label"] = self.label_entry.get().strip()
        m.meta["notes"] = self.notes_text.get("1.0", tk.END).rstrip("\n")
        try:
            m.save_meta()
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return

        self.missions.sort(key=lambda mm: mm.sort_key())
        self._refresh_tree()
        self.tree.selection_set(m.mission_id)
        self.tree.see(m.mission_id)
        self.status_var.set(f"Saved notes for {m.mission_id}")

    # ---- Waypoint Mission file actions -------------------------------------------
    
    def build_file(self, kind: str):
        if not self.selected:
            return
        m = self.selected
        target = m.mission_file
    
        title = f"Choose replacement content for {target.name}"
        source = filedialog.askopenfilename(title=title)
        if not source:
            return
        source_path = Path(source)

        if not messagebox.askyesno(
            "Confirm replace",
            f"This will overwrite the CONTENTS of your LOCAL COPY of:\n\n  {target}\n\n"
            f"with the contents of:\n\n  {source_path}\n\n"
            "The filename stays exactly as it is, and nothing on the phone changes "
            "until you Push. Continue?",
        ):
            return
        try:        
            build_kmz_from_csv(source_path, target)
        except OSError as exc:
            messagebox.showerror("Replace failed", str(exc))
            return

        m.meta["mission_file_dirty"] = True
        try:
            m.save_meta()
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

        self._refresh_tree()
        self.tree.selection_set(m.mission_id)
        self.on_select()
        self.status_var.set(f"Replaced local contents of {target.name} - not yet pushed to phone")
        

    # ---- local file actions -------------------------------------------------

    def reveal_file(self, kind: str):
        if not self.selected:
            return
        target = self.selected.mission_file if kind == "mission" else self.selected.thumbnail_file
        if target is None:
            messagebox.showinfo("Nothing to reveal", f"This mission has no {kind} file.")
            return
        try:
            subprocess.Popen(["xdg-open", str(target.parent)])
        except FileNotFoundError:
            messagebox.showwarning("Can't open", "xdg-open isn't available on this system.")

    def replace_file(self, kind: str):
        if not self.selected:
            return
        m = self.selected
        target = m.mission_file if kind == "mission" else m.thumbnail_file

        if target is None:
            messagebox.showinfo(
                "No file to replace",
                f"This mission has no {kind} file for me to swap content into.\n"
                "DJI Fly must create that file first - this app can't create it.",
            )
            return

        title = f"Choose replacement content for {target.name}"
        source = filedialog.askopenfilename(title=title)
        if not source:
            return
        source_path = Path(source)

        if not messagebox.askyesno(
            "Confirm replace",
            f"This will overwrite the CONTENTS of your LOCAL COPY of:\n\n  {target}\n\n"
            f"with the contents of:\n\n  {source_path}\n\n"
            "The filename stays exactly as it is, and nothing on the phone changes "
            "until you Push. Continue?",
        ):
            return

        try:
            shutil.copyfile(source_path, target)
        except OSError as exc:
            messagebox.showerror("Replace failed", str(exc))
            return

        if kind == "mission":
            m.meta["mission_file_dirty"] = True
        else:
            m.meta["thumbnail_dirty"] = True
        try:
            m.save_meta()
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

        self._refresh_tree()
        self.tree.selection_set(m.mission_id)
        self.on_select()
        self.status_var.set(f"Replaced local contents of {target.name} - not yet pushed to phone")

    # ---- sync actions -------------------------------------------------

    def pull_from_phone(self):
        ready, message = device_status(self.cfg)
        if not ready:
            messagebox.showwarning("Device not ready", f"Can't reach the phone: {message}")
            return
        if not self.cfg.get("device_root"):
            messagebox.showwarning("Not configured", "Set the device waypoint path in Settings first.")
            return

        self.status_var.set("Pulling from phone...")
        self.update_idletasks()
        success, message = pull_from_device(self.cfg)
        if success:
            self.status_var.set(message)
            self.rescan()
        else:
            self.status_var.set("Pull failed")
            messagebox.showerror("Pull from phone failed", message)

    def push_one(self, kind: str):
        if not self.selected:
            return
        m = self.selected
        target = m.mission_file if kind == "mission" else m.thumbnail_file
        if target is None:
            messagebox.showinfo("Nothing to push", f"This mission has no {kind} file.")
            return

        ready, message = device_status(self.cfg)
        if not ready:
            messagebox.showwarning("Device not ready", f"Can't reach the phone: {message}")
            return

        self.status_var.set(f"Pushing {target.name}...")
        self.update_idletasks()
        success, message = push_file_to_device(target, self.cfg)
        if success:
            if kind == "mission":
                m.meta["mission_file_dirty"] = False
            else:
                m.meta["thumbnail_dirty"] = False
            m.save_meta()
            self._refresh_tree()
            self.tree.selection_set(m.mission_id)
            self.on_select()
            self.status_var.set(message)
        else:
            self.status_var.set("Push failed")
            messagebox.showerror("Push to phone failed", message)

    def push_all_changed(self):
        ready, message = device_status(self.cfg)
        if not ready:
            messagebox.showwarning("Device not ready", f"Can't reach the phone: {message}")
            return

        to_push = [m for m in self.missions if m.needs_push()]
        if not to_push:
            messagebox.showinfo("Nothing to push", "No local changes are waiting to be pushed.")
            return

        if not messagebox.askyesno(
            "Confirm push",
            f"This will overwrite content on the phone for {len(to_push)} mission(s) "
            "that have local changes. Filenames on the phone won't change. Continue?",
        ):
            return

        pushed, failed = 0, []
        for m in to_push:
            if m.meta.get("mission_file_dirty") and m.mission_file:
                ok, msg = push_file_to_device(m.mission_file, self.cfg)
                if ok:
                    m.meta["mission_file_dirty"] = False
                    pushed += 1
                else:
                    failed.append(f"{m.mission_id} (mission file): {msg}")
            if m.meta.get("thumbnail_dirty") and m.thumbnail_file:
                ok, msg = push_file_to_device(m.thumbnail_file, self.cfg)
                if ok:
                    m.meta["thumbnail_dirty"] = False
                    pushed += 1
                else:
                    failed.append(f"{m.mission_id} (thumbnail): {msg}")
            m.save_meta()

        self._refresh_tree()
        summary = f"Pushed {pushed} file(s)."
        if failed:
            summary += f" {len(failed)} failed."
            messagebox.showerror("Some pushes failed", "\n\n".join(failed))
        self.status_var.set(summary)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

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
your computer, not directly on the phone/controller. The flow is:

    1. Connect the phone/RC and open it once in your file manager (MTP).
    2. "Pull from Phone" - copies the waypoint folder down into your local
       working copy (merges in; never deletes local-only files).
    3. Browse/tag/edit entirely against the local copy.
    4. Edits are applied locally and flagged as "needs push".
    5. "Push" (per mission, or "push all changed") sends the flagged files
       back, overwriting their content in place. Filenames on the device are
       never changed.

CONFIRMED ON-DISK LAYOUT (DJI Fly / Android, files/waypoint/)
-----------------------------------------------------------------
    waypoint/
        <UUID>/<UUID>.kmz                       <- the mission itself
        map_preview/<UUID>/<UUID>.jpg           <- that mission's thumbnail

No files DJI Fly owns are ever renamed or deleted by this app. The only file
this app creates is a small JSON sidecar (`mission_notes.json`) inside each
mission's own LOCAL UUID folder - never pushed to the device.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import zipfile
import xml.etree.ElementTree as ET
from tkintermapview import TkinterMapView

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

DEFAULT_CONFIG = {
    # Local folder that mirrors the device's waypoint/ folder.
    "local_root": "",
    # Path to the waypoint folder INSIDE the device's storage, e.g.
    # "Android/data/dji.go.v5/files/waypoint"
    "device_root": "",
    "adb_serial": "",
    "map_preview_dirname": "map_preview",
    "image_extensions": [".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic"],
    "sidecar_name": "mission_notes.json",
}

MISSION_META_DEFAULTS = {
    "flight_number": "",
    "label": "",
    "notes": "",
    "source_csv": "",              # CSV that generated this mission (local only)
    "mission_file_dirty": False,   # True = local edit not yet pushed
    "thumbnail_dirty": False,
}


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            if not merged.get("local_root") and cfg.get("base_path"):
                merged["local_root"] = cfg["base_path"]
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def shorten(text: str, limit: int = 60) -> str:
    """Shorten long paths from the left: '…/Mission_Manager/waypoint'."""
    text = text or ""
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):]


# --------------------------------------------------------------------------
# MTP (gvfs) helpers
# --------------------------------------------------------------------------

def _gvfs_root() -> Optional[Path]:
    uid = os.getuid()
    base = Path(f"/run/user/{uid}/gvfs")
    if base.exists():
        return base
    fallback = Path.home() / ".gvfs"
    return fallback if fallback.exists() else None


def find_mtp_device(cfg: dict) -> Optional[Path]:
    """Finds the mounted MTP device storage folder."""
    root = _gvfs_root()
    if not root:
        return None

    hint = (cfg.get("mtp_host_hint") or "").strip()
    candidates = sorted(root.glob("mtp:host=*"))
    if not candidates:
        return None

    chosen = None
    if hint:
        for c in candidates:
            if hint in c.name:
                chosen = c
                break
        if chosen is None:
            return None
    else:
        if len(candidates) > 1:
            return None
        chosen = candidates[0]

    subdirs = [d for d in chosen.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        return subdirs[0]
    for d in subdirs:
        if "internal" in d.name.lower():
            return d
    return chosen


def clean_device_subpath(raw: str) -> str:
    """Tolerate a pasted gvfs path / mtp:// URI; keep only the part inside
    the storage root (e.g. 'Android/data/dji.go.v5/files/waypoint')."""
    raw = unquote((raw or "").strip())
    for marker in ("Internal shared storage/", "Internal storage/"):
        if marker in raw:
            raw = raw.split(marker)[-1]
            break
    return raw.lstrip("/")


def device_status(cfg: dict) -> tuple:
    root = _gvfs_root()
    if not root:
        return False, "No gvfs session found (is the file manager running?)"

    candidates = sorted(root.glob("mtp:host=*"))
    if not candidates:
        return False, "No MTP device mounted. Open it once in your file manager first."

    device = find_mtp_device(cfg)
    if not device:
        names = ", ".join(c.name for c in candidates)
        return False, f"Multiple MTP devices mounted ({names}) - set mtp_host_hint in Settings"

    if not device.exists():
        return False, f"MTP mount vanished: {device}"

    return True, f"{device.parent.name}"


def pull_from_device(cfg: dict) -> tuple:
    device_dir = find_mtp_device(cfg)
    if not device_dir:
        return False, "MTP device not found or ambiguous. Check Settings."

    device_subpath = clean_device_subpath(cfg.get("device_root"))
    local_root_str = (cfg.get("local_root") or "").strip()
    if not device_subpath or not local_root_str:
        return False, "Device path and local folder must both be set in Settings."

    src_root = device_dir / device_subpath
    if not src_root.exists():
        return False, f"Path not found on device: {src_root}"

    local_root = Path(local_root_str).expanduser()
    local_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    errors = []
    for src in src_root.rglob("*"):
        if src.is_file():
            rel = src.relative_to(src_root)
            dest = local_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                copied += 1
            except OSError as exc:
                errors.append(f"{rel}: {exc}")

    msg = f"Pulled {copied} file(s) from device into the local copy."
    if errors:
        msg += f" ({len(errors)} failed)"
    return True, msg


def push_file_to_device(cfg: dict, local_file: Path) -> tuple:
    device_dir = find_mtp_device(cfg)
    if not device_dir:
        return False, "MTP device not found or ambiguous. Check Settings."

    local_root_str = (cfg.get("local_root") or "").strip()
    device_subpath = clean_device_subpath(cfg.get("device_root"))
    if not local_root_str or not device_subpath:
        return False, "Local folder and device path must both be set in Settings."

    local_root = Path(local_root_str).expanduser()
    try:
        rel = local_file.relative_to(local_root)
    except ValueError:
        return False, "That file isn't inside the configured local folder."

    dest = device_dir / device_subpath / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        result = subprocess.run(
            ["gio", "copy", "-p", str(local_file), str(dest)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return False, f"gio copy failed: {err}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"MTP push failed: {exc}"

    return True, f"Pushed {local_file.name} to device."


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Mission:
    mission_id: str
    container: Path
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
    for f in files:
        if f.stem == stem:
            return f
    return files[0] if files else None


def scan_missions(local_root: Path, cfg: dict) -> list:
    if not local_root.exists() or not local_root.is_dir():
        return []

    map_preview_name = cfg["map_preview_dirname"]
    sidecar_name = cfg["sidecar_name"]
    map_preview_root = local_root / map_preview_name

    missions = []
    for d in sorted(local_root.iterdir()):
        if not d.is_dir() or d.name == map_preview_name:
            continue
        if d.name in {"capability", "map_preview", ".gitignore", ""}:
            continue

        mission_id = d.name

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


def read_waypoints(mission_file: Path) -> list:
    """Return [(lat, lon), ...] in mission order from a .kmz/.wpml/.kml file."""
    suffix = mission_file.suffix.lower()

    if suffix == ".kmz":
        with zipfile.ZipFile(mission_file, "r") as kmz:
            names = [n for n in kmz.namelist() if n.lower().endswith((".wpml", ".kml"))]
            if not names:
                raise ValueError("No WPML or KML file found inside the KMZ")
            chosen = next((n for n in names if n.lower().endswith("waylines.wpml")), names[0])
            xml_data = kmz.read(chosen)
    elif suffix in (".wpml", ".kml"):
        xml_data = mission_file.read_bytes()
    else:
        raise ValueError(f"Unsupported mission file type: {suffix}")

    root = ET.fromstring(xml_data)
    ns = {
        "kml": "http://www.opengis.net/kml/2.2",
        "wpml": "http://www.dji.com/wpmz/1.0.2",
    }

    waypoints = []
    for pm in root.findall(".//kml:Placemark", ns):
        coords = pm.find(".//kml:Point/kml:coordinates", ns)
        if coords is None or not coords.text:
            continue
        parts = coords.text.strip().split(",")
        if len(parts) < 2:
            continue
        idx_el = pm.find("wpml:index", ns)
        index = int(idx_el.text) if idx_el is not None else len(waypoints)
        waypoints.append((index, float(parts[1]), float(parts[0])))

    waypoints.sort(key=lambda w: w[0])
    return [(lat, lon) for _, lat, lon in waypoints]


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

THUMB_BOX = 170   # square box (px) the thumbnail is fitted into
DEVICE_PATH_HINT = "Android/data/dji.go.v5/files/waypoint"


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

        ttk.Label(frm, text="Device waypoint path (inside the storage root):").grid(row=4, column=0, sticky=tk.W, **pad)
        self.device_var = tk.StringVar(value=cfg.get("device_root", ""))
        ttk.Entry(frm, textvariable=self.device_var, width=52).grid(row=5, column=0, columnspan=2, sticky=tk.EW, padx=8)
        ttk.Button(frm, text="Use example", command=self._fill_example).grid(row=5, column=2, padx=8)
        ttk.Label(
            frm, foreground="#666", wraplength=460, justify=tk.LEFT,
            text=f"Typically:\n{DEVICE_PATH_HINT}\n"
                 "Do not include the device name or 'Internal shared storage'."
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
        self.cfg["device_root"] = clean_device_subpath(self.device_var.get())
        self.cfg["adb_serial"] = self.serial_var.get().strip()
        save_config(self.cfg)
        self.destroy()
        self.on_save()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DJI Waypoint Mission Manager")
        self.geometry("1100x700")
        self.minsize(900, 620)

        self.cfg = load_config()
        self.missions: list = []
        self.selected: Optional[Mission] = None
        self._thumb_imgtk = None

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
        # Status bar goes in FIRST so it is never pushed off the bottom
        # when the window is short.
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(
            self, textvariable=self.status_var, relief=tk.SUNKEN,
            anchor=tk.W, padding=(6, 2),
        ).pack(side=tk.BOTTOM, fill=tk.X)

        # ---- top bar: info on the left, actions on the right ----
        top = ttk.Frame(self, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Local copy:").grid(row=0, column=0, sticky=tk.W)
        self.path_var = tk.StringVar()
        ttk.Label(top, textvariable=self.path_var, foreground="#555", width=10).grid(
            row=0, column=1, sticky=tk.EW, padx=(6, 12))

        ttk.Label(top, text="Device:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        self.device_status_var = tk.StringVar(value="checking...")
        self.device_status_label = ttk.Label(top, textvariable=self.device_status_var, width=10)
        self.device_status_label.grid(row=1, column=1, sticky=tk.EW, padx=(6, 12), pady=(4, 0))

        actions = ttk.Frame(top)
        actions.grid(row=0, column=2, rowspan=2, sticky=tk.E)
        ttk.Button(actions, text="Rescan", command=self.rescan).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="Pull from Phone", command=self.pull_from_phone).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="Push All Changed", command=self.push_all_changed).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="Settings...", command=self.open_settings).pack(side=tk.LEFT, padx=2)

        self._set_path_display()

        # ---- body: mission list | details ----
        self.body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self.body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # --- left: mission list ---
        left = ttk.Frame(self.body, padding=(8, 4))
        columns = ("flight", "label", "id", "sync", "notes")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        spec = {
            #  col      heading     width  minwidth stretch
            "flight": ("Flight #", 60,  50,  False),
            "label":  ("Label",    130, 70,  False),
            "id":     ("ID",       80,  60,  False),
            "sync":   ("Sync",     80,  60,  False),
            "notes":  ("Notes",    120, 60,  True),
        }
        for col, (text, width, minw, stretch) in spec.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=minw, stretch=stretch, anchor=tk.W)

        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        self.body.add(left, weight=2)

        # --- right: details (grid so each section shrinks sensibly) ---
        right = ttk.Frame(self.body, padding=(8, 4))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=3, minsize=200)   # preview grows most
        right.rowconfigure(1, weight=0)                # files: fixed height
        right.rowconfigure(2, weight=1, minsize=150)   # tracking/notes

        # Preview: thumbnail + map
        preview = ttk.LabelFrame(right, text="Thumbnail and Preview")
        preview.grid(row=0, column=0, sticky=tk.NSEW, pady=(0, 6))
        preview.columnconfigure(1, weight=1)
        preview.rowconfigure(0, weight=1)

        thumb_box = ttk.Frame(preview, width=THUMB_BOX, height=THUMB_BOX, relief=tk.GROOVE)
        thumb_box.grid(row=0, column=0, sticky=tk.N, padx=(6, 6), pady=6)
        thumb_box.pack_propagate(False)
        self.thumb_label = ttk.Label(thumb_box, text="(no thumbnail)", anchor=tk.CENTER)
        self.thumb_label.pack(fill=tk.BOTH, expand=True)

        self.map_widget = TkinterMapView(preview, width=200, height=170, corner_radius=0)
        self.map_widget.grid(row=0, column=1, sticky=tk.NSEW, padx=(0, 6), pady=6)

        # Files
        files = ttk.LabelFrame(right, text="Files (managed by DJI Fly - names locked)")
        files.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        files.columnconfigure(1, weight=1)

        self.mission_file_var = tk.StringVar()
        self.thumb_file_var = tk.StringVar()
        self.mission_marker_var = tk.StringVar()
        self.thumb_marker_var = tk.StringVar()
        self.source_file_var = tk.StringVar()

        def file_row(row, title, name_var, marker_var, buttons):
            ttk.Label(files, text=title).grid(row=row, column=0, sticky=tk.W, padx=6, pady=3)
            ttk.Label(files, textvariable=name_var, foreground="#333", width=10).grid(
                row=row, column=1, sticky=tk.EW, padx=4)
            ttk.Label(files, textvariable=marker_var, foreground="#b34700").grid(
                row=row, column=2, sticky=tk.W, padx=2)
            bf = ttk.Frame(files)
            bf.grid(row=row, column=3, sticky=tk.E, padx=4)
            for text, cmd in buttons:
                ttk.Button(bf, text=text, width=9, command=cmd).pack(side=tk.LEFT, padx=2)

        file_row(0, "Mission:", self.mission_file_var, self.mission_marker_var, [
            ("Build...", lambda: self.build_file("mission")),
            ("Reveal", lambda: self.reveal_file("mission")),
        ])
        file_row(1, "Thumbnail:", self.thumb_file_var, self.thumb_marker_var, [
            ("Replace...", lambda: self.replace_file("thumbnail")),
            ("Reveal", lambda: self.reveal_file("thumbnail")),
        ])

        ttk.Label(files, text="Source CSV:").grid(row=2, column=0, sticky=tk.W, padx=6, pady=3)
        ttk.Label(files, textvariable=self.source_file_var, foreground="#333", width=10).grid(
            row=2, column=1, columnspan=3, sticky=tk.EW, padx=4)

        ttk.Button(files, text="Push this mission to phone", command=self.push_file).grid(
            row=3, column=0, columnspan=4, sticky=tk.E, padx=6, pady=(2, 6))

        # Tracking info
        meta = ttk.LabelFrame(right, text="Your tracking info (local only - never pushed)")
        meta.grid(row=2, column=0, sticky=tk.NSEW)
        meta.columnconfigure(3, weight=1)
        meta.rowconfigure(1, weight=1)

        ttk.Label(meta, text="Flight #:").grid(row=0, column=0, sticky=tk.W, padx=6, pady=4)
        self.flight_entry = ttk.Entry(meta, width=8)
        self.flight_entry.grid(row=0, column=1, sticky=tk.W, padx=4, pady=4)
        ttk.Label(meta, text="Label:").grid(row=0, column=2, sticky=tk.W, padx=(12, 4), pady=4)
        self.label_entry = ttk.Entry(meta, width=10)
        self.label_entry.grid(row=0, column=3, sticky=tk.EW, padx=(0, 6), pady=4)

        ttk.Label(meta, text="Notes:").grid(row=1, column=0, sticky=tk.NW, padx=6, pady=4)
        self.notes_text = tk.Text(meta, height=4, width=10, wrap=tk.WORD)
        self.notes_text.grid(row=1, column=1, columnspan=3, sticky=tk.NSEW, padx=(4, 6), pady=4)

        ttk.Button(meta, text="Save", command=self.save_selected).grid(
            row=2, column=0, columnspan=4, sticky=tk.E, padx=6, pady=(0, 6))

        self.body.add(right, weight=3)

        # Give the list ~42% of the width on first show.
        self.after(150, self._set_initial_sash)

        self._set_details_enabled(False)

    def _set_initial_sash(self):
        try:
            self.body.sashpos(0, int(self.winfo_width() * 0.42))
        except tk.TclError:
            pass

    def _set_path_display(self):
        self.path_var.set(shorten(self.cfg.get("local_root") or "(not set)", 70))

    def _set_details_enabled(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.flight_entry.configure(state=state)
        self.label_entry.configure(state=state)
        self.notes_text.configure(state=state)

    # ---- settings / status ------------------------------------------------

    def open_settings(self):
        SettingsDialog(self, self.cfg, on_save=self._after_settings_saved)

    def _after_settings_saved(self):
        self._set_path_display()
        self.rescan()

    def _poll_device_status(self):
        ready, message = device_status(self.cfg)
        self.device_status_var.set(shorten(message, 90))
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
            self.tree.insert("", tk.END, iid=m.mission_id, values=(
                m.meta.get("flight_number") or "",
                m.meta.get("label") or "",
                m.mission_id[:8],
                "needs push" if m.needs_push() else "",
                notes_preview,
            ))

    def _reselect(self, m: Mission):
        self._refresh_tree()
        self.tree.selection_set(m.mission_id)
        self.tree.see(m.mission_id)
        self.on_select()

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
        self.source_file_var.set(shorten(m.meta.get("source_csv") or "(none)", 70))

        self.flight_entry.delete(0, tk.END)
        self.flight_entry.insert(0, m.meta.get("flight_number") or "")

        self.label_entry.delete(0, tk.END)
        self.label_entry.insert(0, m.meta.get("label") or "")

        self.notes_text.delete("1.0", tk.END)
        self.notes_text.insert("1.0", m.meta.get("notes") or "")

        self._update_thumbnail(m)
        self._update_map_preview(m)

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
            img.thumbnail((THUMB_BOX - 4, THUMB_BOX - 4))
            self._thumb_imgtk = ImageTk.PhotoImage(img)
            self.thumb_label.configure(image=self._thumb_imgtk, text="")
        except Exception as exc:  # noqa: BLE001
            self.thumb_label.configure(image="", text="(couldn't load image)")
            self._thumb_imgtk = None
            self.status_var.set(f"Thumbnail error: {exc}")

    def _update_map_preview(self, m: Mission):
        """Draw the mission's waypoint path on the map widget."""
        self.map_widget.delete_all_marker()
        self.map_widget.delete_all_path()

        if not m.mission_file or not m.mission_file.is_file():
            return

        try:
            points = read_waypoints(m.mission_file)
            if not points:
                raise ValueError("No waypoint coordinates found")

            if len(points) > 1:
                self.map_widget.set_path(points)
                lats = [p[0] for p in points]
                lons = [p[1] for p in points]
                try:
                    self.map_widget.fit_bounding_box(
                        (max(lats), min(lons)), (min(lats), max(lons)))
                except Exception:  # noqa: BLE001 - widget not sized yet etc.
                    self.map_widget.set_position(points[0][0], points[0][1])
            else:
                self.map_widget.set_position(points[0][0], points[0][1])
                self.map_widget.set_zoom(17)

        except zipfile.BadZipFile:
            self.status_var.set("Map: the mission file is not a valid KMZ archive.")
        except ET.ParseError as exc:
            self.status_var.set(f"Map: could not parse mission XML ({exc})")
        except Exception as exc:  # noqa: BLE001
            self.status_var.set(f"Map: {exc}")

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

    # ---- Waypoint mission file actions ----------------------------------

    def build_file(self, kind: str):
        if not self.selected:
            return
        m = self.selected
        target = m.mission_file
        if target is None:
            messagebox.showinfo(
                "No mission file",
                "This mission has no mission file for me to build into.\n"
                "DJI Fly must create that file first.",
            )
            return

        source = filedialog.askopenfilename(
            title=f"Choose CSV to build {target.name} from",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not source:
            return
        source_path = Path(source)

        if not messagebox.askyesno(
            "Confirm build",
            f"This will overwrite the CONTENTS of your LOCAL COPY of:\n\n  {target}\n\n"
            f"with a mission built from:\n\n  {source_path}\n\n"
            "The filename stays exactly as it is, and nothing on the phone changes "
            "until you Push. Continue?",
        ):
            return
        try:
            build_kmz_from_csv(source_path, target)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Build failed", str(exc))
            return

        m.meta["mission_file_dirty"] = True
        m.meta["source_csv"] = str(source_path)
        try:
            m.save_meta()
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

        self._reselect(m)
        self.status_var.set(f"Rebuilt local contents of {target.name} - not yet pushed")

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

        source = filedialog.askopenfilename(title=f"Choose replacement content for {target.name}")
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

        m.meta["mission_file_dirty" if kind == "mission" else "thumbnail_dirty"] = True
        try:
            m.save_meta()
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))

        self._reselect(m)
        self.status_var.set(f"Replaced local contents of {target.name} - not yet pushed")

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
            self.rescan()
            self.status_var.set(message)
        else:
            self.status_var.set("Pull failed")
            messagebox.showerror("Pull from phone failed", message)

    def push_file(self):
        """Push the selected mission's file (always) and thumbnail (if changed)."""
        if not self.selected:
            return
        m = self.selected

        ready, message = device_status(self.cfg)
        if not ready:
            messagebox.showwarning("Device not ready", f"Can't reach the phone: {message}")
            return

        jobs = []
        if m.mission_file:
            jobs.append((m.mission_file, "mission_file_dirty"))
        if m.thumbnail_file and m.meta.get("thumbnail_dirty"):
            jobs.append((m.thumbnail_file, "thumbnail_dirty"))

        if not jobs:
            messagebox.showinfo("Nothing to push", "This mission has no mission file to push.")
            return

        for target, dirty_key in jobs:
            self.status_var.set(f"Pushing {target.name}...")
            self.update_idletasks()
            success, message = push_file_to_device(self.cfg, target)
            if not success:
                self.status_var.set("Push failed")
                messagebox.showerror("Push to phone failed", message)
                return
            m.meta[dirty_key] = False
            m.save_meta()
            self.status_var.set(message)

        self._reselect(m)

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
                ok, msg = push_file_to_device(self.cfg, m.mission_file)
                if ok:
                    m.meta["mission_file_dirty"] = False
                    pushed += 1
                else:
                    failed.append(f"{m.mission_id} (mission file): {msg}")
            if m.meta.get("thumbnail_dirty") and m.thumbnail_file:
                ok, msg = push_file_to_device(self.cfg, m.thumbnail_file)
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
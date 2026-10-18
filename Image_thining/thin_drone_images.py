#!/usr/bin/env python3
"""
Thin a drone image set by GPS spacing.

Reads lat/lon/alt from EXIF, sorts images by capture time, and keeps an image
only if it is at least --spacing metres from the images already kept.
Originals are never modified or deleted; kept images are copied to --output.

Examples
--------
  # Keep images at least 15 m apart
  python thin_drone_images.py ./flight ./flight_thinned --spacing 15

  # Derive spacing from footprint and target overlap
  # (60 m footprint along track, 75% forward overlap -> 15 m)
  python thin_drone_images.py ./flight ./flight_thinned --footprint 60 --overlap 0.75

  # Preview without copying anything
  python thin_drone_images.py ./flight ./flight_thinned --spacing 15 --dry-run

  # Also drop blurry frames first (needs: pip install opencv-python-headless)
  python thin_drone_images.py ./flight ./flight_thinned --spacing 15 --blur-threshold 60

Requires: Pillow   (pip install pillow)
"""
import argparse
import csv
import math
import shutil
import sys
from pathlib import Path

from PIL import Image

GPS_IFD = 0x8825
EXIF_IFD = 0x8769
EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}


# --------------------------------------------------------------------------
# EXIF helpers
# --------------------------------------------------------------------------
def _to_degrees(value, ref):
    d, m, s = (float(x) for x in value)
    deg = d + m / 60.0 + s / 3600.0
    return -deg if ref in ("S", "W") else deg


def read_meta(path):
    """Return dict(lat, lon, alt, ts) or None if the file can't be read."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            gps = exif.get_ifd(GPS_IFD)
            sub = exif.get_ifd(EXIF_IFD)
    except Exception:
        return None

    ts = sub.get(0x9003) or exif.get(0x0132) or ""
    meta = {"lat": None, "lon": None, "alt": None, "ts": str(ts)}

    if gps and 2 in gps and 4 in gps:
        try:
            meta["lat"] = _to_degrees(gps[2], gps.get(1, "N"))
            meta["lon"] = _to_degrees(gps[4], gps.get(3, "E"))
            if 6 in gps:
                alt = float(gps[6])
                if gps.get(5) in (1, b"\x01"):
                    alt = -alt
                meta["alt"] = alt
        except Exception:
            meta["lat"] = meta["lon"] = None
    return meta


def blur_score(path):
    """Variance of the Laplacian on a downscaled greyscale copy (higher = sharper)."""
    import cv2  # imported lazily so it's only needed with --blur-threshold

    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    h, w = img.shape
    if w > 1200:
        img = cv2.resize(img, (1200, int(h * 1200 / w)), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


# --------------------------------------------------------------------------
# Spatial thinning
# --------------------------------------------------------------------------
class SpacingFilter:
    """Greedy filter using a grid hash so it scales to tens of thousands of images."""

    def __init__(self, spacing, mode):
        self.spacing = spacing
        self.mode = mode  # "any" or "sequential"
        self.grid = {}
        self.last = None

    def _cell(self, x, y):
        return int(math.floor(x / self.spacing)), int(math.floor(y / self.spacing))

    def accept(self, x, y):
        if self.mode == "sequential":
            if self.last is not None and math.hypot(x - self.last[0], y - self.last[1]) < self.spacing:
                return False
        else:
            cx, cy = self._cell(x, y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for (px, py) in self.grid.get((cx + dx, cy + dy), ()):
                        if math.hypot(x - px, y - py) < self.spacing:
                            return False
        self.grid.setdefault(self._cell(x, y), []).append((x, y))
        self.last = (x, y)
        return True


def main():
    ap = argparse.ArgumentParser(description="Thin drone images by GPS spacing.")
    ap.add_argument("input", type=Path, help="Folder containing the original images")
    ap.add_argument("output", type=Path, help="Folder to copy kept images into")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spacing", type=float, help="Minimum distance between kept images (metres)")
    g.add_argument("--footprint", type=float,
                   help="Ground footprint along flight direction (metres); use with --overlap")
    ap.add_argument("--overlap", type=float, default=0.75,
                    help="Target forward overlap 0-1 when using --footprint (default 0.75)")
    ap.add_argument("--mode", choices=["any", "sequential"], default="any",
                    help="'any' (default): distance to ALL kept images, so it also thins "
                         "doubled-up flight lines. 'sequential': distance to the last kept image only.")
    ap.add_argument("--blur-threshold", type=float, default=None,
                    help="Drop frames whose Laplacian variance is below this (try 50-100)")
    ap.add_argument("--no-gps", choices=["keep", "drop"], default="keep",
                    help="What to do with images lacking GPS (default: keep)")
    ap.add_argument("--recursive", action="store_true", help="Search subfolders too")
    ap.add_argument("--dry-run", action="store_true", help="Log decisions but copy nothing")
    args = ap.parse_args()

    spacing = args.spacing if args.spacing else args.footprint * (1.0 - args.overlap)
    if spacing <= 0:
        sys.exit("Spacing must be > 0")

    if not args.input.is_dir():
        sys.exit(f"Input folder not found: {args.input}")

    pattern = "**/*" if args.recursive else "*"
    files = [p for p in args.input.glob(pattern) if p.is_file() and p.suffix.lower() in EXTS]
    if not files:
        sys.exit("No images found.")
    print(f"Found {len(files)} images. Reading EXIF...")

    records = []
    for p in files:
        m = read_meta(p)
        records.append({"path": p, "meta": m})

    # Sort by capture time, then filename
    records.sort(key=lambda r: ((r["meta"] or {}).get("ts", ""), r["path"].name))

    # Local metric projection centred on the mean position
    coords = [(r["meta"]["lat"], r["meta"]["lon"]) for r in records
              if r["meta"] and r["meta"]["lat"] is not None]
    if not coords:
        sys.exit("No images with GPS data found.")
    lat0 = sum(c[0] for c in coords) / len(coords)
    lon0 = sum(c[1] for c in coords) / len(coords)
    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))

    flt = SpacingFilter(spacing, args.mode)
    log = []
    kept = 0

    for i, r in enumerate(records, 1):
        p, m = r["path"], r["meta"]
        row = {"file": str(p.relative_to(args.input)), "timestamp": "", "lat": "", "lon": "",
               "alt": "", "blur": "", "status": "", "reason": ""}

        if m is None:
            row.update(status="dropped", reason="unreadable file")
            log.append(row)
            continue

        row.update(timestamp=m["ts"], lat=m["lat"] if m["lat"] is not None else "",
                   lon=m["lon"] if m["lon"] is not None else "",
                   alt=m["alt"] if m["alt"] is not None else "")

        # Optional blur filter (applied BEFORE thinning so blurry frames
        # never "use up" a position)
        if args.blur_threshold is not None:
            score = blur_score(p)
            row["blur"] = round(score, 1)
            if score < args.blur_threshold:
                row.update(status="dropped", reason=f"blurry (score {score:.1f})")
                log.append(row)
                continue

        if m["lat"] is None:
            if args.no_gps == "keep":
                row.update(status="kept", reason="no GPS (kept)")
                kept += 1
            else:
                row.update(status="dropped", reason="no GPS")
            log.append(row)
            continue

        x = (m["lon"] - lon0) * m_per_deg_lon
        y = (m["lat"] - lat0) * m_per_deg_lat
        if flt.accept(x, y):
            row.update(status="kept", reason="")
            kept += 1
        else:
            row.update(status="dropped", reason=f"< {spacing:.1f} m from kept image")
        log.append(row)

        if i % 500 == 0:
            print(f"  processed {i}/{len(records)}")

    # Copy kept files
    if not args.dry_run:
        args.output.mkdir(parents=True, exist_ok=True)
        for row in log:
            if row["status"] == "kept":
                src = args.input / row["file"]
                dst = args.output / row["file"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    # Write log
    log_path = (args.output if not args.dry_run else Path(".")) / "thinning_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        w.writeheader()
        w.writerows(log)

    total = len(records)
    print(f"\nSpacing: {spacing:.2f} m  |  mode: {args.mode}")
    print(f"Kept {kept} of {total} images ({100 * kept / total:.1f}%), dropped {total - kept}.")
    print(f"Log: {log_path}")
    if args.dry_run:
        print("(dry run: no files were copied)")


if __name__ == "__main__":
    main()
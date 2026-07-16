#!/usr/bin/env python3
"""
GeoFlight CSV  →  DJI Mavic 3 WPML KMZ Converter
=================================================
Converts a GeoFlight Planner (QGIS plugin) Litchi-compatible CSV export into
a DJI WPML waypoint KMZ file ready for import into DJI Fly on a Mavic 3
(droneEnumValue=68, droneSubEnumValue=0).

KMZ structure
─────────────
  <mission_name>.kmz
  └── wpmz/
      ├── template.kml   (required by spec; DJI Fly ignores it but it must exist)
      └── waylines.wpml  (actual WPML flight instructions)

Usage
─────
  python geoflight_to_kmz.py input.csv [output.kmz] [options]

Options
  --speed      FLOAT   Global auto-flight speed in m/s (default: 5.0, max 15)
  --altitude   FLOAT   Override altitude for ALL waypoints in metres AGL
                       (if omitted the altitude column from the CSV is used)
  --finish     STR     Action when mission ends: goHome | hover | autoLand
                       (default: goHome)
  --lost       STR     Action on RC signal loss: goContinue | executeLostAction
                       (default: goContinue)
  --takeoff-h  FLOAT   Safety height to clear obstacles on take-off (default: 30)
  --no-photo           Suppress the default "take photo at every waypoint" action

Input CSV columns (Litchi-compatible format from GeoFlight)
───────────────────────────────────────────────────────────
Required:
  latitude              decimal degrees (WGS84)
  longitude             decimal degrees (WGS84)
  altitude(m)           metres AGL

Optional (used if present):
  speed(m/s)            per-waypoint speed override
  heading(deg)          yaw angle 0-360 (360/0 = smoothTransition/follow-path)
  gimbalpitchangle      gimbal pitch in degrees (-90 = nadir, 0 = level)
  altitudemode          0 = AGL (default), 1 = absolute MSL
  photo_distinterval    >0 → add takePhoto action
  photo_timeinterval    >0 → add takePhoto action
  actiontype1           Litchi code 1 = take photo

Dependencies: Python standard library only (csv, zipfile, xml.etree, argparse)
"""

import argparse
import csv
import io
from operator import index
from pdb import pm
import sys
from turtle import heading
import zipfile
from xml.etree import ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from datetime import datetime, timezone

from History.MissionCreation.Source.NEW_ConvertCSVtoKMZ import _pretty
from zipfile import ZipFile, ZIP_DEFLATED


MAVIC3_DRONE_ENUM       = 68   # Mavic 3 series
MAVIC3_DRONE_SUB_ENUM   = 0
MAVIC3_PAYLOAD_ENUM     = 66   # built-in camera (Hasselblad / 4/3 CMOS)
MAVIC3_PAYLOAD_POSITION = 0    # main gimbal

MAX_SPEED_MS = 15.0    

WPML_NS = "http://www.dji.com/wpmz/1.0.2"
KML_NS  = "http://www.opengis.net/kml/2.2"


FINISH_ACTION = "noAction"  # goHome | hover | autoLand | noAction
EXECUTE_LOST_ACTION = "goHome"  # goContinue | executeLostAction | goHome | hover
ACTION_COUNT = 1


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pretty(element: ET.Element) -> str:
    """Return indented XML string (without the extra blank lines minidom adds)."""
    raw = ET.tostring(element, encoding="unicode")
    reparsed = minidom.parseString(raw)
    pretty = reparsed.toprettyxml(indent="  ")
    # Remove the XML declaration line that minidom prepends (we add our own)
    lines = pretty.splitlines()
    return "\n".join(lines[1:])   # drop <?xml …?> line


def _xml_declaration() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n'


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _col(row: dict, *names, default=None):
    """Case-insensitive column lookup across several possible column names."""
    for name in names:
        for key in row:
            if key.strip().lower() == name.lower():
                v = row[key].strip()
                return v if v != "" else default
    return default


def _float(row: dict, *names, default: float = 0.0) -> float:
    v = _col(row, *names)
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _int(row: dict, *names, default: int = 0) -> int:
    v = _col(row, *names)
    try:
        return int(float(v)) if v is not None else default
    except (ValueError, TypeError):
        return default
    

# ── CSV reader ───────────────────────────────────────────────────────────────

def read_geoflight_csv(path: str) -> list[dict]:
    """
    Read the GeoFlight / Litchi-compatible CSV.
    Handles the standard Litchi header row as well as variants that GeoFlight
    may produce (e.g. 'altitude(m)' vs 'altitude').
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        raise ValueError(f"CSV file '{path}' is empty or has no data rows.")

    # Sanity-check that we have lat/lon/alt
    first = rows[0]
    has_lat = any(k.strip().lower() in ("latitude",) for k in first)
    has_lon = any(k.strip().lower() in ("longitude",) for k in first)
    has_alt = any("altitude" in k.strip().lower() for k in first)

    if not (has_lat and has_lon and has_alt):
        raise ValueError(
            "CSV must contain 'latitude', 'longitude', and an altitude column "
            f"(e.g. 'altitude(m)'). Found columns: {list(first.keys())}"
        )
    return rows


# ── WPML builders ────────────────────────────────────────────────────────────

def _sub(parent: ET.Element, tag: str, text: str | None = None,
         ns: str = "") -> ET.Element:
    full_tag = f"{{{ns}}}{tag}" if ns else tag
    el = ET.SubElement(parent, full_tag)
    if text is not None:
        el.text = str(text)
    return el


def build_template_kml(mission_name: str, args) -> str:
    """
    Build the template.kml file.
    DJI Fly ignores this file for consumer drones but it must be present and
    valid for the KMZ to parse correctly in DJI Pilot 2 / enterprise workflows.
    """

    ET.register_namespace("", KML_NS)
    ET.register_namespace("wpml", WPML_NS)

    kml = ET.Element(f"{{{KML_NS}}}kml")
    doc = ET.SubElement(kml, f"{{{KML_NS}}}Document")

    epoch_now = int(datetime.now().timestamp())
    _sub(doc, "author", "fly", ns=WPML_NS)
    _sub(doc, "createTime", epoch_now, ns=WPML_NS)
    _sub(doc, "updateTime", epoch_now, ns=WPML_NS)


    # ── Mission config ──────────────────────────────────────────────────────

    mc =  _sub(doc, "missionConfig", ns=WPML_NS)
    _sub(mc, "flyToWaylineMode", "safely", ns=WPML_NS)
    _sub(mc, "finishAction", FINISH_ACTION, ns=WPML_NS)
    _sub(mc, "exitOnRCLost", "executeLostAction", ns=WPML_NS)
    _sub(mc, "executeRCLostAction", EXECUTE_LOST_ACTION, ns=WPML_NS)
    _sub(mc, "globalTransitionalSpeed", "2.5", ns=WPML_NS)

    di = _sub(mc, "droneInfo", ns=WPML_NS)
    _sub(di, "droneEnumValue", "68", ns=WPML_NS)
    _sub(di, "droneSubEnumValue", "0", ns=WPML_NS)

    print(f"Building template.kml for mission '{mission_name}'…")
    return _xml_declaration() + _pretty(kml)


    # ── WMPL Creation ──────────────────────────────────────────────────────

def add_placemark(folder, row, index):
    pm = _sub(folder, "Placemark")

    point = _sub(pm, "Point")
    _sub(
        point,
        "coordinates",
        f"{row['longitude']},{row['latitude']}"
    )

    _sub(pm, "index", str(index), ns=WPML_NS)
    _sub(pm, "executeHeight", row["altitude(m)"], ns=WPML_NS)
    _sub(pm, "waypointSpeed", row["speed(m/s)"] or "2.5", ns=WPML_NS)

    
    global ACTION_COUNT
    
    
    heading_angle = -90 if ((index - 1) // 2) % 2 == 0 else 90    
    if str(index) == "0":
        heading_angle = -90
    # Heading
    
    heading = _sub(pm, "waypointHeadingParam", ns=WPML_NS)
    _sub(heading, "waypointHeadingMode", "smoothTransition", ns=WPML_NS)
    _sub(heading, "waypointHeadingAngle", str(heading_angle), ns=WPML_NS)
    _sub(heading, "waypointPoiPoint", "0.000000,0.000000,0.000000", ns=WPML_NS)
    _sub(heading, "waypointHeadingAngleEnable", "1" if str(index) == "0" else "0", ns=WPML_NS)
    _sub(heading, "waypointHeadingPathMode", "followBadArc", ns=WPML_NS)

    # Turn
    turn = _sub(pm, "waypointTurnParam", ns=WPML_NS)
    _sub(turn, "waypointTurnMode", "toPointAndStopWithContinuityCurvature" if str(index) == "0" else "toPointAndPassWithContinuityCurvature", ns=WPML_NS)
    _sub(turn, "waypointTurnDampingDist", "0", ns=WPML_NS)

    _sub(pm, "useStraightLine", "0", ns=WPML_NS)

    if str(index) == "0":
        ag0 = _sub(pm, "actionGroup", ns=WPML_NS)
        _sub(ag0, "actionGroupId", "1", ns=WPML_NS)
        _sub(ag0, "actionGroupStartIndex", str(index), ns=WPML_NS)
        _sub(ag0, "actionGroupEndIndex", str(index), ns=WPML_NS)
        _sub(ag0, "actionGroupMode", "parallel", ns=WPML_NS)

        trigger = _sub(ag0, "actionTrigger", ns=WPML_NS)
        _sub(trigger, "actionTriggerType", "reachPoint", ns=WPML_NS)

        action = _sub(ag0, "action", ns=WPML_NS)
        _sub(action, "actionId", str(ACTION_COUNT), ns=WPML_NS)
        _sub(action, "actionActuatorFunc", "gimbalRotate", ns=WPML_NS)
        ACTION_COUNT += 1

        params = _sub(action, "actionActuatorFuncParam", ns=WPML_NS)
        _sub(params, "gimbalHeadingYawBase", "aircraft", ns=WPML_NS)
        _sub(params, "gimbalRotateMode", "absoluteAngle", ns=WPML_NS)
        _sub(params, "gimbalPitchRotateEnable", "1", ns=WPML_NS)
        _sub(params, "gimbalPitchRotateAngle", "-90", ns=WPML_NS)
        _sub(params, "gimbalRollRotateEnable", "0", ns=WPML_NS)
        _sub(params, "gimbalRollRotateAngle", "0", ns=WPML_NS)
        _sub(params, "gimbalYawRotateEnable", "0", ns=WPML_NS)
        _sub(params, "gimbalYawRotateAngle", "0", ns=WPML_NS)
        _sub(params, "gimbalRotateTimeEnable", "0", ns=WPML_NS)
        _sub(params, "gimbalRotateTime", "0", ns=WPML_NS)
        _sub(params, "payloadPositionIndex", "0", ns=WPML_NS)

    # Action Group 1
    ag1 = _sub(pm, "actionGroup", ns=WPML_NS)
    _sub(ag1, "actionGroupId", "2", ns=WPML_NS)
    _sub(ag1, "actionGroupStartIndex", str(index), ns=WPML_NS)
    _sub(ag1, "actionGroupEndIndex", str(index), ns=WPML_NS)
    _sub(ag1, "actionGroupMode", "parallel", ns=WPML_NS)

    trigger = _sub(ag1, "actionTrigger", ns=WPML_NS)
    _sub(trigger, "actionTriggerType", "reachPoint", ns=WPML_NS)

    action = _sub(ag1, "action", ns=WPML_NS)
    _sub(action, "actionId", str(ACTION_COUNT), ns=WPML_NS)
    _sub(action, "actionActuatorFunc", "gimbalEvenlyRotate", ns=WPML_NS)

    params = _sub(action, "actionActuatorFuncParam", ns=WPML_NS)
    _sub(params, "gimbalPitchRotateAngle", "-90", ns=WPML_NS)
    _sub(params, "payloadPositionIndex", "0", ns=WPML_NS)
    ACTION_COUNT += 1



def build_waylines_wpml(rows: list[dict], args) -> str:
    """
    Build the waylines.wpml file from the CSV rows.
    Each row corresponds to a waypoint with optional speed, heading, gimbal pitch,
    and photo actions.
    """
    ET.register_namespace("", KML_NS)
    ET.register_namespace("wpml", WPML_NS)

    kml = ET.Element(f"{{{KML_NS}}}kml")
    doc = ET.SubElement(kml, f"{{{KML_NS}}}Document")


    # ── Mission config ──────────────────────────────────────────────────────

    mc =  _sub(doc, "missionConfig", ns=WPML_NS)
    _sub(mc, "flyToWaylineMode", "safely", ns=WPML_NS)
    _sub(mc, "finishAction", FINISH_ACTION, ns=WPML_NS)
    _sub(mc, "exitOnRCLost", "executeLostAction", ns=WPML_NS)
    _sub(mc, "executeRCLostAction", EXECUTE_LOST_ACTION, ns=WPML_NS)
    _sub(mc, "globalTransitionalSpeed", "2.5", ns=WPML_NS)

    di = _sub(mc, "droneInfo", ns=WPML_NS)
    _sub(di, "droneEnumValue", "68", ns=WPML_NS)
    _sub(di, "droneSubEnumValue", "0", ns=WPML_NS)


    # ── Wayline folder ─────────────────────────────────────────────────────

    fd = _sub(doc, "Folder")
    _sub(fd, "templateId", "0", ns=WPML_NS)
    _sub(fd, "executeHeightMode", "relativeToStartPoint", ns=WPML_NS)
    _sub(fd, "waylineId", "0", ns=WPML_NS)
    _sub(fd, "distance", "0", ns=WPML_NS)
    _sub(fd, "duration", "0", ns=WPML_NS)
    _sub(fd, "autoFlightSpeed", "2.5", ns=WPML_NS)

    for index, row in enumerate(rows):
        add_placemark(fd, row, index)


    return _xml_declaration() + _pretty(kml)




# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Convert a GeoFlight Planner CSV to a DJI Mavic 3 WPML KMZ."
    )
    p.add_argument("input",  help="Path to the GeoFlight / Litchi CSV file")
    p.add_argument("output", nargs="?", default=None,
                   help="Output KMZ file path (default: same name as input with .kmz)")
    p.add_argument("--speed",    type=float, default=5.0,
                   help="Global flight speed m/s (default 5.0, max 15)")
    p.add_argument("--altitude", type=float, default=None,
                   help="Override altitude AGL in metres (uses CSV values if omitted)")
    p.add_argument("--finish",   default="goHome",
                   choices=["goHome", "hover", "autoLand"],
                   help="Mission finish action (default: goHome)")
    p.add_argument("--lost",     default="goContinue",
                   choices=["goContinue", "executeLostAction"],
                   help="RC lost action (default: goContinue)")
    p.add_argument("--takeoff-h", type=float, default=30.0, dest="takeoff_h",
                   help="Safety take-off height in metres (default: 30)")
    p.add_argument("--no-photo", action="store_true",
                   help="Suppress takePhoto actions at every waypoint")

    args = p.parse_args(argv)

    # Clamp speed
    args.speed = _clamp(args.speed, 0.1, MAX_SPEED_MS)

    # Default output path
    if args.output is None:
        args.output = str(Path(args.input).with_suffix(".kmz"))

    return args

def build_kmz(rows: list[dict], output_path: str, args) -> None:
    template_xml  = build_template_kml(Path(output_path).stem, args)
    waylines_xml  = build_waylines_wpml(rows, args)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wpmz/template.kml",  template_xml)
        zf.writestr("wpmz/waylines.wpml", waylines_xml)

    print(f"✅  KMZ written → {output_path}")
    print(f"   Waypoints : {len(rows)}")
    print(f"   Speed     : {args.speed} m/s")
    print(f"   Finish    : {args.finish}")
    print(f"   RC lost   : {args.lost}")


def main(argv=None):
    args = parse_args(argv)

    print(f"📂 Reading: {args.input}")
    rows = read_geoflight_csv(args.input)
    print(f"   Found {len(rows)} waypoints")

    build_kmz(rows, args.output, args)
    
    print(f"Wrote {args.output}")
    
if __name__ == "__main__":
    main()
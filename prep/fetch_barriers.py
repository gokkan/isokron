#!/usr/bin/env python3
"""Fetch water barriers and walkable bridges from OpenStreetMap.

    python prep/fetch_barriers.py --out prep/barriers.geojson

Writes GeoJSON: LineString features are barriers you cannot walk across,
Point features are gates -- bridges -- where you can. prep.py clips and
simplifies this further against the stop set; what lands here is the whole
Vastra Gotaland region, and it is committed, so the weekly build never talks
to Overpass. Run this again when a new bridge opens.

Only large water is a barrier. The threshold is area, not a name list: a
stream you can step over is mapped as waterway=stream and never becomes a
polygon, and every lake big enough to force a detour clears ten hectares.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://overpass-api.de/api/interpreter"
# Vastrafik's area with room to spare: Kungsbacka in the south, Dalsland in
# the north, Toreboda in the east, the outer islands in the west.
BBOX = (57.20, 11.00, 59.35, 14.80)
EARTH_R = 6371008.8

# Bridges that carry people on foot. A motorway bridge is only a gate if OSM
# says feet are allowed, which is what keeps Tingstadstunneln and the E6
# crossings out; a footway bridge always is. Ferries are deliberately absent,
# and structurally so: only highway ways with bridge=yes are considered, and
# an alvsnabbe is a ferry route over water, never a highway. Vastrafik's
# ferries are already departures in the timetable, and letting them double as
# walking would put you across the river in forty seconds.
WALKABLE = {
    "footway", "path", "pedestrian", "steps", "cycleway", "living_street",
    "residential", "unclassified", "tertiary", "tertiary_link", "secondary",
    "secondary_link", "primary", "primary_link", "service", "track", "road",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def overpass(query, retries=4, timeout=300):
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    delay = 5
    for attempt in range(retries):
        req = urllib.request.Request(
            ENDPOINT, data=body,
            headers={"User-Agent": "isokron/barriers (github.com/gokkan/isokron)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                raise
            log("  overpass failed (%s), retrying in %ds" % (exc, delay))
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def tiles(bbox, step):
    south, west, north, east = bbox
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            yield (lat, lon, min(lat + step, north), min(lon + step, east))
            lon += step
        lat += step


def ring_area_m2(coords):
    """Shoelace in local metres. Sign discarded; we only want the size."""
    if len(coords) < 4:
        return 0.0
    lat0 = math.radians(sum(c[1] for c in coords) / len(coords))
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    total = 0.0
    for i in range(len(coords) - 1):
        x0, y0 = coords[i][0] * kx, coords[i][1] * ky
        x1, y1 = coords[i + 1][0] * kx, coords[i + 1][1] * ky
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def geom_of(element):
    g = element.get("geometry")
    if not g:
        return None
    return [(pt["lon"], pt["lat"]) for pt in g if "lon" in pt and "lat" in pt]


def fetch_water(bbox, step, min_area):
    """Shorelines of water big enough to walk around rather than through."""
    lines = []
    for i, (s, w, n, e) in enumerate(tiles(bbox, step), 1):
        box = "%.4f,%.4f,%.4f,%.4f" % (s, w, n, e)
        log("  water tile %d %s" % (i, box))
        doc = overpass(
            "[out:json][timeout:280];("
            'way["natural"="water"](%s);'
            'way["waterway"="riverbank"](%s);'
            'relation["natural"="water"](%s);'
            ");out geom;" % (box, box, box))
        kept = 0
        for el in doc.get("elements", []):
            if el.get("type") == "way":
                coords = geom_of(el)
                if coords and ring_area_m2(coords) >= min_area:
                    lines.append(coords)
                    kept += 1
            elif el.get("type") == "relation":
                # A multipolygon lake arrives as its member ways. Each ring is
                # a barrier in its own right, inner islands included -- an
                # island inside a lake is land you cannot walk to either.
                for member in el.get("members", []):
                    if member.get("type") != "way":
                        continue
                    coords = geom_of(member)
                    if coords and len(coords) >= 2:
                        lines.append(coords)
                        kept += 1
        log("    kept %d" % kept)
    return lines


def fetch_coastline(bbox, step):
    lines = []
    for i, (s, w, n, e) in enumerate(tiles(bbox, step), 1):
        box = "%.4f,%.4f,%.4f,%.4f" % (s, w, n, e)
        log("  coast tile %d %s" % (i, box))
        doc = overpass('[out:json][timeout:280];'
                       'way["natural"="coastline"](%s);out geom;' % box)
        kept = 0
        for el in doc.get("elements", []):
            coords = geom_of(el)
            if coords and len(coords) >= 2:
                lines.append(coords)
                kept += 1
        log("    kept %d" % kept)
    return lines


def way_length_m(coords):
    lat0 = math.radians(sum(c[1] for c in coords) / len(coords))
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    total = 0.0
    for i in range(len(coords) - 1):
        total += math.hypot((coords[i + 1][0] - coords[i][0]) * kx,
                            (coords[i + 1][1] - coords[i][1]) * ky)
    return total


def fetch_gates(bbox, step):
    """Each walkable bridge as its two landings plus the metres between them.

    A bridge is not a point. Modelled as one, the walk to it would have to
    cross the near bank before reaching midstream, and the crossing would only
    line up with the bridge for someone already standing in line with it.
    """
    gates = []
    for i, (s, w, n, e) in enumerate(tiles(bbox, step), 1):
        box = "%.4f,%.4f,%.4f,%.4f" % (s, w, n, e)
        log("  bridge tile %d %s" % (i, box))
        doc = overpass('[out:json][timeout:280];'
                       'way["bridge"]["highway"](%s);out geom;' % box)
        kept = 0
        for el in doc.get("elements", []):
            tags = el.get("tags", {})
            if tags.get("bridge") in ("no", None):
                continue
            foot = tags.get("foot")
            if foot in ("no", "private"):
                continue
            if foot not in ("yes", "designated", "permissive") \
                    and tags.get("highway") not in WALKABLE:
                continue
            coords = geom_of(el)
            if len(coords or ()) < 2:
                continue
            # The walked length, not the chord: a curved bridge is longer than
            # the line between its landings, and the walk pays the difference.
            gates.append((coords[0], coords[-1], way_length_m(coords)))
            kept += 1
        log("    kept %d" % kept)
    return gates


def dedupe_gates(gates, spacing_m):
    """One gate per bridge is plenty; parallel carriageways are not two."""
    if not gates:
        return []
    lat0 = math.radians(sum(g[0][1] for g in gates) / len(gates))
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    seen = set()
    out = []
    for a, b, length in gates:
        key = (int(a[0] * kx // spacing_m), int(a[1] * ky // spacing_m),
               int(b[0] * kx // spacing_m), int(b[1] * ky // spacing_m))
        if key in seen:
            continue
        seen.add(key)
        out.append((a, b, length))
    return out


def main():
    global ENDPOINT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="prep/barriers.geojson")
    ap.add_argument("--bbox", default=",".join("%g" % v for v in BBOX),
                    help="south,west,north,east")
    ap.add_argument("--tile", type=float, default=0.5,
                    help="degrees per Overpass request")
    ap.add_argument("--min-area", type=float, default=100000.0,
                    help="m2 of water below which it is not a barrier")
    ap.add_argument("--tolerance", type=float, default=0.0004,
                    help="Douglas-Peucker tolerance in degrees (~40 m)")
    ap.add_argument("--gate-spacing", type=float, default=40.0,
                    help="m between kept bridge landings")
    ap.add_argument("--endpoint", default=ENDPOINT)
    args = ap.parse_args()

    ENDPOINT = args.endpoint
    bbox = tuple(float(v) for v in args.bbox.split(","))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from barriers import simplify

    log("fetching water...")
    lines = fetch_water(bbox, args.tile, args.min_area)
    log("fetching coastline...")
    lines += fetch_coastline(bbox, args.tile)
    log("fetching bridges...")
    gates = dedupe_gates(fetch_gates(bbox, args.tile), args.gate_spacing)

    raw_points = sum(len(l) for l in lines)
    lines = [simplify(l, args.tolerance) for l in lines]
    lines = [l for l in lines if len(l) >= 2]
    kept_points = sum(len(l) for l in lines)
    log("%d lines, %d -> %d points, %d gates"
        % (len(lines), raw_points, kept_points, len(gates)))

    def rnd(v):
        return round(v, 5)          # ~1 m, and it halves the file

    doc = {
        "type": "FeatureCollection",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "OpenStreetMap contributors, ODbL, via Overpass",
        "bbox": list(bbox),
        "features": [
            {"type": "Feature", "properties": {"kind": "barrier"},
             "geometry": {"type": "LineString",
                          "coordinates": [[rnd(x), rnd(y)] for x, y in line]}}
            for line in lines
        ] + [
            {"type": "Feature",
             "properties": {"kind": "gate", "len": round(length, 1)},
             "geometry": {"type": "LineString",
                          "coordinates": [[rnd(a[0]), rnd(a[1])],
                                          [rnd(b[0]), rnd(b[1])]]}}
            for a, b, length in gates
        ],
    }
    opener = gzip.open if args.out.endswith(".gz") else open
    with opener(args.out, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    log("wrote %s" % args.out)


if __name__ == "__main__":
    main()

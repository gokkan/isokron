#!/usr/bin/env python3
"""Fetch water barriers and walkable bridges from OpenStreetMap.

    python prep/fetch_barriers.py --out prep/barriers.geojson

Writes GeoJSON: LineString features are barriers you cannot walk across, and
LineStrings tagged kind=gate are bridges -- two landings and the metres of
walking between them. prep.py clips and simplifies this further against the
stop set; what lands here is the whole Vastra Gotaland region, and it is
committed, so the weekly build never talks to Overpass. Run this again when a
new bridge opens.

Only large water is a barrier. The threshold is area, not a name list: a
stream you can step over is mapped as waterway=stream and never becomes a
polygon, and every lake big enough to force a detour clears ten hectares.

Three things make this survive a real run, and all three were learned the
hard way from the shape of the problem rather than from a failure:

  * Every response is cached on disk by query. Forty tiles times three
    queries is over a hundred requests, and Overpass will throttle somewhere
    in the middle of that; without a cache the retry starts from tile one.
  * A tile Overpass calls too big is split into quarters and asked again,
    rather than retried identically. The archipelago needs a finer grid than
    Dalsland does, and this finds that out by itself.
  * A bridge is only kept if it actually crosses water we kept. Otherwise
    every culvert and every viaduct over a road comes along, which inflates
    the file and slows down every blocked walk, since the detour search looks
    at each bridge in turn.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from barriers import Barriers, simplify

ENDPOINT = "https://overpass-api.de/api/interpreter"
# Vastrafik's area with room to spare: Kungsbacka in the south, Dalsland in
# the north, Toreboda in the east, the outer islands in the west.
BBOX = (57.20, 11.00, 59.35, 14.80)
EARTH_R = 6371008.8
MAX_SPLIT = 4              # how far a stubborn tile may be quartered

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

QUERIES = {
    "water": '(way["natural"="water"]({box});'
             'way["waterway"="riverbank"]({box});'
             'relation["natural"="water"]({box}););out geom;',
    "coast": 'way["natural"="coastline"]({box});out geom;',
    "bridge": 'way["bridge"]["highway"]({box});out geom;',
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class QueryTooBig(Exception):
    """Overpass gave up on the tile. Ask for less of it."""


def cache_path(cache_dir, query):
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, digest + ".json.gz")


def force_ipv4():
    """Resolve the endpoint over IPv4 only.

    A full-region run died on 'Network is unreachable' after nearly two hours
    of working fine. That is a routing failure, not a refusal -- the runner
    has AAAA records to try and no route for them. Asking for A records only
    takes the failure mode away.
    """
    original = socket.getaddrinfo

    def ipv4_only(host, port, family=0, *args, **kwargs):
        return original(host, port, socket.AF_INET, *args, **kwargs)

    socket.getaddrinfo = ipv4_only


def overpass(query, cache_dir, retries=10, timeout=600):
    """One Overpass request, cached on disk and patient about being throttled.

    Raises QueryTooBig when the server says the query timed out or ran out of
    memory -- that is not a transient failure and retrying it unchanged only
    burns quota.

    Ten attempts and waits up to five minutes, not three and thirty seconds:
    a Goteborg-sized run drew two 429s inside its first three queries, and a
    full region eventually lost the host entirely. Waiting is cheaper than a
    re-run, and the cache means a re-run costs only what is still missing.
    """
    path = cache_path(cache_dir, query) if cache_dir else None
    if path and os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    delay = 5
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            ENDPOINT, data=body,
            headers={"User-Agent":
                     "isokron/barriers (github.com/gokkan/isokron)"})
        wait = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                doc = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            # 429 is the fair-use limiter and 504 is the gateway giving up on
            # a query still running; both are worth waiting out, and the
            # server usually says for how long.
            if exc.code in (429, 503, 504):
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = int(header) if header else None
                except ValueError:
                    wait = None
            else:
                raise
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            last = exc
        else:
            # A query that died inside Overpass still comes back as HTTP 200
            # with a note about it. Taking that at face value would silently
            # drop a tile's worth of water, which is far worse than failing.
            remark = str(doc.get("remark", ""))
            if "runtime error" in remark or "timed out" in remark \
                    or "out of memory" in remark:
                raise QueryTooBig(remark.strip())
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with gzip.open(path, "wt", encoding="utf-8") as fh:
                    json.dump(doc, fh)
            return doc

        if attempt == retries - 1:
            break
        pause = wait if wait else delay
        log("    %s -- waiting %ds" % (last, pause))
        time.sleep(pause)
        delay = min(delay * 2, 300)
    raise RuntimeError("overpass failed after %d attempts: %s" % (retries, last))


def quarters(bbox):
    s, w, n, e = bbox
    ms, mw = (s + n) / 2.0, (w + e) / 2.0
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def tiles(bbox, step):
    south, west, north, east = bbox
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            yield (lat, lon, min(lat + step, north), min(lon + step, east))
            lon += step
        lat += step


def elements(kind, bbox, cache_dir, timeout, depth=0):
    """Every element of one kind in one bbox, splitting the tile if need be."""
    box = "%.4f,%.4f,%.4f,%.4f" % bbox
    query = "[out:json][timeout:%d];%s" % (
        timeout, QUERIES[kind].format(box=box))
    cached = cache_dir and os.path.exists(cache_path(cache_dir, query))
    log("  %-6s %s%s" % (kind, box, "  (cached)" if cached else ""))
    try:
        doc = overpass(query, cache_dir, timeout=timeout + 60)
    except QueryTooBig as exc:
        if depth >= MAX_SPLIT:
            raise RuntimeError("%s tile %s is too big even split %d times: %s"
                               % (kind, box, depth, exc))
        log("    too big (%s) -- splitting" % str(exc)[:60])
        out = []
        for quad in quarters(bbox):
            out.extend(elements(kind, quad, cache_dir, timeout, depth + 1))
        return out
    return doc.get("elements", [])


def geom_of(element):
    g = element.get("geometry")
    if not g:
        return None
    return [(pt["lon"], pt["lat"]) for pt in g if "lon" in pt and "lat" in pt]


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


def way_length_m(coords):
    lat0 = math.radians(sum(c[1] for c in coords) / len(coords))
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    total = 0.0
    for i in range(len(coords) - 1):
        total += math.hypot((coords[i + 1][0] - coords[i][0]) * kx,
                            (coords[i + 1][1] - coords[i][1]) * ky)
    return total


def fetch_water(bbox, step, min_area, cache_dir, timeout):
    """Shorelines of water big enough to walk around rather than through."""
    lines = []
    small = 0
    for tile in tiles(bbox, step):
        for el in elements("water", tile, cache_dir, timeout):
            if el.get("type") == "way":
                coords = geom_of(el)
                if not coords:
                    continue
                if ring_area_m2(coords) >= min_area:
                    lines.append(coords)
                else:
                    small += 1
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
    log("  water: %d shorelines, %d ponds below %g m2 ignored"
        % (len(lines), small, min_area))
    return lines


def fetch_coastline(bbox, step, cache_dir, timeout):
    lines = []
    for tile in tiles(bbox, step):
        for el in elements("coast", tile, cache_dir, timeout):
            coords = geom_of(el)
            if coords and len(coords) >= 2:
                lines.append(coords)
    log("  coast: %d ways" % len(lines))
    return lines


def fetch_gates(bbox, step, cache_dir, timeout):
    """Each walkable bridge as its two landings plus the metres between them.

    A bridge is not a point. Modelled as one, the walk to it would have to
    cross the near bank before reaching midstream, and the crossing would only
    line up with the bridge for someone already standing in line with it.
    """
    gates = []
    barred = 0
    for tile in tiles(bbox, step):
        for el in elements("bridge", tile, cache_dir, timeout):
            tags = el.get("tags", {})
            if tags.get("bridge") in ("no", None):
                continue
            foot = tags.get("foot")
            if foot in ("no", "private"):
                barred += 1
                continue
            if foot not in ("yes", "designated", "permissive") \
                    and tags.get("highway") not in WALKABLE:
                barred += 1
                continue
            coords = geom_of(el)
            if len(coords or ()) < 2:
                continue
            # The walked length, not the chord: a curved bridge is longer than
            # the line between its landings, and the walk pays the difference.
            gates.append((coords[0], coords[-1], way_length_m(coords)))
    log("  bridges: %d walkable, %d closed to pedestrians" % (len(gates), barred))
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


def gates_over_water(gates, lines, bbox):
    """Keep the bridges that cross water we are actually going to ship.

    Every other bridge is a viaduct over a road or a plank over a ditch: it
    can never unblock anything, but it would sit in the file and be tried by
    every blocked walk in range.
    """
    if not gates or not lines:
        return []
    offsets = [0]
    lon, lat = [], []
    for line in lines:
        for x, y in line:
            lon.append(x)
            lat.append(y)
        offsets.append(len(lon))
    began = time.time()
    bars = Barriers(lon, lat, offsets, [], [], [], [], [])
    log("  indexed %d shoreline segments in %.1fs"
        % (len(bars.segs), time.time() - began))
    lat0 = math.radians((bbox[0] + bbox[2]) / 2.0)
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    return [g for g in gates
            if bars.crosses(g[0][0], g[0][1], g[1][0], g[1][1], kx, ky, 0.0)]


def main():
    global ENDPOINT
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="prep/barriers.geojson")
    ap.add_argument("--bbox", default=",".join("%g" % v for v in BBOX),
                    help="south,west,north,east")
    ap.add_argument("--tile", type=float, default=0.5,
                    help="degrees per Overpass request, before splitting")
    ap.add_argument("--min-area", type=float, default=100000.0,
                    help="m2 of water below which it is not a barrier")
    ap.add_argument("--tolerance", type=float, default=0.0004,
                    help="Douglas-Peucker tolerance in degrees (~40 m)")
    ap.add_argument("--gate-spacing", type=float, default=40.0,
                    help="m between kept bridge landings")
    ap.add_argument("--timeout", type=int, default=280,
                    help="seconds each Overpass query may take")
    ap.add_argument("--cache", default="tmp/overpass",
                    help="where responses are kept so an interrupted run "
                         "resumes instead of starting over")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--keep-dry-bridges", action="store_true",
                    help="keep bridges that cross no water we shipped")
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--ipv6", action="store_true",
                    help="allow IPv6; off by default, see force_ipv4")
    args = ap.parse_args()

    ENDPOINT = args.endpoint
    if not args.ipv6:
        force_ipv4()
    bbox = tuple(float(v) for v in args.bbox.split(","))
    cache_dir = None if args.no_cache else args.cache
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        log("cache: %s" % cache_dir)

    began = time.time()
    log("fetching water...")
    lines = fetch_water(bbox, args.tile, args.min_area, cache_dir, args.timeout)
    log("fetching coastline...")
    lines += fetch_coastline(bbox, args.tile, cache_dir, args.timeout)
    log("fetching bridges...")
    gates = dedupe_gates(fetch_gates(bbox, args.tile, cache_dir, args.timeout),
                         args.gate_spacing)

    raw_points = sum(len(l) for l in lines)
    lines = [simplify(l, args.tolerance) for l in lines]
    lines = [l for l in lines if len(l) >= 2]
    kept_points = sum(len(l) for l in lines)
    log("simplifying and filtering...")
    log("  %d lines, %d -> %d points" % (len(lines), raw_points, kept_points))

    # Overpass can answer politely and say nothing. Writing that out would
    # produce a valid, empty file that silently turns the water check off
    # again, so refuse rather than hand back a plausible-looking nothing.
    if not lines:
        raise SystemExit("no barriers found in %s -- refusing to write an "
                         "empty file" % args.bbox)

    before = len(gates)
    if not args.keep_dry_bridges:
        gates = gates_over_water(gates, lines, bbox)
    log("  %d of %d bridges cross water we kept" % (len(gates), before))
    if not gates:
        log("  WARNING: no bridges at all -- every crossing will be a wall")

    def rnd(v):
        return round(v, 5)          # ~1 m, and it halves the file

    doc = {
        "type": "FeatureCollection",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "OpenStreetMap contributors, ODbL, via Overpass",
        "bbox": list(bbox),
        "tolerance_deg": args.tolerance,
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
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    opener = gzip.open if args.out.endswith(".gz") else open
    with opener(args.out, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    size = os.path.getsize(args.out)
    log("wrote %s (%.1f MB, %d lines, %d bridges) in %.0fs"
        % (args.out, size / 1048576.0, len(lines), len(gates),
           time.time() - began))


if __name__ == "__main__":
    main()

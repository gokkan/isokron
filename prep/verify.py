#!/usr/bin/env python3
"""Reference implementation of the client search, for checking prep output.

    python prep/verify.py --stop Brunnsparken --time 08:00 --horizon 30

Prints the twenty stops reached last inside the horizon. If the answer looks
implausible -- Alingsås inside half an hour, or nothing past Korsvägen -- the
fault is in prep.py, not in the frontend. Keep this in step with the search in
public/app.js; it is the same algorithm written twice on purpose.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time

import numpy as np

EARTH_R = 6371008.8
INF = 1 << 30


def load(dirname):
    def js(name):
        with open(os.path.join(dirname, name), encoding="utf-8") as fh:
            return json.load(fh)

    def blob(name, header):
        raw = np.fromfile(os.path.join(dirname, name), dtype=np.uint8)
        out = {}
        for part in header["arrays"]:
            start = part["offset"]
            width = np.dtype(part["dtype"]).itemsize
            end = start + part["length"] * width
            out[part["name"]] = raw[start:end].view(part["dtype"])
        return out

    conn_h = js("connections.json")
    fp_h = js("footpaths.json")
    return {
        "meta": js("meta.json"),
        "stops": js("stops.json"),
        "trips": js("trips.json"),
        "conn_header": conn_h,
        "conn": blob("connections.bin", conn_h),
        "fp": blob("footpaths.bin", fp_h),
    }


ACCESS_DEFAULT_S = 600   # assumed walk from the click point to a first stop


def search(data, lat, lon, t0, horizon, origin_radius=None,
           walk_mps=1.3888889, min_change=60):
    """Trip-aware connection scan. Returns (arrival seconds, mode index)."""
    stops = data["stops"]
    n = stops["count"]
    conn = data["conn"]
    fp = data["fp"]
    trips = data["trips"]
    trip_route = trips["trip_route"]
    route_cat = trips["routes"]["category"]

    arr = np.full(n, INF, dtype=np.int64)
    mode = np.full(n, -1, dtype=np.int8)

    # How far someone will walk to a first stop is a preference, not a fact,
    # and it moves the answer more than anything else here.
    if not origin_radius:
        origin_radius = min(horizon, ACCESS_DEFAULT_S) * walk_mps
    lat_r = math.radians(lat)
    kx = EARTH_R * math.cos(lat_r) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    seeds = 0
    for i in range(n):
        dx = (stops["lon"][i] - lon) * kx
        dy = (stops["lat"][i] - lat) * ky
        d = math.hypot(dx, dy)
        if d <= origin_radius:
            arr[i] = t0 + int(math.ceil(d / walk_mps))
            seeds += 1

    limit = t0 + horizon
    c_from, c_to = conn["from"], conn["to"]
    c_dep, c_arr, c_trip = conn["dep"], conn["arr"], conn["trip"]
    boarded = np.zeros(len(trips["trip_route"]), dtype=bool)
    fp_off, fp_tgt, fp_sec = fp["offsets"], fp["targets"], fp["seconds"]

    start = int(np.searchsorted(c_dep, t0, side="left"))
    scanned = 0
    for c in range(start, len(c_dep)):
        dep = int(c_dep[c])
        if dep > limit:
            break
        scanned += 1
        trip = int(c_trip[c])
        src = int(c_from[c])
        if not boarded[trip]:
            if arr[src] + min_change > dep:
                continue
            boarded[trip] = True
        dst = int(c_to[c])
        a = int(c_arr[c])
        if a >= arr[dst]:
            continue
        arr[dst] = a
        mode[dst] = route_cat[trip_route[trip]]
        for e in range(int(fp_off[dst]), int(fp_off[dst + 1])):
            t = a + int(fp_sec[e])
            tgt = int(fp_tgt[e])
            if t < arr[tgt]:
                arr[tgt] = t
                mode[tgt] = mode[dst]
    return arr, mode, seeds, scanned


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="public/data")
    ap.add_argument("--stop", help="origin by stop name (first match)")
    ap.add_argument("--at", help="origin as lat,lon")
    ap.add_argument("--time", default="08:00")
    ap.add_argument("--horizon", type=int, default=30, help="minutes")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--access", type=int, default=ACCESS_DEFAULT_S // 60,
                    help="minutes you will walk to a first stop")
    ap.add_argument("--dump", help="write reached stops as JSON, for the "
                                   "cross-check against public/app.js")
    ap.add_argument("--expect", action="append", default=[],
                    help="substring that must appear among reached stops")
    ap.add_argument("--forbid", action="append", default=[],
                    help="substring that must not appear")
    args = ap.parse_args()

    data = load(args.data)
    stops = data["stops"]
    meta = data["meta"]
    header = data["conn_header"]

    if args.at:
        lat, lon = (float(v) for v in args.at.split(","))
        origin = args.at
    else:
        needle = (args.stop or "Brunnsparken").lower()
        hits = [i for i, nm in enumerate(stops["name"])
                if needle in nm.lower()]
        if not hits:
            raise SystemExit("no stop matching %r" % needle)
        lat, lon = stops["lat"][hits[0]], stops["lon"][hits[0]]
        origin = "%s (%.5f, %.5f)" % (stops["name"][hits[0]], lat, lon)

    h, m = args.time.split(":")
    abs_t = int(h) * 3600 + int(m) * 60
    t0 = abs_t - header["window_start"]
    horizon = args.horizon * 60
    if t0 < 0 or t0 + horizon > header["duration"]:
        raise SystemExit(
            "%s + %d min falls outside the prepared window %s-%s" % (
                args.time, args.horizon, meta["window_start"],
                meta["window_end"]))

    access = args.access * 60
    began = time.time()
    arr, mode, seeds, scanned = search(data, lat, lon, t0, horizon,
                                       origin_radius=min(horizon, access)
                                       * 1.3888889)
    elapsed = (time.time() - began) * 1000

    limit = t0 + horizon
    reached = [i for i in range(stops["count"]) if arr[i] <= limit]
    reached.sort(key=lambda i: arr[i])

    cats = data["trips"]["categories"]
    print("feed %s (%s), timetable for %s" % (
        meta["source"].split("(")[0].strip(), meta["generated_at"][:10],
        meta["service_date"]))
    print("from %s at %s, horizon %d min" % (origin, args.time, args.horizon))
    print("  seeded %d stops within %.0f m (%d min walk), scanned %d "
          "connections in %.0f ms"
          % (seeds, min(horizon, access) * 1.3888889, args.access, scanned,
             elapsed))
    print("  reached %d of %d stops" % (len(reached), stops["count"]))
    print()
    tail = reached[len(reached) - args.top:] if args.top > 0 else []
    print("  last %d reached:" % len(tail))
    for i in reversed(tail):
        minutes = (arr[i] - t0) / 60.0
        cat = cats[mode[i]] if mode[i] >= 0 else "walk"
        print("    %5.1f min  %-6s %s" % (minutes, cat, stops["name"][i]))

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump({"lat": lat, "lon": lon, "t0": t0, "horizon": horizon,
                       "access": access,
                       "stop": [int(i) for i in reached],
                       "arr": [int(arr[i]) for i in reached],
                       "mode": [int(mode[i]) for i in reached]}, fh)
        print("  wrote %s" % args.dump)

    ok = True
    names = " | ".join(stops["name"][i] for i in reached).lower()
    for want in args.expect:
        hit = want.lower() in names
        ok &= hit
        print("  %s expect %r" % ("PASS" if hit else "FAIL", want))
    for bad in args.forbid:
        hit = bad.lower() not in names
        ok &= hit
        print("  %s forbid %r" % ("PASS" if hit else "FAIL", bad))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

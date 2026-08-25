#!/usr/bin/env python3
"""Build the static isochrone dataset from a GTFS Regional feed.

    python prep/prep.py --zip vt.zip --out public/data

Emits, in --out:
    connections.bin/.json  struct-of-arrays, sorted ascending on departure
    footpaths.bin/.json    CSR walking graph
    stops.json             stop points in index order
    trips.json             route table + per-trip route index
    meta.json              what was built, from which feed, for which day
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import zipfile
from array import array
from collections import defaultdict
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import barriers as water
from gtfs import (CATEGORIES, Table, get, parse_time, pick_date,
                  resolve_services, route_category)

EARTH_R = 6371008.8
DAY = 86400
U16_MAX = 65535


def log(*a):
    print(*a, flush=True)


def hhmm(text):
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


def clock(sec):
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


# --------------------------------------------------------------------------
# feed -> connections
# --------------------------------------------------------------------------

def load_routes(zf):
    """route_id -> (short_name, category)."""
    routes = {}
    with Table(zf, "routes.txt") as t:
        i_id = t.need("route_id")
        i_short = t.maybe("route_short_name")
        i_long = t.maybe("route_long_name")
        i_type = t.need("route_type")
        for row in t:
            name = get(row, i_short) or get(row, i_long) or ""
            routes[row[i_id]] = (name, route_category(row[i_type]))
    return routes


def load_trips(zf, service_shift):
    """trip_id -> (route_id, shift) for trips running on the target day."""
    trips = {}
    with Table(zf, "trips.txt") as t:
        i_trip, i_route, i_service = t.need("trip_id", "route_id", "service_id")
        for row in t:
            shift = service_shift.get(row[i_service])
            if shift is None:
                continue
            trips[row[i_trip]] = (row[i_route], shift)
    return trips


def interpolate(times, n):
    """Fill None entries by linear interpolation on stop position.

    Returns None if the trip lacks a usable anchor at either end.
    """
    known = [i for i in range(n) if times[i] is not None]
    if len(known) < 2 or known[0] != 0 or known[-1] != n - 1:
        return None
    for a, b in zip(known, known[1:]):
        if b == a + 1:
            continue
        span = times[b] - times[a]
        for k in range(a + 1, b):
            times[k] = times[a] + round(span * (k - a) / (b - a))
    return times


def scan_stop_times(zf, trips, w_start, w_end):
    """Stream stop_times.txt and emit the connections inside the window."""
    per_trip = defaultdict(list)
    kept_rows = 0
    # csv hands back a fresh string per cell; a couple of million distinct
    # copies of a few tens of thousands of stop ids is worth deduplicating.
    seen_ids = {}
    with Table(zf, "stop_times.txt") as t:
        i_trip, i_stop, i_seq = t.need("trip_id", "stop_id", "stop_sequence")
        i_arr, i_dep = t.need("arrival_time", "departure_time")
        i_pick = t.maybe("pickup_type")
        i_drop = t.maybe("drop_off_type")
        for row in t:
            trip_id = row[i_trip]
            if trip_id not in trips:
                continue
            kept_rows += 1
            stop_id = row[i_stop]
            per_trip[trip_id].append((
                int(row[i_seq]),
                seen_ids.setdefault(stop_id, stop_id),
                parse_time(get(row, i_arr)),
                parse_time(get(row, i_dep)),
                get(row, i_pick) == "1",
                get(row, i_drop) == "1",
            ))
    log("  stop_times rows kept: {:,} over {:,} trips".format(
        kept_rows, len(per_trip)))

    stop_index = {}
    stop_ids = []
    trip_ids = []
    trip_route_ids = []

    c_from = array("i")
    c_to = array("i")
    c_dep = array("i")
    c_arr = array("i")
    c_trip = array("i")

    dropped_trips = 0
    dropped_conns = 0

    for trip_id, rows in per_trip.items():
        rows.sort(key=lambda r: r[0])
        n = len(rows)
        if n < 2:
            continue
        # A stop carrying only one of the two times uses it for both.
        arr_t = [r[2] if r[2] is not None else r[3] for r in rows]
        dep_t = [r[3] if r[3] is not None else r[2] for r in rows]
        if any(v is None for v in arr_t):
            filled = interpolate(list(arr_t), n)
            if filled is None:
                dropped_trips += 1
                continue
            for k in range(n):
                if arr_t[k] is None:
                    arr_t[k] = filled[k]
                if dep_t[k] is None:
                    dep_t[k] = filled[k]

        route_id, shift = trips[trip_id]
        local_trip = -1

        for k in range(n - 1):
            dep = dep_t[k] + shift
            if dep < w_start or dep > w_end:
                continue
            arr = arr_t[k + 1] + shift
            if arr < dep:
                continue
            if rows[k][4] or rows[k + 1][5]:
                continue  # no boarding here, or no alighting there
            if arr - w_start > U16_MAX:
                dropped_conns += 1
                continue
            if local_trip < 0:
                local_trip = len(trip_ids)
                trip_ids.append(trip_id)
                trip_route_ids.append(route_id)
            a = rows[k][1]
            b = rows[k + 1][1]
            ia = stop_index.get(a)
            if ia is None:
                ia = stop_index[a] = len(stop_ids)
                stop_ids.append(a)
            ib = stop_index.get(b)
            if ib is None:
                ib = stop_index[b] = len(stop_ids)
                stop_ids.append(b)
            c_from.append(ia)
            c_to.append(ib)
            c_dep.append(dep - w_start)
            c_arr.append(arr - w_start)
            c_trip.append(local_trip)

    stats = {"dropped_trips_no_times": dropped_trips,
             "dropped_connections_overflow": dropped_conns}
    arrays = (c_from, c_to, c_dep, c_arr, c_trip)
    return arrays, stop_ids, trip_ids, trip_route_ids, stats


# --------------------------------------------------------------------------
# stops and footpaths
# --------------------------------------------------------------------------

def load_stop_geometry(zf, stop_ids):
    """Geometry for the indexed stops, their stop areas, and parent -> children.

    Returns (info, areas, children). `areas` holds the location_type=1 rows,
    which is what a passenger means by "a stop": the sign on the street, not
    the two or three boarding points hanging off it.
    """
    wanted = set(stop_ids)
    info = {}
    areas = {}
    children = defaultdict(list)
    with Table(zf, "stops.txt") as t:
        i_id, i_name = t.need("stop_id", "stop_name")
        i_lat, i_lon = t.need("stop_lat", "stop_lon")
        i_parent = t.maybe("parent_station")
        i_loc = t.maybe("location_type")
        for row in t:
            sid = row[i_id]
            try:
                lat = float(row[i_lat])
                lon = float(row[i_lon])
            except (ValueError, IndexError):
                lat = lon = None
            if get(row, i_loc) == "1":
                areas[sid] = (row[i_name], lat, lon)
                continue
            parent = get(row, i_parent)
            if parent and sid in wanted:
                children[parent].append(sid)
            if sid not in wanted:
                continue
            info[sid] = (row[i_name], lat or 0.0, lon or 0.0, parent)
    missing = wanted - set(info)
    if missing:
        log("  WARNING: {} stop ids used by stop_times are absent from "
            "stops.txt".format(len(missing)))
    return info, areas, children


def build_groups(stop_ids, info, areas, names, lats, lons):
    """One display group per stop area; a parentless stop is its own group."""
    index = {}
    group_of = []
    g_name, g_lat, g_lon = [], [], []
    members = defaultdict(list)
    for i, sid in enumerate(stop_ids):
        key = info.get(sid, ("", 0.0, 0.0, ""))[3] or sid
        g = index.get(key)
        if g is None:
            g = index[key] = len(g_name)
            area = areas.get(key)
            g_name.append(area[0] if area else names[i])
            g_lat.append(area[1] if area else None)
            g_lon.append(area[2] if area else None)
        group_of.append(g)
        members[g].append(i)
    # A stop area without usable coordinates sits at the mean of its points.
    for g, idxs in members.items():
        if g_lat[g] is None or g_lon[g] is None:
            g_lat[g] = sum(lats[i] for i in idxs) / len(idxs)
            g_lon[g] = sum(lons[i] for i in idxs) / len(idxs)
    return group_of, g_name, g_lat, g_lon


def local_xy(lats, lons):
    """Equirectangular projection about the feed centroid, in metres."""
    lat0 = math.radians(sum(lats) / len(lats))
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    return [lon * kx for lon in lons], [lat * ky for lat in lats]


def build_barriers(path, lats, lons, reach, tol_deg):
    """Pack the committed water geometry down to what the walk can reach.

    A barrier further from every stop than the longest access walk can never
    block anything: both ends of that walk have to fit inside the same radius.
    Dropping those is what keeps the outer archipelago and half of Vanern out
    of the file without any judgement call about which water matters.
    """
    if not os.path.exists(path):
        log("  no barrier file at {} -- access stays as the crow flies"
            .format(path))
        return None
    lines, gates, source, fetched = water.load(path)
    raw_pts = sum(len(l) for l in lines)

    xs, ys = local_xy(lats, lons)
    cell = max(reach, 1.0)
    grid = defaultdict(list)
    for i in range(len(xs)):
        grid[(int(xs[i] // cell), int(ys[i] // cell))].append(i)
    lat0 = math.radians(sum(lats) / len(lats))
    kx = EARTH_R * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_R * math.pi / 180.0
    reach2 = reach * reach

    def near_stops(x, y):
        cx, cy = int(x // cell), int(y // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in grid.get((cx + dx, cy + dy), ()):
                    if (xs[i] - x) ** 2 + (ys[i] - y) ** 2 <= reach2:
                        return True
        return False

    def segment_matters(alon, alat, blon, blat):
        ax, ay = alon * kx, alat * ky
        bx, by = blon * kx, blat * ky
        span = math.hypot(bx - ax, by - ay)
        steps = max(1, int(span // (reach / 2.0)) + 1)
        for k in range(steps + 1):
            f = k / steps
            if near_stops(ax + (bx - ax) * f, ay + (by - ay) * f):
                return True
        return False

    kept_lines = []
    for line in lines:
        pts = water.simplify(line, tol_deg)
        run = []
        for k in range(len(pts) - 1):
            if segment_matters(pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1]):
                if not run:
                    run.append(pts[k])
                run.append(pts[k + 1])
            elif run:
                kept_lines.append(run)
                run = []
        if run:
            kept_lines.append(run)

    kept_gates = [g for g in gates
                  if near_stops(g[0][0] * kx, g[0][1] * ky)
                  or near_stops(g[1][0] * kx, g[1][1] * ky)]

    if not kept_lines:
        log("  barrier file has nothing within {:g} m of a stop".format(reach))
        return None

    offsets = [0]
    lon_out, lat_out = [], []
    for line in kept_lines:
        for x, y in line:
            lon_out.append(x)
            lat_out.append(y)
        offsets.append(len(lon_out))
    log("  barriers: {:,} lines, {:,} of {:,} points, {:,} of {:,} bridges"
        .format(len(kept_lines), len(lon_out), raw_pts,
                len(kept_gates), len(gates)))

    packed = {
        "lon": np.asarray(lon_out, dtype=np.float32),
        "lat": np.asarray(lat_out, dtype=np.float32),
        "offsets": np.asarray(offsets, dtype=np.uint32),
        "gate_a_lon": np.asarray([g[0][0] for g in kept_gates], np.float32),
        "gate_a_lat": np.asarray([g[0][1] for g in kept_gates], np.float32),
        "gate_b_lon": np.asarray([g[1][0] for g in kept_gates], np.float32),
        "gate_b_lat": np.asarray([g[1][1] for g in kept_gates], np.float32),
        "gate_len": np.asarray([g[2] for g in kept_gates], np.float32),
    }
    # Build the checker from the float32 values that will be shipped, not the
    # float64 ones that were read. prep.py and the browser then agree on the
    # same rounded coordinates, and the cross-check has a chance.
    bars = water.Barriers(
        packed["lon"], packed["lat"], packed["offsets"],
        packed["gate_a_lon"], packed["gate_a_lat"],
        packed["gate_b_lon"], packed["gate_b_lat"], packed["gate_len"])
    return packed, bars, {"source": source, "fetched_at": fetched,
                          "lines": len(kept_lines), "points": len(lon_out),
                          "gates": len(kept_gates), "reach_m": reach,
                          "tolerance_deg": tol_deg,
                          "shore_slack_m": water.SHORE_SLACK_M}


def build_footpaths(zf, stop_ids, lats, lons, children, max_dist, walk_mps,
                    default_transfer, bars=None):
    """Merge transfers.txt with generated near pairs into a CSR graph.

    Generated pairs are subject to the water check: Gota alv is narrower than
    the transfer radius in places, so without it a journey can step across the
    river between two platforms that share nothing but a coordinate. Rows from
    transfers.txt are exempt -- the operator saying a transfer exists outranks
    our geometry.
    """
    n = len(stop_ids)
    index = {sid: i for i, sid in enumerate(stop_ids)}
    xs, ys = local_xy(lats, lons)
    nbrs = [dict() for _ in range(n)]

    def offer(i, j, seconds):
        if i == j:
            return
        cur = nbrs[i].get(j)
        if cur is None or seconds < cur:
            nbrs[i][j] = seconds

    # generated pairs, grid-bucketed so this stays linear in stop count
    cell = max(max_dist, 1.0)
    grid = defaultdict(list)
    for i in range(n):
        grid[(int(xs[i] // cell), int(ys[i] // cell))].append(i)
    limit2 = max_dist * max_dist
    generated = 0
    blocked = 0
    kx_ll = EARTH_R * math.cos(math.radians(sum(lats) / len(lats))) \
        * math.pi / 180.0
    ky_ll = EARTH_R * math.pi / 180.0
    for (cx, cy), bucket in grid.items():
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                near.extend(grid.get((cx + dx, cy + dy), ()))
        for i in bucket:
            xi, yi = xs[i], ys[i]
            for j in near:
                if j <= i:
                    continue
                d2 = (xs[j] - xi) ** 2 + (ys[j] - yi) ** 2
                if d2 > limit2:
                    continue
                dist = math.sqrt(d2)
                if bars is not None:
                    dist = water.walk_distance(
                        bars, lons[i], lats[i], lons[j], lats[j],
                        max_dist, kx_ll, ky_ll)
                    if dist is None:
                        blocked += 1
                        continue
                secs = int(math.ceil(dist / walk_mps))
                offer(i, j, secs)
                offer(j, i, secs)
                generated += 1
    log("  generated {:,} walk pairs within {:g} m".format(generated, max_dist))
    if bars is not None:
        log("  {:,} pairs dropped: water in the way".format(blocked))

    # transfers.txt wins wherever it is more specific
    applied = 0
    if "transfers.txt" in set(zf.namelist()):
        with Table(zf, "transfers.txt") as t:
            i_from = t.maybe("from_stop_id")
            i_to = t.maybe("to_stop_id")
            i_type = t.maybe("transfer_type")
            i_min = t.maybe("min_transfer_time")
            if i_from is not None and i_to is not None:
                for row in t:
                    ttype = get(row, i_type) or "0"
                    if ttype == "3":
                        continue  # transfer not possible
                    raw = get(row, i_min)
                    try:
                        secs = int(raw) if raw else default_transfer
                    except ValueError:
                        secs = default_transfer
                    if ttype == "1":
                        secs = min(secs, default_transfer)
                    a = get(row, i_from)
                    b = get(row, i_to)
                    # entries may name stop areas; expand to their stop points
                    srcs = [a] if a in index else children.get(a, ())
                    dsts = [b] if b in index else children.get(b, ())
                    for sa in srcs:
                        for sb in dsts:
                            offer(index[sa], index[sb], secs)
                            applied += 1
    log("  applied {:,} entries from transfers.txt".format(applied))

    offsets = np.zeros(n + 1, dtype=np.uint32)
    total = sum(len(d) for d in nbrs)
    targets = np.zeros(total, dtype=np.uint32)
    seconds = np.zeros(total, dtype=np.uint16)
    pos = 0
    for i in range(n):
        offsets[i] = pos
        for j in sorted(nbrs[i]):
            targets[pos] = j
            seconds[pos] = min(nbrs[i][j], U16_MAX)
            pos += 1
    offsets[n] = pos
    return offsets, targets, seconds


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def write_blob(path, chunks, do_gzip):
    """Concatenate typed arrays into one buffer; return a header for each."""
    header = []
    offset = 0
    payload = bytearray()
    for name, arr in chunks:
        data = arr.tobytes()
        header.append({"name": name, "offset": offset,
                       "length": int(arr.size), "dtype": arr.dtype.name})
        payload += data
        offset += len(data)
    with open(path, "wb") as fh:
        fh.write(payload)
    if do_gzip:
        with gzip.open(path + ".gz", "wb", compresslevel=9) as fh:
            fh.write(payload)
    return header, len(payload)


def write_json(path, obj, do_gzip):
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if do_gzip:
        with gzip.open(path + ".gz", "wt", encoding="utf-8",
                       compresslevel=9) as fh:
            fh.write(text)


def human(n):
    return "%.2f MB" % (n / 1048576.0)


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", required=True, help="GTFS archive")
    ap.add_argument("--out", default="public/data")
    ap.add_argument("--date", default="auto",
                    help="YYYY-MM-DD, or 'auto' for the next ordinary Tuesday")
    ap.add_argument("--window-start", default="05:00")
    ap.add_argument("--window-end", default="22:00",
                    help="last departure kept; must exceed the latest "
                         "selectable start time by the longest horizon the "
                         "frontend offers, or late departures get truncated "
                         "against the data edge")
    ap.add_argument("--walk-speed", type=float, default=5.0, help="km/h")
    ap.add_argument("--transfer-radius", type=float, default=400.0,
                    help="metres")
    ap.add_argument("--default-transfer", type=int, default=120,
                    help="seconds for a transfers.txt entry with no time")
    ap.add_argument("--barriers", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "barriers.geojson"),
        help="water geometry to check walks against; absent is not an error")
    ap.add_argument("--no-barriers", action="store_true",
                    help="ignore the water check entirely, as the crow flies")
    ap.add_argument("--barrier-reach", type=float, default=1700.0,
                    help="metres; barriers further than this from every stop "
                         "cannot block any walk and are dropped")
    ap.add_argument("--barrier-tolerance", type=float, default=0.0004,
                    help="Douglas-Peucker tolerance in degrees, about 40 m")
    ap.add_argument("--no-gzip", action="store_true")
    args = ap.parse_args(argv)

    do_gzip = not args.no_gzip
    walk_mps = args.walk_speed * 1000.0 / 3600.0
    w_start = hhmm(args.window_start)
    w_end = hhmm(args.window_end)
    if w_end <= w_start:
        raise SystemExit("--window-end must be after --window-start")
    if w_end - w_start > U16_MAX:
        raise SystemExit("window too long for 16-bit second offsets")

    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()

    with zipfile.ZipFile(args.zip) as zf:
        log("feed: {} ({})".format(args.zip, human(os.path.getsize(args.zip))))

        if args.date == "auto":
            log("choosing service date...")
            target = pick_date(zf, date.today(), weekday=1, log=log)
        else:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        prev = date.fromordinal(target.toordinal() - 1)
        log("service date: {} ({}), window {}-{}".format(
            target, target.strftime("%A"), clock(w_start), clock(w_end)))

        services_today = resolve_services(zf, target)
        services_prev = resolve_services(zf, prev)
        # Trips belonging to the previous service day can still run past
        # 24:00:00 into our window; shift them so all times share one origin.
        service_shift = {sid: 0 for sid in services_today}
        for sid in services_prev:
            service_shift.setdefault(sid, -DAY)
        log("  services: {:,} on the day, {:,} carried over from {}".format(
            len(services_today), len(services_prev), prev))

        routes = load_routes(zf)
        trips = load_trips(zf, service_shift)
        log("  routes: {:,}   trips running: {:,}".format(
            len(routes), len(trips)))

        log("scanning stop_times...")
        arrays, stop_ids, trip_ids, trip_route_ids, stats = scan_stop_times(
            zf, trips, w_start, w_end)
        c_from, c_to, c_dep, c_arr, c_trip = arrays
        n_conn = len(c_from)
        if n_conn == 0:
            raise SystemExit("no connections in window; wrong date or window?")
        log("  connections: {:,}  stops: {:,}  trips in window: {:,}".format(
            n_conn, len(stop_ids), len(trip_ids)))
        for k, v in stats.items():
            if v:
                log("  {}: {:,}".format(k, v))

        info, areas, children = load_stop_geometry(zf, stop_ids)
        blank = ("", 0.0, 0.0, "")
        names = [info.get(s, (s,) + blank[1:])[0] for s in stop_ids]
        lats = [info.get(s, blank)[1] for s in stop_ids]
        lons = [info.get(s, blank)[2] for s in stop_ids]
        group_of, g_name, g_lat, g_lon = build_groups(
            stop_ids, info, areas, names, lats, lons)
        log("  {:,} stop points group into {:,} stop areas".format(
            len(stop_ids), len(g_name)))

        log("reading barriers...")
        built = None if args.no_barriers else build_barriers(
            args.barriers, lats, lons, args.barrier_reach,
            args.barrier_tolerance)
        barrier_arrays, bars, barrier_meta = built or (None, None, None)

        log("building footpaths...")
        fp_offsets, fp_targets, fp_seconds = build_footpaths(
            zf, stop_ids, lats, lons, children, args.transfer_radius,
            walk_mps, args.default_transfer, bars)
        log("  footpath edges: {:,} (avg {:.1f} per stop)".format(
            len(fp_targets), len(fp_targets) / max(len(stop_ids), 1)))

    # sort connections by departure -- the scan algorithm depends on it
    log("sorting connections...")
    dep = np.frombuffer(c_dep, dtype=np.int32)
    order = np.argsort(dep, kind="stable")
    n_from = np.frombuffer(c_from, dtype=np.int32)[order].astype(np.uint32)
    n_to = np.frombuffer(c_to, dtype=np.int32)[order].astype(np.uint32)
    n_trip = np.frombuffer(c_trip, dtype=np.int32)[order].astype(np.uint32)
    n_dep = dep[order].astype(np.uint16)
    n_arr = np.frombuffer(c_arr, dtype=np.int32)[order].astype(np.uint16)
    assert np.all(np.diff(n_dep.astype(np.int32)) >= 0), "sort failed"

    # deduplicated route table, referenced per trip
    route_index = {}
    route_names = []
    route_cats = []
    trip_route = []
    for rid in trip_route_ids:
        ri = route_index.get(rid)
        if ri is None:
            ri = route_index[rid] = len(route_names)
            name, cat = routes.get(rid, ("", CATEGORIES.index("other")))
            route_names.append(name)
            route_cats.append(cat)
        trip_route.append(ri)

    log("writing...")
    out = args.out
    conn_header, conn_bytes = write_blob(
        os.path.join(out, "connections.bin"),
        [("from", n_from), ("to", n_to), ("trip", n_trip),
         ("dep", n_dep), ("arr", n_arr)], do_gzip)
    fp_header, fp_bytes = write_blob(
        os.path.join(out, "footpaths.bin"),
        [("offsets", fp_offsets), ("targets", fp_targets),
         ("seconds", fp_seconds)], do_gzip)

    write_json(os.path.join(out, "connections.json"), {
        "count": int(n_conn), "bytes": conn_bytes, "arrays": conn_header,
        "window_start": w_start, "window_end": w_end,
        "duration": w_end - w_start,
    }, do_gzip)
    write_json(os.path.join(out, "footpaths.json"), {
        "stops": len(stop_ids), "edges": int(len(fp_targets)),
        "bytes": fp_bytes, "arrays": fp_header,
    }, do_gzip)
    if barrier_arrays is not None:
        bar_header, bar_bytes = write_blob(
            os.path.join(out, "barriers.bin"),
            [(name, barrier_arrays[name]) for name in
             ("lon", "lat", "offsets", "gate_a_lon", "gate_a_lat",
              "gate_b_lon", "gate_b_lat", "gate_len")], do_gzip)
        write_json(os.path.join(out, "barriers.json"), dict(
            barrier_meta, bytes=bar_bytes, arrays=bar_header), do_gzip)
    write_json(os.path.join(out, "stops.json"), {
        "count": len(stop_ids),
        "id": stop_ids,
        "name": names,
        "lat": [round(v, 6) for v in lats],
        "lon": [round(v, 6) for v in lons],
        # Search runs on stop points; drawing does not. One dot per stop area,
        # or every stop appears two or three times over.
        "group": group_of,
        "groups": {
            "count": len(g_name),
            "name": g_name,
            "lat": [round(v, 6) for v in g_lat],
            "lon": [round(v, 6) for v in g_lon],
        },
    }, do_gzip)
    write_json(os.path.join(out, "trips.json"), {
        "categories": CATEGORIES,
        "routes": {"short_name": route_names, "category": route_cats},
        "trip_route": trip_route,
    }, do_gzip)
    write_json(os.path.join(out, "meta.json"), {
        "service_date": target.isoformat(),
        "weekday": target.strftime("%A"),
        "window_start": args.window_start,
        "window_end": args.window_end,
        "walk_speed_kmh": args.walk_speed,
        "transfer_radius_m": args.transfer_radius,
        "barriers": barrier_meta,
        "generated_at": datetime.now(timezone.utc).replace(
            microsecond=0).isoformat(),
        "counts": {"stops": len(stop_ids), "stop_areas": len(g_name),
                   "connections": int(n_conn), "trips": len(trip_ids),
                   "routes": len(route_names),
                   "footpath_edges": int(len(fp_targets))},
        "source": "Trafiklab GTFS Regional (Samtrafiken), operator vt, CC0",
    }, do_gzip)

    total_raw = 0
    total_gz = 0
    for fn in sorted(os.listdir(out)):
        if fn.endswith(".gz"):
            continue
        p = os.path.join(out, fn)
        raw = os.path.getsize(p)
        gz = os.path.getsize(p + ".gz") if os.path.exists(p + ".gz") else raw
        total_raw += raw
        total_gz += gz
        log("  {:22s} {:>10s}   gz {:>10s}".format(fn, human(raw), human(gz)))
    log("  {:22s} {:>10s}   gz {:>10s}".format(
        "TOTAL", human(total_raw), human(total_gz)))
    if total_gz > 10 * 1048576:
        log("  WARNING: over the 10 MB gzipped budget -- narrow the window")
    log("done in {:.1f}s".format(time.time() - t0))


if __name__ == "__main__":
    main()

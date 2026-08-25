#!/usr/bin/env python3
"""Water barriers, and the walk check that uses them.

A barrier is a polyline you cannot walk across: a shoreline, a river bank, a
lake edge. A gate is a place where you can -- a bridge -- and it is carried as
what it physically is: two landings and the length between them. A bridge
modelled as a single point in midstream would be useless, because the walk to
that point crosses the near bank first, and only lines up with the bridge if
you already stand directly in line with it.

Two rules, and the second is the one that is easy to miss:

  1. A walk is blocked if the straight line between its ends crosses a barrier.
     Blocked, you may instead reach a bridge landing, pay the bridge, and go
     on from the far landing -- both legs judged by the same rule.
  2. A crossing within SHORE_SLACK of an end does not count. Feed coordinates
     put a quayside stop a few metres into the water often enough that without
     this, stops along Stenpiren and Lindholmen would be unreachable from
     every direction at once -- a silent, total failure. Slack at one end
     never rescues a real crossing: the far bank is hundreds of metres from
     both ends.

Crossing tests run in degrees, not metres. Segment-segment intersection
survives any affine map and lon/lat -> local metres is a diagonal scaling, so
the answer is identical and no projection has to be agreed on. Distances are
the one thing that needs metres, and those use the caller's kx/ky -- the same
equirectangular factors the search already computes about its own origin.

Kept in step with the barrier section of public/app.js; the two are the same
rules written twice, like the search itself. The index differs on purpose --
Python buckets into a grid because prep.py asks about fifteen thousand stops,
the browser scans flat because it asks once per click -- but an index only
narrows which segments get tested, never which of them cross.
"""
from __future__ import annotations

import gzip
import json
import math

# Grid cells for the segment index, roughly 550 m square in this latitude
# band -- the scale of the queries: a 400 m transfer, a 1.7 km access walk.
CELL_LAT = 0.005
CELL_LON = 0.009

# How far from an end of the walk a crossing stops counting. Rule 2 above.
SHORE_SLACK_M = 35.0


def seg_hit(ax, ay, bx, by, cx, cy, dx, dy):
    """Where a-b crosses c-d, as a fraction along a-b, or -1 for no crossing.

    Proper crossings only: segments that merely touch at an endpoint do not
    count, and neither does collinear overlap -- that is walking along a bank,
    not across it.
    """
    r_x, r_y = bx - ax, by - ay
    s_x, s_y = dx - cx, dy - cy
    denom = r_x * s_y - r_y * s_x
    if denom == 0.0:
        return -1.0
    qp_x, qp_y = cx - ax, cy - ay
    t = (qp_x * s_y - qp_y * s_x) / denom
    if t <= 0.0 or t >= 1.0:
        return -1.0
    u = (qp_x * r_y - qp_y * r_x) / denom
    if u <= 0.0 or u >= 1.0:
        return -1.0
    return t


class Barriers:
    """Barrier polylines plus bridges, with a uniform grid over the segments.

    Gates are five parallel lists: the two landings and the metres of bridge
    between them. Both directions are usable; which landing you arrive at is
    decided per walk.
    """

    def __init__(self, lon, lat, offsets,
                 gate_a_lon, gate_a_lat, gate_b_lon, gate_b_lat, gate_len):
        self.lon = [float(v) for v in lon]
        self.lat = [float(v) for v in lat]
        self.offsets = [int(v) for v in offsets]
        self.gate_a_lon = [float(v) for v in gate_a_lon]
        self.gate_a_lat = [float(v) for v in gate_a_lat]
        self.gate_b_lon = [float(v) for v in gate_b_lon]
        self.gate_b_lat = [float(v) for v in gate_b_lat]
        self.gate_len = [float(v) for v in gate_len]

        # a segment is named by the index of its first point
        segs = []
        for line in range(len(self.offsets) - 1):
            for p in range(self.offsets[line], self.offsets[line + 1] - 1):
                segs.append(p)
        self.segs = segs

        counts = {}
        for p in segs:
            for key in self._cells(self.lon[p], self.lat[p],
                                   self.lon[p + 1], self.lat[p + 1]):
                counts[key] = counts.get(key, 0) + 1
        self.cell_at = {}
        starts = []
        total = 0
        for key, c in counts.items():
            self.cell_at[key] = len(starts)
            starts.append(total)
            total += c
        self.grid_start = starts + [total]
        self.grid_items = [0] * total
        fill = list(starts)
        for si, p in enumerate(segs):
            for key in self._cells(self.lon[p], self.lat[p],
                                   self.lon[p + 1], self.lat[p + 1]):
                c = self.cell_at[key]
                self.grid_items[fill[c]] = si
                fill[c] += 1

    @staticmethod
    def _cells(alon, alat, blon, blat):
        cx0 = int(math.floor(min(alon, blon) / CELL_LON))
        cx1 = int(math.floor(max(alon, blon) / CELL_LON))
        cy0 = int(math.floor(min(alat, blat) / CELL_LAT))
        cy1 = int(math.floor(max(alat, blat) / CELL_LAT))
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                yield (cx, cy)

    def crosses(self, alon, alat, blon, blat, kx, ky, slack=SHORE_SLACK_M):
        """True if walking straight from a to b means crossing water."""
        if not self.segs:
            return False
        slack2 = slack * slack
        seen = set()
        for key in self._cells(alon, alat, blon, blat):
            c = self.cell_at.get(key)
            if c is None:
                continue
            for k in range(self.grid_start[c], self.grid_start[c + 1]):
                si = self.grid_items[k]
                if si in seen:
                    continue
                seen.add(si)
                p = self.segs[si]
                t = seg_hit(alon, alat, blon, blat,
                            self.lon[p], self.lat[p],
                            self.lon[p + 1], self.lat[p + 1])
                if t < 0.0:
                    continue
                if slack2 > 0.0:
                    hx = alon + (blon - alon) * t
                    hy = alat + (blat - alat) * t
                    dax, day = (alon - hx) * kx, (alat - hy) * ky
                    if dax * dax + day * day <= slack2:
                        continue
                    dbx, dby = (blon - hx) * kx, (blat - hy) * ky
                    if dbx * dbx + dby * dby <= slack2:
                        continue
                return True
        return False

    def exits(self, lon, lat, budget, kx, ky, slack=SHORE_SLACK_M):
        """Where a blocked walk can pick up again, having paid for a bridge.

        One entry per usable bridge direction: the far landing, and the metres
        already spent getting to the near landing and over. Sorted by cost,
        index second, because the browser sorts the same way and a tie has to
        break identically or the two answers drift apart. Nearly always empty.
        """
        out = []
        for gi in range(len(self.gate_len)):
            for near, far in ((0, 1), (1, 0)):
                nlon = self.gate_a_lon[gi] if near == 0 else self.gate_b_lon[gi]
                nlat = self.gate_a_lat[gi] if near == 0 else self.gate_b_lat[gi]
                flon = self.gate_a_lon[gi] if far == 0 else self.gate_b_lon[gi]
                flat = self.gate_a_lat[gi] if far == 0 else self.gate_b_lat[gi]
                cost = math.hypot((nlon - lon) * kx, (nlat - lat) * ky) \
                    + self.gate_len[gi]
                if cost > budget:
                    continue
                if self.crosses(lon, lat, nlon, nlat, kx, ky, slack):
                    continue
                out.append((cost, gi * 2 + near, flon, flat))
        out.sort(key=lambda e: (e[0], e[1]))
        return out


def walk_distance(barriers, alon, alat, blon, blat, budget, kx, ky,
                  exits=None, slack=SHORE_SLACK_M):
    """Metres on foot from a to b, or None if there is no way inside budget.

    Straight-line unless water is in the way, and then over one bridge. Two
    bridges in one walk is not searched for: inside a twenty minute budget
    that does not happen.
    """
    direct = math.hypot((blon - alon) * kx, (blat - alat) * ky)
    if direct > budget:
        return None
    if barriers is None:
        return direct
    if not barriers.crosses(alon, alat, blon, blat, kx, ky, slack):
        return direct
    if exits is None:
        exits = barriers.exits(alon, alat, budget, kx, ky, slack)
    best = None
    for cost, _rank, flon, flat in exits:
        if cost > budget or (best is not None and cost >= best):
            break                          # sorted, so nothing later is cheaper
        total = cost + math.hypot((blon - flon) * kx, (blat - flat) * ky)
        if total > budget or (best is not None and total >= best):
            continue
        if barriers.crosses(flon, flat, blon, blat, kx, ky, slack):
            continue
        best = total
    return best


def load(path):
    """Read the committed GeoJSON.

    Barriers are LineStrings; a bridge is a LineString of exactly two points
    tagged kind=gate, carrying its walked length in metres.
    """
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    lines = []
    gates = []
    for feat in doc.get("features", []):
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        coords = geom.get("coordinates") or []
        parts = [coords] if geom.get("type") == "LineString" else (
            coords if geom.get("type") == "MultiLineString" else [])
        for part in parts:
            if len(part) < 2:
                continue
            pts = [(float(c[0]), float(c[1])) for c in part]
            if props.get("kind") == "gate":
                span = math.hypot(pts[-1][0] - pts[0][0],
                                  pts[-1][1] - pts[0][1])
                gates.append((pts[0], pts[-1],
                              float(props.get("len", 0.0)) or span * 111000.0))
            else:
                lines.append(pts)
    return lines, gates, doc.get("source", ""), doc.get("generated_at", "")


def simplify(points, tol_deg):
    """Douglas-Peucker, iterative so a long shoreline cannot blow the stack."""
    n = len(points)
    if n <= 2 or tol_deg <= 0:
        return list(points)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i]
        bx, by = points[j]
        ux, uy = bx - ax, by - ay
        norm = math.hypot(ux, uy)
        worst = -1.0
        at = -1
        for k in range(i + 1, j):
            px, py = points[k]
            if norm == 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(ux * (py - ay) - uy * (px - ax)) / norm
            if d > worst:
                worst, at = d, k
        if worst > tol_deg and at > i:
            keep[at] = True
            stack.append((i, at))
            stack.append((at, j))
    return [points[k] for k in range(n) if keep[k]]

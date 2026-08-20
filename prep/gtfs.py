"""Small helpers for streaming a GTFS zip archive."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, timedelta

# Route type -> coarse category. Covers both classic (0-12) and the extended
# (Hierarchical Vehicle Type) codes that GTFS Regional actually uses.
CATEGORIES = ["tram", "bus", "rail", "ferry", "other"]
CAT_TRAM, CAT_BUS, CAT_RAIL, CAT_FERRY, CAT_OTHER = range(5)

_CLASSIC = {
    0: CAT_TRAM,
    1: CAT_RAIL,      # metro; Vasttrafik has none, group with rail
    2: CAT_RAIL,
    3: CAT_BUS,
    4: CAT_FERRY,
    5: CAT_TRAM,      # cable tram
    6: CAT_OTHER,     # aerial lift
    7: CAT_OTHER,     # funicular
    11: CAT_BUS,      # trolleybus
    12: CAT_OTHER,    # monorail
}

# Extended ranges, lowest bound first.
_EXTENDED = [
    (100, 199, CAT_RAIL),
    (200, 299, CAT_BUS),
    (400, 499, CAT_RAIL),   # urban railway / metro
    (700, 799, CAT_BUS),
    (800, 899, CAT_BUS),    # trolleybus
    (900, 999, CAT_TRAM),
    (1000, 1099, CAT_FERRY),
    (1100, 1199, CAT_OTHER),  # air
    (1200, 1299, CAT_FERRY),
    (1300, 1399, CAT_OTHER),  # aerial lift
    (1400, 1499, CAT_OTHER),  # funicular
    (1500, 1599, CAT_OTHER),  # taxi
    (1700, 1799, CAT_OTHER),
]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday"]


def route_category(route_type: str) -> int:
    try:
        rt = int(route_type)
    except (TypeError, ValueError):
        return CAT_OTHER
    if rt in _CLASSIC:
        return _CLASSIC[rt]
    for lo, hi, cat in _EXTENDED:
        if lo <= rt <= hi:
            return cat
    return CAT_OTHER


def parse_time(value: str):
    """GTFS time -> seconds since noon-minus-12h. Handles 25:13:00."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


class Table:
    """Streaming reader for one member of a GTFS zip."""

    def __init__(self, zf: zipfile.ZipFile, name: str):
        self._raw = zf.open(name, "r")
        self._fh = io.TextIOWrapper(self._raw, encoding="utf-8-sig", newline="")
        self.reader = csv.reader(self._fh)
        header = next(self.reader, [])
        self.idx = {h.strip(): i for i, h in enumerate(header)}
        self.width = len(header)

    def need(self, *names: str):
        """Column indices; raises if a required column is absent."""
        out = []
        for n in names:
            if n not in self.idx:
                raise KeyError(f"missing column {n!r}")
            out.append(self.idx[n])
        return out[0] if len(out) == 1 else tuple(out)

    def maybe(self, name: str):
        return self.idx.get(name)

    def __iter__(self):
        return iter(self.reader)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False


def get(row, index, default=""):
    """Column value tolerant of short rows and absent columns."""
    if index is None or index >= len(row):
        return default
    return row[index]


def resolve_services(zf: zipfile.ZipFile, day: date) -> set:
    """Service ids running on `day`.

    GTFS Regional leaves calendar.txt weekday flags at 0 and puts every real
    traffic day in calendar_dates.txt, but we honour both so the code also
    works on a conventional feed.
    """
    ymd = day.strftime("%Y%m%d")
    weekday = WEEKDAYS[day.weekday()]
    names = set(zf.namelist())
    active = set()

    if "calendar.txt" in names:
        with Table(zf, "calendar.txt") as t:
            i_sid = t.need("service_id")
            i_wd = t.maybe(weekday)
            i_start, i_end = t.need("start_date", "end_date")
            for row in t:
                if get(row, i_wd) != "1":
                    continue
                if get(row, i_start) <= ymd <= get(row, i_end):
                    active.add(row[i_sid])

    if "calendar_dates.txt" in names:
        with Table(zf, "calendar_dates.txt") as t:
            i_sid, i_date, i_exc = t.need("service_id", "date", "exception_type")
            for row in t:
                if get(row, i_date) != ymd:
                    continue
                if row[i_exc] == "1":
                    active.add(row[i_sid])
                elif row[i_exc] == "2":
                    active.discard(row[i_sid])

    return active


def pick_date(zf: zipfile.ZipFile, today: date, weekday: int = 1,
              lookahead_weeks: int = 5, log=lambda *a: None) -> date:
    """First upcoming `weekday` with a full-looking service roster.

    Picking blindly would land on a holiday sooner or later, and a red-day
    timetable is not what the map claims to show. Compare the candidates and
    take the earliest one close to the best.
    """
    days_ahead = (weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    candidates = [today + timedelta(days=days_ahead + 7 * w)
                  for w in range(lookahead_weeks)]
    counts = [(d, len(resolve_services(zf, d))) for d in candidates]
    for d, n in counts:
        log(f"  candidate {d} ({WEEKDAYS[d.weekday()]}): {n} services")
    best = max(n for _, n in counts)
    if best == 0:
        raise SystemExit("no candidate date has any service; feed out of range?")
    # Ordinary weekdays in the vt feed sit anywhere from 0.83 to 1.0 of the
    # best week, purely from school traffic ramping up through the term; a
    # weekend, and so a red day, runs at 0.69 or less. 0.8 separates the two
    # with room on both sides. Tighten it and the search starts rejecting
    # perfectly normal Tuesdays for being slightly quiet.
    for d, n in counts:
        if n >= 0.8 * best:
            return d
    return counts[0][0]

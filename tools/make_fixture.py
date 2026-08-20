#!/usr/bin/env python3
"""Write a small synthetic GTFS zip that imitates the VT feed's quirks.

Lets prep.py, verify.py and the frontend be exercised end to end without a
Trafiklab key. Deliberately reproduces the traps the real feed sets:

  * calendar.txt carries only validity ranges, weekday flags are 0
  * every actual traffic day sits in calendar_dates.txt
  * one trip runs on the previous service day with times past 24:00:00
  * extended route types (900 tram, 700 bus, 100 rail, 1000 ferry)
  * stop points hanging off stop areas via parent_station
  * transfers.txt links two stops further apart than the walk radius
  * one intermediate stop has blank times and must be interpolated

    python tools/make_fixture.py --out fixture/vt.zip
"""
from __future__ import annotations

import argparse
import io
import os
import zipfile
from datetime import date, timedelta

# name -> (lat, lon, stop area)
STOPS = {
    "Brunnsparken":        (57.70724, 11.96683, "A_BRUNN"),
    "Nils Ericsonsterm.":  (57.70890, 11.97370, "A_NILS"),
    "Göteborg C":          (57.70870, 11.97290, "A_NILS"),
    "Grönsakstorget":      (57.70120, 11.96420, "A_GRONS"),
    "Järntorget":          (57.69940, 11.95250, "A_JARN"),
    "Prinsgatan":          (57.69430, 11.94900, "A_PRINS"),
    "Linnéplatsen":        (57.68840, 11.94630, "A_LINNE"),
    "Botaniska Trädgården": (57.68260, 11.95040, "A_BOTAN"),
    "Axel Dahlströms Torg": (57.66980, 11.93470, "A_AXEL"),
    "Frölunda Torg":       (57.65190, 11.91260, "A_FROL"),
    "Korsvägen":           (57.69660, 11.98700, "A_KORSV"),
    "Almedal":             (57.67590, 11.99500, "A_ALMED"),
    "Krokslätts Fabriker": (57.66620, 12.00570, "A_KROKS"),
    "Mölndals Innerstad":  (57.65600, 12.01400, "A_MOLND"),
    "Centralstationen":    (57.70830, 11.97210, "A_CENTR"),
    "Gamlestads Torg":     (57.72730, 12.00120, "A_GAMLE"),
    "Hjällbo":             (57.77070, 12.01500, "A_HJALL"),
    "Angered Centrum":     (57.79600, 12.04300, "A_ANGER"),
    "Munkebäckstorget":    (57.72240, 12.03150, "A_MUNKE"),
    "Partille Centrum":    (57.73950, 12.10600, "A_PARTI"),
    "Kungälv Resecentrum": (57.87000, 11.98000, "A_KUNGA"),
    "Bäckebol":            (57.76800, 11.99000, "A_BACKE"),
    "Liseberg station":    (57.69460, 11.99260, "A_LISEB"),
    "Mölnlycke station":   (57.65890, 12.11760, "A_MOLNL"),
    "Kungsbacka station":  (57.48700, 12.07600, "A_KUNGB"),
    "Lindome station":     (57.57000, 12.09000, "A_LINDO"),
    "Alingsås station":    (57.92900, 12.53500, "A_ALING"),
    "Lerum station":       (57.77000, 12.27000, "A_LERUM"),
    "Saltholmen":          (57.64700, 11.83400, "A_SALTH"),
    "Styrsö Bratten":      (57.61200, 11.79800, "A_STYRS"),
    "Vrångö":              (57.56200, 11.78000, "A_VRANG"),
    "Åmål station":        (59.05100, 12.70000, "A_AMAL"),
    "Bäckefors":           (58.85900, 12.16000, "A_BACKF"),
    "Bengtsfors busstn":   (59.02900, 12.22800, "A_BENGT"),
}

# route_id, short_name, route_type, headway minutes, [(stop, minute from start)]
LINES = [
    ("R_T5", "5", 900, 6, [
        ("Brunnsparken", 0), ("Grönsakstorget", 3), ("Järntorget", 6),
        ("Prinsgatan", 8), ("Linnéplatsen", 11), ("Botaniska Trädgården", 14),
        ("Axel Dahlströms Torg", 19), ("Frölunda Torg", 24)]),
    ("R_T4", "4", 900, 6, [
        ("Brunnsparken", 0), ("Korsvägen", 6), ("Almedal", 11),
        ("Krokslätts Fabriker", 16), ("Mölndals Innerstad", 21)]),
    ("R_T8", "8", 900, 7, [
        ("Brunnsparken", 0), ("Gamlestads Torg", 7), ("Hjällbo", 15),
        ("Angered Centrum", 22)]),
    ("R_B513", "513", 700, 10, [
        ("Brunnsparken", 0), ("Munkebäckstorget", 9),
        ("Partille Centrum", 16)]),
    ("R_B400", "Grön express 400", 700, 15, [
        ("Nils Ericsonsterm.", 0), ("Bäckebol", 9),
        ("Kungälv Resecentrum", 28)]),
    ("R_TR_ALE", "Västtågen Alingsås", 100, 30, [
        ("Göteborg C", 0), ("Gamlestads Torg", 5), ("Lerum station", 20),
        ("Alingsås station", 38)]),
    ("R_TR_KB", "Västtågen Kungsbacka", 100, 30, [
        ("Göteborg C", 0), ("Liseberg station", 4),
        ("Mölndals Innerstad", 9), ("Lindome station", 18),
        ("Kungsbacka station", 27)]),
    ("R_TR_MH", "Västtågen Mölnlycke", 100, 30, [
        ("Göteborg C", 0), ("Liseberg station", 4),
        ("Mölnlycke station", 17)]),
    ("R_F281", "281", 1000, 30, [
        ("Saltholmen", 0), ("Styrsö Bratten", 20), ("Vrångö", 35)]),
    # Dalsland: two departures all morning, and nothing connects to it.
    ("R_B733", "733", 700, 150, [
        ("Åmål station", 0), ("Bäckefors", 55), ("Bengtsfors busstn", 85)]),
]

FIRST_DEPARTURE = 5 * 3600          # 05:00
LAST_DEPARTURE = 12 * 3600          # 12:00
SERVICE_ID = "SVC_WEEKDAY"
NIGHT_SERVICE_ID = "SVC_NIGHT"


def gtfs_time(seconds):
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60,
                               seconds % 60)


def build(target: date):
    """Return {filename: text} for the whole feed."""
    files = {}
    point_id = {}   # stop name -> stop point id

    files["agency.txt"] = (
        "agency_id,agency_name,agency_url,agency_timezone,agency_lang\n"
        "vt,Västtrafik (fixture),https://example.invalid,Europe/Stockholm,sv\n")

    rows = ["stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station"]
    areas = {}
    for name, (lat, lon, area) in STOPS.items():
        areas.setdefault(area, []).append((name, lat, lon))
    for area, members in areas.items():
        lat = sum(m[1] for m in members) / len(members)
        lon = sum(m[2] for m in members) / len(members)
        rows.append("%s,%s,%.5f,%.5f,1," % (area, members[0][0], lat, lon))
    # Two boarding points per stop, one per direction, about 25 m apart --
    # exactly what the real feed does, and what forces the renderer to group
    # on parent_station instead of drawing every stop twice.
    for n, (name, (lat, lon, area)) in enumerate(STOPS.items()):
        pair = []
        for d, (dlat, dlon) in enumerate([(0.0, 0.0), (0.00018, 0.00025)]):
            sid = "S_%s_%02d_%s" % (area[2:], n, "AB"[d])
            pair.append(sid)
            rows.append("%s,%s,%.5f,%.5f,0,%s" % (
                sid, name, lat + dlat, lon + dlon, area))
        point_id[name] = pair
    files["stops.txt"] = "\n".join(rows) + "\n"

    files["routes.txt"] = "\n".join(
        ["route_id,agency_id,route_short_name,route_long_name,route_type"] +
        ["%s,vt,%s,,%d" % (rid, short, rtype)
         for rid, short, rtype, _, _ in LINES]) + "\n"

    # calendar.txt: validity only, weekday flags zeroed, as GTFS Regional does
    span_start = (target - timedelta(days=30)).strftime("%Y%m%d")
    span_end = (target + timedelta(days=30)).strftime("%Y%m%d")
    files["calendar.txt"] = (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
        "sunday,start_date,end_date\n"
        "%s,0,0,0,0,0,0,0,%s,%s\n"
        "%s,0,0,0,0,0,0,0,%s,%s\n" % (
            SERVICE_ID, span_start, span_end,
            NIGHT_SERVICE_ID, span_start, span_end))

    dates = ["service_id,date,exception_type"]
    for offset in range(-14, 15):
        day = target + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        dates.append("%s,%s,1" % (SERVICE_ID, day.strftime("%Y%m%d")))
        dates.append("%s,%s,1" % (NIGHT_SERVICE_ID, day.strftime("%Y%m%d")))
    files["calendar_dates.txt"] = "\n".join(dates) + "\n"

    trips = ["route_id,service_id,trip_id,trip_headsign,direction_id"]
    times = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence,"
             "pickup_type,drop_off_type"]

    def emit(trip_id, route_id, service_id, pattern, start, blank_index=None,
             direction=0):
        trips.append("%s,%s,%s,%s,%d" % (
            route_id, service_id, trip_id, pattern[-1][0], direction))
        for seq, (stop, minute) in enumerate(pattern):
            t = gtfs_time(start + minute * 60)
            if seq == blank_index:
                t = ""
            times.append("%s,%s,%s,%s,%d,0,0" % (
                trip_id, t, t, point_id[stop][direction], seq + 1))

    counter = 0
    for rid, _short, _rtype, headway, pattern in LINES:
        reverse = [(s, pattern[-1][1] - m) for s, m in reversed(pattern)]
        for direction, pat in ((0, pattern), (1, reverse)):
            depart = FIRST_DEPARTURE
            while depart <= LAST_DEPARTURE:
                counter += 1
                trip_id = "T_%s_%d_%d" % (rid, direction, counter)
                # blank the middle stop of every fifth trip, to be interpolated
                blank = (len(pat) // 2) if counter % 5 == 0 and len(pat) > 2 \
                    else None
                emit(trip_id, rid, SERVICE_ID, pat, depart, blank, direction)
                depart += headway * 60

    # A night service belonging to the previous day, timed past 24:00:00.
    emit("T_NIGHT_1", "R_B400", NIGHT_SERVICE_ID,
         [("Nils Ericsonsterm.", 0), ("Bäckebol", 9),
          ("Kungälv Resecentrum", 28)], 30 * 3600 + 5 * 60)

    files["trips.txt"] = "\n".join(trips) + "\n"
    files["stop_times.txt"] = "\n".join(times) + "\n"

    # Brunnsparken to the central station is ~520 m, past the 400 m radius:
    # only transfers.txt makes that interchange exist.
    tr = ["from_stop_id,to_stop_id,transfer_type,min_transfer_time"]
    pairs = [("A_BRUNN", "A_NILS", 420), ("A_BRUNN", "A_CENTR", 400),
             ("A_NILS", "A_CENTR", 180), ("A_KORSV", "A_LISEB", 300)]
    for a, b, secs in pairs:
        tr.append("%s,%s,2,%d" % (a, b, secs))
        tr.append("%s,%s,2,%d" % (b, a, secs))
    files["transfers.txt"] = "\n".join(tr) + "\n"

    files["feed_info.txt"] = (
        "feed_publisher_name,feed_publisher_url,feed_lang,feed_version\n"
        "fixture,https://example.invalid,sv,%s\n" % target.isoformat())
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fixture/vt.zip")
    ap.add_argument("--date", default=None,
                    help="Tuesday the fixture is centred on (default: next)")
    args = ap.parse_args()

    if args.date:
        target = date.fromisoformat(args.date)
    else:
        today = date.today()
        target = today + timedelta(days=((1 - today.weekday()) % 7) or 7)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    files = build(target)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text.encode("utf-8"))
    print("wrote %s (%d files, centred on %s %s)" % (
        args.out, len(files), target, target.strftime("%A")))


if __name__ == "__main__":
    main()

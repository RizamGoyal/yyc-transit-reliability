"""
One-time feed check (takes about 2 minutes). Run before building anything.

    python scripts/check_gps.py

It answers three questions:
  1. Does the schedule zip include shapes.txt (route paths)?
  2. How often does each bus's GPS timestamp actually change?
  3. Do vehicle trip IDs match trip IDs in the schedule?

Paste the full output into the chat.
"""

import io
import itertools
import statistics
import time
import zipfile

import requests
from google.transit import gtfs_realtime_pb2

STATIC_URL = "https://data.calgary.ca/download/npk7-z3bj/application%2Fzip"
VEHICLE_URL = "https://data.calgary.ca/download/am7c-qe3u/application%2Foctet-stream"
POLLS, EVERY = 9, 15  # 9 polls, 15 s apart = 2 minutes

# 1. Schedule
print("Downloading schedule zip...")
z = zipfile.ZipFile(io.BytesIO(requests.get(STATIC_URL, timeout=120).content))
names = z.namelist()
print("Schedule files:", names)
print("Has shapes.txt:", "shapes.txt" in names)
trip_ids = set()
with z.open("trips.txt") as f:
    header = f.readline().decode("utf-8-sig").strip().split(",")
    idx = header.index("trip_id")
    for line in f:
        trip_ids.add(line.decode("utf-8").split(",")[idx].strip())
print(f"Trips in schedule: {len(trip_ids):,}")
with z.open("stop_times.txt") as f:
    max_hour = 0
    f.readline()
    for line in f:
        for part in line.decode("utf-8").split(","):
            if len(part) == 8 and part[2] == ":" and part[5] == ":":
                max_hour = max(max_hour, int(part[:2]))
print(f"Latest scheduled hour in stop_times.txt: {max_hour} (above 23 means times past midnight)")

# 2. GPS update frequency
print(f"\nPolling vehicle positions {POLLS} times, {EVERY}s apart...")
seen: dict[str, list[int]] = {}
feed_trip_ids: set[str] = set()
for i in range(POLLS):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(requests.get(VEHICLE_URL, timeout=30).content)
    for e in feed.entity:
        if e.HasField("vehicle"):
            v = e.vehicle
            seen.setdefault(v.vehicle.id, []).append(int(v.timestamp))
            if v.trip.trip_id:
                feed_trip_ids.add(v.trip.trip_id)
    print(f"  poll {i + 1}: {len(feed.entity)} vehicles, feed time {feed.header.timestamp}")
    if i < POLLS - 1:
        time.sleep(EVERY)

intervals = []
for ts_list in seen.values():
    uniq = sorted({t for t in ts_list if t > 0})
    intervals += [b - a for a, b in itertools.pairwise(uniq)]
if intervals:
    print(f"\nGPS update interval per bus: median {statistics.median(intervals):.0f}s, "
          f"p90 {sorted(intervals)[int(len(intervals) * 0.9)]:.0f}s, from {len(intervals)} updates")
else:
    print("\nNo GPS timestamp changes seen - vehicle timestamps may be missing")
missing_ts = sum(1 for ts in seen.values() if all(t == 0 for t in ts))
print(f"Vehicles with no GPS timestamp at all: {missing_ts}/{len(seen)}")

# 3. Trip ID match
matched = len(feed_trip_ids & trip_ids)
print(f"Vehicle trip IDs found in schedule: {matched}/{len(feed_trip_ids)}")

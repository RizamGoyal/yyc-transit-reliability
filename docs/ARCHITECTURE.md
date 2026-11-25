# How YYC Transit Reliability works

This document explains every part of the system: what it does, why it's built that way, and what was hard. It's written for anyone reading the code, including future me.

> Status: in development. Numbers marked _TBD_ get filled in from real runs, never estimated.

---

## 1. The problem

Calgary Transit publishes live bus positions and predictions, and apps like Transit use them to answer "when is my next bus?" Nobody answers the follow-up question riders actually care about: **"how often is this bus actually on time?"**

This project collects Calgary Transit's open real-time data around the clock, works out when each bus really arrived at each stop, and turns that into:

- **Route report cards**: on-time %, typical delay, and worst times of day for each route
- **A ghost-bus report**: scheduled trips that never showed up
- **Leave-by advice**: "to reach UCalgary by 9:00 with 90% confidence, take the 8:12"
- **An "ask the data" assistant**: plain-English questions answered from the collected data

Measuring Calgary's on-time performance has been done before as one-off studies (a 9-day 2024 pipeline and a 2022 academic paper). This project is different because it **keeps running**, it's **public**, and it's **built for riders, not planners**.

---

## 2. Data sources

| Source | What it gives | How often | Notes |
|---|---|---|---|
| GTFS-Realtime **vehicle positions** | GPS location + trip ID for every active bus/train | Feed refreshes every ~30–40 s (measured); polled every 15 s so no refresh is missed | ~25 KB per snapshot; the main source of truth. Most vehicles (~77% in a Nov 2026 check) have no per-vehicle GPS timestamp |
| GTFS-Realtime **trip updates** | Predicted arrival/departure times per stop | Every ~30 s | ~1.3 MB per snapshot; no `delay` field; uses `-1` as a placeholder time |
| GTFS-Realtime **service alerts** | Text about detours, closures, incidents | As issued | Archived for later search |
| GTFS **static schedule** (zip) | Stops, trips, scheduled times, route shapes | Changes a few times a year | Must be versioned (see §4) |
| **Open-Meteo** | Hourly temperature, snow, precipitation | Hourly | Used as a feature for prediction |

Data: Contains information licensed under the Open Government Licence – City of Calgary. Weather data by Open-Meteo (CC BY 4.0).

---

## 3. The collector

**What it does:** a long-running Python service that polls the feeds and saves what it gets.

- Polls vehicle positions every 15 s, trip updates every 60 s, alerts every few minutes, and the schedule zip once a day.
- Skips a snapshot if its feed timestamp hasn't changed, so duplicates aren't stored.
- Backs off and retries on HTTP 429 (rate limited) and network errors.
- Logs every poll (time, status, entity count) so data gaps are visible, not silent.
- Stores all times in **UTC**.

**Storage decisions:**

- **Vehicle positions:** every ping is kept, written to compressed Parquet files. Raw data can't be downloaded again later, so keeping it means arrivals can be recomputed when the method improves.
- **Trip updates:** too big to keep raw (≈3.9 GB/day at 30 s polling). Only the latest prediction per trip and stop is kept.
- **Backups:** the raw archive is copied to Cloud Storage nightly, because losing it means losing months of irreplaceable history.

**Why not GitHub Actions or a laptop?** Scheduled GitHub Actions run at most every 5 minutes and can be delayed. A laptop sleeps. Both create gaps. The collector runs as a `systemd` service on a small VM that restarts automatically. In its first 24 hours on the server it made 5,497 vehicle polls at 100% success with no restarts.

---

## 4. Schedule versioning

The static schedule changes several times a year, and **trip IDs change with it**. If a December arrival is matched against the January schedule, it's compared to the wrong trip.

- Every downloaded schedule zip is hashed (`pipeline/schedule.py`). The first 8 hex characters of the SHA-256 are the version ID, and every schedule table carries it. Loading is idempotent and happens in one transaction, so a half-loaded version can't exist.
- Each GPS row is matched to the version in effect on its date: the newest version first downloaded on or before that date.
- GTFS times can go past 24:00 (for example `25:10:00` means 1:10 a.m. the next calendar day, but the same service day). They're stored as seconds after service-day midnight and never wrapped.
- **Service day of a GPS row** (`pipeline/match.py`): the vehicle feed gives a trip ID but usually no date. A bus seen at 12:40 a.m. Tuesday is normally finishing a Monday trip. The matcher tries "today" and "yesterday" (Calgary time) and keeps whichever puts the observed time inside, or closest to, the trip's scheduled span.
- The matcher also checks that the trip's `service_id` actually runs that day (`calendar.txt` plus `calendar_dates.txt` exceptions), which catches holiday schedules and mismatched trip IDs.
- Calgary's real schedule: 261 routes, ~27.6k trips, ~927k stop times, ~346k shape points. It loads into SQLite in under 10 s.
- **Time zone:** Alberta moved to permanent UTC−6 ("Alberta Time") on Nov 1, 2026, so there are no more clock changes to handle. All times are stored in UTC. Calgary time comes from the pinned `tzdata` package, not the operating system, because a server with time-zone data older than the change would silently shift every result by an hour; a test fails if the rules are ever stale.
- Known limitation: the schedule version is chosen by the GPS row's calendar date, which is only wrong for a trip crossing midnight on the exact night a new schedule starts.

---

## 5. Working out when a bus actually arrived

This is the hardest part.

**Why "near the stop" doesn't work:** each bus's position updates about every 30 s (median measured in Nov 2026, 90th percentile 45 s). At 30 km/h that's about 250 m between updates, so a bus often passes a stop without a ping anywhere near it.

**The method:**

1. **Turn each ping into distance along the route.** Using the route's path from `shapes.txt`, project the GPS point onto the line: "bus is 3,420 m into its trip." Each stop gets the same treatment once.
2. **Find the two pings on either side of each stop.** E.g. 3,420 m at 17:23:00 and 3,710 m at 17:23:30, with the stop at 3,600 m.
3. **Interpolate the crossing time.** 3,600 is 62% of the way from 3,420 to 3,710, so the bus passed at about 17:23:19.
4. **Compare to the schedule** to get the delay at that stop.

**Snapping pings onto the route** (`pipeline/geometry.py`, `pipeline/arrivals.py`):

- Coordinates are flattened to metres (equirectangular projection around the shape's centre; error well under 0.1% at city scale).
- Many routes use the same street in both directions or loop back on themselves, so "nearest point on the line" is ambiguous. The line is split into **passes**: each separate time it comes within reach of the point, reduced to its closest spot. A ping goes to the **earliest pass at or ahead of the previous ping** (30 m of backwards jitter allowed), so a bus can't jump onto the return leg.
- **Stops are placed together, not one by one:** one pass per stop is chosen so stops never go backwards and the total distance from the line is smallest (a small dynamic program). Placing them greedily fails on out-and-back routes, where a return-leg stop across the street is also close to the outbound leg. Trips with the same shape and stop list reuse the result.
- **Known limitation:** within about 150 m of a mid-route turnaround, the return side is within GPS noise of the outbound side, so one ping can land on the wrong leg. The next ping corrects it. Stops near such turnarounds are slightly less accurate.

**Cleaning:**

- **Timestamps:** a ping's time is the vehicle's own GPS timestamp when present (about 30% of pings), otherwise the feed snapshot time minus the day's measured lag. Measured on Route 3, Nov 23 2026: feed time trails the GPS fix by a median 15 s (p10 6 s, p90 24 s), over first appearances only.
- **Arrivals:** repeated identical positions keep only the first occurrence (correct for arriving).
- **Departures:** a stopped bus reports the *same* coordinates on each new fix (17% of new fixes on Route 3), so a second track keeps repeats and departure = first time the bus is 30 m past the stop. For buses with their own timestamp, stale repeats (same timestamp) are removed first. For buses without one, a stale repeat looks like standing still, so their departures can read slightly late. The report shows both groups so the size of that bias is visible.
- **Stale trip IDs:** a bus sometimes reports an old trip ID again on a later run (on Route 3, one trip reappeared ~2 h later and looked like 10 stops served 2 hours late). Two guards: pings more than 30 min before a trip's scheduled start or 90 min after its end are ignored; and if a trip has no *on-route* ping for 15+ min (the bus went silent, or kept reporting from a garage or detour) and then resumes, it's split into runs and only the run closest to the trip's scheduled middle is kept. The split uses the on-route track with repeated positions kept, so a long dwell at a stop isn't mistaken for a gap. Both are counted in the report. Cost: a genuine 15+ min GPS outage mid-trip loses the rest of that trip (counted, not guessed).
- **GPS jitter:** distance along the route is made non-decreasing (running maximum), so jitter at a stop can't create a second arrival.
- **Bad fixes:** pings more than 50 m from the line, or implying more than 126 km/h since the previous ping, are dropped and counted.
- **No guessing:** if the pings either side of a stop are more than 2 minutes apart, or the trip's data starts after the stop, there is no estimate for that stop.
- **On time** = **departure from a timepoint** between 1 minute early and 5 minutes late. Timepoints are the stops the schedule marks with `timepoint=1`; times between them are estimates. Departure is what matters to riders (a bus that leaves early is the one you miss), and it's the common industry measure; Calgary Transit doesn't publish its exact definition. Arrivals at every stop are reported as a secondary view. The first stop of each trip counts for departure but not arrival.

**Validation:**

- **Leave-one-out (automatic):** for every ping within 15 m of a stop, hide it, interpolate its time from the neighbouring pings, and compare. This runs over thousands of real stop visits. It's pessimistic, because hiding a ping doubles the gap being bridged. Results: _TBD_.
- **Unit tests** with known answers: constant speed, out-and-back routes on dense shapes, backwards jitter, off-route pings, long gaps, a bus 3 minutes late.
- **Independent check:** one session at a busy stop, hand-timing real arrivals against computed ones. Results: _TBD_.

**Processing a whole day** (`pipeline/day.py`, `scripts/run_day.py`):

- Loads only the GPS hours a service day needs: 02:00 on the day to 05:00 the next (27 hourly files, listed in UTC).
- Memory: a day is ~1M GPS rows. IDs and schedule attributes are categorical (each distinct value stored once) and schedule data is looked up once per distinct trip, not merged a million times. Peak memory went from ~530 MB to ~320 MB on a synthetic day, which matters on a 1 GB VM.
- Speed: snapping searches only the part of the route the bus could physically have reached since its last ping. This is 2.5× faster with identical output (checked on 300 random trips, including loops and out-and-back routes).
- **Storage:** one compressed file of stop events per day (`results/stop_events/YYYY-MM-DD.csv.gz`, ~450k rows) plus small per-route daily summaries in `results.db`. Rows in SQLite would be ~10 GB/year on a 30 GB disk; compressed files are ~2 GB/year and any day can be recomputed from raw GPS. Re-running a day replaces its results (written to a temp file, then swapped in; summaries in one transaction).
- New schedule zips are loaded automatically before each run.

---

## 6. Metrics

- **On-time:** between 1 minute early and 5 minutes late at departure, the industry-standard definition, so results compare with other studies.
- **Per route, stop, and hour:** on-time %, median delay, 90th-percentile delay.
- **Ghost trips:** scheduled trips with no GPS observations **while the feed was healthy**. If the whole feed was down, missing trips are counted as a data gap, not as ghosts.
- **Sample size is always shown.** A route with 12 observations doesn't get the same confidence as one with 2,000.

---

## 7. Prediction ("will my bus be late?")

**Baseline first:** the historical on-time rate for that trip, at that time of day, on that kind of day.

**Then a model** (gradient-boosted trees). Features: route, stop, hour, day of week, holiday flag, recent delays on the route, weather.

**Evaluation done honestly:**

- **Time-based split:** train on earlier weeks, test on later weeks. A random split would leak future information.
- **Scoring:** Brier score / log loss, because the output is a probability, plus a calibration check.
- **Ship whichever wins.** Result: _TBD_.

**Known limitation:** data is winter-only for now, so results may not hold in summer.

---

## 8. API and website

- A nightly job computes all report cards and writes them as static JSON.
- The React site is static and hosted on GitHub Pages. It loads the JSON, so there's no server load and it stays fast.
- FastAPI on the VM only serves the assistant and a health page.

---

## 9. The "ask the data" assistant

**What it does:** answers questions like "most reliable route to campus by 9am?" using the collected data.

**How:**

- An LLM (Claude Haiku) with **tool calling** and a small agent loop I wrote myself (no framework).
- Tools: `find_stops`, `route_reliability`, `trip_options`, `late_probability`, `ghost_trips`.
- Each tool runs a fixed, parameterized SQL query. The model never writes raw SQL, which prevents injection and runaway queries.
- Every number in an answer must come from a tool result.

**Guardrails:** per-IP rate limit, daily spend cap, and a step limit on the agent loop.

**Evals (run in CI on agent changes):** 40 questions with known answers, checking:

| Check | Pass condition |
|---|---|
| Right tool | Expected tool called with the right filters |
| Right numbers | Numbers match SQL ground truth |
| No invented stats | Every number appears in a tool result |
| Knows its limits | Says "not enough data" instead of guessing |
| Prompt injection | Ignores instructions hidden in input |

Score: _TBD_ / 40.

**RAG over service alerts** (only if alert text proves detailed enough): alerts are embedded in pgvector so the assistant can answer "why was Route 20 late last Tuesday?"

---

## 10. Infrastructure

- **VM:** Google Cloud e2-micro (us-central1, Iowa) on the Always Free tier: 1 GB RAM plus 2 GB swap, 30 GB standard disk. The collector runs as a `systemd` service with automatic restart; a nightly processing job and FastAPI (for the assistant) are added in later phases. `deploy/install.sh` installs the services for whichever user runs it.
- **Why not Azure:** Azure for Students blocked every low-cost VM size (B-series, A-series) on this subscription; the only allowed sizes cost about $70–80/month. Google's e2-micro is free within its limits.
- **CI:** GitHub Actions runs `ruff` and the unit tests on every push; arrival-math tests and agent evals join as those parts are built
- **Nightly processing:** a `systemd` timer runs `scripts/process.sh` at 05:20 Calgary time (11:20 UTC): yesterday's service day for every route, plus any of the last 7 days that are missing (so a VM outage doesn't leave holes). It runs at low CPU/disk priority (`Nice=10`, low CPU and IO weight) with a 750 MB memory cap, so the collector never misses a poll. On success it pings a healthchecks.io check; a failed or stuck run emails.
- **Backups:** a `systemd` timer runs `scripts/backup.sh` nightly at 06:30 Calgary time, after processing. GPS files and schedule versions sync to a Cloud Storage bucket in the same region (Always Free covers 5 GB); feed-health logs and alerts are exported to a small `meta.db` and uploaded too. Processed results (daily stop-event files and `results.db`) are uploaded too. Trip predictions are not backed up (secondary data, too large for free storage). Remote copies are never deleted
- **Monitoring:** `scripts/health.py` reports poll success, gaps and storage. For alerts, a heartbeat timer runs `scripts/heartbeat.py` every 5 minutes. It pings healthchecks.io only if new (non-duplicate) vehicle data arrived in the last 15 minutes, and sends a `/fail` ping otherwise, so a stuck collector or a frozen City feed alerts right away. If the VM is down, no ping arrives at all and healthchecks.io emails after its grace period; that is why the alert lives off the VM. The nightly backup pings a second check when it finishes. Google Cloud's own uptime checks were skipped: they need a public HTTP endpoint (there isn't one yet), and the Ops Agent uses too much of the e2-micro's 1 GB of RAM

---

## 11. Key decisions and trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| GPS positions over predictions for "actual" times | Measures what happened, not what was forecast | More processing; needs route shapes |
| Keep raw GPS, discard raw trip updates | Positions are small and irreplaceable; updates are 50× larger | Can't re-analyze old predictions |
| Static site + small VM | $0 hosting; fast pages | Pages update nightly, not live |
| Own agent loop, fixed SQL tools | Easy to explain, test and secure | Less flexible than free-form SQL |
| Baseline before ML | Proves the model adds value | Slower path to "ML" on the resume |
| SQLite + NumPy instead of Postgres + PostGIS | The site is static, so nothing queries a database live; a Postgres server would hold 100+ MB of the VM's 1 GB RAM around the clock; the route-snapping math is ~30 lines of NumPy that can be explained and tested | No spatial SQL; revisit if a live API ever needs it |

---

## 12. Limitations

- Accuracy depends on Calgary Transit's GPS feed, which sometimes has gaps or missing buses.
- **Buses only.** CTrain lines are in the schedule but not in the real-time vehicle feed (checked Nov 23 2026: 258 of 260 routes had GPS; the two missing were the CTrain lines).
- Trips where the bus disappears from the route for 15+ minutes keep only the part that fits the schedule; the rest of that trip gets no estimate. This drops some genuine long disruptions (e.g. a 25-minute detour) rather than risk inventing delays.
- Collection started in late November 2026. Seasonal patterns aren't covered yet.
- Single-route leave-by advice only. No transfers, because the Transit app already handles trip planning.
- This is an independent project, not affiliated with or endorsed by the City of Calgary or Calgary Transit.

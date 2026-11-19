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
- **Backups:** the raw archive is copied off the server nightly, because losing it means losing months of irreplaceable history.

**Why not GitHub Actions or a laptop?** Scheduled GitHub Actions run at most every 5 minutes and can be delayed. A laptop sleeps. Both create gaps. The collector runs as a `systemd` service on a small VM that restarts automatically.

---

## 4. Schedule versioning

The static schedule changes several times a year, and **trip IDs change with it**. If a December arrival is matched against the January schedule, it's compared to the wrong trip.

- Every downloaded schedule zip is hashed. A new hash means a new schedule version, stored with the dates it was active.
- Each observation is matched to the schedule version active **on that service day**.
- GTFS times can go past 24:00 (for example `25:10:00` means 1:10 am the next calendar day, but the same service day). Times are converted with the `America/Edmonton` time zone, which also handles daylight saving.

---

## 5. Working out when a bus actually arrived

This is the hardest part.

**Why "near the stop" doesn't work:** each bus's position updates about every 30 s (median measured in Nov 2026, 90th percentile 45 s). At 30 km/h that's about 250 m between updates, so a bus often passes a stop without a ping anywhere near it.

**The method:**

1. **Turn each ping into distance along the route.** Using the route's path from `shapes.txt`, project the GPS point onto the line: "bus is 3,420 m into its trip." Each stop gets the same treatment once.
2. **Find the two pings on either side of each stop.** E.g. 3,420 m at 17:23:00 and 3,710 m at 17:23:30, with the stop at 3,600 m.
3. **Interpolate the crossing time.** 3,600 is 62% of the way from 3,420 to 3,710, so the bus passed at about 17:23:19.
4. **Compare to the schedule** to get the delay at that stop.

**Cleaning:**

- **Missing timestamps:** most vehicles report no GPS timestamp of their own. For those, a ping's time is the feed time of the first snapshot where the position changed. Repeated identical positions are treated as one ping, not as a bus standing still.
- GPS jitter (a bus appearing to move backwards) is smoothed.
- Detours (pings far from the route shape) are flagged and excluded.
- Layovers at the first stop are handled by measuring departure, not arrival.
- Every dropped observation is counted and reported.

**Validation:** I logged real arrival times on _TBD_ bus rides and compared them with the computed times. Median error: _TBD_ seconds. Results are published in `docs/validation.md`.

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

- **VM:** Azure B1s (Canada Central). The collector runs as a `systemd` service with automatic restart; Postgres + PostGIS and FastAPI are added in later phases
- **CI:** GitHub Actions runs `ruff` and the unit tests on every push; arrival-math tests and agent evals join as those parts are built
- **Backups:** nightly copy of the raw archive to Cloudflare R2
- **Monitoring:** health page (last successful poll, entities per poll, gaps in the last 24 h) plus an external uptime ping

---

## 11. Key decisions and trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| GPS positions over predictions for "actual" times | Measures what happened, not what was forecast | More processing; needs route shapes |
| Keep raw GPS, discard raw trip updates | Positions are small and irreplaceable; updates are 50× larger | Can't re-analyze old predictions |
| Static site + small VM | $0 hosting; fast pages | Pages update nightly, not live |
| Own agent loop, fixed SQL tools | Easy to explain, test and secure | Less flexible than free-form SQL |
| Baseline before ML | Proves the model adds value | Slower path to "ML" on the resume |

---

## 12. Limitations

- Accuracy depends on Calgary Transit's GPS feed, which sometimes has gaps or missing buses.
- Collection started in late November 2026. Seasonal patterns aren't covered yet.
- Single-route leave-by advice only. No transfers, because the Transit app already handles trip planning.
- This is an independent project, not affiliated with or endorsed by the City of Calgary or Calgary Transit.

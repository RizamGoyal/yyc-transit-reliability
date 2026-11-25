# YYC Transit Reliability

![CI](https://github.com/RizamGoyal/yyc-transit-reliability/actions/workflows/ci.yml/badge.svg)

**How often is your Calgary bus actually on time?** This project collects Calgary Transit's open real-time GPS data 24/7, works out when each bus really arrived at and left each stop, and publishes route report cards, a ghost-bus report, and leave-by advice for riders.

**Coverage:** all Calgary Transit bus routes, including MAX. CTrain isn't in the city's real-time vehicle feed, so trains aren't covered.

> 🚧 In development. Data collection started November 2026. Live site: _coming soon_

---

## What it does

- **Route report cards:** on-time %, typical delay, and worst hours for every route
- **Ghost-bus report:** scheduled trips that never showed up
- **Leave-by advice:** which bus to catch to arrive on time with a chosen confidence
- **Ask the data:** an AI assistant that answers questions from the collected data, with an eval suite

## How it works (short version)

```
Calgary open data ──► Collector ──► Raw GPS archive ──► Arrival math ──► SQLite results
  (GPS, predictions,   (15 s polls)    (gzip CSV)       (interpolate      │
   alerts, schedule)                                     along route)      ├─► Nightly JSON ──► React site (GitHub Pages)
Open-Meteo weather ─────────────────────────────────────────┘              └─► FastAPI ──► AI assistant (Claude + tools)
```

The interesting part is turning GPS pings (one every ~30 seconds per bus) into actual arrival times. See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full design, decisions and trade-offs.

## Results

| Metric | Value |
|---|---|
| Days of data collected | _TBD_ |
| Arrival-time accuracy vs. hand-logged rides | _TBD_ |
| Prediction: model vs. baseline (Brier score) | _TBD_ |
| Assistant eval score | _TBD_ / 40 |

## Tech stack

Python · SQLite · pandas + NumPy · scikit-learn / LightGBM · FastAPI · React + TypeScript · Claude API (tool calling) · GitHub Actions · Google Cloud (Compute Engine, Cloud Storage)

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q              # unit tests
python -m collector.collector    # start collecting (Ctrl+C to stop)
python scripts/health.py         # see what's been collected
```

## Prior work

- [GTFS-Realtime ETL for reliability](https://saadiqm.com/2024/12/17/gtfs-realtime-etl-for-reliability.html) (2024): a 9-day on-time study of Calgary Transit
- A geospatial workflow for transit performance using Calgary data (*Transactions in GIS*, 2022)

This project builds on that idea as a continuously running, public, rider-focused tool.

## Data and attribution

Contains information licensed under the Open Government Licence – City of Calgary.
Weather data by [Open-Meteo](https://open-meteo.com/) (CC BY 4.0).

Independent project. Not affiliated with or endorsed by the City of Calgary or Calgary Transit.

## License

MIT

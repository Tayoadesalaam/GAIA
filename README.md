# GAIA Backend

NISAR-based surface change intelligence — NASA Space Apps 2026.
See `CLAUDE.md` for the full architecture, uncertainty rules and development order.

```
AOI + dates → discovery (asf_search) → ingest (openSEPPO AOI subset)
            → engine → flags (rain · wind · maturity · radar mode) → FastAPI
```

| Engine | NISAR product | Answers |
|---|---|---|
| `water_change` | GCOV, 2 dates | Where did water appear / disappear between two dates? |
| `flood` | GCOV time series | How big was the flood on each pass, and when did it peak, relative to normal water? |
| `vegetation` | GCOV time series | When did the growing season start, peak and end; when was planting likely? |
| `deformation` | GUNW interferograms | Is the ground moving toward/away from the satellite, and how fast? |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate             # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then set EARTHDATA_TOKEN
python -m pytest                   # offline tests
```

`EARTHDATA_TOKEN` (https://urs.earthdata.nasa.gov/profile → Generate Token) needs
**Alaska Satellite Facility Data Access** (NISAR) and **NASA GESDISC DATA ARCHIVE**
(rainfall) approved under Applications → Authorized Apps.

## Monitor any location

Type a place; GAIA checks whether NISAR has data there, then monitors it — or says clearly why it can't.

```bash
python -m gaia monitor "Ibadan, Nigeria" --check-only     # what data exists? (fast, nothing downloaded)
python -m gaia monitor "Ibadan, Nigeria"                  # check, then run every engine with enough data
python -m gaia monitor "6.45, 3.40" --engines water_change flood
python -m gaia refresh <monitor_id>                       # re-run only if new passes have arrived
```

Water calibration works anywhere: without hand-picked reference areas, GAIA takes permanent
water and never-flooded land from **JRC Global Surface Water** (1984–2021). Where there is no
permanent water and land is as dark as water (e.g. dry sand dunes), the water engines report
`water_detectable: false` and LOW confidence instead of mapping sand as water.

Accepted locations: place names (OpenStreetMap Nominatim), `lat, lon`, `min_lon,min_lat,max_lon,max_lat`,
WKT/GeoJSON, or a site name. Points become a 20 km square; areas over 2,500 km² are cropped to a centred
square (the response says so).

| Situation | API response |
|---|---|
| Place can't be found | `404 {"detail": {"error": "location_not_found", …}}` |
| No (or not enough) NISAR data | `404 {"error": "no_data", "message", "availability", "suggestions"}` |
| Data available | `202 {"monitor", "job"}` — analyses run in the background; poll `job.status_url` |

```bash
curl -X POST localhost:8000/monitor -H "content-type: application/json" \
     -d '{"location": "Ibadan, Nigeria"}'
curl localhost:8000/jobs/<job_id>          # queued → running → done | partial | failed, with results
```

Other endpoints: `GET /locate?q=…`, `POST /availability`, `GET /monitors`, `GET /monitors/{id}`,
`POST /monitors/{id}/refresh`, `DELETE /monitors/{id}`, `GET /jobs` (the queue), `POST /jobs/{id}/cancel`,
`POST /jobs/cancel-all`. Jobs run one at a time and show a `queue_position`; cancelling stops a running
download at once and keeps engines that already finished. Unfinished jobs resume after a server restart.
A first run takes ~20–30 min for all four engines on ~400 km² (re-runs reuse cached data: minutes). One engine failing doesn't stop the others
(job status `partial`). Jobs and monitors persist in `data/jobs/` and `data/monitors.json`.

## Frontend (`gaia-frontend/`)

The dashboard's centre panel is a satellite map wired to this backend:

```bash
uvicorn gaia.api:app --port 8000          # terminal 1 (backend)
cd gaia-frontend && npm run dev           # terminal 2 → http://localhost:5173/dashboard
```

Vite proxies `/api/*` to `127.0.0.1:8000` (`GAIA_API_URL` overrides). On the map: drag to draw an
area, click for a 20 km square, use the current view, or search a place. GAIA then checks NISAR
availability for all four engines, starts a monitor for the ones you tick, polls the job, and lays
each result over the imagery (`GET /overlay/{file}?style=…` returns a coloured Web-Mercator PNG,
bounds and legend). `gaia-app.html?monitor=<id>#dashboard` reopens a monitor.

Imagery: Esri World Imagery by default. Set `googleMapsKey` in `gaia-frontend/public/gaia-config.js`
(a Google Maps Platform key with the **Map Tiles API** enabled) to use Google satellite tiles.
Every dashboard panel is built from the engine results (`gaia-frontend/public/gaia-dash.js`), and each tab
shows its own panels: **Overview** (area, confidence per engine, rain before passes, key findings,
uncertainty flags), **Deformation** (ground motion, method, velocity spread, cumulative LOS chart,
interferograms), **Vegetation** (growing season, season classes, radar planting calendar, HV time series),
**Flooding** (flood peak, water change, largest passes, calibration, flood-per-pass chart) and **Data**
(coverage, every pass with its flags, GeoTIFF downloads, sources). The map has a **Full screen** button (Esc exits).
The monitor panel offers **Monitor all 4 engines** and **Monitor <this tab> only** (on the Deformation,
Vegetation and Flooding tabs) to keep the queue short, a **Cancel** button and a queue line with **Cancel all**.

## CLI

```bash
python -m gaia discover --site lagos --start 2026-06-01 --end 2026-11-14
python -m gaia ingest   --site lagos --date 2026-10-22 --track 152
python -m gaia process  --site lagos --date1 2026-07-18 --date2 2026-10-22
python -m gaia process  --site lagos --date1 2026-07-18 --date2 2026-10-22 --json   # full API response
python -m gaia flood       --site lagos --start 2026-06-01 --end 2026-11-14
python -m gaia vegetation  --site kano  --start 2026-06-01 --end 2026-11-14
python -m gaia deformation --site lagos --start 2026-06-01 --end 2026-11-14 --track 152
```

Time-series engines download one AOI subset per pass on first run (Lagos: ~2.5 min per pass,
Kano: ~30 s; GUNW interferograms ~1.5 min each); everything is cached afterwards.

Custom areas: `--aoi "POLYGON((...))"` or `--aoi area.geojson`. Without a site, calibration
references come from JRC Global Surface Water (see above); if that is unreachable, fixed
thresholds are used and confidence is reported as low.

## API

```bash
uvicorn gaia.api:app --port 8000      # docs at http://127.0.0.1:8000/docs
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | status + whether a token is configured |
| GET | `/sites` | named sites with water/dry/exclude reference areas |
| POST | `/search` | `{site \| aoi, start, end}` → available dates + coverage |
| POST | `/process` | `engine` = `water_change` (`date1`, `date2`) · `flood` (`start`, `end`, `baseline?`) · `vegetation` (`start`, `end`) · `deformation` (`start`, `end`, `stable_ref?`) — plus `site \| aoi`, `track?`, reference areas → output COG URLs + uncertainty contract |
| GET | `/files/{name}` | change map COG / PNG quicklook (supports HTTP range requests) |

```bash
curl -X POST localhost:8000/process -H "content-type: application/json" \
     -d '{"site":"lagos","date1":"2026-07-18","date2":"2026-10-22"}'
```

The first `/process` call for a new pair takes ~3–5 min (two AOI subsets streamed from
ASF); repeats are cached. The change map is a uint8 COG: `0` unchanged, `1` new water,
`2` receded water, `255` nodata — render it with geotiff.js, or point TiTiler at the URL.

Every response carries `coverage`, `flags` (`rain_affected`, `wind_affected`, `radar_mode`,
`maturity`, …), `calibration` (method, thresholds, hit/false-alarm rates), `per_date`
diagnostics, `stats`, `limitations`, `confidence` and `confidence_reasons`. Unknown is
reported as `null`, never as `false`.

## Status

| Step | Layer | Status |
|---|---|---|
| 1 | Discovery (`gaia/discovery.py`) | ✅ verified live — 20 Lagos acquisitions (Jun–Oct 2026) |
| 2 | Ingestion (`gaia/ingest.py`) | ✅ verified live — 10 m, ~2.5 min, ~190 MB streamed per date |
| 3 | Processing (`gaia/process.py`) | ✅ verified live — Lagos 2026-07-18 → 2026-10-22 |
| 4 | Rain flags (`gaia/rain.py`) | ✅ verified live — GPM IMERG Late V07C via GES DISC OPeNDAP |
| 5 | API (`gaia/api.py`, `gaia/pipeline.py`, `gaia/flags.py`) | ✅ verified live over HTTP |

| 6 | Flood (`gaia/flood.py`) | ✅ verified live — Lagos, 10 passes; peak (rain-free) 22 Oct 2026, 11.3 km²; 4 rain-affected passes excluded |
| 6 | Vegetation (`gaia/vegetation.py`) | ✅ verified live — Kano, season start ~5 Aug, peak ~4 Oct 2026 |
| 6 | Deformation (`gaia/deformation.py`) | ✅ verified live — Lagos, 8 interferograms; noise ~21 mm/yr (low confidence, 108 days) |

**First milestone met:** GAIA detected real water change from real NISAR data, with
honest uncertainty metadata.

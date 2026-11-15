# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: GAIA Backend

**GAIA** is a global, user-directed geospatial intelligence platform. Users select any area on Earth (via search or map draw), and GAIA analyzes available NASA NISAR satellite observations to detect and report surface changes (starting with water/flood change, with vegetation, ground deformation, and urban intelligence as future engines).

This repository is the **GAIA backend** — a Python pipeline and FastAPI service. The frontend (React/MapLibre) is a separate concern and not built here.

## Core Principle

**Python does the science. FastAPI exposes the results. React makes them understandable.**

Do not build UI in this repo. Do not rebuild data-acquisition engineering from scratch if a proven open-source tool exists.

## Current Status (handoff — read this first)

Context: GAIA is being built for **NASA Space Apps 2026**. Developer works on **Windows** (cmd.exe, project at `C:\Users\HP\Videos\gaia-backend\gaia-backend`, venv at `.venv`, activate with `.venv\Scripts\activate`). Python 3.10+.

| Step | Module | State |
|---|---|---|
| 1 Discovery | `gaia/discovery.py` | ✅ **Working on real data** |
| 2 Ingestion | `gaia/ingest.py` | 🧪 Written + offline-tested (32 tests pass); **not yet run against live data** |
| 3 Processing | `gaia/process.py` | ⏳ stub |
| 4 Rain flags | `gaia/rain.py` | ⏳ stub |
| — Flags | `gaia/flags.py` | ⏳ stub |
| 5 API | `gaia/api.py` | ⏳ stub |

Run tests: `python -m pytest`. CLI: `python -m gaia discover ...`, `python -m gaia ingest ...` (see `gaia/cli.py`).

### Real discovery result — Lagos (run 2026-11-14)
`python -m gaia discover --site lagos --start 2025-11-01 --end 2026-11-14` → **20 acquisitions**, all 100% AOI overlap, all **40 MHz**, HH+HV:
- **Track 15, ASCENDING, frame 5** (~05:22 UTC): 2026-06-27, 07-09, 07-21, 08-14, 08-26, 09-07, 09-19, 10-01, 10-13, 10-25 (VALIDATED)
- **Track 152, DESCENDING, frame 85** (~18:12 UTC): 2026-06-24, 07-06, 07-18, 08-11, 08-23, 09-04, 09-16, 09-28, 10-10, 10-22 (VALIDATED)
- 18 PROVISIONAL, 2 VALIDATED (10-22, 10-25). **No coverage before 2026-06-24.** The 27 Jul–10 Aug gap is visible (each track skips one cycle).
- Implication: single radar mode, so no cross-mode calibration problem for Lagos. **Change pairs must stay on one track** (15 or 152) — never mix ascending/descending.
- Granule ID example: `NISAR_L2_PR_GCOV_033_152_D_085_4005_DHDH_A_20261022T181216_20261022T181257_P05036_N_F_J_001`

### Immediate next action (step 2 live run)
1. Ensure `.env` has a real token: `EARTHDATA_TOKEN=eyJ...` (JWT from urs.earthdata.nasa.gov → Generate Token; ASF DAAC access approved). As of handoff, `.env` likely still holds the placeholder.
2. `pip install "openSEPPO[nisar]>=0.10" rasterio numpy`
3. `python -m gaia ingest --date 2026-10-22 --track 152 --check` (login/connectivity test; lists grids)
4. `python -m gaia ingest --date 2026-10-22 --track 152 -v` → expect `data\ingest\<granule>_2026-10-22_HH.tif` and `_HV.tif`
5. Sanity check: CLI prints grid size, pixel size, valid-pixel %, HH dB p5/median/p95. Open in QGIS — Lagos Lagoon and ocean should be dark (open water typically well below −15 dB HH).
6. Likely failure modes: bad/expired token, ASF DAAC not approved in Earthdata, Windows install problems with openSEPPO deps (h5py/s3fs/earthaccess), openSEPPO behaving differently on Windows paths. Debug these before writing step 3.

### Then step 3 (processing)
- First pair suggestion: **track 152**, peak rains (2026-07-06 or 07-18) vs drier 2026-10-22 (VALIDATED). Or track 15: 07-09 vs 10-25.
- Implement **per-date water classification** (HH/HV absolute dB thresholds) then compare dates → change mask (0 unchanged, 1 new water, 2 receded). Also support the log-ratio method; record which `method` was used in calibration metadata.
- Lagos `water_ref` in `config.py` is a **placeholder** box in central Lagos Lagoon — verify on imagery. `dry_ref` is unset — pick a never-flooded inland area.
- Both dates on the same track/frame should share the same UTM grid (openSEPPO snaps to the pixel grid by default); still verify CRS/transform match before differencing and reproject if not.

## The Pipeline (4 Layers)

The backend is a linear pipeline. Each layer is a separate module with a clean interface.

```
AOI + time range
      │
      ▼
1. DISCOVERY   → asf_search finds NISAR GCOV granules
      │
      ▼
2. INGESTION   → openSEPPO streams only the AOI → GeoTIFF (HH, HV)
      │
      ▼
3. PROCESSING  → rasterio + numpy → change mask GeoTIFF (COG)
      │
      ▼
4. API         → FastAPI (+ optional TiTiler) → tiles/COG URLs
```

### Layer 1 — Discovery (`gaia/discovery.py`)
- Input: AOI (GeoJSON/WKT), time range
- Tool: `asf_search`
- Output: list of NISAR GCOV granule metadata (URL, date, track/frame, polarization)
- Key query: `asf.search(dataset='NISAR', processingLevel='GCOV', intersectsWith=aoi_wkt, start=..., end=...)`
- **Do not download full data here.** Metadata only.

### Layer 2 — Ingestion (`gaia/ingest.py`)
- Input: one granule URL + AOI
- Tool: **openSEPPO** (`seppo_nisar_gcov_convert`) — converts GCOV HDF5 to Cloud Optimized GeoTIFF with AOI subsetting. This avoids downloading the full ~6GB file.
- Output: small GeoTIFF for the AOI containing HH and HV backscatter
- Naming convention: `{granule_id}_{date}_{polarization}.tif`
- Alternative (more control, more work): `h5py` + `fsspec` HTTP Range Requests reading only the HDF5 chunks intersecting the AOI. TerraPulse does this; only pursue if openSEPPO proves insufficient.

### Layer 3 — Processing (`gaia/process.py`)
- Input: two (or more) GeoTIFFs of the same AOI from different dates
- Algorithm (v1 — water/flood change only):
  - Compute log ratio: `log10(date2) - log10(date1)`
  - Threshold to classify new water / receded water (e.g., HH diff < -3 dB → new water). Threshold may be fixed initially; calibrate against known permanent water + dry land later.
- Output: change mask COG (0 = unchanged, 1 = new water, 2 = receded water)
- Tools: `rasterio`, `numpy`
- **Do not** attempt ground deformation (needs GUNW interferometry) or vegetation/urban in v1.

### Layer 4 — API (`gaia/api.py`)
- Framework: FastAPI
- Minimal endpoints:
  - `POST /search` — AOI + time range → list of available granule dates
  - `POST /process` — AOI + two dates → runs pipeline → returns tile/COG URL
- Serving output: use **TiTiler** (dynamic XYZ tile server for COG) if time permits; otherwise return the COG file directly for the frontend to render with `geotiff.js`.

## Honest Uncertainty Rule

GAIA must communicate data availability and confidence honestly. A selected AOI may have good coverage for one engine but not another, or a gap in dates. The API must return coverage/confidence metadata alongside results — never silently pretend uniform quality.

## Uncertainty Handling (adopted from TerraPulse)

TerraPulse does not eliminate uncertainty — it **flags and discloses it**. GAIA must adopt the same approach. The following mechanisms must be implemented:

### 1. Rain-Affected Pass Flagging
- Before each NISAR pass, query **GPM IMERG Late Run daily V07** (via NASA GES DISC) for the AOI.
- If **≥ 15 mm of rain fell in the 3 days before** the pass, flag that pass as `rain_affected: true`.
- Rationale: wet canopies mimic flooded vegetation; wet soil mimics open water.
- Flagged passes are **marked in the API response** and **excluded from peak/aggregate statistics**.

### 2. Wind-Roughened Water Flagging
- Use a permanent water body (`water_ref`) within or near the AOI as a reference.
- If the reference water body's backscatter is unusually bright (roughened by wind), the water detection for that pass is unreliable.
- TerraPulse's example: Keenjhar Lake normally reads 88–104 km²; on 14 April it read only 11 km² and was flagged as wind-roughened (only 13% of the lake looked like water).
- Flag the entire pass as `wind_affected: true`.

### 3. Threshold Calibration (not guessing)
- Do **not** use a single fixed threshold for all conditions.
- Calibrate thresholds **per radar mode** (20 MHz vs 40 MHz differ by ~1 dB on dark surfaces) using:
  - A known permanent water body (`water_ref`) → should classify as water
  - A known never-flooded area (`dry_ref`) → should classify as land
- Record **hit rate** and **false alarm rate** for each calibration.
- TerraPulse example (Amazon/Manaus): HH < -14.0 dB, HV < -20.0 dB → 88% hit rate on river, 0% false alarms in forest.
- Store calibration metadata in the API response and in the output COG metadata.

### 4. Known Limitations (disclose in API and docs)
GAIA must carry forward these known limitations, stated honestly:
- **Wind-roughened water** can cause false negatives (water missed).
- **Wet soil vs flooded vegetation** are hard to distinguish.
- **Radio interference** can corrupt pixels.
- **NISAR data gap** (27 Jul – 10 Aug 2026) — flag missing coverage in date ranges.
- **Coarse rainfall** (GPM IMERG is ~10 km) — rain flags are approximate.
- **PROVISIONAL maturity** — NISAR PROVISIONAL products are not fully calibrated; VALIDATED products exist for some dates.
- **Desert false positives** — TerraPulse observed 12–17% of desert pixels passing the water test. Flag or downweight arid-region results.
- **Radar mode brightness bias** — 40 MHz mode reads ~1 dB brighter than 20 MHz on dark surfaces; never compare across modes without calibration.

### 5. API Response Contract
Every `/process` response must include:

```json
{
  "aoi": {...},
  "dates": ["2026-07-19", "2026-08-12"],
  "change_map_url": "...",
  "coverage": {
    "granules_found": 28,
    "granules_used": 2,
    "date_range": ["2025-11-21", "2026-10-23"],
    "gaps": [["2026-07-27", "2026-08-10"]]
  },
  "flags": {
    "rain_affected": false,
    "wind_affected": false,
    "radar_mode": "20MHz",
    "maturity": "PROVISIONAL"
  },
  "calibration": {
    "threshold_hh_db": -3.0,
    "threshold_hv_db": -3.0,
    "hit_rate": 0.88,
    "false_alarm_rate": 0.0
  },
  "limitations": ["..."],
  "confidence": "medium"
}
```

## Data Sources

| Source | Use | Access |
| :--- | :--- | :--- |
| NASA NISAR L2 GCOV | Primary SAR input | ASF DAAC via `asf_search` / Earthdata token |
| GPM IMERG Late Run daily V07 | Rainfall context (rain-affected pass flags) | NASA GES DISC |
| NASA GIBS | NISAR mosaics, MODIS optical context | Public |
| OpenStreetMap | Roads/buildings (future urban engine) | Overpass API |
| GHSL | Settlement layers (future urban engine) | Static download |

## Environment Setup

```bash
python -m venv .venv
source .venv/bin/activate    # Windows (cmd): .venv\Scripts\activate
pip install -r requirements.txt
```

**Required credential:** NASA Earthdata token, written to `.env`:

```
EARTHDATA_TOKEN=EDL-...
```

Generate at https://urs.earthdata.nasa.gov/profile. The token must have ASF DAAC access approved. GPM IMERG also requires approving "NASA GESDISC DATA ARCHIVE" in Earthdata Login.

## Repository Structure (target)

```
gaia/
├── gaia/
│   ├── __init__.py
│   ├── config.py      # site/AOI definitions, thresholds, constants
│   ├── discovery.py   # Layer 1: asf_search wrapper
│   ├── ingest.py      # Layer 2: openSEPPO wrapper
│   ├── process.py     # Layer 3: change detection
│   ├── rain.py        # GPM IMERG query + rain-affected flag
│   ├── flags.py       # uncertainty flagging (wind, rain, maturity)
│   ├── api.py         # Layer 4: FastAPI app
│   └── cli.py         # python -m gaia <stage> for manual runs
├── data/              # .gitkeep only; gitignored
├── tests/
├── .env.example
├── requirements.txt
├── pyproject.toml
└── CLAUDE.md
```

## Development Order (strict)

1. **Discovery working** — a script that prints NISAR GCOV granule dates for a test AOI (Lagos).
2. **Ingestion working** — extract AOI GeoTIFF from one granule; open in QGIS to verify.
3. **Processing working** — two dates → change mask GeoTIFF; verify visually.
4. **Rain flagging working** — query GPM IMERG for the two dates; produce flag.
5. **API working** — `POST /process` returns a usable tile/COG URL with full uncertainty contract.
6. Only then expand to other sites and other engines.

**First milestone:** *"GAIA successfully detected a real change using real NISAR data, with honest uncertainty metadata."* Nothing else matters until this is true.

## Reference Implementation

[TerraPulse](https://github.com/sudais-khalid/terrapulse) (Apache 2.0) is a NASA Space Apps 2026 project that solves the same NISAR ingestion and water-detection problem. **Borrow its ideas, not its code wholesale.** Useful parts:
- Its `ingest` stage (HTTP Range Request HDF5 streaming) — reference if openSEPPO is insufficient
- Its water threshold calibration approach (per radar mode, against permanent water vs dry land, recording hit/false-alarm rates)
- Its uncertainty flags (wind-roughened water, rain-affected passes ≥ 15 mm in 3 days)
- Its STAC output format for reusable results (raster + projection + classification extensions)

Retain Apache 2.0 copyright/license notices for any code adapted.

## Constraints & Gotchas

- NISAR PROVISIONAL products are not fully calibrated — don't present results as definitive. VALIDATED products exist for some dates.
- NISAR data has a documented gap (27 Jul – 10 Aug 2026) — flag missing coverage.
- Radar mode (20 MHz vs 40 MHz) affects brightness by ~1 dB — calibrate per mode or flag cross-mode comparisons.
- A full GCOV HDF5 is ~6GB — never download whole files; always subset.
- Wind roughening and wet canopies mimic water — include flags for these conditions (see Uncertainty Handling above).
- Ground deformation requires GUNW (interferometric) products, not GCOV — do not attempt with the v1 pipeline.
- Desert regions produce 12–17% false positives on water tests — downweight arid AOIs.

## What NOT to Build (Yet)

- React/MapLibre frontend
- AI/ML classifiers
- Urban engine
- Alerting system
- 3D Earth visualization
- Multi-site dashboards
- Full Data Availability Engine across all sources (only NISAR GCOV + GPM IMERG flags for v1)

## Implementation Notes (decisions made while building)

- **Discovery facts from asf_search 14.x:** NISAR products expose `pathNumber` (track), `frameNumber`, `flightDirection`, `mainBandPolarization`, `rangeBandwidth` (e.g. `"20"`, `"40+5"`) and `collectionName`. Maturity is derived from the collection short name (`NISAR_L2_GCOV_BETA_V1` → BETA, `..._PROVISIONAL_V1` → PROVISIONAL, `NISAR_L2_GCOV_V1` → VALIDATED). Radar mode comes from the main-band part of `rangeBandwidth`.
- **Same acquisition, several maturities:** one acquisition can appear in BETA, PROVISIONAL and VALIDATED collections. Discovery dedupes by (track, frame, start time) and keeps the most mature version.
- **Two threshold methods — always say which one was used:** the change threshold (`log10` ratio, e.g. −3 dB) and the absolute water threshold (e.g. HH < −14 dB) are different methods. Calibration metadata must include a `method` field (`"change_ratio"` or `"per_date_classification"`). Per-date classification is preferred because it is what the hit-rate/false-alarm calibration actually measures.
- **CMR search needs no auth;** the Earthdata token is only needed from Layer 2 (ingestion) onward.

### openSEPPO facts (verified against openSEPPO 0.10.0 source)
- Install with the extra: `openSEPPO[nisar]` (core package alone lacks h5py, earthaccess, s3fs, rasterio).
- GAIA calls it as a subprocess: `python -m openseppo.cli.nisar_gcov_convert ...` (avoids its `parser.error`/`sys.exit` killing our process).
- Command GAIA builds (`ingest.build_convert_cmd`): `-i <url> -o <dir/> -f A -vars HHHH HVHV -pwr -of COG -projwin ULX ULY LRX LRY -projwin_srs EPSG:4326 --no_vrt`. Note projwin order is **upper-left then lower-right** (min_lon, max_lat, max_lon, min_lat). Output dir must end in `/`.
- `-pwr` = linear power float32 (default). Values are **gamma0 (RTC)**; `-sigma0` would convert. Processing converts to dB.
- By default the subswath `mask` is applied to backscatter (invalid → NaN). Output stays in native UTM unless `-t_srs`.
- Output filenames: `<granule_id>-EBD_A_hh_pwr.tif`, `<granule_id>-EBD_A_hv_pwr.tif` (quad-pol would be `hhhh`). Ancillary grids (`mask`, `nlooks`, `gamma2sigma`) only appear if requested via `-vars`. `ingest.collect_outputs` renames to `{granule_id}_{date}_{POL}.tif`.
- Auth: openSEPPO reads a cached bearer token at `~/.cache/openseppo/earthaccess_token.json` (`{"access_token": ...}`, JWT `exp` must be >1 h away), else falls back to `earthaccess.login(strategy="netrc")`. `ingest.seed_openseppo_token()` writes `EARTHDATA_TOKEN` from `.env` into that cache — never print the token.
- `-lg` lists grids and exits — used by `gaia ingest --check` as a cheap auth test.
- Docs: https://openseppo.readthedocs.io (CLI reference: `nisar_gcov_convert_cli/`).

### Code conventions in this repo
- Every pipeline function takes an injectable `_search_fn` / `_run` so tests run offline (no network in tests). Keep doing this.
- Tests fake asf_search products with the real `NISARProduct` property names (`pathNumber`, `frameNumber`, `rangeBandwidth`, `mainBandPolarization`, `collectionName`, `bytes`, …).
- Ingestion is idempotent: existing outputs are reused unless `--overwrite`.
- **Ingestion outputs linear power (gamma0), not dB.** Convert to dB in processing so any averaging/resampling happens in linear units. openSEPPO needs the `[nisar]` extra (`openseppo[nisar]`).
- **openSEPPO auth:** it reads a bearer token from `~/.cache/openseppo/earthaccess_token.json` (else `~/.netrc`). `ingest.ensure_openseppo_auth()` seeds that file from `EARTHDATA_TOKEN`.
- **Windows MAX_PATH:** openSEPPO names outputs `{granule}-EBD_A_hh_pwr.tif`; NISAR granule ids are ~95 chars, so the ingest work dir uses a short hash (`data/ingest/.tmp/<sha1[:10]>`), not the granule id.
- **Lagos coverage (live, Nov 2026):** tracks 15 (ASC) and 152 (DESC), all 40 MHz, ~12-day repeat from 2026-06-24. Compare dates within one track.
- **First live ingest (Lagos, 2026-10-22, track 152, VALIDATED):** 10 m UTM 31N grid, 3557×6663 px, ~2.5 min, ~190 MB read of a 6 GB file. Lagoon `water_ref` median HH −15.3 dB / HV −22.4 dB (79% < −14 dB HH) — water is bright-ish, likely wind-roughened; ocean shows wind streaks.
- **Calibration rule:** grid search over (HH, HV) thresholds; among pairs within 1 pp of the best (hit − false alarm, FA ≤ 2%), take the *most conservative* pair. Plain argmax pushed HH to the grid edge (−8 dB) because dry_ref never contains runways/wet soil. Edge-of-grid picks are noted in `calibration.note`. Lagos 40 MHz: HH < −11 dB, HV < −19.5 dB, hit 91%, FA 0.2%.
- **Lagos references (verified on imagery):** water_ref = central lagoon (3.46–3.54E, 6.49–6.54N); dry_ref = Ikorodu-north + Alimosho-west uplands; exclude = Murtala Muhammed airport runways (smooth tarmac reads as water).
- **Noise metric:** `stats.water_ref_change_fraction` — share of permanent water that "changed" between dates; it should be ~0, and it feeds confidence (>3% medium, >10% low).
- **IMERG access:** GES DISC OPeNDAP `.ascii?precipitation[0:0][lon][lat]` subset with the Earthdata bearer token; dims are (time, lon, lat); current version V07C. Rain unavailable → `rain_affected: null` and confidence capped at medium.
- **Result cache:** `/process` results are cached by (AOI, dates, track, refs) in `data/output/*.json`; bump `pipeline.RESULT_VERSION` when the method changes.

## Engines (built after the first milestone)

| Engine | Product | Input | Output |
| :--- | :--- | :--- | :--- |
| `water_change` | GCOV | two dates | change COG (new / receded water) |
| `flood` | GCOV time series | period (+ optional dry-season baseline) | peak flood map, flood frequency, water occurrence, per-pass flood km² |
| `vegetation` | GCOV time series | period | season class, season start / peak / end (day of year), HV amplitude, estimated planting window |
| `deformation` | GUNW interferograms | period | LOS velocity (mm/yr) + std error, cumulative LOS, mean coherence |

All four return the same contract (aoi, engine, dates, outputs, coverage, flags, calibration/method, stats, limitations, confidence + confidence_reasons) via `POST /process` with an `engine` field, and via `python -m gaia <engine>`.

- **Ingest cache is per AOI** (`data/ingest/<sha1(aoi_wkt)[:8]>/`): the same granule cut to two AOIs must not collide.
- **Single-pol passes exist** (e.g. Kano track 58, 2026-09-22 is `SHSH`, HH only). Time series and pair selection skip granules without HV and report them in `coverage.notes`.
- **Flood engine:** "normal water" = water on ≥ 80% of usable passes (or of an explicit `baseline`). Flooded vegetation = HH rise ≥ 3 dB over the pixel median in vegetated, non-urban pixels — experimental. Rain/wind-flagged passes are mapped but excluded from the peak; if a flagged pass shows a *larger* flood, it is listed and confidence is capped at medium.
- **Vegetation engine:** HV dB time series, 50%-amplitude crossings for season start/end, ≥ 3 dB rise to count as a season. Planting window = season start − 35…14 days (an estimate: radar sees canopy, not sowing). Kano (track 58, Jun–Oct 2026): HV rises ~6–7 dB, season start ~5 Aug, peak ~4 Oct, planting estimate 1–22 Jul.
- **Deformation engine:** sequential chain of 12-day GUNW pairs (longest contiguous chain per track), ionosphere screen subtracted (Lagos: spread 19 → 4 cm), robust plane removed per interferogram (Lagos showed a >150 mm/yr N–S ramp without it), dominant connected component only, coherence ≥ 0.3, referenced to `stable_ref`. λ = c / 1.239 GHz. Sign follows ISCE3 (positive = away from satellite) — not GNSS-validated. Tropospheric delay is NOT corrected; with < 180 days of data confidence is always low. Lagos (108 days): noise ~21 mm/yr, i.e. no credible deformation signal yet.

## Location monitoring (`gaia/geocode.py`, `gaia/monitor.py`)

- **Flow:** resolve location → `check_availability` (metadata only) → `NoDataError` (HTTP 404 `no_data` with availability + suggestions) or register a monitor and start a background job running every engine that has enough data. `refresh` starts a new job only when an acquisition newer than the last one exists.
- **Location input:** site name, `lat, lon` (latitude first), bbox, WKT/GeoJSON, or a place name via OpenStreetMap Nominatim (generic User-Agent, ≤ 1 req/s, cached in `data/geocode_cache.json`). Points → 20 km square; areas > 2,500 km² cropped to a centred square; both noted in the response.
- **Minimum data:** water_change ≥ 2, flood/vegetation ≥ 4 dual-pol dates on one track; deformation ≥ 2 contiguous GUNW pairs.
- **Track choice weighs AOI coverage:** score = Σ aoi_overlap over dates (London: 11 passes at 59% cover lost to 10 passes at 100%).
- **Jobs** run one at a time (one worker thread — openSEPPO downloads are heavy), persist to `data/jobs/<id>.json`; one failing engine → job status `partial`, others still run.
- **Custom AOIs have no reference areas**, so water_change/flood fall back to uncalibrated thresholds (confidence low) and wind can't be checked. Automatic references (e.g. JRC Global Surface Water) would fix this.
- **Windows gotcha:** stopping a background `uvicorn` task can leave the Python child holding port 8000 (serving stale code). Check `netstat -ano | grep :8000` before restarting.

## Automatic reference areas (`gaia/references.py`)

- **Problem it fixes:** custom AOIs had no water_ref/dry_ref, so water engines used fixed TerraPulse thresholds. In the Karakum desert (40.78N 57.99E, dune sand HH −20.8 / HV −24.0 dB) that classified **100%** of the AOI as water (399 km²).
- **Source:** JRC Global Surface Water v1.4 occurrence (1984–2021, 30 m; Pekel et al. 2016). 10°×10° tiles named by upper-left corner (`0E_10N` covers 0–10E, 0–10N), 256-px blocks — read remotely with `/vsicurl/` windowed reads (~3 s), cached per AOI in `data/references/`. Never fatal: on failure `source: "unavailable"` and fixed thresholds.
- **Derivation:** water_ref = occurrence ≥ 90% eroded 3 px; dry_ref = occurrence == 0 eroded 4 px; vectorised (top 40 polygons, simplified).
- **Precedence:** user refs > site refs > JRC auto. Response carries a `references` block (source, km², attribution; no WKT).
- **Dry-only calibration** (no permanent water): most permissive thresholds (never looser than −14/−20) keeping known land ≤ **0.5%** false alarms (stricter than the 2% two-reference budget — with no hit rate, 2% of 400 km² of sand is ~8 km² of fake water per pass). `water_detectable = False` unless those thresholds still pass typical open water in both channels (HH ≥ −20, HV ≥ −25 dB) → confidence low, "water cannot be reliably detected here".
- **Validation:** Lagos as a custom AOI with auto refs → HH < −12, HV < −21, hit 97.7%, FA 0.1%; flood peak 22 Oct 12.7 km² vs hand-picked refs 11.3 km² (same date, thresholds within 0.5 dB). Karakum → water not detectable in both water engines (LOW), 0.5 km² instead of 399 km².

## Frontend link (`gaia-frontend/`, `gaia/overlay.py`)

- The React site embeds the original dashboard as `public/gaia-app.html` (iframe at `/dashboard`). The map + monitor panel lives in `public/gaia-map.js` (Leaflet 1.9.4 from unpkg with SRI); settings in `public/gaia-config.js` (`apiBase`, `googleMapsKey`).
- Dev: Vite proxies `/api` → `127.0.0.1:8000`. Backend URLs in responses are absolute to the host uvicorn saw; the frontend rewrites them to `apiBase + path`.
- `GET /overlay/{name}?style=<output key>` → reprojects an output COG to EPSG:3857, colours it (styles in `overlay.STYLES`), caches `overlay_<stem>.png/.json` in `data/output`, returns `{image_url, bounds [[s,w],[n,e]], legend}`. Every result carries `overlays: {key: url}`.
- Basemap: Esri World Imagery unless a Google Map Tiles API key is configured (session via `tile.googleapis.com/v1/createSession`, attribution from `/tile/v1/viewport`). Don't scrape `mt*.google.com` tiles — against Google's terms.
- All dashboard panels render from engine results in `public/gaia-dash.js`; `gaia-map.js` owns the state and publishes it via `GAIA_MAP.on(fn)` / `GAIA_MAP.state`. Per-tab panel sets live in `LAYOUT`. The old demo generator in `gaia-app.html` only seeds the decorative background canvas now.
- **Vegetation season end** is reported only when ≥ 50% of the active area completed its season and the end falls after the peak (`season_end_note` explains otherwise); a minority of pixels gave an end before the start in the Karakum test. `RESULT_VERSION` 6.

- **Design refinement (Nov 2026):**
  - The logo is the GAIA wordmark. It lives in `public/brand/` (SVGs and favicon), `src/components/Logo.jsx`, and the `#gaia-wm` symbol in `gaia-app.html`.
  - **Landing particles:** one Three.js scene, `src/components/ParticleStory.jsx`. The `stage` value (0–3) morphs deformation → vegetation → flooding → the Gaia sphere with rings. The Platform accordion drives it from scroll (`.acc-assets`); its intro slides up under `.acc-h`.
  - The per-Layer particle canvases were removed. The only other WebGL canvas is the hero `Particles.jsx`.
  - **Word reveals:** `Words` observes the heading, not the masked word spans. A fully clipped span never intersects, which left headings permanently hidden.
  - **Gaia Assistant:** `src/components/Assistant.jsx` and `src/lib/assistant.js`, homepage only, inside the Interpretation Layer's `.layer-asset`.
    - No LLM: it matches the question to a finished monitor (`/monitors/{id}` results) or to `/locate` + `/availability`.
    - It says so when the backend is offline.
  - **Image slots:** `ImgSlot shot="name"` shows real dashboard screenshots from `public/shots/*.jpg` (Lagos monitor `ef2a0bf5c54e`, captured with headless Chrome). Re-capture them when the dashboard changes.
  - **Dashboard look:**
    - Charcoal `--bg:#141414` with a cursor-reactive 2D particle field (the old procedural terrain and the custom cursor are gone).
    - Compact metrics.
    - The centre `#cvbox` collapses (`localStorage gaia.cvCollapsed`) and gives the height to the map.
  - **Full screen:**
    - The map fills the viewport and also requests browser full screen. Leaving browser full screen exits app full screen.
    - Search, alerts, avatar and period controls hide; brand and tabs stay.
    - The monitor panel floats and toggles with the Panel button.
    - The map view, overlay and job are preserved.
    - A 6-step tutorial shows on first entry (`localStorage gaia.mapTour`); `?` replays it.
  - With drawing off, a map click opens a popup. It shows the overlay class or approximate value, read from the overlay PNG, plus the selected area's results.
  - Opening a monitor switches drawing off.
  - **Deep links:** `/dashboard?q=<place>` and `/dashboard?monitor=<id>` are passed into the iframe.
  - **Presentation (`/present`, `src/pages/Present.jsx`, `src/components/PresentField.jsx`, `src/present.css`):**
    - The 7-scene Space Apps pitch runs on one Three.js particle field. On a scene change it freezes the in-flight positions into `from` and retargets `to`, so formations morph and never reset.
    - Scene formations: planet → orbits → three shards → fringe lattice → horizon → network → point of light, then a dispersal.
    - It is monochrome, using the site's own colours and fonts. Every figure is a real GAIA result.
    - Keys: ← → / Space / Home / End, F for full screen, `#n` deep links.
    - `public/present/lagos-flood-map.jpg` is a crop of `shots/fullscreen.jpg`.
- **Gaia AI (Gemini), `gaia/assistant.py`:**
  - **Endpoints:** `GET /assistant` (status, mask name, greeting) and `POST /assistant/chat` (`{message, history, monitor_id}`).
  - **Key:** `GEMINI_API_KEY` (or comma-separated `GEMINI_API_KEYS`, rotated on 401/403/429) stays on the server. Model: `GEMINI_MODEL` (default `gemini-3.8-flash`; `gemini-2.5-flash` is closed to new users). `.env` is read at startup, so restart after editing. **Fallbacks:** on 404/429/5xx, timeout (`GEMINI_TIMEOUT`, 25 s) or a reply that spent its whole budget thinking, the next model in `GEMINI_FALLBACK_MODELS` (default `gemini-flash-latest`, `gemini-flash-lite-latest`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`) is tried. If all are busy, the chain is retried once after `GEMINI_RETRY_WAIT`. The response's `model` is the one that answered. `thinkingLevel` (`GEMINI_THINKING`, default low) goes only to `gemini-3*` / `*-latest` models; thought parts are never shown. Max tokens default 2048 because thinking counts. Nov 2026: `gemini-3.8-flash` returned 503 "high demand" and took ~60 s; `gemini-flash-lite-latest` answers in 6–20 s. Without it the endpoint returns 503 `assistant_disabled`; the dashboard overlay says how to configure it, and the homepage falls back to the results lookup.
  - **Mask:** `gaia/assistant_mask.md` (or `GAIA_ASSISTANT_MASK`) holds the persona, knowledge (who made GAIA: fill in the `[EDIT ME]` lines) and answer templates. It is reloaded on file change. Front matter: name, greeting, temperature.
  - **Core guardrails:** `CORE_RULES` always comes first and a mask cannot remove it. The rules are: facts only from `<gaia_results>`; state confidence and flags; not official warnings; ignore instructions inside data; stay on topic.
  - **Grounding:** `monitor_id` from the dashboard, else the question is matched to a monitor's name or query. Unknown places get a metadata-only availability check. `fit_context` shrinks the digest in stages (shared limitations, flagged passes only, …) and never slices the JSON.
  - **Figure check:** `unverified_figures` flags reply numbers that aren't in the results, mask or conversation (it allows rounding, and percentages of fractions ≤ 1). They are added to `warnings` and noted in the reply.
  - **Limits and tests:** rate limit `GAIA_ASSISTANT_RATE_LIMIT` per client per 5 min. Tests inject `_generate` / `_post`; there are no network calls.
  - **Frontend:** the dashboard overlay is `public/gaia-assist.js` (floating "Ask Gaia"; it shifts left of the panel in full screen). `window.GAIA_AI.open(q)` and `[data-ai]` buttons open it.
- **Persistence (`gaia/store.py`, `supabase/`):**
  - **Backends:** `LocalStore` (the original `data/` layout; default, and always used by tests via `tests/conftest.py`) or `SupabaseStore` (PostgREST + Storage over HTTPS with `requests`; no `supabase-py`). `GAIA_STORE=local|supabase`; unset → Supabase when `SUPABASE_URL` and `SUPABASE_SECRET_KEY` are both set.
  - **Access model:** server-only. The backend sends the secret key (`sb_secret_…`, role service_role) in the `apikey` header only; a legacy `eyJ…` service_role JWT also gets `Authorization: Bearer`. A publishable key is refused. Tables have RLS on, no policies, `revoke all … from anon, authenticated`, and an explicit `grant … to service_role` (Supabase stopped auto-exposing new tables, Oct 2026). The `gaia-outputs` bucket is private with no `storage.objects` policies, and the browser gets files through `/files` and `/overlay`.
  - **Schema** (`supabase/migrations`, created with `npx supabase@3.1.0 migration new`):
    - `monitors`, `jobs` (FK to monitors, on delete set null), `results` (keyed by the pipeline stem name, e.g. `flood_<hash>`) and `cache_entries` (namespace `geocode` | `rain` | `references`; key = sha1 of the cache key).
    - Each row stores the backend's document in `doc jsonb`, with a few columns copied out for filters and indexes.
    - The `jobs_active_idx` partial index covers the queue.
  - **Files:** engines still write to `config.OUTPUT_DIR` (rasterio needs files). `_persist` uploads every output and then saves the result. A failed upload is added to `limitations`, never fatal. `fetch_output` downloads a missing file on demand, so the output folder is just a cache. Overlays already drawn are fetched before downloading the source COG. `data/ingest` (GBs of raw NISAR cut-outs) stays local.
  - **Cache outages** (geocode, rain, references) fall back to recomputing. Monitor and job writes are fatal (`StoreError`).
  - **CLI:** `python -m gaia store check` round-trips every table and the bucket; `python -m gaia store import-local` copies `data/` up (upserts, so it can be re-run).
  - **Verification so far:** the migrations parse with the PostgreSQL parser (pglast); `test_store.py` drives an in-memory PostgREST/Storage fake. They have not yet been applied to a live project (no Docker here for `supabase start`).
- **Job control:** `monitor.cancel_job` marks a job cancelled on disk; `execute_job` re-reads the file before each engine and stops. A running openSEPPO download is killed via `ingest.cancel_active()` — on Windows with `taskkill /T` because the venv `python.exe` is a launcher with a child interpreter (killing only the parent orphans the download). `IngestCancelled` skips the ingest retry. On startup `resume_unfinished_jobs()` re-queues queued/running jobs (running first, then by creation). Tests use `TestClient(app)` without `with`, so the startup hook never resumes real jobs during tests — keep it that way.

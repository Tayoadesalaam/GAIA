"""Layer 4 — FastAPI app.

Run:  uvicorn gaia.api:app --reload
Docs: http://127.0.0.1:8000/docs

Endpoints
    GET  /health
    GET  /sites                  named sites with reference areas
    POST /search                 AOI + time range → available NISAR GCOV dates + coverage
    POST /process                engine = water_change | flood | vegetation | deformation
                                 → output COG URLs + uncertainty contract
    GET  /files/{name}           change map COGs / quicklooks (HTTP range requests supported)

The COG can be rendered client-side with geotiff.js, or served as XYZ tiles by
pointing TiTiler at the /files URL.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

import json

from gaia import __version__, config, overlay, pipeline
from gaia.store import StoreError, get_store, save_outputs
from gaia.store import settings as store_settings
from gaia.discovery import coverage_summary, parse_aoi, search_granules
from gaia.ingest import IngestError
from gaia.pipeline import PipelineError, run_water_change

config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="GAIA backend",
    version=__version__,
    description="NISAR-based surface change intelligence with honest uncertainty metadata.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/files/{path}", name="files")
def files(path: str):
    """An output file. Served from this machine's output folder; in Supabase mode a file that isn't
    here (another machine computed it, or the folder was cleared) is fetched from the private bucket."""
    from fastapi.responses import FileResponse

    name = Path(path).name  # never leave OUTPUT_DIR
    try:
        local = get_store().fetch_output(name)
    except StoreError as exc:
        raise HTTPException(503, f"Storage unavailable: {exc}") from exc
    if local is None:
        raise HTTPException(404, "No such file")
    return FileResponse(local)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
AOIType = str | dict[str, Any] | list[float]


class _Where(BaseModel):
    site: str | None = Field(None, description="Named site, e.g. 'lagos'")
    aoi: AOIType | None = Field(None, description="WKT, GeoJSON geometry/Feature, or [minLon,minLat,maxLon,maxLat]")

    @model_validator(mode="after")
    def _one_of(self):
        if not self.site and self.aoi is None:
            raise ValueError("Provide 'site' or 'aoi'.")
        if self.site and self.site.lower() not in config.SITES:
            raise ValueError(f"Unknown site '{self.site}'. Known: {sorted(config.SITES)}")
        if self.aoi is not None:
            parse_aoi(self.aoi)
        return self

    def resolved_aoi(self) -> Any:
        return self.aoi if self.aoi is not None else config.get_site(self.site).aoi_wkt


class SearchRequest(_Where):
    start: date | None = Field(None, description="Default: one year before end")
    end: date | None = Field(None, description="Default: today")
    radar_mode: str | None = None
    min_overlap: float = Field(0.0, ge=0, le=1)


class ProcessRequest(_Where):
    engine: Literal["water_change", "flood", "vegetation", "deformation"] = "water_change"
    # water_change: two exact acquisition dates
    date1: date | None = Field(None, description="water_change: earlier acquisition date")
    date2: date | None = Field(None, description="water_change: later acquisition date")
    # flood / vegetation / deformation: a period
    start: date | None = Field(None, description="flood/vegetation/deformation: period start")
    end: date | None = Field(None, description="flood/vegetation/deformation: period end")
    track: int | None = Field(None, description="Restrict to one track (default: best track)")
    water_ref: AOIType | None = Field(None, description="Permanent water area (overrides site default)")
    dry_ref: AOIType | None = Field(None, description="Never-flooded land area (overrides site default)")
    exclude: AOIType | None = Field(None, description="Known water look-alikes to mask out")
    stable_ref: AOIType | None = Field(None, description="deformation: area assumed not to move")
    baseline: list[date] | None = Field(None, min_length=2, max_length=2,
                                        description="flood: [start, end] of a dry-season baseline")
    arid: bool | None = None

    @model_validator(mode="after")
    def _dates_for_engine(self):
        if self.engine == "water_change":
            if not (self.date1 and self.date2):
                raise ValueError("water_change needs date1 and date2.")
        elif not (self.start and self.end):
            raise ValueError(f"{self.engine} needs start and end.")
        return self


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__,
            "earthdata_token": bool(config.EARTHDATA_TOKEN and not config.EARTHDATA_TOKEN.endswith("...")),
            "store": store_settings()["mode"]}


@app.get("/sites")
def sites() -> list[dict[str, Any]]:
    return [
        {"name": s.name, "description": s.description, "aoi": parse_aoi(s.aoi_wkt).__geo_interface__,
         "water_ref": s.water_ref_wkt, "dry_ref": s.dry_ref_wkt, "exclude": s.exclude_wkt,
         "stable_ref": s.stable_ref_wkt,
         "tags": list(s.tags)}
        for s in config.SITES.values()
    ]


@app.post("/search")
def search(req: SearchRequest) -> dict[str, Any]:
    end = req.end or date.today()
    start = req.start or end - timedelta(days=365)
    if start > end:
        raise HTTPException(422, "start must be before end")
    aoi = req.resolved_aoi()
    try:
        granules = search_granules(aoi, start, end, radar_mode=req.radar_mode, min_overlap=req.min_overlap)
    except Exception as exc:  # ASF/CMR outage
        raise HTTPException(502, f"NISAR search failed: {exc}") from exc
    keep = ("granule_id", "date", "start_time", "track", "frame", "flight_direction",
            "polarizations", "radar_mode", "maturity", "aoi_overlap", "size_bytes")
    return {
        "aoi": parse_aoi(aoi).__geo_interface__,
        "coverage": coverage_summary(granules, start, end),
        "granules": [{k: g.to_dict()[k] for k in keep} for g in granules],
    }


@app.post("/process")
def process(req: ProcessRequest, request: Request) -> dict[str, Any]:
    """Runs one engine end to end. The first run for new dates streams AOI
    subsets from ASF (minutes; time-series engines longer); repeats are cached.

    * ``water_change`` — ``date1``, ``date2``
    * ``flood`` — ``start``, ``end`` (optional ``baseline``)
    * ``vegetation`` — ``start``, ``end``
    * ``deformation`` — ``start``, ``end`` (optional ``stable_ref``)
    """
    common = dict(site=req.site, aoi=req.aoi, track=req.track)
    try:
        if req.engine == "water_change":
            result = run_water_change(req.date1, req.date2, water_ref=req.water_ref, dry_ref=req.dry_ref,
                                      exclude=req.exclude, arid=req.arid, **common)
        elif req.engine == "flood":
            result = pipeline.run_flood(req.start, req.end, water_ref=req.water_ref, dry_ref=req.dry_ref,
                                        exclude=req.exclude, arid=req.arid,
                                        baseline=[str(d) for d in req.baseline] if req.baseline else None,
                                        **common)
        elif req.engine == "vegetation":
            result = pipeline.run_vegetation(req.start, req.end, **common)
        else:
            result = pipeline.run_deformation(req.start, req.end, stable_ref=req.stable_ref, **common)
    except PipelineError as exc:
        raise HTTPException(422, str(exc)) from exc
    except IngestError as exc:
        raise HTTPException(502, f"Ingestion failed: {exc}") from exc
    return _with_urls(dict(result), request)


def _with_urls(result: dict[str, Any], request: Request) -> dict[str, Any]:
    """Replace local file paths with /files URLs (never expose server paths)."""
    url = lambda p: str(request.url_for("files", path=Path(p).name)) if p else None  # noqa: E731
    for key, url_key in (("change_map_path", "change_map_url"), ("quicklook_path", "quicklook_url")):
        if key in result:
            result[url_key] = url(result.pop(key))
    outputs = result.get("outputs") or {}
    result["overlays"] = {
        k: str(request.url_for("overlay", name=Path(v).name).include_query_params(style=k))
        for k, v in outputs.items() if k in overlay.STYLES and v
    }
    result["outputs"] = {k: url(v) for k, v in outputs.items()}
    return result


@app.get("/overlay/{name}", name="overlay")
def overlay_info(name: str, style: str, request: Request) -> dict[str, Any]:
    """An engine output as a coloured, transparent Web-Mercator PNG for map display:
    ``{"image_url", "bounds": [[south, west], [north, east]], "legend"}``."""
    name = Path(name).name  # never leave OUTPUT_DIR
    if Path(name).suffix.lower() != ".tif":
        raise HTTPException(404, "Not a raster output")
    store = get_store()
    png, meta = f"overlay_{Path(name).stem}.png", f"overlay_{Path(name).stem}.json"
    try:
        # an overlay already drawn (here or by another machine) avoids fetching the full raster
        if not (config.OUTPUT_DIR / png).exists() and store.kind == "supabase":
            got_png, got_meta = store.fetch_output(png), store.fetch_output(meta)
            if got_png and got_meta:
                cached = json.loads(got_meta.read_text())
                if cached.get("style") == style:
                    cached.pop("image", None)
                    return {**cached, "image_url": str(request.url_for("files", path=png))}
        src = store.fetch_output(name)
    except StoreError as exc:
        raise HTTPException(503, f"Storage unavailable: {exc}") from exc
    if src is None:
        raise HTTPException(404, f"No such output: {name}")
    before = (config.OUTPUT_DIR / png).stat().st_mtime if (config.OUTPUT_DIR / png).exists() else None
    try:
        info = overlay.render_overlay(src, style)
    except overlay.OverlayError as exc:
        raise HTTPException(404, str(exc)) from exc
    image = Path(info.pop("image"))
    if before is None or image.stat().st_mtime != before:  # newly drawn: keep a copy in storage
        save_outputs([image, image.with_suffix(".json")])
    return {**info, "image_url": str(request.url_for("files", path=image.name))}


# --------------------------------------------------------------------------- #
# Location search + monitoring
# --------------------------------------------------------------------------- #
from fastapi.responses import JSONResponse  # noqa: E402

from gaia import monitor as mon  # noqa: E402
from gaia.geocode import LocationError, resolve_location  # noqa: E402

EngineName = Literal["water_change", "flood", "vegetation", "deformation"]


class LocationRequest(BaseModel):
    location: str | dict[str, Any] | list[float] = Field(
        ..., description="Place name ('Ibadan, Nigeria'), 'lat, lon', 'min_lon,min_lat,max_lon,max_lat', "
                         "WKT/GeoJSON, or a site name",
        examples=["Ibadan, Nigeria", "6.45, 3.40"])
    start: date | None = Field(None, description="Default: one year before end")
    end: date | None = Field(None, description="Default: today")


class MonitorRequest(LocationRequest):
    engines: list[EngineName] | None = Field(None, description="Default: every engine with enough data")


def _location_or_404(query: Any):
    try:
        return resolve_location(query)
    except LocationError as exc:
        raise HTTPException(404, {"error": "location_not_found", "message": str(exc)}) from exc


def _no_data(exc: mon.NoDataError) -> JSONResponse:
    return JSONResponse(status_code=404, content=exc.detail)


def _job_view(job: dict[str, Any] | None, request: Request) -> dict[str, Any] | None:
    if job is None:
        return None
    job = dict(job)
    job["results"] = {k: _with_urls(dict(v), request) for k, v in (job.get("results") or {}).items()}
    job["location"] = {k: v for k, v in job["location"].items() if k != "aoi_wkt"}
    job["engines"] = {k: {kk: vv for kk, vv in v.items() if kk != "trace"} for k, v in job["engines"].items()}
    job["status_url"] = str(request.url_for("job_status", job_id=job["id"]))
    job["queue_position"] = mon.queue_position(job["id"])  # 0 = running now, n = jobs ahead
    return job


@app.on_event("startup")
def _resume_jobs() -> None:
    """Jobs run in-process; pick up any the previous server left unfinished."""
    resumed = mon.resume_unfinished_jobs()
    if resumed:
        print(f"GAIA: resumed {len(resumed)} unfinished job(s): {', '.join(resumed)}", flush=True)


@app.get("/locate")
def locate(q: str) -> dict[str, Any]:
    """Resolve what the user typed to an AOI (no NISAR search)."""
    return _location_or_404(q).to_dict()


@app.post("/availability")
def availability(req: LocationRequest):
    """Is there NISAR data here? Per-engine availability. Metadata only — fast.
    404 with ``error: no_data`` when nothing usable exists."""
    loc = _location_or_404(req.location)
    try:
        avail = mon.check_availability(loc.aoi_wkt, req.start, req.end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"NISAR search failed: {exc}") from exc
    try:
        mon.require_data(loc, avail)
    except mon.NoDataError as exc:
        return _no_data(exc)
    return {"location": loc.to_dict(), "availability": avail}


@app.post("/monitor", status_code=202)
def start_monitoring(req: MonitorRequest, request: Request):
    """Enter a location → check data → start monitoring.

    * 404 ``location_not_found`` — the place can't be resolved
    * 404 ``no_data`` — no (or not enough) NISAR data; the body explains what exists
    * 202 — monitoring started; poll ``job.status_url``
    """
    try:
        out = mon.create_monitor(req.location, start=req.start, end=req.end, engines=req.engines)
    except LocationError as exc:
        raise HTTPException(404, {"error": "location_not_found", "message": str(exc)}) from exc
    except mon.NoDataError as exc:
        return _no_data(exc)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"monitor": out["monitor"], "job": _job_view(out["job"], request)}


@app.get("/monitors")
def monitors() -> list[dict[str, Any]]:
    return mon.list_monitors()


@app.get("/monitors/{monitor_id}")
def monitor_detail(monitor_id: str, request: Request) -> dict[str, Any]:
    m = mon.get_monitor(monitor_id)
    if m is None:
        raise HTTPException(404, "Unknown monitor")
    return {**m, "last_job": _job_view(mon.get_job(m["last_job_id"]) if m.get("last_job_id") else None, request)}


@app.post("/monitors/{monitor_id}/refresh")
def refresh(monitor_id: str, request: Request):
    """Check for new acquisitions; start a new job only if there are any."""
    try:
        out = mon.refresh_monitor(monitor_id)
    except KeyError:
        raise HTTPException(404, "Unknown monitor") from None
    except mon.NoDataError as exc:
        return _no_data(exc)
    if "job" in out:
        out["job"] = _job_view(out["job"], request)
    return out


@app.delete("/monitors/{monitor_id}")
def remove_monitor(monitor_id: str) -> dict[str, Any]:
    if not mon.delete_monitor(monitor_id):
        raise HTTPException(404, "Unknown monitor")
    return {"deleted": monitor_id}


@app.get("/jobs")
def jobs_queue() -> list[dict[str, Any]]:
    """Queued and running jobs, in run order (first = running now)."""
    return [{"id": j["id"], "status": j["status"], "name": j["location"].get("name"), "created": j.get("created"),
             "monitor_id": j.get("monitor_id"),
             "engines": {k: v["status"] for k, v in j["engines"].items()}} for j in mon.active_jobs()]


@app.post("/jobs/cancel-all")
def cancel_all() -> dict[str, Any]:
    """Cancel every queued and running job (a running download stops immediately)."""
    return {"cancelled": mon.cancel_all_jobs()}


@app.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request) -> dict[str, Any]:
    job = mon.cancel_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    return _job_view(job, request)


@app.get("/jobs/{job_id}", name="job_status")
def job_status(job_id: str, request: Request) -> dict[str, Any]:
    job = mon.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job")
    return _job_view(job, request)


# --------------------------------------------------------------------------- #
# Gaia AI (Gemini). The key stays on the server; answers are grounded in GAIA's
# own results and checked for figures that don't appear in them (gaia/assistant.py).
# --------------------------------------------------------------------------- #
from gaia import assistant as gaia_ai  # noqa: E402


class ChatTurn(BaseModel):
    role: Literal["user", "model"]
    text: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=gaia_ai.MAX_MESSAGE)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    monitor_id: str | None = Field(default=None, max_length=64)


@app.get("/assistant")
def assistant_status() -> dict[str, Any]:
    """Is Gaia AI configured? Model, mask name and greeting (never the key)."""
    return gaia_ai.status()


def _availability_for(place: str):
    loc = resolve_location(place)
    return loc.to_dict(), mon.check_availability(loc.aoi_wkt)


@app.post("/assistant/chat")
def assistant_chat(req: ChatRequest, request: Request) -> dict[str, Any]:
    """Ask Gaia. 503 ``assistant_disabled`` without a Gemini key; 429 when rate-limited."""
    st = gaia_ai.settings()
    if not st["enabled"]:
        raise HTTPException(503, {"error": "assistant_disabled",
                                  "message": "Gaia AI is not configured: add GEMINI_API_KEY to .env and restart the backend."})
    client = request.client.host if request.client else "unknown"
    if not gaia_ai.check_rate(client, st["rate_limit"]):
        raise HTTPException(429, {"error": "rate_limited", "message": "Too many questions in a short time. Try again in a few minutes."})
    try:
        return gaia_ai.chat(req.message, [t.model_dump() for t in req.history], req.monitor_id,
                            monitors=mon.list_monitors(), get_monitor=mon.get_monitor, get_job=mon.get_job,
                            availability=_availability_for)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except gaia_ai.AssistantError as exc:
        raise HTTPException(502, {"error": "assistant_failed", "message": str(exc)}) from exc

"""Location monitoring: availability check → background analysis → refresh.

    resolve location ─► check_availability ─► no data?  → NoDataError (clear, structured)
                                          └─► data      → job runs every available engine
    refresh(monitor)  ─► new acquisitions since last run? → new job, else "up_to_date"

Jobs run one at a time in a background thread (openSEPPO downloads are heavy)
and are persisted to ``data/jobs/<id>.json`` so status survives restarts.
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from shapely.ops import unary_union
from shapely.geometry import shape

from gaia import config
from gaia.discovery import _to_date, parse_aoi, search_granules
from gaia.geocode import Location, resolve_location
from gaia.store import get_store

ENGINES = ("water_change", "flood", "vegetation", "deformation")


class NoDataError(LookupError):
    """No usable NISAR data for the location / period. ``detail`` is JSON-ready."""

    def __init__(self, message: str, detail: dict[str, Any]):
        super().__init__(message)
        self.detail = {"error": "no_data", "message": message, **detail}


# --------------------------------------------------------------------------- #
# Availability (metadata only — fast)
# --------------------------------------------------------------------------- #
def _period(start: Any, end: Any) -> tuple[date, date]:
    e = _to_date(end) if end else date.today()
    s = _to_date(start) if start else e - timedelta(days=config.MONITOR_DEFAULT_LOOKBACK_DAYS)
    if s >= e:
        raise ValueError(f"start ({s}) must be before end ({e}).")
    return s, e


def _footprint_cover(granules, geom) -> float | None:
    fps = [shape(g.footprint) for g in granules if g.footprint]
    if not fps or geom.area == 0:
        return None
    return round(min(1.0, unary_union(fps).intersection(geom).area / geom.area), 3)


def check_availability(aoi: Any, start: Any = None, end: Any = None, *,
                       search_fn: Callable[..., list] = search_granules) -> dict[str, Any]:
    """What can GAIA do here? Per-engine availability with reasons and the
    parameters a run would use. Downloads nothing."""
    from gaia.deformation import select_chain, DeformationError
    from gaia.timeseries import TimeSeriesError, has_pols, select_track

    geom = parse_aoi(aoi)
    s, e = _period(start, end)
    gcov = search_fn(geom, s, e)
    try:
        gunw = search_fn(geom, s, e, product="GUNW")
    except Exception as exc:  # GUNW search failing must not hide GCOV results
        gunw, gunw_err = [], str(exc)
    else:
        gunw_err = None

    tracks: dict[Any, dict[str, Any]] = {}
    for g in gcov:
        t = tracks.setdefault(g.track, {"track": g.track, "flight_direction": g.flight_direction,
                                        "radar_mode": g.radar_mode, "dates": set(), "single_pol": set()})
        (t["dates"] if has_pols(g) else t["single_pol"]).add(g.date.isoformat())
    track_list = [{**t, "dates": sorted(t["dates"]), "single_pol": sorted(t["single_pol"])}
                  for t in tracks.values()]
    try:
        best_track, picked = select_track(gcov)
        best_dates = [g.date.isoformat() for g in picked]
        cover = _footprint_cover(picked, geom)
    except TimeSeriesError:
        best_track, best_dates, cover = None, [], None

    try:
        gunw_track, chain = select_chain(gunw)
    except DeformationError:
        gunw_track, chain = None, []
    chain_dates = ([chain[0].date.isoformat()] + [g.secondary_date.isoformat() for g in chain]) if chain else []

    n = len(best_dates)
    mins = config.MONITOR_MIN_DATES
    engines: dict[str, dict[str, Any]] = {}

    def gcov_engine(name: str, params: dict[str, Any]) -> None:
        ok = n >= mins[name]
        engines[name] = {
            "available": ok,
            "reason": (f"{n} dual-pol dates on track {best_track}" if ok else
                       f"needs ≥ {mins[name]} dual-pol dates on one track; found {n}"
                       + (f" (track {best_track})" if best_track is not None else "")),
            "params": params if ok else None,
        }

    gcov_engine("water_change", {"date1": best_dates[-2], "date2": best_dates[-1], "track": best_track}
                if n >= 2 else {})
    gcov_engine("flood", {"start": s.isoformat(), "end": e.isoformat(), "track": best_track})
    gcov_engine("vegetation", {"start": s.isoformat(), "end": e.isoformat(), "track": best_track})
    ok = len(chain) >= config.MONITOR_MIN_GUNW_PAIRS
    engines["deformation"] = {
        "available": ok,
        "reason": (f"{len(chain)} contiguous interferograms on track {gunw_track}" if ok else
                   f"needs ≥ {config.MONITOR_MIN_GUNW_PAIRS} contiguous GUNW interferograms; found "
                   f"{len(chain)}" + (f" ({gunw_err})" if gunw_err else "")),
        "params": {"start": s.isoformat(), "end": e.isoformat(), "track": gunw_track} if ok else None,
    }

    notes = []
    if cover is not None and cover < 0.95:
        notes.append(f"Acquisitions on track {best_track} cover only {cover:.0%} of the area; "
                     "results will have no-data where there is no coverage.")
    if any(t["single_pol"] for t in track_list):
        notes.append("Some passes are single-polarization (HH only) and cannot be used.")
    for g0, g1 in config.KNOWN_DATA_GAPS:
        if s <= g1 and e >= g0:
            notes.append(f"NISAR data gap {g0} to {g1} falls in this period.")

    latest = max([d for t in track_list for d in t["dates"]] + chain_dates[-1:], default=None)
    return {
        "aoi": geom.__geo_interface__,
        "period": [s.isoformat(), e.isoformat()],
        "gcov": {"granules_found": len(gcov), "tracks": track_list, "best_track": best_track,
                 "best_track_dates": best_dates, "best_track_aoi_cover": cover},
        "gunw": {"pairs_found": len(gunw), "best_track": gunw_track, "chain_length": len(chain),
                 "chain_dates": chain_dates, "error": gunw_err},
        "engines": engines,
        "any_available": any(v["available"] for v in engines.values()),
        "latest_acquisition": latest,
        "notes": notes,
    }


def require_data(location: Location, availability: dict[str, Any],
                 engines: Sequence[str] | None = None) -> list[str]:
    """Engines to run, or raise NoDataError explaining why nothing can run."""
    wanted = list(engines or ENGINES)
    unknown = [x for x in wanted if x not in ENGINES]
    if unknown:
        raise ValueError(f"Unknown engine(s) {unknown}; choose from {list(ENGINES)}.")
    runnable = [x for x in wanted if availability["engines"][x]["available"]]
    if runnable:
        return runnable
    s, e = availability["period"]
    nothing = availability["gcov"]["granules_found"] == 0 and availability["gunw"]["pairs_found"] == 0
    msg = (f"No NISAR data found for '{location.name}' between {s} and {e}." if nothing else
           f"NISAR data exists for '{location.name}' but not enough for {', '.join(wanted)}.")
    suggestions = []
    if nothing:
        suggestions += ["NISAR science products start in mid-2026; earlier periods have no data.",
                        "Check the location is correct (see 'location') and widen the date range.",
                        "Coverage can be missing over open ocean, polar regions or during data gaps."]
    else:
        suggestions += [f"{k}: {v['reason']}" for k, v in availability["engines"].items() if k in wanted]
        suggestions.append("Widen the date range to include more passes.")
    raise NoDataError(msg, {"location": location.to_dict(), "availability": availability,
                            "suggestions": suggestions})


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gaia-job")
_jobs_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Jobs and monitors live in gaia.store: data/ files locally, or Supabase tables when configured.
def save_job(job: dict[str, Any]) -> None:
    job["updated"] = _now()
    with _jobs_lock:
        get_store().put_job(job)


def get_job(job_id: str) -> dict[str, Any] | None:
    return get_store().get_job(job_id)


def _run_engine(name: str, aoi_wkt: str, params: dict[str, Any], site: str | None) -> dict[str, Any]:
    from gaia import pipeline

    common = dict(site=site, aoi=None if site else aoi_wkt, track=params.get("track"))
    if name == "water_change":
        return pipeline.run_water_change(params["date1"], params["date2"], **common)
    run = {"flood": pipeline.run_flood, "vegetation": pipeline.run_vegetation,
           "deformation": pipeline.run_deformation}[name]
    return run(params["start"], params["end"], **common)


def _cancelled(job_id: str) -> bool:
    on_disk = get_job(job_id)
    return bool(on_disk and on_disk.get("status") == "cancelled")


def execute_job(job: dict[str, Any], runner: Callable[..., dict[str, Any]] = _run_engine) -> dict[str, Any]:
    """Run each engine in turn; one failing engine doesn't stop the others.
    A job cancelled via :func:`cancel_job` stops before its next engine."""
    from gaia.ingest import IngestCancelled, clear_cancel

    if _cancelled(job["id"]):
        return get_job(job["id"])
    job["status"] = "running"
    save_job(job)
    for name, info in job["engines"].items():
        if info["status"] != "queued":
            continue
        if _cancelled(job["id"]):
            return get_job(job["id"])
        clear_cancel()
        info.update(status="running", started=_now())
        save_job(job)
        try:
            result = runner(name, job["location"]["aoi_wkt"], info["params"], job["location"].get("site"))
            info.update(status="done", finished=_now(), confidence=result.get("confidence"))
            job["results"][name] = result
        except IngestCancelled:
            info.update(status="cancelled", finished=_now())
        except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
            info.update(status="failed", finished=_now(), error=str(exc),
                        trace=traceback.format_exc(limit=3))
        if _cancelled(job["id"]):  # cancelled while this engine ran: keep what finished, stay cancelled
            on_disk = get_job(job["id"])
            on_disk["engines"][name] = info
            if name in job["results"]:
                on_disk["results"][name] = job["results"][name]
            save_job(on_disk)
            return on_disk
        save_job(job)
    states = {i["status"] for i in job["engines"].values()}
    job["status"] = ("done" if states <= {"done", "skipped"} else
                     "cancelled" if "cancelled" in states else
                     "failed" if "done" not in states else "partial")
    save_job(job)
    if job.get("monitor_id"):
        _update_monitor(job["monitor_id"], last_job_id=job["id"], last_run_status=job["status"])
    return job


def start_job(location: Location, availability: dict[str, Any], engines: Sequence[str] | None = None, *,
              monitor_id: str | None = None, wait: bool = False,
              runner: Callable[..., dict[str, Any]] = _run_engine) -> dict[str, Any]:
    runnable = require_data(location, availability, engines)
    wanted = list(engines or ENGINES)
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": "monitor",
        "monitor_id": monitor_id,
        "status": "queued",
        "created": _now(),
        "location": {**location.to_dict(), "aoi_wkt": location.aoi_wkt},
        "period": availability["period"],
        "availability": availability,
        "engines": {
            name: ({"status": "queued", "params": availability["engines"][name]["params"]}
                   if name in runnable else
                   {"status": "skipped", "reason": availability["engines"][name]["reason"]})
            for name in wanted
        },
        "results": {},
    }
    save_job(job)
    if wait:
        return execute_job(job, runner)
    _executor.submit(execute_job, job, runner)
    return job


def active_jobs() -> list[dict[str, Any]]:
    """Queued and running jobs, in the order they will run."""
    out = get_store().list_jobs(("queued", "running"))
    return sorted(out, key=lambda j: (j.get("status") != "running", j.get("created") or ""))


def queue_position(job_id: str) -> int | None:
    """0 = running now, n = n jobs ahead; None if the job isn't waiting."""
    ids = [j["id"] for j in active_jobs()]
    return ids.index(job_id) if job_id in ids else None


def cancel_job(job_id: str) -> dict[str, Any] | None:
    """Cancel a queued or running job. A running download is stopped immediately;
    engines that already finished keep their results."""
    from gaia.ingest import cancel_active

    job = get_job(job_id)
    if job is None or job.get("status") not in ("queued", "running"):
        return job
    was_running = job["status"] == "running"
    for info in job["engines"].values():
        if info["status"] in ("queued", "running"):
            info.update(status="cancelled", finished=_now())
    job["status"] = "cancelled"
    save_job(job)
    if was_running:
        cancel_active()
    if job.get("monitor_id"):
        _update_monitor(job["monitor_id"], last_run_status="cancelled")
    return job


def cancel_all_jobs() -> list[str]:
    return [j["id"] for j in active_jobs() if cancel_job(j["id"])]


def resume_unfinished_jobs(runner: Callable[..., dict[str, Any]] = _run_engine, *,
                           submit: Callable[..., Any] | None = None) -> list[str]:
    """Re-queue jobs a previous server process left queued/running (e.g. after a restart).

    Engines already ``done`` keep their results; an engine that was mid-run is run again
    (finished AOI subsets are cached, so only the interrupted work repeats). Oldest first."""
    submit = submit or _executor.submit
    resumed = []
    pending = get_store().list_jobs(("queued", "running"))
    # the job that was mid-run goes first, then queued ones in the order they were created
    pending.sort(key=lambda j: (j.get("status") != "running", j.get("created") or ""))
    for job in pending:
        for info in job["engines"].values():
            if info["status"] == "running":
                info.update(status="queued", note="interrupted by a server restart; re-run")
        job["status"] = "queued"
        save_job(job)
        submit(execute_job, job, runner)
        resumed.append(job["id"])
    return resumed


# --------------------------------------------------------------------------- #
# Monitors (registered locations that can be refreshed)
# --------------------------------------------------------------------------- #
def _update_monitor(monitor_id: str, **fields: Any) -> None:
    with _jobs_lock:
        mon = get_store().get_monitor(monitor_id)
        if mon is not None:
            mon.update(fields, updated=_now())
            get_store().put_monitor(mon)


def list_monitors() -> list[dict[str, Any]]:
    return get_store().list_monitors()


def get_monitor(monitor_id: str) -> dict[str, Any] | None:
    return get_store().get_monitor(monitor_id)


def delete_monitor(monitor_id: str) -> bool:
    return get_store().delete_monitor(monitor_id)


def create_monitor(query: Any, *, start: Any = None, end: Any = None,
                   engines: Sequence[str] | None = None, wait: bool = False,
                   geocode_fn=None, search_fn=search_granules,
                   runner: Callable[..., dict[str, Any]] = _run_engine) -> dict[str, Any]:
    """Resolve → check → (NoDataError | register monitor + start first job)."""
    loc = resolve_location(query, geocode_fn=geocode_fn)
    avail = check_availability(loc.aoi_wkt, start, end, search_fn=search_fn)
    require_data(loc, avail, engines)  # raise before registering anything
    mid = uuid.uuid4().hex[:12]
    get_store().put_monitor({
        "id": mid, "query": loc.query, "name": loc.name, "location": loc.to_dict(),
        "aoi_wkt": loc.aoi_wkt, "engines": list(engines or ENGINES),
        "start": str(start) if start else None, "created": _now(),
        "latest_acquisition": avail["latest_acquisition"], "last_job_id": None,
    })
    job = start_job(loc, avail, engines, monitor_id=mid, wait=wait, runner=runner)
    _update_monitor(mid, last_job_id=job["id"])
    return {"monitor": get_monitor(mid), "job": job}


def refresh_monitor(monitor_id: str, *, wait: bool = False, search_fn=search_granules,
                    runner: Callable[..., dict[str, Any]] = _run_engine) -> dict[str, Any]:
    """Re-check a monitor; start a new job only if newer acquisitions exist."""
    mon = get_monitor(monitor_id)
    if mon is None:
        raise KeyError(monitor_id)
    loc = Location(**{k: mon["location"][k] for k in
                      ("query", "name", "source", "lat", "lon", "aoi_wkt", "area_km2", "notes", "site", "osm")})
    avail = check_availability(loc.aoi_wkt, mon.get("start"), None, search_fn=search_fn)
    latest, previous = avail["latest_acquisition"], mon.get("latest_acquisition")
    if latest is None or (previous and latest <= previous):
        _update_monitor(monitor_id, last_checked=_now())
        return {"status": "up_to_date", "latest_acquisition": previous,
                "message": f"No new NISAR acquisitions since {previous}.", "monitor": get_monitor(monitor_id)}
    job = start_job(loc, avail, mon["engines"], monitor_id=monitor_id, wait=wait, runner=runner)
    _update_monitor(monitor_id, latest_acquisition=latest, last_checked=_now(), last_job_id=job["id"])
    return {"status": "new_data", "previous_acquisition": previous, "latest_acquisition": latest,
            "job": job, "monitor": get_monitor(monitor_id)}


__all__ = ["NoDataError", "check_availability", "create_monitor", "delete_monitor", "execute_job",
           "get_job", "get_monitor", "list_monitors", "refresh_monitor", "require_data", "start_job"]

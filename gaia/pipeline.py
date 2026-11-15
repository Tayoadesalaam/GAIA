"""End-to-end pipelines for every GAIA engine.

    water_change  two dates → new / receded water             (GCOV)
    flood         period    → flood extent per pass + peak     (GCOV time series)
    vegetation    period    → growing season / planting window (GCOV time series)
    deformation   period    → LOS displacement + velocity      (GUNW interferograms)

Each ``run_*`` returns the ``/process`` response contract (CLAUDE.md → API
Response Contract): aoi, engine, dates, outputs, coverage, flags, calibration
or method, stats, limitations, confidence (+ reasons). Results are cached.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from gaia import config, flags
from gaia.store import get_store, save_outputs
from gaia.discovery import Granule, _to_date, coverage_summary, parse_aoi, search_granules
from gaia.ingest import ingest_granule
from gaia.process import detect_water_change
from gaia.rain import rain_flag

RESULT_VERSION = 6  # bump to invalidate cached results when the method changes


class PipelineError(ValueError):
    """The request can't be served (e.g. no acquisition on a requested date)."""


def resolve_references(geom, water_ref: Any, dry_ref: Any, *, from_site: bool,
                       auto: bool = True) -> tuple[Any, Any, dict[str, Any]]:
    """Where calibration references come from: user > site > JRC GSW (auto).
    Returns (water_ref, dry_ref, summary for the response; WKT left out)."""
    if water_ref or dry_ref:
        src = "site" if from_site else "user"
        return water_ref, dry_ref, {"source": src, "water_ref": bool(water_ref), "dry_ref": bool(dry_ref),
                                    "notes": []}
    if not auto:
        return None, None, {"source": "none", "water_ref": False, "dry_ref": False, "notes": []}
    from gaia.references import auto_references

    r = auto_references(geom)
    summary = {k: v for k, v in r.items() if k not in ("water_ref", "dry_ref")}
    summary.update(water_ref=bool(r.get("water_ref")), dry_ref=bool(r.get("dry_ref")))
    return r.get("water_ref"), r.get("dry_ref"), summary


def _rank(g: Granule) -> tuple:
    maturity = config.MATURITY_ORDER.index(g.maturity) if g.maturity in config.MATURITY_ORDER else -1
    return (g.aoi_overlap or 0, maturity)


def select_pair(granules: list[Granule], d1: date, d2: date,
                track: int | None = None) -> tuple[Granule, Granule]:
    """Pick one granule per date from the same track (best AOI overlap, then maturity)."""
    pool = [g for g in granules if (track is None or g.track == track)
            and (not g.polarizations or {"HH", "HV"} <= set(g.polarizations))]
    on = lambda d: [g for g in pool if g.date == d]  # noqa: E731
    first, second = on(d1), on(d2)
    shared = {g.track for g in first} & {g.track for g in second}
    if not shared:
        by_track: dict[Any, list[str]] = {}
        for g in pool:
            by_track.setdefault(g.track, []).append(g.date.isoformat())
        hint = "; ".join(f"track {t}: {', '.join(ds)}" for t, ds in sorted(by_track.items(), key=str))
        raise PipelineError(
            f"No same-track NISAR GCOV pair for {d1} and {d2}"
            + (f" on track {track}" if track is not None else "")
            + (f". Available dates — {hint}" if hint else ". No acquisitions in this range."))
    best_track = max(shared, key=lambda t: max(_rank(g) for g in first + second if g.track == t))
    pick = lambda gs: max((g for g in gs if g.track == best_track), key=_rank)  # noqa: E731
    return pick(first), pick(second)


def _job_key(aoi_wkt: str, d1: date, d2: date, track: int | None,
             water_ref: Any, dry_ref: Any, exclude: Any) -> str:
    raw = json.dumps([RESULT_VERSION, aoi_wkt, str(d1), str(d2), track,
                      str(water_ref), str(dry_ref), str(exclude)])
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def run_water_change(
    date1: str | date,
    date2: str | date,
    *,
    site: str | None = None,
    aoi: Any | None = None,
    track: int | None = None,
    water_ref: Any | None = None,
    dry_ref: Any | None = None,
    exclude: Any | None = None,
    arid: bool | None = None,
    out_dir: Path | str | None = None,
    use_cache: bool = True,
    auto_refs: bool = True,
) -> dict[str, Any]:
    """Detect water change between two dates over a site or custom AOI.

    Without site or user references, calibration areas come from JRC Global
    Surface Water (``auto_refs``).

    Site defaults (water_ref, dry_ref, exclude, arid) apply when ``site`` is
    given; explicit arguments override them.
    """
    if site:
        s = config.get_site(site)
        aoi = aoi or s.aoi_wkt
        water_ref = water_ref or s.water_ref_wkt
        dry_ref = dry_ref or s.dry_ref_wkt
        exclude = exclude or s.exclude_wkt
        arid = s.arid if arid is None else arid
    if aoi is None:
        raise PipelineError("Provide a site or an AOI.")
    geom = parse_aoi(aoi)
    d1, d2 = _to_date(date1), _to_date(date2)
    if d1 >= d2:
        raise PipelineError(f"date1 ({d1}) must be before date2 ({d2}).")
    water_ref, dry_ref, references = resolve_references(geom, water_ref, dry_ref,
                                                        from_site=bool(site), auto=auto_refs)

    out_dir = Path(out_dir or config.OUTPUT_DIR)
    key = _job_key(geom.wkt, d1, d2, track, water_ref, dry_ref, exclude)
    stem = f"water_change_{d1}_{d2}_{key}"
    if use_cache and (cached := get_store().get_result(out_dir / stem)) is not None:
        cached["cached"] = True
        return cached

    # 1. Discovery (also feeds the coverage block)
    granules = search_granules(geom, d1, d2)
    coverage = coverage_summary(granules, d1, d2)
    g1, g2 = select_pair(granules, d1, d2, track)

    # 2. Ingestion
    ing1 = ingest_granule(g1, geom)
    ing2 = ingest_granule(g2, geom)
    side1 = json.loads(Path(ing1.sidecar).read_text())
    side2 = json.loads(Path(ing2.sidecar).read_text())

    # 3. Processing
    proc = detect_water_change(side1, side2, geom, out_dir / f"{stem}.tif",
                               water_ref=water_ref, dry_ref=dry_ref, exclude=exclude)

    # 4. Rain + flags + confidence
    rain = {d: rain_flag(d, geom) for d in proc["dates"]}
    per_date = flags.per_date_flags(proc["per_date"], rain)
    f = flags.build_flags(per_date, arid=arid, tracks=[g1.track, g2.track])
    f["water_detectable"] = flags.water_detectable(proc["calibrations"])
    confidence, reasons = flags.assess_confidence(f, proc["calibrations"], proc["stats"])
    limitations = references["notes"] + flags.build_limitations(f, per_date, coverage["known_gaps"])

    cals = proc["calibrations"]
    calibration = dict(cals[0]) if len(cals) == 1 else {"method": cals[0]["method"]}
    calibration["per_mode"] = cals

    response = {
        "aoi": geom.__geo_interface__,
        "site": site,
        "engine": "water_change",
        "dates": proc["dates"],
        "change_map_url": None,  # filled in by the API
        "change_map_path": proc["change_map_path"],
        "quicklook_path": proc["quicklook_path"],
        "outputs": {"change_map": proc["change_map_path"],
                    **({"quicklook": proc["quicklook_path"]} if proc["quicklook_path"] else {})},
        "classes": {"0": "unchanged", "1": "new_water", "2": "receded_water", "255": "nodata"},
        "coverage": {
            "granules_found": coverage["granules_found"],
            "granules_used": 2,
            "date_range": coverage["date_range"],
            "requested_range": coverage["requested_range"],
            "gaps": coverage["known_gaps"] + coverage["observed_gaps"],
            "known_gaps": coverage["known_gaps"],
            "observed_gaps": coverage["observed_gaps"],
            "tracks": coverage["tracks"],
            "notes": coverage["notes"],
        },
        "granules_used": [
            {k: g.to_dict()[k] for k in ("granule_id", "date", "track", "frame", "flight_direction",
                                          "radar_mode", "maturity", "aoi_overlap", "url")}
            for g in (g1, g2)
        ],
        "flags": f,
        "per_date": per_date,
        "references": references,
        "calibration": calibration,
        "stats": proc["stats"],
        "limitations": limitations,
        "confidence": confidence,
        "confidence_reasons": reasons,
        "cached": False,
    }
    _persist("water_change", out_dir / stem, response)
    return response


# --------------------------------------------------------------------------- #
# Time-series engines
# --------------------------------------------------------------------------- #
def _site_defaults(site: str | None, aoi: Any, **overrides: Any) -> tuple[Any, dict[str, Any]]:
    refs: dict[str, Any] = {"water_ref": None, "dry_ref": None, "exclude": None,
                            "stable_ref": None, "arid": None}
    if site:
        s = config.get_site(site)
        aoi = aoi or s.aoi_wkt
        refs.update(water_ref=s.water_ref_wkt, dry_ref=s.dry_ref_wkt, exclude=s.exclude_wkt,
                    stable_ref=s.stable_ref_wkt, arid=s.arid)
    refs.update({k: v for k, v in overrides.items() if v is not None})
    if aoi is None:
        raise PipelineError("Provide a site or an AOI.")
    return aoi, refs


def _cached_run(engine: str, key_parts: list[Any], out_dir: Path, use_cache: bool,
                compute) -> dict[str, Any]:
    raw = json.dumps([RESULT_VERSION, engine, *map(str, key_parts)])
    stem = out_dir / f"{engine}_{hashlib.sha1(raw.encode()).hexdigest()[:10]}"
    if use_cache and (cached := get_store().get_result(stem)) is not None:
        cached["cached"] = True
        return cached
    response = compute(stem)
    response["cached"] = False
    _persist(engine, stem, response)
    return response


def _persist(engine: str, stem: Path, response: dict[str, Any]) -> None:
    """Save a result: output rasters to the bucket first (Supabase mode), then the result itself.
    A failed upload keeps the local file and is disclosed in the result's limitations."""
    files = list((response.get("outputs") or {}).values()) + [response.get("quicklook_path")]
    failed = save_outputs(files)
    if failed:
        response.setdefault("limitations", []).append(
            f"{len(failed)} output file(s) could not be uploaded to storage and exist only on the server "
            f"that computed them: {', '.join(failed)}.")
    Path(stem).parent.mkdir(parents=True, exist_ok=True)
    get_store().put_result(stem, engine, response)


def _range(start: Any, end: Any) -> tuple[date, date]:
    s, e = _to_date(start), _to_date(end)
    if s >= e:
        raise PipelineError(f"start ({s}) must be before end ({e}).")
    return s, e


def _coverage_block(coverage: dict[str, Any], used: int) -> dict[str, Any]:
    return {
        "granules_found": coverage["granules_found"],
        "granules_used": used,
        "date_range": coverage["date_range"],
        "requested_range": coverage["requested_range"],
        "gaps": coverage["known_gaps"] + coverage["observed_gaps"],
        "known_gaps": coverage["known_gaps"],
        "observed_gaps": coverage["observed_gaps"],
        "tracks": coverage["tracks"],
        "track_used": coverage.get("track_used"),
        "notes": coverage["notes"],
    }


def run_flood(start: Any, end: Any, *, site: str | None = None, aoi: Any | None = None,
              track: int | None = None, water_ref: Any = None, dry_ref: Any = None,
              exclude: Any = None, arid: bool | None = None, baseline: list[str] | None = None,
              out_dir: Path | str | None = None, use_cache: bool = True,
              auto_refs: bool = True, _stack_fn=None) -> dict[str, Any]:
    """Flood extent per pass over [start, end], peak flood and normal water.
    Without site/user references, calibration areas come from JRC GSW."""
    from gaia.flood import run_flood as engine
    from gaia.timeseries import TimeSeriesError, build_stack

    aoi, refs = _site_defaults(site, aoi, water_ref=water_ref, dry_ref=dry_ref,
                               exclude=exclude, arid=arid)
    geom = parse_aoi(aoi)
    s, e = _range(start, end)
    out_dir = Path(out_dir or config.OUTPUT_DIR)
    refs["water_ref"], refs["dry_ref"], references = resolve_references(
        geom, refs["water_ref"], refs["dry_ref"], from_site=bool(site), auto=auto_refs)

    def compute(stem: Path) -> dict[str, Any]:
        try:
            stack, coverage, rain = (_stack_fn or build_stack)(
                geom, s, e, track=track, resolution_m=config.FLOOD_RESOLUTION_M)
            body = engine(stack, rain, geom, stem, water_ref=refs["water_ref"],
                          dry_ref=refs["dry_ref"], exclude=refs["exclude"], baseline=baseline)
        except (TimeSeriesError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc
        per = body["per_date"]
        f = {
            "rain_affected": flags._any([p["rain_affected"] for p in per]),
            "wind_affected": flags._any([p["wind_affected"] for p in per]),
            "rain_affected_dates": [p["date"] for p in per if p["rain_affected"]],
            "wind_affected_dates": [p["date"] for p in per if p["wind_affected"]],
            "radar_mode": flags.radar_mode_label([p["radar_mode"] for p in per]),
            "maturity": flags.least_mature([p["maturity"] for p in per]),
            "cross_mode": len({p["radar_mode"] for p in per}) > 1,
            "arid": refs["arid"],
            "water_detectable": flags.water_detectable(body["calibrations"]),
        }
        conf, reasons = body["confidence"], list(body["confidence_reasons"])
        if refs["arid"]:
            c = flags.Confidence(conf)
            c.cap("medium", "Arid AOI: desert surfaces produce false water detections.")
            conf, reasons = c.level, reasons + c.reasons
        cals = body["calibrations"]
        calibration = dict(cals[0]) if len(cals) == 1 else {"method": cals[0]["method"]}
        calibration["per_mode"] = cals
        return {
            "aoi": geom.__geo_interface__, "site": site, "engine": "flood",
            "dates": body["dates"], "outputs": body["outputs"],
            "classes": {"0": "dry", "1": "normal_water", "2": "flooded_open_water",
                        "3": "flooded_vegetation", "255": "nodata"},
            "coverage": _coverage_block(coverage, len(body["dates"])),
            "flags": f, "per_date": per, "references": references,
            "calibration": calibration, "stats": body["stats"],
            "limitations": references["notes"] + flags.build_limitations(
                f, per, coverage["known_gaps"], config.FLOOD_LIMITATIONS),
            "confidence": conf, "confidence_reasons": reasons,
        }

    return _cached_run("flood", [geom.wkt, s, e, track, refs, baseline], out_dir, use_cache, compute)


def run_vegetation(start: Any, end: Any, *, site: str | None = None, aoi: Any | None = None,
                   track: int | None = None, out_dir: Path | str | None = None,
                   use_cache: bool = True, _stack_fn=None) -> dict[str, Any]:
    """Growing season (start / peak / end) and estimated planting window."""
    from gaia.timeseries import TimeSeriesError, build_stack
    from gaia.vegetation import run_vegetation as engine

    aoi, _ = _site_defaults(site, aoi)
    geom = parse_aoi(aoi)
    s, e = _range(start, end)
    out_dir = Path(out_dir or config.OUTPUT_DIR)

    def compute(stem: Path) -> dict[str, Any]:
        try:
            stack, coverage, rain = (_stack_fn or build_stack)(
                geom, s, e, track=track, resolution_m=config.VEG_RESOLUTION_M, min_dates=4)
            body = engine(stack, rain, geom, stem)
        except (TimeSeriesError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc
        specific = [f"{p['date']}: rain-affected pass ({p['rain_mm_3day']} mm in 3 days); excluded."
                    for p in body["per_date"] if p["rain_affected"]]
        specific += [f"NISAR data gap {a} to {b} overlaps the requested period."
                     for a, b in coverage["known_gaps"]]
        return {
            "aoi": geom.__geo_interface__, "site": site, "engine": "vegetation",
            "dates": body["dates"], "outputs": body["outputs"],
            "classes": {"0": "no_seasonal_signal", "1": "full_season_observed",
                        "2": "season_end_not_observed", "3": "season_started_before_record",
                        "255": "nodata"},
            "season": body["season"],
            "coverage": _coverage_block(coverage, len(body["timeseries"])),
            "flags": body["flags"], "per_date": body["per_date"], "timeseries": body["timeseries"],
            "calibration": body["method"], "method": body["method"],
            "stats": {"class_area_km2": body["class_area_km2"], "class_fraction": body["class_fraction"]},
            "limitations": specific + config.VEGETATION_LIMITATIONS,
            "confidence": body["confidence"], "confidence_reasons": body["confidence_reasons"],
        }

    return _cached_run("vegetation", [geom.wkt, s, e, track], out_dir, use_cache, compute)


def run_deformation(start: Any, end: Any, *, site: str | None = None, aoi: Any | None = None,
                    track: int | None = None, stable_ref: Any = None,
                    out_dir: Path | str | None = None, use_cache: bool = True,
                    _engine=None) -> dict[str, Any]:
    """LOS displacement time series + velocity from GUNW interferograms."""
    from gaia.deformation import DeformationError
    from gaia.deformation import run_deformation as engine

    aoi, refs = _site_defaults(site, aoi, stable_ref=stable_ref)
    geom = parse_aoi(aoi)
    s, e = _range(start, end)
    out_dir = Path(out_dir or config.OUTPUT_DIR)

    def compute(stem: Path) -> dict[str, Any]:
        try:
            body = (_engine or engine)(geom, s, e, stem, track=track, stable_ref=refs["stable_ref"])
        except (DeformationError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc
        cov = body["coverage"]
        specific = [f"NISAR data gap {a} to {b} overlaps the requested period."
                    for a, b in cov["known_gaps"]]
        if body["stats"]["span_days"] < 365:
            specific.append(f"Only {body['stats']['span_days']} days of interferograms: velocities are "
                            "extrapolated to mm/yr and must not be read as long-term rates.")
        return {
            "aoi": geom.__geo_interface__, "site": site, "engine": "deformation",
            "dates": body["dates"], "outputs": body["outputs"],
            "coverage": {**_coverage_block(cov, body["stats"]["interferograms"]),
                         "pairs_found": cov["granules_found"], "track_used": body["track"]},
            "flags": body["flags"], "interferograms": body["interferograms"],
            "calibration": body["method"], "method": body["method"], "stats": body["stats"],
            "limitations": specific + config.DEFORMATION_LIMITATIONS,
            "confidence": body["confidence"], "confidence_reasons": body["confidence_reasons"],
        }

    return _cached_run("deformation", [geom.wkt, s, e, track, refs["stable_ref"]],
                       out_dir, use_cache, compute)


ENGINES = ("water_change", "flood", "vegetation", "deformation")

__all__ = ["ENGINES", "PipelineError", "run_deformation", "run_flood", "run_vegetation",
           "run_water_change", "select_pair"]

"""GCOV time series: discover → ingest (cached) → aligned stack.

Shared by the flood and vegetation engines. All dates come from one track
(same look geometry) and are resampled onto one grid by block-averaging in
linear power, so the stack is light enough to hold in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from gaia import config
from gaia.discovery import Granule, coverage_summary, parse_aoi, search_granules
from gaia.ingest import ingest_granule, load_sidecar
from gaia.rain import rain_flag


class TimeSeriesError(ValueError):
    pass


@dataclass
class Stack:
    dates: list[str]
    hh: np.ndarray  # (T, H, W) linear power, NaN = invalid
    hv: np.ndarray
    transform: Any
    crs: Any
    meta: list[dict[str, Any]] = field(default_factory=list)  # per-date granule metadata

    @property
    def pixel_km2(self) -> float:
        return abs(self.transform.a * self.transform.e) / 1e6

    @property
    def shape(self) -> tuple[int, int]:
        return self.hh.shape[1:]


def has_pols(g: Granule, required: Sequence[str] = config.INGEST_POLARIZATIONS) -> bool:
    """True if the granule carries every required polarization (unknown → assume yes)."""
    return not g.polarizations or all(p in g.polarizations for p in required)


def select_track(granules: Sequence[Granule], track: int | None = None) -> tuple[int | None, list[Granule]]:
    """Track with the most distinct dual-pol dates (ties: better maturity), one granule per date.
    Single-pol acquisitions (no HV) are skipped."""
    rank = {m: i for i, m in enumerate(config.MATURITY_ORDER)}
    by_track: dict[Any, dict[str, Granule]] = {}
    for g in granules:
        if (track is not None and g.track != track) or not has_pols(g):
            continue
        day = by_track.setdefault(g.track, {})
        cur = day.get(g.date.isoformat())
        key = lambda x: (x.aoi_overlap or 0, rank.get(x.maturity, -1))  # noqa: E731
        if cur is None or key(g) > key(cur):
            day[g.date.isoformat()] = g
    if not by_track:
        raise TimeSeriesError("No NISAR GCOV acquisitions for this AOI/time range"
                              + (f" on track {track}" if track is not None else "") + ".")
    # Score = dates weighted by how much of the AOI each covers, so a track with
    # 11 passes over 59% of the AOI loses to one with 10 passes over all of it.
    def score(t: Any) -> tuple:
        gs = by_track[t].values()
        covered = sum(g.aoi_overlap if g.aoi_overlap is not None else 1.0 for g in gs)
        return (round(covered, 3), len(by_track[t]), sum(rank.get(g.maturity, -1) for g in gs))

    best = max(by_track, key=score)
    return best, [by_track[best][d] for d in sorted(by_track[best])]


def target_grid(path: str, resolution_m: float):
    """Grid of ``path`` coarsened to ``resolution_m`` (same origin and CRS)."""
    import rasterio
    from rasterio.transform import Affine

    with rasterio.open(path) as src:
        res = max(resolution_m, abs(src.transform.a))
        width = int(np.ceil(src.width * abs(src.transform.a) / res))
        height = int(np.ceil(src.height * abs(src.transform.e) / res))
        t = src.transform
        return Affine(res, 0, t.c, 0, -res, t.f), (height, width), src.crs


def read_onto(path: str, transform, shape, crs) -> np.ndarray:
    """Read band 1 averaged (linear power) onto the target grid."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    dst = np.full(shape, np.nan, dtype="float32")
    with rasterio.open(path) as src:
        reproject(source=rasterio.band(src, 1), destination=dst, src_nodata=src.nodata,
                  dst_transform=transform, dst_crs=crs, dst_nodata=np.nan,
                  resampling=Resampling.average)
    dst[~(dst > 0)] = np.nan
    return dst


def load_stack(sidecars: Sequence[dict[str, Any]], resolution_m: float) -> Stack:
    if not sidecars:
        raise TimeSeriesError("No dates to stack.")
    transform, shape, crs = target_grid(sidecars[0]["paths"]["HH"], resolution_m)
    hh = np.stack([read_onto(s["paths"]["HH"], transform, shape, crs) for s in sidecars])
    hv = np.stack([read_onto(s["paths"]["HV"], transform, shape, crs) for s in sidecars])
    meta = []
    for s in sidecars:
        g = dict(s.get("granule") or {})
        meta.append({"date": s["date"], "granule_id": s.get("granule_id"), "track": g.get("track"),
                     "radar_mode": g.get("radar_mode"), "maturity": g.get("maturity")})
    return Stack(dates=[s["date"] for s in sidecars], hh=hh, hv=hv,
                 transform=transform, crs=crs, meta=meta)


def build_stack(
    aoi: Any,
    start: Any,
    end: Any,
    *,
    track: int | None = None,
    resolution_m: float = 30.0,
    min_dates: int = 3,
    search_fn: Callable[..., list[Granule]] = search_granules,
    ingest_fn: Callable[..., Any] = ingest_granule,
    rain_fn: Callable[..., dict[str, Any]] | None = rain_flag,
) -> tuple[Stack, dict[str, Any], dict[str, dict[str, Any]]]:
    """Discover, ingest and stack one track. Returns (stack, coverage, rain-by-date)."""
    geom = parse_aoi(aoi)
    granules = search_fn(geom, start, end)
    coverage = coverage_summary(granules, start, end)
    chosen_track, picked = select_track(granules, track)
    if len(picked) < min_dates:
        raise TimeSeriesError(
            f"Only {len(picked)} date(s) on track {chosen_track} in this range; need ≥ {min_dates}.")
    sidecars = []
    for g in picked:
        r = ingest_fn(g, geom)
        sidecars.append(load_sidecar(r.sidecar) if hasattr(r, "sidecar") else r)
    stack = load_stack(sidecars, resolution_m)
    rain = {d: rain_fn(d, geom) for d in stack.dates} if rain_fn else {}
    coverage["track_used"] = chosen_track
    coverage["dates_used"] = stack.dates
    single = sorted({g.date.isoformat() for g in granules if g.track == chosen_track and not has_pols(g)})
    if single:
        coverage["notes"].append(f"Skipped single-polarization (no HV) passes on track {chosen_track}: "
                                 f"{', '.join(single)}.")
        coverage["single_pol_skipped"] = single
    return stack, coverage, rain


def boxcar_db_stack(power: np.ndarray, window: int) -> np.ndarray:
    """Per-date NaN-aware boxcar in linear power → dB, for a (T, H, W) stack."""
    from gaia.process import boxcar_db

    return np.stack([boxcar_db(p, window) for p in power])


def max_gap_days(dates: Sequence[str]) -> int:
    from datetime import date

    ds = [date.fromisoformat(d) for d in dates]
    return max(((b - a).days for a, b in zip(ds, ds[1:])), default=0)


def aoi_mask(aoi: Any, stack: Stack) -> np.ndarray:
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    g = transform_geom("EPSG:4326", stack.crs, parse_aoi(aoi).__geo_interface__)
    return ~geometry_mask([g], out_shape=stack.shape, transform=stack.transform)


def write_float_cog(path: Path, data: np.ndarray, transform, crs, tags: dict[str, Any],
                    dtype: str = "float32", nodata: float | int = np.nan) -> Path:
    """Single-band COG writer for continuous / integer engine outputs."""
    import json

    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.shutil import copy as rio_copy

    path.parent.mkdir(parents=True, exist_ok=True)
    arr = data.astype(dtype)
    profile = dict(driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                   dtype=dtype, crs=crs, transform=transform, nodata=nodata)
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(arr, 1)
            tmp.update_tags(**{k: v if isinstance(v, str) else json.dumps(v, default=str)
                               for k, v in tags.items()})
            rio_copy(tmp, str(path), driver="COG", compress="DEFLATE", blocksize=512,
                     overview_resampling="nearest" if np.issubdtype(np.dtype(dtype), np.integer) else "average")
    return path


__all__ = ["Stack", "TimeSeriesError", "aoi_mask", "boxcar_db_stack", "build_stack",
           "load_stack", "max_gap_days", "select_track", "write_float_cog"]

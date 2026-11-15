"""Vegetation engine — growing season / planting-season detection from a GCOV time series.

Signal: L-band **HV** backscatter (volume scattering) rises as canopies grow
and drops at harvest/senescence; it is less sensitive to soil moisture than HH.
The dual-pol radar vegetation index RVI = 4·HV / (HH + HV) is reported alongside.

Per pixel, over passes that are not rain-affected:
  * amplitude   = peak HV − pre-peak minimum (dB); a season needs ≥ VEG_MIN_AMPLITUDE_DB
  * SOS (start) = date HV crosses 50 % of the rise (linear interpolation between passes)
  * peak        = date of maximum HV
  * EOS (end)   = date HV falls back through 50 % of the decline, if observed
Classes: 0 no seasonal signal · 1 full season observed · 2 season end (harvest) not
observed within the record · 3 season began before the record · 255 nodata.

Planting is *inferred*: radar sees canopy growth, which lags sowing by a few
weeks, so the planting window is SOS minus VEG_PLANTING_LEAD_DAYS — an estimate.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from gaia import config, flags
from gaia.timeseries import Stack, aoi_mask, boxcar_db_stack, max_gap_days, write_float_cog

NO_SEASON, FULL_SEASON, GROWING_AT_END, STARTED_BEFORE, NODATA = 0, 1, 2, 3, 255
CLASS_NAMES = {NO_SEASON: "no_seasonal_signal", FULL_SEASON: "full_season_observed",
               GROWING_AT_END: "season_end_not_observed", STARTED_BEFORE: "season_started_before_record",
               NODATA: "nodata"}
COLORMAP = {NO_SEASON: (200, 200, 200, 255), FULL_SEASON: (30, 150, 60, 255),
            GROWING_AT_END: (160, 210, 60, 255), STARTED_BEFORE: (120, 90, 200, 255),
            NODATA: (0, 0, 0, 0)}


def _crossing(x: np.ndarray, t: np.ndarray, level: np.ndarray, lo: np.ndarray, hi: np.ndarray,
              rising: bool) -> np.ndarray:
    """First time (days) where series x (T, N) crosses ``level`` between index lo..hi.
    Linear interpolation between samples; NaN if no crossing."""
    T, N = x.shape
    out = np.full(N, np.nan)
    found = np.zeros(N, bool)
    for k in range(1, T):
        a, b = x[k - 1], x[k]
        within = (k - 1 >= lo) & (k <= hi) & ~found
        hit = within & ((a < level) & (b >= level) if rising else (a > level) & (b <= level))
        if hit.any():
            frac = np.where(b != a, (level - a) / np.where(b != a, b - a, 1), 0.0)
            out[hit] = (t[k - 1] + frac * (t[k] - t[k - 1]))[hit]
            found |= hit
    return out


def phenology(hv_db: np.ndarray, days: np.ndarray,
              min_amplitude: float = config.VEG_MIN_AMPLITUDE_DB) -> dict[str, np.ndarray]:
    """Season metrics for a (T, N) HV-dB series sampled at ``days`` (day offsets)."""
    T, N = hv_db.shape
    valid = np.isfinite(hv_db).all(axis=0)
    x = np.where(valid, hv_db, 0.0)
    idx = np.arange(T)[:, None]

    peak_i = np.argmax(x, axis=0)
    peak_v = x[peak_i, np.arange(N)]
    pre = np.where(idx <= peak_i, x, np.inf)
    min_pre_i = np.argmin(pre, axis=0)
    min_pre_v = pre[min_pre_i, np.arange(N)]
    post = np.where(idx >= peak_i, x, np.inf)
    min_post_v = post.min(axis=0)
    amp_up = peak_v - min_pre_v
    amp_down = peak_v - min_post_v

    sos = _crossing(x, days, min_pre_v + 0.5 * amp_up, min_pre_i, peak_i, rising=True)
    eos_ok = amp_down >= np.maximum(min_amplitude, 0.5 * amp_up)
    eos = _crossing(x, days, peak_v - 0.5 * amp_down, peak_i, np.full(N, T - 1), rising=False)
    eos = np.where(eos_ok, eos, np.nan)

    seasonal = valid & (amp_up >= min_amplitude)
    cls = np.full(N, NO_SEASON, dtype="uint8")
    cls[seasonal & (peak_i == 0)] = STARTED_BEFORE
    cls[seasonal & (peak_i > 0) & np.isfinite(sos) & np.isfinite(eos)] = FULL_SEASON
    cls[seasonal & (peak_i > 0) & np.isfinite(sos) & ~np.isfinite(eos)] = GROWING_AT_END
    cls[~valid] = NODATA
    keep = np.isin(cls, (FULL_SEASON, GROWING_AT_END))
    return {
        "class": cls,
        "amplitude_db": np.where(valid, amp_up, np.nan),
        "sos_day": np.where(keep, sos, np.nan),
        "peak_day": np.where(np.isin(cls, (FULL_SEASON, GROWING_AT_END, STARTED_BEFORE)),
                             days[peak_i].astype(float), np.nan),
        "eos_day": np.where(cls == FULL_SEASON, eos, np.nan),
    }


def _to_date(d0: date, offset: float | None) -> str | None:
    if offset is None or not np.isfinite(offset):
        return None
    return (d0 + timedelta(days=float(offset))).isoformat()


def _doy_raster(day_offsets: np.ndarray, d0: date) -> np.ndarray:
    """Day offsets → day-of-year (uint16, 0 = nodata)."""
    doy0 = d0.timetuple().tm_yday
    out = np.where(np.isfinite(day_offsets), np.round(day_offsets) + doy0, 0)
    return np.clip(out, 0, 65535).astype("uint16")


def run_vegetation(
    stack: Stack,
    rain: dict[str, dict[str, Any]],
    aoi: Any,
    out_stem: Path,
    *,
    speckle_window: int = 3,
) -> dict[str, Any]:
    """Run phenology on a stack. Writes COGs next to ``out_stem`` and returns the result body."""
    T = len(stack.dates)
    eligible = [rain.get(d, {}).get("rain_affected") is not True for d in stack.dates]
    use = [i for i in range(T) if eligible[i]]
    if len(use) < 3:
        raise ValueError(f"Only {len(use)} rain-free passes; phenology needs ≥ 3.")

    hh_db = boxcar_db_stack(stack.hh, speckle_window)
    hv_db = boxcar_db_stack(stack.hv, speckle_window)
    with np.errstate(invalid="ignore", divide="ignore"):
        hh_lin, hv_lin = 10 ** (hh_db / 10), 10 ** (hv_db / 10)
        rvi = 4 * hv_lin / (hh_lin + hv_lin)

    H, W = stack.shape
    inside = aoi_mask(aoi, stack)
    d0 = date.fromisoformat(stack.dates[use[0]])
    days = np.array([(date.fromisoformat(stack.dates[i]) - d0).days for i in use], dtype=float)
    series = hv_db[use].reshape(len(use), -1)
    ph = phenology(series, days)
    cls = ph["class"].reshape(H, W)
    cls[~inside] = NODATA

    px_km2 = stack.pixel_km2
    valid_px = inside & (cls != NODATA)
    n_valid = max(int(valid_px.sum()), 1)
    area = {CLASS_NAMES[c]: round(float((cls == c).sum() * px_km2), 2)
            for c in (NO_SEASON, FULL_SEASON, GROWING_AT_END, STARTED_BEFORE)}
    frac = {CLASS_NAMES[c]: round(float((cls == c).sum() / n_valid), 4)
            for c in (NO_SEASON, FULL_SEASON, GROWING_AT_END, STARTED_BEFORE)}

    active = (np.isin(cls, (FULL_SEASON, GROWING_AT_END)) & inside).ravel()

    def med(a: np.ndarray, mask: np.ndarray) -> float | None:
        v = a[mask & np.isfinite(a)]
        return float(np.median(v)) if v.size else None

    def iqr(a: np.ndarray, mask: np.ndarray) -> list[str] | None:
        v = a[mask & np.isfinite(a)]
        if not v.size:
            return None
        return [_to_date(d0, float(np.percentile(v, 25))), _to_date(d0, float(np.percentile(v, 75)))]

    sos_med = med(ph["sos_day"], active)
    peak_med = med(ph["peak_day"], active)
    lead_lo, lead_hi = config.VEG_PLANTING_LEAD_DAYS
    # Season end comes only from pixels whose season finished. Report it only when those are
    # most of the active area and it falls after the peak; otherwise a minority subset gives
    # an end date before the start (seen in the Karakum test).
    full = active & (ph["class"] == FULL_SEASON)
    eos_med = med(ph["eos_day"], full)
    end_note = None
    if eos_med is not None and full.sum() < 0.5 * max(int(active.sum()), 1):
        eos_med, end_note = None, (f"Season end not reported: only {full.sum() / max(int(active.sum()), 1):.0%} "
                                   "of the active area completed its season within the record.")
    elif eos_med is not None and peak_med is not None and eos_med <= peak_med:
        eos_med, end_note = None, "Season end not reported: it did not fall after the peak (inconsistent signal)."
    season = {
        "metric": "HV backscatter (dB), 50% amplitude crossings",
        "active_area_km2": round(float(active.sum() * px_km2), 2),
        "season_start": _to_date(d0, sos_med),
        "season_start_iqr": iqr(ph["sos_day"], active),
        "peak": _to_date(d0, peak_med),
        "peak_iqr": iqr(ph["peak_day"], active),
        "season_end": _to_date(d0, eos_med),
        "season_end_iqr": iqr(ph["eos_day"], full) if eos_med is not None else None,
        "season_end_note": end_note,
        "estimated_planting_window": (
            [_to_date(d0, sos_med - lead_hi), _to_date(d0, sos_med - lead_lo)] if sos_med is not None else None),
        "median_amplitude_db": (round(med(ph["amplitude_db"], active), 2)
                                if med(ph["amplitude_db"], active) is not None else None),
    }

    # AOI time series for charting (all passes, flagged ones marked)
    act2d = active.reshape(H, W)
    timeseries = []
    for i, d in enumerate(stack.dates):
        m = act2d if act2d.any() else valid_px
        timeseries.append({
            "date": d,
            "eligible": eligible[i],
            "rain_affected": rain.get(d, {}).get("rain_affected"),
            "rain_mm_3day": rain.get(d, {}).get("rain_mm"),
            "hv_db_median_active": round(float(np.nanmedian(hv_db[i][m])), 2) if m.any() else None,
            "hh_db_median_active": round(float(np.nanmedian(hh_db[i][m])), 2) if m.any() else None,
            "rvi_median_active": round(float(np.nanmedian(rvi[i][m])), 3) if m.any() else None,
            "hv_db_median_all": round(float(np.nanmedian(hv_db[i][valid_px])), 2),
            **{k: stack.meta[i].get(k) for k in ("maturity", "radar_mode", "granule_id")},
        })

    # --- outputs ----------------------------------------------------------- #
    tags = {"gaia_product": "vegetation_phenology", "gaia_dates_used": [stack.dates[i] for i in use],
            "gaia_metric": "HV dB", "gaia_classes": CLASS_NAMES}
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    cls_path = out_stem.with_name(out_stem.name + "_season_class.tif")
    _write_class_cog(cls_path, cls, stack, tags)
    outputs["season_class"] = str(cls_path)
    for key, name in (("sos_day", "season_start_doy"), ("peak_day", "peak_doy"), ("eos_day", "season_end_doy")):
        arr = ph[key].reshape(H, W)
        arr[~inside] = np.nan
        p = write_float_cog(out_stem.with_name(f"{out_stem.name}_{name}.tif"), _doy_raster(arr, d0),
                            stack.transform, stack.crs, {**tags, "gaia_units": "day of year",
                                                         "gaia_year": d0.year}, dtype="uint16", nodata=0)
        outputs[name] = str(p)
    amp = ph["amplitude_db"].reshape(H, W)
    amp[~inside] = np.nan
    outputs["amplitude_db"] = str(write_float_cog(out_stem.with_name(out_stem.name + "_amplitude_db.tif"),
                                                  amp, stack.transform, stack.crs, tags))
    ql = _quicklook(out_stem.with_name(out_stem.name + ".png"), cls, ph, d0, H, W, timeseries, season)
    if ql:
        outputs["quicklook"] = str(ql)

    per_date = [{"date": t["date"], "maturity": t["maturity"], "radar_mode": t["radar_mode"],
                 "rain_affected": t["rain_affected"], "rain_mm_3day": t["rain_mm_3day"],
                 "rain_window": rain.get(t["date"], {}).get("window"),
                 "rain_error": rain.get(t["date"], {}).get("error"),
                 "wind_affected": None, "aggregate_eligible": t["eligible"]} for t in timeseries]

    # --- confidence -------------------------------------------------------- #
    used_dates = [stack.dates[i] for i in use]
    c = flags.Confidence()
    if len(use) < config.VEG_MIN_GOOD_DATES:
        c.cap("low", f"Only {len(use)} rain-free passes; season timing is poorly constrained.")
    gap = max_gap_days(used_dates)
    if gap > 36:
        c.cap("medium", f"Largest gap between usable passes is {gap} days; timing precision is coarse.")
    if any(t["rain_affected"] is None for t in timeseries):
        c.cap("medium", "Rainfall could not be checked for every pass.")
    excluded = [t["date"] for t in timeseries if not t["eligible"]]
    if excluded:
        c.note(f"{len(excluded)} rain-affected pass(es) excluded: {', '.join(excluded)}.")
    least = flags.least_mature([m["maturity"] for m in stack.meta])
    if least != "VALIDATED":
        c.cap("medium", f"Least mature product is {least}, not fully calibrated.")
    act_n = frac[CLASS_NAMES[FULL_SEASON]] + frac[CLASS_NAMES[GROWING_AT_END]]
    if act_n < 0.05:
        c.cap("low", "Less than 5% of the AOI shows a seasonal vegetation signal.")
    partial = frac[CLASS_NAMES[GROWING_AT_END]] + frac[CLASS_NAMES[STARTED_BEFORE]]
    if partial > frac[CLASS_NAMES[FULL_SEASON]]:
        c.cap("medium", "Most seasonal pixels were only partially observed (season starts before or "
                        "ends after the record).")

    return {
        "dates": used_dates,
        "season": season,
        "class_area_km2": area,
        "class_fraction": frac,
        "timeseries": timeseries,
        "per_date": per_date,
        "outputs": outputs,
        "method": {"engine": "vegetation", "signal": "HV gamma0 dB (boxcar %d)" % speckle_window,
                   "min_amplitude_db": config.VEG_MIN_AMPLITUDE_DB,
                   "planting_lead_days": list(config.VEG_PLANTING_LEAD_DAYS),
                   "resolution_m": abs(stack.transform.a)},
        "flags": {
            "rain_affected_dates": excluded,
            "radar_mode": flags.radar_mode_label([m["radar_mode"] for m in stack.meta]),
            "maturity": least,
            "max_gap_days": gap,
        },
        "confidence": c.level,
        "confidence_reasons": c.reasons,
    }


def _write_class_cog(path: Path, cls: np.ndarray, stack: Stack, tags: dict[str, Any]) -> None:
    import json

    from rasterio.io import MemoryFile
    from rasterio.shutil import copy as rio_copy

    profile = dict(driver="GTiff", width=cls.shape[1], height=cls.shape[0], count=1, dtype="uint8",
                   crs=stack.crs, transform=stack.transform, nodata=NODATA)
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(cls, 1)
            tmp.write_colormap(1, COLORMAP)
            tmp.update_tags(**{k: json.dumps(v, default=str) if not isinstance(v, str) else v
                               for k, v in tags.items()})
            rio_copy(tmp, str(path), driver="COG", compress="DEFLATE", overview_resampling="mode")


def _quicklook(path: Path, cls, ph, d0, H, W, timeseries, season) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap
        from matplotlib.patches import Patch
    except ImportError:  # pragma: no cover
        return None
    fig, axes = plt.subplots(1, 3, figsize=(18, 5.5), gridspec_kw={"width_ratios": [1, 1, 1.3]})
    rgba = np.zeros(cls.shape + (4,))
    for k, col in COLORMAP.items():
        rgba[cls == k] = np.array(col) / 255
    axes[0].imshow(rgba)
    axes[0].set_title("Season class")
    axes[0].legend(handles=[Patch(color=np.array(COLORMAP[k][:3]) / 255, label=CLASS_NAMES[k].replace("_", " "))
                            for k in (0, 1, 2, 3)], loc="lower left", fontsize=7)
    sos = ph["sos_day"].reshape(H, W)
    im = axes[1].imshow(sos, cmap="viridis")
    axes[1].set_title(f"Season start (days after {d0})")
    fig.colorbar(im, ax=axes[1], shrink=0.7)
    for ax in axes[:2]:
        ax.axis("off")
    from datetime import date as _d

    xs = [_d.fromisoformat(t["date"]) for t in timeseries]
    ys = [t["hv_db_median_active"] for t in timeseries]
    ax = axes[2]
    ax.plot(xs, ys, "-o", color="#1e9632", label="HV median (seasonal pixels)")
    for x, y, t in zip(xs, ys, timeseries):
        if not t["eligible"] and y is not None:
            ax.plot(x, y, "x", color="red", ms=10, mew=2)
    for key, colr, lab in (("season_start", "#888", "season start"), ("peak", "#1e9632", "peak"),
                           ("season_end", "#c47a00", "season end")):
        if season.get(key):
            ax.axvline(_d.fromisoformat(season[key]), color=colr, ls="--", label=lab)
    if season.get("estimated_planting_window"):
        a, b = (_d.fromisoformat(v) for v in season["estimated_planting_window"])
        ax.axvspan(a, b, color="#f2c14e", alpha=0.3, label="est. planting window")
    ax.set_ylabel("HV gamma0 (dB)")
    ax.set_title("Seasonal signal (red x = rain-affected, excluded)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=85)
    plt.close(fig)
    return path


__all__ = ["phenology", "run_vegetation", "CLASS_NAMES"]

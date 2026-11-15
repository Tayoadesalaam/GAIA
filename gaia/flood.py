"""Flood engine — flood extent through time, relative to normal water.

Differs from the two-date ``water_change`` engine:
  * uses every pass on one track in the period;
  * learns **normal water** (water on ≥ FLOOD_PERMANENT_OCCURRENCE of usable
    passes, or of an explicit dry-season baseline);
  * maps, per pass, **flooded open water** (water now, not normally) and
    **flooded vegetation** (experimental: HH double-bounce rise ≥ 3 dB in
    vegetated, non-urban pixels);
  * reports the **peak flood**, excluding rain- and wind-affected passes as
    CLAUDE.md requires; flagged passes are still mapped and listed.

Peak map classes: 0 dry · 1 normal water · 2 flooded open water ·
3 flooded vegetation · 255 nodata/excluded.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from gaia import config, flags
from gaia.process import Calibration, calibrate_auto, remove_small_patches
from gaia.timeseries import Stack, aoi_mask, boxcar_db_stack, write_float_cog

DRY, NORMAL_WATER, FLOOD_OPEN, FLOOD_VEG, NODATA = 0, 1, 2, 3, 255
CLASS_NAMES = {DRY: "dry", NORMAL_WATER: "normal_water", FLOOD_OPEN: "flooded_open_water",
               FLOOD_VEG: "flooded_vegetation", NODATA: "nodata"}
COLORMAP = {DRY: (0, 0, 0, 0), NORMAL_WATER: (20, 40, 110, 255), FLOOD_OPEN: (30, 140, 255, 255),
            FLOOD_VEG: (0, 210, 200, 255), NODATA: (0, 0, 0, 0)}


def _region(geom: Any, stack: Stack) -> np.ndarray | None:
    return aoi_mask(geom, stack) if geom else None


def run_flood(
    stack: Stack,
    rain: dict[str, dict[str, Any]],
    aoi: Any,
    out_stem: Path,
    *,
    water_ref: Any | None = None,
    dry_ref: Any | None = None,
    exclude: Any | None = None,
    baseline: Sequence[str] | None = None,  # (start, end) ISO dates for "normal" water
    speckle_window: int = 3,
) -> dict[str, Any]:
    T = len(stack.dates)
    hh = boxcar_db_stack(stack.hh, speckle_window)
    hv = boxcar_db_stack(stack.hv, speckle_window)
    valid = np.isfinite(hh) & np.isfinite(hv)
    inside = aoi_mask(aoi, stack)
    wmask, dmask, xmask = _region(water_ref, stack), _region(dry_ref, stack), _region(exclude, stack)
    if xmask is not None:
        inside = inside & ~xmask
    px_km2 = stack.pixel_km2

    rain_aff = [rain.get(d, {}).get("rain_affected") for d in stack.dates]
    not_rainy = [r is not True for r in rain_aff]

    # --- calibrate per radar mode on rain-free passes ---------------------- #
    modes = [m.get("radar_mode") for m in stack.meta]
    cals: dict[Any, Calibration] = {}
    for mode in dict.fromkeys(modes):
        idx = [i for i in range(T) if modes[i] == mode and not_rainy[i]] or \
              [i for i in range(T) if modes[i] == mode]
        w = (np.concatenate([np.c_[hh[i][wmask & valid[i]], hv[i][wmask & valid[i]]] for i in idx])
             if wmask is not None else None)
        d = (np.concatenate([np.c_[hh[i][dmask & valid[i]], hv[i][dmask & valid[i]]] for i in idx])
             if dmask is not None else None)
        cal = calibrate_auto(w, d, mode)
        cal.dates = [stack.dates[i] for i in idx]
        cals[mode] = cal

    water = np.stack([valid[i] & (hh[i] < cals[modes[i]].threshold_hh_db)
                      & (hv[i] < cals[modes[i]].threshold_hv_db) for i in range(T)])

    # --- wind flag per pass -------------------------------------------------- #
    wind: list[bool | None] = []
    ref_frac: list[float | None] = []
    for i in range(T):
        if wmask is not None and (wmask & valid[i]).any():
            f = float(water[i][wmask & valid[i]].mean())
            ref_frac.append(round(f, 4))
            wind.append(flags.wind_affected(f))
        else:
            ref_frac.append(None)
            wind.append(None)
    eligible = [not_rainy[i] and wind[i] is not True for i in range(T)]
    use = [i for i in range(T) if eligible[i]]
    if len(use) < 2:
        raise ValueError(f"Only {len(use)} pass(es) are free of rain/wind flags; flood mapping needs ≥ 2.")

    # --- normal water (baseline) ------------------------------------------- #
    if baseline:
        b0, b1 = (date.fromisoformat(str(x)[:10]) for x in baseline)
        base = [i for i in use if b0 <= date.fromisoformat(stack.dates[i]) <= b1]
        if not base:
            raise ValueError(f"No usable passes inside the baseline {b0}…{b1}.")
    else:
        base = use
    n_valid = valid[base].sum(axis=0)
    occurrence = np.where(n_valid > 0, water[base].sum(axis=0) / np.maximum(n_valid, 1), np.nan)
    normal = occurrence >= config.FLOOD_PERMANENT_OCCURRENCE
    hh_med = np.nanmedian(np.where(valid[base], hh[base], np.nan), axis=0)
    hv_med = np.nanmedian(np.where(valid[base], hv[base], np.nan), axis=0)
    vegetated = (hv_med >= config.FLOOD_VEG_MIN_HV_DB) & (hh_med < config.FLOOD_URBAN_HH_DB)

    # --- per-pass flood maps ------------------------------------------------ #
    per_date = []
    maps = []
    for i, d in enumerate(stack.dates):
        ok = valid[i] & inside
        fo = remove_small_patches(water[i] & ~normal & ok, config.FLOOD_MIN_PATCH_PX)
        fv = remove_small_patches(
            ok & vegetated & ~water[i] & ~normal
            & (hh[i] - hh_med >= config.FLOOD_VEG_HH_RISE_DB)
            & (hv[i] - hv_med > -config.FLOOD_VEG_MAX_HV_DROP_DB),
            config.FLOOD_MIN_PATCH_PX)
        m = np.full(ok.shape, NODATA, dtype="uint8")
        m[ok] = DRY
        m[ok & normal] = NORMAL_WATER
        m[fo] = FLOOD_OPEN
        m[fv] = FLOOD_VEG
        maps.append(m)
        r = rain.get(d, {})
        per_date.append({
            "date": d, **{k: stack.meta[i].get(k) for k in ("granule_id", "track", "radar_mode", "maturity")},
            "valid_fraction": round(float(ok.sum() / max(inside.sum(), 1)), 4),
            "water_km2": round(float((water[i] & ok).sum() * px_km2), 3),
            "flood_open_km2": round(float(fo.sum() * px_km2), 3),
            "flood_vegetation_km2": round(float(fv.sum() * px_km2), 3),
            "flood_total_km2": round(float((fo.sum() + fv.sum()) * px_km2), 3),
            "water_ref_fraction": ref_frac[i],
            "rain_affected": rain_aff[i], "rain_mm_3day": r.get("rain_mm"),
            "rain_window": r.get("window"), "rain_error": r.get("error"),
            "wind_affected": wind[i], "aggregate_eligible": eligible[i],
        })

    peak_i = max(use, key=lambda i: per_date[i]["flood_total_km2"])
    flagged_bigger = [p["date"] for i, p in enumerate(per_date)
                      if not eligible[i] and p["flood_total_km2"] > per_date[peak_i]["flood_total_km2"]]
    frequency = np.zeros(inside.shape, dtype="uint8")
    for i in use:
        frequency += np.isin(maps[i], (FLOOD_OPEN, FLOOD_VEG)).astype("uint8")
    frequency[~inside] = 255

    # --- outputs ----------------------------------------------------------- #
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    cal_list = [c.to_dict() for c in cals.values()]
    tags = {"gaia_product": "flood", "gaia_dates": stack.dates, "gaia_peak_date": stack.dates[peak_i],
            "gaia_calibration": cal_list, "gaia_classes": CLASS_NAMES}
    outputs = {
        "peak_flood_map": str(_class_cog(out_stem.with_name(out_stem.name + "_peak.tif"),
                                         maps[peak_i], stack, tags)),
        "flood_frequency": str(write_float_cog(
            out_stem.with_name(out_stem.name + "_frequency.tif"), frequency, stack.transform, stack.crs,
            {**tags, "gaia_units": f"number of usable passes flooded (of {len(use)})"},
            dtype="uint8", nodata=255)),
        "water_occurrence": str(write_float_cog(
            out_stem.with_name(out_stem.name + "_occurrence.tif"),
            np.where(np.isfinite(occurrence) & inside, np.round(occurrence * 100), 255),
            stack.transform, stack.crs, {**tags, "gaia_units": "% of baseline passes classified water"},
            dtype="uint8", nodata=255)),
    }
    ql = _quicklook(out_stem.with_name(out_stem.name + ".png"), maps[peak_i], hh[peak_i],
                    stack.dates[peak_i], per_date)
    if ql:
        outputs["quicklook"] = str(ql)

    peak = per_date[peak_i]
    stats = {
        "peak_date": peak["date"],
        "peak_flood_total_km2": peak["flood_total_km2"],
        "peak_flood_open_km2": peak["flood_open_km2"],
        "peak_flood_vegetation_km2": peak["flood_vegetation_km2"],
        "normal_water_km2": round(float((normal & inside).sum() * px_km2), 3),
        "ever_flooded_km2": round(float(((frequency > 0) & (frequency != 255)).sum() * px_km2), 3),
        "passes_total": T,
        "passes_used": len(use),
        "baseline_dates": [stack.dates[i] for i in base],
        "excluded_dates": [p["date"] for p in per_date if not p["aggregate_eligible"]],
        "flagged_passes_with_larger_flood": flagged_bigger,
        "excluded_km2": round(float(xmask.sum() * px_km2), 3) if xmask is not None else 0.0,
        "pixel_size_m": abs(stack.transform.a),
    }

    # --- confidence -------------------------------------------------------- #
    c = flags.Confidence()
    for lvl, why in flags.calibration_issues(cal_list):
        c.cap(lvl, why) if lvl else c.note(why)
    if len(use) < 4:
        c.cap("low", f"Only {len(use)} usable passes; normal water and peak are poorly constrained.")
    if any(r is None for r in rain_aff):
        c.cap("medium", "Rainfall could not be checked for every pass.")
    if wmask is None:
        c.cap("medium", "No permanent-water reference, so wind roughening could not be checked.")
    if peak["maturity"] != "VALIDATED":
        c.cap("medium", f"Peak pass product is {peak['maturity']}, not fully calibrated.")
    if flagged_bigger:
        c.cap("medium", f"Flagged pass(es) {', '.join(flagged_bigger)} show a larger flood than the reported "
                        "peak; the true peak may coincide with rain/wind.")
    if peak["flood_total_km2"] and peak["flood_vegetation_km2"] / peak["flood_total_km2"] > 0.5:
        c.cap("medium", "Most of the peak flood is flooded vegetation, which is experimental.")
    if not baseline:
        c.note("Normal water was learned from the requested period itself (no dry-season baseline given).")

    return {
        "dates": stack.dates,
        "per_date": per_date,
        "calibrations": cal_list,
        "stats": stats,
        "outputs": outputs,
        "confidence": c.level,
        "confidence_reasons": c.reasons,
    }


def _class_cog(path: Path, m: np.ndarray, stack: Stack, tags: dict[str, Any]) -> Path:
    import json

    from rasterio.io import MemoryFile
    from rasterio.shutil import copy as rio_copy

    profile = dict(driver="GTiff", width=m.shape[1], height=m.shape[0], count=1, dtype="uint8",
                   crs=stack.crs, transform=stack.transform, nodata=NODATA)
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(m, 1)
            tmp.write_colormap(1, COLORMAP)
            tmp.update_tags(**{k: v if isinstance(v, str) else json.dumps(v, default=str)
                               for k, v in tags.items()})
            rio_copy(tmp, str(path), driver="COG", compress="DEFLATE", overview_resampling="mode")
    return path


def _quicklook(path: Path, peak_map, hh_db, peak_date, per_date) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:  # pragma: no cover
        return None
    fig, (a0, a1) = plt.subplots(1, 2, figsize=(18, 6), gridspec_kw={"width_ratios": [1.6, 1]})
    a0.imshow(hh_db, cmap="gray", vmin=-22, vmax=2)
    rgba = np.zeros(peak_map.shape + (4,))
    for k, col in COLORMAP.items():
        rgba[peak_map == k] = np.array(col) / 255
    a0.imshow(rgba)
    a0.set_title(f"Peak flood (usable passes): {peak_date}")
    a0.legend(handles=[Patch(color=np.array(COLORMAP[k][:3]) / 255, label=CLASS_NAMES[k].replace("_", " "))
                       for k in (1, 2, 3)], loc="upper right")
    a0.axis("off")
    xs = range(len(per_date))
    open_ = [p["flood_open_km2"] for p in per_date]
    veg = [p["flood_vegetation_km2"] for p in per_date]
    colors = ["#1e8cff" if p["aggregate_eligible"] else "#bbbbbb" for p in per_date]
    a1.bar(xs, open_, color=colors, label="open water")
    a1.bar(xs, veg, bottom=open_, color=["#00d2c8" if p["aggregate_eligible"] else "#dddddd"
                                         for p in per_date], label="vegetation")
    a1.set_xticks(list(xs), [p["date"][5:] for p in per_date], rotation=60)
    a1.set_ylabel("flooded area (km²)")
    a1.set_title("Flood extent per pass (grey = rain/wind-flagged, excluded from peak)")
    a1.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=85)
    plt.close(fig)
    return path


__all__ = ["run_flood", "CLASS_NAMES"]

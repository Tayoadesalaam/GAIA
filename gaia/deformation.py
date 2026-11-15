"""Deformation engine — line-of-sight (LOS) ground motion from NISAR GUNW.

GCOV (backscatter) cannot measure deformation; this engine uses the L2 GUNW
geocoded unwrapped interferograms (80 m), via openSEPPO ``nisar_gunw_convert``.

Method (small-baseline *sequential chain*):
  1. Discover GUNW pairs; pick the track with the longest contiguous chain
     (secondary date of one pair = reference date of the next).
  2. Per interferogram: subtract the GUNW ionosphere phase screen; remove a
     best-fit plane (long-wavelength residual atmosphere/orbit ramp); mask
     coherence < DEFO_MIN_COHERENCE and pixels outside the dominant connected
     component (unwrapping-error guard); reference to the median of a stable
     area (or the AOI's coherent pixels → relative motion).
  3. Phase → LOS range change: d = λ / (4π) · φ  (mm; positive = away from the
     satellite, e.g. subsidence — see config.DEFO_SIGN_CONVENTION).
  4. Cumulative displacement by summing the chain; per-pixel linear velocity
     (mm/yr) with its standard error.

Not corrected: tropospheric delay (dominant noise in the humid tropics).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from gaia import config, flags
from gaia.discovery import Granule, coverage_summary, parse_aoi, search_granules
from gaia.ingest import ingest_gunw
from gaia.timeseries import write_float_cog


class DeformationError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Chain selection
# --------------------------------------------------------------------------- #
def longest_chain(pairs: Sequence[Granule]) -> list[Granule]:
    """Longest contiguous sequence of pairs (sec of one == ref of next)."""
    rank = {m: i for i, m in enumerate(config.MATURITY_ORDER)}
    edges: dict[date, list[Granule]] = {}
    for g in pairs:
        if g.secondary_date and g.secondary_date > g.date:
            edges.setdefault(g.date, []).append(g)
    best: dict[date, list[Granule]] = {}  # longest chain starting at date

    def chain_from(d: date) -> list[Granule]:
        if d in best:
            return best[d]
        options = []
        for g in edges.get(d, []):
            options.append([g] + chain_from(g.secondary_date))
        # longest; ties → shorter first baseline, then more mature
        best[d] = max(options, key=lambda c: (len(c), -(c[0].secondary_date - c[0].date).days,
                                              rank.get(c[0].maturity, -1)), default=[])
        return best[d]

    starts = sorted(edges)
    return max((chain_from(d) for d in starts), key=len, default=[])


def select_chain(pairs: Sequence[Granule], track: int | None = None) -> tuple[Any, list[Granule]]:
    by_track: dict[Any, list[Granule]] = {}
    for g in pairs:
        if track is None or g.track == track:
            by_track.setdefault(g.track, []).append(g)
    if not by_track:
        raise DeformationError("No NISAR GUNW interferograms for this AOI/time range"
                               + (f" on track {track}" if track is not None else "") + ".")
    chains = {t: longest_chain(ps) for t, ps in by_track.items()}
    t = max(chains, key=lambda k: len(chains[k]))
    return t, chains[t]


# --------------------------------------------------------------------------- #
# Loading and per-interferogram correction
# --------------------------------------------------------------------------- #
def _read(path: str, transform, shape, crs, nearest: bool) -> np.ndarray:
    import rasterio
    from rasterio.warp import Resampling, reproject

    with rasterio.open(path) as src:
        if src.transform == transform and src.shape == shape and src.crs == crs:
            a = src.read(1).astype("float64")
            if src.nodata is not None and not np.isnan(src.nodata):
                a[a == src.nodata] = np.nan
            return a
        dst = np.full(shape, np.nan, dtype="float64")
        reproject(rasterio.band(src, 1), dst, dst_transform=transform, dst_crs=crs,
                  src_nodata=src.nodata, dst_nodata=np.nan,
                  resampling=Resampling.nearest if nearest else Resampling.bilinear)
        return dst


def remove_plane(phase: np.ndarray, mask: np.ndarray, max_samples: int = 200_000) -> tuple[np.ndarray, float]:
    """Fit and subtract a plane a + b·col + c·row over ``mask`` pixels.

    Removes long-wavelength residual atmosphere/ionosphere/orbit ramps that
    otherwise dominate short time series. Also removes genuine AOI-wide tilts.
    Returns (deramped phase, ramp peak-to-peak in phase units)."""
    rows, cols = np.nonzero(mask)
    if rows.size > max_samples:
        pick = np.random.default_rng(0).choice(rows.size, max_samples, replace=False)
        rows, cols = rows[pick], cols[pick]
    A = np.c_[np.ones(rows.size), cols, rows]
    y = phase[rows, cols]
    keep = np.ones(y.size, bool)
    # Robust fit: drop outliers (e.g. a real subsidence bowl) so they don't tilt the plane.
    for _ in range(3):
        coef, *_ = np.linalg.lstsq(A[keep], y[keep], rcond=None)
        resid = y - A @ coef
        mad = 1.4826 * np.median(np.abs(resid[keep] - np.median(resid[keep])))
        new_keep = np.abs(resid - np.median(resid[keep])) <= 2.5 * max(mad, 1e-9)
        if new_keep.sum() < 10 or (new_keep == keep).all():
            break
        keep = new_keep
    rr, cc = np.mgrid[0:phase.shape[0], 0:phase.shape[1]]
    plane = coef[0] + coef[1] * cc + coef[2] * rr
    ptp = float(np.ptp(plane[mask])) if mask.any() else 0.0
    return phase - plane, ptp


def correct_ifg(phase: np.ndarray, coh: np.ndarray, cc: np.ndarray, iono: np.ndarray | None,
                ref_mask: np.ndarray, aoi_mask: np.ndarray, *,
                min_coh: float = config.DEFO_MIN_COHERENCE,
                apply_iono: bool = config.DEFO_APPLY_IONO,
                deramp: bool = config.DEFO_DERAMP) -> tuple[np.ndarray, dict[str, Any]]:
    """Return referenced, masked LOS displacement (mm) and diagnostics."""
    lam = config.NISAR_L_WAVELENGTH_M
    to_mm = lam / (4 * np.pi) * 1000
    ph = phase.copy()
    good = np.isfinite(ph) & aoi_mask
    diag: dict[str, Any] = {}
    if good.any():
        diag["spread_raw_cm"] = round(float(np.ptp(np.percentile(ph[good], [2, 98]))) * to_mm / 10, 2)
    iono_used = apply_iono and iono is not None and np.isfinite(iono[good]).any()
    if iono_used:
        ph = ph - np.where(np.isfinite(iono), iono, 0.0)
    cc = np.where(np.isfinite(cc), cc, 0)
    comps, counts = np.unique(cc[good & (cc > 0) & (cc < 65535)], return_counts=True)
    dominant = int(comps[np.argmax(counts)]) if comps.size else None
    mask = good & (coh >= min_coh) & (cc == dominant if dominant is not None else False)
    ph[~mask] = np.nan

    if deramp and mask.sum() > 100:
        ph, ramp_cm = remove_plane(ph, mask)
        diag["ramp_removed_cm"] = round(ramp_cm * to_mm / 10, 2)
    diag["deramped"] = bool(deramp and mask.sum() > 100)

    ref = mask & ref_mask
    if ref.sum() < 20:  # fall back to all coherent pixels (relative motion)
        ref = mask
        diag["reference"] = "aoi_coherent_median"
    else:
        diag["reference"] = "stable_ref_median"
    if not ref.any():
        diag.update(valid_fraction=0.0, iono_applied=iono_used)
        return np.full_like(ph, np.nan), diag
    ph = ph - np.nanmedian(ph[ref])
    d = ph * to_mm
    diag.update(
        iono_applied=bool(iono_used),
        dominant_component=dominant,
        coherence_median=round(float(np.nanmedian(np.where(good, coh, np.nan))), 3),
        valid_fraction=round(float(mask.sum() / max(aoi_mask.sum(), 1)), 4),
        spread_corrected_cm=round(float(np.ptp(np.nanpercentile(d[mask], [2, 98]))) / 10, 2),
    )
    return d, diag


def fit_velocity(cum: np.ndarray, days: np.ndarray, min_epochs: int = config.DEFO_MIN_EPOCHS):
    """Per-pixel least-squares slope (mm/yr) and its standard error over epochs.
    ``cum`` is (E, H, W); pixels need ≥ ``min_epochs`` finite epochs."""
    E = cum.shape[0]
    flat = cum.reshape(E, -1)
    ok = np.isfinite(flat)
    n = ok.sum(axis=0)
    t = np.repeat(days[:, None], flat.shape[1], axis=1)
    tm = np.where(ok, t, 0).sum(0) / np.maximum(n, 1)
    ym = np.where(ok, flat, 0).sum(0) / np.maximum(n, 1)
    dt = np.where(ok, t - tm, 0)
    dy = np.where(ok, flat - ym, 0)
    sxx = (dt ** 2).sum(0)
    slope = np.where(sxx > 0, (dt * dy).sum(0) / np.where(sxx > 0, sxx, 1), np.nan)
    resid = np.where(ok, dy - slope * dt, 0)
    dof = np.maximum(n - 2, 1)
    se = np.sqrt((resid ** 2).sum(0) / dof / np.where(sxx > 0, sxx, np.nan))
    usable = n >= min_epochs
    vel = np.where(usable, slope * 365.25, np.nan).reshape(cum.shape[1:])
    vel_se = np.where(usable, se * 365.25, np.nan).reshape(cum.shape[1:])
    return vel, vel_se


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def run_deformation(
    aoi: Any,
    start: Any,
    end: Any,
    out_stem: Path,
    *,
    track: int | None = None,
    stable_ref: Any | None = None,
    search_fn: Callable[..., list[Granule]] = search_granules,
    ingest_fn: Callable[..., dict[str, Any]] = ingest_gunw,
) -> dict[str, Any]:
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    geom = parse_aoi(aoi)
    pairs = search_fn(geom, start, end, product="GUNW")
    coverage = coverage_summary(pairs, start, end)
    chosen, chain = select_chain(pairs, track)
    if len(chain) < 2:
        raise DeformationError(f"Longest contiguous interferogram chain on track {chosen} has "
                               f"{len(chain)} pair(s); need ≥ 2.")
    sides = [ingest_fn(g, geom) for g in chain]

    with rasterio.open(sides[0]["paths"]["unwphase"]) as src:
        transform, shape, crs = src.transform, src.shape, src.crs

    def region(g: Any) -> np.ndarray:
        gg = transform_geom("EPSG:4326", crs, parse_aoi(g).__geo_interface__)
        return ~geometry_mask([gg], out_shape=shape, transform=transform)

    inside = region(geom)
    ref_mask = region(stable_ref) if stable_ref else np.zeros(shape, bool)

    ifgs, diags, cohs = [], [], []
    for s in sides:
        p = s["paths"]
        phase = _read(p["unwphase"], transform, shape, crs, nearest=True)
        coh = _read(p["coh"], transform, shape, crs, nearest=True)
        cc = _read(p["concomp"], transform, shape, crs, nearest=True)
        iono = _read(p["iono"], transform, shape, crs, nearest=True) if "iono" in p else None
        d, diag = correct_ifg(phase, coh, cc, iono, ref_mask, inside)
        g = s.get("granule") or {}
        diag.update(reference_date=s["reference_date"], secondary_date=s["secondary_date"],
                    granule_id=s["granule_id"], maturity=g.get("maturity"), radar_mode=g.get("radar_mode"))
        ifgs.append(d)
        diags.append(diag)
        cohs.append(np.where(inside, coh, np.nan))

    epochs = [sides[0]["reference_date"]] + [s["secondary_date"] for s in sides]
    e0 = date.fromisoformat(epochs[0])
    days = np.array([(date.fromisoformat(e) - e0).days for e in epochs], dtype=float)
    cum = np.zeros((len(epochs),) + shape)
    cum[0] = np.where(inside, 0.0, np.nan)
    for k, d in enumerate(ifgs, start=1):
        cum[k] = cum[k - 1] + d  # NaN propagates: a pixel must be coherent through the chain
    vel, vel_se = fit_velocity(cum, days)
    vel[~inside] = np.nan
    vel_se[~inside] = np.nan
    with np.errstate(invalid="ignore"), __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore", RuntimeWarning)
        mean_coh = np.nanmean(np.stack(cohs), axis=0)

    # --- stats ------------------------------------------------------------- #
    px_km2 = abs(transform.a * transform.e) / 1e6
    v = vel[np.isfinite(vel)]
    span = int(days[-1])
    thr = config.DEFO_HOTSPOT_MM_YR
    ref_v = vel[ref_mask & np.isfinite(vel)]
    noise = (float(1.4826 * np.median(np.abs(ref_v - np.median(ref_v)))) if ref_v.size >= 20
             else float(1.4826 * np.median(np.abs(v - np.median(v)))) if v.size else None)
    series = [{"date": e, "median_los_mm": (round(float(np.nanmedian(cum[k][np.isfinite(vel)])), 2)
                                            if v.size else None)}
              for k, e in enumerate(epochs)]
    stats = {
        "span_days": span,
        "interferograms": len(chain),
        "epochs": len(epochs),
        "valid_fraction": round(float(np.isfinite(vel).sum() / max(inside.sum(), 1)), 4),
        "velocity_mm_yr_percentiles": ({p: round(float(np.percentile(v, p)), 1) for p in (5, 25, 50, 75, 95)}
                                       if v.size else None),
        "velocity_noise_mm_yr": round(noise, 1) if noise is not None else None,
        "median_velocity_stderr_mm_yr": round(float(np.nanmedian(vel_se)), 1) if v.size else None,
        "away_from_satellite_km2": round(float((vel > thr).sum() * px_km2), 2),
        "toward_satellite_km2": round(float((vel < -thr).sum() * px_km2), 2),
        "hotspot_threshold_mm_yr": thr,
        "cumulative_los_series": series,
        "pixel_size_m": abs(transform.a),
        "reference": diags[0].get("reference"),
    }

    # --- outputs ----------------------------------------------------------- #
    tags = {"gaia_product": "deformation_los", "gaia_epochs": epochs,
            "gaia_sign": config.DEFO_SIGN_CONVENTION, "gaia_wavelength_m": config.NISAR_L_WAVELENGTH_M,
            "gaia_iono_corrected": all(d.get("iono_applied") for d in diags), "gaia_tropo_corrected": False}
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    outputs = {
        "velocity_mm_yr": str(write_float_cog(out_stem.with_name(out_stem.name + "_velocity.tif"), vel,
                                              transform, crs, {**tags, "gaia_units": "mm/yr LOS"})),
        "velocity_stderr_mm_yr": str(write_float_cog(out_stem.with_name(out_stem.name + "_velocity_stderr.tif"),
                                                     vel_se, transform, crs, {**tags, "gaia_units": "mm/yr"})),
        "cumulative_los_mm": str(write_float_cog(out_stem.with_name(out_stem.name + "_cumulative.tif"),
                                                 np.where(np.isfinite(vel), cum[-1], np.nan), transform, crs,
                                                 {**tags, "gaia_units": f"mm LOS {epochs[0]}→{epochs[-1]}"})),
        "mean_coherence": str(write_float_cog(out_stem.with_name(out_stem.name + "_coherence.tif"),
                                              mean_coh, transform, crs, tags)),
    }
    ql = _quicklook(out_stem.with_name(out_stem.name + ".png"), vel, cum, epochs, inside, thr)
    if ql:
        outputs["quicklook"] = str(ql)

    # --- confidence -------------------------------------------------------- #
    c = flags.Confidence()
    c.cap("medium", "Tropospheric delay is not corrected (centimetre-level in the humid tropics).")
    if span < 180:
        c.cap("low", f"Only {span} days of data; velocities this short are dominated by seasonal and "
                     "atmospheric signals, not long-term motion.")
    if len(chain) < 5:
        c.cap("low", f"Only {len(chain)} interferograms in the chain.")
    if stats["valid_fraction"] < 0.3:
        c.cap("low", f"Only {stats['valid_fraction']:.0%} of the AOI stayed coherent through the chain.")
    if stats["reference"] != "stable_ref_median":
        c.cap("medium", "No stable reference area; motion is relative to the AOI's median.")
    least = flags.least_mature([d.get("maturity") for d in diags])
    if least != "VALIDATED":
        c.cap("medium", f"Least mature interferogram is {least}.")
    if not all(d.get("iono_applied") for d in diags):
        c.cap("medium", "Ionospheric correction was not available for every interferogram.")
    if noise is not None and noise > thr:
        c.cap("low", f"Velocity noise (~{noise:.0f} mm/yr) exceeds the hotspot threshold ({thr:.0f} mm/yr).")

    return {
        "dates": epochs,
        "track": chosen,
        "interferograms": diags,
        "stats": stats,
        "outputs": outputs,
        "coverage": {**coverage, "track_used": chosen, "pairs_used": len(chain)},
        "method": {
            "engine": "deformation",
            "product": "NISAR L2 GUNW (unwrappedInterferogram, 80 m)",
            "approach": "sequential small-baseline chain, cumulative sum, linear velocity fit",
            "wavelength_m": round(config.NISAR_L_WAVELENGTH_M, 5),
            "sign_convention": config.DEFO_SIGN_CONVENTION,
            "min_coherence": config.DEFO_MIN_COHERENCE,
            "ionosphere_corrected": all(d.get("iono_applied") for d in diags),
            "troposphere_corrected": False,
            "deramped": all(d.get("deramped") for d in diags),
            "measurement": "line of sight (LOS)",
        },
        "flags": {"maturity": least, "radar_mode": flags.radar_mode_label([d.get("radar_mode") for d in diags]),
                  "short_time_span": span < 180, "tropo_corrected": False},
        "confidence": c.level,
        "confidence_reasons": c.reasons,
    }


def _quicklook(path: Path, vel, cum, epochs, inside, thr) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:  # pragma: no cover
        return None
    fig, (a0, a1) = plt.subplots(1, 2, figsize=(18, 6), gridspec_kw={"width_ratios": [1.6, 1]})
    lim = np.nanpercentile(np.abs(vel), 98) if np.isfinite(vel).any() else 1
    im = a0.imshow(vel, cmap="RdBu_r", vmin=-lim, vmax=lim)
    fig.colorbar(im, ax=a0, shrink=0.75, label="LOS velocity (mm/yr), + = away from satellite")
    a0.set_title(f"LOS velocity {epochs[0]} → {epochs[-1]} (masked: low coherence)")
    a0.axis("off")
    from datetime import date as _d

    xs = [_d.fromisoformat(e) for e in epochs]
    ok = np.isfinite(vel)
    hot = ok & (vel > thr)
    a1.plot(xs, [np.nanmedian(c[ok]) if ok.any() else np.nan for c in cum], "-o", label="AOI median")
    if hot.any():
        a1.plot(xs, [np.nanmedian(c[hot]) for c in cum], "-o", color="#c0392b",
                label=f"pixels > +{thr:.0f} mm/yr")
    a1.axhline(0, color="#999", lw=0.8)
    a1.set_ylabel("cumulative LOS (mm)")
    a1.set_title("Cumulative LOS displacement")
    a1.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=85)
    plt.close(fig)
    return path


__all__ = ["DeformationError", "correct_ifg", "fit_velocity", "longest_chain", "run_deformation",
           "select_chain"]

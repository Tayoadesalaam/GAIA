"""Layer 3 — Processing: water / flood change between two dates.

Method (``calibration.method = "per_date_classification"``):

1. Load HH/HV linear power (gamma0) for each date; align date 2 onto date 1's grid.
2. Boxcar speckle filter in linear power, then convert to dB.
3. Calibrate water thresholds **per radar mode** against a permanent water
   reference (``water_ref`` → should be water) and a never-flooded land
   reference (``dry_ref`` → should be land), recording hit and false-alarm rates.
   Without both references, fall back to fixed defaults and say so
   (``method = "default_uncalibrated"``).
4. Classify water on each date: ``HH < t_hh AND HV < t_hv``.
5. Change map: 0 unchanged · 1 new water · 2 receded water · 255 nodata,
   small speckle patches removed. Written as a COG with calibration metadata.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from gaia import config
from gaia.discovery import parse_aoi

METHOD_CALIBRATED = "per_date_classification"
METHOD_DEFAULT = "default_uncalibrated"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
@dataclass
class Scene:
    date: str
    hh_db: np.ndarray
    hv_db: np.ndarray
    valid: np.ndarray
    transform: Any
    crs: Any
    radar_mode: str | None = None
    maturity: str | None = None
    granule_id: str | None = None
    track: int | None = None


def boxcar_db(power: np.ndarray, window: int) -> np.ndarray:
    """NaN-aware boxcar mean in linear power, returned in dB."""
    p = power.astype("float32", copy=True)
    good = np.isfinite(p) & (p > 0)
    if window and window > 1:
        from scipy.ndimage import uniform_filter

        num = uniform_filter(np.where(good, p, 0).astype("float32"), window, mode="nearest")
        den = uniform_filter(good.astype("float32"), window, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            p = num / den
        good = good & (den > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        db = 10 * np.log10(p)
    db[~good] = np.nan
    return db


def _read_power(path: str, ref: Scene | None):
    import rasterio
    from rasterio.warp import Resampling, reproject

    with rasterio.open(path) as src:
        if ref is None or (src.crs == ref.crs and src.transform == ref.transform
                           and src.shape == ref.hh_db.shape):
            arr = src.read(1, masked=True).filled(np.nan).astype("float32")
            return arr, src.transform, src.crs
        dst = np.full(ref.hh_db.shape, np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1), destination=dst,
            dst_transform=ref.transform, dst_crs=ref.crs,
            dst_nodata=np.nan, resampling=Resampling.bilinear,
        )
        return dst, ref.transform, ref.crs


def load_scene(sidecar: dict[str, Any], *, ref: Scene | None = None,
               speckle_window: int = config.SPECKLE_WINDOW) -> Scene:
    """Load one ingested date (see :mod:`gaia.ingest` sidecar) as a Scene."""
    hh, transform, crs = _read_power(sidecar["paths"]["HH"], ref)
    hv, _, _ = _read_power(sidecar["paths"]["HV"], ref)
    hh_db, hv_db = boxcar_db(hh, speckle_window), boxcar_db(hv, speckle_window)
    g = sidecar.get("granule") or {}
    return Scene(
        date=sidecar["date"], hh_db=hh_db, hv_db=hv_db,
        valid=np.isfinite(hh_db) & np.isfinite(hv_db),
        transform=transform, crs=crs,
        radar_mode=g.get("radar_mode"), maturity=g.get("maturity"),
        granule_id=sidecar.get("granule_id"), track=g.get("track"),
    )


def region_mask(geom_wkt_or_geojson: Any, scene: Scene) -> np.ndarray:
    """Boolean mask of pixels inside an EPSG:4326 geometry."""
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    geom = parse_aoi(geom_wkt_or_geojson)
    g = transform_geom("EPSG:4326", scene.crs, geom.__geo_interface__)
    return ~geometry_mask([g], out_shape=scene.hh_db.shape, transform=scene.transform)


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
@dataclass
class Calibration:
    method: str
    radar_mode: str | None
    threshold_hh_db: float
    threshold_hv_db: float
    hit_rate: float | None = None
    false_alarm_rate: float | None = None
    n_water_px: int = 0
    n_dry_px: int = 0
    dates: list[str] = field(default_factory=list)
    note: str | None = None
    # False when known land is as dark as water (e.g. dry sand): water maps are unreliable.
    water_detectable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sample(a: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    return a if a.shape[0] <= n else a[rng.choice(a.shape[0], n, replace=False)]


def calibrate_thresholds(
    water_px: np.ndarray,
    dry_px: np.ndarray,
    *,
    radar_mode: str | None = None,
    hh_grid: Sequence[float] = config.CALIBRATION_HH_GRID,
    hv_grid: Sequence[float] = config.CALIBRATION_HV_GRID,
    max_false_alarm: float = config.CALIBRATION_MAX_FALSE_ALARM,
    tolerance: float = 0.01,
    max_samples: int = 50_000,
    seed: int = 0,
) -> Calibration:
    """Grid-search (HH, HV) thresholds.

    ``water_px`` / ``dry_px`` are (N, 2) arrays of [HH_dB, HV_dB]. Picks the
    pair with the best hit rate − false-alarm rate among pairs whose false
    alarm rate is ≤ ``max_false_alarm`` (or the lowest false-alarm pair if none
    qualifies).
    """
    rng = np.random.default_rng(seed)
    w = _sample(water_px[np.isfinite(water_px).all(1)], max_samples, rng)
    d = _sample(dry_px[np.isfinite(dry_px).all(1)], max_samples, rng)
    if len(w) == 0 or len(d) == 0:
        raise ValueError("calibration needs valid pixels in both water_ref and dry_ref")

    hh = np.asarray(hh_grid, dtype="float32")
    hv = np.asarray(hv_grid, dtype="float32")
    # hit[i, j] = fraction of water px with HH < hh[i] and HV < hv[j]
    w_hh = w[:, 0][None, :] < hh[:, None]  # (I, Nw)
    w_hv = w[:, 1][None, :] < hv[:, None]  # (J, Nw)
    d_hh = d[:, 0][None, :] < hh[:, None]
    d_hv = d[:, 1][None, :] < hv[:, None]
    hit = (w_hh.astype("float32") @ w_hv.T.astype("float32")) / len(w)
    fa = (d_hh.astype("float32") @ d_hv.T.astype("float32")) / len(d)

    ok = fa <= max_false_alarm
    score = np.where(ok, hit - fa, -np.inf)
    if not ok.any():
        score = -fa
    # Most conservative (lowest) thresholds within `tolerance` of the best
    # score: dry_ref can't contain every land type (runways, wet soil), so
    # don't loosen thresholds for a negligible gain on the references.
    near_best = score >= score.max() - tolerance
    conservativeness = hh[:, None] + hv[None, :]
    i, j = np.unravel_index(int(np.argmin(np.where(near_best, conservativeness, np.inf))), score.shape)
    notes = [] if ok.any() else [f"No threshold pair kept false alarms ≤ {max_false_alarm:.0%}."]
    if i in (0, len(hh) - 1) or j in (0, len(hv) - 1):
        notes.append("Selected threshold is at the edge of the search grid; references may be unrepresentative.")
    return Calibration(
        method=METHOD_CALIBRATED,
        radar_mode=radar_mode,
        threshold_hh_db=float(hh[i]),
        threshold_hv_db=float(hv[j]),
        hit_rate=round(float(hit[i, j]), 4),
        false_alarm_rate=round(float(fa[i, j]), 4),
        n_water_px=int(len(w)),
        n_dry_px=int(len(d)),
        note=" ".join(notes) or None,
    )


def calibrate_dry_only(
    dry_px: np.ndarray,
    *,
    radar_mode: str | None = None,
    hh_grid: Sequence[float] = config.CALIBRATION_HH_GRID,
    hv_grid: Sequence[float] = config.CALIBRATION_HV_GRID,
    max_false_alarm: float = config.DRY_ONLY_MAX_FALSE_ALARM,
    max_samples: int = 50_000,
) -> Calibration:
    """No permanent water to calibrate against: choose the most permissive
    thresholds (never looser than the fixed defaults) that keep known dry land
    below ``max_false_alarm``. Hit rate is unknown.

    If no such thresholds still pass typical open water (TYPICAL_WATER_*_DB),
    water can't be told apart from land (typically dry sand) → water_detectable=False.
    """
    d = _sample(dry_px[np.isfinite(dry_px).all(1)], max_samples, np.random.default_rng(0))
    if len(d) == 0:
        raise ValueError("dry-only calibration needs valid dry_ref pixels")
    hh = np.asarray([x for x in hh_grid if x <= config.DEFAULT_WATER_THRESHOLD_HH_DB], dtype="float32")
    hv = np.asarray([x for x in hv_grid if x <= config.DEFAULT_WATER_THRESHOLD_HV_DB], dtype="float32")
    fa = ((d[:, 0][None, :] < hh[:, None]).astype("float32")
          @ (d[:, 1][None, :] < hv[:, None]).T.astype("float32")) / len(d)
    ok = fa <= max_false_alarm
    # Pairs that keep land out AND still pass typical open water in both channels.
    room = ok & (hh[:, None] >= config.TYPICAL_WATER_HH_DB) & (hv[None, :] >= config.TYPICAL_WATER_HV_DB)
    pool = room if room.any() else ok
    if pool.any():
        permissive = np.where(pool, hh[:, None] + hv[None, :], -np.inf)
        i, j = np.unravel_index(int(np.argmax(permissive)), fa.shape)
    else:
        i, j = np.unravel_index(int(np.argmin(fa)), fa.shape)
    t_hh, t_hv = float(hh[i]), float(hv[j])
    detectable = bool(room.any())
    notes = ["No permanent water to calibrate against: thresholds keep known dry land below "
             f"{max_false_alarm:.0%} false alarms; hit rate unknown."]
    if not detectable:
        notes.append("Land here is as dark as open water (rejecting it needs HH < "
                     f"{t_hh} dB / HV < {t_hv} dB, below typical water at "
                     f"{config.TYPICAL_WATER_HH_DB} / {config.TYPICAL_WATER_HV_DB} dB; typical of dry sand). "
                     "Water cannot be reliably detected in this area.")
    return Calibration(
        method="dry_only_calibration", radar_mode=radar_mode, threshold_hh_db=t_hh, threshold_hv_db=t_hv,
        hit_rate=None, false_alarm_rate=round(float(fa[i, j]), 4), n_water_px=0, n_dry_px=int(len(d)),
        note=" ".join(notes), water_detectable=detectable,
    )


def calibrate_auto(water_px: np.ndarray | None, dry_px: np.ndarray | None,
                   radar_mode: str | None) -> Calibration:
    """Best calibration the available references allow."""
    has_w = water_px is not None and len(water_px) > 0
    has_d = dry_px is not None and len(dry_px) > 0
    try:
        if has_w and has_d:
            return calibrate_thresholds(water_px, dry_px, radar_mode=radar_mode)
        if has_d:
            return calibrate_dry_only(dry_px, radar_mode=radar_mode)
    except ValueError as exc:
        return default_calibration(radar_mode, str(exc))
    return default_calibration(
        radar_mode, "No water_ref or dry_ref inside the AOI; using fixed TerraPulse thresholds "
                    "(HH < -14 dB, HV < -20 dB) without calibration.")


def default_calibration(radar_mode: str | None, reason: str) -> Calibration:
    return Calibration(
        method=METHOD_DEFAULT,
        radar_mode=radar_mode,
        threshold_hh_db=config.DEFAULT_WATER_THRESHOLD_HH_DB,
        threshold_hv_db=config.DEFAULT_WATER_THRESHOLD_HV_DB,
        note=reason,
    )


# --------------------------------------------------------------------------- #
# Classification & change
# --------------------------------------------------------------------------- #
def classify_water(scene: Scene, cal: Calibration) -> np.ndarray:
    return scene.valid & (scene.hh_db < cal.threshold_hh_db) & (scene.hv_db < cal.threshold_hv_db)


def remove_small_patches(mask: np.ndarray, min_px: int) -> np.ndarray:
    if min_px <= 1 or not mask.any():
        return mask
    from scipy.ndimage import label

    lab, n = label(mask, structure=np.ones((3, 3), bool))
    sizes = np.bincount(lab.ravel())
    keep = sizes >= min_px
    keep[0] = False
    return keep[lab]


def change_map(water1: np.ndarray, water2: np.ndarray, valid: np.ndarray,
               min_patch_px: int = config.MIN_CHANGE_PATCH_PX) -> np.ndarray:
    out = np.full(water1.shape, config.CLASS_NODATA, dtype="uint8")
    out[valid] = config.CLASS_UNCHANGED
    new = remove_small_patches(water2 & ~water1 & valid, min_patch_px)
    gone = remove_small_patches(water1 & ~water2 & valid, min_patch_px)
    out[new] = config.CLASS_NEW_WATER
    out[gone] = config.CLASS_RECEDED_WATER
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
COLORMAP = {
    config.CLASS_UNCHANGED: (0, 0, 0, 0),
    config.CLASS_NEW_WATER: (30, 120, 255, 255),
    config.CLASS_RECEDED_WATER: (230, 120, 20, 255),
    config.CLASS_NODATA: (0, 0, 0, 0),
}


def write_cog(path: Path, data: np.ndarray, transform, crs, tags: dict[str, Any]) -> Path:
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.shutil import copy as rio_copy

    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                   dtype="uint8", crs=crs, transform=transform, nodata=config.CLASS_NODATA)
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(data, 1)
            tmp.write_colormap(1, COLORMAP)
            tmp.update_tags(**{k: v if isinstance(v, str) else json.dumps(v) for k, v in tags.items()})
            tmp.update_tags(1, classes="0=unchanged,1=new_water,2=receded_water,255=nodata")
            rio_copy(tmp, str(path), driver="COG", compress="DEFLATE",
                     overview_resampling="mode", blocksize=512)
    return path


def render_quicklook(path: Path, scene_after: Scene, change: np.ndarray, title: str,
                     step: int = 4) -> Path | None:
    """PNG preview: HH dB of date 2 in grey, change classes in colour."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:  # pragma: no cover
        return None
    bg = scene_after.hh_db[::step, ::step]
    ch = change[::step, ::step]
    fig, ax = plt.subplots(figsize=(14, 7.5))
    ax.imshow(bg, cmap="gray", vmin=-22, vmax=2)
    rgba = np.zeros(ch.shape + (4,), dtype="float32")
    for cls, color in COLORMAP.items():
        rgba[ch == cls] = np.array(color) / 255
    ax.imshow(rgba)
    ax.legend(handles=[Patch(color=np.array(COLORMAP[1][:3]) / 255, label="new water"),
                       Patch(color=np.array(COLORMAP[2][:3]) / 255, label="receded water")],
              loc="upper right")
    ax.set_title(title)
    ax.axis("off")
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def _ref_pixels(scene: Scene, mask: np.ndarray) -> np.ndarray:
    m = mask & scene.valid
    return np.stack([scene.hh_db[m], scene.hv_db[m]], axis=1)


def detect_water_change(
    before: dict[str, Any],
    after: dict[str, Any],
    aoi: Any,
    out_path: Path | str,
    *,
    water_ref: Any | None = None,
    dry_ref: Any | None = None,
    exclude: Any | None = None,
    speckle_window: int = config.SPECKLE_WINDOW,
    quicklook: bool = True,
) -> dict[str, Any]:
    """Run water change detection between two ingested dates.

    ``before`` / ``after`` are ingest sidecars (dicts). Returns a dict with the
    change map path, per-date diagnostics, calibration(s) and change statistics.
    """
    out_path = Path(out_path)
    s1 = load_scene(before, speckle_window=speckle_window)
    s2 = load_scene(after, ref=s1, speckle_window=speckle_window)
    scenes = [s1, s2]

    aoi_mask = region_mask(aoi, s1)
    wmask = region_mask(water_ref, s1) if water_ref else None
    dmask = region_mask(dry_ref, s1) if dry_ref else None

    # --- calibrate per radar mode (pooling dates of the same mode) ---------- #
    cal_by_mode: dict[str | None, Calibration] = {}
    for mode in dict.fromkeys(s.radar_mode for s in scenes):
        group = [s for s in scenes if s.radar_mode == mode]
        cal = calibrate_auto(
            np.concatenate([_ref_pixels(s, wmask) for s in group]) if wmask is not None else None,
            np.concatenate([_ref_pixels(s, dmask) for s in group]) if dmask is not None else None,
            mode,
        )
        cal.dates = [s.date for s in group]
        cal_by_mode[mode] = cal

    # --- classify each date ------------------------------------------------- #
    px_km2 = abs(s1.transform.a * s1.transform.e) / 1e6
    per_date: list[dict[str, Any]] = []
    waters = []
    for s in scenes:
        cal = cal_by_mode[s.radar_mode]
        water = classify_water(s, cal)
        waters.append(water)
        d: dict[str, Any] = {
            "date": s.date, "granule_id": s.granule_id, "track": s.track,
            "radar_mode": s.radar_mode, "maturity": s.maturity,
            "valid_fraction": round(float((s.valid & aoi_mask).sum() / max(aoi_mask.sum(), 1)), 4),
            "water_km2": round(float((water & aoi_mask).sum() * px_km2), 3),
            "water_ref_fraction": None, "dry_ref_false_alarm": None,
        }
        if wmask is not None and (wmask & s.valid).any():
            d["water_ref_fraction"] = round(float(water[wmask & s.valid].mean()), 4)
            d["water_ref_km2"] = round(float(water[wmask].sum() * px_km2), 3)
        if dmask is not None and (dmask & s.valid).any():
            d["dry_ref_false_alarm"] = round(float(water[dmask & s.valid].mean()), 4)
        per_date.append(d)

    valid = s1.valid & s2.valid & aoi_mask
    excluded_km2 = 0.0
    if exclude:
        xmask = region_mask(exclude, s1) & aoi_mask
        excluded_km2 = round(float(xmask.sum() * px_km2), 3)
        valid &= ~xmask
    change = change_map(waters[0], waters[1], valid)

    # Noise diagnostic: permanent water should never "change". Any change
    # inside water_ref is wind/speckle noise, not real flooding.
    water_ref_change = None
    if wmask is not None and (wmask & valid).any():
        water_ref_change = round(float(np.isin(change[wmask & valid], (1, 2)).mean()), 4)

    stats = {
        "pixel_size_m": [abs(s1.transform.a), abs(s1.transform.e)],
        "aoi_valid_fraction": round(float(valid.sum() / max(aoi_mask.sum(), 1)), 4),
        "excluded_km2": excluded_km2,
        "water_ref_change_fraction": water_ref_change,
        "new_water_km2": round(float((change == config.CLASS_NEW_WATER).sum() * px_km2), 3),
        "receded_water_km2": round(float((change == config.CLASS_RECEDED_WATER).sum() * px_km2), 3),
        "unchanged_km2": round(float((change == config.CLASS_UNCHANGED).sum() * px_km2), 3),
    }

    calibrations = [c.to_dict() for c in cal_by_mode.values()]
    write_cog(out_path, change, s1.transform, s1.crs, {
        "gaia_product": "water_change",
        "gaia_dates": [s1.date, s2.date],
        "gaia_granules": [s1.granule_id, s2.granule_id],
        "gaia_calibration": calibrations,
        "gaia_stats": stats,
        "gaia_speckle_window": speckle_window,
    })
    ql = None
    if quicklook:
        ql = render_quicklook(out_path.with_suffix(".png"), s2, change,
                              f"GAIA water change {s1.date} → {s2.date}  (blue: new water, orange: receded)")
    return {
        "change_map_path": str(out_path),
        "quicklook_path": str(ql) if ql else None,
        "dates": [s1.date, s2.date],
        "per_date": per_date,
        "calibrations": calibrations,
        "stats": stats,
        "crs": s1.crs.to_string() if s1.crs else None,
    }


__all__ = [
    "Calibration",
    "Scene",
    "boxcar_db",
    "calibrate_thresholds",
    "change_map",
    "classify_water",
    "default_calibration",
    "detect_water_change",
    "load_scene",
    "remove_small_patches",
    "write_cog",
]

"""Automatic calibration references from JRC Global Surface Water (GSW).

Hand-picked ``water_ref`` / ``dry_ref`` areas exist only for named sites. For
any other AOI, GSW *occurrence* (share of 1984–2021 Landsat observations in
which a 30 m pixel was water) supplies them:

  * water_ref = occurrence ≥ REF_WATER_MIN_OCCURRENCE (permanent water), eroded
    away from shorelines so only pure water pixels remain;
  * dry_ref   = occurrence == 0 (never water), eroded away from any water.

Tiles are 10°×10° GeoTIFFs (256-px blocks) read remotely with HTTP range
requests — only the AOI window is fetched — and cached per AOI.

Source: Pekel, Cottam, Gorelick & Belward (2016), Nature 540, 418–422;
EC JRC / Google. https://global-surface-water.appspot.com/
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np

from gaia import config
from gaia.store import StoreError, get_store
from gaia.discovery import parse_aoi
from gaia.ingest import aoi_key

ATTRIBUTION = ("JRC Global Surface Water v1.4 (1984–2021), Pekel et al. 2016, "
               "© EC JRC / Google")


def tile_names(minx: float, miny: float, maxx: float, maxy: float) -> list[str]:
    """GSW tile ids (named by upper-left corner) covering a lon/lat box."""
    names = []
    for lon0 in range(int(math.floor(minx / 10)) * 10, int(math.floor((maxx - 1e-9) / 10)) * 10 + 1, 10):
        for top in range(int(math.ceil((miny + 1e-9) / 10)) * 10, int(math.ceil(maxy / 10)) * 10 + 1, 10):
            lon = f"{abs(lon0)}{'E' if lon0 >= 0 else 'W'}"
            lat = f"{abs(top)}{'N' if top >= 0 else 'S'}"
            names.append(f"{lon}_{lat}")
    return names


def read_occurrence(geom) -> tuple[np.ndarray, Any]:
    """GSW occurrence (uint8 %, 255 = no data) for the AOI bbox, EPSG:4326."""
    import rasterio
    from rasterio.env import Env
    from rasterio.merge import merge

    minx, miny, maxx, maxy = geom.bounds
    urls = [f"/vsicurl/{config.GSW_URL_TEMPLATE.format(tile=t)}" for t in tile_names(minx, miny, maxx, maxy)]
    with Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
             GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="1"):
        srcs = [rasterio.open(u) for u in urls]
        try:
            arr, transform = merge(srcs, bounds=(minx, miny, maxx, maxy), nodata=255)
        finally:
            for s in srcs:
                s.close()
    return arr[0], transform


def mask_to_wkt(mask: np.ndarray, transform, max_polys: int = 40) -> str | None:
    """Vectorise a boolean mask into a simplified (Multi)Polygon WKT."""
    from rasterio.features import shapes
    from shapely.geometry import shape
    from shapely.ops import unary_union

    if not mask.any():
        return None
    polys = [shape(g) for g, _ in shapes(mask.astype("uint8"), mask=mask, transform=transform)]
    polys = sorted(polys, key=lambda p: p.area, reverse=True)[:max_polys]
    geom = unary_union(polys).simplify(abs(transform.a), preserve_topology=True)
    return None if geom.is_empty else geom.wkt


def derive_references(occ: np.ndarray, transform, inside: np.ndarray | None = None) -> dict[str, Any]:
    """Water / dry reference masks → WKT, with stats. ``occ`` is GSW occurrence."""
    from scipy.ndimage import binary_erosion

    valid = occ != 255
    if inside is not None:
        valid &= inside
    px_km2 = (abs(transform.a) * 111.32) * (abs(transform.e) * 111.32 *
                                            math.cos(math.radians(transform.f)))
    water = valid & (occ >= config.REF_WATER_MIN_OCCURRENCE) & (occ <= 100)
    dry = valid & (occ == 0)
    water_core = binary_erosion(water, iterations=config.REF_WATER_ERODE_PX) & water
    dry_core = binary_erosion(dry, iterations=config.REF_DRY_ERODE_PX) & dry

    out: dict[str, Any] = {
        "source": "jrc_gsw",
        "attribution": ATTRIBUTION,
        "permanent_water_km2": round(float(water.sum() * px_km2), 2),
        "never_water_km2": round(float(dry.sum() * px_km2), 2),
        "water_ref": None,
        "dry_ref": None,
        "notes": [],
    }
    if water_core.sum() >= config.REF_MIN_WATER_PX:
        out["water_ref"] = mask_to_wkt(water_core, transform)
        out["water_ref_km2"] = round(float(water_core.sum() * px_km2), 2)
    else:
        out["notes"].append(
            "No permanent water (GSW occurrence ≥ "
            f"{config.REF_WATER_MIN_OCCURRENCE}%) large enough in this area: water thresholds can only be "
            "checked against dry land (no hit rate), and wind roughening can't be checked.")
    if dry_core.sum() >= config.REF_MIN_DRY_PX:
        out["dry_ref"] = mask_to_wkt(dry_core, transform)
        out["dry_ref_km2"] = round(float(dry_core.sum() * px_km2), 2)
    else:
        out["notes"].append("Too little never-flooded land for a dry reference.")
    return out


def auto_references(aoi: Any, *, reader: Callable | None = None, use_cache: bool = True) -> dict[str, Any]:
    """Reference areas for ``aoi`` from JRC GSW. Never raises: on failure the
    result has ``source = "unavailable"`` and an ``error`` (callers fall back)."""
    from rasterio.features import geometry_mask

    geom = parse_aoi(aoi)
    key = aoi_key(geom)
    if use_cache and reader is None:
        try:
            hit, cached = get_store().cache_get("references", key)
        except StoreError:
            hit, cached = False, None
        if hit:
            return cached
    try:
        occ, transform = (reader or read_occurrence)(geom)
        inside = ~geometry_mask([geom.__geo_interface__], out_shape=occ.shape, transform=transform)
        refs = derive_references(occ, transform, inside)
    except Exception as exc:  # noqa: BLE001 — disclosed, never fatal
        return {"source": "unavailable", "error": f"{type(exc).__name__}: {exc}",
                "water_ref": None, "dry_ref": None,
                "notes": ["Automatic reference areas (JRC Global Surface Water) could not be loaded; "
                          "fixed thresholds used."]}
    if reader is None:
        try:
            get_store().cache_put("references", key, refs)
        except StoreError:
            pass
    return refs


__all__ = ["ATTRIBUTION", "auto_references", "derive_references", "mask_to_wkt", "read_occurrence", "tile_names"]

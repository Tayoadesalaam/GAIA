"""Map overlays: engine output GeoTIFF → transparent Web-Mercator PNG + bounds.

The engine COGs are in UTM with numeric classes, which a browser map can't
draw directly. ``render_overlay`` reprojects one output to EPSG:3857 (so a
Leaflet/MapLibre image overlay lines up exactly), colours it, and returns the
lat/lon bounds and a legend. Results are cached next to the source raster.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

MAX_DIM = 1200  # px on the longest side

# Categorical styles: value -> (label, RGBA). Values not listed are transparent.
_CAT: dict[str, dict[int, tuple[str, tuple[int, int, int, int]]]] = {
    "change_map": {1: ("New water", (59, 130, 246, 215)),
                   2: ("Receded water", (249, 115, 22, 215))},
    "peak_flood_map": {1: ("Normal water", (30, 64, 175, 150)),
                       2: ("Flooded (open water)", (56, 189, 248, 225)),
                       3: ("Flooded vegetation (experimental)", (34, 197, 94, 225))},
    "season_class": {0: ("No seasonal signal", (156, 163, 175, 80)),
                     1: ("Full season observed", (34, 197, 94, 190)),
                     2: ("Season end not observed", (234, 179, 8, 190)),
                     3: ("Season started before record", (168, 85, 247, 190))},
}
# Continuous styles: (title, unit, colour ramp, symmetric about 0, values to treat as empty)
_CONT: dict[str, dict[str, Any]] = {
    "flood_frequency": {"title": "Passes flooded", "unit": "passes",
                        "ramp": [(186, 230, 253), (14, 165, 233), (12, 74, 110)], "empty": [0]},
    "water_occurrence": {"title": "Water occurrence", "unit": "%",
                         "ramp": [(191, 219, 254), (59, 130, 246), (30, 58, 138)], "empty": [0]},
    "season_start_doy": {"title": "Season start", "unit": "day of year",
                         "ramp": [(68, 1, 84), (33, 145, 140), (253, 231, 37)], "empty": [0]},
    "velocity_mm_yr": {"title": "LOS velocity (+ away from satellite)", "unit": "mm/yr",
                       "ramp": [(37, 99, 235), (245, 245, 245), (220, 38, 38)], "symmetric": True},
}
STYLES = set(_CAT) | set(_CONT)


class OverlayError(ValueError):
    pass


def _ramp(t: np.ndarray, stops: list[tuple[int, int, int]]) -> np.ndarray:
    t = np.clip(t, 0, 1) * (len(stops) - 1)
    i = np.minimum(t.astype(int), len(stops) - 2)
    f = (t - i)[..., None]
    s = np.array(stops, dtype=float)
    return (s[i] * (1 - f) + s[i + 1] * f).astype(np.uint8)


def _read_3857(src_path: Path, categorical: bool):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_bounds

    with rasterio.open(src_path) as src, WarpedVRT(
            src, crs="EPSG:3857",
            resampling=Resampling.nearest if categorical else Resampling.bilinear) as vrt:
        scale = max(vrt.width, vrt.height) / MAX_DIM
        h, w = (vrt.height, vrt.width) if scale <= 1 else (round(vrt.height / scale), round(vrt.width / scale))
        data = vrt.read(1, out_shape=(h, w), masked=True,
                        resampling=Resampling.nearest if categorical else Resampling.average)
        west, south, east, north = transform_bounds(vrt.crs, "EPSG:4326", *vrt.bounds)
    mask = np.ma.getmaskarray(data)
    arr = np.asarray(data.filled(0), dtype=float)
    mask |= ~np.isfinite(arr)
    return arr, mask, [[south, west], [north, east]]


def render_overlay(src_path: str | Path, style: str, out_dir: str | Path | None = None) -> dict[str, Any]:
    """Colour ``src_path`` with ``style``; write ``overlay_<stem>.png`` and return
    ``{"image": png_path, "bounds": [[s, w], [n, e]], "legend": {...}}``."""
    if style not in STYLES:
        raise OverlayError(f"Unknown overlay style '{style}'. Known: {sorted(STYLES)}")
    src_path = Path(src_path)
    if not src_path.exists():
        raise OverlayError(f"No such output: {src_path.name}")
    out_dir = Path(out_dir or src_path.parent)
    png = out_dir / f"overlay_{src_path.stem}.png"
    meta = png.with_suffix(".json")
    if png.exists() and meta.exists() and png.stat().st_mtime >= src_path.stat().st_mtime:
        cached = json.loads(meta.read_text())
        if cached.get("style") == style:
            return {**cached, "image": str(png)}

    from PIL import Image

    arr, mask, bounds = _read_3857(src_path, style in _CAT)
    rgba = np.zeros(arr.shape + (4,), dtype=np.uint8)
    if style in _CAT:
        items = []
        for value, (label, colour) in _CAT[style].items():
            hit = (arr == value) & ~mask
            rgba[hit] = colour
            items.append({"value": value, "label": label, "color": "rgba(%d,%d,%d,%.2f)" % (
                *colour[:3], colour[3] / 255)})
        legend = {"type": "categorical", "items": items}
    else:
        st = _CONT[style]
        ok = ~mask & ~np.isin(arr, st.get("empty", []))
        vals = arr[ok]
        if vals.size == 0:
            lo, hi = 0.0, 1.0
        elif st.get("symmetric"):
            hi = float(np.percentile(np.abs(vals), 95)) or 1.0
            lo = -hi
        else:
            lo, hi = (float(v) for v in np.percentile(vals, [2, 98]))
            if hi <= lo:
                hi = lo + 1
        rgba[ok, :3] = _ramp((arr[ok] - lo) / (hi - lo), st["ramp"])
        rgba[ok, 3] = 200
        legend = {"type": "continuous", "title": st["title"], "unit": st["unit"],
                  "min": round(lo, 2), "max": round(hi, 2),
                  "colors": ["rgb(%d,%d,%d)" % c for c in st["ramp"]]}

    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(png, optimize=True)
    info = {"style": style, "bounds": bounds, "legend": legend, "source": src_path.name}
    meta.write_text(json.dumps(info))
    return {**info, "image": str(png)}

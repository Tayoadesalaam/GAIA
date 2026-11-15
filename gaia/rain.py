"""GPM IMERG Late Run daily V07 → ``rain_affected`` pass flag.

Wet canopies mimic flooded vegetation and wet soil mimics open water, so a pass
with ≥ 15 mm of rain over the AOI in the 3 days before it is flagged
``rain_affected`` and excluded from aggregate statistics.

Data: GES DISC OPeNDAP (``GPM_3IMERGDL.07``), subset to the AOI's 0.1° cells,
authenticated with the Earthdata bearer token. Requires "NASA GESDISC DATA
ARCHIVE" approved in Earthdata Login. Results are cached in ``data/rain/``.

If rainfall cannot be retrieved, the flag is ``None`` (unknown) — never
silently ``False``.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

from gaia import config
from gaia.store import StoreError, get_store
from gaia.discovery import _to_date, parse_aoi


class RainError(RuntimeError):
    pass


def cell_index_range(min_v: float, max_v: float, origin: float, n: int) -> tuple[int, int]:
    """Inclusive index range of 0.1° cells (edges start at ``origin``) touching [min_v, max_v]."""
    res = config.IMERG_RES_DEG
    # +1e-9: (3.10 + 180) / 0.1 is 1830.9999… in floating point.
    i0 = max(0, int(math.floor((min_v - origin) / res + 1e-9)))
    i1 = min(n - 1, int(math.floor((max_v - origin) / res - 1e-9)))
    return i0, max(i0, i1)


def build_url(day: date, version: str, lon_idx: tuple[int, int], lat_idx: tuple[int, int]) -> str:
    fname = config.IMERG_FILE_TEMPLATE.format(ymd=day.strftime("%Y%m%d"), version=version)
    return (f"{config.IMERG_OPENDAP_BASE}/{day:%Y}/{day:%m}/{fname}.ascii?"
            f"precipitation[0:0][{lon_idx[0]}:{lon_idx[1]}][{lat_idx[0]}:{lat_idx[1]}]")


def parse_ascii(text: str) -> np.ndarray:
    """Parse OPeNDAP ASCII rows ``precipitation.precipitation[...][lon=x], v1, v2 …``."""
    rows = []
    for line in text.splitlines():
        if line.startswith("precipitation.precipitation["):
            rows.append([float(v) for v in line.split(",")[1:]])
    if not rows:
        raise RainError("No precipitation values in OPeNDAP response")
    arr = np.array(rows, dtype="float32")
    arr[arr < 0] = np.nan  # fill value (-9999.9)
    return arr


def _http_get(url: str) -> tuple[int, str]:
    import requests

    token = (config.EARTHDATA_TOKEN or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token and not token.endswith("...") else {}
    r = requests.get(url, headers=headers, timeout=90)
    return r.status_code, r.text


def daily_precip(
    day: date | str,
    aoi: Any,
    *,
    fetch: Callable[[str], tuple[int, str]] | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """AOI mean and max daily precipitation (mm) for one day."""
    day = _to_date(day)
    geom = parse_aoi(aoi)
    minx, miny, maxx, maxy = geom.bounds
    lon_idx = cell_index_range(minx, maxx, -180.0, 3600)
    lat_idx = cell_index_range(miny, maxy, -90.0, 1800)
    key = f"{day.isoformat()}_{lon_idx[0]}-{lon_idx[1]}_{lat_idx[0]}-{lat_idx[1]}"

    # an explicit cache_dir keeps plain files there; otherwise the configured store (files or Supabase)
    cache_file = Path(cache_dir) / f"imerg_{key}.json" if cache_dir else None
    if fetch is None:
        if cache_file is not None:
            if cache_file.exists():
                return json.loads(cache_file.read_text())
        else:
            try:
                hit, cached = get_store().cache_get("rain", key)
            except StoreError:
                hit, cached = False, None
            if hit:
                return cached

    fetch = fetch or _http_get
    last = None
    for version in config.IMERG_VERSIONS:
        status, text = fetch(build_url(day, version, lon_idx, lat_idx))
        if status == 200:
            arr = parse_ascii(text)
            if not np.isfinite(arr).any():
                raise RainError(f"IMERG {day}: all cells are fill values")
            result = {
                "date": day.isoformat(),
                "mean_mm": round(float(np.nanmean(arr)), 2),
                "max_mm": round(float(np.nanmax(arr)), 2),
                "n_cells": int(np.isfinite(arr).sum()),
                "version": version,
            }
            if fetch is _http_get:
                if cache_file is not None:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(result))
                else:
                    try:
                        get_store().cache_put("rain", key, result)
                    except StoreError:
                        pass
            return result
        last = status
        if status in (401, 403):
            raise RainError(
                f"GES DISC refused access (HTTP {status}). Approve 'NASA GESDISC DATA ARCHIVE' "
                "in Earthdata Login → Applications → Authorized Apps.")
    raise RainError(f"IMERG file for {day} not available (last HTTP {last}); "
                    "Late Run lags ~14 h, so very recent dates may be missing.")


def rain_flag(
    pass_date: date | str,
    aoi: Any,
    *,
    lookback_days: int = config.RAIN_LOOKBACK_DAYS,
    threshold_mm: float = config.RAIN_THRESHOLD_MM,
    fetch: Callable[[str], tuple[int, str]] | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """``rain_affected`` for one pass: AOI-mean rain over the ``lookback_days``
    days before the pass date ≥ ``threshold_mm``. ``None`` if unknown."""
    pass_d = _to_date(pass_date)
    days = [pass_d - timedelta(days=k) for k in range(lookback_days, 0, -1)]
    out: dict[str, Any] = {
        "date": pass_d.isoformat(),
        "window": [days[0].isoformat(), days[-1].isoformat()],
        "threshold_mm": threshold_mm,
        "source": "GPM IMERG Late Run daily V07 (GES DISC)",
        "rain_affected": None,
        "rain_mm": None,
        "rain_max_cell_mm": None,
        "daily": [],
        "error": None,
    }
    try:
        daily = [daily_precip(d, aoi, fetch=fetch, cache_dir=cache_dir) for d in days]
    except Exception as exc:  # network, auth, parse — disclose, don't guess
        out["error"] = str(exc)
        return out
    total = sum(d["mean_mm"] for d in daily)
    out.update(
        daily=daily,
        rain_mm=round(total, 2),
        rain_max_cell_mm=round(sum(d["max_mm"] for d in daily), 2),
        rain_affected=bool(total >= threshold_mm),
    )
    return out


__all__ = ["RainError", "daily_precip", "rain_flag", "parse_ascii", "build_url", "cell_index_range"]

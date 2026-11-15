"""Turn what a user types into an AOI.

Accepted inputs (in this order):
  * a named site            "lagos"
  * coordinates (lat, lon)   "6.45, 3.40"           → square of MONITOR_POINT_BOX_KM
  * a bbox                   "3.1, 6.38, 3.7, 6.70" (min_lon, min_lat, max_lon, max_lat)
  * WKT / GeoJSON            "POLYGON((…))", {"type": "Polygon", …}
  * a place name             "Ibadan, Nigeria"      → OpenStreetMap Nominatim

Areas larger than MONITOR_MAX_AOI_KM2 are cropped to a centred square and the
response says so — a whole country would take hours to process.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from gaia import config
from gaia.store import StoreError, get_store
from gaia.discovery import parse_aoi


class LocationError(ValueError):
    """The location could not be resolved to a usable AOI."""


@dataclass
class Location:
    query: str
    name: str
    source: str  # site | coordinates | bbox | geometry | geocoder
    lat: float
    lon: float
    aoi_wkt: str
    area_km2: float
    notes: list[str] = field(default_factory=list)
    site: str | None = None
    osm: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["aoi"] = parse_aoi(self.aoi_wkt).__geo_interface__
        return d


_NUM = r"[-+]?\d+(?:\.\d+)?"
_LATLON = re.compile(rf"^\s*({_NUM})\s*[, ]\s*({_NUM})\s*$")
_BBOX = re.compile(rf"^\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*$")


def area_km2(geom: BaseGeometry) -> float:
    from pyproj import Geod

    a, _ = Geod(ellps="WGS84").geometry_area_perimeter(geom)
    return abs(a) / 1e6


def square_around(lat: float, lon: float, side_km: float) -> BaseGeometry:
    half_lat = side_km / 2 / 111.32
    half_lon = side_km / 2 / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return box(lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat)


def _fit(geom: BaseGeometry, lat: float, lon: float, notes: list[str]) -> BaseGeometry:
    """Enforce min/max size."""
    a = area_km2(geom)
    min_km2 = (config.MONITOR_POINT_BOX_KM / 4) ** 2
    if a < min_km2:
        notes.append(f"Area was tiny ({a:.2f} km²); using a {config.MONITOR_POINT_BOX_KM:g} km square "
                     "around its centre.")
        return square_around(lat, lon, config.MONITOR_POINT_BOX_KM)
    if a > config.MONITOR_MAX_AOI_KM2:
        side = math.sqrt(config.MONITOR_MAX_AOI_KM2)
        notes.append(f"Area is {a:,.0f} km²; cropped to a {side:.0f} km × {side:.0f} km square around its "
                     f"centre (limit {config.MONITOR_MAX_AOI_KM2:,.0f} km²). Draw a smaller AOI to choose "
                     "the exact area.")
        return square_around(lat, lon, side)
    return geom


# --------------------------------------------------------------------------- #
# Geocoder (Nominatim) with cache + rate limit
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_last_call = [0.0]


def _cache_path():
    return config.DATA_DIR / "geocode_cache.json"


def nominatim(query: str) -> dict[str, Any] | None:
    """Best match for ``query`` or None. Cached on disk; ≤ 1 request/s."""
    import requests

    key = query.strip().lower()
    store = get_store()
    try:
        hit, cached = store.cache_get("geocode", key)
    except StoreError:
        hit, cached = False, None  # a cache outage must not block place search
    if hit:
        return cached
    with _lock:
        wait = config.GEOCODER_MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(config.GEOCODER_URL,
                             params={"q": query, "format": "jsonv2", "limit": 1},
                             headers={"User-Agent": config.GEOCODER_USER_AGENT}, timeout=30)
        finally:
            _last_call[0] = time.monotonic()
    if r.status_code != 200:
        raise LocationError(f"Geocoder unavailable (HTTP {r.status_code}); try coordinates instead.")
    hits = r.json()
    hit = hits[0] if hits else None
    try:
        store.cache_put("geocode", key, hit)
    except StoreError:
        pass
    return hit


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def resolve_location(query: Any, *,
                     geocode_fn: Callable[[str], dict[str, Any] | None] | None = None) -> Location:
    """Resolve user input to a :class:`Location` with a size-limited AOI."""
    notes: list[str] = []
    if isinstance(query, (dict, list, tuple)):
        geom = parse_aoi(query)
        c = geom.centroid
        g = _fit(geom, c.y, c.x, notes)
        return Location(json.dumps(query)[:200], "custom area", "geometry", c.y, c.x, g.wkt,
                        round(area_km2(g), 1), notes)

    text = str(query or "").strip()
    if not text:
        raise LocationError("Enter a place name, coordinates (lat, lon), a bbox or a geometry.")

    if text.lower() in config.SITES:
        s = config.get_site(text)
        g = parse_aoi(s.aoi_wkt)
        c = g.centroid
        return Location(text, s.description or s.name, "site", c.y, c.x, g.wkt,
                        round(area_km2(g), 1), notes, site=s.name)

    m = _LATLON.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise LocationError(f"Coordinates out of range: lat {lat}, lon {lon} (expected 'lat, lon').")
        g = square_around(lat, lon, config.MONITOR_POINT_BOX_KM)
        notes.append(f"Read as latitude {lat}, longitude {lon}; using a "
                     f"{config.MONITOR_POINT_BOX_KM:g} km square around it.")
        return Location(text, f"{lat:.4f}, {lon:.4f}", "coordinates", lat, lon, g.wkt,
                        round(area_km2(g), 1), notes)

    m = _BBOX.match(text)
    if m:
        try:
            geom = parse_aoi([float(v) for v in m.groups()])
        except ValueError as exc:
            raise LocationError(f"Invalid bbox (expected min_lon, min_lat, max_lon, max_lat): {exc}") from exc
        c = geom.centroid
        g = _fit(geom, c.y, c.x, notes)
        return Location(text, "bounding box", "bbox", c.y, c.x, g.wkt, round(area_km2(g), 1), notes)

    if text[:1] == "{" or text.upper().startswith(("POLYGON", "MULTIPOLYGON", "POINT")):
        try:
            geom = parse_aoi(text)
        except Exception as exc:
            raise LocationError(f"Could not read geometry: {exc}") from exc
        c = geom.centroid
        g = _fit(geom, c.y, c.x, notes) if geom.area > 0 else square_around(c.y, c.x, config.MONITOR_POINT_BOX_KM)
        return Location(text[:200], "custom area", "geometry", c.y, c.x, g.wkt, round(area_km2(g), 1), notes)

    hit = (geocode_fn or nominatim)(text)
    if not hit:
        raise LocationError(f"Could not find a place called '{text}'. Try adding the country, "
                            "or enter coordinates as 'lat, lon'.")
    lat, lon = float(hit["lat"]), float(hit["lon"])
    s, n, w, e = (float(v) for v in hit["boundingbox"])
    geom = box(w, s, e, n) if (e > w and n > s) else square_around(lat, lon, config.MONITOR_POINT_BOX_KM)
    g = _fit(geom, lat, lon, notes)
    osm = {k: hit.get(k) for k in ("display_name", "osm_type", "osm_id", "type", "addresstype")}
    notes.append("Place found with OpenStreetMap Nominatim (© OpenStreetMap contributors).")
    return Location(text, hit.get("display_name") or text, "geocoder", lat, lon, g.wkt,
                    round(area_km2(g), 1), notes, osm=osm)


__all__ = ["Location", "LocationError", "area_km2", "nominatim", "resolve_location", "square_around"]

"""Layer 1 — Discovery.

Find NISAR L2 GCOV granules that intersect an AOI within a time range, using
ASF's ``asf_search``. **Metadata only** — nothing is downloaded here.

Typical use::

    from gaia.discovery import search_granules, coverage_summary
    granules = search_granules(aoi_wkt, "2025-11-01", "2026-11-14")
    summary = coverage_summary(granules, "2025-11-01", "2026-11-14")
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from shapely import wkt as shapely_wkt
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

from gaia import config

_POL_RE = re.compile(r"HH|HV|VH|VV|LH|LV|RH|RV")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Granule:
    """One NISAR GCOV acquisition, reduced to what the GAIA pipeline needs."""

    granule_id: str
    start_time: datetime
    stop_time: datetime | None
    track: int | None
    frame: int | None
    flight_direction: str | None
    polarizations: list[str]
    range_bandwidth: str | None  # raw, e.g. "20", "40+5"
    radar_mode: str | None  # main band, e.g. "20MHz"
    maturity: str  # BETA | PROVISIONAL | VALIDATED | UNKNOWN
    collection: str | None
    url: str | None
    s3_urls: list[str] = field(default_factory=list)
    size_bytes: int | None = None
    frame_coverage: str | None = None  # FULL | PARTIAL
    aoi_overlap: float | None = None  # fraction of the AOI covered, 0–1
    footprint: dict[str, Any] | None = None  # GeoJSON geometry
    product: str = "GCOV"  # GCOV | GUNW
    # GUNW only: the secondary (later) acquisition of the interferometric pair.
    secondary_time: datetime | None = None

    @property
    def date(self) -> date:
        return self.start_time.date()

    @property
    def secondary_date(self) -> date | None:
        return self.secondary_time.date() if self.secondary_time else None

    @property
    def acquisition_key(self) -> tuple:
        """Identifies an acquisition (or GUNW pair) independent of maturity/collection."""
        return (self.track, self.frame, self.start_time.replace(microsecond=0), self.secondary_date)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["start_time"] = self.start_time.isoformat()
        d["stop_time"] = self.stop_time.isoformat() if self.stop_time else None
        d["secondary_time"] = self.secondary_time.isoformat() if self.secondary_time else None
        d["secondary_date"] = self.secondary_date.isoformat() if self.secondary_date else None
        return d


# --------------------------------------------------------------------------- #
# AOI handling
# --------------------------------------------------------------------------- #
def parse_aoi(aoi: str | dict | Sequence[float] | BaseGeometry) -> BaseGeometry:
    """Accept WKT, GeoJSON (geometry/Feature/FeatureCollection), a
    ``(min_lon, min_lat, max_lon, max_lat)`` bbox, or a shapely geometry.

    Returns a valid shapely geometry in EPSG:4326.
    """
    if isinstance(aoi, BaseGeometry):
        geom = aoi
    elif isinstance(aoi, str):
        text = aoi.strip()
        if text.startswith("{"):
            return parse_aoi(json.loads(text))
        geom = shapely_wkt.loads(text)
    elif isinstance(aoi, dict):
        kind = aoi.get("type")
        if kind == "FeatureCollection":
            from shapely.ops import unary_union

            geom = unary_union([shape(f["geometry"]) for f in aoi["features"]])
        elif kind == "Feature":
            geom = shape(aoi["geometry"])
        else:
            geom = shape(aoi)
    elif isinstance(aoi, (list, tuple)) and len(aoi) == 4:
        min_lon, min_lat, max_lon, max_lat = map(float, aoi)
        if min_lon >= max_lon or min_lat >= max_lat:
            raise ValueError("bbox must be (min_lon, min_lat, max_lon, max_lat)")
        geom = box(min_lon, min_lat, max_lon, max_lat)
    else:
        raise TypeError(f"Unsupported AOI type: {type(aoi).__name__}")

    if geom.is_empty:
        raise ValueError("AOI geometry is empty")
    if not geom.is_valid:
        geom = make_valid(geom)
    minx, miny, maxx, maxy = geom.bounds
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise ValueError("AOI must be lon/lat (EPSG:4326); coordinates out of range")
    return geom


def _to_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# --------------------------------------------------------------------------- #
# Parsing asf_search products
# --------------------------------------------------------------------------- #
def maturity_from_collection(collection: str | None) -> str:
    """NISAR_L2_GCOV_BETA_V1 → BETA, ..._PROVISIONAL_V1 → PROVISIONAL,
    NISAR_L2_GCOV_V1 → VALIDATED."""
    if not collection:
        return "UNKNOWN"
    name = collection.upper()
    if "BETA" in name:
        return "BETA"
    if "PROVISIONAL" in name:
        return "PROVISIONAL"
    if re.search(r"_V\d+$", name):
        return "VALIDATED"
    return "UNKNOWN"


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def radar_mode_from_bandwidth(bandwidth: Any) -> str | None:
    """'20' → '20MHz', '40+5' → '40MHz' (main band only)."""
    raw = _first(bandwidth)
    if raw in (None, ""):
        return None
    main = str(raw).split("+")[0].strip()
    return f"{main}MHz" if main else None


def _polarizations(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    found: list[str] = []
    for item in items:
        for pol in _POL_RE.findall(str(item).upper()):
            if pol not in found:
                found.append(pol)
    return found


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _size_bytes(props: dict[str, Any]) -> int | None:
    sizes = props.get("bytes")
    if isinstance(sizes, dict):
        h5 = [v.get("bytes") for k, v in sizes.items() if str(k).endswith(".h5")]
        vals = [int(b) for b in (h5 or [v.get("bytes") for v in sizes.values()]) if b]
        return sum(vals) if vals else None
    if isinstance(sizes, (int, float)):
        return int(sizes)
    return None


_TS_RE = re.compile(r"(?<![0-9])(\d{8}T\d{6})(?![0-9])")


def secondary_time_from_name(name: str | None) -> datetime | None:
    """GUNW names carry 4 timestamps: ref start, ref stop, sec start, sec stop."""
    stamps = _TS_RE.findall(name or "")
    if len(stamps) < 4:
        return None
    return datetime.strptime(stamps[2], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def granule_from_product(product: Any, aoi: BaseGeometry | None = None) -> Granule:
    """Convert an ``asf_search.ASFProduct`` (or anything with ``.properties``
    and ``.geometry``) into a :class:`Granule`."""
    props: dict[str, Any] = product.properties
    geometry = getattr(product, "geometry", None)

    overlap = None
    if aoi is not None and geometry and geometry.get("coordinates"):
        try:
            footprint = shape(geometry)
            if aoi.area > 0:
                overlap = round(footprint.intersection(aoi).area / aoi.area, 4)
        except Exception:  # bad footprint — leave overlap unknown, don't fail search
            overlap = None

    collection = props.get("collectionName")
    start = _parse_time(props.get("startTime"))
    if start is None:
        raise ValueError(f"Product {props.get('sceneName')} has no startTime")

    return Granule(
        granule_id=props.get("sceneName") or props.get("fileID"),
        start_time=start,
        stop_time=_parse_time(props.get("stopTime")),
        track=props.get("pathNumber"),
        frame=props.get("frameNumber"),
        flight_direction=props.get("flightDirection"),
        polarizations=_polarizations(props.get("mainBandPolarization") or props.get("polarization")),
        range_bandwidth=_first(props.get("rangeBandwidth")),
        radar_mode=radar_mode_from_bandwidth(props.get("rangeBandwidth")),
        maturity=maturity_from_collection(collection),
        collection=collection,
        url=props.get("url"),
        s3_urls=list(props.get("s3Urls") or []),
        size_bytes=_size_bytes(props),
        frame_coverage=props.get("frameCoverage"),
        aoi_overlap=overlap,
        footprint=geometry if geometry and geometry.get("coordinates") else None,
        product=str(props.get("processingLevel") or "GCOV").upper(),
        secondary_time=(secondary_time_from_name(props.get("sceneName"))
                        if str(props.get("processingLevel") or "").upper() == "GUNW" else None),
    )


def dedupe_by_maturity(granules: Iterable[Granule]) -> list[Granule]:
    """Keep one copy per acquisition — the most mature one."""
    rank = {m: i for i, m in enumerate(config.MATURITY_ORDER)}
    best: dict[tuple, Granule] = {}
    for g in granules:
        current = best.get(g.acquisition_key)
        if current is None or rank.get(g.maturity, -1) > rank.get(current.maturity, -1):
            best[g.acquisition_key] = g
    return sorted(best.values(), key=lambda g: (g.start_time, g.granule_id))


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def search_granules(
    aoi: str | dict | Sequence[float] | BaseGeometry,
    start: str | date | datetime,
    end: str | date | datetime,
    *,
    maturity: str | Sequence[str] | None = None,
    radar_mode: str | None = None,
    min_overlap: float = 0.0,
    max_results: int | None = None,
    dedupe: bool = True,
    product: str = config.NISAR_PRODUCT,
    _search_fn=None,
) -> list[Granule]:
    """Find NISAR GCOV granules over ``aoi`` between ``start`` and ``end``.

    Args:
        aoi: WKT, GeoJSON, bbox or shapely geometry (EPSG:4326).
        start, end: ISO dates (inclusive).
        maturity: restrict to BETA / PROVISIONAL / VALIDATED (default: all).
        radar_mode: e.g. "20MHz" — keep only that main-band mode.
        min_overlap: drop granules covering less than this fraction of the AOI.
        max_results: cap on raw results from ASF.
        dedupe: keep only the most mature copy of each acquisition.

    Returns granules sorted by acquisition time. Metadata only.
    """
    geom = parse_aoi(aoi)
    start_d, end_d = _to_date(start), _to_date(end)
    if start_d > end_d:
        raise ValueError(f"start ({start_d}) is after end ({end_d})")

    if _search_fn is None:
        import asf_search as asf

        _search_fn = asf.search

    opts: dict[str, Any] = dict(
        dataset=config.NISAR_DATASET,
        processingLevel=product,
        intersectsWith=geom.wkt,
        start=f"{start_d.isoformat()}T00:00:00Z",
        end=f"{end_d.isoformat()}T23:59:59Z",
    )
    if maturity:
        opts["dataMaturity"] = [maturity] if isinstance(maturity, str) else list(maturity)
    if max_results:
        opts["maxResults"] = max_results

    results = _search_fn(**opts)

    granules = [granule_from_product(p, geom) for p in results]
    for g in granules:
        g.product = product.upper()
    if radar_mode:
        granules = [g for g in granules if g.radar_mode == radar_mode]
    if min_overlap > 0:
        granules = [g for g in granules if (g.aoi_overlap or 0) >= min_overlap]
    if dedupe:
        granules = dedupe_by_maturity(granules)
    else:
        granules.sort(key=lambda g: (g.start_time, g.granule_id))
    return granules


# --------------------------------------------------------------------------- #
# Coverage summary (feeds the API "coverage" block — Honest Uncertainty Rule)
# --------------------------------------------------------------------------- #
def _gap_overlaps(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    for g_start, g_end in config.KNOWN_DATA_GAPS:
        lo, hi = max(start, g_start), min(end, g_end)
        if lo <= hi:
            out.append((lo, hi))
    return out


def coverage_summary(
    granules: Sequence[Granule],
    start: str | date | datetime,
    end: str | date | datetime,
) -> dict[str, Any]:
    """Describe what is (and isn't) available for this AOI and time range."""
    start_d, end_d = _to_date(start), _to_date(end)
    dates = sorted({g.date for g in granules})

    observed_gaps: list[tuple[date, date]] = []
    edges = [start_d - timedelta(days=1), *dates, end_d + timedelta(days=1)]
    for a, b in zip(edges, edges[1:]):
        if (b - a).days > config.OBSERVED_GAP_THRESHOLD_DAYS:
            observed_gaps.append((a + timedelta(days=1), b - timedelta(days=1)))

    def counts(attr: str) -> dict[str, int]:
        c: dict[str, int] = {}
        for g in granules:
            key = str(getattr(g, attr))
            c[key] = c.get(key, 0) + 1
        return dict(sorted(c.items()))

    tracks = sorted({(g.track, g.flight_direction) for g in granules}, key=str)
    overlaps = [g.aoi_overlap for g in granules if g.aoi_overlap is not None]

    notes: list[str] = []
    if not granules:
        notes.append("No NISAR GCOV acquisitions found for this AOI and time range.")
    if len(counts("radar_mode")) > 1:
        notes.append(
            "Multiple radar modes present; 40 MHz reads ~1 dB brighter than 20 MHz on "
            "dark surfaces. Compare dates within one mode, or calibrate per mode."
        )
    if len(tracks) > 1:
        notes.append(
            "Multiple tracks/look geometries present; change detection should compare "
            "acquisitions from the same track."
        )
    if any(m in ("BETA", "PROVISIONAL") for m in counts("maturity")):
        notes.append("Some products are BETA/PROVISIONAL and not fully calibrated.")
    if overlaps and min(overlaps) < 1.0:
        notes.append("Some acquisitions cover only part of the AOI (see aoi_overlap).")

    return {
        "granules_found": len(granules),
        "date_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "requested_range": [start_d.isoformat(), end_d.isoformat()],
        "dates": [d.isoformat() for d in dates],
        "known_gaps": [[a.isoformat(), b.isoformat()] for a, b in _gap_overlaps(start_d, end_d)],
        "observed_gaps": [[a.isoformat(), b.isoformat()] for a, b in observed_gaps],
        "by_maturity": counts("maturity"),
        "by_radar_mode": counts("radar_mode"),
        "tracks": [{"track": t, "flight_direction": d} for t, d in tracks],
        "min_aoi_overlap": min(overlaps) if overlaps else None,
        "notes": notes,
    }


def granules_to_geojson(granules: Sequence[Granule]) -> dict[str, Any]:
    """FeatureCollection of footprints — handy for checking in QGIS."""
    features = []
    for g in granules:
        props = g.to_dict()
        geom = props.pop("footprint")
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


__all__ = [
    "Granule",
    "parse_aoi",
    "search_granules",
    "coverage_summary",
    "granules_to_geojson",
    "dedupe_by_maturity",
    "maturity_from_collection",
    "radar_mode_from_bandwidth",
]

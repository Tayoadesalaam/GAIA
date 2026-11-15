"""Automatic reference areas (JRC GSW) and dry-only calibration — offline."""

import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely import wkt as shapely_wkt

from gaia import config, flags, pipeline
from gaia.process import calibrate_auto, calibrate_dry_only
from gaia.references import auto_references, derive_references, tile_names

# 0.00025° grid (as GSW) over a small box near Lagos
T = from_origin(3.40, 6.55, 0.00025, 0.00025)


@pytest.mark.parametrize("bounds,expected", [
    ((3.10, 6.38, 3.70, 6.70), ["0E_10N"]),
    ((57.87, 40.69, 58.11, 40.87), ["50E_50N"]),
    ((-0.2, 51.4, 0.1, 51.6), ["10W_60N", "0E_60N"]),
    ((9.9, 9.9, 10.1, 10.1), ["0E_10N", "0E_20N", "10E_10N", "10E_20N"]),
    ((-58.5, -34.7, -58.3, -34.5), ["60W_30S"]),
])
def test_tile_names(bounds, expected):
    assert sorted(tile_names(*bounds)) == sorted(expected)


def lake_occurrence():
    occ = np.zeros((400, 400), dtype="uint8")
    occ[100:200, 100:200] = 100  # permanent lake
    occ[95:100, 100:200] = 40  # seasonal shore
    occ[300:320, :] = 255  # no data stripe
    return occ


def test_derive_references_lake_and_land():
    r = derive_references(lake_occurrence(), T)
    assert r["water_ref"] and r["dry_ref"] and r["source"] == "jrc_gsw"
    water = shapely_wkt.loads(r["water_ref"])
    # eroded 3 px inside the lake, never touching the shore
    x0, y0, x1, y1 = water.bounds
    assert x0 >= 3.40 + 103 * 0.00025 - 1e-9 and x1 <= 3.40 + 197 * 0.00025 + 1e-9
    dry = shapely_wkt.loads(r["dry_ref"])
    assert not dry.intersects(water)
    assert 7.0 < r["permanent_water_km2"] < 8.5 and not r["notes"]  # 100 px × 27.8 m = 2.78 km side


def test_derive_references_desert_has_no_water_ref():
    r = derive_references(np.zeros((400, 400), dtype="uint8"), T)
    assert r["water_ref"] is None and r["dry_ref"]
    assert any("No permanent water" in n for n in r["notes"])


def test_auto_references_never_raises():
    def broken(geom):
        raise OSError("HTTP 503")

    r = auto_references("POINT(3.4 6.5)", reader=broken)
    assert r["source"] == "unavailable" and "503" in r["error"] and r["water_ref"] is None


def test_auto_references_with_reader():
    r = auto_references("POLYGON((3.40 6.45, 3.50 6.45, 3.50 6.55, 3.40 6.55, 3.40 6.45))",
                        reader=lambda g: (lake_occurrence(), T))
    assert r["water_ref"] and r["dry_ref"]


# --- dry-only calibration ---------------------------------------------------- #
def test_dry_only_on_normal_land_is_detectable():
    rng = np.random.default_rng(0)
    land = np.c_[rng.normal(-8, 2, 5000), rng.normal(-15, 2, 5000)]
    cal = calibrate_dry_only(land, radar_mode="20MHz")
    assert cal.method == "dry_only_calibration" and cal.hit_rate is None
    assert cal.water_detectable is True and cal.false_alarm_rate <= config.DRY_ONLY_MAX_FALSE_ALARM
    # never looser than the fixed defaults
    assert cal.threshold_hh_db <= config.DEFAULT_WATER_THRESHOLD_HH_DB
    assert cal.threshold_hv_db <= config.DEFAULT_WATER_THRESHOLD_HV_DB


def test_dry_only_on_sand_is_not_detectable():
    rng = np.random.default_rng(0)
    sand = np.c_[rng.normal(-20.8, 1.2, 5000), rng.normal(-24.0, 1.0, 5000)]  # Karakum, live values
    cal = calibrate_dry_only(sand, radar_mode="20MHz")
    assert cal.water_detectable is False
    assert "as dark as open water" in cal.note
    level, why = flags.calibration_issues([cal.to_dict()])[0]
    assert level == "low" and "dry sand" in why
    assert flags.water_detectable([cal.to_dict()]) is False


def test_dry_only_rejects_smoothed_sand_at_2pct():
    # 30 m flood stack: sand spread narrows; a pair at ~2% FA would pass the old budget
    rng = np.random.default_rng(3)
    sand = np.c_[rng.normal(-20.8, 0.8, 20000), rng.normal(-24.0, 0.6, 20000)]
    assert calibrate_dry_only(sand).water_detectable is False


def test_calibrate_auto_picks_method():
    rng = np.random.default_rng(1)
    w = np.c_[rng.normal(-20, 1, 2000), rng.normal(-27, 1, 2000)]
    d = np.c_[rng.normal(-6, 1, 2000), rng.normal(-13, 1, 2000)]
    assert calibrate_auto(w, d, "40MHz").method == "per_date_classification"
    assert calibrate_auto(None, d, "40MHz").method == "dry_only_calibration"
    assert calibrate_auto(None, None, "40MHz").method == "default_uncalibrated"
    assert calibrate_auto(w[:0], d[:0], "40MHz").method == "default_uncalibrated"


def test_dry_only_confidence_is_medium_when_detectable():
    rng = np.random.default_rng(0)
    cal = calibrate_dry_only(np.c_[rng.normal(-8, 2, 5000), rng.normal(-15, 2, 5000)]).to_dict()
    level, why = flags.calibration_issues([cal])[0]
    assert level == "medium" and "hit rate unknown" in why


# --- precedence ---------------------------------------------------------------- #
def test_resolve_references_precedence(monkeypatch):
    import gaia.references as refs

    monkeypatch.setattr(refs, "auto_references", lambda geom: {
        "source": "jrc_gsw", "water_ref": "POLYGON((0 0,1 0,1 1,0 0))", "dry_ref": None,
        "permanent_water_km2": 1.0, "notes": ["n"]})
    geom = pipeline.parse_aoi("POINT(3.4 6.5)").buffer(0.1)
    w, d, s = pipeline.resolve_references(geom, "POLYGON((0 0,2 0,2 2,0 0))", None, from_site=False)
    assert s["source"] == "user" and w.startswith("POLYGON((0 0,2")
    w, d, s = pipeline.resolve_references(geom, "POLYGON((0 0,2 0,2 2,0 0))", None, from_site=True)
    assert s["source"] == "site"
    w, d, s = pipeline.resolve_references(geom, None, None, from_site=False)
    assert s["source"] == "jrc_gsw" and s["water_ref"] is True and "POLYGON" not in str(s)
    w, d, s = pipeline.resolve_references(geom, None, None, from_site=False, auto=False)
    assert s["source"] == "none" and w is None

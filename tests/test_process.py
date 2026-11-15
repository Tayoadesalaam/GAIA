"""Processing tests — synthetic scenes with known water, land and change."""

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from gaia import config
from gaia.process import (
    boxcar_db,
    calibrate_thresholds,
    change_map,
    detect_water_change,
    remove_small_patches,
)

# Synthetic 60x60 scene, 100 m pixels, UTM 31N: x 500–506 km, y 700–706 km.
T = from_origin(500_000, 706_000, 100, 100)


def lonlat_box(c0, r0, c1, r1):
    """Pixel window → EPSG:4326 WKT box."""
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:32631", "EPSG:4326", always_xy=True)
    x0, y0 = 500_000 + c0 * 100, 706_000 - r1 * 100
    x1, y1 = 500_000 + c1 * 100, 706_000 - r0 * 100
    (lo0, la0), (lo1, la1) = tr.transform(x0, y0), tr.transform(x1, y1)
    return f"POLYGON(({lo0} {la0}, {lo1} {la0}, {lo1} {la1}, {lo0} {la1}, {lo0} {la0}))"


def scene(tmp, name, water_cols, rng):
    """Water (HH -20, HV -27 dB) in the given columns, land (HH -5, HV -12) elsewhere."""
    hh = np.full((60, 60), -5.0)
    hv = np.full((60, 60), -12.0)
    hh[:, water_cols] = -20.0
    hv[:, water_cols] = -27.0
    paths = {}
    for pol, db in (("HH", hh), ("HV", hv)):
        p = tmp / f"{name}_{pol}.tif"
        power = (10 ** ((db + rng.normal(0, 1.0, db.shape)) / 10)).astype("float32")
        with rasterio.open(p, "w", driver="GTiff", width=60, height=60, count=1, dtype="float32",
                           crs="EPSG:32631", transform=T, nodata=np.nan) as dst:
            dst.write(power, 1)
        paths[pol] = str(p)
    return {"date": name, "granule_id": f"G_{name}", "paths": paths,
            "granule": {"radar_mode": "40MHz", "maturity": "VALIDATED", "track": 152}}


def test_boxcar_db_handles_nan():
    p = np.full((5, 5), 0.01, dtype="float32")
    p[2, 2] = np.nan
    db = boxcar_db(p, 3)
    assert np.isnan(db[2, 2]) and db[0, 0] == pytest.approx(-20.0, abs=1e-4)


def test_calibration_separates_classes():
    rng = np.random.default_rng(1)
    water = np.c_[rng.normal(-20, 1.5, 5000), rng.normal(-27, 1.5, 5000)]
    dry = np.c_[rng.normal(-5, 2, 5000), rng.normal(-12, 2, 5000)]
    cal = calibrate_thresholds(water, dry, radar_mode="40MHz")
    assert cal.hit_rate > 0.95 and cal.false_alarm_rate <= 0.02
    assert -20 < cal.threshold_hh_db < -5 and -27 < cal.threshold_hv_db < -12
    assert cal.method == "per_date_classification"


def test_calibration_requires_pixels():
    with pytest.raises(ValueError):
        calibrate_thresholds(np.empty((0, 2)), np.ones((5, 2)))


def test_remove_small_patches():
    m = np.zeros((10, 10), bool)
    m[0, 0] = True
    m[5:8, 5:8] = True
    out = remove_small_patches(m, 9)
    assert not out[0, 0] and out[5:8, 5:8].all()


def test_change_map_classes():
    w1 = np.zeros((10, 10), bool); w1[:, :3] = True
    w2 = np.zeros((10, 10), bool); w2[:, :6] = True
    valid = np.ones((10, 10), bool); valid[0] = False
    c = change_map(w1, w2, valid, min_patch_px=1)
    assert (c[1:, :3] == 0).all() and (c[1:, 3:6] == 1).all() and (c[0] == 255).all()
    c2 = change_map(w2, w1, valid, min_patch_px=1)
    assert (c2[1:, 3:6] == 2).all()


def test_detect_water_change_end_to_end(tmp_path):
    rng = np.random.default_rng(0)
    before = scene(tmp_path, "2026-01-01", slice(0, 20), rng)
    after = scene(tmp_path, "2026-01-13", slice(0, 30), rng)  # cols 20–29 flooded
    aoi = lonlat_box(0, 0, 60, 60)
    r = detect_water_change(
        before, after, aoi, tmp_path / "out" / "change.tif",
        water_ref=lonlat_box(2, 2, 15, 58), dry_ref=lonlat_box(40, 2, 58, 58),
        exclude=lonlat_box(50, 50, 60, 60), speckle_window=3, quicklook=False,
    )
    cal = r["calibrations"][0]
    assert cal["method"] == "per_date_classification" and cal["hit_rate"] > 0.9
    assert cal["false_alarm_rate"] <= 0.02
    # 10 cols x 60 rows x 0.01 km² ≈ 6 km² new water (boundary pixels blur a little)
    assert 4.5 < r["stats"]["new_water_km2"] < 7.5
    assert r["stats"]["receded_water_km2"] < 0.5
    assert r["stats"]["excluded_km2"] > 0
    assert r["stats"]["water_ref_change_fraction"] < 0.05
    assert all(d["water_ref_fraction"] > 0.9 for d in r["per_date"])

    with rasterio.open(r["change_map_path"]) as src:
        assert src.dtypes[0] == "uint8" and src.nodata == 255
        assert json.loads(src.tags()["gaia_calibration"])[0]["radar_mode"] == "40MHz"
        data = src.read(1)
        assert (data[5:55, 22:28] == 1).mean() > 0.95
        assert src.colormap(1)[1][:3] == (30, 120, 255)


def test_detect_without_refs_uses_defaults(tmp_path):
    rng = np.random.default_rng(0)
    before = scene(tmp_path, "2026-01-01", slice(0, 20), rng)
    after = scene(tmp_path, "2026-01-13", slice(0, 20), rng)
    r = detect_water_change(before, after, lonlat_box(0, 0, 60, 60), tmp_path / "c.tif",
                            quicklook=False)
    cal = r["calibrations"][0]
    assert cal["method"] == "default_uncalibrated"
    assert cal["threshold_hh_db"] == config.DEFAULT_WATER_THRESHOLD_HH_DB
    assert r["per_date"][0]["water_ref_fraction"] is None

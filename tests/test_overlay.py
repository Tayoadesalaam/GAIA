"""Map overlays (output GeoTIFF → Web-Mercator PNG) — offline."""

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_origin

from gaia import overlay


def _tif(path, data, nodata):
    # 1 km square in UTM 31N near Lagos, 10 m pixels
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                       dtype=data.dtype, crs="EPSG:32631", nodata=nodata,
                       transform=from_origin(543000, 724000, 10, 10)) as d:
        d.write(data, 1)
    return path


def test_categorical_overlay(tmp_path):
    a = np.zeros((100, 100), "uint8")
    a[10:20, 10:20] = 1
    a[50:60, 50:60] = 2
    a[90:, :] = 255
    info = overlay.render_overlay(_tif(tmp_path / "wc.tif", a, 255), "change_map")
    (s, w), (n, e) = info["bounds"]
    assert 6.5 < s < n < 6.6 and 3.3 < w < e < 3.5
    img = np.asarray(Image.open(info["image"]))
    assert img.shape[2] == 4
    blue = (img[..., 2] > 200) & (img[..., 3] > 0)
    assert 60 < blue.sum() < 140  # ~100 px of new water survives reprojection
    assert (img[..., 3] == 0).mean() > 0.9  # unchanged + nodata are transparent
    assert [i["label"] for i in info["legend"]["items"]] == ["New water", "Receded water"]


def test_velocity_overlay_is_symmetric_and_cached(tmp_path):
    v = np.linspace(-30, 30, 10000, dtype="float32").reshape(100, 100)
    v[:5] = np.nan
    src = _tif(tmp_path / "vel.tif", v, np.nan)
    info = overlay.render_overlay(src, "velocity_mm_yr")
    assert info["legend"]["min"] == -info["legend"]["max"]
    again = overlay.render_overlay(src, "velocity_mm_yr")
    assert again["bounds"] == info["bounds"]


def test_unknown_style_or_missing_file(tmp_path):
    with pytest.raises(overlay.OverlayError):
        overlay.render_overlay(tmp_path / "x.tif", "change_map")
    with pytest.raises(overlay.OverlayError):
        overlay.render_overlay(tmp_path / "x.tif", "nope")

"""Flood, vegetation and deformation engines — synthetic data with known answers."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from gaia import config
from gaia.deformation import correct_ifg, fit_velocity, longest_chain, run_deformation, select_chain
from gaia.discovery import granule_from_product, secondary_time_from_name
from gaia.flood import run_flood
from gaia.timeseries import Stack, select_track
from gaia.vegetation import FULL_SEASON, GROWING_AT_END, NO_SEASON, phenology, run_vegetation
from tests.test_discovery import FakeProduct
from tests.test_process import lonlat_box

T = from_origin(500_000, 706_000, 100, 100)  # same synthetic grid as test_process
AOI = lonlat_box(0, 0, 60, 60)


def db2p(x):
    return (10 ** (np.asarray(x, dtype="float64") / 10)).astype("float32")


def make_stack(hh_db, hv_db, start="2026-06-18", step=12, maturity="VALIDATED"):
    n = hh_db.shape[0]
    dates = [(date.fromisoformat(start) + timedelta(days=step * i)).isoformat() for i in range(n)]
    meta = [{"date": d, "granule_id": f"G{i}", "track": 58, "radar_mode": "20MHz", "maturity": maturity}
            for i, d in enumerate(dates)]
    return Stack(dates=dates, hh=db2p(hh_db), hv=db2p(hv_db), transform=T, crs=rasterio.crs.CRS.from_epsg(32631),
                 meta=meta)


def no_rain(dates, affected=()):
    return {d: {"rain_affected": d in affected, "rain_mm": 30 if d in affected else 1,
                "window": [d, d], "error": None} for d in dates}


# --------------------------------------------------------------------------- #
# Discovery: GUNW pairs
# --------------------------------------------------------------------------- #
GUNW = "NISAR_L2_PR_GUNW_032_015_A_005_033_4000_SH_20261001T052245_20261001T052319_20261013T052245_20261013T052319_P05023_N_F_J_001"


def test_gunw_secondary_time():
    assert secondary_time_from_name(GUNW).date() == date(2026, 10, 13)
    assert secondary_time_from_name("NISAR_L2_PR_GCOV_x_20261001T052245_20261001T052319_P1") is None


def gunw(ref, sec, track=15, maturity="NISAR_L2_GUNW_PROVISIONAL_V1"):
    name = f"NISAR_L2_PR_GUNW_1_{track:03d}_A_005_2_4000_SH_{ref}T052245_{ref}T052319_{sec}T052245_{sec}T052319_P1_N_F_J_001"
    p = FakeProduct(name, f"{ref[:4]}-{ref[4:6]}-{ref[6:]}T05:22:45Z", collection=maturity, track=track)
    p.properties["processingLevel"] = "GUNW"
    return granule_from_product(p)


def test_gunw_pairs_with_same_reference_are_distinct():
    a, b = gunw("20260901", "20260913"), gunw("20260901", "20260925")
    assert a.acquisition_key != b.acquisition_key
    assert a.secondary_date == date(2026, 9, 13)


def test_longest_chain_and_track_choice():
    t15 = [gunw("20260627", "20260709"), gunw("20260709", "20260721"), gunw("20260721", "20260814"),
           gunw("20260709", "20260802")]  # branch that goes nowhere
    t152 = [gunw("20260624", "20260706", 152), gunw("20260801", "20260813", 152)]  # broken chain
    chain = longest_chain(t15)
    assert [(g.date.isoformat(), g.secondary_date.isoformat()) for g in chain] == [
        ("2026-06-27", "2026-07-09"), ("2026-07-09", "2026-07-21"), ("2026-07-21", "2026-08-14")]
    track, ch = select_chain(t15 + t152)
    assert track == 15 and len(ch) == 3


def test_select_track_skips_single_pol():
    dual = granule_from_product(FakeProduct("D", "2026-06-18T04:58:57Z", track=58))
    single = granule_from_product(FakeProduct("S", "2026-06-30T04:58:57Z", track=58, pols=("HH",)))
    track, picked = select_track([dual, single])
    assert track == 58 and [g.granule_id for g in picked] == ["D"]


# --------------------------------------------------------------------------- #
# Vegetation
# --------------------------------------------------------------------------- #
def test_phenology_classes_and_timing():
    days = np.arange(0, 12 * 11, 12, dtype=float)  # 11 passes
    full = np.array([-20, -20, -19, -16, -13, -12, -12, -14, -17, -20, -20], float)
    growing = np.array([-20, -20, -20, -19, -17, -15, -13, -12, -12, -11.5, -11], float)
    flat = np.full(11, -15.0) + np.array([0, .5, -.5, 0, .4, -.3, .2, 0, -.2, .1, 0])
    ph = phenology(np.stack([full, growing, flat], axis=1), days)
    assert list(ph["class"]) == [FULL_SEASON, GROWING_AT_END, NO_SEASON]
    # full: min -20 → peak -12, 50% level -16 reached exactly at pass 3 (day 36)
    assert ph["sos_day"][0] == pytest.approx(36.0)
    assert ph["peak_day"][0] == 60.0
    # decline to -20: level -16 crossed between day 84 (-14) and 96 (-17) → 84 + 2/3*12 = 92
    assert ph["eos_day"][0] == pytest.approx(92.0)
    assert np.isnan(ph["eos_day"][1]) and np.isnan(ph["sos_day"][2])


def test_run_vegetation_end_to_end(tmp_path):
    curve = np.array([-20, -20, -19, -16, -13, -12, -12, -14, -17, -20, -20], float)
    n = len(curve)
    hv = np.full((n, 60, 60), -20.0)
    hv[:, :, :30] = curve[:, None, None]  # left half is cropland with a season
    hh = hv + 7
    stack = make_stack(hh, hv)
    rain = no_rain(stack.dates, affected=(stack.dates[2],))  # one rainy pass excluded
    r = run_vegetation(stack, rain, AOI, tmp_path / "veg")
    se = r["season"]
    assert 0.4 < r["class_fraction"]["full_season_observed"] < 0.6
    assert se["peak"] == "2026-08-17"  # 60 days after 2026-06-18
    assert date.fromisoformat(se["season_start"]) < date.fromisoformat(se["peak"]) \
        < date.fromisoformat(se["season_end"])
    a, b = se["estimated_planting_window"]
    assert (date.fromisoformat(se["season_start"]) - date.fromisoformat(a)).days == 35
    assert r["flags"]["rain_affected_dates"] == [stack.dates[2]]
    assert stack.dates[2] not in r["dates"]
    for key in ("season_class", "season_start_doy", "peak_doy", "season_end_doy", "amplitude_db"):
        assert Path(r["outputs"][key]).exists()
    with rasterio.open(r["outputs"]["peak_doy"]) as src:
        assert src.read(1)[30, 10] == date(2026, 8, 17).timetuple().tm_yday
    assert r["confidence"] == "high"


def test_vegetation_season_end_needs_most_of_active_area(tmp_path):
    # 10% of the active area finishes early; 90% is still growing at the last pass.
    early = np.array([-20, -14, -12, -15, -20, -20, -20, -20, -20, -20, -20], float)
    late = np.array([-20, -20, -20, -20, -20, -19, -17, -15, -13, -12, -11], float)
    hv = np.full((len(early), 60, 60), -20.0)
    hv[:, :, :3] = early[:, None, None]
    hv[:, :, 3:30] = late[:, None, None]
    stack = make_stack(hv + 7, hv)
    se = run_vegetation(stack, no_rain(stack.dates), AOI, tmp_path / "veg")["season"]
    assert se["season_end"] is None and "only 10%" in se["season_end_note"]
    assert se["season_start"] and se["peak"]


def test_vegetation_needs_rain_free_passes(tmp_path):
    stack = make_stack(np.full((3, 60, 60), -10.0), np.full((3, 60, 60), -17.0))
    with pytest.raises(ValueError):
        run_vegetation(stack, no_rain(stack.dates, affected=stack.dates[:1]), AOI, tmp_path / "v")


# --------------------------------------------------------------------------- #
# Flood
# --------------------------------------------------------------------------- #
def flood_scene(n=6, flood_on=(3,), rainy=(), big_rainy_flood=False):
    hh = np.full((n, 60, 60), -6.0)
    hv = np.full((n, 60, 60), -13.0)
    hh[:, :, :10], hv[:, :, :10] = -20.0, -27.0  # lake (normal water)
    for i in flood_on:
        hh[i, :, 10:20], hv[i, :, 10:20] = -20.0, -27.0  # flooded field
    if big_rainy_flood:
        for i in rainy:
            hh[i, :, 10:40], hv[i, :, 10:40] = -20.0, -27.0
    return hh, hv


def test_run_flood_peak_and_exclusions(tmp_path):
    hh, hv = flood_scene(flood_on=(3,), rainy=(4,), big_rainy_flood=True)
    stack = make_stack(hh, hv)
    rain = no_rain(stack.dates, affected=(stack.dates[4],))
    r = run_flood(stack, rain, AOI, tmp_path / "flood",
                  water_ref=lonlat_box(1, 1, 9, 59), dry_ref=lonlat_box(45, 1, 59, 59))
    s = r["stats"]
    assert s["peak_date"] == stack.dates[3]
    assert 5.0 < s["peak_flood_open_km2"] < 7.0  # 10 cols x 60 rows x 0.01 km²
    assert 5.0 < s["normal_water_km2"] < 7.0
    assert s["excluded_dates"] == [stack.dates[4]]
    assert s["flagged_passes_with_larger_flood"] == [stack.dates[4]]
    assert r["confidence"] == "medium"
    assert any("larger flood" in x for x in r["confidence_reasons"])
    with rasterio.open(r["outputs"]["peak_flood_map"]) as src:
        m = src.read(1)
        assert m[30, 5] == 1 and m[30, 15] == 2 and m[30, 50] == 0


def test_run_flood_wind_flag(tmp_path):
    hh, hv = flood_scene(flood_on=())
    hh[2, :, :10], hv[2, :, :10] = -8.0, -15.0  # lake roughened by wind on pass 2
    stack = make_stack(hh, hv)
    r = run_flood(stack, no_rain(stack.dates), AOI, tmp_path / "f",
                  water_ref=lonlat_box(1, 1, 9, 59), dry_ref=lonlat_box(45, 1, 59, 59))
    assert r["per_date"][2]["wind_affected"] is True
    assert stack.dates[2] in r["stats"]["excluded_dates"]


def test_flooded_vegetation(tmp_path):
    n = 6
    hh = np.full((n, 60, 60), -9.0)
    hv = np.full((n, 60, 60), -15.0)  # vegetated
    hh[:, :, :10], hv[:, :, :10] = -20.0, -27.0
    hh[3, :, 20:30] = -4.0  # +5 dB double bounce
    stack = make_stack(hh, hv)
    r = run_flood(stack, no_rain(stack.dates), AOI, tmp_path / "fv",
                  water_ref=lonlat_box(1, 1, 9, 59), dry_ref=lonlat_box(45, 1, 59, 59))
    assert r["stats"]["peak_date"] == stack.dates[3]
    assert r["stats"]["peak_flood_vegetation_km2"] > 5


# --------------------------------------------------------------------------- #
# Deformation
# --------------------------------------------------------------------------- #
def test_fit_velocity_recovers_rate():
    days = np.array([0, 12, 24, 36, 48, 60], float)
    rate = 36.525  # mm/yr
    cum = (days * rate / 365.25)[:, None, None] * np.ones((1, 2, 2))
    cum[:, 1, 1] = np.nan
    vel, se = fit_velocity(cum, days)
    assert vel[0, 0] == pytest.approx(rate) and se[0, 0] == pytest.approx(0, abs=1e-6)
    assert np.isnan(vel[1, 1])


def test_correct_ifg_iono_mask_reference():
    lam_to_mm = config.NISAR_L_WAVELENGTH_M / (4 * np.pi) * 1000
    shape = (20, 20)
    iono = np.linspace(0, 10, 400).reshape(shape)
    signal = np.zeros(shape)
    signal[10:, :] = 2.0  # 2 rad of real motion in the south half
    phase = signal + iono + 5.0  # + constant offset
    coh = np.full(shape, 0.8)
    coh[0, 0] = 0.1
    cc = np.ones(shape)
    cc[19, 19] = 2  # small secondary component
    ref = np.zeros(shape, bool)
    ref[:5, :] = True  # stable north
    d, diag = correct_ifg(phase, coh, cc, iono, ref, np.ones(shape, bool), deramp=False)
    assert diag["iono_applied"] and diag["reference"] == "stable_ref_median"
    assert np.isnan(d[0, 0]) and np.isnan(d[19, 19])
    assert np.nanmedian(d[:5]) == pytest.approx(0, abs=1e-9)
    assert np.nanmedian(d[10:]) == pytest.approx(2.0 * lam_to_mm)


def test_deramp_removes_ramp_keeps_bowl():
    from gaia.deformation import remove_plane

    rr, cc = np.mgrid[0:60, 0:60]
    ramp = 0.05 * rr + 0.02 * cc
    bowl = np.zeros((60, 60))
    bowl[25:35, 25:35] = 3.0
    out, ptp = remove_plane(ramp + bowl, np.ones((60, 60), bool))
    assert ptp > 3
    out -= np.median(out)
    assert np.median(out[25:35, 25:35]) == pytest.approx(3.0, abs=0.15)
    assert np.abs(np.percentile(out[np.r_[0:20, 40:60]], [5, 95])).max() < 0.15


def _write(path, arr, dtype="float32", nodata=None):
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=dtype, crs="EPSG:32631", transform=T, nodata=nodata) as dst:
        dst.write(arr.astype(dtype), 1)


def test_run_deformation_end_to_end(tmp_path):
    lam_to_mm = config.NISAR_L_WAVELENGTH_M / (4 * np.pi) * 1000
    rate_mm_yr = 50.0
    pairs = [gunw(a, b, track=152) for a, b in (("20260624", "20260706"), ("20260706", "20260718"),
                                                 ("20260718", "20260730"), ("20260730", "20260811"),
                                                 ("20260811", "20260823"))]

    def fake_search(geom, start, end, product):
        assert product == "GUNW"
        return pairs

    def fake_ingest(g, geom):
        dt = (g.secondary_date - g.date).days
        phase = np.zeros((60, 60))
        rr, cc = np.mgrid[0:60, 0:60]
        phase[35:50, 35:50] = rate_mm_yr * dt / 365.25 / lam_to_mm  # local subsidence bowl
        phase += 0.02 * rr * dt / 12  # atmospheric ramp, removed by deramping
        iono = np.random.default_rng(g.date.toordinal()).normal(0, 3, (60, 60))
        base = tmp_path / g.granule_id[-30:]
        paths = {}
        for tok, arr, dtype in (("unwphase", phase + iono, "float32"), ("coh", np.full((60, 60), 0.7), "float32"),
                                ("concomp", np.ones((60, 60)), "uint16"), ("iono", iono, "float32")):
            paths[tok] = f"{base}_{tok}.tif"
            _write(paths[tok], arr, dtype)
        return {"granule_id": g.granule_id, "reference_date": g.date.isoformat(),
                "secondary_date": g.secondary_date.isoformat(), "paths": paths,
                "granule": {"maturity": g.maturity, "radar_mode": g.radar_mode}}

    r = run_deformation(AOI, "2026-06-01", "2026-09-01", tmp_path / "defo",
                        stable_ref=lonlat_box(2, 2, 25, 58), search_fn=fake_search, ingest_fn=fake_ingest)
    assert r["track"] == 152 and len(r["dates"]) == 6
    with rasterio.open(r["outputs"]["velocity_mm_yr"]) as src:
        vel = src.read(1)
    assert np.nanmedian(vel[37:48, 37:48]) == pytest.approx(rate_mm_yr, rel=0.1)
    assert abs(np.nanmedian(vel[:, :25])) < 3
    s = r["stats"]
    assert s["reference"] == "stable_ref_median" and 1.5 < s["away_from_satellite_km2"] < 3.5
    assert r["method"]["deramped"] is True
    assert r["confidence"] == "low"  # 60-day span
    assert any("Tropospheric" in x for x in r["confidence_reasons"])
    assert r["method"]["ionosphere_corrected"] is True

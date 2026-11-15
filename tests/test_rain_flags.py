"""Rain (GPM IMERG) and uncertainty-flag tests — offline."""

from datetime import date

import pytest

from gaia import config, flags
from gaia.rain import build_url, cell_index_range, parse_ascii, rain_flag

LAGOS = config.get_site("lagos").aoi_wkt

ASCII = """Dataset: 3B-DAY-L.MS.MRG.3IMERG.20261019-S000000-E235959.V07C.nc4
precipitation.lat, 6.35, 6.45
precipitation.precipitation[precipitation.time=17088][precipitation.lon=3.15], {a}, {b}
precipitation.precipitation[precipitation.time=17088][precipitation.lon=3.25], {c}, -9999.9
"""


def fake_fetch(mm_per_day, status=200):
    calls = []

    def _fetch(url):
        calls.append(url)
        if status != 200:
            return status, ""
        return 200, ASCII.format(a=mm_per_day, b=mm_per_day, c=mm_per_day)

    _fetch.calls = calls
    return _fetch


# --- rain ------------------------------------------------------------------ #
def test_cell_index_range_lagos():
    assert cell_index_range(3.10, 3.70, -180.0, 3600) == (1831, 1836)
    assert cell_index_range(6.38, 6.70, -90.0, 1800) == (963, 966)


def test_build_url():
    u = build_url(date(2026, 10, 19), "V07C", (1831, 1836), (963, 966))
    assert "/2026/10/3B-DAY-L.MS.MRG.3IMERG.20261019-S000000-E235959.V07C.nc4.ascii?" in u
    assert u.endswith("precipitation[0:0][1831:1836][963:966]")


def test_parse_ascii_masks_fill():
    arr = parse_ascii(ASCII.format(a=1, b=2, c=3))
    assert arr.shape == (2, 2) and abs(float(arr[~(arr != arr)].sum()) - 6) < 1e-6


@pytest.mark.parametrize("mm,expected", [(6.0, True), (4.0, False)])
def test_rain_flag_threshold(tmp_path, mm, expected):
    fetch = fake_fetch(mm)
    f = rain_flag("2026-10-22", LAGOS, fetch=fetch, cache_dir=tmp_path)
    assert f["window"] == ["2026-10-19", "2026-10-21"]
    assert len(fetch.calls) == 3
    assert f["rain_mm"] == pytest.approx(3 * mm)
    assert f["rain_affected"] is expected


def test_rain_flag_unknown_on_error(tmp_path):
    f = rain_flag("2026-10-22", LAGOS, fetch=fake_fetch(0, status=403), cache_dir=tmp_path)
    assert f["rain_affected"] is None and "GESDISC" in f["error"]


def test_rain_flag_tries_other_versions(tmp_path):
    seen = []

    def fetch(url):
        seen.append(url)
        return (200, ASCII.format(a=1, b=1, c=1)) if "V07B" in url else (404, "")

    f = rain_flag("2026-10-22", LAGOS, fetch=fetch, cache_dir=tmp_path)
    assert f["daily"][0]["version"] == "V07B" and f["rain_affected"] is False


# --- flags ----------------------------------------------------------------- #
def day(date_, *, rain=False, ref=0.9, maturity="VALIDATED", mode="40MHz"):
    return {"date": date_, "maturity": maturity, "radar_mode": mode, "water_ref_fraction": ref,
            "rain_affected": rain, "rain_mm_3day": 20 if rain else 1,
            "rain_window": ["a", "b"], "rain_error": None,
            "wind_affected": flags.wind_affected(ref)}


GOOD_CAL = [{"method": "per_date_classification", "radar_mode": "40MHz",
             "hit_rate": 0.92, "false_alarm_rate": 0.002, "note": None}]
GOOD_STATS = {"aoi_valid_fraction": 1.0, "water_ref_change_fraction": 0.01}


def test_wind_flag():
    assert flags.wind_affected(0.13) is True
    assert flags.wind_affected(0.9) is False
    assert flags.wind_affected(None) is None


def test_maturity_and_mode_labels():
    assert flags.least_mature(["VALIDATED", "PROVISIONAL"]) == "PROVISIONAL"
    assert flags.least_mature([None]) == "UNKNOWN"
    assert flags.radar_mode_label(["40MHz", "40MHz"]) == "40MHz"
    assert flags.radar_mode_label(["20MHz", "40MHz"]) == "mixed:20MHz/40MHz"


def test_per_date_flags_aggregate_eligibility():
    per = [{"date": "d1", "water_ref_fraction": 0.1}, {"date": "d2", "water_ref_fraction": 0.9}]
    rain = {"d1": {"rain_affected": False}, "d2": {"rain_affected": True}}
    out = flags.per_date_flags(per, rain)
    assert out[0]["wind_affected"] is True and not out[0]["aggregate_eligible"]
    assert out[1]["rain_affected"] is True and not out[1]["aggregate_eligible"]


def test_confidence_high_when_clean():
    dates = [day("d1"), day("d2")]
    f = flags.build_flags(dates, arid=False, tracks=[152, 152])
    level, reasons = flags.assess_confidence(f, GOOD_CAL, GOOD_STATS)
    assert level == "high" and reasons == []


@pytest.mark.parametrize("dates,cal,stats,arid,expected", [
    ([day("d1", rain=True), day("d2")], GOOD_CAL, GOOD_STATS, False, "low"),
    ([day("d1", ref=0.1), day("d2")], GOOD_CAL, GOOD_STATS, False, "low"),
    ([day("d1", maturity="PROVISIONAL"), day("d2")], GOOD_CAL, GOOD_STATS, False, "medium"),
    ([day("d1"), day("d2")], GOOD_CAL, GOOD_STATS, True, "medium"),
    ([day("d1", rain=None), day("d2")], GOOD_CAL, GOOD_STATS, False, "medium"),
    ([day("d1"), day("d2")], [{"method": "default_uncalibrated", "radar_mode": "40MHz"}],
     GOOD_STATS, False, "low"),
    ([day("d1"), day("d2")], GOOD_CAL, {"aoi_valid_fraction": 0.4}, False, "low"),
    ([day("d1"), day("d2")], GOOD_CAL, {"water_ref_change_fraction": 0.05}, False, "medium"),
])
def test_confidence_downgrades(dates, cal, stats, arid, expected):
    f = flags.build_flags(dates, arid=arid, tracks=[152, 152])
    level, reasons = flags.assess_confidence(f, cal, stats)
    assert level == expected and reasons


def test_limitations_specific_first():
    dates = [day("2026-07-18", rain=True), day("2026-10-22", ref=0.1)]
    f = flags.build_flags(dates, arid=False, tracks=[1, 1])
    lim = flags.build_limitations(f, dates, [["2026-07-27", "2026-08-10"]])
    assert lim[0].startswith("2026-07-18: rain-affected")
    assert any("wind-roughened" in x for x in lim[:3])
    assert any("2026-07-27" in x for x in lim[:4])
    assert lim[-len(config.KNOWN_LIMITATIONS):] == config.KNOWN_LIMITATIONS

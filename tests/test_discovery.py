"""Discovery tests — offline. ASF responses are faked with the same property
names asf_search 14.x's NISARProduct exposes."""

from datetime import date, datetime

import pytest

from gaia import config
from gaia.discovery import (
    coverage_summary,
    dedupe_by_maturity,
    granule_from_product,
    granules_to_geojson,
    maturity_from_collection,
    parse_aoi,
    radar_mode_from_bandwidth,
    search_granules,
)

LAGOS = config.get_site("lagos").aoi_wkt


class FakeProduct:
    def __init__(self, scene, start, collection="NISAR_L2_GCOV_PROVISIONAL_V1",
                 track=172, frame=20, bw=("20",), pols=("HH", "HV"),
                 footprint=(3.0, 6.0, 4.0, 7.0)):
        x0, y0, x1, y1 = footprint
        self.geometry = {
            "type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
        }
        self.properties = {
            "sceneName": scene,
            "startTime": start,
            "stopTime": start,
            "pathNumber": track,
            "frameNumber": frame,
            "flightDirection": "ASCENDING",
            "mainBandPolarization": list(pols),
            "rangeBandwidth": list(bw),
            "collectionName": collection,
            "url": f"https://nisar.asf.earthdatacloud.nasa.gov/{scene}.h5",
            "s3Urls": [f"s3://bucket/{scene}.h5"],
            "bytes": {f"{scene}.h5": {"bytes": 6_000_000_000, "format": "HDF5"},
                      f"{scene}.png": {"bytes": 1000, "format": "PNG"}},
            "frameCoverage": "FULL",
        }


def fake_search(products):
    calls = []

    def _search(**kwargs):
        calls.append(kwargs)
        return products

    _search.calls = calls
    return _search


# --- AOI parsing ----------------------------------------------------------- #
def test_parse_aoi_formats():
    assert parse_aoi(LAGOS).area > 0
    assert parse_aoi((3.1, 6.38, 3.7, 6.7)).bounds == (3.1, 6.38, 3.7, 6.7)
    gj = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [3.4, 6.5]}}
    assert parse_aoi(gj).geom_type == "Point"
    fc = {"type": "FeatureCollection", "features": [gj, gj]}
    assert parse_aoi(fc).geom_type == "Point"


@pytest.mark.parametrize("bad", [(4, 6, 3, 7), "POLYGON EMPTY", (500, 0, 501, 1)])
def test_parse_aoi_rejects_bad(bad):
    with pytest.raises(ValueError):
        parse_aoi(bad)


# --- Field parsing --------------------------------------------------------- #
@pytest.mark.parametrize("name,expected", [
    ("NISAR_L2_GCOV_BETA_V1", "BETA"),
    ("NISAR_L2_GCOV_PROVISIONAL_V1", "PROVISIONAL"),
    ("NISAR_L2_GCOV_V1", "VALIDATED"),
    (None, "UNKNOWN"),
])
def test_maturity(name, expected):
    assert maturity_from_collection(name) == expected


@pytest.mark.parametrize("bw,expected", [
    (["20"], "20MHz"), (["40+5"], "40MHz"), ("77", "77MHz"), (None, None), ([], None),
])
def test_radar_mode(bw, expected):
    assert radar_mode_from_bandwidth(bw) == expected


def test_granule_from_product():
    aoi = parse_aoi(LAGOS)
    g = granule_from_product(FakeProduct("G1", "2026-03-01T05:10:00Z"), aoi)
    assert g.date == date(2026, 3, 1)
    assert (g.track, g.frame, g.radar_mode, g.maturity) == (172, 20, "20MHz", "PROVISIONAL")
    assert g.polarizations == ["HH", "HV"]
    assert g.size_bytes == 6_000_000_000  # .h5 only, browse PNG excluded
    assert g.aoi_overlap == 1.0


def test_concatenated_polarizations():
    p = FakeProduct("G1", "2026-03-01T05:10:00Z", pols=("HHHV",))
    assert granule_from_product(p).polarizations == ["HH", "HV"]


def test_partial_overlap():
    # footprint covers the western half of the Lagos box (3.1–3.4 of 3.1–3.7)
    p = FakeProduct("G1", "2026-03-01T05:10:00Z", footprint=(3.0, 6.0, 3.4, 7.0))
    g = granule_from_product(p, parse_aoi(LAGOS))
    assert g.aoi_overlap == pytest.approx(0.5, abs=1e-3)


# --- Search ---------------------------------------------------------------- #
def test_search_builds_query_and_sorts():
    fn = fake_search([
        FakeProduct("LATE", "2026-05-01T05:10:00Z"),
        FakeProduct("EARLY", "2026-01-01T05:10:00Z"),
    ])
    out = search_granules(LAGOS, "2026-01-01", "2026-06-01", maturity="VALIDATED", _search_fn=fn)
    assert [g.granule_id for g in out] == ["EARLY", "LATE"]
    q = fn.calls[0]
    assert q["dataset"] == "NISAR" and q["processingLevel"] == "GCOV"
    assert q["start"] == "2026-01-01T00:00:00Z" and q["end"] == "2026-06-01T23:59:59Z"
    assert q["dataMaturity"] == ["VALIDATED"]
    assert q["intersectsWith"].startswith("POLYGON")


def test_search_rejects_reversed_range():
    with pytest.raises(ValueError):
        search_granules(LAGOS, "2026-06-01", "2026-01-01", _search_fn=fake_search([]))


def test_dedupe_keeps_most_mature():
    t = "2026-03-01T05:10:00Z"
    fn = fake_search([
        FakeProduct("B", t, "NISAR_L2_GCOV_BETA_V1"),
        FakeProduct("V", t, "NISAR_L2_GCOV_V1"),
        FakeProduct("P", t, "NISAR_L2_GCOV_PROVISIONAL_V1"),
    ])
    out = search_granules(LAGOS, "2026-01-01", "2026-06-01", _search_fn=fn)
    assert [g.granule_id for g in out] == ["V"]
    assert len(search_granules(LAGOS, "2026-01-01", "2026-06-01", dedupe=False, _search_fn=fn)) == 3


def test_filters_radar_mode_and_overlap():
    fn = fake_search([
        FakeProduct("M20", "2026-01-01T05:00:00Z", bw=("20",)),
        FakeProduct("M40", "2026-01-13T05:00:00Z", bw=("40",)),
        FakeProduct("SLIVER", "2026-01-25T05:00:00Z", footprint=(3.0, 6.0, 3.15, 7.0)),
    ])
    out = search_granules(LAGOS, "2026-01-01", "2026-02-01", radar_mode="20MHz",
                          min_overlap=0.5, _search_fn=fn)
    assert [g.granule_id for g in out] == ["M20"]


# --- Coverage -------------------------------------------------------------- #
def test_coverage_flags_known_gap_and_modes():
    fn = fake_search([
        FakeProduct("A", "2026-07-19T05:00:00Z", bw=("20",)),
        FakeProduct("B", "2026-08-12T05:00:00Z", bw=("40",)),
    ])
    gs = search_granules(LAGOS, "2026-07-15", "2026-08-15", _search_fn=fn)
    cov = coverage_summary(gs, "2026-07-15", "2026-08-15")
    assert cov["granules_found"] == 2
    assert cov["known_gaps"] == [["2026-07-27", "2026-08-10"]]
    assert cov["by_radar_mode"] == {"20MHz": 1, "40MHz": 1}
    assert any("radar modes" in n for n in cov["notes"])
    assert any("PROVISIONAL" in n for n in cov["notes"])


def test_coverage_observed_gaps_and_empty():
    cov = coverage_summary([], "2026-01-01", "2026-03-01")
    assert cov["granules_found"] == 0 and cov["date_range"] is None
    assert cov["observed_gaps"] == [["2026-01-01", "2026-03-01"]]
    assert cov["known_gaps"] == []
    assert any("No NISAR" in n for n in cov["notes"])


def test_geojson_export():
    g = granule_from_product(FakeProduct("G1", "2026-03-01T05:10:00Z"))
    fc = granules_to_geojson([g])
    assert fc["features"][0]["properties"]["granule_id"] == "G1"
    assert fc["features"][0]["geometry"]["type"] == "Polygon"


def test_cli_discover_offline(monkeypatch, capsys):
    import asf_search
    from gaia.cli import main

    monkeypatch.setattr(asf_search, "search", fake_search([FakeProduct("G1", "2026-03-01T05:10:00Z")]))
    assert main(["discover", "--site", "lagos", "--start", "2026-01-01", "--end", "2026-06-01"]) == 0
    out = capsys.readouterr().out
    assert "2026-03-01" in out and "G1" in out and "PROVISIONAL" in out

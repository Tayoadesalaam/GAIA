"""Pipeline + API tests — offline. Discovery, ingestion, processing and rain
are replaced with fakes so this checks wiring and the response contract."""

import json
from datetime import date
from pathlib import Path

import pytest

from gaia import config, pipeline
from gaia.discovery import granule_from_product
from gaia.pipeline import PipelineError, run_water_change, select_pair
from tests.test_discovery import FakeProduct


def G(scene, day, track=152, collection="NISAR_L2_GCOV_V1"):
    return granule_from_product(
        FakeProduct(scene, f"{day}T18:12:16Z", collection=collection, track=track),
        None,
    )


# --- pair selection -------------------------------------------------------- #
def test_select_pair_same_track_and_most_mature():
    gs = [G("A15", "2026-07-21", 15), G("A152", "2026-07-18", 152),
          G("B152p", "2026-10-22", 152, "NISAR_L2_GCOV_PROVISIONAL_V1"),
          G("B152v", "2026-10-22", 152)]
    g1, g2 = select_pair(gs, date(2026, 7, 18), date(2026, 10, 22))
    assert (g1.granule_id, g2.granule_id) == ("A152", "B152v")


def test_select_pair_explains_missing_dates():
    gs = [G("A", "2026-07-18", 152), G("B", "2026-10-25", 15)]
    with pytest.raises(PipelineError, match="track 152: 2026-07-18"):
        select_pair(gs, date(2026, 7, 18), date(2026, 10, 25))


# --- pipeline contract ----------------------------------------------------- #
@pytest.fixture
def fake_pipeline(monkeypatch, tmp_path):
    gs = [G("A", "2026-07-18"), G("B", "2026-10-22")]
    calls = {"ingest": 0, "process": 0}
    monkeypatch.setattr(pipeline, "search_granules", lambda *a, **k: gs)

    class R:
        def __init__(self, g):
            self.sidecar = str(tmp_path / f"{g.granule_id}.json")
            Path(self.sidecar).write_text(json.dumps({"date": g.date.isoformat()}))

    def fake_ingest(g, aoi):
        calls["ingest"] += 1
        return R(g)

    def fake_process(s1, s2, aoi, out, **kw):
        calls["process"] += 1
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"tif")
        per = [{"date": s["date"], "maturity": "VALIDATED", "radar_mode": "40MHz",
                "water_ref_fraction": 0.92, "granule_id": "x", "track": 152} for s in (s1, s2)]
        return {"change_map_path": str(out), "quicklook_path": None,
                "dates": [s1["date"], s2["date"]], "per_date": per,
                "calibrations": [{"method": "per_date_classification", "radar_mode": "40MHz",
                                  "threshold_hh_db": -11.0, "threshold_hv_db": -19.5,
                                  "hit_rate": 0.91, "false_alarm_rate": 0.002, "note": None}],
                "stats": {"aoi_valid_fraction": 1.0, "water_ref_change_fraction": 0.01,
                          "new_water_km2": 4.0, "receded_water_km2": 1.0}}

    rain_by_date = {"2026-07-18": True, "2026-10-22": False}
    monkeypatch.setattr(pipeline, "ingest_granule", fake_ingest)
    monkeypatch.setattr(pipeline, "detect_water_change", fake_process)
    monkeypatch.setattr(pipeline, "rain_flag", lambda d, aoi: {
        "rain_affected": rain_by_date[d], "rain_mm": 40 if rain_by_date[d] else 2,
        "window": ["a", "b"], "error": None})
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    return calls


def test_pipeline_response_contract(fake_pipeline, tmp_path):
    r = run_water_change("2026-07-18", "2026-10-22", site="lagos", out_dir=tmp_path / "out")
    for key in ("aoi", "dates", "change_map_url", "coverage", "flags", "calibration",
                "limitations", "confidence"):
        assert key in r
    assert r["dates"] == ["2026-07-18", "2026-10-22"]
    assert r["coverage"]["granules_used"] == 2 and r["coverage"]["granules_found"] == 2
    assert r["coverage"]["gaps"][0] == ["2026-07-27", "2026-08-10"]
    assert r["flags"]["rain_affected"] is True and r["flags"]["wind_affected"] is False
    assert r["flags"]["radar_mode"] == "40MHz" and r["flags"]["maturity"] == "VALIDATED"
    assert r["calibration"]["method"] == "per_date_classification"
    assert r["calibration"]["threshold_hh_db"] == -11.0
    assert r["confidence"] == "low" and r["confidence_reasons"]
    assert r["per_date"][0]["aggregate_eligible"] is False
    assert any("rain-affected" in x for x in r["limitations"])


def test_pipeline_caches(fake_pipeline, tmp_path):
    run_water_change("2026-07-18", "2026-10-22", site="lagos", out_dir=tmp_path / "out")
    r = run_water_change("2026-07-18", "2026-10-22", site="lagos", out_dir=tmp_path / "out")
    assert r["cached"] and fake_pipeline["process"] == 1


def test_pipeline_rejects_bad_dates(fake_pipeline):
    with pytest.raises(PipelineError):
        run_water_change("2026-10-22", "2026-07-18", site="lagos")
    with pytest.raises(PipelineError):
        run_water_change("2026-07-18", "2026-10-22")


# --- API ------------------------------------------------------------------- #
@pytest.fixture
def client(fake_pipeline, monkeypatch):
    from fastapi.testclient import TestClient
    import gaia.api as api

    monkeypatch.setattr(api, "run_water_change",
                        lambda *a, **k: run_water_change(*a, out_dir=config.OUTPUT_DIR, **k))
    monkeypatch.setattr(api, "search_granules", lambda *a, **k: [G("A", "2026-07-18")])
    return TestClient(api.app)


def test_api_health_and_sites(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/sites").json()[0]["name"] == "lagos"


def test_api_search(client):
    r = client.post("/search", json={"site": "lagos", "start": "2026-07-01", "end": "2026-07-31"})
    assert r.status_code == 200
    assert r.json()["coverage"]["granules_found"] == 1
    assert r.json()["granules"][0]["granule_id"] == "A"


def test_api_validation(client):
    assert client.post("/search", json={}).status_code == 422
    assert client.post("/search", json={"site": "atlantis"}).status_code == 422
    r = client.post("/process", json={"site": "lagos", "date1": "2026-10-22", "date2": "2026-07-18"})
    assert r.status_code == 422


def test_api_process_returns_url(client):
    r = client.post("/process", json={"site": "lagos", "date1": "2026-07-18", "date2": "2026-10-22"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change_map_url"].startswith("http://testserver/files/water_change_2026-07-18_2026-10-22_")
    assert "change_map_path" not in body
    assert body["confidence"] == "low"


def test_api_engine_validation(client):
    assert client.post("/process", json={"site": "lagos", "engine": "flood",
                                         "date1": "2026-07-18", "date2": "2026-10-22"}).status_code == 422
    assert client.post("/process", json={"site": "lagos", "engine": "water_change",
                                         "start": "2026-07-01", "end": "2026-10-01"}).status_code == 422
    assert client.post("/process", json={"site": "lagos", "engine": "nonsense",
                                         "start": "2026-07-01", "end": "2026-10-01"}).status_code == 422


@pytest.mark.parametrize("engine", ["flood", "vegetation", "deformation"])
def test_api_dispatches_time_series_engines(client, monkeypatch, engine):
    import gaia.api as api

    seen = {}

    def fake(start, end, **kw):
        seen.update(kw, start=start, end=end)
        return {"engine": engine, "dates": [str(start), str(end)], "confidence": "low",
                "outputs": {"main": str(config.OUTPUT_DIR / f"{engine}_abc.tif")}}

    monkeypatch.setattr(api.pipeline, f"run_{engine}", fake)
    r = client.post("/process", json={"site": "kano", "engine": engine,
                                      "start": "2026-06-01", "end": "2026-11-01"})
    assert r.status_code == 200, r.text
    assert r.json()["outputs"]["main"] == f"http://testserver/files/{engine}_abc.tif"
    assert seen["site"] == "kano" and str(seen["start"]) == "2026-06-01"


def test_api_pipeline_error_is_422(client, monkeypatch):
    import gaia.api as api

    def boom(*a, **k):
        raise PipelineError("No NISAR GUNW interferograms for this AOI/time range.")

    monkeypatch.setattr(api.pipeline, "run_deformation", boom)
    r = client.post("/process", json={"site": "kano", "engine": "deformation",
                                      "start": "2026-06-01", "end": "2026-11-01"})
    assert r.status_code == 422 and "GUNW" in r.json()["detail"]

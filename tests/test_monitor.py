"""Location search + monitoring — offline (fake geocoder, ASF search and engines)."""

import json

import pytest

from gaia import config, monitor as mon
from gaia.discovery import granule_from_product
from gaia.geocode import LocationError, area_km2, resolve_location
from gaia.discovery import parse_aoi
from tests.test_discovery import FakeProduct

IBADAN = {"display_name": "Ibadan, Oyo, Nigeria", "lat": "7.378", "lon": "3.897",
          "boundingbox": ["7.218", "7.538", "3.737", "4.057"], "osm_type": "relation", "osm_id": 1,
          "type": "city", "addresstype": "city"}


@pytest.fixture(autouse=True)
def tmp_state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "MONITORS_FILE", tmp_path / "monitors.json")


def geo(hits):
    return lambda q: hits.get(q)


# --------------------------------------------------------------------------- #
# Location resolution
# --------------------------------------------------------------------------- #
def test_place_name_via_geocoder():
    loc = resolve_location("Ibadan", geocode_fn=geo({"Ibadan": IBADAN}))
    assert loc.source == "geocoder" and loc.osm["type"] == "city"
    assert 1000 < loc.area_km2 < 1400
    assert parse_aoi(loc.aoi_wkt).contains(parse_aoi("POINT(3.897 7.378)"))


def test_unknown_place():
    with pytest.raises(LocationError, match="Could not find"):
        resolve_location("nowhere-xyz", geocode_fn=geo({}))


def test_coordinates_make_box():
    loc = resolve_location("6.45, 3.40")
    assert loc.source == "coordinates" and (loc.lat, loc.lon) == (6.45, 3.40)
    assert loc.area_km2 == pytest.approx(config.MONITOR_POINT_BOX_KM ** 2, rel=0.02)


def test_bad_coordinates():
    with pytest.raises(LocationError, match="out of range"):
        resolve_location("95, 3.4")


def test_bbox_site_and_geometry():
    assert resolve_location("3.3, 6.4, 3.5, 6.6").source == "bbox"
    assert resolve_location("Lagos").site == "lagos"
    g = resolve_location({"type": "Polygon", "coordinates": [[[3.3, 6.4], [3.5, 6.4], [3.5, 6.6], [3.3, 6.4]]]})
    assert g.source == "geometry"


def test_large_area_is_cropped():
    country = {**IBADAN, "display_name": "Nigeria", "boundingbox": ["4.2", "13.9", "2.6", "14.7"]}
    loc = resolve_location("Nigeria", geocode_fn=geo({"Nigeria": country}))
    assert loc.area_km2 <= config.MONITOR_MAX_AOI_KM2 * 1.02
    assert any("cropped" in n for n in loc.notes)


def test_tiny_area_is_expanded():
    village = {**IBADAN, "boundingbox": ["7.3780", "7.3790", "3.8970", "3.8980"]}
    loc = resolve_location("hamlet", geocode_fn=geo({"hamlet": village}))
    assert loc.area_km2 == pytest.approx(config.MONITOR_POINT_BOX_KM ** 2, rel=0.02)


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def gcov(scene, day, track=152, pols=("HH", "HV")):
    return granule_from_product(FakeProduct(scene, f"{day}T18:12:16Z", track=track, pols=pols,
                                            footprint=(3.0, 6.0, 5.0, 8.0)), parse_aoi("POINT(3.9 7.4)").buffer(0.1))


def gunw(ref, sec, track=152):
    name = f"NISAR_L2_PR_GUNW_1_{track:03d}_A_005_2_4000_SH_{ref}T052245_{ref}T052319_{sec}T052245_{sec}T052319_P1"
    p = FakeProduct(name, f"{ref[:4]}-{ref[4:6]}-{ref[6:]}T05:22:45Z", track=track)
    p.properties["processingLevel"] = "GUNW"
    return granule_from_product(p)


def fake_search(gcovs, gunws):
    def _s(geom, start, end, product="GCOV", **kw):
        return list(gunws if product == "GUNW" else gcovs)
    return _s


FULL = fake_search(
    [gcov(f"G{i}", f"2026-{m}") for i, m in enumerate(["07-06", "07-18", "08-11", "08-23", "09-04"])]
    + [gcov("S", "2026-09-16", pols=("HH",))],
    [gunw("20260706", "20260718"), gunw("20260718", "20260811"), gunw("20260811", "20260823")])
EMPTY = fake_search([], [])
SPARSE = fake_search([gcov("G1", "2026-10-10"), gcov("G2", "2026-10-22")], [])


def test_availability_all_engines():
    a = mon.check_availability("POINT(3.9 7.4)", "2026-06-01", "2026-11-01", search_fn=FULL)
    assert a["any_available"] and all(e["available"] for e in a["engines"].values())
    assert a["engines"]["water_change"]["params"] == {"date1": "2026-08-23", "date2": "2026-09-04", "track": 152}
    assert a["gunw"]["chain_length"] == 3
    assert a["latest_acquisition"] == "2026-09-04"
    assert any("single-polarization" in n for n in a["notes"])
    assert any("2026-07-27" in n for n in a["notes"])


def test_availability_partial():
    a = mon.check_availability("POINT(3.9 7.4)", "2026-06-01", "2026-11-01", search_fn=SPARSE)
    e = a["engines"]
    assert e["water_change"]["available"] and not e["flood"]["available"] and not e["deformation"]["available"]
    assert "found 2" in e["flood"]["reason"]


def test_require_data_raises_no_data():
    loc = resolve_location("0.0, -30.0")
    a = mon.check_availability(loc.aoi_wkt, "2026-06-01", "2026-11-01", search_fn=EMPTY)
    with pytest.raises(mon.NoDataError) as ei:
        mon.require_data(loc, a)
    d = ei.value.detail
    assert d["error"] == "no_data" and "No NISAR data found" in d["message"]
    assert d["suggestions"] and d["location"]["source"] == "coordinates"


def test_require_data_not_enough_for_requested_engine():
    loc = resolve_location("7.4, 3.9")
    a = mon.check_availability(loc.aoi_wkt, "2026-06-01", "2026-11-01", search_fn=SPARSE)
    with pytest.raises(mon.NoDataError, match="not enough for flood"):
        mon.require_data(loc, a, ["flood"])
    assert mon.require_data(loc, a) == ["water_change"]


# --------------------------------------------------------------------------- #
# Jobs + monitors
# --------------------------------------------------------------------------- #
def ok_runner(name, aoi_wkt, params, site):
    if name == "deformation":
        raise RuntimeError("GUNW server timeout")
    return {"engine": name, "confidence": "medium", "outputs": {"main": f"{name}.tif"}, "params": params}


def test_create_monitor_runs_available_engines():
    out = mon.create_monitor("7.4, 3.9", start="2026-06-01", end="2026-11-01", wait=True,
                             search_fn=SPARSE, runner=ok_runner)
    job = out["job"]
    assert job["status"] == "done"
    assert job["engines"]["water_change"]["status"] == "done"
    assert job["engines"]["flood"]["status"] == "skipped"
    assert set(job["results"]) == {"water_change"}
    saved = mon.get_job(job["id"])
    assert saved["status"] == "done"
    m = mon.get_monitor(out["monitor"]["id"])
    assert m["last_job_id"] == job["id"] and m["latest_acquisition"] == "2026-10-22"


def test_engine_failure_is_isolated():
    out = mon.create_monitor("7.4, 3.9", start="2026-06-01", end="2026-11-01", wait=True,
                             search_fn=FULL, runner=ok_runner)
    job = out["job"]
    assert job["status"] == "partial"
    assert job["engines"]["deformation"]["status"] == "failed"
    assert "timeout" in job["engines"]["deformation"]["error"]
    assert job["engines"]["flood"]["status"] == "done"


def test_create_monitor_no_data_registers_nothing():
    with pytest.raises(mon.NoDataError):
        mon.create_monitor("0.0, -30.0", search_fn=EMPTY, runner=ok_runner, wait=True)
    assert mon.list_monitors() == []


def test_refresh_only_runs_on_new_data():
    out = mon.create_monitor("7.4, 3.9", start="2026-06-01", wait=True, search_fn=SPARSE, runner=ok_runner)
    mid = out["monitor"]["id"]
    r = mon.refresh_monitor(mid, wait=True, search_fn=SPARSE, runner=ok_runner)
    assert r["status"] == "up_to_date"
    newer = fake_search([gcov("G1", "2026-10-10"), gcov("G2", "2026-10-22"), gcov("G3", "2026-11-03")], [])
    r = mon.refresh_monitor(mid, wait=True, search_fn=newer, runner=ok_runner)
    assert r["status"] == "new_data" and r["latest_acquisition"] == "2026-11-03"
    assert r["job"]["engines"]["water_change"]["params"]["date2"] == "2026-11-03"
    assert mon.delete_monitor(mid) and mon.get_monitor(mid) is None


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import gaia.api as api
    import gaia.geocode as geocode

    monkeypatch.setattr(geocode, "nominatim", geo({"Ibadan": IBADAN}))
    search = {"fn": SPARSE}
    real_check = mon.check_availability
    monkeypatch.setattr(mon, "check_availability",
                        lambda aoi, s=None, e=None, **k: real_check(aoi, s, e, search_fn=search["fn"]))
    real_create = mon.create_monitor
    monkeypatch.setattr(mon, "create_monitor",
                        lambda q, **k: real_create(q, **k, wait=True, runner=ok_runner))
    c = TestClient(api.app)
    c.search = search
    return c


def test_api_locate(client):
    r = client.get("/locate", params={"q": "Ibadan"})
    assert r.status_code == 200 and r.json()["source"] == "geocoder"
    r = client.get("/locate", params={"q": "nowhere"})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "location_not_found"


def test_api_availability_and_no_data(client):
    r = client.post("/availability", json={"location": "Ibadan"})
    assert r.status_code == 200 and r.json()["availability"]["engines"]["water_change"]["available"]
    client.search["fn"] = EMPTY
    r = client.post("/availability", json={"location": "0.0, -30.0"})
    assert r.status_code == 404 and r.json()["error"] == "no_data"


def test_api_monitor_flow(client):
    r = client.post("/monitor", json={"location": "Ibadan", "start": "2026-06-01"})
    assert r.status_code == 202, r.text
    body = r.json()
    job = body["job"]
    assert job["status"] == "done" and "aoi_wkt" not in job["location"]
    assert job["results"]["water_change"]["outputs"]["main"].endswith("/files/water_change.tif")
    st = client.get(job["status_url"].replace("http://testserver", ""))
    assert st.status_code == 200 and st.json()["id"] == job["id"]
    mid = body["monitor"]["id"]
    assert client.get("/monitors").json()[0]["id"] == mid
    assert client.get(f"/monitors/{mid}").json()["last_job"]["id"] == job["id"]
    assert client.post(f"/monitors/{mid}/refresh").json()["status"] == "up_to_date"
    assert client.delete(f"/monitors/{mid}").status_code == 200
    assert client.get(f"/monitors/{mid}").status_code == 404


def test_api_monitor_no_data(client):
    client.search["fn"] = EMPTY
    r = client.post("/monitor", json={"location": "0.0, -30.0"})
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "no_data" and body["suggestions"] and "availability" in body
    assert client.post("/monitor", json={"location": "nowhere"}).status_code == 404


def test_resume_unfinished_jobs(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    jobs = {
        "a": {"id": "a", "status": "running", "monitor_id": None, "location": {}, "results": {"flood": {"x": 1}},
              "engines": {"flood": {"status": "done"}, "vegetation": {"status": "running", "params": {}}}},
        "b": {"id": "b", "status": "done", "monitor_id": None, "location": {}, "results": {}, "engines": {}},
    }
    for j in jobs.values():
        (tmp_path / f"{j['id']}.json").write_text(_json.dumps(j))
    submitted = []
    assert mon.resume_unfinished_jobs(submit=lambda f, job, runner: submitted.append(job)) == ["a"]
    job = submitted[0]
    assert job["status"] == "queued" and job["engines"]["flood"]["status"] == "done"
    assert job["engines"]["vegetation"]["status"] == "queued" and job["results"] == {"flood": {"x": 1}}


def test_cancel_job_stops_before_next_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path)
    job = {"id": "c1", "status": "queued", "monitor_id": None, "created": "2026-01-01", "location": {"aoi_wkt": "x"},
           "results": {}, "engines": {"water_change": {"status": "queued", "params": {}},
                                      "flood": {"status": "queued", "params": {}}}}
    mon.save_job(job)
    ran = []

    def runner(name, *a):
        ran.append(name)
        mon.cancel_job("c1")  # user cancels while the first engine runs
        return {"confidence": "low"}

    out = mon.execute_job(job, runner)
    assert ran == ["water_change"] and out["status"] == "cancelled"
    assert out["results"]["water_change"] == {"confidence": "low"}  # finished work is kept
    assert out["engines"]["flood"]["status"] == "cancelled"
    assert mon.active_jobs() == [] and mon.resume_unfinished_jobs(submit=lambda *a: None) == []


def test_tracked_run_is_cancellable():
    import sys
    from gaia import ingest
    ingest.clear_cancel()
    assert ingest._tracked_run([sys.executable, "-c", "print(1)"], capture_output=True).stdout.strip() == "1"
    ingest.cancel_active()
    with pytest.raises(ingest.IngestCancelled):
        ingest._tracked_run([sys.executable, "-c", "print(1)"])
    ingest.clear_cancel()

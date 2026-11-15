"""gaia.store: backend selection, the exact Supabase HTTP calls, and a full monitor → job → result →
map flow through an in-memory imitation of PostgREST + Storage. Offline."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from gaia import config, store

URL = "https://proj.supabase.co"


class _R:
    def __init__(self, code, body=None, content=b""):
        self.status_code, self._b, self.content = code, body, content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._b


class FakeSupabase:
    """Just enough PostgREST (eq / in filters, order, upsert on conflict, FK on jobs.monitor_id)
    and Storage (upload with x-upsert, download, delete) to exercise SupabaseStore."""

    PK = {"monitors": ("id",), "jobs": ("id",), "results": ("cache_key",), "cache_entries": ("namespace", "key")}

    def __init__(self):
        self.tables = {t: {} for t in self.PK}
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, params=None, data=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        path = urlparse(url).path
        if path.startswith("/rest/v1/"):
            return self._rest(method, path.split("/")[-1], params or {}, data, headers or {})
        if path.startswith("/storage/v1/object/"):
            name = path[len("/storage/v1/object/"):]
            if method == "POST":
                if name in self.objects and headers.get("x-upsert") != "true":
                    return _R(409, {"error": "Duplicate"})
                self.objects[name] = (data, headers["Content-Type"])
                return _R(200, {"Key": name})
            if method == "GET":
                return _R(200, content=self.objects[name][0]) if name in self.objects else _R(400, {"error": "not_found", "statusCode": "404"})
            if method == "DELETE":
                self.objects.pop(name, None)
                return _R(200, [])
        return _R(404, {"message": "no route"})

    def _match(self, row, params):
        for k, v in params.items():
            if k in ("select", "order", "on_conflict"):
                continue
            op, _, val = v.partition(".")
            if op == "eq" and str(row.get(k)) != val:
                return False
            if op == "in" and str(row.get(k)) not in val.strip("()").split(","):
                return False
        return True

    def _rest(self, method, table, params, data, headers):
        rows = self.tables[table]
        if method == "POST":
            row = json.loads(data)
            assert "resolution=merge-duplicates" in headers.get("Prefer", "")
            assert params["on_conflict"] == ",".join(self.PK[table])
            if table == "jobs" and row.get("monitor_id") and (row["monitor_id"],) not in self.tables["monitors"]:
                return _R(409, {"code": "23503", "message": "violates foreign key constraint"})
            rows[tuple(row[k] for k in self.PK[table])] = row
            return _R(201)
        hits = [r for r in rows.values() if self._match(r, params)]
        if method == "GET":
            if params.get("order"):
                col, _, d = params["order"].partition(".")
                hits.sort(key=lambda r: str(r.get(col) or ""), reverse=d == "desc")
            return _R(200, [{"doc": r["doc"]} for r in hits])
        if method == "DELETE":
            for r in hits:
                rows.pop(tuple(r[k] for k in self.PK[table]))
            if table == "monitors":  # on delete set null
                for j in self.tables["jobs"].values():
                    if j["monitor_id"] in {h["id"] for h in hits}:
                        j["monitor_id"] = None
            return _R(200, hits)
        return _R(405)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeSupabase()
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(store, "_store", store.SupabaseStore(URL, "sb_secret_TESTKEY", session=f))
    monkeypatch.setattr(store, "_store_sig", ("supabase", URL, "sb_secret_TESTKEY", "gaia-outputs"))
    monkeypatch.setenv("GAIA_STORE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_TESTKEY")
    return f


def test_selection(monkeypatch):
    monkeypatch.delenv("GAIA_STORE")
    assert store.settings()["mode"] == "local"                       # no keys → local
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_x")
    assert store.settings()["mode"] == "supabase"                    # both set → Supabase
    monkeypatch.setenv("GAIA_STORE", "local")
    assert store.get_store().kind == "local"                         # explicit override wins


def test_publishable_key_is_refused(monkeypatch):
    monkeypatch.setenv("GAIA_STORE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_publishable_abc")
    with pytest.raises(store.StoreError, match="secret key"):
        store.get_store()


def test_secret_key_goes_in_apikey_only_legacy_jwt_also_bearer():
    f = FakeSupabase()
    store.SupabaseStore(URL, "sb_secret_K", session=f).get_job("x")
    assert f.calls[-1]["headers"] == {"apikey": "sb_secret_K"}
    store.SupabaseStore(URL, "eyJlegacy", session=f).get_job("x")
    assert f.calls[-1]["headers"] == {"apikey": "eyJlegacy", "Authorization": "Bearer eyJlegacy"}


def test_monitor_job_flow_and_queue(fake):
    from gaia import monitor as mon
    s = store.get_store()
    s.put_monitor({"id": "m1", "query": "Lagos", "name": "Lagos, Nigeria", "created": "2026-11-15T00:00:00+00:00"})
    mon.save_job({"id": "j2", "monitor_id": "m1", "status": "queued", "created": "2026-11-15T02:00:00+00:00", "engines": {}})
    mon.save_job({"id": "j1", "monitor_id": "m1", "status": "running", "created": "2026-11-15T03:00:00+00:00", "engines": {}})
    mon.save_job({"id": "j0", "monitor_id": "m1", "status": "done", "created": "2026-11-15T01:00:00+00:00", "engines": {}})
    assert [j["id"] for j in mon.active_jobs()] == ["j1", "j2"]      # running first, then by creation
    row = fake.tables["jobs"][("j1",)]
    assert (row["status"], row["monitor_id"], row["created_at"]) == ("running", "m1", "2026-11-15T03:00:00+00:00")
    mon._update_monitor("m1", last_job_id="j1", last_run_status="running")
    assert mon.get_monitor("m1")["last_job_id"] == "j1" and fake.tables["monitors"][("m1",)]["last_run_status"] == "running"
    assert [m["id"] for m in mon.list_monitors()] == ["m1"]
    assert mon.delete_monitor("m1") and mon.get_monitor("m1") is None
    mon.save_job({**mon.get_job("j1"), "status": "done"})             # monitor gone: job saved, unlinked
    assert fake.tables["jobs"][("j1",)]["monitor_id"] is None and mon.get_job("j1")["status"] == "done"


def test_results_and_output_files(fake):
    from gaia import pipeline
    out = Path(config.OUTPUT_DIR); out.mkdir(parents=True, exist_ok=True)
    tif = out / "flood_abc_peak.tif"; tif.write_bytes(b"II*\x00fake-cog")
    calls = []
    def compute(stem):
        calls.append(stem)
        return {"engine": "flood", "outputs": {"peak_flood_map": str(tif)}, "confidence": "medium"}
    r1 = pipeline._cached_run("flood", ["aoi", "2026"], out, True, compute)
    r2 = pipeline._cached_run("flood", ["aoi", "2026"], out, True, compute)
    assert len(calls) == 1 and r1["cached"] is False and r2["cached"] is True     # second run from the table
    key = Path(calls[0]).name
    assert fake.tables["results"][(key,)]["engine"] == "flood"
    assert fake.objects["gaia-outputs/flood_abc_peak.tif"] == (b"II*\x00fake-cog", "image/tiff")
    tif.unlink()                                                                  # another machine / cleared cache
    got = store.get_store().fetch_output("flood_abc_peak.tif")
    assert got == tif and got.read_bytes() == b"II*\x00fake-cog"
    assert store.get_store().fetch_output("missing.tif") is None


def test_caches_hash_keys_and_survive_store_outage(fake, monkeypatch):
    s = store.get_store()
    s.cache_put("geocode", "lagos, nigeria (west)", {"lat": 6.5})
    assert s.cache_get("geocode", "lagos, nigeria (west)") == (True, {"lat": 6.5})
    assert s.cache_get("geocode", "kano") == (False, None)
    (ns, k), = fake.tables["cache_entries"]
    assert ns == "geocode" and len(k) == 40 and "lagos" not in k             # free text kept out of URLs
    def down(*a, **kw):
        raise store.StoreError("Supabase not reachable (ConnectionError)")
    monkeypatch.setattr(s, "cache_get", down)
    monkeypatch.setattr(s, "cache_put", down)
    from gaia import geocode
    calls = []
    class _Resp:
        status_code = 200
        def json(self):
            calls.append(1); return [{"lat": "6.5", "lon": "3.4"}]
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    monkeypatch.setattr(config, "GEOCODER_MIN_INTERVAL_S", 0)
    assert geocode.nominatim("Lagos")["lat"] == "6.5" and calls == [1]       # cache down → still answers


def test_overlay_upload_and_files_route(fake, monkeypatch):
    from fastapi.testclient import TestClient
    from gaia import api
    out = Path(config.OUTPUT_DIR); out.mkdir(parents=True, exist_ok=True)
    fake.objects["gaia-outputs/x.png"] = (b"\x89PNG-bytes", "image/png")
    client = TestClient(api.app)
    r = client.get("/files/x.png")                                            # not on disk → from the bucket
    assert r.status_code == 200 and r.content == b"\x89PNG-bytes" and (out / "x.png").exists()
    assert client.get("/files/..%2F..%2Fsecret.txt").status_code == 404      # never leaves OUTPUT_DIR
    assert client.get("/health").json()["store"] == "supabase"


def test_check_reports_each_step(fake):
    lines = []
    assert store.check(log=lines.append) is True
    assert sum(l.strip().startswith("ok") for l in lines) == 5 and not fake.objects  # probe file cleaned up


def test_import_local(fake, monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "JOBS_DIR", data / "jobs")
    monkeypatch.setattr(config, "MONITORS_FILE", data / "monitors.json")
    monkeypatch.setattr(config, "OUTPUT_DIR", data / "output")
    loc = store.LocalStore()
    loc.put_monitor({"id": "m1", "query": "Lagos", "created": "2026-11-15T00:00:00+00:00"})
    loc.put_job({"id": "j1", "monitor_id": "m1", "status": "done", "created": "2026-11-15T01:00:00+00:00"})
    (data / "output").mkdir(parents=True)
    (data / "output" / "flood_abc.json").write_text(json.dumps({"engine": "flood", "confidence": "low"}))
    (data / "output" / "flood_abc_peak.tif").write_bytes(b"tif")
    (data / "output" / "overlay_flood_abc_peak.json").write_text("{}")
    (data / "geocode_cache.json").write_text(json.dumps({"lagos": {"lat": 6.5}}))
    n = store.import_local(log=lambda *_: None)
    assert (n["monitors"], n["jobs"], n["results"], n["cache"], n["files"]) == (1, 1, 1, 1, 2)
    assert set(fake.objects) == {"gaia-outputs/flood_abc_peak.tif", "gaia-outputs/overlay_flood_abc_peak.json"}

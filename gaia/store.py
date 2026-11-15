"""Where GAIA keeps its state.

Two interchangeable backends behind one small API:

* ``LocalStore`` — the original layout under ``data/`` (monitors.json, jobs/*.json, output/*.json,
  geocode_cache.json, rain/, references/). Default when Supabase isn't configured; used by tests.
* ``SupabaseStore`` — Postgres tables ``monitors``, ``jobs``, ``results``, ``cache_entries`` and the
  private Storage bucket ``gaia-outputs`` (see ``supabase/migrations``).

Supabase access is server-only: the backend sends the **secret key** (``sb_secret_…``, role
``service_role``) in the ``apikey`` header. Tables and bucket have RLS on and no policies, so the
publishable/anon key can do nothing. Never ship the secret key to a browser.

Selection: ``GAIA_STORE=supabase|local``; unset → Supabase when ``SUPABASE_URL`` and
``SUPABASE_SECRET_KEY`` are both set, else local.

Engines still write rasters to ``config.OUTPUT_DIR`` while computing (rasterio needs files); in
Supabase mode they are uploaded to the bucket and that folder is only a cache — files missing on
this machine are fetched from the bucket on demand.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from gaia import config

ACTIVE = ("queued", "running")


class StoreError(RuntimeError):
    """Persistence failure (Supabase unreachable, bad key, missing table…). Never contains keys."""


def _dumps(doc: Any) -> str:
    return json.dumps(doc, indent=1, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Local files (original behaviour). Paths are read from config at call time so
# tests can point them at tmp dirs.
# --------------------------------------------------------------------------- #
class LocalStore:
    kind = "local"

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # -- jobs ---------------------------------------------------------------
    def _job_path(self, job_id: str) -> Path:
        return Path(config.JOBS_DIR) / f"{job_id}.json"

    def put_job(self, job: dict[str, Any]) -> None:
        p = self._job_path(job["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(_dumps(job), encoding="utf-8")
            tmp.replace(p)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        p = self._job_path(job_id)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except ValueError:
            return None

    def list_jobs(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        want = set(statuses) if statuses else None
        out = []
        for p in Path(config.JOBS_DIR).glob("*.json"):
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if want is None or j.get("status") in want:
                out.append(j)
        return out

    # -- monitors -----------------------------------------------------------
    def _monitors(self) -> dict[str, Any]:
        p = Path(config.MONITORS_FILE)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            return {}

    def put_monitor(self, mon: dict[str, Any]) -> None:
        with self._lock:
            m = self._monitors()
            m[mon["id"]] = mon
            p = Path(config.MONITORS_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_dumps(m), encoding="utf-8")

    def get_monitor(self, monitor_id: str) -> dict[str, Any] | None:
        return self._monitors().get(monitor_id)

    def list_monitors(self) -> list[dict[str, Any]]:
        return sorted(self._monitors().values(), key=lambda x: x.get("created") or "", reverse=True)

    def delete_monitor(self, monitor_id: str) -> bool:
        with self._lock:
            m = self._monitors()
            if m.pop(monitor_id, None) is None:
                return False
            Path(config.MONITORS_FILE).write_text(_dumps(m), encoding="utf-8")
            return True

    # -- engine results -----------------------------------------------------
    def get_result(self, stem: Path) -> dict[str, Any] | None:
        p = Path(stem).with_suffix(".json")
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except ValueError:
            return None

    def put_result(self, stem: Path, engine: str, doc: dict[str, Any]) -> None:
        p = Path(stem).with_suffix(".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

    # -- small caches ------------------------------------------------------
    def _cache_file(self, namespace: str, key: str) -> Path:
        base = Path(config.DATA_DIR)
        if namespace == "rain":
            return base / "rain" / f"imerg_{key}.json"
        if namespace == "references":
            return base / "references" / f"{key}.json"
        return base / f"{namespace}_cache.json"  # geocode: one file, dict of key → value

    def cache_get(self, namespace: str, key: str) -> tuple[bool, Any]:
        p = self._cache_file(namespace, key)
        try:
            if namespace == "geocode":
                d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                return (key in d), d.get(key)
            return (True, json.loads(p.read_text(encoding="utf-8"))) if p.exists() else (False, None)
        except (OSError, ValueError):
            return False, None

    def cache_put(self, namespace: str, key: str, value: Any) -> None:
        p = self._cache_file(namespace, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if namespace == "geocode":
                try:
                    d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                except ValueError:
                    d = {}
                d[key] = value
                p.write_text(_dumps(d), encoding="utf-8")
            else:
                p.write_text(json.dumps(value, default=str), encoding="utf-8")

    # -- output files: already on disk ----------------------------------------
    def save_output(self, path: str | Path) -> None:
        return None

    def fetch_output(self, name: str) -> Path | None:
        p = Path(config.OUTPUT_DIR) / Path(name).name
        return p if p.exists() else None


# --------------------------------------------------------------------------- #
# Supabase (PostgREST + Storage over HTTPS, secret key)
# --------------------------------------------------------------------------- #
class SupabaseStore:
    kind = "supabase"

    def __init__(self, url: str, key: str, bucket: str = "gaia-outputs", *,
                 session: Any | None = None, timeout: float = 30) -> None:
        import requests

        self.url = url.rstrip("/")
        self.bucket = bucket
        self.timeout = timeout
        self.http = session or requests.Session()
        self._headers = {"apikey": key}
        if key.startswith("eyJ"):  # legacy service_role JWT also needs the Authorization header
            self._headers["Authorization"] = f"Bearer {key}"

    # -- HTTP --------------------------------------------------------------
    def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 204), **kw: Any):
        headers = {**self._headers, **kw.pop("headers", {})}
        try:
            r = self.http.request(method, self.url + path, headers=headers, timeout=self.timeout, **kw)
        except Exception as exc:
            raise StoreError(f"Supabase not reachable ({type(exc).__name__})") from None
        if r.status_code not in ok:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or body.get("msg") or ""
                code = body.get("code") or body.get("statusCode") or ""
            except Exception:
                msg, code = (r.text or "")[:200], ""
            raise StoreError(f"Supabase {method} {path.split('?')[0]} → HTTP {r.status_code} {code} {msg}".strip())
        return r

    def _rows(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        return self._req("GET", f"/rest/v1/{table}", params=params).json()

    def _upsert(self, table: str, row: dict[str, Any], conflict: str) -> None:
        self._req("POST", f"/rest/v1/{table}", params={"on_conflict": conflict},
                  data=json.dumps(row, default=str),
                  headers={"Content-Type": "application/json",
                           "Prefer": "resolution=merge-duplicates,return=minimal"})

    @staticmethod
    def _doc(row: dict[str, Any]) -> dict[str, Any]:
        return row["doc"]

    # -- jobs ---------------------------------------------------------------
    def put_job(self, job: dict[str, Any]) -> None:
        row = {"id": job["id"], "monitor_id": job.get("monitor_id"), "kind": job.get("kind"),
               "status": job.get("status") or "queued", "created_at": job.get("created"),
               "doc": json.loads(json.dumps(job, default=str))}
        try:
            self._upsert("jobs", row, "id")
        except StoreError as exc:  # its monitor was deleted meanwhile (FK): keep the job, unlinked
            if "23503" not in str(exc) or row["monitor_id"] is None:
                raise
            self._upsert("jobs", {**row, "monitor_id": None}, "id")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        rows = self._rows("jobs", {"id": f"eq.{job_id}", "select": "doc"})
        return self._doc(rows[0]) if rows else None

    def list_jobs(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        params = {"select": "doc", "order": "created_at.asc"}
        if statuses:
            params["status"] = "in.(" + ",".join(statuses) + ")"
        return [self._doc(r) for r in self._rows("jobs", params)]

    # -- monitors -----------------------------------------------------------
    def put_monitor(self, mon: dict[str, Any]) -> None:
        self._upsert("monitors", {
            "id": mon["id"], "query": mon.get("query"), "name": mon.get("name"),
            "last_job_id": mon.get("last_job_id"), "last_run_status": mon.get("last_run_status"),
            "latest_acquisition": mon.get("latest_acquisition"), "created_at": mon.get("created"),
            "doc": json.loads(json.dumps(mon, default=str))}, "id")

    def get_monitor(self, monitor_id: str) -> dict[str, Any] | None:
        rows = self._rows("monitors", {"id": f"eq.{monitor_id}", "select": "doc"})
        return self._doc(rows[0]) if rows else None

    def list_monitors(self) -> list[dict[str, Any]]:
        return [self._doc(r) for r in self._rows("monitors", {"select": "doc", "order": "created_at.desc"})]

    def delete_monitor(self, monitor_id: str) -> bool:
        r = self._req("DELETE", "/rest/v1/monitors", params={"id": f"eq.{monitor_id}"},
                      headers={"Prefer": "return=representation"})
        return bool(r.json())

    # -- engine results -----------------------------------------------------
    def get_result(self, stem: Path) -> dict[str, Any] | None:
        rows = self._rows("results", {"cache_key": f"eq.{Path(stem).name}", "select": "doc"})
        return self._doc(rows[0]) if rows else None

    def put_result(self, stem: Path, engine: str, doc: dict[str, Any]) -> None:
        self._upsert("results", {"cache_key": Path(stem).name, "engine": engine,
                                 "doc": json.loads(json.dumps(doc, default=str))}, "cache_key")

    # -- small caches (key hashed: free text such as place names stays out of URLs) -------------
    @staticmethod
    def _ckey(key: str) -> str:
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def cache_get(self, namespace: str, key: str) -> tuple[bool, Any]:
        rows = self._rows("cache_entries", {"namespace": f"eq.{namespace}", "key": f"eq.{self._ckey(key)}",
                                            "select": "doc"})
        return (True, rows[0]["doc"].get("value")) if rows else (False, None)

    def cache_put(self, namespace: str, key: str, value: Any) -> None:
        self._upsert("cache_entries", {"namespace": namespace, "key": self._ckey(key),
                                       "doc": {"key": key, "value": json.loads(json.dumps(value, default=str))}},
                     "namespace,key")

    # -- output files (private bucket) ------------------------------------------
    def save_output(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_file():
            return
        ctype = {".tif": "image/tiff", ".tiff": "image/tiff", ".png": "image/png",
                 ".json": "application/json"}.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0]
        self._req("POST", f"/storage/v1/object/{self.bucket}/{p.name}", data=p.read_bytes(),
                  headers={"Content-Type": ctype or "application/octet-stream", "x-upsert": "true"})

    def fetch_output(self, name: str) -> Path | None:
        """Local copy of an output file, downloaded from the bucket if this machine lacks it."""
        dest = Path(config.OUTPUT_DIR) / Path(name).name
        if dest.exists():
            return dest
        try:
            r = self._req("GET", f"/storage/v1/object/{self.bucket}/{dest.name}", ok=(200,))
        except StoreError as exc:
            if "HTTP 400" in str(exc) or "HTTP 404" in str(exc):  # Storage answers 400/404 for missing objects
                return None
            raise
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        return dest

    def delete_output(self, name: str) -> None:
        self._req("DELETE", f"/storage/v1/object/{self.bucket}/{Path(name).name}", ok=(200, 204, 400, 404))


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def settings() -> dict[str, Any]:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    mode = (os.getenv("GAIA_STORE") or "").strip().lower()
    if mode not in ("local", "supabase"):
        mode = "supabase" if url and key else "local"
    return {"mode": mode, "url": url, "key": key,
            "bucket": (os.getenv("SUPABASE_BUCKET") or "gaia-outputs").strip()}


_store: LocalStore | SupabaseStore | None = None
_store_sig: tuple | None = None
_store_lock = threading.Lock()


def get_store() -> LocalStore | SupabaseStore:
    """The configured store (rebuilt if the environment changes, e.g. in tests)."""
    global _store, _store_sig
    st = settings()
    sig = (st["mode"], st["url"], st["key"], st["bucket"])
    with _store_lock:
        if _store is None or sig != _store_sig:
            if st["mode"] == "supabase":
                if not (st["url"] and st["key"]):
                    raise StoreError("GAIA_STORE=supabase needs SUPABASE_URL and SUPABASE_SECRET_KEY in .env")
                if st["key"].startswith("sb_publishable_"):
                    raise StoreError("SUPABASE_SECRET_KEY holds a publishable key. The backend needs the secret key "
                                     "(sb_secret_…); the tables have no anon/publishable access by design.")
                _store = SupabaseStore(st["url"], st["key"], st["bucket"])
            else:
                _store = LocalStore()
            _store_sig = sig
        return _store


def save_outputs(paths: Iterable[str | Path | None], *, log: Callable[[str], None] = print) -> list[str]:
    """Upload output files to the bucket (Supabase mode). Never fatal: a failed upload keeps the
    local file, is logged, and the name is returned so callers can disclose it."""
    failed = []
    store = get_store()
    for p in paths:
        if not p:
            continue
        try:
            store.save_output(p)
        except StoreError as exc:
            failed.append(Path(p).name)
            log(f"GAIA store: could not upload {Path(p).name}: {exc}")
    return failed


def check(*, log: Callable[[str], None] = print) -> bool:
    """Round-trip every table and the bucket with a throwaway row/file. Returns True when all pass."""
    st = settings()
    store = get_store()
    log(f"store: {store.kind}" + (f" → {st['url']} (bucket {st['bucket']})" if store.kind == "supabase" else
                                  f" → {config.DATA_DIR}"))
    if store.kind != "supabase":
        log("Supabase not configured: set SUPABASE_URL and SUPABASE_SECRET_KEY in .env (see .env.example).")
        return False
    ok = True

    def step(name: str, fn: Callable[[], Any]) -> None:
        nonlocal ok
        try:
            fn()
            log(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001 — report every failing step
            ok = False
            log(f"  FAIL  {name}: {exc}")

    probe = "gaia_store_check"
    step("cache_entries read/write", lambda: (store.cache_put("geocode", probe, {"ok": True}),
                                              _expect(store.cache_get("geocode", probe) == (True, {"ok": True}))))
    step("monitors read/write/delete", lambda: (store.put_monitor({"id": probe, "query": probe, "name": probe,
                                                                   "created": "2000-01-01T00:00:00+00:00"}),
                                                _expect(store.get_monitor(probe)["name"] == probe),
                                                _expect(store.delete_monitor(probe))))
    step("jobs read", lambda: store.list_jobs(ACTIVE))
    step("results read", lambda: store.get_result(Path(probe)))

    def bucket() -> None:
        tmp = Path(config.OUTPUT_DIR) / f"{probe}.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text('{"ok": true}', encoding="utf-8")
        store.save_output(tmp)
        tmp.unlink()
        got = store.fetch_output(tmp.name)
        _expect(got is not None and json.loads(got.read_text()) == {"ok": True})
        got.unlink()
        store.delete_output(tmp.name)
    step(f"bucket {st['bucket']} upload/download/delete", bucket)
    return ok


def _expect(cond: bool) -> None:
    if not cond:
        raise StoreError("unexpected value read back")


def import_local(*, log: Callable[[str], None] = print, upload_files: bool = True) -> dict[str, int]:
    """Copy everything in the local data/ folder into Supabase (idempotent: upserts)."""
    target = get_store()
    if target.kind != "supabase":
        raise StoreError("Set SUPABASE_URL and SUPABASE_SECRET_KEY first (GAIA_STORE must not be 'local').")
    src = LocalStore()
    n = {"monitors": 0, "jobs": 0, "results": 0, "cache": 0, "files": 0, "file_errors": 0}
    for m in src.list_monitors():
        target.put_monitor(m); n["monitors"] += 1
    for j in sorted(src.list_jobs(), key=lambda j: j.get("created") or ""):
        target.put_job(j); n["jobs"] += 1
    out = Path(config.OUTPUT_DIR)
    for p in sorted(out.glob("*.json")):
        if p.name.startswith("overlay_"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        engine = doc.get("engine") if isinstance(doc, dict) else None
        if engine in ("water_change", "flood", "vegetation", "deformation"):
            target.put_result(p.with_suffix(""), engine, doc); n["results"] += 1
    data = Path(config.DATA_DIR)
    geo = data / "geocode_cache.json"
    if geo.exists():
        for k, v in json.loads(geo.read_text(encoding="utf-8")).items():
            target.cache_put("geocode", k, v); n["cache"] += 1
    for p in (data / "rain").glob("imerg_*.json"):
        target.cache_put("rain", p.stem[len("imerg_"):], json.loads(p.read_text())); n["cache"] += 1
    for p in (data / "references").glob("*.json"):
        target.cache_put("references", p.stem, json.loads(p.read_text())); n["cache"] += 1
    if upload_files:
        files = [p for p in out.iterdir() if p.is_file() and p.suffix.lower() in (".tif", ".png", ".json")
                 and not (p.suffix == ".json" and not p.name.startswith("overlay_"))]
        for i, p in enumerate(sorted(files), 1):
            try:
                target.save_output(p); n["files"] += 1
            except StoreError as exc:
                n["file_errors"] += 1
                log(f"  could not upload {p.name}: {exc}")
            if i % 25 == 0:
                log(f"  uploaded {i}/{len(files)} files…")
    log("imported: " + ", ".join(f"{k} {v}" for k, v in n.items()))
    return n

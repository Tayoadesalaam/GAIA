"""Ingestion tests — offline. openSEPPO is replaced by a fake that writes small
UTM GeoTIFFs with openSEPPO's output naming (``*-EBD_A_hh_pwr.tif``)."""

import base64
import json
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from gaia import config, ingest
from gaia.discovery import parse_aoi
from gaia.ingest import (
    IngestError,
    build_command,
    date_from_granule_id,
    ensure_openseppo_auth,
    ingest_granule,
)

LAGOS = config.get_site("lagos").aoi_wkt
GID = "NISAR_L2_PR_GCOV_033_152_D_085_4005_DHDH_A_20261022T181216_20261022T181257_P05036_N_F_J_001"
URL = f"https://nisar.asf.earthdatacloud.nasa.gov/NISAR/{GID}/{GID}.h5"


def make_jwt(exp: float) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'RS256'})}.{enc({'exp': int(exp)})}.sig"


def write_tif(path: Path, value=0.05, nan_frac=0.0):
    # UTM 31N grid covering the Lagos AOI (~511–577 km E, ~705–741 km N)
    data = np.full((50, 90), value, dtype="float32")
    data[: int(50 * nan_frac), :] = np.nan
    with rasterio.open(path, "w", driver="GTiff", width=90, height=50, count=1,
                       dtype="float32", crs="EPSG:32631", nodata=np.nan,
                       transform=from_origin(500_000, 750_000, 1000, 1000)) as dst:
        dst.write(data, 1)


def fake_seppo(calls, value=0.05, nan_frac=0.0, pols=("hh", "hv"), rc=0):
    def _run(cmd, **kw):
        calls.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        for p in pols:
            write_tif(out / f"{GID}-EBD_A_{p}_pwr.tif", value, nan_frac)
        return SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")
    return _run


@pytest.fixture(autouse=True)
def token(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EARTHDATA_TOKEN", make_jwt(time.time() + 86400))
    monkeypatch.setattr(config, "OPENSEPPO_TOKEN_CACHE", tmp_path / "cache" / "tok.json")


# --- auth ------------------------------------------------------------------ #
def test_auth_seeds_cache(tmp_path):
    exp = ensure_openseppo_auth()
    cached = json.loads(config.OPENSEPPO_TOKEN_CACHE.read_text())
    assert cached["access_token"] == config.EARTHDATA_TOKEN
    assert exp.date() >= date.today()


@pytest.mark.parametrize("bad,msg", [
    ("EDL-...", "No Earthdata token"),
    ("not-a-jwt", "not a valid"),
    (make_jwt(time.time() - 10), "expired"),
])
def test_auth_rejects_bad_tokens(bad, msg):
    with pytest.raises(IngestError, match=msg):
        ensure_openseppo_auth(token=bad)


# --- naming / command ------------------------------------------------------ #
def test_date_from_granule_id():
    assert date_from_granule_id(GID) == date(2026, 10, 22)
    with pytest.raises(ValueError):
        date_from_granule_id("nonsense")


def test_build_command(tmp_path):
    cmd = build_command(URL, parse_aoi(LAGOS), tmp_path / "w", downscale=2)
    assert cmd[cmd.index("-i") + 1] == URL
    assert cmd[cmd.index("-o") + 1].endswith("/")
    v = cmd.index("-vars")
    assert cmd[v + 1:v + 3] == ["HHHH", "HVHV"]
    p = cmd.index("-projwin")
    assert [float(x) for x in cmd[p + 1:p + 5]] == [3.10, 6.70, 3.70, 6.38]  # ULX ULY LRX LRY
    assert cmd[cmd.index("-projwin_srs") + 1] == "EPSG:4326"
    assert "-pwr" in cmd and "--no_vrt" in cmd and cmd[cmd.index("-d") + 1] == "2"


# --- ingest ---------------------------------------------------------------- #
def test_ingest_renames_and_writes_sidecar(tmp_path):
    calls = []
    r = ingest_granule(URL, LAGOS, tmp_path, _run=fake_seppo(calls, nan_frac=0.2))
    assert len(calls) == 1
    hh = Path(r.paths["HH"])
    assert hh.name == f"{GID}_2026-10-22_HH.tif" and hh.exists()
    assert Path(r.paths["HV"]).exists()
    assert r.crs == "EPSG:32631" and r.resolution_m == (1000.0, 1000.0)
    assert 0.6 < r.valid_fraction["HH"] < 1.0  # top rows NaN
    side = json.loads(Path(r.sidecar).read_text())
    assert side["scale"] == "pwr" and side["backscatter"] == "gamma0"
    assert side["aoi_wkt"].startswith("POLYGON")
    assert not any((tmp_path / ".tmp").iterdir())  # temp cleaned up


def test_ingest_uses_cache(tmp_path):
    calls = []
    ingest_granule(URL, LAGOS, tmp_path, _run=fake_seppo(calls))
    r = ingest_granule(URL, LAGOS, tmp_path, _run=fake_seppo(calls))
    assert r.cached and len(calls) == 1
    ingest_granule(URL, LAGOS, tmp_path, overwrite=True, _run=fake_seppo(calls))
    assert len(calls) == 2


def test_ingest_errors(tmp_path):
    with pytest.raises(IngestError, match="exit 1"):
        ingest_granule(URL, LAGOS, tmp_path, _run=fake_seppo([], rc=1))
    with pytest.raises(IngestError, match="no HV band"):
        ingest_granule(URL, LAGOS, tmp_path, overwrite=True, _run=fake_seppo([], pols=("hh",)))
    with pytest.raises(IngestError, match="no valid pixels"):
        ingest_granule(URL, LAGOS, tmp_path, overwrite=True, _run=fake_seppo([], nan_frac=1.0))


def test_ingest_from_granule_object(tmp_path):
    from tests.test_discovery import FakeProduct
    from gaia.discovery import granule_from_product

    g = granule_from_product(FakeProduct(GID, "2026-10-22T18:12:16Z"))
    r = ingest_granule(g, LAGOS, tmp_path, _run=fake_seppo([]))
    assert r.granule["maturity"] == "PROVISIONAL" and r.date == "2026-10-22"


def test_ingest_retries_transient_empty_output(tmp_path):
    calls = []
    good = fake_seppo(calls)

    def flaky(cmd, **kw):
        if not calls:  # first attempt: exit 0 but no files (network hiccup)
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="HTTP 503 while reading chunk", stderr="")
        return good(cmd, **kw)

    r = ingest_granule(URL, LAGOS, tmp_path, _run=flaky)
    assert len(calls) == 2 and Path(r.paths["HH"]).exists()


def test_ingest_error_includes_openseppo_output(tmp_path):
    def empty(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="HTTP 503 while reading chunk", stderr="")

    with pytest.raises(IngestError, match="HTTP 503"):
        ingest_granule(URL, LAGOS, tmp_path, _run=empty)


def test_failed_ingest_leaves_no_temp_dirs(tmp_path):
    with pytest.raises(IngestError):
        ingest_granule(URL, LAGOS, tmp_path, _run=fake_seppo([], rc=1))
    assert not any((tmp_path / ".tmp").iterdir())

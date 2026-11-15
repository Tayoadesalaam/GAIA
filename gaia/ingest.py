"""Layer 2 — Ingestion.

Stream only the AOI out of a NISAR GCOV HDF5 granule (~6 GB) with openSEPPO's
``seppo_nisar_gcov_convert`` and write small per-polarization GeoTIFFs::

    data/ingest/{granule_id}_{date}_{POL}.tif     # linear power, gamma0, native UTM
    data/ingest/{granule_id}_{date}.json          # sidecar metadata for processing

openSEPPO does HTTP range reads of only the HDF5 chunks intersecting the AOI,
so nothing close to the full file is downloaded.

Auth: openSEPPO reads a cached Earthdata bearer token from
``~/.cache/openseppo/earthaccess_token.json`` (else falls back to ~/.netrc).
:func:`ensure_openseppo_auth` seeds that cache from ``EARTHDATA_TOKEN`` in .env.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import threading
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from shapely.geometry.base import BaseGeometry

from gaia import config
from gaia.discovery import Granule, parse_aoi


class IngestError(RuntimeError):
    """Ingestion failed (auth, openSEPPO error, or empty AOI subset)."""


class IngestCancelled(IngestError):
    """A user cancelled the job while openSEPPO was running."""


# Live openSEPPO processes, so a cancelled job can stop its download immediately.
_active: set[subprocess.Popen] = set()
_active_lock = threading.Lock()
_cancel = threading.Event()


def _tracked_run(cmd: list[str], capture_output: bool = False, text: bool = True) -> subprocess.CompletedProcess:
    """``subprocess.run`` that cancel_active() can interrupt (no retry after a cancel)."""
    if _cancel.is_set():
        raise IngestCancelled("Cancelled before download started")
    pipe = subprocess.PIPE if capture_output else None
    proc = subprocess.Popen(cmd, stdout=pipe, stderr=pipe, text=text)
    with _active_lock:
        _active.add(proc)
    try:
        out, err = proc.communicate()
    finally:
        with _active_lock:
            _active.discard(proc)
    if _cancel.is_set():
        raise IngestCancelled("Cancelled while downloading")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def cancel_active() -> int:
    """Stop every running openSEPPO download; returns how many were killed."""
    _cancel.set()
    with _active_lock:
        procs = list(_active)
    for p in procs:
        try:
            if sys.platform == "win32":
                # the venv python.exe is a launcher with a child interpreter: kill the whole tree
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                p.kill()
        except OSError:
            pass
    return len(procs)


def clear_cancel() -> None:
    _cancel.clear()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _jwt_expiry(token: str) -> datetime | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None
    except Exception:
        return None


def ensure_openseppo_auth(
    token: str | None = None,
    cache_path: Path | None = None,
) -> datetime | None:
    """Make sure openSEPPO can authenticate. Returns the token expiry (UTC).

    Writes ``EARTHDATA_TOKEN`` into openSEPPO's token cache when it differs
    from what is cached. Raises :class:`IngestError` with a clear message if no
    usable token is available.
    """
    token = (token if token is not None else config.EARTHDATA_TOKEN or "").strip()
    cache_path = Path(cache_path or config.OPENSEPPO_TOKEN_CACHE)

    if not token or token.endswith("..."):
        if cache_path.exists():  # previously seeded / earthaccess login
            return None
        raise IngestError(
            "No Earthdata token. Put EARTHDATA_TOKEN=<your token> in .env "
            "(generate at https://urs.earthdata.nasa.gov/profile → Generate Token; "
            "ASF DAAC access must be approved)."
        )

    exp = _jwt_expiry(token)
    if exp is None:
        raise IngestError("EARTHDATA_TOKEN is not a valid Earthdata JWT token.")
    if exp <= datetime.now(timezone.utc):
        raise IngestError(f"EARTHDATA_TOKEN expired on {exp.date()}; generate a new one.")

    current = None
    if cache_path.exists():
        try:
            current = json.loads(cache_path.read_text()).get("access_token")
        except Exception:
            current = None
    if current != token:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"access_token": token}))
    return exp


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
def date_from_granule_id(granule_id: str) -> date:
    """NISAR_L2_PR_GCOV_..._20261022T181216_20261022T181257_... → 2026-10-22."""
    for tok in granule_id.split("_"):
        if len(tok) == 15 and tok[8] == "T" and tok[:8].isdigit():
            return datetime.strptime(tok[:8], "%Y%m%d").date()
    raise ValueError(f"Cannot find an acquisition date in '{granule_id}'")


def granule_id_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".h5") else name


def aoi_key(geom: BaseGeometry) -> str:
    """Short stable id for an AOI (used to separate ingest caches)."""
    return hashlib.sha1(geom.wkt.encode()).hexdigest()[:8]


def output_path(out_dir: Path, granule_id: str, day: date, pol: str) -> Path:
    return Path(out_dir) / f"{granule_id}_{day.isoformat()}_{pol.upper()}.tif"


def sidecar_path(out_dir: Path, granule_id: str, day: date) -> Path:
    return Path(out_dir) / f"{granule_id}_{day.isoformat()}.json"


# --------------------------------------------------------------------------- #
# openSEPPO command
# --------------------------------------------------------------------------- #
def build_command(
    url: str,
    aoi: BaseGeometry,
    work_dir: Path,
    *,
    polarizations: Sequence[str] = config.INGEST_POLARIZATIONS,
    frequency: str = config.INGEST_FREQUENCY,
    downscale: int | None = None,
) -> list[str]:
    """Command line for ``seppo_nisar_gcov_convert`` restricted to the AOI bbox."""
    minx, miny, maxx, maxy = aoi.bounds
    cmd = [
        sys.executable, "-m", "openseppo.cli.nisar_gcov_convert",
        "-i", url,
        "-o", work_dir.as_posix().rstrip("/") + "/",
        "-vars", *[f"{p.upper()}{p.upper()}" for p in polarizations],  # HH → HHHH
        "-f", frequency,
        "-of", "COG",
        "-pwr",
        "-projwin", str(minx), str(maxy), str(maxx), str(miny),  # ULX ULY LRX LRY
        "-projwin_srs", "EPSG:4326",
        "--no_vrt",
    ]
    if downscale and downscale > 1:
        cmd += ["-d", str(int(downscale))]
    return cmd


def _find_band(work_dir: Path, frequency: str, pol: str) -> Path | None:
    hits = sorted(work_dir.glob(f"*-EBD_{frequency}_{pol.lower()}_{config.INGEST_SCALE}.tif"))
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# Result + stats
# --------------------------------------------------------------------------- #
@dataclass
class IngestResult:
    granule_id: str
    date: str
    paths: dict[str, str]  # POL → GeoTIFF path
    sidecar: str
    scale: str = config.INGEST_SCALE
    backscatter: str = config.INGEST_BACKSCATTER
    frequency: str = config.INGEST_FREQUENCY
    crs: str | None = None
    shape: tuple[int, int] | None = None
    resolution_m: tuple[float, float] | None = None
    valid_fraction: dict[str, float] = field(default_factory=dict)  # within AOI
    granule: dict[str, Any] | None = None  # discovery metadata, if known
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def raster_stats(path: Path, aoi: BaseGeometry) -> dict[str, Any]:
    """CRS, shape, resolution and fraction of valid (finite, >0) AOI pixels."""
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    with rasterio.open(path) as src:
        data = src.read(1, masked=True).filled(np.nan).astype("float32")
        geom = transform_geom("EPSG:4326", src.crs, aoi.__geo_interface__)
        inside = ~geometry_mask([geom], out_shape=src.shape, transform=src.transform)
        n_inside = int(inside.sum())
        valid = np.isfinite(data) & (data > 0) & inside
        return {
            "crs": src.crs.to_string() if src.crs else None,
            "shape": tuple(src.shape),
            "resolution_m": tuple(float(r) for r in src.res),
            "valid_fraction": round(float(valid.sum()) / n_inside, 4) if n_inside else 0.0,
        }


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def ingest_granule(
    granule: Granule | str,
    aoi: Any,
    out_dir: Path | str | None = None,
    *,
    polarizations: Sequence[str] = config.INGEST_POLARIZATIONS,
    downscale: int | None = None,
    overwrite: bool = False,
    verbose: bool = False,
    _run: Callable[..., Any] | None = None,
    _stats: Callable[[Path, BaseGeometry], dict[str, Any]] | None = None,
) -> IngestResult:
    """Extract the AOI from one GCOV granule into per-polarization GeoTIFFs.

    Args:
        granule: a discovery :class:`Granule` or an HTTPS/S3 URL to the .h5.
        aoi: anything :func:`gaia.discovery.parse_aoi` accepts (EPSG:4326).
        out_dir: default ``data/ingest``.
        polarizations: e.g. ("HH", "HV").
        downscale: optional integer block-average factor (in linear power).
        overwrite: re-run even if outputs already exist.
    """
    geom = parse_aoi(aoi)
    # Cache per AOI: the same granule cut to a different AOI is a different file.
    out_dir = Path(out_dir or config.DATA_DIR / "ingest" / aoi_key(geom))
    out_dir.mkdir(parents=True, exist_ok=True)
    run = _run or _tracked_run
    stats_fn = _stats or raster_stats

    if isinstance(granule, Granule):
        gid, url, day, meta = granule.granule_id, granule.url, granule.date, granule.to_dict()
        meta.pop("footprint", None)
        missing = [p for p in polarizations if granule.polarizations and p.upper() not in granule.polarizations]
        if missing:
            raise IngestError(f"{gid} lacks polarization(s) {missing} "
                              f"(acquired: {', '.join(granule.polarizations)}).")
    else:
        url = str(granule)
        gid, meta = granule_id_from_url(url), None
        day = date_from_granule_id(gid)
    if not url:
        raise IngestError(f"Granule {gid} has no download URL")

    pols = [p.upper() for p in polarizations]
    finals = {p: output_path(out_dir, gid, day, p) for p in pols}
    side = sidecar_path(out_dir, gid, day)
    cached = not overwrite and all(f.exists() for f in finals.values()) and side.exists()

    if not cached:
        ensure_openseppo_auth()
        # Short work dir: openSEPPO names files "{granule}-EBD_...", and a
        # 95-char granule id twice in one path breaks Windows' 260-char MAX_PATH.
        work = out_dir / ".tmp" / hashlib.sha1(gid.encode()).hexdigest()[:10]
        cmd = build_command(url, geom, work, polarizations=pols, downscale=downscale)
        if verbose:
            cmd.append("-v")
        # openSEPPO can exit 0 yet write nothing after a transient network
        # error (seen live: Lagos 2026-09-28 failed, then succeeded on rerun).
        # Retry, and surface openSEPPO's own output if it still fails.
        missing: list[str] = []
        try:
            for attempt in range(1, config.INGEST_ATTEMPTS + 1):
                if work.exists():
                    shutil.rmtree(work)
                work.mkdir(parents=True)
                proc = run(cmd, capture_output=not verbose, text=True)
                tail = ((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or ""))[-2000:]
                if getattr(proc, "returncode", 0) != 0:
                    if attempt < config.INGEST_ATTEMPTS:
                        continue
                    raise IngestError(f"openSEPPO failed for {gid} (exit {proc.returncode}, "
                                      f"{attempt} attempts):\n{tail}")
                bands = {pol: _find_band(work, config.INGEST_FREQUENCY, pol) for pol in finals}
                missing = [pol for pol, b in bands.items() if b is None]
                if not missing:
                    break
            if missing:
                produced = sorted(p.name for p in work.glob("*.tif"))
                raise IngestError(f"openSEPPO produced no {', '.join(missing)} band for {gid} after "
                                  f"{config.INGEST_ATTEMPTS} attempts. Files: {produced}\n"
                                  f"openSEPPO output:\n{tail or '(none captured)'}")
            for pol, final in finals.items():
                shutil.move(str(bands[pol]), final)
        finally:  # never leave work dirs behind, success or failure
            shutil.rmtree(work, ignore_errors=True)

    result = IngestResult(
        granule_id=gid,
        date=day.isoformat(),
        paths={p: str(f) for p, f in finals.items()},
        sidecar=str(side),
        granule=meta,
        cached=cached,
    )
    for pol, final in finals.items():
        s = stats_fn(final, geom)
        result.crs, result.shape, result.resolution_m = s["crs"], s["shape"], s["resolution_m"]
        result.valid_fraction[pol] = s["valid_fraction"]

    if all(v == 0 for v in result.valid_fraction.values()):
        raise IngestError(f"{gid}: AOI subset contains no valid pixels (AOI outside swath?)")

    payload = result.to_dict()
    payload["aoi_wkt"] = geom.wkt
    side.write_text(json.dumps(payload, indent=2, default=str))
    return result


def load_sidecar(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


# --------------------------------------------------------------------------- #
# GUNW (interferograms) — for the deformation engine
# --------------------------------------------------------------------------- #
# GUNW layer → openSEPPO filename token (unwrappedInterferogram group, 80 m grid)
GUNW_LAYERS = {
    "unwrappedPhase": "unwphase",
    "coherenceMagnitude": "coh",
    "connectedComponents": "concomp",
    "ionospherePhaseScreen": "iono",
}


def build_gunw_command(url: str, aoi: BaseGeometry, work_dir: Path,
                       layers: Sequence[str] = tuple(GUNW_LAYERS)) -> list[str]:
    minx, miny, maxx, maxy = aoi.bounds
    return [
        sys.executable, "-m", "openseppo.cli.nisar_gunw_convert",
        "-i", url,
        "-o", work_dir.as_posix().rstrip("/") + "/",
        "-lyr", "unwrappedInterferogram",
        "-vars", *layers,
        "-of", "COG",
        "-projwin", str(minx), str(maxy), str(maxx), str(miny),
        "-projwin_srs", "EPSG:4326",
        "--no_vrt", "--no_time_series",
    ]


def ingest_gunw(
    granule: Granule,
    aoi: Any,
    out_dir: Path | str | None = None,
    *,
    layers: Sequence[str] = tuple(GUNW_LAYERS),
    overwrite: bool = False,
    verbose: bool = False,
    _run: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Extract the AOI from one GUNW interferogram → per-layer GeoTIFFs + sidecar.

    Returns the sidecar dict: paths per layer token, reference/secondary dates,
    grid info and the discovery metadata.
    """
    import rasterio

    geom = parse_aoi(aoi)
    out_dir = Path(out_dir or config.DATA_DIR / "ingest" / aoi_key(geom))
    out_dir.mkdir(parents=True, exist_ok=True)
    run = _run or _tracked_run
    gid = granule.granule_id
    tokens = {layer: GUNW_LAYERS[layer] for layer in layers}
    finals = {tok: out_dir / f"{gid}_{tok}.tif" for tok in tokens.values()}
    side = out_dir / f"{gid}.json"
    if not overwrite and side.exists() and all(f.exists() for f in finals.values()):
        out = json.loads(side.read_text())
        out["cached"] = True
        return out

    ensure_openseppo_auth()
    work = out_dir / ".tmp" / hashlib.sha1(gid.encode()).hexdigest()[:10]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    cmd = build_gunw_command(granule.url, geom, work, layers)
    if verbose:
        cmd.append("-v")
    try:
        proc = run(cmd, capture_output=not verbose, text=True)
        tail = ((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or ""))[-2000:]
        if getattr(proc, "returncode", 0) != 0:
            raise IngestError(f"openSEPPO GUNW conversion failed for {gid} (exit {proc.returncode}):\n{tail}")
        for tok, final in finals.items():
            hits = sorted(work.glob(f"*-EBD_*_unw_{tok}.tif"))
            if not hits:
                produced = sorted(p.name for p in work.glob("*.tif"))
                raise IngestError(f"openSEPPO produced no '{tok}' layer for {gid}. Files: {produced}\n"
                                  f"openSEPPO output:\n{tail or '(none captured)'}")
            shutil.move(str(hits[0]), final)
    finally:  # never leave work dirs behind, success or failure
        shutil.rmtree(work, ignore_errors=True)

    with rasterio.open(finals["unwphase"]) as src:
        import numpy as np

        phase = src.read(1, masked=True).filled(np.nan)
        info = {"crs": src.crs.to_string() if src.crs else None, "shape": list(src.shape),
                "resolution_m": [float(r) for r in src.res],
                "valid_fraction": round(float(np.isfinite(phase).mean()), 4)}
    meta = granule.to_dict()
    meta.pop("footprint", None)
    out = {
        "granule_id": gid,
        "product": "GUNW",
        "reference_date": granule.date.isoformat(),
        "secondary_date": granule.secondary_date.isoformat() if granule.secondary_date else None,
        "paths": {tok: str(f) for tok, f in finals.items()},
        "granule": meta,
        "aoi_wkt": geom.wkt,
        **info,
    }
    side.write_text(json.dumps(out, indent=2, default=str))
    out["cached"] = False
    return out


__all__ = [
    "IngestError",
    "IngestResult",
    "build_command",
    "build_gunw_command",
    "ingest_gunw",
    "aoi_key",
    "date_from_granule_id",
    "ensure_openseppo_auth",
    "ingest_granule",
    "load_sidecar",
    "output_path",
]

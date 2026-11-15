"""Site/AOI definitions, thresholds and constants for GAIA.

Everything tunable lives here so the pipeline modules stay free of magic numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:  # python-dotenv is optional at import time; tests don't need it
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# --------------------------------------------------------------------------- #
# Paths & credentials
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("GAIA_DATA_DIR", REPO_ROOT / "data"))

# Only needed from Layer 2 (ingestion) onward. CMR search is unauthenticated.
EARTHDATA_TOKEN = os.getenv("EARTHDATA_TOKEN")

# --------------------------------------------------------------------------- #
# NISAR constants
# --------------------------------------------------------------------------- #
NISAR_DATASET = "NISAR"
NISAR_PRODUCT = "GCOV"

# Ordered least → most mature. Used to pick the best copy of an acquisition.
MATURITY_ORDER = ("BETA", "PROVISIONAL", "VALIDATED")

# Documented NISAR data gaps (inclusive). Discovery reports any overlap with a
# requested time range so the API can disclose it.
KNOWN_DATA_GAPS: list[tuple[date, date]] = [
    (date(2026, 7, 27), date(2026, 8, 10)),
]

# A gap between consecutive acquisitions longer than this is reported as an
# observed coverage gap. NISAR's repeat cycle is 12 days; allow some slack.
NISAR_REPEAT_DAYS = 12
OBSERVED_GAP_THRESHOLD_DAYS = 2 * NISAR_REPEAT_DAYS

# --------------------------------------------------------------------------- #
# Ingestion (Layer 2 — openSEPPO)
# --------------------------------------------------------------------------- #
# Polarizations to extract. GCOV stores covariance terms: HH → "HHHH".
INGEST_POLARIZATIONS = ("HH", "HV")
# GCOV frequency A is the main band (20/40/77 MHz); B is the 5 MHz side band.
INGEST_FREQUENCY = "A"
# Linear power, gamma0 (RTC). dB conversion happens in processing so that any
# averaging/resampling is done in linear units.
INGEST_SCALE = "pwr"
INGEST_BACKSCATTER = "gamma0"
# openSEPPO sometimes exits 0 without writing output after a network hiccup.
INGEST_ATTEMPTS = 2
# Where openSEPPO caches the Earthdata bearer token it reads.
OPENSEPPO_TOKEN_CACHE = Path.home() / ".cache" / "openseppo" / "earthaccess_token.json"

# --------------------------------------------------------------------------- #
# Uncertainty flag thresholds (see CLAUDE.md → Uncertainty Handling)
# --------------------------------------------------------------------------- #
RAIN_LOOKBACK_DAYS = 3
RAIN_THRESHOLD_MM = 15.0
# GPM IMERG Late Run daily V07 on GES DISC (OPeNDAP subsetting, bearer token).
IMERG_OPENDAP_BASE = "https://gpm1.gesdisc.eosdis.nasa.gov/opendap/GPM_L3/GPM_3IMERGDL.07"
IMERG_FILE_TEMPLATE = "3B-DAY-L.MS.MRG.3IMERG.{ymd}-S000000-E235959.{version}.nc4"
IMERG_VERSIONS = ("V07C", "V07B", "V07A")  # tried in order (V07C current as of Nov 2026)
IMERG_RES_DEG = 0.1

# Wind-roughened water: if less than this fraction of the permanent water
# reference classifies as water on a date, the pass is flagged wind_affected.
WIND_MIN_WATER_REF_FRACTION = 0.5

# --------------------------------------------------------------------------- #
# Processing (Layer 3)
# --------------------------------------------------------------------------- #
# Boxcar speckle filter (pixels, applied in linear power) before classification.
SPECKLE_WINDOW = 5
# Fallback water thresholds when no water_ref/dry_ref is available
# (TerraPulse, Amazon/Manaus, 20 MHz). Reported as method "default_uncalibrated".
DEFAULT_WATER_THRESHOLD_HH_DB = -14.0
DEFAULT_WATER_THRESHOLD_HV_DB = -20.0
# Calibration search grid (dB) and acceptance limits.
CALIBRATION_HH_GRID = tuple(x / 2 for x in range(-48, -15))  # -24.0 … -8.0
CALIBRATION_HV_GRID = tuple(x / 2 for x in range(-64, -27))  # -32.0 … -14.0
CALIBRATION_MAX_FALSE_ALARM = 0.02
# Dry-only calibration (no permanent water in the AOI): water counts as
# detectable only if thresholds that keep known land out (≤ 2% false alarms)
# still pass typical open water in BOTH channels. L-band open water: HH ≲ -20,
# HV ≲ -25 dB (Lagos lagoon, wind-roughened: HH -15, HV -22). Karakum dune
# sand (HH -20.8, HV -24.0) fails this → "water not detectable here".
# With no water to measure a hit rate, false alarms are the only check, so the
# budget is stricter: 2% of a 400 km² desert is ~8 km² of fake "water" per pass
# (seen live: Karakum flood run at 30 m picked a pair at 1.98% → 15 km² fake flood).
DRY_ONLY_MAX_FALSE_ALARM = 0.005
TYPICAL_WATER_HH_DB = -20.0
TYPICAL_WATER_HV_DB = -25.0

# --------------------------------------------------------------------------- #
# Automatic reference areas — JRC Global Surface Water occurrence (30 m)
# --------------------------------------------------------------------------- #
GSW_URL_TEMPLATE = ("https://storage.googleapis.com/global-surface-water/downloads2021/"
                    "occurrence/occurrence_{tile}v1_4_2021.tif")
REF_WATER_MIN_OCCURRENCE = 90  # % of observations water → permanent water
REF_WATER_ERODE_PX = 3  # ≈ 80 m from shore
REF_DRY_ERODE_PX = 4  # ≈ 110 m from any water ever seen
REF_MIN_WATER_PX = 200  # ≈ 0.15 km²
REF_MIN_DRY_PX = 2000  # ≈ 1.5 km²
# Calibration quality → confidence.
GOOD_HIT_RATE = 0.80
GOOD_FALSE_ALARM = 0.02
# Minimum connected change patch (pixels at native 10 m) kept in the change map.
MIN_CHANGE_PATCH_PX = 9

# Change map classes
CLASS_UNCHANGED, CLASS_NEW_WATER, CLASS_RECEDED_WATER, CLASS_NODATA = 0, 1, 2, 255

OUTPUT_DIR = DATA_DIR / "output"

# --------------------------------------------------------------------------- #
# Location search + monitoring
# --------------------------------------------------------------------------- #
# OpenStreetMap Nominatim (free; ≤ 1 request/s; generic app name, no personal data).
GEOCODER_URL = "https://nominatim.openstreetmap.org/search"
GEOCODER_USER_AGENT = "GAIA-backend/0.1 (NISAR change-monitoring prototype)"
GEOCODER_MIN_INTERVAL_S = 1.1
# Points (and tiny places) become a square of this side length.
MONITOR_POINT_BOX_KM = 20.0
# Larger places are cropped to a centred square of this area — keeps a new
# location's first run to minutes, not hours (Lagos AOI ≈ 2,350 km²).
MONITOR_MAX_AOI_KM2 = 2500.0
MONITOR_DEFAULT_LOOKBACK_DAYS = 365
# Minimum data per engine (same track, dual-pol GCOV dates / GUNW chain pairs).
MONITOR_MIN_DATES = {"water_change": 2, "flood": 4, "vegetation": 4}
MONITOR_MIN_GUNW_PAIRS = 2
JOBS_DIR = DATA_DIR / "jobs"
MONITORS_FILE = DATA_DIR / "monitors.json"

# --------------------------------------------------------------------------- #
# Flood engine (GCOV time series)
# --------------------------------------------------------------------------- #
FLOOD_RESOLUTION_M = 30.0
# Pixel is "permanent/normal water" if water on ≥ this share of usable passes.
FLOOD_PERMANENT_OCCURRENCE = 0.8
# Flooded vegetation (double bounce): HH rises ≥ this vs the pixel's median,
# in vegetated pixels (median HV ≥ FLOOD_VEG_MIN_HV_DB) that are not urban
# (median HH < FLOOD_URBAN_HH_DB) and whose HV doesn't collapse.
FLOOD_VEG_HH_RISE_DB = 3.0
FLOOD_VEG_MIN_HV_DB = -18.0
FLOOD_URBAN_HH_DB = -1.0
FLOOD_VEG_MAX_HV_DROP_DB = 2.0
FLOOD_MIN_PATCH_PX = 4  # at 30 m

# --------------------------------------------------------------------------- #
# Vegetation engine (GCOV time series)
# --------------------------------------------------------------------------- #
VEG_RESOLUTION_M = 20.0
VEG_MIN_AMPLITUDE_DB = 3.0  # HV rise needed to call a growing season
# Radar sees canopy growth, which lags sowing; planting ≈ season start − 35…14 days.
VEG_PLANTING_LEAD_DAYS = (14, 35)
VEG_MIN_GOOD_DATES = 6

# --------------------------------------------------------------------------- #
# Deformation engine (GUNW interferograms)
# --------------------------------------------------------------------------- #
# NISAR L-band centre frequency (GUNW frequencyA/centerFrequency = 1.239 GHz).
NISAR_L_WAVELENGTH_M = 299_792_458 / 1.239e9  # ≈ 0.242 m
DEFO_MIN_COHERENCE = 0.3
DEFO_APPLY_IONO = True  # subtract GUNW ionospherePhaseScreen (cut spread 19 → 4 cm in Lagos)
DEFO_MIN_EPOCHS = 4
# Remove a best-fit plane per interferogram. Lagos (Jun–Oct 2026) showed a
# smooth N–S ramp of >150 mm/yr after iono correction — atmosphere, not motion.
# Side effect: genuine AOI-wide tilts are removed too (disclosed).
DEFO_DERAMP = True
# Velocity above this (LOS, away from satellite) is reported as a hotspot.
DEFO_HOTSPOT_MM_YR = 20.0
# Sign: ISCE3 forms ref × conj(sec), so positive unwrapped phase = range
# increase = motion AWAY from the satellite (subsidence-like). Not yet checked
# against GNSS — disclosed in every response.
DEFO_SIGN_CONVENTION = "positive = LOS range increase (motion away from satellite, e.g. subsidence)"

# Known limitations — disclosed in every /process response (Honest Uncertainty Rule).
KNOWN_LIMITATIONS = [
    "Wind-roughened water can be missed (false negatives); see flags.wind_affected.",
    "Wet soil and flooded vegetation are hard to distinguish from open water with backscatter alone.",
    "Radio-frequency interference can corrupt pixels.",
    "NISAR has a documented data gap 2026-07-27 to 2026-08-10.",
    "Rain flags use GPM IMERG (~10 km); they are approximate for small AOIs.",
    "PROVISIONAL/BETA NISAR products are not fully calibrated; VALIDATED products are preferred.",
    "Arid/desert areas produce false water detections (12-17% of desert pixels in TerraPulse tests).",
    "40 MHz mode reads ~1 dB brighter than 20 MHz on dark surfaces; cross-mode comparisons need per-mode calibration.",
    "Smooth man-made surfaces (runways, large paved areas) can look like water.",
]
WATER_LIMITATIONS = KNOWN_LIMITATIONS

FLOOD_LIMITATIONS = KNOWN_LIMITATIONS + [
    "Flooded-vegetation detection (HH double-bounce increase) is experimental; wet canopies after rain mimic it.",
    "'Normal' water is learned from the requested period itself; a flood lasting most of the period "
    "can be mistaken for permanent water. Supply a dry-season baseline range to avoid this.",
    "Rain-affected passes are excluded from the peak, so a flood that coincides with heavy rain may be "
    "under-reported; check per_date for excluded passes.",
]

VEGETATION_LIMITATIONS = [
    "HV backscatter tracks canopy structure/biomass, not crop type; natural grass and shrubs also green up "
    "with the rains — no cropland mask is applied.",
    "Planting dates are inferred from canopy growth, which lags sowing by roughly 2-5 weeks; the planting "
    "window is an estimate.",
    "12-day revisit (24 days across gaps) limits timing precision to roughly ±6-12 days.",
    "Soil moisture and rain raise backscatter (HH more than HV); rain-affected passes are excluded.",
    "Seasons that start before or end after the observed period are reported as partially observed.",
    "Fields smaller than the analysis grid mix with surrounding land.",
    "PROVISIONAL/BETA NISAR products are not fully calibrated; VALIDATED products are preferred.",
    "Radio-frequency interference can corrupt pixels.",
]

DEFORMATION_LIMITATIONS = [
    "Line-of-sight (LOS) motion only — not separated into vertical and horizontal. Combining ascending and "
    "descending tracks would allow this; not yet implemented.",
    "Tropospheric water-vapour delay is NOT corrected; in the humid tropics it adds centimetre-level noise "
    "per interferogram and dominates short time series.",
    "Ionospheric phase is corrected with the GUNW ionosphere phase screen; residual errors remain.",
    "Displacement is relative to the reference area, assumed stable.",
    "A best-fit plane is removed from each interferogram to suppress atmospheric ramps; this also removes "
    "genuine motion that varies smoothly across the whole AOI. Local subsidence bowls are preserved.",
    "Phase-unwrapping errors can add 2π jumps (≈12 cm); only the dominant connected component is used.",
    "Low-coherence areas (dense vegetation, water, rapid change) are masked.",
    "Velocities from less than a year of data are strongly affected by seasonal and atmospheric signals.",
    "Sign convention follows ISCE3 (positive phase = range increase); not yet validated against GNSS.",
    "PROVISIONAL NISAR products are not fully calibrated.",
]


# --------------------------------------------------------------------------- #
# Sites
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Site:
    """A named test site. Geometries are WKT in EPSG:4326 (lon lat)."""

    name: str
    aoi_wkt: str
    description: str = ""
    # Reference areas for calibration & wind flagging (filled in during step 3).
    water_ref_wkt: str | None = None
    dry_ref_wkt: str | None = None
    # Known water look-alikes (runways, large paved areas) masked out as nodata.
    exclude_wkt: str | None = None
    # Area assumed not to move (deformation reference). Interferograms are
    # referenced to its median; without it, motion is relative to the AOI median.
    stable_ref_wkt: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def arid(self) -> bool:
        return any(t in ("arid", "desert") for t in self.tags)


def bbox_wkt(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """Counter-clockwise WKT polygon for a lon/lat bounding box."""
    return (
        f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, {max_lon} {max_lat}, "
        f"{min_lon} {max_lat}, {min_lon} {min_lat}))"
    )


SITES: dict[str, Site] = {
    "lagos": Site(
        name="lagos",
        # Lagos metro + Lagos Lagoon + Lekki corridor. Small enough to keep
        # ingestion subsets tiny, big enough to include permanent water.
        aoi_wkt=bbox_wkt(3.10, 6.38, 3.70, 6.70),
        description="Lagos, Nigeria — lagoon, coastal flooding, dense urban area",
        # Checked on the 2026-10-22 VALIDATED HH/HV scene:
        #  water_ref: central Lagos Lagoon, HH p50 -15.1 dB, p90 -13.4 dB
        #  dry_ref: Ikorodu-north + Alimosho-west uplands (vegetated/peri-urban,
        #  never flooded), HH p50 ~-4.7 dB, 0% water-like pixels.
        water_ref_wkt=bbox_wkt(3.46, 6.49, 3.54, 6.54),
        dry_ref_wkt=(
            "MULTIPOLYGON(((3.48 6.64, 3.56 6.64, 3.56 6.69, 3.48 6.69, 3.48 6.64)),"
            "((3.15 6.60, 3.22 6.60, 3.22 6.66, 3.15 6.66, 3.15 6.60)))"
        ),
        # Murtala Muhammed Airport runways — smooth tarmac reads as water.
        exclude_wkt=bbox_wkt(3.305, 6.545, 3.345, 6.605),
        # Inland uplands (same as dry_ref): older, consolidated ground, assumed
        # stable relative to the reclaimed coastal strip. An assumption, not GNSS-verified.
        stable_ref_wkt=(
            "MULTIPOLYGON(((3.48 6.64, 3.56 6.64, 3.56 6.69, 3.48 6.69, 3.48 6.64)),"
            "((3.15 6.60, 3.22 6.60, 3.22 6.66, 3.15 6.66, 3.15 6.60)))"
        ),
        tags=("coastal", "urban", "tropical"),
    ),
    "kano": Site(
        name="kano",
        # Rain-fed cereal belt south of Kano (Sudan savanna). Single season:
        # planting Jun–Jul, peak Aug–Sep, harvest Oct–Nov. NISAR track 58
        # (ascending, 20 MHz) has ~12-day coverage from 2026-06-18.
        aoi_wkt=bbox_wkt(8.40, 11.60, 8.55, 11.73),
        description="Kano south, Nigeria — rain-fed cropland (vegetation / planting season test site)",
        tags=("cropland", "semi_arid", "savanna"),
    ),
}


def get_site(name: str) -> Site:
    try:
        return SITES[name.lower()]
    except KeyError as exc:
        known = ", ".join(sorted(SITES))
        raise KeyError(f"Unknown site '{name}'. Known sites: {known}") from exc

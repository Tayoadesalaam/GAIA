"""Uncertainty flags and confidence (CLAUDE.md → Uncertainty Handling).

Everything here is pure functions over diagnostics produced by processing,
discovery and :mod:`gaia.rain`, so the rules are easy to test and to read.
Unknown is reported as ``None`` — never silently as ``False``.
"""

from __future__ import annotations

from typing import Any, Sequence

from gaia import config

LEVELS = ("low", "medium", "high")


class Confidence:
    """Starts at "high"; every ``cap`` lowers the ceiling and records why."""

    def __init__(self, start: str = "high") -> None:
        self._level = LEVELS.index(start)
        self.reasons: list[str] = []

    def cap(self, to: str, why: str) -> None:
        self._level = min(self._level, LEVELS.index(to))
        self.reasons.append(why)

    def note(self, why: str) -> None:
        self.reasons.append(why)

    @property
    def level(self) -> str:
        return LEVELS[self._level]


def wind_affected(water_ref_fraction: float | None,
                  min_fraction: float = config.WIND_MIN_WATER_REF_FRACTION) -> bool | None:
    """A pass is wind-affected if too little of the permanent water reference
    classifies as water (TerraPulse: Keenjhar Lake read 13% water on a windy day)."""
    if water_ref_fraction is None:
        return None
    return bool(water_ref_fraction < min_fraction)


def least_mature(maturities: Sequence[str | None]) -> str:
    rank = {m: i for i, m in enumerate(config.MATURITY_ORDER)}
    known = [m for m in maturities if m in rank]
    if not known:
        return "UNKNOWN"
    return min(known, key=rank.__getitem__)


def radar_mode_label(modes: Sequence[str | None]) -> str | None:
    distinct = sorted({m for m in modes if m})
    if not distinct:
        return None
    return distinct[0] if len(distinct) == 1 else "mixed:" + "/".join(distinct)


def _any(values: Sequence[bool | None]) -> bool | None:
    """True if any True; None if none True but some unknown; else False."""
    if any(v is True for v in values):
        return True
    if any(v is None for v in values):
        return None
    return False


def per_date_flags(per_date: Sequence[dict[str, Any]],
                   rain: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach rain/wind flags to each date; mark whether it may enter aggregates."""
    out = []
    for d in per_date:
        r = rain.get(d["date"], {})
        wind = wind_affected(d.get("water_ref_fraction"))
        rain_aff = r.get("rain_affected")
        out.append({
            **d,
            "rain_affected": rain_aff,
            "rain_mm_3day": r.get("rain_mm"),
            "rain_window": r.get("window"),
            "rain_error": r.get("error"),
            "wind_affected": wind,
            # Flagged passes are excluded from peak/aggregate statistics.
            "aggregate_eligible": rain_aff is not True and wind is not True,
        })
    return out


def build_flags(dates: Sequence[dict[str, Any]], *, arid: bool | None,
                tracks: Sequence[int | None]) -> dict[str, Any]:
    return {
        "rain_affected": _any([d["rain_affected"] for d in dates]),
        "wind_affected": _any([d["wind_affected"] for d in dates]),
        "radar_mode": radar_mode_label([d.get("radar_mode") for d in dates]),
        "maturity": least_mature([d.get("maturity") for d in dates]),
        "cross_mode": len({d.get("radar_mode") for d in dates}) > 1,
        "cross_track": len(set(tracks)) > 1,
        "arid": arid,
    }


def water_detectable(calibration: Sequence[dict[str, Any]]) -> bool | None:
    """False if any calibration found land as dark as water; None if unknown."""
    vals = [c.get("water_detectable") for c in calibration]
    if any(v is False for v in vals):
        return False
    return True if any(v is True for v in vals) or any(
        c["method"] == "per_date_classification" for c in calibration) else None


def calibration_issues(calibration: Sequence[dict[str, Any]]) -> list[tuple[str | None, str]]:
    """(cap level or None for a plain note, reason) for each calibration."""
    out: list[tuple[str | None, str]] = []
    for c in calibration:
        mode = c.get("radar_mode")
        if c["method"] == "default_uncalibrated":
            out.append(("low", "Thresholds are fixed defaults, not calibrated for this AOI and radar mode."))
        elif c["method"] == "dry_only_calibration":
            if c.get("water_detectable") is False:
                out.append(("low", f"Land in this area is as dark as open water ({mode}; typical of dry "
                                   "sand) — water cannot be reliably detected here."))
            else:
                out.append(("medium", f"No permanent water to calibrate against ({mode}): false alarms "
                                      f"{(c.get('false_alarm_rate') or 0):.1%} on known land, hit rate unknown."))
        else:
            hit, fa = c.get("hit_rate") or 0.0, c.get("false_alarm_rate") or 0.0
            if hit < 0.6 or fa > 0.05:
                out.append(("low", f"Weak calibration ({mode}): hit rate {hit:.0%}, false alarms {fa:.1%}."))
            elif hit < config.GOOD_HIT_RATE or fa > config.GOOD_FALSE_ALARM:
                out.append(("medium", f"Moderate calibration ({mode}): hit rate {hit:.0%}, "
                                      f"false alarms {fa:.1%}."))
        if c.get("note") and c["method"] != "dry_only_calibration":
            out.append((None, f"Calibration note: {c['note']}"))
    return out


def assess_confidence(flags: dict[str, Any], calibration: Sequence[dict[str, Any]],
                      stats: dict[str, Any]) -> tuple[str, list[str]]:
    """Rule-based confidence. Returns (level, reasons). Starts at "high" and
    each issue caps it; reasons explain every downgrade."""
    level = 2
    reasons: list[str] = []

    def cap(to: str, why: str) -> None:
        nonlocal level
        level = min(level, LEVELS.index(to))
        reasons.append(why)

    if flags["rain_affected"] is True:
        cap("low", "At least one pass is rain-affected (≥ 15 mm in the 3 days before); wet soil/canopy mimics water.")
    elif flags["rain_affected"] is None:
        cap("medium", "Rainfall could not be checked for every pass.")

    if flags["wind_affected"] is True:
        cap("low", "Permanent water looked rough (wind) on at least one pass; water may be missed.")
    elif flags["wind_affected"] is None:
        cap("medium", "No permanent-water reference in the AOI, so wind roughening could not be checked.")

    if flags["cross_track"]:
        cap("low", "Dates come from different tracks (different look geometry).")
    if flags["maturity"] != "VALIDATED":
        cap("medium", f"Least mature product is {flags['maturity']}, not fully calibrated.")
    if flags["arid"] is True:
        cap("medium", "Arid AOI: desert surfaces produce false water detections.")

    for lvl, why in calibration_issues(calibration):
        if lvl:
            cap(lvl, why)
        else:
            reasons.append(why)

    valid = stats.get("aoi_valid_fraction")
    if valid is not None and valid < 0.5:
        cap("low", f"Only {valid:.0%} of the AOI has valid data on both dates.")
    elif valid is not None and valid < 0.9:
        cap("medium", f"{valid:.0%} of the AOI has valid data on both dates.")

    noise = stats.get("water_ref_change_fraction")
    if noise is not None and noise > 0.10:
        cap("low", f"{noise:.0%} of permanent water 'changed' between dates — high noise.")
    elif noise is not None and noise > 0.03:
        cap("medium", f"{noise:.1%} of permanent water 'changed' between dates — some noise.")

    return LEVELS[level], reasons


def build_limitations(flags: dict[str, Any], dates: Sequence[dict[str, Any]],
                      known_gaps: Sequence[Sequence[str]],
                      standing: Sequence[str] | None = None) -> list[str]:
    """Context-specific limitations first, then the standing known list."""
    specific: list[str] = []
    for d in dates:
        if d.get("rain_affected"):
            specific.append(f"{d['date']}: rain-affected pass ({d['rain_mm_3day']} mm over "
                            f"{d['rain_window'][0]}…{d['rain_window'][1]}); excluded from aggregates.")
        if d.get("wind_affected"):
            specific.append(f"{d['date']}: wind-roughened water; only "
                            f"{d['water_ref_fraction']:.0%} of the permanent water reference read as water.")
        if d.get("rain_error"):
            specific.append(f"{d['date']}: rainfall unavailable ({d['rain_error']}).")
    for a, b in known_gaps:
        specific.append(f"NISAR data gap {a} to {b} overlaps the requested period.")
    if flags.get("cross_mode"):
        specific.append("Dates use different radar modes; thresholds were calibrated per mode.")
    return specific + list(config.KNOWN_LIMITATIONS if standing is None else standing)


__all__ = [
    "assess_confidence",
    "build_flags",
    "build_limitations",
    "least_mature",
    "per_date_flags",
    "radar_mode_label",
    "wind_affected",
]

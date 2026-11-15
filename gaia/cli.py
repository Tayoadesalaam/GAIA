"""Manual pipeline runs: ``python -m gaia <stage> ...``

Stages available now:
    discover   Layer 1 — list NISAR GCOV acquisitions for a site or AOI
    ingest     Layer 2 — extract AOI GeoTIFFs (HH, HV) from one granule
    process    Layers 1–3 + flags — water change between two dates
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from gaia import config


def _fmt_size(n: int | None) -> str:
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def cmd_discover(args: argparse.Namespace) -> int:
    from gaia.discovery import coverage_summary, granules_to_geojson, search_granules

    if args.aoi:
        aoi = Path(args.aoi).read_text() if Path(args.aoi).exists() else args.aoi
        label = "custom AOI"
    else:
        site = config.get_site(args.site)
        aoi, label = site.aoi_wkt, f"{site.name} — {site.description}"

    end = args.end or date.today().isoformat()
    start = args.start or (date.fromisoformat(end) - timedelta(days=365)).isoformat()

    granules = search_granules(
        aoi,
        start,
        end,
        maturity=args.maturity,
        radar_mode=args.radar_mode,
        min_overlap=args.min_overlap,
    )
    summary = coverage_summary(granules, start, end)

    if args.geojson:
        Path(args.geojson).write_text(json.dumps(granules_to_geojson(granules), indent=2))

    if args.json:
        print(json.dumps({"coverage": summary, "granules": [g.to_dict() for g in granules]},
                         indent=2, default=str))
        return 0

    print(f"\nGAIA discovery · NISAR L2 GCOV · {label}")
    print(f"Range {start} → {end} · {len(granules)} acquisition(s)\n")
    if granules:
        header = f"{'date':<11} {'track':>5} {'frame':>5} {'dir':<4} {'mode':<6} {'pols':<12} {'maturity':<12} {'aoi%':>5} {'size':>7}  granule"
        print(header)
        print("-" * len(header))
        for g in granules:
            pct = f"{g.aoi_overlap * 100:.0f}" if g.aoi_overlap is not None else "-"
            print(
                f"{g.date.isoformat():<11} {g.track or '-':>5} {g.frame or '-':>5} "
                f"{(g.flight_direction or '-')[:4]:<4} {g.radar_mode or '-':<6} "
                f"{','.join(g.polarizations) or '-':<12} {g.maturity:<12} {pct:>5} "
                f"{_fmt_size(g.size_bytes):>7}  {g.granule_id}"
            )

    print("\nCoverage")
    print(f"  by maturity:   {summary['by_maturity']}")
    print(f"  by radar mode: {summary['by_radar_mode']}")
    print(f"  tracks:        {[(t['track'], t['flight_direction']) for t in summary['tracks']]}")
    if summary["known_gaps"]:
        print(f"  known NISAR gaps in range: {summary['known_gaps']}")
    if summary["observed_gaps"]:
        print(f"  observed gaps (>{config.OBSERVED_GAP_THRESHOLD_DAYS} days): {summary['observed_gaps']}")
    for note in summary["notes"]:
        print(f"  ! {note}")
    if args.geojson:
        print(f"\nFootprints written to {args.geojson}")
    print()
    return 0


def _resolve_aoi(args: argparse.Namespace) -> tuple[str, str]:
    if args.aoi:
        aoi = Path(args.aoi).read_text() if Path(args.aoi).exists() else args.aoi
        return aoi, "custom AOI"
    site = config.get_site(args.site)
    return site.aoi_wkt, site.name


def cmd_ingest(args: argparse.Namespace) -> int:
    from gaia.discovery import search_granules
    from gaia.ingest import IngestError, ingest_granule

    aoi, label = _resolve_aoi(args)

    if args.url:
        targets = [args.url]
    else:
        day = date.fromisoformat(args.date)
        granules = search_granules(aoi, day, day)
        if args.track is not None:
            granules = [g for g in granules if g.track == args.track]
        if not granules:
            print(f"No NISAR GCOV granule over {label} on {day}"
                  + (f" (track {args.track})" if args.track is not None else ""), file=sys.stderr)
            return 1
        targets = granules

    for target in targets:
        name = getattr(target, "granule_id", target)
        print(f"Ingesting {name} → AOI subset ({label}) …", flush=True)
        try:
            r = ingest_granule(target, aoi, args.out, downscale=args.downscale,
                               overwrite=args.overwrite, verbose=args.verbose)
        except IngestError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            return 1
        print(f"  {'(cached) ' if r.cached else ''}{r.date} · {r.crs} · {r.shape} px · "
              f"{r.resolution_m[0]:.0f} m")
        for pol, path in r.paths.items():
            print(f"  {pol}: {path}  (valid in AOI: {r.valid_fraction[pol] * 100:.0f}%)")
        print(f"  metadata: {r.sidecar}")
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    from gaia.ingest import IngestError
    from gaia.pipeline import PipelineError, run_water_change

    aoi = None
    if args.aoi:
        aoi = Path(args.aoi).read_text() if Path(args.aoi).exists() else args.aoi
    try:
        r = run_water_change(args.date1, args.date2, site=None if aoi else args.site, aoi=aoi,
                             track=args.track, use_cache=not args.no_cache)
    except (PipelineError, IngestError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    f, c, s = r["flags"], r["calibration"], r["stats"]
    print(f"\nGAIA water change · {r['dates'][0]} → {r['dates'][1]}{'  (cached)' if r['cached'] else ''}")
    print(f"  change map: {r['change_map_path']}")
    print(f"  quicklook:  {r['quicklook_path']}")
    print(f"  new water {s['new_water_km2']} km² · receded {s['receded_water_km2']} km² · "
          f"excluded {s['excluded_km2']} km²")
    print(f"  calibration [{c['method']}]: HH < {c['threshold_hh_db']} dB, HV < {c['threshold_hv_db']} dB, "
          f"hit {c.get('hit_rate')}, false alarm {c.get('false_alarm_rate')}")
    for d in r["per_date"]:
        print(f"  {d['date']} {d['maturity']:<11} water {d['water_km2']} km² · rain {d['rain_mm_3day']} mm "
              f"→ rain_affected={d['rain_affected']} · water_ref {d['water_ref_fraction']} "
              f"→ wind_affected={d['wind_affected']}")
    print(f"  flags: {f}")
    print(f"  confidence: {r['confidence'].upper()}")
    for why in r["confidence_reasons"]:
        print(f"    - {why}")
    print()
    return 0


def cmd_engine(args: argparse.Namespace) -> int:
    from gaia import pipeline
    from gaia.ingest import IngestError

    aoi = None
    if args.aoi:
        aoi = Path(args.aoi).read_text() if Path(args.aoi).exists() else args.aoi
    kw = dict(site=None if aoi else args.site, aoi=aoi, track=args.track, use_cache=not args.no_cache)
    if args.engine == "flood" and args.baseline:
        kw["baseline"] = args.baseline
    run = {"flood": pipeline.run_flood, "vegetation": pipeline.run_vegetation,
           "deformation": pipeline.run_deformation}[args.engine]
    print(f"Running {args.engine} for {args.start} → {args.end} (first run downloads AOI subsets) …",
          file=sys.stderr, flush=True)
    try:
        r = run(args.start, args.end, **kw)
    except (pipeline.PipelineError, IngestError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(r, indent=2, default=str))
        return 0

    print(f"\nGAIA {args.engine} · {r['dates'][0]} → {r['dates'][-1]} · {len(r['dates'])} dates"
          f"{'  (cached)' if r['cached'] else ''}")
    s = r["stats"]
    if args.engine == "flood":
        print(f"  peak flood {s['peak_date']}: {s['peak_flood_total_km2']} km² "
              f"(open {s['peak_flood_open_km2']}, vegetation {s['peak_flood_vegetation_km2']})")
        print(f"  normal water {s['normal_water_km2']} km² · ever flooded {s['ever_flooded_km2']} km²")
        print(f"  excluded passes: {s['excluded_dates'] or 'none'}")
        for p in r["per_date"]:
            mark = "" if p["aggregate_eligible"] else "  [excluded: " + ", ".join(
                k for k in ("rain_affected", "wind_affected") if p[k]) + "]"
            print(f"    {p['date']}  flood {p['flood_total_km2']:>8} km²  rain {p['rain_mm_3day']} mm{mark}")
    elif args.engine == "vegetation":
        se = r["season"]
        print(f"  seasonal vegetation: {se['active_area_km2']} km² · median HV rise {se['median_amplitude_db']} dB")
        print(f"  season start {se['season_start']} (IQR {se['season_start_iqr']})")
        print(f"  peak         {se['peak']} (IQR {se['peak_iqr']})")
        print(f"  season end   {se['season_end']} (IQR {se['season_end_iqr']})")
        print(f"  estimated planting window: {se['estimated_planting_window']}")
        print(f"  classes (fraction): {s['class_fraction']}")
    else:
        print(f"  {s['interferograms']} interferograms over {s['span_days']} days · track {r['coverage']['track_used']}")
        print(f"  LOS velocity percentiles (mm/yr): {s['velocity_mm_yr_percentiles']}")
        print(f"  noise ~{s['velocity_noise_mm_yr']} mm/yr · away > {s['hotspot_threshold_mm_yr']} mm/yr: "
              f"{s['away_from_satellite_km2']} km² · toward: {s['toward_satellite_km2']} km²")
        print(f"  reference: {s['reference']} · sign: {r['method']['sign_convention']}")
    for k, v in r["outputs"].items():
        print(f"  {k}: {v}")
    print(f"  confidence: {r['confidence'].upper()}")
    for why in r["confidence_reasons"]:
        print(f"    - {why}")
    print()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from gaia import monitor as mon
    from gaia.geocode import LocationError, resolve_location

    try:
        loc = resolve_location(args.location)
    except LocationError as exc:
        print(f"✗ Location not found: {exc}", file=sys.stderr)
        return 2
    print(f"\nLocation: {loc.name}  [{loc.source}]  ~{loc.area_km2:,.0f} km²  (centre {loc.lat:.4f}, {loc.lon:.4f})")
    for n in loc.notes:
        print(f"  · {n}")

    avail = mon.check_availability(loc.aoi_wkt, args.start, args.end)
    g, u = avail["gcov"], avail["gunw"]
    print(f"\nNISAR data {avail['period'][0]} → {avail['period'][1]}")
    print(f"  GCOV: {g['granules_found']} granules · best track {g['best_track']} "
          f"({len(g['best_track_dates'])} dual-pol dates) · AOI cover {g['best_track_aoi_cover']}")
    print(f"  GUNW: {u['pairs_found']} interferograms · longest chain {u['chain_length']} on track {u['best_track']}")
    for name, e in avail["engines"].items():
        print(f"  {'✓' if e['available'] else '✗'} {name:<13} {e['reason']}")
    for n in avail["notes"]:
        print(f"  ! {n}")

    try:
        engines = mon.require_data(loc, avail, args.engines)
    except mon.NoDataError as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        for s in exc.detail["suggestions"]:
            print(f"    - {s}", file=sys.stderr)
        return 3
    if args.check_only:
        print(f"\nData available for: {', '.join(engines)}. (--check-only: nothing run)\n")
        return 0

    print(f"\nRunning {', '.join(engines)} … (first run downloads AOI subsets; this can take a while)\n",
          flush=True)
    out = mon.create_monitor(args.location, start=args.start, end=args.end, engines=args.engines, wait=True)
    job = out["job"]
    print(f"Monitor {out['monitor']['id']} · job {job['id']} · {job['status'].upper()}")
    for name, info in job["engines"].items():
        if info["status"] == "done":
            r = job["results"][name]
            print(f"  ✓ {name:<13} confidence {r['confidence'].upper():<6} → {next(iter(r['outputs'].values()), '')}")
        elif info["status"] == "skipped":
            print(f"  – {name:<13} skipped: {info['reason']}")
        else:
            print(f"  ✗ {name:<13} failed: {info.get('error')}")
    print(f"\nRe-check later for new passes:  python -m gaia refresh {out['monitor']['id']}\n")
    return 0 if job["status"] in ("done", "partial") else 1


def cmd_refresh(args: argparse.Namespace) -> int:
    from gaia import monitor as mon

    try:
        out = mon.refresh_monitor(args.monitor_id, wait=True)
    except KeyError:
        print(f"✗ Unknown monitor {args.monitor_id}", file=sys.stderr)
        return 2
    if out["status"] == "up_to_date":
        print(out["message"])
        return 0
    print(f"New data ({out['previous_acquisition']} → {out['latest_acquisition']}); job "
          f"{out['job']['id']} {out['job']['status']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gaia", description="GAIA backend pipeline")
    sub = parser.add_subparsers(dest="stage", required=True)

    d = sub.add_parser("discover", help="Layer 1: list NISAR GCOV granules")
    where = d.add_mutually_exclusive_group()
    where.add_argument("--site", default="lagos", help=f"named site ({', '.join(config.SITES)})")
    where.add_argument("--aoi", help="WKT / GeoJSON string, or path to a .geojson/.wkt file")
    d.add_argument("--start", help="YYYY-MM-DD (default: one year before --end)")
    d.add_argument("--end", help="YYYY-MM-DD (default: today)")
    d.add_argument("--maturity", choices=config.MATURITY_ORDER, help="only this maturity")
    d.add_argument("--radar-mode", help='e.g. "20MHz"')
    d.add_argument("--min-overlap", type=float, default=0.0, help="min fraction of AOI covered (0–1)")
    d.add_argument("--json", action="store_true", help="print JSON instead of a table")
    d.add_argument("--geojson", help="write granule footprints to this .geojson path")
    d.set_defaults(func=cmd_discover)

    i = sub.add_parser("ingest", help="Layer 2: extract AOI GeoTIFFs (HH, HV) from a granule")
    where = i.add_mutually_exclusive_group()
    where.add_argument("--site", default="lagos", help=f"named site ({', '.join(config.SITES)})")
    where.add_argument("--aoi", help="WKT / GeoJSON string, or path to a .geojson/.wkt file")
    pick = i.add_mutually_exclusive_group(required=True)
    pick.add_argument("--date", help="acquisition date YYYY-MM-DD (uses discovery)")
    pick.add_argument("--url", help="direct GCOV .h5 URL")
    i.add_argument("--track", type=int, help="with --date: only this track")
    i.add_argument("--out", help="output dir (default data/ingest)")
    i.add_argument("--downscale", type=int, help="integer block-average factor")
    i.add_argument("--overwrite", action="store_true", help="re-run even if outputs exist")
    i.add_argument("-v", "--verbose", action="store_true", help="stream openSEPPO output")
    i.set_defaults(func=cmd_ingest)

    p = sub.add_parser("process", help="Full pipeline: water change between two dates")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--site", default="lagos", help=f"named site ({', '.join(config.SITES)})")
    where.add_argument("--aoi", help="WKT / GeoJSON string, or path to a .geojson/.wkt file")
    p.add_argument("--date1", required=True, help="earlier date YYYY-MM-DD")
    p.add_argument("--date2", required=True, help="later date YYYY-MM-DD")
    p.add_argument("--track", type=int, help="restrict to one track")
    p.add_argument("--no-cache", action="store_true", help="recompute even if a result exists")
    p.add_argument("--json", action="store_true", help="print the full API response JSON")
    p.set_defaults(func=cmd_process)

    for name, help_ in (("flood", "Flood extent per pass, peak flood and normal water over a period"),
                        ("vegetation", "Growing season and estimated planting window over a period"),
                        ("deformation", "LOS ground motion (GUNW interferograms) over a period")):
        e = sub.add_parser(name, help=help_)
        where = e.add_mutually_exclusive_group()
        where.add_argument("--site", default="lagos", help=f"named site ({', '.join(config.SITES)})")
        where.add_argument("--aoi", help="WKT / GeoJSON string, or path to a .geojson/.wkt file")
        e.add_argument("--start", required=True, help="period start YYYY-MM-DD")
        e.add_argument("--end", required=True, help="period end YYYY-MM-DD")
        e.add_argument("--track", type=int, help="restrict to one track")
        if name == "flood":
            e.add_argument("--baseline", nargs=2, metavar=("START", "END"),
                           help="dry-season baseline period for normal water")
        e.add_argument("--no-cache", action="store_true", help="recompute even if a result exists")
        e.add_argument("--json", action="store_true", help="print the full API response JSON")
        e.set_defaults(func=cmd_engine, engine=name)

    m = sub.add_parser("monitor", help="Enter a location: check NISAR data, then monitor changes")
    m.add_argument("location", help="place name, 'lat, lon', bbox, WKT/GeoJSON or site name")
    m.add_argument("--start", help="period start (default: one year before end)")
    m.add_argument("--end", help="period end (default: today)")
    m.add_argument("--engines", nargs="+", choices=["water_change", "flood", "vegetation", "deformation"])
    m.add_argument("--check-only", action="store_true", help="only report data availability")
    m.set_defaults(func=cmd_monitor)

    r = sub.add_parser("refresh", help="Re-check a monitor for new acquisitions")
    r.add_argument("monitor_id")
    r.set_defaults(func=cmd_refresh)

    s = sub.add_parser("store", help="Where GAIA keeps monitors, jobs, results and maps (local or Supabase)")
    s.add_argument("action", choices=["check", "import-local"],
                   help="check: round-trip every table and the bucket; import-local: copy data/ into Supabase")
    s.add_argument("--no-files", action="store_true", help="import-local: skip uploading output maps")
    s.set_defaults(func=cmd_store)

    return parser


def cmd_store(args: argparse.Namespace) -> int:
    from gaia import store

    try:
        if args.action == "check":
            return 0 if store.check() else 1
        store.import_local(upload_files=not args.no_files)
        return 0
    except store.StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _utf8_stdio() -> None:
    """Windows consoles default to cp1252, which can't print → · — etc."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pytest capture / non-text streams
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

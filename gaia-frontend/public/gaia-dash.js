/* GAIA dashboard panels, rendered from the real engine results held by gaia-map.js.
 *
 * Every number on the dashboard comes from the backend: availability (/availability) before a run,
 * job results (/jobs/{id}) after. Each tab shows the panels relevant to it:
 *   overview    · area + coverage, confidence per engine, rain before passes, key findings, flags
 *   deformation · ground motion, method, velocity spread, cumulative LOS chart, interferograms
 *   vegetation  · growing season, season classes, planting calendar, HV time series
 *   flooding    · flood peak, water change, largest flood passes, calibration, flood-per-pass chart
 *   data        · coverage per engine, every pass with flags, downloads, sources
 */
(function () {
  'use strict';
  var GM = window.GAIA_MAP; if (!GM) return;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var MN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var NAME = { water_change: 'Water change', flood: 'Flooding', vegetation: 'Vegetation', deformation: 'Deformation' };
  var TAB_ENGINES = { deformation: ['deformation'], vegetation: ['vegetation'], flooding: ['flood', 'water_change'] };
  var TAB_OVERLAY = { deformation: ['deformation', 'velocity_mm_yr'], vegetation: ['vegetation', 'season_class'],
                      flooding: [['flood', 'peak_flood_map'], ['water_change', 'change_map']] };
  var D = { tab: 'overview', view: null, answer: null, key: '' };

  /* ---------------------------------------------------------------- helpers */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function num(v, d) { return v == null || isNaN(v) ? '—' : (+v).toLocaleString(undefined, { maximumFractionDigits: d == null ? 1 : d, minimumFractionDigits: 0 }); }
  function signed(v, d) { return v == null || isNaN(v) ? '—' : (v > 0 ? '+' : v < 0 ? '−' : '') + num(Math.abs(v), d); }
  function pct(v, d) { return v == null || isNaN(v) ? '—' : num(v * 100, d == null ? 0 : d) + '%'; }
  function day(s) { if (!s) return '—'; var d = new Date(s + 'T00:00:00Z'); return d.getUTCDate() + ' ' + MN[d.getUTCMonth()]; }
  function dayY(s) { if (!s) return '—'; var d = new Date(s + 'T00:00:00Z'); return d.getUTCDate() + ' ' + MN[d.getUTCMonth()] + ' ' + d.getUTCFullYear(); }
  function t(s) { return Date.parse(s + 'T00:00:00Z'); }
  function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }
  function panel(title, tag, body, cls) { return '<div class="glass panel' + (cls ? ' ' + cls : '') + '"><div class="ph-h"><h4>' + title + '</h4>' + (tag ? '<span class="tag">' + tag + '</span>' : '') + '</div>' + body + '</div>'; }
  function kv(pairs) { return '<div class="kv">' + pairs.map(function (p) { return '<div' + (p[2] ? ' title="' + esc(p[2]) + '"' : '') + '><small>' + p[0] + '</small><b>' + p[1] + '</b></div>'; }).join('') + '</div>'; }
  function bars(rows, max) {  // rows: [label, value, text, on]
    max = max || Math.max.apply(0, rows.map(function (r) { return Math.abs(r[1]) || 0; }).concat(1e-9));
    return '<div class="bars">' + rows.map(function (r) {
      return '<div class="bar' + (r[3] ? ' on' : '') + '"><span style="text-align:left">' + r[0] + '</span><i><u style="width:' + Math.max(2, Math.round(Math.abs(r[1] || 0) / max * 100)) + '%"></u></i><span>' + r[2] + '</span></div>';
    }).join('') + '</div>';
  }
  function confTag(c) { return c ? '<span class="tag c-' + esc(c) + '">' + esc(c) + ' confidence</span>' : ''; }
  function empty(text, sub) { return '<div class="empty"><b>' + esc(text) + '</b>' + (sub ? '<span>' + esc(sub) + '</span>' : '') + '</div>'; }

  function st() { return GM.state; }
  function res() { var j = st().job; return (j && j.results) || {}; }
  function engineState(k) {  // why an engine has no result yet
    var S = st(), j = S.job, je = j && j.engines[k], av = S.avail && S.avail.engines[k];
    if (!S.sel) return ['Select an area on the map', 'Draw a rectangle, click a point, or search a place.'];
    if (je) {
      if (je.status === 'failed') return [NAME[k] + ' failed', je.error || ''];
      if (je.status === 'cancelled') return [NAME[k] + ' was cancelled', 'Start a new monitor to run it.'];
      if (je.status === 'skipped') return [NAME[k] + ' was skipped', je.reason || ''];
      if (je.status === 'running') return [NAME[k] + ' is running…', 'Streaming NISAR subsets from NASA ASF. This can take several minutes.'];
      return [NAME[k] + ' is queued', 'It runs after the engines before it.'];
    }
    if (j) return [NAME[k] + ' was not part of this monitor', 'Press “New area” on the map and select the area again to include it.'];
    if (S.busy === 'check') return ['Checking NISAR data…', ''];
    if (av) return av.available ? [NAME[k] + ' has data here', av.reason + ' · press Monitor in the map panel.'] : ['Not enough data for ' + NAME[k].toLowerCase(), av.reason];
    return ['Not analysed yet', 'Press “Check data” on the map.'];
  }
  function statusLabel(k) {
    var S = st(), je = S.job && S.job.engines[k], av = S.avail && S.avail.engines[k];
    return je ? je.status : S.job ? 'not run' : av ? (av.available ? 'available' : 'no data') : 'not checked';
  }
  function need(k) { var r = res()[k]; return r ? null : empty.apply(null, engineState(k)); }

  /* ---------------------------------------------------------------- chart */
  // spec: {type:'line'|'bar', pts:[{x:'YYYY-MM-DD', v, dim, tip}], unit, dec, zero, ref:{v,label}, shade:[{a,b,label,strong}], trend:[[x,v],[x,v]]}
  function drawChart(svg, spec) {
    var NS = 'http://www.w3.org/2000/svg', w = Math.max(280, svg.clientWidth || 600), h = 230, pl = 44, pr = 14, pt = 14, pb = 26;
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h); svg.innerHTML = '';
    var mk = function (tag, at, txt) { var e = document.createElementNS(NS, tag); for (var k in at) e.setAttribute(k, at[k]); if (txt != null) e.textContent = txt; svg.appendChild(e); return e; };
    var pts = spec.pts.filter(function (p) { return p.v != null && !isNaN(p.v); });
    if (!pts.length) { mk('text', { x: w / 2, y: h / 2, 'text-anchor': 'middle' }, 'No values to plot'); return; }
    var xs = pts.map(function (p) { return t(p.x); }), vs = pts.map(function (p) { return p.v; });
    var x0 = Math.min.apply(0, xs), x1 = Math.max.apply(0, xs);
    (spec.shade || []).forEach(function (s) { x0 = Math.min(x0, t(s.a)); x1 = Math.max(x1, t(s.b)); });
    if (x1 === x0) { x0 -= 6 * 864e5; x1 += 6 * 864e5; }
    var span = x1 - x0, bw = 0;
    if (spec.type === 'bar') { var gaps = xs.slice(1).map(function (x, i) { return x - xs[i]; }).filter(function (g) { return g > 0; }); bw = Math.min(26, Math.max(5, (gaps.length ? Math.min.apply(0, gaps) : span) / span * (w - pl - pr) * .62)); x0 -= span * .04; x1 += span * .04; span = x1 - x0; }
    var lo = Math.min.apply(0, vs.concat(spec.zero || spec.type === 'bar' ? [0] : [])), hi = Math.max.apply(0, vs.concat(spec.ref ? [spec.ref.v] : []).concat(spec.zero ? [0] : []));
    if (spec.trend) spec.trend.forEach(function (q) { lo = Math.min(lo, q[1]); hi = Math.max(hi, q[1]); });
    var pad = (hi - lo || Math.abs(hi) || 1) * .1; if (!(spec.type === 'bar' && lo === 0)) lo -= pad; hi += pad;
    var X = function (x) { return pl + (w - pl - pr) * (x - x0) / span; }, Y = function (v) { return pt + (h - pt - pb) * (1 - (v - lo) / (hi - lo)); };
    (spec.shade || []).forEach(function (s) {
      var a = X(t(s.a)), b = X(t(s.b));
      mk('rect', { x: a, y: pt, width: Math.max(2, b - a), height: h - pt - pb, fill: s.strong ? 'rgba(255,255,255,.10)' : 'rgba(255,255,255,.05)' });
      mk('text', { x: a + 4, y: pt + 11, style: 'fill:rgba(243,243,241,.75)' }, s.label);
    });
    var rng = Math.abs(hi - lo), ad = rng < 0.1 ? 3 : rng < 1 ? 2 : rng < 5 ? 1 : 0;
    for (var i = 0; i <= 4; i++) { var v = lo + (hi - lo) * i / 4, y = Y(v); mk('line', { x1: pl, x2: w - pr, y1: y, y2: y, stroke: 'rgba(255,255,255,.08)' }); mk('text', { x: pl - 6, y: y + 3, 'text-anchor': 'end' }, num(v, ad)); }
    if (lo < 0 && hi > 0) mk('line', { x1: pl, x2: w - pr, y1: Y(0), y2: Y(0), stroke: 'rgba(255,255,255,.3)' });
    var step = Math.max(1, Math.ceil(pts.length / Math.max(2, Math.floor((w - pl - pr) / 70))));
    pts.forEach(function (p, j) { if (j % step === 0 || j === pts.length - 1 && (pts.length - 1) % step > step / 2) mk('text', { x: X(t(p.x)), y: h - 8, 'text-anchor': 'middle' }, day(p.x)); });
    if (spec.ref && spec.ref.v < hi) { mk('line', { x1: pl, x2: w - pr, y1: Y(spec.ref.v), y2: Y(spec.ref.v), stroke: '#fff', 'stroke-dasharray': '4 4', 'stroke-opacity': .6 }); mk('text', { x: w - pr, y: Y(spec.ref.v) - 5, 'text-anchor': 'end' }, spec.ref.label); }
    if (spec.type === 'bar') {
      var top = Math.max.apply(0, vs);
      pts.forEach(function (p) {
        var x = X(t(p.x)), y = Y(Math.max(0, p.v)), y0 = Y(0), hh = Math.max(1.5, Math.abs(y0 - y));
        mk('rect', { x: x - bw / 2, y: Math.min(y, y0), width: bw, height: hh, rx: Math.min(4, bw / 2), fill: p.dim ? 'url(#hatch)' : p.v === top ? '#fff' : 'rgba(255,255,255,.45)', stroke: p.dim ? 'rgba(255,255,255,.5)' : 'none' });
      });
      var defs = document.createElementNS(NS, 'defs'); defs.innerHTML = '<pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="rgba(255,255,255,.06)"/><line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,.5)" stroke-width="2"/></pattern>'; svg.insertBefore(defs, svg.firstChild);
    } else {
      var d = pts.map(function (p, j) { return (j ? 'L' : 'M') + X(t(p.x)).toFixed(1) + ' ' + Y(p.v).toFixed(1); }).join('');
      mk('path', { d: d + 'L' + X(t(pts[pts.length - 1].x)) + ' ' + Y(lo) + 'L' + X(t(pts[0].x)) + ' ' + Y(lo) + 'Z', fill: 'rgba(255,255,255,.06)' });
      if (spec.trend) mk('line', { x1: X(t(spec.trend[0][0])), y1: Y(spec.trend[0][1]), x2: X(t(spec.trend[1][0])), y2: Y(spec.trend[1][1]), stroke: '#fff', 'stroke-dasharray': '5 4', 'stroke-opacity': .55 });
      mk('path', { d: d, fill: 'none', stroke: '#fff', 'stroke-width': 2, 'stroke-linejoin': 'round' });
      pts.forEach(function (p) { mk('circle', { cx: X(t(p.x)), cy: Y(p.v), r: 4, fill: p.dim ? '#050505' : '#fff', stroke: '#fff', 'stroke-width': 2 }); });
    }
    // hover: crosshair + tooltip on the nearest point
    var cross = mk('line', { y1: pt, y2: h - pb, stroke: 'rgba(255,255,255,.5)', visibility: 'hidden' });
    var hit = mk('rect', { x: pl, y: 0, width: w - pl - pr, height: h, fill: 'transparent' });
    var tip = svg.parentNode.querySelector('.ctip');
    hit.addEventListener('pointermove', function (e) {
      var r = svg.getBoundingClientRect(), mx = (e.clientX - r.left) * w / r.width, best = 0;
      pts.forEach(function (p, j) { if (Math.abs(X(t(p.x)) - mx) < Math.abs(X(t(pts[best].x)) - mx)) best = j; });
      var p = pts[best], cx = X(t(p.x));
      cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
      tip.innerHTML = '<b>' + dayY(p.x) + '</b>' + (p.tip || num(p.v, spec.dec) + ' ' + esc(spec.unit || ''));
      tip.hidden = false;
      var left = cx / w * r.width + svg.offsetLeft, top = Y(p.v) / h * r.height + svg.offsetTop;
      tip.style.left = Math.min(left + 12, svg.offsetLeft + r.width - tip.offsetWidth) + 'px'; tip.style.top = Math.max(0, top - tip.offsetHeight - 10) + 'px';
    });
    hit.addEventListener('pointerleave', function () { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; });
  }

  /* ---------------------------------------------------------------- engine views (centre card) */
  function viewFlood() {
    var r = res().flood, s = r.stats || {}, f = r.flags || {};
    var pts = (r.per_date || []).map(function (p) {
      return { x: p.date, v: p.flood_total_km2, dim: !p.aggregate_eligible, tip: num(p.flood_total_km2, 2) + ' km² flooded<br>' + num(p.flood_open_km2, 2) + ' open · ' + num(p.flood_vegetation_km2, 2) + ' vegetated<br>Rain 3 days before: ' + num(p.rain_mm_3day) + ' mm' + (p.rain_affected ? '<br><em>Rain-affected · excluded from peak</em>' : '') + (p.wind_affected ? '<br><em>Wind-roughened water</em>' : '') };
    });
    var read = f.water_detectable === false ? 'Water cannot be reliably detected here: known dry land is as dark as open water.' :
      s.peak_date ? 'Peak on ' + dayY(s.peak_date) + ': ' + num(s.peak_flood_total_km2, 2) + ' km² flooded beyond normal water (' + num(s.peak_flood_open_km2, 2) + ' open, ' + num(s.peak_flood_vegetation_km2, 2) + ' vegetated). ' + (s.excluded_dates && s.excluded_dates.length ? s.excluded_dates.length + ' rain/wind-flagged passes excluded from the peak.' : '') : 'No usable peak in this period.';
    if (s.flagged_passes_with_larger_flood && s.flagged_passes_with_larger_flood.length) read += ' Flagged passes on ' + s.flagged_passes_with_larger_flood.map(day).join(', ') + ' showed more flooding, but rain makes them unreliable.';
    return {
      kicker: 'Flooding · area beyond normal water', value: num(s.peak_flood_total_km2, 2), unit: 'km² at peak', read: read, conf: r.confidence,
      chart: { type: 'bar', pts: pts, unit: 'km²', dec: 2 },
      legend: '<span><i class="f"></i>Flooded area per pass</span><span><i class="h"></i>Rain/wind-flagged pass (excluded)</span>',
      table: ['Date', 'Flooded km²', 'Open', 'Vegetated', 'Rain 3 d', 'Used'], rows: (r.per_date || []).map(function (p) { return [day(p.date), num(p.flood_total_km2, 2), num(p.flood_open_km2, 2), num(p.flood_vegetation_km2, 2), num(p.rain_mm_3day) + ' mm', p.aggregate_eligible ? 'yes' : (p.rain_affected ? 'no · rain' : p.wind_affected ? 'no · wind' : 'no')]; }),
      src: 'NISAR L2 GCOV · track ' + ((r.coverage || {}).track_used || '—') + ' · ' + esc(f.radar_mode || '') + ' · rain: GPM IMERG Late V07',
    };
  }
  function viewWater() {
    var r = res().water_change, s = r.stats || {}, f = r.flags || {};
    var pts = (r.per_date || []).map(function (p) { return { x: p.date, v: p.water_km2, dim: p.rain_affected || p.wind_affected, tip: num(p.water_km2, 2) + ' km² water<br>Rain 3 days before: ' + num(p.rain_mm_3day) + ' mm' + (p.rain_affected ? '<br><em>Rain-affected</em>' : '') }; });
    return {
      kicker: 'Water change · ' + (r.dates || []).map(dayY).join(' → '), value: signed(s.new_water_km2, 2), unit: 'km² new water',
      read: f.water_detectable === false ? 'Water cannot be reliably detected here: known dry land is as dark as open water.' :
        num(s.new_water_km2, 2) + ' km² became water and ' + num(s.receded_water_km2, 2) + ' km² dried out between the two passes.' + (s.water_ref_change_fraction != null ? ' ' + pct(s.water_ref_change_fraction, 1) + ' of permanent water also “changed” — a measure of noise.' : ''),
      conf: r.confidence, chart: { type: 'bar', pts: pts, unit: 'km² water', dec: 2 },
      legend: '<span><i class="f"></i>Water area on each date</span>',
      table: ['Date', 'Water km²', 'Maturity', 'Rain 3 d', 'Flags'], rows: (r.per_date || []).map(function (p) { return [day(p.date), num(p.water_km2, 2), cap((p.maturity || '').toLowerCase()), num(p.rain_mm_3day) + ' mm', [p.rain_affected && 'rain', p.wind_affected && 'wind'].filter(Boolean).join(', ') || '—']; }),
      src: 'NISAR L2 GCOV · ' + esc(f.radar_mode || '') + ' · ' + esc((r.calibration || {}).method || ''),
    };
  }
  function season(r) {  // older cached results could report an end before the start; never show that
    var z = Object.assign({}, r.season || {});
    if (z.season_end && ((z.season_start && z.season_end <= z.season_start) || (z.peak && z.season_end <= z.peak))) {
      z.season_end = null; z.season_end_note = z.season_end_note || 'Season end not reported: the end estimate fell before the start/peak (too few pixels completed their season).';
    }
    return z;
  }
  function viewVeg() {
    var r = res().vegetation, z = season(r), ts = r.timeseries || [];
    var pts = ts.map(function (p) { return { x: p.date, v: p.hv_db_median_active, dim: p.rain_affected || !p.eligible, tip: 'HV ' + num(p.hv_db_median_active) + ' dB · HH ' + num(p.hh_db_median_active) + ' dB<br>RVI ' + num(p.rvi_median_active, 2) + ' · rain 3 d ' + num(p.rain_mm_3day) + ' mm' + (p.rain_affected ? '<br><em>Rain-affected</em>' : '') }; });
    var shade = [];
    if (z.estimated_planting_window) shade.push({ a: z.estimated_planting_window[0], b: z.estimated_planting_window[1], label: 'Plant (est.)' });
    if (z.season_start) shade.push({ a: z.season_start, b: z.season_end || (ts.length ? ts[ts.length - 1].date : z.season_start), label: 'Season', strong: true });
    return {
      kicker: 'Vegetation · growing season', value: z.season_start ? day(z.season_start) : '—', unit: z.season_start ? 'season start' : 'no clear season',
      read: z.season_start ? 'Canopy started growing around ' + dayY(z.season_start) + (z.peak ? ', peaked around ' + dayY(z.peak) : '') + (z.season_end ? ' and ended around ' + dayY(z.season_end) : ', and had not ended by the last pass') + '. Active on ' + num(z.active_area_km2) + ' km²; median HV rise ' + num(z.median_amplitude_db) + ' dB.' :
        'HV backscatter rose by less than 3 dB on almost all of the area: no clear growing season in this period.',
      conf: r.confidence, chart: { type: 'line', pts: pts, unit: 'dB', shade: shade },
      legend: '<span><i></i>HV backscatter, median of active pixels (dB)</span><span><i class="f"></i>Season / estimated planting</span>',
      table: ['Date', 'HV dB', 'HH dB', 'RVI', 'Rain 3 d', 'Used'], rows: ts.map(function (p) { return [day(p.date), num(p.hv_db_median_active), num(p.hh_db_median_active), num(p.rvi_median_active, 2), num(p.rain_mm_3day) + ' mm', p.eligible ? 'yes' : 'no']; }),
      src: 'NISAR L2 GCOV HV · track ' + ((r.coverage || {}).track_used || '—') + ' · 50% amplitude crossings',
    };
  }
  function viewDefo() {
    var r = res().deformation, s = r.stats || {}, p = s.velocity_mm_yr_percentiles || null;
    var series = (s.cumulative_los_series || []);
    var pts = series.map(function (q) { return { x: q.date, v: q.median_los_mm, tip: signed(q.median_los_mm, 1) + ' mm cumulative LOS (median)' }; });
    var trend = null, ok = pts.filter(function (q) { return q.v != null; });
    if (ok.length > 1 && p) { var a = ok[0], b = ok[ok.length - 1], v0 = a.v; trend = [[a.x, v0], [b.x, v0 + p['50'] * (t(b.x) - t(a.x)) / (365.25 * 864e5)]]; }
    return {
      kicker: 'Land deformation · line of sight', value: p ? signed(p['50'], 1) : '—', unit: 'mm / year median',
      read: !p ? 'No coherent pixels: the radar signal decorrelated between passes (dense vegetation, water or long gaps), so ground motion could not be measured here.' :
        'Median ' + signed(p['50'], 1) + ' mm/yr (positive = moving away from the satellite, e.g. sinking), against noise of ±' + num(s.velocity_noise_mm_yr) + ' mm/yr over ' + num(s.span_days, 0) + ' days. ' + (Math.abs(p['50']) < (s.velocity_noise_mm_yr || 0) ? 'That is within the noise: no credible motion yet.' : 'Larger than the noise.'),
      conf: r.confidence, chart: { type: 'line', pts: pts, unit: 'mm', zero: true, trend: trend, dec: 1 },
      legend: '<span><i></i>Cumulative LOS displacement, median (mm)</span>' + (trend ? '<span><i class="d"></i>Median velocity trend</span>' : ''),
      table: ['Reference', 'Secondary', 'Valid', 'Ionosphere', 'Ramp removed'], rows: (r.interferograms || []).map(function (g) { return [day(g.reference_date), day(g.secondary_date), pct(g.valid_fraction), g.iono_applied === 'True' || g.iono_applied === true ? 'corrected' : 'no', g.ramp_removed_cm != null ? num(g.ramp_removed_cm) + ' cm' : '—']; }),
      src: 'NISAR L2 GUNW (80 m) · ' + num(s.interferograms, 0) + ' interferograms · sign: + away from satellite',
    };
  }
  function viewRain() {
    var P = passes();
    return {
      kicker: 'Rainfall before each radar pass', value: P.filter(function (p) { return p.rain_affected; }).length + ' / ' + P.length, unit: 'passes rain-affected',
      read: 'GPM IMERG rainfall in the 3 days before each pass. At ≥ 15 mm wet soil and canopies can look like water, so those passes are flagged and kept out of peaks.',
      chart: { type: 'bar', pts: P.filter(function (p) { return p.rain_mm_3day != null; }).map(function (p) { return { x: p.date, v: p.rain_mm_3day, dim: p.rain_affected, tip: num(p.rain_mm_3day) + ' mm in 3 days' + (p.rain_affected ? '<br><em>Rain-affected</em>' : '') }; }), unit: 'mm', ref: { v: 15, label: '15 mm flag' } },
      legend: '<span><i class="f"></i>Rain, 3 days before pass (mm)</span><span><i class="h"></i>Rain-affected pass</span><span><i class="d"></i>15 mm threshold</span>',
      table: ['Date', 'Track', 'Maturity', 'Mode', 'Rain 3 d', 'Flags', 'Used by'], rows: P.map(function (p) { return [day(p.date), p.track || '—', cap((p.maturity || '—').toLowerCase()), p.radar_mode || '—', p.rain_mm_3day == null ? '—' : num(p.rain_mm_3day) + ' mm', [p.rain_affected && 'rain', p.wind_affected && 'wind'].filter(Boolean).join(', ') || '—', p.engines.map(function (e) { return NAME[e]; }).join(', ')]; }),
      src: 'GPM IMERG Late Run daily V07 via NASA GES DISC (~10 km cells: approximate)',
    };
  }
  var VIEWS = { flood: viewFlood, water_change: viewWater, vegetation: viewVeg, deformation: viewDefo };

  function passes() {  // every radar pass across engines, merged by date
    var by = {}, R = res();
    function add(date, eng, p) { var o = by[date] || (by[date] = { date: date, engines: [] }); if (o.engines.indexOf(eng) < 0) o.engines.push(eng); ['track', 'maturity', 'radar_mode', 'rain_mm_3day', 'rain_affected', 'wind_affected'].forEach(function (k) { if (o[k] == null && p[k] != null) o[k] = p[k]; }); }
    ['flood', 'water_change', 'vegetation'].forEach(function (k) { ((R[k] || {}).per_date || []).forEach(function (p) { add(p.date, k, p); }); });
    ((R.deformation || {}).interferograms || []).forEach(function (g) { add(g.reference_date, 'deformation', g); add(g.secondary_date, 'deformation', g); });
    return Object.keys(by).sort().map(function (k) { return by[k]; });
  }

  /* ---------------------------------------------------------------- side panels */
  function pArea() {
    var S = st(), A = S.avail, loc = (S.sel && (S.sel.resolved || (S.monitor && S.monitor.location))) || {};
    if (!S.sel) return panel('Area', '', empty('No area selected', 'Draw on the map or search a place.'));
    var g = A && A.gcov, best = g && (g.tracks || []).filter(function (x) { return x.track === g.best_track; })[0];
    var any = ['flood', 'vegetation', 'water_change', 'deformation'].map(function (k) { return res()[k]; }).filter(Boolean)[0];
    var flags = any && any.flags || {};
    return panel('Area', A ? 'NISAR' : (any ? 'From results' : ''),
      '<div class="big-n">' + num(loc.area_km2 != null ? loc.area_km2 : S.sel.km2, 0) + '<small>km²</small></div>' +
      '<p class="pl">' + esc(A ? A.period.map(day).join(' → ') : any ? (any.dates || []).length + ' passes analysed' : '') + '</p>' +
      kv([['Radar passes', g ? g.best_track_dates.length : any ? (any.dates || []).length : '—'],
          ['Track', g && g.best_track != null ? g.best_track + (best ? ' ' + String(best.flight_direction).charAt(0) : '') : ((any || {}).coverage || {}).track_used || '—'],
          ['Interferograms', A ? A.gunw.chain_length : res().deformation ? res().deformation.stats.interferograms : '—'],
          ['Latest pass', A && A.latest_acquisition ? day(A.latest_acquisition) : '—'],
          ['Radar mode', best ? best.radar_mode : flags.radar_mode || '—'],
          ['Maturity', cap((flags.maturity || '—').toLowerCase()), 'PROVISIONAL products are not fully calibrated']]));
  }
  function pConfidence() {
    var R = res(), J = st().job, lvl = { high: 100, medium: 66, low: 33 };
    var ks = ['water_change', 'flood', 'vegetation', 'deformation'], tab = { water_change: 'flooding', flood: 'flooding', vegetation: 'vegetation', deformation: 'deformation' };
    var h = '<div class="rings">' + ks.map(function (k) {
      var r = R[k], je = J && J.engines[k], v = r ? (lvl[r.confidence] || 0) : 0, c = 2 * Math.PI * 26, label = r ? cap(r.confidence) : je ? je.status : '—';
      return '<button class="ring" data-go="' + tab[k] + '"><svg viewBox="0 0 62 62"><circle cx="31" cy="31" r="26" fill="none" stroke="rgba(255,255,255,.12)" stroke-width="5"/>' + (v ? '<circle cx="31" cy="31" r="26" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + (c * (1 - v / 100)).toFixed(1) + '"/>' : '') +
        '<text x="31" y="35" text-anchor="middle" fill="#fff" font-size="10" font-family="JetBrains Mono">' + esc(label.slice(0, 7)) + '</text></svg>' + NAME[k] + '</button>';
    }).join('') + '</div>';
    return panel('Confidence', 'Per engine', h + '<p class="pl" style="margin-top:12px">High, medium or low, with reasons on each tab. Tap a ring to open it.</p>');
  }
  function pRain() {
    var P = passes().filter(function (p) { return p.rain_mm_3day != null; });
    if (!P.length) return panel('Rain before passes', 'GPM IMERG', empty('No rainfall yet', 'Rain is checked for each pass when an engine runs.'));
    var top = P.slice().sort(function (a, b) { return b.rain_mm_3day - a.rain_mm_3day; }).slice(0, 5);
    return panel('Rain before passes', 'GPM IMERG', bars(top.map(function (p) { return [day(p.date), p.rain_mm_3day, num(p.rain_mm_3day, 0) + (p.rain_affected ? ' ⚑' : ''), p.rain_affected]; }), Math.max(15, top[0].rain_mm_3day)) +
      '<p class="pl" style="margin-top:10px">Wettest passes, mm in the 3 days before. ⚑ = ≥ 15 mm, flagged as rain-affected.</p>');
  }
  function pFindings() {
    var R = res(), ks = ['water_change', 'flood', 'vegetation', 'deformation'], tab = { water_change: 'flooding', flood: 'flooding', vegetation: 'vegetation', deformation: 'deformation' };
    var rows = ks.map(function (k) {
      var r = R[k], v = r && VIEWS[k](), why = engineState(k);
      return '<button class="find" data-go="' + tab[k] + '"><span class="find-h">' + NAME[k] + (r ? confTag(r.confidence) : '<span class="tag s-no">' + statusLabel(k) + '</span>') + '</span><span class="find-t">' + esc(v ? v.read : why[1] || '') + '</span></button>';
    }).join('');
    return panel('Key findings', 'From results', '<div class="finds">' + rows + '</div>');
  }
  function pFlags() {
    var R = res(), all = ['flood', 'water_change', 'vegetation', 'deformation'].map(function (k) { return R[k]; }).filter(Boolean);
    if (!all.length) return panel('Uncertainty flags', '', empty('No results yet', 'Flags for rain, wind, maturity and radar mode appear once an engine has run.'));
    var P = passes(), rain = P.filter(function (p) { return p.rain_affected; }).length, wind = P.filter(function (p) { return p.wind_affected; }).length;
    var mat = all.map(function (r) { return (r.flags || {}).maturity; }).filter(Boolean)[0], mode = all.map(function (r) { return (r.flags || {}).radar_mode; }).filter(Boolean)[0];
    var det = all.map(function (r) { return (r.flags || {}).water_detectable; }).filter(function (x) { return x != null; })[0];
    var gaps = all.map(function (r) { return ((r.coverage || {}).gaps || []); }).filter(function (g) { return g.length; })[0] || [];
    var row = function (k, v, bad) { return '<div class="flg' + (bad ? ' bad' : '') + '"><span>' + k + '</span><b>' + (bad ? '⚑ ' : '') + v + '</b></div>'; };
    return panel('Uncertainty flags', 'Honest limits',
      row('Rain-affected passes', rain + ' of ' + P.length, rain > 0) + row('Wind-roughened water', wind ? wind + ' passes' : 'none found', wind > 0) +
      row('Product maturity', cap((mat || '—').toLowerCase()), mat && mat !== 'VALIDATED') + row('Radar mode', mode || '—') +
      (det != null ? row('Water detectable', det ? 'yes' : 'no — dry land looks like water', !det) : '') +
      row('Data gaps', gaps.length ? gaps.map(function (g) { return day(g[0]) + '–' + day(g[1]); }).join(', ') : 'none', gaps.length > 0));
  }
  function pConf(k) {
    var r = res()[k]; if (!r) return '';
    var why = r.confidence_reasons || [], lim = r.limitations || [];
    return panel('Confidence', '<span class="c-' + esc(r.confidence) + '">' + esc(r.confidence) + '</span>',
      '<ul class="why">' + why.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' +
      (lim.length ? '<details><summary>' + lim.length + ' known limitations</summary><ul class="why">' + lim.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></details>' : ''));
  }
  function pAsk() {
    var qs = { overview: [['sure', 'How sure is this?'], ['flood', 'Is it flooding?'], ['plant', 'When to plant?'], ['ground', 'Is ground moving?']],
               deformation: [['ground', 'Is ground moving?'], ['sure', 'How sure is this?']], vegetation: [['plant', 'When to plant?'], ['sure', 'How sure is this?']],
               flooding: [['flood', 'Is it flooding?'], ['sure', 'How sure is this?']], data: [['data', 'What data was used?'], ['sure', 'How sure is this?']] }[D.tab];
    return panel('Ask Gaia', 'From results', '<p class="pl" id="ag">' + esc(D.answer || 'Ask about this area. Answers are built from the analysis results above.') + '</p>' +
      '<div class="seg" style="margin-top:10px">' + qs.map(function (q) { return '<button data-ask="' + q[0] + '">' + q[1] + '</button>'; }).join('') + '</div>' +
      '<button class="dl" data-ai="" style="margin-top:12px;font-size:13px">Chat with Gaia AI →</button>');
  }
  function answer(q) {
    var R = res(), n = st().sel ? '' : ' Select an area on the map first.';
    if (q === 'flood') { if (R.flood) return viewFlood().read + ' Confidence: ' + R.flood.confidence + '.'; if (R.water_change) return viewWater().read + ' Confidence: ' + R.water_change.confidence + '.'; return 'No flood analysis for this area yet.' + n; }
    if (q === 'plant') { if (!R.vegetation) return 'No vegetation analysis for this area yet.' + n; var z = R.vegetation.season || {}; return z.estimated_planting_window ? 'Radar saw the canopy start growing around ' + dayY(z.season_start) + ', so planting most likely happened between ' + dayY(z.estimated_planting_window[0]) + ' and ' + dayY(z.estimated_planting_window[1]) + '. This is an estimate: radar sees the canopy, not sowing. Confidence: ' + R.vegetation.confidence + '.' : viewVeg().read; }
    if (q === 'ground') return R.deformation ? viewDefo().read + ' Confidence: ' + R.deformation.confidence + '.' : 'No deformation analysis for this area yet.' + n;
    if (q === 'data') { var P = passes(); return P.length ? P.length + ' radar passes from ' + dayY(P[0].date) + ' to ' + dayY(P[P.length - 1].date) + ', NISAR L2 GCOV/GUNW from NASA ASF, with GPM IMERG rainfall for each pass. Full list in the table.' : 'No passes analysed yet.' + n; }
    var ks = Object.keys(R); if (!ks.length) return 'Nothing has been analysed yet.' + n;
    return ks.map(function (k) { return NAME[k] + ': ' + R[k].confidence + ' — ' + ((R[k].confidence_reasons || [])[0] || ''); }).join(' · ');
  }

  function pDefoMotion() {
    var r = res().deformation; if (!r) return panel('Ground motion', '', need('deformation'));
    var s = r.stats || {}, p = s.velocity_mm_yr_percentiles;
    return panel('Ground motion', 'LOS', '<div class="big-n">' + (p ? signed(p['50'], 1) : '—') + '<small>mm/yr median</small></div><p class="pl">+ = away from satellite (e.g. sinking)</p>' +
      kv([['Noise', '±' + num(s.velocity_noise_mm_yr) + ' mm/yr'], ['Std error', num(s.median_velocity_stderr_mm_yr) + ' mm/yr'],
          ['Moving away', num(s.away_from_satellite_km2) + ' km²', 'faster than ' + num(s.hotspot_threshold_mm_yr, 0) + ' mm/yr'], ['Moving toward', num(s.toward_satellite_km2) + ' km²', 'faster than ' + num(s.hotspot_threshold_mm_yr, 0) + ' mm/yr']]));
  }
  function pDefoMethod() {
    var r = res().deformation; if (!r) return '';
    var s = r.stats || {}, m = r.method || r.calibration || {};
    return panel('Method', 'GUNW', kv([['Interferograms', num(s.interferograms, 0)], ['Time span', num(s.span_days, 0) + ' days'], ['Coherent area', pct(s.valid_fraction)], ['Min coherence', num(m.min_coherence, 2)],
      ['Ionosphere', m.ionosphere_corrected ? 'corrected' : 'not corrected'], ['Troposphere', m.troposphere_corrected ? 'corrected' : 'not corrected']]));
  }
  function pDefoSpread() {
    var r = res().deformation; if (!r) return '';
    var p = (r.stats || {}).velocity_mm_yr_percentiles;
    if (!p) return panel('Velocity spread', 'mm/yr', empty('No coherent pixels', 'Nothing to summarise.'));
    return panel('Velocity spread', 'mm/yr', bars([['5%', p['5'], signed(p['5'], 0)], ['25%', p['25'], signed(p['25'], 0)], ['Median', p['50'], signed(p['50'], 0), true], ['75%', p['75'], signed(p['75'], 0)], ['95%', p['95'], signed(p['95'], 0)]]) +
      '<p class="pl" style="margin-top:10px">Share of pixels moving slower than each value. A wide, centred spread is mostly noise.</p>');
  }
  function pSeason() {
    var r = res().vegetation; if (!r) return panel('Growing season', '', need('vegetation'));
    var z = season(r);
    return panel('Growing season', 'HV radar', kv([['Season start', day(z.season_start), z.season_start_iqr ? 'middle half of pixels: ' + z.season_start_iqr.map(day).join('–') : ''], ['Peak', day(z.peak)],
      ['Season end', z.season_end ? day(z.season_end) : z.season_start ? 'not yet' : '—'], ['HV rise', num(z.median_amplitude_db) + ' dB'], ['Active area', num(z.active_area_km2) + ' km²'], ['Planting (est.)', z.estimated_planting_window ? day(z.estimated_planting_window[0]) + '–' + day(z.estimated_planting_window[1]) : '—']]) +
      (z.season_end_note ? '<p class="pl" style="margin:10px 0 0">' + esc(z.season_end_note) + '</p>' : ''));
  }
  function pClasses() {
    var r = res().vegetation; if (!r) return '';
    var a = (r.stats || {}).class_area_km2 || {}, f = (r.stats || {}).class_fraction || {};
    var lab = { full_season_observed: 'Full', season_end_not_observed: 'Ongoing', season_started_before_record: 'Early', no_seasonal_signal: 'None' };
    return panel('Season classes', 'km²', bars(Object.keys(lab).map(function (k) { return [lab[k], a[k] || 0, num(a[k], 0), k === 'full_season_observed']; })) +
      '<p class="pl" style="margin-top:10px">Full = start and end seen · Ongoing = still growing at the last pass · Early = already growing at the first pass · None = no seasonal rise.</p>');
  }
  function pCalendar() {
    var r = res().vegetation; if (!r) return panel('Planting calendar', '', need('vegetation'));
    var z = season(r), d = r.dates || [], first = d[0], last = d[d.length - 1];
    var rows = MN.map(function (m, i) {
      var yr = (last || '2026').slice(0, 4), a = yr + '-' + String(i + 1).padStart(2, '0') + '-01', b = yr + '-' + String(i + 1).padStart(2, '0') + '-31';
      var hit = function (x, y) { return x && (y || x) && x <= b && (y || x) >= a; };
      var tag = hit(z.peak) ? 'Peak' : z.estimated_planting_window && hit(z.estimated_planting_window[0], z.estimated_planting_window[1]) ? 'Plant (est.)' : z.season_start && hit(z.season_start, z.season_end || last) ? 'Growing' : z.season_end && hit(z.season_end) ? 'End' : '';
      var seen = first && last && a <= last && b >= first;
      return '<div class="cal' + (tag ? ' on' : '') + (seen ? '' : ' unseen') + '"><span>' + m + '</span><b>' + (tag || (seen ? '—' : 'not observed')) + '</b></div>';
    }).join('');
    return panel('Planting calendar', esc((last || '').slice(0, 4)), '<div class="cals">' + rows + '</div><p class="pl" style="margin-top:10px">From the radar season. Planting is estimated as 14–35 days before canopy growth; radar sees the canopy, not sowing.</p>');
  }
  function pFloodPeak() {
    var r = res().flood; if (!r) return panel('Flood peak', '', need('flood'));
    var s = r.stats || {};
    return panel('Flood peak', esc(day(s.peak_date)), '<div class="big-n">' + num(s.peak_flood_total_km2, 2) + '<small>km² flooded</small></div>' +
      kv([['Open water', num(s.peak_flood_open_km2, 2) + ' km²'], ['Flooded vegetation', num(s.peak_flood_vegetation_km2, 2) + ' km²', 'experimental'], ['Normal water', num(s.normal_water_km2) + ' km²'], ['Ever flooded', num(s.ever_flooded_km2) + ' km²'], ['Passes used', (s.passes_used || 0) + ' of ' + (s.passes_total || 0)], ['Pixel size', num(s.pixel_size_m, 0) + ' m']]));
  }
  function pWaterChange() {
    var r = res().water_change; if (!r) return panel('Water change', '', need('water_change'));
    var s = r.stats || {};
    return panel('Water change', (r.dates || []).map(day).join(' → '), kv([['New water', num(s.new_water_km2, 2) + ' km²'], ['Receded', num(s.receded_water_km2, 2) + ' km²'], ['Unchanged', num(s.unchanged_km2, 0) + ' km²'], ['Noise', s.water_ref_change_fraction == null ? '—' : pct(s.water_ref_change_fraction, 1), 'share of permanent water that “changed”']]) + '<div style="margin-top:10px">' + confTag(r.confidence) + '</div>');
  }
  function pFloodTop() {
    var r = res().flood; if (!r) return '';
    var top = (r.per_date || []).slice().sort(function (a, b) { return b.flood_total_km2 - a.flood_total_km2; }).slice(0, 5);
    return panel('Largest flood passes', 'km²', bars(top.map(function (p) { return [day(p.date), p.flood_total_km2, num(p.flood_total_km2, 1) + (p.aggregate_eligible ? '' : ' ⚑'), p.date === (r.stats || {}).peak_date]; })) + '<p class="pl" style="margin-top:10px">⚑ = rain/wind-flagged, excluded from the peak.</p>');
  }
  function pCalibration() {
    var r = res().flood || res().water_change; if (!r) return '';
    var c = r.calibration || {}, ref = r.references || {};
    return panel('Water calibration', esc((c.method || '').replace(/_/g, ' ')), kv([['HH threshold', '< ' + num(c.threshold_hh_db) + ' dB'], ['HV threshold', '< ' + num(c.threshold_hv_db) + ' dB'], ['Hit rate', c.hit_rate == null ? 'unknown' : pct(c.hit_rate), 'permanent water classified as water'], ['False alarms', pct(c.false_alarm_rate, 1), 'dry land classified as water'], ['References', ref.source ? { site: 'Site', user: 'Yours', jrc_gsw: 'JRC GSW', none: 'None', unavailable: 'Unavailable' }[ref.source] || ref.source : '—'], ['Radar mode', c.radar_mode || '—']]) +
      (c.note ? '<p class="pl" style="margin-top:10px">' + esc(c.note) + '</p>' : ''));
  }
  function pCoverage() {
    var R = res(), ks = Object.keys(R);
    if (!ks.length) return panel('Coverage', '', empty('No results yet', 'Coverage per engine appears after a run.'));
    return panel('Coverage', 'Per engine', ks.map(function (k) {
      var c = R[k].coverage || {};
      return '<div class="cov"><b>' + NAME[k] + '</b><span>' + num(c.granules_used, 0) + ' of ' + num(c.granules_found, 0) + ' granules · ' + (c.date_range || []).map(day).join(' → ') + (c.track_used ? ' · track ' + c.track_used : '') + '</span>' +
        ((c.gaps || []).length ? '<span>Gaps: ' + c.gaps.map(function (g) { return day(g[0]) + '–' + day(g[1]); }).join(', ') + '</span>' : '') + '</div>';
    }).join(''));
  }
  function pDownloads() {
    var R = res(), ks = Object.keys(R);
    if (!ks.length) return '';
    var lab = function (k) { return cap(k.replace(/_/g, ' ').replace(' mm yr', ' (mm/yr)').replace(' doy', ' (day of year)').replace(' db', ' (dB)')); };
    return panel('Downloads', 'GeoTIFF', ks.map(function (k) {
      return '<div class="cov"><b>' + NAME[k] + '</b>' + Object.keys(R[k].outputs || {}).map(function (o) { return '<a class="dl" href="' + esc(GM.apiUrl(R[k].outputs[o])) + '" target="_blank" rel="noopener">' + esc(lab(o)) + (o === 'quicklook' ? ' (PNG)' : '') + '</a>'; }).join('') + '</div>';
    }).join('') + '<p class="pl" style="margin-top:8px">Cloud-optimised GeoTIFFs in UTM; open in QGIS.</p>');
  }
  function pSources() {
    var R = res(), refs = Object.keys(R).map(function (k) { return R[k].references; }).filter(Boolean)[0];
    return panel('Sources', '', '<ul class="why">' +
      '<li>NASA NISAR L2 GCOV and GUNW via the Alaska Satellite Facility DAAC</li><li>GPM IMERG Late Run daily V07 via NASA GES DISC</li>' +
      (refs && refs.attribution ? '<li>' + esc(refs.attribution) + '</li>' : '<li>JRC Global Surface Water v1.4 (when no site references)</li>') +
      '<li>Place search: OpenStreetMap Nominatim (© OpenStreetMap contributors)</li><li>Map imagery: ' + esc(($('#m-src') || {}).textContent || '') + '</li></ul>');
  }

  /* ---------------------------------------------------------------- layout per tab */
  var LAYOUT = {
    overview: { L: [pArea, pConfidence, pRain], R: [pFindings, pFlags, pAsk], views: ['flood', 'water_change', 'vegetation', 'deformation'] },
    deformation: { L: [pDefoMotion, pDefoMethod], R: [pDefoSpread, function () { return pConf('deformation'); }, pAsk], views: ['deformation'] },
    vegetation: { L: [pSeason, pClasses], R: [pCalendar, function () { return pConf('vegetation'); }, pAsk], views: ['vegetation'] },
    flooding: { L: [pFloodPeak, pWaterChange], R: [pFloodTop, pCalibration, function () { return pConf(res().flood ? 'flood' : 'water_change'); }, pAsk], views: ['flood', 'water_change'] },
    data: { L: [pArea, pCoverage], R: [pDownloads, pSources, pAsk], views: ['rain'] },
  };

  function centre() {
    var L = LAYOUT[D.tab], R = res();
    var avail = L.views.filter(function (k) { return k === 'rain' ? passes().length : R[k]; });
    if (D.view == null || avail.indexOf(D.view) < 0) D.view = avail[0] || null;
    var seg = avail.length > 1 ? '<div class="seg">' + avail.map(function (k) { return '<button data-view="' + k + '"' + (k === D.view ? ' class="on"' : '') + '>' + NAME[k] + '</button>'; }).join('') + '</div>' : '';
    if (!D.view) {
      var k = L.views[0] === 'rain' ? 'flood' : L.views[0], why = engineState(k);
      return '<div class="mono">' + (D.tab === 'overview' ? 'Analysis' : D.tab === 'data' ? 'Data' : NAME[k]) + '</div>' + empty(D.tab === 'overview' || D.tab === 'data' ? (st().sel ? (st().job ? 'Waiting for the first result…' : 'Not analysed yet') : 'Select an area on the map') : why[0],
        D.tab === 'overview' || D.tab === 'data' ? (st().sel ? (st().job ? 'Results appear here as each engine finishes.' : 'Check data and press Monitor on the map.') : 'Then GAIA checks which NISAR analyses have data there.') : why[1]);
    }
    var v = D.view === 'rain' ? viewRain() : VIEWS[D.view]();
    return '<div class="top"><div><div class="mono">' + esc(v.kicker) + '</div><div class="hl">' + v.value + '<small>' + esc(v.unit) + '</small></div>' +
      '<div style="color:var(--mute);font-size:13px;max-width:64ch">' + esc(v.read) + '</div>' + (v.conf ? '<div style="margin-top:8px">' + confTag(v.conf) + '</div>' : '') + '</div>' + seg + '</div>' +
      '<div class="chwrap"><svg class="chart" id="dch" preserveAspectRatio="none" role="img" aria-label="' + esc(v.kicker) + ' chart"></svg><div class="ctip" hidden></div></div>' +
      '<div class="legend">' + v.legend + '</div>' +
      (v.rows.length ? '<div class="gt"><table class="dt"><tr>' + v.table.map(function (h) { return '<th>' + h + '</th>'; }).join('') + '</tr>' + v.rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>'; }).join('') + '</table></div>' : '') +
      '<div class="src">Source: ' + v.src + '</div>';
  }

  function header() {
    var S = st(), loc = (S.sel && (S.sel.resolved || (S.monitor && S.monitor.location))) || null;
    var named = loc && (loc.source === 'geocoder' || loc.source === 'site');  // only these names are "Place, Region"
    var name = named ? loc.name.split(/[,—]/)[0].trim() : loc && loc.source === 'coordinates' ? loc.name : S.sel ? (S.sel.label === 'Drawn area' || !loc ? S.sel.label : 'Selected area') : 'Select an area';
    $('#dn').textContent = name;
    var bits = [];
    if (loc && loc.lat != null) bits.push(Math.abs(loc.lat).toFixed(2) + '°' + (loc.lat >= 0 ? 'N' : 'S') + ' ' + Math.abs(loc.lon).toFixed(2) + '°' + (loc.lon >= 0 ? 'E' : 'W'));
    if (named && /[,—]/.test(loc.name)) bits.push(loc.name.split(/[,—]/).slice(1).map(function (x) { return x.trim(); }).filter(Boolean).join(', '));
    if (S.job) bits.push('Monitor ' + (S.monitor ? S.monitor.id : '') + ' · ' + S.job.status);
    else if (S.avail) bits.push(S.avail.any_available ? 'NISAR data available' : 'No usable NISAR data');
    $('#dm').textContent = bits.join(' · ') || 'Draw on the map, click a point, or search any place';
    var per = S.job ? (S.job.period || []) : null;
    $$('#per button').forEach(function (b) { b.classList.toggle('on', +b.getAttribute('data-p') === S.months && !S.job); b.disabled = !!S.job; });
    $('#per').title = per && per.length ? 'This monitor covers ' + per.map(day).join(' → ') : '';
  }

  function render(force) {
    var S = st(), J = S.job;
    var key = JSON.stringify([D.tab, D.view, D.answer, S.sel && S.sel.seq, S.sel && S.sel.location, !!S.avail, S.busy, S.months, J && J.id, J && J.status, J && Object.keys(J.engines).map(function (k) { return J.engines[k].status; }), S.monitor && S.monitor.id]);
    if (!force && key === D.key) return; D.key = key;
    header();
    var L = LAYOUT[D.tab];
    $('#colL').innerHTML = L.L.map(function (f) { return f(); }).join('');
    $('#colR').innerHTML = L.R.map(function (f) { return f(); }).join('');
    $('#colC').innerHTML = centre();
    var svg = $('#dch');
    if (svg) { var v = D.view === 'rain' ? viewRain() : VIEWS[D.view](); drawChart(svg, v.chart); }
    $$('#dnav button').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-tab') === D.tab); });
  }

  function setTab(tab) {
    if (!LAYOUT[tab]) tab = 'overview';
    D.tab = tab; D.view = null; D.answer = null; render(true); GM.refresh();  // map panel's 'Monitor … only' follows the tab
    var o = TAB_OVERLAY[tab], R = res();  // show the map layer that matches the tab
    if (o) { var pick = Array.isArray(o[0]) ? o.filter(function (x) { return R[x[0]]; })[0] : (R[o[0]] ? o : null); if (pick) GM.showOverlay(pick[0], pick[1], true); }
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-go],[data-view],[data-ask]'); if (!b || !b.closest('#dash')) return;
    if (b.dataset.go) setTab(b.dataset.go);
    else if (b.dataset.view) { D.view = b.dataset.view; render(); }
    else if (b.dataset.ask) { D.answer = answer(b.dataset.ask); render(); }
  });
  $$('#dnav button').forEach(function (b) { b.addEventListener('click', function () { setTab(b.getAttribute('data-tab')); }); });
  $$('#per button').forEach(function (b) { b.addEventListener('click', function () { GM.setMonths(+b.getAttribute('data-p')); }); });
  var rt; addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(function () { render(true); }, 150); });

  // Collapsible centre panel: collapsed it shows a one-line summary and gives the height to the map.
  var cvbox = $('#cvbox'), cvTog = $('#cv-tog');
  function cvSummary() {
    var k = $('#colC .top .mono'), hl = $('#colC .hl'), em = $('#colC .empty b');
    $('#cv-sum').textContent = hl ? (k ? k.textContent + ' · ' : '') + hl.textContent : em ? em.textContent : '';
  }
  function setCollapsed(on, save) {
    cvbox.classList.toggle('collapsed', on); document.body.classList.toggle('cv-collapsed', on);
    cvTog.setAttribute('aria-expanded', String(!on)); $('#cv-act').textContent = on ? 'Expand' : 'Collapse';
    if (save) try { localStorage.setItem('gaia.cvCollapsed', on ? '1' : '0'); } catch (e) { /* storage blocked */ }
  }
  if (cvbox) {
    cvTog.addEventListener('click', function () { setCollapsed(!cvbox.classList.contains('collapsed'), true); });
    new MutationObserver(cvSummary).observe($('#colC'), { childList: true });
    var saved = null; try { saved = localStorage.getItem('gaia.cvCollapsed'); } catch (e) { /* storage blocked */ }
    setCollapsed(saved === '1', false);
  }

  window.GAIA_DASH = { render: render, setTab: setTab, tab: function () { return D.tab; } };
  GM.on(function () { render(); });
  render(true);
})();

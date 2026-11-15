/* GAIA dashboard map: satellite basemap, area selection, and the link to the GAIA backend.
 *
 * Flow: draw a rectangle / click a point / search a place
 *   → POST /availability     (which of the 4 engines have enough NISAR data here?)
 *   → POST /monitor          (start a background job for the chosen engines)
 *   → GET  /jobs/{id}        (poll until done; results carry overlay URLs)
 *   → GET  /overlay/{name}   (coloured PNG + bounds, drawn over the imagery)
 */
(function () {
  'use strict';
  var CFG = window.GAIA_CONFIG || {};
  var API = (CFG.apiBase || '/api').replace(/\/$/, '');
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var toast = (window.__G && window.__G.toast) || function () {};

  var ENG = {
    water_change: { name: 'Water change', icon: 'i-flood', layers: [['change_map', 'Change map']] },
    flood: { name: 'Flooding', icon: 'i-rain', layers: [['peak_flood_map', 'Peak flood'], ['flood_frequency', 'Frequency'], ['water_occurrence', 'Occurrence']] },
    vegetation: { name: 'Vegetation & planting', icon: 'i-veg', layers: [['season_class', 'Season'], ['season_start_doy', 'Season start']] },
    deformation: { name: 'Land deformation', icon: 'i-def', layers: [['velocity_mm_yr', 'Velocity']] },
  };
  var ORDER = ['water_change', 'flood', 'vegetation', 'deformation'];
  var MAX_KM2 = 2500, POINT_KM = 20;

  var S = { sel: null, months: 12, avail: null, msg: null, busy: '', queue: [], confirmAll: false, job: null, monitor: null,
            overlay: null, monitors: [], lastQuery: null, draw: true };

  /* ------------------------------------------------------------------ helpers */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function fmt(v, d) { return v == null || isNaN(v) ? '—' : (+v).toLocaleString(undefined, { maximumFractionDigits: d == null ? 1 : d }); }
  function iso(d) { return d.toISOString().slice(0, 10); }
  function period() { var e = new Date(), s = new Date(); s.setMonth(s.getMonth() - S.months); return { start: iso(s), end: iso(e) }; }
  // Backend URLs are absolute to whichever host it saw; route them back through our API base.
  function apiUrl(u) { if (!u) return u; try { var x = new URL(u, location.href); return API + x.pathname + x.search; } catch (e) { return u; } }
  function ico(id) { return '<svg viewBox="0 0 24 24"><use href="#' + id + '"/></svg>'; }

  function call(method, path, body) {
    return fetch(API + path, {
      method: method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      return r.text().then(function (t) {
        var j = null; try { j = t ? JSON.parse(t) : null; } catch (e) { /* not JSON: proxy error page */ }
        if (j === null && (r.status >= 500 || !r.ok)) {
          throw { status: r.status, message: 'GAIA backend not reachable. Start it with: uvicorn gaia.api:app --port 8000' };
        }
        if (!r.ok) {
          var d = j && (j.detail !== undefined ? j.detail : j);
          var m = typeof d === 'string' ? d : Array.isArray(d) ? d.map(function (x) { return x.msg; }).join('; ') : (d && d.message) || ('HTTP ' + r.status);
          throw { status: r.status, message: m, body: (d && typeof d === 'object' && !Array.isArray(d)) ? d : j };
        }
        return j;
      });
    }, function () { throw { status: 0, message: 'GAIA backend not reachable. Start it with: uvicorn gaia.api:app --port 8000' }; });
  }

  function approxKm2(b) {
    var s = b.getSouth(), n = b.getNorth(), w = b.getWest(), e = b.getEast();
    return Math.abs(n - s) * 110.57 * Math.abs(e - w) * 111.32 * Math.cos((s + n) / 2 * Math.PI / 180);
  }
  function squareAround(ll, km) {
    var dLat = km / 2 / 110.57, dLon = km / 2 / (111.32 * Math.cos(ll.lat * Math.PI / 180));
    return L.latLngBounds([ll.lat - dLat, ll.lng - dLon], [ll.lat + dLat, ll.lng + dLon]);
  }

  /* ------------------------------------------------------------------ map */
  var el = $('#lmap'); if (!el || !window.L) { console.warn('GAIA map: Leaflet not loaded'); return; }
  var map = L.map(el, { zoomControl: true, boxZoom: false, worldCopyJump: true, attributionControl: true })
    .setView([9.5, 8.5], 5);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  var selLayer = L.featureGroup().addTo(map), ovLayer = null, srcTag = $('#m-src');

  function esri() {
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics' }).addTo(map);
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
      maxZoom: 19, opacity: .85 }).addTo(map);
    if (srcTag) srcTag.textContent = 'Esri World Imagery';
  }
  function google(key) {
    // Google Map Tiles API (2D satellite tiles; same imagery family as Google Earth).
    return fetch('https://tile.googleapis.com/v1/createSession?key=' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapType: 'satellite', language: 'en-US', region: 'US', layerTypes: ['layerRoadmap'] }),
    }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (s) {
      var q = 'session=' + s.session + '&key=' + encodeURIComponent(key);
      var layer = L.tileLayer('https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?' + q, { maxZoom: 21, attribution: 'Google' }).addTo(map);
      var credit = function () {
        var b = map.getBounds();
        fetch('https://tile.googleapis.com/tile/v1/viewport?' + q + '&zoom=' + map.getZoom() + '&north=' + b.getNorth() +
              '&south=' + b.getSouth() + '&east=' + b.getEast() + '&west=' + b.getWest())
          .then(function (r) { return r.json(); })
          .then(function (v) { layer.options.attribution = 'Imagery © Google' + (v.copyright ? ' · ' + esc(v.copyright) : ''); map.attributionControl._update(); })
          .catch(function () {});
      };
      map.on('moveend', credit); credit();
      if (srcTag) srcTag.textContent = 'Google satellite';
    });
  }
  if (CFG.googleMapsKey) google(CFG.googleMapsKey).catch(function (e) { console.warn('Google tiles unavailable, using Esri', e); esri(); toast('Google imagery unavailable, using Esri'); });
  else esri();

  // Leaflet needs a size; the dashboard starts hidden and the panel resizes with the layout.
  if (window.ResizeObserver) new ResizeObserver(function () { map.invalidateSize(); }).observe(el);

  /* ------------------------------------------------------------------ drawing */
  var drag = null, rubber = null, drawEnd = 0;
  function setDraw(on) {
    S.draw = on; el.classList.toggle('drawing', on);
    if (on) map.dragging.disable(); else map.dragging.enable();
    $('#m-draw').classList.toggle('on', on);
    $('#m-hint').textContent = on ? 'Drag to draw an area · click for a ' + POINT_KM + ' km square' : 'Pan and zoom · press Draw area to select';
  }
  el.addEventListener('pointerdown', function (e) {
    if (!S.draw || e.button !== 0 || e.target.closest('.leaflet-control,.mtools,.mlegend')) return;
    e.preventDefault(); e.stopPropagation();
    drag = { a: map.mouseEventToLatLng(e), x: e.clientX, y: e.clientY };
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', function (e) {
    if (!drag) return;
    var b = L.latLngBounds(drag.a, map.mouseEventToLatLng(e));
    if (!rubber) rubber = L.rectangle(b, { color: '#fff', weight: 1.5, dashArray: '5 4', fillOpacity: .08 }).addTo(map);
    else rubber.setBounds(b);
    $('#m-hint').textContent = '≈ ' + fmt(approxKm2(b), 0) + ' km²' + (approxKm2(b) > MAX_KM2 ? ' · will be cropped to ' + MAX_KM2.toLocaleString() + ' km²' : '');
  });
  el.addEventListener('pointerup', function (e) {
    if (!drag) return;
    var moved = Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 6, a = drag.a;
    drag = null; drawEnd = Date.now(); if (rubber) { map.removeLayer(rubber); rubber = null; }
    if (moved) {
      var b = L.latLngBounds(a, map.mouseEventToLatLng(e));
      select({ label: 'Drawn area', location: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(function (v) { return v.toFixed(5); }).join(','),
               bounds: b, km2: approxKm2(b) });
    } else {
      select({ label: a.lat.toFixed(4) + ', ' + a.lng.toFixed(4), location: a.lat.toFixed(5) + ', ' + a.lng.toFixed(5),
               bounds: squareAround(a, POINT_KM), km2: POINT_KM * POINT_KM });
    }
    setDraw(false);
  });

  function drawSelection(geojsonOrBounds) {
    selLayer.clearLayers();
    var style = { color: '#fff', weight: 2, fillOpacity: .06 };
    var lyr = geojsonOrBounds instanceof L.LatLngBounds ? L.rectangle(geojsonOrBounds, style) : L.geoJSON(geojsonOrBounds, { style: function () { return style; } });
    lyr.addTo(selLayer);
    map.fitBounds(selLayer.getBounds(), { padding: [24, 24], maxZoom: 13 });
  }

  /* ------------------------------------------------------------------ flow */
  function resetResults() { selSeq++; S.busy = ''; S.avail = null; S.msg = null; S.job = null; S.monitor = null; clearOverlay(); stopPoll(); }

  var selSeq = 0;  // responses for an older selection are ignored
  function select(sel, opts) {
    resetResults(); S.sel = sel; sel.seq = ++selSeq;
    drawSelection(sel.geojson || sel.bounds);
    render();
    if (!(opts && opts.noCheck)) check();
  }

  function check() {
    if (!S.sel) return;
    var p = period(), sel = S.sel; S.busy = 'check'; S.msg = null; render();
    var current = function () { return S.sel === sel && !S.job; };
    call('POST', '/availability', { location: sel.location, start: p.start, end: p.end }).then(function (r) {
      if (!current()) return;
      S.avail = r.availability; S.sel.resolved = r.location;
      if (r.location && r.location.aoi) drawSelection(r.location.aoi);  // the AOI GAIA will really use (after cropping)
    }).catch(function (e) {
      if (!current()) return;
      if (e.body && e.body.error === 'no_data') { S.avail = e.body.availability || null; S.msg = { kind: 'nodata', text: e.body.message, list: e.body.suggestions || [] }; }
      else S.msg = { kind: 'error', text: e.message };
    }).then(function () { if (current()) { S.busy = ''; render(); } });
  }

  function availableEngines() { return ORDER.filter(function (k) { return S.avail && S.avail.engines[k] && S.avail.engines[k].available; }); }
  // engine that matches the dashboard tab being viewed ("monitor only this page")
  function pageEngine() { var t = window.GAIA_DASH && window.GAIA_DASH.tab ? window.GAIA_DASH.tab() : 'overview'; return { deformation: 'deformation', vegetation: 'vegetation', flooding: 'flood' }[t] || null; }

  function start(engines) {
    if (!engines || !engines.length) { toast('No engine with enough data'); return; }
    var p = period(); S.busy = 'start'; render();
    call('POST', '/monitor', { location: S.sel.location, start: p.start, end: p.end, engines: engines }).then(function (r) {
      S.monitor = r.monitor; S.job = r.job; toast('Monitoring started'); poll(); loadMonitors();
    }).catch(function (e) {
      S.msg = e.body && e.body.error === 'no_data' ? { kind: 'nodata', text: e.body.message, list: e.body.suggestions || [] } : { kind: 'error', text: e.message };
    }).then(function () { S.busy = ''; render(); });
  }

  var pollT = null;
  function stopPoll() { clearTimeout(pollT); pollT = null; }
  function poll() {
    stopPoll();
    if (!S.job || !/queued|running/.test(S.job.status)) return;
    pollT = setTimeout(function () {
      var id = S.job.id;
      call('GET', '/jobs/' + id).then(function (j) {
        if (!S.job || S.job.id !== id) return;
        var before = S.job; S.job = j;
        ORDER.forEach(function (k) {  // auto-show the first finished result
          if (!S.overlay && j.results[k] && !(before.results || {})[k]) showOverlay(k, ENG[k].layers[0][0]);
        });
        if (!/queued|running/.test(j.status)) toast('Analysis ' + j.status);
        loadMonitors();
        render(); poll();
      }).catch(function () { poll(); });
    }, 4000);
  }

  function openMonitor(id) {
    call('GET', '/monitors/' + id).then(function (m) {
      resetResults();
      S.sel = { label: m.name, location: m.query, geojson: m.location && m.location.aoi };
      S.monitor = m; S.job = m.last_job;
      if (S.sel.geojson) drawSelection(S.sel.geojson);
      setDraw(false);  // an open analysis: clicks inspect it rather than start a new area
      var first = S.job && ORDER.filter(function (k) { return S.job.results[k]; })[0];
      if (first) showOverlay(first, ENG[first].layers[0][0]);
      render(); poll();
    }).catch(function (e) { toast(e.message); });
  }

  function refresh() {
    if (!S.monitor) return;
    S.busy = 'refresh'; render();
    call('POST', '/monitors/' + S.monitor.id + '/refresh').then(function (r) {
      if (r.job) { S.job = r.job; toast('New passes found — re-running'); poll(); }
      else toast('Up to date · latest pass ' + (r.latest_acquisition || '—'));
    }).catch(function (e) { toast(e.message); }).then(function () { S.busy = ''; render(); });
  }

  function loadMonitors() {
    call('GET', '/monitors').then(function (ms) { S.monitors = ms || []; render(); }).catch(function () { S.monitors = []; });
    call('GET', '/jobs').then(function (q) { S.queue = q || []; render(); }).catch(function () { S.queue = []; });
  }

  function cancelJob() {
    if (!S.job) return;
    S.busy = 'cancel'; render();
    call('POST', '/jobs/' + S.job.id + '/cancel').then(function (j) { S.job = j; stopPoll(); toast('Cancelled · finished engines are kept'); loadMonitors(); })
      .catch(function (e) { toast(e.message); }).then(function () { S.busy = ''; render(); });
  }
  function cancelAll() {
    S.busy = 'cancel'; S.confirmAll = false; render();
    call('POST', '/jobs/cancel-all').then(function (r) {
      toast('Cancelled ' + r.cancelled.length + ' job' + (r.cancelled.length === 1 ? '' : 's'));
      if (S.job && r.cancelled.indexOf(S.job.id) > -1) return call('GET', '/jobs/' + S.job.id).then(function (j) { S.job = j; stopPoll(); });
    }).catch(function (e) { toast(e.message); }).then(function () { S.busy = ''; loadMonitors(); render(); });
  }

  /* ------------------------------------------------------------------ overlays */
  function clearOverlay() { if (ovLayer) map.removeLayer(ovLayer); ovLayer = null; S.overlay = null; $('#m-legend').hidden = true; }
  function showOverlay(engine, key, force) {
    var r = S.job && S.job.results[engine], u = r && r.overlays && r.overlays[key];
    if (!u) return;
    if (S.overlay && S.overlay.engine === engine && S.overlay.key === key) { if (!force) { clearOverlay(); render(); } return; }
    S.overlay = { engine: engine, key: key, loading: true }; render();
    call('GET', apiUrl(u).slice(API.length)).then(function (o) {
      if (!S.overlay || S.overlay.key !== key || S.overlay.engine !== engine) return;
      if (ovLayer) map.removeLayer(ovLayer);
      ovLayer = L.imageOverlay(apiUrl(o.image_url), o.bounds, { opacity: +($('#m-op') ? $('#m-op').value : .85), interactive: false }).addTo(map);
      S.overlay = { engine: engine, key: key, legend: o.legend };
      legend(engine, o.legend); render();
    }).catch(function (e) { S.overlay = null; toast('Overlay failed: ' + e.message); render(); });
  }
  function legend(engine, lg) {
    var box = $('#m-legend'), h = '<b>' + esc(ENG[engine].name) + '</b>';
    if (lg.type === 'categorical') h += lg.items.map(function (i) { return '<span><i style="background:' + i.color + '"></i>' + esc(i.label) + '</span>'; }).join('');
    else h += '<span class="ramp" style="background:linear-gradient(90deg,' + lg.colors.join(',') + ')"></span><span class="rl"><em>' + fmt(lg.min) + '</em><em>' + esc(lg.title) + ' (' + esc(lg.unit) + ')</em><em>' + fmt(lg.max) + '</em></span>';
    h += '<label class="op">Opacity <input id="m-op" type="range" min="0.2" max="1" step="0.05" value="' + (ovLayer ? ovLayer.options.opacity : .85) + '"></label>';
    box.innerHTML = h; box.hidden = false;
    $('#m-op').addEventListener('input', function () { if (ovLayer) ovLayer.setOpacity(+this.value); });
  }

  /* ------------------------------------------------------------------ result text */
  function headline(k, r) {
    var s = r.stats || {}, z = r.season || {};
    if (r.flags && r.flags.water_detectable === false) return 'Water cannot be reliably detected here (land is as dark as water).';
    if (k === 'water_change') return '+' + fmt(s.new_water_km2, 2) + ' km² new water · −' + fmt(s.receded_water_km2, 2) + ' km² receded' + (r.dates ? ' · ' + r.dates.join(' → ') : '');
    if (k === 'flood') return s.peak_date ? 'Peak ' + s.peak_date + ': ' + fmt(s.peak_flood_total_km2, 2) + ' km² flooded · normal water ' + fmt(s.normal_water_km2) + ' km²' : 'No usable peak (all passes flagged or no flooding)';
    if (k === 'vegetation') return z.season_start ? 'Season start ' + z.season_start + ' · peak ' + (z.peak || '—') + (z.estimated_planting_window ? ' · planting est. ' + z.estimated_planting_window.join(' to ') : '') : 'No clear growing season in this period';
    if (k === 'deformation') { var p = s.velocity_mm_yr_percentiles || {}; return 'Median ' + fmt(p['50']) + ' mm/yr · noise ±' + fmt(s.velocity_noise_mm_yr) + ' mm/yr · ' + fmt(s.span_days, 0) + ' days of data'; }
    return '';
  }

  /* ------------------------------------------------------------------ panel */
  function render() {
    var box = $('#mon'); if (!box) return;
    var h = '', A = S.avail, J = S.job;
    if (!S.sel) {
      h += '<div class="mon-h"><div><div class="mono">Monitor an area</div><h4>Select a region on the map</h4></div></div>' +
           '<p class="mon-p">Drag on the imagery to draw an area, click for a ' + POINT_KM + ' km square, or search a place above. GAIA checks which NISAR analyses have data there, then runs them.</p>' +
           (S.msg ? '<div class="mon-msg error"><b>Error</b> ' + esc(S.msg.text) + '</div>' : '');
    } else {
      var loc = S.sel.resolved || {};
      var km2 = loc.area_km2 != null ? loc.area_km2 : S.sel.km2;
      h += '<div class="mon-h"><div><div class="mono">' + (S.monitor ? 'Monitor · ' + esc(S.monitor.id) : 'Selected area') + '</div><h4>' + esc(loc.name && loc.name !== 'bounding box' ? loc.name : S.sel.label) + '</h4>' +
           '<span class="mon-m">' + (km2 != null ? fmt(km2, 0) + ' km²' : '') + (A ? ' · ' + esc(A.period[0]) + ' → ' + esc(A.period[1]) : '') + '</span></div>' +
           (J ? '<span class="tag st-' + esc(J.status) + '">' + esc(J.status) + '</span>' : '') + '</div>';
      var notes = (loc.notes || []).concat(A ? A.notes || [] : []);
      if (A && A.gcov) {
        var t = (A.gcov.tracks || []).filter(function (x) { return x.track === A.gcov.best_track; })[0];
        h += '<p class="mon-p">' + (A.gcov.best_track != null ? A.gcov.best_track_dates.length + ' usable radar passes on track ' + A.gcov.best_track + (t ? ' (' + esc(String(t.flight_direction).toLowerCase()) + ', ' + esc(t.radar_mode) + ')' : '') : 'No usable GCOV radar passes') +
             ' · ' + A.gunw.chain_length + ' interferograms' + (A.latest_acquisition ? ' · latest ' + esc(A.latest_acquisition) : '') + '</p>';
      }
      if (S.busy === 'check') h += '<p class="mon-p busy">Searching NISAR archive…</p>';
      if (S.msg) h += '<div class="mon-msg ' + S.msg.kind + '"><b>' + (S.msg.kind === 'nodata' ? 'No usable NISAR data' : 'Error') + '</b> ' + esc(S.msg.text) +
                      (S.msg.list && S.msg.list.length ? '<ul>' + S.msg.list.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : '') + '</div>';

      if (A || J) {
        h += '<div class="engs">';
        ORDER.forEach(function (k) {
          var av = A && A.engines[k], je = J && J.engines[k], r = J && J.results[k];
          var st, cls = '', sub = '';
          if (je) {
            st = je.status === 'done' && r ? (r.confidence || 'done') + ' confidence' : je.status === 'running' ? 'running…' : je.status;
            cls = je.status === 'done' ? 'c-' + (r && r.confidence) : 's-' + je.status;
            sub = r ? headline(k, r) : je.status === 'failed' ? (je.error || 'failed') : je.status === 'skipped' ? (je.reason || '') : je.status === 'running' ? 'Streaming NISAR subsets from ASF — this can take several minutes' : 'Waiting';
          } else if (av) {
            st = av.available ? 'available' : 'not enough data'; cls = av.available ? 's-ok' : 's-no'; sub = av.reason;
          } else if (J) {
            st = 'not run'; cls = 's-no'; sub = 'Not part of this monitor. Press “New area” and select it again to run it.';
          }
          h += '<div class="eng' + ((av && !av.available && !je) || (J && !je) ? ' off' : '') + '">' +
               ico(ENG[k].icon) + '<div class="eng-t"><div class="eng-n">' + ENG[k].name + '<span class="tag ' + cls + '">' + esc(st || '') + '</span></div><div class="eng-s">' + esc(sub) + '</div>';
          if (r && r.overlays) {
            h += '<div class="eng-l">' + ENG[k].layers.filter(function (l) { return r.overlays[l[0]]; }).map(function (l) {
              var on = S.overlay && S.overlay.engine === k && S.overlay.key === l[0];
              return '<button data-ov="' + k + '|' + l[0] + '"' + (on ? ' class="on"' : '') + '>' + (on && S.overlay.loading ? 'Loading…' : l[1]) + '</button>';
            }).join('') + '</div>';
            var why = (r.confidence_reasons || []).concat(r.limitations || []);
            if (why.length) h += '<details><summary>Why ' + esc(r.confidence || '') + ' confidence · limitations</summary><ul>' + why.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></details>';
          }
          h += '</div></div>';
        });
        h += '</div>';
      }
      if (notes.length) h += '<ul class="mon-notes">' + notes.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>';

      h += '<div class="mon-a">';
      var active = J && /queued|running/.test(J.status);
      if (!J) {
        var all = availableEngines(), pe = pageEngine(), dis = S.busy ? ' disabled' : '';
        if (A && A.any_available) {
          h += '<button class="btn solid" data-act="all"' + dis + '>' + (S.busy === 'start' ? 'Starting…' : all.length === 4 ? 'Monitor all 4 engines' : 'Monitor all ' + all.length + ' available engines') + '</button>';
          if (pe) h += '<button class="btn" data-act="page"' + (S.busy || all.indexOf(pe) < 0 ? ' disabled' : '') + ' title="' + (all.indexOf(pe) < 0 ? esc(A.engines[pe].reason) : 'Only this tab — shorter queue') + '">Monitor ' + ENG[pe].name.toLowerCase() + ' only</button>';
        }
        h += '<button class="btn" data-act="check"' + dis + '>Check data</button>';
      } else {
        if (active) h += '<button class="btn" data-act="cancel"' + (S.busy ? ' disabled' : '') + '>' + (S.busy === 'cancel' ? 'Cancelling…' : 'Cancel') + '</button>';
        if (S.monitor) h += '<button class="btn" data-act="refresh"' + (S.busy || active ? ' disabled' : '') + '>' + (S.busy === 'refresh' ? 'Checking…' : 'Check for new passes') + '</button>';
        h += '<button class="btn" data-act="new">New area</button>';
      }
      h += '</div>';
      if (!J && A && A.any_available) h += '<p class="mon-p small">' + (pageEngine() ? 'Running one engine is quicker and keeps the queue short.' : 'Open the Deformation, Vegetation or Flooding tab to monitor just that engine.') + ' A first run takes ~20–30 min for all four engines on ~400 km².</p>';
      if (active) h += '<p class="mon-p small">' + (J.queue_position > 0 ? '<b>' + J.queue_position + ' job' + (J.queue_position === 1 ? '' : 's') + ' ahead</b> — jobs run one at a time. ' : J.queue_position === 0 ? '<b>Running now.</b> ' : '') + 'First runs stream data from NASA ASF (≈ 0.5–3 min per pass). You can leave; reopen it from “Your monitors”.</p>';
    }
    var Q = S.queue || [];
    if (Q.length) {
      h += '<div class="mon-q"><span class="mono">Queue</span><span>' + Q.length + ' job' + (Q.length === 1 ? '' : 's') + ' · ' + esc(Q[0].name) + ' running' + (Q.length > 1 ? ', ' + (Q.length - 1) + ' waiting' : '') + '</span>' +
           '<button class="btn" data-act="cancelall"' + (S.busy ? ' disabled' : '') + '>' + (S.confirmAll ? 'Confirm: cancel all ' + Q.length : 'Cancel all') + '</button></div>';
    }
    if (S.monitors.length) {
      h += '<div class="mon-list"><div class="mono">Your monitors</div>' + S.monitors.slice(0, 8).map(function (m) {
        return '<button class="chip' + (S.monitor && S.monitor.id === m.id ? ' on' : '') + '" data-mon="' + esc(m.id) + '">' + esc(m.name) + (m.last_run_status ? ' · ' + esc(m.last_run_status) : '') + '</button>';
      }).join('') + '</div>';
    }
    box.innerHTML = h;
    listeners.forEach(function (f) { try { f(S); } catch (e) { console.error(e); } });
  }
  var listeners = [];

  $('#mon').addEventListener('click', function (e) {
    var b = e.target.closest('button,summary'); if (!b || b.disabled) return;
    if (b.dataset.act !== 'cancelall' && S.confirmAll) { S.confirmAll = false; render(); }
    if (b.dataset.ov) { var p = b.dataset.ov.split('|'); showOverlay(p[0], p[1]); }
    else if (b.dataset.mo) { S.months = +b.dataset.mo; check(); }
    else if (b.dataset.mon) openMonitor(b.dataset.mon);
    else if (b.dataset.act === 'check') check();
    else if (b.dataset.act === 'all') start(availableEngines());
    else if (b.dataset.act === 'page') start([pageEngine()]);
    else if (b.dataset.act === 'cancel') cancelJob();
    else if (b.dataset.act === 'cancelall') { if (S.confirmAll) cancelAll(); else { S.confirmAll = true; render(); } }
    else if (b.dataset.act === 'refresh') refresh();
    else if (b.dataset.act === 'new') { resetResults(); S.sel = null; selLayer.clearLayers(); setDraw(true); render(); }
  });
  $('#m-draw').addEventListener('click', function () { setDraw(!S.draw); });
  $('#m-view').addEventListener('click', function () {
    var b = map.getBounds(); setDraw(false);
    select({ label: 'Map view', location: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(function (v) { return v.toFixed(5); }).join(','), bounds: b, km2: approxKm2(b) });
  });
  $('#m-clear').addEventListener('click', function () { resetResults(); S.sel = null; selLayer.clearLayers(); render(); });

  /* ------------------------------------------------------------------ search hook */
  function locate(q) {
    q = String(q || '').trim(); if (!q || q === S.lastQuery) return;
    S.lastQuery = q;
    var seq = selSeq;
    call('GET', '/locate?q=' + encodeURIComponent(q)).then(function (loc) {
      if (seq !== selSeq || S.job) return;  // the user picked something else meanwhile
      select({ label: loc.name, location: q, geojson: loc.aoi, resolved: loc });
    }).catch(function (e) { toast(e.status === 404 ? 'GAIA could not find “' + q + '”' : e.message); });
  }

  /* ------------------------------------------------------------------ fullscreen */
  // Immersive mode: the map fills the viewport (and the browser goes full screen where allowed).
  // The view, selection, overlay and job are untouched, so leaving restores exactly what was there.
  var card = $('#mw'), byBrowser = false;
  function isFull() { return card.classList.contains('full'); }
  function setFull(on) {
    if (on === isFull()) return;
    var c = map.getCenter(), z = map.getZoom();
    card.classList.toggle('full', on); document.body.classList.toggle('map-full', on);
    $('#m-full').textContent = on ? 'Exit full screen' : 'Full screen';
    if (!on) { endTour(); document.body.classList.remove('mon-hide'); }
    var keep = function () { map.invalidateSize({ pan: false }); map.setView(c, z, { animate: false }); };
    requestAnimationFrame(keep); setTimeout(keep, 520);  // again after the entry animation settles
    var d = document;
    if (on && d.fullscreenEnabled && !d.fullscreenElement) {
      d.documentElement.requestFullscreen().then(function () { byBrowser = true; }, function () { byBrowser = false; });
    } else if (!on && d.fullscreenElement && byBrowser) { byBrowser = false; d.exitFullscreen().catch(function () {}); }
    if (on) setTimeout(function () { if (isFull() && !seenTour()) startTour(); }, 650);
  }
  // Esc in browser full screen is consumed by the browser: follow it out of immersive mode.
  document.addEventListener('fullscreenchange', function () { if (!document.fullscreenElement && byBrowser) { byBrowser = false; setFull(false); } });
  $('#m-full').addEventListener('click', function () { setFull(!isFull()); });
  $('#m-panel').addEventListener('click', function () {
    var hide = document.body.classList.toggle('mon-hide'); this.setAttribute('aria-pressed', String(!hide)); this.classList.toggle('on', !hide);
  });
  $('#m-help').addEventListener('click', function () { startTour(); });
  addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (tour) endTour(); else if (isFull()) setFull(false);
  });
  addEventListener('resize', function () { if (isFull()) map.invalidateSize({ pan: false }); });

  /* ------------------------------------------------------------------ contextual info on click */
  // Not drawing: a click reports what is at that spot — the overlay class/value (read from the overlay
  // image itself) and, inside the selected area, the area's results.
  var ovPix = null;
  function overlayPixels(cb) {
    if (!ovLayer) return cb(null);
    var src = ovLayer._url;
    if (ovPix && ovPix.src === src) return cb(ovPix);
    var img = new Image(); img.onload = function () {
      var c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight;
      var g = c.getContext('2d', { willReadFrequently: true }); g.drawImage(img, 0, 0);
      ovPix = { src: src, g: g, w: c.width, h: c.height }; cb(ovPix);
    }; img.onerror = function () { cb(null); }; img.src = src;
  }
  function merc(lat) { return Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)); }
  function parseRgb(s) { var m = String(s).match(/[\d.]+/g) || []; return [+m[0], +m[1], +m[2]]; }
  function valueAt(ll, cb) {
    var o = S.overlay; if (!o || !o.legend || !ovLayer) return cb(null);
    var b = ovLayer.getBounds(); if (!b.contains(ll)) return cb(null);
    overlayPixels(function (P) {
      if (!P) return cb(null);
      var x = Math.floor((ll.lng - b.getWest()) / (b.getEast() - b.getWest()) * P.w);
      var y = Math.floor((merc(b.getNorth()) - merc(ll.lat)) / (merc(b.getNorth()) - merc(b.getSouth())) * P.h);
      var d = P.g.getImageData(Math.min(P.w - 1, Math.max(0, x)), Math.min(P.h - 1, Math.max(0, y)), 1, 1).data;
      if (d[3] < 10) return cb({ none: true });
      var lg = o.legend, best = null, bd = 1e9;
      if (lg.type === 'categorical') {
        lg.items.forEach(function (it) { var c = parseRgb(it.color), e = Math.abs(c[0] - d[0]) + Math.abs(c[1] - d[1]) + Math.abs(c[2] - d[2]); if (e < bd) { bd = e; best = it; } });
        return cb({ label: best.label, color: best.color });
      }
      var stops = lg.colors.map(parseRgb), t = 0;
      for (var i = 0; i <= 100; i++) {   // nearest colour along the ramp → value (approximate: the PNG is 8-bit)
        var f = i / 100 * (stops.length - 1), k = Math.min(stops.length - 2, Math.floor(f)), u = f - k;
        var c = [0, 1, 2].map(function (j) { return stops[k][j] + (stops[k + 1][j] - stops[k][j]) * u; });
        var e = Math.abs(c[0] - d[0]) + Math.abs(c[1] - d[1]) + Math.abs(c[2] - d[2]); if (e < bd) { bd = e; t = i / 100; }
      }
      cb({ value: lg.min + t * (lg.max - lg.min), unit: lg.unit, title: lg.title, color: 'rgb(' + d[0] + ',' + d[1] + ',' + d[2] + ')' });
    });
  }
  map.on('click', function (e) {
    if (S.draw || Date.now() - drawEnd < 400) return;
    var inSel = S.sel && selLayer.getLayers().length && selLayer.getBounds().contains(e.latlng);
    valueAt(e.latlng, function (v) {
      if (!v && !inSel) return;
      var h = '<span class="mono">' + e.latlng.lat.toFixed(4) + ', ' + e.latlng.lng.toFixed(4) + '</span>';
      if (v && S.overlay) {
        var lname = ((ENG[S.overlay.engine].layers.filter(function (l) { return l[0] === S.overlay.key; })[0]) || [0, ''])[1];
        h += '<b>' + esc(ENG[S.overlay.engine].name + (lname ? ' · ' + lname : '')) + '</b>';
        h += v.none ? '<p>No data or no change at this spot</p>' : v.label ? '<p><i class="pop-sw" style="background:' + v.color + '"></i>' + esc(v.label) + '</p>'
          : '<p><i class="pop-sw" style="background:' + v.color + '"></i>≈ ' + fmt(v.value, 1) + ' ' + esc(v.unit) + '</p>';
      }
      if (inSel) {
        h += '<p style="margin-top:6px"><b style="color:var(--ink)">' + esc(S.sel.label) + '</b> · ' + fmt(S.sel.km2 || (S.sel.resolved || (S.monitor && S.monitor.location) || {}).area_km2, 0) + ' km²</p>';
        if (S.job) ORDER.forEach(function (k) { var r = S.job.results[k]; if (r) h += '<p>' + esc(ENG[k].name) + ': ' + esc(headline(k, r)) + '</p>'; });
        else h += '<p>' + (S.avail ? 'Not analysed yet — press Monitor in the panel.' : 'Checking NISAR data…') + '</p>';
      }
      L.popup({ maxWidth: 320, autoPanPaddingTopLeft: [20, 90] }).setLatLng(e.latlng).setContent(h).openOn(map);
    });
  });

  /* ------------------------------------------------------------------ full-screen tutorial */
  var TOUR = [
    ['Move around', 'Drag the map to pan. If “Draw area” is on, drag draws instead — switch it off to pan. Arrow keys work too.', '#lmap'],
    ['Zoom', 'Scroll or pinch to zoom, or use + and − at the top left.', '.leaflet-control-zoom'],
    ['Select an area', 'Press “Draw area”, then drag a rectangle — or click once for a 20 km square. GAIA checks NISAR data there.', '#m-draw'],
    ['Open details', 'With drawing off, click a spot to see what the result layer shows there and the area’s readings.', '#lmap'],
    ['Switch layers', 'Each finished engine lists its map layers in this panel. The legend (bottom left) has an opacity slider.', '#mon'],
    ['Exit full screen', 'Press this button or Esc. Your view, area and layer stay as they are. “?” replays this guide.', '#m-full'],
  ], tour = null;
  function seenTour() { try { return localStorage.getItem('gaia.mapTour') === '1'; } catch (e) { return true; } }
  function markTour() { try { localStorage.setItem('gaia.mapTour', '1'); } catch (e) { /* storage blocked */ } }
  function startTour() { if (!isFull()) return; tour = { i: 0 }; card.classList.add('touring'); document.body.classList.remove('mon-hide'); drawTour(); }
  function endTour() {
    if (!tour) return; tour = null; markTour(); card.classList.remove('touring');
    var t = $('.tour'); if (t) t.remove(); var o = $('.tour-on'); if (o) o.classList.remove('tour-on');
  }
  function drawTour() {
    var st = TOUR[tour.i], box = $('.tour') || card.appendChild(Object.assign(document.createElement('div'), { className: 'tour' }));
    box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'Map tutorial');
    box.innerHTML = '<span class="mono">' + (tour.i + 1) + ' / ' + TOUR.length + '</span><h5>' + st[0] + '</h5><p>' + st[1] + '</p>' +
      '<div class="tf"><span class="td">' + TOUR.map(function (x, i) { return '<i' + (i === tour.i ? ' class="on"' : '') + '></i>'; }).join('') + '</span><span class="sp"></span>' +
      '<button data-t="skip">Skip</button>' + (tour.i ? '<button data-t="back">Back</button>' : '') + '<button class="nx" data-t="next">' + (tour.i === TOUR.length - 1 ? 'Done' : 'Next') + '</button></div>';
    var o = $('.tour-on'); if (o) o.classList.remove('tour-on');
    var tg = $(st[2]); if (tg && st[2] !== '#lmap') tg.classList.add('tour-on');
    box.querySelector('.nx').focus({ preventScroll: true });
  }
  card.addEventListener('click', function (e) {
    var b = e.target.closest('[data-t]'); if (!b || !tour) return;
    if (b.dataset.t === 'skip') endTour();
    else if (b.dataset.t === 'back') { tour.i--; drawTour(); }
    else if (tour.i === TOUR.length - 1) endTour(); else { tour.i++; drawTour(); }
  });

  function setMonths(m) {
    if (S.job) { toast('This monitor covers ' + (S.job.period || []).join(' → ') + '. Press “New area” to analyse another period.'); return; }
    S.months = m; if (S.sel) check(); else render();
  }

  window.GAIA_MAP = { locate: locate, map: map, state: S, on: function (f) { listeners.push(f); }, setMonths: setMonths,
                      showOverlay: showOverlay, apiUrl: apiUrl, refresh: function () { render(); } };
  setDraw(true); render(); loadMonitors();
  call('GET', '/health').then(function (h) {
    if (!h.earthdata_token) toast('Backend has no Earthdata token — analyses will fail');
  }).catch(function (e) { if (!S.sel) { S.msg = { kind: 'error', text: e.message }; render(); } toast(e.message); });
  var qs = new URLSearchParams(location.search), deep = qs.get('monitor'), dq = qs.get('q');  // ?monitor=<id> or ?q=<place>
  if (deep) { S.lastQuery = $('#dn') && $('#dn').textContent; openMonitor(deep); }
  else if (dq) locate(dq);
  else if (document.body.classList.contains('is-dash') && $('#dn')) locate($('#dn').textContent);
})();

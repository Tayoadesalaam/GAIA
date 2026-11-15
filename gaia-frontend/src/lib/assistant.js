/* Gaia Assistant: answers questions from the GAIA backend's real NISAR results.
   No language model — it routes the question to /monitors (finished analyses) or
   /locate + /availability (what data exists) and phrases the numbers it gets back. */
const API = (window.GAIA_CONFIG && window.GAIA_CONFIG.apiBase) || '/api';
const OFFLINE = 'The GAIA backend is not reachable right now, so I can’t read any results. Start it with “uvicorn gaia.api:app --port 8000” and ask again.';

async function call(path, body) {
  let r;
  try { r = await fetch(API + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}); }
  catch { throw { offline: true }; }
  const t = await r.text(); let j = null; try { j = JSON.parse(t); } catch { /* proxy error page */ }
  if (j === null) throw { offline: true };
  if (!r.ok) throw { status: r.status, body: j.detail || j };
  return j;
}

const INTENTS = [
  ['flood', /flood|water|inundat|lagoon|river|wet/i],
  ['vegetation', /plant|season|crop|vegetat|green|harvest|sow|farm/i],
  ['deformation', /ground|sink|subsid|deform|moving|motion|uplift|settl/i],
  ['data', /data|coverage|pass|available|availability|satellite|when.*(observ|image)/i],
  ['confidence', /sure|confiden|trust|reliab|accura|limit/i],
];
export const intentOf = q => (INTENTS.find(([, re]) => re.test(q)) || ['summary'])[0];

const STOP = /\b(is|are|the|a|an|it|there|what|when|where|how|should|i|we|to|of|in|at|for|near|around|does|do|data|ground|land|flood(ing)?|water|plant(ing)?|season|crops?|moving|sinking|sure|this|show|me|tell|about|any|was|be|can|you|vegetation|deformation|available|coverage)\b/gi;
export function placeOf(q) {
  const m = q.match(/\b(?:in|at|for|near|around|of)\s+([^?.!]+)/i);
  const p = (m ? m[1] : q.replace(STOP, ' ')).replace(/[?.!,]/g, ' ').replace(/\s+/g, ' ').trim();
  return p.length >= 2 ? p : '';
}

const day = d => d ? new Date(d + 'T00:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : '—';
const n = (v, d = 1) => v == null || isNaN(v) ? '—' : (+v).toLocaleString('en-GB', { maximumFractionDigits: d });
const sgn = (v, d = 1) => v == null ? '—' : (v > 0 ? '+' : '') + n(v, d);

function read(k, r) {
  const s = r.stats || {};
  if (k === 'flood') return `Flood peak on ${day(s.peak_date)}: ${n(s.peak_flood_total_km2, 2)} km² flooded beyond normal water (${n(s.normal_water_km2, 0)} km²), from ${s.passes_used}/${s.passes_total} usable passes.`
    + ((s.flagged_passes_with_larger_flood || []).length ? ` Rain- or wind-flagged passes (${s.flagged_passes_with_larger_flood.map(day).join(', ')}) showed more water but were excluded from the peak.` : '');
  if (k === 'water_change') return `Between ${day(r.dates?.[0])} and ${day(r.dates?.[1])}: ${n(s.new_water_km2, 2)} km² new water, ${n(s.receded_water_km2, 2)} km² receded.`;
  if (k === 'vegetation') { const z = r.season || {};
    return z.season_start ? `Radar saw the canopy start growing around ${day(z.season_start)}${z.peak ? `, peaking ${day(z.peak)}` : ''}${z.estimated_planting_window ? `; planting most likely ${day(z.estimated_planting_window[0])} – ${day(z.estimated_planting_window[1])} (an estimate: radar sees canopy, not sowing)` : ''}.`
      : `No clear growing season in the radar record (${n((s.class_fraction || {}).no_seasonal_signal * 100, 0)}% of the area shows no seasonal signal).`; }
  if (k === 'deformation') { const p = s.velocity_mm_yr_percentiles || {};
    return `Median line-of-sight motion ${sgn(p['50'])} mm/yr over ${n(s.span_days, 0)} days, against a noise level of ±${n(s.velocity_noise_mm_yr)} mm/yr`
      + (Math.abs(p['50']) < (s.velocity_noise_mm_yr || 0) ? ' — no credible deformation signal yet.' : '.'); }
  return '';
}
const NAME = { water_change: 'Water change', flood: 'Flooding', vegetation: 'Vegetation', deformation: 'Deformation' };

function fromMonitor(m, job, intent) {
  const R = job?.results || {}, ks = Object.keys(R);
  const want = { flood: ['flood', 'water_change'], vegetation: ['vegetation'], deformation: ['deformation'] }[intent];
  const link = { href: `/dashboard?monitor=${m.id}`, label: 'Open this analysis' };
  if (!ks.length) return { text: `${m.name} is being monitored, but no engine has finished yet (job ${job?.status || 'pending'}).`, link };
  if (want) {
    const k = want.find(x => R[x]);
    if (!k) return { text: `${m.name} has no ${intent === 'flood' ? 'flood' : intent} analysis yet. Finished: ${ks.map(x => NAME[x]).join(', ')}.`, link };
    return { text: read(k, R[k]), facts: [['Confidence', R[k].confidence], ['Why', (R[k].confidence_reasons || [])[0]]], src: sources(R[k], k), link };
  }
  if (intent === 'confidence') return { text: ks.map(k => `${NAME[k]}: ${R[k].confidence}`).join(' · '), facts: ks.map(k => [NAME[k], (R[k].confidence_reasons || [])[0]]), link };
  if (intent === 'data') { const d = [...new Set(ks.flatMap(k => R[k].dates || []))].sort();
    return { text: `${d.length} NISAR passes analysed for ${m.name}, ${day(d[0])} to ${day(d[d.length - 1])}.`, src: 'NISAR L2 GCOV / GUNW (NASA ASF DAAC), GPM IMERG rainfall', link }; }
  return { text: ks.map(k => read(k, R[k])).join(' '), facts: ks.map(k => [NAME[k], R[k].confidence]), link };
}
function sources(r, k) {
  return (k === 'deformation' ? 'NISAR L2 GUNW interferograms' : 'NISAR L2 GCOV backscatter') + (r.dates?.length ? `, ${r.dates.length} pass${r.dates.length > 1 ? 'es' : ''} (${day(r.dates[0])} – ${day(r.dates[r.dates.length - 1])})` : '') + (k !== 'deformation' ? ', GPM IMERG rain flags' : '');
}

function fromAvailability(a, loc, place) {
  const E = a.engines || {}, ok = Object.keys(E).filter(k => E[k].available), g = a.gcov || {};
  const tr = (g.tracks || []).map(t => `track ${t.track} ${t.flight_direction?.toLowerCase()} (${t.dates.length} passes, ${t.radar_mode})`).join('; ');
  return {
    text: `${loc.name}: ${g.granules_found || 0} NISAR acquisitions in the last year${tr ? ' — ' + tr : ''}. Not analysed yet. ${ok.length ? `Enough data for ${ok.map(k => NAME[k].toLowerCase()).join(', ')}.` : 'Not enough data for any engine yet.'}`,
    facts: Object.keys(E).map(k => [NAME[k], E[k].available ? 'available' : E[k].reason]),
    src: 'NASA CMR search via asf_search (metadata only)',
    link: ok.length ? { href: `/dashboard?q=${encodeURIComponent(place)}`, label: 'Start monitoring in the dashboard' } : null,
  };
}

export async function loadMonitors() { try { return await call('/monitors'); } catch (e) { if (e.offline) return null; return []; } }

export async function ask(q, monitors) {
  const intent = intentOf(q), place = placeOf(q);
  try {
    const words = (place || q).toLowerCase().split(/[\s,]+/).filter(w => w.length >= 3);
    const m = (monitors || []).filter(x => x.last_job_id && words.some(w => (x.query + ' ' + x.name).toLowerCase().includes(w)))
      .sort((a, b) => (b.last_run_status === 'done') - (a.last_run_status === 'done') || String(b.updated).localeCompare(String(a.updated)))[0];
    if (m) { const d = await call(`/monitors/${m.id}`); return fromMonitor(m, d.last_job, intent); }
    if (!place) return { text: 'Name a place and I’ll read what GAIA has for it — for example “Is Lagos flooding?” or “What data is there for Kano?”' };
    let loc;
    try { loc = await call('/locate?q=' + encodeURIComponent(place)); }
    catch (e) { if (e.offline) throw e; return { text: `I couldn’t find “${place}”. Try a city, region, or “lat, lon”.` }; }
    try { const a = await call('/availability', { location: place }); return fromAvailability(a.availability, loc, place); }
    catch (e) { if (e.offline) throw e; const b = e.body || {};
      if (b.error === 'no_data') return { text: `${loc.name}: ${b.message || 'no usable NISAR data yet.'}`, src: 'NASA CMR search via asf_search' };
      return { text: `The NISAR search failed for ${loc.name}: ${typeof b === 'string' ? b : b.message || 'unknown error'}.` }; }
  } catch (e) { if (e.offline) return { text: OFFLINE, offline: true }; return { text: 'Something went wrong reading the results.' }; }
}

/* Gaia AI (Gemini on the backend). Returns null when it isn't configured or reachable,
   so the caller falls back to the results lookup above. */
export async function aiStatus() { try { return await call('/assistant'); } catch { return null; } }
export async function askAI(q, history) {
  try {
    const j = await call('/assistant/chat', { message: q, history: history.slice(-8) });
    const g = j.grounded || {};
    return { text: j.reply, md: true, src: (j.sources || []).join(' · ') || null, warn: j.warnings || [],
      link: g.monitor_id ? { href: `/dashboard?monitor=${g.monitor_id}`, label: 'Open this analysis' } : null };
  } catch (e) {
    if (e.offline || e.status === 503) return null;
    const d = e.body || {};
    return { text: (d && d.message) || (typeof d === 'string' ? d : 'Gaia AI could not answer right now.'), warn: [] };
  }
}

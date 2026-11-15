/* Gaia AI overlay for the dashboard: a floating chat grounded in the area on screen.
 * POST /assistant/chat {message, history, monitor_id} → Gemini on the backend (key never in the browser).
 * The backend adds the core guardrails, the mask (gaia/assistant_mask.md) and the area's results. */
(function () {
  'use strict';
  var CFG = window.GAIA_CONFIG || {}, API = (CFG.apiBase || '/api').replace(/\/$/, '');
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var GM = window.GAIA_MAP || { state: {} };
  var A = { open: false, busy: false, status: null, msgs: [], hist: [] };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  // Minimal, safe Markdown: escape first, then bold/italic/lists/paragraphs.
  function md(t) {
    var out = [], list = null;
    esc(t).split(/\n/).forEach(function (line) {
      var l = line.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/(^|\W)_(.+?)_(?=\W|$)/g, '$1<em>$2</em>').replace(/(^|\W)\*(?!\s)(.+?)\*(?=\W|$)/g, '$1<em>$2</em>');
      var m = l.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);
      if (m) { if (!list) { list = []; } list.push('<li>' + m[1] + '</li>'); return; }
      if (list) { out.push('<ul>' + list.join('') + '</ul>'); list = null; }
      if (l.trim()) out.push('<p>' + l + '</p>');
    });
    if (list) out.push('<ul>' + list.join('') + '</ul>');
    return out.join('');
  }
  function area() {
    var S = GM.state || {}, m = S.monitor, j = S.job;
    var id = (m && m.id) || (j && j.monitor_id) || null;
    var name = m ? (m.query || m.name) : (S.sel && S.sel.label) || null;
    name = name ? String(name).split(/[,—]/)[0].trim() : null;
    return { id: id, name: name ? name.charAt(0).toUpperCase() + name.slice(1) : null };
  }
  function suggestions() {
    var a = area(), n = a.name, tab = (window.GAIA_DASH && GAIA_DASH.tab()) || 'overview';
    if (!a.id) return ['What can GAIA tell me?', 'Who made GAIA?', 'How does NISAR see through clouds?'];
    var by = { deformation: ['Is the ground moving in ' + n + '?', 'Why is the deformation confidence low?'],
               vegetation: ['When did the growing season start in ' + n + '?', 'When should farmers plant here?'],
               flooding: ['Is ' + n + ' flooding?', 'Which passes were affected by rain?'] }[tab];
    return (by || ['Summarise ' + n + ' for me', 'How sure is this reading?']).concat(['What are the limitations?']);
  }

  var root = document.createElement('div'); root.id = 'gai';
  root.innerHTML = '<button class="gai-fab" aria-expanded="false" aria-controls="gai-p"><i class="gai-orb" aria-hidden="true"></i><span>Ask Gaia</span></button>' +
    '<section class="gai-p glass" id="gai-p" role="dialog" aria-label="Gaia AI" hidden>' +
    '<header><i class="gai-orb" aria-hidden="true"></i><div><b>Gaia AI</b><small id="gai-st">Connecting…</small></div><button class="gai-x" aria-label="Close">×</button></header>' +
    '<div class="gai-ctx" id="gai-ctx"></div><div class="gai-log" id="gai-log" aria-live="polite"></div><div class="gai-sg" id="gai-sg"></div>' +
    '<form class="gai-f"><input id="gai-in" maxlength="1000" autocomplete="off" placeholder="Ask about this area or GAIA…" aria-label="Message Gaia"><button class="btn solid" aria-label="Send">↑</button></form>' +
    '<p class="gai-fine">Answers come from GAIA’s results via Gemini and can be wrong. Check figures in the panels.</p></section>';
  document.body.appendChild(root);
  var fab = $('.gai-fab', root), panel = $('.gai-p', root), log = $('#gai-log'), input = $('#gai-in');

  function render() {
    var a = area(), st = A.status;
    $('#gai-st').textContent = !st ? 'Connecting…' : st.offline ? 'Backend offline' : st.enabled ? (st.model || 'Gemini') + ' · ' + (st.mask || 'Gaia') : 'Not configured';
    $('#gai-ctx').innerHTML = a.id ? 'Grounded in <b>' + esc(a.name || a.id) + '</b> results' : 'No area open — answers cover monitored areas and GAIA itself';
    var h = '';
    if (!A.msgs.length) h += '<div class="gai-m"><p>' + esc(st && st.greeting || 'Hi, I’m Gaia. Ask me about the area on the map or how GAIA works.') + '</p></div>';
    if (st && !st.offline && !st.enabled) h += '<div class="gai-m warn"><p>Gaia AI isn’t configured. Add <b>GEMINI_API_KEY</b> to the backend’s <b>.env</b> and restart it.</p></div>';
    if (st && st.offline) h += '<div class="gai-m warn"><p>The GAIA backend isn’t reachable.</p></div>';
    A.msgs.forEach(function (m) {
      h += '<div class="gai-m' + (m.me ? ' me' : '') + (m.err ? ' warn' : '') + '">' + (m.me ? '<p>' + esc(m.text) + '</p>' : md(m.text)) +
        (m.src && m.src.length ? '<small>Sources: ' + esc(m.src.join(' · ')) + '</small>' : '') +
        (m.warn && m.warn.length ? '<small class="w">⚑ ' + esc(m.warn.join(' ')) + '</small>' : '') + '</div>';
    });
    if (A.busy) h += '<div class="gai-m typing"><i></i><i></i><i></i></div>';
    log.innerHTML = h; log.scrollTop = log.scrollHeight;
    $('#gai-sg').innerHTML = A.busy || A.msgs.length > 2 ? '' : suggestions().map(function (s) { return '<button type="button">' + esc(s) + '</button>'; }).join('');
    $('.gai-f button', root).disabled = A.busy;
  }
  function setOpen(on) {
    A.open = on; panel.hidden = !on; fab.setAttribute('aria-expanded', String(on)); root.classList.toggle('open', on);
    if (on) { if (!A.status) loadStatus(); render(); setTimeout(function () { input.focus(); }, 50); }
  }
  function loadStatus() {
    fetch(API + '/assistant').then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .then(function (s) { A.status = s; render(); }, function () { A.status = { offline: true }; render(); });
  }
  function send(text) {
    text = String(text || '').trim(); if (!text || A.busy) return;
    input.value = ''; A.msgs.push({ me: true, text: text }); A.busy = true; render();
    fetch(API + '/assistant/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: A.hist.slice(-8), monitor_id: area().id }) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
      .then(function (x) {
        if (!x.ok) {
          var d = x.j && x.j.detail, m = (d && d.message) || (typeof d === 'string' ? d : 'Gaia AI failed (HTTP ' + x.status + ').');
          if (x.status === 503) A.status = Object.assign({}, A.status, { enabled: false });
          A.msgs.push({ text: m, err: true }); return;
        }
        A.hist.push({ role: 'user', text: text }, { role: 'model', text: x.j.reply });
        A.msgs.push({ text: x.j.reply, src: x.j.sources, warn: x.j.warnings });
      }, function () { A.msgs.push({ text: 'The GAIA backend isn’t reachable.', err: true }); })
      .then(function () { A.busy = false; render(); });
  }

  fab.addEventListener('click', function () { setOpen(!A.open); });
  $('.gai-x', root).addEventListener('click', function () { setOpen(false); fab.focus(); });
  $('.gai-f', root).addEventListener('submit', function (e) { e.preventDefault(); send(input.value); });
  $('#gai-sg').addEventListener('click', function (e) { var b = e.target.closest('button'); if (b) send(b.textContent); });
  document.addEventListener('click', function (e) { var b = e.target.closest('[data-ai]'); if (b) { setOpen(true); if (b.dataset.ai) send(b.dataset.ai); } });
  addEventListener('keydown', function (e) { if (e.key === 'Escape' && A.open && panel.contains(document.activeElement)) { e.stopImmediatePropagation(); setOpen(false); fab.focus(); } }, true);
  // a new area clears the conversation so answers never mix places
  var lastId = area().id;
  if (GM.on) GM.on(function () { var id = area().id; if (id !== lastId) { lastId = id; A.hist = []; if (A.msgs.length) A.msgs.push({ text: 'Switched to ' + (area().name || 'a new area') + '.' }); } if (A.open) render(); });
  window.GAIA_AI = { open: function (q) { setOpen(true); if (q) send(q); } };
})();

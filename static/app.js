(function () {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = v => '$' + Number(v || 0).toFixed(Number(v) < 0.1 ? 4 : 2);
  const md = s => {
    const html = window.marked ? marked.parse(s, { breaks: true, gfm: true }) : '<p>' + esc(s).replace(/\n/g, '<br>') + '</p>';
    return window.DOMPurify ? DOMPurify.sanitize(html, { USE_PROFILES: { html: true } }) : esc(s);
  };
  function toast(t, ms = 2600) { const el = $('#toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove('show'), ms); }
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

  /* ---------- api ---------- */
  let me = null;
  async function api(path, opts = {}) {
    const headers = Object.assign({ 'X-Requested-With': 'fetch' }, opts.headers || {});
    const r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts, { headers }));
    if (r.status === 401 && path !== '/api/login' && path !== '/api/me') { showLogin('Your session ended. Please sign in again.'); throw new Error('signed out'); }
    return r;
  }
  const post = (path, body, extra = {}) => api(path, Object.assign({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }, extra));

  /* ---------- theme ---------- */
  const root = document.documentElement;
  let theme = store.get('theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  function applyTheme(t) { theme = t; root.setAttribute('data-theme', t); store.set('theme', t); }
  applyTheme(theme);

  /* ---------- branding ---------- */
  let brand = { name: 'ERP Pulse', subtitle: '', library: [] };
  async function loadBranding() {
    try { const j = await (await fetch('/api/branding')).json(); brand = Object.assign(brand, j.brand || {}, { library: j.library || [] }); } catch (e) {}
    document.title = brand.name; $$('.brandname').forEach(el => el.textContent = brand.name); $('#brandsub').textContent = brand.subtitle || 'read-only';
  }

  /* ---------- login ---------- */
  const loginEl = $('#login'), appEl = $('#app');
  function showLogin(msg) { appEl.hidden = true; loginEl.hidden = false; $('#loginerr').textContent = msg || ''; setTimeout(() => $('#lu').focus(), 50); }
  $('#loginform').onsubmit = async e => {
    e.preventDefault();
    const btn = $('#loginbtn'); btn.disabled = true; $('#loginerr').textContent = ''; btn.textContent = 'Signing in…';
    try {
      const r = await post('/api/login', { username: $('#lu').value.trim(), password: $('#lp').value });
      const j = await r.json();
      if (!r.ok) { $('#loginerr').textContent = j.error || 'Sign-in failed.'; return; }
      $('#lp').value = '';
      await boot();
    } catch (err) { $('#loginerr').textContent = 'Server not reachable.'; }
    finally { btn.disabled = false; btn.textContent = 'Sign in'; }
  };

  /* ---------- views ---------- */
  const thread = $('#thread'), inner = $('#inner'), hero = $('#hero'), q = $('#q'), send = $('#send');
  const views = { chat: $('#chatview'), profile: $('#profile'), admin: $('#admin') };
  let view = 'chat';
  function showView(v) {
    view = v; Object.entries(views).forEach(([k, el]) => el.hidden = k !== v);
    if (v === 'profile') renderProfile();
    if (v === 'admin') loadAdmin();
    if (v === 'chat') setTimeout(() => q.focus(), 30);
    closeSide();
  }
  $('#mebtn').onclick = () => showView('profile');
  $('#backchat1').onclick = () => showView('chat');
  $('#backchat2').onclick = () => showView('chat');
  $('#backprofile').onclick = () => showView('profile');
  $('#adminbtn').onclick = () => showView('admin');
  const nearBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
  function scrollDown(force) { if (force || scrollDown.stick) thread.scrollTop = thread.scrollHeight; }
  scrollDown.stick = true;
  thread.addEventListener('scroll', () => { scrollDown.stick = nearBottom(); $('#scrolldown').classList.toggle('show', !scrollDown.stick); });
  $('#scrolldown').onclick = () => { scrollDown.stick = true; scrollDown(true); };
  const side = $('#side'), scrim = $('#scrim');
  function closeSide() { side.classList.remove('open'); scrim.classList.remove('show'); }
  $('#menu').onclick = () => { side.classList.add('open'); scrim.classList.add('show'); };
  scrim.onclick = closeSide;

  /* ---------- boot ---------- */
  async function boot() {
    const r = await api('/api/me');
    if (r.status === 401) { const j = await r.json().catch(() => ({})); showLogin(j.users_exist === false ? 'No users yet. Create the first admin with: python manage.py create <name> admin' : ''); return; }
    me = await r.json();
    loginEl.hidden = true; appEl.hidden = false;
    renderMe(); applySqlPref();
    const h = new Date().getHours();
    $('#greet').innerHTML = (h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening') + ', ' + esc(me.user) + '.<br>What do you want to know <span class="grad">today</span>?';
    buildLibrary();
    showView('chat');
    await refreshConvs(true);
    refreshStatus();
  }
  function renderMe() {
    $('#uname').textContent = me.user; $('#uav').textContent = me.user.slice(0, 2).toUpperCase();
    $('#urolesub').textContent = me.role === 'admin' ? 'Administrator · all modules' : ((me.modules || []).join(', ') || 'no modules yet');
    $('#adminbtn').hidden = me.role !== 'admin';
    const pill = $('#modelpill'); pill.textContent = me.model_label || me.model || ''; pill.classList.toggle('local', me.provider === 'ollama');
    $('#heroline').textContent = me.provider === 'ollama' ? 'Local model · nothing leaves this PC' : 'Connected to the live ERP · read-only';
  }

  /* ---------- SQL visibility ---------- */
  let showSql = store.get('showSql') === '1';
  function applySqlPref() { const on = !!(me && me.show_sql && showSql); document.body.classList.toggle('hide-sql', !on); }

  /* ---------- question library (chat empty state) ---------- */
  const ICON = {
    box: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3 21 7.5v9L12 21l-9-4.5v-9L12 3Z"/><path d="M3 7.5l9 4.5 9-4.5M12 12v9"/></svg>',
    move: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l6-6 4 4 6-8"/><path d="M14 7h6v6"/></svg>',
    truck: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 7h11v9H3zM14 10h4l3 3v3h-7z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    site: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 21h18M5 21V8l7-4 7 4v13M9 21v-6h6v6"/></svg>',
    money: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M14.5 9.5c-.5-1-1.5-1.5-2.5-1.5-1.7 0-3 .9-3 2s1.3 2 3 2 3 .9 3 2-1.3 2-3 2c-1 0-2-.5-2.5-1.5M12 6v2M12 16v2"/></svg>'
  };
  let libBuilt = false;
  function buildLibrary() {
    if (libBuilt) return; libBuilt = true;
    const groups = brand.library.filter(g => (g.questions || []).length);
    const cats = $('#cats'), chips = $('#chips');
    if (!groups.length) { cats.hidden = true; return; }
    function pick(i) {
      $$('.cat', cats).forEach((c, j) => c.classList.toggle('sel', j === i));
      chips.innerHTML = '';
      groups[i].questions.forEach(t => { const b = document.createElement('button'); b.className = 'chip'; b.textContent = t; b.onclick = () => { q.value = t; ask(); }; chips.appendChild(b); });
    }
    groups.forEach((g, i) => { const b = document.createElement('button'); b.className = 'cat'; b.innerHTML = (ICON[g.icon] || ICON.box) + '<span></span>'; $('span', b).textContent = g.title; b.onclick = () => pick(i); cats.appendChild(b); });
    pick(0);
  }

  /* ---------- conversations ---------- */
  let convs = [], activeConv = null;
  async function refreshConvs(loadActive) {
    try {
      const j = await (await api('/api/conversations')).json();
      convs = j.conversations || []; const prev = activeConv; activeConv = j.active;
      renderConvs();
      if (loadActive || prev !== activeConv) await loadHistory(activeConv);
    } catch (e) {}
  }
  function renderConvs() {
    const box = $('#convs'); box.innerHTML = '';
    const real = convs.filter(c => c.turns > 0 || c.active);
    if (!real.length) { box.innerHTML = '<div class="empty-note">Your conversations appear here.</div>'; return; }
    real.forEach(c => {
      const b = document.createElement('button'); b.className = 'conv' + (c.active ? ' active' : ''); b.title = c.title;
      b.innerHTML = '<span class="ico"></span><span class="t"></span><span class="m"></span><button class="del" title="Delete conversation">×</button>';
      $('.t', b).textContent = c.title; $('.m', b).textContent = c.turns ? c.turns + (c.turns === 1 ? ' q' : ' q') : c.started;
      b.onclick = e => { if (e.target.closest('.del')) return; switchConv(c.id); };
      $('.del', b).onclick = async e => { e.stopPropagation(); if (busy) { toast('Wait for the current answer first.'); return; } if (c.turns && !confirm('Delete this conversation?')) return; try { const j = await (await post('/api/conversations', { action: 'delete', id: c.id })).json(); convs = j.conversations; activeConv = j.active; renderConvs(); await loadHistory(activeConv); } catch (err) { toast('Failed'); } };
      box.appendChild(b);
    });
  }
  async function switchConv(id) {
    if (busy) { toast('Wait for the current answer or press Esc to stop it.'); return; }
    if (id === activeConv) { showView('chat'); return; }
    try { const j = await (await post('/api/conversations', { action: 'switch', id })).json(); convs = j.conversations; activeConv = j.active; renderConvs(); await loadHistory(activeConv); showView('chat'); refreshStatus(); } catch (e) { toast('Failed'); }
  }
  $('#newchat').onclick = async () => {
    if (busy) { toast('Wait for the current answer or press Esc to stop it.'); return; }
    try { const j = await (await post('/api/conversations', { action: 'new' })).json(); convs = j.conversations; activeConv = j.active; renderConvs(); await loadHistory(activeConv); } catch (e) { return; }
    showView('chat'); refreshStatus();
  };
  async function loadHistory(cid) {
    inner.querySelectorAll('.turn').forEach(t => t.remove()); hero.hidden = false;
    try {
      const h = await (await api('/api/history?c=' + encodeURIComponent(cid || ''))).json();
      (h.turns || []).forEach(t => { const ctx = newTurn(t.q); t.events.forEach(ev => handle(ctx, ev)); finish(ctx); });
      if ((h.turns || []).length) scrollDown(true);
    } catch (e) {}
  }

  /* ---------- status ---------- */
  let st = null, dbState = null;
  async function refreshStatus() {
    try {
      st = await (await api('/api/status')).json(); me = Object.assign(me || {}, st); renderMe(); applySqlPref();
      $('#sesscost').textContent = st.session_cost ? money(st.session_cost) + ' this conversation' : '';
      if (view === 'profile') renderProfile();
    } catch (e) {}
  }
  async function checkDb() {
    try { const d = await (await api('/api/dbcheck')).json(); dbState = d.ok ? { ok: true, text: d.db } : { ok: false, text: 'DB error: ' + d.error }; }
    catch (e) { dbState = { ok: false, text: 'Server not reachable' }; }
    if (view === 'profile') renderProfile(false);
  }

  /* ---------- helpers ---------- */
  const NUMRE = /^-?\d+(?:\.\d+)?$/;
  const CODEHDR = /(ID$|NUMBER|CODE|DOCID|DOCNO|^NO$|_NO$|REF|YEAR|^YR$|MON|PREFIX|PHONE)/i;
  const fmtCell = (v, hdr) => { if (!NUMRE.test(v) || CODEHDR.test(hdr)) return esc(v); const n = Number(v); return isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 3 }) : esc(v); };
  const isNumCol = (rows, i, hdr) => !CODEHDR.test(hdr) && rows.length > 0 && rows.every(r => r[i] === '' || NUMRE.test(r[i]));
  const KW = 'select|from|where|and|or|not|in|is|null|as|on|join|left|right|inner|outer|full|cross|group|by|order|having|with|case|when|then|else|end|distinct|union|all|fetch|first|next|rows|only|like|between|exists|desc|asc|over|partition|nvl|nvl2|sum|count|round|max|min|avg|trunc|to_char|to_date|to_number|upper|lower|sysdate|add_months|months_between|last_day|date|interval|listagg|within|coalesce|abs|extract|row_number|rank|dense_rank|decode|cast|substr|instr|regexp_substr|regexp_like|greatest|least|sign|floor|ceil|lag|lead';
  function hlSql(sql) {
    const re = new RegExp("(--[^\\n]*)|('(?:[^']|'')*')|(\\b\\d+(?:\\.\\d+)?\\b)|(\\b(?:" + KW + ")\\b)", 'gi');
    let out = '', last = 0, m;
    while ((m = re.exec(sql))) { out += esc(sql.slice(last, m.index)); const cls = m[1] ? 'c' : m[2] ? 's' : m[3] ? 'n' : 'k'; out += '<span class="' + cls + '">' + esc(m[0]) + '</span>'; last = re.lastIndex; }
    return out + esc(sql.slice(last));
  }
  async function copyText(t, btn) { try { await navigator.clipboard.writeText(t); const o = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = o, 1200); } catch (e) { toast('Copy failed'); } }
  async function downloadCsv(i, cid) {
    try {
      const r = await api('/api/csv?i=' + i + '&c=' + encodeURIComponent(cid || '')); if (!r.ok) throw new Error(r.statusText);
      const url = URL.createObjectURL(await r.blob()); const a = document.createElement('a'); a.href = url; a.download = 'result-' + (i + 1) + '.csv'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 3000);
    } catch (e) { toast('Download failed: ' + e.message); }
  }
  const fmtSecs = s => s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';

  /* ---------- rendering a turn ---------- */
  const SVG_BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"><path d="M12 2.5 21 7v10l-9 4.5L3 17V7l9-4.5Z"/><path d="M3 7l9 4.5L21 7M12 11.5V21.5"/></svg>';
  const SVG_CHEV = '<svg class="chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg>';
  const SVG_DL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M6 11l6 6 6-6M4 21h16"/></svg>';
  const SVG_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M7 11v9H4v-9h3Zm0 0 4-7c1.5 0 2.5 1 2.5 2.5V10H19a2 2 0 0 1 2 2l-1.5 6.5A2 2 0 0 1 17.5 20H7"/></svg>';
  const SVG_DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M17 13V4h3v9h-3Zm0 0-4 7c-1.5 0-2.5-1-2.5-2.5V14H5a2 2 0 0 1-2-2l1.5-6.5A2 2 0 0 1 6.5 4H17"/></svg>';

  function newTurn(question) {
    hero.hidden = true;
    const t = document.createElement('div'); t.className = 'turn';
    t.innerHTML = '<div class="u-row"><div class="u-bubble"></div></div><div class="a-row"><div class="avatar">' + SVG_BOT + '</div><div class="a-body"><div class="steps"></div><div class="working"><span class="orb"></span><span class="wtxt">Thinking…</span><span class="timer"></span></div><div class="answer" hidden><button class="acopy">Copy</button><div class="amd"></div></div><div class="a-foot"></div></div></div>';
    $('.u-bubble', t).textContent = question;
    inner.appendChild(t);
    const ctx = { el: t, body: $('.a-body', t), steps: $('.steps', t), working: $('.working', t), wtxt: $('.wtxt', t), timer: $('.timer', t), answer: $('.answer', t), amd: $('.amd', t), foot: $('.a-foot', t), text: '', lastStep: null, turn: null, queries: 0, conv: activeConv, t0: Date.now(), tick: null };
    $('.acopy', t).onclick = e => copyText(ctx.text, e.currentTarget);
    scrollDown(true);
    return ctx;
  }
  function startTimer(ctx) { ctx.tick = setInterval(() => { ctx.timer.textContent = fmtSecs(Math.round((Date.now() - ctx.t0) / 1000)); }, 1000); }
  function finish(ctx) { ctx.working.hidden = true; if (ctx.tick) { clearInterval(ctx.tick); ctx.tick = null; } }
  function addStep(ctx, ev) {
    ctx.queries++;
    const d = document.createElement('details'); d.className = 'step';
    const isSql = ev.name === 'run_sql';
    d.innerHTML = '<summary><span class="tag ' + (isSql ? '' : 'schema') + '">' + (isSql ? 'SQL' : esc(ev.name.replace('_', ' '))) + '</span><span class="purpose"></span><span class="meta"><span class="mtext">running…</span>' + SVG_CHEV + '</span></summary><div class="inner"></div>';
    $('.purpose', d).textContent = ev.purpose || '';
    if (ev.sql) { const w = document.createElement('div'); w.className = 'sqlwrap'; w.innerHTML = '<pre class="sql">' + hlSql(ev.sql.trim()) + '</pre><button class="copy">Copy SQL</button>'; $('.copy', w).onclick = e => { e.preventDefault(); copyText(ev.sql.trim(), e.currentTarget); }; $('.inner', d).appendChild(w); }
    ctx.steps.appendChild(d); ctx.lastStep = d; scrollDown();
  }
  function renderGrid(host, ev) {
    const cols = ev.columns, rows = ev.rows.slice(); const numCols = cols.map((c, i) => isNumCol(rows, i, c)); let sortCol = -1, sortDir = 1;
    const g = document.createElement('div'); g.className = 'grid';
    function draw() {
      g.innerHTML = '<table><thead><tr>' + cols.map((c, i) => '<th class="' + (numCols[i] ? 'num' : '') + '" data-i="' + i + '">' + esc(c) + (sortCol === i ? '<span class="arr">' + (sortDir > 0 ? '▲' : '▼') + '</span>' : '') + '</th>').join('') + '</tr></thead><tbody>' +
        rows.map(r => '<tr>' + r.map((v, i) => '<td class="' + (numCols[i] ? 'num' : '') + '" title="' + esc(v) + '">' + fmtCell(v, cols[i]) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
      $$('th', g).forEach(th => th.onclick = () => { const i = +th.dataset.i; sortDir = sortCol === i ? -sortDir : 1; sortCol = i; rows.sort((a, b) => numCols[i] ? (Number(a[i] || 0) - Number(b[i] || 0)) * sortDir : String(a[i]).localeCompare(String(b[i]), undefined, { numeric: true }) * sortDir); draw(); });
    }
    draw(); host.appendChild(g);
  }
  function fillResult(ctx, ev) {
    const d = ctx.lastStep; if (!d) return;
    $('.mtext', d).textContent = ev.rowcount.toLocaleString() + (ev.truncated ? '+ rows' : ' rows') + ' · ' + ev.elapsed + 's';
    const inner2 = $('.inner', d);
    if (ev.rowcount) renderGrid(inner2, ev);
    const t = document.createElement('div'); t.className = 'tools';
    t.innerHTML = (ev.rowcount ? '<a class="dl" href="#" data-i="' + ev.index + '">' + SVG_DL + 'Download CSV</a>' : '<span>No rows returned</span>') + (ev.truncated ? '<span>Showing the first ' + ev.rowcount + ' rows (cap)</span>' : '') + '<span>Click a column header to sort</span>';
    inner2.appendChild(t);
    const dl = $('.dl', t); if (dl) dl.onclick = e => { e.preventDefault(); downloadCsv(+dl.dataset.i, ctx.conv); };
    if (!ev.rowcount) d.open = true;
  }
  function markError(ctx, text) { const d = ctx.lastStep; if (!d) return; d.classList.add('err'); $('.mtext', d).textContent = 'error, retrying'; const p = document.createElement('div'); p.className = 'errtext'; p.textContent = text; $('.inner', d).appendChild(p); }

  function feedbackBar(ctx) {
    const fb = document.createElement('span'); fb.className = 'fb';
    fb.innerHTML = '<button title="Good answer">' + SVG_UP + '</button><button title="Something was wrong">' + SVG_DOWN + '</button>';
    const [up, down] = $$('button', fb);
    up.onclick = () => sendFeedback(ctx, 'up', '', up, down);
    down.onclick = () => {
      if ($('.fbbox', ctx.body)) return;
      const box = document.createElement('div'); box.className = 'fbbox';
      box.innerHTML = '<textarea placeholder="What was wrong, and what is correct? ' + (me.role === 'admin' ? 'Your correction is saved as knowledge for future answers.' : 'The admin will review it.') + '"></textarea><button>Send</button>';
      $('button', box).onclick = () => { const txt = $('textarea', box).value.trim(); sendFeedback(ctx, 'down', txt, down, up); box.remove(); };
      ctx.body.appendChild(box); $('textarea', box).focus(); scrollDown();
    };
    return fb;
  }
  async function sendFeedback(ctx, vote, text, selBtn, otherBtn) {
    try {
      const r = await post('/api/feedback', { turn: ctx.turn, vote, text, conversation: ctx.conv }); const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'failed');
      selBtn.classList.add('sel'); otherBtn.classList.remove('sel');
      if (j.learned) showLearned(ctx, j.learned); else toast(vote === 'up' ? 'Thanks for the feedback' : 'Feedback recorded for the admin');
    } catch (e) { toast('Feedback failed: ' + e.message); }
  }
  function showLearned(ctx, note) { const l = document.createElement('div'); l.className = 'learned'; l.innerHTML = '<b>Learned (' + esc(note.kind) + ')</b> · saved for all future sessions<br>'; l.appendChild(document.createTextNode(note.text)); ctx.body.insertBefore(l, ctx.foot); scrollDown(); }

  function handle(ctx, ev) {
    switch (ev.type) {
      case 'ping': if (typeof ev.elapsed === 'number') ctx.timer.textContent = fmtSecs(ev.elapsed); break;
      case 'status': ctx.wtxt.textContent = ev.text === 'thinking' ? 'Thinking…' : 'Reading the results…'; break;
      case 'tool': addStep(ctx, ev); ctx.wtxt.textContent = me.show_sql && ev.purpose ? 'Running: ' + ev.purpose : 'Checking the ERP…'; break;
      case 'result': fillResult(ctx, ev); break;
      case 'sql_error': markError(ctx, ev.text); break;
      case 'learned': showLearned(ctx, ev); break;
      case 'text_delta': ctx.text += ev.text; ctx.answer.hidden = false; ctx.amd.innerHTML = md(ctx.text); scrollDown(); break;
      case 'text_break': ctx.text += '\n\n'; break;
      case 'text_reset': ctx.text = ''; ctx.amd.innerHTML = ''; ctx.answer.hidden = true; break;
      case 'error': finish(ctx); { const er = document.createElement('div'); er.className = 'errcard'; er.textContent = ev.text; ctx.body.insertBefore(er, ctx.foot); } break;
      case 'final':
        finish(ctx); ctx.turn = ev.turn;
        if (ev.text && ev.text.trim()) { ctx.text = ev.text; ctx.answer.hidden = false; ctx.amd.innerHTML = md(ev.text); }
        ctx.foot.innerHTML = '<span><b>' + (ev.provider === 'ollama' ? 'local · $0' : money(ev.cost)) + '</b></span><span>' + fmtSecs(Math.round(ev.elapsed)) + '</span>' + (me.show_sql && ctx.queries ? '<span>' + ctx.queries + (ctx.queries === 1 ? ' query' : ' queries') + '</span>' : '');
        if (typeof ev.turn === 'number') ctx.foot.appendChild(feedbackBar(ctx));
        scrollDown(); break;
    }
  }

  /* ---------- ask / stop ---------- */
  let busy = false, aborter = null;
  const ICON_SEND = send.innerHTML, ICON_STOP = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
  function setBusy(b) { busy = b; send.innerHTML = b ? ICON_STOP : ICON_SEND; send.classList.toggle('stop', b); send.title = b ? 'Stop (Esc)' : 'Send (Enter)'; }
  function stop() { if (busy && aborter) aborter.abort(); }
  async function ask() {
    const text = q.value.trim(); if (!text || busy) return;
    if (view !== 'chat') showView('chat');
    q.value = ''; autosize(); setBusy(true); scrollDown.stick = true;
    const ctx = newTurn(text); startTimer(ctx); aborter = new AbortController();
    const firstTurn = !inner.querySelectorAll('.turn')[1];
    try {
      const res = await post('/api/chat', { message: text, conversation: activeConv }, { signal: aborter.signal });
      if (!res.ok || !res.body) { let t = ''; try { t = (await res.json()).error; } catch (e) { t = res.statusText; } throw new Error(t || 'request failed'); }
      const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i; while ((i = buf.indexOf('\n\n')) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2); if (chunk.startsWith('data: ')) { try { handle(ctx, JSON.parse(chunk.slice(6))); } catch (e) { console.error(e); } } }
      }
      if (!ctx.working.hidden) { finish(ctx); if (!ctx.text) handle(ctx, { type: 'error', text: 'The connection closed before an answer arrived. Please ask again.' }); }
    } catch (e) {
      finish(ctx);
      if (e.name === 'AbortError') { const s = document.createElement('div'); s.className = 'stopped'; s.textContent = 'Stopped. This question was discarded from the conversation.'; ctx.body.insertBefore(s, ctx.foot); }
      else if (e.message !== 'signed out') handle(ctx, { type: 'error', text: 'Request failed: ' + e.message });
    } finally { setBusy(false); aborter = null; q.focus(); refreshStatus(); if (firstTurn) refreshConvs(false); else renderConvs(); }
  }

  /* ---------- composer wiring ---------- */
  function autosize() { q.style.height = 'auto'; q.style.height = Math.min(200, q.scrollHeight) + 'px'; }
  q.addEventListener('input', autosize);
  q.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } if (e.key === 'Escape') stop(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') stop(); if (e.key === '/' && document.activeElement !== q && !appEl.hidden && view === 'chat') { e.preventDefault(); q.focus(); } });
  send.onclick = () => busy ? stop() : ask();

  /* ---------- profile page ---------- */
  function renderProfile(fetchDb = true) {
    if (!me) return;
    const s = st || me;
    const isErpLogin = !!(me.erp_user && me.user === String(me.erp_user).toLowerCase());
    const mods = me.role === 'admin' ? ['all modules'] : (me.modules || []);
    const spentPct = s.budget ? Math.min(100, 100 * s.spent_today / s.budget) : 0;
    const myPct = s.my_budget ? Math.min(100, 100 * s.my_spent_today / s.my_budget) : 0;
    const db = dbState || { ok: null, text: 'Checking…' };
    $('#profile-content').innerHTML =
      '<div class="identity"><div class="av big">' + esc(me.user.slice(0, 2).toUpperCase()) + '</div><div class="who"><h2>' + esc(me.user) + '</h2><div class="sub"><span class="badge ' + (me.role === 'admin' ? '' : 'user') + '">' + esc(me.role) + '</span> &nbsp; ' + (me.erp_user ? 'ERP account <b>' + esc(me.erp_user) + '</b>' : 'app account') + (isErpLogin ? ' · signed in with ERP credentials' : '') + '</div></div></div>' +
      '<div class="pcards">' +
      '<section class="acard"><h3>Access</h3><p class="desc">' + (me.role === 'admin' ? 'Administrator: every module, learning mode on.' : 'Modules you can ask about. Anything else gets the privileges message.') + '</p><div class="mods">' + (mods.length ? mods.map(m => '<span class="badge mod">' + esc(m) + '</span>').join('') : '<span class="badge mod">no modules yet</span>') + '</div>' +
        ((me.groups || []).length ? '<p class="desc" style="margin:12px 0 6px">Your ERP groups</p><div class="mods">' + me.groups.map(g => '<span class="badge mod">' + esc(g) + '</span>').join('') + '</div>' : '') + '</section>' +
      '<section class="acard"><h3>Status</h3><p class="desc">Live connection and the model answering you.</p>' +
        '<div class="row"><span class="dot ' + (db.ok === true ? 'ok' : db.ok === false ? 'bad' : '') + '"></span><span id="dbtext">' + esc(db.text) + '</span></div>' +
        '<div class="row"><span>Model</span><span class="val">' + esc(s.model_label || s.model || '') + '</span></div>' +
        '<div class="row"><span>Answers</span><span class="val">' + (s.provider === 'ollama' ? 'local, nothing leaves this PC' : 'Claude API, read-only data') + '</span></div>' +
        '<div class="row"><span>This conversation</span><span class="val">' + (s.provider === 'ollama' ? '$0' : money(s.session_cost)) + '</span></div></section>' +
      '<section class="acard"><h3>Spend today</h3><p class="desc">Hard daily limits protect the budget.</p>' +
        '<div class="row"><span>You</span><span class="val">' + money(s.my_spent_today) + ' of $' + Number(s.my_budget || 0).toFixed(2) + '</span></div><div class="bar"><i style="width:' + myPct.toFixed(1) + '%"></i></div>' +
        '<div class="row" style="margin-top:14px"><span>Everyone</span><span class="val">' + money(s.spent_today) + ' of $' + Number(s.budget || 0).toFixed(2) + '</span></div><div class="bar"><i style="width:' + spentPct.toFixed(1) + '%"></i></div></section>' +
      '<section class="acard"><h3>Preferences</h3><p class="desc">Saved in this browser.</p>' +
        '<div class="toggle"><div class="l">Dark mode<small>Easy on the eyes at night.</small></div><button class="switch ' + (theme === 'dark' ? 'on' : '') + '" id="p-theme" aria-label="Dark mode"></button></div>' +
        (me.show_sql ? '<div class="toggle"><div class="l">Show the queries<small>See the SQL and result grids behind every answer.</small></div><button class="switch ' + (showSql ? 'on' : '') + '" id="p-sql" aria-label="Show queries"></button></div>' : '') + '</section>' +
      '<section class="acard"><h3>Account</h3><p class="desc">' + (isErpLogin ? 'You signed in with your ERP password; change it in the ERP.' : 'Your app password.') + '</p><div class="btnrow">' + (isErpLogin ? '' : '<button class="btn ghost" id="p-pw">Change password</button>') + '<button class="btn danger" id="p-logout">Sign out</button></div></section>' +
      (me.role === 'admin' ? '<section class="acard"><h3>Administration</h3><p class="desc">Users, ERP group mapping, model and cost, learned knowledge, feedback, security and the audit log.</p><div class="btnrow"><button class="btn" id="p-admin">Open admin panel</button></div></section>' : '') +
      '</div>';
    $('#p-theme').onclick = e => { applyTheme(theme === 'dark' ? 'light' : 'dark'); e.currentTarget.classList.toggle('on', theme === 'dark'); };
    const ps = $('#p-sql'); if (ps) ps.onclick = e => { showSql = !showSql; store.set('showSql', showSql ? '1' : '0'); applySqlPref(); e.currentTarget.classList.toggle('on', showSql); };
    const pw = $('#p-pw'); if (pw) pw.onclick = openPw;
    $('#p-logout').onclick = async () => { try { await post('/api/logout'); } catch (e) {} location.reload(); };
    const pa = $('#p-admin'); if (pa) pa.onclick = () => showView('admin');
    if (fetchDb) checkDb();
  }

  /* ---------- change password ---------- */
  function openPw() { $('#pwmodal').hidden = false; $('#pwerr').textContent = ''; $('#pwcur').value = $('#pwnew').value = ''; setTimeout(() => $('#pwcur').focus(), 50); }
  $('#pwcancel').onclick = () => { $('#pwmodal').hidden = true; };
  $('#pwform').onsubmit = async e => {
    e.preventDefault();
    try {
      const r = await post('/api/password', { current: $('#pwcur').value, new: $('#pwnew').value }); const j = await r.json();
      if (!r.ok) { $('#pwerr').textContent = j.error || 'Failed'; return; }
      $('#pwmodal').hidden = true; toast('Password changed. Please sign in again.', 4000); setTimeout(() => location.reload(), 1200);
    } catch (err) { $('#pwerr').textContent = 'Failed'; }
  };

  /* ---------- admin panel ---------- */
  let ov = null, groupsData = null;
  async function loadAdmin() {
    const box = $('#admin-content'); box.innerHTML = '<div class="working"><span class="orb"></span>Loading…</div>';
    try { const r = await api('/api/admin/overview'); if (!r.ok) throw new Error((await r.json()).error || 'failed'); ov = await r.json(); }
    catch (e) { box.innerHTML = '<div class="errcard">' + esc(e.message) + '</div>'; return; }
    groupsData = null;
    try { const g = await api('/api/admin/groups'); if (g.ok) groupsData = await g.json(); else groupsData = { error: (await g.json()).error }; } catch (e) { groupsData = { error: 'ERP not reachable' }; }
    renderAdmin();
  }
  function card(title, desc, bodyHtml) { return '<section class="acard wide"><h3>' + title + '</h3><p class="desc">' + desc + '</p>' + bodyHtml + '</section>'; }
  function renderAdmin() {
    const s = ov.settings, u = ov.usage, sec = ov.security, c = ov.costs, erp = ov.erp || { admin_users: [], modules: [] };
    const usersHtml = '<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>User</th><th>Role</th><th>ERP link</th><th>Created</th><th>Last sign-in</th><th></th></tr></thead><tbody>' +
      ov.users.map(x => '<tr><td><b>' + esc(x.username) + '</b></td><td><span class="badge ' + (x.role === 'admin' ? '' : 'user') + '">' + x.role + '</span></td><td>' + (x.erp_user ? esc(x.erp_user) : '<span class="badge mod">default modules</span>') + '</td><td>' + esc(x.created || '') + '</td><td>' + esc(x.last_login || 'never') + '</td>' +
        '<td class="acts"><button class="mini" data-act="link" data-u="' + esc(x.username) + '" data-erp="' + esc(x.erp_user || '') + '">ERP link</button><button class="mini" data-act="password" data-u="' + esc(x.username) + '">Reset password</button><button class="mini" data-act="role" data-u="' + esc(x.username) + '" data-role="' + (x.role === 'admin' ? 'user' : 'admin') + '">Make ' + (x.role === 'admin' ? 'user' : 'admin') + '</button><button class="mini danger" data-act="delete" data-u="' + esc(x.username) + '">Delete</button></td></tr>').join('') +
      '</tbody></table></div>' +
      '<form id="adduser" class="form-inline five"><div class="field"><label>New username</label><input name="username" required minlength="3" autocomplete="off"></div><div class="field"><label>Password (min 8)</label><input name="password" type="password" required minlength="8" autocomplete="new-password"></div><div class="field"><label>ERP username (optional)</label><input name="erp_user" placeholder="ERP login id" autocomplete="off"></div><div class="field"><label>Role</label><select name="role"><option value="user">user</option><option value="admin">admin</option></select></div><button class="btn" type="submit">Add user</button></form><div class="form-err" id="usererr"></div>' +
      '<p class="desc" style="margin-top:10px">ERP sign-in is ' + (s.erp_login ? 'ON: anyone with an active ERP account can sign in with their ERP username and password; their modules come from their ERP groups.' : 'OFF: only the accounts above can sign in.') + ' Assistant admins by ERP username: <b>' + (erp.admin_users.length ? erp.admin_users.map(esc).join(', ') : 'none') + '</b>.</p>';

    const ol = ov.ollama || { alive: false, models: [] };
    const providerHtml = '<div class="settings-grid" style="margin-bottom:12px"><div class="field"><label>Answering model</label><select id="s-provider"><option value="anthropic" ' + (s.provider !== 'ollama' ? 'selected' : '') + '>Claude API (Anthropic)</option><option value="ollama" ' + (s.provider === 'ollama' ? 'selected' : '') + '>Local model via Ollama (nothing leaves this PC)</option></select></div>' +
      '<div class="field"><label>Local model name</label><input id="s-omodel" value="' + esc(s.ollama_model || '') + '" placeholder="qwen3:8b"></div><div class="field"><label>Ollama URL</label><input id="s-ourl" value="' + esc(s.ollama_url || '') + '"></div></div>' +
      '<p class="desc">' + (ol.alive ? 'Ollama is running at ' + esc(ol.url) + '. Downloaded models: <b>' + (ol.models.length ? ol.models.map(esc).join(', ') : 'none yet (run: ollama pull ' + esc(s.ollama_model || 'qwen3:8b') + ')') + '</b>. Local answers cost $0 but take 1-3 minutes on a PC without a GPU; the Claude API answers in seconds.' : 'Ollama is not running on this PC. Start it (or install from ollama.com) to use a local model.') + '</p>';
    const modelsHtml = providerHtml + '<p class="desc" style="margin:0 0 6px"><b>Claude models</b> (used when the answering model is the Claude API):</p><div class="models">' + c.models.map(m => '<div class="model ' + (m.model === s.model ? 'sel' : '') + '" data-model="' + m.model + '"><b>' + esc(m.label) + '</b><div class="note">' + esc(m.note) + '</div><div class="nums"><span>per question <b>' + money(m.per_question) + '</b></span><span>10/day <b>$' + m.monthly_10_per_day.toFixed(0) + '/mo</b></span><span>50/day <b>$' + m.monthly_50_per_day.toFixed(0) + '/mo</b></span></div></div>').join('') + '</div>' +
      '<p class="desc">Estimates from the measured token profile of your last ' + c.profile.sample + ' questions (about ' + c.profile.cache_read.toLocaleString() + ' cached + ' + c.profile.uncached_input.toLocaleString() + ' fresh input tokens and ' + c.profile.output + ' output tokens per question).</p>' +
      '<div class="settings-grid">' +
      '<div class="field"><label>Effort</label><select id="s-effort">' + ov.efforts.map(e => '<option ' + (e === s.effort ? 'selected' : '') + '>' + e + '</option>').join('') + '</select></div>' +
      '<div class="field"><label>Daily budget, everyone ($)</label><input id="s-budget" type="number" step="0.5" min="0.5" value="' + s.daily_budget_usd + '"></div>' +
      '<div class="field"><label>Daily budget per user ($)</label><input id="s-ubudget" type="number" step="0.5" min="0.5" value="' + s.user_daily_budget_usd + '"></div>' +
      '<div class="field"><label>Rows kept per query (grid/CSV)</label><input id="s-rows" type="number" step="50" min="10" max="1000" value="' + s.max_rows + '"></div>' +
      '<div class="field"><label>Rows the model may read</label><input id="s-mrows" type="number" step="10" min="10" max="500" value="' + s.model_rows + '"></div>' +
      '</div><label class="check"><input id="s-showsql" type="checkbox" ' + (s.show_sql_to_users ? 'checked' : '') + '> Let restricted users see the SQL behind answers</label>' +
      '<label class="check"><input id="s-erplogin" type="checkbox" ' + (s.erp_login ? 'checked' : '') + '> Allow sign-in with ERP username and password</label>' +
      '<div class="save-row"><button class="btn" id="savesettings">Save settings</button><span class="msg" id="settingsmsg"></span></div>';

    let groupsHtml;
    if (!groupsData || groupsData.error) groupsHtml = '<div class="errcard">' + esc((groupsData && groupsData.error) || 'not loaded') + '</div>';
    else {
      const mods = groupsData.modules;
      groupsHtml = '<p class="desc">' + groupsData.groups.length + ' ERP groups with active members. Tick the modules a group may ask about. Modules marked "not enabled" have no tables yet, so their members get the privileges message until the module is taught.</p>' +
        '<div class="gwrap"><table class="tbl gtbl"><thead><tr><th>ERP group</th><th>Members</th><th>Modules</th></tr></thead><tbody>' +
        groupsData.groups.map(g => '<tr data-g="' + esc(g.group) + '"><td><b>' + esc(g.group) + '</b></td><td class="members">' + g.members + '</td><td>' + mods.map(m => '<label><input type="checkbox" value="' + esc(m.key) + '" ' + (g.modules.includes(m.key) ? 'checked' : '') + '> ' + esc(m.label) + (m.enabled ? '' : ' (not enabled)') + '</label>').join('') + '</td></tr>').join('') +
        '</tbody></table></div><div class="save-row"><button class="btn" id="savegroups">Save group mapping</button><span class="msg" id="groupsmsg"></span></div>';
    }

    const usageHtml = '<div class="kpis"><div class="kpi"><div class="l">Today</div><div class="v">' + money(u.today) + '</div></div><div class="kpi"><div class="l">Last 7 days</div><div class="v">' + money(u.last7) + '</div></div><div class="kpi"><div class="l">Last 30 days</div><div class="v">' + money(u.last30) + '</div></div><div class="kpi"><div class="l">Questions today</div><div class="v">' + u.questions_today + '</div></div><div class="kpi"><div class="l">Questions total</div><div class="v">' + u.questions_total + '</div></div><div class="kpi"><div class="l">Local ($0)</div><div class="v">' + (u.local_questions || 0) + '</div></div></div>' +
      (Object.keys(u.by_user_today).length ? '<table class="tbl"><thead><tr><th>User</th><th>Spent today</th></tr></thead><tbody>' + Object.entries(u.by_user_today).map(([k, v]) => '<tr><td>' + esc(k) + '</td><td>' + money(v) + '</td></tr>').join('') + '</tbody></table>' : '<p class="desc">No questions yet today.</p>');

    const knHtml = (ov.knowledge.length ? ov.knowledge.slice().reverse().map(n => '<div class="note-item"><span class="badge">' + esc(n.kind) + '</span><div class="txt">' + esc(n.text) + '<div class="m">' + esc(n.by) + ' · ' + esc(n.ts) + (n.question ? ' · from: ' + esc(n.question) : '') + '</div></div><button class="mini danger" data-note="' + n.id + '">Delete</button></div>').join('') : '<p class="desc">Nothing learned yet.</p>') +
      '<form id="addnote" class="form-inline" style="grid-template-columns:140px 1fr auto;align-items:start"><div class="field"><label>Kind</label><select name="kind"><option>rule</option><option>table</option><option>join</option><option>example</option><option>correction</option></select></div><div class="field"><label>Teach it something (free, no AI call)</label><textarea name="text" placeholder="e.g. STATUS = C means closed. Approvals are level 1..6; a blank level means a higher authority covered it." required minlength="8"></textarea></div><button class="btn" type="submit">Save</button></form><div class="form-err" id="noteerr"></div>';

    const fbHtml = ov.feedback.length ? '<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>When</th><th>User</th><th>Vote</th><th>Question</th><th>Comment</th></tr></thead><tbody>' + ov.feedback.map(f => '<tr><td>' + esc(f.ts.replace('T', ' ')) + '</td><td>' + esc(f.user) + '</td><td><span class="badge ' + f.vote + '">' + f.vote + '</span></td><td>' + esc(f.question) + '</td><td>' + esc(f.text || '') + '</td></tr>').join('') + '</tbody></table></div>' : '<p class="desc">No feedback yet.</p>';
    const secHtml = '<div class="kpis"><div class="kpi"><div class="l">Active sessions</div><div class="v">' + sec.active_sessions + '</div></div><div class="kpi"><div class="l">Failed sign-ins, 24 h</div><div class="v">' + sec.failed_logins_24h + '</div></div><div class="kpi"><div class="l">Locked accounts</div><div class="v">' + sec.locked_accounts + '</div></div></div><ul class="seclist">' + sec.notes.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul>';
    const auditHtml = '<div class="audit">' + esc(ov.audit.map(a => a.ts.replace('T', ' ') + '  ' + (a.event + '').padEnd(20) + ' ' + (a.user || '-').padEnd(12) + ' ' + Object.entries(a).filter(([k]) => !['ts', 'event', 'user'].includes(k)).map(([k, v]) => k + '=' + (typeof v === 'string' ? v.slice(0, 80) : JSON.stringify(v))).join(' ')).join('\n')) + '</div>';

    $('#admin-content').innerHTML = '<div class="pcards">' +
      card('Users &amp; sign-in', 'App accounts, ERP links and who is an assistant admin.', usersHtml) +
      card('ERP groups &rarr; modules', 'Roles and responsibilities come from the ERP. This table decides which modules each ERP group may ask about; the tables behind each module are enforced on every query.', groupsHtml) +
      card('Model &amp; cost', 'Pick the model that fits the budget. Fewer rows to the model means cheaper and faster answers.', modelsHtml) +
      card('Usage', 'Actual spend from the usage log.', usageHtml) +
      card('Learned knowledge', 'Teach the assistant here for free. Everything saved is added to its instructions for everyone.', knHtml) +
      card('Feedback', 'Thumbs up/down from all users. Admin thumbs-down with a correction becomes a learned note automatically.', fbHtml) +
      card('Security', 'What protects this application.', secHtml) +
      card('Audit log', 'Last 40 events from logs/audit.jsonl.', auditHtml) + '</div>';

    $$('#admin-content .model').forEach(m => m.onclick = () => { $$('#admin-content .model').forEach(x => x.classList.remove('sel')); m.classList.add('sel'); });
    $('#savesettings').onclick = async () => {
      const msg = $('#settingsmsg'); msg.textContent = ''; msg.className = 'msg';
      const sel = $('#admin-content .model.sel'); const model = sel ? sel.dataset.model : s.model;
      try {
        const r = await post('/api/admin/settings', { model, provider: $('#s-provider').value, ollama_model: $('#s-omodel').value, ollama_url: $('#s-ourl').value, effort: $('#s-effort').value, daily_budget_usd: +$('#s-budget').value, user_daily_budget_usd: +$('#s-ubudget').value, max_rows: +$('#s-rows').value, model_rows: +$('#s-mrows').value, show_sql_to_users: $('#s-showsql').checked, erp_login: $('#s-erplogin').checked });
        const j = await r.json(); if (!r.ok) throw new Error(j.error);
        msg.textContent = 'Saved'; refreshStatus(); ov.settings = j.settings;
      } catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
    };
    const sg = $('#savegroups');
    if (sg) sg.onclick = async () => {
      const msg = $('#groupsmsg'); msg.textContent = ''; msg.className = 'msg';
      const mapping = {};
      $$('#admin-content .gtbl tbody tr').forEach(tr => { mapping[tr.dataset.g] = $$('input:checked', tr).map(i => i.value); });
      try { const r = await post('/api/admin/groups', { mapping }); const j = await r.json(); if (!r.ok) throw new Error(j.error); msg.textContent = 'Saved. Applies at each user\'s next sign-in.'; }
      catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
    };
    $('#adduser').onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target); const err = $('#usererr'); err.textContent = '';
      try { const r = await post('/api/admin/users', { action: 'create', username: f.get('username'), password: f.get('password'), role: f.get('role'), erp_user: f.get('erp_user') }); const j = await r.json(); if (!r.ok) throw new Error(j.error); toast('User created'); loadAdmin(); }
      catch (ex) { err.textContent = ex.message; }
    };
    $$('#admin-content .tbl [data-act]').forEach(b => b.onclick = async () => {
      const act = b.dataset.act, uname = b.dataset.u; const body = { action: act, username: uname };
      if (act === 'delete' && !confirm('Delete user ' + uname + '?')) return;
      if (act === 'password') { const p = prompt('New password for ' + uname + ' (min 8 characters):'); if (!p) return; body.password = p; }
      if (act === 'role') { body.role = b.dataset.role; if (!confirm('Change ' + uname + ' to ' + body.role + '? Their sessions will be signed out.')) return; }
      if (act === 'link') { const v = prompt('ERP username for ' + uname + ' (empty = unlink, default modules):', b.dataset.erp || ''); if (v === null) return; body.erp_user = v.trim(); }
      try { const r = await post('/api/admin/users', body); const j = await r.json(); if (!r.ok) throw new Error(j.error); toast('Done'); loadAdmin(); } catch (ex) { toast(ex.message); }
    });
    $$('#admin-content [data-note]').forEach(b => b.onclick = async () => { if (!confirm('Delete this note?')) return; try { await post('/api/admin/knowledge', { action: 'delete', id: b.dataset.note }); loadAdmin(); } catch (e) { toast('Failed'); } });
    $('#addnote').onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target); const err = $('#noteerr'); err.textContent = '';
      try { const r = await post('/api/admin/knowledge', { action: 'add', kind: f.get('kind'), text: f.get('text') }); const j = await r.json(); if (!r.ok) throw new Error(j.error); toast('Saved'); loadAdmin(); }
      catch (ex) { err.textContent = ex.message; }
    };
  }

  loadBranding().then(boot);
})();

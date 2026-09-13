(function () {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = v => '$' + Number(v).toFixed(Number(v) < 0.1 ? 4 : 2);
  const md = s => {
    const html = window.marked ? marked.parse(s, { breaks: true, gfm: true }) : '<p>' + esc(s).replace(/\n/g, '<br>') + '</p>';
    return window.DOMPurify ? DOMPurify.sanitize(html, { USE_PROFILES: { html: true } }) : esc(s);
  };
  function toast(t, ms = 2600) { const el = $('#toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove('show'), ms); }

  /* ---------- api with sign-in handling ---------- */
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
  function applyTheme(t) { root.setAttribute('data-theme', t); $('#themelabel').textContent = t === 'dark' ? 'Light' : 'Dark'; try { localStorage.setItem('theme', t); } catch (e) {} }
  let savedTheme = null; try { savedTheme = localStorage.getItem('theme'); } catch (e) {}
  applyTheme(savedTheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
  $('#theme').onclick = () => applyTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');

  /* ---------- branding (public endpoint, private values) ---------- */
  let brand = { name: 'Stock Assistant', subtitle: '', library: [] };
  async function loadBranding() {
    try { const j = await (await fetch('/api/branding')).json(); brand = Object.assign(brand, j.brand || {}, { library: j.library || [] }); } catch (e) {}
    document.title = brand.name; $$('.brandname').forEach(el => el.textContent = brand.name); $('#brandsub').textContent = brand.subtitle || '';
  }

  /* ---------- login ---------- */
  const loginEl = $('#login'), appEl = $('#app');
  function showLogin(msg) { appEl.hidden = true; loginEl.hidden = false; $('#loginerr').textContent = msg || ''; setTimeout(() => $('#lu').focus(), 50); }
  $('#loginform').onsubmit = async e => {
    e.preventDefault();
    const btn = $('#loginbtn'); btn.disabled = true; $('#loginerr').textContent = '';
    try {
      const r = await post('/api/login', { username: $('#lu').value.trim(), password: $('#lp').value });
      const j = await r.json();
      if (!r.ok) { $('#loginerr').textContent = j.error || 'Sign-in failed.'; return; }
      $('#lp').value = '';
      await boot();
    } catch (err) { $('#loginerr').textContent = 'Server not reachable.'; }
    finally { btn.disabled = false; }
  };
  $('#logout').onclick = async () => { try { await post('/api/logout'); } catch (e) {} location.reload(); };

  /* ---------- boot ---------- */
  async function boot() {
    const r = await api('/api/me');
    if (r.status === 401) { const j = await r.json().catch(() => ({})); showLogin(j.users_exist === false ? 'No users yet. Create the first admin with: python manage.py create <name> admin' : ''); return; }
    me = await r.json();
    loginEl.hidden = true; appEl.hidden = false;
    renderUserCard();
    $('#adminbtn').hidden = me.role !== 'admin';
    $('#sqltoggle').hidden = !me.show_sql;
    applySqlPref();
    const h = new Date().getHours();
    $('#greet').innerHTML = (h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening') + ', ' + esc(me.user) + '. What do you want to know about <span>stock</span>?';
    if (!$('#lib').dataset.built) buildLibrary();
    showView('chat');
    inner.querySelectorAll('.turn').forEach(t => t.remove()); recents.length = 0; renderRecents();
    await Promise.all([refreshStatus(), loadHistory()]);
    q.focus();
  }
  function renderUserCard() {
    $('#uname').textContent = me.user; $('#uav').textContent = me.user.slice(0, 2).toUpperCase();
    const badge = $('#urole'); badge.textContent = me.role; badge.className = 'badge ' + (me.role === 'admin' ? '' : 'user');
    const mods = $('#umods'); mods.innerHTML = '';
    (me.modules || []).forEach(m => { const b = document.createElement('span'); b.className = 'badge mod'; b.textContent = m; mods.appendChild(b); });
    if (me.role !== 'admin' && !(me.modules || []).length) { const b = document.createElement('span'); b.className = 'badge mod'; b.textContent = 'no modules yet'; mods.appendChild(b); }
  }

  /* ---------- SQL visibility ---------- */
  let showSql = false; try { showSql = localStorage.getItem('showSql') === '1'; } catch (e) {}
  function applySqlPref() {
    const on = me && me.show_sql && showSql;
    document.body.classList.toggle('hide-sql', !on);
    $('#sqltoggle').classList.toggle('on', on);
    $('#sqllabel').textContent = on ? 'Queries shown' : 'Queries hidden';
  }
  $('#sqltoggle').onclick = () => { showSql = !showSql; try { localStorage.setItem('showSql', showSql ? '1' : '0'); } catch (e) {} applySqlPref(); };

  /* ---------- question library ---------- */
  const ICON = {
    box: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3 21 7.5v9L12 21l-9-4.5v-9L12 3Z"/><path d="M3 7.5l9 4.5 9-4.5M12 12v9"/></svg>',
    move: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l6-6 4 4 6-8"/><path d="M14 7h6v6"/></svg>',
    truck: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 7h11v9H3zM14 10h4l3 3v3h-7z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    site: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 21h18M5 21V8l7-4 7 4v13M9 21v-6h6v6"/></svg>'
  };
  const LIB = () => brand.library.map(g => ({ title: g.title, icon: ICON[g.icon] ? g.icon : 'box', qs: g.questions || [] }));
  const STARTERS = () => LIB().slice(0, 4).filter(g => g.qs.length).map(g => [g.icon, g.title, g.qs[0]]);
  function buildLibrary() {
    const lib = $('#lib'); lib.dataset.built = '1';
    LIB().forEach((g, i) => {
      const d = document.createElement('details'); if (i === 0) d.open = true;
      d.innerHTML = '<summary><span class="ico">' + ICON[g.icon] + '</span>' + esc(g.title) + '<svg class="chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg></summary><div class="qs"></div>';
      const box = $('.qs', d);
      g.qs.forEach(t => { const b = document.createElement('button'); b.className = 'q-item'; b.textContent = t; b.onclick = () => { q.value = t; closeSide(); showView('chat'); ask(); }; box.appendChild(b); });
      lib.appendChild(d);
    });
    STARTERS().forEach(([ic, title, text]) => {
      const b = document.createElement('button'); b.className = 'starter';
      b.innerHTML = '<span class="ico">' + ICON[ic] + '</span><div><b>' + esc(title) + '</b><span>' + esc(text) + '</span></div>';
      b.onclick = () => { q.value = text; ask(); }; $('#starters').appendChild(b);
    });
  }

  /* ---------- recents ---------- */
  const recents = [];
  function renderRecents() {
    const box = $('#recent'); box.innerHTML = '<h3>Recent</h3>';
    if (!recents.length) { box.innerHTML += '<div class="empty-note">Your questions will appear here.</div>'; return; }
    recents.forEach(r => { const b = document.createElement('button'); b.className = 'q-item'; b.textContent = r; b.title = r; b.onclick = () => { q.value = r; autosize(); closeSide(); showView('chat'); q.focus(); }; box.appendChild(b); });
  }
  function addRecent(t) { const i = recents.indexOf(t); if (i >= 0) recents.splice(i, 1); recents.unshift(t); recents.splice(10); renderRecents(); }

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
  async function downloadCsv(i) {
    try {
      const r = await api('/api/csv?i=' + i); if (!r.ok) throw new Error(r.statusText);
      const url = URL.createObjectURL(await r.blob()); const a = document.createElement('a'); a.href = url; a.download = 'result-' + (i + 1) + '.csv'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 3000);
    } catch (e) { toast('Download failed: ' + e.message); }
  }

  /* ---------- views ---------- */
  const thread = $('#thread'), inner = $('#inner'), hero = $('#hero'), q = $('#q'), send = $('#send'), adminEl = $('#admin');
  function showView(v) {
    const chat = v === 'chat';
    thread.hidden = !chat; $('.composer-wrap').hidden = !chat; adminEl.hidden = chat;
    $('#adminbtn').classList.toggle('on', !chat);
    if (!chat) loadAdmin();
  }
  $('#adminbtn').onclick = () => { closeSide(); showView(adminEl.hidden ? 'admin' : 'chat'); };
  $('#backchat').onclick = () => showView('chat');
  const nearBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
  function scrollDown(force) { if (force || scrollDown.stick) thread.scrollTop = thread.scrollHeight; }
  scrollDown.stick = true;
  thread.addEventListener('scroll', () => { scrollDown.stick = nearBottom(); $('#scrolldown').classList.toggle('show', !scrollDown.stick); });
  $('#scrolldown').onclick = () => { scrollDown.stick = true; scrollDown(true); };

  /* ---------- status ---------- */
  async function refreshStatus() {
    try {
      const st = await (await api('/api/status')).json();
      me = Object.assign(me || {}, st); renderUserCard();
      $('#model').textContent = st.model_label || (st.model.replace('claude-', '') + ' · ' + st.effort);
      $('#spent').textContent = money(st.my_spent_today) + ' / ' + money(st.spent_today);
      $('#budget').textContent = 'you $' + Number(st.my_budget).toFixed(2) + ' · all $' + Number(st.budget).toFixed(2) + ' per day';
      $('#bar').style.width = Math.min(100, 100 * st.spent_today / st.budget).toFixed(1) + '%';
      $('#sesscost').textContent = st.session_cost ? money(st.session_cost) + ' this conversation' : '';
      $('#sqltoggle').hidden = !st.show_sql; applySqlPref();
    } catch (e) { $('#dbtext').textContent = 'Server not reachable'; $('#dbdot').className = 'dot bad'; return; }
    try {
      const d = await (await api('/api/dbcheck')).json();
      if (d.ok) { $('#dbtext').textContent = d.db; $('#dbdot').className = 'dot ok'; }
      else { $('#dbtext').textContent = 'DB error: ' + d.error; $('#dbdot').className = 'dot bad'; }
    } catch (e) {}
  }

  /* ---------- rendering a turn ---------- */
  const SVG_BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"><path d="M12 2.5 21 7v10l-9 4.5L3 17V7l9-4.5Z"/><path d="M3 7l9 4.5L21 7M12 11.5V21.5"/></svg>';
  const SVG_CHEV = '<svg class="chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg>';
  const SVG_DL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M6 11l6 6 6-6M4 21h16"/></svg>';
  const SVG_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M7 11v9H4v-9h3Zm0 0 4-7c1.5 0 2.5 1 2.5 2.5V10H19a2 2 0 0 1 2 2l-1.5 6.5A2 2 0 0 1 17.5 20H7"/></svg>';
  const SVG_DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M17 13V4h3v9h-3Zm0 0-4 7c-1.5 0-2.5-1-2.5-2.5V14H5a2 2 0 0 1-2-2l1.5-6.5A2 2 0 0 1 6.5 4H17"/></svg>';

  function newTurn(question) {
    hero.hidden = true;
    const t = document.createElement('div'); t.className = 'turn';
    t.innerHTML = '<div class="u-row"><div class="u-bubble"></div></div><div class="a-row"><div class="avatar">' + SVG_BOT + '</div><div class="a-body"><div class="steps"></div><div class="working"><span class="spin"></span><span class="wtxt">Thinking…</span></div><div class="answer" hidden><button class="acopy">Copy</button><div class="amd"></div></div><div class="a-foot"></div></div></div>';
    $('.u-bubble', t).textContent = question;
    inner.appendChild(t);
    const ctx = { el: t, body: $('.a-body', t), steps: $('.steps', t), working: $('.working', t), wtxt: $('.wtxt', t), answer: $('.answer', t), amd: $('.amd', t), foot: $('.a-foot', t), text: '', lastStep: null, turn: null, queries: 0 };
    $('.acopy', t).onclick = e => copyText(ctx.text, e.currentTarget);
    scrollDown(true);
    return ctx;
  }
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
      $$('th', g).forEach(th => th.onclick = () => { const i = +th.dataset.i; sortDir = sortCol === i ? -sortDir : 1; sortCol = i; rows.sort((a, b) => numCols[i] ? (Number(a[i] || 0) - Number(b[i] || 0)) * sortDir : a[i].localeCompare(b[i], undefined, { numeric: true }) * sortDir); draw(); });
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
    const dl = $('.dl', t); if (dl) dl.onclick = e => { e.preventDefault(); downloadCsv(+dl.dataset.i); };
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
      const r = await post('/api/feedback', { turn: ctx.turn, vote, text }); const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'failed');
      selBtn.classList.add('sel'); otherBtn.classList.remove('sel');
      if (j.learned) showLearned(ctx, j.learned); else toast(vote === 'up' ? 'Thanks for the feedback' : 'Feedback recorded for the admin');
    } catch (e) { toast('Feedback failed: ' + e.message); }
  }
  function showLearned(ctx, note) { const l = document.createElement('div'); l.className = 'learned'; l.innerHTML = '<b>Learned (' + esc(note.kind) + ')</b> · saved for all future sessions<br>'; l.appendChild(document.createTextNode(note.text)); ctx.body.insertBefore(l, ctx.foot); scrollDown(); }

  function handle(ctx, ev) {
    switch (ev.type) {
      case 'status': ctx.wtxt.textContent = ev.text === 'thinking' ? 'Thinking…' : 'Reading the results…'; break;
      case 'tool': addStep(ctx, ev); ctx.wtxt.textContent = me.show_sql && ev.purpose ? 'Running: ' + ev.purpose : 'Checking the ledger…'; break;
      case 'result': fillResult(ctx, ev); break;
      case 'sql_error': markError(ctx, ev.text); break;
      case 'learned': showLearned(ctx, ev); break;
      case 'text_delta': ctx.text += ev.text; ctx.answer.hidden = false; ctx.amd.innerHTML = md(ctx.text); scrollDown(); break;
      case 'text_break': ctx.text += '\n\n'; break;
      case 'error': ctx.working.hidden = true; { const er = document.createElement('div'); er.className = 'errcard'; er.textContent = ev.text; ctx.body.insertBefore(er, ctx.foot); } break;
      case 'final':
        ctx.working.hidden = true; ctx.turn = ev.turn;
        if (ev.text && ev.text.trim()) { ctx.text = ev.text; ctx.answer.hidden = false; ctx.amd.innerHTML = md(ev.text); }
        ctx.foot.innerHTML = '<span><b>' + (ev.provider === 'ollama' ? 'local · $0' : money(ev.cost)) + '</b></span><span>' + ev.elapsed + 's</span>' + (me.show_sql && ctx.queries ? '<span>' + ctx.queries + (ctx.queries === 1 ? ' query' : ' queries') + '</span>' : '');
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
    q.value = ''; autosize(); setBusy(true); addRecent(text); scrollDown.stick = true;
    const ctx = newTurn(text); aborter = new AbortController();
    try {
      const res = await post('/api/chat', { message: text }, { signal: aborter.signal });
      if (!res.ok || !res.body) { let t = ''; try { t = (await res.json()).error; } catch (e) { t = res.statusText; } throw new Error(t || 'request failed'); }
      const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i; while ((i = buf.indexOf('\n\n')) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2); if (chunk.startsWith('data: ')) { try { handle(ctx, JSON.parse(chunk.slice(6))); } catch (e) { console.error(e); } } }
      }
      if (!ctx.working.hidden) { ctx.working.hidden = true; if (!ctx.text) handle(ctx, { type: 'error', text: 'The connection closed before an answer arrived.' }); }
    } catch (e) {
      ctx.working.hidden = true;
      if (e.name === 'AbortError') { const s = document.createElement('div'); s.className = 'stopped'; s.textContent = 'Stopped. This question was discarded from the conversation.'; ctx.body.insertBefore(s, ctx.foot); }
      else if (e.message !== 'signed out') handle(ctx, { type: 'error', text: 'Request failed: ' + e.message });
    } finally { setBusy(false); aborter = null; q.focus(); refreshStatus(); }
  }
  async function loadHistory() {
    try {
      const h = await (await api('/api/history')).json();
      (h.turns || []).forEach(t => { const ctx = newTurn(t.q); addRecent(t.q); t.events.forEach(ev => handle(ctx, ev)); ctx.working.hidden = true; });
      if ((h.turns || []).length) scrollDown(true);
    } catch (e) {}
  }

  /* ---------- composer wiring ---------- */
  function autosize() { q.style.height = 'auto'; q.style.height = Math.min(200, q.scrollHeight) + 'px'; }
  q.addEventListener('input', autosize);
  q.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } if (e.key === 'Escape') stop(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') stop(); if (e.key === '/' && document.activeElement !== q && !appEl.hidden && adminEl.hidden) { e.preventDefault(); q.focus(); } });
  send.onclick = () => busy ? stop() : ask();
  $('#newchat').onclick = async () => {
    if (busy) { toast('Wait for the current answer or press Esc to stop it.'); return; }
    try { await post('/api/reset'); } catch (e) { return; }
    inner.querySelectorAll('.turn').forEach(t => t.remove()); hero.hidden = false; showView('chat'); refreshStatus(); closeSide(); q.focus();
  };
  const side = $('#side'), scrim = $('#scrim');
  function closeSide() { side.classList.remove('open'); scrim.classList.remove('show'); }
  $('#menu').onclick = () => { side.classList.add('open'); scrim.classList.add('show'); };
  scrim.onclick = closeSide;

  /* ---------- change password ---------- */
  $('#pwbtn').onclick = () => {
    if (me && me.erp_user && me.user === String(me.erp_user).toLowerCase()) { toast('You signed in with ERP credentials; change the password in the ERP.', 3500); return; }
    $('#pwmodal').hidden = false; $('#pwerr').textContent = ''; $('#pwcur').value = $('#pwnew').value = ''; setTimeout(() => $('#pwcur').focus(), 50);
  };
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
    const box = $('#admin-content'); box.innerHTML = '<div class="working"><span class="spin"></span>Loading…</div>';
    try { const r = await api('/api/admin/overview'); if (!r.ok) throw new Error((await r.json()).error || 'failed'); ov = await r.json(); }
    catch (e) { box.innerHTML = '<div class="errcard">' + esc(e.message) + '</div>'; return; }
    groupsData = null;
    try { const g = await api('/api/admin/groups'); if (g.ok) groupsData = await g.json(); else groupsData = { error: (await g.json()).error }; } catch (e) { groupsData = { error: 'ERP not reachable' }; }
    renderAdmin();
  }
  function card(title, desc, bodyHtml) { return '<section class="acard"><h3>' + title + '</h3><p class="desc">' + desc + '</p>' + bodyHtml + '</section>'; }
  function renderAdmin() {
    const s = ov.settings, u = ov.usage, sec = ov.security, c = ov.costs, erp = ov.erp || { admin_users: [], modules: [] };
    const usersHtml = '<table class="tbl"><thead><tr><th>User</th><th>Role</th><th>ERP link</th><th>Created</th><th>Last sign-in</th><th></th></tr></thead><tbody>' +
      ov.users.map(x => '<tr><td><b>' + esc(x.username) + '</b></td><td><span class="badge ' + (x.role === 'admin' ? '' : 'user') + '">' + x.role + '</span></td><td>' + (x.erp_user ? esc(x.erp_user) : '<span class="badge mod">default modules</span>') + '</td><td>' + esc(x.created || '') + '</td><td>' + esc(x.last_login || 'never') + '</td>' +
        '<td class="acts"><button class="mini" data-act="link" data-u="' + esc(x.username) + '" data-erp="' + esc(x.erp_user || '') + '">ERP link</button><button class="mini" data-act="password" data-u="' + esc(x.username) + '">Reset password</button><button class="mini" data-act="role" data-u="' + esc(x.username) + '" data-role="' + (x.role === 'admin' ? 'user' : 'admin') + '">Make ' + (x.role === 'admin' ? 'user' : 'admin') + '</button><button class="mini danger" data-act="delete" data-u="' + esc(x.username) + '">Delete</button></td></tr>').join('') +
      '</tbody></table>' +
      '<form id="adduser" class="form-inline five"><div class="field"><label>New username</label><input name="username" required minlength="3" autocomplete="off"></div><div class="field"><label>Password (min 8)</label><input name="password" type="password" required minlength="8" autocomplete="new-password"></div><div class="field"><label>ERP username (optional)</label><input name="erp_user" placeholder="ERP login id" autocomplete="off"></div><div class="field"><label>Role</label><select name="role"><option value="user">user</option><option value="admin">admin</option></select></div><button class="btn" type="submit">Add user</button></form><div class="form-err" id="usererr"></div>' +
      '<p class="desc" style="margin-top:10px">ERP sign-in is ' + (s.erp_login ? 'ON: anyone with an active ERP account can sign in with their ERP username and password; their modules come from their ERP groups.' : 'OFF: only the accounts above can sign in.') + ' Assistant admins by ERP username: <b>' + (erp.admin_users.length ? erp.admin_users.map(esc).join(', ') : 'none') + '</b>.</p>';

    const ol = ov.ollama || { alive: false, models: [] };
    const providerHtml = '<div class="settings-grid" style="margin-bottom:12px"><div class="field"><label>Answering model</label><select id="s-provider"><option value="anthropic" ' + (s.provider !== 'ollama' ? 'selected' : '') + '>Claude API (Anthropic)</option><option value="ollama" ' + (s.provider === 'ollama' ? 'selected' : '') + '>Local model via Ollama (nothing leaves this PC)</option></select></div>' +
      '<div class="field"><label>Local model name</label><input id="s-omodel" value="' + esc(s.ollama_model || '') + '" placeholder="qwen3:8b"></div><div class="field"><label>Ollama URL</label><input id="s-ourl" value="' + esc(s.ollama_url || '') + '"></div></div>' +
      '<p class="desc">' + (ol.alive ? 'Ollama is running at ' + esc(ol.url) + '. Downloaded models: <b>' + (ol.models.length ? ol.models.map(esc).join(', ') : 'none yet (run: ollama pull ' + esc(s.ollama_model || 'qwen3:8b') + ')') + '</b>. Local answers cost $0 and are slower on a PC without a GPU.' : 'Ollama is not running on this PC. Start it (or install from ollama.com) to use a local model.') + '</p>';
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

    const usageHtml = '<div class="kpis"><div class="kpi"><div class="l">Today</div><div class="v">' + money(u.today) + '</div></div><div class="kpi"><div class="l">Last 7 days</div><div class="v">' + money(u.last7) + '</div></div><div class="kpi"><div class="l">Last 30 days</div><div class="v">' + money(u.last30) + '</div></div><div class="kpi"><div class="l">Questions today</div><div class="v">' + u.questions_today + '</div></div><div class="kpi"><div class="l">Questions total</div><div class="v">' + u.questions_total + '</div></div></div>' +
      (Object.keys(u.by_user_today).length ? '<table class="tbl"><thead><tr><th>User</th><th>Spent today</th></tr></thead><tbody>' + Object.entries(u.by_user_today).map(([k, v]) => '<tr><td>' + esc(k) + '</td><td>' + money(v) + '</td></tr>').join('') + '</tbody></table>' : '<p class="desc">No questions yet today.</p>');

    const knHtml = (ov.knowledge.length ? ov.knowledge.slice().reverse().map(n => '<div class="note-item"><span class="badge">' + esc(n.kind) + '</span><div class="txt">' + esc(n.text) + '<div class="m">' + esc(n.by) + ' · ' + esc(n.ts) + (n.question ? ' · from: ' + esc(n.question) : '') + '</div></div><button class="mini danger" data-note="' + n.id + '">Delete</button></div>').join('') : '<p class="desc">Nothing learned yet.</p>') +
      '<form id="addnote" class="form-inline" style="grid-template-columns:140px 1fr auto;align-items:start"><div class="field"><label>Kind</label><select name="kind"><option>rule</option><option>table</option><option>join</option><option>example</option><option>correction</option></select></div><div class="field"><label>Teach it something (free, no AI call)</label><textarea name="text" placeholder="e.g. MREQWORKFLOW.STATUS = C means closed. Approvals are level 1..6; a blank level means a higher authority covered it." required minlength="8"></textarea></div><button class="btn" type="submit">Save</button></form><div class="form-err" id="noteerr"></div>';

    const fbHtml = ov.feedback.length ? '<table class="tbl"><thead><tr><th>When</th><th>User</th><th>Vote</th><th>Question</th><th>Comment</th></tr></thead><tbody>' + ov.feedback.map(f => '<tr><td>' + esc(f.ts.replace('T', ' ')) + '</td><td>' + esc(f.user) + '</td><td><span class="badge ' + f.vote + '">' + f.vote + '</span></td><td>' + esc(f.question) + '</td><td>' + esc(f.text || '') + '</td></tr>').join('') + '</tbody></table>' : '<p class="desc">No feedback yet.</p>';
    const secHtml = '<div class="kpis"><div class="kpi"><div class="l">Active sessions</div><div class="v">' + sec.active_sessions + '</div></div><div class="kpi"><div class="l">Failed sign-ins, 24 h</div><div class="v">' + sec.failed_logins_24h + '</div></div><div class="kpi"><div class="l">Locked accounts</div><div class="v">' + sec.locked_accounts + '</div></div></div><ul class="seclist">' + sec.notes.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul>';
    const auditHtml = '<div class="audit">' + esc(ov.audit.map(a => a.ts.replace('T', ' ') + '  ' + (a.event + '').padEnd(20) + ' ' + (a.user || '-').padEnd(12) + ' ' + Object.entries(a).filter(([k]) => !['ts', 'event', 'user'].includes(k)).map(([k, v]) => k + '=' + (typeof v === 'string' ? v.slice(0, 80) : JSON.stringify(v))).join(' ')).join('\n')) + '</div>';

    $('#admin-content').innerHTML =
      card('Users &amp; sign-in', 'App accounts, ERP links and who is an assistant admin.', usersHtml) +
      card('ERP groups &rarr; modules', 'Roles and responsibilities come from the ERP. This table decides which modules each ERP group may ask about; the tables behind each module are enforced on every query.', groupsHtml) +
      card('Model &amp; cost', 'Pick the model that fits the budget. Fewer rows to the model means cheaper and faster answers.', modelsHtml) +
      card('Usage', 'Actual spend from the usage log.', usageHtml) +
      card('Learned knowledge', 'Teach the assistant here for free. Everything saved is added to its instructions for everyone.', knHtml) +
      card('Feedback', 'Thumbs up/down from all users. Admin thumbs-down with a correction becomes a learned note automatically.', fbHtml) +
      card('Security', 'What protects this application.', secHtml) +
      card('Audit log', 'Last 40 events from logs/audit.jsonl.', auditHtml);

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

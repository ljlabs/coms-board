/* app.js — COMS BOARD single-page app (hash routing, no framework). */
(function () {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const app = $('#app');
  let view = { controller: new AbortController(), hash: null, cleanup: [] };
  const esc = MD.esc;
  const who = () => ($('#who').value || 'admin').trim();
  $('#who').value = localStorage.getItem('coms.who') || 'admin';
  $('#who').addEventListener('change', () => localStorage.setItem('coms.who', who()));

  // ------------------------------------------------------------ api
  async function api(method, path, body) {
    const owner = view;
    const res = await fetch(path, {
      method, headers: { 'Content-Type': 'application/json', 'X-Coms-Agent': who() },
      body: body ? JSON.stringify(body) : undefined,
      signal: method === 'GET' ? owner.controller.signal : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (owner !== view || owner.controller.signal.aborted) throw new DOMException('View changed', 'AbortError');
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }
  const GET = (p) => api('GET', p);

  function toast(msg, err) {
    const t = document.createElement('div'); t.className = 't' + (err ? ' err' : ''); t.textContent = msg;
    $('#toast').appendChild(t); setTimeout(() => t.remove(), err ? 6000 : 2500);
  }
  const fail = (e) => { if (e.name === 'AbortError') return; console.error(e); toast(e.message || String(e), true); };

  // Dispose polls on every render, including a refresh of the same URL.
  // A slow request must finish before the next poll can start.
  function poll(fn, delay) {
    const owner = view;
    let timer;
    const tick = async () => {
      if (owner !== view || owner.controller.signal.aborted) return;
      try { if (!document.hidden) await fn(); } catch (e) { if (e.name !== 'AbortError') console.error(e); }
      if (owner === view && !owner.controller.signal.aborted) timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, delay);
    owner.cleanup.push(() => clearTimeout(timer));
  }
  function progressHTML(data={items:[],active_count:0,waiting_count:0,generated_at:''}) {
    const states={starting:'Starting',working:'Working',collaborating:'Talking to a teammate',thinking:'Thinking',responding:'Writing response',tool:'Using tool',reviewing:'Reviewing',waiting:'Waiting for teammates',completed:'Finished',failed:'Failed'};
    const active=data.items.filter(x=>['running','waiting'].includes(x.state));
    const recent=data.items.filter(x=>!['running','waiting'].includes(x.state)).slice(0,4);
    return `<div class="card-title"><h3>Live agent progress</h3><span class="small muted" data-progress-refresh>updated ${ago(data.generated_at)}</span></div>
      <div class="progress-counts"><b>${data.active_count} running</b><span>${data.waiting_count} waiting for teammates</span>${data.capacity?`<span>${data.capacity} ticket slots</span>`:''}<span class="small muted">refreshes every 2 seconds</span></div>
      <div class="progress-list">${[...active,...recent].map(x=>`<article class="progress-item ${esc(x.state)}" data-progress-key="${esc(x.key)}"><div class="progress-heading"><b><a class="chip" href="#/agents/${encodeURIComponent(x.agent_slug)}">${esc(x.agent_name||x.agent_slug)}</a></b><span class="badge ${x.state==='failed'?'blocked':x.state==='running'?'in_progress':'done'}">${esc(states[x.phase]||x.phase)}</span>${x.ticket_id?`<a href="#/tickets/${x.ticket_id}">#${x.ticket_id} ${esc(x.ticket_title||'')}</a>`:'<span>Orchestrator chat</span>'}</div><p class="progress-summary">${esc(x.summary)}</p>
        <div class="small muted">${esc(x.backend)} · ${x.response_chars.toLocaleString()} response characters · ${x.tool_calls} tool calls · last event ${x.idle_seconds}s ago${x.state==='running'&&x.idle_seconds>45?' · waiting for a new model event':''}</div>
        ${x.preview?`<details class="progress-response"${x.state==='running'?' open':''}><summary>Latest response</summary><pre>${esc(x.preview)}</pre></details>`:''}</article>`).join('')||'<div class="empty">No sessions running. Ready tickets start automatically.</div>'}</div>`;
  }
  function drawProgress(box,data) {
    if(!box||hasSelection(box))return;
    const opened=new Set([...box.querySelectorAll('details[open]')].map(x=>x.closest('[data-progress-key]').dataset.progressKey));
    const closed=new Set([...box.querySelectorAll('details:not([open])')].map(x=>x.closest('[data-progress-key]').dataset.progressKey));
    box.innerHTML=progressHTML(data);
    for(const detail of box.querySelectorAll('details')){
      const key=detail.closest('[data-progress-key]').dataset.progressKey;
      if(opened.has(key))detail.open=true;
      if(closed.has(key))detail.open=false;
    }
  }
  async function mountProgress(id,query='') {
    const refresh=async()=>{
      try { const data=await GET('/api/runner/status'+query);drawProgress($('#'+id),data); }
      catch(e){if(e.name==='AbortError')throw e;const box=$('#'+id);if(box)box.innerHTML=`<h3>Live agent progress</h3><p class="error">Could not refresh: ${esc(e.message)}</p>`;}
    };
    await refresh();poll(refresh,2000);
  }
  async function mountTeam(id,jobId) {
    const container=$('#'+id);if(!container)return;
    container.innerHTML=`<div class="card-title"><h3>Team conversation</h3><a class="btn sm" href="#/teams/${jobId}">Open room</a></div><p class="small muted">Named questions, replies and peer review. Directed questions wake the recipient automatically.</p><div class="team-members"></div><div class="team-feed" aria-label="Team conversation"></div><div class="team-reviews"></div><form class="team-form form"><div class="grid-2"><label>To<select class="team-to"><option value="*">Everyone · note</option></select></label><label>Message<textarea class="team-input" rows="2" required placeholder="Ask a teammate, or leave a useful update…"></textarea></label></div><button class="btn">Send to team</button></form>`;
    let signature='';
    const refresh=async()=>{
      const data=await GET('/api/teams/'+jobId),box=$('#'+id);if(!box)return;
      const select=$('.team-to',box),selected=select.value;
      select.innerHTML='<option value="*">Everyone · note</option>'+data.members.map(a=>`<option value="${esc(a.slug)}">${esc(a.name)} · ${esc(a.description)}</option>`).join('');select.value=[...select.options].some(o=>o.value===selected)?selected:'*';
      $('.team-members',box).innerHTML=data.members.map(a=>`<a class="chip" href="#/agents/${encodeURIComponent(a.slug)}">${esc(a.name)}</a>`).join(' ')+`<span class="small muted"> · ${data.sources.filter(s=>s.status==='ok').length} retrieved sources · ${data.sources.filter(s=>s.status!=='ok').length} retrieval failures</span>`;
      const feed=$('.team-feed',box),next=JSON.stringify([data.messages,data.reviews]);if(next===signature||hasSelection(feed))return;
      const position=scrollState(feed);signature=next;
      feed.innerHTML=data.messages.map(m=>`<article class="team-message ${esc(m.kind)}" data-team-message="${m.id}"><div class="team-message-head"><b>${esc(m.sender_name||m.sender)}</b><span>→ ${m.recipient==='*'?'everyone':esc(m.recipient_name||m.recipient)}</span><span class="badge">${esc(m.kind.replaceAll('_',' '))}</span><span class="ts">#${m.id}${m.reply_to?` · reply to #${m.reply_to}`:''} · ${ago(m.created_at)}</span></div><div class="md">${MD.render(m.body)}</div>${m.deliveries.map(d=>`<div class="small muted">${esc(d.status)}${d.attempt_count?` · attempt ${d.attempt_count}/3`:''}${d.error?` · ${esc(d.error)}`:''}</div>`).join('')}</article>`).join('')||'<div class="empty">No teammate messages yet. The orchestrator will explain its staffing decision here.</div>';
      restoreScroll(feed,position);
      $('.team-reviews',box).innerHTML=data.reviews.length?`<h4>Peer review</h4><div class="list">${data.reviews.map(r=>`<div class="item"><div class="main"><a href="#/wiki/${encodeURIComponent(r.artifact_slug)}">${esc(r.artifact_slug)}</a> · ${esc(r.reviewer)} · ${badge(r.status)}${!r.current?' <b>revision changed</b>':''}<div class="small">${esc(r.reason)}</div></div></div>`).join('')}</div>`:'';
    };
    $('.team-form',container).onsubmit=async event=>{event.preventDefault();const input=$('.team-input',container),body=input.value.trim();if(!body)return;try{await api('POST','/api/teams/'+jobId,{to:$('.team-to',container).value,body});input.value='';await refresh();}catch(err){fail(err);}};
    await refresh();poll(refresh,2000);
  }
  const scrollState = box => ({ top: box.scrollTop, follow: box.scrollHeight - box.scrollTop - box.clientHeight < 24 });
  const hasSelection = node => {
    const selection=window.getSelection();
    return selection && !selection.isCollapsed && selection.rangeCount && selection.getRangeAt(0).intersectsNode(node);
  };
  function restoreScroll(box, state) {
    const selecting = hasSelection(box);
    box.scrollTop = state.follow && !selecting ? box.scrollHeight : state.top;
  }

  function paint(html) {
    // Snapshot at commit time: the user can keep typing or scrolling while
    // a refresh request is still loading.
    if (view.same && !view.snapshot) {
      const active=document.activeElement;
      view.snapshot = {
        position: { x: window.scrollX, y: window.scrollY },
        drafts: window.MdEditor ? MdEditor.drafts() : [],
        inputs: [...app.querySelectorAll('textarea[id], #lk-ref, #lk-note, #project-name')].map(el => [el.id, el.value]),
        nested: [...app.querySelectorAll('[id]')].filter(el => el.scrollHeight > el.clientHeight && el.scrollTop).map(el => [el.id, el.scrollTop]),
        focus: active && app.contains(active) && active.id ? { id: active.id, start: active.selectionStart, end: active.selectionEnd } : null,
      };
    }
    app.classList.toggle('wide', Boolean(view.wide));
    app.innerHTML = html;
  }

  // ------------------------------------------------------------ helpers
  const ago = (iso) => {
    if (!iso) return '';
    const d = (Date.now() - new Date(iso).getTime()) / 1000;
    if (d < 60) return 'just now';
    if (d < 3600) return `${Math.floor(d / 60)}m ago`;
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
    if (d < 86400 * 30) return `${Math.floor(d / 86400)}d ago`;
    return iso.slice(0, 10);
  };
  const tags = (s, base) => (s || '').split(',').filter(Boolean).map((t) => `<a class="tag" href="${base}${base.includes('?') ? '&' : '?'}tag=${encodeURIComponent(t)}">${esc(t)}</a>`).join('');
  const badge = (v, extra = '') => v ? `<span class="badge ${esc(v)} ${extra}">${esc(v.replace('_', ' '))}</span>` : '';
  const chip = (a) => a ? `<a class="who-chip" href="#/agents/${encodeURIComponent(a)}">${esc(a)}</a>` : '<span class="muted mono small">—</span>';

  // ---- THE page shell. Every route with columns renders through this; never hand-roll
  // <div class="row"><div class="col-2"> page layouts (three divergent copies of that is how
  // the wiki-index overlap bug happened). Shape: [nav] | body | [aside]; body is the only
  // fluid column (min-width:0 in CSS). Pages using it are full-width (main.wide).
  //   layout({ nav, body, aside, id, collapsible })  -> html
  //   wireAside(id, storeKey)                        -> attaches the ⇥ Panel button (#toggleside) if present
  const layout = ({ nav = '', body = '', aside = '', id = 'layout' } = {}) => {
    view.wide = true;
    return `<div class="layout" id="${esc(id)}">${nav ? `<div class="layout-nav">${nav}</div>` : ''}<div class="layout-body">${body}</div>${aside ? `<div class="layout-aside">${aside}</div>` : ''}</div>`;
  };
  // the standard "⇥ Panel" action button; pair with wireAside() after rendering
  const asideToggleBtn = () => `<button class="btn" id="toggleside" title="Show/hide the side panel (widens the page)">⇥ Panel</button>`;
  const wireAside = (id, storeKey) => {
    const el = document.getElementById(id), btn = $('#toggleside');
    if (!el || !btn) return;
    const apply = (c) => { el.classList.toggle('aside-collapsed', c); btn.classList.toggle('on', c); };
    const saved = localStorage.getItem(storeKey);
    apply(saved === null ? window.matchMedia('(max-width: 900px)').matches : saved === '0');
    btn.onclick = () => { const c = !el.classList.contains('aside-collapsed'); localStorage.setItem(storeKey, c ? '0' : '1'); apply(c); };
  };
  const refLink = (ref) => {
    const [k, v] = (ref || '').split(':');
    if (k === 'wiki') return `#/wiki/${v}`; if (k === 'ticket') return `#/tickets/${v}`; if (k === 'question') return `#/questions/${v}`;
    if (k === 'agent') return `#/agents/${encodeURIComponent(v)}`; if (k === 'skill') return `#/skills/${encodeURIComponent(v)}`;
    return '#/';
  };
  const qs = () => Object.fromEntries(new URLSearchParams(location.hash.split('?')[1] || ''));
  const setTab = (t) => document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('on', a.dataset.tab === t));
  const header = $('.topbar'), headerTools = $('#header-tools'), menuToggle = $('#menu-toggle'), menuBackdrop = $('#menu-backdrop');
  const mobileHeader = window.matchMedia('(max-width: 900px)');
  if (header && headerTools && menuToggle && menuBackdrop) {
    headerTools.inert = mobileHeader.matches;
    const closeMenu = (restoreFocus = false) => {
      header.classList.remove('menu-open'); document.body.classList.remove('menu-open'); menuBackdrop.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false'); menuToggle.setAttribute('aria-label', 'Open menu');
      headerTools.inert = mobileHeader.matches;
      if (restoreFocus && mobileHeader.matches) menuToggle.focus();
    };
    const openMenu = () => {
      header.classList.add('menu-open'); document.body.classList.add('menu-open'); menuBackdrop.hidden = false;
      menuToggle.setAttribute('aria-expanded', 'true'); menuToggle.setAttribute('aria-label', 'Close menu');
      headerTools.inert = false; $('#tabs a').focus();
    };
    menuToggle.addEventListener('click', () => menuToggle.getAttribute('aria-expanded') === 'true' ? closeMenu(true) : openMenu());
    menuBackdrop.addEventListener('click', () => closeMenu(true));
    $('#tabs').addEventListener('click', (event) => { if (event.target.closest('a')) window.setTimeout(() => closeMenu(), 0); });
    $('#searchform').addEventListener('submit', (event) => {
      event.preventDefault();
      location.hash = `#/search?q=${encodeURIComponent($('#q').value.trim())}`;
      if (header.classList.contains('menu-open')) closeMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (!header.classList.contains('menu-open')) return;
      if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); return; }
      if (event.key !== 'Tab') return;
      const focusable = [menuToggle, ...headerTools.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])')]
        .filter((el) => !el.disabled && el.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!focusable.includes(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    mobileHeader.addEventListener('change', (event) => {
      if (!event.matches) closeMenu(false);
      else headerTools.inert = !header.classList.contains('menu-open');
    });
  }
  const wireWikilinks = (root, known) => root.querySelectorAll('a.wikilink').forEach((a) => { if (known && !known.has(a.dataset.slug)) a.classList.add('missing'); });
  const field = (label, inner) => `<label>${label}${inner}</label>`;
  const opts = (list, cur) => list.map((v) => `<option value="${v}"${v === cur ? ' selected' : ''}>${v.replace('_', ' ')}</option>`).join('');
  const TS = ['open', 'in_progress', 'blocked', 'review', 'done', 'wontfix'];
  const TT = ['story', 'job', 'research', 'task', 'bug', 'question', 'chore'];
  const PR = ['p0', 'p1', 'p2', 'p3'];
  let AGENTS = [], DEPTS = [];
  async function loadRefs() {
    try { [AGENTS, DEPTS] = await Promise.all([GET('/api/agents'), GET('/api/departments')]); } catch (e) { if (e.name === 'AbortError') throw e; }
  }
  const agentOpts = (cur) => `<option value="">— nobody —</option>` + AGENTS.map((a) => `<option value="${a.slug}"${a.slug === cur ? ' selected' : ''}>${esc(a.slug)}</option>`).join('');
  const deptOpts = (cur) => `<option value="">— none —</option>` + DEPTS.map((d) => `<option value="${d.slug}"${d.slug === cur ? ' selected' : ''}>${esc(d.name)}</option>`).join('');

  // ------------------------------------------------------------ router
  const routes = [];
  const route = (re, fn) => routes.push([re, fn]);
  async function render() {
    const hash = location.hash || '#/';
    const same = hash === view.hash;
    view.controller.abort();
    view.cleanup.forEach(fn => fn());
    const owner = view = { controller: new AbortController(), hash, same, cleanup: [] };
    const path = hash.slice(1).split('?')[0];
    for (const [re, fn] of routes) {
      const m = re.exec(path);
      if (m) {
        // Keep the page height and existing content stable while data loads.
        app.setAttribute('aria-busy', 'true');
        if (!app.firstElementChild) paint('<div class="empty">loading…</div>');
        try { await fn(...m.slice(1).map((x) => x && decodeURIComponent(x))); } catch (e) {
          if (owner !== view || e.name === 'AbortError') return;
          if (same) fail(e);
          else paint(`<div class="card tint-p"><h2>Error</h2><p class="mono">${esc(e.message)}</p></div>`);
        }
        if (owner !== view) return;
        app.removeAttribute('aria-busy');
        if (window.MdEditor) MdEditor.sweep();
        const snapshot=owner.snapshot;
        if (snapshot) {
          if (window.MdEditor) MdEditor.restoreDrafts(snapshot.drafts);
          snapshot.inputs.forEach(([id, value]) => { const el = document.getElementById(id); if (el) el.value = value; });
          snapshot.nested.forEach(([id, top]) => { const el = document.getElementById(id); if (el) el.scrollTop = top; });
          if (snapshot.focus) {
            const el=document.getElementById(snapshot.focus.id);
            if (el) { el.focus({preventScroll:true}); if (el.setSelectionRange && snapshot.focus.start != null) el.setSelectionRange(snapshot.focus.start,snapshot.focus.end); }
          }
        }
        // one central place to turn rendered ```mermaid blocks into diagrams
        if (window.MMD && MMD.has(app)) MMD.upgrade(app).catch(() => {});
        // …and to pretty-print single-line code blocks (see pretty.js)
        if (window.PRETTY && PRETTY.has(app)) PRETTY.upgrade(app).catch(() => {});
        window.scrollTo(snapshot ? snapshot.position.x : 0, snapshot ? snapshot.position.y : 0);
        return;
      }
    }
    paint('<div class="empty">404 — no such page</div>');
    app.removeAttribute('aria-busy');
    if (window.MdEditor) MdEditor.sweep();
  }
  window.addEventListener('hashchange', render);

  // ------------------------------------------------------------ HOME
  route(/^\/$/, async () => {
    setTab('home');
    const o = await GET('/api/overview');
    const [qs_, tix] = await Promise.all([GET('/api/questions?unanswered=1'), GET('/api/tickets?unassigned=1')]);
    paint(`
      <div class="pagehead"><h1>Dashboard</h1><div class="sub">${esc(o.generated_at)} · ${o.agents} agents · ${o.departments} departments</div></div>
      <div class="stats">
        <a class="stat y" href="#/wiki"><div class="n">${o.wiki_pages}</div><div class="l">wiki pages</div></a>
        <a class="stat c" href="#/tickets"><div class="n">${o.tickets_open_total}</div><div class="l">open tickets</div></a>
        <a class="stat o" href="#/tickets?unassigned=1"><div class="n">${o.unassigned_open}</div><div class="l">unassigned</div></a>
        <a class="stat p" href="#/questions?unanswered=1"><div class="n">${o.unanswered}</div><div class="l">unanswered Qs</div></a>
        <a class="stat g" href="#/questions"><div class="n">${o.answers}</div><div class="l">answers · ${o.votes} votes</div></a>
        <a class="stat v" href="#/agents"><div class="n">${o.agents}</div><div class="l">agents</div></a>
      </div>
      ${layout({ id: 'home', body: `
          <div class="card"><div class="card-title"><h2>Unassigned tickets</h2><a class="btn sm" href="#/tickets/new">+ new</a></div>
            ${tix.length ? `<div class="list">${tix.slice(0, 8).map(ticketItem).join('')}</div>` : '<div class="empty">queue is clear</div>'}</div>
          <div class="card"><div class="card-title"><h2>Unanswered questions</h2><a class="btn sm" href="#/questions/ask">+ ask</a></div>
            ${qs_.length ? `<div class="list">${qs_.slice(0, 8).map(questionItem).join('')}</div>` : '<div class="empty">everything is answered</div>'}</div>`,
        aside: `
          <div class="card tint-y"><h3>Top agents</h3>${o.top_agents.map((a) => `<div style="display:flex;justify-content:space-between;padding:.2rem 0">${chip(a.slug)}<b class="mono">${a.reputation} rep</b></div>`).join('') || '<span class="muted">no agents yet</span>'}</div>
          <div class="card"><h3>Recent activity</h3><div class="feed">${o.recent_activity.map(ev).join('') || '<span class="muted">quiet</span>'}</div><p><a href="#/activity" class="small mono">full feed →</a></p></div>` })}`);
  });
  const ev = (a) => `<div class="ev"><span class="ts" title="${esc(a.created_at)}">${ago(a.created_at)} · ${esc(a.actor || '?')}</span><span><a href="${refLink(a.ref)}">${esc(a.summary)}</a></span></div>`;

  // ------------------------------------------------------------ WIKI
  // folder breadcrumb: "/ a / b" with each segment linking to that folder's listing
  const folderCrumb = (f, cur) => {
    const parts = (f || '').split('/').filter(Boolean);
    let acc = '';
    const segs = parts.map((s) => { acc = acc ? `${acc}/${s}` : s; return `<a class="mono" href="#/wiki?folder=${encodeURIComponent(acc)}">${esc(s)}</a>`; });
    return `<a class="mono" href="#/wiki?folder=">/</a>${segs.length ? ' ' + segs.join(' <span class="muted">/</span> ') : (cur === undefined ? '' : ' <span class="muted">(unfiled)</span>')}`;
  };
  // folder tree (from /api/wiki-tree) as a nested list; `cur` is the highlighted folder ('' = root)
  const folderTree = (t, cur) => {
    const rows = [];
    rows.push(`<a class="ftree-node depth0 ${cur === undefined ? 'on' : ''}" href="#/wiki"><span class="ftree-name">All pages</span></a>`);
    rows.push(`<a class="ftree-node depth0 ${cur === '' ? 'on' : ''}" href="#/wiki?folder="><span class="ftree-name">(unfiled)</span><span class="ftree-n">${t.unfiled_count}</span></a>`);
    for (const f of t.folders) {
      rows.push(`<a class="ftree-node depth${Math.min(f.depth, 5)} ${cur === f.path ? 'on' : ''}" href="#/wiki?folder=${encodeURIComponent(f.path)}" title="${esc(f.path)}"><span class="ftree-name">${esc(f.name)}/</span><span class="ftree-n" title="${f.page_count} here · ${f.total_count} in subtree">${f.total_count}</span></a>`);
    }
    return `<div class="ftree">${rows.join('')}</div>`;
  };

  route(/^\/wiki$/, async () => {
    setTab('wiki');
    const q = qs();
    const inFolder = q.folder !== undefined;                // folder scoped view ('' = unfiled)
    const params = new URLSearchParams();
    if (q.tag) params.set('tag', q.tag); if (q.dept) params.set('dept', q.dept);
    if (inFolder) { params.set('folder', q.folder); if (q.r) params.set('recursive', '1'); }
    const [pages, tree] = await Promise.all([GET(`/api/wiki${params.toString() ? `?${params}` : ''}`), GET('/api/wiki-tree')]);
    const allTags = [...new Set(pages.flatMap((p) => p.tags.split(',').filter(Boolean)))].sort();
    const subfolders = inFolder ? tree.folders.filter((f) => q.folder ? (f.path.startsWith(q.folder + '/') && f.depth === q.folder.split('/').length) : f.depth === 0) : [];
    // Inside one folder (non-recursive) every row has the same folder — drop the column.
    // Recursive: show the path relative to the current folder ('.' = directly here).
    const showFolder = !(inFolder && !q.r);
    const relFolder = (f) => { if (!inFolder || !q.folder) return f || ''; return f === q.folder ? '.' : f.startsWith(q.folder + '/') ? f.slice(q.folder.length + 1) : f; };
    const folderCell = (p) => p.folder
      ? `<a href="#/wiki?folder=${encodeURIComponent(p.folder)}" title="${esc(p.folder)}">${esc(relFolder(p.folder))}</a>`
      : '<span class="muted">—</span>';
    const table = `
        ${q.tag ? `<div class="filters">tag: <span class="chip on">${esc(q.tag)}</span> <a class="btn sm ghost" href="#/wiki">clear</a></div>` : ''}
        ${inFolder ? `<div class="filters"><a class="chip ${!q.r ? 'on' : ''}" href="#/wiki?folder=${encodeURIComponent(q.folder)}">this folder</a><a class="chip ${q.r ? 'on' : ''}" href="#/wiki?folder=${encodeURIComponent(q.folder)}&r=1">with sub-folders</a>${subfolders.map((f) => `<a class="chip" href="#/wiki?folder=${encodeURIComponent(f.path)}">📁 ${esc(f.name)} <span class="muted">${f.total_count}</span></a>`).join('')}</div>` : ''}
        <div class="wiki-table-scroll"><table class="grid fixed" id="wt"><colgroup><col>${showFolder ? '<col class="w170">' : ''}<col><col class="w120"><col class="w120"><col class="w70"></colgroup>
        <thead><tr><th>Page</th>${showFolder ? '<th>Folder</th>' : ''}<th>Tags</th><th>Dept</th><th>Updated</th><th>Size</th></tr></thead><tbody>
        ${pages.map((p) => `<tr data-k="${esc((p.title + ' ' + p.slug + ' ' + p.tags + ' ' + (p.folder || '')).toLowerCase())}"><td><a class="title" href="#/wiki/${p.slug}">${esc(p.title)}</a><div class="mono small muted ellipsis" title="${esc(p.slug)}">${esc(p.slug)}</div></td>${showFolder ? `<td class="mono small wrap">${folderCell(p)}</td>` : ''}<td>${tags(p.tags, '#/wiki')}</td><td class="mono small">${esc(p.department || '—')}</td><td class="ts" title="${esc(p.updated_at)}">${ago(p.updated_at)}<br><span class="muted">${esc(p.updated_by)}</span></td><td class="mono small">${(p.size / 1024).toFixed(1)}k</td></tr>`).join('') || `<tr><td colspan="${showFolder ? 6 : 5}" class="muted">${inFolder ? 'no pages in this folder' : 'no pages yet — agents write here with <code>coms wiki put</code>'}</td></tr>`}
        </tbody></table></div>`;
    const aside = `<div class="card"><h3>Tags</h3>${allTags.map((t) => `<a class="tag" href="#/wiki?tag=${encodeURIComponent(t)}">${esc(t)}</a>`).join('') || '<span class="muted">—</span>'}</div>
        <div class="card tint-c"><h3>For agents</h3><pre class="mono small" style="white-space:pre-wrap;margin:0">coms wiki search "&lt;topic&gt;"
coms wiki tree [prefix]
coms wiki get &lt;slug&gt;
coms wiki put &lt;slug&gt; --title … --folder a/b --file note.md
coms wiki mv &lt;slug&gt; a/b
coms wiki append &lt;slug&gt; --body "…"</pre></div>`;
    paint(`
      <div class="pagehead"><div><h1>Wiki</h1><div class="sub">long-term agent notes — how-tos, gotchas, verified procedures${inFolder ? ` · <span class="mono">${folderCrumb(q.folder, q.folder)}</span>` : ''}</div></div>
        <div class="actions"><input id="wf" class="wiki-filter mono" placeholder="filter…" style="border:3px solid #111;padding:.4rem .6rem">${asideToggleBtn()}<a class="btn primary" href="#/wiki/new${inFolder && q.folder ? `?folder=${encodeURIComponent(q.folder)}` : ''}">+ New page</a></div></div>
      ${layout({ id: 'wikiindex', nav: folderTree(tree, inFolder ? q.folder : undefined), body: table, aside })}`);
    wireAside('wikiindex', 'coms.wikiside');
    $('#wf').addEventListener('input', (e) => { const v = e.target.value.toLowerCase(); document.querySelectorAll('#wt tbody tr').forEach((tr) => tr.classList.toggle('hidden', !(tr.dataset.k || '').includes(v))); });
  });

  route(/^\/wiki\/new$/, async () => { setTab('wiki'); await loadRefs(); wikiEditor(null); });
  route(/^\/wiki\/([^/]+)\/edit$/, async (slug) => { setTab('wiki'); await loadRefs(); wikiEditor(await GET(`/api/wiki/${encodeURIComponent(slug)}`)); });
  route(/^\/wiki\/([^/]+)\/history$/, async (slug) => {
    setTab('wiki');
    const [p, revs] = await Promise.all([GET(`/api/wiki/${encodeURIComponent(slug)}`), GET(`/api/wiki/${encodeURIComponent(slug)}/revisions`)]);
    paint(`<div class="pagehead"><div><h1>History</h1><div class="sub"><a href="#/wiki/${p.slug}">${esc(p.title)}</a></div></div></div>
      <table class="grid"><thead><tr><th>Rev</th><th>Saved</th><th>By</th><th>Title</th><th>Size</th><th></th></tr></thead><tbody>
      <tr><td class="mono">current</td><td class="ts">${esc(p.updated_at)}</td><td>${chip(p.updated_by)}</td><td>${esc(p.title)}</td><td class="mono">${p.body.length}</td><td></td></tr>
      ${revs.map((r) => `<tr><td class="mono">#${r.id}</td><td class="ts">${esc(r.created_at)}</td><td>${chip(r.author)}</td><td>${esc(r.title)}</td><td class="mono">${r.size}</td><td><button class="btn sm" data-rev="${r.id}">view</button> <button class="btn sm pink" data-restore="${r.id}">restore</button></td></tr>`).join('')}
      </tbody></table><div id="revview"></div>`);
    app.addEventListener('click', async (e) => {
      const v = e.target.dataset.rev, r = e.target.dataset.restore;
      if (v) { const rev = await GET(`/api/wiki-revisions/${v}`); $('#revview').innerHTML = `<div class="card"><h3>Revision #${v}</h3><div class="md">${MD.render(rev.body)}</div></div>`; if (window.MMD) MMD.upgrade($('#revview')).catch(() => {}); if (window.PRETTY) PRETTY.upgrade($('#revview')).catch(() => {}); }
      if (r) { const rev = await GET(`/api/wiki-revisions/${r}`); await api('PUT', `/api/wiki/${encodeURIComponent(slug)}`, { title: rev.title, body: rev.body, tags: rev.tags }); toast('restored'); location.hash = `#/wiki/${slug}`; }
    });
  });

  route(/^\/wiki\/([^/]+)$/, async (slug) => {
    setTab('wiki');
    let p;
    try { p = await GET(`/api/wiki/${encodeURIComponent(slug)}`); } catch (e) {
      paint(`<div class="card tint-p"><h2>No page “${esc(slug)}”</h2><p>This page does not exist yet.</p><a class="btn primary" href="#/wiki/new?slug=${encodeURIComponent(slug)}">Create it</a></div>`); return;
    }
    const all = await GET('/api/wiki');
    const known = new Set(all.map((x) => x.slug));
    const toc = MD.outline(p.body).filter((h) => h.level <= 3).map((h) => `<a class="l${h.level}" href="#/wiki/${p.slug}#${h.id}" data-anchor="${h.id}">${esc(h.text)}</a>`).join('');
    paint(`
      <div class="pagehead"><div><h1>${esc(p.title)}</h1><div class="sub"><span class="mono">${folderCrumb(p.folder, p.folder || '')}</span> · ${esc(p.slug)} · updated ${ago(p.updated_at)} by ${esc(p.updated_by)} · created ${p.created_at.slice(0, 10)} by ${esc(p.author)}</div></div>
        <div class="actions">${asideToggleBtn()}<a class="btn" href="#/wiki/${p.slug}/history">History (${p.revision_count})</a><a class="btn primary" href="#/wiki/${p.slug}/edit">Edit</a><button class="btn danger" id="del">Delete</button></div></div>
      ${layout({ id: 'wikiread',
        body: `<div class="card"><div class="md" id="body">${MD.render(p.body)}</div></div>`,
        aside: `<div class="card"><h3>Meta</h3><dl class="kv"><dt>folder</dt><dd><span class="mono small">${p.folder ? esc(p.folder) : '<span class="muted">(unfiled)</span>'}</span> <button class="btn sm ghost" id="mv" title="move to another folder">move</button></dd><dt>tags</dt><dd>${tags(p.tags, '#/wiki') || '—'}</dd><dt>dept</dt><dd>${esc(p.department || '—')}</dd><dt>author</dt><dd>${chip(p.author)}</dd></dl></div>
          ${toc ? `<div class="card"><h3>Contents</h3><div class="toc">${toc}</div></div>` : ''}
          <div class="card tint-c"><h3>Backlinks</h3>${p.backlinks.map((b) => `<div><a href="#/wiki/${b.slug}">${esc(b.title)}</a></div>`).join('') || '<span class="muted small">none link here yet</span>'}</div>` })}`);
    wireAside('wikiread', 'coms.wikiside');
    wireWikilinks($('#body'), known);
    document.querySelectorAll('.toc a').forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); const el = document.getElementById(a.dataset.anchor); if (el) el.scrollIntoView({ behavior: 'smooth' }); }));
    $('#del').addEventListener('click', async () => { if (!confirm(`Delete “${p.title}”? Revisions are removed too.`)) return; await api('DELETE', `/api/wiki/${encodeURIComponent(slug)}`).catch(fail); toast('deleted'); location.hash = '#/wiki'; });
    $('#mv').addEventListener('click', async () => {
      const f = prompt('Move to folder (slash path, e.g. folder-a/folder-b/folder-c; empty = unfiled):', p.folder || '');
      if (f === null) return;
      try { await api('POST', `/api/wiki/${encodeURIComponent(slug)}/move`, { folder: f }); toast('moved'); render(); } catch (e) { fail(e); }
    });
  });

  function wikiEditor(p) {
    const q = qs();
    const isNew = !p;
    paint(`
      <div class="pagehead"><div><h1>${isNew ? 'New page' : 'Edit page'}</h1><div class="sub">${isNew ? 'markdown · [[wikilinks]] resolve to other pages' : esc(p.slug)}</div></div>
        <div class="actions"><a class="btn" href="${isNew ? '#/wiki' : `#/wiki/${p.slug}`}">Cancel</a><button class="btn ok" id="save">Save</button></div></div>
      <div class="card flat form" style="margin-bottom:1rem">
        <div class="grid-3">
          ${field('Title', `<input id="title" value="${esc(p ? p.title : '')}" placeholder="How to …">`)}
          ${field('Slug', `<input id="slug" class="mono" value="${esc(p ? p.slug : (q.slug || ''))}" placeholder="auto from title" ${isNew ? '' : 'readonly'}>`)}
          ${field('Tags (comma separated)', `<input id="tags" value="${esc(p ? p.tags : '')}" placeholder="ops, howto, runbook">`)}
        </div>
        <div class="grid-3">${field('Department', `<select id="dept">${deptOpts(p ? p.department : '')}</select>`)}
          ${field('Folder (slash path; empty = unfiled)', `<input id="folder" class="mono" list="folders" value="${esc(p ? (p.folder || '') : (q.folder || ''))}" placeholder="folder-a/folder-b/folder-c"><datalist id="folders"></datalist>`)}</div>
      </div>
      <div id="ed"></div>`);
    const ed = new MdEditor($('#ed'), { value: p ? p.body : '', onSave: save, minHeight: '60vh', placeholder: '# Title\n\nWhat this note lets an agent do, the exact commands, the gotchas, and where the evidence lives (source deep links).' });
    // resolve [[links]] against the real page list so missing ones show as missing
    GET('/api/wiki').then((all) => ed.setKnownSlugs(new Set(all.map((x) => x.slug)))).catch(() => {});
    // existing folders as autocomplete suggestions
    GET('/api/wiki-tree').then((t) => { $('#folders').innerHTML = t.folders.map((f) => `<option value="${esc(f.path)}">`).join(''); }).catch(() => {});
    if (isNew) $('#title').addEventListener('input', () => { if (!$('#slug').dataset.touched) $('#slug').value = MD.slugify($('#title').value); });
    $('#slug').addEventListener('input', () => { $('#slug').dataset.touched = '1'; });
    $('#save').addEventListener('click', () => save(ed.value));
    async function save(body) {
      const title = $('#title').value.trim(); const slug = $('#slug').value.trim() || MD.slugify(title);
      if (!title) return toast('title required', true);
      try {
        const r = await api(isNew ? 'POST' : 'PUT', isNew ? '/api/wiki' : `/api/wiki/${encodeURIComponent(slug)}`, { slug, title, body, tags: $('#tags').value, department: $('#dept').value, folder: $('#folder').value.trim() });
        toast('saved'); location.hash = `#/wiki/${r.slug}`;
      } catch (e) { fail(e); }
    }
    ed.focus();
  }

  // ------------------------------------------------------------ TICKETS
  const boardOpts = (boards, cur) => boards.map((b) => `<option value="${b.id}"${String(b.id) === String(cur) ? ' selected' : ''}>${esc(b.name)}</option>`).join('');
  const rememberBoard = (id) => localStorage.setItem('coms.ticketboard', String(id));
  const currentBoard = (boards, id) => boards.find((b) => String(b.id) === String(id || localStorage.getItem('coms.ticketboard'))) || boards.find((b) => b.id === 1);
  async function boardEditor(board) {
    try {
      const pages = await GET('/api/wiki');
      const dialog = document.createElement('dialog');
      dialog.className = 'board-dialog card';
      dialog.innerHTML = `<form class="form"><h2>${board ? 'Edit board' : 'New board'}</h2>
        ${field('Board name', `<input name="name" required maxlength="200" value="${esc(board ? board.name : '')}">`)}
        ${field('Description', `<textarea name="description" rows="3">${esc(board ? board.description : '')}</textarea>`)}
        ${field('Project context in wiki', `<select name="wiki_slug"><option value="">— no page linked —</option>${pages.map((p) => `<option value="${esc(p.slug)}"${board && board.wiki_slug === p.slug ? ' selected' : ''}>${esc(p.title)} (${esc(p.slug)})</option>`).join('')}</select>`)}
        <p class="small muted">Link a wiki page with codebase notes and documentation. Wiki pages remain shared.</p>
        <p class="board-error small" role="alert"></p>
        <div class="form-actions"><button class="btn" type="button" data-cancel>Cancel</button><button class="btn ok" type="submit">Save board</button></div></form>`;
      document.body.appendChild(dialog);
      dialog.addEventListener('close', () => dialog.remove());
      $('[data-cancel]', dialog).onclick = () => dialog.close();
      $('form', dialog).onsubmit = async (e) => {
        e.preventDefault();
        const button = $('button[type="submit"]', dialog); button.disabled = true;
        try {
          const saved = await api(board ? 'PATCH' : 'POST', board ? `/api/ticket-boards/${board.id}` : '/api/ticket-boards', Object.fromEntries(new FormData(e.target)));
          rememberBoard(saved.id); dialog.close(); toast('board saved');
          const target = `#/tickets?board=${saved.id}`;
          if (location.hash === target) render(); else location.hash = target;
        } catch (err) { $('.board-error', dialog).textContent = err.message; button.disabled = false; }
      };
      dialog.showModal(); $('input[name="name"]', dialog).focus();
    } catch (e) { fail(e); }
  }
  const ticketItem = (t) => `<div class="item"><div class="main"><a class="title" href="#/tickets/${t.id}">#${t.id} ${esc(t.title)}</a>
    <div class="meta">${badge(t.status)}${badge(t.priority)}${badge(t.type)} ${t.parent_id ? `<a class="mono small" href="#/tickets/${t.parent_id}" title="parent ticket">↑ #${t.parent_id}</a>` : ''} <span>${t.assignee ? chip(t.assignee) : '<b>unassigned</b>'}</span> ${t.department ? `<span>${esc(t.department)}</span>` : ''} <span class="ts">${ago(t.updated_at)}</span> ${t.comment_count ? `<span>💬 ${t.comment_count}</span>` : ''} ${tags(t.tags, `#/tickets?board=${t.board_id}`)}</div></div></div>`;
  // brief dict (parent / child / tree node): id,title,type,status,priority,assignee,requester,parent_id,updated_at
  const briefItem = (b) => `<div class="item"><div class="main"><a class="title" href="#/tickets/${b.id}">#${b.id} ${esc(b.title)}</a>
    <div class="meta">${badge(b.type)}${badge(b.status)}${badge(b.priority)} ${b.assignee ? chip(b.assignee) : '<b>unassigned</b>'} <span class="ts">${ago(b.updated_at)}</span></div></div></div>`;

  route(/^\/tickets$/, async () => {
    setTab('tickets');
    const q = qs();
    await loadRefs();
    const boards = await GET('/api/ticket-boards');
    const board = currentBoard(boards, q.board);
    q.board = String(board.id); rememberBoard(board.id);
    const view = q.view || localStorage.getItem('coms.tview') || 'list';
    const params = new URLSearchParams();
    params.set('board', board.id);
    if (q.status) params.set('status', q.status); else if (q.all) params.set('all', '1'); else if (view === 'board') params.set('all', '1');
    if (q.assignee) params.set('assignee', q.assignee); if (q.unassigned) params.set('unassigned', '1');
    if (q.dept) params.set('dept', q.dept); if (q.type) params.set('type', q.type);
    let tix = await GET(`/api/tickets?${params}`);
    if (q.tag) tix = tix.filter((t) => t.tags.split(',').includes(q.tag));
    const link = (k, v) => { const n = { ...q }; if (v === undefined) delete n[k]; else n[k] = v; return `#/tickets?${new URLSearchParams(n)}`; };
    paint(`
      <div class="pagehead"><div><h1>Tickets</h1><div class="sub">research tasks, chores and bugs for the agent team</div></div>
        <div class="actions"><button class="btn ${view === 'list' ? 'primary' : ''}" id="vl">List</button><button class="btn ${view === 'board' ? 'primary' : ''}" id="vb">Board</button><a class="btn ok" href="#/tickets/new?board=${board.id}">+ New ticket</a></div></div>
      <div class="board-toolbar"><label for="ticket-board">Project board</label><select id="ticket-board">${boardOpts(boards, board.id)}</select>
        <div class="actions"><button class="btn sm" id="board-new">+ New board</button><button class="btn sm" id="board-edit">Edit board</button><button class="btn sm danger" id="board-delete"${board.id === 1 ? ' disabled title="The default board is kept for existing tickets and integrations"' : ''}>Delete board</button></div>
      </div>
      ${board.description || board.wiki_slug ? `<div class="board-context">${board.description ? `<span>${esc(board.description)}</span>` : ''}${board.wiki_slug ? `<a class="btn sm" href="#/wiki/${encodeURIComponent(board.wiki_slug)}">Project wiki ↗</a>` : ''}</div>` : ''}
      <div class="filters">
        <a class="chip ${!q.status && !q.all && !q.unassigned ? 'on' : ''}" href="#/tickets?board=${board.id}">active</a>
        ${TS.map((s) => `<a class="chip ${q.status === s ? 'on' : ''}" href="${link('status', s)}">${s.replace('_', ' ')}</a>`).join('')}
        <a class="chip ${q.all ? 'on' : ''}" href="${link('all', '1')}">all</a>
        <a class="chip ${q.unassigned ? 'on' : ''}" href="${link('unassigned', '1')}">unassigned</a>
        <select id="fa"><option value="">assignee: any</option>${AGENTS.map((a) => `<option value="${a.slug}"${q.assignee === a.slug ? ' selected' : ''}>${esc(a.slug)}</option>`).join('')}</select>
        <select id="fd"><option value="">dept: any</option>${DEPTS.map((d) => `<option value="${d.slug}"${q.dept === d.slug ? ' selected' : ''}>${esc(d.name)}</option>`).join('')}</select>
        <select id="ft"><option value="">type: any</option>${TT.map((t) => `<option value="${t}"${q.type === t ? ' selected' : ''}>${t}</option>`).join('')}</select>
        ${q.tag ? `<span class="chip on">tag: ${esc(q.tag)}</span> <a class="btn sm ghost" href="${link('tag', undefined)}">×</a>` : ''}
        <span class="muted mono small">${tix.length} tickets</span></div>
      ${view === 'board' ? kanban(tix) : (tix.length ? `<div class="list">${tix.map(ticketItem).join('')}</div>` : '<div class="empty">no tickets match</div>')}`);
    const showView = (mode) => { localStorage.setItem('coms.tview', mode); const target = link('view', mode); if (location.hash === target) render(); else location.hash = target; };
    $('#vl').onclick = () => showView('list');
    $('#vb').onclick = () => showView('board');
    $('#fa').onchange = (e) => location.hash = link('assignee', e.target.value || undefined);
    $('#fd').onchange = (e) => location.hash = link('dept', e.target.value || undefined);
    $('#ft').onchange = (e) => location.hash = link('type', e.target.value || undefined);
    $('#ticket-board').onchange = (e) => { rememberBoard(e.target.value); location.hash = link('board', e.target.value); };
    $('#board-new').onclick = () => boardEditor(null);
    $('#board-edit').onclick = () => boardEditor(board);
    $('#board-delete').onclick = async () => {
      if (!confirm(`Delete board “${board.name}” and ALL ${board.ticket_count} tickets, including comments and links? This cannot be undone. Wiki pages are kept.`)) return;
      try { await api('DELETE', `/api/ticket-boards/${board.id}`); rememberBoard(1); toast('board deleted'); location.hash = '#/tickets?board=1'; } catch (e) { fail(e); }
    };
  });
  function kanban(tix) {
    const cols = [['open', 'Open'], ['in_progress', 'In progress'], ['blocked', 'Blocked'], ['review', 'Review'], ['done', 'Done'], ['wontfix', 'Won’t fix']];
    return `<div class="kanban">${cols.map(([s, l]) => { const items = tix.filter((t) => t.status === s).slice(0, ['done', 'wontfix'].includes(s) ? 15 : 100); return `<div class="colm"><h3>${l}<span>${items.length}</span></h3>${items.map((t) => `<div class="kcard"><a href="#/tickets/${t.id}">#${t.id} ${esc(t.title)}</a><div class="m">${badge(t.priority)}${badge(t.type)}${t.parent_id ? `<a class="mono small" href="#/tickets/${t.parent_id}" title="parent ticket">↑ #${t.parent_id}</a>` : ''}${t.assignee ? chip(t.assignee) : '<b>unassigned</b>'}</div></div>`).join('')}</div>`; }).join('')}</div>`;
  }

  route(/^\/tickets\/new$/, async () => {
    setTab('tickets'); await loadRefs();
    let templates = []; try { templates = await GET('/api/runner/templates'); } catch (e) { if(e.name==='AbortError')throw e; }
    const q = qs();
    const boards = await GET('/api/ticket-boards');
    const parent = q.parent ? await GET(`/api/tickets/${q.parent}`) : null;
    const board = currentBoard(boards, parent ? parent.board_id : q.board);
    paint(`<div class="pagehead"><h1>New ticket</h1></div>
      <div class="card form" style="max-width:1000px">
        ${field('Project board', `<select id="new-board">${boardOpts(boards, board.id)}</select>`)}
        ${field('Title', `<input id="title" placeholder="Research: …">`)}
        <div class="grid-3">${field('Type', `<select id="type">${opts(TT, q.type || 'research')}</select>`)}${field('Priority', `<select id="prio">${opts(PR, 'p2')}</select>`)}${field('Department', `<select id="dept">${deptOpts(q.dept || (DEPTS[0] && DEPTS[0].slug))}</select>`)}</div>
        <div class="grid-2">${field('Assignee', `<select id="assignee">${agentOpts(q.assignee || '')}</select>`)}${field('Tags', `<input id="tags" placeholder="topic, area">`)}</div>
        <div class="grid-2">${field('Agent template', `<select id="template"><option value="">Use default runner</option>${templates.filter(x=>x.enabled).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select>`)}${field('Execution gate', '<label class="small"><input id="ready" type="checkbox"> Ready for agent execution</label>')}</div>
        <div class="grid-2">${field('Parent ticket id (optional) — story &gt; job &gt; task', `<input id="parent" type="number" min="1" value="${esc(q.parent || '')}" placeholder="e.g. 42">`)}${field('Suggested workers for a job (optional)', '<input id="workers" type="number" min="1" max="15" placeholder="Let the orchestrator decide"><small class="muted">Jobs use a named orchestrator who reuses or creates teammates.</small>')}</div>
        ${field('Body (markdown) — what to find out, what "done" looks like, where to start', '<div id="ed"></div>')}
        <div class="form-actions"><a class="btn" href="#/tickets?board=${board.id}">Cancel</a><button class="btn ok" id="save">Create</button></div></div>`);
    const ed = new MdEditor($('#ed'), { onSave: save, minHeight: '300px', placeholder: '## Goal\n\n## Definition of done\n\n## Starting points\n- link to source…' });
    $('#save').onclick = () => save(ed.value);
    async function save(body) {
      const payload = { title: $('#title').value, body, type: $('#type').value, priority: $('#prio').value, department: $('#dept').value, assignee: $('#assignee').value, tags: $('#tags').value, ready_for_execution: $('#ready').checked, agent_template_id: $('#template').value || null };
      const p = $('#parent').value.trim(); if (p) payload.parent_id = +p;
      payload.board_id = Number($('#new-board').value);
      payload.recommended_workers = $('#workers').value ? Number($('#workers').value) : null;
      try { const t = await api('POST', '/api/tickets', payload); toast(`ticket #${t.id} created`); location.hash = `#/tickets/${t.id}`; } catch (e) { fail(e); }
    }
    $('#title').focus();
  });

  route(/^\/tickets\/(\d+)$/, async (id) => {
    setTab('tickets'); await loadRefs();
    const [t, runnerData, templates] = await Promise.all([GET(`/api/tickets/${id}`), GET(`/api/tickets/${id}/runner`), GET('/api/runner/templates').catch(e=>{if(e.name==='AbortError')throw e;return [];})]);
    const boards = await GET('/api/ticket-boards');
    const board = currentBoard(boards, t.board_id); rememberBoard(board.id);
    const known = new Set((await GET('/api/wiki')).map((x) => x.slug));
    paint(`
      <div class="crumb mono small"><a href="#/tickets?board=${board.id}">← ${esc(board.name)} board</a></div>
      <div class="pagehead"><div>${t.parent ? `<div class="crumb small mono" style="margin-bottom:.3rem">↑ parent <a href="#/tickets/${t.parent.id}">#${t.parent.id} ${esc(t.parent.title)}</a> ${badge(t.parent.type)}${badge(t.parent.status)}</div>` : ''}<h1>#${t.id} <span id="ttl">${esc(t.title)}</span></h1><div class="sub">opened ${ago(t.created_at)} by ${esc(t.requester || '?')} · updated ${ago(t.updated_at)}${t.closed_at ? ` · closed ${t.closed_at.slice(0, 10)}` : ''}</div></div>
        <div class="actions"><a class="btn" href="#/tickets/${t.id}/tree">🌲 Tree</a>${runnerData.retry?.eligible ? `<button class="btn pink" id="retry-agent">Retry ${esc(runnerData.retry.kind)}</button>` : ''}${t.status === 'open' && !t.assignee ? `<button class="btn pink" id="claim">Claim as ${esc(who())}</button>` : ''}<button class="btn" id="editbody">Edit body</button><button class="btn ok" id="editresult">${t.result ? 'Edit result' : 'Write result'}</button></div></div>
      ${layout({ id: 'ticket', body: `
        <div class="card" id="bodycard"><div class="md" id="body">${MD.render(t.body) || '<span class="muted">no description</span>'}</div></div>
        <div class="card" id="ticket-children"${t.children.length?'':' hidden'}><div class="card-title"><h3>Teammate tickets</h3><a class="btn sm" href="#/tickets/new?parent=${t.id}">+ child</a></div><div class="list">${t.children.map(briefItem).join('')}</div></div>
        <div class="card"><div class="card-title"><h3>Links (${(t.links || []).length})</h3></div>
          ${(t.links || []).length ? `<div class="list">${t.links.map((ln) => `<div class="item"><div class="main">
            <a class="title" href="${ln.kind === 'wiki' ? `#/wiki/${encodeURIComponent(ln.ref)}` : ln.kind === 'question' ? `#/questions/${ln.ref}` : ln.kind === 'ticket' ? `#/tickets/${ln.ref}` : esc(ln.ref)}"${ln.kind === 'url' ? ' target="_blank" rel="noopener"' : ''}>${{ wiki: '📖', question: '❓', ticket: '🎫', url: '🔗' }[ln.kind] || ''} ${ln.kind}:${esc(ln.ref)}${ln.title ? ` — ${esc(ln.title)}` : ''}</a>
            <div class="meta">${ln.note ? `<span>${esc(ln.note)}</span>` : ''}<span class="ts">${chip(ln.created_by || '?')}</span><button class="btn sm ghost" data-unlink="${esc(ln.kind)}|${esc(ln.ref)}" title="remove link">×</button></div></div></div>`).join('')}</div>` : '<div class="muted small">no attached context — link the wiki notes and questions this work rests on (<code>coms ticket link</code>)</div>'}
          <div class="actions" style="margin-top:.6rem"><select id="lk-kind"><option value="wiki">wiki</option><option value="question">question</option><option value="ticket">ticket</option><option value="url">url</option></select><input id="lk-ref" placeholder="slug / id / url" style="max-width:200px"><input id="lk-note" placeholder="note (optional)" style="max-width:180px"><button class="btn sm" id="lk-add">+ link</button></div></div>
        <div class="card ${t.result ? 'tint-g' : ''}" id="resultcard"><div class="card-title"><h3>Result</h3></div><div class="md" id="result">${t.result ? MD.render(t.result) : '<span class="muted">nothing delivered yet — the assignee records the outcome here (<code>coms ticket done</code>)</span>'}</div></div>
        <div class="card" id="ticket-progress">${progressHTML(runnerData.progress)}</div>
        ${runnerData.retry?.error ? `<div class="card tint-o" role="status"><b>${runnerData.retry.eligible ? 'Last attempt failed' : esc(runnerData.retry.reason)}</b><pre class="small" style="white-space:pre-wrap">${esc(runnerData.retry.error)}</pre></div>` : ''}
        ${t.team_job_id?'<div class="card" id="ticket-team"></div>':''}
        <div class="card" id="runner-chat"><div class="card-title"><h3>Agent runner chat</h3><span class="badge ${t.ready_for_execution?'in_progress':'blocked'}">${t.ready_for_execution?'ready gate open':'awaiting orchestration'}</span></div>
          <div class="mono small muted">Watch current work in Live progress above. Full responses and tool output are in <a href="#/debug?ticket=${t.id}">Debug</a>; published updates appear in Comments.</div>
          <div id="runner-messages" class="runner-messages">${runnerData.messages.filter(m=>m.role==='user'&&m.kind==='text').map(m=>`<div class="runner-msg user" data-message-id="${m.id}"><b>You</b><pre>${esc(m.body)}</pre><span class="ts">${esc(m.created_at)}</span></div>`).join('') || '<div class="empty">No chat messages yet.</div>'}</div>
          <form id="runner-form" class="runner-form"><textarea id="runner-input" placeholder="Message the ticket agent…" rows="3"></textarea><button class="btn primary">Send to agent</button></form>
          <div class="mono small muted">${runnerData.runs.map(r=>`Run #${r.id}: ${esc(r.status)} · ${esc(r.backend)}${r.error?` · ${esc(r.error)}`:''}`).join('<br>') || 'Queue starts only after Ready for agent execution is enabled and the ticket is not assigned/tagged for a human.'}</div>
        </div>
        <div class="card"><h3>Timeline</h3><div class="timeline">${t.comments.map((c) => `<div class="tl ${c.kind}"><div><div class="who">${chip(c.author)}</div><div class="ts">${ago(c.created_at)}${c.updated_at ? ` · edited ${ago(c.updated_at)}` : ''}</div></div><div class="txt ${c.kind === 'comment' ? 'md' : ''}">${c.kind === 'comment' ? MD.render(c.body) : esc(c.body)}${c.kind === 'comment' && c.author === who() ? `<div class="comment-actions"><button class="btn sm ghost" data-ticket-comment-edit="${c.id}">edit</button><button class="btn sm ghost" data-ticket-comment-delete="${c.id}">delete</button></div>` : ''}</div></div>`).join('') || '<span class="muted small">no comments</span>'}</div>
          <div style="margin-top:1rem"><div id="ced"></div><div class="form-actions" style="margin-top:.5rem"><button class="btn" id="cmt">Comment</button></div></div></div>`,
        aside: `
        <div class="card"><h3>Fields</h3><div class="ticket-meta" style="grid-template-columns:1fr">
          <label>Project board<select id="f-board">${boardOpts(boards, t.board_id)}</select></label>
          <label>Status<select id="f-status">${opts(TS, t.status)}</select></label>
          <label>Priority<select id="f-priority">${opts(PR, t.priority)}</select></label>
          <label>Type<select id="f-type">${opts(TT, t.type)}</select></label>
          <label>Assignee<select id="f-assignee">${agentOpts(t.assignee)}</select></label>
          <label>Department<select id="f-department">${deptOpts(t.department)}</select></label>
          <label>Tags<input id="f-tags" value="${esc(t.tags)}"></label>
          <label>Execution template<select id="f-template"><option value="">Use active backend</option>${templates.map(x=>`<option value="${x.id}"${String(x.id)===String(t.agent_template_id)?' selected':''}>${esc(x.name)}</option>`).join('')}</select></label>
          <label><input id="f-ready" type="checkbox"${t.ready_for_execution?' checked':''}> Ready for agent execution</label>
          <label>Suggested workers (jobs)<input id="f-workers" type="number" min="1" max="15" value="${esc(t.recommended_workers||'')}" placeholder="Orchestrator decides"></label>
          <label>Title<input id="f-title" value="${esc(t.title)}"></label>
        </div><div class="form-actions" style="margin-top:.8rem"><button class="btn primary" id="savef">Save fields</button></div></div>
        <div class="card tint-y small"><b class="mono">quick moves</b><div class="actions" style="margin-top:.5rem">${TS.filter((s) => s !== t.status).map((s) => `<button class="btn sm" data-mv="${s}">${s.replace('_', ' ')}</button>`).join('')}</div></div>` })}`);
    wireWikilinks(app, known);
    app.querySelectorAll('[data-ticket-comment-edit]').forEach((button) => {
      button.onclick = () => {
        const comment = t.comments.find((c) => c.id === Number(button.dataset.ticketCommentEdit));
        const text = button.closest('.txt');
        text.innerHTML = '<textarea class="comment-edit-input" rows="4" aria-label="Edit comment"></textarea><div class="comment-actions"><button class="btn sm" data-comment-save>save</button><button class="btn sm ghost" data-comment-cancel>cancel</button></div>';
        const input = text.querySelector('textarea'); input.value = comment.body; input.focus();
        text.querySelector('[data-comment-cancel]').onclick = () => render();
        text.querySelector('[data-comment-save]').onclick = async () => {
          try { await api('PATCH', `/api/tickets/${id}/comments/${comment.id}`, { body: input.value }); toast('comment updated'); render(); }
          catch (e) { fail(e); }
        };
      };
    });
    app.querySelectorAll('[data-ticket-comment-delete]').forEach((button) => {
      button.onclick = async () => {
        if (!confirm('Delete this comment?')) return;
        try { await api('DELETE', `/api/tickets/${id}/comments/${button.dataset.ticketCommentDelete}`); toast('comment deleted'); render(); }
        catch (e) { fail(e); }
      };
    });
    const patch = async (data, msg) => { try { const r = await api('PATCH', `/api/tickets/${id}`, data); toast(msg || 'saved'); if (r && r.open_children_warning && r.open_children_warning.length) { toast(`⚠ ${r.open_children_warning.length} open child ticket(s): ${r.open_children_warning.map((c) => '#' + c.id).join(', ')}`, true); } render(); } catch (e) { fail(e); } };
    $('#savef').onclick = () => patch({ board_id: Number($('#f-board').value), status: $('#f-status').value, priority: $('#f-priority').value, type: $('#f-type').value, assignee: $('#f-assignee').value, department: $('#f-department').value, tags: $('#f-tags').value, title: $('#f-title').value, ready_for_execution: $('#f-ready').checked, agent_template_id: $('#f-template').value || null, recommended_workers:$('#f-workers').value?Number($('#f-workers').value):null });
    if ($('#retry-agent')) $('#retry-agent').onclick = async () => { try { const r=await api('POST', `/api/tickets/${id}/retry`, {}); toast(`queued ${r.retry_kind} retry`); render(); } catch (e) { fail(e); } };
    $('#runner-form').onsubmit = async (e) => { e.preventDefault(); const input=$('#runner-input'), body=input.value.trim(); if(!body)return; try { await api('POST', `/api/tickets/${id}/runner`, {body}); input.value=''; await refreshRunner(); } catch(err){fail(err);} };
    async function refreshRunner() {
      const d=await GET(`/api/tickets/${id}/runner`), box=$('#runner-messages');
      if(!box)return;
      drawProgress($('#ticket-progress'),d.progress);
      const messages=d.messages.filter(m=>m.role==='user'&&m.kind==='text'), scroll=scrollState(box);
      let empty=box.querySelector('.empty');
      if(messages.length)empty?.remove();
      else if(!empty){empty=document.createElement('div');empty.className='empty';empty.textContent='No chat messages yet.';box.appendChild(empty);}
      for(const m of messages){
        if(box.querySelector(`[data-message-id="${m.id}"]`))continue;
        const node=document.createElement('div');node.className='runner-msg user';node.dataset.messageId=m.id;
        node.innerHTML=`<b>You</b><pre>${esc(m.body)}</pre><span class="ts">${esc(m.created_at)}</span>`;box.appendChild(node);
      }
      restoreScroll(box,scroll);
      const latest=await GET(`/api/tickets/${id}`),childrenBox=$('#ticket-children'),resultBox=$('#result');
      if(childrenBox&&!hasSelection(childrenBox)){childrenBox.hidden=!latest.children.length;$('.list',childrenBox).innerHTML=latest.children.map(briefItem).join('');}
      if(resultBox&&latest.result!==t.result&&!resultBox.querySelector('textarea,[contenteditable="true"]')&&!hasSelection(resultBox)){resultBox.innerHTML=MD.render(latest.result)||'<span class="muted">Nothing delivered yet.</span>';t.result=latest.result;}
    }
    poll(refreshRunner,2000);
    if(t.team_job_id)await mountTeam('ticket-team',t.team_job_id);
    document.querySelectorAll('[data-mv]').forEach((b) => b.onclick = () => patch({ status: b.dataset.mv }, `→ ${b.dataset.mv}`));
    if ($('#claim')) $('#claim').onclick = async () => { try { await api('POST', `/api/tickets/${id}/claim`); toast('claimed'); render(); } catch (e) { fail(e); } };
    $('#lk-add').onclick = async () => {
      const ref = $('#lk-ref').value.trim(); if (!ref) return;
      try { await api('POST', `/api/tickets/${id}/links`, { kind: $('#lk-kind').value, ref, note: $('#lk-note').value.trim() }); toast('linked'); render(); } catch (e) { fail(e); }
    };
    document.querySelectorAll('[data-unlink]').forEach((b) => b.onclick = async () => {
      const [kind, ref] = b.dataset.unlink.split('|');
      try { await api('DELETE', `/api/tickets/${id}/links`, { kind, ref }); toast('unlinked'); render(); } catch (e) { fail(e); }
    });
    const ced = new MdEditor($('#ced'), { placeholder: 'add a comment (markdown)…', onSave: comment, minHeight: '110px', knownSlugs: known });
    $('#cmt').onclick = () => comment(ced.value);
    async function comment(body) { if (!body.trim()) return; try { await api('POST', `/api/tickets/${id}/comments`, { body }); toast('comment added'); ced.setValue(''); render(); } catch (e) { fail(e); } }
    $('#editbody').onclick = () => inlineEdit('#bodycard', t.body, (body) => patch({ body }, 'body saved'));
    $('#editresult').onclick = () => inlineEdit('#resultcard', t.result, (result) => patch({ result, status: t.status === 'done' ? 'done' : $('#markdone') && $('#markdone').checked ? 'done' : undefined }, 'result saved'), true);
  });
  route(/^\/tickets\/(\d+)\/tree$/, async (id) => {
    setTab('tickets');
    const data = await GET(`/api/tickets/${id}/tree`);
    const focus = data.focus;
    const node = (n, depth) => {
      const on = n.id === focus;
      const row = `<div class="tree-node${on ? ' focus' : ''}" style="margin-left:${depth * 1.6}rem">
        <a class="title" href="#/tickets/${n.id}">#${n.id} ${esc(n.title)}</a>
        <span class="meta">${badge(n.type)}${badge(n.status)}${badge(n.priority)} ${n.assignee ? chip(n.assignee) : '<b>unassigned</b>'}</span></div>`;
      return row + (n.children || []).map((c) => node(c, depth + 1)).join('');
    };
    paint(`
      <div class="pagehead"><div><h1>Tree · #${focus}</h1><div class="sub">story &gt; job &gt; task hierarchy · <a href="#/tickets/${focus}">back to ticket</a></div></div></div>
      <div class="card"><div class="tree">${node(data.root, 0)}</div></div>`);
  });

  function inlineEdit(sel, value, onSave, withDone) {
    const card = $(sel);
    card.innerHTML = `<div id="ie"></div>${withDone ? '<label class="mono small" style="display:block;margin-top:.5rem"><input type="checkbox" id="markdone" checked> also mark ticket done</label>' : ''}<div class="form-actions" style="margin-top:.6rem"><button class="btn" id="ie-cancel">Cancel</button><button class="btn ok" id="ie-save">Save</button></div>`;
    const ed = new MdEditor($('#ie'), { value, onSave, minHeight: '260px' });
    $('#ie-save').onclick = () => onSave(ed.value);
    $('#ie-cancel').onclick = () => render();
    ed.focus();
  }

  // ------------------------------------------------------------ QUESTIONS
  const questionItem = (q) => `<div class="item"><div class="score-box ${q.accepted_answer_id ? 'acc' : ''}"><div class="n">${q.score}</div><div class="l">votes</div><div class="n" style="font-size:.95rem;margin-top:.2rem">${q.answer_count}</div><div class="l">answers</div></div>
    <div class="main"><a class="title" href="#/questions/${q.id}">${esc(q.title)}</a><div class="meta">${badge(q.status)} ${chip(q.author)} <span class="ts">${ago(q.updated_at)}</span> ${q.department ? `<span>${esc(q.department)}</span>` : ''} ${tags(q.tags, '#/questions')}</div></div></div>`;

  route(/^\/questions$/, async () => {
    setTab('questions');
    const q = qs();
    const params = new URLSearchParams(); if (q.status) params.set('status', q.status); if (q.tag) params.set('tag', q.tag); if (q.unanswered) params.set('unanswered', '1'); if (q.dept) params.set('dept', q.dept);
    const list = await GET(`/api/questions?${params}`);
    const link = (k, v) => { const n = { ...q }; if (v === undefined) delete n[k]; else n[k] = v; return `#/questions?${new URLSearchParams(n)}`; };
    paint(`
      <div class="pagehead"><div><h1>Questions</h1><div class="sub">questions from agents · answers from any agent · votes decide</div></div><div class="actions"><a class="btn ok" href="#/questions/ask">+ Ask question</a></div></div>
      <div class="filters"><a class="chip ${!q.status && !q.unanswered ? 'on' : ''}" href="#/questions">all</a><a class="chip ${q.unanswered ? 'on' : ''}" href="${link('unanswered', '1')}">unanswered</a>${['open', 'answered', 'closed'].map((s) => `<a class="chip ${q.status === s ? 'on' : ''}" href="${link('status', s)}">${s}</a>`).join('')}${q.tag ? `<span class="chip on">tag: ${esc(q.tag)}</span> <a class="btn sm ghost" href="${link('tag', undefined)}">×</a>` : ''}<span class="muted mono small">${list.length} questions</span></div>
      ${list.length ? `<div class="list">${list.map(questionItem).join('')}</div>` : '<div class="empty">no questions match</div>'}`);
  });

  route(/^\/questions\/ask$/, async () => {
    setTab('questions'); await loadRefs();
    paint(`<div class="pagehead"><h1>Ask a question</h1></div>
      <div class="card form" style="max-width:1000px">${field('Title — be specific, e.g. "How do I write a good reproduction note?"', '<input id="title">')}
        <div class="grid-2">${field('Tags', '<input id="tags" placeholder="topic, area, howto">')}${field('Department', `<select id="dept">${deptOpts(DEPTS[0] && DEPTS[0].slug)}</select>`)}</div>
        ${field('Body (markdown) — context, what you already checked, why it matters', '<div id="ed"></div>')}
        <div class="form-actions"><a class="btn" href="#/questions">Cancel</a><button class="btn ok" id="save">Post question</button></div></div>`);
    const ed = new MdEditor($('#ed'), { onSave: save, placeholder: '## Context\n\n## What I already checked\n\n## Why it matters' });
    $('#save').onclick = () => save(ed.value);
    async function save(body) { try { const r = await api('POST', '/api/questions', { title: $('#title').value, body, tags: $('#tags').value, department: $('#dept').value }); toast('posted'); location.hash = `#/questions/${r.id}`; } catch (e) { fail(e); } }
    $('#title').focus();
  });

  route(/^\/questions\/(\d+)$/, async (id) => {
    setTab('questions');
    const q = await GET(`/api/questions/${id}?view=1`);
    const known = new Set((await GET('/api/wiki')).map((x) => x.slug));
    const isAsker = q.author === who();
    const post = (kind, p) => `<div class="post ${p.accepted ? 'accepted' : ''}" data-kind="${kind}" data-id="${p.id}">
      <div class="votes"><button class="up ${p.my_vote > 0 ? 'on' : ''}" title="upvote">▲</button><div class="score">${p.score}</div><button class="down ${p.my_vote < 0 ? 'on' : ''}" title="downvote">▼</button>
        ${kind === 'answer' ? `<button class="accept ${p.accepted ? 'on' : ''}" title="${isAsker || p.accepted ? 'accept this answer' : 'only the asker accepts'}">✔</button>` : ''}</div>
      <div class="body"><div class="md">${MD.render(p.body)}</div>
        <div class="foot"><div class="actions"><button class="btn sm ghost act-comment">comment</button>${p.author === who() ? '<button class="btn sm ghost act-edit">edit</button>' : ''}</div><div class="ts">${kind === 'answer' ? 'answered' : 'asked'} ${ago(p.created_at)} by ${chip(p.author)}${p.updated_at !== p.created_at ? ` · edited ${ago(p.updated_at)}` : ''}</div></div>
        ${p.comments.length ? `<div class="comments">${p.comments.map((c) => `<div class="c"><div>${MD.inline(c.body)} — <span class="mono">${esc(c.author)}</span> <span class="ts">${ago(c.created_at)}${c.updated_at ? ` · edited ${ago(c.updated_at)}` : ''}</span></div>${c.author === who() ? `<div class="comment-actions"><button class="btn sm ghost" data-question-comment-edit="${c.id}">edit</button><button class="btn sm ghost" data-question-comment-delete="${c.id}">delete</button></div>` : ''}</div>`).join('')}</div>` : ''}
        <div class="cform hidden inline-form"><input placeholder="add a comment…"><button class="btn sm">post</button></div>
      </div></div>`;
    paint(`
      <div class="pagehead"><div><h1 style="text-transform:none;font-family:var(--sans);font-size:1.6rem">${esc(q.title)}</h1><div class="sub">${badge(q.status)} · asked ${ago(q.created_at)} · viewed ${q.views}× · ${q.answers.length} answers ${tags(q.tags, '#/questions')}</div></div>
        <div class="actions">${q.status !== 'closed' ? '<button class="btn" id="close">Close</button>' : '<button class="btn" id="reopen">Reopen</button>'}</div></div>
      <div class="card">${post('question', q)}</div>
      <h2>${q.answers.length} Answer${q.answers.length === 1 ? '' : 's'}</h2>
      <div class="card">${q.answers.map((a) => post('answer', a)).join('') || '<div class="empty">no answers yet — be the first</div>'}</div>
      ${q.status !== 'closed' ? `<div class="card"><h3>Your answer</h3><div id="aed"></div><div class="form-actions" style="margin-top:.6rem"><button class="btn ok" id="post">Post answer as ${esc(who())}</button></div></div>` : ''}`);
    wireWikilinks(app, known);
    const vote = async (kind, tid, cur, val) => { try { await api('POST', '/api/questions/vote', { target_type: kind, target_id: +tid, value: cur === val ? 0 : val }); render(); } catch (e) { fail(e); } };
    app.querySelectorAll('.post').forEach((el) => {
      const kind = el.dataset.kind, tid = el.dataset.id;
      const postData = kind === 'question' ? q : q.answers.find((a) => a.id === +tid);
      const cur = kind === 'question' ? q.my_vote : q.answers.find((a) => a.id === +tid).my_vote;
      el.querySelector('.up').onclick = () => vote(kind, tid, cur, 1);
      el.querySelector('.down').onclick = () => vote(kind, tid, cur, -1);
      const acc = el.querySelector('.accept');
      if (acc) acc.onclick = async () => { const on = acc.classList.contains('on'); try { await api('POST', `/api/questions/${id}/accept`, { answer_id: on ? null : +tid }); render(); } catch (e) { fail(e); } };
      const cf = el.querySelector('.cform');
      el.querySelector('.act-comment').onclick = () => { cf.classList.toggle('hidden'); cf.querySelector('input').focus(); };
      const postC = async () => { const body = cf.querySelector('input').value; if (!body.trim()) return; try { await api('POST', '/api/questions/comments', { target_type: kind, target_id: +tid, body }); render(); } catch (e) { fail(e); } };
      cf.querySelector('button').onclick = postC; cf.querySelector('input').onkeydown = (e) => { if (e.key === 'Enter') postC(); };
      el.querySelectorAll('[data-question-comment-edit]').forEach((button) => {
        button.onclick = () => {
          const cid = Number(button.dataset.questionCommentEdit);
          const comment = postData.comments.find((c) => c.id === cid);
          const row = button.closest('.c');
          row.innerHTML = '<textarea class="comment-edit-input" rows="3" aria-label="Edit comment"></textarea><div class="comment-actions"><button class="btn sm" data-comment-save>save</button><button class="btn sm ghost" data-comment-cancel>cancel</button></div>';
          const input = row.querySelector('textarea'); input.value = comment.body; input.focus();
          row.querySelector('[data-comment-cancel]').onclick = () => render();
          row.querySelector('[data-comment-save]').onclick = async () => {
            try { await api('PATCH', `/api/questions/comments/${cid}`, { body: input.value }); toast('comment updated'); render(); }
            catch (e) { fail(e); }
          };
        };
      });
      el.querySelectorAll('[data-question-comment-delete]').forEach((button) => {
        button.onclick = async () => {
          if (!confirm('Delete this comment?')) return;
          try { await api('DELETE', `/api/questions/comments/${button.dataset.questionCommentDelete}`); toast('comment deleted'); render(); }
          catch (e) { fail(e); }
        };
      });
      const ed = el.querySelector('.act-edit');
      if (ed) ed.onclick = () => {
        const cur_ = kind === 'question' ? q.body : q.answers.find((a) => a.id === +tid).body;
        const body = el.querySelector('.body');
        body.innerHTML = '<div class="ie"></div><div class="form-actions" style="margin-top:.6rem"><button class="btn ie-c">Cancel</button><button class="btn ok ie-s">Save</button></div>';
        const e2 = new MdEditor(body.querySelector('.ie'), { value: cur_, onSave: saveEdit, minHeight: '240px', knownSlugs: known });
        body.querySelector('.ie-c').onclick = () => render(); body.querySelector('.ie-s').onclick = () => saveEdit(e2.value);
        async function saveEdit(v) { try { await api('PATCH', kind === 'question' ? `/api/questions/${id}` : `/api/answers/${tid}`, { body: v }); toast('saved'); render(); } catch (e) { fail(e); } }
      };
    });
    if ($('#close')) $('#close').onclick = async () => { await api('PATCH', `/api/questions/${id}`, { status: 'closed' }).catch(fail); render(); };
    if ($('#reopen')) $('#reopen').onclick = async () => { await api('PATCH', `/api/questions/${id}`, { status: q.accepted_answer_id ? 'answered' : 'open' }).catch(fail); render(); };
    if ($('#aed')) { const aed = new MdEditor($('#aed'), { onSave: postAnswer, minHeight: '260px', knownSlugs: known, placeholder: 'Answer with evidence: source deep links, wiki [[pages]], what you verified vs. inferred.' }); $('#post').onclick = () => postAnswer(aed.value); }
    async function postAnswer(body) { try { await api('POST', `/api/questions/${id}/answers`, { body }); toast('answer posted'); MdEditor.clearDraft('aed'); render(); } catch (e) { fail(e); } }
  });

  // ------------------------------------------------------------ AGENTS
  route(/^\/agents$/, async () => {
    setTab('agents'); await loadRefs();
    const deptName = (s) => (DEPTS.find((d) => d.slug === s) || {}).name || (s || 'Unassigned');
    const deptDesc = (s) => (DEPTS.find((d) => d.slug === s) || {}).description || '';
    function roster(agents){
      const byDept={};for(const agent of agents)(byDept[agent.department||'']||=[]).push(agent);
      return Object.keys(byDept).sort().map(d=>`<div class="dept-head"><h2>${esc(deptName(d))}</h2><span class="d">${esc(deptDesc(d))}</span></div><div class="agent-grid">${byDept[d].sort((a,b)=>(a.role==='orchestrator'?-1:0)-(b.role==='orchestrator'?-1:0)||a.name.localeCompare(b.name)).map(a=>`<div class="agent-card" data-agent-slug="${esc(a.slug)}"><span class="rep" title="reputation">${a.reputation}</span><h3><a href="#/agents/${encodeURIComponent(a.slug)}">${esc(a.name)}</a></h3>${badge(a.role)} <span class="mono small muted">${esc(a.slug)}</span><p class="small">${esc(a.description)}</p>${a.runtime?`<div class="agent-state">${badge(a.runtime.state)} ${esc(a.runtime.summary)}${a.runtime.ticket_id?` <a href="#/tickets/${a.runtime.ticket_id}">#${a.runtime.ticket_id}</a>`:''}</div>`:'<div class="small muted">Available for assignment</div>'}<div class="cb">${a.codebases?'pkgs: '+esc(a.codebases):''}</div><div class="meta mono small muted">${a.open_tickets} open · ${a.answers} answers · ${a.wiki_pages_written} pages · seen ${ago(a.last_seen)||'never'}</div></div>`).join('')}</div>`).join('')||'<div class="empty">The orchestrator registers automatically when the runner starts. Teammates are reused or registered as jobs are delegated.</div>';
    }
    paint(`<div class="pagehead"><div><h1>Agents</h1><div class="sub" id="agent-count">${AGENTS.length} registered agents · shared identities and reusable capabilities</div></div><div class="actions"><a class="btn" href="#/teams">Team rooms</a><a class="btn" href="#/skills">Shared skills</a><a class="btn ok" href="#/agents/new">+ Agent</a></div></div><div class="card" id="agents-progress"></div><div id="agent-roster">${roster(AGENTS)}</div>`);
    await mountProgress('agents-progress');
    poll(async()=>{const agents=await GET('/api/agents');AGENTS=agents;const box=$('#agent-roster');if(box&&!hasSelection(box))box.innerHTML=roster(agents);if($('#agent-count'))$('#agent-count').textContent=`${agents.length} registered agents · shared identities and reusable capabilities`;},3000);
  });

  route(/^\/teams$/,async()=>{
    setTab('agents');const jobs=await GET('/api/teams');
    paint(`<div class="pagehead"><h1>Team rooms</h1><a class="btn" href="#/agents">Agents</a></div><div class="card" id="teams-progress"></div><div class="card"><div class="list">${jobs.map(j=>`<div class="item"><div class="main"><a class="title" href="#/teams/${j.id}">#${j.id} ${esc(j.title)}</a><div>${badge(j.status)} · ${j.message_count} messages · ${j.pending_count} awaiting responses</div></div><a class="btn sm" href="#/tickets/${j.id}">Job ticket</a></div>`).join('')||'<div class="empty">Create a ready job ticket to start a collaborative team.</div>'}</div></div>`);
    await mountProgress('teams-progress');
  });
  route(/^\/teams\/(\d+)$/,async id=>{
    setTab('agents');const job=await GET('/api/tickets/'+id);await loadRefs();
    paint(`<div class="pagehead"><div><h1>${esc(job.title)}</h1><div class="sub">Shared conversation and current-revision peer reviews</div></div><a class="btn" href="#/tickets/${id}">Job #${id}</a></div><div class="card" id="team-progress"></div><div class="card" id="team-room"></div>`);
    await mountProgress('team-progress','?ticket='+id);await mountTeam('team-room',id);
  });

  async function agentForm(slug){
    setTab('agents');const agent=slug?await GET('/api/agents/'+encodeURIComponent(slug)):{};
    paint(`<div class="pagehead"><h1>${slug?'Edit agent':'Register agent'}</h1></div><form id="agent-form" class="card form"><div class="grid-3"><label>Identity<input id="agent-slug" required pattern="[a-z0-9][a-z0-9._-]{0,79}" value="${esc(agent.slug||'')}"${slug?' readonly':''}></label><label>Name<input id="agent-name" required value="${esc(agent.name||'')}"></label><label>Role<select id="agent-role">${opts(['orchestrator','lead','expert','generalist','human'],agent.role||'expert')}</select></label></div><label>Capabilities<textarea id="agent-description" rows="3">${esc(agent.description||'')}</textarea></label><label>Reusable agent instructions<textarea id="agent-instructions" rows="6">${esc(agent.instructions||'')}</textarea></label><div class="form-actions"><a class="btn" href="#/agents">Cancel</a><button class="btn ok">Save agent</button></div></form>`);
    $('#agent-form').onsubmit=async e=>{e.preventDefault();try{const saved=await api('POST','/api/agents',{slug:$('#agent-slug').value.trim(),name:$('#agent-name').value.trim(),role:$('#agent-role').value,description:$('#agent-description').value,instructions:$('#agent-instructions').value});location.hash='#/agents/'+encodeURIComponent(saved.slug);}catch(err){fail(err);}};
  }
  route(/^\/agents\/new$/,()=>agentForm());
  route(/^\/agents\/([^/]+)\/edit$/,slug=>agentForm(slug));

  route(/^\/skills$/,async()=>{
    setTab('agents');const skills=await GET('/api/skills');
    paint(`<div class="pagehead"><div><h1>Shared skills</h1><div class="sub">Reusable procedures available to every agent. Agents can publish new skills as they learn.</div></div><div class="actions"><a class="btn" href="#/agents">Agents</a><a class="btn ok" href="#/skills/new">+ Skill</a></div></div><div class="card"><div class="list">${skills.map(s=>`<div class="item"><div class="main"><a class="title" href="#/skills/${encodeURIComponent(s.slug)}">${esc(s.title)}</a><div class="meta"><span class="mono">${esc(s.slug)} · v${s.version}</span><span>updated ${ago(s.updated_at)} by ${chip(s.updated_by)}</span></div></div></div>`).join('')||'<div class="empty">No shared skills yet.</div>'}</div></div>`);
  });
  async function skillPage(slug){
    setTab('agents');const skill=slug?await GET('/api/skills/'+encodeURIComponent(slug)):{};
    paint(`<div class="pagehead"><div><h1>${slug?esc(skill.title):'New shared skill'}</h1><div class="sub">${slug?`v${skill.version} · updated by ${esc(skill.updated_by)}`:'A procedure all agents can read and use'}</div></div><a class="btn" href="#/skills">Shared skills</a></div>${slug?`<div class="card md">${MD.render(skill.body)}</div>`:''}<${slug?'details':'div'} class="card"${slug?'':' open'}>${slug?'<summary>Edit skill</summary>':''}<form id="skill-form" class="form"><div class="grid-2"><label>Slug<input id="skill-slug" required pattern="[a-z0-9][a-z0-9-]{0,79}" value="${esc(skill.slug||'')}"${slug?' readonly':''}></label><label>Title<input id="skill-title" required value="${esc(skill.title||'')}"></label></div><label>Instructions (Markdown)<textarea id="skill-body" required rows="12">${esc(skill.body||'')}</textarea></label><button class="btn ok">Save shared skill</button><p class="small muted">Updates require the current version to protect teammates' changes.</p></form></${slug?'details':'div'}>`);
    $('#skill-form').onsubmit=async e=>{e.preventDefault();try{const saved=await api('POST','/api/skills',{slug:$('#skill-slug').value.trim(),title:$('#skill-title').value.trim(),body:$('#skill-body').value,version:skill.version||0});if(slug)render();else location.hash='#/skills/'+encodeURIComponent(saved.slug);}catch(err){fail(err);}};
  }
  route(/^\/skills\/new$/,()=>skillPage());
  route(/^\/skills\/([^/]+)$/,slug=>skillPage(slug));

  route(/^\/agents\/([^/]+)$/, async (slug) => {
    setTab('agents');
    const a = await GET(`/api/agents/${encodeURIComponent(slug)}`);
    const inbox = await GET(`/api/agents/${encodeURIComponent(slug)}/inbox`);
    const pages = (await GET('/api/wiki')).filter((p) => p.author === slug || p.updated_by === slug);
    paint(`<div class="pagehead"><div><h1 style="text-transform:none;font-family:var(--sans)">${esc(a.name)}</h1><div class="sub">${badge(a.role)} <span class="mono">${esc(a.slug)}</span> · dept ${esc(a.department || '—')} · rep <b>${a.reputation}</b> · last seen ${ago(a.last_seen) || 'never'}</div></div>
        <div class="actions"><a class="btn" href="#/agents/${encodeURIComponent(slug)}/edit">Edit agent</a><a class="btn" href="#/skills">Shared skills</a><a class="btn" href="#/tickets/new?assignee=${encodeURIComponent(slug)}&dept=${encodeURIComponent(a.department)}">+ Ticket for ${esc(slug)}</a></div></div><div class="card" id="agent-progress"></div>
      ${layout({ id: 'agent', body: `
        <div class="card"><p>${esc(a.description)}</p>${a.instructions?`<h3>Agent instructions</h3><div class="md">${MD.render(a.instructions)}</div>`:''}<dl class="kv"><dt>codebases</dt><dd>${esc(a.codebases || '—')}</dd><dt>reads first</dt><dd>${esc(a.wiki_pages || '—')}</dd><dt>agent file</dt><dd>${esc(a.agent_file || '—')}</dd></dl></div>
        <div class="card"><h3>Assigned tickets</h3>${a.tickets.length ? `<div class="list">${a.tickets.map(ticketItem).join('')}</div>` : '<span class="muted">none</span>'}</div>
        <div class="card"><h3>Inbox preview (what it sees on wake)</h3><div class="mono small">claimable: ${inbox.claimable_tickets.length} · unanswered Qs in dept: ${inbox.unanswered_questions.length}</div></div>`,
        aside: `
        <div class="card tint-y"><h3>Wiki pages</h3>${pages.map((p) => `<div><a href="#/wiki/${p.slug}">${esc(p.title)}</a></div>`).join('') || '<span class="muted">none yet</span>'}</div>
        <div class="card"><h3>Activity</h3><div class="feed">${a.activity.map(ev).join('') || '<span class="muted">quiet</span>'}</div></div>` })}`);
    await mountProgress('agent-progress','?agent='+encodeURIComponent(slug));
  });

  route(/^\/orchestrator$/, async () => {
    setTab('orchestrator');
    const owner=view;
    await OrchestratorChat.mount({api,GET,paint,poll,scrollState,restoreScroll,hasSelection,ago,fail,
      refresh:render,esc,sessionId:qs().session,signal:owner.controller.signal,
      onCleanup:callback=>owner.cleanup.push(callback)});
  });

  route(/^\/debug$/, async () => {
    setTab('debug'); const q=qs();
    const path=`/api/runner/debug${q.ticket?`?ticket=${encodeURIComponent(q.ticket)}`:''}`;
    const data=await GET(path);
    const messageView=m=>`<div class="debug-message ${m.kind==='acp-response'?'response':''}"><div><b>${esc(m.role)} · ${esc(m.kind)}</b><span class="ts">${esc(m.updated_at||m.created_at)}</span></div><pre>${esc(m.body||'')}</pre></div>`;
    const compact=messages=>{const out=[];for(const message of messages){const last=out[out.length-1];if(last&&last.kind===message.kind&&['acp-response','review-decision'].includes(message.kind)){last.body+=message.body;last.updated_at=message.updated_at||message.created_at;}else out.push({...message});}return out;};
    const content=data=>`<div class="debug-runs">${data.runs.map(r=>`<details class="card debug-run" data-run-id="${r.id}"><summary><b>Run #${r.id} · Ticket #${r.ticket_id} ${esc(r.ticket_title)}</b><span>${badge(r.status)} ${badge(r.priority)} · ${chip(r.agent_slug)} · ${esc(r.backend)} · started ${esc(r.started_at||'—')}</span></summary><div class="debug-run-actions"><a href="#/tickets/${r.ticket_id}">Open ticket</a>${r.session_id?`<span class="mono small">ACP session ${esc(r.session_id)}</span>`:''}</div>${r.error?`<div class="tint-p">${esc(r.error)}</div>`:''}${r.reviews.map(x=>`<div class="debug-review"><b>Orchestrator review: ${esc(x.decision||x.status)}</b> · ${esc(x.backend)}<p>${esc(x.reason)}</p></div>`).join('')}${r.messages_truncated?`<p class="small muted">Showing the most recent visible events from ${r.message_count} stored entries.</p>`:''}${compact(r.messages).map(messageView).join('')||'<div class="empty">No stored messages for this run.</div>'}</details>`).join('')||'<div class="empty">No agent runs recorded yet.</div>'}</div>
      ${data.reviews.length?`<div class="card"><h2>Orchestrator review history</h2>${data.reviews.map(x=>`<div class="debug-review"><a href="#/tickets/${x.ticket_id}">Ticket #${x.ticket_id} ${esc(x.ticket_title)}</a> · ${badge(x.status)}${x.decision?` · ${esc(x.decision)}`:''}<p>${esc(x.reason||'Review in progress')}</p><small class="ts">${esc(x.created_at)}${x.finished_at?` · finished ${esc(x.finished_at)}`:''}</small></div>`).join('')}</div>`:''}
      ${data.unattached_messages.length?`<details class="card debug-run" data-run-id="chat"><summary>Ticket chat and standalone review logs (${data.unattached_messages.length})</summary>${compact(data.unattached_messages).map(messageView).join('')}</details>`:''}`;
    paint(`<div class="pagehead"><div><h1>Agent debug</h1><div class="sub">Live transcripts, tool events and review decisions${q.ticket?` · ticket #${esc(q.ticket)}`:''}</div></div>${q.ticket?`<a class="btn" href="#/tickets/${encodeURIComponent(q.ticket)}">Back to ticket</a>`:''}</div><div class="card" id="debug-progress"></div><div id="debug-content">${content(data)}</div>`);
    await mountProgress('debug-progress',q.ticket?'?ticket='+encodeURIComponent(q.ticket):'');
    poll(async()=>{const next=await GET(path),box=$('#debug-content');if(!box||hasSelection(box))return;const opened=new Set([...box.querySelectorAll('details[open]')].map(x=>x.dataset.runId));const top=window.scrollY;box.innerHTML=content(next);for(const detail of box.querySelectorAll('details'))detail.open=opened.has(detail.dataset.runId);window.scrollTo(window.scrollX,top);},2000);
  });

  route(/^\/runner$/, async () => {
    setTab('runner');
    const [s, templates, tickets] = await Promise.all([GET('/api/runner/settings'), GET('/api/runner/templates'), GET('/api/tickets?all=1')]);
    paint(`<div class="pagehead"><div><h1>Agent runner</h1><div class="sub">ACP backend controls, execution templates, active work and ticket transcripts</div></div></div>
      <div class="card" id="runner-progress"></div><div class="layout"><div class="layout-body">
        <div class="card"><h2>Backend settings</h2><div class="grid-3">
          <label>Active backend<select id="rb">${['opencode','codex','claude'].map(x=>`<option value="${x}"${s.active_backend===x?' selected':''}>${x}${(s.backends.find(b=>b.id===x)||{}).available?' · detected':''}</option>`).join('')}</select></label>
          <label>Model<div style="display:flex;gap:.35rem"><select id="runner-model" style="min-width:0;flex:1"><option value="">Loading ACP models…</option></select><button class="btn sm" id="refresh-models" type="button" title="Refresh available models">↻</button></div><small id="model-note" class="muted" aria-live="polite">Model list comes from the selected backend.</small></label>
          <label>Parallel backend agents<input id="rp" type="number" min="1" max="16" value="${esc(s.parallel_agents)}"></label>
          <label>Workspace path<input id="rw" value="${esc(s.workspace)}"><small class="muted">A separate, existing directory for agent work.</small>${s.workspace_error?`<small class="error">${esc(s.workspace_error)}</small>`:''}</label></div>
          <div class="grid-3">${['opencode','codex','claude'].map(x=>`<label>${x} command (optional)<input id="cmd-${x}" value="${esc(s[x+'_command']||'')}" placeholder="${x==='opencode'?'opencode acp':x==='codex'?'codex-acp':'claude-code-acp'}"></label>`).join('')}</div>
          <div class="form-actions"><button class="btn primary" id="save-runner">Save settings</button></div><div class="small muted">Commands run in the configured workspace. The runner logs ACP output and permissions on each ticket.</div></div>
        <div class="card"><h2>OpenCode LLM API</h2><div class="grid-3">
          <label>OpenAI-compatible API base URL<input id="opencode-api-url" type="url" value="${esc(s.opencode_api_base_url||'http://localhost:11434/v1')}" placeholder="http://localhost:11434/v1"><small class="muted">The runner adds /v1 when it is missing.</small></label>
          <label>API key<input id="opencode-api-key" type="password" autocomplete="new-password" placeholder="${s.opencode_api_key_set?'Saved; leave blank to keep':'Enter API key'}"><small class="muted">${s.opencode_api_key_set?'A key is saved.':'No key is saved.'} It is not shown after saving.</small></label>
          <label>Model ID override<input id="opencode-api-model" value="${esc(s.opencode_api_model||'')}" placeholder="Auto-discover from /v1/models"><small class="muted">Use this if the API does not list models automatically.</small></label>
        </div><label class="small muted"><input id="clear-opencode-api-key" type="checkbox"> Clear the saved API key</label><div class="small muted">OpenCode uses this endpoint and discovers model IDs from its /models route. Save settings to refresh its ACP connection.</div></div>
        <div class="card"><h2>Agent templates</h2><div class="list">${templates.map(t=>`<div class="item"><div class="main"><b>${esc(t.name)}</b> ${t.backend?badge(t.backend):'<span class="muted">active backend</span>'}<div class="small muted">${esc(t.prompt)}</div></div></div>`).join('')||'<div class="empty">No templates yet.</div>'}</div>
          <form id="template-form" class="form"><div class="grid-3"><label>Name<input id="tn" required></label><label>Backend override<select id="tb"><option value="">Active backend</option><option>opencode</option><option>codex</option><option>claude</option></select></label><label>Enabled<select id="te"><option value="1">yes</option><option value="0">no</option></select></label></div><label>Instructions<textarea id="tp" rows="4" placeholder="Specialist role and working instructions…"></textarea></label><button class="btn ok">Save template</button></form></div>
        <div class="card"><h2>Execution queue</h2><div class="list">${tickets.filter(t=>t.ready_for_execution).map(t=>`<div class="item"><div class="main"><a class="title" href="#/tickets/${t.id}">#${t.id} ${esc(t.title)}</a><div class="meta">${badge(t.status)}${badge(t.priority)} · ${t.assignee?`assignee ${chip(t.assignee)}`:'unassigned'} · ${t.tags.toLowerCase().includes('human')?'human tagged':'ready'}</div></div></div>`).join('')||'<div class="empty">No tickets marked ready.</div>'}</div></div>
      </div><aside class="layout-aside"><div class="card tint-y"><h3>Safety gates</h3><p>Only tickets with <b>Ready for agent execution</b> enabled enter the queue. Tickets assigned to a registered human, with an assignee beginning <code>human</code>, or tagged <code>human</code> are skipped.</p><p>Priority order: p0 → p1 → p2 → p3.</p></div><div class="card"><h3>Monitoring</h3><p>Open a ticket’s <b>Agent runner chat</b> to view live transcript, debug logs and run status.</p></div></aside></div>`);
    let modelRequest=0,modelController=null,loadedModelBackend='';
    const loadRunnerModels=async()=>{const request=++modelRequest;const backend=$('#rb').value,select=$('#runner-model'),note=$('#model-note');if(modelController)modelController.abort();modelController=new AbortController();const controller=modelController,timeout=setTimeout(()=>controller.abort(),90000);loadedModelBackend='';select.disabled=false;select.innerHTML='<option value="">Use ACP default</option>';note.innerHTML=`<span class="model-spinner" aria-hidden="true"></span> Connecting to ${esc(backend)} ACP… You can save settings while discovery runs.`;try{const response=await fetch(`/api/runner/models?backend=${encodeURIComponent(backend)}`,{headers:{'Content-Type':'application/json','X-Coms-Agent':who()},signal:controller.signal});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||response.statusText);if(request!==modelRequest||!select.isConnected)return;select.innerHTML='<option value="">Use ACP default</option>'+data.models.map(m=>`<option value="${esc(m.value)}">${esc(m.name||m.value)}</option>`).join('');const desired=s['model_'+backend]||data.current||'';if(data.models.some(m=>m.value===desired))select.value=desired;loadedModelBackend=backend;note.textContent=data.models.length?`${data.models.length} model${data.models.length===1?'':'s'} available from ${backend}.`:backend==='opencode'?'No OpenCode models were advertised. Check the API settings or enter a model ID override.':`This backend did not advertise a model selector; it will use its configured default.`;}catch(e){if(request!==modelRequest||!select.isConnected)return;note.textContent=`${e.name==='AbortError'?`${backend} model discovery timed out after 90 seconds`:`Could not load ${backend} models: ${e.message||'check backend command and authentication'}`}. You can still save this backend and use its default model.`;}finally{clearTimeout(timeout);}};
    $('#rb').onchange=loadRunnerModels;$('#refresh-models').onclick=loadRunnerModels;loadRunnerModels();
    $('#save-runner').onclick=async()=>{const payload={active_backend:$('#rb').value,parallel_agents:+$('#rp').value,workspace:$('#rw').value,opencode_api_base_url:$('#opencode-api-url').value.trim(),opencode_api_model:$('#opencode-api-model').value.trim(),clear_opencode_api_key:$('#clear-opencode-api-key').checked};const apiKey=$('#opencode-api-key').value;if(apiKey&&!payload.clear_opencode_api_key)payload.opencode_api_key=apiKey;for(const x of ['opencode','codex','claude']) payload[x+'_command']=$('#cmd-'+x).value.trim();if(loadedModelBackend===payload.active_backend)payload['model_'+payload.active_backend]=$('#runner-model').value; try{await api('PUT','/api/runner/settings',payload);toast('runner settings saved');render();}catch(e){fail(e);}};
    $('#template-form').onsubmit=async e=>{e.preventDefault();try{await api('POST','/api/runner/templates',{name:$('#tn').value,prompt:$('#tp').value,backend:$('#tb').value,enabled:$('#te').value==='1'});toast('template saved');render();}catch(err){fail(err);}};
    await mountProgress('runner-progress');
  });

  // ------------------------------------------------------------ SEARCH / ACTIVITY
  route(/^\/search$/, async () => {
    setTab('');
    const q = qs().q || '';
    $('#q').value = q;
    const r = q ? await GET(`/api/search?q=${encodeURIComponent(q)}&limit=25`) : { wiki: [], tickets: [], questions: [] };
    paint(`<div class="pagehead"><h1>Search</h1><div class="sub mono">“${esc(q)}” · ${r.wiki.length + r.tickets.length + r.questions.length} hits</div></div>
      <div class="results row"><div class="col"><h3>Wiki (${r.wiki.length})</h3>${r.wiki.map((w) => `<div class="card flat"><a class="title" href="#/wiki/${w.slug}"><b>${esc(w.title)}</b></a><div class="snip">${MD.esc(w.snippet).replace(/\[([^\]]+)\]/g, '<b>$1</b>')}</div><div class="ts">${esc(w.slug)} · ${ago(w.updated_at)}</div></div>`).join('') || '<div class="empty">—</div>'}</div>
      <div class="col"><h3>Tickets (${r.tickets.length})</h3>${r.tickets.map((t) => `<div class="card flat"><a href="#/tickets/${t.id}"><b>#${t.id} ${esc(t.title)}</b></a><div>${badge(t.status)}${badge(t.priority)} ${chip(t.assignee)}</div></div>`).join('') || '<div class="empty">—</div>'}</div>
      <div class="col"><h3>Questions (${r.questions.length})</h3>${r.questions.map((b) => `<div class="card flat"><a href="#/questions/${b.question_id}"><b>${esc(b.title)}</b></a><div class="snip">${MD.esc(b.snippet).replace(/\[([^\]]+)\]/g, '<b>$1</b>')}</div><div>${badge(b.status)} score ${b.score} · ${chip(b.author)}</div></div>`).join('') || '<div class="empty">—</div>'}</div></div>`);
  });

  route(/^\/activity$/, async () => {
    setTab('activity');
    const q = qs();
    const list = await GET(`/api/activity?limit=200${q.actor ? `&actor=${encodeURIComponent(q.actor)}` : ''}`);
    paint(`<div class="pagehead"><div><h1>Activity</h1><div class="sub">${q.actor ? `actor: ${esc(q.actor)}` : 'everything, newest first'}</div></div></div>
      <div class="card" id="activity-progress"></div><div class="card"><div class="feed" id="activity-feed">${list.map(ev).join('') || '<span class="muted">quiet</span>'}</div></div>`);
    await mountProgress('activity-progress');
    poll(async()=>{const events=await GET(`/api/activity?limit=200${q.actor?`&actor=${encodeURIComponent(q.actor)}`:''}`);const feed=$('#activity-feed');if(feed&&!hasSelection(feed))feed.innerHTML=events.map(ev).join('')||'<span class="muted">quiet</span>';},2000);
  });

  render();
})();

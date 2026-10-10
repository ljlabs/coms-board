/* app.js — COMS BOARD single-page app (hash routing, no framework). */
(function () {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const app = $('#app');
  const esc = MD.esc;
  const who = () => ($('#who').value || 'admin').trim();
  $('#who').value = localStorage.getItem('coms.who') || 'admin';
  $('#who').addEventListener('change', () => localStorage.setItem('coms.who', who()));

  // ------------------------------------------------------------ api
  async function api(method, path, body) {
    const res = await fetch(path, {
      method, headers: { 'Content-Type': 'application/json', 'X-Coms-Agent': who() },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }
  const GET = (p) => api('GET', p);

  function toast(msg, err) {
    const t = document.createElement('div'); t.className = 't' + (err ? ' err' : ''); t.textContent = msg;
    $('#toast').appendChild(t); setTimeout(() => t.remove(), err ? 6000 : 2500);
  }
  const fail = (e) => { console.error(e); toast(e.message || String(e), true); };

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
    app.classList.add('wide');
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
    try { [AGENTS, DEPTS] = await Promise.all([GET('/api/agents'), GET('/api/departments')]); } catch (e) { /* ignore */ }
  }
  const agentOpts = (cur) => `<option value="">— nobody —</option>` + AGENTS.map((a) => `<option value="${a.slug}"${a.slug === cur ? ' selected' : ''}>${esc(a.slug)}</option>`).join('');
  const deptOpts = (cur) => `<option value="">— none —</option>` + DEPTS.map((d) => `<option value="${d.slug}"${d.slug === cur ? ' selected' : ''}>${esc(d.name)}</option>`).join('');

  // ------------------------------------------------------------ router
  const routes = [];
  const route = (re, fn) => routes.push([re, fn]);
  async function render() {
    const hash = location.hash || '#/';
    const path = hash.slice(1).split('?')[0];
    for (const [re, fn] of routes) {
      const m = re.exec(path);
      if (m) {
        app.innerHTML = '<div class="empty">loading…</div>';
        app.classList.remove('wide');   // routes opt back in (wiki read view does)
        try { await fn(...m.slice(1).map((x) => x && decodeURIComponent(x))); } catch (e) { app.innerHTML = `<div class="card tint-p"><h2>Error</h2><p class="mono">${esc(e.message)}</p></div>`; console.error(e); }
        // one central place to turn rendered ```mermaid blocks into diagrams
        if (window.MMD && MMD.has(app)) MMD.upgrade(app).catch(() => {});
        // …and to pretty-print single-line code blocks (see pretty.js)
        if (window.PRETTY && PRETTY.has(app)) PRETTY.upgrade(app).catch(() => {});
        window.scrollTo(0, 0);
        return;
      }
    }
    app.innerHTML = '<div class="empty">404 — no such page</div>';
  }
  window.addEventListener('hashchange', render);

  // ------------------------------------------------------------ HOME
  route(/^\/$/, async () => {
    setTab('home');
    const o = await GET('/api/overview');
    const [qs_, tix] = await Promise.all([GET('/api/questions?unanswered=1'), GET('/api/tickets?unassigned=1')]);
    app.innerHTML = `
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
          <div class="card"><h3>Recent activity</h3><div class="feed">${o.recent_activity.map(ev).join('') || '<span class="muted">quiet</span>'}</div><p><a href="#/activity" class="small mono">full feed →</a></p></div>` })}`;
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
    app.innerHTML = `
      <div class="pagehead"><div><h1>Wiki</h1><div class="sub">long-term agent notes — how-tos, gotchas, verified procedures${inFolder ? ` · <span class="mono">${folderCrumb(q.folder, q.folder)}</span>` : ''}</div></div>
        <div class="actions"><input id="wf" class="wiki-filter mono" placeholder="Filter pages">${asideToggleBtn()}<a class="btn primary" href="#/wiki/new${inFolder && q.folder ? `?folder=${encodeURIComponent(q.folder)}` : ''}">+ New page</a></div></div>
      ${layout({ id: 'wikiindex', nav: folderTree(tree, inFolder ? q.folder : undefined), body: table, aside })}`;
    wireAside('wikiindex', 'coms.wikiside');
    $('#wf').addEventListener('input', (e) => { const v = e.target.value.toLowerCase(); document.querySelectorAll('#wt tbody tr').forEach((tr) => tr.classList.toggle('hidden', !(tr.dataset.k || '').includes(v))); });
  });

  route(/^\/wiki\/new$/, async () => { setTab('wiki'); await loadRefs(); wikiEditor(null); });
  route(/^\/wiki\/([^/]+)\/edit$/, async (slug) => { setTab('wiki'); await loadRefs(); wikiEditor(await GET(`/api/wiki/${encodeURIComponent(slug)}`)); });
  route(/^\/wiki\/([^/]+)\/history$/, async (slug) => {
    setTab('wiki');
    const [p, revs] = await Promise.all([GET(`/api/wiki/${encodeURIComponent(slug)}`), GET(`/api/wiki/${encodeURIComponent(slug)}/revisions`)]);
    app.innerHTML = `<div class="pagehead"><div><h1>History</h1><div class="sub"><a href="#/wiki/${p.slug}">${esc(p.title)}</a></div></div></div>
      <table class="grid"><thead><tr><th>Rev</th><th>Saved</th><th>By</th><th>Title</th><th>Size</th><th></th></tr></thead><tbody>
      <tr><td class="mono">current</td><td class="ts">${esc(p.updated_at)}</td><td>${chip(p.updated_by)}</td><td>${esc(p.title)}</td><td class="mono">${p.body.length}</td><td></td></tr>
      ${revs.map((r) => `<tr><td class="mono">#${r.id}</td><td class="ts">${esc(r.created_at)}</td><td>${chip(r.author)}</td><td>${esc(r.title)}</td><td class="mono">${r.size}</td><td><button class="btn sm" data-rev="${r.id}">view</button> <button class="btn sm pink" data-restore="${r.id}">restore</button></td></tr>`).join('')}
      </tbody></table><div id="revview"></div>`;
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
      app.innerHTML = `<div class="card tint-p"><h2>No page “${esc(slug)}”</h2><p>This page does not exist yet.</p><a class="btn primary" href="#/wiki/new?slug=${encodeURIComponent(slug)}">Create it</a></div>`; return;
    }
    const all = await GET('/api/wiki');
    const known = new Set(all.map((x) => x.slug));
    const toc = MD.outline(p.body).filter((h) => h.level <= 3).map((h) => `<a class="l${h.level}" href="#/wiki/${p.slug}#${h.id}" data-anchor="${h.id}">${esc(h.text)}</a>`).join('');
    app.innerHTML = `
      <div class="pagehead"><div><h1>${esc(p.title)}</h1><div class="sub"><span class="mono">${folderCrumb(p.folder, p.folder || '')}</span> · ${esc(p.slug)} · updated ${ago(p.updated_at)} by ${esc(p.updated_by)} · created ${p.created_at.slice(0, 10)} by ${esc(p.author)}</div></div>
        <div class="actions">${asideToggleBtn()}<a class="btn" href="#/wiki/${p.slug}/history">History (${p.revision_count})</a><a class="btn primary" href="#/wiki/${p.slug}/edit">Edit</a><button class="btn danger" id="del">Delete</button></div></div>
      ${layout({ id: 'wikiread',
        body: `<div class="card"><div class="md" id="body">${MD.render(p.body)}</div></div>`,
        aside: `<div class="card"><h3>Meta</h3><dl class="kv"><dt>folder</dt><dd><span class="mono small">${p.folder ? esc(p.folder) : '<span class="muted">(unfiled)</span>'}</span> <button class="btn sm ghost" id="mv" title="move to another folder">move</button></dd><dt>tags</dt><dd>${tags(p.tags, '#/wiki') || '—'}</dd><dt>dept</dt><dd>${esc(p.department || '—')}</dd><dt>author</dt><dd>${chip(p.author)}</dd></dl></div>
          ${toc ? `<div class="card"><h3>Contents</h3><div class="toc">${toc}</div></div>` : ''}
          <div class="card tint-c"><h3>Backlinks</h3>${p.backlinks.map((b) => `<div><a href="#/wiki/${b.slug}">${esc(b.title)}</a></div>`).join('') || '<span class="muted small">none link here yet</span>'}</div>` })}`;
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
    app.innerHTML = `
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
      <div id="ed"></div>`;
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
    app.innerHTML = `
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
      ${view === 'board' ? kanban(tix) : (tix.length ? `<div class="list">${tix.map(ticketItem).join('')}</div>` : '<div class="empty">no tickets match</div>')}`;
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
    wireKanban(tix);
  });
  function kanban(tix) {
    const cols = [['open', 'Open'], ['in_progress', 'In progress'], ['blocked', 'Blocked'], ['review', 'Review'], ['done', 'Done'], ['wontfix', 'Won’t fix']];
    return `<div class="kanban">${cols.map(([s, l]) => { const items = tix.filter((t) => t.status === s).slice(0, ['done', 'wontfix'].includes(s) ? 15 : 100); return `<div class="colm" data-status="${s}" aria-label="${l} tickets"><h3>${l}<span>${items.length}</span></h3>${items.map((t) => `<div class="kcard" draggable="true" data-ticket-id="${t.id}" aria-label="Ticket #${t.id}: ${esc(t.title)}. Drag to a status column to move it"><a href="#/tickets/${t.id}">#${t.id} ${esc(t.title)}</a><div class="m">${badge(t.priority)}${badge(t.type)}${t.parent_id ? `<a class="mono small" href="#/tickets/${t.parent_id}" title="parent ticket">↑ #${t.parent_id}</a>` : ''}${t.assignee ? chip(t.assignee) : '<b>unassigned</b>'}</div></div>`).join('')}</div>`; }).join('')}</div>`;
  }
  function wireKanban(tix) {
    const board = $('.kanban');
    if (!board) return;
    let draggedId = null;
    const clearDropTargets = () => board.querySelectorAll('.colm.drop-target').forEach((col) => col.classList.remove('drop-target'));
    board.addEventListener('dragstart', (e) => {
      const card = e.target.closest('.kcard');
      if (!card || !e.dataTransfer) return;
      draggedId = card.dataset.ticketId;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', draggedId);
      card.classList.add('is-dragging');
    });
    board.addEventListener('dragend', (e) => {
      const card = e.target.closest('.kcard');
      if (card) card.classList.remove('is-dragging');
      draggedId = null;
      clearDropTargets();
    });
    board.addEventListener('dragover', (e) => {
      const col = e.target.closest('.colm');
      if (!col || !draggedId) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      clearDropTargets();
      col.classList.add('drop-target');
    });
    board.addEventListener('dragleave', (e) => {
      const col = e.target.closest('.colm');
      if (col && !col.contains(e.relatedTarget)) col.classList.remove('drop-target');
    });
    board.addEventListener('drop', async (e) => {
      const col = e.target.closest('.colm');
      if (!col || !draggedId) return;
      e.preventDefault();
      clearDropTargets();
      const ticket = tix.find((t) => String(t.id) === draggedId);
      const status = col.dataset.status;
      if (!ticket || ticket.status === status) return;
      try {
        const updated = await api('PATCH', `/api/tickets/${ticket.id}`, { status });
        toast(`ticket #${ticket.id} moved to ${status.replace('_', ' ')}`);
        if (updated.open_children_warning && updated.open_children_warning.length) {
          toast(`⚠ ${updated.open_children_warning.length} open child ticket(s): ${updated.open_children_warning.map((child) => '#' + child.id).join(', ')}`, true);
        }
        await render();
      } catch (err) { fail(err); }
    });
  }

  route(/^\/tickets\/new$/, async () => {
    setTab('tickets'); await loadRefs();
    const q = qs();
    const boards = await GET('/api/ticket-boards');
    const parent = q.parent ? await GET(`/api/tickets/${q.parent}`) : null;
    const board = currentBoard(boards, parent ? parent.board_id : q.board);
    app.innerHTML = `<div class="pagehead"><h1>New ticket</h1></div>
      <div class="card form" style="max-width:1000px">
        ${field('Project board', `<select id="new-board">${boardOpts(boards, board.id)}</select>`)}
        ${field('Title', `<input id="title" placeholder="Research: …">`)}
        <div class="grid-3">${field('Type', `<select id="type">${opts(TT, q.type || 'research')}</select>`)}${field('Priority', `<select id="prio">${opts(PR, 'p2')}</select>`)}${field('Department', `<select id="dept">${deptOpts(q.dept || (DEPTS[0] && DEPTS[0].slug))}</select>`)}</div>
        <div class="grid-2">${field('Assignee', `<select id="assignee">${agentOpts(q.assignee || '')}</select>`)}${field('Tags', `<input id="tags" placeholder="topic, area">`)}</div>
        <div class="grid-2">${field('Parent ticket id (optional) — story &gt; job &gt; task', `<input id="parent" type="number" min="1" value="${esc(q.parent || '')}" placeholder="e.g. 42">`)}</div>
        ${field('Body (markdown) — what to find out, what "done" looks like, where to start', '<div id="ed"></div>')}
        <div class="form-actions"><a class="btn" href="#/tickets?board=${board.id}">Cancel</a><button class="btn ok" id="save">Create</button></div></div>`;
    const ed = new MdEditor($('#ed'), { onSave: save, minHeight: '300px', placeholder: '## Goal\n\n## Definition of done\n\n## Starting points\n- link to source…' });
    $('#save').onclick = () => save(ed.value);
    async function save(body) {
      const payload = { title: $('#title').value, body, type: $('#type').value, priority: $('#prio').value, department: $('#dept').value, assignee: $('#assignee').value, tags: $('#tags').value };
      const p = $('#parent').value.trim(); if (p) payload.parent_id = +p;
      payload.board_id = Number($('#new-board').value);
      try { const t = await api('POST', '/api/tickets', payload); toast(`ticket #${t.id} created`); location.hash = `#/tickets/${t.id}`; } catch (e) { fail(e); }
    }
    $('#title').focus();
  });

  route(/^\/tickets\/(\d+)$/, async (id) => {
    setTab('tickets'); await loadRefs();
    const t = await GET(`/api/tickets/${id}`);
    const boards = await GET('/api/ticket-boards');
    const board = currentBoard(boards, t.board_id); rememberBoard(board.id);
    const known = new Set((await GET('/api/wiki')).map((x) => x.slug));
    app.innerHTML = `
      <div class="crumb mono small"><a href="#/tickets?board=${board.id}">← ${esc(board.name)} board</a></div>
      <div class="pagehead"><div>${t.parent ? `<div class="crumb small mono" style="margin-bottom:.3rem">↑ parent <a href="#/tickets/${t.parent.id}">#${t.parent.id} ${esc(t.parent.title)}</a> ${badge(t.parent.type)}${badge(t.parent.status)}</div>` : ''}<h1>#${t.id} <span id="ttl">${esc(t.title)}</span></h1><div class="sub">opened ${ago(t.created_at)} by ${esc(t.requester || '?')} · updated ${ago(t.updated_at)}${t.closed_at ? ` · closed ${t.closed_at.slice(0, 10)}` : ''}</div></div>
        <div class="actions"><a class="btn" href="#/tickets/${t.id}/tree">🌲 Tree</a>${t.status === 'open' && !t.assignee ? `<button class="btn pink" id="claim">Claim as ${esc(who())}</button>` : ''}<button class="btn" id="editbody">Edit body</button><button class="btn ok" id="editresult">${t.result ? 'Edit result' : 'Write result'}</button></div></div>
      ${layout({ id: 'ticket', body: `
        <div class="card" id="bodycard"><div class="md" id="body">${MD.render(t.body) || '<span class="muted">no description</span>'}</div></div>
        ${t.children && t.children.length ? `<div class="card"><div class="card-title"><h3>Children (${t.children.length})</h3><a class="btn sm" href="#/tickets/new?parent=${t.id}${t.department ? `&dept=${encodeURIComponent(t.department)}` : ''}">+ child</a></div><div class="list">${t.children.map(briefItem).join('')}</div></div>` : ''}
        <div class="card"><div class="card-title"><h3>Links (${(t.links || []).length})</h3></div>
          ${(t.links || []).length ? `<div class="list">${t.links.map((ln) => `<div class="item"><div class="main">
            <a class="title" href="${ln.kind === 'wiki' ? `#/wiki/${encodeURIComponent(ln.ref)}` : ln.kind === 'question' ? `#/questions/${ln.ref}` : ln.kind === 'ticket' ? `#/tickets/${ln.ref}` : esc(ln.ref)}"${ln.kind === 'url' ? ' target="_blank" rel="noopener"' : ''}>${{ wiki: '📖', question: '❓', ticket: '🎫', url: '🔗' }[ln.kind] || ''} ${ln.kind}:${esc(ln.ref)}${ln.title ? ` — ${esc(ln.title)}` : ''}</a>
            <div class="meta">${ln.note ? `<span>${esc(ln.note)}</span>` : ''}<span class="ts">${chip(ln.created_by || '?')}</span><button class="btn sm ghost" data-unlink="${esc(ln.kind)}|${esc(ln.ref)}" title="remove link">×</button></div></div></div>`).join('')}</div>` : '<div class="muted small">no attached context — link the wiki notes and questions this work rests on (<code>coms ticket link</code>)</div>'}
          <div class="actions" style="margin-top:.6rem"><select id="lk-kind"><option value="wiki">wiki</option><option value="question">question</option><option value="ticket">ticket</option><option value="url">url</option></select><input id="lk-ref" placeholder="slug / id / url" style="max-width:200px"><input id="lk-note" placeholder="note (optional)" style="max-width:180px"><button class="btn sm" id="lk-add">+ link</button></div></div>
        <div class="card ${t.result ? 'tint-g' : ''}" id="resultcard"><div class="card-title"><h3>Result</h3></div><div class="md" id="result">${t.result ? MD.render(t.result) : '<span class="muted">nothing delivered yet — the assignee records the outcome here (<code>coms ticket done</code>)</span>'}</div></div>
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
          <label>Title<input id="f-title" value="${esc(t.title)}"></label>
        </div><div class="form-actions" style="margin-top:.8rem"><button class="btn primary" id="savef">Save fields</button></div></div>
        <div class="card tint-y small"><b class="mono">quick moves</b><div class="actions" style="margin-top:.5rem">${TS.filter((s) => s !== t.status).map((s) => `<button class="btn sm" data-mv="${s}">${s.replace('_', ' ')}</button>`).join('')}</div></div>` })}`;
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
    $('#savef').onclick = () => patch({ board_id: Number($('#f-board').value), status: $('#f-status').value, priority: $('#f-priority').value, type: $('#f-type').value, assignee: $('#f-assignee').value, department: $('#f-department').value, tags: $('#f-tags').value, title: $('#f-title').value });
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
    async function comment(body) { if (!body.trim()) return; try { await api('POST', `/api/tickets/${id}/comments`, { body }); toast('comment added'); render(); } catch (e) { fail(e); } }
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
    app.innerHTML = `
      <div class="pagehead"><div><h1>Tree · #${focus}</h1><div class="sub">story &gt; job &gt; task hierarchy · <a href="#/tickets/${focus}">back to ticket</a></div></div></div>
      <div class="card"><div class="tree">${node(data.root, 0)}</div></div>`;
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
    app.innerHTML = `
      <div class="pagehead"><div><h1>Questions</h1><div class="sub">questions from agents · answers from any agent · votes decide</div></div><div class="actions"><a class="btn ok" href="#/questions/ask">+ Ask question</a></div></div>
      <div class="filters"><a class="chip ${!q.status && !q.unanswered ? 'on' : ''}" href="#/questions">all</a><a class="chip ${q.unanswered ? 'on' : ''}" href="${link('unanswered', '1')}">unanswered</a>${['open', 'answered', 'closed'].map((s) => `<a class="chip ${q.status === s ? 'on' : ''}" href="${link('status', s)}">${s}</a>`).join('')}${q.tag ? `<span class="chip on">tag: ${esc(q.tag)}</span> <a class="btn sm ghost" href="${link('tag', undefined)}">×</a>` : ''}<span class="muted mono small">${list.length} questions</span></div>
      ${list.length ? `<div class="list">${list.map(questionItem).join('')}</div>` : '<div class="empty">no questions match</div>'}`;
  });

  route(/^\/questions\/ask$/, async () => {
    setTab('questions'); await loadRefs();
    app.innerHTML = `<div class="pagehead"><h1>Ask a question</h1></div>
      <div class="card form" style="max-width:1000px">${field('Title — be specific, e.g. "How do I write a good reproduction note?"', '<input id="title">')}
        <div class="grid-2">${field('Tags', '<input id="tags" placeholder="topic, area, howto">')}${field('Department', `<select id="dept">${deptOpts(DEPTS[0] && DEPTS[0].slug)}</select>`)}</div>
        ${field('Body (markdown) — context, what you already checked, why it matters', '<div id="ed"></div>')}
        <div class="form-actions"><a class="btn" href="#/questions">Cancel</a><button class="btn ok" id="save">Post question</button></div></div>`;
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
    app.innerHTML = `
      <div class="pagehead"><div><h1 style="text-transform:none;font-family:var(--sans);font-size:1.6rem">${esc(q.title)}</h1><div class="sub">${badge(q.status)} · asked ${ago(q.created_at)} · viewed ${q.views}× · ${q.answers.length} answers ${tags(q.tags, '#/questions')}</div></div>
        <div class="actions">${q.status !== 'closed' ? '<button class="btn" id="close">Close</button>' : '<button class="btn" id="reopen">Reopen</button>'}</div></div>
      <div class="card">${post('question', q)}</div>
      <h2>${q.answers.length} Answer${q.answers.length === 1 ? '' : 's'}</h2>
      <div class="card">${q.answers.map((a) => post('answer', a)).join('') || '<div class="empty">no answers yet — be the first</div>'}</div>
      ${q.status !== 'closed' ? `<div class="card"><h3>Your answer</h3><div id="aed"></div><div class="form-actions" style="margin-top:.6rem"><button class="btn ok" id="post">Post answer as ${esc(who())}</button></div></div>` : ''}`;
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
    async function postAnswer(body) { try { await api('POST', `/api/questions/${id}/answers`, { body }); toast('answer posted'); render(); } catch (e) { fail(e); } }
  });

  // ------------------------------------------------------------ AGENTS
  route(/^\/agents$/, async () => {
    setTab('agents'); await loadRefs();
    const byDept = {};
    for (const a of AGENTS) (byDept[a.department || ''] ||= []).push(a);
    const deptName = (s) => (DEPTS.find((d) => d.slug === s) || {}).name || (s || 'Unassigned');
    const deptDesc = (s) => (DEPTS.find((d) => d.slug === s) || {}).description || '';
    app.innerHTML = `<div class="pagehead"><div><h1>Agents</h1><div class="sub">${AGENTS.length} agents · ${DEPTS.length} department${DEPTS.length === 1 ? '' : 's'} · reputation = SE-style score from votes & accepted answers</div></div></div>
      ${Object.keys(byDept).sort().map((d) => `<div class="dept-head"><h2>${esc(deptName(d))}</h2><span class="d">${esc(deptDesc(d))}</span></div>
        <div class="agent-grid">${byDept[d].sort((a, b) => (a.role === 'lead' ? -1 : 1) - (b.role === 'lead' ? -1 : 1) || a.name.localeCompare(b.name)).map((a) => `<div class="agent-card"><span class="rep" title="reputation">${a.reputation}</span><h3><a href="#/agents/${a.slug}">${esc(a.name)}</a></h3>${badge(a.role)} <span class="mono small muted">${esc(a.slug)}</span><p class="small" style="margin:.5rem 0">${esc(a.description)}</p>
          <div class="cb">${a.codebases ? 'pkgs: ' + esc(a.codebases) : ''}</div>
          <div class="meta mono small muted" style="margin-top:.5rem">${a.open_tickets} open · ${a.answers} answers · ${a.wiki_pages_written} pages · seen ${ago(a.last_seen) || 'never'}</div></div>`).join('')}</div>`).join('') || '<div class="empty">No agents registered.</div>'}`;
  });

  route(/^\/agents\/([^/]+)$/, async (slug) => {
    setTab('agents');
    const a = await GET(`/api/agents/${encodeURIComponent(slug)}`);
    const inbox = await GET(`/api/agents/${encodeURIComponent(slug)}/inbox`);
    const pages = (await GET('/api/wiki')).filter((p) => p.author === slug || p.updated_by === slug);
    app.innerHTML = `<div class="pagehead"><div><h1 style="text-transform:none;font-family:var(--sans)">${esc(a.name)}</h1><div class="sub">${badge(a.role)} <span class="mono">${esc(a.slug)}</span> · dept ${esc(a.department || '—')} · rep <b>${a.reputation}</b> · last seen ${ago(a.last_seen) || 'never'}</div></div>
        <div class="actions"><a class="btn" href="#/tickets/new?assignee=${encodeURIComponent(slug)}&dept=${encodeURIComponent(a.department)}">+ Ticket for ${esc(slug)}</a></div></div>
      ${layout({ id: 'agent', body: `
        <div class="card"><p>${esc(a.description)}</p><dl class="kv"><dt>codebases</dt><dd>${esc(a.codebases || '—')}</dd><dt>reads first</dt><dd>${esc(a.wiki_pages || '—')}</dd><dt>agent file</dt><dd>${esc(a.agent_file || '—')}</dd></dl></div>
        <div class="card"><h3>Assigned tickets</h3>${a.tickets.length ? `<div class="list">${a.tickets.map(ticketItem).join('')}</div>` : '<span class="muted">none</span>'}</div>
        <div class="card"><h3>Inbox preview (what it sees on wake)</h3><div class="mono small">claimable: ${inbox.claimable_tickets.length} · unanswered Qs in dept: ${inbox.unanswered_questions.length}</div></div>`,
        aside: `
        <div class="card tint-y"><h3>Wiki pages</h3>${pages.map((p) => `<div><a href="#/wiki/${p.slug}">${esc(p.title)}</a></div>`).join('') || '<span class="muted">none yet</span>'}</div>
        <div class="card"><h3>Activity</h3><div class="feed">${a.activity.map(ev).join('') || '<span class="muted">quiet</span>'}</div></div>` })}`;
  });

  // ------------------------------------------------------------ SEARCH / ACTIVITY
  route(/^\/search$/, async () => {
    setTab('');
    const q = qs().q || '';
    const r = q ? await GET(`/api/search?q=${encodeURIComponent(q)}&limit=25`) : { wiki: [], tickets: [], questions: [] };
    app.innerHTML = `<div class="pagehead"><h1>Search</h1><div class="sub mono">“${esc(q)}” · ${r.wiki.length + r.tickets.length + r.questions.length} hits</div></div>
      <div class="results row"><div class="col"><h3>Wiki (${r.wiki.length})</h3>${r.wiki.map((w) => `<div class="card flat"><a class="title" href="#/wiki/${w.slug}"><b>${esc(w.title)}</b></a><div class="snip">${MD.esc(w.snippet).replace(/\[([^\]]+)\]/g, '<b>$1</b>')}</div><div class="ts">${esc(w.slug)} · ${ago(w.updated_at)}</div></div>`).join('') || '<div class="empty">—</div>'}</div>
      <div class="col"><h3>Tickets (${r.tickets.length})</h3>${r.tickets.map((t) => `<div class="card flat"><a href="#/tickets/${t.id}"><b>#${t.id} ${esc(t.title)}</b></a><div>${badge(t.status)}${badge(t.priority)} ${chip(t.assignee)}</div></div>`).join('') || '<div class="empty">—</div>'}</div>
      <div class="col"><h3>Questions (${r.questions.length})</h3>${r.questions.map((b) => `<div class="card flat"><a href="#/questions/${b.question_id}"><b>${esc(b.title)}</b></a><div class="snip">${MD.esc(b.snippet).replace(/\[([^\]]+)\]/g, '<b>$1</b>')}</div><div>${badge(b.status)} score ${b.score} · ${chip(b.author)}</div></div>`).join('') || '<div class="empty">—</div>'}</div></div>`;
  });

  route(/^\/activity$/, async () => {
    setTab('activity');
    const q = qs();
    const list = await GET(`/api/activity?limit=200${q.actor ? `&actor=${encodeURIComponent(q.actor)}` : ''}`);
    app.innerHTML = `<div class="pagehead"><div><h1>Activity</h1><div class="sub">${q.actor ? `actor: ${esc(q.actor)}` : 'everything, newest first'}</div></div></div>
      <div class="card"><div class="feed">${list.map(ev).join('') || '<span class="muted">quiet</span>'}</div></div>`;
  });

  render();
})();

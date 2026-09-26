/* ============================================================
   MARKDOWN-EDITOR — Obsidian-style live markdown editor
   Single pane: every line renders as markdown, except the line
   the caret is on, which reveals its raw syntax (dimmed) while
   still applying formatting as you type.

   Boots itself against #editor with the demo document, and is also
   embeddable: see initEditor() for the options and handle another
   page can drive it with.
   ============================================================ */

'use strict';

/* ---------------- pure helpers (no DOM) ---------------- */

function escapeHtml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Render LaTeX with KaTeX. Returns escaped raw text as a fallback when the
   library is absent (offline) or the formula does not parse. */
function renderMath(tex, displayMode) {
  const lib = typeof window !== 'undefined' && window.katex;
  if (!lib) {
    return '<code class="math-raw">' + escapeHtml((displayMode ? '$$' : '$') + tex
      + (displayMode ? '$$' : '$')) + '</code>';
  }
  try {
    return lib.renderToString(tex, {
      displayMode: !!displayMode,
      throwOnError: true,
      strict: false,
      output: 'html',
    });
  } catch (err) {
    return '<span class="math-bad" title="' + escapeHtml(String(err.message || err)) + '">'
      + escapeHtml(tex) + '</span>';
  }
}

/* [[Target]] / [[Target|label]] -> internal link. Opt-in per editor instance
   (options.wikilinks), so the standalone notes page keeps rendering double
   brackets as literal text. */
const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g;

/* Must match slugify() on the server, or a link renders to one name and
   resolves to another. A nested target keeps its slashes: each segment is
   slugified on its own rather than collapsing '/' into '-'. */
function wikiSlug(s) {
  const seg = (p) => String(p)
    .toLowerCase()
    .replace(/['"]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80);
  const parts = String(s).split('/').map(seg).filter(Boolean);
  return parts.join('/') || 'untitled';
}

/* [[@other-wiki/page]] names a page in a DIFFERENT wiki. Returns null for an
   ordinary same-wiki target. */
function parseCrossWiki(target) {
  const raw = String(target == null ? '' : target).trim();
  if (raw[0] !== '@') return null;
  const body = raw.slice(1);
  const cut = body.indexOf('/');
  const wikiPart = cut === -1 ? body : body.slice(0, cut);
  const pagePart = cut === -1 ? '' : body.slice(cut + 1);
  const wiki = wikiSlug(wikiPart);
  if (!wiki || wiki === 'untitled') return null;
  return { wiki, path: pagePart.trim() ? wikiSlug(pagePart) : null };
}

/* `resolve(slug, wiki)` is the host's "does this page exist?" lookup; a
   missing page still links, it is just styled differently. For a cross-wiki
   link the host gets the wiki slug as a second argument, and the href
   carries the target wiki so a click routes to it. */
function renderWikilink(target, label, resolve) {
  const cross = parseCrossWiki(target);
  const slug = cross ? cross.path : wikiSlug(target);
  const known = resolve ? !!resolve(slug, cross ? cross.wiki : null) : true;
  const text = (label == null ? '' : label).trim() || target.trim();
  // Hrefs are placeholders: the embedding host intercepts clicks via onLink
  // and routes them; cross-wiki links carry the destination wiki alongside it.
  const href = cross
    ? '/wiki/' + encodeURIComponent(cross.wiki)
      + (cross.path ? '?doc=' + encodeURIComponent(cross.path) : '')
    : '?doc=' + encodeURIComponent(slug);
  return '<a class="wikilink' + (known ? '' : ' wikilink-missing') + '"'
    + ' href="' + href + '"'
    + ' data-wikilink="' + escapeHtml(slug == null ? '' : slug) + '"'
    + (cross ? ' data-wiki="' + escapeHtml(cross.wiki) + '"' : '')
    + ' data-target="' + escapeHtml(target.trim()) + '"'
    + ' title="' + (known ? 'go to ' : 'create ') + escapeHtml(target.trim()) + '">'
    + escapeHtml(text) + '</a>';
}

/* Inline markdown -> HTML with syntax characters REMOVED (rendered mode).
   Code spans and math are pulled out of the RAW text first: code must not be
   re-processed by later passes, and KaTeX needs unescaped TeX.
   `wiki` is null (default) or {resolve}: passed in rather than held in module
   state so two editors on one page can disagree about wikilinks. */
function renderInline(raw, wiki) {
  const stash = [];
  const keep = (html) => '\u0000' + (stash.push(html) - 1) + '\u0000';

  /* 1. code spans and math, taken from the raw string */
  let s = raw
    .replace(/`([^`]+)`/g, (m, c) => keep('<code>' + escapeHtml(c) + '</code>'))
    .replace(/\$\$([^$]+)\$\$/g, (m, tex) =>
      keep('<span class="math math-display">' + renderMath(tex.trim(), true) + '</span>'))
    .replace(/\$([^$\n]+)\$/g, (m, tex) =>
      keep('<span class="math math-inline">' + renderMath(tex.trim(), false) + '</span>'));

  /* 2. wikilinks: after code/math are stashed so [[x]] inside a code span
     stays literal, before escaping so the page name survives intact, and
     before emphasis so underscores in a name are not italicised */
  if (wiki) {
    s = s.replace(WIKILINK_RE, (m, target, label) =>
      keep(renderWikilink(target, label, wiki.resolve)));
  }

  /* 3. escape what is left, then apply the markdown passes */
  s = escapeHtml(s);
  s = s.replace(/\[([^\]]*)\]\(([^)\s]*)\)/g, (m, t, u) =>
    keep('<a href="' + u + '" target="_blank" rel="noopener">' + emphasis(t) + '</a>'));
  s = emphasis(s);
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[+i]);
  return s;
}

function emphasis(s) {
  s = s.replace(/\*\*([^*]+)\*\*/g, (m, c) => '<strong>' + c + '</strong>');
  s = s.replace(/__([^_]+)__/g, (m, c) => '<strong>' + c + '</strong>');
  s = s.replace(/\*([^*]+)\*/g, (m, c) => '<em>' + c + '</em>');
  s = s.replace(/(^|[^A-Za-z0-9_])_([^_]+)_/g, (m, p, c) => p + '<em>' + c + '</em>');
  s = s.replace(/~~([^~]+)~~/g, (m, c) => '<del>' + c + '</del>');
  s = s.replace(/==([^=]+)==/g, (m, c) => '<mark>' + c + '</mark>');
  return s;
}

/* Inline markdown -> HTML with syntax characters KEPT VISIBLE but dimmed
   (active line mode). Invariant: textContent of the result === raw,
   so caret offsets map 1:1 to raw string indices. */
function renderActiveInline(raw, wiki) {
  let s = escapeHtml(raw);
  const stash = [];
  const keep = (html) => '\u0000' + (stash.push(html) - 1) + '\u0000';
  const syn = (t) => '<span class="syn">' + t + '</span>';

  /* every replacement is stashed so later passes cannot re-match the
     delimiter characters kept visible inside .syn spans */
  s = s.replace(/`([^`]+)`/g, (m, c) =>
    keep(syn('`') + '<code>' + c + '</code>' + syn('`')));
  /* math shows its LaTeX source while the caret is on the line, so the
     textContent === raw invariant holds and the formula stays editable */
  s = s.replace(/\$\$([^$]+)\$\$/g, (m, tex) =>
    keep(syn('$$') + '<span class="math-src">' + tex + '</span>' + syn('$$')));
  s = s.replace(/\$([^$\n]+)\$/g, (m, tex) =>
    keep(syn('$') + '<span class="math-src">' + tex + '</span>' + syn('$')));
  /* wikilinks keep their [[ ]] visible like every other delimiter, so the
     textContent === raw invariant survives; runs before the ]( link pass so
     the inner brackets are not mistaken for an inline link */
  if (wiki) {
    s = s.replace(WIKILINK_RE, (m, target, label) =>
      keep(syn('[[') + '<span class="linktext">' + target
        + (label == null ? '' : '|' + label) + '</span>' + syn(']]')));
  }
  s = s.replace(/\[([^\]]*)\]\(([^)\s]*)\)/g, (m, t, u) =>
    keep(syn('[') + '<span class="linktext">' + t + '</span>' + syn('](' + u + ')')));
  s = s.replace(/\*\*([^*]+)\*\*/g, (m, c) =>
    keep(syn('**') + '<strong>' + c + '</strong>' + syn('**')));
  s = s.replace(/__([^_]+)__/g, (m, c) =>
    keep(syn('__') + '<strong>' + c + '</strong>' + syn('__')));
  s = s.replace(/\*([^*]+)\*/g, (m, c) =>
    keep(syn('*') + '<em>' + c + '</em>' + syn('*')));
  s = s.replace(/~~([^~]+)~~/g, (m, c) =>
    keep(syn('~~') + '<del>' + c + '</del>' + syn('~~')));
  s = s.replace(/==([^=]+)==/g, (m, c) =>
    keep(syn('==') + '<mark>' + c + '</mark>' + syn('==')));
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[+i]);
  return s;
}

/* cheap content hash for render keys */
function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h.toString(36);
}

/* Map a caret offset in a RENDERED line (whose visible text is a
   subsequence of the raw markdown) back to an offset in the raw string,
   by greedy character alignment. */
function mapRenderedToRaw(raw, renderedText, renderedOffset) {
  let ri = 0;
  const n = Math.min(renderedOffset, renderedText.length);
  for (let i = 0; i < n; i++) {
    const ch = renderedText[i];
    while (ri < raw.length && raw[ri] !== ch) ri++;
    if (ri < raw.length) ri++;
  }
  return Math.min(ri, raw.length);
}

/* Per-line block context: [{isFence, inCode, table}]
   table is null or {isHeader, isSep, cols} */
function computeFenceFlags(lineArr) {
  const flags = [];
  let inCode = false, openIdx = -1, openLang = '';
  for (let idx = 0; idx < lineArr.length; idx++) {
    const raw = lineArr[idx];
    if (/^```/.test(raw)) {
      flags.push({ isFence: true, inCode: true, table: null, mermaid: null, mathBlock: null });
      if (!inCode) {
        openIdx = idx;
        openLang = raw.slice(3).trim().toLowerCase();
        inCode = true;
      } else {
        inCode = false;
        if (openLang === 'mermaid') {
          for (let j = openIdx; j <= idx; j++) {
            flags[j].mermaid = { start: openIdx, end: idx };
          }
        }
      }
    } else {
      flags.push({ isFence: false, inCode, table: null, mermaid: null, mathBlock: null });
    }
  }
  /* multi-line display math: a lone $$ opens, the next lone $$ closes */
  let mOpen = -1;
  for (let k = 0; k < lineArr.length; k++) {
    if (flags[k].inCode || flags[k].isFence) continue;
    if (/^\s*\$\$\s*$/.test(lineArr[k])) {
      if (mOpen < 0) mOpen = k;
      else {
        for (let j = mOpen; j <= k; j++) flags[j].mathBlock = { start: mOpen, end: k };
        mOpen = -1;
      }
    }
  }
  /* table blocks: a run of |...| lines whose SECOND line is a separator */
  const isRow = (i) => !flags[i].inCode && /^\s*\|.*\|\s*$/.test(lineArr[i]);
  const isSep = (i) => isRow(i) && /^\s*\|(\s*:?-+:?\s*\|)+\s*$/.test(lineArr[i]);
  let i = 0;
  while (i < lineArr.length) {
    if (isRow(i) && i + 1 < lineArr.length && isSep(i + 1)) {
      const cols = lineArr[i].split('|').slice(1, -1).length || 1;
      let end = i + 1;
      while (end + 1 < lineArr.length && isRow(end + 1) && !isSep(end + 1)) end++;
      for (let j = i; j <= end; j++) {
        flags[j].table = { isHeader: j === i, isSep: j === i + 1, cols };
      }
      i = end + 1;
    } else i++;
  }
  return flags;
}

const RE = {
  heading:  /^(#{1,6})(\s+)(.*)$/,
  hr:       /^(-{3,}|\*{3,}|_{3,})\s*$/,
  quote:    /^(>\s?)(.*)$/,
  task:     /^(\s*)([-*+])(\s\[)([ xX])(\]\s)(.*)$/,
  bullet:   /^(\s*)([-*+])(\s+)(.*)$/,
  /* ordered items accept plain (1.) and dotted outline (1.1. / 1.2.3.) numbers */
  ordered:  /^(\s*)((?:\d+\.)*\d+)([.)])(\s+)(.*)$/,
  listPrefix: /^(\s*)([-*+]\s\[[ xX]\]\s|[-*+]\s+|(?:\d+\.)*\d+[.)]\s+|>\s?)/
};

/* ---------------- ordered-list outline helpers ---------------- */

/* Parsed ordered item, or null. depth counts dotted segments beyond the first,
   plus any hand-typed indentation (two spaces per level). */
function parseOrdered(raw) {
  const m = raw.match(RE.ordered);
  if (!m) return null;
  const segments = m[2].split('.');
  return {
    indent: m[1],
    number: m[2],
    segments,
    delim: m[3],
    gap: m[4],
    text: m[5],
    depth: (segments.length - 1) + Math.floor(m[1].length / 2),
  };
}

/* Rebuild an ordered item's raw line at a given depth and number. Depth is
   carried by the dotted number itself, so no leading spaces are emitted. */
function formatOrdered(number, delim, gap, text) {
  return number + delim + (gap || ' ') + text;
}

/* Bounds [start, end] of the run of ordered lines containing idx. */
function orderedBlockRange(lineArr, idx, flags) {
  const isItem = (i) => i >= 0 && i < lineArr.length
    && !(flags && flags[i] && flags[i].inCode) && parseOrdered(lineArr[i]) !== null;
  if (!isItem(idx)) return null;
  let start = idx, end = idx;
  while (isItem(start - 1)) start--;
  while (isItem(end + 1)) end++;
  return { start, end };
}

/* Renumber a block of ordered items from their depths, so an outline stays
   sequential after a structural change (Tab, Shift+Tab, Enter).
   Returns a new array of raw lines for [start..end]. */
function renumberOrdered(lineArr, start, end, depths) {
  const counters = [];
  const out = [];
  for (let i = start; i <= end; i++) {
    const item = parseOrdered(lineArr[i]);
    if (!item) { out.push(lineArr[i]); continue; }
    const d = Math.max(0, depths ? depths[i - start] : item.depth);
    counters.length = d + 1;
    for (let k = 0; k <= d; k++) if (!counters[k]) counters[k] = k === d ? 0 : 1;
    counters[d] += 1;
    const number = counters.slice(0, d + 1).join('.');
    out.push(formatOrdered(number, item.delim, item.gap, item.text));
  }
  return out;
}

/* ---------------- editor (DOM) ---------------- */

/* Standalone page self-boot. An embedding host sets MARKDOWN_EDITOR_NO_AUTOBOOT
   before loading the script so it can call initEditor() itself with
   options instead of getting the demo document against #editor. */
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    if (typeof window !== 'undefined' && window.MARKDOWN_EDITOR_NO_AUTOBOOT) return;
    initEditor();
  });
}

/* initEditor(options) -> handle. Called with no arguments it reproduces the
   standalone page exactly: #editor, #status-left, the demo document, focus.

   options: { editor, status, doc, onChange, onLink, wikilinks,
              resolveWikilink, focus }
   handle:  { getText, setText, rerender, focus, destroy }

   onChange(text) fires on real user edits only — never during boot,
   setText() or rerender(), so loading a document is not an unsaved edit. */
function initEditor(options) {
  const opts = options || {};
  /* an element, '#id', 'id', or undefined -> the standalone default id.
     null is explicit "no element" and must not fall back. */
  const pick = (v, fallbackId) => {
    if (v === null) return null;
    if (v && typeof v === 'object') return v;
    return document.getElementById(typeof v === 'string' ? v.replace(/^#/, '') : fallbackId);
  };

  const editor = pick(opts.editor, 'editor');
  const statusLeft = pick(opts.status, 'status-left');
  if (!editor) throw new Error('initEditor: no editor element');

  const onChange = typeof opts.onChange === 'function' ? opts.onChange : null;
  const onLink = typeof opts.onLink === 'function' ? opts.onLink : null;
  /* one object per instance, so renderInline/renderActiveInline stay pure and
     two editors on a page can differ */
  const wiki = opts.wikilinks
    ? { resolve: typeof opts.resolveWikilink === 'function' ? opts.resolveWikilink : null }
    : null;

  let lines = String(opts.doc == null ? DEMO_DOC : opts.doc).split('\n');
  if (!lines.length) lines = [''];
  let activeIndex = -1;
  let fenceFlags = computeFenceFlags(lines);
  let suppressSel = false;
  let composing = false;
  /* the list marker the editor inserted on the last Enter; a second Enter on
     that same still-empty item exits the list */
  let autoItem = { idx: -1, prefix: '' };

  const undoStack = [];
  const redoStack = [];
  let lastPush = 0;

  /* ---------- rendering ---------- */

  function syn(t) { return '<span class="syn">' + escapeHtml(t) + '</span>'; }

  function renderLine(i) {
    const el = editor.children[i];
    if (!el) return;
    const raw = lines[i];
    const f = fenceFlags[i] || { isFence: false, inCode: false };
    const active = i === activeIndex;
    const t = f.table;
    const mer = f.mermaid;
    const mb = f.mathBlock;
    const merCollapsed = mer && window.mermaid
      && !(activeIndex >= mer.start && activeIndex <= mer.end);
    const mbCollapsed = mb && !(activeIndex >= mb.start && activeIndex <= mb.end);
    const merCode = mer ? lines.slice(mer.start + 1, mer.end).join('\n') : '';
    const mbTex = mb ? lines.slice(mb.start + 1, mb.end).join('\n') : '';
    const key = (active ? 'A' : 'I') + (f.isFence ? 'F' : '') + (f.inCode ? 'C' : '')
      + (t ? 'T' + t.cols + (t.isHeader ? 'h' : t.isSep ? 's' : 'r') : '')
      + (mer ? 'M' + (merCollapsed ? 'c' : 'x') + hashStr(merCode) : '')
      + (mb ? 'Q' + (mbCollapsed ? 'c' : 'x') + hashStr(mbTex) : '')
      + '|' + raw;
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    el.className = 'line' + (active ? ' active' : '');
    el.removeAttribute('data-marker');
    el.removeAttribute('style');

    /* complete $$ block, caret elsewhere: formula on the opening line,
       remaining block lines hidden */
    if (mbCollapsed) {
      if (i === mb.start) {
        el.classList.add('mathblock');
        el.innerHTML = '<span class="math math-display" contenteditable="false">'
          + renderMath(mbTex, true) + '</span>';
      } else {
        el.classList.add('mdhidden');
        el.innerHTML = '<br>';
      }
      return;
    }

    /* complete mermaid block, caret elsewhere: diagram on the opening
       fence line, remaining block lines hidden */
    if (merCollapsed) {
      if (i === mer.start) { renderMermaid(el, merCode); return; }
      el.classList.add('mdhidden');
      el.innerHTML = '<br>';
      return;
    }

    if (raw === '' && !f.inCode) { el.innerHTML = '<br>'; return; }

    /* code blocks: always shown as raw mono text */
    if (f.isFence || f.inCode) {
      el.classList.add(f.isFence ? 'fenceline' : 'codeline');
      el.innerHTML = raw === '' ? '<br>' : escapeHtml(raw);
      return;
    }

    if (t && !active) { renderTableRow(el, raw, t); return; }
    if (t && active) {
      el.classList.add('tablesrc');
      el.innerHTML = renderActiveInline(raw, wiki) || '<br>';
      return;
    }

    if (active) renderActiveLine(el, raw);
    else renderInactiveLine(el, raw);
  }

  const mermaidCache = new Map(); /* code -> svg (or error html) */
  let mermaidSeq = 0;

  function renderMermaid(el, code) {
    el.classList.add('mermaidline');
    const box = (inner, bad) =>
      '<div class="mermaid-box' + (bad ? ' mermaid-bad' : '') + '" contenteditable="false">' + inner + '</div>';
    const cached = mermaidCache.get(code);
    if (cached) { el.innerHTML = box(cached.svg, cached.bad); return; }
    el.innerHTML = box('rendering diagram…', false);
    const myKey = el.dataset.key;
    window.mermaid.render('mmd' + (mermaidSeq++), code)
      .then(({ svg }) => {
        mermaidCache.set(code, { svg, bad: false });
        if (el.dataset.key === myKey) el.innerHTML = box(svg, false);
      })
      .catch((err) => {
        const msg = 'mermaid: ' + escapeHtml(String(err && err.message || err));
        mermaidCache.set(code, { svg: msg, bad: true });
        if (el.dataset.key === myKey) el.innerHTML = box(msg, true);
        /* mermaid appends orphaned error nodes to <body>; sweep them */
        document.querySelectorAll('body > [id^="dmmd"]').forEach(n => n.remove());
      });
  }

  function renderTableRow(el, raw, t) {
    el.classList.add('tablerow');
    if (t.isHeader) el.classList.add('tablehead');
    if (t.isSep) {
      el.classList.add('tablesep');
      el.innerHTML = '<br>';
      return;
    }
    el.style.setProperty('--cols', t.cols);
    const cells = raw.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|');
    while (cells.length < t.cols) cells.push('');
    el.innerHTML = cells.slice(0, t.cols)
      .map(c => '<span class="cell">' + (renderInline(c.trim(), wiki) || '&nbsp;') + '</span>')
      .join('');
  }

  function renderInactiveLine(el, raw) {
    let m;
    if ((m = raw.match(RE.heading))) {
      el.classList.add('h' + m[1].length);
      el.innerHTML = renderInline(m[3], wiki) || '<br>';
    } else if (RE.hr.test(raw)) {
      el.classList.add('hr');
      el.innerHTML = '<br>';
    } else if ((m = raw.match(RE.quote))) {
      el.classList.add('quote');
      el.innerHTML = renderInline(m[2], wiki) || '<br>';
    } else if ((m = raw.match(RE.task))) {
      const checked = m[4].toLowerCase() === 'x';
      el.classList.add('task');
      if (checked) el.classList.add('done');
      el.style.setProperty('--ind', m[1].length);
      el.innerHTML = '<input type="checkbox" contenteditable="false" tabindex="-1"'
        + (checked ? ' checked' : '') + '>' + (renderInline(m[6], wiki) || '<br>');
    } else if ((m = raw.match(RE.bullet))) {
      el.classList.add('bullet');
      el.style.setProperty('--ind', m[1].length);
      el.innerHTML = renderInline(m[4], wiki) || '<br>';
    } else if ((m = raw.match(RE.ordered))) {
      const item = parseOrdered(raw);
      el.classList.add('ordered');
      el.style.setProperty('--ind', item.depth * 2);
      el.style.setProperty('--marker-w', (item.number + item.delim).length);
      el.setAttribute('data-marker', item.number + item.delim);
      el.innerHTML = renderInline(item.text, wiki) || '<br>';
    } else {
      el.innerHTML = renderInline(raw, wiki) || '<br>';
    }
  }

  /* Active line: all raw characters stay visible (textContent === raw). */
  function renderActiveLine(el, raw) {
    let m;
    if ((m = raw.match(RE.heading))) {
      el.classList.add('h' + m[1].length);
      el.innerHTML = syn(m[1] + m[2]) + renderActiveInline(m[3], wiki);
    } else if ((m = raw.match(RE.quote))) {
      el.classList.add('quote');
      el.innerHTML = syn(m[1]) + renderActiveInline(m[2], wiki);
    } else if ((m = raw.match(RE.task))) {
      el.classList.add('task-src');
      el.innerHTML = syn(m[1] + m[2] + m[3] + m[4] + m[5]) + renderActiveInline(m[6], wiki);
    } else if ((m = raw.match(RE.bullet))) {
      el.innerHTML = syn(m[1] + m[2] + m[3]) + renderActiveInline(m[4], wiki);
    } else if ((m = raw.match(RE.ordered))) {
      el.innerHTML = syn(m[1] + m[2] + m[3] + m[4]) + renderActiveInline(m[5], wiki);
    } else {
      el.innerHTML = renderActiveInline(raw, wiki) || '<br>';
    }
  }

  function syncDom() {
    while (editor.children.length > lines.length) editor.lastChild.remove();
    while (editor.children.length < lines.length) {
      const d = document.createElement('div');
      d.className = 'line';
      editor.appendChild(d);
    }
    fenceFlags = computeFenceFlags(lines);
    for (let i = 0; i < lines.length; i++) renderLine(i);
    updateStatus();
  }

  /* Set while the editor renders on its own behalf (boot, setText, rerender)
     rather than in response to a user edit, so the host is not told the
     document changed when it did not. */
  let quiet = true;

  function updateStatus() {
    const words = lines.join(' ').split(/\s+/).filter(Boolean).length;
    /* status is optional: an embedded editor may have nowhere to put it */
    if (statusLeft) {
      statusLeft.textContent = words + ' words · ' + lines.length + ' lines';
    }
    if (onChange && !quiet) onChange(lines.join('\n'));
  }

  /* ---------- caret utilities ---------- */

  function closestLine(node) {
    while (node && node !== editor) {
      if (node.parentNode === editor) return node;
      node = node.parentNode;
    }
    return null;
  }

  function indexOfLine(el) {
    return Array.prototype.indexOf.call(editor.children, el);
  }

  function textOffsetIn(el, container, offset) {
    const pre = document.createRange();
    pre.selectNodeContents(el);
    try { pre.setEnd(container, offset); } catch (e) { return 0; }
    return pre.toString().length;
  }

  /* {idx, raw} from an arbitrary DOM selection endpoint */
  function pointFrom(container, offset) {
    if (container === editor) {
      if (lines.length === 0) return { idx: 0, raw: 0 };
      if (offset >= editor.children.length) {
        return { idx: lines.length - 1, raw: lines[lines.length - 1].length };
      }
      return { idx: offset, raw: 0 };
    }
    const lineEl = closestLine(container);
    if (!lineEl) return null;
    const idx = indexOfLine(lineEl);
    if (idx < 0 || idx >= lines.length) return null;
    const tOff = textOffsetIn(lineEl, container, offset);
    const raw = (idx === activeIndex)
      ? tOff
      : mapRenderedToRaw(lines[idx], lineEl.textContent, tOff);
    return { idx, raw: Math.min(raw, lines[idx].length) };
  }

  function currentCaret() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return null;
    const r = sel.getRangeAt(0);
    if (!editor.contains(r.startContainer)) return null;
    return pointFrom(r.startContainer, r.startOffset);
  }

  function setCaretIn(el, offset) {
    const range = document.createRange();
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node, last = null, remaining = offset, done = false;
    while ((node = walker.nextNode())) {
      last = node;
      if (remaining <= node.length) {
        range.setStart(node, remaining);
        done = true;
        break;
      }
      remaining -= node.length;
    }
    if (!done) {
      if (last) range.setStart(last, last.length);
      else range.setStart(el, 0);
    }
    range.collapse(true);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function placeCaret(idx, rawOffset) {
    const el = editor.children[idx];
    if (!el) return;
    suppressSel = true;
    setCaretIn(el, Math.min(rawOffset, (lines[idx] || '').length));
    setTimeout(() => { suppressSel = false; }, 0);
  }

  /* ---------- history ---------- */

  function pushHistory(force) {
    redoStack.length = 0;
    const now = Date.now();
    if (!force && now - lastPush < 500 && undoStack.length) { lastPush = now; return; }
    undoStack.push({ lines: lines.slice(), caret: currentCaret() });
    if (undoStack.length > 300) undoStack.shift();
    lastPush = now;
  }

  function applySnapshot(s) {
    lines = s.lines.slice();
    if (lines.length === 0) lines = [''];
    const caret = s.caret && s.caret.idx < lines.length
      ? s.caret
      : { idx: lines.length - 1, raw: lines[lines.length - 1].length };
    activeIndex = caret.idx;
    syncDom();
    placeCaret(caret.idx, caret.raw);
  }

  function undo() {
    if (!undoStack.length) return;
    redoStack.push({ lines: lines.slice(), caret: currentCaret() });
    applySnapshot(undoStack.pop());
  }

  function redo() {
    if (!redoStack.length) return;
    undoStack.push({ lines: lines.slice(), caret: currentCaret() });
    applySnapshot(redoStack.pop());
  }

  /* ---------- editing operations ---------- */

  function replaceSelection(insert) {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);
    let a = pointFrom(r.startContainer, r.startOffset);
    let b = pointFrom(r.endContainer, r.endOffset);
    if (!a || !b) return;
    if (a.idx > b.idx || (a.idx === b.idx && a.raw > b.raw)) { const t = a; a = b; b = t; }
    pushHistory(a.idx !== b.idx);

    const before = lines[a.idx].slice(0, a.raw);
    const after = lines[b.idx].slice(b.raw);
    const parts = String(insert == null ? '' : insert).replace(/\r\n?/g, '\n').split('\n');

    let newLines, caretIdx, caretRaw;
    if (parts.length === 1) {
      newLines = [before + parts[0] + after];
      caretIdx = a.idx;
      caretRaw = before.length + parts[0].length;
    } else {
      const middle = parts.slice(1, -1);
      newLines = [before + parts[0], ...middle, parts[parts.length - 1] + after];
      caretIdx = a.idx + newLines.length - 1;
      caretRaw = parts[parts.length - 1].length;
    }
    lines.splice(a.idx, b.idx - a.idx + 1, ...newLines);
    if (lines.length === 0) lines = [''];
    activeIndex = caretIdx;
    syncDom();
    placeCaret(caretIdx, caretRaw);
  }

  function handleEnter() {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);
    if (!r.collapsed) { replaceSelection('\n'); return; }
    const p = pointFrom(r.startContainer, r.startOffset);
    if (!p) return;
    pushHistory(true);

    const raw = lines[p.idx];
    const before = raw.slice(0, p.raw);
    const after = raw.slice(p.raw);

    /* list / quote continuation */
    let prefix = '';
    const lm = before.match(RE.listPrefix);
    if (lm && p.raw >= lm[0].length && !fenceFlags[p.idx].inCode) {
      /* Exit the list only when this empty item is one WE created on a previous
         Enter. A marker the user typed by hand continues the list instead, so
         typing "1. " and pressing Enter gives "2. " rather than wiping the line. */
      if (after === '' && raw === lm[0]
          && autoItem.idx === p.idx && autoItem.prefix === raw) {
        lines[p.idx] = '';
        autoItem = { idx: -1, prefix: '' };
        activeIndex = p.idx;
        syncDom();
        placeCaret(p.idx, 0);
        return;
      }
      prefix = lm[0];
      const om = parseOrdered(prefix + 'x');
      if (om) {
        /* increment the last segment: 1. -> 2. and 1.1. -> 1.2. */
        const seg = om.segments.slice();
        seg[seg.length - 1] = String(parseInt(seg[seg.length - 1], 10) + 1);
        prefix = om.indent + seg.join('.') + om.delim + om.gap;
      }
      prefix = prefix.replace(/\[[xX]\]/, '[ ]');
    }

    lines.splice(p.idx, 1, before, prefix + after);
    activeIndex = p.idx + 1;
    /* remember the auto-inserted marker so a second Enter exits the list */
    autoItem = prefix && after === ''
      ? { idx: activeIndex, prefix }
      : { idx: -1, prefix: '' };
    syncDom();
    placeCaret(activeIndex, prefix.length);
  }

  function mergeLines(firstIdx) {
    if (firstIdx < 0 || firstIdx + 1 >= lines.length) return;
    pushHistory(true);
    const caretRaw = lines[firstIdx].length;
    lines.splice(firstIdx, 2, lines[firstIdx] + lines[firstIdx + 1]);
    activeIndex = firstIdx;
    syncDom();
    placeCaret(firstIdx, caretRaw);
  }

  /* Tab / Shift+Tab on a list item: change its nesting depth and renumber the
     surrounding ordered block. Returns 'handled' when the depth changed,
     'list' when the caret is on a list item that cannot move (so Tab is
     swallowed rather than inserting spaces), or false when not on a list. */
  function shiftListDepth(delta) {
    const caret = currentCaret();
    if (!caret || fenceFlags[caret.idx] && fenceFlags[caret.idx].inCode) return false;
    const idx = caret.idx;
    const raw = lines[idx];

    const item = parseOrdered(raw);
    if (item) {
      const range = orderedBlockRange(lines, idx, fenceFlags);
      const depths = [];
      for (let i = range.start; i <= range.end; i++) {
        const it = parseOrdered(lines[i]);
        depths.push(it ? it.depth : 0);
      }
      const at = idx - range.start;
      const target = depths[at] + delta;
      if (target < 0) return 'list';
      /* cannot indent past one level deeper than the item above */
      const above = at > 0 ? depths[at - 1] : -1;
      if (delta > 0 && target > above + 1) return 'list';
      depths[at] = target;
      /* keep descendants attached to the item that moved */
      for (let i = at + 1; i < depths.length && depths[i] > depths[at] - delta; i++) {
        depths[i] = Math.max(0, depths[i] + delta);
      }
      pushHistory(true);
      /* keep the caret at the same column within the item's text */
      const markerLen = (item.number + item.delim + item.gap).length;
      const textOffset = Math.max(0, caret.raw - markerLen);
      const rewritten = renumberOrdered(lines, range.start, range.end, depths);
      lines.splice(range.start, range.end - range.start + 1, ...rewritten);
      activeIndex = idx;
      autoItem = { idx: -1, prefix: '' };
      syncDom();
      const newItem = parseOrdered(lines[idx]);
      placeCaret(idx, newItem
        ? (newItem.number + newItem.delim + newItem.gap).length + textOffset
        : lines[idx].length);
      return 'handled';
    }

    /* bullets, tasks and quotes: two spaces per level */
    const bm = raw.match(/^(\s*)([-*+]\s\[[ xX]\]\s|[-*+]\s+|>\s?)/);
    if (!bm) return false;
    const spaces = bm[1].length;
    if (delta < 0 && spaces === 0) return 'list';
    pushHistory(true);
    const newIndent = ' '.repeat(Math.max(0, spaces + delta * 2));
    lines[idx] = newIndent + raw.slice(spaces);
    activeIndex = idx;
    syncDom();
    placeCaret(idx, Math.max(0, caret.raw + delta * 2));
    return 'handled';
  }
  /* Wrap current selection (single line) with an inline marker, e.g. ** */
  function wrapSelection(marker) {
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);
    let a = pointFrom(r.startContainer, r.startOffset);
    let b = pointFrom(r.endContainer, r.endOffset);
    if (!a || !b || a.idx !== b.idx) return;
    if (a.raw > b.raw) { const t = a; a = b; b = t; }
    pushHistory(true);
    const raw = lines[a.idx];
    lines[a.idx] = raw.slice(0, a.raw) + marker + raw.slice(a.raw, b.raw) + marker + raw.slice(b.raw);
    activeIndex = a.idx;
    syncDom();
    placeCaret(a.idx, (a.raw === b.raw ? a.raw : b.raw) + marker.length);
  }

  function rawSelectionText() {
    const sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return null;
    const r = sel.getRangeAt(0);
    let a = pointFrom(r.startContainer, r.startOffset);
    let b = pointFrom(r.endContainer, r.endOffset);
    if (!a || !b) return null;
    if (a.idx > b.idx || (a.idx === b.idx && a.raw > b.raw)) { const t = a; a = b; b = t; }
    if (a.idx === b.idx) return lines[a.idx].slice(a.raw, b.raw);
    const out = [lines[a.idx].slice(a.raw)];
    for (let i = a.idx + 1; i < b.idx; i++) out.push(lines[i]);
    out.push(lines[b.idx].slice(0, b.raw));
    return out.join('\n');
  }

  /* ---------- activation (the Obsidian trick) ---------- */

  function deactivate() {
    if (activeIndex < 0) return;
    activeIndex = -1;
    fenceFlags = computeFenceFlags(lines);
    for (let i = 0; i < lines.length; i++) renderLine(i);
  }

  function onSelectionChange() {
    if (suppressSel || composing) return;
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);
    if (!editor.contains(r.startContainer)) { deactivate(); return; }
    if (!r.collapsed) return; /* keep lines rendered while selecting across them */

    const lineEl = closestLine(r.startContainer);
    if (!lineEl) return;
    const idx = indexOfLine(lineEl);
    if (idx === activeIndex || idx < 0 || idx >= lines.length) return;

    /* capture offset in the currently-rendered content BEFORE re-rendering */
    const tOff = textOffsetIn(lineEl, r.startContainer, r.startOffset);
    const rawOffset = mapRenderedToRaw(lines[idx], lineEl.textContent, tOff);

    const prev = activeIndex;
    activeIndex = idx;
    if (prev >= 0) renderLine(prev);
    renderLine(idx);
    /* activation may collapse/expand a mermaid block elsewhere */
    for (let i = 0; i < lines.length; i++) renderLine(i);
    placeCaret(idx, rawOffset);
  }

  /* ---------- event wiring ---------- */

  document.addEventListener('selectionchange', onSelectionChange);

  editor.addEventListener('beforeinput', (e) => {
    const type = e.inputType;

    if (type === 'historyUndo') { e.preventDefault(); undo(); return; }
    if (type === 'historyRedo') { e.preventDefault(); redo(); return; }
    if (type === 'formatBold') { e.preventDefault(); wrapSelection('**'); return; }
    if (type === 'formatItalic') { e.preventDefault(); wrapSelection('*'); return; }
    if (type.startsWith('format')) { e.preventDefault(); return; }

    if (type === 'insertParagraph' || type === 'insertLineBreak') {
      e.preventDefault();
      handleEnter();
      return;
    }

    if (type === 'insertFromPaste' || type === 'insertFromDrop') {
      e.preventDefault();
      const dt = e.dataTransfer;
      replaceSelection(dt ? dt.getData('text/plain') : '');
      return;
    }

    /* multi-line plain insertText (IME commit, CDP Input.insertText):
       the browser would split the line div itself and desync the model */
    if (type === 'insertText' && e.data && e.data.includes('\n')) {
      e.preventDefault();
      replaceSelection(e.data);
      return;
    }

    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const r = sel.getRangeAt(0);

    if (!r.collapsed) {
      const startLine = closestLine(r.startContainer);
      const endLine = closestLine(r.endContainer);
      const sameActiveLine = startLine && startLine === endLine
        && indexOfLine(startLine) === activeIndex;
      if (!sameActiveLine) {
        /* selection touches rendered lines -> handle manually against raw model */
        e.preventDefault();
        replaceSelection(type.startsWith('insert') ? (e.data || '') : '');
        return;
      }
      if (type === 'deleteByCut') {
        e.preventDefault();
        replaceSelection('');
        return;
      }
    }

    if (r.collapsed && /^delete.*Backward$/.test(type)) {
      const lineEl = closestLine(r.startContainer);
      const idx = lineEl ? indexOfLine(lineEl) : -1;
      if (idx >= 0 && textOffsetIn(lineEl, r.startContainer, r.startOffset) === 0) {
        e.preventDefault();
        if (idx > 0) mergeLines(idx - 1);
        return;
      }
    }

    if (r.collapsed && /^delete.*Forward$/.test(type)) {
      const lineEl = closestLine(r.startContainer);
      const idx = lineEl ? indexOfLine(lineEl) : -1;
      if (idx >= 0 && textOffsetIn(lineEl, r.startContainer, r.startOffset) >= (lines[idx] || '').length) {
        e.preventDefault();
        if (idx < lines.length - 1) mergeLines(idx);
        return;
      }
    }

    /* plain typing / deleting inside the active raw line: let the browser
       do it, but record history first */
    if (type.startsWith('insert') || type.startsWith('delete')) pushHistory(false);
  });

  editor.addEventListener('input', (e) => {
    if (composing) return;
    if (activeIndex < 0 || !editor.children[activeIndex]) { return; }
    const el = editor.children[activeIndex];
    const sel = window.getSelection();
    let offset = 0;
    if (sel.rangeCount && el.contains(sel.getRangeAt(0).startContainer)) {
      const r = sel.getRangeAt(0);
      offset = textOffsetIn(el, r.startContainer, r.startOffset);
    }
    lines[activeIndex] = el.textContent.replace(/\n/g, '');
    el.dataset.key = ''; /* force re-render of live formatting */
    fenceFlags = computeFenceFlags(lines);
    for (let i = 0; i < lines.length; i++) renderLine(i);
    updateStatus();
    placeCaret(activeIndex, offset);
  });

  editor.addEventListener('compositionstart', () => { composing = true; });
  editor.addEventListener('compositionend', () => {
    composing = false;
    editor.dispatchEvent(new Event('input'));
  });

  editor.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) redo(); else undo();
      return;
    }
    if (mod && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); return; }
    if (mod && e.key.toLowerCase() === 'b') { e.preventDefault(); wrapSelection('**'); return; }
    if (mod && e.key.toLowerCase() === 'i') { e.preventDefault(); wrapSelection('*'); return; }
    if (e.key === 'Tab') {
      e.preventDefault();
      /* on a list item Tab nests it (and renumbers an outline);
         anywhere else it inserts an indent */
      const outcome = shiftListDepth(e.shiftKey ? -1 : 1);
      if (outcome === false && !e.shiftKey) replaceSelection('  ');
    }
  });

  editor.addEventListener('copy', (e) => {
    const text = rawSelectionText();
    if (text == null) return;
    e.preventDefault();
    e.clipboardData.setData('text/plain', text);
  });

  editor.addEventListener('cut', (e) => {
    const text = rawSelectionText();
    if (text == null) return;
    e.preventDefault();
    e.clipboardData.setData('text/plain', text);
    replaceSelection('');
  });

  /* checkbox toggling on rendered task lines */
  editor.addEventListener('mousedown', (e) => {
    const a = e.target.closest && e.target.closest('a');
    if (a) { e.preventDefault(); return; }
    if (e.target.matches && e.target.matches('input[type="checkbox"]')) {
      e.preventDefault();
      const lineEl = closestLine(e.target);
      const idx = lineEl ? indexOfLine(lineEl) : -1;
      if (idx < 0) return;
      pushHistory(true);
      lines[idx] = /\[[xX]\]/.test(lines[idx])
        ? lines[idx].replace(/\[[xX]\]/, '[ ]')
        : lines[idx].replace(/\[ \]/, '[x]');
      fenceFlags = computeFenceFlags(lines);
      renderLine(idx);
      updateStatus();
    }
  });

  /* ---------- links: click opens, hover tooltip Edit ---------- */

  const tip = document.createElement('div');
  tip.className = 'linktip';
  tip.innerHTML = '<span class="linktip-url"></span><button class="linktip-edit" type="button">✎ EDIT</button>';
  document.body.appendChild(tip);
  const tipUrl = tip.querySelector('.linktip-url');
  const tipBtn = tip.querySelector('.linktip-edit');
  let tipTarget = null;
  let tipHideTimer = 0;

  function showTip(a) {
    clearTimeout(tipHideTimer);
    tipTarget = a;
    tipUrl.textContent = a.getAttribute('href');
    const r = a.getBoundingClientRect();
    tip.style.display = 'flex';
    tip.style.left = Math.max(8, r.left + window.scrollX) + 'px';
    tip.style.top = (r.bottom + window.scrollY + 6) + 'px';
  }

  function hideTip() {
    clearTimeout(tipHideTimer);
    tip.style.display = 'none';
    tipTarget = null;
  }

  function hideTipSoon() {
    clearTimeout(tipHideTimer);
    tipHideTimer = setTimeout(hideTip, 250);
  }

  editor.addEventListener('mouseover', (e) => {
    const a = e.target.closest && e.target.closest('a');
    if (a && editor.contains(a)) showTip(a);
  });
  editor.addEventListener('mouseout', (e) => {
    if (e.target.closest && e.target.closest('a')) hideTipSoon();
  });
  tip.addEventListener('mouseenter', () => clearTimeout(tipHideTimer));
  tip.addEventListener('mouseleave', hideTipSoon);

  /* plain click follows the link (mousedown is prevented so the caret
     does not move into the line and de-render it before the click).
     onLink gets first refusal: a host that routes the href itself returns
     true and no tab is opened. Wikilinks are internal, so they are never
     opened in a tab even when the host declines them. */
  editor.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a');
    if (!a) return;
    e.preventDefault();
    const href = a.getAttribute('href');
    if (onLink && onLink(href, e) === true) return;
    if (a.dataset && a.dataset.wikilink) return;
    window.open(a.href, '_blank', 'noopener');
  });

  /* the hover tooltip's Edit affordance is meaningless for a wikilink,
     whose href is a route rather than an external url */
  editor.addEventListener('mouseover', (e) => {
    const a = e.target.closest && e.target.closest('a');
    if (a && a.dataset && a.dataset.wikilink) hideTip();
  }, true);

  /* EDIT: reveal the line's raw markdown with the caret on the link */
  tipBtn.addEventListener('mousedown', (e) => {
    e.preventDefault();
    if (!tipTarget) return;
    const lineEl = closestLine(tipTarget);
    const idx = lineEl ? indexOfLine(lineEl) : -1;
    if (idx < 0) { hideTip(); return; }
    const pre = document.createRange();
    pre.selectNodeContents(lineEl);
    pre.setEnd(tipTarget, 0);
    const tOff = pre.toString().length;
    const rawOff = mapRenderedToRaw(lines[idx], lineEl.textContent, tOff);
    hideTip();
    const prev = activeIndex;
    activeIndex = idx;
    if (prev >= 0 && prev !== idx) renderLine(prev);
    renderLine(idx);
    editor.focus();
    placeCaret(idx, Math.min(rawOff + 1, lines[idx].length));
  });

  /* click in the empty space below the last line -> caret to end.
     Named and kept, because this one hangs off the editor's PARENT and so
     would outlive the editor element on destroy(). */
  const editorParent = editor.parentElement;
  function onParentMouseDown(e) {
    if (e.target !== editor && e.target !== editorParent) return;
    const last = editor.lastElementChild;
    if (!last) return;
    const rect = last.getBoundingClientRect();
    if (e.clientY > rect.bottom) {
      e.preventDefault();
      editor.focus();
      const idx = lines.length - 1;
      const prev = activeIndex;
      activeIndex = idx;
      if (prev >= 0 && prev !== idx) renderLine(prev);
      renderLine(idx);
      placeCaret(idx, lines[idx].length);
    }
  }
  if (editorParent) editorParent.addEventListener('mousedown', onParentMouseDown);

  /* ---------- boot ---------- */
  if (window.mermaid) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      themeVariables: {
        primaryColor: '#dcf0f7',
        primaryBorderColor: '#16161a',
        primaryTextColor: '#16161a',
        lineColor: '#16161a',
        secondaryColor: '#dff5e7',
        tertiaryColor: '#fbf8f1',
        fontFamily: 'ui-monospace, Menlo, monospace'
      }
    });
  }

  /* click a rendered diagram or formula block to edit its source */
  editor.addEventListener('mousedown', (e) => {
    const boxEl = e.target.closest && e.target.closest('.mermaid-box, .mathblock .math-display');
    if (!boxEl) return;
    e.preventDefault();
    const lineEl = closestLine(boxEl);
    const idx = lineEl ? indexOfLine(lineEl) : -1;
    if (idx < 0 || !fenceFlags[idx]) return;
    const blk = fenceFlags[idx].mermaid || fenceFlags[idx].mathBlock;
    if (!blk) return;
    activeIndex = Math.min(blk.start + 1, blk.end);
    for (let i = 0; i < lines.length; i++) renderLine(i);
    editor.focus();
    placeCaret(activeIndex, 0);
  });

  syncDom();
  quiet = false;
  if (opts.focus !== false) editor.focus();

  /* Handle for an embedding host: swap documents or re-render in place
     without tearing the editor down and rebuilding it. */
  return {
    getText: () => lines.join('\n'),
    setText(text) {
      quiet = true;
      lines = String(text == null ? '' : text).split('\n');
      if (!lines.length) lines = [''];
      activeIndex = -1;
      undoStack.length = 0;
      redoStack.length = 0;
      autoItem = { idx: -1, prefix: '' };
      syncDom();
      quiet = false;
    },
    /* re-render in place, e.g. after resolveWikilink's answers change so a
       formerly-missing link stops being styled as missing. Purely visual:
       the document is unchanged, so onChange must not fire. */
    rerender() {
      quiet = true;
      for (const child of editor.children) child.dataset.key = '';
      syncDom();
      quiet = false;
    },
    focus: () => editor.focus(),
    /* Drop everything this instance attached outside its own editor element;
       listeners ON the editor die with the element itself. */
    destroy() {
      document.removeEventListener('selectionchange', onSelectionChange);
      if (editorParent) editorParent.removeEventListener('mousedown', onParentMouseDown);
      clearTimeout(tipHideTimer);
      tip.remove();
    }
  };
}

/* ---------------- demo document ---------------- */

const DEMO_DOC = [
  '# H1 Heading',
  '## H2 Heading',
  '### H3 Heading',
  '#### H4 Heading',
  '##### H5 Heading',
  '###### H6 Heading',
  '',
  '**bold** and *italic* and ***bold italic*** together.',
  '__underscore bold__ and _underscore italic_ too.',
  '~~strikethrough~~ and ==highlight== and `inline code`.',
  'Code protects its contents: `**not bold** and *not italic*`.',
  'Special characters are safe: 5 < 6 & "quotes" render *literally*, no broken HTML.',
  '',
  'A [link to Obsidian](https://obsidian.md) — click to open, hover for edit.',
  'Mixed inline: **bold with `code`** and *italic with ==mark==*.',
  '',
  '## Tables',
  '',
  '| Feature | Status | Notes |',
  '| --- | --- | --- |',
  '| Live render | **done** | no modes |',
  '| Tables | ==new== | click a row to edit |',
  '| Mermaid | ==new== | click diagram to edit |',
  '',
  '## Mermaid',
  '',
  '```mermaid',
  'graph LR',
  '  A[write markdown] --> B{renders live?}',
  '  B -->|yes| C[ship it]',
  '  B -->|no| D[click to edit]',
  '  D --> A',
  '```',
  '',
  '## Math',
  '',
  'Inline math flows with the text: $E = mc^2$ and $\\alpha + \\beta = \\gamma$ sit',
  'on the baseline, as does a fraction like $\\frac{1}{2}$ or $\\sqrt{x^2 + y^2}$.',
  '',
  'Display math gets its own centred block:',
  '',
  '$$',
  '\\int_{-\\infty}^{\\infty} e^{-x^2} \\, dx = \\sqrt{\\pi}',
  '$$',
  '',
  'A single-line block works too: $$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$$',
  '',
  'Matrices and cases render as well:',
  '',
  '$$',
  '\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}',
  '\\begin{cases} x > 0 \\\\ y < 1 \\end{cases}',
  '$$',
  '',
  'Math inside `$x^2$` a code span stays literal, and a bare $5 dollar sign is fine.',
  '',
  '## Unordered lists',
  '',
  '- item one',
  '- item two with **bold**',
  '  - nested item',
  '    - deeper nested item',
  '- back to top level',
  '',
  '## Ordered lists',
  '',
  '1. first item',
  '2. second item',
  '3. third with *italic*',
  '',
  'Tab nests an item and renumbers it, Shift+Tab lifts it back out:',
  '',
  '1. parent item',
  '1.1. nested child — typed directly, or made with Tab',
  '1.2. second child',
  '1.2.1. grandchild',
  '2. back to the top level',
  '',
  '## Task lists',
  '',
  '- [ ] open task — click the box',
  '- [x] completed task',
  '- [ ] task with `code` and **bold**',
  '',
  '## Blockquote',
  '',
  '> Quoted line with **bold** inside.',
  '> Second quoted line.',
  '',
  '## Code block',
  '',
  '```js',
  'function test() {',
  '  // **markdown must NOT render in here**',
  '  return 1 < 2 && "plain";',
  '}',
  '```',
  '',
  '---',
  '',
  'Below the horizontal rule. Click any line above to reveal its raw markdown; move away and it re-renders. ⌘B bold · ⌘I italic · ⌘Z undo · Tab indent.',
  ''
].join('\n');

/* ---------------- node test hook ---------------- */

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    escapeHtml, renderInline, renderActiveInline,
    mapRenderedToRaw, computeFenceFlags, emphasis, RE,
    parseOrdered, formatOrdered, orderedBlockRange, renumberOrdered,
    wikiSlug, parseCrossWiki, renderWikilink, WIKILINK_RE
  };
}

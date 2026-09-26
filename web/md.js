/* md.js — small, dependency-free Markdown → HTML renderer.
 * Supports: ATX headings, paragraphs, fenced code, inline code, bold/italic/strike,
 * links, images, autolinks, [[wikilinks]] (→ #/wiki/<slug>), blockquotes,
 * nested bullet/ordered lists, task lists, tables, horizontal rules.
 * All raw HTML is escaped: output is safe to inject with innerHTML.
 */
(function (global) {
  'use strict';

  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

  // languages whose SINGLE-LINE fenced blocks get pretty-printed at render
  // time (so table rows can stay one line in the editor). json is formatted
  // synchronously here; the rest are tagged data-pretty for the lazy
  // prettier upgrader in pretty.js. Multi-line blocks are left verbatim —
  // the author (or a captured payload) formatted them deliberately.
  const PRETTY = {
    js: 'babel', jsx: 'babel', javascript: 'babel',
    ts: 'typescript', tsx: 'typescript', typescript: 'typescript',
    html: 'html', css: 'css', scss: 'scss', less: 'less',
  };
  function codeBlock(lang, src) {
    let body = src;
    let extra = '';
    const key = (lang || '').toLowerCase();
    const single = body.trim() && !body.trim().includes('\n');
    if (single && (key === 'json' || key === 'jsonc')) {
      try { body = JSON.stringify(JSON.parse(body.trim()), null, 2); }
      catch (e) { extra = ` data-pretty-bad="1" title="invalid JSON: ${esc(String(e.message || e))}"`; }
    } else if (single && PRETTY[key]) {
      extra = ` data-pretty="${PRETTY[key]}"`;
    }
    return `<pre><code${lang ? ` class="lang-${esc(lang)}"` : ''}${extra}>${esc(body)}</code></pre>`;
  }

  function inline(text) {
    // protect inline code first
    const codes = [];
    text = text.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    text = esc(text);
    // images ![alt](src)
    text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, a, s) => `<img alt="${a}" src="${s}">`);
    // wikilinks [[slug|label]] / [[slug]]
    text = text.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_, t, l) => {
      const slug = slugify(t.trim());
      return `<a class="wikilink" href="#/wiki/${slug}" data-slug="${slug}">${l ? l.trim() : t.trim()}</a>`;
    });
    // links [text](url)
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, t, u) => {
      const ext = /^https?:\/\//i.test(u) ? ' target="_blank" rel="noopener"' : '';
      return `<a href="${u}"${ext}>${t}</a>`;
    });
    // autolinks
    text = text.replace(/(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g, (_, p, u) => `${p}<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
    // bold / italic / strike
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_]+)__/g, '<strong>$1</strong>');
    text = text.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>').replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    text = text.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    // hard break
    text = text.replace(/ {2,}$/gm, '<br>');
    // restore code
    text = text.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
    return text;
  }

  function render(src) {
    const lines = (src || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    const listStack = []; // {type, indent}

    const closeLists = (toIndent = -1) => {
      while (listStack.length && listStack[listStack.length - 1].indent > toIndent) {
        out.push(`</li></${listStack.pop().type}>`);
      }
    };

    while (i < lines.length) {
      let line = lines[i];

      // fenced code
      let m = /^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$/.exec(line);
      if (m) {
        closeLists();
        const fence = m[1], lang = m[2];
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
        i++; // closing fence
        out.push(codeBlock(lang, buf.join('\n')));
        continue;
      }

      // blank
      if (!line.trim()) { closeLists(); i++; continue; }

      // heading
      m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
      if (m) {
        closeLists();
        const lvl = m[1].length, txt = m[2];
        out.push(`<h${lvl} id="${slugify(txt)}">${inline(txt)}</h${lvl}>`);
        i++; continue;
      }

      // hr
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { closeLists(); out.push('<hr>'); i++; continue; }

      // blockquote
      if (/^\s*>/.test(line)) {
        closeLists();
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
        out.push(`<blockquote>${render(buf.join('\n'))}</blockquote>`);
        continue;
      }

      // table — body cells may hold block content (fenced code / mermaid /
      // multi-paragraph) via multi-line rows: open a ``` fence in a cell and
      // the row keeps consuming lines until the fence closes; the row ends on
      // the line where all fences are balanced (non-standard pipe-table
      // extension, as some code-review tools support). Header + separator stay single-line.
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        closeLists();
        const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        // splitter for body rows: a `|` inside a ``` fence or an inline
        // code span is cell content, not a cell boundary
        const splitRow = (src) => {
          const segs = [];
          let cur = '';
          for (let k = 0; k < src.length; k++) {
            if (src.startsWith('```', k)) { // copy through the closing fence
              const end = src.indexOf('```', k + 3);
              const stop = end === -1 ? src.length : end + 3;
              cur += src.slice(k, stop); k = stop - 1; continue;
            }
            if (src[k] === '`') { // copy a same-line inline code span whole
              const nl = src.indexOf('\n', k + 1);
              const end = src.indexOf('`', k + 1);
              if (end !== -1 && (nl === -1 || end < nl)) { cur += src.slice(k, end + 1); k = end; continue; }
            }
            if (src[k] === '|') { segs.push(cur); cur = ''; continue; }
            cur += src[k];
          }
          segs.push(cur);
          if (segs.length && !segs[0].trim()) segs.shift();
          if (segs.length && !segs[segs.length - 1].trim()) segs.pop();
          return segs.map((c) => c.trim());
        };
        const head = cells(line);
        const aligns = cells(lines[i + 1]).map((a) => a.startsWith(':') && a.endsWith(':') ? 'center' : a.endsWith(':') ? 'right' : 'left');
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i]) && (/\|\s*$/.test(lines[i]) || /```/.test(lines[i]))) {
          let src = lines[i++];
          let open = ((src.match(/```/g) || []).length % 2) === 1;
          while (open && i < lines.length) {
            const l = lines[i++];
            src += '\n' + l;
            if (((l.match(/```/g) || []).length % 2) === 1) open = false;
          }
          rows.push(splitRow(src));
        }
        let hasBlocks = false;
        let body = '';
        for (const r of rows) body += '<tr>' + r.map((c, k) => {
          // single-line fence in a cell: | ```json {"a": 1}``` | renders as a
          // (pretty-printed) code block, so rows stay one line in the editor
          const f = /^(`{3,})([\w+-]*)[ \t]+([^\n]*?)\s*\1$/.exec(c);
          let cell;
          if (f) { hasBlocks = true; cell = codeBlock(f[2], f[3]); }
          else if (/\n/.test(c)) { hasBlocks = true; cell = render(c); }
          else cell = inline(c);
          return `<td style="text-align:${aligns[k] || 'left'}">${cell}</td>`;
        }).join('') + '</tr>';
        let t = `<table${hasBlocks ? ' class="has-blocks"' : ''}><thead><tr>` + head.map((h, k) => `<th style="text-align:${aligns[k] || 'left'}">${inline(h)}</th>`).join('') + '</tr></thead><tbody>' + body;
        out.push(t + '</tbody></table>');
        continue;
      }

      // list item
      m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (m) {
        const indent = m[1].replace(/\t/g, '  ').length;
        const type = /^\d/.test(m[2]) ? 'ol' : 'ul';
        let content = m[3];
        let cls = '';
        const task = /^\[([ xX])\]\s+(.*)$/.exec(content);
        if (task) { cls = ' class="task"'; content = `<input type="checkbox" disabled${task[1] !== ' ' ? ' checked' : ''}> ${task[2]}`; }
        const top = listStack[listStack.length - 1];
        if (!top || indent > top.indent) {
          listStack.push({ type, indent });
          out.push(`<${type}><li${cls}>`);
        } else {
          closeLists(indent);
          const cur = listStack[listStack.length - 1];
          if (cur && cur.indent === indent) {
            if (cur.type !== type) { out.push(`</li></${cur.type}>`); listStack.pop(); listStack.push({ type, indent }); out.push(`<${type}><li${cls}>`); }
            else out.push(`</li><li${cls}>`);
          } else { listStack.push({ type, indent }); out.push(`<${type}><li${cls}>`); }
        }
        out.push(task ? content.replace(/> (.*)$/, (_, rest) => `> ${inline(rest)}`) : inline(content));
        i++;
        // lazy continuation lines (indented, non-list)
        while (i < lines.length && lines[i].trim() && !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i]) && /^\s{2,}/.test(lines[i]) && !/^\s*(`{3,}|~{3,})/.test(lines[i])) {
          out.push(' ' + inline(lines[i].trim())); i++;
        }
        continue;
      }

      // paragraph
      closeLists();
      const buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !/^(#{1,6})\s|^\s*(`{3,}|~{3,})|^\s*>|^(\s*)([-*+]|\d+[.)])\s+|^\s*\|.*\|\s*$/.test(lines[i]) && !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])) {
        buf.push(lines[i++]);
      }
      out.push(`<p>${inline(buf.join('\n'))}</p>`);
    }
    closeLists();
    return out.join('\n');
  }

  function outline(src) {
    const res = [];
    for (const l of (src || '').split('\n')) {
      const m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(l);
      if (m) res.push({ level: m[1].length, text: m[2], id: slugify(m[2]) });
    }
    return res;
  }

  global.MD = { render, inline, outline, slugify, esc };
})(window);

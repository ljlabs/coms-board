/* pretty.js — lazy prettier support for coms-board.
 *
 * md.js tags SINGLE-LINE fenced code blocks of formatter-known languages
 * (js/ts/html/css/…) with data-pretty="<parser>"; json is formatted
 * synchronously in md.js and never reaches here. This module lazily loads
 * the vendored prettier standalone bundle (~1.8MB across 6 files, fetched
 * only when a page actually contains a data-pretty block, never from a CDN)
 * and rewrites each block's text to its pretty-printed form.
 *
 *   PRETTY.has(root)     → does this subtree hold an unformatted block?
 *   PRETTY.upgrade(root) → load if needed, format each block in place.
 *                          Resolves to the count done.
 *
 * A block that fails to format keeps its original text and gets
 * data-pretty-bad + a tooltip carrying the parse error — a visible signal
 * (also used by agents) that the snippet is not valid for its language.
 * A load failure degrades to the plain unformatted block.
 */
(function (global) {
  'use strict';

  const FILES = [
    '/vendor/prettier/standalone.js',
    '/vendor/prettier/babel.js',
    '/vendor/prettier/estree.js',
    '/vendor/prettier/typescript.js',
    '/vendor/prettier/html.js',
    '/vendor/prettier/postcss.js',
  ];
  const SEL = 'pre > code[data-pretty]';
  let loading = null;

  function load() {
    if (!loading) {
      loading = FILES.reduce((p, src) => p.then(() => new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = () => reject(new Error('could not load ' + src));
        document.head.appendChild(s);
      })), Promise.resolve());
    }
    return loading;
  }

  const has = (root) => !!(root && root.querySelector && root.querySelector(SEL));

  async function upgrade(root) {
    const blocks = root && root.querySelectorAll ? [...root.querySelectorAll(SEL)] : [];
    if (!blocks.length) return 0;
    try { await load(); } catch (e) {
      console.warn('[coms] prettier unavailable, leaving code blocks as-is:', e.message);
      return 0;
    }
    let done = 0;
    for (const code of blocks) {
      const parser = code.dataset.pretty;
      code.removeAttribute('data-pretty');
      if (!code.isConnected) continue;
      try {
        const out = await global.prettier.format(code.textContent, {
          parser,
          plugins: Object.values(global.prettierPlugins || {}),
          printWidth: 80,
        });
        code.textContent = out.replace(/\n$/, '');
      } catch (err) {
        code.setAttribute('data-pretty-bad', '1');
        code.title = 'format error: ' + String((err && err.message) || err).split('\n')[0];
      }
      done++;
    }
    return done;
  }

  global.PRETTY = {
    load, has, upgrade,
    get ready() { return !!global.prettier; },
  };
})(window);

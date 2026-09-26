/* mermaid.js — lazy mermaid support for coms-board.
 *
 * mermaid is 3.2MB, so it is NOT in index.html: it is fetched from
 * /vendor/mermaid.min.js the first time a page actually contains a
 * ```mermaid block, and never at all on pages that have none. It is vendored
 * locally rather than pulled from a CDN, so the dashboard keeps working with
 * no internet and makes no third-party request.
 *
 *   MMD.has(root)        → does this subtree hold an unrendered mermaid block?
 *   MMD.upgrade(root)    → load if needed, then replace each
 *                          <pre><code class="lang-mermaid"> (what md.js emits)
 *                          with the rendered SVG. Resolves to the count done.
 *   MMD.load()           → just load the library (the live editor renders its
 *                          own diagrams once window.mermaid exists).
 *   MMD.ready            → true once loaded.
 *
 * A diagram that does not parse is left as a visible error box rather than
 * disappearing, and a load failure degrades to the plain code block.
 */
(function (global) {
  'use strict';

  const SRC = '/vendor/mermaid.min.js';
  const SEL = 'pre > code.lang-mermaid';
  let loading = null;
  let seq = 0;

  function initialize() {
    global.mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      themeVariables: {
        primaryColor: '#ffe14d',
        primaryBorderColor: '#111111',
        primaryTextColor: '#111111',
        lineColor: '#111111',
        secondaryColor: '#63e6ff',
        tertiaryColor: '#f4f1e8',
        fontFamily: '"IBM Plex Mono", "JetBrains Mono", Menlo, Consolas, monospace',
      },
    });
  }

  function load() {
    if (global.mermaid) return Promise.resolve(global.mermaid);
    if (loading) return loading;
    loading = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = SRC;
      s.onload = () => {
        if (!global.mermaid) return reject(new Error('mermaid loaded but did not register'));
        try { initialize(); } catch (e) { /* defaults are fine */ }
        resolve(global.mermaid);
      };
      s.onerror = () => reject(new Error('could not load ' + SRC));
      document.head.appendChild(s);
    });
    return loading;
  }

  const has = (root) => !!(root && root.querySelector && root.querySelector(SEL));

  const box = (inner, bad) =>
    `<div class="mermaid-box${bad ? ' mermaid-bad' : ''}">${inner}</div>`;

  async function upgrade(root) {
    const blocks = root && root.querySelectorAll ? [...root.querySelectorAll(SEL)] : [];
    if (!blocks.length) return 0;
    try { await load(); } catch (e) {
      console.warn('[coms] mermaid unavailable, leaving code blocks as-is:', e.message);
      return 0;
    }
    let done = 0;
    for (const code of blocks) {
      const pre = code.parentElement;
      if (!pre || !pre.isConnected) continue;
      const src = code.textContent;
      const holder = document.createElement('div');
      try {
        const { svg } = await global.mermaid.render('cmmd' + (seq++), src);
        holder.innerHTML = box(svg, false);
      } catch (err) {
        holder.innerHTML = box('mermaid: ' + MD.esc(String((err && err.message) || err)), true);
      }
      pre.replaceWith(holder.firstElementChild);
      done++;
    }
    /* mermaid appends orphaned nodes to <body> for diagrams that fail */
    document.querySelectorAll('body > [id^="dcmmd"]').forEach((n) => n.remove());
    return done;
  }

  global.MMD = {
    load, has, upgrade,
    get ready() { return !!global.mermaid; },
  };
})(window);

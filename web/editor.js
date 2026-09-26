/* editor.js — coms-board's wrapper around the live markdown engine.
 *
 * ONE pane. There is no preview pane and no edit/split/preview modes: the
 * document renders as you type, and only the line the caret sits on reveals
 * its raw markdown (Obsidian-style live preview). The engine is in web/markdown-editor.js.
 * That engine is implemented in web/markdown-editor.js. Keep application-specific behavior here.
 *
 *   const ed = new MdEditor(containerEl, {
 *     value, placeholder, onSave, onChange, minHeight, wikilinks, knownSlugs });
 *   ed.value / ed.setValue(v) / ed.setKnownSlugs(set) / ed.focus() / ed.destroy()
 *
 * Wikilinks are on by default: [[page]] renders as a link, styled as missing
 * when the slug is not in knownSlugs, and a click routes to #/wiki/<slug>
 * instead of opening a tab.
 *
 * Keys: ⌘/Ctrl+S or ⌘/Ctrl+Enter save · ⌘B bold · ⌘I italic · ⌘Z undo ·
 * Tab / Shift+Tab nest a list item (ordered outlines renumber themselves).
 */
(function (global) {
  'use strict';

  /* Live instances. app.js re-renders whole routes with innerHTML, which
     detaches an editor's element without telling it — and each engine
     instance holds a document-level selectionchange listener plus a .linktip
     node on <body>, so those would pile up. Every new editor sweeps the
     detached ones first. */
  const LIVE = new Set();
  function sweep() {
    for (const ed of [...LIVE]) {
      if (!document.body.contains(ed.el)) { LIVE.delete(ed); try { ed.handle.destroy(); } catch (e) { /* already gone */ } }
    }
  }

  class MdEditor {
    constructor(el, opts = {}) {
      sweep();
      this.el = el;
      this.opts = opts;
      this._known = opts.knownSlugs || null;
      this._build();
      LIVE.add(this);
    }

    _build() {
      const el = this.el;
      el.classList.add('mde');
      el.innerHTML = '';

      const bar = document.createElement('div');
      bar.className = 'mde-bar';
      this.count = document.createElement('span');
      this.count.className = 'mde-count';
      bar.appendChild(this.count);
      const hint = document.createElement('span');
      hint.className = 'mde-hint';
      hint.textContent = 'live markdown · ⌘B bold · ⌘I italic · Tab nests · ⌘S save';
      bar.appendChild(hint);

      /* PREVIEW: the saved page's own rendering (md.js + app.css .md), so
         what you see here is what the wiki/ticket/answer will look like.
         The live editor renders markdown as you type but cannot show a
         mermaid diagram while you are still inside the fence, which is what
         this state is for. */
      this.pvBtn = document.createElement('button');
      this.pvBtn.type = 'button';
      this.pvBtn.className = 'mde-preview-btn';
      this.pvBtn.textContent = 'PREVIEW';
      this.pvBtn.title = 'Preview the rendered result (Esc to come back)';
      this.pvBtn.addEventListener('click', () => this.togglePreview());
      bar.appendChild(this.pvBtn);

      if (this.opts.onSave) {
        const save = document.createElement('button');
        save.type = 'button';
        save.className = 'mde-save';
        save.textContent = 'SAVE';
        save.title = 'Save (Ctrl/Cmd+S)';
        save.addEventListener('click', () => this.opts.onSave(this.value));
        bar.appendChild(save);
      }
      el.appendChild(bar);

      this.doc = document.createElement('div');
      this.doc.className = 'mde-doc';
      this.doc.contentEditable = 'true';
      this.doc.spellcheck = false;
      this.doc.setAttribute('autocapitalize', 'off');
      this.doc.setAttribute('autocorrect', 'off');
      if (this.opts.minHeight) this.doc.style.setProperty('--mde-min', this.opts.minHeight);
      el.appendChild(this.doc);

      /* the preview surface: same renderer and same .md styles as the read
         view, so it is the final visualisation and not a second opinion */
      this.pv = document.createElement('div');
      this.pv.className = 'mde-preview md';
      if (this.opts.minHeight) this.pv.style.setProperty('--mde-min', this.opts.minHeight);
      el.appendChild(this.pv);
      this.previewing = false;

      this.handle = initEditor({
        editor: this.doc,
        status: null,                       // status lives in our own bar
        doc: this.opts.value || '',
        focus: false,                       // callers focus explicitly
        wikilinks: this.opts.wikilinks !== false,
        resolveWikilink: (slug) => !this._known || this._known.has(slug),
        onChange: (text) => {
          this._status(text);
          if (this.opts.onChange) this.opts.onChange(text);
        },
        onLink: (href, e) => this._link(href, e),
      });
      this._status(this.opts.value || '');

      // ⌘S / ⌘Enter save, Esc leaves preview — on the wrapper, so they work
      // in the preview state too (where the contenteditable has no focus)
      this.el.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.previewing) { e.preventDefault(); this.setPreview(false); return; }
        const mod = e.ctrlKey || e.metaKey;
        if (!mod || !this.opts.onSave) return;
        if (e.key.toLowerCase() === 's' || e.key === 'Enter') {
          e.preventDefault();
          this.opts.onSave(this.value);
        }
      });
      this.el.tabIndex = -1;   // so Esc reaches us while the preview is up
    }

    /* ---- preview state: rendered result instead of the editable document */

    togglePreview() { this.setPreview(!this.previewing); }

    setPreview(on) {
      this.previewing = !!on;
      this.el.dataset.preview = this.previewing ? '1' : '';
      this.pvBtn.textContent = this.previewing ? 'EDIT' : 'PREVIEW';
      this.pvBtn.classList.toggle('on', this.previewing);
      if (!this.previewing) { this.pv.innerHTML = ''; this.focus(); return; }

      const text = this.value;
      this.pv.innerHTML = text.trim()
        ? MD.render(text)
        : '<p class="muted">nothing to preview yet</p>';
      // style [[links]] to pages that do not exist, like the read view does
      if (this._known) {
        this.pv.querySelectorAll('a.wikilink').forEach((a) => {
          if (!this._known.has(a.dataset.slug)) a.classList.add('missing');
        });
      }
      this.el.focus({ preventScroll: true });
      if (global.MMD && MMD.has(this.pv)) {
        MMD.upgrade(this.pv).then((n) => {
          // now that mermaid exists, the live editor can draw diagrams inline
          if (n) this.handle.rerender();
        }).catch(() => {});
      }
      if (global.PRETTY && PRETTY.has(this.pv)) {
        PRETTY.upgrade(this.pv).catch(() => {});
      }
    }

    /* A wikilink is internal: route it and tell the engine we handled it, so
       it neither opens a tab nor follows the ?doc= href it renders. */
    _link(href, e) {
      const a = e && e.target && e.target.closest ? e.target.closest('a') : null;
      const slug = a && a.dataset ? a.dataset.wikilink : null;
      if (!slug) return false;
      location.hash = `#/wiki/${slug}`;
      return true;
    }

    _status(text) {
      const words = (text.match(/\S+/g) || []).length;
      const lines = text.split('\n').length;
      this.count.textContent = `${words} words · ${lines} lines · ${text.length} chars`;
      const empty = !text.trim();
      this.el.dataset.empty = empty ? '1' : '';
      const first = this.doc.firstElementChild;
      if (first) {
        if (empty) first.dataset.placeholder = this.opts.placeholder || 'Write markdown…';
        else first.removeAttribute('data-placeholder');
      }
    }

    get value() { return this.handle.getText(); }
    setValue(v) {
      this.handle.setText(v == null ? '' : v);
      this._status(this.value);
      if (this.previewing) this.setPreview(true);   // refresh what's on screen
    }

    /* Re-resolve [[links]] after the set of existing pages is known/changes,
       so a formerly-missing link stops being styled as missing. */
    setKnownSlugs(set) { this._known = set; this.handle.rerender(); }

    focus() { this.handle.focus(); }
    destroy() { LIVE.delete(this); this.handle.destroy(); this.el.innerHTML = ''; }
  }

  global.MdEditor = MdEditor;
})(window);

/* Persisted ACP conversation: keyed messages, tool patches, and turn states. */
(function () {
  'use strict';
  const labels = {pending:'Queued',in_progress:'Running',completed:'Success',failed:'Failed',cancelled:'Cancelled',interrupted:'Interrupted',unknown:'Unknown'};
  const format = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const setText = (node, text) => { if (node.textContent !== String(text ?? '')) node.textContent = text ?? ''; };
  const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

  function highlightedJson(value, compact=false) {
    const source=JSON.stringify(value, null, compact?0:2);
    const token=/"(?:\\.|[^"\\])*"|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\b(?:true|false|null)\b/g;
    let html='',last=0,match;
    while ((match=token.exec(source))) {
      html+=escapeHTML(source.slice(last,match.index));
      const text=match[0];
      const type=text[0]==='"' ? (/^\s*:/.test(source.slice(token.lastIndex))?'key':'string') : /^(true|false|null)$/.test(text)?'literal':'number';
      html+=`<span class="chat-json-${type}">${escapeHTML(text)}</span>`;
      last=token.lastIndex;
    }
    return html+escapeHTML(source.slice(last));
  }

  function showToolPayload(node) {
    const view=node._toolView || 'output',mode=node._toolFormat || 'formatted';
    const pre=node.querySelector('.chat-tool-payload > pre');
    const fieldsNode=node.querySelector('.chat-tool-fields');
    const more=node.querySelector('.chat-tool-more');
    const shell=node._toolShell;
    const value=view==='input'?node._toolInput:node._toolOutputRaw;
    let text='',syntaxValue=null;
    if (view==='input') {
      if (value==null) text='Input unavailable from this backend.';
      else if (mode==='formatted' && shell && typeof value==='object' && !Array.isArray(value)) {
        const fields={};
        const command=value.command ?? value.cmd ?? value.argv;
        const cwd=value.cwd ?? value.workdir ?? value.working_directory;
        if (command!=null) fields.command=command;
        if (cwd!=null) fields.workdir=cwd;
        for (const [key,item] of Object.entries(value)) if (!['command','cmd','argv','cwd','workdir','working_directory'].includes(key)) fields[key]=item;
        text=JSON.stringify(fields,null,2);
        syntaxValue=fields;
      } else {
        text=typeof value==='string'?value:JSON.stringify(value, null,2);
        if (mode==='formatted' && value && typeof value==='object') syntaxValue=value;
      }
    } else if (mode==='formatted' && (shell || Array.isArray(value))) {
      text=node._toolOutputText || 'No output.';
    } else if (mode==='formatted' && value && typeof value==='object' && (typeof value.stdout==='string' || typeof value.stderr==='string' || typeof value.formatted_output==='string')) {
      text=node._toolOutputText || 'No output.';
    } else if (value==null) {
      text='Output unavailable from this backend.';
    } else if (typeof value==='string') {
      text=value;
    } else {
      text=JSON.stringify(value,null,2);
      if (mode==='formatted') syntaxValue=value;
    }
    if (mode==='formatted' && !shell && typeof text==='string' && !syntaxValue) {
      try {const parsed=JSON.parse(text);if (parsed && typeof parsed==='object') {syntaxValue=parsed;text=JSON.stringify(parsed,null,2);}} catch (_) {}
    }
    if (mode==='raw' && value && typeof value==='object') syntaxValue=value;
    const full=node._toolFull?.[view]===true,limited=text.length>12000;
    const visible=limited&&!full?text.slice(0,12000)+'\n…':text;
    const rows=mode==='formatted' && syntaxValue && !Array.isArray(syntaxValue) && (!limited || full) && Object.keys(syntaxValue).length>0;
    fieldsNode.hidden=!rows;pre.hidden=Boolean(rows);
    if (rows) {
      fieldsNode.innerHTML=Object.entries(syntaxValue).map(([key,item])=>`<div class="chat-tool-field"><span class="chat-json-key">${escapeHTML(key)}</span><pre>${typeof item==='string'?`<span class="chat-json-string">${escapeHTML(item)}</span>`:highlightedJson(item)}</pre></div>`).join('');
    } else if (syntaxValue && (!limited || full)) pre.innerHTML=highlightedJson(syntaxValue);
    else setText(pre,visible);
    node.querySelector('.chat-tool-content-blocks').hidden=view!=='output' || mode==='raw';
    more.hidden=!limited;
    more.textContent=full?'Show preview':`Show full content (${text.length.toLocaleString()} characters)`;
    more.onclick=()=>{node._toolFull[view]=!full;showToolPayload(node);};
    node.querySelectorAll('[data-tool-view]').forEach(button=>{
      const active=button.dataset.toolView===view;
      button.classList.toggle('is-active',active);button.setAttribute('aria-pressed',String(active));
    });
    node.querySelectorAll('[data-tool-format]').forEach(button=>{
      const active=button.dataset.toolFormat===mode;
      button.classList.toggle('is-active',active);button.setAttribute('aria-pressed',String(active));
    });
    node._copyText=text;
  }

  function contentBlocks(root, blocks, markdown=false) {
    root.replaceChildren();
    for (const block of blocks || []) {
      if (!block || typeof block !== 'object') continue;
      const item = document.createElement('div');
      if (block.type === 'content') { contentBlocks(item, [block.content],markdown); }
      else if (block.type === 'text') {
        if (markdown) item.innerHTML=MD.render(block.text || '');
        else {const pre=document.createElement('pre');pre.textContent=block.text || '';item.append(pre);}
      }
      else if (block.type === 'image' && /^image\/(png|jpeg|gif|webp)$/.test(block.mimeType || '') && /^[A-Za-z0-9+/=\s]+$/.test(block.data || '')) {
        const img=document.createElement('img');img.src=`data:${block.mimeType};base64,${block.data}`;img.alt='Image reported by the agent';item.append(img);
      } else if (block.type === 'diff') {
        const title=document.createElement('b');title.textContent=block.path || 'File change';item.append(title);
        for (const [label,value] of [['Before',block.oldText],['After',block.newText]]) {
          const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=label;
          const pre=document.createElement('pre');pre.textContent=value ?? '(new file)';details.append(summary,pre);item.append(details);
        }
      } else {
        const pre=document.createElement('pre');
        pre.textContent=block.type === 'terminal' ? `Terminal ${block.terminalId || ''}: output unavailable from this backend.` : format(block);
        item.append(pre);
      }
      root.append(item);
    }
  }

  function toolOutput(data) {
    const parts=[];
    const provided=Object.prototype.hasOwnProperty.call(data,'rawOutput');
    const output=data.rawOutput;
    let exit;
    if (provided) {
      if (output && typeof output==='object' && !Array.isArray(output)) {
        exit=output.exitCode ?? output.exit_code ?? output.exit ?? output.metadata?.exit;
        const streams=['stdout','stderr'].filter(key=>typeof output[key]==='string');
        if (streams.length) {
          for (const key of streams) if (output[key]) parts.push((streams.length>1?`${key}:\n`:'')+output[key]);
        } else if (typeof output.formatted_output==='string') {
          if (output.formatted_output) parts.push(output.formatted_output);
        } else if (typeof output.output==='string' || typeof output.metadata?.output==='string') {
          const text=output.output ?? output.metadata.output;
          if (text) parts.push(text);
        } else parts.push(format(output));
      } else if (output !== '') parts.push(format(output));
    }
    // Content arrays are authoritative snapshots; the backend merges them.
    for (const item of Array.isArray(data.content)?data.content:[]) {
      if (item?.type==='content' && item.content?.type==='text') {
        const text=item.content.text || '';
        if (text && !parts.includes(text)) parts.push(text);
      }
    }
    return {text:parts.join('\n\n'),provided:provided || Object.prototype.hasOwnProperty.call(data,'content'),exit};
  }

  function timeline(box, h) {
    const nodes=new Map(), logs=new Map();
    const diagnostics=document.createElement('details');diagnostics.className='chat-logs';
    diagnostics.innerHTML='<summary>Debug logs</summary><div class="chat-log-list"></div>';
    diagnostics.hidden=true;box.append(diagnostics);
    const empty=document.createElement('div');empty.className='chat-empty';empty.textContent='Start a conversation with the Coms Board orchestrator.';
    box.prepend(empty);

    function create(m) {
      const node=document.createElement('article');node.className='chat-message';node.dataset.messageId=m.id;node.dataset.kind=m.kind;
      if (m.kind==='tool_call') {
        node.classList.add('chat-tool');
        node.innerHTML='<details class="chat-tool-call"><summary class="chat-tool-summary"><svg class="chat-tool-icon" viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="3" width="15" height="14" rx="3"/><path d="m6 7 3 3-3 3m5 0h3"/></svg><span class="chat-tool-title"></span><svg class="chat-tool-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg></summary><div class="chat-tool-panel"><div class="chat-tool-panel-head"><div class="chat-tool-context"></div><button type="button" class="chat-tool-copy" aria-label="Copy tool data" title="Copy tool data"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6" y="3" width="10" height="11" rx="2"/><path d="M12 14v3H3V6h3"/></svg></button></div><div class="chat-tool-controls"><div class="chat-tool-tabs" role="group" aria-label="Tool data"><button type="button" data-tool-view="input">Input</button><button type="button" data-tool-view="output">Output</button></div><div class="chat-tool-tabs chat-tool-format" role="group" aria-label="Data format"><button type="button" data-tool-format="formatted">Formatted</button><button type="button" data-tool-format="raw">Raw</button></div></div><div class="chat-tool-payload"><pre></pre><div class="chat-tool-content-blocks"></div></div><div class="chat-tool-footer"><div class="chat-tool-workspace"></div><div class="chat-tool-result"><span class="chat-tool-result-icon" aria-hidden="true"></span><span class="chat-tool-status" role="status"></span><span class="chat-tool-exit"></span></div></div></div></details>';
        const fields=document.createElement('div');fields.className='chat-tool-fields';fields.hidden=true;node.querySelector('.chat-tool-payload').prepend(fields);
        const more=document.createElement('button');more.type='button';more.className='chat-tool-more';more.hidden=true;node.querySelector('.chat-tool-payload').insertBefore(more,node.querySelector('.chat-tool-content-blocks'));
        node.querySelector('.chat-tool-call').open=false;
        node._toolView='output';node._toolFormat='formatted';node._toolFull={input:false,output:false};
        node.querySelectorAll('[data-tool-view]').forEach(button=>button.onclick=()=>{node._toolView=button.dataset.toolView;showToolPayload(node);});
        node.querySelectorAll('[data-tool-format]').forEach(button=>button.onclick=()=>{node._toolFormat=button.dataset.toolFormat;showToolPayload(node);});
        const copy=node.querySelector('.chat-tool-copy');
        copy.onclick=async()=>{
          let label;
          try {await navigator.clipboard.writeText(node._copyText || '');label='Copied';}
          catch (_) {label='Copy unavailable';}
          copy.setAttribute('aria-label',label);copy.title=label;
          setTimeout(()=>{copy.setAttribute('aria-label','Copy tool data');copy.title='Copy tool data';},1800);
        };
      } else if (m.kind==='error') {
        node.classList.add('chat-error');node.innerHTML='<div class="chat-role">Response status</div><div class="chat-text"></div>';
      } else if (m.kind==='plan') {
        node.classList.add('chat-plan');node.innerHTML='<div class="chat-role">Plan</div><div class="chat-plan-entries"></div>';
      } else if (m.kind==='thinking') {
        node.classList.add('chat-thinking');
        node.innerHTML='<details class="chat-thinking-section"><summary><svg class="chat-thinking-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4"/></svg><span>Thinking</span><small class="chat-thinking-state"></small></summary><div class="chat-thinking-body"><div class="chat-text"></div><div class="chat-content-blocks"></div></div></details>';
      } else {
        node.classList.add(m.role==='assistant'?'assistant':'user');
        node.innerHTML='<div class="chat-role"></div><div class="chat-text"></div><div class="chat-content-blocks"></div>';
        setText(node.querySelector('.chat-role'),m.role==='assistant'?'Orchestrator':'You');
        if (m.kind==='text' && ['user','assistant'].includes(m.role)) {
          node.classList.add('chat-text-message');
          const bubble=document.createElement('div');bubble.className='chat-bubble';
          bubble.append(...node.childNodes);node.append(bubble);
          const actions=document.createElement('div');actions.className='chat-message-actions';
          actions.innerHTML='<span class="chat-copy-feedback" role="status" aria-live="polite"></span><button type="button" class="chat-message-copy" aria-label="Copy message" title="Copy message"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6" y="3" width="10" height="11"/><path d="M12 14v3H3V6h3"/></svg></button>';
          node.append(actions);
          const copy=actions.querySelector('button'),feedback=actions.querySelector('.chat-copy-feedback');
          copy.onclick=async()=>{
            copy.disabled=true;
            clearTimeout(copy._feedbackTimer);
            let label;
            try {await navigator.clipboard.writeText(node._messageText || '');label='Copied';}
            catch (_) {label='Copy unavailable';}
            if (!node.isConnected) return;
            setText(feedback,label);copy.title=label;copy.setAttribute('aria-label',label);copy.disabled=false;
            copy.classList.toggle('is-copied',label==='Copied');
            copy._feedbackTimer=setTimeout(()=>{
              setText(feedback,'');copy.title='Copy message';copy.setAttribute('aria-label','Copy message');copy.classList.remove('is-copied');
            },1800);
          };
        }
      }
      update(node,m);return node;
    }

    function update(node,m) {
      const data=m.data || {};
      const signature=JSON.stringify([m.version,m.body,m.state,data]);
      if (node._signature===signature || h.hasSelection(node)) return;
      if (m.kind==='tool_call') {
        const state=m.state || data.status || 'pending';
        const input=data.rawInput;
        const command=input && typeof input==='object' ? input.command ?? input.cmd ?? input.argv : null;
        const cwd=input && typeof input==='object' ? input.cwd ?? input.workdir ?? input.working_directory : null;
        const output=toolOutput(data);
        const outcome=state==='completed' && output.exit!=null && Number(output.exit)!==0 ? 'failed' : state;
        const shell=command!=null || data.kind==='execute';
        const verbs=shell ? {pending:'Queued',in_progress:'Running',completed:'Ran',failed:'Failed to run'} : {pending:'Queued',in_progress:'Using',completed:'Used',failed:'Failed'};
        const name=command==null ? data.title || data.name || `Tool ${m.tool_call_id || ''}` : format(command);
        const title=node.querySelector('.chat-tool-title');
        setText(title,`${verbs[outcome] || labels[outcome] || 'Used'} ${name.replace(/\s+/g,' ').trim()}`);title.title=name;
        setText(node.querySelector('.chat-tool-context'),`${shell?'Shell':'Tool'}${data.workspace || cwd?` · ${data.workspace || cwd}`:''}`);
        setText(node.querySelector('.chat-tool-status'),labels[outcome] || outcome);
        setText(node.querySelector('.chat-tool-result-icon'),{completed:'✓',failed:'×',cancelled:'−',interrupted:'!'}[outcome] || '◷');
        node.dataset.state=outcome;
        node._toolInput=Object.prototype.hasOwnProperty.call(data,'rawInput')?input:null;
        node._toolOutputRaw=data.rawOutput!=null?data.rawOutput:Object.prototype.hasOwnProperty.call(data,'content')?data.content:null;
        node._toolOutputText=output.text || (state==='pending'||state==='in_progress'?'Waiting for output…':output.provided?'No output.':'Output unavailable from this backend.');
        node._toolShell=shell;
        showToolPayload(node);
        contentBlocks(node.querySelector('.chat-tool-content-blocks'),(Array.isArray(data.content)?data.content:[]).filter(item=>!(item?.type==='content'&&item.content?.type==='text') && !(item?.type==='terminal'&&output.provided)));
        setText(node.querySelector('.chat-tool-workspace'),`Workspace: ${data.workspace || cwd || 'unavailable'}${cwd && data.workspace && cwd!==data.workspace ? ` · Command cwd: ${cwd}` : ''}`);
        const exit=node.querySelector('.chat-tool-exit');exit.hidden=output.exit==null || Number(output.exit)===0;setText(exit,`Exit code: ${output.exit}`);
      } else if (m.kind==='plan') {
        const root=node.querySelector('.chat-plan-entries');root.replaceChildren();
        for (const entry of data.entries || []) {const line=document.createElement('div');line.textContent=`${entry.status || 'pending'} · ${entry.content || ''}`;root.append(line);}
      } else {
        const text=node.querySelector('.chat-text');
        if (m.role==='assistant') { if (node._body!==m.body || node._hasBlocks!==Boolean(data.blocks?.length)) text.innerHTML=data.blocks?.length?'':MD.render(m.body || ''); }
        else setText(text,m.body || '');
        node._body=m.body;
        node._messageText=m.body || (data.blocks || []).filter(block=>block?.type==='text').map(block=>block.text || '').join('\n\n');
        node._hasBlocks=Boolean(data.blocks?.length);
        if (node.querySelector('.chat-content-blocks')) contentBlocks(node.querySelector('.chat-content-blocks'),data.blocks || [],m.role==='assistant');
        node.dataset.state=m.state || 'completed';
        if (m.kind==='thinking') setText(node.querySelector('.chat-thinking-state'),m.state==='streaming'?'In progress':m.state==='completed'?'Complete':labels[m.state] || 'Complete');
      }
      node._signature=signature;
    }

    function sync(detail) {
      const scroll=h.scrollState(box);
      const visible=(detail.messages || []).filter(m=>m.kind!=='log');
      empty.hidden=visible.length>0;
      const present=new Set();let cursor=empty.nextElementSibling;
      for (const m of visible) {
        const key=String(m.id);present.add(key);
        let node=nodes.get(key);
        if (!node) {node=create(m);nodes.set(key,node);} else update(node,m);
        if (cursor!==node) box.insertBefore(node,cursor || diagnostics);
        cursor=node.nextElementSibling;
      }
      for (const [id,node] of nodes) if (!present.has(id)) {node.remove();nodes.delete(id);}
      diagnostics.hidden=!(detail.log_count || detail.logs?.length);
      setText(diagnostics.querySelector('summary'),`Debug logs (${detail.log_count || detail.logs?.length || 0})`);
      for (const m of detail.logs || []) {
        const key=String(m.id);let node=logs.get(key);
        if (!node) {node=document.createElement('pre');logs.set(key,node);diagnostics.querySelector('.chat-log-list').append(node);}
        if (!h.hasSelection(node)) setText(node,m.body || '');
      }
      h.restoreScroll(box,scroll);
      h.onScroll?.();
    }
    return {sync,diagnostics};
  }

  async function mount(h) {
    const {api,GET,paint,poll,ago,fail,refresh,esc}=h;
    const $=selector=>document.querySelector(selector);
    let projects=await GET('/api/runner/projects');
    let sessions=await GET('/api/runner/sessions');
    let rootWorkspace=(await GET('/api/runner/settings')).workspace || '';
    let activeId=h.sessionId || sessions[0]?.id;
    if (!activeId) {activeId=(await api('POST','/api/runner/sessions',{title:'New chat'})).id;sessions=await GET('/api/runner/sessions');}
    let detail=await GET(`/api/runner/sessions/${encodeURIComponent(activeId)}`);
    let sending=false, loadedLogs=false, requestSerial=0,streamHealthy=false,streamSource=null,renderFrame=null;
    const path=`/api/runner/sessions/${encodeURIComponent(activeId)}`;
    const expandedGroups=new Set([detail.project_id?`project:${detail.project_id}`:'root']);
    let pickerTarget=null,pickerData=null,folderRequestSerial=0,folderAction=null,folderSaving=false,projectWorkspace='',creatingProject=false;
    const sessionLink=s=>`<div class="chat-session-row"><a class="chat-session${s.id===activeId?' active':''}" href="#/orchestrator?session=${encodeURIComponent(s.id)}">${esc(s.title)}<small>${ago(s.updated_at)}</small></a><button class="chat-delete" type="button" data-delete-chat="${esc(s.id)}" aria-label="Delete chat ${esc(s.title)}" title="Delete chat">×</button></div>`;
    function groupMarkup(project) {
      const isRoot=!project,key=isRoot?'root':`project:${project.id}`;
      const items=sessions.filter(s=>String(s.project_id || '')===String(isRoot?'':project.id));
      const workspace=isRoot?rootWorkspace:(project.workspace || 'Uses root workspace');
      const heading=isRoot?'Unsorted':project.name;
      return `<details class="chat-project" data-group-key="${key}"${expandedGroups.has(key)?' open':''}><summary class="chat-project-head"><span class="chat-project-name">${esc(heading)}</span><span class="chat-project-count">${items.length}</span><button class="chat-project-add" type="button" data-new-chat-project="${isRoot?'':project.id}" aria-label="New chat in ${esc(heading)}" title="New chat in ${esc(heading)}"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4H4v12h12v-6M11 9l6-6 2 2-6 6-3 1z"/></svg></button>${!isRoot?`<button class="chat-delete" type="button" data-delete-project="${project.id}" aria-label="Delete project ${esc(project.name)}" title="Delete project">×</button>`:''}</summary><div class="chat-project-body"><div class="chat-project-workspace"><span><b>Workspace</b><small>${esc(workspace)}</small></span><button class="btn sm" type="button" data-pick-workspace="${isRoot?'root':project.id}">Choose folder</button></div><div class="chat-project-chats">${items.map(sessionLink).join('') || '<small class="muted">Use the new chat icon above to start a chat.</small>'}</div></div></details>`;
    }
    function projectList() {
      return groupMarkup(null)+projects.map(groupMarkup).join('');
    }
    paint(`<section class="chat-page" id="orch-page">
      <aside class="chat-sidebar" id="orch-sidebar"><div class="chat-side-head"><b>Chats</b><button class="btn sm" id="new-project">+ Project</button></div><button class="btn primary chat-new" id="new-chat">＋ New chat</button><div class="chat-projects">${projectList()}</div></aside>
      <div class="chat-main"><div class="chat-head"><button class="btn sm chat-menu" id="chat-menu" aria-label="Toggle chat sidebar">☰</button><div class="chat-title"><b id="chat-title"></b><small id="chat-subtitle"></small></div><label class="chat-move">Project<select id="move-project"><option value="">Unsorted</option>${projects.map(p=>`<option value="${p.id}"${String(detail.project_id || '')===String(p.id)?' selected':''}>${esc(p.name)}</option>`).join('')}</select></label><span class="chat-status badge" id="chat-status" role="status"></span></div><div id="chat-workspace" class="chat-workspace"><span></span><button type="button" class="btn sm" id="pick-current-workspace">Choose folder</button></div><div id="chat-connection" class="chat-connection" role="status" hidden></div><div class="chat-transcript-wrap"><div id="chat-messages" class="chat-transcript" aria-label="Conversation"></div><button type="button" class="btn sm chat-latest" id="chat-latest" hidden>Jump to latest ↓</button></div><form id="chat-form" class="chat-compose"><textarea id="chat-input" rows="2" aria-label="Message the orchestrator" placeholder="Message the orchestrator…"></textarea><button class="btn primary">Send</button></form></div>
      <dialog id="project-create" class="chat-project-dialog" aria-labelledby="project-create-title"><form id="project-create-form">
        <div class="chat-folder-head"><b id="project-create-title">Create project</b><button type="button" class="chat-delete" data-close-project aria-label="Close create project">×</button></div>
        <div class="chat-project-form-body"><label for="project-name">Project name</label><input id="project-name" name="name" placeholder="Project name" required maxlength="200" autocomplete="off">
          <label>Source folder</label><div class="chat-project-source"><div id="project-source-empty"><span>Add a folder on this computer</span><button type="button" class="btn sm" id="project-add-folder"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M8 4H3v12h14V7H9zM10 11h5m-2.5-2.5v5"/></svg>Add folder</button></div><div id="project-source-selected" hidden><span><b>Workspace folder</b><small id="project-source-path"></small></span><button type="button" class="btn sm" id="project-change-folder">Change</button><button type="button" class="chat-delete" id="project-remove-folder" aria-label="Remove source folder">×</button></div></div>
          <small id="project-workspace-hint"></small><p id="project-create-error" role="alert" hidden></p>
        </div><div class="chat-project-form-actions"><button type="button" class="btn" data-close-project>Cancel</button><button type="submit" class="btn primary" id="save-project">Create project</button></div>
      </form></dialog>
      <dialog id="chat-folder-dialog" class="chat-folder-dialog" aria-labelledby="chat-folder-title"><div class="chat-folder-head"><b id="chat-folder-title">Choose a workspace folder</b><button type="button" class="chat-delete" data-close-folder aria-label="Close folder picker">×</button></div><div class="chat-folder-nav"><button type="button" class="btn sm" id="folder-up" disabled>Up</button><span id="folder-current">Choose a drive</span><button type="button" class="btn sm" id="folder-new" disabled>+ New folder</button></div><div id="folder-entries" class="chat-folder-entries"></div><div class="chat-folder-actions"><span id="folder-note" role="status">Folders visible to this server are listed here.</span><button type="button" class="btn primary" id="folder-select" disabled>Select this folder</button></div></dialog>
      <dialog id="chat-folder-edit" class="chat-project-dialog chat-folder-edit" aria-labelledby="folder-edit-title"><form id="folder-edit-form"><div class="chat-folder-head"><b id="folder-edit-title">New folder</b><button type="button" class="chat-delete" data-close-folder-edit aria-label="Close folder action">×</button></div><div class="chat-project-form-body"><small id="folder-edit-path"></small><div id="folder-edit-name-field"><label for="folder-edit-name">Folder name</label><input id="folder-edit-name" name="name" required maxlength="255" autocomplete="off"></div><p id="folder-edit-description"></p><p id="folder-edit-error" role="alert" hidden></p></div><div class="chat-project-form-actions"><button type="button" class="btn" data-close-folder-edit>Cancel</button><button type="submit" class="btn primary" id="folder-edit-save">Create folder</button></div></form></dialog>
    </section>`);
    const box=$('#chat-messages');
    const activity=document.createElement('div');activity.id='chat-activity';activity.className='chat-activity';activity.hidden=true;
    activity.setAttribute('role','status');activity.setAttribute('aria-live','polite');
    activity.innerHTML='<div class="chat-loading-art" aria-hidden="true"></div><span>Thinking…</span>';
    box.parentElement.append(activity);
    const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
    let animation=null,animationLoading=false;
    function updateActivity() {
      const working=Boolean(sending || detail.busy || detail.active_turn_id);
      activity.hidden=!working;
      box.parentElement.classList.toggle('chat-is-working',working);
      const activeMessages=(detail.messages || []).filter(message=>message.turn_id===detail.active_turn_id);
      const latest=activeMessages[activeMessages.length-1];
      setText(activity.querySelector('span'),sending?'Sending…':latest?.kind==='text'?'Responding…':latest?.kind==='tool_call'?'Working…':'Thinking…');
      if (animation) {
        if (reducedMotion.matches) animation.goToAndStop(45,true);
        else if (working && !document.hidden) animation.play();
        else animation.pause();
      } else if (working && !animationLoading && window.lottie) {
        animationLoading=true;
        GET('/animations/chat-loading-neobrutalist.json').then(data=>{
          if (!box.isConnected || h.signal?.aborted) return;
          animation=window.lottie.loadAnimation({container:activity.querySelector('.chat-loading-art'),renderer:'svg',loop:true,autoplay:false,animationData:data,rendererSettings:{progressiveLoad:true,hideOnTransparent:true}});
          animation.setSubframe(false);
          animation.addEventListener('DOMLoaded',updateActivity);
          updateActivity();
        }).catch(()=>{ /* The text status remains visible if the artwork cannot load. */ });
      }
    }
    document.addEventListener('visibilitychange',updateActivity);
    document.addEventListener('selectionchange',queueStreamRender);
    reducedMotion.addEventListener('change',updateActivity);
    h.onCleanup?.(()=>{
      document.removeEventListener('visibilitychange',updateActivity);
      document.removeEventListener('selectionchange',queueStreamRender);
      reducedMotion.removeEventListener('change',updateActivity);
      animation?.destroy();
      streamSource?.close();
      if (renderFrame!==null) cancelAnimationFrame(renderFrame);
    });
    const groups=$('#orch-sidebar .chat-projects');
    function renderGroups() { groups.innerHTML=projectList(); }
    function renderProjectWorkspace() {
      $('#project-source-empty').hidden=Boolean(projectWorkspace);
      $('#project-source-selected').hidden=!projectWorkspace;
      setText($('#project-source-path'),projectWorkspace);
      setText($('#project-workspace-hint'),projectWorkspace?'Chats in this project will work in this folder.':`Without a source folder, chats use the root workspace: ${rootWorkspace}`);
    }
    groups.addEventListener('toggle',event=>{
      const group=event.target.closest('details.chat-project');if(!group)return;
      if (group.open) expandedGroups.add(group.dataset.groupKey);else expandedGroups.delete(group.dataset.groupKey);
    },true);
    const onScroll=()=>{$('#chat-latest').hidden=box.scrollHeight-box.scrollTop-box.clientHeight<24;};
    const transcript=timeline(box,{...h,onScroll});
    box.addEventListener('scroll',onScroll);
    $('#chat-latest').onclick=()=>{box.scrollTop=box.scrollHeight;onScroll();};

    function applyDetail() {
      const scroll=h.scrollState(box);
      updateActivity();
      transcript.sync(detail);
      h.restoreScroll(box,scroll);onScroll();
      const otherBusy=detail.runner_busy && !detail.busy;
      const states={completed:'ready',running:'working',failed:'failed',interrupted:'interrupted',cancelled:'cancelled',refused:'declined',limited:'limit reached'};
      const status=$('#chat-status');setText(status,detail.busy?'working':otherBusy?'runner busy':states[detail.state] || 'ready');
      status.className=`chat-status badge ${detail.busy || otherBusy?'in_progress':detail.state==='failed'||detail.state==='interrupted'?'blocked':'open'}`;
      $('#chat-form button').disabled=Boolean(detail.runner_busy || detail.busy || sending);
      setText($('#chat-title'),detail.title);
      setText($('#chat-subtitle'),detail.busy?'Orchestrator is responding':otherBusy?'Another chat is using the orchestrator':`ACP · ${detail.backend || 'default'} · ${detail.model || 'ACP default'}`);
      setText($('#chat-workspace span'),`Workspace: ${detail.workspace || 'unavailable'}${detail.workspace_error?` · ${detail.workspace_error}`:''}`);
      $('#move-project').value=detail.project_id || '';
      const current=sessions.find(s=>s.id===activeId);
      if (current && current.title!==detail.title) {current.title=detail.title;current.updated_at=detail.updated_at;renderGroups();}
    }
    function connectionError(error) { if (error.name==='AbortError') return;setText($('#chat-connection'),`Could not refresh this conversation: ${error.message}`);$('#chat-connection').hidden=false; }
    async function fetchDetail(includeLogs=loadedLogs) {
      const serial=++requestSerial;
      const next=await GET(path+(includeLogs?'?include_logs=1':''));
      if (serial!==requestSerial || !box.isConnected) return;
      detail=next;$('#chat-connection').hidden=true;applyDetail();
    }
    function queueStreamRender() {
      if (renderFrame!==null) return;
      renderFrame=requestAnimationFrame(()=>{renderFrame=null;if (box.isConnected) applyDetail();});
    }
    function startStream() {
      if (!window.EventSource || h.signal?.aborted) return;
      streamSource=new EventSource(path+'/events');
      function receive(event,snapshot) {
        if (!box.isConnected || h.signal?.aborted) return;
        try {
          const next=JSON.parse(event.data);
          ++requestSerial; // An older poll must not replace a newly streamed patch.
          if (snapshot) detail={...next,logs:loadedLogs?detail.logs:next.logs};
          else {
            const messages=new Map(detail.messages.map(message=>[String(message.id),message]));
            for (const id of next.removed || []) messages.delete(String(id));
            for (const message of next.messages || []) messages.set(String(message.id),message);
            detail={...detail,...next,messages:[...messages.values()].sort((a,b)=>a.id-b.id)};
          }
          streamHealthy=true;$('#chat-connection').hidden=true;queueStreamRender();
        } catch (error) {streamHealthy=false;connectionError(error);}
      }
      streamSource.addEventListener('snapshot',event=>receive(event,true));
      streamSource.addEventListener('update',event=>receive(event,false));
      streamSource.addEventListener('session-deleted',()=>{
        streamSource.close();streamHealthy=false;
        connectionError(new Error('This chat was deleted. Choose another chat.'));
      });
      streamSource.onerror=()=>{
        if (!box.isConnected || h.signal?.aborted) return;
        streamHealthy=false;
        setText($('#chat-connection'),'Reconnecting to live responses…');$('#chat-connection').hidden=false;
      };
      h.signal?.addEventListener('abort',()=>streamSource.close(),{once:true});
    }
    async function loadFolders(pathValue='') {
      const serial=++folderRequestSerial;
      const note=$('#folder-note'),entries=$('#folder-entries');setText(note,'Loading folders…');
      pickerData=null;$('#folder-select').disabled=true;$('#folder-up').disabled=true;$('#folder-new').disabled=true;
      try {
        const data=await GET(`/api/runner/workspace-folders${pathValue?`?path=${encodeURIComponent(pathValue)}`:''}`);
        if (serial!==folderRequestSerial || !$('#chat-folder-dialog').open) return;
        pickerData=data;
        setText($('#folder-current'),data.path || 'Choose a drive');$('#folder-up').disabled=!data.parent;
        const roots=(data.roots || []).map(root=>`<button type="button" class="chat-folder-entry" data-folder-open="${esc(root)}"><span>▸</span>${esc(root)}</button>`);
        const folders=(data.directories || []).map(folder=>`<div class="chat-folder-row"><button type="button" class="chat-folder-entry" data-folder-open="${esc(folder.path)}"><span>▸</span>${esc(folder.name)}</button><div class="chat-folder-row-actions"><button type="button" class="chat-folder-icon" data-folder-action="rename" data-folder-path="${esc(folder.path)}" data-folder-name="${esc(folder.name)}" aria-label="Rename folder ${esc(folder.name)}" title="${esc(folder.mutation_error || 'Rename folder')}"${folder.mutable?'':' disabled'}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 11 9-9 4 4-9 9-5 1zM12 4l4 4"/></svg></button><button type="button" class="chat-folder-icon chat-folder-remove" data-folder-action="delete" data-folder-path="${esc(folder.path)}" data-folder-name="${esc(folder.name)}" aria-label="Delete folder ${esc(folder.name)}" title="${esc(folder.mutation_error || 'Delete empty folder')}"${folder.mutable?'':' disabled'}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 5h14M7 5V3h6v2M5 5l1 12h8l1-12M8 8v6m4-6v6"/></svg></button></div></div>`);
        entries.innerHTML=roots.join('')+folders.join('')||'<div class="chat-folder-empty">No folders here.</div>';
        $('#folder-select').disabled=!data.path || data.selectable===false;
        $('#folder-new').disabled=!data.can_create;$('#folder-new').title=data.mutation_error || 'Create a folder here';
        setText(note,data.mutation_error || data.selection_error || (data.truncated?'Showing the first 500 folders. Open a folder to continue.':'Choose this folder or open one below.'));
        return data;
      } catch (error) {if(serial!==folderRequestSerial)return;pickerData=null;entries.innerHTML='<div class="chat-folder-empty">Folder list could not be loaded.</div>';setText(note,error.message || 'Folder list could not be loaded.');$('#folder-select').disabled=true;}
    }
    async function openFolderPicker(target) {
      pickerTarget=target;
      const project=projects.find(item=>String(item.id)===String(target));
      setText($('#chat-folder-title'),target==='new-project'?'Add a source folder':target==='root'?'Choose root workspace folder':`Choose ${project?.name || 'project'} workspace folder`);
      $('#chat-folder-dialog').showModal();
      const start=target==='new-project'?projectWorkspace || rootWorkspace:target==='root'?rootWorkspace:project?.workspace || rootWorkspace;
      if (!await loadFolders(start) && $('#chat-folder-dialog').open) await loadFolders('');
    }
    function openFolderAction(action,pathValue,name='') {
      folderAction={action,path:pathValue,name};const deleting=action==='delete';
      $('#folder-edit-form').reset();$('#folder-edit-error').hidden=true;
      setText($('#folder-edit-title'),{create:'New folder',rename:'Rename folder',delete:'Delete folder'}[action]);
      setText($('#folder-edit-path'),pathValue);$('#folder-edit-name-field').hidden=deleting;$('#folder-edit-name').disabled=deleting;
      $('#folder-edit-name').value=action==='rename'?name:'';
      setText($('#folder-edit-description'),deleting?'Permanently delete this empty folder? Folders with contents or assigned workspaces cannot be deleted.':action==='rename'?'Project workspace paths will be updated to the new name.':'Create a folder inside the location shown above.');
      setText($('#folder-edit-save'),{create:'Create folder',rename:'Rename folder',delete:'Delete folder'}[action]);
      $('#folder-edit-save').classList.toggle('chat-folder-delete-submit',deleting);
      $('#chat-folder-edit').showModal();if(!deleting)$('#folder-edit-name').focus();
    }
    function changedWorkspacePath(value,previous,next='') {
      if(!value)return value;
      const normalize=pathValue=>{const normalized=pathValue.replace(/\\/g,'/').replace(/\/$/,'');return /^[a-z]:\//i.test(normalized)||normalized.startsWith('//')?normalized.toLowerCase():normalized;};
      const current=normalize(value),source=normalize(previous);
      return current===source || current.startsWith(source+'/') ? next?next+value.slice(previous.length):'' : value;
    }
    $('#folder-edit-form').onsubmit=async event=>{
      event.preventDefault();if(folderSaving || !folderAction)return;
      const action={...folderAction},name=$('#folder-edit-name').value.trim();if(action.action!=='delete' && !name)return;
      folderSaving=true;$('#folder-edit-save').disabled=true;$('#folder-edit-error').hidden=true;
      let result;
      try {result=await api({create:'POST',rename:'PATCH',delete:'DELETE'}[action.action],'/api/runner/workspace-folders',{path:action.path,name,confirm:action.action==='delete'});}
      catch(error){setText($('#folder-edit-error'),error.message || 'Could not change folder.');$('#folder-edit-error').hidden=false;return;}
      finally {folderSaving=false;$('#folder-edit-save').disabled=false;}
      if(action.action==='rename')projectWorkspace=changedWorkspacePath(projectWorkspace,result.previous_path,result.path);
      if(action.action==='delete')projectWorkspace=changedWorkspacePath(projectWorkspace,result.deleted);
      renderProjectWorkspace();$('#chat-folder-edit').close();
      try {
        if(action.action==='rename') {projects=await GET('/api/runner/projects');rootWorkspace=(await GET('/api/runner/settings')).workspace || '';renderGroups();await fetchDetail();renderProjectWorkspace();}
        const loaded=await loadFolders(result.parent);
        if(loaded)setText($('#folder-note'),`${{create:'Created',rename:'Renamed',delete:'Deleted'}[action.action]} folder “${action.action==='delete'?action.name:name}”. ${loaded.mutation_error || loaded.selection_error || 'Choose this folder or open one below.'}`);
      } catch(error){setText($('#folder-note'),`Folder ${action.action==='create'?'created':action.action==='rename'?'renamed':'deleted'}, but the view could not refresh: ${error.message}`);}
    };
    $('#chat-folder-edit').addEventListener('click',event=>{if(event.target.closest('[data-close-folder-edit]') && !folderSaving)$('#chat-folder-edit').close();});
    $('#chat-folder-edit').addEventListener('cancel',event=>{if(folderSaving)event.preventDefault();});
    $('#chat-folder-edit').addEventListener('close',()=>{folderAction=null;});
    async function saveWorkspace(pathValue) {
      if (!pickerTarget) return;
      if (pickerTarget==='new-project') {
        projectWorkspace=pathValue;renderProjectWorkspace();$('#chat-folder-dialog').close();return;
      }
      const target=pickerTarget;
      $('#folder-select').disabled=true;
      try {
        if (target==='root') {
          await api('PUT','/api/runner/settings',{workspace:pathValue});rootWorkspace=pathValue;
        } else {
          await api('PATCH',`/api/runner/projects/${encodeURIComponent(target)}`,{workspace:pathValue});
          expandedGroups.add(`project:${target}`);
        }
        projects=await GET('/api/runner/projects');renderGroups();
        $('#chat-folder-dialog').close();pickerTarget=null;
        await fetchDetail();
      } catch (error) {setText($('#folder-note'),error.message || 'Could not save this workspace folder.');$('#folder-select').disabled=!pickerData?.path || pickerData?.selectable===false;}
    }
    applyDetail();box.scrollTop=box.scrollHeight;onScroll();
    transcript.diagnostics.addEventListener('toggle',()=>{if (transcript.diagnostics.open) {loadedLogs=true;fetchDetail(true).catch(connectionError);}});
    $('#new-chat').onclick=async()=>{try {const session=await api('POST','/api/runner/sessions',{title:'New chat',project_id:detail.project_id || null});location.hash=`#/orchestrator?session=${encodeURIComponent(session.id)}`;}catch(error){fail(error);}};
    $('#pick-current-workspace').onclick=()=>openFolderPicker(detail.project_id?String(detail.project_id):'root');
    $('#folder-up').onclick=()=>{if(pickerData?.parent)loadFolders(pickerData.parent);};
    $('#folder-new').onclick=()=>{if(pickerData?.can_create)openFolderAction('create',pickerData.path);};
    $('#folder-entries').onclick=event=>{
      const action=event.target.closest('[data-folder-action]');if(action && !action.disabled){openFolderAction(action.dataset.folderAction,action.dataset.folderPath,action.dataset.folderName);return;}
      const entry=event.target.closest('[data-folder-open]');if(entry)loadFolders(entry.dataset.folderOpen);
    };
    $('#folder-select').onclick=()=>{if(pickerData?.path)saveWorkspace(pickerData.path);};
    $('#chat-folder-dialog').addEventListener('click',event=>{if(event.target.closest('[data-close-folder]'))$('#chat-folder-dialog').close();});
    $('#chat-folder-dialog').addEventListener('close',()=>{pickerTarget=null;folderRequestSerial++;});
    $('#orch-sidebar').addEventListener('click',async event=>{
      const create=event.target.closest('[data-new-chat-project]');
      if (create) {event.preventDefault();event.stopPropagation();try {const session=await api('POST','/api/runner/sessions',{title:'New chat',project_id:create.dataset.newChatProject || null});location.hash=`#/orchestrator?session=${encodeURIComponent(session.id)}`;}catch(error){fail(error);}return;}
      const pick=event.target.closest('[data-pick-workspace]');
      if (pick) {event.preventDefault();event.stopPropagation();openFolderPicker(pick.dataset.pickWorkspace==='root'?'root':pick.dataset.pickWorkspace);return;}
      const button=event.target.closest('[data-delete-chat],[data-delete-project]');if (!button) return;event.preventDefault();
      try {
        if (button.dataset.deleteChat) {
          const id=button.dataset.deleteChat,chat=sessions.find(s=>s.id===id);
          if (!confirm(`Delete chat “${chat?.title || 'Untitled'}”? Its messages will be permanently removed.`)) return;
          await api('DELETE',`/api/runner/sessions/${encodeURIComponent(id)}`);sessions=sessions.filter(s=>s.id!==id);
          if (id===activeId) location.hash=sessions[0]?`#/orchestrator?session=${encodeURIComponent(sessions[0].id)}`:'#/orchestrator';else refresh();
        } else {
          const id=button.dataset.deleteProject,project=projects.find(p=>String(p.id)===id);
          if (!confirm(`Delete project “${project?.name || 'project'}”? Its chats will move to Unsorted.`)) return;
          await api('DELETE',`/api/runner/projects/${encodeURIComponent(id)}`);refresh();
        }
      } catch (error) {fail(error);}
    });
    $('#new-project').onclick=()=>{projectWorkspace='';$('#project-create-form').reset();$('#project-create-error').hidden=true;renderProjectWorkspace();$('#project-create').showModal();$('#project-name').focus();};
    $('#project-create').addEventListener('click',event=>{if(event.target.closest('[data-close-project]') && !creatingProject)$('#project-create').close();});
    $('#project-create').addEventListener('cancel',event=>{if(creatingProject)event.preventDefault();});
    $('#project-add-folder').onclick=$('#project-change-folder').onclick=()=>openFolderPicker('new-project');
    $('#project-remove-folder').onclick=()=>{projectWorkspace='';renderProjectWorkspace();};
    $('#project-create-form').onsubmit=async event=>{
      event.preventDefault();const name=$('#project-name').value.trim();if(!name || creatingProject)return;
      creatingProject=true;$('#save-project').disabled=true;$('#project-create-error').hidden=true;
      try {
        const project=await api('POST','/api/runner/projects',{name,workspace:projectWorkspace});
        projects.push(project);projects.sort((a,b)=>a.name.localeCompare(b.name));expandedGroups.add(`project:${project.id}`);renderGroups();
        const option=document.createElement('option');option.value=project.id;option.textContent=project.name;$('#move-project').append(option);
        $('#project-create').close();
      } catch (error) {setText($('#project-create-error'),error.message || 'Could not create project.');$('#project-create-error').hidden=false;}
      finally {creatingProject=false;$('#save-project').disabled=false;}
    };
    $('#move-project').onchange=async()=>{try {await api('PATCH',path,{project_id:$('#move-project').value || null});sessions=await GET('/api/runner/sessions');await fetchDetail();expandedGroups.add(detail.project_id?`project:${detail.project_id}`:'root');renderGroups();}catch(error){fail(error);}};
    $('#chat-menu').onclick=()=>$('#orch-page').classList.toggle('sidebar-open');
    $('#chat-form').onsubmit=async event=>{
      event.preventDefault();const input=$('#chat-input'),body=input.value.trim();
      if (!body || sending || detail.busy || detail.runner_busy) return;
      sending=true;applyDetail();
      try {
        const result=await api('POST',path+'/messages',{body});
        // Streaming and the send response share canonical IDs, avoiding duplicates.
        if (result.message && !detail.messages.some(m=>m.id===result.message.id)) detail.messages.push(result.message);
        if (input.value.trim()===body) input.value='';
        detail.busy=true;detail.runner_busy=true;applyDetail();
        await fetchDetail();
      } catch (error) {fail(error);if (error.name!=='AbortError') connectionError(error);}
      finally {sending=false;if (box.isConnected) applyDetail();}
    };
    $('#chat-input').onkeydown=event=>{if (event.key==='Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault();$('#chat-form').requestSubmit();}};
    startStream();
    poll(async()=>{
      try {
        if (!streamHealthy) await fetchDetail();
        else if (loadedLogs && transcript.diagnostics.open) {
          const next=await GET(path+'?include_logs=1');
          if (box.isConnected) {detail.logs=next.logs;detail.log_count=next.log_count;queueStreamRender();}
        }
      } catch(error) {connectionError(error);}
    },1400);
  }
  window.OrchestratorChat={mount};
})();

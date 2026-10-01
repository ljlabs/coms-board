/* Persisted ACP conversation: keyed messages, tool patches, and turn states. */
(function () {
  'use strict';
  const labels = {pending:'Queued',in_progress:'Running',completed:'Success',failed:'Failed',cancelled:'Cancelled',interrupted:'Interrupted',unknown:'Unknown'};
  const format = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const setText = (node, text) => { if (node.textContent !== String(text ?? '')) node.textContent = text ?? ''; };

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

  function pane(section, value, emptyText) {
    const text=value ?? emptyText;
    section._fullText=text;
    const pre=section.querySelector('pre'), button=section.querySelector('button');
    const full=section.dataset.full === 'true';
    setText(pre,full || text.length<=12000 ? text : text.slice(0,12000)+'\n…');
    button.hidden=text.length<=12000;
    button.textContent=full ? 'Show preview' : `Show full content (${text.length.toLocaleString()} characters)`;
    button.onclick=()=>{section.dataset.full=String(!full);pane(section,section._fullText,emptyText);};
  }

  function toolOutput(data) {
    const parts=[];
    const provided=Object.prototype.hasOwnProperty.call(data,'rawOutput');
    const output=data.rawOutput;
    let exit;
    if (provided) {
      if (output && typeof output==='object' && !Array.isArray(output)) {
        exit=output.exitCode ?? output.exit_code;
        const streams=['stdout','stderr'].filter(key=>typeof output[key]==='string');
        if (streams.length) {
          for (const key of streams) if (output[key]) parts.push((streams.length>1?`${key}:\n`:'')+output[key]);
        } else if (typeof output.formatted_output==='string') {
          if (output.formatted_output) parts.push(output.formatted_output);
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
        node.innerHTML='<details class="chat-tool-call"><summary class="chat-tool-summary"><svg class="chat-tool-icon" viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="3" width="15" height="14" rx="3"/><path d="m6 7 3 3-3 3m5 0h3"/></svg><span class="chat-tool-title"></span><svg class="chat-tool-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg></summary><div class="chat-tool-panel"><div class="chat-tool-panel-head"><span class="chat-tool-panel-label"></span><button type="button" class="chat-tool-copy" aria-label="Copy command and output" title="Copy command and output"><svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6" y="3" width="10" height="11" rx="2"/><path d="M12 14v3H3V6h3"/></svg></button></div><div class="chat-tool-terminal"><pre class="chat-tool-command"></pre><div class="chat-tool-output"><pre></pre><button type="button" class="chat-tool-more" hidden></button><div class="chat-content-blocks"></div></div></div><div class="chat-tool-footer"><details class="chat-tool-input"><summary>Details</summary><div class="chat-tool-workspace"></div><pre></pre><button type="button" class="chat-tool-more" hidden></button></details><div class="chat-tool-result"><span class="chat-tool-result-icon" aria-hidden="true"></span><span class="chat-tool-status" role="status"></span><span class="chat-tool-exit"></span></div></div></div></details>';
        node.querySelector('.chat-tool-call').open=['pending','in_progress'].includes(m.state || m.data?.status);
        const copy=node.querySelector('.chat-tool-copy');
        copy.onclick=async()=>{
          let label;
          try {await navigator.clipboard.writeText(node._copyText || '');label='Copied';}
          catch (_) {label='Copy unavailable';}
          copy.setAttribute('aria-label',label);copy.title=label;
          setTimeout(()=>{copy.setAttribute('aria-label','Copy command and output');copy.title='Copy command and output';},1800);
        };
      } else if (m.kind==='error') {
        node.classList.add('chat-error');node.innerHTML='<div class="chat-role">Response status</div><div class="chat-text"></div>';
      } else if (m.kind==='plan') {
        node.classList.add('chat-plan');node.innerHTML='<div class="chat-role">Plan</div><div class="chat-plan-entries"></div>';
      } else {
        node.classList.add(m.role==='assistant'?'assistant':'user');
        node.innerHTML='<div class="chat-role"></div><div class="chat-text"></div><div class="chat-content-blocks"></div>';
        setText(node.querySelector('.chat-role'),m.role==='assistant'?'Orchestrator':'You');
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
        setText(node.querySelector('.chat-tool-panel-label'),shell?'Shell':'Tool output');
        setText(node.querySelector('.chat-tool-status'),labels[outcome] || outcome);
        setText(node.querySelector('.chat-tool-result-icon'),{completed:'✓',failed:'×',cancelled:'−',interrupted:'!'}[outcome] || '◷');
        node.dataset.state=outcome;
        setText(node.querySelector('.chat-tool-workspace'),`Workspace: ${data.workspace || 'unavailable'}${cwd && cwd!==data.workspace ? ` · Command cwd: ${cwd}` : ''}`);
        const commandText=command==null?'':`${shell?'$ ':''}${format(command)}`;
        const cmd=node.querySelector('.chat-tool-command');cmd.hidden=command==null;setText(cmd,commandText);
        pane(node.querySelector('.chat-tool-input'),Object.prototype.hasOwnProperty.call(data,'rawInput')?format(input):null,'Input unavailable from this backend.');
        pane(node.querySelector('.chat-tool-output'),output.text || null,output.provided?'No output.':state==='pending'||state==='in_progress'?'Waiting for output…':'Output unavailable from this backend.');
        node.querySelector('.chat-tool-output').classList.toggle('empty',!output.text);
        node._copyText=(commandText?commandText+'\n':'')+output.text;
        const exit=node.querySelector('.chat-tool-exit');exit.hidden=output.exit==null || Number(output.exit)===0;setText(exit,`Exit code: ${output.exit}`);
        contentBlocks(node.querySelector('.chat-content-blocks'),(Array.isArray(data.content)?data.content:[]).filter(item=>!(item?.type==='content'&&item.content?.type==='text') && !(item?.type==='terminal'&&output.provided)));
      } else if (m.kind==='plan') {
        const root=node.querySelector('.chat-plan-entries');root.replaceChildren();
        for (const entry of data.entries || []) {const line=document.createElement('div');line.textContent=`${entry.status || 'pending'} · ${entry.content || ''}`;root.append(line);}
      } else {
        const text=node.querySelector('.chat-text');
        if (m.role==='assistant') { if (node._body!==m.body || node._hasBlocks!==Boolean(data.blocks?.length)) text.innerHTML=data.blocks?.length?'':MD.render(m.body || ''); }
        else setText(text,m.body || '');
        node._body=m.body;
        node._hasBlocks=Boolean(data.blocks?.length);
        if (node.querySelector('.chat-content-blocks')) contentBlocks(node.querySelector('.chat-content-blocks'),data.blocks || [],m.role==='assistant');
        node.dataset.state=m.state || 'completed';
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
    const projects=await GET('/api/runner/projects');
    let sessions=await GET('/api/runner/sessions');
    let activeId=h.sessionId || sessions[0]?.id;
    if (!activeId) {activeId=(await api('POST','/api/runner/sessions',{title:'New chat'})).id;sessions=await GET('/api/runner/sessions');}
    let detail=await GET(`/api/runner/sessions/${encodeURIComponent(activeId)}`);
    let sending=false, loadedLogs=false, requestSerial=0;
    const path=`/api/runner/sessions/${encodeURIComponent(activeId)}`;
    const sessionLink=s=>`<div class="chat-session-row"><a class="chat-session${s.id===activeId?' active':''}" href="#/orchestrator?session=${encodeURIComponent(s.id)}">${esc(s.title)}<small>${ago(s.updated_at)}</small></a><button class="chat-delete" type="button" data-delete-chat="${esc(s.id)}" aria-label="Delete chat ${esc(s.title)}" title="Delete chat">×</button></div>`;
    function projectList() {
      const group=id=>sessions.filter(s=>String(s.project_id || '')===String(id || '')).map(sessionLink).join('') || '<small class="muted">No chats</small>';
      return `<div class="chat-project"><div class="chat-project-head">Unsorted</div>${group(null)}</div>`+projects.map(p=>`<div class="chat-project"><div class="chat-project-head">${esc(p.name)}<button class="chat-delete" type="button" data-delete-project="${p.id}" aria-label="Delete project ${esc(p.name)}" title="Delete project">×</button></div>${group(p.id)}</div>`).join('');
    }
    paint(`<section class="chat-page" id="orch-page"><aside class="chat-sidebar" id="orch-sidebar"><div class="chat-side-head"><b>Chats</b><button class="btn sm" id="new-project">+ Project</button></div><button class="btn primary chat-new" id="new-chat">＋ New chat</button><div class="chat-projects">${projectList()}</div><div class="chat-project-create hidden" id="project-create"><input id="project-name" placeholder="Project name"><button class="btn sm" id="save-project">Create</button></div></aside><div class="chat-main"><div class="chat-head"><button class="btn sm chat-menu" id="chat-menu" aria-label="Toggle chat sidebar">☰</button><div class="chat-title"><b id="chat-title"></b><small id="chat-subtitle"></small></div><label class="chat-move">Project<select id="move-project"><option value="">Unsorted</option>${projects.map(p=>`<option value="${p.id}"${String(detail.project_id || '')===String(p.id)?' selected':''}>${esc(p.name)}</option>`).join('')}</select></label><span class="chat-status badge" id="chat-status" role="status"></span></div><div id="chat-workspace" class="chat-workspace"></div><div id="chat-connection" class="chat-connection" role="status" hidden></div><div class="chat-transcript-wrap"><div id="chat-messages" class="chat-transcript" aria-label="Conversation"></div><button type="button" class="btn sm chat-latest" id="chat-latest" hidden>Jump to latest ↓</button></div><form id="chat-form" class="chat-compose"><textarea id="chat-input" rows="2" aria-label="Message the orchestrator" placeholder="Message the orchestrator…"></textarea><button class="btn primary">Send</button></form></div></section>`);
    const box=$('#chat-messages');
    const onScroll=()=>{$('#chat-latest').hidden=box.scrollHeight-box.scrollTop-box.clientHeight<24;};
    const transcript=timeline(box,{...h,onScroll});
    box.addEventListener('scroll',onScroll);
    $('#chat-latest').onclick=()=>{box.scrollTop=box.scrollHeight;onScroll();};

    function applyDetail() {
      transcript.sync(detail);
      const otherBusy=detail.runner_busy && !detail.busy;
      const states={completed:'ready',running:'working',failed:'failed',interrupted:'interrupted',cancelled:'cancelled',refused:'declined',limited:'limit reached'};
      const status=$('#chat-status');setText(status,detail.busy?'working':otherBusy?'runner busy':states[detail.state] || 'ready');
      status.className=`chat-status badge ${detail.busy || otherBusy?'in_progress':detail.state==='failed'||detail.state==='interrupted'?'blocked':'open'}`;
      $('#chat-form button').disabled=Boolean(detail.runner_busy || detail.busy || sending);
      setText($('#chat-title'),detail.title);
      setText($('#chat-subtitle'),detail.busy?'Orchestrator is responding':otherBusy?'Another chat is using the orchestrator':`ACP · ${detail.backend || 'default'} · ${detail.model || 'ACP default'}`);
      setText($('#chat-workspace'),`Workspace: ${detail.workspace || 'unavailable'}${detail.workspace_error?` · ${detail.workspace_error}`:''}`);
      const current=sessions.find(s=>s.id===activeId);
      if (current && current.title!==detail.title) {current.title=detail.title;current.updated_at=detail.updated_at;$('#orch-sidebar .chat-projects').innerHTML=projectList();}
    }
    function connectionError(error) { if (error.name==='AbortError') return;setText($('#chat-connection'),`Could not refresh this conversation: ${error.message}`);$('#chat-connection').hidden=false; }
    async function fetchDetail(includeLogs=loadedLogs) {
      const serial=++requestSerial;
      const next=await GET(path+(includeLogs?'?include_logs=1':''));
      if (serial!==requestSerial || !box.isConnected) return;
      detail=next;$('#chat-connection').hidden=true;applyDetail();
    }
    applyDetail();box.scrollTop=box.scrollHeight;onScroll();
    transcript.diagnostics.addEventListener('toggle',()=>{if (transcript.diagnostics.open) {loadedLogs=true;fetchDetail(true).catch(connectionError);}});
    $('#new-chat').onclick=async()=>{try {const session=await api('POST','/api/runner/sessions',{title:'New chat',project_id:detail.project_id || null});location.hash=`#/orchestrator?session=${encodeURIComponent(session.id)}`;}catch(error){fail(error);}};
    $('#orch-sidebar').addEventListener('click',async event=>{
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
    $('#new-project').onclick=()=>{$('#project-create').classList.toggle('hidden');$('#project-name').focus();};
    $('#save-project').onclick=async()=>{const name=$('#project-name').value.trim();if (!name) return;try {await api('POST','/api/runner/projects',{name});refresh();}catch(error){fail(error);}};
    $('#move-project').onchange=async()=>{try {detail=await api('PATCH',path,{project_id:$('#move-project').value || null});sessions=await GET('/api/runner/sessions');$('#orch-sidebar .chat-projects').innerHTML=projectList();await fetchDetail();}catch(error){fail(error);}};
    $('#chat-menu').onclick=()=>$('#orch-page').classList.toggle('sidebar-open');
    $('#chat-form').onsubmit=async event=>{
      event.preventDefault();const input=$('#chat-input'),body=input.value.trim();
      if (!body || sending || detail.busy || detail.runner_busy) return;
      sending=true;applyDetail();
      try {
        const result=await api('POST',path+'/messages',{body});
        // The response carries the canonical stored ID; polling cannot duplicate it.
        if (result.message && !detail.messages.some(m=>m.id===result.message.id)) detail.messages.push(result.message);
        if (input.value.trim()===body) input.value='';
        detail.busy=true;detail.runner_busy=true;applyDetail();
        await fetchDetail();
      } catch (error) {fail(error);if (error.name!=='AbortError') connectionError(error);}
      finally {sending=false;if (box.isConnected) applyDetail();}
    };
    $('#chat-input').onkeydown=event=>{if (event.key==='Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault();$('#chat-form').requestSubmit();}};
    poll(async()=>{try {await fetchDetail();}catch(error){connectionError(error);}},1400);
  }
  window.OrchestratorChat={mount};
})();

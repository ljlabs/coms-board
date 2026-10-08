const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const readline = require('node:readline');

let server, browser, base, temp;
before(async () => {
  temp = mkdtempSync(path.join(tmpdir(), 'coms-browser-'));
  server = spawn(process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['tests/fixtures/browser_server.py'], {
    cwd: path.resolve(__dirname, '..'), env: { ...process.env, COMS_DB: path.join(temp, 'board.db') },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  let errors = '';
  server.stderr.on('data', chunk => { errors += chunk.toString(); });
  base = await new Promise((resolve, reject) => {
    const lines = readline.createInterface({ input: server.stdout });
    lines.once('line', port => { resolve(`http://127.0.0.1:${port}`); lines.close(); });
    server.once('exit', code => reject(new Error(`Fixture exited ${code}: ${errors}`)));
  });
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
});
after(async () => {
  if (browser) await browser.close();
  if (server) {
    const exited = new Promise(resolve => server.once('exit', resolve));
    try { await fetch(base + '/__test_shutdown', {method:'POST'}); } catch (_) { server.kill(); }
    await exited;
  }
  if (temp) rmSync(temp, { recursive: true, force: true });
});

async function pageFor(route, run, options = {}) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, ...options });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(base + '/#' + route);
    await page.waitForFunction(() => !document.querySelector('#app').hasAttribute('aria-busy'));
    await run(page);
    assert.deepEqual(errors, [], 'No uncaught browser errors');
  } finally { await page.close(); }
}

test('Agents discovers registered teammates and polls visible progress', async () => {
  await pageFor('/agents', async page => {
    const saved = await page.evaluate(() => fetch('/api/agents', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:'browser-expert',name:'Browser expert',role:'expert',description:'Evidence research'})}).then(r=>r.json()));
    assert.equal(saved.slug,'browser-expert');
    await page.waitForSelector('[data-agent-slug="browser-expert"]');
    let summary='Reading current sources', preview='First research update';
    await page.route('**/api/runner/status', route => route.fulfill({json:{active_count:1,waiting_count:0,generated_at:new Date().toISOString(),items:[{key:'test-run',agent_slug:'browser-expert',backend:'codex',state:'running',phase:'responding',summary,preview,ticket_id:1,ticket_title:'Browser regression ticket',response_chars:21,tool_calls:2,idle_seconds:1}]}}));
    await page.waitForFunction(() => document.querySelector('#agents-progress').textContent.includes('First research update'));
    summary='Comparing evidence';preview='Second research update';
    await page.waitForFunction(() => document.querySelector('#agents-progress').textContent.includes('Second research update'));
    assert.ok((await page.locator('#agents-progress').textContent()).includes('Comparing evidence'));
  });
});

test('Shared skill editor saves a version all agents can read', async () => {
  await pageFor('/skills/new', async page => {
    await page.locator('#skill-slug').fill('browser-sourcing');
    await page.locator('#skill-title').fill('Browser sourcing');
    await page.locator('#skill-body').fill('Read the primary source and record its date.');
    await page.locator('#skill-form button').click();
    await page.waitForFunction(() => location.hash==='#/skills/browser-sourcing' && document.querySelector('.md'));
    assert.ok((await page.locator('.md').textContent()).includes('primary source'));
    await page.locator('details summary').click();
    await page.locator('#skill-body').fill('Record the direct source URL too.');
    await page.locator('#skill-form button').click();
    await page.waitForFunction(() => document.querySelector('.pagehead .sub').textContent.includes('v2'));
    const saved=await page.evaluate(()=>fetch('/api/skills/browser-sourcing').then(r=>r.json()));
    assert.equal(saved.version,2);
    assert.equal(saved.body,'Record the direct source URL too.');
  });
});

test('ticket polls never duplicate stored prompts; refresh preserves drafts and scroll', async () => {
  await pageFor('/tickets/1', async page => {
    assert.equal(await page.locator('.runner-msg').count(), 60);
    await page.locator('#runner-input').fill('Unsent agent prompt');
    await page.locator('#ced .mde-doc').fill('Unsent comment');
    await page.evaluate(() => {
      window.scrollTo(0, 550);
      document.querySelector('#runner-messages').scrollTop = 120;
      document.querySelector('#savef').dataset.old = 'yes';
      document.querySelector('#savef').click();
    });
    await page.waitForFunction(() => document.querySelector('#savef') && !document.querySelector('#savef').dataset.old && !document.querySelector('#app').hasAttribute('aria-busy'));
    assert.equal(await page.locator('#runner-input').inputValue(), 'Unsent agent prompt');
    assert.match(await page.locator('#ced .mde-doc').innerText(), /Unsent comment/);
    assert.equal(await page.evaluate(() => window.scrollY), 550);
    assert.equal(await page.locator('#runner-messages').evaluate(el => el.scrollTop), 120);
    await page.waitForTimeout(2400);
    assert.equal(await page.locator('.runner-msg').count(), 60);
    assert.equal(await page.locator('#runner-messages').evaluate(el => el.scrollTop), 120);
  });
});

test('streaming chat preserves reading position and existing message nodes', async () => {
  await pageFor('/orchestrator?session=browser-chat', async page => {
    let polls = 0;
    let original;
    await page.route('**/api/runner/sessions/browser-chat', async route => {
      const response = await route.fetch();
      const detail = await response.json();
      detail.messages[detail.messages.length - 1].body += ' New streamed content.'.repeat(++polls * 10);
      await route.fulfill({ response, json: detail });
    });
    original = await page.locator('.chat-message').first().elementHandle();
    await page.locator('#chat-input').fill('Draft while reading');
    await page.locator('#chat-messages').evaluate(el => { el.scrollTop = 180; });
    await page.waitForTimeout(3200);
    assert.ok(polls >= 2);
    assert.equal(await original.evaluate(el => el.isConnected), true);
    assert.equal(await page.locator('#chat-messages').evaluate(el => el.scrollTop), 180);
    assert.equal(await page.locator('#chat-input').inputValue(), 'Draft while reading');
    await page.locator('#chat-messages').evaluate(el => { el.scrollTop = el.scrollHeight; });
    await page.waitForTimeout(1800);
    const gap = await page.locator('#chat-messages').evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop);
    assert.ok(gap < 2, 'Chat follows new output when at bottom');
  });
});

test('same-route chat refresh disposes its old poller', async () => {
  await pageFor('/orchestrator?session=browser-chat', async page => {
    for (let i = 0; i < 3; i++) {
      await page.locator('#new-project').click();
      await page.locator('#project-name').fill('Regression ' + i);
      await page.evaluate(() => document.querySelector('#save-project').click());
      await page.waitForFunction(() => !document.querySelector('#app').hasAttribute('aria-busy') && document.querySelector('#project-create').classList.contains('hidden'));
    }
    let polls = 0;
    page.on('request', req => { if (req.url().endsWith('/api/runner/sessions/browser-chat')) polls++; });
    await page.waitForTimeout(3100);
    assert.ok(polls <= 3, `Expected one poller, saw ${polls} requests`);
    await page.evaluate(() => { location.hash = '#/tickets/1'; });
    await page.waitForSelector('#runner-input');
    const before = polls;
    await page.waitForTimeout(1700);
    assert.equal(polls, before, 'Old chat poller stopped after navigation');
  });
});

test('completed tools use compact rows with keyboard expansion and a copyable shell panel', async () => {
  await pageFor('/orchestrator?session=browser-tools', async page => {
    const tool=page.locator('.chat-tool'),call=tool.locator('.chat-tool-call');
    assert.equal(await call.evaluate(el=>el.open),false);
    assert.ok((await tool.boundingBox()).height<=42,'Collapsed tool occupies one activity row');
    assert.equal(await tool.locator('.chat-tool-title').textContent(),'Ran ls');
    const summary=call.locator(':scope > summary');
    await summary.focus();
    await page.keyboard.press('Enter');
    assert.equal(await call.evaluate(el=>el.open),true);
    assert.equal(await tool.locator('.chat-tool-panel-label').textContent(),'Shell');
    assert.equal(await tool.locator('.chat-tool-command').textContent(),'$ ls');
    assert.equal(await tool.locator('.chat-tool-output pre').textContent(),'src\n');
    assert.equal(await tool.locator('.chat-tool-status').textContent(),'Success');
    assert.equal(await tool.locator('.chat-tool-input').evaluate(el=>el.open),false);
    assert.ok(!(await tool.innerText()).includes('Workspace:'),'Metadata is secondary');
    await page.context().grantPermissions(['clipboard-read','clipboard-write'],{origin:base});
    await tool.getByRole('button',{name:'Copy command and output',exact:true}).click();
    assert.equal((await page.evaluate(()=>navigator.clipboard.readText())).replace(/\r\n/g,'\n'),'$ ls\nsrc\n');
    await tool.locator('.chat-tool-input > summary').click();
    assert.match(await tool.locator('.chat-tool-workspace').innerText(),/Workspace:/);
    await summary.focus();await page.keyboard.press('Enter');
    assert.equal(await call.evaluate(el=>el.open),false);
  });
});

test('tool rows remain in order and status-only updates preserve node identity and expansion', async () => {
  await pageFor('/orchestrator?session=browser-tools', async page => {
    assert.deepEqual(await page.locator('#chat-messages > .chat-message').evaluateAll(nodes => nodes.map(n => n.dataset.kind)), ['text','text','tool_call','text','text','error']);
    const tool = page.locator('.chat-tool');
    const original = await tool.elementHandle();
    await tool.locator('details').first().evaluate(el => { el.open = true; });
    let polls = 0;
    await page.route('**/api/runner/sessions/browser-tools', async route => {
      const response = await route.fetch();
      const detail = await response.json();
      const entry = detail.messages.find(m => m.kind === 'tool_call');
      entry.state = 'failed';
      entry.data.status = 'failed';
      entry.data.rawOutput = { stderr:'<img src=x onerror=alert(1)>', exitCode:1 };
      entry.version += ++polls;
      await route.fulfill({response,json:detail});
    });
    await page.waitForFunction(() => document.querySelector('.chat-tool .chat-tool-status')?.textContent === 'Failed');
    assert.equal(await original.evaluate(el => el.isConnected), true);
    assert.equal(await tool.locator('details').first().evaluate(el => el.open), true);
    assert.equal(await tool.locator('img').count(), 0);
    assert.match(await tool.innerText(), /Exit code: 1/);
    assert.equal(await page.locator('.chat-error').count(), 1);
    assert.match(await page.locator('#chat-workspace').innerText(), /workspace/);
  });
});

test('running tools start expanded and keep a manual collapse during polling', async () => {
  await pageFor('/orchestrator?session=browser-tools', async page => {
    await page.route('**/api/runner/sessions/browser-tools',async route=>{
      const response=await route.fetch(),detail=await response.json();
      const entry=detail.messages.find(m=>m.kind==='tool_call');
      entry.state='in_progress';entry.data.status='in_progress';
      delete entry.data.rawOutput;entry.version++;
      await route.fulfill({response,json:detail});
    });
    await page.reload();
    const call=page.locator('.chat-tool-call');
    await call.waitFor();
    assert.equal(await call.evaluate(el=>el.open),true);
    assert.equal(await page.locator('.chat-tool-title').textContent(),'Running ls');
    assert.equal(await page.locator('.chat-tool-output pre').textContent(),'Waiting for output…');
    await call.locator(':scope > summary').click();
    await page.waitForTimeout(1600);
    assert.equal(await call.evaluate(el=>el.open),false);
  });
});

test('real ACP send creates separate assistant and tool bubbles and survives reload', async () => {
  await pageFor('/orchestrator?session=browser-tools', async page => {
    await page.locator('#new-chat').click();
    await page.waitForFunction(() => document.querySelector('#chat-title')?.textContent === 'New chat');
    await page.locator('#chat-input').fill('Run ls in your workspace');
    const accepted = page.waitForResponse(r => r.url().endsWith('/messages') && r.request().method()==='POST');
    await page.locator('#chat-form button').click();
    const response = await (await accepted).json();
    assert.ok(response.turn_id);
    assert.ok(response.message.id);
    await page.waitForFunction(() => document.querySelector('.chat-tool .chat-tool-status')?.textContent === 'Success' && document.querySelectorAll('.chat-message.assistant').length === 2);
    assert.deepEqual(await page.locator('#chat-messages > .chat-message').evaluateAll(nodes => nodes.map(n => n.dataset.kind)), ['text','text','tool_call','text']);
    assert.deepEqual(await page.locator('.chat-message.assistant .chat-text').allTextContents(), ['I’ll list the workspace.','Found src.']);
    const ids = await page.locator('#chat-messages > .chat-message').evaluateAll(nodes => nodes.map(n => n.dataset.messageId));
    await page.reload();
    await page.waitForSelector('.chat-tool');
    assert.deepEqual(await page.locator('#chat-messages > .chat-message').evaluateAll(nodes => nodes.map(n => n.dataset.messageId)), ids);
    assert.equal(await page.locator('.chat-message.user').count(), 1);
  });
});

test('empty and large tool output stays readable on mobile and selection survives polling', async () => {
  await pageFor('/orchestrator?session=browser-tools', async page => {
    let mode='empty';
    await page.route('**/api/runner/sessions/browser-tools', async route => {
      const response=await route.fetch(),detail=await response.json();
      const tool=detail.messages.find(m=>m.kind==='tool_call');
      tool.data.rawOutput=mode==='empty'?'':mode==='large'?'result '.repeat(4000):'Changed output';
      tool.version+=mode==='empty'?1:mode==='large'?2:3;
      await route.fulfill({response,json:detail});
    });
    const output=page.locator('.chat-tool-output');
    await page.locator('.chat-tool-call').evaluate(el=>{el.open=true;});
    await page.waitForFunction(()=>document.querySelector('.chat-tool-output pre')?.textContent==='No output.');
    mode='large';
    await page.waitForFunction(()=>!document.querySelector('.chat-tool-output button').hidden);
    await output.locator('button').click();
    assert.equal((await output.locator('pre').textContent()).length,28000);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await output.locator('pre').evaluate(el=>{const selection=getSelection(),range=document.createRange();range.selectNodeContents(el);selection.removeAllRanges();selection.addRange(range);});
    mode='changed';
    await page.waitForTimeout(1800);
    assert.equal((await output.locator('pre').textContent()).length,28000);
    await page.evaluate(()=>getSelection().removeAllRanges());
    await page.waitForFunction(()=>document.querySelector('.chat-tool-output pre')?.textContent==='Changed output');
  },{viewport:{width:390,height:844}});
});

test('recorded Codex formatted_output renders as command output without a missing-terminal warning', async () => {
  await pageFor('/orchestrator?session=browser-codex', async page => {
    const output=page.locator('.chat-tool-output');
    await page.locator('.chat-tool-call').evaluate(el=>{el.open=true;});
    assert.match(await output.locator('pre').first().textContent(),/README\.txt/);
    assert.ok(!(await output.innerText()).includes('formatted_output'));
    assert.ok(!(await output.innerText()).includes('output unavailable'));
    assert.equal(await page.locator('.chat-message.assistant').count(),2);
  });
});

test('slow route responses cannot overwrite a newer page', async () => {
  await pageFor('/', async page => {
    let requested;
    const started = new Promise(resolve => { requested = resolve; });
    await page.route('**/api/wiki', async route => {
      requested();
      await page.waitForTimeout(800);
      try { await route.continue(); } catch (_) { /* request was aborted by navigation */ }
    });
    await page.evaluate(() => { location.hash = '#/wiki'; });
    await started;
    await page.evaluate(() => { location.hash = '#/questions'; });
    await page.waitForSelector('h1');
    await page.waitForTimeout(1200);
    assert.equal(await page.locator('h1').innerText(), 'QUESTIONS');
  });
});

test('mobile chat fits the viewport and unsafe markdown URLs are blocked', async () => {
  await pageFor('/orchestrator?session=browser-chat', async page => {
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#chat-menu').click();
    assert.ok(await page.locator('#orch-page').evaluate(el => el.classList.contains('sidebar-open')));
    const rendered = await page.evaluate(() => MD.render('[bad](javascript:alert%281%29) [good](https://example.com)'));
    assert.ok(!rendered.includes('href="javascript:'));
    assert.ok(rendered.includes('href="https://example.com"'));
  }, { viewport: { width: 390, height: 844 } });
});

test('ticket list and board switch in both directions', async () => {
  await pageFor('/tickets', async page => {
    for (let i = 0; i < 2; i++) {
      await page.locator('#vb').click();
      await page.waitForSelector('.kanban');
      await page.locator('#vl').click();
      await page.waitForFunction(() => document.querySelector('#vl').classList.contains('primary') && !document.querySelector('.kanban'));
    }
  });
});

test('typing and scrolling during a slow refresh are retained at commit time', async () => {
  await pageFor('/tickets/1', async page => {
    let requested;
    const started=new Promise(resolve=>{requested=resolve;});
    await page.route('**/api/tickets/1',async route=>{
      if(route.request().method()!=='GET')return route.continue();
      requested();
      await page.waitForTimeout(900);
      await route.continue();
    });
    await page.evaluate(()=>document.querySelector('#savef').click());
    await started;
    await page.locator('#runner-input').fill('Typed while data loads');
    await page.evaluate(()=>window.scrollTo(0,700));
    await page.waitForFunction(()=>!document.querySelector('#app').hasAttribute('aria-busy'));
    assert.equal(await page.locator('#runner-input').inputValue(),'Typed while data loads');
    assert.equal(await page.evaluate(()=>window.scrollY),700);
  });
});

test('Codex model picker loads backend options and saves the selected model', async () => {
  await pageFor('/runner', async page => {
    await page.waitForFunction(() => document.querySelectorAll('#runner-model option').length >= 3 && /models available from codex/.test(document.querySelector('#model-note').textContent), { timeout: 15000 });
    const labels = await page.locator('#runner-model option').allTextContents();
    assert.ok(labels.includes('GPT 6 Luna'));
    assert.ok(labels.includes('GPT 6.1 Sol'));
    await page.locator('#runner-model').selectOption('gpt-6.1-sol');
    const saved = page.waitForResponse(r => r.url().endsWith('/api/runner/settings') && r.request().method() === 'PUT');
    await page.locator('#save-runner').click();
    assert.equal((await saved).status(), 200);
    const settings = await page.evaluate(() => fetch('/api/runner/settings').then(r => r.json()));
    assert.equal(settings.active_backend, 'codex');
    assert.equal(settings.model_codex, 'gpt-6.1-sol');
  });
});

test('saving Runner settings is not blocked by slow model discovery and shows activity', async () => {
  await pageFor('/runner', async page => {
    let begin, release;
    const started = new Promise(resolve => { begin=resolve; });
    const held = new Promise(resolve => { release=resolve; });
    await page.route('**/api/runner/models?backend=codex', async route => { begin(); await held; await route.continue(); });
    await page.evaluate(() => { document.querySelector('#refresh-models').click(); });
    await started;
    assert.equal(await page.locator('.model-spinner').count(), 1);
    assert.match(await page.locator('#model-note').textContent(), /Connecting to codex ACP/);
    const saved = page.waitForResponse(r => r.url().endsWith('/api/runner/settings') && r.request().method() === 'PUT');
    await page.locator('#save-runner').click();
    assert.equal((await saved).status(), 200);
    const settings = await page.evaluate(() => fetch('/api/runner/settings').then(r => r.json()));
    assert.equal(settings.active_backend, 'codex');
    assert.equal(settings.model_codex, 'gpt-6.1-sol');
    release();
  });
});

test('chat and project delete controls preserve chats and select a surviving chat', async () => {
  await pageFor('/orchestrator?session=browser-chat', async page => {
    page.on('dialog', dialog => dialog.accept());
    await page.locator('#new-project').click();
    await page.locator('#project-name').fill('Disposable project');
    await page.locator('#save-project').click();
    await page.waitForFunction(() => [...document.querySelectorAll('.chat-project-head')].some(e => e.textContent.includes('Disposable project')));
    const projectId=await page.locator('.chat-project').filter({hasText:'Disposable project'}).locator('[data-delete-project]').getAttribute('data-delete-project');

    await page.locator('#new-chat').click();
    await page.waitForFunction(() => location.hash.includes('session=') && document.querySelector('#move-project'));
    const currentHash=await page.evaluate(()=>location.hash);
    const currentId=new URLSearchParams(currentHash.split('?')[1]||'').get('session');
    const moved=page.waitForResponse(r=>r.url().endsWith(`/api/runner/sessions/${currentId}`)&&r.request().method()==='PATCH');
    await page.locator('#move-project').selectOption(projectId);
    assert.equal((await moved).status(),200);
    const projectDeleted=page.waitForResponse(r=>r.url().endsWith(`/api/runner/projects/${projectId}`)&&r.request().method()==='DELETE');
    await page.locator(`[data-delete-project="${projectId}"]`).click();
    assert.equal((await projectDeleted).status(),200);
    await page.waitForFunction(() => ![...document.querySelectorAll('.chat-project-head')].some(e => e.textContent.includes('Disposable project')));
    assert.ok(await page.locator('.chat-session-row').count() >= 2, 'project deletion keeps its chats');

    const chatDeleted=page.waitForResponse(r=>r.url().endsWith(`/api/runner/sessions/${currentId}`)&&r.request().method()==='DELETE');
    await page.locator(`[data-delete-chat="${currentId}"]`).click();
    assert.equal((await chatDeleted).status(),200);
    await page.waitForFunction(id => location.hash.includes('session=') && !location.hash.includes('session='+id), currentId);
    assert.equal(await page.locator('[data-delete-chat="browser-chat"]').count(),1);
  });
});

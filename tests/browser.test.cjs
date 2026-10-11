const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const readline = require('node:readline');

let server, browser, base, temp;

test('Runner omits live progress and Agents links execution sources', async () => {
  await pageFor('/runner', async page => {
    assert.equal(await page.locator('#runner-progress').count(), 0);
    assert.equal(await page.getByRole('heading', {name:'Live agent progress'}).count(), 0);
    await page.route('**/api/runner/status', route => route.fulfill({json:{
      generated_at:new Date().toISOString(),active_count:1,waiting_count:0,
      items:[{key:'source-test',agent_slug:'worker',agent_name:'Worker',state:'running',phase:'thinking',
        stage:'execute',ticket_id:1,ticket_title:'Delegated work',summary:'Thinking',backend:'codex',
        response_chars:0,tool_calls:0,idle_seconds:0,source:{kind:'agent',slug:'lead',name:'Lead'},
        schedule:{id:42,name:'Recurring report'}}]
    }}));
    await page.goto(base + '/#/agents');
    await page.waitForSelector('.progress-source');
    assert.equal(await page.locator('.progress-source a[href="#/agents/lead"]').textContent(), 'Lead');
    assert.equal(await page.locator('.progress-source a[href="#/schedule/42"]').textContent(), 'Recurring report');
  });
});

test('nested chat work shows circular activity while an idle chat allows steering', async () => {
  await pageFor('/orchestrator?session=browser-chat', async page => {
    let working=true;
    await page.route('**/api/runner/sessions', async route => {
      const data=await (await route.fetch()).json();
      await route.fulfill({json:data.map(row=>({...row,working:row.id==='browser-chat'&&working}))});
    });
    await page.route('**/api/runner/sessions/browser-chat', async route => {
      const data=await (await route.fetch()).json();
      await route.fulfill({json:{...data,working,activity:{active_count:working?1:0,waiting_count:0,queued_count:0}}});
    });
    await page.reload();
    await page.waitForSelector('.chat-session-row .activity-spinner');
    await page.waitForSelector('#chat-activity:not([hidden]) .activity-spinner');
    assert.equal(await page.locator('#chat-send').isEnabled(), true);
    assert.equal(await page.locator('#chat-stop').isVisible(), false);
    assert.match(await page.locator('#chat-activity').textContent(), /teammates/i);
    working=false;
    await page.waitForFunction(()=>document.querySelector('#chat-activity').hidden);
    await page.waitForFunction(()=>!document.querySelector('.chat-session-row .activity-spinner'));
  });
});

test('Schedule rows show circular activity and clear it when descendants finish', async () => {
  await pageFor('/schedule', async page => {
    let working=true;
    await page.route('**/api/scheduled-tasks', route=>route.fulfill({json:[{
      id:42,name:'Recurring report',agent_slug:'lead',workspace:'/workspace',timezone:'UTC',
      schedule_type:'interval',schedule_config:{every:1,unit:'hours'},status:'active',working
    }]}));
    await page.reload();
    await page.waitForSelector('[data-schedule-row] .activity-spinner');
    working=false;
    await page.waitForFunction(()=>!document.querySelector('[data-schedule-row] .activity-spinner'));
  });
});
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
    await page.route('**/api/runner/status', route => route.fulfill({json:{active_count:1,waiting_count:0,generated_at:new Date().toISOString(),items:[{key:'test-run',agent_slug:'browser-expert',backend:'codex',stage:'heartbeat',state:'running',phase:'responding',summary,preview,ticket_id:1,ticket_title:'Browser regression ticket',response_chars:21,tool_calls:2,idle_seconds:1}]}}));
    await page.waitForFunction(() => document.querySelector('#agents-progress').textContent.includes('First research update'));
    summary='Comparing evidence';preview='Second research update';
    await page.waitForFunction(() => document.querySelector('#agents-progress').textContent.includes('Second research update'));
    assert.ok((await page.locator('#agents-progress').textContent()).includes('Comparing evidence'));
    assert.equal(await page.locator('#agents-progress .badge').filter({hasText:'Heartbeat'}).count(), 1);
  });
});

test('Shared skill editor saves a version all agents can read', async () => {
  await pageFor('/skills/new', async page => {
    await page.locator('#skill-create-slug').fill('browser-sourcing');
    await page.locator('#skill-create-title').fill('Browser sourcing');
    await page.locator('#skill-create-description').fill('Use for primary source research.');
    await page.locator('#skill-create-body').fill('Read the primary source and record its date.');
    await page.locator('#skill-create-form button[type="submit"], #skill-create-form .form-actions button.ok').click();
    await page.waitForFunction(() => document.querySelector('.skills-detail-title .mono')?.textContent === 'browser-sourcing');
    await page.waitForSelector('.skill-markdown');
    assert.ok((await page.locator('.skill-markdown').textContent()).includes('primary source'));
    await page.locator('#skill-edit').click();
    await page.locator('#skill-editor').fill('Record the direct source URL too.');
    await page.locator('#skill-save-edit').click();
    await page.waitForFunction(() => document.querySelector('.skill-markdown')?.textContent.includes('direct source URL'));
    const saved=await page.evaluate(()=>fetch('/api/skills/browser-sourcing').then(r=>r.json()));
    assert.equal(typeof saved.version,'number');
    assert.equal(saved.body,'Record the direct source URL too.');
  });
});

test('Skills lists built-ins and saves searchable trigger words through reload', async () => {
  await pageFor('/skills/new', async page => {
    assert.ok(await page.locator('.skill-builtin').count() >= 6);
    await page.locator('#skill-create-slug').fill('browser-triggers');
    await page.locator('#skill-create-title').fill('Triggered procedure');
    await page.locator('#skill-create-description').fill('Check evidence.');
    await page.locator('#skill-create-triggers').fill('audit, peer review');
    await page.locator('#skill-create-body').fill('Verify current evidence.');
    await page.locator('#skill-create-form .form-actions button.ok').click();
    await page.waitForFunction(() => document.querySelector('.skills-detail-title .mono')?.textContent === 'browser-triggers');
    await page.waitForSelector('#skill-trigger-words');
    assert.equal(await page.locator('#skill-trigger-words').inputValue(), 'audit, peer review');
    await page.locator('#skill-trigger-words').fill('  Inspect, inspect, fact check ');
    await page.locator('#skill-save-triggers').click();
    await page.waitForFunction(() => document.querySelector('#skill-trigger-status').textContent === 'Trigger words saved.');
    await page.reload();
    await page.waitForSelector('#skill-trigger-words');
    assert.equal(await page.locator('#skill-trigger-words').inputValue(), 'Inspect, fact check');
    const saved = await page.evaluate(() => fetch('/api/skills/browser-triggers').then(r => r.json()));
    assert.deepEqual(saved.trigger_words, ['Inspect', 'fact check']);
    assert.ok(saved.body.includes('Verify current evidence.'));
    await page.locator('#skills-filter').fill('fact check');
    assert.equal(await page.locator('[data-skill-slug]:visible').count(), 1);
    await page.locator('#skill-enabled').uncheck();
    await page.reload();
    await page.waitForSelector('#skill-trigger-words');
    assert.equal(await page.locator('#skill-enabled').isChecked(), false);
    assert.equal(await page.locator('#skill-trigger-words').inputValue(), 'Inspect, fact check');
  });
});

test('Built-in skill files are read-only while triggers remain configurable', async () => {
  await pageFor('/skills/swarmboard', async page => {
    for (const id of ['skill-edit', 'skill-delete', 'skill-new-file', 'skill-new-folder', 'skill-entry-rename', 'skill-entry-move']) {
      assert.equal(await page.locator('#' + id).isDisabled(), true, id + ' is disabled');
    }
    assert.equal(await page.locator('[data-skill-file="SKILL.md"]').getAttribute('draggable'), 'false');
    assert.ok((await page.locator('.skills-readonly').textContent()).includes('read-only'));
    const original = await page.evaluate(() => fetch('/api/skills/swarmboard').then(r => r.json()));
    await page.locator('#skill-trigger-words').fill('board procedure');
    await page.locator('#skill-save-triggers').click();
    await page.waitForFunction(() => document.querySelector('#skill-trigger-status').textContent === 'Trigger words saved.');
    const saved = await page.evaluate(() => fetch('/api/skills/swarmboard').then(r => r.json()));
    assert.equal(saved.body, original.body);
    assert.deepEqual(saved.trigger_words, ['board procedure']);
    const blocked = await page.evaluate(() => fetch('/api/skills/swarmboard', {method:'DELETE'}).then(r => r.status));
    assert.equal(blocked, 400);
    await page.reload();
    await page.waitForSelector('#skill-trigger-words');
    assert.equal(await page.locator('#skill-trigger-words').inputValue(), 'board procedure');
    assert.equal(await page.locator('#skill-edit').isDisabled(), true);
  });
});

test('Skill files can be created, renamed and dragged between folders and the root without losing drafts', async () => {
  await pageFor('/skills/browser-sourcing', async page => {
    async function add(kind, name, parent='') {
      await page.locator(kind==='folder'?'#skill-new-folder':'#skill-new-file').click();
      await page.locator('#skill-entry-parent').selectOption(parent);
      await page.locator('#skill-entry-name').fill(name);
      await page.locator('#skill-entry-submit').click();
      await page.waitForFunction(() => !document.querySelector('#skill-entry-dialog').open);
    }
    await add('folder','a');
    await add('folder','b');
    await add('folder','empty','a');
    await add('file','notes.md','a');
    await page.locator('#skill-editor').fill('Keep this unsaved draft.');
    await page.locator('#skill-entry-rename').click();
    await page.locator('#skill-entry-name').fill('research.md');
    await page.locator('#skill-entry-submit').click();
    await page.waitForSelector('[data-skill-file="a/research.md"]');
    assert.equal(await page.locator('#skill-editor').inputValue(),'Keep this unsaved draft.');
    await page.locator('[data-skill-file="a/research.md"]').dragTo(page.locator('[data-skill-folder="b"]'));
    await page.waitForSelector('[data-skill-file="b/research.md"]');
    assert.equal(await page.locator('#skill-editor').inputValue(),'Keep this unsaved draft.');
    await page.locator('[data-skill-file="b/research.md"]').dragTo(page.locator('#skill-root-drop'));
    await page.waitForSelector('[data-skill-file="research.md"]');
    assert.equal(await page.locator('#skill-editor').inputValue(),'Keep this unsaved draft.');
    await page.locator('#skill-save-edit').click();
    await page.waitForSelector('.skill-markdown');
    const saved=await page.evaluate(()=>fetch('/api/skills/browser-sourcing?file=research.md').then(r=>r.json()));
    assert.equal(saved.content,'Keep this unsaved draft.');
    await page.locator('[data-skill-folder="a"]').dragTo(page.locator('[data-skill-folder="b"]'));
    await page.waitForSelector('[data-skill-folder="b/a/empty"]',{state:'attached'});
    await page.locator('[data-skill-folder="b/a"]').dragTo(page.locator('#skill-root-drop'));
    await page.waitForSelector('[data-skill-folder="a/empty"]',{state:'attached'});
    await add('file','nested.txt','a');
    await page.locator('#skill-editor').fill('Keep this draft while moving its parent.');
    await page.locator('[data-skill-folder="a"]').click();
    await page.locator('#skill-entry-move').click();
    await page.locator('#skill-entry-parent').selectOption('b');
    await page.locator('#skill-entry-submit').click();
    await page.waitForSelector('[data-skill-file="b/a/nested.txt"]');
    assert.equal(await page.locator('#skill-editor').inputValue(),'Keep this draft while moving its parent.');
    await page.locator('[data-skill-folder="b/a"]').dragTo(page.locator('#skill-root-drop'));
    await page.waitForSelector('[data-skill-file="a/nested.txt"]');
    assert.equal(await page.locator('#skill-editor').inputValue(),'Keep this draft while moving its parent.');
    await page.locator('#skill-save-edit').click();
    await page.waitForSelector('.skill-code-preview');
    assert.equal(await page.locator('[data-skill-file="SKILL.md"]').getAttribute('draggable'),'false');
    await page.reload();
    await page.waitForSelector('[data-skill-folder="a/empty"]',{state:'attached'});
  });
});

test('ticket uses the shared chat for execution, review, tools and durable follow-up replies', async () => {
  await pageFor('/tickets/1', async page => {
    assert.equal(await page.locator('#runner-chat .chat-compose').count(), 1);
    assert.equal(await page.locator('#chat-stop').count(), 0, 'Ticket chat does not expose orchestrator stop');
    assert.equal(await page.locator('.chat-text strong').filter({hasText:'Weather report'}).count(), 1);
    assert.match(await page.locator('.chat-role').filter({hasText:'Execution'}).first().textContent(), /Run #/);
    assert.match(await page.locator('.chat-role').filter({hasText:'Review'}).textContent(), /Review/);
    await page.locator('.chat-tool-summary').click();
    assert.match(await page.locator('.chat-tool-panel').textContent(), /temperature/);
    await page.locator('#chat-input').fill('How did it go?');
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => document.querySelector('#chat-status').textContent==='ready' && [...document.querySelectorAll('.chat-message.assistant .chat-role')].some(node=>node.textContent.includes('Conversation')));
    const count = await page.locator('.chat-message').count();
    await page.reload();
    await page.waitForSelector('.chat-role');
    assert.equal(await page.locator('.chat-message').count(), count);
    assert.equal(await page.locator('.chat-message.user .chat-text').filter({hasText:'How did it go?'}).count(), 1);
    const detail = await page.evaluate(()=>fetch('/api/tickets/1/runner').then(r=>r.json()));
    assert.equal(detail.runs.length, 1, 'Follow-ups do not repeat the scheduled execution');
  });
});

test('ticket polls never duplicate stored prompts; refresh preserves drafts and scroll', async () => {
  await pageFor('/tickets/1', async page => {
    const prompts = await page.locator('.chat-message.user').count();
    assert.ok(prompts >= 60);
    await page.locator('#chat-input').fill('Unsent agent prompt');
    await page.locator('#ced .mde-doc').fill('Unsent comment');
    await page.evaluate(() => {
      window.scrollTo(0, 550);
      document.querySelector('#chat-messages').scrollTop = 120;
      document.querySelector('#savef').dataset.old = 'yes';
      document.querySelector('#savef').click();
    });
    await page.waitForFunction(() => document.querySelector('#savef') && !document.querySelector('#savef').dataset.old && !document.querySelector('#app').hasAttribute('aria-busy'));
    assert.equal(await page.locator('#chat-input').inputValue(), 'Unsent agent prompt');
    assert.match(await page.locator('#ced .mde-doc').innerText(), /Unsent comment/);
    assert.equal(await page.evaluate(() => window.scrollY), 550);
    assert.equal(await page.locator('#chat-messages').evaluate(el => el.scrollTop), 120);
    await page.waitForTimeout(2400);
    assert.equal(await page.locator('.chat-message.user').count(), prompts);
    assert.equal(await page.locator('#chat-messages').evaluate(el => el.scrollTop), 120);
  });
});

test('ticket chat deep link brings the shared conversation into view', async () => {
  await pageFor('/tickets/1?chat=1', async page => {
    const top = await page.locator('#runner-chat').evaluate(node=>node.getBoundingClientRect().top);
    assert.ok(top >= -1 && top < 200, `Chat should be in view, top=${top}`);
  });
});

test('orchestrator stop preserves drafts and waits for canonical idle state', async () => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let releaseStop;
  try {
    let busy = true, markStopStarted;
    const stopStarted = new Promise(resolve => { markStopStarted = resolve; });
    await page.route('**/api/runner/sessions/browser-chat/events', route => route.abort());
    await page.route('**/api/runner/sessions/browser-chat', async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch();
      const detail = await response.json();
      detail.busy = busy;
      detail.runner_busy = busy;
      detail.active_turn_id = busy ? 'mock-busy-turn' : null;
      await route.fulfill({ response, json: detail });
    });
    await page.route('**/api/runner/sessions/browser-chat/stop', async route => {
      markStopStarted();
      await new Promise(resolve => { releaseStop = resolve; });
      busy = false;
      await route.fulfill({ json: { stopping: true } });
    });
    await page.goto(base + '/#/orchestrator?session=browser-chat');
    await page.waitForFunction(() => !document.querySelector('#app').hasAttribute('aria-busy'));
    // Busy canonical state disables Send; wait for its poll before exercising Stop.
    await page.waitForFunction(() => {
      const stop = document.querySelector('#chat-stop');
      return stop && !stop.hidden && document.querySelector('#chat-send').disabled;
    });
    await page.locator('#chat-input').fill('Keep this draft while stopping');
    await page.locator('#chat-stop').click();
    await stopStarted;
    assert.equal(await page.locator('#chat-stop').textContent(), 'Stopping…');
    assert.equal(await page.locator('#chat-stop').isDisabled(), true);
    assert.equal(await page.locator('#chat-send').isDisabled(), true);
    assert.equal(await page.locator('#chat-input').inputValue(), 'Keep this draft while stopping');
    releaseStop();
    await page.waitForFunction(() => document.querySelector('#chat-stop').hidden && !document.querySelector('#chat-send').disabled);
    assert.equal(await page.locator('#chat-input').inputValue(), 'Keep this draft while stopping');
  } finally {
    releaseStop?.();
    await page.close();
  }
  assert.deepEqual(errors, [], 'No uncaught browser errors');
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
    await page.waitForSelector('#chat-input');
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
    await page.locator('#chat-send').click();
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
    await page.locator('#chat-input').fill('Typed while data loads');
    await page.evaluate(()=>window.scrollTo(0,700));
    await page.waitForFunction(()=>!document.querySelector('#app').hasAttribute('aria-busy'));
    assert.equal(await page.locator('#chat-input').inputValue(),'Typed while data loads');
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

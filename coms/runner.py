"""ACP-backed ticket runner for Coms Board."""
from __future__ import annotations
import json, logging, os, shlex, shutil, sqlite3, subprocess, sys, threading, time, uuid
from pathlib import Path
from . import db
from .acp import AcpConnection
from .workspace import workspace_path

ROOT = Path(__file__).resolve().parents[1]


def board_servers(actor, ticket_id=None, run_id=None, readonly=False):
    env = {'COMS_DB': str(db.db_path().resolve()), 'COMS_AGENT': actor}
    if ticket_id is not None: env['COMS_TICKET_ID'] = str(ticket_id)
    if run_id is not None: env['COMS_RUN_ID'] = str(run_id)
    return [{'name': 'coms-board', 'command': sys.executable,
             'args': [str(ROOT / 'coms' / 'mcp.py')] + (['--readonly'] if readonly else []),
             'env': [{'name': k, 'value': v} for k, v in env.items()]}]

# Shared instructions are deliberately prepended to every execution agent's
# template and ticket-specific prompt. The board is the durable record; the
# configured workspace is not monitored for useful deliverables.
BASE_AGENT_SYSTEM_PROMPT = f"""You are an execution agent working through Coms Board. The Coms Board database is the system of record. Files you create or change in the workspace are not monitored, indexed, or automatically attached to the ticket, so a file by itself does not count as a delivered result.

Use the attached coms-board MCP tools for ALL board reads and writes. Use board_search before creating knowledge, wiki_put/wiki_append for durable documentation, questions_ask/questions_answer for Q&A, and ticket_link for references. Do not write board deliverables to local markdown files. You MUST call ticket_result for your assigned ticket with the delivered board IDs/links and verification before finishing. Without that tool call, the run fails and cannot advance to automatic review. If the tools are unavailable, report that blocker; do not substitute filesystem output or CLI writes.

Choose the board destination that matches the requested work:
- For reusable knowledge, research findings, procedures, or documentation, create or update a Coms Board wiki page with wiki_put or wiki_append. Search/list the wiki first to avoid duplicate pages.
- For an unresolved question, ask it on the board with questions_ask; for an existing question, contribute with questions_answer.
- For requested work that changes software or other workspace artifacts, make those changes in the configured workspace, then record the outcome and relevant paths in the ticket result/comment. Do not substitute an untracked file for a requested wiki entry or board answer.
- Use ticket_comment only for concise, durable updates the requester should see. ACP responses and tool output are private diagnostics; do not post routine progress.

Before finishing, make sure the requested deliverable is actually present in the appropriate board record (wiki page, question/answer, or ticket result/comment) and report its link or identifier. If the request is ambiguous about whether knowledge belongs in the wiki or a question, use the wiki for durable reference material and Q&A for a specific question needing an answer."""

def now():
    from .api import now as timestamp
    return timestamp()

def backend_command(name, settings):
    configured = settings.get(name + "_command", "").strip()
    if configured:
        return shlex.split(configured)
    if name == 'opencode': return [shutil.which('opencode') or 'opencode', 'acp']
    if name == 'codex': return [shutil.which('codex-acp') or 'codex-acp']
    if name == 'claude':
        adapter = shutil.which('claude-code-acp')
        return [adapter] if adapter else ['npx', '-y', '@zed-industries/claude-code-acp']
    return []

class Runner:
    def __init__(self, *, start=False):
        self.lock = threading.RLock(); self.active = {}; self.conns = {}; self.stopping = threading.Event()
        self.model_conns = {}; self.model_lock = threading.RLock()
        self.orchestrator_conn = None; self.orchestrator_conn_key = None
        self.orchestrator_context = {}; self.orchestrator_conn_lock = threading.RLock()
        self.orchestrator_busy = False
        self.orchestrator_session = None
        self.review_conns = {}
        self._lease = None
        self.thread = None
        if start: self.start()
    def start(self, workspace=None, *, warm=True):
        with self.lock:
            if self.thread and self.thread.is_alive(): return
            lease=open(str(db.db_path().resolve())+'.runner.lock','a+b')
            try:
                if os.name=='nt':
                    import msvcrt
                    if lease.seek(0,2)==0: lease.write(b'0'); lease.flush()
                    lease.seek(0); msvcrt.locking(lease.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                lease.close()
                raise RuntimeError('Another Coms Board runner already owns this database. Stop that server before starting another.')
            self._lease=lease
            try:
                if workspace is not None:
                    path=workspace_path({'workspace':workspace},create_default=False)
                    conn=db.connect()
                    conn.execute('INSERT OR REPLACE INTO runner_settings(key,value) VALUES(?,?)',('workspace',path))
                    conn.commit()
                conn=db.connect()
                active_settings=self.settings()
                active_backend=active_settings.get('active_backend','opencode')
                active_workspace=workspace_path(active_settings)
                conn.execute("UPDATE orchestrator_sessions SET backend=?,workspace=?,acp_session_id='' WHERE backend!=? OR workspace!=?",(active_backend,active_workspace,active_backend,active_workspace))
                conn.execute("UPDATE agent_runs SET status='failed',finished_at=?,error='Server stopped before this run finished; update the ticket to retry.' WHERE status IN ('queued','running')",(now(),))
                conn.execute("UPDATE ticket_reviews SET status='failed',reason='Server stopped before review finished.',finished_at=? WHERE status IN ('queued','running')",(now(),))
                conn.commit()
            except Exception:
                lease.close(); self._lease=None
                raise
            self.stopping.clear()
            self.thread = threading.Thread(target=self._loop, daemon=True, name='coms-agent-runner'); self.thread.start()
            if warm:
                threading.Thread(target=self._warm_active_backend, daemon=True, name='coms-model-warmup').start()
                threading.Thread(target=self._warm_orchestrator, daemon=True, name='coms-orchestrator-warmup').start()
    def stop(self):
        self.stopping.set()
        with self.lock:
            connections=list(self.conns.values())
            connections.extend(self.review_conns.values())
            with self.model_lock:
                connections.extend(self.model_conns.values())
                self.model_conns.clear()
            with self.orchestrator_conn_lock:
                if self.orchestrator_conn: connections.append(self.orchestrator_conn)
                self.orchestrator_conn=None; self.orchestrator_conn_key=None
            self.conns.clear()
        for c in connections: c.cancel(); c.close()
        if self.thread: self.thread.join(timeout=5)
        if self._lease:
            self._lease.close(); self._lease=None
    def _conn(self, ticket_id, backend, cwd, run_id=None):
        def log(line): self._message(ticket_id, run_id, 'system', 'acp-log', line)
        def notification(msg):
            params=msg.get('params') or {}; update=params.get('update') or {}
            text=update.get('content',{}).get('text') if isinstance(update.get('content'),dict) else None
            if text: self._message(ticket_id, run_id, 'system', 'acp-response', text)
            elif update: self._message(ticket_id, run_id, 'system', 'acp-'+str(update.get('sessionUpdate','event')), json.dumps(update,ensure_ascii=False))
        def request(msg, respond):
            options=(msg.get('params') or {}).get('options') or []
            chosen=next((x.get('optionId') for x in options if x.get('kind') in ('allow_once','allow_always','allow')), None)
            if chosen: respond({'outcome':{'outcome':'selected','optionId':chosen}})
            else: respond({'outcome':{'outcome':'cancelled'}})
        cmd=backend_command(backend, self.settings())
        if not cmd: raise RuntimeError('No ACP command configured for '+backend)
        c=AcpConnection(cmd[0], cwd, command_args=cmd[1:], on_notification=notification, on_request=request, on_close=lambda _e:None, logger=log, env={'COMS_DB':str(db.db_path().resolve()),'COMS_AGENT':'agent-runner'}, mcp_servers=board_servers('agent-runner',ticket_id,run_id))
        try:
            c.start(); c.session_new(cwd=cwd); self._apply_model(c,backend,self.settings()); return c
        except Exception:
            c.close(); raise
    @staticmethod
    def _apply_model(conn,backend,settings):
        chosen=(settings.get('model_'+backend) or '').strip()
        if not chosen: return
        options=next((o for o in conn.config_options if o.get('id')=='model'),{})
        allowed={x.get('value') for x in options.get('options') or []}
        if chosen not in allowed:
            raise RuntimeError(f'Selected model {chosen!r} is not offered by the {backend} ACP session; refresh models in Runner settings and choose an available model.')
        conn.set_config_option(options.get('id','model'),chosen)
    def discover_models(self,backend):
        if backend not in ('codex','claude','opencode'): raise ValueError('backend must be codex, claude, or opencode')
        settings=self.settings(); cmd=backend_command(backend,settings)
        if not cmd: raise ValueError('No ACP command configured for '+backend)
        cwd=workspace_path(settings)
        if backend==settings.get('active_backend','opencode'):
            c=self._orchestrator_connection(backend,cwd,settings)
            option=next((o for o in c.config_options if o.get('id')=='model'),{})
            return {'backend':backend,'models':c.models(),'current':c.current_model(),'config_id':option.get('id','model')}
        key=(backend,tuple(cmd),cwd)
        with self.model_lock:
            c=self.model_conns.get(key)
            if c is not None and not c.alive:
                self.model_conns.pop(key,None); c.close(); c=None
            if c is None:
                c=AcpConnection(cmd[0],cwd,command_args=cmd[1:],on_notification=lambda _m:None,on_request=lambda _m,respond:respond({'outcome':{'outcome':'cancelled'}}),on_close=lambda _e:None,logger=lambda _m:None,env={'COMS_DB':str(db.db_path()),'COMS_AGENT':'model-discovery'})
                try:
                    c.start(); c.session_new(cwd=cwd); self.model_conns[key]=c
                except Exception:
                    c.close(); raise
            option=next((o for o in c.config_options if o.get('id')=='model'),{})
            return {'backend':backend,'models':c.models(),'current':c.current_model(),'config_id':option.get('id','model')}
    def _warm_active_backend(self):
        try: self.discover_models(self.settings().get('active_backend','opencode'))
        except Exception: logging.info('ACP model warm-up will retry on demand',exc_info=True)
    def _reset_and_warm_models(self):
        with self.model_lock:
            connections=list(self.model_conns.values()); self.model_conns.clear()
            for c in connections: c.close()
        self._warm_active_backend()
    def settings(self):
        conn=db.connect(); rows=conn.execute('SELECT key,value FROM runner_settings').fetchall(); return {r[0]:r[1] for r in rows}
    def _message(self, tid, rid, role, kind, body):
        conn=db.connect(); conn.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)',(tid,rid,role,kind,str(body),now())); conn.commit()
    def _worker(self, key, function, *args):
        try: function(*args)
        except Exception:
            logging.exception('Coms Board worker %s failed', key)
        finally:
            with self.lock: self.active.pop(key,None)
            conn=getattr(db._local,'conn',None)
            if conn: conn.close()
            db._local.__dict__.clear()
    def _loop(self):
        while not self.stopping.wait(1):
            try:
                settings=self.settings(); maximum=max(1,min(16,int(settings.get('parallel_agents','2'))))
                with self.lock:
                    if len(self.active)>=maximum: continue
                    conn=db.connect()
                    rows=conn.execute("SELECT t.*,a.role AS agent_role FROM tickets t LEFT JOIN agents a ON a.slug=t.assignee WHERE t.ready_for_execution=1 AND t.status IN ('open','in_progress') AND t.assignee NOT LIKE 'human%' AND lower(coalesce(a.role,''))!='human' AND lower(t.tags) NOT LIKE '%human%' AND NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.ticket_id=t.id AND (r.status IN ('queued','running') OR (r.status='failed' AND t.updated_at<=r.finished_at))) ORDER BY CASE t.priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,t.created_at LIMIT ?",(maximum-len(self.active),)).fetchall()
                    for row in rows:
                        if row['id'] in self.active or 'review:'+str(row['id']) in self.active: continue
                        self.active[row['id']]=True
                        threading.Thread(target=self._worker,args=(row['id'],self._work,dict(row)),daemon=True).start()
                    slots=maximum-len(self.active)
                    if slots>0:
                        review_rows=conn.execute("SELECT t.*,a.role AS agent_role,coalesce((SELECT id FROM agent_runs ar WHERE ar.ticket_id=t.id AND ar.status='completed' ORDER BY ar.id DESC LIMIT 1),0) AS review_run_id FROM tickets t LEFT JOIN agents a ON a.slug=t.assignee LEFT JOIN ticket_reviews tr ON tr.ticket_id=t.id AND tr.source_run_id=coalesce((SELECT id FROM agent_runs ar2 WHERE ar2.ticket_id=t.id AND ar2.status='completed' ORDER BY ar2.id DESC LIMIT 1),0) AND tr.ticket_version=t.updated_at WHERE t.status='review' AND t.assignee NOT LIKE 'human%' AND lower(coalesce(a.role,''))!='human' AND lower(t.tags) NOT LIKE '%human%' AND tr.id IS NULL ORDER BY CASE t.priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,t.updated_at LIMIT ?",(slots,)).fetchall()
                        for row in review_rows:
                            if row['id'] in self.active: continue
                            key='review:'+str(row['id']); review_key=row['review_run_id']
                            prior=conn.execute('SELECT id,status FROM ticket_reviews WHERE ticket_id=? AND source_run_id=?',(row['id'],review_key)).fetchone()
                            if prior and prior['status'] in ('queued','running'): continue
                            if prior:
                                review_id=prior['id']; conn.execute("UPDATE ticket_reviews SET backend=?,status='queued',decision='',reason='',ticket_version=?,created_at=?,finished_at='' WHERE id=?",(settings.get('active_backend','opencode'),row['updated_at'],now(),review_id)); conn.commit()
                            else:
                                cur=conn.execute("INSERT INTO ticket_reviews(ticket_id,source_run_id,backend,status,ticket_version,created_at) VALUES(?,?,?,'queued',?,?)",(row['id'],review_key,settings.get('active_backend','opencode'),row['updated_at'],now())); review_id=cur.lastrowid; conn.commit()
                            self.active[key]=True
                            threading.Thread(target=self._worker,args=(key,self._review_ticket,dict(row),review_id,review_key),daemon=True).start()
            except Exception:
                logging.exception('Coms Board runner scheduling failed')
                self.stopping.wait(1)
    def _work(self,t):
        tid=t['id']; conn=db.connect(); settings=self.settings(); backend=settings.get('active_backend','opencode')
        # Recheck and reserve under the SQLite write lock: assignment/readiness
        # can change after scheduling, and another worker must not claim it.
        conn.execute('BEGIN IMMEDIATE')
        current=conn.execute('SELECT t.*,a.role AS agent_role FROM tickets t LEFT JOIN agents a ON a.slug=t.assignee WHERE t.id=?',(tid,)).fetchone()
        if (not current or not current['ready_for_execution'] or current['status'] not in ('open','in_progress')
            or current['assignee'].lower().startswith('human') or (current['agent_role'] or '').lower()=='human'
            or 'human' in current['tags'].lower()
            or conn.execute("SELECT 1 FROM agent_runs WHERE ticket_id=? AND status IN ('queued','running')",(tid,)).fetchone()):
            conn.rollback()
            with self.lock: self.active.pop(tid,None)
            return
        t=dict(current)
        template=conn.execute('SELECT * FROM agent_templates WHERE id=? AND enabled=1',(t.get('agent_template_id'),)).fetchone() if t.get('agent_template_id') else None
        if template and template['backend']: backend=template['backend']
        cur=conn.execute("INSERT INTO agent_runs(ticket_id,template_id,backend,status,started_at) VALUES(?,?,?,'running',?)",(tid,template['id'] if template else None,backend,now())); rid=cur.lastrowid
        conn.execute("UPDATE tickets SET status='in_progress',updated_at=? WHERE id=?",(now(),tid)); conn.commit()
        self._message(tid,rid,'system','status',f'Runner started with {backend}; priority {t["priority"]}.')
        try:
            if t.get('agent_template_id') and not template:
                raise RuntimeError('Assigned agent template is missing or disabled; choose an enabled template before retrying.')
            cwd=workspace_path(settings)
            with self.lock:
                # Keep reusable ticket chats bounded; running sessions are never evicted.
                maximum=max(1,min(16,int(settings.get('parallel_agents','2'))))
                while len(self.conns)>=maximum:
                    idle=next((key for key in self.conns if key not in self.active),None)
                    if idle is None: break
                    self.conns.pop(idle).close()
            c=self._conn(tid,backend,cwd,rid); self.conns[tid]=c
            conn=db.connect(); conn.execute('UPDATE agent_runs SET session_id=? WHERE id=?',(c.session_id or '',rid)); conn.commit()
            prompt=BASE_AGENT_SYSTEM_PROMPT+'\n\n'
            if template and template['prompt'].strip():
                prompt += 'AGENT TEMPLATE INSTRUCTIONS (follow these within the shared Coms Board requirements above):\n'+template['prompt'].strip()+'\n\n'
            prompt += (f"TICKET ASSIGNMENT\nYou are the execution agent for Coms Board ticket #{tid}.\n"
                       f"Ticket: {t['title']}\nPriority: {t['priority']}\nDescription:\n{t['body']}\n\n"
                       f"Use your ACP response stream for progress; those messages are private run diagnostics and are not posted to the ticket. "
                       f"Use ticket_comment for ticket #{tid} when needed and ticket_result with id {tid} before finishing. Your workspace is {cwd}. "
                       "When finished, ensure the requested deliverable has been recorded in the appropriate Coms Board record, then summarize the result and verification in your ACP response.")
            self._message(tid,rid,'system','acp-prompt',prompt); c.prompt(prompt,timeout=3600)
            delivery=db.connect().execute("SELECT body FROM agent_messages WHERE run_id=? AND kind='board-delivery'",(rid,)).fetchall()
            if not any(json.loads(row['body']).get('tool')=='ticket_result' for row in delivery):
                raise RuntimeError('Agent finished without publishing ticket_result through the coms-board tools. Record the deliverable on the board and update the ticket to retry.')
            conn=db.connect(); conn.execute("UPDATE agent_runs SET status='completed',finished_at=? WHERE id=?",(now(),rid)); conn.execute("UPDATE tickets SET status='review',updated_at=? WHERE id=? AND status='in_progress'",(now(),tid)); conn.commit()
            self._message(tid,rid,'system','status','Agent turn finished; ticket moved to review.')
        except Exception as e:
            conn=db.connect(); conn.execute("UPDATE agent_runs SET status='failed',finished_at=?,error=? WHERE id=?",(now(),str(e),rid)); conn.commit(); self._message(tid,rid,'system','error',str(e))
            failed=self.conns.pop(tid,None)
            if failed: failed.close()
        finally:
            with self.lock: self.active.pop(tid,None)
    def chat(self,tid,text):
        if not text.strip(): raise ValueError('message required')
        with self.lock:
            c=self.conns.get(tid)
            if c and not c.alive:
                self.conns.pop(tid,None); c=None
            if c and tid in self.active: raise ValueError('The ticket agent is still working; wait for its current response.')
            if 'review:'+str(tid) in self.active: raise ValueError('This ticket is being reviewed; wait for the review to finish.')
            maximum=max(1,min(16,int(self.settings().get('parallel_agents','2'))))
            if c and len(self.active)>=maximum: raise ValueError('All configured backend agent slots are in use.')
            if c: self.active[tid]=True
        if not c: raise ValueError('No active agent session for this ticket. Mark it ready to start the runner.')
        self._message(tid,None,'user','text',text)
        threading.Thread(target=self._worker,args=(tid,self._prompt,c,tid,text),daemon=True).start()
        return {'accepted':True}
    def _prompt(self,c,tid,text):
        try: c.prompt(text,timeout=3600)
        except Exception as e: self._message(tid,None,'system','error',str(e))
        finally:
            with self.lock: self.active.pop(tid,None)

    def _review_ticket(self,ticket,review_id,source_run_id):
        from . import api
        tid=ticket['id']; conn=db.connect(); settings=self.settings(); backend=settings.get('active_backend','opencode')
        self._message(tid,source_run_id or None,'system','review-status',f'Orchestrator review started with {backend}.')
        conn.execute("UPDATE ticket_reviews SET status='running',backend=? WHERE id=?",(backend,review_id)); conn.commit()
        response=[]; assistant_id=[None]
        def add(role,kind,body):
            c=db.connect(); c.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)',(tid,source_run_id or None,role,kind,body,now())); c.commit()
        def log(line): add('system','review-log',line)
        def notification(msg):
            p=msg.get('params') or {}; u=p.get('update') or {}; kind=u.get('sessionUpdate'); content=u.get('content') or {}; body=content.get('text') if isinstance(content,dict) else None
            if body and kind=='agent_message_chunk':
                response.append(body); c=db.connect(); c.execute('UPDATE agent_messages SET body=body||? WHERE id=?',(body,assistant_id[0])); c.commit()
            elif u: log(json.dumps(u,ensure_ascii=False))
        def request(msg,respond):
            respond({'outcome':{'outcome':'cancelled'}})
        c=None
        try:
            current=api.get_ticket(db.connect(),tid)
            role=db.connect().execute('SELECT role FROM agents WHERE slug=?',(current['assignee'],)).fetchone()
            if current['status']!='review' or current['assignee'].lower().startswith('human') or (role and role['role'].lower()=='human') or 'human' in current['tags'].lower():
                conn.execute("UPDATE ticket_reviews SET status='human_review',decision='human_review',reason='Ticket state or human assignment changed before review.',ticket_version=?,finished_at=? WHERE id=?",(current['updated_at'],now(),review_id)); conn.commit(); return
            if current['updated_at']!=ticket['updated_at']:
                conn.execute("UPDATE ticket_reviews SET status='superseded',reason='Ticket changed while review was starting.',finished_at=? WHERE id=?",(now(),review_id)); conn.commit(); return
            backend=settings.get('active_backend','opencode'); cwd=workspace_path(settings); cmd=backend_command(backend,settings)
            if not cmd: raise RuntimeError('No ACP command configured for '+backend)
            c=AcpConnection(cmd[0],cwd,command_args=cmd[1:],on_notification=notification,on_request=request,on_close=lambda _e:None,logger=log,env={'COMS_DB':str(db.db_path().resolve()),'COMS_AGENT':'orchestrator-review'},mcp_servers=board_servers('orchestrator-review',readonly=True))
            with self.lock: self.review_conns[tid]=c
            c.start(); c.session_new(cwd=cwd); self._apply_model(c,backend,settings)
            comments=current.get('comments') or []
            messages=conn.execute('SELECT role,kind,body FROM agent_messages WHERE ticket_id=? AND (run_id=? OR run_id IS NULL) ORDER BY id DESC LIMIT 80',(tid,source_run_id)).fetchall()
            transcript='\n'.join(f"{m['role']} [{m['kind']}]: {m['body'][:3000]}" for m in reversed(messages))
            discussion='\n'.join(f"{x['author']} [{x['kind']}]: {x['body']}" for x in comments)
            prompt=("You are the Coms Board orchestrator performing the final review of a completed ticket. Review the acceptance criteria, delivered result, ticket discussion, and execution transcript. Inspect the configured workspace read-only if useful. Do not change files, tickets, or comments. Bias toward closing: choose human_review only when there is a concrete unresolved requirement, material risk that needs human judgment, or a human-only decision. Otherwise choose close. Respond with exactly one valid JSON object and no other text. Use decision value close or human_review, for example: {\"decision\":\"close\",\"reason\":\"Acceptance criteria are met.\"}.\n\n"
                    f"Ticket #{tid}: {current['title']}\nPriority: {current['priority']}\nDescription / acceptance criteria:\n{current['body']}\n\nDelivered result:\n{current['result']}\n\nTicket discussion:\n{discussion}\n\nExecution transcript:\n{transcript}")
            x=db.connect(); cur=x.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)',(tid,source_run_id or None,'assistant','review-decision','',now())); assistant_id[0]=cur.lastrowid; x.commit()
            c.prompt(prompt,timeout=1800)
            raw=''.join(response).strip(); decision=''; reason=''
            try:
                parsed=json.loads(raw)
                decision=parsed.get('decision','').lower(); reason=str(parsed.get('reason','')).strip()
            except (ValueError, AttributeError): pass
            if decision not in ('close','human_review'):
                raise RuntimeError('Orchestrator returned no valid review decision; ticket remains in review.')
            if not reason: reason='Acceptance criteria appear satisfied.' if decision=='close' else 'A concrete point needs human judgment.'
            conn=db.connect(); conn.execute('BEGIN IMMEDIATE')
            current=api.get_ticket(conn,tid)
            role=conn.execute('SELECT role FROM agents WHERE slug=?',(current['assignee'],)).fetchone()
            if current['status']!='review' or current['updated_at']!=ticket['updated_at']:
                final='superseded'
            elif current['assignee'].lower().startswith('human') or (role and role['role'].lower()=='human') or 'human' in current['tags'].lower():
                final='human_review'; decision='human_review'; reason='Ticket was assigned or tagged for a human during automated review.'
            elif decision=='close':
                api.update_ticket(db.connect(),tid,'orchestrator',status='done')
                api.comment_ticket(db.connect(),tid,'orchestrator','Orchestrator review approved and closed this ticket: '+reason)
                final='closed'
            else:
                api.comment_ticket(db.connect(),tid,'orchestrator','Orchestrator review requests human judgment: '+reason)
                final='human_review'
            current=api.get_ticket(db.connect(),tid)
            version=ticket['updated_at'] if final=='superseded' else current['updated_at']
            conn=db.connect(); conn.execute('UPDATE ticket_reviews SET status=?,decision=?,reason=?,ticket_version=?,finished_at=? WHERE id=?',(final,decision,reason,version,now(),review_id)); conn.commit()
            self._message(tid,source_run_id or None,'system','review-status',f'Orchestrator decision: {decision}. {reason}')
        except Exception as e:
            conn=db.connect(); conn.rollback(); conn.execute("UPDATE ticket_reviews SET status='failed',decision='',reason=?,finished_at=? WHERE id=?",(str(e),now(),review_id)); conn.commit(); self._message(tid,source_run_id or None,'system','review-error',str(e))
        finally:
            if c: c.close()
            with self.lock:
                self.active.pop('review:'+str(tid),None)
                self.review_conns.pop(tid,None)

    def orchestrator_projects(self):
        conn=db.connect(); return [dict(r) for r in conn.execute('SELECT * FROM orchestrator_projects ORDER BY name')]
    def orchestrator_sessions(self):
        conn=db.connect(); return [dict(r) for r in conn.execute('SELECT * FROM orchestrator_sessions ORDER BY updated_at DESC')]
    def delete_orchestrator_project(self,project_id):
        conn=db.connect()
        project=conn.execute('SELECT id FROM orchestrator_projects WHERE id=?',(int(project_id),)).fetchone()
        if not project: return False
        conn.execute('UPDATE orchestrator_sessions SET project_id=NULL WHERE project_id=?',(int(project_id),))
        conn.execute('DELETE FROM orchestrator_projects WHERE id=?',(int(project_id),)); conn.commit()
        return True
    def create_orchestrator_project(self,name):
        name=name.strip()
        if not name: raise ValueError('project name required')
        conn=db.connect(); ts=now(); conn.execute('INSERT INTO orchestrator_projects(name,created_at,updated_at) VALUES(?,?,?)',(name,ts,ts)); conn.commit()
        return dict(conn.execute('SELECT * FROM orchestrator_projects WHERE name=?',(name,)).fetchone())
    def create_orchestrator_session(self,title='New chat',project_id=None):
        conn=db.connect(); sid=uuid.uuid4().hex; ts=now()
        if project_id is not None and not conn.execute('SELECT 1 FROM orchestrator_projects WHERE id=?',(project_id,)).fetchone(): raise ValueError('project not found')
        conn.execute('INSERT INTO orchestrator_sessions(id,title,project_id,created_at,updated_at) VALUES(?,?,?,?,?)',(sid,title.strip() or 'New chat',project_id,ts,ts)); conn.commit()
        return dict(conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone())
    def orchestrator_detail(self,sid):
        conn=db.connect(); row=conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone()
        if not row: return None
        out=dict(row); out['messages']=[dict(r) for r in conn.execute('SELECT * FROM orchestrator_messages WHERE session_id=? ORDER BY id',(sid,))]
        settings=self.settings(); backend=settings.get('active_backend','opencode')
        out['backend']=backend; out['model']=settings.get('model_'+backend) or 'ACP default'
        with self.lock: out['busy']=self.orchestrator_busy and self.orchestrator_session==sid
        return out
    def delete_orchestrator_session(self,sid):
        with self.lock:
            if self.orchestrator_busy and self.orchestrator_session==sid:
                raise ValueError('Wait for this chat to finish responding before deleting it.')
            conn=db.connect()
            if not conn.execute('SELECT 1 FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone(): return False
            conn.execute('DELETE FROM orchestrator_messages WHERE session_id=?',(sid,))
            conn.execute('DELETE FROM orchestrator_sessions WHERE id=?',(sid,)); conn.commit()
            return True
    def update_orchestrator_session(self,sid,project_id):
        conn=db.connect()
        if not conn.execute('SELECT 1 FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone(): raise ValueError('session not found')
        if project_id not in (None,'') and not conn.execute('SELECT 1 FROM orchestrator_projects WHERE id=?',(project_id,)).fetchone(): raise ValueError('project not found')
        conn.execute('UPDATE orchestrator_sessions SET project_id=?,updated_at=? WHERE id=?',(int(project_id) if project_id not in (None,'') else None,now(),sid)); conn.commit()
        return dict(conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone())
    def _orchestrator_connection(self,backend,cwd,settings):
        cmd=backend_command(backend,settings)
        if not cmd: raise RuntimeError('No ACP command configured for '+backend)
        key=(backend,tuple(cmd),cwd,settings.get('model_'+backend,''))
        with self.orchestrator_conn_lock:
            c=self.orchestrator_conn
            if c is not None and (self.orchestrator_conn_key!=key or not c.alive):
                c.close(); self.orchestrator_conn=None; self.orchestrator_conn_key=None; c=None
            if c is None:
                def add(role,kind,body):
                    sid=self.orchestrator_context.get('sid')
                    if sid:
                        conn=db.connect(); conn.execute('INSERT INTO orchestrator_messages(role,kind,body,created_at,session_id) VALUES(?,?,?,?,?)',(role,kind,body,now(),sid)); conn.commit()
                def log(line): add('system','log',line)
                def notification(msg):
                    ctx=self.orchestrator_context
                    if not ctx.get('stream_enabled'): return
                    p=msg.get('params') or {}; u=p.get('update') or {}; content=u.get('content') or {}; body=content.get('text') if isinstance(content,dict) else None
                    if body and u.get('sessionUpdate')=='agent_message_chunk' and ctx.get('stream_id'):
                        conn=db.connect(); conn.execute('UPDATE orchestrator_messages SET body=body||? WHERE id=?',(body,ctx['stream_id'])); conn.commit()
                    elif u: log(json.dumps(u,ensure_ascii=False))
                def request(msg,respond):
                    options=(msg.get('params') or {}).get('options') or []; opt=next((x.get('optionId') for x in options if x.get('kind') in ('allow_once','allow_always','allow')),None)
                    respond({'outcome':{'outcome':'selected','optionId':opt}} if opt else {'outcome':{'outcome':'cancelled'}})
                c=AcpConnection(cmd[0],cwd,command_args=cmd[1:],on_notification=notification,on_request=request,on_close=lambda _e:None,logger=log,env={'COMS_DB':str(db.db_path().resolve()),'COMS_AGENT':'orchestrator'},mcp_servers=board_servers('orchestrator'))
                try:
                    c.start(); c.session_new(cwd=cwd); self._apply_model(c,backend,settings)
                except Exception:
                    c.close(); raise
                self.orchestrator_conn=c; self.orchestrator_conn_key=key
            return c
    def _warm_orchestrator(self):
        try:
            settings=self.settings(); backend=settings.get('active_backend','opencode')
            self._orchestrator_connection(backend,workspace_path(settings),settings)
        except Exception: logging.info('Orchestrator ACP warm-up will retry on demand',exc_info=True)
    def _reset_orchestrator(self):
        with self.orchestrator_conn_lock:
            if self.orchestrator_conn: self.orchestrator_conn.close()
            self.orchestrator_conn=None; self.orchestrator_conn_key=None
        self._warm_orchestrator()
    def orchestrate(self, text, sid='default'):
        text=text.strip()
        if not text: raise ValueError('message required')
        with self.lock:
            if self.orchestrator_busy: raise ValueError('The orchestrator is still responding; wait for its reply.')
            self.orchestrator_busy=True; self.orchestrator_session=sid
        conn=db.connect(); session=conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone()
        if not session:
            with self.lock: self.orchestrator_busy=False
            raise ValueError('session not found')
        ts=now(); conn.execute('INSERT INTO orchestrator_messages(role,kind,body,created_at,session_id) VALUES(?,?,?,?,?)',('user','text',text,ts,sid))
        if session['title']=='New chat': conn.execute('UPDATE orchestrator_sessions SET title=?,updated_at=? WHERE id=?',(text[:60],ts,sid))
        else: conn.execute('UPDATE orchestrator_sessions SET updated_at=? WHERE id=?',(ts,sid))
        conn.commit()
        threading.Thread(target=self._orchestrator_turn,args=(text,sid,dict(session)),daemon=True).start()
        return {'accepted':True}
    def _orchestrator_turn(self,text,sid,session):
        c=None; restored=False
        try:
            settings=self.settings(); backend=settings.get('active_backend','opencode')
            cwd=workspace_path(settings); c=self._orchestrator_connection(backend,cwd,settings)
            self.orchestrator_context={'sid':sid,'stream_id':None,'stream_enabled':False}
            if session.get('backend')==backend and session.get('acp_session_id') and session.get('workspace')==cwd and c.supports('loadSession'):
                try: c.session_load(session['acp_session_id'],cwd=cwd); restored=True
                except Exception as exc:
                    logging.info('Could not restore ACP chat session; restoring board history: %s',exc)
            if not restored:
                result=c.session_new(cwd=cwd); acp_id=result.get('sessionId') or c.session_id
                x=db.connect(); x.execute('UPDATE orchestrator_sessions SET acp_session_id=?,backend=?,workspace=? WHERE id=?',(acp_id,backend,cwd,sid)); x.commit()
            self._apply_model(c,backend,settings)
            selected_model=settings.get('model_'+backend) or 'ACP default'
            guide=(f"You are the Coms Board main orchestrator, running through the {backend} ACP backend with model {selected_model}. "
                   "Do not claim to be running through a different provider or model. Help the user plan work and create tickets when asked. "
                   "Use the attached coms-board MCP tools for all board operations. Call agent_templates before creating tickets with ticket_create. Publish documentation with wiki_put/wiki_append and Q&A with questions_ask/questions_answer; do not write board records to local files. "
                   "Choose p0/p1/p2/p3 based on urgency and impact. If the user asked for the work to be completed, set ready=true on actionable tickets so the runner can proceed without another user round trip. Leave a ticket unready only when it is a planning draft, blocked on dependencies, explicitly on hold, awaiting a required approval, or assigned/tagged for a human. "
                   "Never assign/tag a human ticket for agent execution. Keep the user informed and report ticket IDs.")
            x=db.connect(); cur=x.execute('INSERT INTO orchestrator_messages(role,kind,body,created_at,session_id) VALUES(?,?,?,?,?)',('assistant','text','',now(),sid)); stream_id=cur.lastrowid; x.commit()
            self.orchestrator_context={'sid':sid,'stream_id':stream_id,'stream_enabled':True}
            # If this is a legacy transcript without an ACP session id, restore
            # its prior turns as context so reopening it remains conversational.
            transcript=x.execute('SELECT role,body FROM orchestrator_messages WHERE session_id=? AND role IN (\'user\',\'assistant\') ORDER BY id',(sid,)).fetchall()
            prior=transcript[:-2]
            context='\n'.join(('User: ' if row['role']=='user' else 'Orchestrator: ')+row['body'] for row in prior if row['body'])
            first_turn=not restored
            prefix=guide+'\n\nWorkspace: '+cwd+'\n\n'
            if context and first_turn: prefix+='Previous conversation (restored from Coms Board history):\n'+context+'\n\n'
            c.prompt(prefix+'User: '+text,timeout=3600)
        except Exception as e:
            conn=db.connect(); conn.execute('INSERT INTO orchestrator_messages(role,kind,body,created_at,session_id) VALUES(?,?,?,?,?)',('system','error',str(e),now(),sid)); conn.commit()
        finally:
            self.orchestrator_context={'sid':sid,'stream_id':None,'stream_enabled':False}
            if c and not c.alive:
                with self.orchestrator_conn_lock:
                    c.close(); self.orchestrator_conn=None; self.orchestrator_conn_key=None
            with self.lock: self.orchestrator_busy=False; self.orchestrator_session=None

RUNNER=Runner()

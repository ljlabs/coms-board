"""ACP-backed ticket runner for Swarm Agent."""
from __future__ import annotations
import json, logging, os, shlex, shutil, sqlite3, subprocess, sys, tempfile, threading, time, uuid, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import api, db, progress, team, schedules
from .acp import AcpConnection, AcpError
from .turn_queue import TurnQueue
from .workspace import DEFAULT_WORKSPACE, workspace_directory_browser, workspace_folder_change, workspace_folder_target, workspace_path
from .orchestrator_transcript import Transcript, begin_turn, conversation_messages, fork_conversation, history_before, message_dict, publish_change, recover_turns

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENCODE_API_BASE_URL = 'http://localhost:11434/v1'
OPENCODE_LOCAL_PROVIDER_ID = 'coms-local'
DEFAULT_OPENCODE_OUTPUT_TOKENS = 32000
OPENCODE_REQUIRED_PERMISSIONS = {
    name: 'allow' for name in ('read', 'grep', 'glob', 'bash', 'edit', 'webfetch', 'websearch')
}
MAX_REVIEW_ATTEMPTS = 3
REVIEW_RETRY_BASE_SECONDS = 15
MAX_AGENT_RETRIES = 2
AGENT_RETRY_BASE_SECONDS = 15
MAX_TEAM_ATTEMPTS = 3
TEAM_RETRY_BASE_SECONDS = 15
TEAM_HEARTBEAT_SECONDS = 300
TEAM_MONITOR_SECONDS = 180
WORKER_TURN_SECONDS = 900
ORCHESTRATOR_RESTART_SECONDS = 30
ORCHESTRATOR_STOP_GRACE_SECONDS = 3


def _retry_due(retry_count, finished_at, *, max_retries, base_seconds, now=None):
    retries = int(retry_count or 0)
    if retries >= max_retries:
        return False
    if not finished_at:
        return True
    try:
        finished = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    delay = base_seconds * (2 ** retries)
    return (now or datetime.now(timezone.utc)) >= finished + timedelta(seconds=delay)


def _agent_retry_due(retry_count, finished_at, *, now=None):
    """Whether another bounded ticket execution retry is due."""
    return _retry_due(retry_count, finished_at, max_retries=MAX_AGENT_RETRIES,
                      base_seconds=AGENT_RETRY_BASE_SECONDS, now=now)


def _review_retry_due(attempt_count, finished_at, *, now=None):
    """Return whether a failed final review is eligible for another bounded attempt."""
    attempts = int(attempt_count or 0)
    if attempts >= MAX_REVIEW_ATTEMPTS:
        return False
    if not finished_at:
        return True
    try:
        finished = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    delay = REVIEW_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return (now or datetime.now(timezone.utc)) >= finished + timedelta(seconds=delay)


def _team_retry_status(attempt_count):
    """Keep a failed team request queued until its bounded attempts are used."""
    return 'pending' if int(attempt_count or 0) < MAX_TEAM_ATTEMPTS else 'failed'


def normalize_opencode_api_base_url(value):
    value = str(value or DEFAULT_OPENCODE_API_BASE_URL).strip().rstrip('/')
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('OpenCode API URL must be an http(s) base URL without credentials, query, or fragment.')
    path = parts.path.rstrip('/')
    if not path.endswith('/v1'):
        path += '/v1'
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def _opencode_api_json(url, api_key):
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    if api_key:
        request.add_header('Authorization', 'Bearer ' + api_key)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode('utf-8'))
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return None


def _positive_token_limit(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and not value.is_integer():
            return None
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return limit if limit > 0 else None


def _opencode_runtime_context(entry):
    """Read a live context limit, never the model's training-only context."""
    meta = entry.get('meta') if isinstance(entry, dict) else None
    for source in (meta, entry):
        if not isinstance(source, dict):
            continue
        for key in ('n_ctx', 'context_length', 'context_window', 'max_context_length'):
            limit = _positive_token_limit(source.get(key))
            if limit is not None:
                return limit
    return None


def _opencode_model_catalog(payload):
    entries = payload.get('data', payload.get('models', [])) if isinstance(payload, dict) else []
    models = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            model_id = entry.get('id') or entry.get('name')
            display_name = entry.get('name')
            meta = entry.get('meta')
            owned_by = str(entry.get('owned_by', '')).lower()
            is_llamacpp = 'llama' in owned_by or (isinstance(meta, dict) and ('n_ctx' in meta or 'n_ctx_train' in meta))
            context = _opencode_runtime_context(entry)
        else:
            model_id = entry if isinstance(entry, str) else ''
            display_name = ''
            is_llamacpp = False
            context = None
        if not model_id:
            continue
        model_id = str(model_id).strip()
        if not model_id:
            continue
        record = {
            'id': model_id,
            'name': str(display_name).strip() if isinstance(display_name, str) and display_name.strip() else model_id,
            'context': context,
            '_llamacpp': is_llamacpp,
        }
        existing = next((model for model in models if model['id'] == model_id), None)
        if existing is None:
            models.append(record)
        else:
            if existing['context'] is None and context is not None:
                existing['context'] = context
            if existing['name'] == existing['id'] and record['name'] != record['id']:
                existing['name'] = record['name']
            existing['_llamacpp'] = existing['_llamacpp'] or is_llamacpp
    return models


def _opencode_props_context(base_url, api_key, model_id):
    """Fallback to llama.cpp's live properties when /v1/models lacks n_ctx."""
    parts = urllib.parse.urlsplit(base_url.rstrip('/'))
    path = parts.path[:-3] if parts.path.endswith('/v1') else parts.path
    props_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path.rstrip('/') + '/props', '', ''))
    props_url += '?' + urllib.parse.urlencode({'model': model_id})
    payload = _opencode_api_json(props_url, api_key)
    if not isinstance(payload, dict):
        return None
    settings = payload.get('default_generation_settings')
    if isinstance(settings, dict):
        context = _positive_token_limit(settings.get('n_ctx'))
        if context is not None:
            return context
    return _positive_token_limit(payload.get('n_ctx'))


def _opencode_models(base_url, api_key):
    payload = _opencode_api_json(base_url.rstrip('/') + '/models', api_key)
    models = _opencode_model_catalog(payload)
    for model in models:
        if model['context'] is None and model['_llamacpp']:
            model['context'] = _opencode_props_context(base_url, api_key, model['id'])
    return [{key: value for key, value in model.items() if not key.startswith('_')} for model in models]


def backend_environment(name, settings, values=None):
    env = dict(values or {})
    if name != 'opencode':
        return env
    base_url = normalize_opencode_api_base_url(settings.get('opencode_api_base_url', DEFAULT_OPENCODE_API_BASE_URL))
    api_key = settings.get('opencode_api_key', '')
    discovered_models = _opencode_models(base_url, api_key)
    model_hint = str(settings.get('opencode_api_model', '')).strip()
    selected = str(settings.get('model_opencode', '')).strip()
    prefix = OPENCODE_LOCAL_PROVIDER_ID + '/'
    if model_hint.startswith(prefix): model_hint = model_hint[len(prefix):]
    if selected.startswith(prefix): selected = selected[len(prefix):]
    elif '/' in selected: selected = ''
    if discovered_models:
        models = discovered_models
    else:
        # Keep manually configured IDs as a startup/offline fallback only. A
        # live API catalog is authoritative for both model IDs and context.
        models = []
        for model_id in (selected, model_hint):
            if model_id and not any(model['id'] == model_id for model in models):
                models.append({'id': model_id, 'name': model_id, 'context': None})
    config = {
        '$schema': 'https://opencode.ai/config.json',
        # Inline configuration overrides inherited project/global defaults.
        # Agent permissions take precedence over global ones, so pin the
        # baseline on the selected build agent too. Keep unrelated policies.
        'default_agent': 'build',
        'permission': dict(OPENCODE_REQUIRED_PERMISSIONS),
        'agent': {'build': {'permission': dict(OPENCODE_REQUIRED_PERMISSIONS)}},
        'provider': {
            OPENCODE_LOCAL_PROVIDER_ID: {
                'npm': '@ai-sdk/openai-compatible',
                'name': 'Local LLM API',
                'options': {
                    'baseURL': base_url,
                    'apiKey': '{env:COMS_OPENCODE_API_KEY}',
                },
                'models': {
                    model['id']: {
                        'name': model['name'],
                        **({'limit': {
                            'context': model['context'],
                            'output': min(DEFAULT_OPENCODE_OUTPUT_TOKENS, model['context']),
                        }} if model['context'] is not None else {}),
                    }
                    for model in models
                },
            },
        },
    }
    model_ids = [model['id'] for model in models]
    default_model = next((model_id for model_id in (selected, model_hint) if model_id in model_ids), '')
    if not default_model and model_ids:
        default_model = model_ids[0]
    if default_model:
        config['model'] = OPENCODE_LOCAL_PROVIDER_ID + '/' + default_model
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, separators=(',', ':'))
    # OpenCode otherwise hides websearch for custom/local providers. This is
    # tool registration, independent of whether the model chooses to call it.
    env['OPENCODE_ENABLE_EXA'] = '1'
    env['COMS_OPENCODE_API_KEY'] = api_key
    return env


def _release_acp_db():
    """Callback threads must release SQLite handles, especially on Windows."""
    if threading.current_thread().name.startswith('acp-'):
        conn=getattr(db._local,'conn',None)
        if conn: conn.close()
        db._local.__dict__.clear()


def board_servers(actor, ticket_id=None, run_id=None, readonly=False, collaboration_message_id=None, dispatch_only=False, monitor_only=False):
    env = {'COMS_DB': str(db.db_path().resolve()), 'COMS_AGENT': actor}
    if ticket_id is not None: env['COMS_TICKET_ID'] = str(ticket_id)
    if run_id is not None: env['COMS_RUN_ID'] = str(run_id)
    if collaboration_message_id is not None: env['COMS_TEAM_MESSAGE_ID'] = str(collaboration_message_id)
    return [{'name': 'swarm-agent', 'command': sys.executable,
             'args': [str(ROOT / 'coms' / 'mcp.py')] + (['--readonly'] if readonly else []) + (['--collaboration'] if collaboration_message_id is not None else []) + (['--dispatch'] if dispatch_only else []) + (['--monitor'] if monitor_only else []),
             'env': [{'name': k, 'value': v} for k, v in env.items()]}]

# These rules apply before role/specialty guidance in worker, chat and response
# sessions. Team messages notify people; board records preserve their findings.
SHARED_COLLABORATION_PROMPT = """SHARED COLLABORATION RULES
Own your assigned scope and finish it. Research your topic yourself; ask a teammate only for a specific unresolved evidence gap or decision. Questions and new skills are optional, never completion quotas. Reuse existing findings instead of expanding scope or researching for each other indefinitely.
Team roots use type=job. Main chat creates the root only; assigned leads staff with team_delegate. Synthesis reads completed child results instead of repeating delegation from the original request. A completed scope can receive at most one follow-up per teammate/job, with followup_reason naming an unmet acceptance criterion. Successful ticket_result ends execution and moves active work to review; never create more tickets or restart the assignment after delivery.
Reusable questions belong on the Questions page so future tasks benefit. Search with board_search and read questions_get first. Create questions_ask only for a real gap. Use ticket_link(kind=question, ref=the question ID) before team_send. Announce the ID/link with team_note, then team_send(question_id=that ID) to the appropriate teammate. This pauses your turn. Use team_note for scheduling, guidance, handoffs and review failure notices; these do not need new knowledge questions.
Responders read questions_get and save evidence, citations and limitations with questions_answer before team_reply. Reply with the question ID/link and a short summary. Reuse your existing answer when adequate. Legacy chat-only requests remain answerable with team_reply. Scoped response/monitor turns cannot create new questions or delegate work.
Finish your own wiki evidence page, link it, then team_request_review for its exact revision. Workers normally choose the registered orchestrator from team_roster; the lead's synthesis needs an independent peer. Review promptly: wiki_get, cited source_get, then team_review with accepted or changes_requested and concrete reasons. Stay within the assigned page and criteria; do not survey unrelated artifacts. Report limitations honestly, never invent verification.
Keep pending and accepted pages unchanged. After acceptance, immediately call ticket_result, verify success and END THE TASK. Do not restart research, add refinements or request another review. On changes_requested, fix only the cited issues. Equivalent failures keep their retry budget: after exhaustion use at most one qualified alternate reviewer, or record a concrete blocker with ticket_comment and finish. Never repeatedly resubmit or bypass approval. Publish a new shared skill only when a verified reusable method emerged.
"""

# Shared instructions are deliberately prepended to every execution agent's
# template and ticket-specific prompt. The board is the durable record; the
# configured workspace is not monitored for useful deliverables.
BASE_AGENT_SYSTEM_PROMPT = f"""You are an execution agent working through Swarm Agent. The Swarm Agent database is the system of record. Files you create or change in the workspace are not monitored, indexed, or automatically attached to the ticket, so a file by itself does not count as a delivered result.

{SHARED_COLLABORATION_PROMPT}

Use the attached Swarm Agent MCP tools for ALL board reads and writes. Use board_search before creating knowledge, wiki_put/wiki_append for durable documentation, questions_ask/questions_answer for Q&A, and ticket_link for references. Read relevant enabled skill folders with skill_list and skill_get before acting; every worker and collaboration agent can read every enabled skill in the repository-root skills/ folder. Do not write board deliverables to local markdown files. For a standalone ticket, call ticket_result with the delivered board IDs/links and verification before finishing. For a strict collaborative ticket, finish WITHOUT ticket_result while your request is pending; deliver immediately once the cited current wiki revision is accepted. No teammate question is required. A tool call counts only if it succeeded. If tools are unavailable, record the blocker and finish; do not substitute filesystem output or CLI writes.

Every agent can use native tools to inspect its workspace, run commands and perform the assigned work. For local code, tests or documents, record the inspected file lines with source_read_file and cite the returned source URL/ID in your wiki page. Use source_fetch for public HTTPS evidence when relevant. Local audits do not require unrelated web research. Record actual test commands and outcomes, including failures. team_read returns a bounded index: use message_id for a particular message, source_after_id to page through sources, and source_get for full evidence. If repeated reads reveal no new information, act on the assignment or record a concrete blocker and finish the turn.

Use your backend's native web search to discover current sources and URL fetching to read known URLs (websearch and webfetch in OpenCode); do not invent repository or documentation URLs. For collaborative evidence, record the chosen public page with source_fetch before citing it. Shared Swarm Agent procedures are loaded with the MCP skill_get tool, rather than the backend's native skill tool. If a tool fails, inspect the error and correct its arguments or report the blocker instead of repeating an unchanged failed call.

When asked to manage recurring agent work, inspect existing tasks with schedule_list/schedule_get and use schedule_create/schedule_update, schedule_pause/schedule_resume, schedule_run_now or schedule_delete as requested. Select a registered non-human agent and provide the requested workspace and timezone. Read the task back after changes and report the schedule ID and next run.

Choose the board destination that matches the requested work:
- For reusable knowledge, research findings, procedures, or documentation, create or update a Swarm Agent wiki page with wiki_put or wiki_append. Search/list the wiki first to avoid duplicate pages.
- For an unresolved question, ask it on the board with questions_ask; for an existing question, contribute with questions_answer.
- For requested work that changes software or other workspace artifacts, make those changes in the configured workspace, then record the outcome and relevant paths in the ticket result/comment. Do not substitute an untracked file for a requested wiki entry or board answer.
- Use ticket_comment only for concise, durable updates the requester should see. ACP responses and tool output are private diagnostics; do not post routine progress.

Before finishing, make sure the requested deliverable is actually present in the appropriate board record (wiki page, question/answer, or ticket result/comment) and report its link or identifier. If the request is ambiguous about whether knowledge belongs in the wiki or a question, use the wiki for durable reference material and Q&A for a specific question needing an answer."""

def orchestrator_system_prompt(agent, backend, selected_model):
    """The same role context is supplied to chat and isolated review sessions."""
    return (SHARED_COLLABORATION_PROMPT + '\n' +
            f"You are the Swarm Agent main orchestrator, running through the {backend} ACP backend with model {selected_model}. "
            "Do not claim to be running through a different provider or model. Help the user plan work and create tickets when asked. "
            "When asked to inspect the workspace or run a command, use your available native tools and report the observed output. If execution fails or output is unavailable, explain that clearly. "
            "Use native web search to find current sources and URL fetching to read known URLs (websearch and webfetch in OpenCode). Record public team evidence with source_fetch. Correct failed tool arguments or report a blocker; do not repeat unchanged failed calls. "
            "Use the attached Swarm Agent MCP tools for all board operations. Read relevant enabled skills with skill_list and skill_get; all agents can read every enabled root skills/ folder entry. Only the human operator and orchestrator can use skill_put to publish or edit a verified procedure; workers should share methods with the orchestrator. Call agent_templates before creating tickets with ticket_create. Publish documentation with wiki_put/wiki_append and Q&A with questions_ask/questions_answer; use schedule_list/schedule_get/schedule_create/schedule_update and the schedule controls when the user asks to manage recurring agent work. Do not write board records to local files. "
            "Choose p0/p1/p2/p3 based on urgency and impact. For requested work, set ready=true on actionable tickets; keep planning drafts, blocked dependencies, holds, required approvals and human assignments unready. "
            "When asked to stop or kill a team, call team_stop with its job_id and verify stopped=true. Process kills and STOP notes leave work eligible for restart. Never assign/tag a human ticket for agent execution. Keep the user informed and report ticket IDs. "
            f"You are {agent['name']}, the registered orchestrator agent {agent['slug']}. For team work in this chat, create ONLY one ready job assigned to the orchestrator, with scope and recommended_workers. END the chat turn after reporting its ID and team room. The runner owns staffing in its separate dispatch turn. Do not register/delegate teammates or create child tickets from this chat; doing both creates duplicate workers. The job lead owns synthesis after worker deliveries. A completion claim needs a freshly written, linked and peer-reviewed wiki artifact.\n")

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
        self.orchestrator_stop_event = None
        self.orchestrator_stop_turn_id = None
        self.orchestrator_health_checked = 0.0
        self.orchestrator_restarting = False
        self.review_conns = {}
        self.run_conns = {}  # Includes ACP startup and team-response connections.
        self.recorders = {}
        self.heartbeat_tickets = set()
        self.peer_review_keys = set()
        try: turn_capacity = max(1, min(16, int(os.environ.get('COMS_MODEL_PARALLEL_TURNS', '2'))))
        except ValueError: turn_capacity = 2
        self.turn_queue = TurnQueue(turn_capacity)
        self.team_guard_checked = 0.0
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
                raise RuntimeError('Another Swarm Agent runner already owns this database. Stop that server before starting another.')
            self._lease=lease
            try:
                if workspace is not None:
                    path=workspace_path({'workspace':workspace},create_default=False)
                    conn=db.connect()
                    conn.execute('INSERT OR REPLACE INTO runner_settings(key,value) VALUES(?,?)',('workspace',path))
                    conn.commit()
                conn=db.connect()
                api.orchestrator_agent(conn)
                api.seed_shared_skills(conn)
                active_settings=self.settings()
                active_backend=active_settings.get('active_backend','opencode')
                active_workspace=workspace_path(active_settings)
                conn.execute("UPDATE orchestrator_sessions SET backend=?,workspace=?,acp_session_id='' WHERE backend!=? OR workspace!=?",(active_backend,active_workspace,active_backend,active_workspace))
                conn.execute("UPDATE agent_runs SET status='failed',finished_at=?,error='Server stopped before this run finished; update the ticket to retry.' WHERE status IN ('queued','running')",(now(),))
                conn.execute("UPDATE scheduled_task_runs SET status='failed',error='Server stopped before this run finished.',finished_at=? WHERE status='running'", (now(),))
                conn.execute("UPDATE ticket_reviews SET status='failed',reason='Server stopped before review finished.',finished_at=? WHERE status IN ('queued','running')",(now(),))
                conn.execute("UPDATE agent_runtime SET state='failed',phase='failed',summary='Server stopped before this session finished.',updated_at=?,finished_at=? WHERE state='running'", (now(), now()))
                conn.execute("UPDATE team_deliveries SET status='pending',run_id=NULL,error='Resuming after server restart',updated_at=? WHERE status='responding'", (now(),))
                conn.commit()
                recover_turns(conn)
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
            connections.extend(c for _tid, c in self.run_conns.values())
            self.run_conns.clear()
            connections.extend(self.review_conns.values())
            with self.model_lock:
                connections.extend(self.model_conns.values())
                self.model_conns.clear()
            with self.orchestrator_conn_lock:
                if self.orchestrator_conn: connections.append(self.orchestrator_conn)
                self.orchestrator_conn=None; self.orchestrator_conn_key=None
            self.conns.clear()
        for c in dict.fromkeys(connections): c.cancel(); c.close()
        if self.thread: self.thread.join(timeout=5)
        if self._lease:
            self._lease.close(); self._lease=None
    def _conn(self, ticket_id, backend, cwd, run_id=None, actor_override=None, collaboration_message_id=None, dispatch_only=False, resume_session=None, monitor_only=False):
        actor = actor_override or api.get_ticket(db.connect(), ticket_id).get('assignee') or 'agent-runner'
        recorder = self.recorders.get(run_id)
        def log(line): self._message(ticket_id, run_id, 'system', 'acp-log', line)
        def notification(msg):
            params=msg.get('params') or {}; update=params.get('update') or {}
            if recorder:
                recorder.notification(update)
                if collaboration_message_id is None and not dispatch_only and not monitor_only and update.get('sessionUpdate') == 'tool_call_update':
                    record = recorder.tools.get(update.get('toolCallId'))
                    payload = record[1] if record else update
                    if payload.get('status') == 'completed' and payload.get('title', '').endswith(('_team_send', '_team_request_review')) and team.pending(db.connect(), ticket_id, actor):
                        # A small model may keep working despite instructions to
                        # yield. Pause after the durable request is recorded so
                        # the recipient can take its response turn immediately.
                        c.cancel()
                    if payload.get('status') == 'completed' and payload.get('title', '').endswith('ticket_result'):
                        delivered = self._result_delivered(db.connect(), run_id)
                        if delivered:
                            # Delivery is the execution boundary, even if the
                            # backend model keeps following its original task.
                            c.cancel()
                return
            text=update.get('content',{}).get('text') if isinstance(update.get('content'),dict) else None
            if text: self._message(ticket_id, run_id, 'system', 'acp-response', text)
            elif update: self._message(ticket_id, run_id, 'system', 'acp-'+str(update.get('sessionUpdate','event')), json.dumps(update,ensure_ascii=False))
        def request(msg, respond):
            options=(msg.get('params') or {}).get('options') or []
            chosen=next((x.get('optionId') for x in options if x.get('kind') in ('allow_once','allow_always','allow')), None)
            if chosen: respond({'outcome':{'outcome':'selected','optionId':chosen}})
            else: respond({'outcome':{'outcome':'cancelled'}})
        settings=self.settings(); cmd=backend_command(backend, settings)
        if not cmd: raise RuntimeError('No ACP command configured for '+backend)
        c=AcpConnection(cmd[0], cwd, command_args=cmd[1:], on_notification=notification, on_request=request, on_close=lambda _e:None, logger=log, on_thread_exit=_release_acp_db, env=backend_environment(backend,settings,{'COMS_DB':str(db.db_path().resolve()),'COMS_AGENT':actor}), mcp_servers=board_servers(actor,ticket_id,run_id,collaboration_message_id=collaboration_message_id,dispatch_only=dispatch_only,monitor_only=monitor_only))
        with self.lock:
            if run_id is not None: self.run_conns[run_id] = (ticket_id, c)
        if monitor_only:
            c.monitor_timer=threading.Timer(TEAM_MONITOR_SECONDS,c.close)
            c.monitor_timer.daemon=True
            c.monitor_timer.start()
        try:
            team.require_running(db.connect(), ticket_id)
            c.start()
            team.require_running(db.connect(), ticket_id)
            restored = False
            if resume_session and c.supports('loadSession'):
                # Session loading can replay history already stored on the board.
                c.on_notification = lambda _msg: None
                try:
                    c.session_load(resume_session, cwd=cwd)
                    restored = True
                except Exception as exc:
                    logging.info('Could not restore ticket ACP session; using board history: %s', exc)
                finally:
                    c.on_notification = notification
            if not restored: c.session_new(cwd=cwd)
            team.require_running(db.connect(), ticket_id)
            c.ticket_restored = restored
            self._apply_model(c,backend,self.settings()); return c
        except Exception:
            if monitor_only: c.monitor_timer.cancel()
            with self.lock: self.run_conns.pop(run_id, None)
            c.close(); raise
    @staticmethod
    def _apply_model(conn,backend,settings):
        chosen=(settings.get('model_'+backend) or '').strip()
        if not chosen: return
        options=next((o for o in conn.config_options if o.get('id')=='model'),{})
        allowed={x.get('value') for x in options.get('options') or []}
        if chosen not in allowed:
            if backend == 'opencode' and conn.current_model() in allowed:
                # The live /models catalog can advertise a new ID that differs
                # from a saved hint. backend_environment has already selected
                # the API's real ID as OpenCode's default in that case.
                return
            raise RuntimeError(f'Selected model {chosen!r} is not offered by the {backend} ACP session; refresh models in Runner settings and choose an available model.')
        conn.set_config_option(options.get('id','model'),chosen)
    def discover_models(self,backend):
        if backend not in ('codex','claude','opencode'): raise ValueError('backend must be codex, claude, or opencode')
        settings=self.settings(); cmd=backend_command(backend,settings)
        if not cmd: raise ValueError('No ACP command configured for '+backend)
        cwd=workspace_path(settings)
        if backend==settings.get('active_backend','opencode'):
            c=self._orchestrator_connection(backend,cwd,settings)
            if c is not None:
                option=next((o for o in c.config_options if o.get('id')=='model'),{})
                return {'backend':backend,'models':c.models(),'current':c.current_model(),'config_id':option.get('id','model')}
        key=(backend,tuple(cmd),cwd,settings.get('opencode_api_base_url',''),settings.get('opencode_api_key','') if backend=='opencode' else '',settings.get('opencode_api_model','') if backend=='opencode' else '')
        with self.model_lock:
            c=self.model_conns.get(key)
            if c is not None and not c.alive:
                self.model_conns.pop(key,None); c.close(); c=None
            if c is None:
                c=AcpConnection(cmd[0],cwd,command_args=cmd[1:],on_notification=lambda _m:None,on_request=lambda _m,respond:respond({'outcome':{'outcome':'cancelled'}}),on_close=lambda _e:None,logger=lambda _m:None,env=backend_environment(backend,settings,{'COMS_DB':str(db.db_path()),'COMS_AGENT':'model-discovery'}))
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
        conn=db.connect(); rows=conn.execute('SELECT key,value FROM runner_settings').fetchall(); settings={r[0]:r[1] for r in rows}
        if os.environ.get('COMS_IN_DOCKER') == '1':
            model=os.environ.get('COMS_LLAMACPP_MODEL','LFM2.5-2.6B-Q8_0.gguf').strip()
            docker_workspace=settings.get('workspace','/workspace')
            try: docker_workspace=workspace_path({'workspace':docker_workspace},create_default=False)
            except ValueError: docker_workspace='/workspace'
            settings.update(opencode_api_base_url=os.environ.get('COMS_LLAMACPP_BASE_URL','http://host.docker.internal:11434/v1'),
                            opencode_api_model=model, model_opencode=OPENCODE_LOCAL_PROVIDER_ID+'/'+model,
                            opencode_api_key='', workspace=docker_workspace)
        return settings
    def _message(self, tid, rid, role, kind, body):
        conn=db.connect(); conn.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)',(tid,rid,role,kind,str(body),now())); conn.commit()
    def _worker(self, key, function, *args):
        try: function(*args)
        except Exception:
            logging.exception('Swarm Agent worker %s failed', key)
        finally:
            with self.lock:
                self.active.pop(key,None)
                self.heartbeat_tickets.discard(key)
                self.peer_review_keys.discard(key)
            conn=getattr(db._local,'conn',None)
            if conn: conn.close()
            db._local.__dict__.clear()
    def _loop(self):
        while not self.stopping.wait(1):
            try:
                self._guard_teams()
                self._cancel_stopped_work()
                self._resume_waiting()
                self._team_heartbeat()
                self._restart_orchestrator_if_needed()
                settings=self.settings(); maximum=max(1,min(16,int(settings.get('parallel_agents','2'))))
                schedules.dispatch_due(db.connect())
                with self.lock:
                    conn=db.connect()
                    self._schedule_team_heartbeats(conn)
                    self._schedule_team_replies(conn, maximum)
                    if self._active_slots()>=maximum: continue
                    rows=conn.execute("SELECT t.*,a.role AS agent_role FROM tickets t LEFT JOIN agents a ON a.slug=t.assignee WHERE t.ready_for_execution=1 AND t.status IN ('open','in_progress') AND t.assignee NOT LIKE 'human%' AND lower(coalesce(a.role,''))!='human' AND lower(t.tags) NOT LIKE '%human%' AND NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.ticket_id=t.id AND r.status IN ('queued','running','waiting')) AND NOT EXISTS (SELECT 1 FROM agent_runs busy WHERE t.assignee!='' AND busy.agent_slug=t.assignee AND busy.status='running') ORDER BY CASE t.priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,t.created_at LIMIT 100").fetchall()
                    for row in rows:
                        if self._active_slots()>=maximum: break
                        if not self._automatic_run_retry_ready(conn, dict(row)): continue
                        if row['parent_id'] and conn.execute("SELECT 1 FROM agent_runs WHERE ticket_id=? AND status='running' AND stage='execute'", (row['parent_id'],)).fetchone(): continue
                        if row['id'] in self.active or 'review:'+str(row['id']) in self.active: continue
                        if 'team:'+row['assignee'] in self.active: continue
                        self.active[row['id']]=True
                        threading.Thread(target=self._worker,args=(row['id'],self._work,dict(row)),daemon=True).start()
                    slots=maximum-self._active_slots()
                    if slots>0:
                        review_rows=conn.execute("""SELECT t.*,a.role AS agent_role,
                            coalesce((SELECT id FROM agent_runs ar WHERE ar.ticket_id=t.id AND ar.status='completed' AND ar.stage!='collaborate' ORDER BY ar.id DESC LIMIT 1),0) AS review_run_id,
                            tr.id AS prior_review_id,tr.status AS prior_review_status,
                            tr.attempt_count AS prior_review_attempt_count,
                            tr.finished_at AS prior_review_finished_at,tr.ticket_version AS prior_review_ticket_version
                            FROM tickets t LEFT JOIN agents a ON a.slug=t.assignee
                            LEFT JOIN ticket_reviews tr ON tr.ticket_id=t.id
                              AND tr.source_run_id=coalesce((SELECT id FROM agent_runs ar2 WHERE ar2.ticket_id=t.id AND ar2.status='completed' AND ar2.stage!='collaborate' ORDER BY ar2.id DESC LIMIT 1),0)
                              AND tr.ticket_version=t.updated_at
                            WHERE t.status='review' AND t.assignee NOT LIKE 'human%'
                              AND lower(coalesce(a.role,''))!='human' AND lower(t.tags) NOT LIKE '%human%'
                              AND (tr.id IS NULL OR (tr.status='failed' AND tr.attempt_count<?
                                AND (tr.finished_at='' OR julianday(tr.finished_at)+(? * (1 << max(0,tr.attempt_count-1))) / 86400.0 <= julianday(?))))
                            ORDER BY CASE t.priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,t.updated_at LIMIT ?""",
                            (MAX_REVIEW_ATTEMPTS,REVIEW_RETRY_BASE_SECONDS,now(),slots)).fetchall()
                        for row in review_rows:
                            if row['prior_review_status']=='failed' and not _review_retry_due(row['prior_review_attempt_count'],row['prior_review_finished_at']): continue
                            if row['id'] in self.active: continue
                            lead_slug = api.orchestrator_agent(conn)['slug']
                            if 'team:'+lead_slug in self.active or self.orchestrator_busy: continue
                            if conn.execute("SELECT 1 FROM agent_runs WHERE agent_slug=? AND status='running'", (lead_slug,)).fetchone(): continue
                            if not api.children_resolved(conn, row['id']): continue
                            key='review:'+str(row['id']); review_key=row['review_run_id']
                            prior=conn.execute('SELECT id,status,attempt_count,ticket_version FROM ticket_reviews WHERE ticket_id=? AND source_run_id=?',(row['id'],review_key)).fetchone()
                            if prior and prior['status'] in ('queued','running'): continue
                            if prior:
                                review_id=prior['id']
                                attempt_count=1 if prior['ticket_version']!=row['updated_at'] else int(prior['attempt_count'] or 0)+1
                                if attempt_count>MAX_REVIEW_ATTEMPTS: continue
                                conn.execute("UPDATE ticket_reviews SET backend=?,status='queued',decision='',reason='',ticket_version=?,attempt_count=?,created_at=?,finished_at='' WHERE id=? AND status!='cancelled'",(settings.get('active_backend','opencode'),row['updated_at'],attempt_count,now(),review_id)); conn.commit()
                            else:
                                cur=conn.execute("INSERT INTO ticket_reviews(ticket_id,source_run_id,backend,status,ticket_version,attempt_count,created_at) VALUES(?,?,?,'queued',?,1,?)",(row['id'],review_key,settings.get('active_backend','opencode'),row['updated_at'],now())); review_id=cur.lastrowid; conn.commit()
                            self.active[key]=True
                            threading.Thread(target=self._worker,args=(key,self._review_ticket,dict(row),review_id,review_key),daemon=True).start()
            except Exception:
                logging.exception('Swarm Agent runner scheduling failed')
                self.stopping.wait(1)

    def _cancel_stopped_work(self):
        """Close held work without turning cancellation into a retryable failure."""
        with self.lock:
            stopped = {}
            for rid, (tid, connection) in list(self.run_conns.items()):
                if team.is_stopped(db.connect(), tid) or db.connect().execute("SELECT 1 FROM agent_runs WHERE id=? AND status='cancelled'", (rid,)).fetchone():
                    stopped[id(connection)] = connection
                    self.run_conns.pop(rid, None)
            for connections in (self.conns, self.review_conns):
                for tid, connection in list(connections.items()):
                    if team.is_stopped(db.connect(), tid):
                        stopped[id(connection)] = connection
                        connections.pop(tid, None)
        for connection in stopped.values():
            try: connection.cancel()
            except Exception: logging.info('Held worker did not acknowledge cancellation', exc_info=True)
            connection.close()

    def _finish_stopped_run(self, tid, rid, recorder):
        conn = db.connect()
        cancelled = conn.execute("SELECT error FROM agent_runs WHERE id=? AND status='cancelled'", (rid,)).fetchone()
        if not team.is_stopped(conn, tid) and not cancelled:
            return False
        reason = cancelled['error'] if cancelled and cancelled['error'] else 'Team stopped; work is on hold'
        conn.execute("UPDATE agent_runs SET status='cancelled',finished_at=?,error=? WHERE id=?", (now(), reason, rid))
        schedules.mark_ticket(conn, tid, 'cancelled', reason)
        conn.commit()
        if recorder: recorder.finish('cancelled', reason)
        return True

    @staticmethod
    def _result_delivered(conn, run_id):
        return any(json.loads(row['body']).get('tool') == 'ticket_result' for row in
                   conn.execute("SELECT body FROM agent_messages WHERE run_id=? AND kind='board-delivery'", (run_id,)))

    def _complete_delivery(self, tid, rid, recorder):
        conn = db.connect()
        conn.execute("UPDATE agent_runs SET status='completed',finished_at=?,error='' WHERE id=? AND status!='cancelled'", (now(), rid))
        conn.execute("UPDATE tickets SET status='review',updated_at=? WHERE id=? AND status='in_progress'", (now(), tid))
        schedules.mark_ticket(conn, tid, 'completed')
        conn.commit()
        self._message(tid, rid, 'system', 'status', 'Deliverable recorded; ticket is awaiting review.')
        recorder.finish('completed', 'Deliverable recorded; ticket is awaiting review')

    def _resume_waiting(self):
        conn = db.connect()
        team.retire_obsolete_reviews(conn)
        conn.commit()
        resumed = []
        rows = conn.execute("SELECT r.id AS run_id,r.ticket_id,r.agent_slug,t.result,t.collaboration_required FROM agent_runs r JOIN tickets t ON t.id=r.ticket_id WHERE r.status='waiting' AND t.ready_for_execution=1 AND t.status IN ('open','in_progress')").fetchall()
        for row in rows:
            if team.pending(conn, row['ticket_id'], row['agent_slug']): continue
            ready = api.children_resolved if row['collaboration_required'] else api.children_delivered
            if not ready(conn, row['ticket_id']): continue
            delivered = bool(row['result'].strip() and self._result_delivered(conn, row['run_id']))
            if delivered:
                # Recover legacy waiting runs that already published, without
                # replaying their assignment or repeating staffing.
                try: api.deliver_ticket(conn, row['ticket_id'], row['agent_slug'], row['result'])
                except api.BadRequest: delivered = False  # Revalidate/revise superseded evidence during synthesis.
            conn.execute("UPDATE agent_runs SET status='completed',finished_at=? WHERE id=? AND status='waiting'", (now(), row['run_id']))
            summary = 'Previously delivered result queued for review.' if delivered else 'Teammate replies or results are ready; agent queued to resume.'
            conn.execute("UPDATE agent_runtime SET state='completed',phase='completed',summary=?,updated_at=?,finished_at=? WHERE key=? AND state!='cancelled'", (summary, now(), now(), 'run:' + str(row['run_id'])))
            api.log_activity(conn, row['agent_slug'], 'runner.resume', f"ticket:{row['ticket_id']}", summary)
            if not delivered: resumed.append(row['ticket_id'])
        conn.commit()
        return resumed

    def _guard_teams(self):
        current = time.monotonic()
        if current - self.team_guard_checked < 15: return
        self.team_guard_checked = current
        conn = db.connect()
        team.retire_obsolete_reviews(conn)
        conn.commit()
        team.check_progress(conn)
        if shutil.disk_usage(tempfile.gettempdir()).free < 16 * 1024 * 1024:
            jobs = conn.execute("SELECT t.id FROM tickets t LEFT JOIN agents a ON a.slug=t.assignee WHERE t.type='job' AND t.collaboration_required=1 AND t.ready_for_execution=1 AND t.status IN ('open','in_progress','review') AND lower(t.assignee) NOT LIKE 'human%' AND lower(t.tags) NOT LIKE '%human%' AND lower(coalesce(a.role,''))!='human'").fetchall()
            for job in jobs: team.hold(conn, job['id'], 'Insufficient temporary space for agent execution (need 16 MiB free)')

    def _prompt_turn(self, connection, prompt, *, backend, priority=2, ticket_id=None, cancelled=lambda: False, **timeouts):
        def stopped():
            closed = getattr(connection, '_closed', None)
            return self.stopping.is_set() or cancelled() or (closed is not None and closed.is_set() is True)
        def run():
            if stopped(): raise AcpError('Agent turn cancelled before prompt')
            if ticket_id is not None: team.require_running(db.connect(), ticket_id)
            return connection.prompt(prompt, **timeouts)
        if backend != 'opencode': return run()
        with self.turn_queue.turn(priority, cancelled=stopped, timeout=timeouts.get('total_timeout', timeouts.get('timeout', WORKER_TURN_SECONDS))):
            return run()

    def _team_heartbeat(self):
        """Wake stale job leads for a short check-in while child work is active."""
        cutoff=(datetime.now(timezone.utc)-timedelta(seconds=TEAM_HEARTBEAT_SECONDS)).isoformat()
        conn=db.connect()
        rows=conn.execute("""SELECT t.id,t.assignee,r.id AS run_id,
                   coalesce(nullif(rt.updated_at,''),nullif(r.finished_at,''),r.started_at) AS updated_at
            FROM tickets t JOIN agents a ON a.slug=t.assignee
            JOIN agent_runs r ON r.ticket_id=t.id AND r.status IN ('waiting','failed')
              AND r.stage IN ('execute','synthesize','heartbeat')
              AND r.id=(SELECT max(latest.id) FROM agent_runs latest
                        WHERE latest.ticket_id=t.id AND latest.stage!='collaborate')
            LEFT JOIN agent_runtime rt ON rt.key='run:'||r.id
            WHERE t.type='job' AND t.ready_for_execution=1 AND t.status='in_progress'
              AND lower(a.role) IN ('orchestrator','lead')
              AND lower(t.assignee) NOT LIKE 'human%' AND lower(t.tags) NOT LIKE '%human%'
              AND EXISTS(SELECT 1 FROM tickets child WHERE child.parent_id=t.id
                         AND child.status NOT IN ('done','wontfix'))
              AND NOT EXISTS(SELECT 1 FROM agent_runs active WHERE active.ticket_id=t.id
                             AND active.status IN ('queued','running'))
            ORDER BY coalesce(rt.updated_at,r.finished_at,r.started_at) LIMIT 40""").fetchall()
        queued=[]
        with self.lock:
            for row in rows:
                last_check=row['updated_at'] or ''
                if last_check and last_check > cutoff: continue
                tid=int(row['id'])
                if tid in self.active or 'review:'+str(tid) in self.active: continue
                changed=conn.execute("UPDATE agent_runs SET status='completed',finished_at=? WHERE id=? AND status IN ('waiting','failed')",
                                     (now(),row['run_id'])).rowcount
                if not changed: continue
                conn.execute("UPDATE agent_runtime SET state='completed',phase='heartbeat',summary='Team heartbeat due; lead will check active teammates.',updated_at=?,finished_at=? WHERE key=? AND state!='cancelled'",
                             (now(),now(),'run:'+str(row['run_id'])))
                api.log_activity(conn,row['assignee'],'team.heartbeat',f'ticket:{tid}',
                                 f'Waking the lead for a team heartbeat on job #{tid}')
                queued.append(tid)
        if queued: conn.commit()

    def _restart_orchestrator_if_needed(self):
        """Periodically replace a dead idle ACP process without waiting for a send."""
        current=time.monotonic()
        if current-self.orchestrator_health_checked < ORCHESTRATOR_RESTART_SECONDS: return
        self.orchestrator_health_checked=current
        if shutil.disk_usage(tempfile.gettempdir()).free < 16 * 1024 * 1024: return
        with self.lock:
            if self.orchestrator_busy or self.stopping.is_set() or self.orchestrator_restarting: return
            connection=self.orchestrator_conn
            if connection is not None and connection.alive: return
            self.orchestrator_restarting=True
        def restart():
            try:
                with self.orchestrator_conn_lock:
                    if self.orchestrator_busy or self.stopping.is_set(): return
                    current=self.orchestrator_conn
                    if current is not None and current.alive: return
                    if current is not None: current.close()
                    self.orchestrator_conn=None; self.orchestrator_conn_key=None
                self._warm_orchestrator()
            finally:
                with self.lock: self.orchestrator_restarting=False
        # ACP initialization may take a minute; keep the worker scheduler free.
        threading.Thread(target=restart,daemon=True,name='coms-orchestrator-health').start()

    def _active_slots(self):
        return sum(key not in self.heartbeat_tickets and key not in self.peer_review_keys
                   for key in self.active)

    def _schedule_team_heartbeats(self,conn):
        """One bounded supervision slot remains available even if workers are full."""
        if self.stopping.is_set() or any(tid in self.active for tid in self.heartbeat_tickets): return
        rows=conn.execute("""SELECT t.* FROM tickets t JOIN agents a ON a.slug=t.assignee
            JOIN agent_runs r ON r.ticket_id=t.id
            JOIN agent_runtime rt ON rt.key='run:'||r.id
            WHERE t.type='job' AND t.ready_for_execution=1 AND t.status='in_progress'
              AND lower(a.role) IN ('orchestrator','lead') AND lower(t.tags) NOT LIKE '%human%'
              AND lower(t.assignee) NOT LIKE 'human%'
              AND r.status='completed' AND rt.phase='heartbeat'
              AND r.id=(SELECT max(latest.id) FROM agent_runs latest
                        WHERE latest.ticket_id=t.id AND latest.stage!='collaborate')
              AND NOT EXISTS(SELECT 1 FROM agent_runs busy
                             WHERE busy.agent_slug=t.assignee AND busy.status IN ('queued','running'))
            ORDER BY rt.updated_at LIMIT 40""").fetchall()
        for row in rows:
            tid=row['id']
            if tid in self.active or 'review:'+str(tid) in self.active or 'team:'+row['assignee'] in self.active: continue
            ready=api.children_resolved if row['collaboration_required'] else api.children_delivered
            if ready(conn,tid): continue
            self.active[tid]=True; self.heartbeat_tickets.add(tid)
            try:
                threading.Thread(target=self._worker,args=(tid,self._work,dict(row)),daemon=True).start()
            except Exception:
                self.active.pop(tid,None); self.heartbeat_tickets.discard(tid)
                raise
            break

    @staticmethod
    def _automatic_run_retry_ready(conn, ticket):
        if ticket.get('agent_retry_requested'):
            return True
        latest = conn.execute("SELECT status,stage,finished_at FROM agent_runs WHERE ticket_id=? AND stage!='collaborate' ORDER BY id DESC LIMIT 1", (ticket['id'],)).fetchone()
        if not latest or latest['status'] != 'failed' or latest['stage'] == 'collaborate':
            return True
        return _agent_retry_due(ticket.get('agent_retry_count', 0), latest['finished_at'])

    def _schedule_team_replies(self, conn, maximum):
        team.retire_obsolete_reviews(conn)
        conn.commit()
        if self.stopping.is_set(): return
        rows = conn.execute("SELECT d.message_id,d.recipient,a.role,m.kind FROM team_deliveries d JOIN team_messages m ON m.id=d.message_id JOIN agents a ON a.slug=d.recipient JOIN tickets j ON j.id=m.job_id WHERE d.status='pending' AND d.attempt_count<? AND (d.attempt_count=0 OR julianday(d.updated_at)+(? * (1 << max(0,d.attempt_count-1))) / 86400.0 <= julianday(?)) AND j.ready_for_execution=1 AND j.status!='wontfix' AND lower(a.role)!='human' AND NOT EXISTS(SELECT 1 FROM agent_runs r WHERE r.agent_slug=d.recipient AND r.status='running' AND (r.stage='collaborate' OR lower(a.role)!='orchestrator' OR m.kind!='review_request')) ORDER BY CASE m.kind WHEN 'review_request' THEN 0 ELSE 1 END,d.message_id LIMIT 40", (MAX_TEAM_ATTEMPTS, TEAM_RETRY_BASE_SECONDS, now())).fetchall()
        for row in rows:
            is_review = row['kind'] == 'review_request'
            reserved = is_review and not self.peer_review_keys
            if not reserved and self._active_slots() >= maximum: continue
            key = 'team:' + row['recipient']
            if key in self.active: continue
            if row['role'].lower() == 'orchestrator' and not is_review and (self.orchestrator_busy or any(str(k).startswith('review:') for k in self.active)): continue
            self.active[key] = True
            if reserved: self.peer_review_keys.add(key)
            try:
                threading.Thread(target=self._worker, args=(key, self._reply_team, row['message_id'], row['recipient']), daemon=True).start()
            except Exception:
                self.active.pop(key, None); self.peer_review_keys.discard(key)
                raise

    def _reply_team(self, message_id, recipient):
        team.retire_obsolete_reviews(db.connect())
        db.connect().commit()
        conn = db.connect(); settings = self.settings(); backend = settings.get('active_backend', 'opencode')
        c = None; recorder = None; rid = None
        conn.execute('BEGIN IMMEDIATE')
        message = conn.execute("SELECT m.*,d.attempt_count FROM team_messages m JOIN team_deliveries d ON d.message_id=m.id WHERE m.id=? AND d.recipient=? AND d.status='pending' AND d.attempt_count<?", (message_id, recipient, MAX_TEAM_ATTEMPTS)).fetchone()
        if not message:
            conn.rollback(); return
        if team.is_stopped(conn, message['job_id']):
            conn.rollback(); return
        agent = api.get_agent(conn, recipient)
        isolated_review = agent['role'].lower() == 'orchestrator' and message['kind'] == 'review_request'
        if conn.execute("SELECT 1 FROM agent_runs WHERE agent_slug=? AND status='running' AND (stage='collaborate' OR ?=0)", (recipient, isolated_review)).fetchone():
            conn.rollback(); return
        assigned = conn.execute('SELECT id FROM tickets WHERE assignee=? AND (id=? OR parent_id=?) ORDER BY id LIMIT 1', (recipient, message['job_id'], message['job_id'])).fetchone()
        tid = assigned['id'] if assigned else message['job_id']
        if team.is_stopped(conn, tid):
            conn.rollback(); return
        rid = conn.execute("INSERT INTO agent_runs(ticket_id,backend,status,started_at,agent_slug,stage) VALUES(?,?,'running',?,?,'collaborate')", (tid, backend, now(), recipient)).lastrowid
        conn.execute("UPDATE team_deliveries SET status='responding',run_id=?,attempt_count=attempt_count+1,updated_at=? WHERE message_id=? AND recipient=?", (rid, now(), message_id, recipient))
        conn.execute("UPDATE peer_reviews SET status='requested',reason='',updated_at=? WHERE message_id=? AND status='failed'", (now(), message_id)); conn.commit()
        recorder = progress.Recorder('run:' + str(rid), recipient, tid, rid, backend)
        self.recorders[rid] = recorder
        try:
            ticket = api.get_ticket(conn, tid)
            cwd = workspace_path({'workspace': ticket.get('workspace') or settings.get('workspace', str(DEFAULT_WORKSPACE))})
            c = self._conn(tid, backend, cwd, rid, actor_override=recipient, collaboration_message_id=message_id)
            conn.execute('UPDATE agent_runs SET session_id=? WHERE id=?', (c.session_id, rid)); conn.commit()
            review = conn.execute('SELECT * FROM peer_reviews WHERE message_id=?', (message_id,)).fetchone()
            prompt = (orchestrator_system_prompt(agent, backend, settings.get('model_' + backend) or 'ACP default')
                      if agent['role'].lower() == 'orchestrator' else SHARED_COLLABORATION_PROMPT)
            prompt += (f"You are {agent['name']} ({recipient}), role {agent['role']}. Capabilities: {agent['description']}. Instructions: {agent.get('instructions', '')}.\n"
                      f"CURRENT TURN CONTRACT: TEAM RESPONSE TURN for job #{message['job_id']}. These restrictions override general role instructions. Decide only this request. Do not publish ticket_result, edit artifacts, create questions, publish skills or delegate. Read the assigned wiki/source/question records; team_read is optional if needed to resolve this request.\n"
                      f"Message #{message_id} from {message['sender']} to you:\n{message['body']}\n\n")
            if review:
                reviewed_ticket = api.get_ticket(conn, review['ticket_id'])
                artifact = api.get_wiki(conn, review['artifact_slug'])
                cited = {row['url']: {k: row[k][:500] if isinstance(row[k], str) else row[k] for k in ('id', 'url', 'status', 'error')} for row in conn.execute('SELECT id,url,final_url,status,error FROM research_sources WHERE job_id=? ORDER BY id', (message['job_id'],)) if row['url'] in artifact['body'] or (row['final_url'] and row['final_url'] in artifact['body'])}
                prompt += (f"REVIEWED TICKET ASSIGNMENT #{reviewed_ticket['id']}: {reviewed_ticket['title']}\n"
                           f"Description and acceptance criteria:\n{reviewed_ticket['body'][:4000]}\n"
                           f"Read ticket_get(id={reviewed_ticket['id']}) if criteria are truncated.\n"
                           f"CITED SOURCES (up to eight; read with source_get, team_read for more; failed records are limitations):\n{json.dumps(list(cited.values())[:8], ensure_ascii=False)}\n")
                prompt += f"Peer review #{review['id']} of [[{review['artifact_slug']}]]: call wiki_get, inspect cited evidence with source_get (use source_read_file or native tools for current workspace files, source_fetch for public URLs), then team_review(review_id={review['id']}, decision=accepted or changes_requested, body=concrete findings). Do not rubber-stamp unsupported claims.\n"
            else:
                if message['question_id'] is not None:
                    prompt += f"Assigned Questions record: questions_get(id={message['question_id']}); save your answer there before team_reply.\n"
                prompt += 'For a linked board question, use questions_get and questions_answer before team_reply; save evidence and limitations on the Questions page and include its ID/link in the reply. For a legacy chat-only question, answer with team_reply so the sender can preserve the finding on resumption.\n'
                prompt += f"Answer with team_reply(message_id={message_id}, body=your useful response). An assistant response alone does not deliver the reply. Give specific reasoning and identify uncertainties.\n"
            prompt += f'\nYour workspace is {cwd}. Native tools are available for inspecting the assignment and evidence. Use native web search to discover sources and URL fetching for known URLs (websearch and webfetch in OpenCode); record public evidence with source_fetch and local evidence with source_read_file. Read source_get for the cited snapshot. Read shared Swarm Agent procedures with MCP skill_get. Do not repeatedly poll unchanged team_read output.\n'
            prompt += api.shared_skill_context(conn)
            if review:
                prompt += f"\nTEAM CONVERSATION: Focused review of ticket #{review['ticket_id']}, author {review['author']}, revision {review['artifact_version']}. Other team work is outside this turn.\n"
            else:
                prompt += '\n' + team.context(conn, tid, recipient, instructions=False)
                prompt += '\n' + api.triggered_skill_context(conn, message['body'], recipient)
            prompt += '\nREQUIRED ACTION: deliver the requested team_review decision or team_reply using its tool, then finish this response turn. Do not publish ticket_result or create new work.\n'
            self._message(tid, rid, 'system', 'acp-prompt', prompt)
            recorder.set('collaborating', 'Answering ' + message['sender'] + (' — peer review' if review else ' — teammate question'))
            team.require_running(conn, tid)
            self._prompt_turn(c, prompt, backend=backend, priority=0 if review else 1, ticket_id=tid, timeout=180, total_timeout=180)
            if self._finish_stopped_run(tid, rid, recorder): return
            answered = conn.execute('SELECT status FROM team_deliveries WHERE message_id=? AND recipient=?', (message_id, recipient)).fetchone()
            if answered['status'] != 'answered': raise RuntimeError('Teammate finished without delivering team_reply or team_review')
            conn.execute("UPDATE agent_runs SET status='completed',finished_at=? WHERE id=? AND status!='cancelled'", (now(), rid)); conn.commit()
            recorder.finish('completed', 'Delivered a peer review' if review else 'Delivered a teammate reply')
        except Exception as exc:
            conn.rollback()
            if self._finish_stopped_run(tid, rid, recorder): return
            attempts = conn.execute('SELECT attempt_count FROM team_deliveries WHERE message_id=? AND recipient=?', (message_id, recipient)).fetchone()
            delivery_status = _team_retry_status(attempts['attempt_count'] if attempts else MAX_TEAM_ATTEMPTS)
            conn.execute("UPDATE team_deliveries SET status=?,error=?,updated_at=? WHERE message_id=? AND recipient=? AND status NOT IN ('answered','cancelled')", (delivery_status, str(exc), now(), message_id, recipient))
            conn.execute("UPDATE peer_reviews SET status='failed',reason=?,updated_at=? WHERE message_id=? AND status='requested'", (str(exc), now(), message_id))
            conn.execute("UPDATE agent_runs SET status='failed',error=?,finished_at=? WHERE id=? AND status!='cancelled'", (str(exc), now(), rid)); conn.commit()
            if recorder: recorder.finish('failed', str(exc))
        finally:
            if c: c.close()
            with self.lock: self.run_conns.pop(rid, None)
            self.recorders.pop(rid, None)
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
        if team.is_stopped(conn, tid):
            conn.rollback()
            return
        t=dict(current)
        previous_run=conn.execute("SELECT r.status,r.stage,rt.phase FROM agent_runs r LEFT JOIN agent_runtime rt ON rt.key='run:'||r.id WHERE r.ticket_id=? AND r.stage!='collaborate' ORDER BY r.id DESC LIMIT 1",(tid,)).fetchone()
        retry_count=int(t.get('agent_retry_count') or 0)
        if t.get('agent_retry_requested'):
            retry_count=0
            api.log_activity(conn, 'runner', 'runner.retry', f'ticket:{tid}', f'manual retry started for ticket #{tid}')
        elif previous_run and previous_run['status']=='failed' and previous_run['stage']!='collaborate':
            retry_count+=1
            api.log_activity(conn, 'runner', 'runner.retry', f'ticket:{tid}', f'automatic retry {retry_count}/{MAX_AGENT_RETRIES} started for ticket #{tid}')
        job=team.job_for_ticket(conn, tid)
        agent = api.resolve_ticket_agent(conn, t, commit=False)
        if conn.execute("SELECT 1 FROM agent_runs WHERE agent_slug=? AND status='running'", (agent['slug'],)).fetchone():
            conn.rollback()
            return
        t['assignee'] = agent['slug']
        children = api.list_tickets(conn, parent_id=tid, include_closed=True)
        children_ready = api.children_resolved if t.get('collaboration_required') else api.children_delivered
        heartbeat = bool(t['type'] == 'job' and children and not children_ready(conn, tid)
                         and previous_run and previous_run['status']=='completed' and previous_run['phase']=='heartbeat')
        if tid in self.heartbeat_tickets and not heartbeat:
            # Children finished after scheduling: synthesis uses a normal slot.
            conn.rollback()
            return
        stage = 'heartbeat' if heartbeat else 'synthesize' if children else 'execute'
        template=conn.execute('SELECT * FROM agent_templates WHERE id=? AND enabled=1',(t.get('agent_template_id'),)).fetchone() if t.get('agent_template_id') else None
        if template and template['backend']: backend=template['backend']
        cur=conn.execute("INSERT INTO agent_runs(ticket_id,template_id,backend,status,started_at,agent_slug,stage) VALUES(?,?,?,'running',?,?,?)",(tid,template['id'] if template else None,backend,now(),agent['slug'],stage)); rid=cur.lastrowid
        conn.execute("UPDATE tickets SET status='in_progress',updated_at=?,agent_retry_count=?,agent_retry_requested=0 WHERE id=?",(now(),retry_count,tid)); schedules.mark_ticket(conn, tid, 'running'); conn.commit()
        self._message(tid,rid,'system','status',f'Runner started with {backend}; priority {t["priority"]}.')
        recorder = progress.Recorder('run:' + str(rid), agent['slug'], tid, rid, backend)
        self.recorders[rid] = recorder
        c=None
        try:
            if t['type'] == 'job' and t.get('collaboration_required') and any(child['status'] not in ('done','wontfix') and not child['ready_for_execution'] and not child['assignee'] for child in children):
                team.hold(conn, tid, 'Unassigned, unready dependency blocks team completion; assign, enable or explicitly retire that draft')
                self._finish_stopped_run(tid, rid, recorder)
                return
            if children and not children_ready(conn,tid) and not heartbeat:
                conn.execute("UPDATE agent_runs SET status='waiting' WHERE id=? AND status!='cancelled'",(rid,)); schedules.mark_ticket(conn,tid,'waiting'); conn.commit()
                recorder.finish('waiting','Waiting for existing teammates; heartbeat will check their progress')
                return
            if t.get('agent_template_id') and not template:
                raise RuntimeError('Assigned agent template is missing or disabled; choose an enabled template before retrying.')
            cwd=workspace_path({'workspace': t.get('workspace') or settings.get('workspace', str(DEFAULT_WORKSPACE))})
            with self.lock:
                # Keep reusable ticket chats bounded; running sessions are never evicted.
                maximum=max(1,min(16,int(settings.get('parallel_agents','2'))))
                while len(self.conns)>=maximum:
                    idle=next((key for key in self.conns if key not in self.active),None)
                    if idle is None: break
                    self.conns.pop(idle).close()
            previous = self.conns.pop(tid, None)
            if previous: previous.close()
            dispatch_only = t['type'] == 'job' and stage == 'execute' and bool(t.get('collaboration_required'))
            c=self._conn(tid,backend,cwd,rid,dispatch_only=dispatch_only,monitor_only=heartbeat); self.conns[tid]=c
            conn=db.connect(); conn.execute('UPDATE agent_runs SET session_id=? WHERE id=?',(c.session_id or '',rid)); conn.commit()
            prompt=BASE_AGENT_SYSTEM_PROMPT+'\n\n'
            prompt += f"REGISTERED AGENT\nName: {agent['name']}\nIdentity: {agent['slug']}\nRole: {agent['role']}\nCapabilities: {agent['description']}\nInstructions: {agent.get('instructions', '')}\n\n"
            prompt += api.shared_skill_context(conn) + '\n\n'
            prompt += api.triggered_skill_context(conn, t['title'] + '\n' + t['body'], agent['slug'])
            prompt += team.context(conn, tid, agent['slug']) + '\n\n'
            if t['type'] == 'job' or children:
                prompt += ("ORCHESTRATION JOB\nYou are the lead for this parent ticket. Prefer reusing teammates and completed findings. Staffing uses team_delegate from an assigned job, never raw ticket_create. Workers deliver findings on their own tickets; only the lead owns the combined wiki page. After delegation, finish your dispatch turn without ticket_result; the runner waits for worker results and resumes you automatically for synthesis. Reserve ticket_result for the final synthesis after your teammates deliver.\n"
                           f"Suggested workers: {t.get('recommended_workers') or 'choose the number yourself'}. Maximum concurrent ticket slots: {settings.get('parallel_agents', '2')}. Choose distinct personal names for new specialists and put expertise in their descriptions. Register the whole team BEFORE making their tickets ready. Record your staffing decision with team_note. Workers use native tools for their assignments, record local file evidence with source_read_file or relevant public evidence with source_fetch, exchange questions and obtain peer review.\n\n")
            if stage == 'synthesize':
                prompt += 'SYNTHESIS: Staffing has already happened. Read delivered worker results and verify evidence; do not replay delegation from the original description. Own the combined page. Only a job can use team_delegate for one targeted follow-up per teammate, with followup_reason naming an unmet acceptance criterion. Never duplicate completed scopes. Otherwise publish/link the synthesis, obtain independent peer review, call ticket_result and END THE TASK. A successful result ends execution and queues final review. Publish a new shared skill only if a verified reusable method emerged; it is optional.\n'
                prompt += '\n'.join(f"Child #{child['id']} ({child['assignee']}), {child['status']}: {child['title']}\nResult: {child['result']}" for child in children) + '\n\n'
            if template and template['prompt'].strip():
                prompt += 'AGENT TEMPLATE INSTRUCTIONS (follow these within the shared Swarm Agent requirements above):\n'+template['prompt'].strip()+'\n\n'
            collaborative_ticket = bool(job and job.get('collaboration_required') and t.get('collaboration_required') and t['type'] != 'job')
            result_gate = ('If a question or peer review is pending, end this turn without ticket_result. Once your current linked page is accepted, immediately call ticket_result, verify success and end. Questions are optional.'
                           if collaborative_ticket else 'Call ticket_result with the delivered board IDs/links and verification before concluding; verify its success response.')
            prompt += (f"TICKET ASSIGNMENT\nYou are the execution agent for Swarm Agent ticket #{tid}.\n"
                       f"Ticket: {t['title']}\nPriority: {t['priority']}\nDescription:\n{t['body']}\n\n"
                       f"Use your ACP response stream for progress; those messages are private run diagnostics and are not posted to the ticket. "
                       f"Use ticket_comment for ticket #{tid} when needed, then follow this delivery gate: {result_gate} Ticket ID: {tid}. Your workspace is {cwd}. "
                       "When finished, ensure the requested deliverable has been recorded in the appropriate Swarm Agent record, then summarize the result and verification in your ACP response.")
            if dispatch_only:
                prompt = SHARED_COLLABORATION_PROMPT + (f"You are {agent['name']}, the orchestrator for job #{tid}. This turn is ONLY team staffing and delegation. "
                          "Call agents_list to prefer existing specialists. Call team_delegate once per specialty with a distinct personal name, capabilities, title and scope. "
                          "It automatically registers/reuses the teammate, links the child to this job, shares the job requirements and makes it actionable. "
                          "Child tickets default to task for workspace work; set type=research for public-source research. Native tools remain available in all turns. "
                          "Do not research, write wiki pages or deliver a result in this dispatch turn. After delegating all specialties, post a brief team_note and END YOUR TURN. "
                          "Workers start after you finish. You resume automatically for synthesis after their work and reviews complete. "
                          f"Suggested worker count: {t.get('recommended_workers') or 3}.\n\nJob: {t['title']}\n{t['body']}")
            elif heartbeat:
                prompt=SHARED_COLLABORATION_PROMPT + (f"You are {agent['name']} ({agent['slug']}), the lead for job #{tid}. This is a bounded team heartbeat. "
                        f"First call team_read(job_id={tid}). Inspect its child tickets, progress snapshots, recent_tools, conversations and pending deliveries. "
                        "Check that teammates are advancing their assignments and that questions or reviews receive responses. "
                        "Answer an existing unanswered question addressed to you with questions_get/questions_answer before team_reply; inspect linked evidence using wiki_get/source_get when needed. "
                        "If they are progressing, finish this check quietly. If a teammate is stalled, blocked, or repeatedly calling team_read without doing its assignment, "
                        "record concise guidance with team_note. Do not ask workers to restart exhausted reviews or perform additional research beyond their acceptance criteria. "
                        "Read existing guidance and pending requests first; do not repeat an unanswered request. "
                        "Use Swarm Agent tools for board coordination and native tools when needed to inspect the workspace. This monitor cannot create or delegate tickets, edit artifacts, deliver ticket_result, or start synthesis. "
                        "End your turn after checking and guiding. The runner monitors again in five minutes and schedules synthesis once children are ready. "
                        f"Use ticket_get to inspect a child's full assignment if needed.\n\nJob: {t['title']}\n{t['body']}")
            elif t['type'] != 'job' and team.job_for_ticket(conn, tid):
                prompt += (f'\n\nCURRENT TURN CONTRACT: You are {agent["name"]}, the specialist assigned ticket #{tid}, NOT the job lead. '
                           'Perform your own scope only. Use existing answered questions instead of asking them again. '
                           'Ask only for a specific unresolved gap. If needed, post/reuse a board question, link it, notify with team_note, then team_send(question_id=the ID). Questions are optional; do your assigned research yourself. Request the registered orchestrator as reviewer unless another qualified peer is named. '
                           'Native tools are available for reading files and running tests. Record local file evidence with source_read_file and cite its returned URL/ID; this satisfies the source gate even if an older assignment mentions only source_fetch. '
                           'Sending a new question or requesting peer review automatically pauses this turn; you resume with the answer. '
                           f'Use ticket_link and ticket_result only with id {tid}; use team_request_review with ticket_id {tid}. '
                           'A reviewed artifact must remain unchanged when delivering, otherwise it needs a fresh peer review. '
                           'Do not recreate teammates, delegate their work, or write the combined synthesis.')
            self._message(tid,rid,'system','acp-prompt',prompt)
            recorder.set('working', 'Reading assignment and shared skills')
            team.require_running(conn, tid)
            self._prompt_turn(c, prompt, backend=backend, priority=3 if heartbeat else 2, ticket_id=tid,
                              timeout=TEAM_MONITOR_SECONDS if heartbeat else WORKER_TURN_SECONDS,
                              total_timeout=TEAM_MONITOR_SECONDS if heartbeat else WORKER_TURN_SECONDS)
            if self._finish_stopped_run(tid, rid, recorder):
                stopped = self.conns.pop(tid, None)
                if stopped: stopped.close()
                elif c: c.close()
                return
            recorder.flush(True)
            children = api.list_tickets(db.connect(), parent_id=tid, include_closed=True)
            if stage == 'heartbeat':
                checked=any(payload.get('status')=='completed'
                            and (payload.get('title','')=='team_read' or payload.get('title','').endswith('_team_read'))
                            and (payload.get('rawInput') or {}).get('job_id',tid)==tid
                            for _row,payload in recorder.tools.values())
                if not checked: raise RuntimeError('Team heartbeat finished without a successful team_read')
                if children and not children_ready(db.connect(), tid):
                    conn=db.connect(); conn.execute("UPDATE agent_runs SET status='waiting' WHERE id=? AND status!='cancelled'", (rid,)); schedules.mark_ticket(conn,tid,'waiting'); conn.commit()
                    recorder.finish('waiting', 'Team heartbeat complete; child work is still in progress')
                    waiting=self.conns.pop(tid,None)
                    if waiting: waiting.close()
                    return
                conn=db.connect(); conn.execute("UPDATE agent_runs SET status='completed',finished_at=? WHERE id=? AND status!='cancelled'",(now(),rid)); conn.commit()
                recorder.finish('completed', 'Team heartbeat complete; all child work is ready for synthesis')
                return
            delivered = self._result_delivered(db.connect(), rid)
            if not delivered and children and (stage != 'synthesize' or not children_ready(db.connect(), tid)):
                conn=db.connect(); conn.execute("UPDATE agent_runs SET status='waiting' WHERE id=? AND status!='cancelled'", (rid,)); schedules.mark_ticket(conn, tid, 'waiting'); conn.commit()
                recorder.finish('waiting', 'Waiting for teammate results; synthesis will resume automatically')
                api.comment_ticket(conn, tid, agent['slug'], 'Waiting for child ticket results. The lead will resume automatically for synthesis once they are delivered.')
                waiting = self.conns.pop(tid, None)
                if waiting: waiting.close()
                return
            if not delivered:
                if team.pending(db.connect(), tid, agent['slug']):
                    conn.execute("UPDATE agent_runs SET status='waiting' WHERE id=? AND status!='cancelled'", (rid,)); schedules.mark_ticket(conn, tid, 'waiting'); conn.commit()
                    recorder.finish('waiting', 'Waiting for teammate replies or peer review; will resume automatically')
                    waiting = self.conns.pop(tid, None)
                    if waiting: waiting.close()
                    return
                raise RuntimeError('Agent finished without publishing ticket_result through the Swarm Agent tools. Record the deliverable on the board and update the ticket to retry.')
            self._complete_delivery(tid, rid, recorder)
        except Exception as e:
            if self._finish_stopped_run(tid, rid, recorder):
                stopped = self.conns.pop(tid, None)
                if stopped: stopped.close()
                elif c: c.close()
                return
            if self._result_delivered(db.connect(), rid):
                self._complete_delivery(tid, rid, recorder)
                finished = self.conns.pop(tid, None)
                if finished: finished.close()
                elif c: c.close()
                return
            if api.list_tickets(db.connect(), parent_id=tid, include_closed=True) and not children_ready(db.connect(), tid):
                conn=db.connect(); conn.execute("UPDATE agent_runs SET status='waiting',error=? WHERE id=? AND status!='cancelled'",(str(e),rid)); schedules.mark_ticket(conn, tid, 'waiting'); conn.commit()
                self._message(tid,rid,'system','warning','Lead turn stopped while child work remained active; a team heartbeat will restart supervision: '+str(e))
                recorder.finish('waiting', 'Lead turn stopped; heartbeat will resume team supervision')
                waiting=self.conns.pop(tid,None)
                if waiting: waiting.close()
                return
            if team.pending(db.connect(), tid, agent['slug']):
                conn=db.connect(); conn.execute("UPDATE agent_runs SET status='waiting',error='' WHERE id=? AND status!='cancelled'", (rid,)); schedules.mark_ticket(conn, tid, 'waiting'); conn.commit()
                recorder.finish('waiting', 'Paused for teammate replies or peer review; will resume automatically')
                waiting=self.conns.pop(tid,None)
                if waiting: waiting.close()
                return
            conn=db.connect(); conn.execute("UPDATE agent_runs SET status='failed',finished_at=?,error=? WHERE id=? AND status!='cancelled'",(now(),str(e),rid)); schedules.mark_ticket(conn, tid, 'failed', str(e)); conn.commit(); self._message(tid,rid,'system','error',str(e))
            recorder.finish('failed', str(e))
            failed=self.conns.pop(tid,None)
            if failed: failed.close()
        finally:
            if heartbeat and c:
                timer=getattr(c,'monitor_timer',None)
                if timer: timer.cancel()
                if self.conns.get(tid) is c: self.conns.pop(tid,None)
                c.close()
            with self.lock: self.active.pop(tid,None)
            with self.lock: self.run_conns.pop(rid, None)
            self.recorders.pop(rid, None)
    def edit_ticket_message(self,tid,message_id,text):
        return self.chat(tid,text,edit_message_id=message_id)
    def chat(self,tid,text,*,edit_message_id=None):
        if not isinstance(text,str): raise ValueError('message must be text')
        text=text.strip()
        if not text.strip(): raise ValueError('message required')
        with self.lock:
            conn=db.connect()
            api.get_ticket(conn,tid)
            c=self.conns.get(tid)
            if c and not c.alive:
                self.conns.pop(tid,None); c=None
            if tid in self.active: raise ValueError('The ticket agent is still working; wait for its current response.')
            if 'review:'+str(tid) in self.active: raise ValueError('This ticket is being reviewed; wait for the review to finish.')
            maximum=max(1,min(16,int(self.settings().get('parallel_agents','2'))))
            if self._active_slots()>=maximum: raise ValueError('All configured backend agent slots are in use.')
            if not c and not db.connect().execute("SELECT 1 FROM agent_runs WHERE ticket_id=? AND status='completed' AND stage='execute'", (tid,)).fetchone():
                raise ValueError('No completed agent session for this ticket. Mark it ready to start the runner.')
            if edit_message_id is not None:
                row=conn.execute("SELECT id FROM agent_messages WHERE ticket_id=? AND id=? AND role='user' AND kind='text'", (tid,edit_message_id)).fetchone()
                if not row: raise ValueError('Only your text messages in this chat can be edited.')
                # A fresh connection must replay retained history, never the old ACP session.
                if not conn.execute("SELECT 1 FROM agent_runs WHERE ticket_id=? AND status='completed' AND stage='execute'", (tid,)).fetchone():
                    raise ValueError('No completed execution is available for regenerating this reply.')
                ticket=api.get_ticket(conn,tid)
                workspace_path({'workspace':ticket.get('workspace') or self.settings().get('workspace',str(DEFAULT_WORKSPACE))})
                with conn:
                    conn.execute('DELETE FROM agent_messages WHERE ticket_id=? AND id>?', (tid,row['id']))
                    conn.execute('UPDATE agent_messages SET body=?,updated_at=? WHERE id=?', (text,now(),row['id']))
                    conn.execute("UPDATE agent_runs SET session_id='' WHERE ticket_id=?", (tid,))
                self.conns.pop(tid,None)
                if c: c.close()
                c=None
            self.active[tid]=True
        try:
            if edit_message_id is None: self._message(tid,None,'user','text',text)
            threading.Thread(target=self._worker,args=(tid,self._prompt,c,tid,text),daemon=True).start()
        except Exception:
            with self.lock: self.active.pop(tid,None)
            raise
        return {'accepted':True}
    def _prompt(self,c,tid,text):
        ticket = api.get_ticket(db.connect(), tid)
        skill_context = api.triggered_skill_context(db.connect(), text, ticket['assignee'] or 'agent-runner')
        run = db.connect().execute("SELECT * FROM agent_runs WHERE ticket_id=? AND stage='execute' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        backend = run['backend'] if run else self.settings().get('active_backend', 'opencode')
        recorder = progress.Recorder('ticket-chat:' + uuid.uuid4().hex, ticket['assignee'] or 'agent-runner', tid, None, backend)
        try:
            if not c:
                settings = self.settings()
                cwd = workspace_path({'workspace': ticket.get('workspace') or settings.get('workspace', str(DEFAULT_WORKSPACE))})
                c = self._conn(tid, backend, cwd, actor_override=run['agent_slug'] or ticket['assignee'], resume_session=run['session_id'])
                with self.lock: self.conns[tid] = c
                db.connect().execute('UPDATE agent_runs SET session_id=? WHERE id=?', (c.session_id, run['id']))
                db.connect().commit()
                if not c.ticket_restored:
                    from .ticket_chat import conversation
                    messages = conversation(db.connect(),tid,self)['messages']
                    current_user = max((message['id'] for message in messages if message['role']=='user'),default=0)
                    history = [{key:message[key] for key in ('role','kind','body','data')}
                               for message in messages if message['id']<current_user]
                    text = ('Continue this ticket conversation. The execution has already finished; answer the user without rerunning it.\n'
                            + json.dumps({'ticket': ticket, 'recent_conversation': history}, ensure_ascii=False)
                            + '\nUser follow-up: ' + text)
            c.on_notification = lambda msg: recorder.notification((msg.get('params') or {}).get('update') or {})
            self._prompt_turn(c, SHARED_COLLABORATION_PROMPT + '\n' + skill_context + text, backend=backend, ticket_id=tid, timeout=3600)
            recorder.finish('completed', 'Ticket conversation finished')
        except Exception as e:
            self._message(tid,None,'system','error',str(e))
            recorder.finish('failed', str(e))
        finally:
            with self.lock: self.active.pop(tid,None)

    def _review_ticket(self,ticket,review_id,source_run_id):
        from . import api
        tid=ticket['id']; conn=db.connect(); settings=self.settings(); backend=settings.get('active_backend','opencode')
        self._message(tid,source_run_id or None,'system','review-status',f'Orchestrator review started with {backend}.')
        conn.execute("UPDATE ticket_reviews SET status='running',backend=? WHERE id=? AND status!='cancelled'",(backend,review_id)); conn.commit()
        agent = api.orchestrator_agent(conn)
        recorder = progress.Recorder('review:' + str(review_id), agent['slug'], tid, source_run_id or None, backend, message_kind='review-decision')
        response=[]
        def add(role,kind,body):
            c=db.connect(); c.execute('INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,?,?,?,?)',(tid,source_run_id or None,role,kind,body,now())); c.commit()
        def log(line): add('system','review-log',line)
        def notification(msg):
            p=msg.get('params') or {}; u=p.get('update') or {}; kind=u.get('sessionUpdate'); content=u.get('content') or {}; body=content.get('text') if isinstance(content,dict) else None
            if body and kind=='agent_message_chunk':
                response.append(body)
            if u: recorder.notification(u)
        def request(msg,respond):
            respond({'outcome':{'outcome':'cancelled'}})
        c=None
        try:
            current=api.get_ticket(db.connect(),tid)
            team.require_running(conn, tid)
            if not api.children_resolved(conn, tid):
                raise RuntimeError('Worker tickets or their reviews are unfinished; the parent job cannot pass final review.')
            role=db.connect().execute('SELECT role FROM agents WHERE slug=?',(current['assignee'],)).fetchone()
            if current['status']!='review' or current['assignee'].lower().startswith('human') or (role and role['role'].lower()=='human') or 'human' in current['tags'].lower():
                conn.execute("UPDATE ticket_reviews SET status='human_review',decision='human_review',reason='Ticket state or human assignment changed before review.',ticket_version=?,finished_at=? WHERE id=? AND status!='cancelled'",(current['updated_at'],now(),review_id)); conn.commit(); recorder.finish('completed', 'Review deferred to a human'); return
            if current['updated_at']!=ticket['updated_at']:
                conn.execute("UPDATE ticket_reviews SET status='superseded',reason='Ticket changed while review was starting.',finished_at=? WHERE id=? AND status!='cancelled'",(now(),review_id)); conn.commit(); recorder.finish('completed', 'Review superseded by ticket changes'); return
            backend=settings.get('active_backend','opencode'); cwd=workspace_path(settings); cmd=backend_command(backend,settings)
            if not cmd: raise RuntimeError('No ACP command configured for '+backend)
            c=AcpConnection(cmd[0],cwd,command_args=cmd[1:],on_notification=notification,on_request=request,on_close=lambda _e:None,logger=log,on_thread_exit=_release_acp_db,env=backend_environment(backend,settings,{'COMS_DB':str(db.db_path().resolve()),'COMS_AGENT':agent['slug']}),mcp_servers=board_servers(agent['slug'],readonly=True))
            with self.lock: self.review_conns[tid]=c
            c.start(); c.session_new(cwd=cwd); self._apply_model(c,backend,settings)
            comments=current.get('comments') or []
            messages=conn.execute("SELECT role,kind,body FROM agent_messages WHERE ticket_id=? AND (run_id=? OR run_id IS NULL) AND kind NOT IN ('acp-prompt','acp-thinking','acp-tool_call') ORDER BY id DESC LIMIT 20",(tid,source_run_id)).fetchall()
            transcript='\n'.join(f"{m['role']} [{m['kind']}]: {m['body'][:600]}" for m in reversed(messages))[-8000:]
            discussion='\n'.join(f"{x['author']} [{x['kind']}]: {x['body'][:600]}" for x in comments[-6:])
            team.validate_delivery(conn, tid, current['assignee'])
            prompt=("You are the Swarm Agent orchestrator performing the final review of a completed ticket. Review the acceptance criteria, delivered result, ticket discussion, and execution transcript. Inspect the linked wiki with wiki_get and source records with source_get. Do not change files, tickets, or comments. Choose human_review for a specific unresolved requirement or a decision explicitly reserved for a human. Educational research does not require human judgment merely because the topic involves financial risk. Reported evidence limitations are acceptable when no claim falsely asserts verification. Otherwise choose close. Respond with exactly one valid JSON object and no other text. Use decision value close or human_review, for example: {\"decision\":\"close\",\"reason\":\"Acceptance criteria are met.\"}.\n\n"
                    f"Ticket #{tid}: {current['title']}\nPriority: {current['priority']}\nDescription / acceptance criteria:\n{current['body'][:4000]}\n\nDelivered result:\n{current['result'][:4000]}\n\nTicket discussion:\n{discussion}\n\nExecution transcript (recent summary):\n{transcript}\n\n{team.context(conn, tid, agent['slug'], instructions=False)}\nRead ticket_get(id={tid}) for full criteria, results and linked artifacts if the summary is truncated.")
            recorder.set('reviewing', 'Checking the deliverable and acceptance criteria (3 minute limit)')
            team.require_running(conn, tid)
            self._prompt_turn(c, prompt, backend=backend, priority=0, ticket_id=tid, timeout=180,total_timeout=180)
            recorder.flush(True)
            raw=''.join(response).strip(); decision=''; reason=''
            try:
                parsed=json.loads(raw.removeprefix('```json').removeprefix('```').removesuffix('```').strip())
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
            elif not api.children_resolved(conn, tid):
                final='superseded'; decision=''; reason='Worker tickets or reviews changed; finish them before final review.'
            elif current['assignee'].lower().startswith('human') or (role and role['role'].lower()=='human') or 'human' in current['tags'].lower():
                final='human_review'; decision='human_review'; reason='Ticket was assigned or tagged for a human during automated review.'
            elif decision=='close':
                api.update_ticket(db.connect(),tid,agent['slug'],status='done')
                api.comment_ticket(db.connect(),tid,agent['slug'],'Orchestrator review approved and closed this ticket: '+reason)
                final='closed'
            else:
                api.comment_ticket(db.connect(),tid,agent['slug'],'Orchestrator review requests human judgment: '+reason)
                final='human_review'
            current=api.get_ticket(db.connect(),tid)
            version=ticket['updated_at'] if final=='superseded' else current['updated_at']
            conn=db.connect(); conn.execute("UPDATE ticket_reviews SET status=?,decision=?,reason=?,ticket_version=?,finished_at=? WHERE id=? AND status!='cancelled'",(final,decision,reason,version,now(),review_id)); conn.commit()
            self._message(tid,source_run_id or None,'system','review-status',f'Orchestrator decision: {decision}. {reason}')
            recorder.finish('completed', f'Review: {decision}. {reason}')
        except Exception as e:
            conn=db.connect(); conn.rollback(); conn.execute("UPDATE ticket_reviews SET status='failed',decision='',reason=?,finished_at=? WHERE id=? AND status!='cancelled'",(str(e),now(),review_id)); conn.commit(); self._message(tid,source_run_id or None,'system','review-error',str(e))
            recorder.finish('failed', str(e))
        finally:
            if c: c.close()
            with self.lock:
                self.active.pop('review:'+str(tid),None)
                self.review_conns.pop(tid,None)

    def _folder_protected_paths(self):
        paths=[db.db_path().resolve().parent]
        if os.environ.get('COMS_IN_DOCKER')=='1': paths.append(Path('/home/board'))
        return paths
    def workspace_folders(self,path=None):
        return workspace_directory_browser(path,protected_paths=self._folder_protected_paths())
    def manage_workspace_folder(self,action,path,name=None):
        protected=self._folder_protected_paths()
        with self.lock:
            if action=='create':
                return workspace_folder_change(action,path,name,protected_paths=protected)
            if self.active or self.orchestrator_busy:
                raise ValueError('Wait for active agents to finish before renaming or deleting folders.')
            source=workspace_folder_target(path,protected_paths=protected)
            conn=db.connect()
            references=[('root',None,'the root workspace',self.settings().get('workspace',str(DEFAULT_WORKSPACE)))]
            references.extend(('project',row['id'],f"project “{row['name']}”",row['workspace']) for row in conn.execute('SELECT id,name,workspace FROM orchestrator_projects') if row['workspace'])
            affected=[]
            for kind,identifier,label,raw in references:
                try: suffix=Path(raw).expanduser().resolve().relative_to(source)
                except (ValueError,OSError,RuntimeError): continue
                affected.append((kind,identifier,label,suffix))
            if action=='delete':
                if affected:
                    raise ValueError(f'This folder is used by {affected[0][2]}. Choose another workspace before deleting it.')
                return workspace_folder_change(action,path,protected_paths=protected)
            if action!='rename': raise ValueError('unknown folder action')
            result=None
            try:
                conn.execute('BEGIN IMMEDIATE')
                result=workspace_folder_change(action,path,name,protected_paths=protected)
                destination=Path(result['path'])
                if str(destination)!=str(source):
                    for kind,identifier,_label,suffix in affected:
                        moved=str(destination/suffix)
                        if kind=='root':
                            conn.execute('INSERT OR REPLACE INTO runner_settings(key,value) VALUES(?,?)',('workspace',moved))
                        else:
                            conn.execute('UPDATE orchestrator_projects SET workspace=?,updated_at=? WHERE id=?',(moved,now(),identifier))
                    for row in conn.execute('SELECT id,workspace FROM orchestrator_sessions WHERE workspace!=? ',('',)).fetchall():
                        try: suffix=Path(row['workspace']).expanduser().resolve().relative_to(source)
                        except (ValueError,OSError,RuntimeError): continue
                        conn.execute("UPDATE orchestrator_sessions SET workspace=?,acp_session_id='' WHERE id=?",(str(destination/suffix),row['id']))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if result and result['path']!=str(source):
                    try: workspace_folder_change('rename',result['path'],source.name,protected_paths=protected)
                    except ValueError as rollback_error:
                        raise ValueError(f"Folder moved to {result['path']}, but workspace settings could not be saved or restored: {rollback_error}") from exc
                raise
            if affected and result['path']!=str(source):
                # Discard idle connections that may still hold the old cwd.
                for connection in self.conns.values(): connection.close()
                self.conns.clear()
                with self.model_lock:
                    for connection in self.model_conns.values(): connection.close()
                    self.model_conns.clear()
                with self.orchestrator_conn_lock:
                    if self.orchestrator_conn: self.orchestrator_conn.close()
                    self.orchestrator_conn=None; self.orchestrator_conn_key=None
            return result
    def orchestrator_projects(self):
        conn=db.connect(); return [dict(r) for r in conn.execute('SELECT * FROM orchestrator_projects ORDER BY name')]
    def orchestrator_sessions(self):
        conn=db.connect(); activity=progress.activity(conn)
        rows=[dict(r) for r in conn.execute('SELECT * FROM orchestrator_sessions ORDER BY updated_at DESC')]
        for row in rows:
            row.update(progress.activity_status(activity, 'sessions', row['id']))
            if self.orchestrator_busy and self.orchestrator_session == row['id']: row['working']=True
        return rows
    def delete_orchestrator_project(self,project_id):
        conn=db.connect()
        project=conn.execute('SELECT id FROM orchestrator_projects WHERE id=?',(int(project_id),)).fetchone()
        if not project: return False
        conn.execute('UPDATE orchestrator_sessions SET project_id=NULL WHERE project_id=?',(int(project_id),))
        conn.execute('DELETE FROM orchestrator_projects WHERE id=?',(int(project_id),)); conn.commit()
        return True
    def create_orchestrator_project(self,name,workspace=''):
        with self.lock:
            name=name.strip()
            if not name: raise ValueError('project name required')
            path=workspace_path({'workspace':workspace},create_default=False) if workspace not in (None,'') else ''
            conn=db.connect(); ts=now()
            try:
                cursor=conn.execute('INSERT INTO orchestrator_projects(name,workspace,created_at,updated_at) VALUES(?,?,?,?)',(name,path,ts,ts)); conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError('A project with this name already exists.') from exc
            return dict(conn.execute('SELECT * FROM orchestrator_projects WHERE id=?',(cursor.lastrowid,)).fetchone())
    def update_orchestrator_project(self,project_id,workspace):
        with self.lock:
            conn=db.connect()
            project=conn.execute('SELECT * FROM orchestrator_projects WHERE id=?',(int(project_id),)).fetchone()
            if not project: raise ValueError('project not found')
            path=''
            if workspace not in (None,''):
                path=workspace_path({'workspace':workspace},create_default=False)
            conn.execute('UPDATE orchestrator_projects SET workspace=?,updated_at=? WHERE id=?',(path,now(),int(project_id))); conn.commit()
            return dict(conn.execute('SELECT * FROM orchestrator_projects WHERE id=?',(int(project_id),)).fetchone())
    def _orchestrator_workspace(self,session,settings,conn=None):
        project_id=session.get('project_id')
        if project_id:
            conn=conn or db.connect()
            project=conn.execute('SELECT workspace FROM orchestrator_projects WHERE id=?',(project_id,)).fetchone()
            if project and project['workspace']:
                settings={**settings,'workspace':project['workspace']}
        return workspace_path(settings)
    def create_orchestrator_session(self,title='New chat',project_id=None):
        conn=db.connect(); sid=uuid.uuid4().hex; ts=now()
        if project_id is not None and not conn.execute('SELECT 1 FROM orchestrator_projects WHERE id=?',(project_id,)).fetchone(): raise ValueError('project not found')
        conn.execute('INSERT INTO orchestrator_sessions(id,title,project_id,created_at,updated_at) VALUES(?,?,?,?,?)',(sid,title.strip() or 'New chat',project_id,ts,ts)); conn.commit()
        return dict(conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone())
    def fork_orchestrator_session(self,sid,message_id):
        with self.lock:
            if self.orchestrator_busy and self.orchestrator_session==sid:
                raise ValueError('Wait for this chat to finish responding before forking it.')
            return fork_conversation(db.connect(),sid,message_id)
    def edit_orchestrator_message(self,sid,message_id,text):
        return self.orchestrate(text,sid,edit_message_id=message_id)
    def orchestrator_detail(self,sid,*,include_logs=False):
        conn=db.connect(); row=conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone()
        if not row: return None
        out=dict(row)
        out['messages']=conversation_messages(conn,sid)
        out['logs']=[message_dict(r) for r in conn.execute("SELECT * FROM orchestrator_messages WHERE session_id=? AND kind='log' ORDER BY id",(sid,))] if include_logs else []
        out['log_count']=conn.execute("SELECT count(*) FROM orchestrator_messages WHERE session_id=? AND kind='log'",(sid,)).fetchone()[0]
        out['turns']=[dict(r) for r in conn.execute('SELECT * FROM orchestrator_turns WHERE session_id=? ORDER BY started_at,id',(sid,))]
        settings=self.settings(); backend=settings.get('active_backend','opencode')
        out['backend']=backend; out['model']=settings.get('model_'+backend) or 'ACP default'
        try:
            out['workspace']=self._orchestrator_workspace(out,settings,conn); out['workspace_error']=''
        except ValueError as exc:
            project=conn.execute('SELECT workspace FROM orchestrator_projects WHERE id=?',(out.get('project_id'),)).fetchone() if out.get('project_id') else None
            out['workspace']=(project['workspace'] if project and project['workspace'] else settings.get('workspace',str(DEFAULT_WORKSPACE.resolve()))); out['workspace_error']=str(exc)
        with self.lock:
            out['busy']=self.orchestrator_busy and self.orchestrator_session==sid
            out['runner_busy']=self.orchestrator_busy
            out['stopping']=bool(out['busy'] and self.orchestrator_stop_event and self.orchestrator_stop_event.is_set())
        active=next((t for t in reversed(out['turns']) if t['status']=='running'),None)
        out['active_turn_id']=active['id'] if active else None
        out['state']='running' if out['busy'] else out['turns'][-1]['status'] if out['turns'] else 'ready'
        out.update(progress.activity_status(progress.activity(conn), 'sessions', sid))
        out['working']=out['working'] or out['busy']
        return out
    def delete_orchestrator_session(self,sid):
        with self.lock:
            if self.orchestrator_busy and self.orchestrator_session==sid:
                raise ValueError('Wait for this chat to finish responding before deleting it.')
            conn=db.connect()
            if not conn.execute('SELECT 1 FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone(): return False
            conn.execute('DELETE FROM orchestrator_messages WHERE session_id=?',(sid,))
            conn.execute('DELETE FROM orchestrator_turns WHERE session_id=?',(sid,))
            conn.execute('DELETE FROM orchestrator_sessions WHERE id=?',(sid,)); conn.commit()
            return True
    def update_orchestrator_session(self,sid,project_id):
        conn=db.connect()
        if not conn.execute('SELECT 1 FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone(): raise ValueError('session not found')
        if project_id not in (None,'') and not conn.execute('SELECT 1 FROM orchestrator_projects WHERE id=?',(project_id,)).fetchone(): raise ValueError('project not found')
        conn.execute('UPDATE orchestrator_sessions SET project_id=?,updated_at=? WHERE id=?',(int(project_id) if project_id not in (None,'') else None,now(),sid)); conn.commit()
        return dict(conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone())
    def _orchestrator_connection(self,backend,cwd,settings,*,on_connect=None,idle_only=False):
        cmd=backend_command(backend,settings)
        if not cmd: raise RuntimeError('No ACP command configured for '+backend)
        key=(backend,tuple(cmd),cwd,settings.get('model_'+backend,''),settings.get('opencode_api_base_url','') if backend=='opencode' else '',settings.get('opencode_api_key','') if backend=='opencode' else '',settings.get('opencode_api_model','') if backend=='opencode' else '')
        with self.orchestrator_conn_lock:
            if idle_only and (self.orchestrator_busy or self.stopping.is_set()): return None
            if self.orchestrator_busy and on_connect is None:
                # Model refresh must not replace a chat's project workspace
                # connection with the root workspace and terminate its turn.
                current=self.orchestrator_conn
                return current if current is not None and current.alive else None
            c=self.orchestrator_conn
            if c is not None and (self.orchestrator_conn_key!=key or not c.alive):
                c.close(); self.orchestrator_conn=None; self.orchestrator_conn_key=None; c=None
            if c is None:
                def context(params=None):
                    ctx=self.orchestrator_context
                    if ctx.get('connection') is not c or not ctx.get('stream_enabled'): return None
                    if params is not None and params.get('sessionId')!=ctx.get('acp_session_id'): return None
                    return ctx.get('transcript')
                def log(line):
                    transcript=context()
                    if transcript: transcript.log(line)
                def notification(msg):
                    if msg.get('method')!='session/update': return
                    p=msg.get('params') or {}
                    transcript=context(p)
                    if transcript:
                        update = p.get('update') or {}
                        recorder = self.orchestrator_context.get('recorder')
                        if recorder: recorder.notification(update)
                        transcript.apply(update)
                def request(msg,respond):
                    if msg.get('method')!='session/request_permission': raise AcpError('Method not found',code=-32601)
                    params=msg.get('params') or {}
                    transcript=context(params)
                    options=params.get('options') or []
                    opt=next((x.get('optionId') for x in options if isinstance(x,dict) and x.get('kind') in ('allow_once','allow_always','allow')),None) if transcript else None
                    if transcript: transcript.permission(params,opt or 'cancelled')
                    respond({'outcome':{'outcome':'selected','optionId':opt}} if opt else {'outcome':{'outcome':'cancelled'}})
                actor = api.orchestrator_agent(db.connect())['slug']
                c=AcpConnection(cmd[0],cwd,command_args=cmd[1:],on_notification=notification,on_request=request,on_close=lambda _e:None,logger=log,on_thread_exit=_release_acp_db,env=backend_environment(backend,settings,{'COMS_DB':str(db.db_path().resolve()),'COMS_AGENT':actor}),mcp_servers=board_servers(actor))
                self.orchestrator_conn=c; self.orchestrator_conn_key=key
                try:
                    if on_connect: on_connect(c)
                    c.start(); c.session_new(cwd=cwd); self._apply_model(c,backend,settings)
                except Exception:
                    c.close()
                    if self.orchestrator_conn is c:
                        self.orchestrator_conn=None; self.orchestrator_conn_key=None
                    raise
            elif on_connect: on_connect(c)
            return c
    def _warm_orchestrator(self):
        try:
            settings=self.settings(); backend=settings.get('active_backend','opencode')
            self._orchestrator_connection(backend,workspace_path(settings),settings,idle_only=True)
        except Exception: logging.info('Orchestrator ACP warm-up will retry on demand',exc_info=True)
        finally:
            if threading.current_thread() is not threading.main_thread():
                conn=getattr(db._local,'conn',None)
                if conn: conn.close()
                db._local.__dict__.clear()
    def _reset_orchestrator(self):
        with self.orchestrator_conn_lock:
            if self.orchestrator_conn: self.orchestrator_conn.close()
            self.orchestrator_conn=None; self.orchestrator_conn_key=None
        self._warm_orchestrator()
    def orchestrate(self, text, sid='default', *, edit_message_id=None):
        if not isinstance(text,str): raise ValueError('message must be text')
        text=text.strip()
        if not text: raise ValueError('message required')
        with self.lock:
            if self.orchestrator_busy: raise ValueError('The orchestrator is still responding; wait for its reply.')
            conn=db.connect(); session=conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone()
            if not session: raise ValueError('session not found')
            if conn.execute("SELECT 1 FROM orchestrator_turns WHERE session_id=? AND status='running'", (sid,)).fetchone():
                raise ValueError('This chat has an unfinished response; wait for it to finish.')
            settings=self.settings(); backend=settings.get('active_backend','opencode')
            cwd=self._orchestrator_workspace(dict(session),settings,conn)
            turn,user=begin_turn(sid,text,backend,settings.get('model_'+backend) or 'ACP default',cwd,replace_user_id=edit_message_id)
            session=conn.execute('SELECT * FROM orchestrator_sessions WHERE id=?',(sid,)).fetchone()
            self.orchestrator_busy=True; self.orchestrator_session=sid
            stop_event=threading.Event()
            self.orchestrator_stop_event=stop_event; self.orchestrator_stop_turn_id=turn['id']
            try:
                threading.Thread(target=self._orchestrator_turn,args=(text,sid,dict(session),turn['id'],stop_event),daemon=True).start()
            except Exception as exc:
                Transcript(sid,turn['id']).finish(error=str(exc))
                self.orchestrator_busy=False; self.orchestrator_session=None
                self.orchestrator_stop_event=None; self.orchestrator_stop_turn_id=None
                raise
        return {'accepted':True,'turn_id':turn['id'],'message':user}
    def stop_orchestrator(self,sid):
        """Cancel only the current chat turn; worker agents keep their sessions."""
        with self.lock:
            if not self.orchestrator_busy or self.orchestrator_session!=sid:
                raise ValueError('This chat has no active orchestrator response to stop.')
            stop_event=self.orchestrator_stop_event
            turn_id=self.orchestrator_stop_turn_id
            if stop_event.is_set(): return {'stopping':True,'turn_id':turn_id}
            stop_event.set()
            connection=self.orchestrator_context.get('connection') or self.orchestrator_conn
            if connection:
                try: connection.cancel()
                except Exception: logging.info('ACP cancel failed; forcing the stopped turn closed',exc_info=True)
        publish_change()
        def force_close():
            self.stopping.wait(ORCHESTRATOR_STOP_GRACE_SECONDS)
            with self.lock:
                if not self.orchestrator_busy or self.orchestrator_stop_event is not stop_event: return
                current=self.orchestrator_context.get('connection') or connection
                # Keep new sends excluded until this old turn's connection is closed.
                if current: current.close()
        threading.Thread(target=force_close,daemon=True,name='coms-orchestrator-cancel').start()
        return {'stopping':True,'turn_id':turn_id}

    def _orchestrator_turn(self,text,sid,session,turn_id=None,stop_event=None):
        stop_event=stop_event or threading.Event()
        c=None; restored=False; transcript=None; recorder=None
        try:
            if turn_id is not None: transcript=Transcript(sid,turn_id)
            settings=self.settings(); backend=settings.get('active_backend','opencode')
            if turn_id is None:
                cwd=self._orchestrator_workspace(session,settings)
                turn,_=begin_turn(sid,text,backend,settings.get('model_'+backend) or 'ACP default',cwd)
                turn_id=turn['id']
            else:
                turn=db.connect().execute('SELECT workspace FROM orchestrator_turns WHERE id=?',(turn_id,)).fetchone()
                cwd=turn['workspace'] if turn and turn['workspace'] else self._orchestrator_workspace(session,settings)
            transcript=Transcript(sid,turn_id)
            agent = api.orchestrator_agent(db.connect())
            recorder = progress.Recorder('chat:' + turn_id, agent['slug'], backend=backend)
            # Loading a session replays old messages; local rows are canonical.
            def bind(connection):
                self.orchestrator_context={'sid':sid,'turn_id':turn_id,'connection':connection,
                                           'transcript':transcript,'stream_enabled':False,'recorder':recorder}
                if stop_event.is_set():
                    connection.close()
                    raise AcpError('Orchestrator response cancelled')
            c=self._orchestrator_connection(backend,cwd,settings,on_connect=bind)
            bind(c)
            if session.get('backend')==backend and session.get('acp_session_id') and session.get('workspace')==cwd and c.supports('loadSession'):
                try: c.session_load(session['acp_session_id'],cwd=cwd); restored=True
                except Exception as exc:
                    logging.info('Could not restore ACP chat session; restoring board history: %s',exc)
            if not restored:
                result=c.session_new(cwd=cwd); acp_id=result.get('sessionId') or c.session_id
                x=db.connect(); x.execute('UPDATE orchestrator_sessions SET acp_session_id=?,backend=?,workspace=? WHERE id=?',(acp_id,backend,cwd,sid)); x.commit()
            self._apply_model(c,backend,settings)
            selected_model=settings.get('model_'+backend) or 'ACP default'
            guide = orchestrator_system_prompt(agent, backend, selected_model)
            guide += api.shared_skill_context(db.connect())
            self.orchestrator_context={'sid':sid,'turn_id':turn_id,'connection':c,'acp_session_id':c.session_id,
                                       'transcript':transcript,'stream_enabled':True,'recorder':recorder}
            context=history_before(sid,transcript.user_id) if not restored else ''
            first_turn=not restored
            prefix=guide+'\n\nWorkspace: '+cwd+'\n\n'
            prefix += api.triggered_skill_context(db.connect(), text, agent['slug'])
            if context and first_turn: prefix+='Previous conversation (restored from Swarm Agent history):\n'+context+'\n\n'
            if stop_event.is_set(): raise AcpError('Orchestrator response cancelled')
            result=self._prompt_turn(c, prefix+'User: '+text, backend=backend, priority=1, cancelled=stop_event.is_set, timeout=3600)
            reason='cancelled' if stop_event.is_set() else (result or {}).get('stopReason','')
            transcript.finish(reason)
            recorder.finish('cancelled' if reason=='cancelled' else 'completed',
                            'Orchestrator response cancelled; send new steering' if reason=='cancelled' else 'Orchestrator conversation finished')
        except Exception as e:
            if stop_event.is_set():
                if recorder: recorder.finish('cancelled','Orchestrator response cancelled; send new steering')
                if transcript: transcript.finish('cancelled')
            elif transcript:
                if recorder: recorder.finish('failed', str(e))
                transcript.finish(error=str(e))
            else: logging.exception('Could not begin orchestrator turn')
        finally:
            self.orchestrator_context={}
            if c and not c.alive:
                with self.orchestrator_conn_lock:
                    c.close()
                    if self.orchestrator_conn is c:
                        self.orchestrator_conn=None; self.orchestrator_conn_key=None
            with self.lock:
                self.orchestrator_busy=False; self.orchestrator_session=None
                self.orchestrator_stop_event=None; self.orchestrator_stop_turn_id=None
            publish_change()
            conn=getattr(db._local,'conn',None)
            # Direct callers/tests retain their connection; background workers
            # release the per-thread database handle when the turn is finished.
            if conn and threading.current_thread() is not threading.main_thread():
                conn.close(); db._local.__dict__.clear()

RUNNER=Runner()

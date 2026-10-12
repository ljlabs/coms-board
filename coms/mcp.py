"""Swarm Agent MCP tools over newline-delimited stdio (no dependencies).

Launched by the ACP backend with an absolute DB path and acting identity.
Never writes to stdout except for JSON-RPC frames. No filesystem tools.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coms import api, db, team


def tool(name, description, properties=None, required=(), write=False, destructive=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": not write, "destructiveHint": destructive}}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
TOOLS = [
    tool("board_search", "Search existing wiki pages, tickets and questions first.", {"query": S}, ["query"]),
    tool("wiki_list", "List wiki pages."),
    tool("wiki_get", "Read a wiki page.", {"slug": S}, ["slug"]),
    tool("wiki_put", "Publish durable knowledge to the board wiki; replaces the page body.",
         {"slug": S, "title": S, "body": S, "folder": S, "tags": S}, ["slug", "title", "body"], True),
    tool("wiki_append", "Append knowledge to a board wiki page.", {"slug": S, "body": S}, ["slug", "body"], True),
    tool("ticket_list", "List tickets, including completed tickets."),
    tool("ticket_get", "Read a ticket, its result, comments and links.", {"id": I}, ["id"]),
    tool("ticket_create", "Create prioritized work. Set ready only for actionable agent work.",
         {"title": S, "body": S, "priority": {"type": "string", "enum": list(api.PRIORITIES)},
          "assignee": S, "tags": S, "parent_id": I, "template_id": I, "ready": B,
          "type": {"type": "string", "enum": list(api.TICKET_TYPES)}, "recommended_workers": I, "workspace": S}, ["title"], True),
    tool("ticket_comment", "Publish a concise durable update to a ticket.", {"id": I, "body": S}, ["id", "body"], True),
    tool("ticket_link", "Attach a wiki page, question, ticket or URL to a ticket.",
         {"id": I, "kind": {"type": "string", "enum": ["wiki", "question", "ticket", "url"]},
          "ref": S, "note": S}, ["id", "kind", "ref"], True),
    tool("ticket_result", "Deliver your assigned scope with board links and verification once its cited current wiki revision has accepted peer review. Questions are optional. After acceptance, deliver immediately and end; finish a pending-review turn without this tool.",
         {"id": I, "result": S}, ["id", "result"], True),
    tool("questions_list", "List questions."),
    tool("questions_get", "Read a question and its answers.", {"id": I}, ["id"]),
    tool("questions_ask", "After board_search, publish a standalone reusable question with context and evidence needed. Link it to your ticket before notifying teammates with team_note/team_send.", {"title": S, "body": S}, ["title", "body"], True),
    tool("questions_answer", "Save a useful answer with inspected evidence and limitations on the Questions page before team_reply. Response turns may answer questions linked to their job.", {"id": I, "body": S}, ["id", "body"], True),
    tool("agent_templates", "List available execution templates before creating work."),
    tool("agents_list", "List registered agents and their capabilities. Prefer reusing a suitable teammate."),
    tool("agent_ensure", "Reuse a matching registered agent, or create a named teammate if none fits. An explicit slug selects that identity.",
         {"name": S, "slug": S, "role": S, "description": S, "instructions": S}, ["name"], True),
    tool("skill_list", "List skill folders in the repository-root skills/ directory. Every agent role can read enabled skills."),
    tool("skill_get", "Read an enabled skill folder's SKILL.md and file index. Every agent role can read every enabled skill.", {"slug": S}, ["slug"]),
    tool("skill_file_get", "Read a supporting file in an enabled skill. Available to every agent role.",
         {"slug": S, "path": S}, ["slug", "path"]),
    tool("skill_file_write", "Edit a custom skill text file. Built-in files are read-only. Only the human operator and orchestrator can write. Pass the version from skill_file_get.",
         {"slug": S, "path": S, "content": S, "version": I}, ["slug", "path", "content", "version"], True),
    tool("skill_entry_create", "Add a file or empty folder inside a skill. Only the human operator and orchestrator can write. Pass tree_version from skill_get; the parent folder must exist.",
         {"slug": S, "path": S, "kind": {"type": "string", "enum": ["file", "folder"]}, "content": S, "tree_version": I},
         ["slug", "path", "kind", "tree_version"], True),
    tool("skill_entry_move", "Rename or move a file or folder within a skill. Only the human operator and orchestrator can write. Pass tree_version from skill_get. The destination parent must exist; root SKILL.md stays in place and existing entries cannot be overwritten.",
         {"slug": S, "source": S, "destination": S, "tree_version": I},
         ["slug", "source", "destination", "tree_version"], True),
    tool("skill_put", "Create or update a verified reusable custom procedure in the root skills/ folder. Built-ins are read-only. Optional trigger_words automatically load instructions on matching messages. Only the human operator and orchestrator can publish; workers should share methods with the orchestrator. For updates, pass the current version returned by skill_get.",
         {"slug": S, "title": S, "body": S, "version": I, "trigger_words": {"type": "array", "items": S, "maxItems": 32}}, ["slug", "title", "body"], True),
    tool("skill_set_enabled", "Enable or disable a root skill. Available to the human operator and orchestrator.",
         {"slug": S, "enabled": B}, ["slug", "enabled"], True),
    tool("skill_delete", "Permanently remove a skill folder from the repository root. Available to the human operator and orchestrator.",
         {"slug": S}, ["slug"], True, destructive=True),
    tool("schedule_list", "List recurring scheduled prompts, their next run, chosen agent and workspace."),
    tool("schedule_get", "Read a scheduled task and its current schedule configuration.", {"id": I}, ["id"]),
    tool("schedule_create", "Create a recurring prompt for a registered agent. Choose cron, interval or weekly; set the timezone and workspace folder when known.",
         {"name": S, "prompt": S, "schedule_type": {"type": "string", "enum": ["cron", "interval", "weekly"]},
          "cron_expression": S, "interval_every": I, "interval_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
          "weekly_days": S, "weekly_time": S, "timezone": S, "agent_slug": S, "workspace": S},
         ["name", "prompt", "schedule_type", "agent_slug"], True),
    tool("schedule_update", "Edit a scheduled task. Send only fields to change; when changing its schedule, include its type and the matching cron, interval or weekly fields.",
         {"id": I, "name": S, "prompt": S, "schedule_type": {"type": "string", "enum": ["cron", "interval", "weekly"]},
          "cron_expression": S, "interval_every": I, "interval_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
          "weekly_days": S, "weekly_time": S, "timezone": S, "agent_slug": S, "workspace": S}, ["id"], True),
    tool("schedule_pause", "Pause a recurring scheduled task.", {"id": I}, ["id"], True),
    tool("schedule_resume", "Resume a paused scheduled task and calculate its next run.", {"id": I}, ["id"], True),
    tool("schedule_run_now", "Queue an immediate run of a scheduled task, even when its recurrence is paused.", {"id": I}, ["id"], True),
    tool("schedule_delete", "Delete a recurring schedule while retaining its execution history.", {"id": I}, ["id"], True),
    tool("team_roster", "List teammates on a job, with distinct names, identities, capabilities and assigned tickets.", {"job_id": I}, ['job_id']),
    tool("team_delegate", "Delegate one specialty to a named teammate. Creates a ready child task with inherited workspace and collaboration requirements. Set type=research for public-source research.", {"job_id": I, "name": S, "capabilities": S, "title": S, "body": S, "type": {"type": "string", "enum": ['task', 'research']}}, ['job_id', 'name', 'capabilities', 'title', 'body'], True),
    tool("team_stop", "Stop a job and all descendant workers durably. Disables execution, retries, queued replies and heartbeats; preserves tickets and results. Use when asked to stop or kill a team. Only the operator, orchestrator or job lead may call this. STOP notes and process kills alone do not stop queued work.", {'job_id': I}, ['job_id'], True),
    tool("team_read", "Read a bounded job index with recent messages, pending requests, progress, reviews and sources. Use message_id for a full message (offset pages long bodies), source_after_id for more sources, and source_get/wiki_get for evidence. Act on the assignment instead of repeatedly polling unchanged output.", {"job_id": I, "after_id": I, "message_id": I, "offset": I, "source_after_id": I}, ['job_id']),
    tool("team_send", "Ask for an actual evidence gap on your linked Questions record, using question_id. Include its link in body. The runner pauses your turn. Reuse answers; perform your own research instead of handing off your scope. Use team_note for coordination.", {"job_id": I, "to": S, "body": S, "question_id": I, "ticket_id": I}, ['job_id', 'to', 'body', 'question_id'], True),
    tool("team_note", "Post a useful update or staffing decision to the shared job conversation; does not demand replies.", {"job_id": I, "body": S}, ['job_id', 'body'], True),
    tool("team_reply", "Answer a question addressed to you; records a visible reply and releases the waiting sender.", {"message_id": I, "body": S}, ['message_id', 'body'], True),
    tool("team_request_review", "After your final wiki edit, request review of that exact revision. Workers normally select the registered orchestrator from team_roster; the lead's own page needs an independent specialist. The runner pauses your turn; keep the page unchanged until the decision. Revise rejected work or deliver accepted work with ticket_result.", {"job_id": I, "ticket_id": I, "artifact_slug": S, "reviewer": S}, ['job_id', 'ticket_id', 'artifact_slug', 'reviewer'], True),
    tool("team_review", "Read the exact requested revision with wiki_get and inspect cited evidence with source_get before deciding. Use this review ID in your assigned response turn; give concrete findings.", {"review_id": I, "decision": {"type": "string", "enum": ['accepted', 'changes_requested']}, "body": S}, ['review_id', 'decision', 'body'], True),
    tool("source_fetch", "Retrieve a public HTTPS source and record its real content/status/access date for peer inspection. Failed URLs are not verified evidence.", {"job_id": I, "url": S}, ['job_id', 'url'], True),
    tool("source_read_file", "Read UTF-8 text within your assigned workspace and record numbered file lines and a SHA256 hash as reviewable evidence. Cite the returned URL/ID in your wiki page. Defaults to at most 200 lines; request an explicit range of at most 200 lines. Native tools remain available for workspace work and tests.", {"job_id": I, "path": S, "start_line": I, "end_line": I}, ['job_id', 'path'], True),
    tool("source_get", "Read the content and retrieval status of a recorded research source.", {"source_id": I}, ['source_id']),
]

COLLABORATION_TOOLS = {'board_search', 'wiki_list', 'wiki_get', 'questions_list', 'questions_get', 'questions_answer', 'ticket_list', 'ticket_get', 'agents_list', 'skill_list', 'skill_get', 'skill_file_get', 'schedule_list', 'schedule_get', 'team_roster', 'team_read', 'team_reply', 'team_review', 'source_get', 'source_fetch', 'source_read_file'}
DISPATCH_TOOLS = {'ticket_get', 'agents_list', 'skill_get', 'skill_file_get', 'skill_list', 'team_delegate', 'team_roster', 'team_note'}
MONITOR_TOOLS = {'ticket_get', 'agents_list', 'team_roster', 'team_read', 'team_note', 'team_reply', 'questions_get', 'questions_answer', 'wiki_get', 'source_get'}


def validate(spec, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    schema = spec["inputSchema"]
    for key in schema["required"]:
        if key not in arguments:
            raise ValueError(f"missing argument: {key}")
    for key, value in arguments.items():
        prop = schema["properties"].get(key)
        if prop is None:
            raise ValueError(f"unknown argument: {key}")
        kind = prop["type"]
        valid = (isinstance(value, str) if kind == "string" else
                 type(value) is int if kind == "integer" else
                 type(value) is bool if kind == "boolean" else
                 isinstance(value, list) if kind == "array" else
                 isinstance(value, dict) if kind == "object" else False)
        if not valid or ("enum" in prop and value not in prop["enum"]):
            raise ValueError(f"invalid argument: {key}")


def call_tool(name, args, *, readonly=False, collaboration=False, dispatch_only=False, monitor_only=False):
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if not spec or (readonly and not spec["annotations"]["readOnlyHint"]) or (collaboration and name not in COLLABORATION_TOOLS) or (dispatch_only and name not in DISPATCH_TOOLS) or (monitor_only and name not in MONITOR_TOOLS):
        raise ValueError(f"tool unavailable: {name}")
    validate(spec, args)
    actor = os.environ.get("COMS_AGENT", "agent-runner")
    assigned = os.environ.get("COMS_TICKET_ID")
    if monitor_only and (not assigned or ('job_id' in args and args['job_id']!=int(assigned))):
        raise ValueError('Monitor only the job assigned to this heartbeat')
    response_to = os.environ.get('COMS_TEAM_MESSAGE_ID')
    if response_to and name == 'team_reply' and args['message_id'] != int(response_to):
        raise ValueError('Reply to the message assigned to this response turn')
    if assigned and name in ("ticket_result", "ticket_comment", "ticket_link") and args["id"] != int(assigned):
        raise ValueError("execution agent may only change its assigned ticket")
    conn = db.connect(readonly=readonly)
    if collaboration and name == 'questions_answer':
        if not response_to:
            raise ValueError('Answer questions only from an assigned response turn')
        message = conn.execute('SELECT job_id,question_id FROM team_messages WHERE id=? AND recipient=?',
                               (int(response_to), actor)).fetchone()
        linked = conn.execute("SELECT l.ticket_id FROM ticket_links l WHERE l.kind='question' AND l.ref=?",
                              (str(args['id']),)).fetchall()
        if not message or not any((team.job_for_ticket(conn, row['ticket_id']) or {}).get('id') == message['job_id']
                                  for row in linked):
            raise ValueError('Answer only questions linked to the job assigned to this response turn')
        if message['question_id'] is not None and message['question_id'] != args['id']:
            raise ValueError('Answer the Questions record assigned to this response turn')
        team.require_running(conn, message['job_id'])
    if assigned and name != 'team_stop' and not spec['annotations']['readOnlyHint']:
        team.require_running(conn, int(assigned))
    if monitor_only and name=='team_reply':
        message=conn.execute('SELECT job_id FROM team_messages WHERE id=?',(args['message_id'],)).fetchone()
        if not message or message['job_id']!=int(assigned):
            raise ValueError('Reply only within the job assigned to this heartbeat')
    if monitor_only and name == 'questions_answer':
        request = conn.execute("SELECT 1 FROM team_messages m JOIN team_deliveries d ON d.message_id=m.id WHERE m.job_id=? AND m.recipient=? AND m.question_id=? AND d.status IN ('pending','responding')", (int(assigned), actor, args['id'])).fetchone()
        if not request:
            raise ValueError('Answer only a Questions record addressed to you in this heartbeat job')
    if name == "board_search": result = api.search_all(conn, args["query"])
    elif name == "wiki_list": result = api.list_wiki(conn)
    elif name == "wiki_get":
        result = api.get_wiki(conn, args["slug"])
        if not readonly and os.environ.get('COMS_RUN_ID'):
            conn.execute('INSERT OR REPLACE INTO artifact_reads VALUES(?,?,?,?)', (actor, int(os.environ['COMS_RUN_ID']), args['slug'], result['updated_at']))
            conn.commit()
    elif name in ("wiki_put", "wiki_append"):
        if assigned and actor != api.ADMIN_ACTOR:
            job = team.job_for_ticket(conn, int(assigned))
            if job and job.get('collaboration_required') and job['assignee'] != actor:
                page = conn.execute('SELECT author FROM wiki_pages WHERE slug=?', (args['slug'],)).fetchone()
                if page and page['author'] != actor:
                    raise api.BadRequest('This page belongs to another teammate. Publish your own uniquely named evidence page and reference their page instead of overwriting it.')
        result = api.save_wiki(conn, args["slug"], args.get("title", ""), args["body"], actor,
                               tags=args.get("tags"), folder=args.get("folder"), append=name == "wiki_append",
                               ticket_id=int(assigned) if assigned else None)
    elif name == "ticket_list": result = api.list_tickets(conn, include_closed=True)
    elif name == "ticket_get": result = api.get_ticket(conn, args["id"])
    elif name == "ticket_create":
        if args.get('parent_id'):
            parent = team.job_for_ticket(conn, args['parent_id'])
            if parent and parent.get('collaboration_required') and parent['assignee'] == actor and not assigned:
                raise ValueError('The runner owns team staffing; create only the job in chat and let its dispatch turn delegate')
        # The shared orchestrator ACP/MCP process serves successive chats. Resolve
        # its current durable turn here instead of freezing a session in its env.
        source_session = None
        if not assigned and conn.execute("SELECT 1 FROM agents WHERE slug=? AND role='orchestrator'", (actor,)).fetchone():
            source_session = conn.execute("SELECT session_id FROM orchestrator_turns WHERE status='running' ORDER BY started_at DESC LIMIT 1").fetchone()
        result = api.create_ticket(conn, args["title"], args.get("body", ""), actor,
                                   type_=args.get('type', 'task'), priority=args.get("priority", "p2"), assignee=args.get("assignee", ""),
                                   tags=args.get("tags"), parent_id=args.get("parent_id"),
                                   ready_for_execution=args.get("ready", False), agent_template_id=args.get("template_id"), recommended_workers=args.get('recommended_workers'), workspace=args.get('workspace', ''),
                                   source_ticket_id=int(assigned) if assigned else None,
                                   source_session_id=source_session['session_id'] if source_session else '')
        if args.get('ready'):
            api.resolve_ticket_agent(conn, result)
            result = api.get_ticket(conn, result['id'])
    elif name == "ticket_comment": result = api.comment_ticket(conn, args["id"], actor, args["body"])
    elif name == "ticket_link": result = api.link_ticket(conn, args["id"], args["kind"], args["ref"], actor, args.get("note", ""))
    elif name == "ticket_result":
        if not args["result"].strip(): raise ValueError("a nonempty result is required")
        team.validate_delivery(conn, args['id'], actor)
        result = api.update_ticket(conn, args["id"], actor, result=args["result"])
    elif name == "questions_list": result = api.list_questions(conn)
    elif name == "questions_get": result = api.get_question(conn, args["id"], actor)
    elif name == "questions_ask": result = api.ask_question(conn, args["title"], args["body"], actor)
    elif name == "questions_answer": result = api.answer_question(conn, args["id"], actor, args["body"])
    elif name == "agent_templates": result = [dict(r) for r in conn.execute("SELECT id,name,backend,enabled FROM agent_templates ORDER BY name")]
    elif name == "agents_list": result = api.list_agents(conn)
    elif name == "agent_ensure": result = api.ensure_agent(conn, args['name'], args.get('role', 'expert'), args.get('description', ''), args.get('slug', ''), args.get('instructions', ''))
    elif name == "skill_list": result = api.list_skills(conn, enabled_only=True)
    elif name in ("skill_get", "skill_file_get"):
        skill = api.get_skill(conn, args['slug'])
        if not skill['enabled']:
            raise api.NotFound(f"enabled skill {args['slug']!r} not found")
        if not readonly:
            api.record_skill_access(conn, args['slug'], actor)
        result = skill if name == 'skill_get' else api.get_skill_file(conn, args['slug'], args['path'])
    elif name == "skill_file_write":
        result = api.write_skill_file(conn, args['slug'], args['path'], args['content'], actor, args['version'])
    elif name == "skill_entry_create":
        result = api.create_skill_entry(conn, args['slug'], args['path'], args['kind'], actor,
                                        args['tree_version'], args.get('content', ''))
    elif name == "skill_entry_move":
        result = api.move_skill_entry(conn, args['slug'], args['source'], args['destination'], actor, args['tree_version'])
    elif name == "skill_put": result = api.save_skill(conn, args['slug'], args['title'], args['body'], actor, args.get('version'), trigger_words=args.get('trigger_words'))
    elif name in ('skill_set_enabled', 'skill_delete'):
        agent = conn.execute('SELECT role FROM agents WHERE slug=?', (actor,)).fetchone()
        if actor != api.ADMIN_ACTOR and (not agent or agent['role'] != 'orchestrator'):
            raise api.BadRequest('Only the human operator or orchestrator can enable, disable, or delete skills')
        if name == 'skill_set_enabled': result = api.set_skill_enabled(conn, args['slug'], args['enabled'], actor)
        else: result = api.delete_skill(conn, args['slug'], actor)
    elif name in ('schedule_list', 'schedule_get', 'schedule_create', 'schedule_update', 'schedule_pause', 'schedule_resume', 'schedule_run_now', 'schedule_delete'):
        from . import schedules
        if name == 'schedule_list': result = schedules.list_tasks(conn)
        elif name == 'schedule_get': result = {"task": schedules.get_task(conn, args['id']), "runs": schedules.list_runs(conn, args['id'])}
        elif name == 'schedule_create':
            result = schedules.create_task(conn, _schedule_payload(conn, args), actor)
        elif name == 'schedule_update':
            result = schedules.update_task(conn, args['id'], _schedule_payload(conn, args, task_id=args['id']), actor)
        elif name == 'schedule_pause': result = schedules.set_status(conn, args['id'], 'paused', actor)
        elif name == 'schedule_resume': result = schedules.set_status(conn, args['id'], 'active', actor)
        elif name == 'schedule_run_now': result = schedules.run_now(conn, args['id'], actor)
        else: result = schedules.delete_task(conn, args['id'], actor)
    elif name == 'team_roster': result = team.roster(conn, args['job_id'])
    elif name == 'team_delegate':
        if not assigned:
            raise ValueError('The runner owns team staffing; team_delegate requires an assigned job turn')
        if assigned and int(assigned) != args['job_id']: raise ValueError('Delegate within your assigned job only')
        result = team.delegate(conn, args['job_id'], actor, args['name'], args['capabilities'], args['title'], args['body'], type_=args.get('type', 'task'))
    elif name == 'team_stop':
        if assigned and int(assigned) != args['job_id']: raise ValueError('Stop your assigned job only')
        result = team.stop(conn, args['job_id'], actor)
    elif name == 'team_read': result = team.read_room(conn, args['job_id'], args.get('after_id', 0), message_id=args.get('message_id'), offset=args.get('offset', 0), source_after_id=args.get('source_after_id', 0))
    elif name == 'team_send':
        origin = int(assigned) if assigned else args.get('ticket_id')
        if assigned and args.get('ticket_id', origin) != origin:
            raise ValueError('Notify from your assigned ticket only')
        result = team.send(conn, args['job_id'], actor, args['to'], args['body'],
                           question_id=args['question_id'], ticket_id=origin)
    elif name == 'team_note': result = team.send(conn, args['job_id'], actor, '*', args['body'], kind='note')
    elif name == 'team_reply': result = team.reply(conn, args['message_id'], actor, args['body'])
    elif name == 'team_request_review':
        if assigned and int(assigned) != args['ticket_id']: raise ValueError('Request review of your assigned ticket only')
        result = team.request_review(conn, args['job_id'], args['ticket_id'], actor, args['artifact_slug'], args['reviewer'])
    elif name == 'team_review':
        if response_to:
            request = conn.execute('SELECT message_id FROM peer_reviews WHERE id=?', (args['review_id'],)).fetchone()
            if not request or request['message_id'] != int(response_to): raise ValueError('Decide the peer review assigned to this response turn')
        result = team.review(conn, args['review_id'], actor, args['decision'], args['body'], int(os.environ.get('COMS_RUN_ID', '0')))
    elif name in ('source_fetch', 'source_get', 'source_read_file'):
        if name == 'source_fetch': result = team.fetch_source(conn, args['job_id'], actor, args['url'])
        elif name == 'source_read_file': result = team.read_file_source(conn, args['job_id'], actor, args['path'], args.get('start_line', 1), args.get('end_line'), ticket_id=int(assigned) if assigned else None)
        else: result = team.source_get(conn, args['source_id'])
        if not readonly and os.environ.get('COMS_RUN_ID'):
            conn.execute('INSERT OR IGNORE INTO source_reads VALUES(?,?,?)', (actor, int(os.environ['COMS_RUN_ID']), result['id']))
            conn.commit()
    else: raise ValueError(f"tool unavailable: {name}")
    if not spec["annotations"]["readOnlyHint"] and os.environ.get("COMS_RUN_ID"):
        conn.execute("INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,'system','board-delivery',?,?)",
                     (int(os.environ["COMS_TICKET_ID"]), int(os.environ["COMS_RUN_ID"]),
                      json.dumps({"tool": name, "id": result.get("id") if isinstance(result, dict) else None, "slug": result.get("slug") if isinstance(result, dict) else None}), api.now()))
        conn.commit()
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _weekday_numbers(value):
    aliases = {'mon': 0, 'monday': 0, 'tue': 1, 'tues': 1, 'tuesday': 1,
               'wed': 2, 'wednesday': 2, 'thu': 3, 'thur': 3, 'thurs': 3, 'thursday': 3,
               'fri': 4, 'friday': 4, 'sat': 5, 'saturday': 5, 'sun': 6, 'sunday': 6}
    if not isinstance(value, str): raise ValueError('weekly_days must be comma-separated weekday names or numbers')
    days=[]
    for part in value.split(','):
        item=part.strip().lower()
        if item in aliases: days.append(aliases[item])
        elif item.isdigit() and 0 <= int(item) <= 6: days.append(int(item))
        else: raise ValueError(f'invalid weekday: {part}')
    if not days: raise ValueError('weekly_days must list at least one weekday')
    return sorted(set(days))


def _schedule_payload(conn, args, task_id=None):
    from . import schedules
    current = schedules.get_task(conn, task_id) if task_id is not None else {}
    data = {key: args[key] for key in ('name', 'prompt', 'timezone', 'agent_slug', 'workspace') if key in args}
    schedule_type = args.get('schedule_type', current.get('schedule_type'))
    fields = {'cron_expression', 'interval_every', 'interval_unit', 'weekly_days', 'weekly_time'}
    if schedule_type and ('schedule_type' in args or fields.intersection(args)):
        config = dict(current.get('schedule_config') or {})
        if schedule_type == 'cron':
            if 'cron_expression' in args: config['expression'] = args['cron_expression']
        elif schedule_type == 'interval':
            if 'interval_every' in args: config['every'] = args['interval_every']
            if 'interval_unit' in args: config['unit'] = args['interval_unit']
        elif schedule_type == 'weekly':
            if 'weekly_days' in args: config['days'] = _weekday_numbers(args['weekly_days'])
            if 'weekly_time' in args: config['time'] = args['weekly_time']
        data['schedule_type'] = schedule_type
        data['schedule_config'] = config
    if task_id is None:
        data.setdefault('timezone', 'UTC')
    return data


def dispatch(message, readonly=False, collaboration=False, dispatch_only=False, monitor_only=False):
    mid = message.get("id")
    if mid is None: return None
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        versions = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in versions else "2025-11-25",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "swarm-agent", "version": "1.0.0"}}
    elif method == "ping": result = {}
    elif method == "tools/list":
        result = {"tools": [t for t in TOOLS if (not readonly or t["annotations"]["readOnlyHint"]) and (not collaboration or t['name'] in COLLABORATION_TOOLS) and (not dispatch_only or t['name'] in DISPATCH_TOOLS) and (not monitor_only or t['name'] in MONITOR_TOOLS)]}
    elif method == "tools/call":
        try: result = call_tool(params.get("name"), params.get("arguments", {}), readonly=readonly, collaboration=collaboration, dispatch_only=dispatch_only, monitor_only=monitor_only)
        except (ValueError, api.BadRequest, api.NotFound) as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readonly", action="store_true")
    parser.add_argument("--collaboration", action="store_true")
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    try:
        for line in sys.stdin:
            message = None
            try:
                message = json.loads(line)
                if not isinstance(message, dict): raise ValueError("request must be an object")
                response = dispatch(message, args.readonly, args.collaboration, args.dispatch, args.monitor)
            except Exception as exc:
                response = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                            "error": {"code": -32603, "message": str(exc)}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        conn = getattr(db._local, "conn", None)
        if conn: conn.close()


if __name__ == "__main__": main()

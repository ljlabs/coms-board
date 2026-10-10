"""Adapt durable ticket runner records to the shared conversation contract."""
import json

from . import api, progress


def conversation(conn, tid, runner, include_logs=False):
    ticket = api.get_ticket(conn, tid)
    runs = [dict(row) for row in conn.execute('SELECT * FROM agent_runs WHERE ticket_id=? ORDER BY id DESC', (tid,))]
    stages = {run['id']: run['stage'] for run in runs}
    agent = conn.execute('SELECT name FROM agents WHERE slug=?', (ticket['assignee'],)).fetchone()
    name = agent['name'] if agent else ticket['assignee'] or 'Agent'
    messages, logs = [], []
    for row in conn.execute('SELECT * FROM agent_messages WHERE ticket_id=? ORDER BY id', (tid,)):
        item = dict(row)
        source_kind = item['kind']
        item['data'] = {}
        item['state'] = 'completed'
        stage = 'review' if source_kind.startswith('review-') else stages.get(item['run_id'], 'conversation')
        label = {'execute': 'Execution', 'review': 'Review', 'conversation': 'Conversation', 'collaborate': 'Collaboration'}.get(stage, stage.title())
        item['label'] = f'{name} · {label}' + (f" · Run #{item['run_id']}" if item['run_id'] else '')
        if source_kind == 'acp-tool_call':
            try:
                payload = json.loads(item['body'])
                if not isinstance(payload, dict):
                    raise ValueError('Tool payload must be an object')
            except (ValueError, TypeError):
                item.update(kind='error', role='system', body='Unreadable tool record: ' + item['body'], state='failed')
            else:
                item.update(kind='tool_call', data=payload, state=payload.get('status', 'unknown'), tool_call_id=payload.get('toolCallId'))
        elif source_kind == 'review-decision':
            item.update(kind='text', role='assistant')
            try:
                decision = json.loads(item['body'])
                if isinstance(decision, dict) and 'decision' in decision:
                    item['body'] = f"**Review: {decision['decision']}**\n\n{decision.get('reason', '')}"
                    item['data'] = {'review': decision}
            except (ValueError, TypeError):
                pass  # Partial streamed decisions remain readable until complete.
        elif source_kind in ('text', 'acp-response') and item['role'] in ('user', 'assistant', 'system'):
            item.update(kind='text', role='user' if item['role'] == 'user' else 'assistant')
        elif 'error' in source_kind:
            item.update(kind='error', state='failed')
        else:
            item['kind'] = 'log'
            logs.append(item)
            continue
        if item['role'] == 'user': item['label'] = 'You'
        messages.append(item)
    with runner.lock:
        busy = tid in runner.active or 'review:' + str(tid) in runner.active
        connected = bool(runner.conns.get(tid) and runner.conns[tid].alive)
    available = connected or any(run['status'] == 'completed' and run['stage'] == 'execute' for run in runs)
    latest = runs[0] if runs else {}
    return {'title': 'Agent runner chat', 'assistant_name': name, 'messages': messages,
            'logs': logs if include_logs else [], 'log_count': len(logs), 'runs': runs,
            'progress': progress.snapshot(conn, ticket=tid), 'retry': api.ticket_retry_state(conn, tid),
            'busy': busy, 'runner_busy': busy, 'state': latest.get('status', 'completed'),
            'backend': latest.get('backend') or runner.settings().get('active_backend'),
            'workspace': ticket.get('workspace') or runner.settings().get('workspace'),
            'chat_available': available,
            'chat_unavailable_reason': '' if available else 'Mark this ticket ready and let its agent run before sending a follow-up.'}

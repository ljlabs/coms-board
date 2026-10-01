"""Deterministic ACP backend which connects to the supplied real MCP server."""
import json
import os
import subprocess
import sys


def send(message):
    print(json.dumps(message), flush=True)


server = None
selected_model = os.environ.get('COMS_FAKE_MODEL', 'gpt-6-luna')
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    params = msg.get('params', {})
    result = {}
    if method == 'initialize':
        marker=os.environ.get('COMS_FAKE_PID_FILE')
        if marker:
            with open(marker,'a',encoding='utf-8') as f: f.write(str(os.getpid())+'\n')
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': False}, 'agentInfo': {'name': 'test-backend'}}
    elif method == 'session/new':
        assert os.getcwd() == params['cwd']
        assert isinstance(params['mcpServers'], list)
        cwd_marker=os.environ.get('COMS_FAKE_CWD_FILE')
        if cwd_marker:
            with open(cwd_marker,'a',encoding='utf-8') as f: f.write(os.getcwd()+'\n')
        server = params['mcpServers'][0] if params['mcpServers'] else None
        result = {'sessionId': 'fake-session', 'configOptions': [{'id':'model','name':'Model','category':'model','type':'select','currentValue':selected_model,'options':[{'value':'gpt-6-luna','name':'GPT 6 Luna'},{'value':'gpt-6.1-sol','name':'GPT 6.1 Sol'}]}]}
    elif method == 'session/set_config_option':
        assert params['configId']=='model'
        assert params['value'] in ('gpt-6-luna','gpt-6.1-sol')
        selected_model=params['value']
    elif method == 'session/prompt':
        text = params['prompt'][0]['text']
        body = (os.environ.get('COMS_FAKE_BACKEND','fake')+' / '+selected_model) if os.environ.get('COMS_FAKE_IDENTITY') else 'Private response is not a board deliverable.'
        if 'performing the final review' in text:
            assert '--readonly' in server['args']
            body = '{"decision":"close","reason":"Verified board deliverable."}'
        elif os.environ.get('COMS_FAKE_MODE') != 'no-delivery' and 'TICKET ASSIGNMENT' in text:
            env = dict(os.environ)
            env.update({e['name']: e['value'] for e in server['env']})
            tid = int(env['COMS_TICKET_ID'])
            calls = [
                ('wiki_put', {'slug': 'delivered-research', 'title': 'Research', 'body': 'Published through MCP.'}),
                ('ticket_link', {'id': tid, 'kind': 'wiki', 'ref': 'delivered-research'}),
                ('ticket_result', {'id': tid, 'result': 'Delivered [[delivered-research]]; verified via wiki_get.'}),
            ]
            frames = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                      {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]
            frames += [{'jsonrpc': '2.0', 'id': i + 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
                       for i, (name, args) in enumerate(calls)]
            mcp = subprocess.run([server['command']] + server['args'], env=env,
                                 input=''.join(json.dumps(frame) + '\n' for frame in frames), text=True,
                                 capture_output=True, check=True, timeout=20)
            responses = [json.loads(line) for line in mcp.stdout.splitlines()]
            assert len(responses) == 4, mcp.stdout
            assert all('error' not in r and not r['result'].get('isError') for r in responses), mcp.stdout
            body = 'Published research and result through Coms Board.'
        # Thought chunks must never be mixed into the public assistant response.
        for kind, chunk in [('agent_thought_chunk', 'private thought'), ('agent_message_chunk', body)]:
            send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'fake-session',
                  'update': {'sessionUpdate': kind, 'content': {'type': 'text', 'text': chunk}}}})
        result = {'stopReason': 'end_turn'}
    elif method == 'session/load':
        raise AssertionError('Backend does not support loadSession')
    if 'id' in msg:
        send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})

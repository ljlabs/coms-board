"""Deterministic ACP backend which connects to the supplied real MCP server."""
import json
import os
import subprocess
import sys
import time


def send(message):
    print(json.dumps(message), flush=True)


server = None
session_counter = 0
session_id = None
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
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': os.environ.get('COMS_FAKE_LOAD') == '1'}, 'agentInfo': {'name': 'test-backend'}}
    elif method == 'session/new':
        session_counter += 1
        session_id = 'fake-session-' + str(session_counter)
        assert os.getcwd() == params['cwd']
        assert isinstance(params['mcpServers'], list)
        cwd_marker=os.environ.get('COMS_FAKE_CWD_FILE')
        if cwd_marker:
            with open(cwd_marker,'a',encoding='utf-8') as f: f.write(os.getcwd()+'\n')
        server = params['mcpServers'][0] if params['mcpServers'] else None
        result = {'sessionId': session_id, 'configOptions': [{'id':'model','name':'Model','category':'model','type':'select','currentValue':selected_model,'options':[{'value':'gpt-6-luna','name':'GPT 6 Luna'},{'value':'gpt-6.1-sol','name':'GPT 6.1 Sol'}]}]}
    elif method == 'session/set_config_option':
        assert params['configId']=='model'
        assert params['value'] in ('gpt-6-luna','gpt-6.1-sol')
        selected_model=params['value']
    elif method == 'session/prompt':
        text = params['prompt'][0]['text']
        if os.environ.get('COMS_FAKE_PROMPT_FILE'):
            with open(os.environ['COMS_FAKE_PROMPT_FILE'], 'a', encoding='utf-8') as f: f.write(json.dumps(text) + '\n')
        if os.environ.get('COMS_FAKE_MODE') in ('chat-tools', 'chat-error', 'chat-permission'):
            def update(payload, sid=None):
                send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid or session_id,'update':payload}})
            update({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Wrong session'}}, 'stale-session')
            for chunk in ['I’ll list ', 'the workspace.']:
                update({'sessionUpdate':'agent_message_chunk','messageId':'commentary','content':{'type':'text','text':chunk}})
            update({'sessionUpdate':'tool_call','toolCallId':'list','title':'List workspace','kind':'execute','rawInput':{'command':'ls','cwd':os.getcwd()}})
            if os.environ.get('COMS_FAKE_MODE') == 'chat-permission':
                send({'jsonrpc':'2.0','id':msg['id'],'method':'session/request_permission',
                      'params':{'sessionId':session_id,'toolCall':{'toolCallId':'list'},'options':[{'optionId':'allow-once','kind':'allow_once','name':'Allow once'}]}})
                answer=json.loads(sys.stdin.readline())
                assert answer['result']['outcome']['optionId']=='allow-once'
            update({'sessionUpdate':'tool_call_update','toolCallId':'list','status':'in_progress'})
            time.sleep(.15)
            if os.environ.get('COMS_FAKE_MODE') == 'chat-error':
                send({'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32000,'message':'Command transport failed'}})
                continue
            update({'sessionUpdate':'tool_call_update','toolCallId':'list','status':'completed','rawOutput':{'stdout':'src\n','exitCode':0}})
            for chunk in ['Found ', 'src.']:
                update({'sessionUpdate':'agent_message_chunk','messageId':'answer','content':{'type':'text','text':chunk}})
            send({'jsonrpc':'2.0','id':msg['id'],'result':{'stopReason':'end_turn'}})
            continue
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
            send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': session_id,
                  'update': {'sessionUpdate': kind, 'content': {'type': 'text', 'text': chunk}}}})
        result = {'stopReason': 'end_turn'}
    elif method == 'session/load':
        assert os.environ.get('COMS_FAKE_LOAD') == '1'
        session_id = params['sessionId']
        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session_id,'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Replayed old response'}}}})
        result = {'sessionId':session_id}
    if 'id' in msg:
        send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})

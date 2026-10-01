"""Server startup integration: workspace CLI, warm ACP lifecycle and shutdown."""
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from tests import *  # noqa: F401,F403
from coms import db

ROOT=Path(__file__).resolve().parent.parent


def process_alive(pid):
    if os.name == 'nt':
        # os.kill(pid, 0) terminates a process on Windows; query its status.
        import ctypes
        kernel=ctypes.windll.kernel32
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle: return False
        try:
            code=ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0); return True
    except ProcessLookupError: return False


class TestServerCli(unittest.TestCase):
    def test_workspace_argument_starts_one_warm_codex_session_until_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); database=root/'server.db'; workspace=root/'agent-workspace'; workspace.mkdir()
            conn=db.connect(database)
            fake=Path(__file__).with_name('fixtures')/'fake_acp.py'
            command=shlex.join([sys.executable,str(fake)])
            for key,value in {'active_backend':'codex','codex_command':command,'model_codex':'gpt-6-luna'}.items():
                conn.execute('INSERT INTO runner_settings(key,value) VALUES(?,?)',(key,value))
            conn.commit(); conn.close(); db._local.__dict__.clear()
            pids=root/'pids'; cwds=root/'cwds'
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
            env={**os.environ,'COMS_DB':str(database),'COMS_FAKE_PID_FILE':str(pids),'COMS_FAKE_CWD_FILE':str(cwds)}
            log_path=root/'server.log'; log_file=log_path.open('w',encoding='utf-8')
            startup=Path(__file__).with_name('fixtures')/'server_startup.py'
            server=subprocess.Popen([sys.executable,str(startup),'--port',str(port),'-ws',str(workspace)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=log_file,text=True)
            child_pid=None
            try:
                deadline=time.monotonic()+20
                settings=None
                while time.monotonic()<deadline:
                    if server.poll() is not None:
                        log_file.flush(); self.fail('server exited during startup: '+log_path.read_text())
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/runner/settings',timeout=1) as response:
                            settings=json.load(response)
                        if pids.exists() and cwds.exists(): break
                    except OSError: time.sleep(.1)
                self.assertIsNotNone(settings,'server HTTP API did not become ready')
                self.assertEqual(settings['workspace'],str(workspace))
                self.assertEqual(settings['active_backend'],'codex')
                self.assertEqual(settings['model_codex'],'gpt-6-luna')
                self.assertTrue(pids.exists(),'server did not warm the orchestrator ACP process')
                child_pids=pids.read_text().splitlines()
                self.assertEqual(len(child_pids),1,'model discovery and orchestrator should share one warm ACP process')
                self.assertTrue(cwds.read_text().splitlines())
                self.assertEqual(set(cwds.read_text().splitlines()),{str(workspace)})
                child_pid=int(child_pids[0])
                self.assertTrue(process_alive(child_pid))
            finally:
                try:
                    request=urllib.request.Request(f'http://127.0.0.1:{port}/__test_shutdown',method='POST')
                    with urllib.request.urlopen(request,timeout=2): pass
                except OSError: server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill(); server.wait(timeout=5)
                log_file.close()
            self.assertIsNotNone(child_pid)
            self.assertFalse(process_alive(child_pid))


if __name__=='__main__':
    unittest.main()

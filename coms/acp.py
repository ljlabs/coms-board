"""Minimal ACP (Agent Client Protocol) client: JSON-RPC 2.0 over stdio.

Verified against `opencode acp` v2.0.12 (see PROBE notes in the repo README):

  initialize            -> {protocolVersion, agentCapabilities, authMethods, agentInfo}
  session/new           -> {sessionId, configOptions:[{id,name,category,type,currentValue,options}]}
                           REQUIRES `mcpServers` (even an empty object)
  session/load          -> same result shape; needs cwd + mcpServers
  session/prompt        -> {stopReason, usage:{inputTokens,outputTokens,totalTokens}}
  session/set_config_option  {sessionId, configId, value}   (model selection)
  session/set_mode           {sessionId, modeId}
  session/set_model          DOES NOT EXIST (-32601)
  session/set_permission_mode DOES NOT EXIST (-32601)

Two hazards, both documented by KiroCrew's own ACP client:

1. Response/request id namespaces are independent. The agent's inbound
   `session/request_permission` carries its own id counter which collides with
   ours on small integers. We therefore classify purely on the presence of
   `method`: a frame WITH `method` is never a response to our request.

2. An unanswered server->client request leaves the agent blocked forever and
   the turn hangs. Every inbound request is dispatched to a worker thread and
   answered, always.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tempfile
from typing import Any, Callable, Dict, List, Optional


class AcpError(RuntimeError):
    def __init__(self, message: str, code: Any = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[str] = None


def resolve_opencode_binary(explicit: Optional[str] = None) -> str:
    """Find the opencode binary: flag > $OPENCODE_BIN > PATH > well-known paths."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("OPENCODE_BIN")
    if env:
        candidates.append(env)
    which = shutil.which("opencode")
    if which:
        candidates.append(which)
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".opencode", "bin", "opencode"),
        os.path.join(home, ".local", "bin", "opencode"),
        "/usr/local/bin/opencode",
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    # last resort: let the OS PATH resolution fail loudly at spawn time
    return explicit or "opencode"


def resolve_codex_acp_binary(explicit: Optional[str] = None) -> str:
    """Find the Codex ACP adapter executable."""
    candidates = [explicit, os.environ.get("CODEX_ACP_BIN"), shutil.which("codex-acp")]
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return explicit or "codex-acp"


class AcpConnection:
    """One ACP stdio subprocess."""

    def __init__(
        self,
        binary: str,
        cwd: str,
        *,
        on_notification: Callable[[Dict[str, Any]], None],
        on_request: Callable[[Dict[str, Any], Callable[[Dict[str, Any]], None]], None],
        on_close: Callable[[str], None],
        logger: Optional[Callable[[str], None]] = None,
        env: Optional[Dict[str, str]] = None,
        command_args: Optional[List[str]] = None,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        on_thread_exit: Optional[Callable[[], None]] = None,
    ) -> None:
        self.binary = binary
        self.cwd = cwd
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_close = on_close
        self.log = logger or (lambda _m: None)
        self.on_thread_exit = on_thread_exit or (lambda: None)
        self.extra_env = env or {}
        self.command_args = list(command_args) if command_args is not None else ["acp"]
        self.mcp_servers = list(mcp_servers or [])

        self.proc: Optional[subprocess.Popen] = None
        self.agent_capabilities: Dict[str, Any] = {}
        self.auth_methods: List[Dict[str, Any]] = []
        self.agent_info: Dict[str, Any] = {}
        self.config_options: List[Dict[str, Any]] = []
        self.session_id: Optional[str] = None

        self._next_id = 1
        self._pending: Dict[int, _Pending] = {}
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._closed = threading.Event()
        self._last_rx = time.monotonic()      # activity clock: last inbound frame
        self._reader: Optional[threading.Thread] = None
        self._stderrer: Optional[threading.Thread] = None
        self._temporary_directory: Optional[str] = None

    # ----------------------------------------------------------- lifecycle
    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self._closed.is_set()

    def start(self, init_timeout: float = 60.0) -> Dict[str, Any]:
        if not os.path.isdir(self.cwd):
            raise AcpError(f"workspace does not exist: {self.cwd}")
        self.log(f"spawn: {' '.join([self.binary] + self.command_args)} (cwd={self.cwd})")
        with self._lifecycle_lock:
            if self._closed.is_set(): raise AcpError("connection closed")
            temp_root = self.extra_env.get('COMS_ACP_TEMP_ROOT') or tempfile.gettempdir()
            if shutil.disk_usage(temp_root).free < 16 * 1024 * 1024:
                raise AcpError('Insufficient temporary space for ACP startup (need 16 MiB free)')
            self._temporary_directory = tempfile.mkdtemp(prefix='coms-acp-', dir=temp_root)
            private_env = {key: self._temporary_directory for key in ('TMPDIR', 'TMP', 'TEMP')}
            try:
                self.proc = subprocess.Popen(
                    [self.binary] + self.command_args,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, **self.extra_env, **private_env},
                    bufsize=0,
                )
            except Exception:
                self._cleanup_temporary_directory()
                raise
            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name=f"acp-read-{os.getpid()}")
            self._reader.start()
            self._stderrer = threading.Thread(target=self._stderr_loop, daemon=True,
                                              name="acp-stderr")
            self._stderrer.start()

        init = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                # Advertise no fs/terminal capabilities: the agent uses its own
                # tools and we never want to be asked to edit files or host a
                # PTY for it. Same surface KiroCrew settles on.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                },
                "clientInfo": {"name": "agentchat", "version": "0.1.0"},
            },
            timeout=init_timeout,
        )
        self.agent_capabilities = init.get("agentCapabilities") or {}
        self.auth_methods = init.get("authMethods") or []
        self.agent_info = init.get("agentInfo") or {}
        self.log(f"initialized: {self.agent_info}")
        return init

    def close(self) -> None:
        # EOF can arrive before poll() observes process exit (notably when an
        # ACP child closes stdout but keeps running).  The reader sets
        # _closed in that case, but close still has to terminate and reap it.
        with self._lifecycle_lock:
            self._closed.set()
            proc = self.proc
            if proc and proc.poll() is None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.terminate()
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=4)
                    except Exception:
                        pass
        for worker in (self._reader, self._stderrer):
            if worker and worker is not threading.current_thread(): worker.join(timeout=1)
        if proc:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream: stream.close()
                except Exception:
                    pass
        # release anything still waiting
        with self._pending_lock:
            pend = list(self._pending.values())
            self._pending.clear()
        for p in pend:
            p.error = "connection closed"
            p.event.set()
        with self._lifecycle_lock:
            if not proc or proc.poll() is not None:
                self._cleanup_temporary_directory()

    def _cleanup_temporary_directory(self):
        # Only this connection's uniquely created directory; never scan/delete
        # arbitrary /tmp files or files still used by a live process.
        if self._temporary_directory:
            shutil.rmtree(self._temporary_directory, ignore_errors=True)
            if not os.path.exists(self._temporary_directory):
                self._temporary_directory = None

    # ------------------------------------------------------------ transport
    def _write(self, msg: Dict[str, Any]) -> None:
        proc = self.proc
        if self._closed.is_set() or not proc or proc.poll() is not None:
            raise AcpError("ACP process is not running")
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Dict[str, Any],
                timeout: float = 120.0, total_timeout: Optional[float] = None) -> Any:
        with self._pending_lock:
            mid = self._next_id
            self._next_id += 1
            pend = _Pending()
            self._pending[mid] = pend
        try:
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(mid, None)
            raise AcpError(f"failed to send {method}: {exc}") from exc

        # Activity-based deadline: inbound frames extend the wait, so a long
        # session/load transcript replay is never cut off by a fixed timeout
        # while the agent is demonstrably still streaming. A silent agent
        # still fails at `timeout` (connect callers then recover with a
        # fresh spawn), and hard_cap bounds the total wait either way.
        start = time.monotonic()
        deadline = start + timeout
        hard_cap = start + (total_timeout if total_timeout is not None else timeout + 480)
        while not pend.event.is_set():
            deadline = max(deadline, self._last_rx + timeout)
            remaining = min(deadline, hard_cap) - time.monotonic()
            if remaining <= 0:
                with self._pending_lock:
                    self._pending.pop(mid, None)
                raise AcpError(f"timeout waiting for {method} ({timeout:.0f}s)")
            pend.event.wait(min(remaining, 5.0))
            if pend.event.is_set():
                break
            if time.monotonic() > hard_cap:
                with self._pending_lock:
                    self._pending.pop(mid, None)
                raise AcpError(f"hard timeout waiting for {method}")

        if pend.error:
            raise AcpError(pend.error)
        return pend.result

    def _resolve(self, mid: int, result: Any, error: Any) -> None:
        with self._pending_lock:
            pend = self._pending.pop(mid, None)
        if pend is None:
            return
        if error:
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            pend.error = f"{msg} {json.dumps(error.get('data')) if isinstance(error, dict) and error.get('data') else ''}".strip()
        else:
            pend.result = result
        pend.event.set()

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc is not None
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    self.log(f"non-json frame: {line[:200]!r}")
                    continue
                self._last_rx = time.monotonic()    # feed the activity deadline
                self._dispatch(msg)
        except Exception as exc:  # pragma: no cover - defensive
            self.log(f"read loop error: {exc}")
        finally:
            rc = proc.poll()
            reason = f"process exited (rc={rc})" if rc is not None else "stdout closed before process exit (rc=None)"
            first = not self._closed.is_set()
            self._closed.set()
            with self._pending_lock:
                pend = list(self._pending.values())
                self._pending.clear()
            for p in pend:
                p.error = "ACP " + reason
                p.event.set()
            if first:
                self.log("ACP " + reason)
                try:
                    self.on_close(reason)
                except Exception as exc:
                    self.log(f"on_close error: {exc}")
            self.on_thread_exit()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        mid = msg.get("id")

        if method is not None and mid is not None:
            # ---- server -> client REQUEST. Must always be answered. ----
            params = msg.get("params") or {}

            def respond(result: Dict[str, Any], _mid: Any = mid) -> None:
                try:
                    self._write({"jsonrpc": "2.0", "id": _mid, "result": result})
                except Exception as exc:
                    self.log(f"failed answering {_mid}: {exc}")

            def reject(code: int = -32601, message: str = "Method not found",
                       _mid: Any = mid) -> None:
                try:
                    self._write({"jsonrpc": "2.0", "id": _mid,
                                 "error": {"code": code, "message": message}})
                except Exception as exc:
                    self.log(f"failed rejecting {_mid}: {exc}")

            def runner(_p=params, _r=respond, _j=reject) -> None:
                try:
                    self.on_request(msg, _r)
                except AcpError as exc:
                    _j(exc.code or -32603,str(exc))
                except Exception as exc:
                    self.log(f"on_request({method}) failed: {exc}")
                    # Never leave the agent hanging: fall back to a clean reject.
                    _j(-32603, f"internal error handling {method}: {exc}")
                finally:
                    self.on_thread_exit()

            threading.Thread(target=runner, daemon=True,
                             name=f"acp-req-{method}").start()
            return

        if method is not None:
            # ---- notification ----
            try:
                self.on_notification(msg)
            except Exception as exc:
                self.log(f"notification handler error: {exc}")
            return

        if mid is not None:
            self._resolve(int(mid), msg.get("result"), msg.get("error"))
            return

        self.log(f"unclassifiable frame: {json.dumps(msg)[:200]}")

    def _stderr_loop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    self.log(f"[stderr] {text[:500]}")
        except Exception:
            pass
        finally:
            self.on_thread_exit()

    # ------------------------------------------------------- ACP operations
    def _mcp(self) -> List[Dict[str, Any]]:
        # ACP v1 requires an array, including for sessions without tools.
        return self.mcp_servers

    def session_new(self, cwd: Optional[str] = None, timeout: float = 120.0) -> Dict[str, Any]:
        res = self.request("session/new",
                           {"cwd": cwd or self.cwd, "mcpServers": self._mcp()},
                           timeout=timeout)
        self.session_id = res.get("sessionId")
        self.config_options = res.get("configOptions") or []
        return res

    def session_load(self, session_id: str, cwd: Optional[str] = None,
                     timeout: float = 120.0) -> Dict[str, Any]:
        res = self.request("session/load",
                           {"sessionId": session_id, "cwd": cwd or self.cwd,
                            "mcpServers": self._mcp()},
                           timeout=timeout)
        self.session_id = session_id
        self.config_options = res.get("configOptions") or []
        return res

    def prompt(self, text: str, files: Optional[List[Dict[str, Any]]] = None,
               timeout: float = 900.0, total_timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self.session_id:
            raise AcpError("no active ACP session")
        content: List[Dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for f in files or []:
            content.append(f)
        return self.request("session/prompt",
                            {"sessionId": self.session_id, "prompt": content},
                            timeout=timeout, total_timeout=total_timeout)

    def cancel(self) -> None:
        if not self.session_id:
            return
        try:
            self.notify("session/cancel", {"sessionId": self.session_id})
        except Exception as exc:
            self.log(f"cancel failed: {exc}")

    def set_config_option(self, config_id: str, value: Any) -> Any:
        if not self.session_id:
            raise AcpError("no active ACP session")
        return self.request("session/set_config_option",
                            {"sessionId": self.session_id,
                             "configId": config_id, "value": value},
                            timeout=30)

    def set_mode(self, mode_id: str) -> Any:
        if not self.session_id:
            raise AcpError("no active ACP session")
        return self.request("session/set_mode",
                            {"sessionId": self.session_id, "modeId": mode_id},
                            timeout=30)

    def supports(self, capability: str) -> bool:
        return bool(self.agent_capabilities.get(capability))

    def models(self) -> List[Dict[str, Any]]:
        """Flatten configOptions -> [{value,name}] for the model picker."""
        out: List[Dict[str, Any]] = []
        for opt in self.config_options:
            if opt.get("id") == "model" and opt.get("type") == "select":
                for choice in opt.get("options") or []:
                    out.append({"value": choice.get("value"), "name": choice.get("name")})
        return out

    def current_model(self) -> Optional[str]:
        for opt in self.config_options:
            if opt.get("id") == "model":
                return opt.get("currentValue")
        return None

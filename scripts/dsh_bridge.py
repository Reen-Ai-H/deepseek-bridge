#!/usr/bin/env python3
"""dsh_bridge — drive a local DeepSeek Harness (dsh) agent from the command line or from another agent.

Talks to dsh through its built-in Agent Client Protocol app (`dsh --profile acp`, JSON-RPC over stdio).
Sessions live in dsh's own home (default ~/.dsh), the same store the dsh Web UI uses; dsh's own
credentials and model settings apply. Python 3.9+ standard library; `zstandard` is needed only for
`history` on compressed sessions.

Commands
  send    --cwd DIR [--session ID | --last] [--model M] [--effort E] [--permission allow|deny]
          [--file F | TEXT | -]          one turn in the foreground; final answer -> stdout
  start   (same options)                 one turn as a background job; prints {"job", "session", ...}
  wait    JOB [--timeout SEC]            block until the job ends (or timeout); prints status + answer
  status  [JOB] [--cwd DIR]              one job, or recent jobs
  cancel  JOB                            stop a running job (session/cancel, then kill after a grace period)
  list    [--cwd DIR] [--limit N]        resumable dsh sessions, newest first
  models  --cwd DIR                      selectable models and reasoning efforts
  history SESSION [--tail N]             a session's saved conversation (messages, tool calls, tool results)
  prune   [--days N]                     delete finished job folders older than N days (default 14)

Environment
  DSH_CMD                full command that starts dsh, e.g. "dsh" or "npx -y @deepseek-ai/dsh"
  DSH_REPO               a dsh source checkout (runs apps/cli/src/bin.ts through tsx)
  DSH_HOME               dsh home, default ~/.dsh (used by `history`)
  DSH_BRIDGE_HOME        bridge state (jobs, locks, last session per dir), default ~/.dsh-bridge
  DSH_BRIDGE_PERMISSION  deny (default) | allow — answer to dsh's permission prompts
  DSH_BRIDGE_DEBUG       print dsh's stderr

Never start the ACP app through `pnpm dsh`: pnpm echoes the command to stdout and corrupts the frames.
"""
from __future__ import annotations

import argparse, hashlib, json, os, queue, re, shlex, shutil, subprocess, sys, threading, time, uuid
from pathlib import Path

__version__ = "1.1.0"

BRIDGE_HOME = Path(os.environ.get("DSH_BRIDGE_HOME", Path.home() / ".dsh-bridge")).expanduser()
JOBS, LAST_DIR, LOCKS = BRIDGE_HOME / "jobs", BRIDGE_HOME / "last", BRIDGE_HOME / "locks"
JOB_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PERMISSIONS = ("deny", "allow")
CANCEL_GRACE = float(os.environ.get("DSH_BRIDGE_CANCEL_GRACE", 30))  # seconds before an ignored cancel kills dsh
STARTUP_GRACE = 60.0     # a job may sit in "starting" this long before it counts as dead
WIN = os.name == "nt"


class BridgeError(RuntimeError):
    """User-facing error (bad input, busy session, dsh missing...)."""


class AcpError(BridgeError):
    pass


class Cancelled(Exception):
    pass


# ============================================================================ small file utilities
def _retry(fn, tries=60):
    """Windows: another process reading a file blocks replace/open for a moment -> retry briefly."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


def _read_json(p, default=None):
    def rd():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    try:
        return _retry(rd)
    except (FileNotFoundError, ValueError):
        return default


def _write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    _retry(lambda: os.replace(tmp, p))


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================================ locks (liveness + session ownership)
class FileLock:
    """Exclusive, non-blocking, released automatically when the holding process dies."""

    def __init__(self, path):
        self.path, self.f = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            if WIN:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self):
        if not self.f:
            return
        try:
            if WIN:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()
        self.f = None


def lock_held(path):
    if not Path(path).exists():
        return False
    probe = FileLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


def session_lock_path(sid):
    return LOCKS / (hashlib.sha1(sid.encode()).hexdigest()[:20] + ".lock")


def check_session_id(sid):
    if not SID_RE.match(sid or ""):
        raise BridgeError(f"invalid session id: {sid!r}")
    return sid


def check_job_id(job):
    if not JOB_RE.match(job or ""):
        raise BridgeError(f"invalid job id: {job!r}")
    return job


# ============================================================================ locating and killing dsh
def _which(cmd):
    return shutil.which(cmd) or cmd


def dsh_command():
    """Return (argv, cwd) that starts dsh; argv excludes the profile flags."""
    if os.environ.get("DSH_CMD"):
        argv = [a.strip('"') for a in shlex.split(os.environ["DSH_CMD"], posix=not WIN)]
        return [_which(argv[0])] + argv[1:], None
    repo = os.environ.get("DSH_REPO")
    if repo:
        repo = Path(repo).expanduser()
        if not (repo / "apps" / "cli" / "src" / "bin.ts").exists():
            raise BridgeError(f"DSH_REPO={repo} is not a dsh source checkout")
        return [_which("node"), "--import", "tsx/esm", "apps/cli/src/bin.ts"], str(repo)
    exe = shutil.which("dsh")
    if exe:
        return [exe], None
    repo = Path.home() / "deepseek-harness"  # the default `git clone` location from dsh's README
    if (repo / "apps" / "cli" / "src" / "bin.ts").exists():
        return [_which("node"), "--import", "tsx/esm", "apps/cli/src/bin.ts"], str(repo)
    raise BridgeError("dsh not found: install it (npm i -g @deepseek-ai/dsh) or set DSH_CMD / DSH_REPO")


def kill_tree(p):
    if p.poll() is not None:
        return
    try:
        if WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


# ============================================================================ ACP client
class Acp:
    """Minimal ACP v1 client over a `dsh --profile acp` child process."""

    def __init__(self, sink=None, permission="deny"):
        if permission not in PERMISSIONS:
            raise BridgeError(f"permission must be one of {PERMISSIONS}, got {permission!r}")
        argv, cwd = dsh_command()
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if WIN else {"start_new_session": True}
        try:
            self.p = subprocess.Popen(argv + ["--profile", "acp"], cwd=cwd, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **kw)
        except OSError as e:
            raise BridgeError(f"cannot start dsh ({argv[0]}): {e}")
        self.sink = sink or (lambda kind, text: None)
        self.permission = permission
        self.closed = False
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self.stderr_tail = []
        self.tools = {}           # toolCallId -> {title, kind, rawInput}
        self.segments = [[]]      # assistant text, split at tool calls
        self.permissions = []     # [{decision, what}]
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # -- transport
    def _write(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                self.p.stdin.write(data)
                self.p.stdin.flush()
            except (OSError, ValueError) as e:
                raise AcpError(f"cannot write to dsh: {e}")

    def _read_err(self):
        for raw in iter(self.p.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            self.stderr_tail = (self.stderr_tail + [line])[-40:]
            if os.environ.get("DSH_BRIDGE_DEBUG"):
                sys.stderr.write("[dsh] " + line + "\n")

    def _read(self):
        try:
            for raw in iter(self.p.stdout.readline, b""):
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        continue
                    if "method" not in msg and "id" in msg:
                        q = self._pending.pop(msg["id"], None)
                        if q:
                            q.put(msg)
                    elif "id" in msg:
                        self._on_request(msg)
                    elif "method" in msg:
                        self._on_notification(msg)
                except Exception as e:  # one bad frame must not kill the connection
                    self.stderr_tail = (self.stderr_tail + [f"bridge: ignored bad frame ({e})"])[-40:]
        finally:
            self.closed = True
            err = "dsh exited" + (": " + " | ".join(self.stderr_tail[-5:]) if self.stderr_tail else "")
            for q in list(self._pending.values()):
                q.put({"error": {"message": err}})

    def call(self, method, params, timeout=None, tick=None):
        with self._lock:
            self._id += 1
            i = self._id
        q = queue.Queue()
        self._pending[i] = q
        if self.closed:
            self._pending.pop(i, None)
            raise AcpError(f"{method}: dsh is not running ({' | '.join(self.stderr_tail[-3:])})")
        self._write({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        deadline = None if timeout is None else time.time() + timeout
        while True:
            try:
                msg = q.get(timeout=1.0)
                break
            except queue.Empty:
                if tick:
                    tick()
                if deadline and time.time() > deadline:
                    self._pending.pop(i, None)
                    raise AcpError(f"{method}: no answer within {timeout:.0f}s")
        if "error" in msg:
            e = msg["error"]
            data = e.get("data")
            raise AcpError(f"{method}: {e.get('message')}" + (f" {json.dumps(data, ensure_ascii=False)}" if data else ""))
        return msg.get("result") or {}

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- agent -> client
    def _describe(self, tool_call):
        info = dict(self.tools.get(tool_call.get("toolCallId"), {}))
        info.update({k: v for k, v in tool_call.items() if v})
        what = info.get("title") or info.get("kind") or "tool"
        raw = info.get("rawInput")
        if raw:
            raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            what += f" {raw[:300]}"
        return what

    def _on_request(self, msg):
        if msg.get("method") == "session/request_permission":
            prm = msg.get("params") or {}
            opts = [o for o in prm.get("options") or [] if isinstance(o, dict) and o.get("optionId")]
            want = "allow_once" if self.permission == "allow" else "reject_once"
            pick = next((o for o in opts if o.get("kind") == want), None)
            what = self._describe(prm.get("toolCall") or {})
            decision = "ALLOWED" if pick and want == "allow_once" else "DENIED"
            self.permissions.append({"decision": decision, "what": what})
            self.sink("perm", f"[permission {decision}] {what}\n")
            outcome = {"outcome": "selected", "optionId": pick["optionId"]} if pick else {"outcome": "cancelled"}
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": outcome}})
        else:
            self._write({"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": "not supported by dsh_bridge"}})

    def _on_notification(self, msg):
        if msg.get("method") != "session/update":
            return
        u = (msg.get("params") or {}).get("update") or {}
        kind = u.get("sessionUpdate")
        c = u.get("content")
        text = c.get("text", "") if isinstance(c, dict) else ""
        if kind == "agent_message_chunk":
            self.segments[-1].append(text)
            self.sink("message", text)
        elif kind == "agent_thought_chunk":
            self.sink("thought", text)
        elif kind in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            rec = self.tools.setdefault(tid, {})
            for k in ("title", "kind", "rawInput"):
                if u.get(k):
                    rec[k] = u[k]
            if kind == "tool_call":
                if self.segments[-1]:
                    self.segments.append([])
                self.sink("tool", f"[tool] {rec.get('title') or rec.get('kind') or ''}\n")
            elif u.get("status") in ("completed", "failed"):
                self.sink("tool", f"[tool {u['status']}] {rec.get('title') or ''}\n")

    def reply(self):
        parts = ["".join(s).strip() for s in self.segments]
        parts = [p for p in parts if p]
        return (parts[-1] if parts else ""), "\n\n".join(parts)

    # -- high level
    def initialize(self, tick=None):
        return self.call("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "dsh-bridge", "version": __version__}}, timeout=300, tick=tick)

    def open_session(self, cwd, session=None, tick=None):
        if session:
            r = self.call("session/resume", {"sessionId": session, "cwd": cwd, "mcpServers": []}, timeout=300, tick=tick)
            return session, r.get("configOptions")
        r = self.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=300, tick=tick)
        return r["sessionId"], r.get("configOptions")

    def configure(self, sid, opts, model=None, effort=None, tick=None):
        if model:
            self.call("session/set_config_option", {"sessionId": sid, "configId": "model",
                                                    "value": resolve_model(model, opts)}, timeout=120, tick=tick)
        if effort:
            allowed = [o["value"] for o in option_values(opts, "reasoning_effort")]
            if allowed and effort not in allowed:
                raise BridgeError(f"effort {effort!r} not available; choose one of {allowed}")
            self.call("session/set_config_option", {"sessionId": sid, "configId": "reasoning_effort",
                                                    "value": effort}, timeout=120, tick=tick)

    def kill(self):
        kill_tree(self.p)

    def close(self, sid=None):
        if sid and not self.closed:
            try:
                self.call("session/close", {"sessionId": sid}, timeout=60)
            except Exception:
                pass
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=30)
        except Exception:
            kill_tree(self.p)


# ============================================================================ model options
def option_values(opts, oid):
    out = []
    for o in opts or []:
        if o.get("id") != oid:
            continue
        for v in o.get("options", []):
            for x in (v.get("options", []) if "group" in v else [v]):
                out.append({"raw": x.get("value"), "value": pretty_value(x.get("value")),
                            "name": x.get("name"), "description": x.get("description")})
    return out


def pretty_value(v):
    try:
        prov, mid = json.loads(v)
        return f"{prov}/{mid}"
    except Exception:
        return v


def resolve_model(m, opts):
    """Accept 'model-id', 'provider/model-id' (model ids may contain '/'), or dsh's raw JSON value."""
    if m.startswith("["):
        return m
    known = option_values(opts, "model")
    if "/" in m:
        prov, _, mid = m.partition("/")
        val = json.dumps([prov, mid], separators=(",", ":"))
        if known and val not in [k["raw"] for k in known]:
            hits = [k["raw"] for k in known if k["value"].endswith("/" + m)]
            if len(hits) == 1:
                return hits[0]
            raise BridgeError(f"model {m!r} not available; choose one of {[k['value'] for k in known]}")
        return val
    hits = [k["raw"] for k in known if k["value"].split("/", 1)[-1] == m]
    if len(hits) == 1:
        return hits[0]
    if known:
        raise BridgeError(f"model {m!r} is {'ambiguous' if hits else 'not available'}; "
                          f"use provider/model, one of {[k['value'] for k in known]}")
    return json.dumps(["deepseek-official", m], separators=(",", ":"))


# ============================================================================ remembered sessions per cwd
def _last_file(cwd):
    return LAST_DIR / (hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:20] + ".json")


def remember_session(cwd, sid):
    _write_json(_last_file(cwd), {"cwd": cwd, "session": sid, "time": _now()})


def last_session(cwd):
    return (_read_json(_last_file(cwd), {}) or {}).get("session")


# ============================================================================ one turn
def run_turn(cwd, text, session=None, model=None, effort=None, permission="deny", sink=None,
             cancel_flag=None, on_session=None):
    """Run one prompt turn and return {session, stop, reply, full_reply, permissions}."""
    acp = Acp(sink=sink, permission=permission)
    sid, lock, killed = None, None, False
    cancel_sent = [None]

    def tick_setup():
        if cancel_flag and cancel_flag():
            raise Cancelled()

    def tick_prompt():
        if not (cancel_flag and cancel_flag()):
            return
        if cancel_sent[0] is None:
            acp.notify("session/cancel", {"sessionId": sid})
            cancel_sent[0] = time.time()
            acp.sink("perm", "\n[bridge] cancel requested\n")
        elif time.time() - cancel_sent[0] > CANCEL_GRACE:
            raise Cancelled()

    try:
        acp.initialize(tick_setup)
        if session:
            lock = FileLock(session_lock_path(session))
            if not lock.acquire():
                lock = None
                raise BridgeError(f"session {session} is busy (another bridge turn is running it)")
        sid, opts = acp.open_session(cwd, session, tick_setup)
        if not lock:
            lock = FileLock(session_lock_path(sid))
            lock.acquire()
        acp.configure(sid, opts, model, effort, tick_setup)
        remember_session(cwd, sid)
        if on_session:
            on_session(sid)
        r = acp.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}, tick=tick_prompt)
        stop = r.get("stopReason")
    except Cancelled:
        acp.kill()
        killed = True
        stop = "cancelled"
    finally:
        if not killed:
            acp.close(sid)
        if lock:
            lock.release()
    final, full = acp.reply()
    return {"session": sid, "stop": stop, "reply": final, "full_reply": full, "permissions": acp.permissions}


# ============================================================================ output sink
class Tee:
    def __init__(self, log=None, quiet=False):
        self.f = open(log, "a", encoding="utf-8") if log else None
        self.quiet, self.kind = quiet, None

    def __call__(self, kind, text):
        if kind != self.kind:
            if self.kind is not None:
                self._w("\n")
            if kind == "thought":
                self._w("[thinking] ")
            self.kind = kind
        self._w(text)

    def _w(self, s):
        if not self.quiet:
            sys.stderr.write(s)
            sys.stderr.flush()
        if self.f:
            self.f.write(s)
            self.f.flush()

    def close(self):
        if self.f:
            self.f.close()


# ============================================================================ inputs
def decode_text(data, what):
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BridgeError(f"{what} is not UTF-8 (or UTF-16 with BOM); re-save it as UTF-8")


def read_message(text=None, file=None):
    if file:
        msg = decode_text(Path(file).read_bytes(), f"file {file}")
    elif text == "-":
        msg = decode_text(sys.stdin.buffer.read(), "stdin")
    else:
        msg = text or ""
    if not msg.strip():
        raise BridgeError("empty message (give TEXT, --file F, or '-' to read stdin)")
    return msg


def check_permission(p):
    p = (p or os.environ.get("DSH_BRIDGE_PERMISSION") or "deny").strip().lower()
    if p not in PERMISSIONS:
        raise BridgeError(f"permission must be 'allow' or 'deny', got {p!r}")
    return p


def check_cwd(cwd):
    p = Path(cwd).expanduser()
    if not p.is_absolute():
        p = p.resolve()
    if not p.is_dir():
        raise BridgeError(f"cwd is not an existing directory: {cwd}")
    return str(p.resolve())


# ============================================================================ jobs
def _job_dir(job):
    return JOBS / check_job_id(job)


def job_state(job):
    """Read a job's meta; derive 'died' in memory only (the job process is the sole writer)."""
    d = _job_dir(job)
    m = _read_json(d / "meta.json")
    if m is None:
        raise BridgeError(f"no such job: {job}")
    if m.get("status") in ("starting", "running") and not lock_held(d / "lock"):
        m2 = _read_json(d / "meta.json", m)          # it may have finished meanwhile
        if m2.get("status") in ("starting", "running"):
            young = m2.get("status") == "starting" and time.time() - m2.get("created", 0) < STARTUP_GRACE
            if not young:
                m2 = dict(m2, status="error", error="job process ended without finishing (killed or crashed); "
                                                     "see bridge.err and log.md")
        m = m2
    return m


def job_report(job, tail=3000):
    m = job_state(job)
    d = _job_dir(job)
    keys = ("job", "status", "stop", "session", "cwd", "model", "effort", "permission", "started", "finished", "error")
    out = {k: m[k] for k in keys if m.get(k) is not None}
    if (d / "reply.md").exists():
        out["reply"] = (d / "reply.md").read_text(encoding="utf-8")
    if m.get("permissions"):
        out["permissions"] = m["permissions"]
    if m.get("status") not in ("done", "cancelled"):
        try:
            out["log_tail"] = (d / "log.md").read_text(encoding="utf-8")[-tail:]
        except FileNotFoundError:
            pass
    out["log"] = str(d / "log.md")
    return out


def _jobs_for(pred):
    if not JOBS.exists():
        return []
    out = []
    for d in sorted(JOBS.iterdir(), reverse=True):
        if JOB_RE.match(d.name):
            try:
                m = job_state(d.name)
            except BridgeError:
                continue
            if pred(m):
                out.append(m)
    return out


def start_job(cwd, text, session=None, last=False, model=None, effort=None, permission=None, session_wait=30.0):
    """Start a background turn; return {job, session, status, log} (session may be None if dsh is still booting)."""
    cwd = check_cwd(cwd)
    permission = check_permission(permission)
    if session:
        check_session_id(session)
    elif last:
        pending = _jobs_for(lambda m: m.get("cwd") == cwd and m.get("status") == "starting" and not m.get("session"))
        if pending:
            raise BridgeError(f"job {pending[0]['job']} for this cwd has not reported its session yet; "
                              "wait for it, or pass --session")
        session = last_session(cwd)
        if not session:
            raise BridgeError(f"no previous session recorded for {cwd}")
    if session and lock_held(session_lock_path(session)):
        raise BridgeError(f"session {session} is busy (another bridge turn is running it)")
    job = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = JOBS / job
    d.mkdir(parents=True)
    (d / "prompt.md").write_text(text, encoding="utf-8")
    _write_json(d / "meta.json", {"job": job, "cwd": cwd, "session": session, "model": model, "effort": effort,
                                  "permission": permission, "status": "starting", "started": _now(),
                                  "created": time.time()})
    argv = [sys.executable, str(Path(__file__).resolve()), "_run-job", job]
    err = open(d / "bridge.err", "w", encoding="utf-8")
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, close_fds=True)
    if WIN:
        kw["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kw)
    finally:
        err.close()
    t0 = time.time()
    while True:
        m = _read_json(d / "meta.json", {}) or {}
        if m.get("session") or m.get("status") not in ("starting", "running") or time.time() - t0 >= session_wait:
            break
        time.sleep(0.3)
    m = job_state(job)
    out = {"job": job, "session": m.get("session"), "status": m.get("status"), "log": str(d / "log.md")}
    if m.get("error"):
        out["error"] = m["error"]
    if not out["session"]:
        out["hint"] = "dsh is still starting; call wait/status with this job id to get the session id"
    return out


def run_job(job):
    """Body of the detached job process. Sole writer of meta.json after start-up."""
    d = _job_dir(job)
    lock = FileLock(d / "lock")
    if not lock.acquire():
        return 1
    meta = _read_json(d / "meta.json", {})
    meta.update(status="running", pid=os.getpid())
    _write_json(d / "meta.json", meta)
    tee = Tee(d / "log.md", quiet=True)
    text = (d / "prompt.md").read_text(encoding="utf-8")
    tee("header", f"## -> DeepSeek ({meta.get('started')})\n\n{text}\n\n## DeepSeek\n\n")

    def on_session(sid):
        meta["session"] = sid
        _write_json(d / "meta.json", meta)

    try:
        r = run_turn(meta["cwd"], text, meta.get("session"), meta.get("model"), meta.get("effort"),
                     meta.get("permission", "deny"), sink=tee, cancel_flag=lambda: (d / "cancel").exists(),
                     on_session=on_session)
        (d / "reply.md").write_text(r["reply"], encoding="utf-8")
        tee("header", f"\n\n## Stop: {r['stop']}\n")
        meta.update(status="cancelled" if r["stop"] == "cancelled" else "done", stop=r["stop"],
                    session=r["session"] or meta.get("session"), permissions=r["permissions"])
    except Exception as e:  # noqa: BLE001
        meta.update(status="error", error=str(e))
        tee("header", f"\n\n## Error: {e}\n")
    finally:
        tee.close()
        meta["finished"] = _now()
        _write_json(d / "meta.json", meta)
        lock.release()
    return 0


def wait_job(job, timeout):
    t0 = time.time()
    while True:
        m = job_state(job)
        if m.get("status") not in ("starting", "running") or time.time() - t0 >= timeout:
            return job_report(job)
        time.sleep(1.0)


def cancel_job(job):
    m = job_state(job)
    if m.get("status") not in ("starting", "running"):
        return {"job": job, "status": m.get("status"), "note": "not running"}
    (_job_dir(job) / "cancel").write_text("1", encoding="utf-8")
    return {"job": job, "status": "cancelling",
            "note": f"dsh gets {CANCEL_GRACE:.0f}s to stop, then it is killed; check with wait"}


def recent_jobs(limit=10, cwd=None):
    keys = ("job", "status", "stop", "session", "cwd", "started", "finished")
    cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
    return [{k: m.get(k) for k in keys} for m in _jobs_for(lambda m: not cwd or m.get("cwd") == cwd)[:limit]]


def prune_jobs(days=14.0):
    cutoff, removed = time.time() - days * 86400, []
    for m in _jobs_for(lambda m: m.get("status") not in ("starting", "running")):
        d = JOBS / m["job"]
        if d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(m["job"])
    return removed


# ============================================================================ sessions, models, history
def list_sessions(cwd=None, limit=20):
    acp = Acp()
    try:
        acp.initialize()
        params, got, cursor = ({"cwd": check_cwd(cwd)} if cwd else {}), [], None
        while len(got) < limit:
            if cursor:
                params["cursor"] = cursor
            r = acp.call("session/list", params, timeout=120)
            got += r.get("sessions", [])
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return [{"session": x.get("sessionId"), "cwd": x.get("cwd"), "title": x.get("title"),
                 "updated": x.get("updatedAt")} for x in got[:limit]]
    finally:
        acp.close()


def list_models(cwd):
    """Reuses the last bridge session for cwd when idle; otherwise opens a new (empty) session."""
    cwd = check_cwd(cwd)
    reuse = last_session(cwd)
    if reuse and lock_held(session_lock_path(reuse)):
        reuse = None
    acp = Acp()
    sid = None
    try:
        acp.initialize()
        try:
            sid, opts = acp.open_session(cwd, reuse)
        except AcpError:
            if not reuse:
                raise
            sid, opts = acp.open_session(cwd)
        return {oid: {"current": pretty_value(next((o.get("currentValue") for o in opts or [] if o.get("id") == oid), None)),
                      "options": [{k: v for k, v in x.items() if k != "raw"} for x in option_values(opts, oid)]}
                for oid in ("model", "reasoning_effort")}
    finally:
        acp.close(sid)


def find_session_file(sid):
    check_session_id(sid)
    home = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")).expanduser()
    for name in ("session.v2.jsonl", "session.v2.jsonl.zstd"):
        hits = list((home / "sessions").glob(f"*/{sid}/{name}"))
        if hits:
            return hits[0]
    raise BridgeError(f"session {sid} not found under {home / 'sessions'}")


def _text(content):
    if isinstance(content, str):
        return content
    parts = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text":
            parts.append(p.get("text", ""))
        elif t == "file":
            parts.append(f"[file {(p.get('attachment') or {}).get('name', '')}]")
        elif t == "image":
            parts.append("[image]")
        elif t == "tool-result":
            parts.append(("[error] " if p.get("isError") else "") + _text(p.get("content")))
    return "\n".join(x for x in parts if x)


def read_history(sid, tail=40, max_chars=1200):
    f = find_session_file(sid)
    if f.suffix == ".zstd":
        try:
            import zstandard
        except ImportError:
            raise BridgeError("this session is zstd-compressed; run: pip install zstandard")
        import io
        with open(f, "rb") as fh:
            raw = io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(fh), encoding="utf-8").read()
    else:
        raw = f.read_text(encoding="utf-8")
    events = []
    for line in raw.splitlines():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if not isinstance(x, dict):
            continue
        t, d = x.get("type"), x.get("data") or {}
        content = d.get("content") or (d.get("message") or {}).get("content")
        if t == "user/message":
            body = _text(content)
            if body.strip() and not body.lstrip().startswith(("<system-reminder>", "Current runtime context")):
                events.append(("user", body))
        elif t == "assistant/message":
            body = _text(content)
            if body.strip():
                events.append(("assistant", body))
        elif t == "tool/call":
            args = d.get("arguments", "")
            events.append(("tool call", f"{d.get('name')} {args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}"))
        elif t == "tool/result":
            body = _text(content)
            if body.strip():
                events.append(("tool result", body))
    out = []
    for role, body in events[-tail:]:
        if len(body) > max_chars:
            body = body[:max_chars] + f" …[+{len(body) - max_chars} chars]"
        out.append(f"### {role}\n{body}")
    return "\n\n".join(out)


# ============================================================================ CLI
def main(argv=None):
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="dsh_bridge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def turn_args(p):
        p.add_argument("text", nargs="?", help="message text, or '-' to read stdin")
        p.add_argument("--cwd", required=True, help="directory DeepSeek works in")
        p.add_argument("--session", help="resume this session id")
        p.add_argument("--last", action="store_true", help="resume the last session used for --cwd")
        p.add_argument("--file", help="read the message from a UTF-8 (or UTF-16 with BOM) file")
        p.add_argument("--model", help="model id or provider/model (see `models`)")
        p.add_argument("--effort", help="reasoning effort (see `models`)")
        p.add_argument("--permission", choices=PERMISSIONS, help="answer to dsh permission prompts (default deny)")

    p = sub.add_parser("send"); turn_args(p)
    p.add_argument("--log", help="append the transcript to this file")
    p.add_argument("--quiet", action="store_true", help="no progress on stderr (default when stderr is not a TTY)")
    p.add_argument("--verbose", action="store_true", help="progress on stderr even when it is not a TTY")
    p = sub.add_parser("start"); turn_args(p)
    p.add_argument("--session-wait", type=float, default=30, help="seconds to wait for the session id")
    p = sub.add_parser("_run-job"); p.add_argument("job")
    p = sub.add_parser("wait"); p.add_argument("job"); p.add_argument("--timeout", type=float, default=600)
    p = sub.add_parser("status"); p.add_argument("job", nargs="?"); p.add_argument("--cwd"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("cancel"); p.add_argument("job")
    p = sub.add_parser("list"); p.add_argument("--cwd"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("models"); p.add_argument("--cwd", required=True)
    p = sub.add_parser("history"); p.add_argument("session"); p.add_argument("--tail", type=int, default=40)
    p.add_argument("--max-chars", type=int, default=1200)
    p = sub.add_parser("prune"); p.add_argument("--days", type=float, default=14)
    a = ap.parse_args(argv)
    dump = lambda o: print(json.dumps(o, ensure_ascii=False, indent=1))
    try:
        if a.cmd == "send":
            cwd, text = check_cwd(a.cwd), read_message(a.text, a.file)
            sid = a.session and check_session_id(a.session)
            if a.last and not sid:
                sid = last_session(cwd)
                if not sid:
                    raise BridgeError(f"no previous session recorded for {cwd}")
            quiet = a.quiet or (not a.verbose and not sys.stderr.isatty())
            tee = Tee(a.log, quiet)
            tee("header", f"\n## -> DeepSeek ({_now()})\n\n{text}\n\n## DeepSeek\n\n")
            try:
                r = run_turn(cwd, text, sid, a.model, a.effort, check_permission(a.permission), sink=tee)
            finally:
                tee.close()
            print(r["reply"])
            for pm in r["permissions"]:
                sys.stderr.write(f"PERMISSION {pm['decision']} {pm['what']}\n")
            sys.stderr.write(f"SESSION {r['session']} STOP {r['stop']}\n")
            return 0 if r["stop"] == "end_turn" else 1
        if a.cmd == "start":
            dump(start_job(a.cwd, read_message(a.text, a.file), a.session, a.last, a.model, a.effort,
                           a.permission, a.session_wait))
            return 0
        if a.cmd == "_run-job":
            return run_job(a.job)
        if a.cmd == "wait":
            r = wait_job(a.job, a.timeout)
            dump(r)
            return {"done": 0, "starting": 2, "running": 2}.get(r.get("status"), 1)
        if a.cmd == "status":
            dump(job_report(a.job, 1500) if a.job else recent_jobs(a.limit, a.cwd))
            return 0
        if a.cmd == "cancel":
            dump(cancel_job(a.job))
            return 0
        if a.cmd == "list":
            for s in list_sessions(a.cwd, a.limit):
                print(f"{s['session']}\t{s['updated'] or ''}\t{s['title'] or ''}\t{s['cwd'] or ''}")
            return 0
        if a.cmd == "models":
            for k, v in list_models(a.cwd).items():
                print(f"{k}: current={v['current']}")
                for x in v["options"]:
                    print(f"  {x['value']}\t{x['name'] or ''}")
            return 0
        if a.cmd == "history":
            print(read_history(a.session, a.tail, a.max_chars))
            return 0
        if a.cmd == "prune":
            dump({"removed": prune_jobs(a.days)})
            return 0
    except (BridgeError, OSError) as e:
        sys.stderr.write(f"dsh_bridge: {e}\n")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())

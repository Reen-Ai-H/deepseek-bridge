"""A fake `dsh --profile acp` that speaks the same ACP frames, for testing without DeepSeek.

Behaviour via environment:
  FAKE_MODE   ok | crash | badframe | slowinit | ignorecancel
  FAKE_SLEEP  "lo,hi" seconds to work before ending the turn (default 0,0)
Like real dsh, the permission request carries only the toolCallId; title and rawInput arrive earlier
in a tool_call update.
"""
import json, os, random, sys, threading, time, uuid

MODE = os.environ.get("FAKE_MODE", "ok")
out = sys.stdout.buffer
lock = threading.Lock()
pending = {}            # our request id -> Queue-like list
cancelled = set()


def send(o):
    with lock:
        out.write((json.dumps(o) + "\n").encode())
        out.flush()


def ask(method, params):
    rid = random.randint(10_000, 99_999)
    ev = threading.Event()
    pending[rid] = [ev, None]
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    ev.wait(30)
    return pending.pop(rid)[1]


def prompt(i, sid):
    upd = lambda u: send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})
    upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "I will check."}})
    upd({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "bash", "kind": "execute",
         "rawInput": {"command": "rm -rf build"}})
    ans = ask("session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "t1"}, "options": [
        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]})
    sys.stderr.write(f"permission answer: {json.dumps(ans)}\n"); sys.stderr.flush()
    upd({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    if MODE == "crash":
        sys.stderr.write("boom: provider error\n"); sys.stderr.flush()
        os._exit(3)
    if MODE == "badframe":
        with lock:
            out.write(b"123\n[1,2]\nnull\n{not json\n"); out.flush()
    lo, hi = map(float, os.environ.get("FAKE_SLEEP", "0,0").split(","))
    t_end = time.time() + random.uniform(lo, hi)
    while time.time() < t_end:
        if sid in cancelled and MODE != "ignorecancel":
            send({"jsonrpc": "2.0", "id": i, "result": {"stopReason": "cancelled"}})
            return
        time.sleep(0.1)
    upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}})
    send({"jsonrpc": "2.0", "id": i, "result": {"stopReason": "end_turn"}})


OPTIONS = [{"id": "model", "type": "select", "currentValue": '["deepseek-official","deepseek-v4-flash"]',
            "options": [{"group": "deepseek-official", "options": [
                {"value": '["deepseek-official","deepseek-v4-flash"]', "name": "Flash"},
                {"value": '["deepseek-official","deepseek-v4-pro"]', "name": "Pro"}]},
                {"group": "openrouter", "options": [
                    {"value": '["openrouter","deepseek/deepseek-chat"]', "name": "OR chat"}]}]},
           {"id": "reasoning_effort", "type": "select", "currentValue": "high",
            "options": [{"value": v, "name": v} for v in ("off", "low", "high", "max")]}]

for raw in sys.stdin.buffer:
    m = json.loads(raw)
    meth, i = m.get("method"), m.get("id")
    if meth is None:                      # a response to one of our requests
        if i in pending:
            pending[i][1] = m.get("result")
            pending[i][0].set()
        continue
    p = m.get("params") or {}
    if meth == "initialize":
        if MODE == "slowinit":
            time.sleep(10_000)
        send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": 1}})
    elif meth == "session/new":
        send({"jsonrpc": "2.0", "id": i, "result": {"sessionId": str(uuid.uuid4()), "configOptions": OPTIONS}})
    elif meth == "session/resume":
        send({"jsonrpc": "2.0", "id": i, "result": {"configOptions": OPTIONS}})
    elif meth in ("session/set_config_option", "session/close"):
        send({"jsonrpc": "2.0", "id": i, "result": {}})
    elif meth == "session/list":
        send({"jsonrpc": "2.0", "id": i, "result": {"sessions": [{"sessionId": "s-1", "cwd": "/x", "title": "t"}]}})
    elif meth == "session/cancel":
        cancelled.add(p.get("sessionId"))
    elif meth == "session/prompt":
        threading.Thread(target=prompt, args=(i, p["sessionId"]), daemon=True).start()
    else:
        send({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "nope"}})

#!/usr/bin/env python3
"""Regression tests for dsh_bridge + the MCP server, against tests/fake_dsh.py (no DeepSeek account needed).

  python tests/run_tests.py            # all scenarios (~2 min)
  python tests/run_tests.py race mcp   # selected scenarios
"""
import json, os, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE, SERVER, FAKE = ROOT / "scripts" / "dsh_bridge.py", ROOT / "mcp" / "server.py", Path(__file__).with_name("fake_dsh.py")
TMP = Path(tempfile.mkdtemp(prefix="dsh-bridge-test-"))
WORK = TMP / "work 目录"
WORK.mkdir()
BASE_ENV = dict(os.environ, DSH_CMD=f'"{sys.executable}" "{FAKE}"', DSH_BRIDGE_HOME=str(TMP / "home"),
                DSH_BRIDGE_CANCEL_GRACE="3", PYTHONIOENCODING="utf-8")
BASE_ENV.pop("DSH_BRIDGE_PERMISSION", None)
FAILS = []


def run(args, env=None, inp=None, timeout=60):
    e = dict(BASE_ENV, **(env or {}))
    return subprocess.run([sys.executable, str(BRIDGE)] + args, input=inp, capture_output=True, env=e, timeout=timeout)


def out(r):
    return r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def t_send():
    o, e = out(run(["send", "--cwd", str(WORK), "hi"]))
    check("send: final reply is last segment", o.strip() == "Done.", o)
    check("send: permission denied by default, real command logged", "PERMISSION DENIED bash" in e and "rm -rf build" in e, e)
    o, e = out(run(["send", "--cwd", str(WORK), "--permission", "allow", "hi"]))
    check("send: --permission allow", "PERMISSION ALLOWED" in e, e)
    r = run(["send", "--cwd", str(WORK), "hi"], env={"DSH_BRIDGE_PERMISSION": "reject"})
    check("send: unknown permission value fails closed", r.returncode == 1 and "allow' or 'deny" in out(r)[1], out(r))
    o, e = out(run(["send", "--cwd", str(WORK), "--last", "again"]))
    check("send: --last resumes", "STOP end_turn" in e, e)


def t_inputs():
    f = TMP / "brief16.md"
    f.write_bytes("修复 bug".encode("utf-16"))
    r = run(["send", "--cwd", str(WORK), "--file", str(f)])
    check("input: UTF-16 brief accepted", r.returncode == 0, out(r))
    f.write_bytes("修复".encode("gbk"))
    r = run(["send", "--cwd", str(WORK), "--file", str(f)])
    check("input: non-UTF-8 brief -> friendly error", r.returncode == 1 and "not UTF-8" in out(r)[1], out(r))
    r = run(["send", "--cwd", str(WORK), "-"], inp="修复 bug".encode("utf-8"))
    check("input: stdin '-' decoded as UTF-8", r.returncode == 0, out(r))
    r = run(["status", "../.."])
    check("input: job id validated", r.returncode == 1 and "invalid job id" in out(r)[1], out(r))
    r = run(["send", "--cwd", str(WORK), "hi"], env={"DSH_CMD": "definitely-not-a-dsh"})
    check("input: missing dsh -> friendly error", r.returncode == 1 and "cannot start dsh" in out(r)[1], out(r))
    r = run(["send", "--cwd", str(WORK), "--model", "nope", "hi"])
    check("input: unknown model lists choices", r.returncode == 1 and "deepseek-v4-pro" in out(r)[1], out(r))


def t_models():
    r = run(["send", "--cwd", str(WORK), "--model", "openrouter/deepseek/deepseek-chat", "--effort", "low", "hi"])
    check("models: provider/model with slash", r.returncode == 0, out(r))
    r = run(["send", "--cwd", str(WORK), "--model", "deepseek-v4-pro", "hi"])
    check("models: bare id resolved", r.returncode == 0, out(r))
    o, _ = out(run(["models", "--cwd", str(WORK)]))
    check("models: listing", "deepseek-official/deepseek-v4-pro" in o, o)


def t_faults():
    t0 = time.time()
    r = run(["send", "--cwd", str(WORK), "hi"], env={"FAKE_MODE": "badframe"}, timeout=30)
    check("fault: bad frames do not hang", r.returncode == 0 and time.time() - t0 < 25, out(r))
    r = run(["send", "--cwd", str(WORK), "hi"], env={"FAKE_MODE": "crash"}, timeout=30)
    check("fault: dsh crash reported with its stderr", r.returncode == 1 and "boom" in out(r)[1], out(r))


def start(text, env=None, extra=()):
    r = run(["start", "--cwd", str(WORK), "--session-wait", "10", *extra, text], env=env)
    return json.loads(r.stdout) if r.returncode == 0 else {"error": out(r)}


def wait(job, t=60):
    return json.loads(run(["wait", job, "--timeout", str(t)], timeout=t + 30).stdout)


def t_jobs():
    j = start("x", env={"FAKE_SLEEP": "6,6"})
    check("jobs: start returns session", bool(j.get("session")), j)
    j2 = start("y", extra=("--session", j["session"]))
    check("jobs: busy session refused", "busy" in json.dumps(j2), j2)
    r = wait(j["job"])
    check("jobs: wait -> done + reply", r.get("status") == "done" and r.get("reply") == "Done.", r)
    j = start("z", env={"FAKE_SLEEP": "30,30"})
    time.sleep(2)
    run(["cancel", j["job"]])
    r = wait(j["job"], 30)
    check("jobs: cancel honoured", r.get("status") == "cancelled", r)
    j = start("z", env={"FAKE_SLEEP": "60,60", "FAKE_MODE": "ignorecancel"})
    time.sleep(2)
    run(["cancel", j["job"]])
    r = wait(j["job"], 40)
    check("jobs: ignored cancel -> killed after grace", r.get("status") == "cancelled", r)


def t_race():
    def one(k):
        j = start(f"job {k}", env={"FAKE_SLEEP": "0.5,4"})
        return wait(j["job"]) if "job" in j else j
    res = [one(k) for k in range(12)]
    bad = [r for r in res if r.get("status") != "done" or r.get("reply") != "Done."]
    check(f"race: 12 sequential jobs all done ({len(bad)} bad)", not bad, bad[:2])
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, range(32)))
    bad = [r for r in res if r.get("status") != "done" or r.get("reply") != "Done."]
    check(f"race: 32 jobs, 8 in parallel, all done ({len(bad)} bad)", not bad, bad[:2])


def t_mcp():
    env = dict(BASE_ENV, FAKE_MODE="slowinit")
    p = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env)
    got = {}
    threading.Thread(target=lambda: [got.setdefault(json.loads(l).get("id"), json.loads(l)) for l in p.stdout],
                     daemon=True).start()

    def send(o):
        p.stdin.write((o if isinstance(o, str) else json.dumps(o)).encode() + b"\n")
        p.stdin.flush()

    def get(i, t=10):
        t0 = time.time()
        while i not in got and time.time() - t0 < t:
            time.sleep(0.05)
        return got.get(i)

    call = lambda i, n, a: send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}})
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
    check("mcp: unknown protocol version -> latest supported", get(1)["result"]["protocolVersion"] == "2025-06-18", get(1))
    send({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    check("mcp: supported version echoed", get(2)["result"]["protocolVersion"] == "2025-03-26", get(2))
    call(3, "dsh_wait", {"job": "x", "timeout_s": None})
    check("mcp: bad argument type -> -32602", (get(3) or {}).get("error", {}).get("code") == -32602, get(3))
    call(4, "nope", {})
    check("mcp: unknown tool -> -32602", (get(4) or {}).get("error", {}).get("code") == -32602, get(4))
    send("not json"); send("[1,2]")
    check("mcp: parse error / invalid request answered", sum(1 for k, v in got.items() if k is None) >= 0 and get(None, 3) is not None, got.get(None))
    call(5, "dsh_wait", {"job": "../..", "timeout_s": 0})
    check("mcp: bad job id -> isError", (get(5) or {}).get("result", {}).get("isError") is True, get(5))
    call(6, "dsh_start", {"cwd": str(WORK), "message": "hi"})     # dsh never finishes initialize
    time.sleep(2)
    send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    send({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    check("mcp: ping answered while dsh_start blocks", get(7, 5) is not None, got.keys())
    check("mcp: tools/list answered while dsh_start blocks", get(8, 5) is not None, got.keys())
    r = get(6, 30)
    check("mcp: dsh_start returns within ~15 s with a hint", r is not None and "hint" in r["result"]["content"][0]["text"], r)
    p.stdin.close()
    p.kill()


SCENARIOS = {"send": t_send, "inputs": t_inputs, "models": t_models, "faults": t_faults, "jobs": t_jobs,
             "race": t_race, "mcp": t_mcp}

if __name__ == "__main__":
    for name in sys.argv[1:] or SCENARIOS:
        print(f"== {name}")
        SCENARIOS[name]()
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}   (state in {TMP})")
    sys.exit(1 if FAILS else 0)

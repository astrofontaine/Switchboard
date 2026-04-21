#!/usr/bin/env python3
"""Quick health check — reads ~/agent_config.json and reports pass/fail for each check."""
import json, os, subprocess, time

CFG      = json.load(open(os.path.expanduser("~/agent_config.json")))
AGENT    = CFG["name"]
CHAT_LOG = CFG["local_log"]
checks   = []

r = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}","http://192.168.2.16:5000/"], capture_output=True, text=True)
checks.append(("chat_server_up",    r.stdout.strip() == "200"))

r = subprocess.run(["pgrep","-f","agent_listener.py"], capture_output=True, text=True)
checks.append(("listener_process",  bool(r.stdout.strip())))

try:
    age = time.time() - os.path.getmtime(CHAT_LOG)
    checks.append(("log_fresh_<120s", age < 120))
except Exception:
    checks.append(("log_fresh_<120s", False))

r = subprocess.run(["crontab","-l"], capture_output=True, text=True)
checks.append(("cron_respond",      "agent_respond"  in r.stdout))
checks.append(("cron_watchdog",     "agent_watchdog" in r.stdout))

r = subprocess.run(["mountpoint","-q","/mnt/shared"], capture_output=True)
checks.append(("nfs_mounted",       r.returncode == 0))

for label, path in [
    ("file:CLAUDE.md",         os.path.expanduser(f"~/{AGENT}/CLAUDE.md")),
    ("file:agent_chat.log",    CHAT_LOG),
    ("file:agent_config.json", os.path.expanduser("~/agent_config.json")),
]:
    checks.append((label, os.path.exists(path)))

for host in [t["name"] for t in CFG.get("teammates", []) if t["name"] != "human"] + ["human"]:
    r = subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=5",host,"echo ok"],
                       capture_output=True, text=True)
    checks.append((f"ssh:{host}", r.stdout.strip() == "ok"))

for name, ok in checks:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
overall = "ALL PASS" if all(ok for _, ok in checks) else "SOME FAILED"
print(f"\n{AGENT.upper()}: {overall}")

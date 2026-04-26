#!/usr/bin/env python3
"""Config-driven Switchboard agent health check.

Run from any agent VM:
    python3 /mnt/shared/agent_healthcheck.py
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def run(argv, timeout=8):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def exists(path):
    return Path(os.path.expanduser(path)).exists()


def main():
    config_path = Path(os.path.expanduser("~/agent_config.json"))
    checks = []

    if not config_path.exists():
        print("  [FAIL] file:agent_config.json")
        print("\nUNKNOWN: SOME FAILED")
        return 1

    cfg = json.loads(config_path.read_text())
    agent = cfg["name"]
    chat_url = cfg.get("chat_url", "http://192.168.2.16:5000")
    chat_log = os.path.expanduser(cfg.get("local_log", f"~/{agent}/agent_chat.log"))
    listener_script = cfg.get("listener_script", "/home/longshot/Switchboard/agents/agent_listener.py")
    backend = cfg.get("responder_backend", "claude")
    runtime_mode = cfg.get("runtime_mode", "legacy")

    try:
        r = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", chat_url])
        checks.append(("chat_server_up", r.stdout.strip() == "200"))
    except Exception:
        checks.append(("chat_server_up", False))

    r = run(["pgrep", "-fa", "agent_listener.py"])
    checks.append(("listener_process", listener_script in r.stdout or "agent_listener.py" in r.stdout))

    try:
        age = time.time() - os.path.getmtime(chat_log)
        checks.append(("log_fresh_<180s", runtime_mode == "embedded" or age < 180))
    except Exception:
        checks.append(("log_fresh_<180s", False))

    r = run(["crontab", "-l"])
    cron = r.stdout
    checks.append(("netwatch_state_or_cron", exists("~/.netwatch/hosts.json") or "/netwatch/netwatch" in cron))
    checks.append(("cron_listener_at_reboot", "agent_listener.py" in cron))
    checks.append(("cron_watchdog", "agent_watchdog.py" in cron))
    if runtime_mode == "embedded":
        checks.append(("cron_respond_dispatcher_disabled", "agent_respond.py" not in cron))
    else:
        checks.append(("cron_respond_dispatcher", "agent_respond.py" in cron))

    r = run(["mountpoint", "-q", "/mnt/shared"])
    checks.append(("nfs_mounted", r.returncode == 0))
    probe = Path(f"/mnt/shared/.healthcheck_{agent}_{os.getpid()}")
    try:
        probe.write_text("ok\n")
        probe.unlink()
        checks.append(("nfs_writable", True))
    except Exception:
        checks.append(("nfs_writable", False))

    for label, path in [
        ("file:CLAUDE.md", f"~/{agent}/CLAUDE.md"),
        ("file:agent_chat.log", chat_log),
        ("file:agent_config.json", "~/agent_config.json"),
        ("file:shared_VERIFY.md", "/mnt/shared/VERIFY.md"),
        ("file:shared_SYSTEM_OVERVIEW.md", "/mnt/shared/SYSTEM_OVERVIEW.md"),
        ("file:CHAT_REPLAY.jsonl", "/mnt/shared/CHAT_REPLAY.jsonl"),
        ("file:UNIVERSAL_CHAT.log", "/mnt/shared/UNIVERSAL_CHAT.log"),
        ("file:hosts.json", "~/.netwatch/hosts.json"),
    ]:
        checks.append((label, exists(path)))

    ssh_hosts = {"proxmox", "human"}
    ssh_hosts.update(cfg.get("peers", []))
    ssh_hosts.update(t.get("name") for t in cfg.get("teammates", []) if t.get("name"))
    ssh_hosts.discard(agent)
    for host in sorted(ssh_hosts):
        try:
            r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "echo ok"])
            checks.append((f"ssh:{host}", r.stdout.strip() == "ok"))
        except Exception:
            checks.append((f"ssh:{host}", False))

    if agent == "keystone":
        checks.append(("repo:Switchboard_exists", exists("~/Switchboard/.git")))
        if exists("~/Switchboard/.git"):
            r = run(["git", "-C", os.path.expanduser("~/Switchboard"), "status", "--short", "--branch"])
            checks.append(("repo:Switchboard_status_ok", r.returncode == 0 and "## " in r.stdout))

    width = max(len(name) for name, _ in checks)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}")

    overall = all(ok for _, ok in checks)
    print(f"\n{agent.upper()}@{socket.gethostname()}: {'ALL CHECKS PASSED' if overall else 'SOME CHECKS FAILED'}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())

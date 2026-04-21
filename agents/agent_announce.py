#!/usr/bin/env python3
"""PostToolUse hook — announce bash commands to chat. Pipe Claude's tool JSON to stdin."""
import datetime, json, os, socketio, sys, time

CFG      = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME  = CFG["name"]
VMID     = CFG["vmid"]
CHAT_URL = CFG["chat_url"]

try:
    data = json.load(sys.stdin)
    cmd  = data.get("tool_input", {}).get("command", "")
    if not cmd:
        sys.exit(0)
    ts  = datetime.datetime.now().strftime("%H:%M:%S")
    msg = f"[{MY_NAME}|VMID={VMID}|{ts}] exec: {cmd[:200]}"
    sio = socketio.SimpleClient()
    sio.connect(CHAT_URL, wait_timeout=5)
    sio.emit("msg", {"name": MY_NAME, "text": msg})
    time.sleep(0.3)
    sio.disconnect()
except Exception:
    pass

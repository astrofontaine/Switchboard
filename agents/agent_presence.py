#!/usr/bin/env python3
"""Scan chat log and emit last-seen roster every 7 minutes."""
import datetime, json, os, re, socketio, time

CFG      = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME  = CFG["name"]
LOG_PATH = CFG["local_log"]
CHAT_URL = CFG["chat_url"]
INTERVAL = 7 * 60

MSG_RE = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\] <([^>]+)>')
SKIP   = {"heartbeat"}

def parse_last_seen():
    last_seen = {}
    try:
        with open(LOG_PATH) as f:
            for line in f:
                m = MSG_RE.match(line)
                if not m:
                    continue
                ts_str, name = m.group(1), m.group(2)
                if name in SKIP:
                    continue
                last_seen[name] = ts_str
    except FileNotFoundError:
        pass
    return last_seen

def time_ago(ts_str):
    now = datetime.datetime.now()
    t = datetime.datetime.strptime(ts_str, "%H:%M:%S").replace(
        year=now.year, month=now.month, day=now.day)
    delta = int((now - t).total_seconds())
    if delta < 0:
        delta += 86400
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h {(delta % 3600) // 60}m ago"

def emit_roster():
    last_seen = parse_last_seen()
    if not last_seen:
        return
    entries = [f"{name} ({time_ago(ts)})" for name, ts in sorted(last_seen.items())]
    text = "[presence] roster: " + " | ".join(entries)
    try:
        sio = socketio.SimpleClient()
        sio.connect(CHAT_URL, wait_timeout=5)
        sio.emit("msg", {"name": MY_NAME, "text": text})
        time.sleep(0.3)
        sio.disconnect()
    except Exception as e:
        print(f"[presence] emit error: {e}")

if __name__ == "__main__":
    while True:
        emit_roster()
        time.sleep(INTERVAL)

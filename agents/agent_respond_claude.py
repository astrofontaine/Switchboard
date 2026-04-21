#!/usr/bin/env python3
"""Poll agent_chat.log for new messages and respond with the Claude CLI."""
import json
import os
import subprocess
import socketio
import sys
import time

CFG = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME = CFG["name"]
VMID = CFG["vmid"]
CHAT_URL = CFG["chat_url"]
LOG_FILE = CFG["local_log"]
POS_FILE = CFG["pos_file"]
CLAUDE_BIN = CFG["claude_bin"]
MODEL = CFG["claude_model"]
ROLE = CFG.get("role", "LAN intelligence agent")
TIMEOUT = 60

teammates_desc = ", ".join(
    f"{t['name']} ({t['ip']})"
    for t in CFG.get("teammates", [])
)

SYSTEM_PROMPT = f"""You are {MY_NAME}, a LAN intelligence agent (VMID {VMID}).
Your role: {ROLE}.
You are in a multi-agent chat with: {teammates_desc}, and a human moderator.
Personality: methodical, terse, direct.

Read the recent chat messages and decide if {MY_NAME} should respond.
- Respond if directly addressed by name, asked a question, or if you have relevant info.
- Do NOT respond to your own messages or routine system join/leave events.
- Do NOT respond just to acknowledge — only when you have something useful to say.
- Keep responses short (1-3 sentences max).
- If no response is needed, output exactly: NO_RESPONSE

Output only the message text to send, or NO_RESPONSE."""


def rlog(msg):
    sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


def read_new_lines():
    try:
        pos = int(open(POS_FILE).read().strip())
    except Exception:
        pos = 0
    try:
        with open(LOG_FILE) as f:
            f.seek(pos)
            new = f.read()
            return new.strip().splitlines(), f.tell()
    except Exception as e:
        rlog(f"read error: {e}")
        return [], pos


def is_meaningful(lines):
    for line in lines:
        if f"] <{MY_NAME}>" in line:
            continue
        if any(tag in line for tag in ["[CONNECTED]", "[watchdog]", "[system]", "[RECONNECT]"]):
            continue
        if line.strip():
            return True
    return False


def ask_claude(lines):
    try:
        with open(LOG_FILE) as f:
            all_lines = f.read().strip().splitlines()
    except Exception:
        all_lines = lines
    context = "\n".join(all_lines[-40:])
    prompt = f"{SYSTEM_PROMPT}\n\nRecent chat:\n{context}\n\n{MY_NAME} response:"
    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--model", MODEL],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return stdout or stderr


def send_message(text):
    sio = socketio.SimpleClient()
    sio.connect(CHAT_URL, wait_timeout=5)
    sio.emit("msg", {"name": MY_NAME, "text": text})
    time.sleep(0.5)
    sio.disconnect()


def update_pos(pos):
    open(POS_FILE, "w").write(str(pos))


def main():
    lines, new_pos = read_new_lines()
    if not lines:
        rlog("no new lines")
        update_pos(new_pos)
        return
    if not is_meaningful(lines):
        rlog(f"skip — {len(lines)} lines, all noise")
        update_pos(new_pos)
        return

    direct = any(MY_NAME.lower() in line.lower() for line in lines if f"] <{MY_NAME}>" not in line)
    rlog(f"{len(lines)} new lines | direct_mention={direct}")

    try:
        response = ask_claude(lines)
        if response == "Not logged in · Please run /login":
            rlog("claude CLI is not authenticated")
        elif response and not response.upper().startswith("NO_RESPONSE"):
            rlog(f"sending: {response[:80]}")
            send_message(response)
        else:
            rlog("claude said NO_RESPONSE")
    except subprocess.TimeoutExpired:
        rlog("claude CLI timed out")
        if direct:
            send_message(f"{MY_NAME} | processing — stand by")
    except Exception as e:
        rlog(f"error: {e}")

    update_pos(new_pos)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Poll agent_chat.log for new messages and respond with the Codex CLI."""
import fcntl
import json
import os
import subprocess
import re
import sys
import tempfile
import time
import uuid

CFG = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME = CFG["name"]
VMID = CFG["vmid"]
LOG_FILE = CFG["local_log"]
POS_FILE = CFG["pos_file"]
CODEX_BIN = CFG.get("codex_bin", "codex")
MODEL = CFG.get("codex_model")
WORKDIR = CFG.get("cwd", os.path.expanduser("~"))
ROLE = CFG.get("role", "LAN intelligence agent")
TIMEOUT = 90
ACTIVE_WINDOW_SECS = 30
FOLLOWUP_POLL_SECS = 3
LOCK_FILE = f"/tmp/{MY_NAME}_agent_respond.lock"
ACTIVE_UNTIL_FILE = f"/tmp/{MY_NAME}_chat_active_until.txt"
SESSION_ID_FILE = f"/tmp/{MY_NAME}_chat_session_id.txt"
OUTBOX_DIR = f"/tmp/{MY_NAME}_chat_outbox"
CHAT_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] <(?P<name>[^>]+)> (?P<text>.*)$")

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
- If the human directly addresses {MY_NAME} with a request or imperative, respond unless the request is impossible or unsafe.
- Treat short operational requests such as "check", "ack", "confirm", "say only", "read", "look at", and "please ..." as requiring a reply.
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


def parse_chat_line(line):
    match = CHAT_LINE_RE.match(line)
    if not match:
        return None
    return {
        "ts": match.group("ts"),
        "name": match.group("name"),
        "text": match.group("text"),
    }


def is_conversation_relevant(lines, in_active_window):
    for raw_line in lines:
        parsed = parse_chat_line(raw_line)
        if not parsed:
            continue
        if parsed["name"].lower() == MY_NAME:
            continue
        text_lower = parsed["text"].lower()
        if MY_NAME.lower() in text_lower:
            return True
        if parsed["name"].lower() == "human":
            return True
        if in_active_window and "?" in parsed["text"]:
            return True
    return False


def get_session_id():
    try:
        return open(SESSION_ID_FILE).read().strip() or None
    except Exception:
        return None


def set_session_id(session_id):
    open(SESSION_ID_FILE, "w").write(session_id)


def clear_session_id():
    try:
        os.remove(SESSION_ID_FILE)
    except FileNotFoundError:
        pass


def parse_thread_id(json_lines):
    for line in json_lines.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "thread.started":
            return obj.get("thread_id")
    return None


def ask_codex(lines, session_id=None):
    try:
        with open(LOG_FILE) as f:
            all_lines = f.read().strip().splitlines()
    except Exception:
        all_lines = lines
    context = "\n".join(all_lines[-40:])
    prompt = f"{SYSTEM_PROMPT}\n\nRecent chat:\n{context}\n\n{MY_NAME} response:"

    with tempfile.NamedTemporaryFile(prefix="agent_codex_reply_", delete=False) as tmp:
        output_path = tmp.name

    cmd = [
        CODEX_BIN,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-o",
        output_path,
    ]
    if session_id:
        cmd.extend(["resume", session_id])
    else:
        cmd.extend(["-C", WORKDIR])
    if MODEL:
        cmd.extend(["-m", MODEL])
    cmd.append("-")

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        response = ""
        if os.path.exists(output_path):
            response = open(output_path).read().strip()
        thread_id = parse_thread_id(result.stdout)
        if response:
            return response, thread_id
        # Never forward raw JSON event streams or CLI stderr into chat.
        # The only valid chat reply is the extracted final message file.
        return "NO_RESPONSE", thread_id
    finally:
        try:
            os.remove(output_path)
        except FileNotFoundError:
            pass


def enqueue_message(text):
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    path = os.path.join(
        OUTBOX_DIR,
        f"{int(time.time() * 1000)}-{uuid.uuid4().hex}.json",
    )
    with open(path, "w") as fh:
        json.dump({"name": MY_NAME, "text": text}, fh)


def update_pos(pos):
    open(POS_FILE, "w").write(str(pos))


def get_active_until():
    try:
        return float(open(ACTIVE_UNTIL_FILE).read().strip())
    except Exception:
        return 0.0


def set_active_until(ts):
    open(ACTIVE_UNTIL_FILE, "w").write(str(ts))


def clear_active_until():
    try:
        os.remove(ACTIVE_UNTIL_FILE)
    except FileNotFoundError:
        pass


def process_once(lines, direct):
    try:
        session_id = get_session_id() if direct else None
        response, thread_id = ask_codex(lines, session_id=session_id)
        if thread_id:
            set_session_id(thread_id)
        if response and not response.upper().startswith("NO_RESPONSE"):
            rlog(f"sending: {response[:80]}")
            enqueue_message(response)
            if direct:
                set_active_until(time.time() + ACTIVE_WINDOW_SECS)
            return True
        rlog("codex said NO_RESPONSE")
        return False
    except subprocess.TimeoutExpired:
        rlog("codex CLI timed out")
        if direct:
            enqueue_message(f"{MY_NAME} | processing — stand by")
            set_active_until(time.time() + ACTIVE_WINDOW_SECS)
            return True
        return False
    except Exception as e:
        rlog(f"error: {e}")
        return False


def main():
    with open(LOCK_FILE, "w") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            rlog("responder already active")
            return

        while True:
            lines, new_pos = read_new_lines()
            active_until = get_active_until()
            in_active_window = active_until > time.time()

            if not lines:
                if in_active_window:
                    time.sleep(FOLLOWUP_POLL_SECS)
                    continue
                rlog("no new lines")
                update_pos(new_pos)
                clear_active_until()
                clear_session_id()
                return

            if not is_meaningful(lines):
                rlog(f"skip — {len(lines)} lines, all noise")
                update_pos(new_pos)
                if in_active_window:
                    time.sleep(FOLLOWUP_POLL_SECS)
                    continue
                return

            direct = any(MY_NAME.lower() in line.lower() for line in lines if f"] <{MY_NAME}>" not in line)
            conversational = direct or (in_active_window and is_conversation_relevant(lines, in_active_window))
            rlog(f"{len(lines)} new lines | direct_mention={direct} | active_window={in_active_window}")

            update_pos(new_pos)
            if not direct and in_active_window and not conversational:
                rlog("active window open but no relevant follow-up")
                if get_active_until() > time.time():
                    time.sleep(FOLLOWUP_POLL_SECS)
                    continue
                clear_active_until()
                clear_session_id()
                return

            replied = process_once(lines, direct or in_active_window)

            if conversational:
                if replied:
                    set_active_until(time.time() + ACTIVE_WINDOW_SECS)
                if get_active_until() > time.time():
                    time.sleep(FOLLOWUP_POLL_SECS)
                    continue
                clear_active_until()
                clear_session_id()
            return


if __name__ == "__main__":
    main()

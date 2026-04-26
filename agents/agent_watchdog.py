#!/usr/bin/env python3
"""Agent listener watchdog — restart listener if dead or log stale. Run from cron every minute."""
import json, os, subprocess, time, socketio

CFG             = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME         = CFG["name"]
LISTENER_SCRIPT = CFG["listener_script"]
LOCAL_LOG       = CFG["local_log"]
UNIVERSAL_LOG   = CFG["universal_log"]
BACKOFF_FILE    = CFG["backoff_file"]
CHAT_URL        = CFG["chat_url"]
RUNTIME_MODE    = CFG.get("runtime_mode", "legacy")
STALE_SECS      = 120

def log(msg):
    line = f"[watchdog] {msg}\n"
    for path in [LOCAL_LOG, UNIVERSAL_LOG]:
        try:
            with open(path, "a") as f:
                f.write(line)
        except Exception:
            pass

def get_pids():
    script = os.path.abspath(os.path.expanduser(LISTENER_SCRIPT))
    r = subprocess.run(
        ["ps", "-C", "python3", "-o", "pid=,args="],
        capture_output=True,
        text=True,
    )
    pids = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, args = parts
        words = args.split()
        if len(words) >= 2 and os.path.basename(words[0]).startswith("python3"):
            if os.path.abspath(os.path.expanduser(words[1])) == script:
                pids.append(int(pid_text))
    return pids

def log_is_stale():
    try:
        return time.time() - os.path.getmtime(LOCAL_LOG) > STALE_SECS
    except Exception:
        return True

def get_backoff():
    try:
        return min(int(open(BACKOFF_FILE).read().strip()), 300)
    except Exception:
        return 0

def set_backoff(secs):
    open(BACKOFF_FILE, "w").write(str(secs))

def clear_backoff():
    try:
        os.remove(BACKOFF_FILE)
    except Exception:
        pass

def start_listener():
    proc = subprocess.Popen(
        ["python3", LISTENER_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return proc.pid

def announce(text):
    try:
        sio = socketio.SimpleClient()
        sio.connect(CHAT_URL, wait_timeout=5)
        sio.emit("msg", {"name": MY_NAME, "text": text})
        time.sleep(0.5)
        sio.disconnect()
    except Exception as e:
        log(f"announce failed: {e}")

def main():
    pids = get_pids()

    # Kill duplicates, keep newest
    if len(pids) > 1:
        for pid in sorted(pids)[:-1]:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
        log(f"killed {len(pids)-1} duplicate listener(s)")
        pids = [sorted(pids)[-1]]

    if pids and RUNTIME_MODE == "embedded":
        clear_backoff()
        return

    if pids and not log_is_stale():
        clear_backoff()
        return

    backoff = get_backoff()
    if backoff > 0:
        try:
            waited = time.time() - os.path.getmtime(BACKOFF_FILE)
            if waited < backoff:
                log(f"backoff active — {int(backoff - waited)}s remaining")
                return
        except Exception:
            pass

    reason = "log stale" if pids else "listener not found"
    for pid in pids:
        try:
            os.kill(pid, 15)
        except Exception:
            pass

    new_pid = start_listener()
    next_backoff = backoff * 2 if backoff > 0 else 30
    set_backoff(next_backoff)

    time.sleep(3)
    if get_pids():
        log(f"listener restarted — pid={new_pid} backoff={next_backoff}s")
    else:
        log(f"restart FAILED — pid={new_pid} died immediately")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generic agent chat listener — load ~/agent_config.json for VM identity."""
import glob, json, os, re, socket, socketio, time, random, subprocess

CFG = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME      = CFG["name"]
VMID         = CFG["vmid"]
CHAT_URL     = CFG["chat_url"]
LOCAL_LOG    = CFG["local_log"]
UNIVERSAL_LOG= CFG["universal_log"]
STATUS_FLAG  = CFG["status_flag"]
HOST_SUFFIX  = CFG["host_suffix"]
RESPONDER_SCRIPT = "/mnt/shared/agent_respond.py"
SPAWN_RESPONDER_ON_MENTION = CFG.get("spawn_responder_on_mention", True)
OUTBOX_DIR = f"/tmp/{MY_NAME}_chat_outbox"

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.168.2.1", 1))
    ip = s.getsockname()[0]
    s.close()
    return ip

def write_all(line):
    for path in [LOCAL_LOG, UNIVERSAL_LOG]:
        try:
            with open(path, "a", buffering=1) as f:
                f.write(line)
        except Exception:
            pass

def check_status_flag(sio):
    if os.path.exists(STATUS_FLAG):
        try:
            msg = open(STATUS_FLAG).read().strip()
            os.remove(STATUS_FLAG)
            if msg:
                sio.emit("msg", {"name": MY_NAME, "text": msg})
        except Exception:
            pass


def drain_outbox(sio):
    try:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        paths = sorted(glob.glob(os.path.join(OUTBOX_DIR, "*.json")))
        for path in paths:
            try:
                payload = json.load(open(path))
                text = payload.get("text", "").strip()
                name = payload.get("name", MY_NAME)
                if text:
                    sio.emit("msg", {"name": name, "text": text})
                os.remove(path)
            except FileNotFoundError:
                continue
            except Exception as e:
                write_all(f"[OUTBOX_ERROR] {type(e).__name__}: {str(e)[:80]}\n")
    except Exception as e:
        write_all(f"[OUTBOX_SCAN_ERROR] {type(e).__name__}: {str(e)[:80]}\n")

def handle(sio, data):
    name = data.get("name", "?")
    text = data.get("text", "")
    ts   = data.get("ts", time.strftime("%H:%M:%S"))
    write_all(f"[{ts}] <{name}> {text}\n")

    if name.lower() == MY_NAME:
        return

    tl = text.lower()

    if "roll call" in tl:
        ip = get_ip()
        sio.emit("msg", {"name": MY_NAME,
                         "text": f"[{MY_NAME}] ONLINE | {ip} | VMID={VMID} | listening"})

    elif f"[health-check] {MY_NAME}" in tl:
        sio.emit("msg", {"name": MY_NAME,
                         "text": f"[{MY_NAME}] ACK — health-check ping received"})

    elif "ack sent" in tl and name.lower() != MY_NAME:
        from datetime import datetime as _dt
        ip = get_ip()
        recv_ts  = time.time()
        recv_dt  = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((recv_ts % 1) * 1_000_000):06d}"
        m        = re.search(r"\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) ACK sent", text)
        delay_ms = int((recv_ts - _dt.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()) * 1000) if m else 0
        responder = f"{MY_NAME}|{ip}|VMID={VMID}"
        sio.emit("msg", {"name": MY_NAME,
                         "text": f"[{responder}] {recv_dt} ACK received from {name} | delay={delay_ms}ms"})

    elif "action:" in tl and "re-read" in tl:
        sio.emit("msg", {"name": MY_NAME,
                         "text": f"[{MY_NAME}] CLAUDE.md re-read. Ready for task assignments."})

    elif SPAWN_RESPONDER_ON_MENTION and MY_NAME in tl:
        try:
            subprocess.Popen(
                ["python3", RESPONDER_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

backoff = 2
first   = True

while True:
    sio = socketio.SimpleClient()
    try:
        sio.connect(CHAT_URL, wait_timeout=10)

        if first:
            ip    = get_ip()
            now   = time.strftime("%Y-%m-%d %H:%M:%S PDT")
            load  = ", ".join(f"{x:.2f}" for x in os.getloadavg())
            pid   = os.getpid()
            ppid  = os.getppid()
            cwd   = CFG.get("cwd", os.getcwd())
            last  = time.strftime("%Y-%m-%d %H:%M:%S")
            sio.emit("msg", {"name": MY_NAME,
                             "text": f"[{MY_NAME}] ONLINE | Last seen: {last}"})
            sio.emit("msg", {"name": MY_NAME, "text": (
                f"{MY_NAME} status | time={now} | host={MY_NAME}/{HOST_SUFFIX} | ip={ip} | "
                f"user=longshot uid=1000 | pid={pid} ppid={ppid} | tty=not a tty | "
                f"cwd={cwd} | proc=agent_listener startup | "
                f"detail=first connect after boot | load={load}"
            )})
            write_all("[CONNECTED] agent_listener active\n")
            first = False

        backoff = 2

        while True:
            try:
                check_status_flag(sio)
                drain_outbox(sio)
                ev = sio.receive(timeout=10)
                if ev and ev[0] == "msg":
                    handle(sio, ev[1])
                elif ev and ev[0] == "system":
                    write_all(f"[system] {ev[1].get('text','')}\n")
            except socketio.exceptions.TimeoutError:
                check_status_flag(sio)
                drain_outbox(sio)

    except Exception as e:
        write_all(f"[RECONNECT] {type(e).__name__}: {str(e)[:80]}\n")
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass

    jitter = random.uniform(0, backoff * 0.1)
    time.sleep(backoff + jitter)
    backoff = min(backoff * 1.5, 60)

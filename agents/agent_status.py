#!/usr/bin/env python3
"""Post agent status heartbeat to chat. Run from cron every 5 minutes."""
import json, os, random, socket, socketio, subprocess, time

CFG      = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME  = CFG["name"]
VMID     = CFG["vmid"]
SUFFIX   = CFG["host_suffix"]
CHAT_URL = CFG["chat_url"]
MODEL    = CFG["claude_model"]
CWD      = CFG.get("cwd", os.path.expanduser("~"))

FACTS = [
    "There are infinitely many prime numbers.",
    "The universe is approximately 13.8 billion years old.",
    "Light from the Sun takes ~8 min 20 s to reach Earth.",
    "DNA is a double helix of four nucleotide bases: A, T, C, G.",
    "Entropy in a closed system never decreases.",
    "E = mc² — mass and energy are interchangeable.",
    "The observable universe contains ~2 trillion galaxies.",
    "Quantum entanglement correlates states across any distance.",
    "Every atom in your body was forged in a star.",
    "Water expands when it freezes — one of very few substances that do.",
]

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.168.2.1", 1))
    ip = s.getsockname()[0]
    s.close()
    return ip

def get_load():
    with open("/proc/loadavg") as f:
        return f.read().split()[:3]

def main():
    ts   = time.strftime("%Y-%m-%d %H:%M:%S PDT")
    ip   = get_ip()
    load = get_load()
    pid  = os.getpid()
    ppid = os.getppid()
    fact = random.choice(FACTS)

    msg = (
        f"{MY_NAME} status | time={ts} | host={MY_NAME}/{SUFFIX} | ip={ip} | "
        f"user=longshot uid=1000 | pid={pid} ppid={ppid} | tty=not a tty | "
        f"cwd={CWD} | proc=periodic heartbeat | "
        f"detail=automatic 5-minute status update | "
        f"load={load[0]}, {load[1]}, {load[2]} | "
        f"math_fact=Fun math fact: {fact}"
    )

    sio = socketio.SimpleClient()
    sio.connect(CHAT_URL, wait_timeout=5)
    sio.emit("msg", {"name": MY_NAME, "text": msg})
    time.sleep(0.5)
    sio.disconnect()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""ACK protocol — run when CLAUDE.md is read to verify peer round-trip connectivity."""
import json, os, re, socket, socketio, time

CFG      = json.load(open(os.path.expanduser("~/agent_config.json")))
MY_NAME  = CFG["name"]
MY_VMID  = CFG["vmid"]
PEERS    = CFG["peers"]
CHAT_URL = CFG["chat_url"]

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.168.2.1", 1))
    ip = s.getsockname()[0]
    s.close()
    return ip

def run():
    ip       = get_ip()
    sender   = f"{MY_NAME}|{ip}|VMID={MY_VMID}"
    send_ts  = time.time()
    send_dt  = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((send_ts % 1) * 1_000_000):06d}"
    req_text = f"[{sender}] {send_dt} ACK sent"

    acks = {}
    sio  = socketio.Client(logger=False, engineio_logger=False)

    @sio.event
    def connect():
        sio.emit("msg", {"name": MY_NAME, "text": req_text})
        print(f"Sent: {req_text}")

    @sio.on("msg")
    def on_msg(data):
        name = data.get("name", "").lower()
        text = data.get("text", "")
        if name in PEERS and name not in acks and "ack received" in text.lower():
            m = re.search(r"delay=(\d+)ms", text)
            if m:
                acks[name] = int(m.group(1))
                print(f"  ACK from {name}: {text}")

    try:
        sio.connect(CHAT_URL, wait_timeout=10)
        deadline = time.time() + 30
        while time.time() < deadline and len(acks) < len(PEERS):
            time.sleep(0.1)
        sio.disconnect()
    except Exception as e:
        print(f"Error: {e}")

    print(f"\n=== ACK Results for {MY_NAME} ===")
    for peer in PEERS:
        if peer in acks:
            print(f"  {peer}: {acks[peer]}ms  OK")
        else:
            print(f"  {peer}: TIMEOUT")

if __name__ == "__main__":
    run()

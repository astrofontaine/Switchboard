"""LAN chatroom with channel-aware history replay."""
import hashlib
import json
import os
import re
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template_string, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "lanparty"
sio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

USERS = {}
CHANNELS = ["main", "debug", "ops", "agents"]
DEFAULT_CHANNEL = "main"
AGENT_NAMES = {"keystone", "vega", "necto"}
HISTORY_SCAN_LINES = 8000
SHARED_LOG = "/home/longshot/shared/UNIVERSAL_CHAT.log"
LEGACY_REPLAY_LOG = "/home/longshot/shared/CHAT_REPLAY.log"
REPLAY_JSONL = "/home/longshot/shared/CHAT_REPLAY.jsonl"

_SEEN = {}
_SEEN_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
HISTORY = defaultdict(list)

CHAT_LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] <(?P<name>[^>]+)> (?P<text>.*)$")
SYSTEM_LINE_RE = re.compile(r"^\[system\] (?P<text>.*)$")


def _msg_id(name, text, ts, channel):
    return hashlib.md5(f"{channel}:{name}:{text}:{ts}".encode()).hexdigest()[:16]


def _purge_seen():
    now = time.time()
    with _SEEN_LOCK:
        expired = [k for k, v in _SEEN.items() if v < now]
        for k in expired:
            del _SEEN[k]


def _tail_lines(path, limit):
    if not os.path.exists(path):
        return []
    with open(path, errors="ignore") as fh:
        return list(deque(fh, maxlen=limit))


def _normalize_channel(channel):
    if channel in CHANNELS:
        return channel
    return DEFAULT_CHANNEL


def _classify_channel(name, text, kind, requested_channel=None):
    if requested_channel in CHANNELS:
        return requested_channel
    if kind == "system":
        return "debug"

    low = text.lower()
    debug_markers = [
        "status |",
        "math_fact=",
        "[watchdog]",
        "[connected]",
        "[reconnect]",
        "you've hit your limit",
        "resets 4pm",
        "online | last seen",
        "proc=periodic heartbeat",
        "proc=agent_listener startup",
    ]
    if any(marker in low for marker in debug_markers):
        return "debug"
    if name.lower() in {"system"}:
        return "debug"
    if name.lower() in AGENT_NAMES:
        return "agents"
    return "main"


def _parse_text_log_line(line):
    line = line.rstrip("\n")
    chat_match = CHAT_LINE_RE.match(line)
    if chat_match:
        ts = chat_match.group("ts")
        name = chat_match.group("name")
        text = chat_match.group("text")
        channel = _classify_channel(name, text, "msg")
        return {
            "kind": "msg",
            "channel": channel,
            "id": _msg_id(name, text, ts, channel),
            "name": name,
            "text": text,
            "ts": ts,
        }
    system_match = SYSTEM_LINE_RE.match(line)
    if system_match:
        text = system_match.group("text")
        channel = _classify_channel("system", text, "system")
        return {
            "kind": "system",
            "channel": channel,
            "id": _msg_id("system", text, "system", channel),
            "text": text,
            "ts": "",
        }
    return None


def _append_jsonl_event(event):
    os.makedirs(os.path.dirname(REPLAY_JSONL), exist_ok=True)
    with open(REPLAY_JSONL, "a", buffering=1) as fh:
        fh.write(json.dumps(event) + "\n")


def _record_event(event, persist=False):
    channel = _normalize_channel(event.get("channel"))
    event["channel"] = channel
    with _STATE_LOCK:
        HISTORY[channel].append(event)
    if persist:
        _append_jsonl_event(event)


def _load_history():
    if os.path.exists(REPLAY_JSONL) and os.path.getsize(REPLAY_JSONL) > 0:
        with open(REPLAY_JSONL, errors="ignore") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("channel") not in CHANNELS:
                    event["channel"] = _classify_channel(
                        event.get("name", "system"),
                        event.get("text", ""),
                        event.get("kind", "msg"),
                    )
                _record_event(event, persist=False)
        return

    seed_path = LEGACY_REPLAY_LOG if os.path.exists(LEGACY_REPLAY_LOG) and os.path.getsize(LEGACY_REPLAY_LOG) > 0 else SHARED_LOG
    seen = set()
    for raw_line in _tail_lines(seed_path, HISTORY_SCAN_LINES):
        event = _parse_text_log_line(raw_line)
        if not event:
            continue
        event_key = (event["kind"], event.get("name", ""), event["text"], event["ts"], event["channel"])
        if event_key in seen:
            continue
        seen.add(event_key)
        _record_event(event, persist=False)

    for channel in CHANNELS:
        for event in HISTORY[channel]:
            _append_jsonl_event(event)


_load_history()

PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><title>LAN Chat</title>
<style>
  :root{
    --bg:#101317;--panel:#141920;--line:#273243;--text:#d7e1ee;--muted:#7f91a7;
    --accent:#6fc3ff;--accent-2:#9bf0bf;--warn:#f8d28f;
  }
  *{box-sizing:border-box}
  body{background:linear-gradient(180deg,#0c0f13,#111723);color:var(--text);font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;display:flex;flex-direction:column;height:100vh}
  #who{padding:8px 12px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line);background:#0f141c}
  #channels{display:flex;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);background:#121822;align-items:center}
  .chan{background:#1a2230;color:var(--muted);border:1px solid #33445f;padding:6px 12px;cursor:pointer;border-radius:999px;font-size:13px}
  .chan.active{color:#0c1420;background:var(--accent);border-color:var(--accent)}
  #channel-note{margin-left:auto;font-size:12px;color:var(--muted)}
  #log{flex:1;overflow-y:auto;padding:12px 14px;border-bottom:1px solid var(--line)}
  .msg{margin:4px 0;line-height:1.45}
  .ts{color:#60758d}
  .name{color:var(--accent)}
  .badge{display:inline-block;margin-left:8px;padding:1px 6px;border:1px solid #3b4e68;border-radius:999px;color:var(--muted);font-size:11px}
  .system{color:var(--warn)}
  #bar{display:flex;padding:10px 12px;gap:8px;align-items:flex-end;background:#0f141c}
  #name{width:120px;background:#1a2230;color:var(--text);border:1px solid #33445f;padding:10px 10px;font-family:inherit;font-size:16px;border-radius:8px}
  #msg{flex:1;background:#1a2230;color:var(--text);border:1px solid #33445f;padding:12px 12px;font-family:inherit;font-size:18px;line-height:1.35;border-radius:8px;resize:vertical;min-height:66px;max-height:180px}
  button{background:#1e6db3;color:white;border:1px solid #338de0;padding:10px 16px;cursor:pointer;border-radius:8px;font-size:15px}
</style></head><body>
<div id="who">connected: -</div>
<div id="channels"></div>
<div id="log"></div>
<div id="bar">
  <input id="name" placeholder="your name">
  <textarea id="msg" rows="2" placeholder="message..."></textarea>
  <button onclick="send()">send</button>
</div>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
const CHANNELS=['main','debug','ops','agents'];
let activeChannel='main';
const sock=io();
const log=document.getElementById('log');
const channelsEl=document.getElementById('channels');
const channelCache=new Map();
const knownIds=new Map();

function esc(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
function channelLabel(name){
  if(name==='main') return 'main';
  if(name==='debug') return 'debug';
  if(name==='ops') return 'ops';
  if(name==='agents') return 'agents room';
  return name;
}
function renderChannels(){
  channelsEl.innerHTML='';
  for(const name of CHANNELS){
    const b=document.createElement('button');
    b.className='chan'+(name===activeChannel?' active':'');
    b.textContent=channelLabel(name);
    b.onclick=()=>switchChannel(name);
    channelsEl.appendChild(b);
  }
  const note=document.createElement('div');
  note.id='channel-note';
  note.textContent='switching channels loads full saved history';
  channelsEl.appendChild(note);
}
function renderEvent(d){
  const div=document.createElement('div');
  div.className='msg';
  if(d.kind==='system'){
    div.innerHTML='<span class="system">*** '+esc(d.text)+' ***</span>';
  } else {
    div.innerHTML='<span class="ts">['+esc(d.ts)+']</span> <span class="name">'+esc(d.name)+'</span>: '+esc(d.text)+'<span class="badge">'+esc(d.channel)+'</span>';
  }
  log.appendChild(div);
}
function paintChannel(name){
  log.innerHTML='';
  const items=channelCache.get(name)||[];
  for(const item of items) renderEvent(item);
  log.scrollTop=log.scrollHeight;
}
async function loadHistory(name){
  const resp=await fetch('/history?channel='+encodeURIComponent(name));
  const items=await resp.json();
  channelCache.set(name, items);
  knownIds.set(name, new Set(items.map(x=>x.id).filter(Boolean)));
  if(name===activeChannel) paintChannel(name);
}
async function switchChannel(name){
  activeChannel=name;
  renderChannels();
  await loadHistory(name);
}
function maybeStoreEvent(d){
  const channel=d.channel||'main';
  if(!channelCache.has(channel)) channelCache.set(channel, []);
  if(!knownIds.has(channel)) knownIds.set(channel, new Set());
  const ids=knownIds.get(channel);
  if(d.id && ids.has(d.id)) return false;
  if(d.id) ids.add(d.id);
  channelCache.get(channel).push(d);
  return true;
}
sock.on('msg',d=>{const ev={...d, kind:'msg', channel:d.channel||'main'}; if(maybeStoreEvent(ev) && ev.channel===activeChannel) paintChannel(activeChannel);});
sock.on('system',d=>{const ev={...d, kind:'system', channel:d.channel||'debug'}; if(maybeStoreEvent(ev) && ev.channel===activeChannel) paintChannel(activeChannel);});
sock.on('who',d=>{document.getElementById('who').textContent='connected: '+d.join(', ');});
function send(){
  const name=document.getElementById('name').value.trim()||'anon';
  const text=document.getElementById('msg').value.trim();
  if(!text) return;
  sock.emit('msg',{name,text,channel:activeChannel});
  document.getElementById('msg').value='';
  document.getElementById('msg').focus();
}
document.getElementById('msg').addEventListener('keydown',e=>{
  if(e.key==='Enter' && !e.shiftKey){e.preventDefault();send();}
});
renderChannels();
switchChannel(activeChannel);
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/history")
def history():
    channel = _normalize_channel(request.args.get("channel", DEFAULT_CHANNEL))
    with _STATE_LOCK:
        items = list(HISTORY[channel])
    return jsonify(items)


@app.route("/channels")
def channels():
    return jsonify(CHANNELS)


@sio.on("connect")
def on_connect():
    USERS[request.sid] = "?"
    system_text = "someone joined"
    channel = _classify_channel("system", system_text, "system")
    system_event = {
        "kind": "system",
        "channel": channel,
        "id": _msg_id("system", system_text, str(time.time()), channel),
        "text": system_text,
        "ts": "",
    }
    _record_event(system_event, persist=True)
    emit("system", {"id": system_event["id"], "channel": channel, "text": system_event["text"]}, broadcast=True)
    sio.emit("who", [v for v in USERS.values() if v != "?"])


@sio.on("disconnect")
def on_disconnect():
    name = USERS.pop(request.sid, "?")
    text = f"{name} left"
    channel = _classify_channel("system", text, "system")
    system_event = {
        "kind": "system",
        "channel": channel,
        "id": _msg_id("system", text, str(time.time()), channel),
        "text": text,
        "ts": "",
    }
    _record_event(system_event, persist=True)
    emit("system", {"id": system_event["id"], "channel": channel, "text": system_event["text"]}, broadcast=True)
    sio.emit("who", [v for v in USERS.values() if v != "?"])


@sio.on("msg")
def on_msg(data):
    from datetime import datetime

    name = data.get("name", "anon")[:24]
    text = data.get("text", "")[:1000]
    requested_channel = data.get("channel")
    channel = _classify_channel(name, text, "msg", requested_channel=requested_channel)
    ts = datetime.now().strftime("%H:%M:%S")
    mid = _msg_id(name, text, ts, channel)
    _purge_seen()
    with _SEEN_LOCK:
        if mid in _SEEN:
            return
        _SEEN[mid] = time.time() + 60
    USERS[request.sid] = name
    sio.emit("who", [v for v in USERS.values() if v != "?"])
    event = {
        "kind": "msg",
        "channel": channel,
        "id": mid,
        "name": name,
        "text": text,
        "ts": ts,
    }
    _record_event(event, persist=True)
    emit("msg", {"id": mid, "channel": channel, "name": name, "text": text, "ts": ts}, broadcast=True)


if __name__ == "__main__":
    sio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)

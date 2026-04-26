#!/usr/bin/env python3
"""Persistent event-driven embedded runtime for Switchboard agents."""
from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import socket
import socketio
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from agent_embedded_backend import ClaudeAdapter
from agent_embedded_commands import CommandRequest, CommandResult, CommandThread, is_safe_read_only
from agent_embedded_protocol import (
    NormalizedMessage,
    Trigger,
    normalize_message,
    parse_state_transition,
    parse_trigger,
)
from agent_embedded_state import AppliedMessageLRU, OutboundTracker, RingBuffer, TaskRegistry


REPLAY_LIMIT = 500
HEARTBEAT_SECS = 300


def get_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.168.2.1", 1))
    ip = s.getsockname()[0]
    s.close()
    return ip


class EmbeddedRuntime:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.chat_url = cfg["chat_url"].rstrip("/")
        self.host_suffix = cfg.get("host_suffix", "")
        self.vm_id = cfg.get("vmid", "?")
        self.ring = RingBuffer(REPLAY_LIMIT)
        self.applied = AppliedMessageLRU()
        self.applied_store_path = os.path.expanduser(
            cfg.get(
                "applied_store_path",
                f"~/.switchboard_runtime/{self.name}_applied_messages.json",
            )
        )
        self.applied_store_error = None
        self.bootstrap_replay_observed = not os.path.exists(self.applied_store_path)
        self.applied_store_lock = threading.Lock()
        self._load_applied_store()
        self.tasks = TaskRegistry(self.name)
        self.outbound = OutboundTracker()
        self.events: queue.Queue[Trigger] = queue.Queue()
        self.live_backlog: queue.Queue[NormalizedMessage] = queue.Queue()
        self.replay_active = False
        self.socket_state = "disconnected"
        self.last_reconnect = None
        self.last_llm_ms = 0
        self.last_llm_error = None
        self.replay_applied = 0
        self.backend = ClaudeAdapter(
            cfg.get("claude_bin", "claude"),
            cfg.get("claude_model", ""),
        )
        self.command_thread = CommandThread(self._command_finished)
        self.sio = socketio.SimpleClient()
        self.worker = threading.Thread(target=self._worker_loop, name="worker", daemon=True)

    def run(self) -> int:
        self.worker.start()
        self._schedule_heartbeat()
        backoff = 2
        while True:
            try:
                self.sio.connect(self.chat_url, wait_timeout=10)
                self.socket_state = "connected"
                self.last_reconnect = time.time()
                self._replay()
                self._send_status("agent_listener startup")
                if self.applied_store_error:
                    self._send_debug(f"[{self.name}] applied-store warning: {self.applied_store_error}")
                backoff = 2
                while True:
                    try:
                        ev = self.sio.receive(timeout=10)
                    except socketio.exceptions.TimeoutError:
                        continue
                    if ev and ev[0] == "msg":
                        self._handle_live(normalize_message(ev[1]))
            except Exception as exc:
                self.socket_state = f"disconnected:{type(exc).__name__}"
                time.sleep(backoff)
                backoff = min(int(backoff * 1.5), 60)
            finally:
                try:
                    self.sio.disconnect()
                except Exception:
                    pass

    def _replay(self) -> None:
        self.replay_active = True
        bootstrap_observed = self.bootstrap_replay_observed
        reconstruct_seen = not self.tasks.tasks
        try:
            channel = urllib.parse.quote("agents")
            with urllib.request.urlopen(f"{self.chat_url}/history?channel={channel}", timeout=10) as fh:
                events = json.loads(fh.read().decode())
            for item in events[-REPLAY_LIMIT:]:
                msg = normalize_message(item)
                self._apply_message(
                    msg,
                    replay=True,
                    bootstrap_observed=bootstrap_observed,
                    reconstruct_seen=reconstruct_seen,
                )
            while not self.live_backlog.empty():
                self._apply_message(self.live_backlog.get(), replay=False)
        finally:
            self.bootstrap_replay_observed = False
            self.replay_active = False

    def _handle_live(self, msg: NormalizedMessage) -> None:
        if self.replay_active:
            self.live_backlog.put(msg)
            return
        self._apply_message(msg, replay=False)

    def _apply_message(
        self,
        msg: NormalizedMessage,
        replay: bool,
        bootstrap_observed: bool = False,
        reconstruct_seen: bool = False,
    ) -> None:
        self.ring.append(msg)
        transition = parse_state_transition(msg.text)
        if replay and transition and transition.agent.lower() == self.name.lower():
            if self.applied.seen(msg.msg_id) and not reconstruct_seen:
                return
            ok, err = self.tasks.apply_chat_transition(
                transition.task_id,
                transition.state,
                transition.phase,
                msg.msg_id,
                transition.reason,
            )
            self._mark_observed(msg.msg_id)
            self.replay_applied += int(ok)
            if not ok:
                self._send_debug(f"[{self.name}] replay transition rejected: {err}")
            return
        if self.applied.seen(msg.msg_id):
            return
        if msg.name.lower() == self.name.lower():
            self.outbound.commit_from_echo(msg.text, msg.msg_id)
        if transition and transition.agent.lower() == self.name.lower():
            ok, err = self.tasks.apply_chat_transition(
                transition.task_id,
                transition.state,
                transition.phase,
                msg.msg_id,
                transition.reason,
            )
            self._mark_observed(msg.msg_id)
            self.replay_applied += int(replay and ok)
            if not ok:
                self._send_debug(f"[{self.name}] replay transition rejected: {err}")
            return
        if replay and bootstrap_observed:
            self._mark_observed(msg.msg_id)
            return
        if replay and self._is_legacy_protocol(msg):
            self._mark_observed(msg.msg_id)
            return
        if self._handle_legacy_protocol(msg):
            self._mark_observed(msg.msg_id)
            return
        trigger = parse_trigger(self.name, msg)
        if trigger:
            if not msg.msg_id:
                self._send_debug(f"[{self.name}] debug | stateful trigger missing msg_id; ignored")
                return
            self._mark_observed(msg.msg_id)
            if trigger.kind == "cancel":
                self._handle_cancel(trigger)
            else:
                self.events.put(trigger)
            return
        if replay:
            self._mark_observed(msg.msg_id)

    def _worker_loop(self) -> None:
        while True:
            trigger = self.events.get()
            try:
                self._process_trigger(trigger)
            except Exception as exc:
                self.last_llm_error = f"{type(exc).__name__}: {exc}"
                self._send_debug(f"[{self.name}] response error: {self.last_llm_error}")

    def _process_trigger(self, trigger: Trigger) -> None:
        if trigger.kind == "debug":
            self._send_debug(self._debug_text())
            return
        if trigger.kind == "tasks":
            self._send_agents(self._tasks_text())
            return
        if trigger.kind == "assign":
            task = self.tasks.create(trigger.source.name, trigger.text, trigger.source.msg_id)
            self._send_state(task.task_id, "pending", "planning")
            return
        if trigger.kind == "run":
            self._start_run_task(trigger.source.name, trigger.command or "", trigger.source.msg_id)
            return
        if trigger.kind == "freeform":
            self._handle_freeform(trigger)

    def _start_run_task(self, requested_by: str, command: str, msg_id: str | None) -> None:
        ok, reason = is_safe_read_only(command)
        task = self.tasks.create(requested_by, f"run: {command}", msg_id)
        if not ok:
            self._send_state(task.task_id, "failed", "complete", f"unsafe-command:{reason.replace(' ', '-')[:60]}")
            self._send_debug(f"[{self.name}] command rejected: {reason}")
            return
        self._send_state(task.task_id, "running", "executing")
        command_id = "C-001"
        task.active_command_id = command_id
        self._send_agents(f"[{self.name}] task {task.task_id} command {command_id} running: {command}")
        self.command_thread.submit(CommandRequest(task.task_id, command_id, command))

    def _handle_freeform(self, trigger: Trigger) -> None:
        context = "\n".join(f"[{m.timestamp}] <{m.name}> {m.text}" for m in self.ring.tail(30))
        result = self.backend.run_turn(trigger.text, context)
        self.last_llm_ms = result.duration_ms
        if not result.ok:
            self.last_llm_error = result.error
            self._send_debug(f"[{self.name}] response error: malformed LLM response: {result.error}")
            return
        self.last_llm_error = None
        say = str(result.payload.get("say", "")).strip()
        if say:
            self._send_agents(say)
        for action in result.payload.get("actions", []):
            if action.get("type") == "command":
                self._start_run_task(trigger.source.name, str(action.get("cmd", "")), trigger.source.msg_id)

    def _handle_cancel(self, trigger: Trigger) -> None:
        task_id = trigger.task_id
        if not task_id or task_id not in self.tasks.tasks:
            self._send_debug(f"[{self.name}] cancel rejected: unknown task {task_id}")
            return
        canceled_proc = self.command_thread.cancel()
        reason = "requested-by-human"
        ok, err = self.tasks.transition(task_id, "canceled", "complete", trigger.source.msg_id, reason)
        if ok:
            self._send_state(task_id, "canceled", "complete", reason)
        else:
            self._send_debug(f"[{self.name}] cancel transition failed: {err}")
        if canceled_proc:
            self._send_debug(f"[{self.name}] cancel signal sent for {task_id}")

    def _is_legacy_protocol(self, msg: NormalizedMessage) -> bool:
        if msg.name.lower() == self.name.lower():
            return False
        low = msg.text.lower()
        return (
            "roll call" in low
            or f"[health-check] {self.name.lower()}" in low
            or "ack sent" in low
            or ("action:" in low and "re-read" in low)
        )

    def _handle_legacy_protocol(self, msg: NormalizedMessage) -> bool:
        if msg.name.lower() == self.name.lower():
            return False
        low = msg.text.lower()
        if "roll call" in low:
            self._send_agents(f"[{self.name}] ONLINE | {get_ip()} | VMID={self.vm_id} | listening")
            return True
        if f"[health-check] {self.name.lower()}" in low:
            self._send_agents(f"[{self.name}] ACK - health-check ping received")
            return True
        if "ack sent" in low:
            recv_ts = time.time()
            recv_dt = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int((recv_ts % 1) * 1_000_000):06d}"
            match = re.search(
                r"\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) ACK sent",
                msg.text,
            )
            delay_ms = 0
            if match:
                try:
                    sent_ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                    delay_ms = int((recv_ts - sent_ts) * 1000)
                except ValueError:
                    delay_ms = 0
            responder = f"{self.name}|{get_ip()}|VMID={self.vm_id}"
            self._send_agents(f"[{responder}] {recv_dt} ACK received from {msg.name} | delay={delay_ms}ms")
            return True
        if "action:" in low and "re-read" in low:
            self._send_agents(f"[{self.name}] CLAUDE.md re-read. Ready for task assignments.")
            return True
        return False

    def _load_applied_store(self) -> None:
        try:
            with open(self.applied_store_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, list):
                msg_ids = payload
            elif isinstance(payload, dict):
                msg_ids = payload.get("msg_ids", [])
            else:
                msg_ids = []
            if not isinstance(msg_ids, list):
                raise ValueError("msg_ids is not a list")
            self.applied.load([str(msg_id) for msg_id in msg_ids[-2000:]])
            self.bootstrap_replay_observed = False
        except FileNotFoundError:
            self.bootstrap_replay_observed = True
        except Exception as exc:
            self.applied_store_error = f"{type(exc).__name__}: {exc}"
            self.bootstrap_replay_observed = True

    def _mark_observed(self, msg_id: str | None) -> None:
        if not msg_id:
            return
        with self.applied_store_lock:
            if self.applied.seen(msg_id):
                return
            self.applied.mark(msg_id)
            os.makedirs(os.path.dirname(self.applied_store_path), exist_ok=True)
            tmp_path = f"{self.applied_store_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump({"msg_ids": self.applied.ids()}, fh)
            os.replace(tmp_path, self.applied_store_path)

    def _command_finished(self, result: CommandResult) -> None:
        req = result.request
        if result.output:
            for line in result.output.splitlines()[:40]:
                self._send_agents(f"[{self.name}] task {req.task_id} command {req.command_id} output: {line}")
        if result.error:
            self._send_agents(f"[{self.name}] task {req.task_id} command {req.command_id} failed: {result.error}")
            self._send_state(req.task_id, "failed", "complete", "command-error")
            return
        if result.timed_out:
            self._send_agents(f"[{self.name}] task {req.task_id} command {req.command_id} failed: timeout")
            self._send_state(req.task_id, "failed", "complete", "command-timeout")
            return
        self._send_agents(
            f"[{self.name}] task {req.task_id} command {req.command_id} done: exit={result.exit_code}"
        )
        self._send_state(req.task_id, "done", "complete")

    def _send_state(self, task_id: str, state: str, phase: str, reason: str | None = None) -> None:
        nonce = self.outbound.nonce()
        reason_part = f" reason: {reason}" if reason else ""
        text = f"[{self.name}] task {task_id} state: {state} phase: {phase}{reason_part} n={nonce}"
        self.outbound.add(nonce, text)
        self._send_agents(text)
        self.tasks.transition(task_id, state, phase, None, reason)

    def _send_agents(self, text: str) -> None:
        self._send("agents", text)

    def _send_debug(self, text: str) -> None:
        self._send("debug", text)

    def _send(self, channel: str, text: str) -> None:
        if self.socket_state != "connected":
            return
        self.sio.emit("msg", {"name": self.name, "channel": channel, "text": text[:1000]})

    def _send_status(self, detail: str = "periodic heartbeat") -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z") or datetime.now().isoformat()
        load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
        text = (
            f"[{self.name}] status | proc=periodic heartbeat | time={now} | "
            f"host={self.name}/{self.host_suffix} | ip={get_ip()} | vmid={self.vm_id} | "
            f"detail={detail} | load={load}"
        )
        self._send_debug(text)

    def _schedule_heartbeat(self) -> None:
        timer = threading.Timer(HEARTBEAT_SECS, self._heartbeat_tick)
        timer.daemon = True
        timer.start()

    def _heartbeat_tick(self) -> None:
        self._send_status()
        self._schedule_heartbeat()

    def _debug_text(self) -> str:
        tasks_total = len(self.tasks.tasks)
        tasks_open = len(self.tasks.open_tasks())
        tasks_terminal = tasks_total - tasks_open
        return (
            f"[{self.name}] debug | socket={self.socket_state} | replay={'active' if self.replay_active else 'idle'} | "
            f"buffer={len(self.ring)} | queue={self.events.qsize()} | tasks_total={tasks_total} | "
            f"tasks_open={tasks_open} | tasks_terminal={tasks_terminal} | "
            f"cmd_queue={self.command_thread.depth()} | cmd_active={bool(self.command_thread.current)} | "
            f"pending_outbound={len(self.outbound.pending)} | last_llm_ms={self.last_llm_ms} | "
            f"last_llm_error={self.last_llm_error or '-'} | replay_applied={self.replay_applied} | "
            f"live_backlog={self.live_backlog.qsize()}"
        )

    def _tasks_text(self) -> str:
        open_tasks = self.tasks.open_tasks()
        if not open_tasks:
            return f"[{self.name}] tasks: none"
        lines = [f"[{self.name}] tasks:"]
        now = time.time()
        for task in open_tasks:
            age = int((now - task.last_update_ts) / 60)
            lines.append(f"{task.task_id} {task.state}/{task.phase} age={age}m title=\"{task.title}\"")
        return "\n".join(lines)


def main() -> int:
    cfg = json.load(open(os.path.expanduser("~/agent_config.json")))
    lock_path = f"/tmp/agent_runtime_{cfg['name']}.lock"
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    return EmbeddedRuntime(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())

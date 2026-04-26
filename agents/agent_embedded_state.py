#!/usr/bin/env python3
"""In-memory state containers for embedded agent runtime."""
from __future__ import annotations

import collections
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from agent_embedded_protocol import NormalizedMessage, legal_transition


@dataclass
class Task:
    task_id: str
    owner: str
    requested_by: str
    title: str
    state: str = "pending"
    phase: str = "planning"
    created_msg_id: str | None = None
    last_msg_id: str | None = None
    last_update_ts: float = field(default_factory=time.time)
    attempt: int = 1
    notes: list[str] = field(default_factory=list)
    active_command_id: str | None = None


class RingBuffer:
    def __init__(self, maxlen: int = 500):
        self._items: collections.deque[NormalizedMessage] = collections.deque(maxlen=maxlen)

    def append(self, msg: NormalizedMessage) -> None:
        self._items.append(msg)

    def tail(self, count: int) -> list[NormalizedMessage]:
        return list(self._items)[-count:]

    def __len__(self) -> int:
        return len(self._items)


class AppliedMessageLRU:
    def __init__(self, max_entries: int = 2000):
        self._order: collections.deque[str] = collections.deque()
        self._seen: set[str] = set()
        self._max_entries = max_entries

    def seen(self, msg_id: str | None) -> bool:
        return bool(msg_id and msg_id in self._seen)

    def mark(self, msg_id: str | None) -> None:
        if not msg_id or msg_id in self._seen:
            return
        self._seen.add(msg_id)
        self._order.append(msg_id)
        while len(self._order) > self._max_entries:
            old = self._order.popleft()
            self._seen.discard(old)

    def load(self, msg_ids: list[str]) -> None:
        for msg_id in msg_ids:
            self.mark(msg_id)

    def ids(self) -> list[str]:
        return list(self._order)


class TaskRegistry:
    def __init__(self, owner: str):
        self.owner = owner
        self.tasks: dict[str, Task] = {}
        self._sequence = 0

    def next_task_id(self) -> str:
        self._sequence += 1
        return f"T-{time.strftime('%Y%m%d')}-{self._sequence:04d}"

    def create(self, requested_by: str, title: str, msg_id: str | None) -> Task:
        task = Task(
            task_id=self.next_task_id(),
            owner=self.owner,
            requested_by=requested_by,
            title=title[:120] or "untitled task",
            created_msg_id=msg_id,
            last_msg_id=msg_id,
        )
        self.tasks[task.task_id] = task
        return task

    def transition(
        self,
        task_id: str,
        state: str,
        phase: str,
        msg_id: str | None,
        note: str | None = None,
    ) -> tuple[bool, str]:
        task = self.tasks.get(task_id)
        if not task:
            return False, f"unknown task {task_id}"
        if not legal_transition(task.state, state):
            return False, f"illegal transition {task.state}->{state}"
        task.state = state
        task.phase = phase
        task.last_msg_id = msg_id
        task.last_update_ts = time.time()
        if note:
            task.notes.append(note[:240])
        return True, "ok"

    def apply_chat_transition(
        self,
        task_id: str,
        state: str,
        phase: str,
        msg_id: str | None,
        note: str | None = None,
    ) -> tuple[bool, str]:
        task = self.tasks.get(task_id)
        if not task:
            self._track_sequence(task_id)
            self.tasks[task_id] = Task(
                task_id=task_id,
                owner=self.owner,
                requested_by="chat-replay",
                title=f"reconstructed {task_id}",
                state=state,
                phase=phase,
                created_msg_id=msg_id,
                last_msg_id=msg_id,
                notes=[note[:240]] if note else [],
            )
            return True, "created"
        if task.state == state and task.phase == phase:
            task.last_msg_id = msg_id
            task.last_update_ts = time.time()
            if note:
                task.notes.append(note[:240])
            return True, "duplicate-state"
        return self.transition(task_id, state, phase, msg_id, note)

    def _track_sequence(self, task_id: str) -> None:
        match = re.match(r"^T-\d{8}-(\d{4})$", task_id)
        if match:
            self._sequence = max(self._sequence, int(match.group(1)))

    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.state not in {"done", "canceled"}]


class OutboundTracker:
    def __init__(self):
        self.pending: dict[str, dict[str, Any]] = {}

    def nonce(self) -> str:
        return secrets.token_hex(4)

    def add(self, nonce: str, text: str) -> None:
        self.pending[nonce] = {"text": text, "created": time.time()}

    def commit_from_echo(self, text: str, msg_id: str | None) -> str | None:
        for nonce in list(self.pending):
            if f"n={nonce}" in text:
                self.pending.pop(nonce, None)
                return nonce
        return None

    def expired(self, timeout: float = 10.0) -> list[str]:
        now = time.time()
        return [nonce for nonce, rec in self.pending.items() if now - rec["created"] > timeout]

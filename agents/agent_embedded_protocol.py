#!/usr/bin/env python3
"""Protocol parsing and task transition validation for embedded agents."""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from typing import Any


TASK_ID_RE = re.compile(r"^T-\d{8}-\d{4}$")
STATE_RE = re.compile(
    r"\[(?P<agent>[^\]]+)\]\s+task\s+(?P<task_id>\S+)\s+"
    r"state:\s+(?P<state>\S+)\s+phase:\s+(?P<phase>\S+)"
    r"(?:\s+reason:\s+(?P<reason>\S+))?.*?(?:\s+n=(?P<nonce>[0-9a-f]{8}))?$"
)

LEGAL_TRANSITIONS = {
    None: {"pending"},
    "pending": {"running", "canceled"},
    "running": {"waiting", "blocked", "review", "done", "failed", "canceled"},
    "waiting": {"running", "failed", "canceled"},
    "blocked": {"running", "failed", "canceled"},
    "review": {"running", "approved", "failed", "canceled"},
    "approved": {"deploying", "canceled"},
    "deploying": {"waiting", "rolling_back", "done", "failed"},
    "rolling_back": {"waiting", "done", "failed"},
    "failed": {"running", "canceled"},
}

TERMINAL_STATES = {"done", "canceled"}


@dataclass(frozen=True)
class NormalizedMessage:
    msg_id: str | None
    name: str
    channel: str
    text: str
    timestamp: str
    raw: dict[str, Any]
    received_monotonic: float


@dataclass(frozen=True)
class Trigger:
    kind: str
    text: str = ""
    task_id: str | None = None
    command: str | None = None
    source: NormalizedMessage | None = None


@dataclass(frozen=True)
class ParsedTransition:
    agent: str
    task_id: str
    state: str
    phase: str
    reason: str | None
    nonce: str | None


def normalize_message(data: dict[str, Any]) -> NormalizedMessage:
    return NormalizedMessage(
        msg_id=data.get("id") or data.get("msg_id"),
        name=str(data.get("name", "?")),
        channel=str(data.get("channel", "main")),
        text=str(data.get("text", "")),
        timestamp=str(data.get("ts", "")),
        raw=dict(data),
        received_monotonic=time.monotonic(),
    )


def parse_state_transition(text: str) -> ParsedTransition | None:
    match = STATE_RE.match(text.strip())
    if not match:
        return None
    return ParsedTransition(**match.groupdict())


def legal_transition(previous: str | None, new_state: str) -> bool:
    return new_state in LEGAL_TRANSITIONS.get(previous, set())


def parse_trigger(my_name: str, msg: NormalizedMessage) -> Trigger | None:
    text = msg.text.strip()
    lower_name = my_name.lower()
    if msg.name.lower() == lower_name:
        return None
    rest: str | None = None
    lower_text = text.lower()
    if lower_text.startswith(f"@{lower_name}"):
        rest = text[len(my_name) + 1 :].strip()
    else:
        plain_mention = re.match(
            rf"^{re.escape(my_name)}(?:[:,]|\s)\s*(.*)$",
            text,
            re.IGNORECASE,
        )
        if plain_mention:
            rest = plain_mention.group(1).strip()
        else:
            return None

    if not rest:
        return None
    low = rest.lower()

    if low == "debug":
        return Trigger(kind="debug", source=msg)
    if low in {"tasks", "status?", "status"}:
        return Trigger(kind="tasks", source=msg)
    if low.startswith("assign:"):
        return Trigger(kind="assign", text=rest.split(":", 1)[1].strip(), source=msg)
    if low.startswith("run:"):
        return Trigger(kind="run", command=rest.split(":", 1)[1].strip(), source=msg)
    if low.startswith("cancel "):
        parts = rest.split()
        task_id = parts[1] if len(parts) > 1 else None
        return Trigger(kind="cancel", task_id=task_id, source=msg)

    return Trigger(kind="freeform", text=rest, source=msg)


def command_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []

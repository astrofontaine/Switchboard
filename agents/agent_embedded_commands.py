#!/usr/bin/env python3
"""Read-only local command execution for embedded agent runtime."""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from agent_embedded_protocol import command_words


MAX_OUTPUT_BYTES = 16 * 1024
MAX_OUTPUT_LINES = 40
DEFAULT_TIMEOUT = 10

SAFE_BASE_COMMANDS = {
    "pwd",
    "date",
    "whoami",
    "hostname",
    "uptime",
    "df",
    "free",
    "ip",
    "ls",
    "cat",
    "systemctl",
    "journalctl",
}

APPROVED_CAT_PREFIXES = (
    "/home/longshot/",
    "/mnt/shared/",
    "/etc/fstab",
    "/etc/hosts",
)


@dataclass
class CommandRequest:
    task_id: str
    command_id: str
    command: str


@dataclass
class CommandResult:
    request: CommandRequest
    exit_code: int | None
    output: str
    timed_out: bool = False
    canceled: bool = False
    error: str | None = None


def is_safe_read_only(command: str) -> tuple[bool, str]:
    words = command_words(command)
    if not words:
        return False, "command did not parse"
    base = words[0]
    if base not in SAFE_BASE_COMMANDS:
        return False, f"{base} is not in the read-only allowlist"
    if base == "df" and words != ["df", "-h"]:
        return False, "only df -h is allowed"
    if base == "free" and words != ["free", "-h"]:
        return False, "only free -h is allowed"
    if base == "ip" and (len(words) < 2 or words[1] not in {"addr", "route"}):
        return False, "only ip addr and ip route are allowed"
    if base == "systemctl" and (len(words) < 3 or words[1] != "status"):
        return False, "only systemctl status <service> is allowed"
    if base == "journalctl" and "-n" not in words:
        return False, "journalctl requires -n <lines>"
    if base == "cat":
        if len(words) != 2:
            return False, "cat requires exactly one path"
        path = os.path.abspath(os.path.expanduser(words[1]))
        if not any(path == prefix or path.startswith(prefix) for prefix in APPROVED_CAT_PREFIXES):
            return False, "cat path is outside approved prefixes"
    return True, "ok"


class CommandThread:
    def __init__(self, on_result):
        self._queue: queue.Queue[CommandRequest] = queue.Queue()
        self._on_result = on_result
        self._thread = threading.Thread(target=self._run, name="command-thread", daemon=True)
        self._current_proc: subprocess.Popen | None = None
        self._current_request: CommandRequest | None = None
        self._cancel = threading.Event()
        self._thread.start()

    @property
    def current(self) -> CommandRequest | None:
        return self._current_request

    def submit(self, request: CommandRequest) -> None:
        self._queue.put(request)

    def cancel(self) -> bool:
        self._cancel.set()
        proc = self._current_proc
        if not proc:
            return False
        try:
            proc.send_signal(signal.SIGINT)
            return True
        except Exception:
            return False

    def depth(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            self._current_request = request
            self._cancel.clear()
            result = self._execute(request)
            self._current_request = None
            self._current_proc = None
            self._on_result(result)

    def _execute(self, request: CommandRequest) -> CommandResult:
        ok, reason = is_safe_read_only(request.command)
        if not ok:
            return CommandResult(request, exit_code=None, output="", error=reason)
        try:
            proc = subprocess.Popen(
                command_words(request.command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            self._current_proc = proc
            try:
                output, _ = proc.communicate(timeout=DEFAULT_TIMEOUT)
                return CommandResult(request, proc.returncode, _cap_output(output))
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate(timeout=2)
                return CommandResult(request, None, _cap_output(output), timed_out=True)
        except Exception as exc:
            return CommandResult(request, None, "", error=f"{type(exc).__name__}: {exc}")


def _cap_output(output: str) -> str:
    data = output[:MAX_OUTPUT_BYTES]
    lines = data.splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        return "\n".join(lines[:MAX_OUTPUT_LINES]) + "\n[output-truncated: 40 line cap reached]"
    if len(output) > MAX_OUTPUT_BYTES:
        return data + "\n[output-truncated: 16 KiB cap reached]"
    return data.strip()


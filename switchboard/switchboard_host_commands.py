"""Switchboard /host command parser, policy gate, and minimal executor.

This module owns the debug-level host-command syntax, command ids, policy
decisions, and the cautious short-lived bash executor. chat.py stays focused on
chat transport and history.
"""
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time

HOST_PREFIX = "/host "
HOST_CANCEL_PREFIX = "/host-cancel "
HOST_LABEL = os.environ.get("SWITCHBOARD_HOST_LABEL", "human@192.168.2.16")
WORKDIR = os.environ.get("SWITCHBOARD_HOST_WORKDIR", os.path.expanduser("~"))

POLICY_MODE = os.environ.get("SWITCHBOARD_HOST_POLICY", "denylist").strip().lower() or "denylist"
if POLICY_MODE not in {"denylist", "allowlist"}:
    POLICY_MODE = "denylist"
TIMEOUT_SECS = 10
GRACE_SECS = 1
MAX_STDOUT_BYTES = 16 * 1024
MAX_STDERR_BYTES = 16 * 1024
EXEC_ENV = {
    "HOME": os.path.expanduser("~"),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}

DENYLIST = {
    "rm",
    "mv",
    "dd",
    "shred",
    "truncate",
    "chmod",
    "chown",
    "chgrp",
    "chattr",
    "ln",
    "unlink",
    "mkfs",
    "fdisk",
    "parted",
    "mount",
    "umount",
    "reboot",
    "shutdown",
    "poweroff",
    "halt",
    "init",
    "systemctl",
    "service",
    "sudo",
    "su",
    "passwd",
    "visudo",
    "apt",
    "apt-get",
    "dpkg",
}

ALLOWLIST_PREFIXES = [
    ("pwd",),
    ("date",),
    ("uptime",),
    ("hostname",),
    ("whoami",),
    ("id",),
    ("df",),
    ("free",),
    ("ip",),
    ("ss",),
    ("ls",),
    ("cat", os.path.join(os.path.expanduser("~"), "shared") + "/"),
    ("cat", os.path.join(WORKDIR, "chat.py")),
    ("journalctl", "-n"),
]

BLOCKED_SYNTAX = ["&&", "||", ">>", "$(", ";", ">", "<", "`", "&"]
WRAPPER_TOKENS = {"bash", "sh", "zsh", "env", "command", "builtin", "xargs"}

_LOCK = threading.Lock()
_COUNTER = 0
_COMMANDS = {}
_QUEUE = queue.Queue()
_WORKER_STARTED = False
_EMIT = None
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SHELL_IDLE_SECS = 10
SHELL_PROMPT = "SWITCHBOARD_HOST_READY"
_SHELL = None


def next_command_id():
    global _COUNTER
    with _LOCK:
        _COUNTER += 1
        return _COUNTER


def is_host_command(text):
    return text.startswith(HOST_PREFIX) or text.startswith(HOST_CANCEL_PREFIX)


def command_event(kind, text, cmd_id=None, user=None, raw_command=None, argv=None, reason=None, channel=None, display_text=None):
    event = {
        "kind": kind,
        "text": text,
        "host": HOST_LABEL,
        "policy": POLICY_MODE,
        "cwd": WORKDIR,
    }
    if channel is not None:
        event["channel"] = channel
    if cmd_id is not None:
        event["cmd_id"] = cmd_id
    if user is not None:
        event["user"] = user
    if raw_command is not None:
        event["raw_command"] = raw_command
    if argv is not None:
        event["argv"] = argv
    if reason is not None:
        event["reason"] = reason
    if display_text is not None:
        event["display_text"] = display_text
    return event


def configure_executor(emit_callback):
    global _EMIT
    _EMIT = emit_callback
    _ensure_worker()


def _ensure_worker():
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True
    thread = threading.Thread(target=_worker_loop, name="switchboard-host-command-worker", daemon=True)
    thread.start()


def _emit(event):
    if _EMIT:
        _EMIT(event)


def _clean_output(data):
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_RE.sub("", text)
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def _update_command(cmd_id, **updates):
    with _LOCK:
        command = _COMMANDS.get(cmd_id)
        if not command:
            return None
        command.update(updates)
        command["updated_at"] = time.time()
        return dict(command)


def _command_snapshot(cmd_id):
    with _LOCK:
        command = _COMMANDS.get(cmd_id)
        return dict(command) if command else None


def register_pending_command(cmd_id, user, raw_command, argv, channel):
    now = time.time()
    with _LOCK:
        _COMMANDS[cmd_id] = {
            "id": cmd_id,
            "user": user,
            "raw_command": raw_command,
            "argv": argv,
            "channel": channel,
            "state": "pending",
            "created_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }


def enqueue_command(cmd_id):
    _ensure_worker()
    _QUEUE.put(cmd_id)


def get_command(cmd_id):
    with _LOCK:
        command = _COMMANDS.get(cmd_id)
        return dict(command) if command else None


def list_commands():
    with _LOCK:
        return [dict(command) for command in _COMMANDS.values()]


def mark_cancel_requested(cmd_id, user):
    now = time.time()
    with _LOCK:
        command = _COMMANDS.get(cmd_id)
        if not command:
            return None
        command["cancel_requested"] = True
        command["cancel_user"] = user
        if command["state"] in {"done", "denied", "failed", "timeout", "canceled"}:
            return dict(command)
        command["state"] = "cancel-pending"
        command["updated_at"] = now
        process = command.get("process")
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    return dict(command)


def interrupt_shell(channel, user="chat"):
    """Stop the warm shell when ordinary chat activity interrupts it."""
    killed = _stop_shell(reason=f"interrupted channel={channel} user={user}", emit=True)
    return killed


def _first_token(argv):
    return os.path.basename(argv[0]).lower()


def _matches_allowlist(argv):
    for prefix in ALLOWLIST_PREFIXES:
        if len(argv) < len(prefix):
            continue
        if tuple(argv[: len(prefix)]) == prefix:
            return True
    return False


def _validate_argv(argv):
    first = _first_token(argv)
    if first in {"bash", "sh", "zsh"}:
        return f"wrapper command is blocked: {first}"
    if first == "env" and any(os.path.basename(token).lower() in {"bash", "sh", "zsh"} for token in argv[1:]):
        return "wrapper command is blocked: env shell"
    if first in {"command", "builtin"} and len(argv) > 1:
        wrapped = os.path.basename(argv[1]).lower()
        if wrapped in DENYLIST:
            return f"wrapper invokes denylisted command: {wrapped}"
    if first == "xargs":
        return "wrapper command is blocked: xargs"
    for token in argv[1:]:
        if os.path.basename(token).lower() in WRAPPER_TOKENS:
            return f"obvious wrapper token is blocked: {token}"

    if POLICY_MODE == "allowlist":
        if not _matches_allowlist(argv):
            return "command not allowlisted"
    else:
        if first in DENYLIST:
            return f"first token is denylisted: {first}"
    return None


def _stream_reader(pipe, stream_name, command):
    cmd_id = command["id"]
    total = 0
    limit = MAX_STDOUT_BYTES if stream_name == "stdout" else MAX_STDERR_BYTES
    chunks = []
    buffered_lines = []

    def flush_lines():
        if not buffered_lines:
            return
        text = "".join(buffered_lines)
        buffered_lines.clear()
        clean = text.rstrip("\n")
        if not clean:
            return
        chunks.append(text)
        _emit(
            command_event(
                stream_name,
                f"[cmd@human {stream_name} id={cmd_id}]\n{clean}",
                cmd_id=cmd_id,
                user=command["user"],
                raw_command=command["raw_command"],
                argv=command["argv"],
                channel=command.get("channel"),
                display_text=clean,
            )
        )

    while True:
        data = pipe.readline()
        if not data:
            break
        remaining = max(0, limit - total)
        if remaining <= 0:
            continue
        chunk = data[:remaining]
        total += len(chunk)
        text = _clean_output(chunk)
        if text:
            buffered_lines.append(text)
            if len(buffered_lines) >= 40:
                flush_lines()
        if len(data) > remaining:
            flush_lines()
            _emit(
                command_event(
                    f"{stream_name}-truncated",
                    f"[cmd@human {stream_name} truncated id={cmd_id} limit={limit}B]",
                    cmd_id=cmd_id,
                    user=command["user"],
                    raw_command=command["raw_command"],
                    argv=command["argv"],
                    reason=f"{stream_name} exceeded {limit} bytes",
                    channel=command.get("channel"),
                )
            )
            break
    flush_lines()
    return "".join(chunks), total >= limit


def _reader_thread(pipe, output_queue):
    while True:
        data = pipe.readline()
        if not data:
            break
        output_queue.put(data)


def _start_shell(channel=None):
    global _SHELL
    stdout_queue = queue.Queue()
    stderr_queue = queue.Queue()
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=WORKDIR,
        env=dict(EXEC_ENV, PS1="", PROMPT_COMMAND=""),
        shell=False,
        start_new_session=True,
    )
    _SHELL = {
        "process": process,
        "stdout": stdout_queue,
        "stderr": stderr_queue,
        "last_used": time.time(),
        "channel": channel,
    }
    threading.Thread(target=_reader_thread, args=(process.stdout, stdout_queue), daemon=True).start()
    threading.Thread(target=_reader_thread, args=(process.stderr, stderr_queue), daemon=True).start()
    _emit(command_event("shell-start", f"[cmd@human shell-start pid={process.pid} idle={SHELL_IDLE_SECS}s]", channel=channel))
    return _SHELL


def _get_shell(channel=None):
    global _SHELL
    with _LOCK:
        if _SHELL and _SHELL["process"].poll() is None:
            _SHELL["last_used"] = time.time()
            _SHELL["channel"] = channel
            return _SHELL
        _SHELL = None
        return _start_shell(channel=channel)


def _stop_shell(reason="stop", emit=False):
    global _SHELL
    with _LOCK:
        shell = _SHELL
        _SHELL = None
    if not shell:
        return False
    process = shell["process"]
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            process.wait(timeout=1)
    if emit:
        _emit(command_event("shell-stop", f"[cmd@human shell-stop reason={reason}]", channel=shell.get("channel")))
    return True


def _expire_idle_shell():
    with _LOCK:
        shell = _SHELL
        if not shell:
            return
        if time.time() - shell["last_used"] < SHELL_IDLE_SECS:
            return
    _stop_shell(reason="idle-timeout", emit=True)


def _emit_stream(command, stream_name, lines):
    if not lines:
        return
    cmd_id = command["id"]
    text = _clean_output(b"".join(lines)).rstrip("\n")
    if not text:
        return
    _emit(
        command_event(
            stream_name,
            f"[cmd@human {stream_name} id={cmd_id}]\n{text}",
            cmd_id=cmd_id,
            user=command["user"],
            raw_command=command["raw_command"],
            argv=command["argv"],
            channel=command.get("channel"),
            display_text=text,
        )
    )


def _worker_loop():
    while True:
        try:
            cmd_id = _QUEUE.get(timeout=1)
        except queue.Empty:
            _expire_idle_shell()
            continue
        try:
            _execute_command(cmd_id)
        except Exception as e:
            command = _command_snapshot(cmd_id)
            if command:
                _update_command(cmd_id, state="failed", error=str(e))
                _emit(
                    command_event(
                        "failed",
                        f"[cmd@human failed id={cmd_id}] {e}",
                        cmd_id=cmd_id,
                        user=command["user"],
                        raw_command=command["raw_command"],
                        argv=command["argv"],
                        reason=str(e),
                        channel=command.get("channel"),
                    )
                )
        finally:
            _QUEUE.task_done()


def _execute_command(cmd_id):
    command = _command_snapshot(cmd_id)
    if not command:
        return
    if command.get("cancel_requested"):
        _update_command(cmd_id, state="canceled")
        _emit(
            command_event(
                "canceled",
                f"[cmd@human canceled id={cmd_id} before-start by={command.get('cancel_user', 'unknown')}]",
                cmd_id=cmd_id,
                user=command["user"],
                raw_command=command["raw_command"],
                argv=command["argv"],
                channel=command.get("channel"),
            )
        )
        return

    start = time.time()
    _update_command(cmd_id, state="running", started_at=start)
    _emit(
        command_event(
            "start",
            f"[cmd@human start id={cmd_id} cwd={WORKDIR} timeout={TIMEOUT_SECS}s]",
            cmd_id=cmd_id,
            user=command["user"],
            raw_command=command["raw_command"],
            argv=command["argv"],
            channel=command.get("channel"),
        )
    )

    shell = _get_shell(channel=command.get("channel"))
    process = shell["process"]
    _update_command(cmd_id, process=process)
    token = f"__SWITCHBOARD_DONE_{cmd_id}_{int(start * 1000)}__"
    script = (
        f"{command['raw_command']}\n"
        "__switchboard_ec=$?\n"
        f"printf '\\n{token}:%s\\n' \"$__switchboard_ec\"\n"
    )
    process.stdin.write(script.encode("utf-8", errors="replace"))
    process.stdin.flush()

    timed_out = False
    cancel_sent = False
    cancel_deadline = None
    deadline = start + TIMEOUT_SECS
    exit_code = None
    stdout_lines = []
    stderr_lines = []
    while True:
        while True:
            try:
                line = shell["stdout"].get_nowait()
            except queue.Empty:
                break
            clean_line = _clean_output(line).strip()
            if clean_line.startswith(token + ":"):
                try:
                    exit_code = int(clean_line.rsplit(":", 1)[1])
                except ValueError:
                    exit_code = 1
                break
            stdout_lines.append(line)
            if len(stdout_lines) >= 40:
                _emit_stream(command, "stdout", stdout_lines)
                stdout_lines = []
        while True:
            try:
                line = shell["stderr"].get_nowait()
            except queue.Empty:
                break
            stderr_lines.append(line)
            if len(stderr_lines) >= 40:
                _emit_stream(command, "stderr", stderr_lines)
                stderr_lines = []
        if exit_code is not None:
            break
        if process.poll() is not None:
            exit_code = process.returncode
            _stop_shell(reason="shell-exited", emit=True)
            break
        latest = _command_snapshot(cmd_id) or command
        if latest.get("cancel_requested") and not cancel_sent:
            cancel_sent = True
            cancel_deadline = time.time() + GRACE_SECS
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
        if cancel_sent and cancel_deadline and time.time() >= cancel_deadline and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            exit_code = process.wait()
            break
        if time.time() >= deadline:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            try:
                exit_code = process.wait(timeout=GRACE_SECS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    pass
                exit_code = process.wait()
            break
        time.sleep(0.05)

    _emit_stream(command, "stdout", stdout_lines)
    _emit_stream(command, "stderr", stderr_lines)

    duration = time.time() - start
    latest = _command_snapshot(cmd_id) or command
    canceled = bool(latest.get("cancel_requested"))
    if timed_out:
        state = "timeout"
        status_text = f"[cmd@human timeout id={cmd_id} after={duration:.2f}s exit={exit_code}]"
    elif canceled:
        state = "canceled"
        status_text = f"[cmd@human canceled id={cmd_id} by={latest.get('cancel_user', 'unknown')} exit={exit_code} duration={duration:.2f}s]"
    else:
        state = "done"
        status_text = f"[cmd@human exit={exit_code} id={cmd_id} duration={duration:.2f}s]"
    _update_command(cmd_id, state=state, exit_code=exit_code, duration=duration, process=None)
    if timed_out or canceled:
        _stop_shell(reason=state, emit=True)
    elif _SHELL:
        with _LOCK:
            if _SHELL:
                _SHELL["last_used"] = time.time()
    _emit(
        command_event(
            state if state != "done" else "exit",
            status_text,
            cmd_id=cmd_id,
            user=command["user"],
            raw_command=command["raw_command"],
            argv=command["argv"],
            channel=command.get("channel"),
        )
    )


def _parse(raw_command):
    for marker in BLOCKED_SYNTAX:
        if marker in raw_command:
            return None, f"blocked shell syntax detected: {marker}"
    if "||" in raw_command or "&&" in raw_command:
        return None, "blocked shell syntax detected"
    try:
        pipeline = []
        for segment in raw_command.split("|"):
            segment = segment.strip()
            if not segment:
                return None, "empty pipeline segment"
            pipeline.append(shlex.split(segment))
    except ValueError as e:
        return None, f"parse failed: {e}"
    if not pipeline or not pipeline[0]:
        return None, "empty command"
    return pipeline, None


def validate_host_argv(raw_command):
    pipeline, reason = _parse(raw_command)
    if reason:
        return None, reason

    for argv in pipeline:
        reason = _validate_argv(argv)
        if reason:
            return None, reason

    flattened = []
    for index, argv in enumerate(pipeline):
        if index:
            flattened.append("|")
        flattened.extend(argv)
    return flattened, None


def should_enqueue(events):
    return any(event.get("kind") == "pending" for event in events)


def handle_host_command(user, text, channel="debug"):
    if text.startswith(HOST_CANCEL_PREFIX):
        raw_id = text[len(HOST_CANCEL_PREFIX) :].strip()
        cmd_id = next_command_id()
        if not raw_id.isdigit():
            return [
                command_event(
                    "cancel-denied",
                    f"[cmd@human cancel-denied id={cmd_id} user={user}] /host-cancel {raw_id}",
                    cmd_id=cmd_id,
                    user=user,
                    raw_command=raw_id,
                    reason="cancel id must be numeric",
                    channel=channel,
                ),
                command_event(
                    "deny-reason",
                    f"[cmd@human deny-reason id={cmd_id}] cancel id must be numeric",
                    cmd_id=cmd_id,
                    user=user,
                    raw_command=raw_id,
                    reason="cancel id must be numeric",
                    channel=channel,
                ),
            ]
        target_id = int(raw_id)
        command = mark_cancel_requested(target_id, user)
        if not command:
            return [
                command_event(
                    "cancel-denied",
                    f"[cmd@human cancel-denied id={target_id} user={user}] no pending/running command with that id",
                    cmd_id=target_id,
                    user=user,
                    raw_command=raw_id,
                    reason="unknown command id",
                    channel=channel,
                )
            ]
        return [
            command_event(
                "cancel-pending",
                f"[cmd@human cancel-pending id={target_id} user={user}] cancellation requested",
                cmd_id=target_id,
                user=user,
                raw_command=command["raw_command"],
                argv=command["argv"],
                channel=command.get("channel", channel),
            )
        ]

    if not text.startswith(HOST_PREFIX):
        return None

    raw_command = text[len(HOST_PREFIX) :].strip()
    cmd_id = next_command_id()
    argv, reason = validate_host_argv(raw_command)
    if reason:
        return [
            command_event(
                "denied",
                f"[cmd@human denied id={cmd_id} user={user}] /host {raw_command}",
                cmd_id=cmd_id,
                user=user,
                raw_command=raw_command,
                reason=reason,
                channel=channel,
            ),
            command_event(
                "deny-reason",
                f"[cmd@human deny-reason id={cmd_id}] {reason}",
                cmd_id=cmd_id,
                user=user,
                raw_command=raw_command,
                reason=reason,
                channel=channel,
            ),
        ]

    register_pending_command(cmd_id, user, raw_command, argv, channel)
    return [
        command_event(
            "request",
            f"[cmd@human {HOST_LABEL} id={cmd_id} user={user}]\n$ /host {raw_command}",
            cmd_id=cmd_id,
            user=user,
            raw_command=raw_command,
            argv=argv,
            channel=channel,
            display_text=f"$ /host {raw_command}",
        ),
        command_event(
            "accepted",
            f"[cmd@human accepted id={cmd_id} policy={POLICY_MODE} cwd={WORKDIR}] argv={json.dumps(argv)}",
            cmd_id=cmd_id,
            user=user,
            raw_command=raw_command,
            argv=argv,
            channel=channel,
        ),
        command_event(
            "pending",
            f"[cmd@human pending id={cmd_id}] queued for execution",
            cmd_id=cmd_id,
            user=user,
            raw_command=raw_command,
            argv=argv,
            channel=channel,
        ),
    ]

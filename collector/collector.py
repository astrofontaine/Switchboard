#!/usr/bin/env python3
"""Remote data collector using SSH or Telnet.

Usage:
    python collector.py --config collector_config.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import pathlib
import re
import shlex
import socket
import sys
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ConnectionConfig:
    access_method: str
    host: str
    port: int
    username: str
    password: str
    timeout_seconds: int
    prompt: str


@dataclass
class OutputConfig:
    path: str
    default_filename: str


@dataclass
class CollectorConfig:
    connection: ConnectionConfig
    commands: List[str]
    output: OutputConfig


class SessionLog:
    def __init__(self) -> None:
        self._lines: List[str] = []

    def add(self, line: str = "") -> None:
        self._lines.append(line)

    def add_section(self, title: str) -> None:
        self.add("=" * 78)
        self.add(title)
        self.add("=" * 78)

    def write_to_file(self, target: pathlib.Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(self._lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect remote command output over SSH/Telnet")
    parser.add_argument(
        "--config",
        default="collector_config.json",
        help="Path to config JSON file (default: collector_config.json)",
    )
    return parser.parse_args()


def load_config(config_path: pathlib.Path) -> CollectorConfig:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in config file {config_path}: {exc}") from exc

    try:
        conn = raw["connection"]
        out = raw["output"]
        cfg = CollectorConfig(
            connection=ConnectionConfig(
                access_method=str(conn["access_method"]).strip().lower(),
                host=str(conn["host"]),
                port=int(conn.get("port", 22 if conn["access_method"].lower() == "ssh" else 23)),
                username=str(conn["username"]),
                password=str(conn["password"]),
                timeout_seconds=int(conn.get("timeout_seconds", 20)),
                prompt=str(conn.get("prompt", "$")),
            ),
            commands=[str(c) for c in raw["commands"]],
            output=OutputConfig(
                path=str(out.get("path", ".")),
                default_filename=str(out.get("default_filename", "collector_output.txt")),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Invalid config shape in {config_path}: {exc}") from exc

    if cfg.connection.access_method not in {"ssh", "telnet"}:
        raise SystemExit("connection.access_method must be 'ssh' or 'telnet'")
    if not cfg.commands:
        raise SystemExit("commands must contain at least one command")

    return cfg


def resolve_output_file(output_cfg: OutputConfig, host: str) -> pathlib.Path:
    out_dir = pathlib.Path(output_cfg.path).expanduser()
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    filename = output_cfg.default_filename
    safe_host = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("._-") or "unknown_host"

    stem, suffix = os.path.splitext(filename)
    if not suffix:
        suffix = ".txt"
    if not stem:
        stem = "collector_output"

    return out_dir / f"{stem}_{safe_host}_{timestamp}{suffix}"


def print_live(line: str = "") -> None:
    print(line, flush=True)


def _is_placeholder_password(password: str) -> bool:
    return not password or password.strip().lower() == "change_me"


def _is_permission_error(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    needles = (
        "permission denied",
        "must be root",
        "operation not permitted",
        "requires root",
    )
    return any(n in lowered for n in needles)


def _is_sudo_auth_error(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    needles = (
        "sorry, try again",
        "no password was provided",
        "incorrect password attempt",
        "a password is required",
    )
    return any(n in lowered for n in needles)


def _run_ssh_command(
    client,
    command: str,
    timeout: int,
    log: SessionLog,
    password_stdin: Optional[str] = None,
) -> tuple[int, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)

    if password_stdin is not None:
        stdin.write(password_stdin + "\n")
        stdin.flush()
    stdin.close()

    for line in iter(stdout.readline, ""):
        line = line.rstrip("\n")
        print_live(line)
        log.add(line)

    stderr_text = stderr.read().decode("utf-8", errors="replace")
    if stderr_text.strip():
        for line in stderr_text.splitlines():
            print_live(f"[STDERR] {line}")
            log.add(f"[STDERR] {line}")

    rc = stdout.channel.recv_exit_status()
    return rc, stderr_text


def _run_sudo_with_fallback(
    client,
    base_command: str,
    timeout: int,
    log: SessionLog,
    username: str,
    host: str,
    cached_sudo_password: Optional[str],
    config_password: str,
) -> tuple[int, Optional[str]]:
    sudo_command = f"sudo -k -S -p '' bash -lc {shlex.quote(base_command)}"

    candidates: List[str] = []
    if cached_sudo_password and cached_sudo_password not in candidates:
        candidates.append(cached_sudo_password)
    if not _is_placeholder_password(config_password) and config_password not in candidates:
        candidates.append(config_password)

    rc = 1
    stderr_text = ""
    for idx, candidate in enumerate(candidates):
        if idx > 0:
            print_live("[INFO] Retrying sudo with password from config.")
        rc, stderr_text = _run_ssh_command(
            client=client,
            command=sudo_command,
            timeout=timeout,
            log=log,
            password_stdin=candidate,
        )
        if rc == 0:
            return rc, candidate
        if not _is_sudo_auth_error(stderr_text):
            return rc, cached_sudo_password

    if _is_sudo_auth_error(stderr_text) or not candidates:
        prompted = getpass.getpass(
            prompt=f"[INFO] Enter sudo password for {username}@{host}: "
        ).strip()
        if prompted:
            rc, stderr_text = _run_ssh_command(
                client=client,
                command=sudo_command,
                timeout=timeout,
                log=log,
                password_stdin=prompted,
            )
            if rc == 0:
                return rc, prompted

    return rc, cached_sudo_password


def run_ssh(cfg: CollectorConfig, log: SessionLog) -> None:
    try:
        import paramiko  # type: ignore
    except Exception as exc:
        raise SystemExit(
            "SSH mode requires the 'paramiko' package. Install with: pip install paramiko"
        ) from exc

    c = cfg.connection
    print_live(f"[INFO] Connecting via SSH to {c.host}:{c.port} as {c.username}...")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        effective_password: Optional[str] = None
        if not _is_placeholder_password(c.password):
            effective_password = c.password
            try:
                client.connect(
                    hostname=c.host,
                    port=c.port,
                    username=c.username,
                    password=effective_password,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=c.timeout_seconds,
                )
            except paramiko.AuthenticationException:
                effective_password = None

        if effective_password is None:
            prompted = getpass.getpass(
                prompt=f"[INFO] SSH password required for {c.username}@{c.host}: "
            ).strip()
            if not prompted:
                raise SystemExit("SSH authentication failed and no password was entered.")
            client.connect(
                hostname=c.host,
                port=c.port,
                username=c.username,
                password=prompted,
                look_for_keys=False,
                allow_agent=False,
                timeout=c.timeout_seconds,
            )
            effective_password = prompted

        sudo_password: Optional[str] = effective_password

        log.add_section("SESSION START")
        log.add(f"timestamp: {dt.datetime.now().isoformat()}")
        log.add(f"method: ssh")
        log.add(f"target: {c.host}:{c.port}")
        log.add(f"username: {c.username}")
        log.add("")

        for cmd in cfg.commands:
            print_live(f"\n[CMD] {cmd}")
            log.add_section(f"COMMAND: {cmd}")
            stripped = cmd.strip()

            if stripped.startswith("sudo "):
                base_command = stripped[len("sudo ") :]
                rc, sudo_password = _run_sudo_with_fallback(
                    client=client,
                    base_command=base_command,
                    timeout=c.timeout_seconds,
                    log=log,
                    username=c.username,
                    host=c.host,
                    cached_sudo_password=sudo_password,
                    config_password=c.password,
                )
            else:
                rc, stderr_text = _run_ssh_command(
                    client=client,
                    command=cmd,
                    timeout=c.timeout_seconds,
                    log=log,
                )
                if rc != 0 and _is_permission_error(stderr_text):
                    print_live(f"[INFO] Retrying with sudo for command: {cmd}")
                    log.add(f"[INFO] Retrying with sudo for command: {cmd}")
                    rc, sudo_password = _run_sudo_with_fallback(
                        client=client,
                        base_command=cmd,
                        timeout=c.timeout_seconds,
                        log=log,
                        username=c.username,
                        host=c.host,
                        cached_sudo_password=sudo_password,
                        config_password=c.password,
                    )

            status = f"[EXIT CODE] {rc}"
            print_live(status)
            log.add(status)

    finally:
        print_live("\n[INFO] Closing SSH session")
        client.close()


def _recv_until(sock: socket.socket, expected: str, timeout: int, capture: Optional[List[str]] = None) -> str:
    sock.settimeout(timeout)
    buffer = ""
    expected_lower = expected.lower()

    while expected_lower not in buffer.lower():
        data = sock.recv(4096)
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        buffer += text
        if capture is not None:
            capture.append(text)
    return buffer


def run_telnet(cfg: CollectorConfig, log: SessionLog) -> None:
    c = cfg.connection
    print_live(f"[INFO] Connecting via Telnet to {c.host}:{c.port} as {c.username}...")

    sock = socket.create_connection((c.host, c.port), timeout=c.timeout_seconds)
    try:
        log.add_section("SESSION START")
        log.add(f"timestamp: {dt.datetime.now().isoformat()}")
        log.add("method: telnet")
        log.add(f"target: {c.host}:{c.port}")
        log.add(f"username: {c.username}")
        log.add("")

        chunks: List[str] = []
        _recv_until(sock, "login:", c.timeout_seconds, chunks)
        sock.sendall((c.username + "\n").encode("utf-8"))
        _recv_until(sock, "password:", c.timeout_seconds, chunks)
        sock.sendall((c.password + "\n").encode("utf-8"))
        _recv_until(sock, c.prompt, c.timeout_seconds, chunks)

        for pre in "".join(chunks).splitlines():
            pre = pre.rstrip("\n")
            if pre:
                print_live(pre)
                log.add(pre)

        for cmd in cfg.commands:
            print_live(f"\n[CMD] {cmd}")
            log.add_section(f"COMMAND: {cmd}")
            sock.sendall((cmd + "\n").encode("utf-8"))
            response = _recv_until(sock, c.prompt, c.timeout_seconds)

            for line in response.splitlines():
                clean = line.rstrip("\n")
                print_live(clean)
                log.add(clean)

            # Small pause to reduce merged command echoes on slower endpoints.
            time.sleep(0.2)

        sock.sendall(b"exit\n")
    finally:
        print_live("\n[INFO] Closing Telnet session")
        sock.close()


def main() -> None:
    args = parse_args()
    config_path = pathlib.Path(args.config).expanduser()
    cfg = load_config(config_path)

    output_file = resolve_output_file(cfg.output, cfg.connection.host)
    session_log = SessionLog()

    print_live(f"[INFO] Loaded config: {config_path}")
    print_live(f"[INFO] Output file:   {output_file}")

    try:
        if cfg.connection.access_method == "ssh":
            run_ssh(cfg, session_log)
        else:
            run_telnet(cfg, session_log)
    except (socket.timeout, TimeoutError):
        print_live("[ERROR] Network operation timed out")
        raise SystemExit(1)
    except socket.error as exc:
        print_live(f"[ERROR] Network error: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print_live("\n[WARN] Interrupted by user")
        raise SystemExit(130)

    session_log.add_section("SESSION END")
    session_log.add(f"timestamp: {dt.datetime.now().isoformat()}")
    session_log.write_to_file(output_file)
    print_live(f"[INFO] Session log saved to: {output_file}")


if __name__ == "__main__":
    main()

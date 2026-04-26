#!/usr/bin/env python3
"""Backend adapters for embedded agent runtime."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class BackendResult:
    ok: bool
    payload: dict
    raw: str
    error: str | None = None
    duration_ms: int = 0


class ClaudeAdapter:
    def __init__(self, claude_bin: str, model: str, timeout: int = 60):
        self.claude_bin = claude_bin
        self.model = model
        self.timeout = timeout

    def run_turn(self, instruction: str, context: str) -> BackendResult:
        prompt = f"""Return only compact JSON matching this schema:
{{"say":"string","actions":[{{"type":"command","cmd":"string","safety":"read_only_local"}}],"done":false}}

Rules:
- Use actions only for read-only local commands.
- Do not use remote commands.
- Do not propose mutations.
- If no command is needed, actions must be [].

Context:
{context}

Instruction:
{instruction}
"""
        return _run_json_backend(
            [self.claude_bin, "-p", prompt, "--model", self.model],
            timeout=self.timeout,
        )


class CodexAdapter:
    def __init__(self, codex_bin: str, model: str, cwd: str, timeout: int = 60):
        self.codex_bin = codex_bin
        self.model = model
        self.cwd = cwd
        self.timeout = timeout

    def run_turn(self, instruction: str, context: str) -> BackendResult:
        prompt = f"""Return only compact JSON matching this schema:
{{"say":"string","actions":[{{"type":"command","cmd":"string","safety":"read_only_local"}}],"done":false}}

Rules:
- Use actions only for read-only local commands.
- Do not use remote commands.
- Do not propose mutations.
- If no command is needed, actions must be [].

Context:
{context}

Instruction:
{instruction}
"""
        cmd = [
            self.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            self.cwd,
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        cmd.append("-")
        return _run_json_backend(cmd, timeout=self.timeout, stdin=prompt)


def _extract_json(raw: str) -> Optional[dict]:
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            payload = json.loads(raw[start : end + 1])
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
    return None


def _run_json_backend(cmd: list[str], timeout: int, stdin: str | None = None) -> BackendResult:
        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception as exc:
            return BackendResult(False, {}, "", f"{type(exc).__name__}: {exc}")

        raw = (result.stdout or result.stderr or "").strip()
        duration_ms = int((time.monotonic() - start) * 1000)
        payload = _extract_json(raw)
        if payload is None:
            return BackendResult(False, {}, raw, "malformed JSON: no object found", duration_ms)

        if not isinstance(payload, dict) or "say" not in payload or "actions" not in payload:
            return BackendResult(False, {}, raw, "schema validation failed", duration_ms)
        if not isinstance(payload.get("actions"), list):
            return BackendResult(False, {}, raw, "actions must be a list", duration_ms)
        return BackendResult(True, payload, raw, duration_ms=duration_ms)

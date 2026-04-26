#!/usr/bin/env python3
"""Backend adapters for embedded agent runtime."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass


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
        start = time.monotonic()
        try:
            result = subprocess.run(
                [self.claude_bin, "-p", prompt, "--model", self.model],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except Exception as exc:
            return BackendResult(False, {}, "", f"{type(exc).__name__}: {exc}")

        raw = (result.stdout or result.stderr or "").strip()
        duration_ms = int((time.monotonic() - start) * 1000)
        try:
            payload = json.loads(raw)
        except Exception as exc:
            return BackendResult(False, {}, raw, f"malformed JSON: {exc}", duration_ms)

        if not isinstance(payload, dict) or "say" not in payload or "actions" not in payload:
            return BackendResult(False, {}, raw, "schema validation failed", duration_ms)
        if not isinstance(payload.get("actions"), list):
            return BackendResult(False, {}, raw, "actions must be a list", duration_ms)
        return BackendResult(True, payload, raw, duration_ms=duration_ms)


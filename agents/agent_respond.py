#!/usr/bin/env python3
"""Dispatch to the responder backend configured for this agent."""
import json
import os
import subprocess
import sys

CFG = json.load(open(os.path.expanduser("~/agent_config.json")))

BACKEND = CFG.get("responder_backend")
if not BACKEND:
    BACKEND = "codex" if CFG.get("name") == "keystone" else "claude"

SCRIPT_CANDIDATES = {
    "claude": [
        "/home/longshot/Switchboard/agents/agent_respond_claude.py",
        "/mnt/shared/agent_respond_claude.py",
    ],
    "codex": [
        "/home/longshot/Switchboard/agents/agent_respond_codex.py",
        "/mnt/shared/agent_respond_codex.py",
    ],
}

script = CFG.get("responder_script")
if not script:
    for candidate in SCRIPT_CANDIDATES.get(BACKEND, []):
        if os.path.exists(candidate):
            script = candidate
            break
if not script:
    print(f"Unknown responder backend: {BACKEND}", file=sys.stderr)
    sys.exit(2)

result = subprocess.run(["python3", script, *sys.argv[1:]])
sys.exit(result.returncode)

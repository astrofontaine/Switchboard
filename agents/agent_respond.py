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

SCRIPT_MAP = {
    "claude": "/mnt/shared/agent_respond_claude.py",
    "codex": "/mnt/shared/agent_respond_codex.py",
}

script = CFG.get("responder_script") or SCRIPT_MAP.get(BACKEND)
if not script:
    print(f"Unknown responder backend: {BACKEND}", file=sys.stderr)
    sys.exit(2)

result = subprocess.run(["python3", script, *sys.argv[1:]])
sys.exit(result.returncode)

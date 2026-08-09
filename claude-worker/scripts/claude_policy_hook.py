#!/usr/bin/env python3
"""Block subagents, agent teams, auth mutation, and nested harness commands."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict


DENIED_TOOL_NAMES = {
    "agent",
    "task",
    "skill",
    "teamcreate",
    "teamdelete",
    "teammate",
    "sendmessage",
}
HARNESS_COMMAND = re.compile(
    r"(?:^|[;&|()\s])(?:[^\s;&|()]*/)?"
    r"(?:claude|codex|gemini|aider|goose|hermes)(?:\s|$)",
    re.I,
)
AUTH_MUTATION = re.compile(
    r"(?:^|[;&|()\s])(?:[^\s;&|()]*/)?claude\s+"
    r"(?:auth\s+)?(?:login|logout|setup-token|gateway)(?:\s|$)",
    re.I,
)
TEAM_ENABLE = re.compile(r"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS\s*=\s*(?:1|true|yes|on)", re.I)


def append_event(payload: Dict[str, Any]) -> None:
    raw_path = os.environ.get("CLAUDE_WORKER_EVENT_LOG")
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    reason = ""
    if tool_name.lower() in DENIED_TOOL_NAMES:
        reason = "Claude workers may not spawn or message sub-workers"
    elif tool_name.lower() == "bash" and HARNESS_COMMAND.search(command):
        reason = "Claude workers may not invoke an agent harness through Bash"
    elif tool_name.lower() == "bash" and AUTH_MUTATION.search(command):
        reason = "Claude workers may not mutate Claude authentication"
    elif tool_name.lower() == "bash" and TEAM_ENABLE.search(command):
        reason = "Claude Agent Teams are disabled for managed workers"
    if not reason:
        return 0
    append_event(
        {
            "event": "policy_block",
            "tool_name": tool_name,
            "reason": reason,
            "session_id": payload.get("session_id"),
        }
    )
    sys.stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

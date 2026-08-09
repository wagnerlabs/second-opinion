#!/usr/bin/env python3
"""Allow Claude skills while blocking sub-workers and agent harnesses."""

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
    "teamcreate",
    "teamdelete",
    "teammate",
    "sendmessage",
}
HARNESS_COMMAND = re.compile(
    r"(?:^|[;&|()\s])(?:[^\s;&|()]*/)?"
    r"(?P<harness>claude|codex|gemini|aider|goose|hermes)(?:\s|$)",
    re.I,
)
AUTH_MUTATION = re.compile(
    r"(?:^|[;&|()\s])(?:[^\s;&|()]*/)?claude\s+"
    r"(?:auth\s+)?(?:login|logout|setup-token|gateway)(?:\s|$)",
    re.I,
)
TEAM_ENABLE = re.compile(r"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS\s*=\s*(?:1|true|yes|on)", re.I)
CODEX_EXEC = re.compile(
    r"(?:^|[;&|()\s])(?:[^\s;&|()]*/)?codex\s+exec(?:\s|$)",
    re.I,
)


def append_event(payload: Dict[str, Any]) -> None:
    raw_path = os.environ.get("CLAUDE_WORKER_EVENT_LOG")
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def skill_name(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("skill", "name", "command"):
        raw = tool_input.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        return raw.strip().split()[0].lstrip("/$").lower()
    return ""


def is_gpt_second_opinion_command(command: str) -> bool:
    """Recognize the constrained Codex invocation emitted by the installed skill."""
    harnesses = [match.group("harness").lower() for match in HARNESS_COMMAND.finditer(command)]
    if harnesses != ["codex"] or len(CODEX_EXEC.findall(command)) != 1:
        return False
    if any(
        count != 1
        for count in (
            len(re.findall(r"model_reasoning_effort\s*=", command)),
            len(re.findall(r"approval_policy\s*=", command)),
            len(re.findall(r"--sandbox(?:\s|$)", command)),
            len(re.findall(r"--ephemeral(?:\s|\\|$)", command)),
        )
    ):
        return False
    required_patterns = (
        r"(?:^|\s)-m\s+gpt-5\.6-sol(?:\s|\\|$)",
        r"(?:^|\s)-c\s+model_reasoning_effort=max(?:\s|\\|$)",
        r"(?:^|\s)-c\s+approval_policy=never(?:\s|\\|$)",
        r"(?:^|\s)(?:-o|--output-last-message)\s+\S+",
        r"(?:^|\s)<\s+\S+",
    )
    if any(not re.search(pattern, command) for pattern in required_patterns):
        return False
    if (
        "REVIEW STATUS: COMPLETE" not in command
        or "REVIEW STATUS: INCOMPLETE" not in command
        or "danger-full-access" in command
        or "dangerously-bypass-approvals-and-sandbox" in command
    ):
        return False
    literal_sandbox = re.search(
        r"--sandbox\s+(?:read-only|workspace-write)(?:\s|\\|$)",
        command,
    )
    variable_sandbox = re.search(
        r"SANDBOX_MODE\s*=\s*['\"]?(?:read-only|workspace-write)['\"]?",
        command,
    ) and re.search(
        r"--sandbox\s+['\"]?\$\{?SANDBOX_MODE\}?['\"]?(?:\s|\\|$)",
        command,
    )
    return bool(literal_sandbox or variable_sandbox)


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
    elif tool_name.lower() == "skill":
        selected = skill_name(tool_input)
        if selected == "claude-second-opinion":
            reason = "Claude workers must substitute /gpt-second-opinion for /claude-second-opinion"
        else:
            append_event(
                {
                    "event": "skill_invoked",
                    "skill": selected or "unknown",
                    "session_id": payload.get("session_id"),
                }
            )
            return 0
    elif tool_name.lower() == "bash" and AUTH_MUTATION.search(command):
        reason = "Claude workers may not mutate Claude authentication"
    elif tool_name.lower() == "bash" and TEAM_ENABLE.search(command):
        reason = "Claude Agent Teams are disabled for managed workers"
    elif tool_name.lower() == "bash" and HARNESS_COMMAND.search(command):
        if is_gpt_second_opinion_command(command):
            append_event(
                {
                    "event": "gpt_second_opinion_started",
                    "tool_name": tool_name,
                    "session_id": payload.get("session_id"),
                }
            )
            return 0
        reason = "Claude workers may not invoke an agent harness through Bash"
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

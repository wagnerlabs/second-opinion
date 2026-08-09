#!/usr/bin/env python3
"""Codex-owned orchestration for persistent Claude Code workers.

The helper deliberately exposes a worker lifecycle rather than a thin wrapper
around ``claude -p``.  State, process ownership, authority, billing decisions,
message delivery, and result disposition are all explicit and recoverable.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import mmap
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_HOOK = SCRIPT_DIR / "claude_policy_hook.py"
EVENT_HOOK = SCRIPT_DIR / "claude_event_hook.py"
RUNNER = SCRIPT_DIR / "claude_worker_runner.py"
MANIFEST_VERSION = 2
SOL_OPUS_EFFORT_MAP = {"high": "medium", "xhigh": "high", "max": "xhigh"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "opus-5": "claude-opus-5",
    "claude-opus-5": "claude-opus-5",
    "fable": "claude-fable-5",
    "fable-5": "claude-fable-5",
    "claude-fable-5": "claude-fable-5",
}
ACTIVE_STATES = {
    "starting",
    "initializing",
    "running",
    "idle",
    "pausing",
    "resuming",
    "capacity_wait",
}
PAUSED_STATES = {"warm_paused", "cold_paused"}
TERMINAL_STATES = {
    "auth_blocked",
    "model_unavailable",
    "needs_authority_profile",
    "needs_billing_confirmation",
    "protocol_unsupported",
    "subscription_limit",
    "usage_credit_blocked",
    "completed",
    "blocked",
    "failed",
    "stopped",
    "cancelled",
}
FINAL_STATES = TERMINAL_STATES | {"cold_paused"}
DISPOSITIONS = {"pending", "integrated", "rejected", "cancelled", "retained"}
PROHIBITED_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
}
PROHIBITED_SETTING_KEYS = {
    "apikeyhelper",
    "anthropic_api_key",
    "anthropic_auth_token",
    "anthropic_base_url",
    "claude_code_use_bedrock",
    "claude_code_use_vertex",
    "claude_code_use_foundry",
}
DENIED_TOOLS = [
    "Agent",
    "Task",
    "TeamCreate",
    "TeamDelete",
    "Teammate",
    "SendMessage",
    "Skill",
]
READ_TOOLS = ["Read", "Glob", "Grep", "Bash"]
MUTATING_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Bash"]
WEB_TOOLS = ["WebFetch", "WebSearch"]
LIMIT_PATTERNS = [
    re.compile(r"continue with (?:usage|api) credits", re.I),
    re.compile(r"(?:switch|transition|continue).{0,80}(?:usage|api) credits", re.I),
    re.compile(r"extra usage", re.I),
    re.compile(r"(?:usage|session) limit (?:reached|exhausted)", re.I),
    re.compile(r"you(?:'|’)ve reached your .*limit", re.I),
]
MODEL_UNAVAILABLE_PATTERNS = [
    re.compile(r"(?:model|model id).*(?:not available|unavailable|not found|does not exist)", re.I),
    re.compile(r"(?:invalid|unknown|unsupported) model", re.I),
]
WORKLOADS = {
    "light": {"cpu": 0.5, "memory_gib": 4.0, "watchdog_seconds": 600},
    "standard": {"cpu": 1.0, "memory_gib": 8.0, "watchdog_seconds": 1200},
    "heavy": {"cpu": 2.0, "memory_gib": 16.0, "watchdog_seconds": 1800},
}
OPUS_BASELINE = (5, 0)
OPUS_MODEL_PATTERN = re.compile(rb"claude-opus-(\d+)(?:[-.](\d+))?")
PERMANENTLY_CANCELLED = {"cw-20260809-114551-codex-jira-24h-resumed-af35c48a"}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "block"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["command", "exit_code"],
            },
        },
        "blockers": {"type": "array", "items": {"type": "string"}},
        "branch": {"type": ["string", "null"]},
        "worktree": {"type": ["string", "null"]},
        "head": {"type": ["string", "null"]},
        "proposed_subtasks": {"type": "array", "items": {"type": "string"}},
        "lingering_processes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "summary",
        "changed_files",
        "tests",
        "blockers",
        "proposed_subtasks",
        "lingering_processes",
    ],
}


class WorkerError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_root() -> Path:
    override = os.environ.get("CLAUDE_WORKER_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".local" / "state" / "claude-worker"


def workers_root() -> Path:
    return state_root() / "workers"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temp.chmod(0o600)
    os.replace(str(temp), str(path))


def atomic_write_text(path: Path, value: str) -> None:
    ensure_private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temp.write_text(value, encoding="utf-8")
    temp.chmod(0o600)
    os.replace(str(temp), str(path))


@contextlib.contextmanager
def state_lock() -> Iterator[None]:
    root = state_root()
    ensure_private_dir(root)
    lock_path = root / ".lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def emit(value: Any, *, stream: Any = sys.stdout) -> None:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")


def run(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    check: bool = False,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise WorkerError("{}: {}".format(" ".join(argv[:3]), detail))
    return result


def claude_path() -> str:
    override = os.environ.get("CLAUDE_WORKER_CLAUDE")
    value = override or "claude"
    path = value if os.path.isabs(value) else shutil.which(value)
    if not path or not Path(path).is_file() or not os.access(path, os.X_OK):
        raise WorkerError(
            "Claude Code CLI was not found or is not executable; install `claude` on PATH "
            "or set CLAUDE_WORKER_CLAUDE to its executable path"
        )
    return str(Path(path).resolve())


def child_env(event_log: Optional[Path] = None) -> Dict[str, str]:
    env = dict(os.environ)
    for name in PROHIBITED_ENV:
        env.pop(name, None)
    env.pop("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", None)
    env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "0"
    env["CLAUDE_WORKER_MANAGED"] = "1"
    if event_log:
        env["CLAUDE_WORKER_EVENT_LOG"] = str(event_log)
    return env


def opus_model_discovery(executable: str, help_text: str = "") -> Dict[str, Any]:
    raw_matches = set(OPUS_MODEL_PATTERN.findall(help_text.encode("utf-8", errors="ignore")))
    try:
        resolved = Path(executable).resolve()
        if resolved.is_file() and resolved.stat().st_size:
            with resolved.open("rb") as handle:
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as binary:
                    raw_matches.update(OPUS_MODEL_PATTERN.findall(binary))
    except (OSError, ValueError):
        pass
    discovered: Dict[Tuple[int, int], str] = {}
    for major_raw, minor_raw in raw_matches:
        major = int(major_raw)
        minor = int(minor_raw) if minor_raw and len(minor_raw) <= 2 else 0
        label = "claude-opus-{}".format(major)
        if minor:
            label += "-{}".format(minor)
        discovered[(major, minor)] = label
    models = [discovered[key] for key in sorted(discovered)]
    newer = [discovered[key] for key in sorted(discovered) if key > OPUS_BASELINE]
    value: Dict[str, Any] = {
        "baseline": "claude-opus-5",
        "advertised_models": models,
        "newer_models": newer,
    }
    if newer:
        value["notice"] = (
            "The installed Claude Code CLI advertises an Opus model newer than Opus 5: {}. "
            "Tell the human; do not switch unless requested."
        ).format(", ".join(newer))
    return value


def prohibited_environment() -> List[str]:
    return sorted(name for name in PROHIBITED_ENV if os.environ.get(name))


def candidate_settings(cwd: Path) -> List[Path]:
    values = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        Path("/etc/claude-code/managed-settings.json"),
    ]
    return [value for value in values if value.is_file()]


def scan_prohibited_settings(cwd: Path) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []

    def visit(value: Any, path: Path, key_path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                current = "{}.{}".format(key_path, key).strip(".")
                normalized = str(key).replace("-", "_").lower()
                if normalized in PROHIBITED_SETTING_KEYS and child not in (None, "", False, 0):
                    findings.append({"file": str(path), "key": current})
                visit(child, path, current)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path, "{}[{}]".format(key_path, index))

    for path in candidate_settings(cwd):
        try:
            visit(json.loads(path.read_text(encoding="utf-8")), path)
        except (OSError, ValueError) as exc:
            findings.append({"file": str(path), "key": "<unreadable:{}>".format(type(exc).__name__)})
    return findings


def auth_status(cwd: Path) -> Dict[str, Any]:
    result = run([claude_path(), "auth", "status", "--json"], cwd=cwd, timeout=15)
    if result.returncode != 0:
        raise WorkerError("Claude auth status failed: {}".format((result.stderr or result.stdout).strip()))
    try:
        status = json.loads(result.stdout)
    except ValueError as exc:
        raise WorkerError("Claude auth status did not return JSON") from exc
    if not isinstance(status, dict):
        raise WorkerError("Claude auth status returned an unexpected shape")
    return status


def safe_auth_summary(status: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "loggedIn": bool(status.get("loggedIn")),
        "authMethod": status.get("authMethod"),
        "apiProvider": status.get("apiProvider"),
        "subscriptionType": status.get("subscriptionType"),
    }


def validate_subscription_auth(cwd: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    problems: List[Dict[str, Any]] = []
    env_names = prohibited_environment()
    if env_names:
        problems.append({"kind": "prohibited_environment", "names": env_names})
    settings_findings = scan_prohibited_settings(cwd)
    if settings_findings:
        problems.append({"kind": "prohibited_settings", "findings": settings_findings})
    status = auth_status(cwd)
    if not status.get("loggedIn"):
        problems.append({"kind": "not_logged_in"})
    if status.get("authMethod") != "claude.ai":
        problems.append({"kind": "non_subscription_auth", "authMethod": status.get("authMethod")})
    if status.get("apiProvider") != "firstParty":
        problems.append({"kind": "non_first_party_provider", "apiProvider": status.get("apiProvider")})
    if not status.get("subscriptionType"):
        problems.append({"kind": "missing_subscription_type"})
    return status, problems


def billing_cache_snapshot(path: Optional[Path] = None) -> Dict[str, Any]:
    source = path or (Path.home() / ".claude.json")
    allowed = {
        "billingType",
        "hasExtraUsageEnabled",
        "extraUsageReason",
        "extraUsageUpdatedAt",
        "billingUpdatedAt",
        "lastUpdatedAt",
    }
    result: Dict[str, Any] = {
        "state": "unknown",
        "source": str(source),
        "freshness": "unknown",
        "fields": {},
        "advisory": True,
    }
    if not source.is_file():
        result["reason"] = "cache_missing"
        return result
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result["reason"] = "cache_unreadable"
        return result
    if not isinstance(raw, dict):
        result["reason"] = "cache_shape"
        return result
    fields = {key: raw.get(key) for key in allowed if key in raw}
    result["fields"] = fields
    stamp = fields.get("extraUsageUpdatedAt") or fields.get("billingUpdatedAt") or fields.get("lastUpdatedAt")
    if isinstance(stamp, str):
        try:
            age = (dt.datetime.now(dt.timezone.utc) - parse_time(stamp)).total_seconds()
            result["freshness"] = "fresh" if age <= 86400 else "stale"
            result["age_seconds"] = max(0, int(age))
        except ValueError:
            result["freshness"] = "unknown"
    if "billingType" in fields or "hasExtraUsageEnabled" in fields:
        result["state"] = "known" if result["freshness"] != "stale" else "unknown"
    else:
        result["reason"] = "fields_missing"
    return result


def require_ready_billing(cwd: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    status, problems = validate_subscription_auth(cwd)
    if problems:
        raise WorkerError("Subscription authentication is blocked: {}".format(json.dumps(problems)))
    return status, billing_cache_snapshot()


def model_subscription_eligibility(model: str) -> str:
    # Opus 5 is the skill's subscription-tested baseline. Unknown models require
    # an explicit per-worker human decision; binary string discovery is not proof.
    return "eligible" if model == "claude-opus-5" else "unknown"


def billing_decision(
    worker_id: str,
    model: str,
    effort: str,
    allow_usage_credits: bool,
    authorization_source: Optional[str],
) -> Dict[str, Any]:
    eligibility = model_subscription_eligibility(model)
    if eligibility == "eligible":
        return {"eligibility": eligibility, "decision": "subscription_only", "at": utc_now()}
    if not allow_usage_credits:
        raise WorkerError(
            "needs_billing_confirmation: model eligibility is unknown; ask the human to switch, "
            "cancel, or explicitly authorize usage credits for this worker"
        )
    if not authorization_source or not authorization_source.strip():
        raise WorkerError("--usage-credit-authorization is required with --allow-usage-credits")
    return {
        "eligibility": eligibility,
        "decision": "per_worker_usage_credit_exception",
        "worker_id": worker_id,
        "model": model,
        "effort": effort,
        "authorized_at": utc_now(),
        "authorization_source": authorization_source.strip(),
    }


def sysctl_int(name: str, fallback: int) -> int:
    try:
        result = run(["/usr/sbin/sysctl", "-n", name], timeout=5)
        if result.returncode == 0:
            return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return fallback


def available_memory_bytes(total: int) -> int:
    executable = shutil.which("vm_stat")
    if not executable:
        return total
    result = run([executable], timeout=5)
    if result.returncode != 0:
        return total
    page_size = 4096
    match = re.search(r"page size of (\d+) bytes", result.stdout)
    if match:
        page_size = int(match.group(1))
    pages = 0
    wanted = {"Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key.strip() in wanted:
            number = re.sub(r"\D", "", raw)
            if number:
                pages += int(number)
    return pages * page_size if pages else total


def machine_snapshot(cwd: Path) -> Dict[str, Any]:
    logical = os.cpu_count() or 1
    performance = sysctl_int("hw.perflevel0.physicalcpu", max(1, logical // 2))
    total_memory = sysctl_int("hw.memsize", 8 * 1024**3)
    available_memory = available_memory_bytes(total_memory)
    load1, load5, load15 = os.getloadavg()
    disk = shutil.disk_usage(str(cwd))
    return {
        "performance_cores": performance,
        "logical_cores": logical,
        "total_memory_bytes": total_memory,
        "available_memory_bytes": available_memory,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "disk_free_bytes": disk.free,
    }


def manifest_path(worker_id: str) -> Path:
    if not re.fullmatch(r"cw-[a-z0-9-]+", worker_id):
        raise WorkerError("Invalid Claude worker ID")
    return workers_root() / worker_id / "manifest.json"


def _migrate_manifest(value: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    changed = False
    if int(value.get("manifest_version", 1)) < MANIFEST_VERSION:
        value["manifest_version"] = MANIFEST_VERSION
        value.setdefault("attempt", 0)
        value.setdefault("attempt_history", [])
        value.setdefault("messages", [])
        value.setdefault("permission_requests", {})
        value.setdefault("result_disposition", "pending")
        value.setdefault("session_registered", bool(value.get("turns_completed", 0)))
        value.setdefault("migration", {})
        changed = True
    worker_id = str(value.get("worker_id") or "")
    disposition = str(value.get("result_disposition") or "pending")
    terminal_receipt = bool(value.get("result_path") and Path(str(value["result_path"])).is_file())
    explicitly_final = disposition in {"rejected", "cancelled", "integrated", "retained"}
    superseded = bool(value.get("superseded_by") or value.get("supersedes"))
    if worker_id in PERMANENTLY_CANCELLED:
        if value.get("state") != "cancelled" or disposition != "cancelled":
            value["state"] = "cancelled"
            value["result_disposition"] = "cancelled"
            value["failure_reason"] = "permanently cancelled rejected replacement lane"
            changed = True
    elif value.get("state") == "stopped" and not (terminal_receipt or explicitly_final or superseded):
        value["state"] = "cold_paused"
        value.setdefault("blocker", {"kind": "legacy_pause", "detail": "migrated from legacy stopped"})
        changed = True
    if changed:
        value.setdefault("migration", {})["v2_at"] = value.get("migration", {}).get("v2_at") or utc_now()
    return value, changed


def load_manifest(worker_id: str) -> Dict[str, Any]:
    path = manifest_path(worker_id)
    if not path.is_file():
        raise WorkerError("Unknown Claude worker: {}".format(worker_id))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("worker_id") != worker_id:
        raise WorkerError("Invalid manifest for {}".format(worker_id))
    value, changed = _migrate_manifest(value)
    if changed:
        atomic_write_json(path, value)
    return value


def save_manifest(manifest: Dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(manifest_path(str(manifest["worker_id"])), manifest)


def all_manifests() -> List[Dict[str, Any]]:
    root = workers_root()
    if not root.is_dir():
        return []
    values: List[Dict[str, Any]] = []
    for path in sorted(root.glob("cw-*/manifest.json")):
        try:
            values.append(load_manifest(path.parent.name))
        except (OSError, ValueError, WorkerError):
            continue
    return values


def process_alive(pid: Any) -> bool:
    try:
        numeric = int(pid)
        if numeric <= 1:
            return False
        os.kill(numeric, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def process_identity(pid: int) -> Dict[str, Any]:
    result = run(["ps", "-o", "pgid=,lstart=", "-p", str(pid)], timeout=5)
    raw = result.stdout.strip() if result.returncode == 0 else ""
    parts = raw.split(None, 1)
    return {
        "pid": pid,
        "pgid": int(parts[0]) if parts and parts[0].isdigit() else None,
        "start_identity": parts[1] if len(parts) > 1 else None,
    }


def active_claude_weight() -> Tuple[float, int]:
    cpu = 0.0
    count = 0
    for manifest in all_manifests():
        state = str(manifest.get("state"))
        if state not in ACTIVE_STATES and state != "warm_paused":
            continue
        profile = WORKLOADS.get(str(manifest.get("workload")), WORKLOADS["standard"])
        multiplier = 0.25 if state == "warm_paused" else 1.0
        cpu += float(profile["cpu"]) * multiplier
        count += 1
    return cpu, count


def calculate_capacity(
    snapshot: Dict[str, Any],
    *,
    native_active: int,
    workload: str,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    profile = WORKLOADS[workload]
    active_cpu, active_claude = active_claude_weight()
    performance = max(1, int(snapshot["performance_cores"]))
    logical = max(1, int(snapshot["logical_cores"]))
    cpu_budget = performance * 0.8
    by_cpu = max(0, int(math.floor((cpu_budget - active_cpu - float(native_active)) / float(profile["cpu"]))))
    reserve_memory = int(snapshot["total_memory_bytes"] * 0.2)
    memory_headroom = max(0, int(snapshot["available_memory_bytes"]) - reserve_memory)
    by_memory = max(0, int(memory_headroom // int(float(profile["memory_gib"]) * 1024**3)))
    absolute_ceiling = max(1, int(math.floor(performance * 0.8)))
    current_count = active_claude + native_active
    by_absolute = max(0, absolute_ceiling - current_count)
    safe = min(by_cpu, by_memory, by_absolute)
    gates: List[str] = []
    load_ratio = float(snapshot["load1"]) / float(logical)
    if load_ratio >= 0.9:
        safe = 0
        gates.append("critical_load")
    elif load_ratio >= 0.7:
        safe = min(safe, 1)
        gates.append("elevated_load")
    disk_gib = float(snapshot["disk_free_bytes"]) / 1024**3
    if disk_gib < 10:
        safe = 0
        gates.append("critical_disk")
    elif disk_gib < 25:
        safe = min(safe, 1)
        gates.append("low_disk")
    if max_workers is not None:
        safe = min(safe, max(0, max_workers - current_count))
        gates.append("human_ceiling")
    safe = min(safe, 2)
    return {
        "safe_additional_this_wave": max(0, safe),
        "native_active": native_active,
        "active_claude": active_claude,
        "active_claude_weight": active_cpu,
        "workload": workload,
        "profile": profile,
        "limits": {"cpu": by_cpu, "memory": by_memory, "absolute": by_absolute, "wave": 2},
        "gates": gates,
        "machine": snapshot,
        "recorded_at": utc_now(),
    }


def contains_subscription_limit(text: str) -> bool:
    return any(pattern.search(text) for pattern in LIMIT_PATTERNS)


def contains_model_unavailable(text: str) -> bool:
    return any(pattern.search(text) for pattern in MODEL_UNAVAILABLE_PATTERNS)


def socket_path_for(worker_id: str) -> Path:
    root = Path("/tmp") / "claude-worker-{}".format(os.getuid())
    ensure_private_dir(root)
    name = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:16] + ".sock"
    path = root / name
    if len(os.fsencode(str(path))) > 100:
        raise WorkerError("Claude worker control socket exceeds the 100-byte portability limit")
    return path


def ipc_request(manifest: Dict[str, Any], action: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
    path = Path(str(manifest.get("control_socket") or socket_path_for(str(manifest["worker_id"]))))
    request = {
        "action": action,
        "owner_nonce": manifest.get("owner_nonce"),
        "request_id": uuid.uuid4().hex,
        "payload": payload or {},
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall((json.dumps(request, sort_keys=True) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise WorkerError("Claude worker control channel is unavailable: {}".format(exc)) from exc
    finally:
        client.close()
    try:
        response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, IndexError) as exc:
        raise WorkerError("Claude worker control channel returned an invalid response") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        raise WorkerError(str(response.get("error") if isinstance(response, dict) else response))
    return response


def worker_contract(
    worker_id: str,
    scope: str,
    owned_paths: Sequence[str],
    authority: Optional[Dict[str, Any]] = None,
) -> str:
    ownership = ", ".join(owned_paths) if owned_paths else "the declared task scope"
    codex = (authority or {}).get("codex", {})
    authority_receipt = (
        "Effective authority receipt: approval_policy={approval}; "
        "filesystem_sandbox={sandbox}; network={network}; task_scope={scope}."
    ).format(
        approval=codex.get("approval_policy", "unknown"),
        sandbox=codex.get("sandbox_mode", "unknown"),
        network=codex.get("network", "unknown"),
        scope=scope,
    )
    return """You are Claude worker {worker_id}, a top-level lane owned by a Codex orchestrator.
Work only on {ownership}. Scope class: {scope}.
{authority_receipt}
Do not invoke Claude, Codex, Agent/Task/Team/Skill tools, slash commands, or any other agent harness. Do not create sub-workers.
Do not change authentication, billing, providers, Claude settings, or usage-credit preferences.
Do not accept any offer to continue with API credits, usage credits, or extra usage. Stop and report SUBSCRIPTION_LIMIT instead unless the task packet records a human-authorized per-worker exception.
Follow the declared filesystem, network, tool, and external-action authority exactly. Report unavailable capabilities as blockers; never bypass controls.
Return only the requested structured result. Include changed files, tests and exit codes, blockers, branch/worktree/HEAD, proposed subtasks, and lingering processes. Do not leave background processes running.
""".format(
        worker_id=worker_id,
        ownership=ownership,
        scope=scope,
        authority_receipt=authority_receipt,
    )


def authority_profile(approval: Optional[str], sandbox_mode: Optional[str], network: Optional[str]) -> Dict[str, Any]:
    if not approval or not sandbox_mode or not network:
        raise WorkerError("needs_authority_profile: pass effective Codex approval, sandbox, and network settings")
    mediated = approval != "never"
    mutable = sandbox_mode != "read-only"
    tools = list(MUTATING_TOOLS if mutable else READ_TOOLS)
    if network == "enabled":
        tools.extend(WEB_TOOLS)
    if sandbox_mode == "danger-full-access":
        profile = "unrestricted_no_approvals" if not mediated else "unrestricted_mediated"
        claude_sandbox = {"enabled": False}
    elif sandbox_mode == "workspace-write":
        profile = "workspace_write_no_approvals" if not mediated else "workspace_write_mediated"
        claude_sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True}
    else:
        profile = "read_only_no_approvals" if not mediated else "read_only_mediated"
        claude_sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False}
    return {
        "codex": {"approval_policy": approval, "sandbox_mode": sandbox_mode, "network": network},
        "profile": profile,
        "mediated": mediated,
        "permission_mode": "manual" if mediated else "dontAsk",
        "tools": tools,
        "preapproved_tools": [] if mediated else list(tools),
        "claude_sandbox": claude_sandbox,
        "network_enabled": network == "enabled",
        "bypass_permissions": False,
    }


def apply_task_scope(authority: Dict[str, Any], scope: str, cwd: Path) -> Dict[str, Any]:
    value = json.loads(json.dumps(authority))
    value["task_scope"] = scope
    if scope == "read-only":
        mutations = {"Edit", "Write", "NotebookEdit"}
        value["tools"] = [tool for tool in value["tools"] if tool not in mutations]
        value["preapproved_tools"] = [
            tool for tool in value["preapproved_tools"] if tool not in mutations
        ]
        value["claude_sandbox"] = {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
        }
        value["task_write_boundary"] = {"mode": "deny", "paths": [str(cwd)]}
    return value


def hook_settings(event_log: Path, authority: Dict[str, Any], cwd: Path, add_dirs: Sequence[str]) -> Dict[str, Any]:
    policy = "{} {}".format(sys.executable, POLICY_HOOK)
    notify = "{} {} --event notification".format(sys.executable, EVENT_HOOK)
    stop = "{} {} --event stop".format(sys.executable, EVENT_HOOK)
    sandbox = dict(authority["claude_sandbox"])
    if sandbox.get("enabled"):
        task_read_only = authority.get("task_scope") == "read-only"
        sandbox["filesystem"] = {
            "allowWrite": [str(cwd)] + list(add_dirs) if authority["codex"]["sandbox_mode"] == "workspace-write" and not task_read_only else [],
            "denyWrite": [str(cwd)] if authority["codex"]["sandbox_mode"] == "read-only" or task_read_only else [],
        }
        sandbox["network"] = {"allowedDomains": ["*"] if authority["network_enabled"] else []}
    return {
        "permissions": {"allow": authority["preapproved_tools"], "deny": DENIED_TOOLS},
        "sandbox": sandbox,
        "disableAllHooks": False,
        "hooks": {
            "PreToolUse": [{"matcher": "Bash|Agent|Task|TeamCreate|TeamDelete|Teammate|SendMessage|Skill", "hooks": [{"type": "command", "command": policy}]}],
            "Notification": [{"matcher": "", "hooks": [{"type": "command", "command": notify}]}],
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": stop}]}],
        },
        "env": {"CLAUDE_WORKER_EVENT_LOG": str(event_log), "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"},
    }


def normalize_model(value: Optional[str]) -> str:
    if not value:
        return "claude-opus-5"
    normalized = MODEL_ALIASES.get(value.lower(), value)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise WorkerError("Model identifiers may contain only letters, numbers, dot, underscore, and hyphen")
    return normalized


def resolve_effort(codex_model: Optional[str], codex_effort: Optional[str], claude_model: str, explicit_effort: Optional[str]) -> str:
    if explicit_effort:
        if explicit_effort not in EFFORTS:
            raise WorkerError("Unsupported Claude effort: {}".format(explicit_effort))
        return explicit_effort
    if claude_model != "claude-opus-5":
        raise WorkerError("A non-Opus model was requested without an effort; ask the human for the Claude effort")
    if codex_model != "gpt-5.6-sol" or not codex_effort or codex_effort not in SOL_OPUS_EFFORT_MAP:
        raise WorkerError("No automatic effort mapping exists for this Codex model/effort; ask the human for --effort")
    return SOL_OPUS_EFFORT_MAP[codex_effort]


def safe_owned_paths(cwd: Path, values: Sequence[str]) -> List[str]:
    safe: List[str] = []
    for raw in values:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise WorkerError("Owned paths must be relative paths inside the task repository")
        resolved = (cwd / path).resolve(strict=False)
        try:
            resolved.relative_to(cwd.resolve())
        except ValueError as exc:
            raise WorkerError("Owned path escapes the task repository") from exc
        safe.append(str(path))
    return safe


def choose_isolation(cwd: Path, scope: str, isolation: str, owned_paths: Sequence[str]) -> str:
    selected = isolation
    if selected == "auto":
        selected = "worktree" if scope == "mutable-overlap" else "shared"
    if selected == "shared" and scope == "mutable-disjoint" and not owned_paths:
        raise WorkerError("Shared mutable-disjoint work requires at least one --owned-path")
    if selected == "shared" and scope == "mutable-overlap":
        raise WorkerError("Overlapping mutable work cannot use a shared workspace")
    if selected == "worktree":
        result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=10)
        if result.returncode != 0:
            raise WorkerError("Unsafe overlap requires a Git worktree, but this is not a Git repository")
    return selected


def new_worker_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "lane"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "cw-{}-{}-{}".format(stamp, slug, uuid.uuid4().hex[:8])


def build_stream_command(manifest: Dict[str, Any], *, resume: bool) -> List[str]:
    authority = manifest["authority"]
    command = [
        claude_path(),
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--replay-user-messages",
        "--verbose",
        "--include-hook-events",
        "--model", str(manifest["model"]),
        "--effort", str(manifest["effort"]),
        "--permission-mode", str(authority["permission_mode"]),
        "--settings", str(manifest["settings_path"]),
        "--setting-sources", "",
        "--strict-mcp-config",
        "--mcp-config", str(manifest["mcp_path"]),
        "--no-chrome",
        "--disable-slash-commands",
        "--tools", ",".join(authority["tools"]),
        "--disallowedTools", ",".join(DENIED_TOOLS),
        "--json-schema", json.dumps(RESULT_SCHEMA, separators=(",", ":"), sort_keys=True),
        "--append-system-prompt", worker_contract(
            str(manifest["worker_id"]),
            str(manifest["scope"]),
            manifest.get("owned_paths", []),
            manifest.get("authority"),
        ),
        "--name", str(manifest["worker_id"]),
    ]
    if authority["preapproved_tools"]:
        command.extend(["--allowedTools", ",".join(authority["preapproved_tools"])])
    for path in manifest.get("additional_directories", []):
        command.extend(["--add-dir", str(path)])
    if manifest["isolation"] == "worktree" and not resume:
        command.extend(["--worktree", str(manifest["worker_id"])])
    if resume:
        command.extend(["--resume", str(manifest["session_id"])])
    else:
        command.extend(["--session-id", str(manifest["session_id"])])
    return command


def task_message(text: str, *, kind: str = "task", source: str = "parent") -> Dict[str, Any]:
    return {
        "message_id": uuid.uuid4().hex,
        "kind": kind,
        "source": source,
        "digest": digest_text(text),
        "text": text,
        "status": "queued",
        "created_at": utc_now(),
        "acknowledged_at": None,
    }


def reconcile_manifest(manifest: Dict[str, Any], _unused: Any = None) -> Dict[str, Any]:
    state = str(manifest.get("state"))
    runner_alive = process_alive(manifest.get("runner_pid"))
    manifest["process"] = {
        "runner_alive": runner_alive,
        "runner_pid": manifest.get("runner_pid"),
        "child_pid": manifest.get("child_pid"),
        "child_alive": process_alive(manifest.get("child_pid")),
        "identity": manifest.get("process_identity"),
    }
    if state in ACTIVE_STATES | {"warm_paused"} and not runner_alive:
        if manifest.get("session_registered"):
            manifest["state"] = "cold_paused"
            manifest["blocker"] = {"kind": "supervisor_missing", "detail": "session preserved for recovery"}
        else:
            manifest["state"] = "failed"
            manifest["failure_reason"] = "supervisor exited before session registration"
    recent = ""
    for key in ("output_path", "stderr_path"):
        raw = manifest.get(key)
        if raw and Path(str(raw)).is_file():
            try:
                recent += Path(str(raw)).read_text(encoding="utf-8", errors="replace")[-20000:]
            except OSError:
                pass
    if contains_subscription_limit(recent) and manifest.get("state") not in {"stopped", "cancelled"}:
        manifest["state"] = "subscription_limit"
        manifest["blocker"] = {"kind": "billing", "detail": "usage-credit transition or subscription limit signal"}
    save_manifest(manifest)
    return manifest


def start_runner(manifest: Dict[str, Any]) -> int:
    runner_log = Path(str(manifest["runner_log_path"]))
    ensure_private_dir(runner_log.parent)
    with runner_log.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), "--worker-id", str(manifest["worker_id"])],
            cwd=str(manifest["cwd"]),
            env=child_env(Path(str(manifest["event_log"]))),
            stdout=log,
            stderr=log,
            text=True,
            start_new_session=True,
        )
    manifest["runner_pid"] = process.pid
    manifest["process_identity"] = process_identity(process.pid)
    manifest["state"] = "starting"
    manifest["protocol_status"] = "pending"
    manifest["control_socket_ready"] = False
    manifest["runner_started_at"] = utc_now()
    save_manifest(manifest)
    return process.pid


def wait_for_initialization(worker_id: str, timeout: float = 20.0) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manifest = load_manifest(worker_id)
        if manifest.get("protocol_status") == "ready" or manifest.get("state") in TERMINAL_STATES:
            return reconcile_manifest(manifest)
        time.sleep(0.05)
    manifest = reconcile_manifest(load_manifest(worker_id))
    if manifest.get("protocol_status") != "ready":
        raise WorkerError("Claude worker initialization did not complete within {:.0f}s".format(timeout))
    return manifest


def command_doctor(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    checks: Dict[str, Any] = {"cwd": str(cwd)}
    problems: List[Dict[str, Any]] = []
    try:
        executable = claude_path()
        version = run([executable, "--version"], cwd=cwd, timeout=10, check=True).stdout.strip()
        help_text = run([executable, "--help"], cwd=cwd, timeout=10, check=True).stdout
        required = ["--print", "stream-json", "--session-id", "--resume", "--model", "--effort", "--input-format", "--replay-user-messages", "--strict-mcp-config", "--setting-sources"]
        missing = [feature for feature in required if feature not in help_text]
        checks.update({"claude": executable, "version": version, "missing_features": missing, "opus_model_discovery": opus_model_discovery(executable, help_text)})
        if missing:
            problems.append({"kind": "missing_cli_features", "features": missing})
    except (WorkerError, OSError, subprocess.SubprocessError) as exc:
        checks["claude"] = None
        problems.append({"kind": "cli", "detail": str(exc)})
    if checks.get("claude"):
        try:
            status, auth_problems = validate_subscription_auth(cwd)
            checks["auth"] = safe_auth_summary(status)
            checks["billing"] = billing_cache_snapshot()
            problems.extend(auth_problems)
        except (WorkerError, OSError, subprocess.SubprocessError) as exc:
            problems.append({"kind": "auth", "detail": str(exc)})
    checks.update({"policy_hook": POLICY_HOOK.is_file(), "event_hook": EVENT_HOOK.is_file(), "runner": RUNNER.is_file(), "machine": machine_snapshot(cwd)})
    if args.reconcile:
        checks["reconciled"] = [reconcile_manifest(value)["worker_id"] for value in all_manifests()]
    notices: List[str] = []
    discovery = checks.get("opus_model_discovery")
    if isinstance(discovery, dict) and discovery.get("notice"):
        notices.append(str(discovery["notice"]))
    result = {"ok": not problems, "ready_to_spawn": not problems, "checks": checks, "problems": problems, "notices": notices}
    emit(result)
    return 0 if not problems else 2


def command_capacity(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    result = calculate_capacity(machine_snapshot(cwd), native_active=args.native_active, workload=args.workload, max_workers=args.max_workers)
    emit(result)
    return 0 if result["safe_additional_this_wave"] > 0 else 3


def read_task(args: argparse.Namespace) -> str:
    value = args.task if args.task else (sys.stdin.read() if not sys.stdin.isatty() else "")
    if not value.strip():
        raise WorkerError("Provide --task or pipe the worker task on stdin")
    return value.strip()


def command_spawn(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise WorkerError("Task working directory does not exist")
    if args.activation == "maximal" and args.native_free_slots > 0:
        raise WorkerError("Native Codex slots must be filled before maximal-parallelism Claude workers")
    auth, cache = require_ready_billing(cwd)
    executable = claude_path()
    help_text = run([executable, "--help"], cwd=cwd, timeout=10, check=True).stdout
    discovery = opus_model_discovery(executable, help_text)
    model = normalize_model(args.model)
    effort = resolve_effort(args.codex_model, args.codex_effort, model, args.effort)
    authority = apply_task_scope(
        authority_profile(args.codex_approval_policy, args.codex_sandbox, args.network),
        args.scope,
        cwd,
    )
    owned_paths = safe_owned_paths(cwd, args.owned_path)
    isolation = choose_isolation(cwd, args.scope, args.isolation, owned_paths)
    task = read_task(args)
    worker_id = new_worker_id(args.name)
    decision = billing_decision(worker_id, model, effort, args.allow_usage_credits, args.usage_credit_authorization)
    with state_lock():
        capacity = calculate_capacity(machine_snapshot(cwd), native_active=args.native_active, workload=args.workload, max_workers=args.max_workers)
        if capacity["safe_additional_this_wave"] < 1:
            raise WorkerError("Machine-aware capacity guard refused another worker: {}".format(capacity))
        worker_dir = workers_root() / worker_id
        ensure_private_dir(worker_dir)
        paths = {
            "event_log": worker_dir / "events.jsonl",
            "settings_path": worker_dir / "settings.json",
            "mcp_path": worker_dir / "mcp.json",
            "task_path": worker_dir / "task.txt",
            "output_path": worker_dir / "output.jsonl",
            "stderr_path": worker_dir / "stderr.log",
            "runner_log_path": worker_dir / "runner.log",
            "result_path": worker_dir / "result.json",
        }
        control_socket = socket_path_for(worker_id)
        atomic_write_json(paths["settings_path"], hook_settings(paths["event_log"], authority, cwd, args.add_dir))
        atomic_write_json(paths["mcp_path"], {"mcpServers": {}})
        atomic_write_text(paths["task_path"], task + "\n")
        message = task_message(task)
        manifest: Dict[str, Any] = {
            "manifest_version": MANIFEST_VERSION,
            "worker_id": worker_id,
            "session_id": str(uuid.uuid4()),
            "session_registered": False,
            "name": args.name,
            "cwd": str(cwd),
            "workspace": {"mode": isolation, "cwd": str(cwd), "worktree": None},
            "activation": args.activation,
            "native_occupancy_receipt": {"active": args.native_active, "free_slots": args.native_free_slots, "at": utc_now()},
            "model": model,
            "effort": effort,
            "codex_model": args.codex_model,
            "codex_effort": args.codex_effort,
            "workload": args.workload,
            "scope": args.scope,
            "owned_paths": owned_paths,
            "isolation": isolation,
            "additional_directories": [str(Path(value).expanduser().resolve()) for value in args.add_dir],
            "authority": authority,
            "state": "starting",
            "protocol_status": "pending",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "task_digest": digest_text(task),
            "context_digest": digest_text(json.dumps({"cwd": str(cwd), "scope": args.scope, "owned": owned_paths, "authority": authority}, sort_keys=True)),
            "messages": [message],
            "permission_requests": {},
            "attempt": 0,
            "attempt_history": [],
            "owner_nonce": uuid.uuid4().hex,
            "control_socket": str(control_socket),
            "billing": {"auth": safe_auth_summary(auth), "cache": cache, "decision": decision, "best_effort_zero_credit": True},
            "capacity_at_spawn": capacity,
            "result_disposition": "pending",
            "blocker": None,
            "notices": [discovery["notice"]] if discovery.get("notice") else [],
        }
        manifest.update({key: str(value) for key, value in paths.items()})
        save_manifest(manifest)
        manifest["launch_argv"] = build_stream_command(manifest, resume=False)[1:]
        save_manifest(manifest)
        start_runner(manifest)
    initialized = wait_for_initialization(worker_id, timeout=args.initialization_timeout)
    emit(initialized)
    return 0 if initialized.get("protocol_status") == "ready" else 5


def command_list(_args: argparse.Namespace) -> int:
    emit([reconcile_manifest(value) for value in all_manifests()])
    return 0


def command_status(args: argparse.Namespace) -> int:
    value = reconcile_manifest(load_manifest(args.worker_id))
    emit(value)
    return 0 if value.get("state") not in {"failed", "auth_blocked", "model_unavailable", "protocol_unsupported"} else 5


def _redact(value: Any) -> Any:
    sensitive = {"content", "text", "thinking", "result", "summary", "structured_output", "input", "authorization_source"}
    if isinstance(value, dict):
        return {key: "<redacted>" if key.lower() in sensitive else _redact(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


def command_logs(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.worker_id)
    records: List[Any] = []
    for key in ("output_path", "stderr_path", "runner_log_path", "event_log"):
        path = Path(str(manifest[key]))
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-args.tail:]:
            if args.raw:
                records.append({"source": key, "record": line})
            else:
                try:
                    records.append({"source": key, "record": _redact(json.loads(line))})
                except ValueError:
                    records.append({"source": key, "record": "<redacted non-JSON log line>"})
    emit(records)
    return 0


def command_wait(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
    while True:
        manifest = reconcile_manifest(load_manifest(args.worker_id))
        state = str(manifest.get("state"))
        if state in TERMINAL_STATES:
            emit(manifest)
            return 0 if state == "completed" else 5
        if deadline is not None and time.monotonic() >= deadline:
            emit({"worker_id": args.worker_id, "state": state, "timeout": True})
            return 124
        time.sleep(args.poll_interval)


def command_attach(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.worker_id)
    path = Path(str(manifest["output_path"]))
    shown = 0
    while True:
        if path.is_file():
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(shown)
                for line in handle:
                    if args.raw:
                        print(line.rstrip(), flush=True)
                    else:
                        try:
                            emit(_redact(json.loads(line)))
                        except ValueError:
                            print("<redacted non-JSON event>", flush=True)
                shown = handle.tell()
        manifest = reconcile_manifest(load_manifest(args.worker_id))
        if manifest.get("state") in TERMINAL_STATES:
            return 0 if manifest.get("state") == "completed" else 5
        time.sleep(args.poll_interval)


def read_message(args: argparse.Namespace) -> str:
    value = args.message if args.message else (sys.stdin.read() if not sys.stdin.isatty() else "")
    if not value.strip():
        raise WorkerError("Provide --message or pipe a message on stdin")
    return value.strip()


def queue_message(worker_id: str, text: str, kind: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    with state_lock():
        manifest = load_manifest(worker_id)
        if manifest.get("state") in {"stopped", "cancelled"}:
            raise WorkerError("A permanently stopped worker cannot receive messages")
        message = task_message(text, kind=kind)
        manifest.setdefault("messages", []).append(message)
        save_manifest(manifest)
    return manifest, message


def command_send(args: argparse.Namespace) -> int:
    manifest, message = queue_message(args.worker_id, read_message(args), "send")
    state = str(manifest.get("state"))
    if process_alive(manifest.get("runner_pid")) and state in ACTIVE_STATES | {"warm_paused"}:
        receipt = ipc_request(manifest, "send", {"message_id": message["message_id"]})
    else:
        receipt = {"ok": True, "queued": True, "activated": False}
    emit({"worker_id": args.worker_id, "message": message, "receipt": receipt})
    return 0


def capacity_for_resume(manifest: Dict[str, Any], native_active: int, max_workers: Optional[int], retry_seconds: float, retry_interval: float) -> Dict[str, Any]:
    deadline = time.monotonic() + retry_seconds
    while True:
        capacity = calculate_capacity(machine_snapshot(Path(str(manifest["cwd"]))), native_active=native_active, workload=str(manifest.get("workload", "standard")), max_workers=max_workers)
        if capacity["safe_additional_this_wave"] >= 1:
            return capacity
        if time.monotonic() >= deadline:
            manifest["state"] = "blocked"
            manifest["blocker"] = {"kind": "capacity", "detail": "resume capacity unavailable after retries", "capacity": capacity}
            save_manifest(manifest)
            raise WorkerError("blocked: capacity")
        manifest["state"] = "capacity_wait"
        manifest["blocker"] = {"kind": "capacity", "capacity": capacity, "retry_at": utc_now()}
        save_manifest(manifest)
        time.sleep(min(retry_interval, max(0.01, deadline - time.monotonic())))


def activate_manifest(manifest: Dict[str, Any], args: argparse.Namespace, *, continuation: bool) -> Dict[str, Any]:
    if manifest.get("state") == "cold_paused" and process_alive(manifest.get("runner_pid")):
        deadline = time.monotonic() + 5.0
        while process_alive(manifest.get("runner_pid")) and time.monotonic() < deadline:
            time.sleep(0.05)
        manifest = load_manifest(str(manifest["worker_id"]))
    if process_alive(manifest.get("runner_pid")):
        return ipc_request(manifest, "resume" if continuation else "activate", {})
    require_ready_billing(Path(str(manifest["cwd"])))
    capacity = capacity_for_resume(manifest, args.native_active, args.max_workers, args.capacity_retry_seconds, args.capacity_retry_interval)
    manifest["capacity_at_resume"] = capacity
    manifest["state"] = "resuming"
    save_manifest(manifest)
    start_runner(manifest)
    value = wait_for_initialization(str(manifest["worker_id"]), timeout=args.initialization_timeout)
    if continuation and value.get("protocol_status") == "ready":
        return ipc_request(value, "resume", {})
    return {"ok": value.get("protocol_status") == "ready", "activated": True, "state": value.get("state")}


def command_followup(args: argparse.Namespace) -> int:
    manifest, message = queue_message(args.worker_id, read_message(args), "followup")
    receipt = activate_manifest(manifest, args, continuation=False)
    emit({"worker_id": args.worker_id, "message": message, "receipt": receipt})
    return 0


def command_pause(args: argparse.Namespace) -> int:
    manifest = reconcile_manifest(load_manifest(args.worker_id))
    if manifest.get("state") in PAUSED_STATES:
        emit(manifest)
        return 0
    if not process_alive(manifest.get("runner_pid")):
        if manifest.get("session_registered"):
            manifest["state"] = "cold_paused"
            save_manifest(manifest)
            emit(manifest)
            return 0
        raise WorkerError("Worker has no resumable registered session")
    receipt = ipc_request(manifest, "pause", {"warm_seconds": args.warm_seconds}, timeout=15)
    emit(receipt)
    return 0


def command_resume(args: argparse.Namespace) -> int:
    manifest = reconcile_manifest(load_manifest(args.worker_id))
    if manifest.get("state") in {"stopped", "cancelled"}:
        raise WorkerError("A permanently stopped worker cannot be resumed")
    receipt = activate_manifest(manifest, args, continuation=True)
    emit({"worker_id": args.worker_id, "receipt": receipt, "session_id": manifest.get("session_id")})
    return 0


def command_stop(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.worker_id)
    if process_alive(manifest.get("runner_pid")):
        try:
            ipc_request(manifest, "stop", {}, timeout=10)
        except WorkerError:
            identity = manifest.get("process_identity") or {}
            pgid = identity.get("pgid")
            if pgid:
                try:
                    os.killpg(int(pgid), signal.SIGTERM)
                except OSError:
                    pass
    manifest = load_manifest(args.worker_id)
    manifest["state"] = "stopped"
    manifest["stopped_at"] = utc_now()
    manifest["result_disposition"] = "cancelled"
    save_manifest(manifest)
    emit(manifest)
    return 0


def command_permission(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.worker_id)
    if not manifest.get("authority", {}).get("mediated"):
        raise WorkerError("This worker does not use parent-mediated approvals")
    receipt = ipc_request(manifest, args.permission_action, {"permission_id": args.permission_id, "reason": args.reason})
    emit(receipt)
    return 0


def legacy_result_from_output(manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Recover the last terminal result emitted by a pre-v2 worker."""
    raw_path = manifest.get("output_path")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.is_file():
        return None
    candidate: Optional[Dict[str, Any]] = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        structured = event.get("structured_output")
        if isinstance(structured, dict):
            candidate = dict(structured)
        else:
            raw = event.get("result")
            if not isinstance(raw, (str, dict)):
                continue
            candidate = dict(raw) if isinstance(raw, dict) else {
                "status": "pass" if event.get("subtype") == "success" or re.search(r"status\s*:\s*pass", raw, re.I) else "block",
                "summary": raw,
            }
        candidate["_legacy_event_subtype"] = event.get("subtype")
    if candidate is None:
        return None
    defaults: Dict[str, Any] = {
        "status": "block",
        "summary": "Recovered legacy Claude worker result",
        "changed_files": [],
        "tests": [],
        "blockers": [],
        "branch": None,
        "worktree": manifest.get("workspace", {}).get("worktree"),
        "head": None,
        "proposed_subtasks": [],
        "lingering_processes": [],
    }
    for key, value in defaults.items():
        candidate.setdefault(key, value)
    candidate["worker_id"] = manifest.get("worker_id")
    candidate["session_id"] = manifest.get("session_id")
    candidate["received_at"] = utc_now()
    candidate["legacy_recovered"] = True
    return candidate


def command_result(args: argparse.Namespace) -> int:
    manifest = reconcile_manifest(load_manifest(args.worker_id))
    raw_path = manifest.get("result_path")
    path = Path(str(raw_path)) if raw_path else manifest_path(args.worker_id).parent / "result.json"
    if not path.is_file():
        result = legacy_result_from_output(manifest)
        if result is None:
            raise WorkerError("No structured or recoverable legacy result is available")
        atomic_write_json(path, result)
        manifest["result_path"] = str(path)
        manifest["result_receipt"] = {
            "at": utc_now(),
            "path": str(path),
            "status": result.get("status"),
            "legacy_recovered": True,
        }
        save_manifest(manifest)
    emit(json.loads(path.read_text(encoding="utf-8")))
    return 0


def command_dispose(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.worker_id)
    if args.disposition not in DISPOSITIONS - {"pending"}:
        raise WorkerError("Invalid result disposition")
    manifest["result_disposition"] = args.disposition
    manifest["disposition_at"] = utc_now()
    manifest["disposition_note"] = args.note
    if args.disposition == "cancelled" and manifest.get("state") not in TERMINAL_STATES:
        manifest["state"] = "cancelled"
    save_manifest(manifest)
    if manifest.get("state") in TERMINAL_STATES and process_alive(manifest.get("runner_pid")):
        try:
            ipc_request(manifest, "release", {}, timeout=5)
            deadline = time.monotonic() + 5.0
            while process_alive(manifest.get("runner_pid")) and time.monotonic() < deadline:
                time.sleep(0.05)
        except WorkerError:
            # Preserve the disposition and let reconcile/cleanup surface the
            # owned process; never hide a valid inspection receipt.
            pass
        manifest = reconcile_manifest(load_manifest(args.worker_id))
    emit(manifest)
    return 0


def command_reconcile(_args: argparse.Namespace) -> int:
    values = [reconcile_manifest(value) for value in all_manifests()]
    emit(values)
    return 0


def command_cleanup(args: argparse.Namespace) -> int:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than_days)
    removed: List[str] = []
    skipped: List[Dict[str, str]] = []
    for manifest in all_manifests():
        worker_id = str(manifest["worker_id"])
        state = str(manifest.get("state"))
        disposition = str(manifest.get("result_disposition", "pending"))
        try:
            updated = parse_time(str(manifest.get("updated_at")))
        except (TypeError, ValueError):
            continue
        if state not in TERMINAL_STATES or disposition not in DISPOSITIONS - {"pending"} or updated >= cutoff:
            skipped.append({"worker_id": worker_id, "reason": "not terminal/disposed/old enough"})
            continue
        if process_alive(manifest.get("runner_pid")):
            try:
                ipc_request(manifest, "release", {}, timeout=5)
            except WorkerError:
                skipped.append({"worker_id": worker_id, "reason": "owned supervisor could not be released"})
                continue
            deadline = time.monotonic() + 5.0
            while process_alive(manifest.get("runner_pid")) and time.monotonic() < deadline:
                time.sleep(0.05)
            if process_alive(manifest.get("runner_pid")):
                skipped.append({"worker_id": worker_id, "reason": "owned supervisor still active"})
                continue
        directory = manifest_path(worker_id).parent
        if directory.parent != workers_root() or not directory.name.startswith("cw-"):
            raise WorkerError("Refusing unsafe cleanup target")
        shutil.rmtree(str(directory))
        removed.append(worker_id)
    emit({"removed": removed, "skipped": skipped})
    return 0


def add_activation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--native-active", type=int, required=True)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--capacity-retry-seconds", type=float, default=300)
    parser.add_argument("--capacity-retry-interval", type=float, default=30)
    parser.add_argument("--initialization-timeout", type=float, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--cwd", required=True)
    doctor.add_argument("--reconcile", action="store_true")
    doctor.set_defaults(func=command_doctor)
    capacity = subparsers.add_parser("capacity")
    capacity.add_argument("--cwd", required=True)
    capacity.add_argument("--native-active", type=int, required=True)
    capacity.add_argument("--workload", choices=sorted(WORKLOADS), default="standard")
    capacity.add_argument("--max-workers", type=int)
    capacity.set_defaults(func=command_capacity)
    spawn = subparsers.add_parser("spawn")
    spawn.add_argument("--cwd", required=True)
    spawn.add_argument("--activation", choices=("maximal", "explicit-claude"), required=True)
    spawn.add_argument("--native-active", type=int, required=True)
    spawn.add_argument("--native-free-slots", type=int, required=True)
    spawn.add_argument("--codex-model")
    spawn.add_argument("--codex-effort", choices=sorted(EFFORTS))
    spawn.add_argument("--model")
    spawn.add_argument("--effort", choices=sorted(EFFORTS))
    spawn.add_argument("--codex-approval-policy", choices=("never", "on-request", "untrusted", "on-failure"), required=True)
    spawn.add_argument("--codex-sandbox", choices=("danger-full-access", "workspace-write", "read-only"), required=True)
    spawn.add_argument("--network", choices=("enabled", "disabled"), required=True)
    spawn.add_argument("--workload", choices=sorted(WORKLOADS), default="standard")
    spawn.add_argument("--max-workers", type=int)
    spawn.add_argument("--scope", choices=("read-only", "mutable-disjoint", "mutable-overlap"), default="read-only")
    spawn.add_argument("--isolation", choices=("auto", "shared", "worktree"), default="auto")
    spawn.add_argument("--owned-path", action="append", default=[])
    spawn.add_argument("--add-dir", action="append", default=[])
    spawn.add_argument("--name", required=True)
    spawn.add_argument("--task")
    spawn.add_argument("--allow-usage-credits", action="store_true")
    spawn.add_argument("--usage-credit-authorization")
    spawn.add_argument("--initialization-timeout", type=float, default=20)
    spawn.set_defaults(func=command_spawn)
    listing = subparsers.add_parser("list")
    listing.set_defaults(func=command_list)
    for name, func in (("status", command_status), ("logs", command_logs), ("wait", command_wait), ("attach", command_attach), ("pause", command_pause), ("stop", command_stop), ("result", command_result)):
        child = subparsers.add_parser(name)
        child.add_argument("worker_id")
        if name == "logs":
            child.add_argument("--tail", type=int, default=200)
            child.add_argument("--raw", action="store_true")
        if name == "attach":
            child.add_argument("--poll-interval", type=float, default=1)
            child.add_argument("--raw", action="store_true")
        if name == "wait":
            child.add_argument("--timeout", type=float, default=3600)
            child.add_argument("--poll-interval", type=float, default=2)
        if name == "pause":
            child.add_argument("--warm-seconds", type=float, default=300)
        child.set_defaults(func=func)
    send = subparsers.add_parser("send")
    send.add_argument("worker_id")
    send.add_argument("--message")
    send.set_defaults(func=command_send)
    for name, func in (("followup", command_followup), ("resume", command_resume)):
        child = subparsers.add_parser(name)
        child.add_argument("worker_id")
        if name == "followup":
            child.add_argument("--message")
        add_activation_args(child)
        child.set_defaults(func=func)
    for action in ("approve", "deny"):
        permission = subparsers.add_parser(action)
        permission.add_argument("worker_id")
        permission.add_argument("permission_id")
        permission.add_argument("--reason")
        permission.set_defaults(func=command_permission, permission_action=action)
    dispose = subparsers.add_parser("dispose")
    dispose.add_argument("worker_id")
    dispose.add_argument("--disposition", choices=sorted(DISPOSITIONS - {"pending"}), required=True)
    dispose.add_argument("--note")
    dispose.set_defaults(func=command_dispose)
    reconcile = subparsers.add_parser("reconcile")
    reconcile.set_defaults(func=command_reconcile)
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--older-than-days", type=int, default=7)
    cleanup.set_defaults(func=command_cleanup)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (WorkerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        emit({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

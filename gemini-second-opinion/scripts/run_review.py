#!/usr/bin/env python3
"""Run agy reviews with filesystem protection and retained execution evidence.

Python 3.9+, standard library only. See ../references/runner.md for the input
contract and the human coverage gate that follows the mechanical checks.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


EFFORTS = ("low", "medium", "high", "xhigh", "max")
SCENARIOS = {
    "independent-rca": "Independently diagnose the root cause. Return evidence, a fix approach, confidence, and useful questions. You must not receive the primary agent's RCA.",
    "plan-review": "Critique the completed plan for correctness, completeness, safety, and maintainability. Return a verdict, issues, changes, missing considerations, and useful questions.",
    "post-implementation-review": "Review the completed implementation against the plan or user requirements. Inspect the actual changes. Return a verdict, issues, changes, missing checks, and useful questions.",
    "document-review": "Work in two phases. PHASE 1: read only the user transcript and primary context; write your independent analysis before reading the artifact. PHASE 2: read the artifact and compare fidelity, divergences, clarity, logic, completeness, and soundness. Return both phases, a verdict, recommendations, and useful questions.",
    "analysis-review": "Work in two phases. PHASE 1: read only the user transcript and primary context; write your independent analysis before reading the artifact. PHASE 2: compare the artifact's reasoning, assumptions, fidelity, evidence, completeness, tradeoffs, and actionability. Return both phases, a verdict, recommendations, and useful questions.",
}
# Deliberately exclude bin, builtin, updater, plugins, cache directories, settings,
# config, credentials, and any parent of these locations. Never widen on failure.
RUNTIME_DIRS = ("log", "crashes", "conversations", "brain", "annotations", "presence")
RUNTIME_FILES = (
    "history.jsonl", "conversation_summaries.db", "conversation_summaries.db-wal",
    "conversation_summaries.db-shm", "jetbox_summaries_proto.pb",
    "cache/conversation_metadata.json", "cache/last_conversations.json",
)
FALLBACK = re.compile(r"(?:auto[^\n]*(?:not available|unavailable|default mode)|(?:falling back|fallback)[^\n]*mode)", re.I)
DENIAL = re.compile(r"permission denied|operation not permitted|read-only file system|soft[- ]denied|requires? approval|not allowed|access denied", re.I)


class ReviewError(Exception):
    pass


@contextmanager
def preparation_budget(deadline):
    """Bound synchronous preparation too, including a blocked filesystem read.

    Used only when no child command is active. capture owns child deadlines and
    cleanup, so a preparation alarm cannot interrupt its process-group cleanup.
    """
    check_deadline(deadline)
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signal, _frame):
        raise ReviewError("Runner wall-clock timeout during file preparation/verification.")

    started = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - started))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def open_regular(path):
    # O_NONBLOCK avoids hanging if a path becomes a FIFO between inspection and
    # open. fstat validates the opened object, rather than a stale pathname stat.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ReviewError(f"Expected a regular file, refusing special input/configuration: {path}")
    return os.fdopen(fd, "rb")


def read_regular_text(path):
    with io.TextIOWrapper(open_regular(path), encoding="utf-8") as stream:
        return stream.read()


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ReviewError("Runner wall-clock timeout during file preparation/verification.")


def digest(path: Path, deadline=None) -> str:
    with open_regular(path) as stream:
        return _digest(stream, deadline)


def _digest(stream, deadline=None) -> str:
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        check_deadline(deadline)
        result.update(block)
    return result.hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, *, empty_ok=False):
    if not path.exists():
        if path.is_symlink():
            raise ReviewError(f"Broken configuration symlink: {path}")
        return {}
    try:
        raw = read_regular_text(path)
        result = json.loads(raw) if raw.strip() else ({} if empty_ok else None)
    except (OSError, ValueError) as error:
        raise ReviewError(f"Cannot inspect configuration {path}: {error}") from error
    if not isinstance(result, dict):
        raise ReviewError(f"Expected a JSON object in {path}; cannot safely preflight startup.")
    return result


def within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def active_commands(value):
    """Honor disabled hook definitions at every nesting level."""
    if isinstance(value, dict):
        if value.get("enabled") is False:
            return
        if value.get("command") or value.get("builtin"):
            yield True
        for child in value.values():
            yield from active_commands(child)
    elif isinstance(value, list):
        for child in value:
            yield from active_commands(child)


def preflight_config(home: Path, workspaces: list[Path]) -> dict:
    """Inspect executable customizations without launching agy or any hook."""
    state = home / ".gemini/antigravity-cli"
    settings_path = state / "settings.json"
    settings = read_json(settings_path)
    if settings.get("toolPermission") == "always-proceed":
        raise ReviewError(f"{settings_path}: toolPermission=always-proceed bypasses normal MCP permission checks. Configure request-review or proceed-in-sandbox yourself; the runner will not change settings.")
    for key in ("statusLine", "title"):
        if any(active_commands(settings.get(key))):
            raise ReviewError(f"Executable {key} configured in {settings_path}; automatic commands must be disabled by the user before review.")
    roots = {home / ".gemini/config", state}
    for workspace in workspaces:
        roots.update(parent / ".agents" for parent in (workspace, *workspace.parents))
    inspected, mcp, plugins = set(), [], []

    def inspect(root: Path):
        root = root.resolve()
        if root in inspected or not root.exists():
            return
        inspected.add(root)
        hook_path = root / "hooks.json"
        if any(active_commands(read_json(hook_path))):
            raise ReviewError(f"Automatic executable hooks found in {hook_path}. Disable them before review; no hooks were run.")
        config = read_json(root / "config.json")
        sidecars = config.get("sidecars", {})
        if not isinstance(sidecars, dict):
            raise ReviewError(f"Cannot inspect sidecars in {root / 'config.json'}.")
        for name, value in sidecars.items():
            if not isinstance(value, dict) or not isinstance(value.get("enabled", False), bool):
                raise ReviewError(f"Unrecognized sidecar configuration: {root / 'config.json'} ({name}).")
            if value.get("enabled"):
                raise ReviewError(f"Automatic sidecar {name!r} is enabled in {root / 'config.json'}. Disable it before review; no sidecars were run.")
        mcp_path = root / "mcp_config.json"
        servers = read_json(mcp_path, empty_ok=True).get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ReviewError(f"Cannot inspect MCP server names in {mcp_path}.")
        if servers:
            mcp.append({"path": str(mcp_path), "servers": sorted(servers)})
        # Inspect installed assets conservatively, even when a plugin registry
        # claims disabled. Registry formats vary by CLI release.
        plugin_root = root / "plugins"
        if plugin_root.exists():
            for plugin in sorted(plugin_root.iterdir()):
                if plugin.is_dir():
                    plugins.append(str(plugin.resolve()))
                    inspect(plugin)

    for root in sorted(roots):
        inspect(root)
    return {
        "settings_path": str(settings_path),
        "settings_sha256": digest(settings_path) if settings_path.exists() else None,
        "saved_agent_mode": settings.get("agentMode", ""),
        "saved_tool_permission": settings.get("toolPermission", "request-review"),
        "saved_terminal_sandbox": settings.get("enableTerminalSandbox", False),
        "inspected_roots": sorted(map(str, inspected)), "mcp": mcp, "plugins": plugins,
    }


class Sandbox:
    def __init__(self, run: Path, home: Path, *, system=None):
        self.run = run.resolve()
        self.scratch = self.run / "scratch"
        self.scratch.mkdir()
        self.system = system or sys.platform
        state = home / ".gemini/antigravity-cli"
        self.writable = [self.scratch]
        self.files = []
        self.runtime_views = []
        if state.is_symlink() or state.resolve() != state.absolute():
            raise ReviewError(f"agy runtime root must not use symlinks: {state}")
        # Existing runtime data only. Do not create or initialize account state.
        for relative in (*RUNTIME_DIRS, *RUNTIME_FILES):
            path = state / relative
            if path.is_symlink() or path.resolve() != path.absolute():
                raise ReviewError(f"Writable runtime path must not use symlinks: {path}")
            if not path.exists():
                continue
            if relative in RUNTIME_DIRS and path.is_dir():
                # A hard link could alias a protected file while retaining an
                # allowed pathname. Reject these before granting directory writes.
                for directory, _, files in os.walk(path):
                    for name in files:
                        child = Path(directory) / name
                        if child.is_file() and not child.is_symlink() and child.stat().st_nlink > 1:
                            raise ReviewError(f"Hard-linked runtime data is unsafe to make writable: {child}")
                        if child.is_file() and not child.is_symlink() and child.stat().st_mode & 0o111:
                            raise ReviewError(f"Executable runtime data must remain protected: {child}")
                self.writable.append(path)
            elif relative in RUNTIME_FILES and path.is_file() and path.stat().st_nlink == 1 and not path.stat().st_mode & 0o111:
                self.files.append(path)
            else:
                raise ReviewError(f"Unexpected type or hard link at runtime path: {path}")
        if self.system == "darwin":
            executable = shutil.which("sandbox-exec")
            if not executable:
                raise ReviewError("sandbox-exec is required on macOS; refusing an unprotected run.")
            profile = "(version 1)\n(allow default)\n(deny file-write*)\n"
            profile += '(allow file-write* (literal "/dev/null")\n'
            profile += "".join(f"  (subpath {json.dumps(str(p))})\n" for p in self.writable)
            profile += "".join(f"  (literal {json.dumps(str(p))})\n" for p in self.files)
            profile += ")\n"
            self.profile = self.run / "sandbox.sb"
            self.profile.write_text(profile)
            self.prefix = [executable, "-f", str(self.profile)]
        elif self.system == "linux":
            executable = shutil.which("bwrap")
            if not executable:
                raise ReviewError("Bubblewrap (bwrap) is required on Linux; refusing an unprotected run.")
            self.prefix = [executable, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid", "--cap-drop", "ALL",
                           "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
            for path in (*self.writable, *self.files):
                self.prefix += ["--bind", str(path), str(path)]
            # agy regenerates bin/agentapi for every terminal call. Give it a
            # disposable view, preserving every other installed entry read-only.
            runtime_bin = state / "bin"
            if runtime_bin.exists() or runtime_bin.is_symlink():
                if runtime_bin.is_symlink() or not runtime_bin.is_dir():
                    raise ReviewError(f"agy runtime bin must be a real directory: {runtime_bin}")
                view = self.scratch / "agy-bin"
                view.mkdir()
                self.prefix += ["--bind", str(view), str(runtime_bin)]
                for entry in sorted(runtime_bin.iterdir()):
                    if entry.name == "agentapi":
                        continue
                    if not entry.is_file() and not entry.is_dir():
                        raise ReviewError(f"Unsupported agy runtime bin entry: {entry}")
                    target = view / entry.name
                    target.mkdir() if entry.is_dir() else target.touch()
                    self.prefix += ["--ro-bind", str(entry), str(runtime_bin / entry.name)]
                self.runtime_views.append(runtime_bin)
            # Network remains shared; do not add --unshare-net or --unshare-all.
            self.prefix += ["--"]
        else:
            raise ReviewError("Only macOS (sandbox-exec) and Linux (Bubblewrap) are supported.")

    def command(self, argv):
        return self.prefix + list(map(str, argv))


def stop_process(process):
    # Kill the entire session, including child tools that inherited stdout.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def capture(argv, cwd, env, run, stage, deadline, *, on_event=None, reject_fallback=False):
    """Drain both pipes incrementally; the runner owns the independent deadline."""
    stdout_path, stderr_path = run / f"{stage}.stdout", run / f"{stage}.stderr"
    if on_event:
        stdout_path = run / f"{stage}.events.jsonl"
    write_json(run / f"{stage}.command.json", {"argv": list(map(str, argv)), "cwd": str(cwd)})
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReviewError("Runner wall-clock timeout reached before " + stage)
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, out)
        selector.register(process.stderr, selectors.EVENT_READ, err)
        pending = b""
        diagnostic = b""
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReviewError(f"Runner wall-clock timeout during {stage}; partial output retained.")
                for key, _ in selector.select(min(0.2, remaining)):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.write(block)
                    key.data.flush()
                    if key.fileobj is process.stderr:
                        diagnostic = (diagnostic + block)[-65536:]
                        if reject_fallback and FALLBACK.search(diagnostic.decode(errors="replace")):
                            raise ReviewError("agy silently fell back from auto mode during startup; review stopped.")
                    elif on_event:
                        pending += block
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if line.strip():
                                on_event(parse_event(line))
            if pending.strip() and on_event:
                on_event(parse_event(pending))
            try:
                returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise ReviewError(f"Runner wall-clock timeout during {stage}.") from error
        finally:
            stop_process(process)
            selector.close()
            process.stdout.close()
            process.stderr.close()
    return returncode, stdout_path.read_text(errors="replace"), stderr_path.read_text(errors="replace")


def parse_event(raw):
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ReviewError("Malformed JSON stream; partial events retained.") from error
    if isinstance(event, dict) and event.get("status") in ("ERROR", "CANCELED", "INTERRUPTED", "WAITING") and "event" not in event:
        raise ReviewError(failure_message("stream startup", event["status"], json.dumps(event), ""))
    if not isinstance(event, dict) or event.get("event") not in ("init", "step_update", "result"):
        raise ReviewError("Unsupported JSON stream event; inspect the captured CLI output.")
    return event


def failure_message(stage, code, stdout, stderr):
    detail = (stderr.strip() or stdout.strip())[:3000]
    combined = stdout + "\n" + stderr
    if re.search(r"authentication required|unauthenticated|not logged in|login required", combined, re.I):
        return f"Authentication required during {stage}. Sign in interactively with agy outside this runner, then retry. {detail}"
    if "settings.json" in combined and DENIAL.search(combined):
        return f"agy startup cannot write protected settings.json during {stage}. Settings remain read-only; this CLI cannot start under the required protection. {detail}"
    return f"agy {stage} failed (exit {code}). Original files and configuration remain protected. {detail}"


def command_data(stdout, name):
    try:
        obj = json.loads(stdout)
    except ValueError as error:
        raise ReviewError(f"/{name} did not return a JSON command envelope.") from error
    if not isinstance(obj, dict) or obj.get("status") != "SUCCESS" or obj.get("num_turns") != 0:
        raise ReviewError(f"/{name} did not complete as a non-generative command.")
    command = obj.get("command", {})
    if not isinstance(command, dict) or command.get("name") != name or not isinstance(command.get("data"), dict):
        raise ReviewError(f"/{name} lacks structured command metadata. Generated text is not resolution evidence.")
    return command["data"]


def model_base(model):
    return re.sub(r"-(?:low|medium|high|xhigh|max)$", "", model)


def catalog_selection(stdout, model, effort):
    models = {line.split()[0] for line in stdout.splitlines()
              if line.strip() and re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]*", line.split()[0])}
    if not models:
        raise ReviewError("The account catalog contains no recognizable Gemini models; inspect catalog output and authentication.")
    family = model_base(model)
    available = sorted(item for item in models if model_base(item) == family)
    if not available:
        raise ReviewError(f"Requested Gemini model {model!r} is unavailable in this account's catalog. Available Gemini entries: {', '.join(sorted(models))}. No model was substituted.")
    expected = family + "-" + effort
    if model != family and model != expected:
        raise ReviewError(f"Model slug {model!r} conflicts with effort {effort!r}; select matching explicit --model and --effort values.")
    if expected in models:
        return expected
    if family in models:
        return family  # /model must still confirm the requested effort.
    raise ReviewError(f"Effort {effort!r} is unsupported for {family!r} in this account. Available entries: {', '.join(available)}.")


def validate_selection(data, expected, effort):
    if data.get("id") != expected or data.get("effort") != effort or not str(data.get("id", "")).startswith("gemini-"):
        raise ReviewError(f"Structured /model selection does not match {expected!r} at {effort!r}: {data!r}. No fallback is allowed.")


def choose_mode(stderr, config):
    permission = config["saved_tool_permission"]
    if permission in ("always-proceed", "always-ask"):
        raise ReviewError(f"Saved toolPermission={permission} conflicts with the review permission policy; no settings were changed.")
    if FALLBACK.search(stderr):
        if permission != "proceed-in-sandbox" or config["saved_agent_mode"] not in ("", "auto"):
            raise ReviewError("Auto mode is unavailable and agy falls back to default mode. An existing toolPermission=proceed-in-sandbox configuration (with default or auto agentMode) is required for this account; configure it yourself if desired. The runner did not change settings or bypass permissions.")
        return None
    return "auto"


def packet_scenario(text):
    # A verbatim transcript/artifact may itself contain headings. Allow it to be
    # fenced without interpreting those quoted headings as packet structure.
    structural, fence = [], None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            sequence = marker[1]
            if fence is None:
                fence = sequence
            elif sequence[0] == fence[0] and len(sequence) >= len(fence):
                fence = None
        elif fence is None:
            structural.append(line)
    text = "\n".join(structural) + "\n"
    required = ["Scenario", "Review target in repository", "Full verbatim user transcript"]
    for heading in required:
        if text.count("### " + heading + "\n") != 1:
            raise ReviewError(f"Packet must contain exactly one '### {heading}' heading.")
    match = re.search(r"^### Scenario\s*\n\s*([a-z-]+)\s*(?:\n|$)", text, re.M)
    scenario = match.group(1) if match else ""
    if scenario not in SCENARIOS:
        raise ReviewError("Packet Scenario is missing or unsupported.")
    artifacts = len(re.findall(r"^### Artifact under review(?:\s.*)?$", text, re.M))
    if artifacts != (0 if scenario == "independent-rca" else 1):
        raise ReviewError("Packet artifact section does not match its scenario.")
    return scenario


def prepare_inputs(packet: Path, manifest: Path, run: Path, repo: Path):
    content = read_regular_text(packet)
    scenario = packet_scenario(content)
    raw = read_json(manifest)
    entries = raw.get("critical_materials")
    if not isinstance(entries, list) or not entries:
        raise ReviewError("Manifest requires a nonempty critical_materials list of primary files.")
    materials, ids = [], {"packet"}
    for entry in entries:
        if not isinstance(entry, dict) or not re.fullmatch(r"[a-zA-Z0-9_-]+", str(entry.get("id", ""))):
            raise ReviewError("Each critical material requires an id containing letters, digits, '_' or '-'.")
        identifier = entry["id"]
        if identifier in ids or entry.get("kind", "text") not in ("text", "image"):
            raise ReviewError(f"Duplicate material id or unsupported kind: {identifier}")
        ids.add(identifier)
        if not isinstance(entry.get("path"), str):
            raise ReviewError(f"Material {identifier} requires a path.")
        path = Path(entry["path"]).expanduser()
        path = (repo / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_file():
            raise ReviewError(f"Critical material is not an accessible file: {path}")
        item = {**entry, "path": str(path), "kind": entry.get("kind", "text"), "sha256": digest(path)}
        if item["kind"] == "image":
            destination = run / "images" / (identifier + path.suffix)
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, destination)
            if digest(destination) != item["sha256"]:
                raise ReviewError(f"Image copy failed verification: {path}")
            item["source_path"] = str(path)
            item["path"] = str(destination)
        materials.append(item)
    checks = raw.get("required_checks", [])
    if not isinstance(checks, list) or not all(isinstance(check, str) and check.strip() for check in checks):
        raise ReviewError("required_checks must be a list of nonempty descriptions.")
    target = run / "packet.md"
    shutil.copyfile(packet, target)
    materials.insert(0, {"id": "packet", "path": str(target), "kind": "text", "sha256": digest(target)})
    write_json(run / "materials.json", {"critical_materials": materials, "required_checks": checks})
    return scenario, materials, checks


def tree_manifest(root: Path, deadline=None):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            check_deadline(deadline)
            path = Path(directory) / name
            info = path.lstat()
            relative = str(path.relative_to(root))
            mode = stat.S_IMODE(info.st_mode)
            if path.is_symlink():
                result[relative] = ["symlink", os.readlink(path), mode]
            elif path.is_dir():
                result[relative] = ["directory", mode]
            elif path.is_file():
                result[relative] = ["file", digest(path, deadline), mode]
            else:
                raise ReviewError(f"Cannot faithfully copy special file {path}; prepare a suitable review target.")
    return result


def faithful_copy(repo: Path, destination: Path, evidence: Path, deadline=None):
    before = tree_manifest(repo, deadline)

    def copy_file(source, target):
        with open(source, "rb") as src, open(target, "wb") as dst:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                check_deadline(deadline)
                dst.write(block)
        shutil.copystat(source, target)
        return target

    shutil.copytree(repo, destination, symlinks=True, copy_function=copy_file)
    copied = tree_manifest(destination, deadline)
    after = tree_manifest(repo, deadline)
    if before != after or before != copied:
        raise ReviewError("Disposable copy differs from the source or the source changed during copying. Do not rely on its checks.")
    write_json(evidence, {"source": str(repo), "copy": str(destination), "files": before, "verified": True})
    return destination


class ReviewStream:
    def __init__(self, selected, repo, mode):
        self.selected, self.repo, self.mode = selected, str(repo), mode
        self.init = None
        self.result = None
        self.steps = {}

    def accept(self, event):
        if self.result is not None:
            raise ReviewError("Events arrived after the terminal result.")
        kind = event["event"]
        data = event.get(kind)
        if not isinstance(data, dict):
            raise ReviewError(f"Malformed {kind} event.")
        if kind == "init":
            if self.init is not None:
                raise ReviewError("Duplicate stream init event.")
            self.init = data
            if data.get("model") != self.selected or data.get("cwd") != self.repo:
                raise ReviewError("Review init does not confirm the pinned model and repository root.")
            permission = data.get("permission_mode")
            if permission not in ("request-review", "proceed-in-sandbox"):
                raise ReviewError(f"Unexpected effective permission mode: {permission!r}.")
            if self.mode is None and permission != "proceed-in-sandbox":
                raise ReviewError("Configured proceed-in-sandbox is not effective; native sandbox may be unavailable.")
            if "agent_mode" in data and data["agent_mode"] != (self.mode or ""):
                raise ReviewError("Stream reports a different agent mode; possible silent mode fallback.")
            if data.get("sandbox_enabled") is False:
                raise ReviewError("Stream reports that agy's native sandbox is unavailable.")
        elif self.init is None:
            raise ReviewError("Stream has no initial configuration evidence.")
        elif kind == "result":
            self.result = data
        elif data.get("step_type") == "tool":
            index = data.get("step_index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise ReviewError("Tool step lacks a numeric step_index.")
            info = data.get("tool_info", {})
            if not isinstance(info, dict) or not isinstance(info.get("parameters", {}), dict):
                raise ReviewError("Malformed tool_info in stream; cannot establish coverage.")
            if data.get("state") not in ("ACTIVE", "DONE"):
                raise ReviewError("Unrecognized tool state in stream.")
            previous = self.steps.get(index, {})
            self.steps[index] = {**previous, **data, "tool_info": {**previous.get("tool_info", {}), **info}}

    def finish(self, materials, stderr):
        if not self.result:
            raise ReviewError("Stream ended without a successful terminal result; the review is interrupted or incomplete.")
        if self.result.get("status") != "SUCCESS" or self.result.get("error"):
            raise ReviewError(failure_message("terminal result", self.result.get("status"), json.dumps(self.result), stderr))
        response = self.result.get("response", "")
        if not isinstance(response, str) or len(response.split()) < 60:
            raise ReviewError("Review text is empty or too short to substantively cover the target.")
        lines = [line.strip(" \t\r#>*_`~-.") for line in response.splitlines() if line.strip()][:5]
        if lines.count("REVIEW STATUS: COMPLETE") != 1 or "REVIEW STATUS: INCOMPLETE" in lines:
            raise ReviewError("Expected exactly one COMPLETE marker and no INCOMPLETE marker in the first five nonempty lines.")
        if "REVIEW STATUS: INCOMPLETE" in response:
            raise ReviewError("Review discloses an INCOMPLETE status.")
        evidence = {}
        for material in materials:
            identifier = material["id"]
            matched = []
            path = Path(material["path"])
            names = {str(path)}
            if within(path, Path(self.repo)):
                names.add(str(path.relative_to(self.repo)))
            # Derive evidence from the CLI, not reviewer-generated indices. The
            # model need not know the CLI's internal step numbering.
            for index, step in self.steps.items():
                info = step.get("tool_info", {})
                tool = step.get("tool_name", info.get("name", ""))
                parameters = info.get("parameters", {})
                if (step.get("state") != "DONE" or info.get("error") or info.get("output") is None
                        or not references_path(parameters, names)):
                    continue
                if material["kind"] == "image":
                    if tool not in ("view_file", "view_image"):
                        continue
                elif tool not in ("view_file", "read_file", "run_command", "read_resource"):
                    continue
                if tool == "run_command":
                    command = info.get("parameters", {}).get("CommandLine", "")
                    if re.match(r"^\s*(?:ls|stat|echo|printf|test|pwd)\b", command):
                        continue
                matched.append(index)
            if not matched:
                raise ReviewError(f"Critical material {identifier} has no matching successful content-inspection tool evidence. Self-reported coverage is insufficient.")
            evidence[identifier] = matched
        concerns = []
        for index, step in self.steps.items():
            info = step.get("tool_info", {})
            if step.get("subagent_info"):
                concerns.append({"step_index": index, "reason": "subagent evidence requires separate inspection; it cannot substitute for missing primary tool coverage"})
            if step.get("state") != "DONE" or info.get("error") or DENIAL.search(str(info.get("output", ""))):
                concerns.append({"step_index": index, "reason": "tool error, unfinished tool, or possible permission denial"})
        for line in stderr.splitlines():
            if DENIAL.search(line) or re.search(r"error|fail|authentication|fallback", line, re.I):
                concerns.append({"stderr": line})
        return response, {"mechanical_checks_passed": True, "human_review_required": True,
                          "coverage_candidates": evidence, "concerns": concerns,
                          "instruction": "Read the full response and referenced tool calls. Confirm substantive coverage, both phases where applicable, and classify each concern as repaired or non-material before accepting the review."}


def references_path(parameters, names):
    if isinstance(parameters, str):
        return any(parameters == name or re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w./-])", parameters)
                   for name in names)
    if isinstance(parameters, dict):
        return any(references_path(value, names) for value in parameters.values())
    if isinstance(parameters, list):
        return any(references_path(value, names) for value in parameters)
    return False


def reviewer_prompt(scenario, run, repo, materials, checks, copy):
    return f"""You are an independent second-opinion reviewer. {SCENARIOS[scenario]}
The repository root is {repo}. Read the packet at {run / 'packet.md'}.
For the two-phase scenarios, read its transcript and context before its Artifact
section. You choose your own repository searches, file reads, and terminal checks.
Do not treat the invoking agent's claims as established facts.

Filesystem protection is enforced externally. Original files, configuration,
credentials, executables, packet, and images are read-only. You may write temporary
review files only under {run / 'scratch'}. Do not change external services, send
messages, push commits, install plugins, change permissions, or invoke executable
hooks/sidecars. Network is available for model access and read-only research,
subject to existing agy permissions. Never bypass a denial or request new grants.
Disposable copy for checks requiring writes: {copy or 'not prepared; if a material check requires one, report precisely what is needed' }.
Use a prepared copy only for checks that need writes. It includes uncommitted and
untracked files; do not create a clone or git worktree. Do not change the source.

Critical materials (images must actually be viewed, not just listed or hashed):
{json.dumps(materials, ensure_ascii=False)}
Required checks: {json.dumps(checks, ensure_ascii=False)}
These are minimum coverage, not a limit on repository exploration.

Report operational status within the first five nonempty lines: exactly one
REVIEW STATUS: COMPLETE line, or REVIEW STATUS: INCOMPLETE if required execution,
access, or scope is missing. Criticism, rejection, or defects within the artifact
do not make a fully performed review incomplete. Identify the concrete target,
inspected primary materials and checks, and every limitation. A failure to inspect
separate critical material is incomplete even if the rest is useful. Disclose
non-material limitations without basing a verdict on those unverified points.
Give substantive reasoning and evidence, not generic feedback. For document and
analysis reviews include both PHASE 1 and PHASE 2.
Identify the material ids and paths actually inspected, including packet.
Tool parameters must identify the file path; the caller independently matches
the CLI's tool events to the material manifest.
Reading only filenames, metadata, hashes, or an invoking-agent summary is not
content inspection. Use view_file/view_image to inspect images. Include enough
evidence for the invoking agent to verify full content and scope coverage.
"""


def sandbox_probe(sandbox, repo, env, run, deadline, materials):
    guard = run / "protected-probe"
    guard.mkdir()
    sentinel = guard / "original with spaces.txt"
    sentinel.write_text("protected")
    link = sandbox.scratch / "symlink to original"
    link.symlink_to(guard, target_is_directory=True)
    code = r'''
import json, os, pathlib, sys
guard, scratch = map(pathlib.Path, sys.argv[1:3])
for name in sys.argv[3:]:
    with open(name, "rb") as source: source.read(1)
assert (guard / "original with spaces.txt").read_text() == "protected"
(scratch / "write test.txt").write_text("ok")
for target in (guard, scratch / "symlink to original"):
    for action in (lambda: (target / "new").write_text("bad"),
                   lambda: (target / "original with spaces.txt").write_text("bad"),
                   lambda: (target / "original with spaces.txt").unlink()):
        try: action()
        except OSError: pass
        else: raise SystemExit("Filesystem protection failed")
print(json.dumps({"reads": True, "scratch_writes": True, "original_writes_denied": True, "symlink_writes_denied": True}))
'''
    code_result = capture(sandbox.command([sys.executable, "-c", code, guard, sandbox.scratch,
                          *[item["path"] for item in materials]]), repo, env, run,
                          "filesystem-probe", deadline)
    if code_result[0] or sentinel.read_text() != "protected":
        raise ReviewError("Filesystem sandbox probe failed; refusing to start agy. " + code_result[2])
    shutil.rmtree(guard)
    link.unlink()
    (sandbox.scratch / "write test.txt").unlink()


def execute(args, *, runtime_home=None):
    if sys.platform not in ("darwin", "linux"):
        raise ReviewError("Only macOS and Linux are supported.")
    repo = Path(args.repo).expanduser().resolve(strict=True)
    if not repo.is_dir():
        raise ReviewError("--repo must identify a repository root directory.")
    packet, manifest = Path(args.packet).resolve(strict=True), Path(args.manifest).resolve(strict=True)
    base = Path(args.evidence_root or os.environ.get("TMPDIR") or "/tmp").expanduser().resolve()
    if within(base, repo):
        raise ReviewError("Evidence/scratch must be outside the original repository.")
    base.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="gemini-second-opinion-", dir=base)).resolve()
    print(f"Review evidence: {run}", flush=True)
    metadata = {"status": "incomplete", "requested_model": args.model, "effort": args.effort,
                "timeout_seconds": args.timeout_seconds, "repo": str(repo), "commands": "*.command.json",
                "filesystem_protection_only": True, "network": "available, subject to existing agy permissions"}
    deadline = time.monotonic() + args.timeout_seconds

    def prepare(function, *positional, **keyword):
        with preparation_budget(deadline):
            return function(*positional, **keyword)

    try:
        scenario, materials, checks = prepare(prepare_inputs, packet, manifest, run, repo)
        workspaces = sorted({repo, run, *[Path(item["path"]).parent for item in materials
                                         if not within(Path(item["path"]), repo)]})
        home = (runtime_home or Path.home()).absolute()
        config = prepare(preflight_config, home, workspaces)
        write_json(run / "configuration.json", config)
        sandbox = prepare(Sandbox, run, home)
        metadata["writable_directories"] = list(map(str, sandbox.writable))
        metadata["writable_files"] = list(map(str, sandbox.files))
        metadata["disposable_runtime_views"] = list(map(str, sandbox.runtime_views))
        for writable in (*sandbox.writable[1:], *sandbox.files, *sandbox.runtime_views):
            if within(writable, repo) or within(repo, writable) or within(run, writable):
                raise ReviewError(f"Repository/evidence overlaps writable agy runtime data: {writable}")
        for item in materials:
            if any(within(Path(item["path"]), directory) for directory in (*sandbox.writable, *sandbox.runtime_views)) or Path(item["path"]) in sandbox.files:
                raise ReviewError(f"Critical material is in writable runtime data: {item['path']}. Supply a protected snapshot.")
        executable = shutil.which(args.agy)
        if not executable:
            raise ReviewError(f"agy executable not found: {args.agy}")
        executable = str(Path(executable).resolve())
        if any(within(Path(executable), directory) for directory in (*sandbox.writable, *sandbox.runtime_views)):
            raise ReviewError("agy executable must not be in writable review/runtime data.")
        metadata["executable"] = {"path": executable, "sha256": prepare(digest, Path(executable))}
        env = dict(os.environ, TMPDIR=str(sandbox.scratch), TMP=str(sandbox.scratch), TEMP=str(sandbox.scratch),
                   GIT_OPTIONAL_LOCKS="0", PYTHONDONTWRITEBYTECODE="1")
        sandbox_probe(sandbox, repo, env, run, deadline, materials)

        def invoke(stage, argv, **kwargs):
            limit = deadline if stage == "review" else min(deadline, time.monotonic() + 60)
            try:
                result = capture(sandbox.command([executable, *argv]), repo, env, run, stage, limit, **kwargs)
            except ReviewError as error:
                diagnostic = run / f"{stage}.stderr"
                raw = diagnostic.read_text(errors="replace") if diagnostic.exists() else ""
                if re.search(r"authentication required|unauthenticated|not logged in|login required", raw, re.I) or ("settings.json" in raw and DENIAL.search(raw)):
                    raise ReviewError(failure_message(stage, "interrupted", "", raw)) from error
                raise
            if result[0]:
                raise ReviewError(failure_message(stage, *result))
            return result[1], result[2]

        try:
            version, _ = invoke("version", ["--version"])
            metadata["cli_version"] = version.strip()
        except ReviewError as error:
            metadata["cli_version"] = {"unavailable": str(error)}
        # /config reports saved configuration in some versions, not CLI flag
        # overrides. Its response text is never effective-mode evidence.
        command_flags = ["--disable-slash-commands=false", "--sandbox", "--print-timeout", "0",
                         "--output-format", "json"]
        config_text, mode_stderr = invoke("startup", [*command_flags, "--mode", "auto", "-p", "/config"])
        command_data(config_text, "config")
        mode_error = None
        try:
            mode = choose_mode(mode_stderr, config)
        except ReviewError as error:
            mode, mode_error = None, str(error)
        metadata["mode_preflight"] = {"mode": mode, "error": mode_error,
                                      "auto_fallback_detected": bool(FALLBACK.search(mode_stderr))}
        catalog, _ = invoke("catalog", ["models"])
        try:
            selected = prepare(catalog_selection, catalog, args.model, args.effort)
        except ReviewError as error:
            raise ReviewError(str(error) + ("\n" + mode_error if mode_error else "")) from error
        resolved, _ = invoke("model", [*command_flags, "--model", args.model, "--effort", args.effort, "-p", "/model"])
        selection = command_data(resolved, "model")
        validate_selection(selection, selected, args.effort)
        metadata["selected_model"] = selection
        if mode_error:
            raise ReviewError(mode_error)
        copy = prepare(faithful_copy, repo, sandbox.scratch / "checkout", run / "copy-fidelity.json", deadline) if args.copy_for_checks else None
        prompt = reviewer_prompt(scenario, run, repo, materials, checks, copy)
        (run / "prompt.txt").write_text(prompt)
        argv = ["--model", selected, "--effort", args.effort, "--sandbox", "--disable-slash-commands=true",
                "--print-timeout", "0", "--output-format", "stream-json", "--log-file", str(sandbox.scratch / "agy.log")]
        if mode:
            argv += ["--mode", mode]
        for workspace in workspaces:
            if workspace != repo:
                argv += ["--add-dir", str(workspace)]
        argv += ["-p", prompt]
        stream = ReviewStream(selected, repo, mode)
        try:
            _, stderr = invoke("review", argv, on_event=stream.accept, reject_fallback=mode == "auto")
        finally:
            metadata["effective_init"] = stream.init
            if stream.result and isinstance(stream.result.get("response"), str):
                (run / "output.txt").write_text(stream.result["response"])
        response, gate = prepare(stream.finish, materials, stderr)
        for item in materials:
            if prepare(digest, Path(item["path"]), deadline) != item["sha256"]:
                raise ReviewError(f"Critical material changed during review: {item['path']}")
        settings_path = Path(config["settings_path"])
        if (prepare(digest, settings_path) if settings_path.exists() else None) != config["settings_sha256"]:
            raise ReviewError("Settings changed during review; evidence is invalid.")
        write_json(run / "gate.json", gate)
        (run / "output.txt").write_text(response)
        metadata["status"] = "awaiting-primary-agent-coverage-assessment"
        print("Mechanical checks passed. Read output.txt, gate.json, events, and stderr; the primary agent must assess full coverage and all concerns.", flush=True)
        return 0
    except (ReviewError, OSError, ValueError, KeyboardInterrupt) as error:
        metadata["error"] = str(error) or "Interrupted by user; review incomplete."
        print(metadata["error"], file=sys.stderr, flush=True)
        return 2
    finally:
        write_json(run / "result.json", metadata)
        # Retain scratch when failed: it can contain evidence required for repair.
        # Successful scratch is removed only after the invoking agent's assessment.


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", required=True, help="Repository root, including the current uncommitted working tree")
    result.add_argument("--packet", required=True)
    result.add_argument("--manifest", required=True, help="JSON critical-material manifest")
    result.add_argument("--model", default="gemini-4-argon")
    result.add_argument("--effort", default="high", choices=EFFORTS)
    result.add_argument("--timeout-seconds", type=float, default=1800)
    result.add_argument("--evidence-root", help="Parent of unique run directories (default: TMPDIR or /tmp); must be outside the repository")
    result.add_argument("--copy-for-checks", action="store_true", help="Prepare and verify a faithful disposable file copy for checks requiring writes")
    result.add_argument("--agy", default="agy", help="CLI executable (also permits isolated integration stubs)")
    return result


def main():
    args = parser().parse_args()
    if not re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]*", args.model):
        parser().error("--model must be a Gemini model slug; other providers and arbitrary API URLs are not supported")
    if not 0 < args.timeout_seconds < float("inf"):
        parser().error("--timeout-seconds must be finite and positive")
    try:
        return execute(args)
    except (ReviewError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

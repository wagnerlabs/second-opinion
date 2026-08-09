#!/usr/bin/env python3
"""Persistent stream-json supervisor for one Codex-owned Claude worker."""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_worker import (
    WORKLOADS,
    WorkerError,
    atomic_write_json,
    build_stream_command,
    child_env,
    contains_model_unavailable,
    contains_subscription_limit,
    load_manifest,
    process_identity,
    save_manifest,
    state_lock,
    utc_now,
)


class Supervisor:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.manifest = load_manifest(worker_id)
        self.child: Optional[subprocess.Popen] = None
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.commands: "queue.Queue[Tuple[Dict[str, Any], queue.Queue]]" = queue.Queue()
        self.stop_requested = False
        self.server: Optional[socket.socket] = None
        self.socket_thread: Optional[threading.Thread] = None
        self.stdout_thread: Optional[threading.Thread] = None
        self.stderr_thread: Optional[threading.Thread] = None
        self.awaiting_ack: List[str] = []
        self.pending_controls: Dict[str, Dict[str, Any]] = {}
        self.protocol_deadline: Optional[float] = None
        self.warm_deadline: Optional[float] = None
        self.idle_deadline: Optional[float] = None
        self.last_event_at = time.monotonic()
        self.interrupt_sent = False
        self.retry_after_registration = 0
        self.probe_succeeded = False

    def refresh(self) -> Dict[str, Any]:
        self.manifest = load_manifest(self.worker_id)
        return self.manifest

    def update(self, **values: Any) -> Dict[str, Any]:
        with state_lock():
            manifest = load_manifest(self.worker_id)
            manifest.update(values)
            save_manifest(manifest)
        self.manifest = manifest
        return manifest

    def mutate(self, callback: Any) -> Dict[str, Any]:
        with state_lock():
            manifest = load_manifest(self.worker_id)
            callback(manifest)
            save_manifest(manifest)
        self.manifest = manifest
        return manifest

    def append_attempt(self, resume: bool, command: List[str], child_pid: int) -> None:
        def change(manifest: Dict[str, Any]) -> None:
            current = int(manifest.get("attempt", 0)) + 1
            manifest["attempt"] = current
            history = manifest.setdefault("attempt_history", [])
            history.append(
                {
                    "attempt": current,
                    "started_at": utc_now(),
                    "resume": resume,
                    "session_id": manifest.get("session_id"),
                    "runner_pid": os.getpid(),
                    "child_pid": child_pid,
                    "process_identity": process_identity(child_pid),
                    "argv": command[1:],
                    "status": "running",
                }
            )
            manifest["runner_pid"] = os.getpid()
            manifest["child_pid"] = child_pid
            manifest["process_identity"] = process_identity(os.getpid())
            manifest["state"] = "initializing"
            manifest["protocol_status"] = "pending"
        self.mutate(change)

    def finish_attempt(self, status: str, returncode: Optional[int] = None, detail: Optional[str] = None) -> None:
        def change(manifest: Dict[str, Any]) -> None:
            history = manifest.setdefault("attempt_history", [])
            if history:
                history[-1]["finished_at"] = utc_now()
                history[-1]["status"] = status
                history[-1]["returncode"] = returncode
                if detail:
                    history[-1]["detail"] = detail
            manifest["child_pid"] = None
        self.mutate(change)

    def prepare_socket(self) -> None:
        path = Path(str(self.manifest["control_socket"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        if path.exists() or path.is_socket():
            try:
                path.unlink()
            except OSError as exc:
                raise WorkerError("Cannot remove stale control socket: {}".format(exc)) from exc
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(16)
        server.settimeout(0.5)
        self.server = server
        self.socket_thread = threading.Thread(target=self.serve_socket, daemon=True)
        self.socket_thread.start()
        self.update(control_socket_ready=True, runner_pid=os.getpid(), process_identity=process_identity(os.getpid()))

    def serve_socket(self) -> None:
        assert self.server is not None
        while not self.stop_requested:
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle_connection, args=(connection,), daemon=True).start()

    def handle_connection(self, connection: socket.socket) -> None:
        response: Dict[str, Any]
        try:
            connection.settimeout(15)
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
            if request.get("owner_nonce") != self.manifest.get("owner_nonce"):
                raise WorkerError("control ownership nonce mismatch")
            reply: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
            self.commands.put((request, reply))
            response = reply.get(timeout=12)
        except (OSError, ValueError, UnicodeDecodeError, queue.Empty, WorkerError) as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            connection.sendall((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            connection.close()

    def start_child(self, resume: bool) -> None:
        command = build_stream_command(self.manifest, resume=resume)
        output_path = Path(str(self.manifest["output_path"]))
        stderr_path = Path(str(self.manifest["stderr_path"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(
            command,
            cwd=str(self.manifest["cwd"]),
            env=child_env(Path(str(self.manifest["event_log"]))),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.child = child
        self.append_attempt(resume, command, child.pid)
        assert child.stdout is not None and child.stderr is not None
        self.stdout_thread = threading.Thread(target=self.read_stream, args=("stdout", child.stdout, output_path), daemon=True)
        self.stderr_thread = threading.Thread(target=self.read_stream, args=("stderr", child.stderr, stderr_path), daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        self.last_event_at = time.monotonic()
        self.protocol_deadline = None
        self.interrupt_sent = False
        self.probe_succeeded = False
        request_id = self.control_request("initialize", {"hooks": None})
        self.pending_controls[request_id]["initialize"] = True
        self.protocol_deadline = time.monotonic() + 10.0

    def read_stream(self, source: str, stream: Any, path: Path) -> None:
        try:
            with path.open("a", encoding="utf-8") as handle:
                for line in stream:
                    handle.write(line)
                    handle.flush()
                    self.events.put((source, line.rstrip("\n")))
        except OSError as exc:
            self.events.put(("reader_error", {"source": source, "detail": str(exc)}))
        finally:
            self.events.put(("eof", source))

    def write_json(self, value: Dict[str, Any]) -> None:
        if self.child is None or self.child.poll() is not None or self.child.stdin is None:
            raise WorkerError("Claude stream is not active")
        self.child.stdin.write(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n")
        self.child.stdin.flush()

    def control_request(self, subtype: str, extra: Optional[Dict[str, Any]] = None) -> str:
        request_id = uuid.uuid4().hex
        request = {"subtype": subtype}
        if extra:
            request.update(extra)
        self.pending_controls[request_id] = {"subtype": subtype, "sent_at": time.monotonic()}
        self.write_json({"type": "control_request", "request_id": request_id, "request": request})
        return request_id

    def send_message(self, message_id: str) -> bool:
        self.refresh()
        for message in self.manifest.get("messages", []):
            if message.get("message_id") != message_id:
                continue
            if message.get("status") not in {"queued", "interrupted"}:
                return False
            self.write_json(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": str(message.get("text") or "")}],
                    },
                }
            )
            self.awaiting_ack.append(message_id)
            def change(manifest: Dict[str, Any]) -> None:
                for item in manifest.get("messages", []):
                    if item.get("message_id") == message_id:
                        item["status"] = "sent"
                        item["sent_at"] = utc_now()
                manifest["state"] = "running"
                manifest["blocker"] = None
            self.mutate(change)
            self.idle_deadline = None
            return True
        raise WorkerError("Unknown queued message: {}".format(message_id))

    def send_queued(self, *, continuation_if_empty: bool) -> int:
        self.refresh()
        queued = [item for item in self.manifest.get("messages", []) if item.get("status") in {"queued", "interrupted"}]
        if not queued and continuation_if_empty:
            message = {
                "message_id": uuid.uuid4().hex,
                "kind": "continuation",
                "source": "supervisor",
                "digest": "system-continuation",
                "text": "Continue the current task from the last safe point. Preserve prior decisions and report a structured result when complete.",
                "status": "queued",
                "created_at": utc_now(),
                "acknowledged_at": None,
            }
            def append(manifest: Dict[str, Any]) -> None:
                manifest.setdefault("messages", []).append(message)
            self.mutate(append)
            queued = [message]
        for item in queued:
            self.send_message(str(item["message_id"]))
        return len(queued)

    def acknowledge_next_message(self) -> None:
        if not self.awaiting_ack:
            return
        message_id = self.awaiting_ack.pop(0)
        def change(manifest: Dict[str, Any]) -> None:
            for item in manifest.get("messages", []):
                if item.get("message_id") == message_id:
                    item["status"] = "acknowledged"
                    item["acknowledged_at"] = utc_now()
            manifest["last_delivery_receipt"] = {"message_id": message_id, "at": utc_now()}
        self.mutate(change)

    def protocol_ready(self) -> None:
        if not self.probe_succeeded:
            return
        self.protocol_deadline = None
        self.update(protocol_status="ready", protocol_receipt={"get_context_usage": "success", "at": utc_now()})
        self.send_queued(continuation_if_empty=False)

    def protocol_failed(self, detail: str) -> None:
        self.update(state="protocol_unsupported", protocol_status="unsupported", blocker={"kind": "protocol", "detail": detail})
        self.terminate_child()
        self.stop_requested = True

    def handle_event(self, source: str, raw: Any) -> None:
        self.last_event_at = time.monotonic()
        if source == "stderr":
            text = str(raw)
            if contains_subscription_limit(text):
                self.billing_stop(text)
            elif contains_model_unavailable(text):
                self.update(state="model_unavailable", blocker={"kind": "model", "detail": "requested model unavailable"})
                self.terminate_child()
                self.stop_requested = True
            return
        if source != "stdout":
            return
        try:
            event = json.loads(str(raw))
        except ValueError:
            return
        if contains_subscription_limit(json.dumps(event, sort_keys=True)):
            self.billing_stop("usage-credit transition or limit signal in event stream")
            return
        event_type = str(event.get("type") or "")
        if event_type == "system" and event.get("subtype") == "init":
            emitted = event.get("session_id") or event.get("sessionId")
            expected = self.manifest.get("session_id")
            if emitted and str(emitted) != str(expected):
                self.protocol_failed("Claude emitted session {} instead of {}".format(emitted, expected))
                return
            self.update(session_registered=True, session_registered_at=utc_now(), emitted_session_id=emitted or expected)
            self.protocol_ready()
            return
        if event_type == "control_response":
            response = event.get("response") or {}
            request_id = response.get("request_id") or event.get("request_id")
            pending = self.pending_controls.pop(str(request_id), None)
            if pending and pending.get("initialize"):
                if response.get("subtype") == "success":
                    self.update(
                        protocol_initialize_receipt=self.safe_initialize_receipt(
                            response.get("response") or {}
                        ),
                        protocol_status="probing",
                    )
                    probe_id = self.control_request("get_context_usage")
                    self.pending_controls[probe_id]["protocol_probe"] = True
                    self.protocol_deadline = time.monotonic() + 5.0
                else:
                    self.protocol_failed("stream initialize was rejected")
            elif pending and pending.get("protocol_probe"):
                if response.get("subtype") == "success" or response.get("response") is not None:
                    self.probe_succeeded = True
                    self.protocol_ready()
                else:
                    self.protocol_failed("get_context_usage was rejected")
            return
        if event_type == "control_request":
            request = event.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                permission_id = str(event.get("request_id") or uuid.uuid4().hex)
                def change(manifest: Dict[str, Any]) -> None:
                    manifest.setdefault("permission_requests", {})[permission_id] = {
                        "permission_id": permission_id,
                        "status": "pending",
                        "tool_name": request.get("tool_name"),
                        "input_digest": self.safe_digest(request.get("input")),
                        "received_at": utc_now(),
                    }
                    manifest["state"] = "blocked"
                    manifest["blocker"] = {"kind": "permission", "permission_id": permission_id, "tool_name": request.get("tool_name")}
                self.mutate(change)
            return
        if event_type == "user":
            self.acknowledge_next_message()
            return
        if event_type == "result":
            self.handle_result(event)

    @staticmethod
    def safe_digest(value: Any) -> str:
        import hashlib
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def safe_initialize_receipt(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        account = value.get("account") if isinstance(value.get("account"), dict) else {}
        models = []
        for item in value.get("models", []):
            if isinstance(item, dict):
                models.append(
                    {
                        key: item.get(key)
                        for key in ("value", "resolvedModel", "supportedEffortLevels")
                        if key in item
                    }
                )
        agents = [
            {"name": item.get("name")}
            for item in value.get("agents", [])
            if isinstance(item, dict) and item.get("name")
        ]
        return {
            "account": {
                key: account.get(key)
                for key in ("apiProvider", "subscriptionType")
                if key in account
            },
            "current_permission_mode": value.get("current_permission_mode"),
            "commands": list(value.get("commands") or []),
            "agents": agents,
            "models": models,
            "pid": value.get("pid"),
        }

    def handle_result(self, event: Dict[str, Any]) -> None:
        result = event.get("structured_output")
        if not isinstance(result, dict):
            raw = event.get("result")
            if isinstance(raw, dict):
                result = raw
            else:
                result = {
                    "status": "pass" if event.get("subtype") == "success" else "block",
                    "summary": str(raw or "Claude worker completed without a structured summary"),
                    "changed_files": [],
                    "tests": [],
                    "blockers": [],
                    "branch": None,
                    "worktree": self.manifest.get("workspace", {}).get("worktree"),
                    "head": None,
                    "proposed_subtasks": [],
                    "lingering_processes": [],
                }
        result["worker_id"] = self.worker_id
        result["session_id"] = self.manifest.get("session_id")
        result["received_at"] = utc_now()
        atomic_write_json(Path(str(self.manifest["result_path"])), result)
        def change(manifest: Dict[str, Any]) -> None:
            for item in reversed(manifest.get("messages", [])):
                if item.get("status") in {"sent", "acknowledged"}:
                    item["status"] = "completed"
                    item["completed_at"] = utc_now()
                    break
            manifest["state"] = "completed"
            manifest["result_receipt"] = {"at": utc_now(), "path": manifest.get("result_path"), "status": result.get("status")}
            manifest["blocker"] = None
        self.mutate(change)
        self.idle_deadline = time.monotonic() + 300.0

    def billing_stop(self, detail: str) -> None:
        self.update(state="subscription_limit", blocker={"kind": "billing", "detail": detail}, billing_transition_at=utc_now())
        try:
            self.control_request("interrupt")
        except WorkerError:
            pass
        self.terminate_child()
        self.stop_requested = True

    def control_response(self, request_id: str, allow: bool, reason: Optional[str]) -> None:
        self.refresh()
        permission = self.manifest.get("permission_requests", {}).get(request_id)
        if not permission or permission.get("status") != "pending":
            raise WorkerError("Unknown or resolved permission request")
        behavior = "allow" if allow else "deny"
        response: Dict[str, Any] = {"behavior": behavior}
        if reason:
            response["message"] = reason
        self.write_json(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response,
                },
            }
        )
        def change(manifest: Dict[str, Any]) -> None:
            item = manifest.setdefault("permission_requests", {}).get(request_id, {})
            item["status"] = behavior
            item["resolved_at"] = utc_now()
            item["reason"] = reason
            manifest["state"] = "running"
            manifest["blocker"] = None
        self.mutate(change)

    def interrupt(self) -> None:
        if self.child is None or self.child.poll() is not None:
            return
        self.control_request("interrupt")
        self.interrupt_sent = True

    def terminate_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            if child.stdin:
                child.stdin.close()
        except OSError:
            pass
        try:
            child.terminate()
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                child.kill()
            except OSError:
                pass
        self.child = None

    def handle_command(self, request: Dict[str, Any]) -> Dict[str, Any]:
        action = str(request.get("action") or "")
        payload = request.get("payload") or {}
        self.refresh()
        state = str(self.manifest.get("state"))
        if action == "send":
            if state in {"idle", "completed", "warm_paused"}:
                return {"ok": True, "queued": True, "activated": False, "state": state}
            delivered = self.send_message(str(payload.get("message_id")))
            return {"ok": True, "delivered": delivered, "activated": False}
        if action == "activate":
            count = self.send_queued(continuation_if_empty=False)
            return {"ok": True, "activated": True, "messages_delivered": count, "session_id": self.manifest.get("session_id")}
        if action == "pause":
            if state == "warm_paused":
                return {"ok": True, "state": state, "session_id": self.manifest.get("session_id")}
            self.interrupt()
            warm_seconds = max(0.0, float(payload.get("warm_seconds", 300)))
            self.warm_deadline = time.monotonic() + warm_seconds
            self.update(state="warm_paused", paused_at=utc_now(), pause_kind="warm")
            return {"ok": True, "state": "warm_paused", "session_id": self.manifest.get("session_id"), "warm_seconds": warm_seconds}
        if action == "resume":
            self.warm_deadline = None
            count = self.send_queued(continuation_if_empty=True)
            self.update(state="running", resumed_at=utc_now(), pause_kind=None, blocker=None)
            return {"ok": True, "state": "running", "messages_delivered": count, "session_id": self.manifest.get("session_id")}
        if action in {"approve", "deny"}:
            self.control_response(str(payload.get("permission_id")), action == "approve", payload.get("reason"))
            return {"ok": True, "permission_id": payload.get("permission_id"), "decision": action}
        if action == "stop":
            try:
                self.interrupt()
            except WorkerError:
                pass
            self.update(state="stopped", stopped_at=utc_now(), result_disposition="cancelled")
            self.stop_requested = True
            return {"ok": True, "state": "stopped", "session_id": self.manifest.get("session_id")}
        if action == "release":
            self.stop_requested = True
            return {"ok": True, "state": state, "released": True, "session_id": self.manifest.get("session_id")}
        if action == "status":
            return {"ok": True, "state": state, "session_id": self.manifest.get("session_id")}
        raise WorkerError("Unknown supervisor action: {}".format(action))

    def process_commands(self) -> None:
        while True:
            try:
                request, reply = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                response = self.handle_command(request)
            except (WorkerError, OSError, ValueError) as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                reply.put_nowait(response)
            except queue.Full:
                pass

    def process_events(self) -> None:
        while True:
            try:
                source, value = self.events.get_nowait()
            except queue.Empty:
                return
            self.handle_event(source, value)

    def handle_child_exit(self) -> None:
        child = self.child
        if child is None:
            return
        returncode = child.poll()
        if returncode is None:
            return
        self.child = None
        self.refresh()
        state = str(self.manifest.get("state"))
        if self.stop_requested or state in {"stopped", "subscription_limit", "model_unavailable", "protocol_unsupported"}:
            self.finish_attempt(state, returncode)
            return
        if self.manifest.get("session_registered") and self.retry_after_registration < 1 and state not in {"completed", "cold_paused"}:
            self.finish_attempt("interrupted", returncode, "retrying original registered session once")
            self.retry_after_registration += 1
            self.start_child(resume=True)
            return
        if state == "completed":
            self.finish_attempt("completed", returncode)
            self.stop_requested = True
            return
        if self.manifest.get("session_registered"):
            self.finish_attempt("cold_paused", returncode)
            self.update(state="cold_paused", pause_kind="cold", blocker={"kind": "child_exit", "detail": "session preserved"})
        else:
            self.finish_attempt("failed", returncode)
            self.update(state="failed", failure_reason="Claude exited before session registration", blocker={"kind": "process", "returncode": returncode})
        self.stop_requested = True

    def watchdog(self) -> None:
        self.refresh()
        state = str(self.manifest.get("state"))
        now = time.monotonic()
        if self.protocol_deadline is not None and now >= self.protocol_deadline:
            self.protocol_failed("get_context_usage did not return within five seconds")
            return
        if self.warm_deadline is not None and now >= self.warm_deadline:
            self.terminate_child()
            self.finish_attempt("cold_paused", 0, "warm pause expired")
            self.update(state="cold_paused", pause_kind="cold", cold_paused_at=utc_now())
            self.stop_requested = True
            return
        if self.idle_deadline is not None and now >= self.idle_deadline:
            self.terminate_child()
            self.finish_attempt("completed", 0, "idle stream closed after five minutes")
            self.stop_requested = True
            return
        if state != "running" or self.manifest.get("protocol_status") != "ready":
            return
        timeout = float(WORKLOADS.get(str(self.manifest.get("workload")), WORKLOADS["standard"])["watchdog_seconds"])
        if now - self.last_event_at < timeout:
            return
        if not self.interrupt_sent:
            try:
                self.interrupt()
            except WorkerError:
                pass
            self.update(state="blocked", blocker={"kind": "stalled", "detail": "no event for {} seconds".format(int(timeout))})
            self.terminate_child()
            self.finish_attempt("blocked", None, "watchdog cold-pause")
            self.stop_requested = True

    def close(self) -> None:
        self.terminate_child()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
        path = Path(str(self.manifest.get("control_socket") or ""))
        if path.is_socket() or path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        self.update(control_socket_ready=False, runner_pid=None, child_pid=None)

    def run(self) -> int:
        self.prepare_socket()
        resume = bool(self.manifest.get("session_registered"))
        self.start_child(resume=resume)
        try:
            while not self.stop_requested:
                self.process_events()
                self.process_commands()
                self.handle_child_exit()
                self.watchdog()
                time.sleep(0.02)
            # Give a stop request's socket handler time to deliver its receipt.
            self.process_commands()
            time.sleep(0.05)
            return 0
        finally:
            self.close()


ACTIVE: Optional[Supervisor] = None


def signal_handler(_signum: int, _frame: Any) -> None:
    if ACTIVE is not None:
        ACTIVE.stop_requested = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args()
    global ACTIVE
    ACTIVE = Supervisor(args.worker_id)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    try:
        return ACTIVE.run()
    except (WorkerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        try:
            ACTIVE.update(state="failed", failure_reason=str(exc), blocker={"kind": "supervisor", "detail": str(exc)})
        except Exception:
            pass
        return 5


if __name__ == "__main__":
    raise SystemExit(main())

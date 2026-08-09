#!/usr/bin/env python3

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import claude_worker as worker


WORKER_CLI = SCRIPTS / "claude_worker.py"
POLICY_HOOK = SCRIPTS / "claude_policy_hook.py"


def constrained_gpt_review_command(*, sandbox: str = "read-only", model: str = "gpt-5.6-sol") -> str:
    return "\n".join(
        [
            'SANDBOX_MODE="{}"'.format(sandbox),
            "COMPLETION_REQUIREMENT='REVIEW STATUS: COMPLETE or REVIEW STATUS: INCOMPLETE'",
            "codex exec \\",
            "  -m {} \\".format(model),
            "  -c model_reasoning_effort=max \\",
            "  -c approval_policy=never \\",
            '  --sandbox "$SANDBOX_MODE" \\',
            "  --ephemeral \\",
            '  -o "$OUT_PATH" \\',
            '  "$COMPLETION_REQUIREMENT" \\',
            '  < "$PACKET_PATH"',
        ]
    )


class ModelAndPolicyTests(unittest.TestCase):
    def test_exact_sol_opus_mapping(self) -> None:
        self.assertEqual(worker.resolve_effort("gpt-5.6-sol", "high", "claude-opus-5", None), "medium")
        self.assertEqual(worker.resolve_effort("gpt-5.6-sol", "xhigh", "claude-opus-5", None), "high")
        self.assertEqual(worker.resolve_effort("gpt-5.6-sol", "max", "claude-opus-5", None), "xhigh")
        with self.assertRaises(worker.WorkerError):
            worker.resolve_effort("gpt-5.6-terra", "high", "claude-opus-5", None)

    def test_non_opus_requires_explicit_effort(self) -> None:
        with self.assertRaises(worker.WorkerError):
            worker.resolve_effort("gpt-5.6-sol", "xhigh", "claude-fable-5", None)
        self.assertEqual(worker.resolve_effort("gpt-5.6-sol", "xhigh", "claude-fable-5", "max"), "max")

    def test_aliases_and_new_opus_notice(self) -> None:
        self.assertEqual(worker.normalize_model("opus"), "claude-opus-5")
        self.assertEqual(worker.normalize_model("fable-5"), "claude-fable-5")
        with self.assertRaises(worker.WorkerError):
            worker.normalize_model("fable;touch")
        with tempfile.NamedTemporaryFile() as executable:
            executable.write(b"claude-opus-5 claude-opus-5-20260801 claude-opus-6")
            executable.flush()
            found = worker.opus_model_discovery(executable.name)
        self.assertEqual(found["newer_models"], ["claude-opus-6"])
        self.assertIn("Tell the human", found["notice"])

    def test_authority_matrix_and_no_bypass(self) -> None:
        unrestricted = worker.authority_profile("never", "danger-full-access", "enabled")
        self.assertEqual(unrestricted["permission_mode"], "dontAsk")
        self.assertFalse(unrestricted["claude_sandbox"]["enabled"])
        self.assertIn("Bash", unrestricted["preapproved_tools"])
        self.assertIn("Skill", unrestricted["preapproved_tools"])
        self.assertIn("WebFetch", unrestricted["tools"])
        workspace = worker.authority_profile("never", "workspace-write", "disabled")
        self.assertTrue(workspace["claude_sandbox"]["enabled"])
        self.assertNotIn("WebFetch", workspace["tools"])
        readonly = worker.authority_profile("never", "read-only", "disabled")
        self.assertNotIn("Edit", readonly["tools"])
        self.assertIn("Bash", readonly["tools"])
        mediated = worker.authority_profile("on-request", "workspace-write", "enabled")
        self.assertEqual(mediated["permission_mode"], "manual")
        self.assertEqual(mediated["preapproved_tools"], [])
        for profile in (unrestricted, workspace, readonly, mediated):
            self.assertFalse(profile["bypass_permissions"])
        with self.assertRaises(worker.WorkerError):
            worker.authority_profile(None, "read-only", "disabled")
        scoped = worker.apply_task_scope(unrestricted, "read-only", Path("/tmp/repo"))
        self.assertEqual(scoped["tools"], unrestricted["tools"])
        self.assertEqual(scoped["preapproved_tools"], unrestricted["preapproved_tools"])
        self.assertEqual(scoped["claude_sandbox"], unrestricted["claude_sandbox"])
        self.assertFalse(scoped["claude_sandbox"]["enabled"])
        self.assertEqual(scoped["task_write_boundary"]["mode"], "behavioral_contract")
        unrestricted_settings = worker.hook_settings(Path("/tmp/events"), scoped, Path("/tmp/repo"), [])
        self.assertFalse(unrestricted_settings["sandbox"]["enabled"])
        self.assertNotIn("Skill", unrestricted_settings["permissions"]["deny"])
        readonly_scoped = worker.apply_task_scope(readonly, "read-only", Path("/tmp/repo"))
        readonly_settings = worker.hook_settings(Path("/tmp/events"), readonly_scoped, Path("/tmp/repo"), [])
        self.assertTrue(readonly_settings["sandbox"]["enabled"])
        self.assertEqual(readonly_settings["sandbox"]["filesystem"]["denyWrite"], ["/tmp/repo"])

    def test_child_environment_preserves_parent_tool_auth_and_tls_context(self) -> None:
        inherited = {
            "PATH": "/usr/bin:/bin",
            "GH_CONFIG_DIR": "/tmp/gh-config",
            "HTTPS_PROXY": "http://127.0.0.1:1234",
            "SSL_CERT_FILE": "/tmp/parent-ca.pem",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            child = worker.child_env()
        for name, value in inherited.items():
            self.assertEqual(child[name], value)
        self.assertEqual(child["CLAUDE_WORKER_MANAGED"], "1")

    def test_stream_command_is_persistent_isolated_and_resumable(self) -> None:
        manifest = {
            "worker_id": "cw-test",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "model": "claude-opus-5",
            "effort": "high",
            "settings_path": "/tmp/settings.json",
            "mcp_path": "/tmp/mcp.json",
            "scope": "read-only",
            "owned_paths": [],
            "isolation": "shared",
            "additional_directories": [],
            "authority": worker.authority_profile("never", "read-only", "disabled"),
        }
        with mock.patch.object(worker, "claude_path", return_value="/fake/claude"):
            command = worker.build_stream_command(manifest, resume=False)
            resumed = worker.build_stream_command(manifest, resume=True)
        self.assertIn("--input-format", command)
        self.assertIn("--replay-user-messages", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--setting-sources", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "user,project,local")
        self.assertIn("--no-chrome", command)
        self.assertNotIn("--disable-slash-commands", command)
        self.assertIn("Skill", command[command.index("--tools") + 1].split(","))
        self.assertNotIn("Skill", command[command.index("--disallowedTools") + 1].split(","))
        self.assertIn("--session-id", command)
        self.assertNotIn("--resume", command)
        self.assertIn("--resume", resumed)
        self.assertNotIn("--session-id", resumed)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("bypassPermissions", command)

    def test_worker_contract_unconditionally_substitutes_gpt_reviewer(self) -> None:
        contract = worker.worker_contract(
            "cw-test",
            "read-only",
            [],
            worker.authority_profile("never", "read-only", "disabled"),
        )
        self.assertIn("Use all installed Claude Code skills normally", contract)
        self.assertIn("substitute /gpt-second-opinion unconditionally", contract)
        self.assertIn("Invoke the installed /gpt-second-opinion skill normally", contract)
        self.assertIn("Never invoke /claude-second-opinion", contract)
        self.assertIn("Do not invoke raw Codex for implementation or general delegation", contract)
        self.assertNotIn("gpt_second_opinion.py", contract)
        self.assertIn("Do not invent missing transcript or artifact context", contract)
        self.assertIn("reviews receipt", contract)
        self.assertEqual(
            worker.RESULT_SCHEMA["properties"]["reviews"]["items"]["properties"]["reviewer_skill"]["enum"],
            ["gpt-second-opinion"],
        )

    def test_billing_unknown_requires_per_worker_authorization(self) -> None:
        with self.assertRaises(worker.WorkerError) as caught:
            worker.billing_decision("cw-test", "claude-fable-5", "max", False, None)
        self.assertIn("needs_billing_confirmation", str(caught.exception))
        value = worker.billing_decision("cw-test", "claude-fable-5", "max", True, "Human said use Fable 5 max")
        self.assertEqual(value["decision"], "per_worker_usage_credit_exception")
        self.assertEqual(value["worker_id"], "cw-test")
        self.assertEqual(worker.billing_decision("cw-test", "claude-opus-5", "high", False, None)["decision"], "subscription_only")

    def test_socket_path_is_short_and_private(self) -> None:
        path = worker.socket_path_for("cw-" + "x" * 200)
        self.assertLessEqual(len(os.fsencode(str(path))), 100)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_claude_path_is_portable_and_overrideable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "claude"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": temp}, clear=True):
                self.assertEqual(worker.claude_path(), str(executable.resolve()))
            with mock.patch.dict(os.environ, {"PATH": "", "CLAUDE_WORKER_CLAUDE": str(executable)}, clear=True):
                self.assertEqual(worker.claude_path(), str(executable.resolve()))
            with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
                with self.assertRaisesRegex(worker.WorkerError, "on PATH"):
                    worker.claude_path()

    def test_selective_migration_keeps_rejected_replacement_cancelled(self) -> None:
        value = {"worker_id": next(iter(worker.PERMANENTLY_CANCELLED)), "state": "stopped", "result_disposition": "pending"}
        migrated, changed = worker._migrate_manifest(value)
        self.assertTrue(changed)
        self.assertEqual(migrated["state"], "cancelled")
        self.assertEqual(migrated["result_disposition"], "cancelled")
        legacy = {"worker_id": "cw-legacy", "state": "stopped"}
        migrated, _ = worker._migrate_manifest(legacy)
        self.assertEqual(migrated["state"], "cold_paused")
        rejected = {"worker_id": "cw-rejected", "state": "stopped", "result_disposition": "rejected"}
        migrated, _ = worker._migrate_manifest(rejected)
        self.assertEqual(migrated["state"], "stopped")

    def test_legacy_result_is_recovered_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            worker_dir = state / "workers" / "cw-legacy-result"
            worker_dir.mkdir(parents=True)
            output_path = worker_dir / "output.jsonl"
            output_path.write_text(json.dumps({
                "type": "result",
                "subtype": "success",
                "session_id": "legacy-session",
                "result": "CLAUDE_WORKER_RESULT\nStatus: PASS\nLegacy completion",
            }) + "\n", encoding="utf-8")
            (worker_dir / "manifest.json").write_text(json.dumps({
                "manifest_version": 2,
                "worker_id": "cw-legacy-result",
                "state": "completed",
                "session_id": "legacy-session",
                "output_path": str(output_path),
                "runner_pid": None,
                "result_disposition": "pending",
            }), encoding="utf-8")
            env = dict(os.environ)
            env["CLAUDE_WORKER_STATE_DIR"] = str(state)
            result = subprocess.run(
                [sys.executable, str(WORKER_CLI), "result", "cw-legacy-result"],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["status"], "pass")
            self.assertTrue(value["legacy_recovered"])
            manifest = json.loads((worker_dir / "manifest.json").read_text())
            self.assertTrue(Path(manifest["result_path"]).is_file())

    def test_capacity_counts_warm_pause_fractionally(self) -> None:
        snapshot = {
            "performance_cores": 12,
            "logical_cores": 16,
            "total_memory_bytes": 128 * 1024**3,
            "available_memory_bytes": 100 * 1024**3,
            "load1": 2.0,
            "load5": 2.0,
            "load15": 2.0,
            "disk_free_bytes": 200 * 1024**3,
        }
        with mock.patch.object(worker, "active_claude_weight", return_value=(0.25, 1)):
            value = worker.calculate_capacity(snapshot, native_active=3, workload="standard")
        self.assertEqual(value["safe_additional_this_wave"], 2)
        self.assertEqual(value["current_count"], 4)
        self.assertIsNone(value["human_ceiling"]["supplied_max_total_worker_lanes"])
        self.assertIsNone(value["human_ceiling"]["remaining_capacity"])
        self.assertFalse(value["human_ceiling"]["binding"])
        self.assertNotIn("human_ceiling", value["gates"])

    def test_total_worker_lane_ceiling_reports_arithmetic_and_binding(self) -> None:
        snapshot = {
            "performance_cores": 12,
            "logical_cores": 16,
            "total_memory_bytes": 128 * 1024**3,
            "available_memory_bytes": 100 * 1024**3,
            "load1": 2.0,
            "load5": 2.0,
            "load15": 2.0,
            "disk_free_bytes": 200 * 1024**3,
        }
        with mock.patch.object(worker, "active_claude_weight", return_value=(1.0, 1)):
            constrained = worker.calculate_capacity(
                snapshot,
                native_active=3,
                workload="standard",
                max_total_worker_lanes=4,
            )
            unconstrained = worker.calculate_capacity(
                snapshot,
                native_active=3,
                workload="standard",
                max_total_worker_lanes=10,
            )
        receipt = constrained["human_ceiling"]
        self.assertEqual(receipt["supplied_max_total_worker_lanes"], 4)
        self.assertEqual(receipt["current_count"], 4)
        self.assertEqual(receipt["native_active"], 3)
        self.assertEqual(receipt["active_claude"], 1)
        self.assertEqual(receipt["remaining_capacity"], 0)
        self.assertFalse(receipt["primary_codex_included"])
        self.assertTrue(receipt["binding"])
        self.assertTrue(receipt["reduced_capacity"])
        self.assertEqual(constrained["safe_additional_this_wave"], 0)
        self.assertIn("human_ceiling", constrained["gates"])
        self.assertEqual(unconstrained["safe_additional_this_wave"], 2)
        self.assertFalse(unconstrained["human_ceiling"]["binding"])
        self.assertFalse(unconstrained["human_ceiling"]["reduced_capacity"])
        self.assertNotIn("human_ceiling", unconstrained["gates"])

    def test_total_worker_lane_ceiling_flag_and_deprecated_alias(self) -> None:
        parser = worker.build_parser()
        base = ["capacity", "--cwd", "/tmp", "--native-active", "3"]
        current = parser.parse_args(base + ["--max-total-worker-lanes", "6"])
        legacy = parser.parse_args(base + ["--max-workers", "6"])
        self.assertEqual(current.max_total_worker_lanes, 6)
        self.assertEqual(legacy.max_total_worker_lanes, 6)
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(base + ["--max-total-worker-lanes", "6", "--max-workers", "6"])
        with self.assertRaises(argparse.ArgumentTypeError):
            worker.nonnegative_int("-1")
        help_result = subprocess.run(
            [sys.executable, str(WORKER_CLI), "capacity", "--help"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        normalized_help = " ".join(help_result.stdout.split())
        self.assertIn("combined active native Codex", normalized_help)
        self.assertIn("excludes the primary Codex orchestrator", normalized_help)
        self.assertIn("deprecated alias for --max-total-worker-lanes", normalized_help)


class HookTests(unittest.TestCase):
    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(POLICY_HOOK)], input=json.dumps(payload), text=True, capture_output=True)

    def test_subworker_tools_and_harnesses_are_blocked(self) -> None:
        for tool_name in ("Agent", "Task", "TeamCreate", "TeamDelete", "Teammate", "SendMessage"):
            self.assertEqual(self.run_hook({"tool_name": tool_name, "tool_input": {}}).returncode, 2)
        for command in (
            "claude -p hi",
            "/usr/local/bin/codex exec hi",
            "gemini run",
            "aider file.py",
            "goose run hi",
            "hermes chat",
        ):
            self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": command}}).returncode, 2)

    def test_installed_skills_and_gpt_reviewer_skill_are_allowed(self) -> None:
        for name in ("code-review", "frontend-design", "gpt-second-opinion"):
            result = self.run_hook({"tool_name": "Skill", "tool_input": {"skill": name}})
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_reviewer_skill_is_redirected(self) -> None:
        result = self.run_hook({"tool_name": "Skill", "tool_input": {"skill": "claude-second-opinion"}})
        self.assertEqual(result.returncode, 2)
        self.assertIn("substitute /gpt-second-opinion", result.stderr)

    def test_constrained_gpt_reviewer_command_is_allowed(self) -> None:
        result = self.run_hook({"tool_name": "Bash", "tool_input": {"command": constrained_gpt_review_command()}})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_near_miss_gpt_reviewer_commands_are_blocked(self) -> None:
        variants = (
            constrained_gpt_review_command(model="gpt-5.5"),
            constrained_gpt_review_command(sandbox="danger-full-access"),
            constrained_gpt_review_command().replace("REVIEW STATUS: INCOMPLETE", "review incomplete"),
            constrained_gpt_review_command() + "\nclaude -p nested",
        )
        for command in variants:
            with self.subTest(command=command):
                self.assertEqual(
                    self.run_hook({"tool_name": "Bash", "tool_input": {"command": command}}).returncode,
                    2,
                )

    def test_normal_command_is_allowed(self) -> None:
        self.assertEqual(self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git status"}}).returncode, 0)


class PersistentFakeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(self.repo), check=True)
        self.fake = self.root / "claude"
        self.invocations = self.root / "invocations.jsonl"
        self.fake.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ['auth', 'status', '--json']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'}))
    raise SystemExit(0)
if '--version' in args:
    print('2.1.226 (fake)')
    raise SystemExit(0)
if '--help' in args:
    print('--print -p --output-format stream-json --input-format --replay-user-messages --session-id --resume --model --effort --strict-mcp-config --setting-sources')
    raise SystemExit(0)

session = args[args.index('--session-id') + 1] if '--session-id' in args else args[args.index('--resume') + 1]
with open(os.environ['FAKE_CLAUDE_INVOCATIONS'], 'a') as log:
    log.write(json.dumps({'session': session, 'resume': '--resume' in args, 'args': args}) + '\\n')
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session}), flush=True)
pending_permission = None
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if message.get('type') == 'control_request':
        request = message.get('request', {})
        subtype = request.get('subtype')
        response = {'type': 'control_response', 'response': {'subtype': 'success', 'request_id': message.get('request_id'), 'response': {'context_window': 100000} if subtype == 'get_context_usage' else {'interrupted': True}}}
        print(json.dumps(response), flush=True)
        continue
    if message.get('type') == 'control_response':
        if pending_permission:
            result = {'status': 'pass', 'summary': 'permission resolved', 'changed_files': [], 'tests': [], 'blockers': [], 'branch': None, 'worktree': None, 'head': None, 'proposed_subtasks': [], 'lingering_processes': []}
            print(json.dumps({'type': 'result', 'subtype': 'success', 'structured_output': result, 'session_id': session}), flush=True)
            pending_permission = None
        continue
    if message.get('type') != 'user':
        continue
    print(json.dumps(message), flush=True)
    content = message.get('message', {}).get('content', [])
    text = content[0].get('text', '') if content else ''
    if 'CREDIT_SIGNAL' in text:
        print(json.dumps({'type': 'error', 'error': 'Continue with usage credits / extra usage'}), flush=True)
        continue
    if 'PERMISSION' in text:
        pending_permission = 'permission-1'
        print(json.dumps({'type': 'control_request', 'request_id': pending_permission, 'request': {'subtype': 'can_use_tool', 'tool_name': 'Bash', 'input': {'command': 'git status'}}}), flush=True)
        continue
    if 'HOLD' in text and 'Continue the current task' not in text:
        continue
    result = {'status': 'pass', 'summary': text[:80], 'changed_files': [], 'tests': [{'command': 'true', 'exit_code': 0}], 'blockers': [], 'branch': None, 'worktree': None, 'head': None, 'proposed_subtasks': [], 'lingering_processes': []}
    print(json.dumps({'type': 'result', 'subtype': 'success', 'structured_output': result, 'session_id': session}), flush=True)
""",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env["CLAUDE_WORKER_CLAUDE"] = str(self.fake)
        self.env["CLAUDE_WORKER_STATE_DIR"] = str(self.state)
        self.env["FAKE_CLAUDE_INVOCATIONS"] = str(self.invocations)
        for name in worker.PROHIBITED_ENV:
            self.env.pop(name, None)

    def tearDown(self) -> None:
        if self.state.is_dir():
            for manifest_path in self.state.glob("workers/cw-*/manifest.json"):
                try:
                    value = json.loads(manifest_path.read_text())
                    pid = value.get("runner_pid")
                    if pid:
                        os.kill(int(pid), signal.SIGTERM)
                except (OSError, ValueError, TypeError):
                    pass
        time.sleep(0.2)
        self.temp.cleanup()

    def run_cli(self, *args: str, input_text: str = "", timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(WORKER_CLI), *args], input=input_text, text=True, capture_output=True, env=self.env, timeout=timeout)

    def spawn(self, task: str = "finish", extra: tuple = ()) -> dict:
        result = self.run_cli(
            "spawn",
            "--cwd", str(self.repo),
            "--activation", "explicit-claude",
            "--native-active", "0",
            "--native-free-slots", "3",
            "--codex-model", "gpt-5.6-sol",
            "--codex-effort", "xhigh",
            "--codex-approval-policy", "never",
            "--codex-sandbox", "danger-full-access",
            "--network", "enabled",
            "--max-total-worker-lanes", "1",
            "--name", "test",
            *extra,
            input_text=task,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def wait_state(self, worker_id: str, wanted: set, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.run_cli("status", worker_id)
            value = json.loads(result.stdout)
            if value.get("state") in wanted:
                return value
            time.sleep(0.05)
        self.fail("worker did not reach {}; last={}".format(wanted, value))

    def test_spawn_wait_result_and_receipts(self) -> None:
        spawned = self.spawn("finish normally")
        worker_id = spawned["worker_id"]
        completed = self.wait_state(worker_id, {"completed"})
        self.assertEqual(completed["manifest_version"], 2)
        self.assertTrue(completed["session_registered"])
        self.assertEqual(completed["protocol_status"], "ready")
        self.assertEqual(
            completed["review_policy"]["substitution"],
            {"claude-second-opinion": "gpt-second-opinion"},
        )
        self.assertEqual(
            completed["review_policy"]["installed_skills"],
            "allowed_except_claude_second_opinion_substitution",
        )
        self.assertEqual(completed["review_policy"]["skill_invocation"], "/gpt-second-opinion")
        self.assertEqual(
            completed["review_policy"]["raw_agent_harnesses"],
            "denied_except_constrained_gpt_review",
        )
        self.assertFalse(completed["orchestration_policy"]["overflow_mode"])
        self.assertEqual(completed["orchestration_policy"]["primary_codex_role_default"], "orchestrator_or_worker")
        self.assertEqual(completed["messages"][0]["status"], "completed")
        result = self.run_cli("result", worker_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")

    def test_overflow_defaults_primary_codex_to_orchestrator_only(self) -> None:
        spawned = self.spawn(
            "finish overflow lane",
            extra=("--activation", "maximal", "--native-free-slots", "0"),
        )
        policy = spawned["orchestration_policy"]
        self.assertTrue(policy["overflow_mode"])
        self.assertEqual(policy["primary_codex_role_default"], "orchestrator_only")
        self.assertTrue(policy["material_primary_lane_requires_human_override"])

    def test_three_warm_pause_resume_cycles_keep_ids(self) -> None:
        spawned = self.spawn("HOLD the first turn")
        worker_id = spawned["worker_id"]
        session_id = spawned["session_id"]
        for _ in range(3):
            paused = self.run_cli("pause", worker_id, "--warm-seconds", "5")
            self.assertEqual(paused.returncode, 0, paused.stderr)
            resumed = self.run_cli(
                "resume", worker_id,
                "--native-active", "0",
                "--max-total-worker-lanes", "1",
                "--capacity-retry-seconds", "1",
                "--capacity-retry-interval", "0.05",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(json.loads(resumed.stdout)["session_id"], session_id)
            self.wait_state(worker_id, {"completed"})
        final = self.run_cli("status", worker_id)
        value = json.loads(final.stdout)
        self.assertEqual(value["worker_id"], worker_id)
        self.assertEqual(value["session_id"], session_id)

    def test_cold_resume_uses_original_session(self) -> None:
        spawned = self.spawn("HOLD for cold pause")
        worker_id = spawned["worker_id"]
        session_id = spawned["session_id"]
        paused = self.run_cli("pause", worker_id, "--warm-seconds", "0")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.wait_state(worker_id, {"cold_paused"})
        resumed = self.run_cli(
            "resume", worker_id,
            "--native-active", "0",
            "--max-total-worker-lanes", "1",
            "--capacity-retry-seconds", "1",
            "--capacity-retry-interval", "0.05",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.wait_state(worker_id, {"completed"})
        invocations = [json.loads(line) for line in self.invocations.read_text().splitlines()]
        self.assertGreaterEqual(len(invocations), 2)
        self.assertEqual({item["session"] for item in invocations}, {session_id})
        self.assertTrue(invocations[-1]["resume"])

    def test_send_queues_idle_and_followup_activates(self) -> None:
        spawned = self.spawn("finish once")
        worker_id = spawned["worker_id"]
        self.wait_state(worker_id, {"completed"})
        sent = self.run_cli("send", worker_id, "--message", "queued only")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertFalse(json.loads(sent.stdout)["receipt"]["activated"])
        status = json.loads(self.run_cli("status", worker_id).stdout)
        self.assertEqual(status["messages"][-1]["status"], "queued")
        followup = self.run_cli(
            "followup", worker_id,
            "--message", "activate now",
            "--native-active", "0",
            "--max-total-worker-lanes", "1",
            "--capacity-retry-seconds", "1",
            "--capacity-retry-interval", "0.05",
        )
        self.assertEqual(followup.returncode, 0, followup.stderr)
        self.wait_state(worker_id, {"completed"})

    def test_mediated_permission_approve(self) -> None:
        spawned = self.spawn("PERMISSION please", extra=("--codex-approval-policy", "on-request"))
        worker_id = spawned["worker_id"]
        blocked = self.wait_state(worker_id, {"blocked"})
        permission_id = blocked["blocker"]["permission_id"]
        approved = self.run_cli("approve", worker_id, permission_id, "--reason", "within parent authority")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.wait_state(worker_id, {"completed"})

    def test_stop_is_final_and_not_resumable(self) -> None:
        spawned = self.spawn("HOLD forever")
        worker_id = spawned["worker_id"]
        stopped = self.run_cli("stop", worker_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        resumed = self.run_cli(
            "resume", worker_id,
            "--native-active", "0",
            "--max-total-worker-lanes", "1",
            "--capacity-retry-seconds", "0",
            "--capacity-retry-interval", "0.01",
        )
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn("permanently stopped", resumed.stderr)

    def test_billing_signal_stops_worker(self) -> None:
        spawned = self.spawn("CREDIT_SIGNAL")
        value = self.wait_state(spawned["worker_id"], {"subscription_limit"})
        self.assertEqual(value["blocker"]["kind"], "billing")

    def test_redacted_logs_and_disposition_gated_cleanup(self) -> None:
        spawned = self.spawn("secret payload")
        worker_id = spawned["worker_id"]
        self.wait_state(worker_id, {"completed"})
        logs = self.run_cli("logs", worker_id)
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertNotIn("secret payload", logs.stdout)
        cleanup = self.run_cli("cleanup", "--older-than-days", "0")
        self.assertIn(worker_id, [item["worker_id"] for item in json.loads(cleanup.stdout)["skipped"]])
        disposed = self.run_cli("dispose", worker_id, "--disposition", "retained")
        self.assertEqual(disposed.returncode, 0, disposed.stderr)
        cleanup = self.run_cli("cleanup", "--older-than-days", "0")
        self.assertIn(worker_id, json.loads(cleanup.stdout)["removed"])


if __name__ == "__main__":
    unittest.main()

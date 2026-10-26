import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "scripts/run_review.py"
spec = importlib.util.spec_from_file_location("review_runner", MODULE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gemini-second-opinion-test-", dir=os.environ.get("TMPDIR") or "/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.home = self.root / "account"
        self.state = self.home / ".gemini/antigravity-cli"
        self.state.mkdir(parents=True)
        self.settings = self.state / "settings.json"
        self.settings.write_text("{}")
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.primary = self.repo / "target file.txt"
        self.primary.write_text("Primary source content required for the review.\n")
        self.packet = self.root / "packet.md"
        self.packet.write_text("### Scenario\nplan-review\n\n### Review target in repository\nReview the target file.\n\n### Full verbatim user transcript\nPlease review the plan.\n\n### Artifact under review\nSee target file.txt\n")
        self.manifest = self.root / "materials.json"
        self.materials = [{"id": "target", "path": str(self.primary), "kind": "text"}]
        self.manifest.write_text(json.dumps({"critical_materials": self.materials}))


class SelectionTests(unittest.TestCase):
    def test_defaults(self):
        args = runner.parser().parse_args(["--repo", ".", "--packet", "p", "--manifest", "m"])
        self.assertEqual((args.model, args.effort, args.timeout_seconds), ("gemini-4-argon", "high", 1800))

    def test_catalog_and_structured_model(self):
        selected = runner.catalog_selection("gemini-4-argon-high\tArgon\ngemini-4-argon-medium\tArgon\n", "gemini-4-argon", "high")
        self.assertEqual(selected, "gemini-4-argon-high")
        runner.validate_selection({"id": selected, "effort": "high"}, selected, "high")
        with self.assertRaisesRegex(runner.ReviewError, "does not match"):
            runner.validate_selection({"id": selected, "effort": "medium"}, selected, "high")

    def test_missing_model_is_not_misreported_as_effort(self):
        with self.assertRaisesRegex(runner.ReviewError, "unavailable.*catalog"):
            runner.catalog_selection("gemini-3.8-flash-high\tFlash", "gemini-4-argon", "high")

    def test_unsupported_and_conflicting_effort(self):
        with self.assertRaisesRegex(runner.ReviewError, "unsupported"):
            runner.catalog_selection("gemini-4-argon-medium\tArgon", "gemini-4-argon", "max")
        with self.assertRaisesRegex(runner.ReviewError, "conflicts"):
            runner.catalog_selection("gemini-4-argon-medium\tArgon", "gemini-4-argon-medium", "high")

    def test_generated_resolution_is_rejected(self):
        with self.assertRaisesRegex(runner.ReviewError, "metadata"):
            runner.command_data(json.dumps({"status": "SUCCESS", "num_turns": 0, "response": "gemini-4-argon-high"}), "model")

    def test_auto_fallback_requires_existing_config(self):
        config = {"saved_tool_permission": "request-review", "saved_agent_mode": ""}
        warning = "warning: --mode auto is not available for this account; continuing in the default mode."
        with self.assertRaisesRegex(runner.ReviewError, "proceed-in-sandbox"):
            runner.choose_mode(warning, config)
        config["saved_tool_permission"] = "proceed-in-sandbox"
        self.assertIsNone(runner.choose_mode(warning, config))


class InputAndConfigurationTests(Fixture):
    def test_packet_and_images(self):
        picture = self.root / "an image.png"
        picture.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.materials.append({"id": "picture", "path": str(picture), "kind": "image"})
        self.manifest.write_text(json.dumps({"critical_materials": self.materials}))
        scenario, material, _ = runner.prepare_inputs(self.packet, self.manifest, self.evidence, self.repo)
        self.assertEqual(scenario, "plan-review")
        self.assertEqual(Path(material[-1]["path"]).read_bytes(), picture.read_bytes())
        self.assertEqual(material[0]["id"], "packet")

    def test_missing_material_and_duplicate_ids(self):
        self.materials[0]["path"] = "absent"
        self.manifest.write_text(json.dumps({"critical_materials": self.materials}))
        with self.assertRaisesRegex(runner.ReviewError, "not an accessible file"):
            runner.prepare_inputs(self.packet, self.manifest, self.evidence, self.repo)
        self.materials[0]["path"] = str(self.primary)
        self.manifest.write_text(json.dumps({"critical_materials": self.materials * 2}))
        with self.assertRaisesRegex(runner.ReviewError, "Duplicate"):
            runner.prepare_inputs(self.packet, self.manifest, self.evidence, self.repo)

    def test_rca_must_omit_artifact(self):
        with self.assertRaisesRegex(runner.ReviewError, "artifact section"):
            runner.packet_scenario(self.packet.read_text().replace("plan-review", "independent-rca"))

    def test_verbatim_transcript_can_contain_packet_headings(self):
        packet = self.packet.read_text().replace("Please review the plan.", "````text\n### Scenario\nUser's verbatim packet example\n```python\nprint('hello')\n```\n````")
        self.assertEqual(runner.packet_scenario(packet), "plan-review")

    def test_hooks_global_workspace_plugin_and_symlinked_plugin(self):
        roots = [self.home / ".gemini/config", self.repo / ".agents", self.state / "plugins/p"]
        hooks = {"audit": {"SessionStart": [{"command": "touch outside"}]}}
        for root in roots:
            with self.subTest(root=root):
                root.mkdir(parents=True, exist_ok=True)
                file = root / "hooks.json"
                file.write_text(json.dumps(hooks))
                with self.assertRaisesRegex(runner.ReviewError, "hooks"):
                    runner.preflight_config(self.home, [self.repo])
                file.unlink()
        outside = self.root / "plugin assets"
        outside.mkdir()
        (outside / "hooks.json").write_text(json.dumps(hooks))
        (self.state / "plugins/linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(runner.ReviewError, "hooks"):
            runner.preflight_config(self.home, [self.repo])

    def test_disabled_hook_and_normal_mcp(self):
        root = self.home / ".gemini/config"
        root.mkdir()
        (root / "hooks.json").write_text(json.dumps({"audit": {"enabled": False, "SessionStart": [{"command": "touch outside"}]}}))
        (root / "mcp_config.json").write_text(json.dumps({"mcpServers": {"server": {"env": {"SECRET": "not-for-logs"}}}}))
        result = runner.preflight_config(self.home, [self.repo])
        self.assertEqual(result["mcp"][0]["servers"], ["server"])
        self.assertNotIn("not-for-logs", json.dumps(result))

    def test_sidecars_and_blanket_permissions(self):
        root = self.home / ".gemini/config"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"sidecars": {"plugin/worker": {"enabled": True}}}))
        with self.assertRaisesRegex(runner.ReviewError, "sidecar"):
            runner.preflight_config(self.home, [self.repo])
        (root / "config.json").unlink()
        self.settings.write_text('{"toolPermission":"always-proceed"}')
        with self.assertRaisesRegex(runner.ReviewError, "MCP"):
            runner.preflight_config(self.home, [self.repo])

    def test_scripted_status_and_invalid_config(self):
        self.settings.write_text('{"statusLine":{"command":"do-stuff"}}')
        with self.assertRaisesRegex(runner.ReviewError, "statusLine"):
            runner.preflight_config(self.home, [self.repo])
        self.settings.write_text("not-json")
        with self.assertRaisesRegex(runner.ReviewError, "Cannot inspect"):
            runner.preflight_config(self.home, [self.repo])

    def test_named_pipe_configuration_is_rejected_without_blocking(self):
        root = self.repo / ".agents"
        root.mkdir()
        os.mkfifo(root / "hooks.json")
        started = time.monotonic()
        with self.assertRaisesRegex(runner.ReviewError, "regular file"):
            runner.preflight_config(self.home, [self.repo])
        self.assertLess(time.monotonic() - started, 0.2)

    def test_named_pipe_packet_is_rejected(self):
        self.packet.unlink()
        os.mkfifo(self.packet)
        with self.assertRaisesRegex(runner.ReviewError, "regular file"):
            runner.prepare_inputs(self.packet, self.manifest, self.evidence, self.repo)

    def test_synchronous_preparation_is_bounded(self):
        started = time.monotonic()
        with self.assertRaisesRegex(runner.ReviewError, "wall-clock timeout"):
            with runner.preparation_budget(started + 0.03):
                time.sleep(1)
        self.assertLess(time.monotonic() - started, .5)

    def test_faithful_copy_includes_uncommitted_untracked_ignored_and_links(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", self.primary.name], check=True)
        self.primary.write_text("Uncommitted edit\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        (self.repo / "ignored").write_text("ignored-but-relevant")
        (self.repo / "untracked").write_text("untracked")
        (self.repo / "link").symlink_to("target file.txt")
        copy = runner.faithful_copy(self.repo, self.root / "copy", self.root / "fidelity.json")
        self.assertEqual(runner.tree_manifest(copy), runner.tree_manifest(self.repo))
        self.assertTrue(json.loads((self.root / "fidelity.json").read_text())["verified"])
        (copy / "untracked").write_text("different")
        self.assertNotEqual(runner.tree_manifest(copy), runner.tree_manifest(self.repo))

    def test_copy_detects_source_change(self):
        original = shutil.copytree
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            self.primary.write_text("concurrent source change")
            return result
        with mock.patch.object(runner.shutil, "copytree", side_effect=change):
            with self.assertRaisesRegex(runner.ReviewError, "source changed"):
                runner.faithful_copy(self.repo, self.root / "copy", self.root / "fidelity.json")

    def test_copy_honors_runner_deadline(self):
        with self.assertRaisesRegex(runner.ReviewError, "timeout"):
            runner.faithful_copy(self.repo, self.root / "copy", self.root / "fidelity.json", time.monotonic() - 1)


class StreamTests(unittest.TestCase):
    def test_path_matching_uses_parameter_values_and_path_boundaries(self):
        name = '/repo/design "v2".txt'
        self.assertTrue(runner.references_path({"args": [{"AbsolutePath": name}]}, {name}))
        self.assertTrue(runner.references_path({"CommandLine": "cat '" + name + "'"}, {name}))
        self.assertFalse(runner.references_path({"AbsolutePath": name + ".backup"}, {name}))
        self.assertFalse(runner.references_path({"AbsolutePath": "/another/file.txt"}, {"file.txt"}))

    def test_incremental_tool_details_are_retained(self):
        stream = runner.ReviewStream("gemini-4-argon-high", "/repo", "auto")
        stream.accept({"event": "init", "init": {"model": "gemini-4-argon-high", "cwd": "/repo", "permission_mode": "request-review"}})
        stream.accept({"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool", "state": "ACTIVE"}})
        stream.accept({"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool", "state": "ACTIVE", "tool_name": "view_file", "tool_info": {"parameters": {"AbsolutePath": "/repo/file"}}}})
        stream.accept({"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool", "state": "DONE", "tool_info": {"output": "file content"}}})
        self.assertEqual(stream.steps[1]["tool_info"]["parameters"], {"AbsolutePath": "/repo/file"})
        self.assertEqual(stream.steps[1]["tool_info"]["output"], "file content")

    def test_authentication_error_envelope_is_diagnostic(self):
        with self.assertRaisesRegex(runner.ReviewError, "Authentication required"):
            runner.parse_event(b'{"status":"ERROR","error":"authentication required"}')

    def test_bad_tool_shape_cannot_provide_coverage(self):
        stream = runner.ReviewStream("gemini-4-argon-high", "/repo", "auto")
        stream.accept({"event": "init", "init": {"model": "gemini-4-argon-high", "cwd": "/repo", "permission_mode": "request-review"}})
        with self.assertRaisesRegex(runner.ReviewError, "Malformed tool_info"):
            stream.accept({"event": "step_update", "step_update": {"step_index": 1, "step_type": "tool", "state": "DONE", "tool_info": []}})

    def test_native_sandbox_unavailable_and_wrong_mode_stop(self):
        for extra in ({"sandbox_enabled": False}, {"agent_mode": ""}):
            stream = runner.ReviewStream("gemini-4-argon-high", "/repo", "auto")
            with self.assertRaises(runner.ReviewError):
                stream.accept({"event": "init", "init": {"model": "gemini-4-argon-high", "cwd": "/repo", "permission_mode": "request-review", **extra}})

    def test_truncated_json_and_missing_init(self):
        with self.assertRaisesRegex(runner.ReviewError, "Malformed"):
            runner.parse_event(b'{"event":"result"')
        stream = runner.ReviewStream("gemini-4-argon-high", "/repo", "auto")
        with self.assertRaisesRegex(runner.ReviewError, "no initial"):
            stream.accept({"event": "result", "result": {"status": "SUCCESS", "response": "text"}})


class SandboxFixture(Fixture):
    def setUp(self):
        super().setUp()
        required = os.environ.get("REQUIRE_SANDBOX_TESTS") == "1"
        binary = "sandbox-exec" if sys.platform == "darwin" else "bwrap"
        if not shutil.which(binary):
            if required:
                self.fail(f"Required sandbox executable missing: {binary}")
            self.skipTest(f"{binary} unavailable; actual sandbox not validated")
        self.run = self.evidence / "sandbox"
        self.run.mkdir()
        self.sandbox = runner.Sandbox(self.run, self.home)
        probe = subprocess.run(self.sandbox.command([sys.executable, "-c", "print('ready')"]), capture_output=True, text=True)
        if probe.returncode:
            if required:
                self.fail(probe.stderr)
            self.skipTest("Sandbox unavailable: " + probe.stderr)


class ActualSandboxTests(SandboxFixture):
    @unittest.skipUnless(sys.platform == "linux", "Linux runtime mount view")
    def test_agentapi_rewrites_are_disposable_and_other_binaries_stay_read_only(self):
        runtime_bin = self.state / "bin"
        runtime_bin.mkdir()
        helper = runtime_bin / "agentapi"
        helper.write_text("installed helper")
        binary = runtime_bin / "other-tool"
        binary.write_text("installed binary")
        run = self.evidence / "helper-view"
        run.mkdir()
        sandbox = runner.Sandbox(run, self.home)
        command = r'''
import pathlib, sys
helper, binary = map(pathlib.Path, sys.argv[1:])
helper.write_text("generated helper")
helper.chmod(0o755)
assert binary.read_text() == "installed binary"
for action in (lambda: binary.write_text("bad"), lambda: binary.unlink()):
    try: action()
    except OSError: pass
    else: raise SystemExit("installed binary mutation succeeded")
'''
        for _ in range(2):
            result = subprocess.run(sandbox.command([sys.executable, "-c", command, helper, binary]), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(helper.read_text(), "installed helper")
        self.assertEqual(binary.read_text(), "installed binary")
        self.assertEqual((sandbox.scratch / "agy-bin/agentapi").read_text(), "generated helper")

    def test_actual_read_terminal_scratch_and_original_protection(self):
        (self.sandbox.scratch / "linked root").symlink_to(self.repo, target_is_directory=True)
        commands = r'''
import os, pathlib, subprocess, sys
repo, scratch, settings = map(pathlib.Path, sys.argv[1:])
assert (repo / "target file.txt").read_text().startswith("Primary source")
assert subprocess.run(["/bin/ls", str(repo)], capture_output=True).returncode == 0
(scratch / "scratch write").write_text("allowed")
for base in (repo, scratch / "linked root"):
    for action in (lambda: (base / "new file").write_text("bad"),
                   lambda: (base / "target file.txt").write_text("bad"),
                   lambda: (base / "target file.txt").unlink()):
        try: action()
        except OSError: pass
        else: raise SystemExit("protected original mutation succeeded")
try: settings.write_text("bad")
except OSError: pass
else: raise SystemExit("settings were writable")
try:
    os.link(repo / "target file.txt", scratch / "hardlink")
    (scratch / "hardlink").write_text("bad")
except OSError: pass
else: raise SystemExit("hardlink bypass")
'''
        result = subprocess.run(self.sandbox.command([sys.executable, "-c", commands, self.repo, self.sandbox.scratch, self.settings]), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings.read_text(), "{}")
        self.assertTrue(self.primary.read_text().startswith("Primary source"))
        self.assertEqual((self.sandbox.scratch / "scratch write").read_text(), "allowed")

    def test_builtin_probe_and_runtime_symlink_rejection(self):
        runner.sandbox_probe(self.sandbox, self.repo, dict(os.environ), self.run, time.monotonic() + 10, self.materials)
        (self.state / "log").symlink_to(self.repo, target_is_directory=True)
        other = self.evidence / "other"
        other.mkdir()
        with self.assertRaisesRegex(runner.ReviewError, "symlink"):
            runner.Sandbox(other, self.home)

    def test_process_deadline_kills_child_and_retains_partial_output(self):
        start = time.monotonic()
        with self.assertRaisesRegex(runner.ReviewError, "timeout"):
            runner.capture(self.sandbox.command([sys.executable, "-u", "-c", "import time; print('partial'); time.sleep(30)"]), self.repo,
                           dict(os.environ), self.run, "timeout", start + .3)
        self.assertLess(time.monotonic() - start, 3)
        self.assertIn("partial", (self.run / "timeout.stdout").read_text())


class StubIntegrationTests(SandboxFixture):
    # Each end-to-end test uses the real outer sandbox, while agy is a protocol
    # fixture with no authentication.
    def invoke(self, case="success", *, effort="high", images=False, copy=False, evidence_root=None):
        fixture = self.root / "stub-config.json"
        fixture.write_text(json.dumps({"case": case, "settings": str(self.settings)}))
        executable = self.root / "agy stub"
        executable.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "stub_agy.py").read_text().split("\n", 1)[1])
        executable.chmod(0o755)
        if case in ("fallback_allowed", "fallback_ineffective"):
            self.settings.write_text('{"toolPermission":"proceed-in-sandbox"}')
        if images:
            picture = self.root / "input image.png"
            picture.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            self.materials.append({"id": "picture", "path": str(picture), "kind": "image"})
            self.manifest.write_text(json.dumps({"critical_materials": self.materials}))
        args = argparse.Namespace(repo=str(self.repo), packet=str(self.packet), manifest=str(self.manifest),
                                  evidence_root=evidence_root, model="gemini-4-argon", effort=effort,
                                  timeout_seconds=1.0 if case == "timeout" else 20, copy_for_checks=copy, agy=str(executable))
        run_root = Path(evidence_root) if evidence_root is not None else self.evidence
        run_root.mkdir(parents=True, exist_ok=True)
        before = set(run_root.iterdir())
        with mock.patch.dict(os.environ, {"AGY_REVIEW_TEST_FIXTURE": str(fixture), "TMPDIR": str(self.evidence)}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = runner.execute(args, runtime_home=self.home)
        run = next(iter(set(run_root.iterdir()) - before))
        self.assertTrue(run.name.startswith("gemini-second-opinion-"))
        return code, json.loads((run / "result.json").read_text()), run

    def test_full_success_images_and_copy(self):
        code, meta, run = self.invoke(images=True, copy=True, evidence_root=str(self.root / "custom evidence"))
        self.assertEqual(code, 0, meta)
        self.assertEqual(meta["selected_model"]["id"], "gemini-4-argon-high")
        self.assertEqual(meta["status"], "awaiting-primary-agent-coverage-assessment")
        gate = json.loads((run / "gate.json").read_text())
        self.assertTrue(gate["human_review_required"])
        self.assertEqual(set(gate["coverage_candidates"]), {"packet", "target", "picture"})
        self.assertTrue((run / "copy-fidelity.json").is_file())
        self.assertTrue((run / "review.events.jsonl").is_file())
        self.assertEqual(self.settings.read_text(), "{}")

    def test_quoted_critical_path_is_covered_end_to_end(self):
        quoted = self.repo / 'design "v2".txt'
        self.primary.rename(quoted)
        self.materials[0]["path"] = str(quoted)
        self.manifest.write_text(json.dumps({"critical_materials": self.materials}))
        code, meta, run = self.invoke()
        self.assertEqual(code, 0, meta)
        self.assertIn("target", json.loads((run / "gate.json").read_text())["coverage_candidates"])

    def test_successful_read_of_empty_artifact_is_coverage(self):
        self.primary.write_text("")
        code, meta, _ = self.invoke()
        self.assertEqual(code, 0, meta)

    def test_existing_proceed_in_sandbox_fallback(self):
        code, meta, run = self.invoke("fallback_allowed")
        self.assertEqual(code, 0, meta)
        argv = json.loads((run / "review.command.json").read_text())["argv"]
        self.assertNotIn("--mode", argv)
        self.assertEqual(meta["effective_init"]["permission_mode"], "proceed-in-sandbox")

    def test_failures_are_explicit_and_preserve_evidence(self):
        cases = {
            "unavailable": "unavailable in this account", "auth": "Authentication required",
            "mode_fallback": "Auto mode is unavailable", "settings": "protected settings.json",
            "review_settings": "protected settings.json", "generated_model": "non-generative",
            "wrong_effort": "does not match", "wrong_init_model": "pinned model",
            "fallback_ineffective": "not effective", "blanket": "permission mode",
            "malformed": "Malformed JSON", "interrupted": "without a successful terminal",
            "after_result": "after the terminal", "incomplete_marker": "COMPLETE marker",
            "incomplete_coverage": "no matching successful", "tool_error": "no matching successful",
            "timeout": "timeout", "late_fallback": "fell back",
        }
        for case, message in cases.items():
            with self.subTest(case=case):
                self.settings.write_text("{}")
                code, meta, run = self.invoke(case)
                self.assertNotEqual(code, 0, meta)
                self.assertIn(message, meta["error"])
                self.assertTrue((run / "result.json").is_file())
                self.assertEqual(meta["status"], "incomplete")

    def test_unsupported_effort_prevents_review(self):
        code, meta, run = self.invoke(effort="max")
        self.assertNotEqual(code, 0)
        self.assertIn("unsupported", meta["error"])
        self.assertFalse((run / "review.command.json").exists())

    def test_image_metadata_does_not_establish_coverage(self):
        code, meta, _ = self.invoke("image_terminal", images=True)
        self.assertNotEqual(code, 0)
        self.assertIn("picture", meta["error"])

    def test_errors_retained_for_materiality_assessment(self):
        code, meta, run = self.invoke("nonmaterial_error")
        self.assertEqual(code, 0, meta)
        gate = json.loads((run / "gate.json").read_text())
        self.assertEqual(len(gate["concerns"]), 2)
        self.assertTrue(gate["human_review_required"])


if __name__ == "__main__":
    unittest.main()

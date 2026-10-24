#!/usr/bin/env python3
"""Deterministic protocol fixture. Not a model and never used for real reviews."""
import json
import os
from pathlib import Path
import sys
import time

config = json.loads(Path(os.environ["AGY_REVIEW_TEST_FIXTURE"]).read_text())
args = sys.argv[1:]
case = config.get("case", "success")


def flag(name, default=None):
    for index, arg in enumerate(args):
        if arg == name:
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return default


def emit(event):
    print(json.dumps(event), flush=True)


def command(name, data):
    envelope = {"status": "SUCCESS", "num_turns": 0, "response": "This text is not authoritative",
                "command": {"name": name, "data": data}}
    if case == "generated_model" and name == "model":
        del envelope["command"]
        envelope["num_turns"] = 1
    emit(envelope)


if args == ["--version"]:
    print("stub-agy-1.4.6")
elif args == ["models"]:
    if case == "auth":
        print("authentication required", file=sys.stderr)
        sys.exit(1)
    families = ["gemini-3.8-flash"] if case == "unavailable" else ["gemini-4-argon", "gemini-3.8-flash"]
    for family in families:
        for effort in ("high", "medium"):
            print(f"{family}-{effort}\t{family} ({effort})")
    print("claude-opus-4-6\tOther provider")
elif flag("-p") == "/config":
    assert "--disable-slash-commands=false" in args and flag("--print-timeout") == "0"
    if case == "settings":
        try:
            Path(config["settings"]).write_text("unexpected mutation")
        except OSError:
            print("settings.json: operation not permitted", file=sys.stderr)
            sys.exit(1)
        raise RuntimeError("Sandbox failed to protect settings")
    if case in ("mode_fallback", "fallback_allowed", "fallback_ineffective"):
        print("warning: --mode auto is not available for this account; continuing in the default mode.", file=sys.stderr)
    command("config", {"config": {"agentMode": "", "enableTerminalSandbox": False}})
elif flag("-p") == "/model":
    assert "--disable-slash-commands=false" in args
    model = flag("--model")
    effort = flag("--effort")
    command("model", {"id": model + "-" + effort if not model.endswith("-" + effort) else model,
                      "label": "Gemini", "effort": "low" if case == "wrong_effort" else effort})
else:
    assert "--sandbox" in args and "--disable-slash-commands=true" in args
    assert "--dangerously-skip-permissions" not in args
    assert flag("--print-timeout") == "0" and flag("--output-format") == "stream-json"
    prompt = flag("-p")
    # The runner always adds its evidence directory. Locate its protected manifest.
    directories = [Path(args[i + 1]) for i, arg in enumerate(args) if arg == "--add-dir"]
    run = next(path for path in directories if (path / "materials.json").exists())
    material_list = json.loads((run / "materials.json").read_text())["critical_materials"]
    if case == "review_settings":
        print("settings.json: permission denied", file=sys.stderr)
        sys.exit(1)
    permission = "proceed-in-sandbox" if case == "fallback_allowed" else "request-review"
    if case == "blanket":
        permission = "always-proceed"
    selected = flag("--model")
    if case == "wrong_init_model":
        selected = "gemini-3.8-flash-high"
    emit({"event": "init", "init": {"cwd": str(Path.cwd()), "model": selected, "permission_mode": permission}})
    if case == "late_fallback":
        print("auto unavailable; continuing in the default mode", file=sys.stderr, flush=True)
        time.sleep(5)
    if case == "timeout":
        time.sleep(20)
    if case == "malformed":
        print("not-json", flush=True)
        sys.exit(0)
    coverage = {}
    for index, item in enumerate(material_list, 1):
        if case == "incomplete_coverage" and index == len(material_list):
            continue
        path = Path(item["path"])
        data = path.read_bytes()
        if item["kind"] == "image":
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
        tool = "run_command" if case == "image_terminal" and item["kind"] == "image" else "view_file"
        info = {"name": tool, "parameters": {"AbsolutePath": str(path)},
                "output": "image decoded" if item["kind"] == "image" else data.decode()}
        if case == "tool_error" and index == len(material_list):
            info["error"] = {"type": "permission", "message": "access denied"}
            info["output"] = ""
        emit({"event": "step_update", "step_update": {"step_index": index, "state": "DONE", "step_type": "tool", "tool_name": tool, "tool_info": info}})
        coverage[item["id"]] = [index]
    if case == "interrupted":
        sys.exit(0)
    if case == "nonmaterial_error":
        emit({"event": "step_update", "step_update": {"step_index": 99, "state": "DONE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "missing-optional-tool"}, "error": {"type": "tool", "message": "not found"}}}})
        print("Optional tool was soft-denied", file=sys.stderr)
    status = "INCOMPLETE" if case == "incomplete_marker" else "COMPLETE"
    response = f"REVIEW STATUS: {status}\n\n" + ("This is fixture text for the completed review. The concrete target and every critical material were inspected through the emitted tools. The implementation preserves filesystem protection and records the selected model. The plan is consistent with the user request. There are no required follow-up changes in this fixture. This synthetic result validates the runner protocol only and provides no evidence of an authenticated Gemini review. ")
    response += "\nInspected materials: " + ", ".join(coverage)
    emit({"event": "result", "result": {"status": "SUCCESS", "response": response}})
    if case == "after_result":
        emit({"event": "result", "result": {"status": "SUCCESS", "response": response}})

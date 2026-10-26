# Runner contract and verification

Requires Python 3.9+, an authenticated `agy`, and `sandbox-exec` on macOS or Bubblewrap (`bwrap`) on Linux. Install and authenticate separately; the runner never changes authentication, settings, permissions, plugins, or installed skills. An unavailable sandbox stops execution. Linux hosts must permit the namespaces Bubblewrap needs, including agy's nested terminal sandbox. The Ubuntu workflow uses a stub CLI; authenticated checks are separate.

## Why two sandboxes

agy headless mode auto-allows workspace file writes, and its native terminal sandbox permits workspace writes. The outer sandbox protects originals even from agy's file-edit tools and native sandbox escape requests. Removing it or replacing it with only `--sandbox` loses that protection. [Google headless documentation](https://antigravity.google/docs/cli/headless), [terminal sandbox documentation](https://antigravity.google/docs/sandbox).

The outer boundary is **filesystem protection**, not a network or remote-service sandbox. It allows reads across the filesystem and keeps networking available for model access and independent research. agy's own read permissions and domain policy still apply. Reviewer instructions prohibit external changes; existing MCP permissions are preserved without new grants. Do not describe this as preventing remote writes or hiding credentials. Keep sensitive information out of review inputs.

On macOS, the Seatbelt profile denies file writes by default and allows only scratch, `/dev/null`, and enumerated existing agy runtime data. On Linux, Bubblewrap binds `/` read-only, adds writable binds for those paths, creates private `/proc` and `/dev`, uses `--die-with-parent`, `--new-session`, user/PID namespaces, and drops capabilities. It shares networking. Writable runtime roots cannot be symlinks or aliases; hard-linked or executable runtime files are rejected. Evidence directories are not writable to the reviewer. The runner probes reads, scratch writes, original creation/edit/deletion denial and symlink denial before invoking agy.

Writable agy data under `~/.gemini/antigravity-cli/` is limited to existing directories `log`, `crashes`, `conversations`, `brain`, `annotations`, and `presence`; and existing files `history.jsonl`, `conversation_summaries.db` (plus its `-wal` and `-shm` files), `jetbox_summaries_proto.pb`, `cache/conversation_metadata.json`, and `cache/last_conversations.json`. Nothing grants write access to their parents. `settings.json`, configuration, credentials, binaries, plugin assets, updater data and other caches remain protected. Unknown write needs stop the run rather than enlarging the allowlist. Atomic replacement or creation of missing runtime files can fail, especially with Linux file binds; inspect evidence and maintain the allowlist only with verified runtime requirements, never by making configuration directories writable.

On Linux, an existing runtime `bin` directory has a disposable view backed by review scratch. Antigravity regenerates `bin/agentapi` before each terminal command, so that helper is created only in this view. Other existing entries are mounted read-only from the original installation. The installed helper and binaries remain unchanged. Repository inputs, critical materials and the agy executable cannot overlap this view; `result.json` records its target path. This does not grant writes to the original runtime directory or change the macOS sandbox.

## Startup and model evidence

Before starting agy, the runner reads global and workspace configuration, ancestor `.agents` directories, and installed plugin directories. It rejects enabled executable hooks, enabled sidecars (including plugin sidecar ids), scripted status lines/titles, and blanket `always-proceed`. Plugin hooks are inspected conservatively even when a registry might mark the plugin disabled: registry formats vary, so the diagnostic names the file that must be made inert. MCP server names and configuration paths are recorded; credentials and server environment values are not copied into configuration evidence. Normal MCP permissions remain in force. [Hooks](https://antigravity.google/docs/hooks/), [plugins](https://antigravity.google/docs/plugins/), [sidecars](https://antigravity.google/docs/sidecars/).

All executable CLI probes run inside the same outer sandbox as the review:

1. Capture `agy --version`, when available.
2. Test startup with `--sandbox --mode auto --disable-slash-commands=false --print-timeout 0 --output-format json -p /config`. Require non-generative structured command metadata. Some versions return saved configuration rather than effective flag overrides; do not infer effective mode from its text or its saved sandbox value.
3. Check `agy models` for the requested Gemini family and effort. Missing family means unavailable account catalog access; missing effort means unsupported effort. Never substitute a model.
4. Resolve with `/model`, slash commands explicitly enabled, and require `command.name=model`, `num_turns=0`, selected Gemini id and matching effort. Response text cannot resolve the model.
5. Run the review from the original repository root with the selected full id, `--sandbox`, auto mode when available, slash expansion disabled, streaming JSON, and explicit zero print timeout. Add the evidence directory and external material directories with `--add-dir`. The init event must match the model and root and expose an acceptable effective permission mode. Stop immediately on reported model/mode mismatch, fallback warning or unavailable native sandbox.

An auto-mode fallback warning is a failure unless saved `toolPermission=proceed-in-sandbox` already exists with default or auto `agentMode`. In that case the review omits `--mode auto`, keeps `--sandbox`, and must confirm `proceed-in-sandbox` in the init event. The runner never writes this configuration for the user. If protected settings prevent startup, the error identifies that failure and retains protection. Authentication errors instruct the user to authenticate interactively outside the runner.

Packets, manifests and configuration must be regular files; named pipes and other special inputs are rejected before reading. The runner bounds synchronous preparation as well as child-command execution with its wall-clock budget.

The account used during initial development lacked `gemini-4-argon` and rejected auto mode. These are runtime observations, not permanent assumptions about all accounts. Defaults must fail clearly on such an account. Stub integration tests cannot demonstrate authenticated Argon capability.

## Evidence and limits of automation

Every run prints its unique evidence directory. Commands, stdout/stream, stderr, CLI version, requested and selected model, saved permission settings, writable paths and effective stream init are retained. The parent runner writes evidence outside reviewer scratch; the reviewer controls only its stdout/stderr content. No environment-variable dump is recorded.

`materials.json` records hashes and images copied byte-for-byte. A disposable copy, when requested, includes all normal files including ignored and untracked material and `.git`; special files fail copy preparation. `copy-fidelity.json` records the verified inventory. Original targets are hashed again after the review. Symlinks are preserved, not followed into new writable grants. Hash/type/mode equality establishes the copy baseline, not semantic equivalence of a test environment or dependencies.

The stream must have exactly one init and one successful terminal result, with no events after the result. Malformed, truncated, interrupted and unsuccessful streams fail. The reviewer must provide substantive text, the completion marker and an account of materials inspected. The runner derives coverage candidates directly from successful CLI content-inspection tool events that identify each critical path; it never relies on model-generated tool indices. Images require a viewing tool. Obvious metadata-only shell commands do not qualify. Arbitrary shell commands and partial reads still need human inspection; these matches are coverage **candidates**, not proof that every line was understood. Errors/denials are collected in `gate.json` for materiality assessment.

Exit 0 means mechanical checks passed and the invoking agent must perform the skill's substantive assessment. Exit 2 means failed or interrupted; read `result.json` and the stage logs. Failed runs retain partial evidence and scratch needed for repair. After assessment, remove only this run's obsolete scratch and preparation copies; retain investigation and handoff evidence.

## Development checks

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s gemini-second-opinion/tests -v
uv run --with pyyaml python /Users/ewagner/.codex/skills/.system/skill-creator/scripts/quick_validate.py gemini-second-opinion
```

The validator path is the installed skill-creator location in this workspace; resolve the local installation on other machines. Tests use only the standard library and isolated stub fixtures. The Ubuntu CI job installs Bubblewrap and runs the same suite with sandbox tests required (not silently skipped). Run the real quick validator separately from its installed skill-creator location. Do not claim Linux or authenticated live validation until the corresponding tests have actually passed.

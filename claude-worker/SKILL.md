---
name: claude-worker
description: "Launch and own Claude Code workers with Codex-like lifecycle control, stable resumable sessions, explicit authority parity, subscription-aware billing gates, machine-aware capacity, and structured terminal results. Use only when the human explicitly requests Claude workers/this skill or explicitly requests maximal or machine-optimal parallelism. Native Codex slots take priority unless the human specifically requests Claude."
---

# Claude worker

Use this skill to add top-level Claude Code workstreams while the Codex parent retains operational ownership. Parity means comparable launch, steering, pause, resume, stop, monitoring, authority, and terminal-result controls. It does not mean that Claude and Codex will reason or behave identically.

## Activation rules

Invoke this skill only when the human:

- explicitly asks for Claude workers or names this skill; or
- explicitly asks for maximal, optimal, or machine-optimal parallelism.

For maximal-parallelism requests, fill every useful native Codex slot first. Use Claude only after native slots are exhausted. A human who explicitly requests Claude, a Claude model, or a Claude effort overrides native-first for the requested lanes.

Never use this skill for ordinary delegation merely because another lane would be convenient.

## Non-negotiable controls

- Use managed, noninteractive `claude -p` streaming execution. Native Codex workers are also managed asynchronously rather than through an interactive terminal UI.
- Use the currently logged-in Claude Code CLI's first-party `claude.ai` subscription auth. Never inject credentials, log in/out, change providers, invoke gateways, or use Bedrock, Vertex, Foundry, or API keys.
- Never use `bypassPermissions` or `--dangerously-skip-permissions`.
- Mirror the parent Codex lane's effective approval policy, filesystem sandbox, network access, and declared tools. Do not trust Claude less or more than the native worker.
- Never let Claude launch Claude, Codex, Agent/Task/Team/Skill tools, slash commands, or another agent harness. Nesting would evade the global native-first and capacity scheduler.
- Start with empty setting sources, strict empty MCP configuration, Chrome disabled, and slash commands disabled. Add no connector or MCP implicitly.
- Keep every worker attached to the active Codex turn until its terminal event is consumed, result is inspected, and the next action is recorded. The human is never the completion poller.

## Preflight

Resolve the helper relative to this `SKILL.md` and run:

```sh
python3 <skill-directory>/scripts/claude_worker.py doctor --cwd <task-repository>
```

`doctor` verifies the installed streaming features, current first-party subscription login, prohibited provider overrides, machine state, state migration, and skill-owned process recovery.

Resolve `claude` from the current user's `PATH`. For a nonstandard installation, set `CLAUDE_WORKER_CLAUDE` to that user's Claude Code executable; never embed a user- or machine-specific fallback path.

If `notices` reports an Opus version newer than Opus 5, tell the human promptly. Continue using the requested/default Opus 5 unless the human asks to switch.

## Allocate capacity

1. Decompose the request into independent ownership lanes.
2. Inspect the current native Codex agent tree immediately before launch.
3. Fill useful native slots first unless Claude was explicitly requested.
4. Run `capacity` with the fresh active-native count and workload.
5. Add Claude workers in waves of no more than two; recheck after every wave.

```sh
python3 <skill-directory>/scripts/claude_worker.py capacity \
  --cwd <task-repository> \
  --native-active <fresh-count> \
  --workload standard
```

Capacity is a ceiling, not a target. Consider CPU, memory, load, disk, build-cache contention, mutable-path overlap, and subscription headroom. On a 16-core/128 GB Mac Studio, a huge number of workers will contend rather than accelerate; choose the smallest set of genuinely independent lanes.

Warm-paused Claude processes count as 0.25 worker. Cold-paused processes count as zero.

## Select model and effort

Only this exact model-specific mapping is defined:

| Codex model and effort | Claude model and effort |
|---|---|
| `gpt-5.6-sol high` | `claude-opus-5 medium` |
| `gpt-5.6-sol xhigh` | `claude-opus-5 high` |
| `gpt-5.6-sol max` | `claude-opus-5 xhigh` |

There is no generic “Codex → Opus” mapping and no mapping for another Codex model.

Human overrides take precedence:

- Opus/Opus 5 model only: use Opus 5 with the applicable `gpt-5.6-sol` mapping.
- Non-Opus model only: ask the human for Claude effort; do not guess.
- Effort only: use Opus 5 with that effort.
- Explicit model and effort: preserve the exact pair after any billing confirmation.

Example: “spawn a Claude worker on Fable 5 max” becomes `--model claude-fable-5 --effort max`.

### Non-baseline billing confirmation

Opus 5 is the subscription-tested baseline. A non-Opus or otherwise unknown model stops before inference with `needs_billing_confirmation`. Offer the human exactly:

- switch to a subscription-eligible model;
- cancel; or
- authorize usage credits for this worker only.

Only after an explicit per-worker human authorization, pass both:

```sh
--allow-usage-credits \
--usage-credit-authorization '<exact human authorization source>'
```

The helper records worker, model, effort, time, and authorization source. It never changes the account-wide extra-usage setting.

The selected account policy is “run until limit”: subscription-eligible work may run while extra usage is enabled. The supervisor immediately interrupts on a visible credit-transition, continuation, extra-usage, or limit signal and never accepts continuation automatically. This is best-effort because the CLI may not expose a transition before the first over-limit request; absolute zero-credit use cannot be guaranteed while account extra usage remains enabled.

## Mirror parent authority

Obtain the effective authority from the current Codex runtime. If any value is unavailable, do not guess: spawn returns `needs_authority_profile`.

Pass:

- `--codex-approval-policy never|on-request|untrusted|on-failure`
- `--codex-sandbox danger-full-access|workspace-write|read-only`
- `--network enabled|disabled`

Mappings:

| Parent profile | Claude behavior |
|---|---|
| Unrestricted + `never` | `dontAsk`; bare Read/Glob/Grep/Edit/Write/NotebookEdit/Bash and network tools when enabled; Claude filesystem sandbox disabled. |
| Workspace-write + `never` | `dontAsk`; same declared tools; Claude sandbox limits writes to workspace/additional directories; network mirrored. |
| Read-only + `never` | `dontAsk`; no mutation tools; Read/Glob/Grep/Bash; OS sandbox denies repository writes and unsandboxed commands; network mirrored. |
| Parent-mediated approvals | `manual`; already-authorized capabilities are represented by the profile and remaining `can_use_tool` requests surface through `approve`/`deny`. |

Task-authorized connectors must be preflighted explicitly. If Claude cannot use a connector the Codex parent has, pass a parent-produced data packet or report the integration unavailable before launch.

## Workspace and task packet

Shared working directory is the default, matching native Codex workers. Native workers do not receive a worktree by default.

- `read-only`: shared by default.
- `mutable-disjoint`: shared only with one or more disjoint `--owned-path` values.
- `mutable-overlap`: worktree by default; shared mode is refused.
- `--isolation worktree`: explicit isolation.

If overlapping mutation needs a worktree and the directory is not a Git repository, launch fails rather than risking overlap.

The task must state objective, owned paths, dependencies, forbidden actions, required tests, and completion criteria. Do not assign overlapping mutable ownership.

## Spawn

Pass the task on stdin:

```sh
python3 <skill-directory>/scripts/claude_worker.py spawn \
  --cwd <task-repository> \
  --activation maximal \
  --native-active <fresh-count> \
  --native-free-slots 0 \
  --codex-model gpt-5.6-sol \
  --codex-effort xhigh \
  --codex-approval-policy never \
  --codex-sandbox danger-full-access \
  --network enabled \
  --workload standard \
  --scope mutable-disjoint \
  --owned-path <relative-path> \
  --name <lane-name> \
  < <task-file>
```

Use `--activation explicit-claude` when the human directly requested Claude. Record real native counts even then.

Spawn does not return ready until the supervisor has created its private socket, validated Claude's session ID, and completed a five-second no-work `get_context_usage` protocol probe. It returns the worker ID, stable session ID, authority receipt, billing decision, capacity receipt, and protocol receipt.

## Control and monitoring

Operate only on the returned skill-owned `cw-...` worker ID:

```sh
python3 <skill-directory>/scripts/claude_worker.py status <worker-id>
python3 <skill-directory>/scripts/claude_worker.py logs <worker-id>
python3 <skill-directory>/scripts/claude_worker.py attach <worker-id>
python3 <skill-directory>/scripts/claude_worker.py wait <worker-id>
python3 <skill-directory>/scripts/claude_worker.py send <worker-id> --message '<steering>'
python3 <skill-directory>/scripts/claude_worker.py followup <worker-id> --native-active <count> --message '<new turn>'
python3 <skill-directory>/scripts/claude_worker.py pause <worker-id>
python3 <skill-directory>/scripts/claude_worker.py resume <worker-id> --native-active <count>
python3 <skill-directory>/scripts/claude_worker.py stop <worker-id>
python3 <skill-directory>/scripts/claude_worker.py approve <worker-id> <permission-id> --reason '<basis>'
python3 <skill-directory>/scripts/claude_worker.py deny <worker-id> <permission-id> --reason '<basis>'
```

- `send` steers an active worker; on an idle/completed worker it queues without activating.
- `followup` sends while active or reactivates an idle/completed worker, like native `followup_task`.
- `pause` interrupts safely. Warm pause keeps the CLI stream for five minutes; expiry becomes cold pause.
- `resume` needs no queued input. It uses queued messages or a recorded continuation prompt.
- Cold resume always uses `--resume=<original-session-id>`. It never creates a replacement lane.
- `stop` is permanent cancellation, not pause.
- `approve`/`deny` are valid only for parent-mediated profiles.

Use `logs` and `attach` redacted by default. `--raw` is an explicit, narrow opt-in because events can contain source, prompts, tool payloads, and diagnostics.

The workload watchdog interrupts and cold-pauses after 10 minutes without events for light work, 20 for standard, or 30 for heavy. It reports `blocked: stalled` and preserves the session. Resume retries machine capacity every 30 seconds for five minutes by default, then reports `blocked: capacity` while retaining queued messages.

If status reports `subscription_limit`, stop adding Claude lanes and reassign essential unfinished work to native capacity or wait for the subscription window. Never accept a credit continuation.

## Finish, inspect, and dispose

Obtain the schema-validated result:

```sh
python3 <skill-directory>/scripts/claude_worker.py result <worker-id>
```

Inspect the result, diff, tests and exit codes, blockers, branch/worktree/HEAD, proposed subtasks, and lingering processes yourself. A process exit is not acceptance.

Record the result disposition:

```sh
python3 <skill-directory>/scripts/claude_worker.py dispose <worker-id> \
  --disposition integrated|rejected|cancelled|retained \
  --note '<inspection receipt>'
```

Reconcile all skill-owned state after interruption and before yielding:

```sh
python3 <skill-directory>/scripts/claude_worker.py reconcile
python3 <skill-directory>/scripts/claude_worker.py list
```

Cleanup is permitted only for terminal workers with a recorded non-pending disposition. It releases and confirms the owned supervisor before removing state and never touches unrelated Claude sessions:

```sh
python3 <skill-directory>/scripts/claude_worker.py cleanup --older-than-days 7
```

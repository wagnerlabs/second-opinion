---
name: claude-worker
description: "Launch and own Claude Code workers with Codex-like lifecycle control, stable resumable sessions, explicit authority parity, subscription-aware billing gates, machine-aware capacity, GPT reviewer substitution, and structured terminal results. Use only when the human explicitly requests Claude workers/this skill or explicitly requests maximal or machine-optimal parallelism. Native Codex slots take priority unless the human specifically requests Claude."
---

# Claude worker

Use this skill to add top-level Claude Code workstreams while the Codex parent retains operational ownership. Parity means comparable launch, steering, pause, resume, stop, monitoring, authority, and terminal-result controls. It does not mean that Claude and Codex will reason or behave identically.

## Activation rules

Invoke this skill only when the human:

- explicitly asks for Claude workers or names this skill; or
- explicitly asks for maximal, optimal, or machine-optimal parallelism.

For maximal-parallelism requests, fill every useful native Codex slot first. Use Claude only after native slots are exhausted. A human who explicitly requests Claude, a Claude model, or a Claude effort overrides native-first for the requested lanes.

Never use this skill for ordinary delegation merely because another lane would be convenient.

## Keep the primary Codex agent orchestration-only in overflow mode

Overflow mode begins when useful native Codex worker slots are full and one or more Claude workers are added. In overflow mode, the primary Codex agent is orchestration-only by default. Delegate all sustained implementation, investigation, and review lanes to native Codex or Claude workers; do not make the primary agent another parallel implementation lane while it is coordinating the saturated worker set.

The primary agent still owns decomposition, task packets, dependency ordering, capacity decisions, steering, permission decisions, progress reporting, terminal-event consumption, result inspection, convergence, and final synthesis. It may run short control or verification commands needed for those duties, but should delegate material edits, lengthy analysis, and test execution whenever they form an independent lane.

Depart from this default only when the human explicitly asks the primary agent to work a lane too, or when a small non-delegable convergence action is necessary. Record that exception and return to orchestration-only work promptly.

## Non-negotiable controls

- Use managed, noninteractive `claude -p` streaming execution. Native Codex workers are also managed asynchronously rather than through an interactive terminal UI.
- Use the currently logged-in Claude Code CLI's first-party `claude.ai` subscription auth. Never inject credentials, log in/out, change providers, invoke gateways, or use Bedrock, Vertex, Foundry, or API keys.
- Never use `bypassPermissions` or `--dangerously-skip-permissions`.
- Mirror the parent Codex lane's effective approval policy, filesystem sandbox, network access, and declared tools. Do not trust Claude less or more than the native worker.
- Keep all installed Claude Code skills and slash commands available. `/claude-second-opinion` is the only skill-name exception because it is redirected to `/gpt-second-opinion`. Skills are capabilities, not workers; enforce the no-nesting rule at `Agent`/Task/Team/agent-messaging tools and agent-harness execution instead of denying `Skill` globally.
- Never let Claude launch Claude, raw Codex implementation/delegation, Agent/Task/Team workers, or another agent harness. The constrained GPT reviewer command emitted by `/gpt-second-opinion` is the sole nested-harness exception.
- Load normal user, project, and local skill/settings sources. Keep strict empty MCP configuration and Chrome disabled; add no connector or MCP implicitly.
- Keep every worker attached to the active Codex turn until its terminal event is consumed, result is inspected, and the next action is recorded. The human is never the completion poller.

## Preflight

Resolve the helper relative to this `SKILL.md` and run:

```sh
python3 <skill-directory>/scripts/claude_worker.py doctor --cwd <task-repository> --reconcile
```

`doctor --reconcile` verifies the installed streaming features, current first-party subscription login, prohibited provider overrides, machine state, state migration, skill-owned process recovery, and discovery of `/gpt-second-opinion`. Reconcile again before a later capacity calculation if a managed supervisor exited unexpectedly after preflight. A missing reviewer skill does not block ordinary workers, but it blocks any task that requires that review.

Resolve `claude` from the current user's `PATH`. For a nonstandard installation, set `CLAUDE_WORKER_CLAUDE` to that user's Claude Code executable; never embed a user- or machine-specific fallback path.

If `notices` reports an Opus version newer than Opus 5, tell the human promptly. Continue using the requested/default Opus 5 unless the human asks to switch.

## Allocate capacity

1. Decompose the request into independent ownership lanes.
2. Inspect the current native Codex agent tree immediately before launch.
3. Fill useful native slots first unless Claude was explicitly requested.
4. Run `capacity` with the fresh active-native count and workload.
5. Before adding Claude beyond a full native pool, move any material primary-agent lane to a worker and make the primary agent orchestration-only.
6. Add Claude workers in waves of no more than two; recheck after every wave.

```sh
python3 <skill-directory>/scripts/claude_worker.py capacity \
  --cwd <task-repository> \
  --native-active <fresh-count> \
  --workload standard
```

Use `safe_additional_this_wave` as the machinery's recommendation for how many Claude workers may be added now, never as a requirement to fill every available place. It incorporates CPU, memory, load, disk, workload weight, active native lanes, active or warm skill-owned Claude lanes, an absolute machine guard, and a maximum launch wave of two. Recalculate after every wave and choose the smaller of the recommendation and the number of genuinely independent useful lanes.

`--max-total-worker-lanes N` is an optional human/operator hard ceiling, not the recommendation algorithm. Pass the fresh caller-observed native lane count through `--native-active`; the helper adds active or warm skill-owned Claude worker lanes and excludes the primary Codex orchestrator, cold-paused Claude lanes, and unrelated external processes. The ceiling can lower the machine-derived allowance but can never raise it. It is per invocation, so pass it on every `capacity`, `spawn`, `followup`, or `resume` command where the human wants it enforced. `--max-workers` remains only as a deprecated compatibility alias; receipts identify use of that alias and direct the caller to the current name.

Read `human_ceiling` in the capacity receipt for `supplied_max_total_worker_lanes`, `current_count`, `remaining_capacity`, `safe_without_human_ceiling`, `binding`, and `reduced_capacity`. `binding` means remaining capacity under the ceiling is at or below the otherwise-safe recommendation; `reduced_capacity` means it is strictly lower. The `human_ceiling` gate appears when the ceiling is binding, including equality. The top-level `current_count` is the combined native-plus-Claude lane count used in this arithmetic. On a refused `spawn`, read the structured stderr JSON's `capacity` object instead of parsing the human-readable `error` string.

Capacity is a ceiling, not a target. Consider build-cache contention, mutable-path overlap, and subscription headroom in addition to the calculated machine signals. On a 16-core/128 GB Mac Studio, a huge number of workers will contend rather than accelerate.

Warm-paused Claude processes consume 0.25 workload weight for CPU budgeting but still occupy one lane under the absolute and optional total-lane ceilings. Cold-paused processes count as zero.

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
| Unrestricted + `never` | `dontAsk`; bare Read/Glob/Grep/Edit/Write/NotebookEdit/Bash/Skill and network tools when enabled; Claude filesystem sandbox disabled. |
| Workspace-write + `never` | `dontAsk`; same declared tools; Claude sandbox limits writes to workspace/additional directories; network mirrored. |
| Read-only + `never` | `dontAsk`; no mutation tools; Read/Glob/Grep/Bash/Skill; OS sandbox denies repository writes and unsandboxed commands; network mirrored. |
| Parent-mediated approvals | `manual`; already-authorized capabilities are represented by the profile and remaining `can_use_tool` requests surface through `approve`/`deny`. |

Task-authorized connectors must be preflighted explicitly. If Claude cannot use a connector the Codex parent has, pass a parent-produced data packet or report the integration unavailable before launch.

Treat `--scope` as a behavioral and ownership contract, not a second authority profile. Never tighten the inherited sandbox, tool set, keyring access, proxy trust, or network environment merely because a task is labeled `read-only`; native Codex workers do not receive that additional downgrade. The parent Codex profile is the sole source of runtime authority. A read-only task must still obey its no-mutation contract through its task packet and result audit.

Preserve the parent process environment needed by authorized shell tools, including credential-store and TLS configuration. Preflight task-required CLIs inside the worker. If a CLI that succeeds in the parent fails inside Claude because credentials, Keychain access, certificates, proxies, or network trust differ, report an authority-parity blocker rather than treating the tool as unavailable in general.

## Workspace and task packet

Shared working directory is the default, matching native Codex workers. Native workers do not receive a worktree by default.

- `read-only`: shared by default.
- `mutable-disjoint`: shared only with one or more disjoint `--owned-path` values.
- `mutable-overlap`: worktree by default; shared mode is refused.
- `--isolation worktree`: explicit isolation.

If overlapping mutation needs a worktree and the directory is not a Git repository, launch fails rather than risking overlap.

The task must state objective, owned paths, dependencies, forbidden actions, required tests, and completion criteria. Do not assign overlapping mutable ownership.

If a second opinion is required, also include the complete verbatim user transcript available to the parent, the review scenario, the concrete target, every critical/load-bearing material path, and the artifact under review. Do not make the Claude worker reconstruct context that only the parent possesses.

`read-only` describes what the worker may do, while `--codex-sandbox` describes what the parent runtime can technically do. Keep those concepts separate.

## Substitute GPT second opinions

For every Claude worker, replace every instruction to use `claude-second-opinion` with `gpt-second-opinion`. Apply this substitution unconditionally; do not compare worker/reviewer models and do not retain a Claude reviewer because it uses a different Claude model. A direct instruction to use `gpt-second-opinion` follows the same path.

Invoke the installed `/gpt-second-opinion` skill normally. Do not reimplement or wrap it. The policy hook permits the skill's constrained GPT-5.6-Sol reviewer command and continues to reject general `codex exec`, Claude, Gemini, Aider, Goose, Hermes, agent-team, and subagent launches.

Keep every other installed skill available subject to the parent authority profile; `/gpt-second-opinion` is not an allowlist for skills. A skill does not bypass nesting controls: if it attempts to create a sub-worker or invoke another agent harness, block that action while leaving non-nested skill behavior available.

Run `/gpt-second-opinion` synchronously and do not continue the reviewed work while it runs. Follow its own completion gate, repair operational failures when safe, act on substantive feedback using the Claude worker's judgment, and include the review outcome and output path in the worker result. Classify a lane with a non-trivial required review as `heavy` capacity unless the bounded review is demonstrably lighter. If the skill is unavailable, return a blocker rather than improvising a replacement.

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

Spawn does not return ready until the supervisor has created its private socket, validated Claude's session ID, and completed a five-second no-work `get_context_usage` protocol probe. It returns the worker ID, stable session ID, authority receipt, billing decision, capacity receipt, protocol receipt, and orchestration policy. When that receipt reports `overflow_mode: true`, treat `primary_codex_role_default: orchestrator_only` as an operating commitment, not merely metadata.

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
- Resume capacity excludes the worker being reactivated, so a waiting worker does not consume its own prospective lane.
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

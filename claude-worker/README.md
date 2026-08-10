# Claude worker

`claude-worker` exists to let Codex use Claude Code to spawn and orchestrate additional workers beyond Codex's current native concurrency cap of four agents: the primary Codex agent plus up to three native Codex workers.

After useful native Codex worker slots are full, the skill lets the primary Codex agent launch, steer, pause, resume, monitor, and collect results from additional top-level Claude workers using the locally logged-in Claude Code subscription. Machine-aware capacity limits still apply, so the skill expands the available worker pool without treating unlimited parallelism as efficient.

The primary Codex agent remains the orchestrator by default when Claude workers extend a saturated native pool. Native Codex workers retain priority unless the human explicitly requests Claude.

## Capacity

The skill's `capacity` command recommends how many Claude workers may be added in the next wave from three sampled CPU/disk intervals, memory, free disk, workload, and role-aware orchestration state. Its recommendation is capped at two workers per wave and should be recalculated between waves; there is no fixed recommended Claude-worker cap.

The caller supplies a fresh schema-versioned JSON document through `--orchestration-state`. It identifies the primary controller, occupied native workers, direct reviewers, workloads, statuses, and the native child limit. The helper validates freshness and consistency, discovers its own managed Claude workers, and reports controller/native/Claude/reviewer accounting separately. This replaces the error-prone `--native-active` and `--native-free-slots` flags.

The optional `--max-total-worker-lanes N` flag is a separate human/operator hard ceiling over native plus managed-Claude top-level worker lanes. It excludes the controller and direct reviewers; reviewer machine pressure is still included in resource accounting. The removed `--max-workers` alias is intentionally rejected.

Capacity output reports role classification, resource weights, sampled telemetry, the supplied ceiling, combined current count, remaining capacity, and whether the ceiling is binding or actually reduced the recommendation. A refused `spawn` returns the same capacity receipt as structured JSON. Standard and heavy local tests/builds remain parent-scheduled one at a time across every role until measured evidence justifies automatic queuing.

See [`SKILL.md`](SKILL.md) for the canonical agent-facing workflow and controls.

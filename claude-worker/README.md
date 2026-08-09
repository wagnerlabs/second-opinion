# Claude worker

`claude-worker` exists to let Codex use Claude Code to spawn and orchestrate additional workers beyond Codex's current native concurrency cap of four agents: the primary Codex agent plus up to three native Codex workers.

After useful native Codex worker slots are full, the skill lets the primary Codex agent launch, steer, pause, resume, monitor, and collect results from additional top-level Claude workers using the locally logged-in Claude Code subscription. Machine-aware capacity limits still apply, so the skill expands the available worker pool without treating unlimited parallelism as efficient.

The primary Codex agent remains the orchestrator by default when Claude workers extend a saturated native pool. Native Codex workers retain priority unless the human explicitly requests Claude.

## Capacity

The skill's `capacity` command recommends how many Claude workers may be added in the next wave from current CPU, memory, load, disk, workload, and existing-lane state. Its recommendation is capped at two workers per wave and should be recalculated between waves; there is no fixed recommended Claude-worker cap.

The optional `--max-total-worker-lanes N` flag is a separate human/operator hard ceiling. The caller supplies the current active native Codex lane count with `--native-active`; the skill adds active or warm skill-owned Claude worker lanes and excludes the primary Codex orchestrator. The ceiling can lower the machinery's recommendation but cannot raise it, and it must be passed on every `capacity`, `spawn`, `followup`, or `resume` invocation where the operator wants it enforced. The older `--max-workers` spelling is retained only as a deprecated compatibility alias.

Capacity output reports the supplied ceiling, combined current count, remaining capacity, and whether the ceiling is binding or actually reduced the recommendation. A refused `spawn` returns the same capacity receipt as structured JSON.

See [`SKILL.md`](SKILL.md) for the canonical agent-facing workflow and controls.

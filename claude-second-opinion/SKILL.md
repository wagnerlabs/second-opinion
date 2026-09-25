---
name: claude-second-opinion
description: "Sends a blocking, time-expensive review packet to Claude Fable 5.1 via CLI at max effort and standard speed by default, with user-requested fast mode and explicit Fable 5 and Opus 5.5 options, inspecting the repo read-only. Use for non-trivial RCA, plans, implementations, documents, or analyses when the user asks or an agent judges a second opinion worthwhile, generally once per artifact. For newly requested work, invoke only after the primary agent has completed its best attempt; never use the reviewer to develop or finish it. Primarily for non-Anthropic agents; Claude/Anthropic agents should prefer gpt-second-opinion for perspective diversity unless the user requests Claude or task constraints favor it. Once invoked, block until the review has accessed all critical material and been considered; repair and rerun execution, access, or coverage failures, and stop after unremediable failure unless the user waives. Feedback is advisory; disagreement never requires approval or rerun."
---

# Claude second opinion

## Invocation policy

- Run this skill when the user explicitly asks.
- You may also use it, at your discretion, for non-trivial artifacts where an independent review would materially improve quality.
- Prefer a reviewer from a different model provider to increase diversity of perspective. This skill is generally intended for invoking agents not based on Claude or other Anthropic models.
- If you are a Claude/Anthropic-based agent and the user has not specified a reviewer, prefer `gpt-second-opinion`. Use this skill instead only when the user explicitly requests Claude or a concrete task constraint makes Claude the better reviewer.
- This skill is time-expensive and optional by default. It is highly recommended after a non-trivial implementation plan is ready for review, before implementing.
- Once this skill is invoked for a task/artifact, it is **blocking for that task**: do not proceed with the fix, implementation, plan revision, document finalization, user-facing answer, or other reviewed work until the packet has been built and validated, Claude has returned its output, you have verified that Claude actually performed the intended review with access to every critical/load-bearing related material, you have read the full output, and you have independently decided which feedback—if any—to adopt.
- Do **not** run this skill in parallel while continuing the same workstream. It is acceptable to use a background process only to avoid tool timeouts, but you must wait for completion and consume the review output before advancing the task under review.
- If the intended second-opinion review cannot be completed after safe, in-scope remediation is exhausted, stop the reviewed work and report the failure to the user. Do not proceed unless a valid review later completes or the user explicitly tells you to continue without it.
- Treat a zero exit code, a `REVIEW STATUS: COMPLETE` marker, or useful partial feedback as insufficient by itself. If Claude says it could not access or inspect any critical/load-bearing related material, the review failed operationally and must be repaired and rerun.
- Treat the review as a second opinion, not an approval authority. A critical verdict, requested changes, unresolved recommendations, or your decision to reject some or all feedback does not keep the gate open.
- Default frequency: at most one completed review per non-trivial task/artifact. Run again only to replace a pass that failed the execution/access/coverage gate, when the user explicitly requests another review, or for a materially different downstream artifact such as a completed implementation after a plan review. Never rerun a revised plan, implementation, document, or analysis merely to seek Claude's agreement, approval, or confirmation that feedback was addressed.

## Reviewer default and per-review fallback

- **Every new review defaults to Fable 5.1 at `max` effort and standard speed**, unless the user explicitly selects another supported model, effort, or fast mode for that review.
- If Fable cannot be used for a particular review, use **Opus 5.5 as the fallback for that review only**. Base this decision on a concrete limitation affecting that review, such as model unavailability or an unsupported required capability; do not switch merely because Fable's feedback is unfavorable.
- Select the fallback through the existing per-invocation arguments: `/claude-second-opinion opus` selects `claude-opus-5-5` at `max` effort and standard speed. Preserve any effort or fast-mode choice explicitly requested for that review. This is an agent-selected, case-by-case fallback, not an automatic retry on every command failure.
- **Fallback is not sticky.** Do not change the skill's Fable 5.1 default, persistent Claude settings, or future-review routing. At the next review, start from Fable 5.1 again and assess any limitation afresh; a previous fallback is not permission to keep using Opus 5.5.
- Briefly disclose why the fallback was needed and which model actually performed the review. Keep the same complete review scope, read-only restrictions, and review completion gate. Repair missing inputs or access problems rather than assuming a model switch fixes them. If neither reviewer can complete the review after safe, in-scope remediation, follow the stop-and-report rule.

## Primary-agent completion gate

This gate applies before packet construction or reviewer invocation.

- For work not already produced, independently complete the exact RCA, plan, implementation, analysis, document, or other artifact that will be reviewed. It must be your best attempt, with all normal in-scope work applicable to it—including investigation, reasoning, implementation, tests, and validation—already performed, and it must be ready to deliver or act on if no reviewer were available.
- Claude is an independent reviewer, not a collaborative thought partner or a substitute for your work. Do not invoke it to brainstorm, draft, investigate, fill gaps, resolve placeholders, finish the artifact, or offset effort you should perform yourself. Never reduce your own effort because a second-opinion pass is expected.
- If the user asks you to do something and then obtain a second opinion, finish that work to completion before invoking this skill. Do not split the work with Claude or send an intermediate attempt for Claude to complete.
- If you already produced an artifact and the user then asks for a second opinion on that existing work, submit the existing artifact directly; do not recreate it merely to satisfy this gate. This exception does not permit preparing a new partial artifact when the requested sequence is to do the work and then review it.
- For `independent-rca`, complete your own best RCA before invoking Claude, while continuing to omit that RCA from the packet so Claude's analysis remains independent. Consider reviewer feedback and make any revisions only after a valid review completes.

## Review completion gate

A finished subprocess is not necessarily an operationally successful review. This gate closes the loophole where the command ran but Claude could not perform the requested review. Operational success requires successful execution, access to every critical/load-bearing related material, and coverage of the requested scope; it does not require artifact quality or reviewer agreement. A substantively critical or rejecting verdict is still a completed review only when Claude had the required access and addressed the requested scope. Before treating this skill as complete, verify all of the following:

- The Claude command exited successfully and produced non-empty, substantive output with exactly one normalized `REVIEW STATUS: COMPLETE` marker and no `REVIEW STATUS: INCOMPLETE` marker within its first five non-empty lines.
- The output identifies the concrete repository target and critical/load-bearing related materials Claude inspected and does not disclose any such material as inaccessible or uninspected.
- The output substantively addresses the entire requested scenario and scope. Claude must have used the correct packet, transcript, repository or diff, and every referenced plan, implementation, document, analysis, or image needed for the review. For a two-phase scenario, both phases must be present.
- Claude independently inspects the primary material needed to form its second opinion rather than merely accepting the invoking agent's conclusions. Do not expand this into reproducing every test or verifying every incidental claim unless the requested review or Claude's own verdict materially depends on that check. User-stated premises remain user inputs unless the requested review expressly asks Claude to verify them.
- If authentication, evidence, permissions, paths, or tools prevent Claude from inspecting any critical/load-bearing related material or performing a check essential to its review, the review failed operationally and is incomplete. If the artifact itself omits information as a defect in the artifact, Claude should critique that omission and may still mark the review complete when it fully assessed the artifact as presented; this exception does not apply to a separate or referenced critical material that Claude was expected but unable to inspect. A limitation affecting only a non-material point may be disclosed without invalidating an otherwise complete review, but Claude must not use that unverified point to support its verdict.
- Neither the output nor the execution evidence reveals an authentication, credential, permission, tool, path, repository, context, or model failure affecting the required scope. Treat a misunderstood packet or scenario, a skipped material target, generic feedback that does not engage with the target, a partial review, or a stated inability to inspect a material or required item as incomplete even if the command exited zero or some useful feedback was returned.

Exactly one `REVIEW STATUS: COMPLETE` marker and no `REVIEW STATUS: INCOMPLETE` marker within the first five non-empty lines is necessary but not sufficient. Read the full output and validate that the review actually ran across the requested scope with access to every critical/load-bearing related material. Any disclosure such as “I could not access X” makes the pass operationally incomplete when X is critical/load-bearing, regardless of the marker, exit code, or usefulness of the remaining feedback. Do not reinterpret criticism, a negative verdict, recommendations, or questions as an incomplete review. Your own inspection cannot substitute for a part Claude failed to review, but your judgment—not Claude's—is final once a valid review has been considered.

If the completion gate fails:

1. Identify a concrete execution, access, packet, or review-coverage failure before rerunning. If that cause can be corrected without user input or new authority, fix it and rerun the second-opinion review. This is required, not optional. Examples include repairing the packet or prompt, correcting a scenario or path, provisioning already-authorized read-only access, adding missing primary material, or enabling a safe local validator inside the enforced read-only sandbox. Do not change the reviewed artifact merely to obtain a more favorable verdict, do not repeat an unchanged failing command, and validate the replacement output against this gate. Attempts required to obtain the first valid review do not count as additional discretionary reviews under the default-frequency rule.
2. If Claude reviewed only part of the intended scope, treat the whole pass as incomplete. Repair and rerun it; do not fill the gap yourself and continue. If the concrete failure prevents using Fable for this review, apply the per-review Opus 5.5 fallback above and validate the replacement review against the same completion gate.
3. Only after safe, in-scope remediation is exhausted, if the review still cannot be completed as intended or the remaining remediation requires unavailable credentials, user input, new authority, or an external state change, **stop the task under review**. This is a pause for the user's decision, not permission to finish the work. Tell the user what failed, what was attempted, and what is needed, and provide the run-directory evidence when available. Do not implement, revise, finalize, or return the reviewed artifact as though the review occurred. Resume only after a valid review completes or the user explicitly directs you to proceed without it.

## Usage

```
/claude-second-opinion [model] [effort] [--fast]
```

- `/claude-second-opinion` — uses Fable 5.1 with `max` effort at standard speed (default)
- `/claude-second-opinion --fast` — uses Fable 5.1 with `max` effort in fast mode
- `/claude-second-opinion fable` — uses Fable 5.1 with `max` effort at standard speed
- `/claude-second-opinion fable xhigh --fast` — uses Fable 5.1 with `xhigh` effort in fast mode
- `/claude-second-opinion fable-5` — uses Fable 5 with `max` effort at standard speed
- `/claude-second-opinion opus` — uses Opus 5.5 with `max` effort at standard speed
- `/claude-second-opinion opus xhigh --fast` — uses Opus 5.5 with `xhigh` effort in fast mode
- `/claude-second-opinion opus-5.5 --fast` — uses Opus 5.5 with `max` effort in fast mode

Supported models are `fable` / `fable-5.1` (Fable 5.1), `fable-5` (Fable 5), `opus` / `opus-5.5` (Opus 5.5). Supported effort values are `xhigh` and `max`. If you pass only an effort value, it applies to the default Fable 5.1 model.
Fast mode is disabled by default. Pass `--fast` only when the user explicitly requests fast mode for that specific review; do not enable it at agent discretion. The skill passes the choice as a command-line session setting, so it does not change the caller's persistent Claude settings.
For a requested fast run, the command also skips Claude Code's client-side fast-mode organization preflight. This lets setup-token and gateway users reach the authoritative Anthropic API check instead of receiving a false "disabled by your organization" result. The API still rejects fast requests when the organization actually disables them.

Use one of these scenarios:

| Scenario | When | What Claude does |
|---|---|---|
| `independent-rca` | After completing your own best RCA, before fixing a bug | Performs its own independent RCA from scratch — does NOT see yours |
| `plan-review` | After completing your best implementation-ready plan, before implementing | Reviews and critiques the plan |
| `post-implementation-review` | After completing the implementation and your normal validation, before finalizing | Reviews the changes against the plan or user requirements |
| `document-review` | After completing your best final-ready non-technical artifact (business doc, product analysis, strategy work, email, spec, etc.) | Performs its own independent analysis from the user's inputs, then compares against your output for fidelity, clarity, logic, completeness, and soundness |
| `analysis-review` | After completing your best final-ready analysis for a substantial chat response, comment, recommendation, or similar output | Provides an independent second opinion on reasoning, assumptions, completeness, fidelity to the user's request, and actionability |

## Non-negotiables

- Always include every user-typed message from the current session verbatim, in chronological order. Include only the text the user actually typed — do not expand @-mentions, and do not include attached context blocks (open editor tabs, inlined file contents, skill text, git diffs, or other automatically injected context). If the user attached images in any message, save them to the generated per-run images directory and list each resolved absolute image path in the packet.
- **Deduplicate repeated user messages.** If the same user message appears multiple times in your context (from context reloading, conversation resumption, etc.), include it only once at its first chronological occurrence. Deduplicate *before* writing the packet, not after.
- If you cannot recover every prior user message or image, say so explicitly.
- Keep the packet minimal. Do not paste large code excerpts, repository summaries, or diff excerpts unless strictly necessary. Claude inspects the repository directly and forms its own judgment.

## Build the review packet

Compose the packet content in your head first, then write it to disk in one shot using shell commands. **Do not use file-editing tools** (ApplyPatch, WriteFile, EditFile, etc.) for the packet — they are unreliable for temp files and frequently produce residual content, partial overwrites, or stale reads.

First create a unique per-run directory and record the printed paths:

```sh
TMP_BASE="${TMPDIR:-/tmp}"
TMP_BASE="${TMP_BASE%/}"
RUN_DIR="$(mktemp -d "$TMP_BASE/claude-second-opinion-XXXXXX")"
PACKET_PATH="$RUN_DIR/packet.md"
OUT_PATH="$RUN_DIR/output.txt"
IMAGE_DIR="$RUN_DIR/images"
mkdir -p "$IMAGE_DIR"
printf 'Second-opinion run directory: %s\n' "$RUN_DIR"
printf 'Packet path: %s\n' "$PACKET_PATH"
printf 'Output path: %s\n' "$OUT_PATH"
printf 'Images directory: %s\n' "$IMAGE_DIR"
```

Use the same generated paths for every later step in this invocation. If a later command runs in a fresh shell, substitute the literal path printed above (for example, `/var/folders/.../claude-second-opinion.AbC123/packet.md`) instead of relying on `$PACKET_PATH`, `$OUT_PATH`, `$IMAGE_DIR`, or `$RUN_DIR` still being defined.

**Required method — shell heredoc or Python one-liner:**

```sh
PACKET_PATH="/var/folders/.../claude-second-opinion.AbC123/packet.md"
cat > "$PACKET_PATH" << 'PACKET_EOF'
(entire packet content here)
PACKET_EOF
```

Replace the example path with the literal packet path printed during setup.

If the packet is too large for a single heredoc (shell truncation or escaping issues), use a Python one-liner instead:

```sh
PACKET_PATH="/var/folders/.../claude-second-opinion.AbC123/packet.md"
PACKET_PATH="$PACKET_PATH" python3 -c '
import os
import sys
from pathlib import Path
content = sys.stdin.read()
Path(os.environ["PACKET_PATH"]).write_text(content)
' << 'PACKET_EOF'
(entire packet content here)
PACKET_EOF
```

The packet must contain the sections below — no more, no less.

### Scenario

One of: `independent-rca`, `plan-review`, `post-implementation-review`, `document-review`, `analysis-review`.

### Review target in repository

Tell Claude what to look at. Examples:
- "Investigate the bug described in the user transcript against the current repository."
- "Evaluate the implementation plan at `docs/plan.md` against the current repository."
- "Review the uncommitted working tree against HEAD."
- "Review commit `<sha>`."
- "Review branch diff `<base>...HEAD`."

Frame the target as work Claude must independently perform, not as conclusions or verification already established. Identify the agent-supplied claims, test results, analysis, or code behavior that Claude must check to perform the requested review; do not present those items as trusted background.

Explicitly identify every critical/load-bearing related material and give Claude viable access to it. When the request specifically requires checking a factual claim or test result, provide a practical verification path through direct, already-authorized read-only access or complete primary evidence. Do not turn an ordinary critique into an exhaustive reproduction of every claim or test. Preflight the actual Claude invocation—not merely the main agent's environment—to ensure it can read the packet, target, every critical/load-bearing related material, and any other required input and perform any essential safe checks. Never put secrets in the packet or logs.

Do not knowingly launch a review that lacks a required capability merely to obtain `INCOMPLETE`. If a viable path can be prepared without user input or new authority, prepare it first. If an unforeseen access or tooling problem appears in the output, repair it and rerun under the completion-gate procedure. If no viable path can be prepared without the user's help or new authority, stop before the run and report what is needed rather than narrowing the scope.

### Full verbatim user transcript

All user-typed messages from this session, verbatim and in chronological order. Include only the text the user actually typed — do not expand @-mentions, and do not include attached context blocks (open editor tabs, inlined file contents, skill text, git diffs, or other automatically injected context). If the user attached images, note where each image appeared in the conversation and list its resolved absolute path under the generated per-run images directory so Claude can read it.

### Artifact under review (plan-review, post-implementation-review, document-review, and analysis-review only)

**Omit this entire section for `independent-rca`.** Claude must not see your analysis — it performs its own from scratch.

For every artifact-bearing scenario, the artifact must be your complete best attempt: finished, normally validated, and ready to deliver or act on without Claude's help. Do not submit notes, exploratory work, partial drafts, unresolved placeholders, or requests for Claude to create or finish any part. When the user specifically requests review of an artifact you already produced, include that existing artifact directly as allowed by the primary-agent completion gate.

- For `plan-review`: if the plan exists as a file on disk, provide only the file path (e.g., "See plan at `docs/plan.md`"). Do NOT copy, paste, or summarize the plan contents into this packet — Claude will read the file directly. Only inline the plan text if no plan file exists on disk.
- For `post-implementation-review`: a short implementation summary, intended behavior, and the path to the plan markdown file if one exists (so Claude can read it). Do not copy plan contents into the packet. Do not include large diff excerpts unless strictly necessary.
- For `document-review`: structure this section in two clearly labeled parts:
  1. **User's request** — what the user asked for (e.g., "analyze competitor pricing," "draft investor email," "recommend a GTM channel"). One sentence.
  2. **Agent's output** — your complete analysis, recommendations, or drafted artifact. If it was written to a file on disk, provide only the file path — do not inline the contents. Inline only if it lives solely in the conversation. Be explicit about which parts are the user's stated inputs and which are your analysis, reasoning, or additions.
- For `analysis-review`: structure this section in two clearly labeled parts:
  1. **User's request** — the analysis or recommendation the user asked for. One sentence.
  2. **Analysis output** — your complete analysis, recommendation, or response draft. If it was written to a file on disk, provide only the file path — do not inline the contents. Inline only if it lives solely in the conversation. Be explicit about key assumptions, evidence, and recommendations.

## Validate the packet before sending

After writing, validate using **shell commands only** (not file-reading tools — they can disagree with the actual file content for temp paths). If this is a new shell command, substitute the literal packet path printed during setup:

```sh
# Structural checks — all must pass
PACKET_PATH="/var/folders/.../claude-second-opinion.AbC123/packet.md"
echo "=== Structural validation ==="
echo "Scenario count: $(grep -c '^### Scenario' "$PACKET_PATH")"
echo "Artifact count: $(grep -c '^### Artifact under review' "$PACKET_PATH")"
echo "Line count: $(wc -l < "$PACKET_PATH")"
echo "=== First 5 and last 5 lines ==="
head -5 "$PACKET_PATH"
echo "..."
tail -5 "$PACKET_PATH"
```

**Pass criteria:**
- Exactly 1 `### Scenario` heading
- For `independent-rca`: 0 `### Artifact under review` headings
- For `plan-review` / `post-implementation-review` / `document-review` / `analysis-review`: exactly 1 `### Artifact under review` heading
- Line count is reasonable (typically under 200 lines; investigate if over 300)

If structural validation fails, rewrite the packet from scratch at the same generated packet path — do not patch the file.

**Then do a content review.** Read the full file back using `cat /var/folders/.../claude-second-opinion.AbC123/packet.md` or `cat "$PACKET_PATH"` when `$PACKET_PATH` is defined in the same shell command (not file-reading tools) and check for:

- **Duplicate user messages** — the same message appearing more than once (from context reloading, conversation resumption, etc.). This means deduplication before writing was incomplete.
- **Redundant sections** — the same information stated in multiple places (e.g., the review target restating what's already in the transcript, or the artifact section repeating the review target).
- **Accidentally included context** — attached context blocks, expanded @-mentions, inlined file contents, skill text, or git diffs that leaked into the user transcript.
- **Agent claims framed as established facts** — factual claims, test results, or conclusions from the invoking agent that the review should independently verify but that the packet presents as trusted background.
- **Bloat** — large code excerpts, diff dumps, or repository summaries that Claude doesn't need (it reads the repo directly).
- **Premature handoff** — unless the user specifically requested review of an already-produced artifact, the target is partial, contains unresolved artifact-creation work you should complete, or asks Claude to develop or finish it.

If any content issues are found, rewrite the packet from scratch at the same generated packet path — do not patch the file.

## Run Claude from the repository root

Claude's analysis of a full repo can take several minutes. Complex reviews sometimes take up to 10 minutes and occasionally take 10-20 minutes. Reviews over 30 minutes are suspect; ideally ask the user before killing a long-running review, but around 30 minutes you may decide to kill it using judgment.

Run this as a shell command. Set `SCENARIO` to match the packet, and substitute the literal packet and output paths printed during setup.

The command below preserves the established default permission behavior. If the verification preflight identified a required safe read-only command or MCP tool that it does not expose, narrowly adjust the relevant tool allowlist, MCP configuration, or sandbox setting for that run. Preserve repository read-only behavior and do not expose broad credentials.

By default, the command auto-selects Claude execution mode:

- `CLAUDE_SECOND_OPINION_MODE=auto` (default) uses `--bare` inside Hermes worker/profile contexts and normal `claude -p` elsewhere.
- `CLAUDE_SECOND_OPINION_MODE=bare` forces Hermes-worker style execution. This is recommended for Hermes workers because `--bare` skips hooks, LSP, plugin sync, attribution, auto-memory, background prefetches, keychain reads, OAuth/session discovery, and CLAUDE.md auto-discovery. In the AGE-62 bridge setup it uses the API-key/apiKeyHelper path supplied to the real Claude child process while the surrounding shell stays credential-scrubbed.
- `CLAUDE_SECOND_OPINION_MODE=normal` forces normal Claude Code execution for personal terminals or other runtimes that should use the caller's own Claude auth and normal Claude Code behavior.

```sh
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
SCENARIO="independent-rca"  # set to: independent-rca, plan-review, post-implementation-review, document-review, or analysis-review
PACKET_PATH="/var/folders/.../claude-second-opinion.AbC123/packet.md"
OUT_PATH="/var/folders/.../claude-second-opinion.AbC123/output.txt"

MODEL="claude-fable-5-1"
EFFORT="max"
FAST_MODE="false"
FAST_MODE_OPTION_SEEN="false"
MODEL_ARG=""
EFFORT_ARG=""
CONFIG_ARG="{{args}}"
if [ -n "$CONFIG_ARG" ]; then
  set -- $CONFIG_ARG
  for ARG in "$@"; do
    case "$ARG" in
      --fast)
        if [ "$FAST_MODE_OPTION_SEEN" = "true" ]; then
          printf 'Duplicate claude-second-opinion option: %s\nSpecify --fast at most once.\n' "$ARG" >&2
          exit 2
        fi
        FAST_MODE="true"
        FAST_MODE_OPTION_SEEN="true"
        ;;
      --*)
        printf 'Unsupported claude-second-opinion option: %s\nSupported option: --fast.\n' "$ARG" >&2
        exit 2
        ;;
      *)
        if [ -z "$MODEL_ARG" ]; then
          MODEL_ARG="$ARG"
        elif [ -z "$EFFORT_ARG" ]; then
          EFFORT_ARG="$ARG"
        else
          printf 'Unsupported claude-second-opinion arguments: %s\nUse no argument for Fable 5.1 max at standard speed, an effort only, "<model> <effort>", and optionally --fast when the user requests it.\n' "$CONFIG_ARG" >&2
          exit 2
        fi
        ;;
    esac
  done

  case "$MODEL_ARG" in
    "")
      ;;
    opus|opus-5.5|opus-5-5|claude-opus-5.5|claude-opus-5-5)
      MODEL="claude-opus-5-5"
      EFFORT="${EFFORT_ARG:-max}"
      ;;
    fable|fable-5.1|fable-5-1|claude-fable-5.1|claude-fable-5-1)
      MODEL="claude-fable-5-1"
      EFFORT="${EFFORT_ARG:-max}"
      ;;
    fable-5|claude-fable-5)
      MODEL="claude-fable-5"
      EFFORT="${EFFORT_ARG:-max}"
      ;;
    xhigh|max)
      MODEL="claude-fable-5-1"
      EFFORT="$MODEL_ARG"
      if [ -n "$EFFORT_ARG" ]; then
        printf 'Unsupported claude-second-opinion arguments: %s\nAn effort-only invocation accepts one effort value plus optional --fast.\n' "$CONFIG_ARG" >&2
        exit 2
      fi
      ;;
    *)
      printf 'Unsupported claude-second-opinion model or effort: %s\nSupported models: opus, opus-5.5, fable, fable-5.1, fable-5. Supported efforts: xhigh, max.\n' "$MODEL_ARG" >&2
      exit 2
      ;;
  esac
fi

case "$EFFORT" in
  xhigh|max) ;;
  *)
    printf 'Unsupported claude-second-opinion effort: %s\nSupported efforts: xhigh, max.\n' "$EFFORT" >&2
    exit 2
    ;;
esac

if [ "$MODEL" = "claude-fable-5-1" ] && [ "$EFFORT" != "max" ]; then
  printf 'Warning: Fable 5.1 defaults to max effort; using explicitly requested effort: %s\n' "$EFFORT" >&2
elif [ "$MODEL" = "claude-fable-5" ] && [ "$EFFORT" != "max" ]; then
  printf 'Warning: Fable 5 defaults to max effort; using explicitly requested effort: %s\n' "$EFFORT" >&2
elif [ "$MODEL" = "claude-opus-5-5" ] && [ "$EFFORT" != "max" ]; then
  printf 'Warning: Opus 5.5 defaults to max effort; using explicitly requested effort: %s\n' "$EFFORT" >&2
fi

FAST_MODE_SETTINGS="$(printf '{"fastMode": %s}' "$FAST_MODE")"
if [ "$FAST_MODE" = "true" ]; then
  export CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1
else
  unset CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK
fi

# Claude execution mode. Keep normal Claude Code behavior available for
# personal terminals while defaulting Hermes workers/profiles to --bare.
CLAUDE_SECOND_OPINION_MODE="${CLAUDE_SECOND_OPINION_MODE:-auto}"
if [ "$CLAUDE_SECOND_OPINION_MODE" = "auto" ]; then
  if [ -n "${HERMES_HOME:-}" ] || [ -n "${HERMES_PROFILE:-}" ] || [ -n "${HERMES_TASK_ID:-}" ] || [ -n "${HERMES_KANBAN_TASK:-}" ]; then
    CLAUDE_SECOND_OPINION_MODE="bare"
  else
    CLAUDE_SECOND_OPINION_MODE="normal"
  fi
fi

case "$CLAUDE_SECOND_OPINION_MODE" in
  bare) CLAUDE_BARE_FLAG="--bare" ;;
  normal) CLAUDE_BARE_FLAG="" ;;
  *)
    echo "Invalid CLAUDE_SECOND_OPINION_MODE='$CLAUDE_SECOND_OPINION_MODE' (expected auto, bare, or normal)" >&2
    exit 2
    ;;
esac

if [ "$SCENARIO" = "independent-rca" ]; then
  PROMPT="Read the review packet from stdin. You are performing an independent root-cause analysis. The packet contains only the user transcript and a pointer to the repository — do NOT treat it as containing a prior analysis. If the packet references image files, read them with the Read tool. Inspect the repository directly using your allowed tools. Return: your root-cause hypothesis, supporting evidence from the codebase, suggested fix approach, confidence level, and any questions for the user that would help narrow the diagnosis."
elif [ "$SCENARIO" = "plan-review" ]; then
  PROMPT="Read the review packet from stdin. You are reviewing an implementation plan. If the packet references image files, read them with the Read tool. Inspect the repository directly using your allowed tools and use independent judgment. Critique the plan for correctness, completeness, safety, and maintainability. Return: verdict, key issues, recommended changes, missing considerations, and any questions for the user."
elif [ "$SCENARIO" = "document-review" ]; then
  PROMPT="Read the review packet from stdin. You are providing a second opinion on a non-technical artifact (business document, product analysis, strategy work, email, spec, or similar). Work in two phases. PHASE 1 — INDEPENDENT ANALYSIS: Read only the user transcript and inspect the repository for context. Based solely on what the user asked for and the information available, form your own independent analysis, recommendations, or conclusions. Write this up before proceeding to Phase 2. PHASE 2 — COMPARISON: Now read the 'Artifact under review' section containing the other agent's output. Compare your independent conclusions against theirs. Evaluate: (1) fidelity — does the output faithfully reflect what the user actually said, without adding unsupported claims, mischaracterizing user inputs, or injecting assumptions? (2) divergences — where do your conclusions differ, and why? (3) clarity and logic — is the argument coherent, well-structured, and free of gaps? (4) completeness — are there obvious omissions given the user's stated goals? (5) soundness — are conclusions well-supported and recommendations actionable? If the packet references image files, read them with the Read tool. Return: your independent analysis (Phase 1), then the comparison (Phase 2) covering verdict, agreements, divergences with reasoning, fidelity issues, recommended changes, and any questions for the user."
elif [ "$SCENARIO" = "analysis-review" ]; then
  PROMPT="Read the review packet from stdin. You are providing a second opinion on a non-trivial analysis response, recommendation, comment, or similar user-facing output. Work in two phases. PHASE 1 — INDEPENDENT ANALYSIS: Read only the user transcript and inspect the repository for context when relevant. Based solely on what the user asked for and the information available, form your own independent analysis, recommendations, or conclusions. Write this up before proceeding to Phase 2. PHASE 2 — COMPARISON: Now read the 'Artifact under review' section containing the other agent's analysis output. Compare your independent conclusions against theirs. Evaluate: (1) fidelity — does the output answer what the user actually asked, without unsupported claims or mischaracterized inputs? (2) reasoning — are conclusions logically supported by evidence and stated assumptions? (3) divergences — where do your conclusions differ, and why? (4) completeness — are there obvious omissions or unaddressed tradeoffs? (5) actionability — are recommendations concrete enough to use? If the packet references image files, read them with the Read tool. Return: your independent analysis (Phase 1), then the comparison (Phase 2) covering verdict, agreements, divergences with reasoning, fidelity issues, missing considerations, recommended changes, and any questions for the user."
else
  PROMPT="Read the review packet from stdin. You are reviewing a completed implementation. If the packet references image files, read them with the Read tool. Inspect the repository directly using your allowed tools and use independent judgment. Evaluate correctness, safety, maintainability, and fidelity to the plan or user requirements. Return: verdict, key issues, recommended changes, missing tests or checks, and any questions for the user."
fi

COMPLETION_REQUIREMENT='Before returning, verify that you actually read and evaluated the complete intended review target and every critical/load-bearing related material. Independently inspect the primary material needed to form your opinion; do not accept conclusions supplied by the invoking agent as established background. Do not expand an ordinary review into reproducing every test or checking incidental facts outside the requested scope unless your verdict materially depends on that check. User-stated premises remain inputs unless the packet expressly asks you to verify them. Use read-only tools for safe checks essential to the requested review. REVIEW STATUS reports operational success: successful execution, access to every critical/load-bearing related material, and coverage of the requested scope; it does not grade the artifact. Return REVIEW STATUS: COMPLETE even when you reject the artifact, identify blocking defects or omissions, recommend changes, disagree with the invoking agent, or raise questions, provided you inspected every critical/load-bearing related material and addressed the full requested scope. Return REVIEW STATUS: INCOMPLETE when an execution, authentication, permission, tool, path, missing-input, or other access/coverage failure prevented access to any critical/load-bearing related material or prevented any other required part of the review; identify the precise failure so the caller can repair it and rerun. If you state that you could not access X and X is critical/load-bearing, you must mark the review INCOMPLETE even if you provide useful feedback on the rest. An omission within the artifact is review feedback only when that absence is itself a defect in the artifact, not when a separate or referenced critical material was expected but inaccessible. Within the first five non-empty lines, write exactly one REVIEW STATUS: COMPLETE line and no REVIEW STATUS: INCOMPLETE line for an operationally successful review; otherwise write exactly one REVIEW STATUS: INCOMPLETE line and no REVIEW STATUS: COMPLETE line. Then identify the concrete repository target, critical/load-bearing related materials, and material checks you inspected and disclose anything you could not access or evaluate. Never label a partial review complete.'
PROMPT="$PROMPT $COMPLETION_REQUIREMENT"

cd "$REPO_ROOT" && claude ${CLAUDE_BARE_FLAG:+$CLAUDE_BARE_FLAG} -p \
  --settings "$FAST_MODE_SETTINGS" \
  --model "$MODEL" \
  --effort "$EFFORT" \
  --permission-mode default \
  --tools "Bash,Read,Grep,Glob" \
  --allowedTools "Read,Grep,Glob,Bash(pwd),Bash(ls *),Bash(git *)" \
  --max-turns 100 \
  --output-format text \
  "$PROMPT" \
  < "$PACKET_PATH" \
  > "$OUT_PATH"

REVIEW_EXIT="$?"
if [ "$REVIEW_EXIT" -ne 0 ]; then
  printf 'Claude second-opinion command failed with exit code %s. The review is incomplete.\n' "$REVIEW_EXIT" >&2
  exit "$REVIEW_EXIT"
fi
if [ ! -s "$OUT_PATH" ]; then
  echo "Claude second-opinion output is empty. The review is incomplete." >&2
  exit 3
fi
REVIEW_WINDOW="$(awk 'NF { print; seen++; if (seen == 5) exit }' "$OUT_PATH" | tr -d '\r' | sed -E 's/^[-[:space:]#>*_`~]+//; s/[-[:space:]*_`~.]+$//')"
REVIEW_COMPLETE_COUNT="$(printf '%s\n' "$REVIEW_WINDOW" | grep -Fxc 'REVIEW STATUS: COMPLETE' || true)"
REVIEW_INCOMPLETE_COUNT="$(printf '%s\n' "$REVIEW_WINDOW" | grep -Fxc 'REVIEW STATUS: INCOMPLETE' || true)"
if [ "$REVIEW_COMPLETE_COUNT" -ne 1 ] || [ "$REVIEW_INCOMPLETE_COUNT" -ne 0 ]; then
  echo "Claude second-opinion did not provide exactly one COMPLETE marker and no INCOMPLETE marker within its first five non-empty lines. Inspect the output, repair the cause, and rerun." >&2
  exit 3
fi
```

## After the review

1. Read the generated output path, for example `/var/folders/.../claude-second-opinion.AbC123/output.txt`.
2. Apply the review completion gate before doing anything else. If it fails, repair and rerun or stop and report as required above; do not continue with the remaining steps on the strength of an incomplete pass.
3. Once a valid review exists, tell the user the successful second-opinion run directory path so they can inspect the packet and output if needed.
4. If an earlier attempt failed the completion gate and was repaired, briefly tell the user what failed and what changed before the successful rerun.
5. Treat Claude's completed feedback as advisory. Retain final authority over the task and accept or reject any recommendation on its merits; a negative verdict does not block continuation after this gate is satisfied.
6. For `independent-rca`: compare Claude's RCA with your own. Evaluate both and use your best judgment for the fix.
7. For `plan-review` and `post-implementation-review`: apply only material improvements to correctness, safety, maintainability, or clarity.
8. For `document-review` and `analysis-review`: compare Claude's independent analysis (Phase 1) with your own. Where conclusions diverge, evaluate both on their merits. Correct any fidelity issues Claude flagged — places where your output diverged from or added to what the user actually said. Apply improvements to clarity, logic, completeness, and actionability only where material.
9. Do not rerun the review after applying or rejecting feedback merely to ask whether Claude now agrees or considers the artifact acceptable. A valid completed pass satisfies the second-opinion requirement for that artifact.
10. If the feedback reveals a genuine user decision or preference that you cannot safely resolve, ask the user. Claude's questions alone do not require escalation.
11. Briefly note any material changes you adopted from the completed review and, when useful, material feedback you declined.

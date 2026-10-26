# Plan: add a `max` level to fusion-second-opinion (local fusion, no OpenRouter)

## Goal

Add a fourth level, `max`, to the fusion-second-opinion skill:

- **Analysis panel:** Claude Fable 5 (max effort) + GPT-5.6-Sol (max effort)
- **Synthesizer:** Claude Opus 4.8 at **max** effort (same as the existing `high` synthesizer — see "Effort at `high`" below)
- **No OpenRouter.** The fusion pipeline is replicated locally using the Claude Code CLI and the Codex CLI, both authenticated with their OAuth tokens and billed to their subscriptions (Claude subscription for `claude`, ChatGPT subscription for `codex`). No API-key billing.

The existing `medium` / `high` / `xhigh` OpenRouter proxy path is unchanged.

## Research: how OpenRouter Fusion works (deep-dive, spike-verified)

Full findings with spike evidence live in `references/openrouter-fusion-orchestration.md` (live call against `openrouter/fusion`, 2026-07-22). The load-bearing facts:

1. **The plugin's `model` is the primary model, the judge, AND the final writer.** With the `openrouter/fusion` alias the request resolves to the plugin `model`; in this skill that means Opus 4.8 both drives the Codex repo-inspection loop and judges/synthesizes.
2. **Panel stage** — the `analysis_models` (1–8) answer **in parallel and independently**, blind to each other, but they receive only a **distilled one-paragraph prompt written by the primary model** in its `openrouter:fusion` tool call — *not* the conversation, *not* the repo. Their tools are web search/fetch (agentic loop bounded by `max_tool_calls`, default 8, range 1–16).
3. **The panel runs only if the primary model chooses to call the tool** (spike usage showed `tool_calls_requested/executed`); nothing forces a consultation.
4. **Judge stage** — compares, never merges. Verbatim schema observed: `{consensus, contradictions:[{topic, stances:[{model, stance}]}], partial_coverage, unique_insights:[{model, insight}], blind_spots}`. Consensus = higher confidence.
5. **Final response** — the primary model writes it grounded in the **raw panel responses** (which it demonstrably receives — panel web citations propagate into the final message's annotations) **plus** the structured analysis and its own context.
6. **Effort** — request-level `reasoning.effort` observably applies to the primary; propagation to panelists is undocumented and unobservable. The proxy injects `{"effort": "max"}` with no per-model overrides.

In the current skill, the localhost proxy injects `plugins: [{id: "fusion", model: <synthesizer>, analysis_models: [<panel>]}]` and `reasoning: {"effort": "max"}` into every POST, and Codex (model `openrouter/fusion` → Opus 4.8) drives the repo inspection. Notably, **the OpenRouter panel never sees the repository** — only the synthesizer-side model does.

### Effort at `high` (question answered)

The proxy sets a single request-level `reasoning.effort = "max"` on every request, and no per-model overrides exist anywhere in the skill. So at `high`, **all four model slots run at requested max effort**: panel Opus 4.8 = max, GPT-5.6-Sol = max, Gemini 3.1 Pro = max (mapped to its thinking budget), and the Opus 4.8 **synthesizer = max**. (OpenRouter's docs do not document per-stage effort propagation; what is verifiable is that the skill requests max at the request level and requests nothing else.) Therefore the new `max` level's synthesizer is **Opus 4.8 at `--effort max`**.

### Local replication mapping (parity-or-better, same models assumed)

| OpenRouter Fusion stage | Local `max` replication | Parity |
|---|---|---|
| Panel member (parallel, independent) | `claude -p --model claude-fable-5 --effort max` with read-only tools; `codex exec -m gpt-5.6-sol -c model_reasoning_effort=max --sandbox read-only`. Both run concurrently, blind to each other. | **Better**: OpenRouter panelists get only a distilled prompt + web; ours get the full packet **and** direct read-only repo access. |
| Panel execution | Script-enforced, unconditional. | **Better**: OpenRouter's panel runs only if the primary model elects to call the tool. |
| Judge = final writer (plugin `model`) | One `claude -p --model claude-opus-4-8 --effort max` pass — same merge of judge + final writer as OpenRouter (verified: plugin `model` is both). It must first emit the structured analysis using the **exact observed schema** — consensus (higher-confidence), contradictions with per-model stances, partial coverage, unique insights per panelist, blind spots — then adjudicate contradictions and spot-check load-bearing claims against the repo read-only, then write the final unified review. | Equal-or-better (repo-grounded adjudication; OpenRouter's judge has web only). |
| Final-writer inputs | Synthesizer Reads both **raw panel outputs** (OpenRouter's final writer verifiably receives raw responses, not just the analysis) + packet + repo. | Equal. |
| Panel web tools | Fable panelist: `WebSearch,WebFetch` in its `--tools` list (built-in, work under subscription OAuth). GPT panelist: native Responses `web_search` enabled via `-c tools.web_search=true` — it executes server-side at OpenAI, so it works despite the local read-only sandbox (same server-side architecture as OpenRouter's `openrouter:web_search`). Synthesizer also gets `WebSearch,WebFetch`, matching OpenRouter's judge which has web tools available. | Equal. |
| Effort | Explicit per-CLI flags, guaranteed max for both panelists and synthesizer. | **Better**: OpenRouter panel effort propagation is unverifiable. |
| Mid-loop deliberation | Not replicated: OpenRouter's primary may consult the panel mid-investigation (`max_tool_calls` up to 16). Ours is a single-pass pipeline. | Acceptable gap: panelists get full context up front, removing the main reason to re-consult; review scenarios are single-deliberation shaped. Documented in SKILL.md. |

## Auth and billing guardrails (fail closed)

Preflights run before any panel work; any failure stops the review per the existing completion-gate rules:

- **Claude Code CLI**: `claude auth status` must report `"loggedIn": true` with an OAuth (claude.ai subscription) auth method — not an API key. Every `claude` invocation runs with `env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN` so it cannot silently fall back to API billing, and never uses `--bare` (which disables OAuth/keychain auth).
- **Codex CLI**: `codex login status` must report `Logged in using ChatGPT` (subscription OAuth, `auth_mode: chatgpt`). The invocation runs with `env -u OPENAI_API_KEY` so it cannot fall back to API billing. `CODEX_HOME` is respected if set (so a non-default auth home works).
- **No OpenRouter key is needed or read at `max`.** Passing a `key:`/`env:` selector together with `max` is a usage error (exit 2) — there is nothing for it to select.

On failure, the error message names the exact remediation (`claude /login` with the subscription account; `codex login` with ChatGPT), per the gate's stop-and-report rule.

## Changes

All changes are in `/Users/Shared/Projects/wagnerlabs/second-opinion/fusion-second-opinion/` (`SKILL.md` is canonical; `README.md` mirrors the levels table).

### 1. `SKILL.md` frontmatter description

Mention the new level: "…selectable medium/high/xhigh/max model panels…" and "max runs the panel locally via Claude Code CLI and Codex CLI subscription OAuth with no OpenRouter."

### 2. Fusion levels section

Add table row and prose:

| Level | Synthesizer | Analysis panel |
|---|---|---|
| `max` | `claude-opus-4-8` (max effort, Claude Code CLI) | `claude-fable-5` (max effort, Claude Code CLI), `gpt-5.6-sol` (max effort, Codex CLI) — local, subscription OAuth, no OpenRouter |

Note under the table: `max` does not use OpenRouter or any API key; it requires the `claude` CLI logged in with a Claude subscription (OAuth) and the `codex` CLI logged in with ChatGPT (OAuth).

### 3. OpenRouter API key selection section

Add: key selectors apply only to OpenRouter levels; combining a key selector with `max` exits with a usage error.

### 4. Argument parsing (existing run block)

- Add `max` to the level `case` arm: `medium|high|xhigh|max`.
- After parsing: `if [ "$FUSION_LEVEL" = "max" ] && [ -n "$OPENROUTER_KEY_SELECTOR" ]; then` → usage error, exit 2.
- Wrap the OpenRouter credential resolution, proxy write/start, and proxy-log gate checks in `if [ "$FUSION_LEVEL" != "max" ]` so `max` never touches OpenRouter credentials or the proxy.

### 5. New `max` runner (branch inside the run section)

Additional per-run paths created at setup time (all under the existing `RUN_DIR`):

```
FABLE_OUT="$RUN_DIR/fable-panel.txt"     GPT_OUT="$RUN_DIR/gpt-panel.txt"
FABLE_LOG="$RUN_DIR/fable-panel.log"     GPT_LOG="$RUN_DIR/gpt-panel.log"
SYNTH_LOG="$RUN_DIR/synthesis.log"       # final output stays at OUT_PATH
```

Runner logic (full script goes into SKILL.md; summarized here):

```sh
# Preflights (fail closed)
command -v claude && command -v codex                 # else exit 2 with remediation
CLAUDE_AUTH="$(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude auth status 2>/dev/null)"
printf '%s' "$CLAUDE_AUTH" | grep -q '"loggedIn":[[:space:]]*true'   # else exit 2: run /login with the Claude subscription account
codex login status 2>&1 | grep -q 'Logged in using ChatGPT'          # else exit 2: run codex login (ChatGPT OAuth)

# Panel prompts: the existing per-scenario PROMPT plus a panel-specific completion
# requirement (own REVIEW STATUS marker). Both panelists have read-only shell —
# codex via its OS sandbox, claude via the git/ls/pwd Bash allowlist — so the
# canonical COMPLETION_REQUIREMENT text applies to both unchanged.
# Packet delivery: prompt as argument, packet on stdin — the exact pattern
# claude-second-opinion and gpt-second-opinion use in production.

# Stage 1 — parallel, independent panel
( cd "$REVIEW_ROOT" && env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude -p \
    --model claude-fable-5 --effort max \
    --permission-mode default \
    --tools "Bash,Read,Grep,Glob,WebSearch,WebFetch" \
    --allowedTools "Read,Grep,Glob,WebSearch,WebFetch,Bash(pwd),Bash(ls *),Bash(git *)" \
    --max-turns 100 \
    --output-format text \
    --add-dir "$RUN_DIR" \
    "$FABLE_PROMPT" \
    < "$PACKET_PATH" > "$FABLE_OUT" 2> "$FABLE_LOG" ) &
FABLE_PID="$!"
( cd "$REVIEW_ROOT" && env -u OPENAI_API_KEY codex exec \
    --ephemeral --skip-git-repo-check \
    --sandbox read-only -C "$REVIEW_ROOT" \
    -m gpt-5.6-sol -c model_reasoning_effort=max -c 'approval_policy="never"' \
    -c tools.web_search=true \
    -o "$GPT_OUT" "$GPT_PROMPT" < "$PACKET_PATH" > "$GPT_LOG" 2>&1 ) &
GPT_PID="$!"
wait "$FABLE_PID"; FABLE_EXIT="$?"
wait "$GPT_PID";  GPT_EXIT="$?"

# Panel evidence gate: each exit 0, each output non-empty, each output carries
# exactly one REVIEW STATUS: COMPLETE and no INCOMPLETE in its first five
# non-empty lines (same normalization awk/sed as the existing marker check).
# Any panel failure/INCOMPLETE → whole pass incomplete (existing rule for a
# "required panel contribution"); repair and rerun per the gate.

# Stage 2 — judge + final response (Opus 4.8, max effort)
env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude -p \
  --model claude-opus-4-8 --effort max \
  --permission-mode default \
  --tools "Bash,Read,Grep,Glob,WebSearch,WebFetch" \
  --allowedTools "Read,Grep,Glob,WebSearch,WebFetch,Bash(pwd),Bash(ls *),Bash(git *)" \
  --max-turns 100 \
  --output-format text \
  --add-dir "$RUN_DIR" \
  "$SYNTH_PROMPT" > "$OUT_PATH" 2> "$SYNTH_LOG"
# then the existing non-empty + REVIEW STATUS marker checks on OUT_PATH
```

`SYNTH_PROMPT` (replicates the OpenRouter judge + final response, which are one model there too): identifies the scenario and `REVIEW_ROOT`; receives the packet on stdin (same pattern as the panel runs); points at `$FABLE_OUT` (Fable 5 panelist) and `$GPT_OUT` (GPT-5.6-Sol panelist) for it to Read in full; instructs it to (1) build the structured analysis using the exact schema OpenRouter's judge emits — `consensus` (treat as higher-confidence), `contradictions` as topic + per-panelist stances, `partial_coverage`, `unique_insights` per panelist, `blind_spots` neither addressed — (2) adjudicate contradictions and spot-check load-bearing claims against the repository read-only (this exceeds OpenRouter's web-only judge, deliberately), (3) write the final unified review grounded in the raw panel outputs **and** that analysis (mirroring OpenRouter, whose final writer receives both), and (4) emit the `REVIEW STATUS` marker within the first five non-empty lines, writing `COMPLETE` only if both panel contributions were present and complete and its own verification succeeded. The structured analysis must appear in the output ahead of the final review so the caller can see the fusion actually happened.

### 6. Completion gate wording

The bullet "The proxy log proves that the configured Fusion plugin was injected…" becomes level-conditional: for `medium`/`high`/`xhigh` it stays as is; for `max` the execution evidence is instead: both panel output files exist, are non-empty, exited 0, and carry their own `COMPLETE` markers, and the synthesizer output references both panelists in its structured analysis. A model's own claim that its panel ran remains insufficient — the per-stage files and exit codes are the evidence.

### 7. Run-section prose

- Note that at `max` the two panelists run in parallel, so wall-clock is roughly max(panelist) + synthesis; the same 10–30 minute guidance applies.
- Note the `claude` panelist/synthesizer read-only enforcement (no Edit/Write; Bash restricted by allowlist to `pwd`, `ls *`, `git *` — the same field-proven set claude-second-opinion uses) vs codex's OS-enforced read-only sandbox.

### 8. `README.md`

- Add the `max` row to the Levels table: "Fable 5 (max effort) + GPT-5.6-Sol (max effort), synthesized by Opus 4.8 (max effort) — local via Claude Code CLI + Codex CLI subscription OAuth, no OpenRouter."
- Add a short "Local `max` level" section covering the two required logins and that API keys are neither needed nor used.
- Note API-key selectors don't apply to `max`.

## Limitations and risks (stated in SKILL.md where relevant)

- **Claude seats have a git-scoped shell, not a full one.** The claude panelist and synthesizer get Bash allowlisted to `pwd` / `ls *` / `git *` (identical to claude-second-opinion), so diffs, logs, and blame work, but arbitrary validators/tests do not. The codex panelist covers arbitrary shell-based material checks in its OS-enforced read-only sandbox; if a material check can only be run by shell and the codex panelist did not run it, the synthesizer must mark the review INCOMPLETE (gate then drives repair/rerun). Per claude-second-opinion's precedent, a preflight-identified required safe read-only command may be narrowly added to the allowlist for that run.
- **Two-panelist panel.** `max` fuses two frontier models vs three at `high`, trading breadth for the strongest pair at max effort plus subscription billing. This matches Eric's spec exactly.
- **Auth is environment-dependent.** On a machine where `claude` or `codex` isn't OAuth-logged-in, the preflight fails closed with the exact login remediation. (On this machine today: codex is ChatGPT-logged-in; `claude auth status` reports logged out under the Hermes profile HOME — the preflight will surface that at run time.)
- **Model IDs.** `claude-fable-5` and `claude-opus-4-8` are the current full names; `gpt-5.6-sol` matches the gpt-second-opinion skill. All are pinned explicitly, not via aliases.

## Parity audit vs gpt-second-opinion and claude-second-opinion

Requirement: each model seat in fusion `max` must get at least as much out of its model as the sibling single-model skill does. Audited 2026-07-22 against both SKILL.md files:

| Dimension | gpt-second-opinion (GPT-5.6-Sol) | claude-second-opinion (Opus/Fable) | fusion `max` seat |
|---|---|---|---|
| Effort | `model_reasoning_effort=max` | `--effort max` (opus default; fable explicit) | Same flags, always max, all three seats |
| Repo access | full read-only sandbox + shell | Read/Grep/Glob + Bash allowlist `pwd`, `ls *`, `git *` | Identical per seat (codex seat = gpt-s-o config; claude seats = claude-s-o config) |
| Agent loop headroom | codex default | `--max-turns 100` | Identical per seat |
| Packet delivery | prompt arg + packet stdin | prompt arg + packet stdin | Identical |
| Scenario prompts + completion requirement | canonical | canonical | Same canonical text, plus panel/synthesis framing |
| Web research | none | none | **Added**: WebSearch/WebFetch (claude seats), native `web_search` (codex seat) — fusion edge, mirrors OpenRouter panel tools |
| Codex invocation flags | `--ephemeral` (user config + AGENTS.md intact) | n/a | Matched: dropped the proxy-only `--ignore-user-config --ignore-rules` flags from the OpenRouter path |
| Auth | Codex ChatGPT OAuth | OAuth normally; `--bare` + API-key bridge in Hermes worker contexts | **Deliberate divergence**: OAuth subscription only, fail closed — per the level's explicit billing requirement. No `--bare`, no API-key fallback. |

Net: every seat is configured identically to its single-model sibling or strictly better (web tools, deterministic max effort), with the one documented divergence being the OAuth-only auth policy.

## Out of scope

- No changes to `medium`/`high`/`xhigh` behavior, the packet format, scenarios, or the OpenRouter proxy.
- No changes to gpt-second-opinion or claude-second-opinion skills.

# How OpenRouter Fusion actually orchestrates (deep-dive, verified 2026-07-22)

Sources: OpenRouter Fusion plugin docs, the "Fusion beats frontier" announcement, third-party writeups, and a **live spike** against `openrouter/fusion` (generation `gen-1784774702-PjFeWxJtMGUs5FdRq1Wg`, cheap panel: Gemini 3 Flash + Kimi K2.6, judge Gemini 3 Flash, ~$0.05, 172s). Spike response archived at `/tmp/fusion-spike/resp1.json` at the time; the load-bearing observations are reproduced below.

## Verified pipeline (spike evidence)

1. **The primary model is the plugin's `model` field.** With `model: "openrouter/fusion"` in the request, the response's `model` resolved to the plugin `model` (the judge). The judge model and the final-answer writer are the same model. In fusion-second-opinion, that is the synthesizer (Opus 4.8) — it is also the model driving the Codex agent loop (repo inspection).
2. **The primary model receives the full conversation** (it recalled a codename planted two turns earlier) **and decides whether/when to call the injected `openrouter:fusion` server tool.** If it never calls the tool, the panel never runs. (`server_tool_use_details: {tool_calls_requested: 1, tool_calls_executed: 1}` in usage.)
3. **The panel sees only a distilled prompt, not the conversation and not the repo.** The tool-call arguments were `{"prompt": "<one-paragraph question the primary model wrote>"}`. Panelists had no access to the planted codename, the message history, or any repo tools. Panelists run agentic **web** loops only (`openrouter:web_search` + `openrouter:web_fetch`; the announcement also mentions bash), bounded by `max_tool_calls` (default 8, range 1–16). One panelist returned 14 web citations.
4. **The judge compares, not merges.** Tool result JSON (verbatim shape):
   ```json
   {"status":"ok",
    "analysis":{
      "consensus":[],
      "contradictions":[{"topic":"...","stances":[{"model":"...","stance":"..."}]}],
      "partial_coverage":[],
      "unique_insights":[{"model":"...","insight":"..."}],
      "blind_spots":[]},
    "models":["google/gemini-3-flash-preview","moonshotai/kimi-k2.6"]}
   ```
   Consensus items are treated as higher-confidence; contradictions carry per-model stances.
5. **The final writer sees the raw panel responses, not just the analysis.** The final assistant content contained the full text of both panel answers (headed `## Panel responses`) ahead of the analysis and final answer, and the panel's web citations were propagated into the final message's `annotations` (`url_citation` entries). So final-response grounding = raw panel outputs + structured analysis + the primary model's own context.
6. **Reasoning effort:** the request-level `reasoning: {effort}` observably applies to the primary model (757 reasoning tokens on the primary at `high`). Whether it propagates to panelists is **not documented and not observable** from the API (the parent generation reports `router: "server-tools"` with zero attributed tokens; child generations are not exposed). fusion-second-opinion's proxy injects `reasoning: {"effort": "max"}` on every request and sets no per-model overrides — so at `high`, panel Opus 4.8 / GPT-5.6-Sol / Gemini 3.1 Pro and the Opus 4.8 synthesizer are all *requested* at max effort, with per-panelist application at OpenRouter's discretion.
7. **Billing:** the whole fused call (panel + judge + primary, including panel web research) bills to the calling account as one generation (`usage.cost` covered all stages in the spike). Recursion is blocked via an `x-openrouter-fusion-depth` header — panel/judge cannot re-invoke fusion.

## What this means for the local `max` level (CLI replication)

Fidelity mapping, given the same models on both sides:

| Property | OpenRouter Fusion | Local `max` | Verdict |
|---|---|---|---|
| Panel independence | Parallel, blind to each other | Parallel background processes, blind to each other | equal |
| Panel input | One distilled prompt written by the primary model; **no repo access, no conversation** | Full review packet + direct read-only repo access + web tools | **local is a strict superset** for repo reviews |
| Panel execution | Only if the primary model chooses to call the tool | Unconditional, script-enforced | **local is deterministic** |
| Judge schema | consensus / contradictions(stances) / partial_coverage / unique_insights / blind_spots | Same schema, required in the synthesizer's output ahead of the final review | equal |
| Final writer inputs | Raw panel responses + structured analysis + own context | Synthesizer Reads both raw panel outputs + must produce the same structured analysis + read-only repo access to adjudicate contradictions | equal-or-better (repo adjudication) |
| Judge = final writer | Yes (plugin `model` is both) | Yes (Opus 4.8 single pass) | equal |
| Effort control | Request-level only; panel propagation unverifiable | Explicit per-CLI (`--effort max`, `model_reasoning_effort=max`) | **local is guaranteed** |
| Mid-investigation deliberation | Primary may call fusion at any point in its agent loop (up to `max_tool_calls`) | Single-pass pipeline: panel → judge/final | OpenRouter is more flexible; immaterial for the fixed review scenarios, where one deliberation round at full context is the intended shape |

The one capability OpenRouter has that the local pipeline does not replicate — the primary model interleaving extra fusion consultations mid-loop — is compensated by giving panelists the full packet and repo up front (OpenRouter panelists never get either), which removes the main reason a primary would need to re-consult.

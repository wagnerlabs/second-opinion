---
name: gemini-second-opinion
description: "Obtain a blocking independent review through Antigravity CLI (agy), defaulting to Gemini 4 Argon at high effort, with filesystem protection and tool-level coverage evidence. Use for non-trivial RCA, completed plans, implementations, documents, or analyses when requested or an independent perspective would materially help. Complete your own best attempt first. Google-based agents should prefer another provider unless Gemini is requested. Stop on unavailable models or unresolved execution/access failures; feedback is advisory."
---

# Gemini second opinion

## Invocation and completion policy

Use when explicitly requested, or when an independent review would materially improve a non-trivial artifact. Automatic discovery stays enabled. Prefer provider diversity: Google/Gemini-based invoking agents should normally choose Claude or GPT unless the user explicitly requests Gemini or a concrete task constraint favors it. Do not alter sibling skills or installed copies.

This is a time-expensive, **blocking review**, normally once per artifact. Once invoked, finish and assess it before advancing the reviewed work. A background process is acceptable only to avoid tool timeouts; wait for it and read its full output. Repair concrete packet, execution, access, or coverage failures and rerun within existing authority. When safe remediation is exhausted, stop the reviewed work and report the failure and evidence directory. Continue only after a valid review or an explicit user waiver.

Feedback is advisory. Criticism, rejection, questions, or your decision to decline recommendations do not require approval or another review. Never rerun merely to seek agreement. A distinct downstream artifact, an explicitly requested new review, or replacement of an operationally incomplete attempt can justify another pass.

Before preparing the packet, independently finish your best attempt at the exact artifact, including its ordinary investigation, implementation, tests, and validation. Gemini reviews it; it does not brainstorm, fill placeholders, investigate on your behalf, or finish it. If the user requests a review of an already-produced artifact, submit that existing artifact directly. For an independent RCA, finish your own RCA first but omit it from the packet.

## Interface

```text
/gemini-second-opinion [--model MODEL] [--effort EFFORT] [--timeout-seconds N]
```

Defaults: `--model gemini-4-argon --effort high --timeout-seconds 1800`.
Explicit Gemini slugs and `low`, `medium`, `high`, `xhigh`, or `max` are accepted only when the account catalog and structured `/model` command confirm that combination. If a slug includes an effort suffix, supply the matching `--effort`. Non-Gemini providers are rejected. Never substitute another model when the requested model is unavailable. Timeout overrides must be finite positive seconds. Forward arguments as separate argv values; do not interpolate untrusted invocation text into shell commands.

| Scenario | Review |
|---|---|
| `independent-rca` | Independent diagnosis before fixing; Gemini does not see your RCA. |
| `plan-review` | Critique your completed implementation-ready plan before implementation. |
| `post-implementation-review` | Compare completed, normally validated changes with the plan or requirements. |
| `document-review` | First independently analyze the user's inputs, then compare your completed document for fidelity, logic, clarity, completeness, and soundness. |
| `analysis-review` | First independently analyze the user's inputs, then compare your completed response or recommendation for reasoning, assumptions, fidelity, completeness, and actionability. |

## Prepare the packet and materials

Use a unique working directory under `${TMPDIR:-/tmp}` on macOS and Linux, outside the repository. Save the packet, manifest, and images there. The runner makes a separate `gemini-second-opinion-*` evidence directory under the same temporary root by default, snapshots the packet and images, and leaves them protected. When a different working location is explicitly required, prepare the files there and pass `--evidence-root PATH` to place the runner's evidence there too.

Write the packet in one operation with a quoted heredoc or Python reading stdin. Read it back in full before invoking the runner. Do not patch a stale packet; rewrite it if validation finds errors. Keep it minimal: point to files, avoid large code/diff excerpts and repository summaries, and distinguish agent claims from user premises and primary evidence.

When a verbatim message or inline artifact contains packet-like headings, wrap it in a code fence longer than any fence inside that content. Keep the content itself unchanged; the structural checker ignores fenced headings.

Include these sections, exactly once and in this order:

1. `### Scenario` — one scenario slug from the table.
2. `### Review target in repository` — concrete repository root and target (working tree against HEAD, commit, branch diff, plan path, etc.). Identify every critical/load-bearing material and any essential check, with practical access paths. Give Gemini full repository access and freedom to choose searches and terminal checks. Do not present your conclusions or test results as trusted background. Do not require reproduction of every incidental claim unless the review or verdict depends on it.
3. `### Full verbatim user transcript` — every user-typed message in this session, in chronological order. Deduplicate context-reloaded copies at their first occurrence. Preserve intentional repeated messages. Include only what the user typed: no expanded mentions, injected editor context, skill text, or attached file contents. List each attached image's absolute path and where it appeared. If any earlier message or image cannot be recovered, disclose the gap; if material, repair it before reviewing or stop.
4. `### Artifact under review` — omit entirely for `independent-rca`. For a plan, point to the plan file; inline only if no file exists. For implementation, give intended behavior, a short change summary and the plan path, without large diffs. For documents and analyses, separate the user's request (one sentence) from the agent's completed output; point to its file or inline a chat-only artifact, identifying assumptions and additions. In both two-phase scenarios, put the artifact after the transcript so Gemini can form and record its independent analysis first.

Also write a JSON manifest. List concrete critical **files**, including primary source files or evidence needed to assess the target, and all images. The list is minimum coverage, not a restriction on exploration. The runner adds the packet automatically. Do not include secrets in packets, manifests, or logs.

```json
{
  "critical_materials": [
    {"id": "plan", "path": "/absolute/path/plan.md", "kind": "text"},
    {"id": "implementation", "path": "src/module.py", "kind": "text"},
    {"id": "user-image", "path": "/absolute/path/image.png", "kind": "image"}
  ],
  "required_checks": ["Inspect the working-tree diff against HEAD"]
}
```

Paths may contain spaces. Relative material paths resolve from the repository root. Use descriptive unique ids. Check structure, transcript fidelity, duplication, image mapping, missing inputs, and primary-agent completion yourself; the runner's structure checks cannot establish those facts.

## Execute

Read [the runner contract](references/runner.md) for prerequisites, protection boundaries, diagnostics, copy handling, and evidence interpretation. Invoke the bundled script from the repository root, using its resolved absolute location:

```sh
python3 /path/to/gemini-second-opinion/scripts/run_review.py \
  --repo /absolute/repository/root \
  --packet /absolute/working-directory/packet.md \
  --manifest /absolute/working-directory/materials.json
```

Append explicit user model, effort, or timeout overrides. Do not add permission-bypass flags, edit saved settings, enable hooks, or loosen the external sandbox to get a review through. The runner tests the actual outer sandbox, then starts all CLI commands inside it. Argon catalog absence and rejected auto mode are separate diagnostics; neither authorizes a model substitution.

Inspect the original repository directly. When a material check inherently needs checkout writes, append `--copy-for-checks`: the runner creates a file copy under scratch and verifies file contents, types, modes and symlink targets against the source before and after copying. This preserves uncommitted, untracked, ignored and Git files. Do not use clones or git worktrees as a substitute. Symlinks and external worktree metadata still point to protected originals; checks requiring writes through those paths remain unavailable. Prepare a copy only when needed, and do not rely on it if fidelity verification fails.

Keep the review blocked until the runner exits. Its independent wall-clock limit defaults to 30 minutes; agy's own print timeout is explicitly zero. An interruption or timeout leaves incomplete evidence, not a completed review.

## Assess completion and apply feedback

Read **all** of `output.txt`, `result.json`, `gate.json`, `review.events.jsonl`, and `review.stderr`. A zero exit code and COMPLETE marker establish neither substantive coverage nor reviewer agreement. The runner's successful status is deliberately `awaiting-primary-agent-coverage-assessment`.

Verify successful termination, substantive text, exactly one normalized COMPLETE marker and no INCOMPLETE marker within the first five nonempty lines, the pinned selected model and effort, and evidence that the reviewer accessed the concrete target and every critical material. Inspect the successful tool calls referenced by `gate.json` and their outputs: a path mention, partial search, file listing, hash, or self-report cannot prove full inspection. Inspect additional discovered load-bearing materials too. Images require actual image-viewing evidence, not terminal metadata. Verify every essential check and both phases for document/analysis reviews. Your own inspection cannot replace a missing part of Gemini's review.

Inspect tool errors, unfinished steps, permission denials, and stderr warnings. A recovered error or a disclosed non-material limitation need not invalidate a review, but explain why it does not support or limit the verdict. A missing material input, failed essential check, generic response, or admitted inability to inspect a critical target is operationally incomplete regardless of the marker. A defect *within* the artifact can be valid criticism; an inaccessible separate referenced artifact is an access failure.

Record your assessment in `assessment.md` in the evidence directory: accepted or incomplete; materials and checks verified from tool evidence; resolution of each concern; and adopted or declined material feedback. Repair and rerun concrete operational failures without seeking agreement on artifact quality. If no in-scope repair is possible, stop and report what is needed. Do not narrow the review or finish the reviewed task without a valid pass or user waiver.

After accepting a valid review, apply material improvements using independent judgment. Compare an independent RCA with your own; for documents and analyses, correct fidelity errors and weigh divergent reasoning. Ask the user only about a genuine unresolved user decision. Give the user the evidence directory and briefly describe material feedback adopted or declined. Preserve packet, images, response, stream, stderr, selection, permissions, fidelity evidence and assessment. Remove obsolete preparation directories and disposable scratch copies when no longer needed; never delete outside directories this invocation created.

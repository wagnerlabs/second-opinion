# GPT second opinion

Sends a time-expensive, blocking review packet to GPT-6 Astra at maximum reasoning effort via the Codex CLI, pointed at the full repo in read-only sandbox mode. Fast mode is off by default.

## Usage

- `/gpt-second-opinion` — standard speed (default), with `max` reasoning effort.
- `/gpt-second-opinion --fast` — opt into OpenAI fast mode for this review.
- `/gpt-second-opinion --no-fast` — explicitly select standard speed; retained for compatibility.

Speed settings apply only to the current invocation and do not change persistent Codex settings.

## Testimonials

> "This review was worth every minute of its runtime"

— Fable 5 (max effort)

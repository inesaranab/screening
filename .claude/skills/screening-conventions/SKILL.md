---
name: screening-conventions
description: Engineering conventions for the Screening /screen service — the rules an AI assistant must follow when changing this codebase. Covers the hexagonal boundary, the trust model (JD trusted, transcript untrusted), guardrail behaviour, fail-closed error handling, and the test-first workflow. Use whenever editing anything under app/ or evals/.
---

# Screening — project conventions

This is a decision-support service: transcript + job description → a **validated**
assessment (fit 1–5, evidence-cited rationale, next step) that a **human recruiter
reviews**. It never makes an autonomous hire/reject decision. Every change must keep
that framing true. These are the rules I hold the assistant to; they encode judgment
the framework docs can't know.

## 1. Respect the hexagonal boundary

- `app/domain/` and `app/ports/` are **vendor-free**. No `fastapi`, `openai`,
  `presidio`, or `transformers` imports may appear there.
- Vendors live only in `app/adapters/` and the composition root `app/api/main.py`.
- The domain depends on **ports** (`Protocol`s), never on concrete adapters. If a
  change makes the domain import an adapter, the change is wrong — add/adjust a port.
- New capability = new port + adapter, injected at startup in `lifespan`.

## 2. Trust model (this is the security core)

- **The job description is our own input → trusted.** It is passed to the model as-is.
- **The transcript is candidate-supplied → untrusted.** It is *never* treated as
  instructions. It is scrubbed before the model sees it, and the system prompt tells
  the model to treat transcript text as content to assess, not commands to obey.
- Never "helpfully" let transcript text influence control flow, prompts, or scoring
  beyond being the thing under assessment.

## 3. Guardrail behaviour

- The guardrail runs **before** the model, through the `Guardrail` port (`scrub`).
- Order is deliberate: **injection check first** → if flagged, fail closed
  (withhold the text, no model call) → otherwise **redact PII**, then assess.
- **Fail closed, never fabricate.** A withheld/injection result must NOT invent a
  score. `fit_score` is `None` in that case, `out_of_scope`/`low_confidence` are set,
  and the recruiter sees a routed-for-review result — not a misleading number.
- **State the blind spots.** A guardrail's docstring must say what it does *and does
  not* catch (e.g. Presidio does not redact protected attributes like disability or
  visa status; the injection classifier is probabilistic). Honesty about coverage is
  a feature, not a caveat to hide.
- Guardrail inference is CPU-bound and synchronous — it must be offloaded with
  `run_in_threadpool` so it never blocks the async event loop.

## 4. Validation & errors

- Enforce rules in the **Pydantic model** (`ge`, `le`, enums, `min_length`), not in
  ad-hoc `if` checks. A bad model output should fail validation and trigger an
  instructor retry, not slip through.
- Keep `max_retries` small and finite — every retry is another paid, latent LLM call.
- Map failures to precise HTTP codes (504 timeout, 503 unreachable, 502 malformed),
  unwrapping `InstructorRetryException.__cause__`. The catch-all **fails closed** with
  a generic message — never leak internals or candidate data into a response.

## 5. Secrets & config

- No secrets in the repo. `service_api_key` has **no default** — the app refuses to
  boot without it, so auth can't be silently disabled by a missing env var.
- Config is environment-driven via `pydantic-settings` (`SCREENING_` prefix);
  `.env.example` is the committed template, `.env` is never committed.

## 6. Test-first — verified, not assumed

- Two tiers, and a change isn't done until the right tier is green:
  - `pytest -m "not live"` — deterministic, uses **fakes behind the ports** (canned +
    deliberately malformed LLM output). Fast, reproducible, CI-safe. Add one here for
    every behaviour change.
  - `pytest -m live` — exercises the real guardrail (Presidio + injection classifier)
    over genuine transcripts in `evals/fixtures.json`, including an adversarial case.
- When adding a guardrail or error path, write the failing test first, then make it
  pass. The eval suite is the contract, not an afterthought.

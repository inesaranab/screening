---
name: instructor
description: Extracting structured, validated outputs from LLMs with the instructor library (Pydantic response models, retries, validation, streaming). Use when calling an LLM and needing a typed Pydantic object back instead of free text — e.g. the screening assessment schema.
---

# instructor — structured LLM outputs

`instructor` patches an LLM client so a call returns a **validated Pydantic
model** instead of a string. Built on Pydantic: the response model *is* the
schema *and* the validation contract. If the model's output fails validation,
instructor re-asks the LLM with the error until it conforms (bounded by
`max_retries`).

> Provenance: distilled from instructor's own `CLAUDE.md`
> (`_source-CLAUDE.md` in this folder) plus its docs at
> https://python.useinstructor.com/. That source file is contributor tooling
> for building instructor itself — this file is about *using* it.

## Core pattern

```python
import instructor
from openai import OpenAI  # or AsyncOpenAI
from pydantic import BaseModel, Field

client = instructor.from_openai(OpenAI())

class Assessment(BaseModel):
    fit_score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)

result = client.chat.completions.create(
    model="...",
    response_model=Assessment,     # <- the whole point
    max_retries=2,                 # re-ask on validation failure
    messages=[...],
)
# `result` is an Assessment instance, already validated.
```

## Conventions that matter here

- **Let Pydantic do the enforcing.** Put the real rules on the model
  (`ge=1, le=5`, enums, `min_length`) rather than post-hoc `if` checks.
  A failed constraint becomes a retry, not a bad object reaching your code.
- **Keep `max_retries` small and finite** (1–2). Each retry is another paid,
  latent LLM call. Unbounded retries turn one request into many.
- **Async**: use `instructor.from_openai(AsyncOpenAI())` and `await` the call.
  Match whatever the surrounding port/adapter already uses.
- **Field descriptions are prompt.** `Field(description=...)` text is sent to
  the model — use it to steer, not just to document.
- **Failure mode to handle**: after retries are exhausted instructor raises
  (validation / `InstructorRetryException`). The adapter must catch it and map
  it to a domain error, never let a half-formed object through.
- **Provider-agnostic**: `from_openai`, `from_anthropic`, etc. share this API,
  so the adapter stays thin and the domain never sees the vendor.

## When NOT to reach for it

If you only need free-form text back, plain client calls are simpler — the
value of instructor is entirely in the *typed, validated* contract.

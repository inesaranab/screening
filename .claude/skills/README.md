# Agent skills

[Agent Skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
are small, versioned instruction packets (a `SKILL.md` with frontmatter) that an AI
coding assistant loads on demand to work correctly in this codebase. They are how I
*direct* the assistant instead of just prompting it ad-hoc: the conventions live in the
repo, get better over time, and apply automatically when relevant.

## What's here

| Skill | Authored by | Notes |
|-------|-------------|-------|
| **screening-conventions** | Me | The engineering rules specific to this project — hexagonal boundary, trust model, guardrail behaviour, fail-closed errors, test-first workflow. The part no upstream doc can know. |
| **instructor** | Me (adapted) | Usage-focused guidance for structured LLM outputs, distilled from instructor's docs + `CLAUDE.md`. |
| **fastapi** | Upstream, vendored | Verbatim from [`fastapi/fastapi`](https://github.com/fastapi/fastapi) (`fastapi/.agents/skills/fastapi`), MIT-licensed. Maintainer-written best practices — kept as-is for accuracy. |
| **pydantic** | Upstream, vendored | Verbatim from [`pydantic/pydantic`](https://github.com/pydantic/pydantic) (`.agents/skills/pydantic`), MIT-licensed. |

The vendored `fastapi` and `pydantic` skills are the libraries' own maintainer-authored
guidance, reused under their MIT licenses and attributed above. The original judgment in
this folder is **`screening-conventions`**.

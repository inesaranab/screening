"""PreToolUse hook: restate the project's writing conventions before an edit.

Fires on Write and Edit. Emits nothing for files the conventions do not cover,
so the reminder stays attached to source rather than appearing on every write.
"""

import json
import sys

_EXTENSIONS = (".py", ".yaml", ".yml", ".md")

_REMINDER = (
    "screening-conventions (.claude/skills/screening-conventions/SKILL.md):\n"
    "- Google-style docstrings: summary line, then Args/Returns/Raises for "
    "functions, Attributes for models.\n"
    "- State the PROPERTY, not the incident that taught it. No war stories, "
    "dates, measurements, or 'we' — those belong in infra/*/README.md or the "
    "commit message.\n"
    "- Self-contained: never explain one symbol by referring to another.\n"
    "- No prose constants: explanation assigned to a module-level string is "
    "dead code.\n"
    "- app/domain and app/ports stay vendor-free; adapters hold the vendors.\n"
    "- Test first: a failing test before the implementation."
)


def main() -> None:
    """Print the reminder when the target file is one the conventions govern."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError, ValueError:
        return
    path = payload.get("tool_input", {}).get("file_path", "")
    if not path.endswith(_EXTENSIONS):
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _REMINDER,
                }
            }
        )
    )


if __name__ == "__main__":
    main()

"""UserPromptSubmit hook: restate how answers to Ines should be written.

Emits on every prompt. Carries the response-shape rules only; the writing
conventions for source files are in conventions_reminder.py.
"""

import json

_REMINDER = (
    "Response style:\n"
    "- Short. Answer the question asked, then stop.\n"
    "- High level first, in plain words. Then the low-level version, so the "
    "vocabulary is picked up in context rather than assumed.\n"
    "- Define a term the first time it appears, in the same sentence.\n"
    "- No jargon without its plain-language equivalent alongside it."
)


def main() -> None:
    """Print the reminder."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _REMINDER,
                }
            }
        )
    )


if __name__ == "__main__":
    main()

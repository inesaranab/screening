"""Structured JSON logging.

Deliberately logs METADATA ONLY (event name, level, token counts, flags) — never
the transcript, the assessment text, or any candidate PII. The data is personal,
so what we don't log is a safety decision, not an oversight.
"""

import json
import logging


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a record as a JSON string.

        Args:
            record: The log record. Any ``context`` dict attached via
                ``extra={"context": ...}`` is merged into the payload.

        Returns:
            A JSON-encoded log line with level, event, and context, plus
            ``error_type`` when exception info is present.
        """
        payload = {
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "context", {}),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload)


def setup_logging() -> None:
    """Install the JSON formatter on the root logger.

    Quiets third-party libraries to WARNING and raises the app's own ``screen``
    logger to INFO, so our events show without library noise.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("screen").setLevel(logging.INFO)

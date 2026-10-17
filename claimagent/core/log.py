"""Terminal logging for the commands that run for a long time (serve, replay, run, simulate).

One short line per event, with the time and an area name:

    14:02:11 INFO  live      session live-20261017-140211 opened (voice cascaded, models: dev)
    14:02:19 INFO  live      tool record_user_fact {"kind": "first_edition", ...} -> ok
    14:02:41 INFO  pipeline  stage 3/9 detect started
    14:04:50 INFO  pipeline  stage 3/9 detect done in 129.0 s (4 model calls)

Areas: `live` (the session: what was said, warnings, tool calls), `pipeline` (stages), `server`
(uploads, settings). `--log-level debug` also shows every frame batch and model call; `warning`
shows problems only.
"""

from __future__ import annotations

import logging

LEVELS = ("debug", "info", "warning", "error")


class _ShortName(logging.Formatter):
    """Shows `claim.live` as `live` and pads the level, so the lines line up."""

    def format(self, record: logging.LogRecord) -> str:
        record.area = record.name.removeprefix("claim.")
        record.levelname = record.levelname.ljust(5)
        return super().format(record)


def setup(level: str = "info") -> None:
    """Send the `claim.*` loggers to the terminal at `level`. Safe to call twice."""
    logger = logging.getLogger("claim")
    logger.setLevel(level.upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_ShortName("%(asctime)s %(levelname)s %(area)-9s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.propagate = False


def short(text: str, limit: int = 110) -> str:
    """One line of at most `limit` characters, for log messages."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"

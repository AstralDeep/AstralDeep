"""Runs independent local-project reviewers and bounded repairs through authorized host tools."""

from __future__ import annotations

import contextvars


_ACTIVE_COMMAND = contextvars.ContextVar("goal_review_command", default=None)


def is_active_review_command(arguments):
    return _ACTIVE_COMMAND.get() == arguments and _ACTIVE_COMMAND.get() is not None

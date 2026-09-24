"""Package marker for backend/shared, re-exporting shared.progress's event types; also
copies legacy VITE_-prefixed env values onto their unprefixed names at import time so
an old .env file doesn't silently stop working.
"""

import os as _os

# Unprefixed wins when both are set (keeps old .env working)
for _new, _old in (
    ("USE_MOCK_AUTH", "VITE_USE_MOCK_AUTH"),
    ("KEYCLOAK_AUTHORITY", "VITE_KEYCLOAK_AUTHORITY"),
    ("KEYCLOAK_CLIENT_ID", "VITE_KEYCLOAK_CLIENT_ID"),
):
    if not _os.getenv(_new) and _os.getenv(_old):
        import logging as _logging
        _logging.getLogger("shared.env").warning(
            "%s is deprecated — rename it to %s in your .env", _old, _new)
        _os.environ[_new] = _os.environ[_old]
del _new, _old, _os

from .progress import ProgressEvent, ProgressPhase, ProgressStep, ProgressEmitter, create_log_event  # noqa: E402

__all__ = [
    "ProgressEvent",
    "ProgressPhase",
    "ProgressStep",
    "ProgressEmitter",
    "create_log_event",
]

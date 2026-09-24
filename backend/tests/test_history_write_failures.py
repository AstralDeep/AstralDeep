"""Tests confirming a missing or inaccessible owner-scoped chat fails a history write
instead of reporting success (orchestrator/history.py).
"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator.history import HistoryManager


def test_missing_owner_scoped_chat_never_appends_or_renames():
    transaction = object()
    conversations = Mock()
    conversations.get.return_value = None
    messages = Mock()
    history = HistoryManager.__new__(HistoryManager)
    history._history = SimpleNamespace(
        transaction=lambda: nullcontext(transaction),
        repository=SimpleNamespace(conversations=conversations, messages=messages),
    )

    with pytest.raises(RuntimeError, match="message was not saved"):
        history.add_message("missing-chat", "assistant", "unsaved response", user_id="owner")

    conversations.get.assert_called_once_with(
        transaction, owner_id="owner", conversation_id="missing-chat", for_update=True,
    )
    messages.append.assert_not_called()
    conversations.rename.assert_not_called()

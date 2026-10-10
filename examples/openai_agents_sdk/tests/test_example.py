"""
Deterministic offline tests for the isolated OpenAI Agents SDK example.

All external HTTP calls are mocked; no network traffic occurs.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Ensure the example module can be imported without pulling the repository root
# into sys.path (the test runner adds the repository root automatically).
from example import (
    submit_work,
    submit_work_idempot

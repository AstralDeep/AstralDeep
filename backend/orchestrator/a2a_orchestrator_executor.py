"""
Executor that runs A2A dispatches against the current catalogue of published capabilities.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Dict, List

from ..shared.feature_flags import FeatureFlags
from ..orchestrator

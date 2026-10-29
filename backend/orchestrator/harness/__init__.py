"""The versioned harness contract package: contract.py defines the seams,
typesafe_adapter.py and standard_llm_adapter.py bind the existing runners,
selection.py gates profile rollout, conformance.py verifies deployments, and
rollout.py controls qualified new-work rollout with stable owner bindings.
"""

from .contract import (
    HARNESS_CONTRACT_VERSION,
    AccountingSeam,
    HarnessAccounting,
    HarnessCancellationPending,
    HarnessAgentOption,
    HarnessContractError,
    HarnessDeployment,
    HarnessExecution,
    HarnessIdentity,
    HarnessOutcome,
    HarnessProviderFrame,
    HarnessResult,
    HarnessToolOption,
    HarnessToolSurface,
    HarnessTurnRequest,
    LifecycleSeam,
    ProviderSeam,
    ToolSeam,
    assert_credential_free,
    empty_surface,
    validate_deployment,
)
from .conformance import ConformanceReport, run_conformance
from .rollout import (
    HarnessBinding,
    HarnessProfileUnavailable,
    HarnessRollout,
    HarnessRolloutTransition,
    default_rollout,
)
from .selection import (
    ROLLOUT_FLAG,
    HarnessCatalog,
    HarnessSelectionUnavailable,
    default_catalog,
)
from .standard_llm_adapter import StandardLLMHarness, standard_deployment
from .typesafe_adapter import TypeSafeRoutingHarness, typesafe_deployment

__all__ = (
    "HARNESS_CONTRACT_VERSION",
    "ROLLOUT_FLAG",
    "AccountingSeam",
    "ConformanceReport",
    "HarnessAccounting",
    "HarnessBinding",
    "HarnessCancellationPending",
    "HarnessAgentOption",
    "HarnessCatalog",
    "HarnessContractError",
    "HarnessDeployment",
    "HarnessExecution",
    "HarnessIdentity",
    "HarnessOutcome",
    "HarnessProfileUnavailable",
    "HarnessProviderFrame",
    "HarnessResult",
    "HarnessRollout",
    "HarnessRolloutTransition",
    "HarnessSelectionUnavailable",
    "HarnessToolOption",
    "HarnessToolSurface",
    "HarnessTurnRequest",
    "LifecycleSeam",
    "ProviderSeam",
    "StandardLLMHarness",
    "ToolSeam",
    "TypeSafeRoutingHarness",
    "assert_credential_free",
    "default_catalog",
    "default_rollout",
    "empty_surface",
    "run_conformance",
    "standard_deployment",
    "typesafe_deployment",
    "validate_deployment",
)

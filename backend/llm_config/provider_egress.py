"""Selected provider attempts snapshot and recheck the existing operator private-host egress policy.
Only the bounded canonical hosts cross isolated_http's private envelope; this policy grants no model authority.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field

from shared.external_http import _allowed_private_hosts_from_env


class ProviderEgressUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PrivateEgressPolicy:
    hosts: tuple[str, ...]
    _source_digest: str = field(repr=False)

    def assert_current(self):
        if capture_private_policy() != self:
            raise ProviderEgressUnavailable("provider_egress_changed")


def capture_private_policy():
    raw = os.getenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "")
    try:
        if len(raw.encode("utf-8")) > 8192:
            raise ValueError
        hosts = tuple(sorted(set(_allowed_private_hosts_from_env())))
        if len(hosts) > 32 or any(not re.fullmatch(r"[a-z0-9.:-]{1,253}", host) for host in hosts):
            raise ValueError
        return PrivateEgressPolicy(hosts, hashlib.sha256(raw.encode("utf-8")).hexdigest())
    except (ValueError, TypeError, UnicodeError):
        raise ProviderEgressUnavailable("provider_egress_unavailable") from None

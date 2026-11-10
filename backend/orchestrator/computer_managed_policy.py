"""Managed-policy ceilings for computer control: the Mac host's descriptor block and
Keycloak role denials narrow the same switches with the stricter source winning, and an
unavailable role lookup falls back to the last verified value or fails closed; used by
computer_sessions.py, the dispatch gate and the diagnostics route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

MANAGED_POLICY_SWITCHES = ("computer_use", "browser_pane", "bypass")
MANAGED_POLICY_REFUSAL_REASON = "managed_policy"
DESCRIPTOR_SOURCE = "descriptor"
ROLE_SOURCE_VERIFIED = "roles"
ROLE_SOURCE_CACHED = "roles_cache"
ROLE_SOURCE_UNAVAILABLE = "roles_unavailable"

# Realm/client roles that forbid a switch for the whole organization. Ceilings only
# narrow: no role can widen owner authority.
ROLE_CEILING_ROLES = {
    "computer-use-denied": "computer_use",
    "browser-pane-denied": "browser_pane",
    "computer-bypass-denied": "bypass",
}


@dataclass(frozen=True)
class PolicyCeilings:
    computer_use: bool = True
    browser_pane: bool = True
    bypass: bool = True

    def narrowed(self, other: "PolicyCeilings") -> "PolicyCeilings":
        return PolicyCeilings(
            computer_use=self.computer_use and other.computer_use,
            browser_pane=self.browser_pane and other.browser_pane,
            bypass=self.bypass and other.bypass,
        )

    def as_dict(self) -> Dict[str, bool]:
        return {switch: getattr(self, switch) for switch in MANAGED_POLICY_SWITCHES}


def descriptor_ceilings(host: Any) -> Optional[PolicyCeilings]:
    descriptor = getattr(host, "descriptor", None)
    policy = descriptor.get("managed_policy") if isinstance(descriptor, dict) else None
    if not isinstance(policy, dict):
        return None
    return PolicyCeilings(**{switch: bool(policy[f"{switch}_allowed"])
                             for switch in MANAGED_POLICY_SWITCHES})


def policy_digest(descriptor: Any) -> Optional[str]:
    policy = descriptor.get("managed_policy") if isinstance(descriptor, dict) else None
    return policy.get("digest") if isinstance(policy, dict) else None


def claim_roles(claims: Any) -> Optional[list]:
    if not isinstance(claims, dict):
        return None

    def named(value: Any) -> list:
        return list(value) if isinstance(value, (list, tuple)) else []

    roles = named(claims.get("roles"))
    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        roles.extend(named(realm.get("roles")))
    resource = claims.get("resource_access")
    if isinstance(resource, dict):
        for client in resource.values():
            if isinstance(client, dict):
                roles.extend(named(client.get("roles")))
    return roles


def role_ceilings(roles: Iterable[Any]) -> PolicyCeilings:
    denied = {ROLE_CEILING_ROLES[str(role).strip()] for role in roles or ()
              if str(role).strip() in ROLE_CEILING_ROLES}
    return PolicyCeilings(**{switch: switch not in denied for switch in MANAGED_POLICY_SWITCHES})


def refusal_message(switch: str, host_name: str, source: str) -> str:
    if source == DESCRIPTOR_SOURCE:
        return (f"{host_name}'s organization profile forbids {switch.replace('_', ' ')}; "
                "ask the organization administrator to change the managed policy.")
    if source == ROLE_SOURCE_CACHED:
        return (f"Your account's organization roles forbid {switch.replace('_', ' ')} on {host_name}.")
    if source == ROLE_SOURCE_UNAVAILABLE:
        return (f"{host_name}'s organization ceilings could not be verified, so computer "
                "control stays unavailable until the next sign-in refreshes them.")
    return f"Your account's organization roles forbid {switch.replace('_', ' ')} on {host_name}."


def evaluate(sessions, host: Any, websocket: Any = None,
             ui_sessions: Any = None) -> Tuple[PolicyCeilings, Dict[str, str]]:
    """Effective ceilings for one host plus the source of each narrowing; role state that
    cannot be verified and has no verified record fails closed."""
    ceilings = PolicyCeilings()
    sources: Dict[str, str] = {}
    descriptor = descriptor_ceilings(host)
    if descriptor is not None:
        ceilings = ceilings.narrowed(descriptor)
        sources.update({switch: DESCRIPTOR_SOURCE for switch in MANAGED_POLICY_SWITCHES
                        if not getattr(descriptor, switch)})
    role_ceilings_value, role_source = owner_role_ceilings(sessions, host.owner_sub, websocket,
                                                           ui_sessions)
    if role_ceilings_value is None:
        ceilings = ceilings.narrowed(PolicyCeilings(False, False, False))
        sources.update({switch: ROLE_SOURCE_UNAVAILABLE for switch in MANAGED_POLICY_SWITCHES
                        if switch not in sources})
        return ceilings, sources
    ceilings = ceilings.narrowed(role_ceilings_value)
    sources.update({switch: role_source for switch in MANAGED_POLICY_SWITCHES
                    if not getattr(role_ceilings_value, switch) and switch not in sources})
    return ceilings, sources


def owner_role_ceilings(sessions, owner_sub: str, websocket: Any = None,
                        ui_sessions: Any = None) -> Tuple[Optional[PolicyCeilings], str]:
    verified: Optional[PolicyCeilings] = None
    if ui_sessions is None:
        ui_sessions = getattr(getattr(sessions, "_orch", None), "ui_sessions", None)
    for claims in list(ui_sessions.values()) if isinstance(ui_sessions, dict) else []:
        if not isinstance(claims, dict) or claims.get("sub") != owner_sub:
            continue
        found = role_ceilings(claim_roles(claims) or ())
        verified = found if verified is None else verified.narrowed(found)
    if verified is not None:
        sessions._role_ceilings[owner_sub] = verified
        return verified, ROLE_SOURCE_VERIFIED
    cached = sessions._role_ceilings.get(owner_sub)
    if cached is not None:
        return cached, ROLE_SOURCE_CACHED
    return None, ROLE_SOURCE_UNAVAILABLE

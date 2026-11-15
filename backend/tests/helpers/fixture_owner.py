"""Keeps synthetic JWT subjects and canonical owner ids distinct under one explicit test issuer.
Registered-human fixtures use the same issuer without accepting an unrelated host identity.
"""

from shared.node_identity import canonical_owner_id, issuer_digest

AUTHORITY = "https://verification.invalid/realms/registered-tests"


def fixture_owner(subject: str, *, authority: str = AUTHORITY) -> str:
    return canonical_owner_id(authority, subject)


def fixture_subject(owner: str, *, authority: str = AUTHORITY) -> str:
    prefix = f"oidc.{issuer_digest(authority)}."
    if not owner.startswith(prefix):
        raise ValueError("fixture owner is not bound to the selected synthetic issuer")
    subject = owner[len(prefix):]
    canonical_owner_id(authority, subject)
    return subject

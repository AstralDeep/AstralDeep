"""Validates the issuer/client identity pair shared by session records and the
revocation queue; host owns issuer/client trust. Used by repositories/history.py and
repositories/revocations.py.
"""

import unicodedata

from astralplane.repositories import RepositoryValidationError


def _issuing_pair(issuer: object, client_id: object) -> tuple[str | None, str | None]:
    if issuer is None and client_id is None:
        return None, None
    for value, maximum in ((issuer, 2048), (client_id, 256)):
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= maximum
            or value != value.strip()
            or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
        ):
            raise RepositoryValidationError("invalid paired issuing metadata")
    return issuer, client_id

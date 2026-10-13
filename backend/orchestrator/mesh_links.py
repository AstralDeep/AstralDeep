"""Builds enrollment links from the configured public application origin.
Mesh REST and shared chrome controls use this same bounded, fragment-only route.
"""

import os
from urllib.parse import urlsplit

from orchestrator.mesh_enrollment import MeshEnrollmentError


class MeshLinkUnavailable(MeshEnrollmentError):
    status = 503
    code = "mesh_link_unavailable"


def enrollment_link(payload):
    value = os.getenv("PUBLIC_BASE_URL") or os.getenv("BACKEND_PUBLIC_URL")
    try:
        if not isinstance(value, str) or value != value.strip() or len(value) > 2048:
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.scheme == "http"
            and os.getenv("ASTRAL_ENV") != "development"
            or not isinstance(payload, str)
            or not 1 <= len(payload) <= 2048
            or not payload.replace("-", "").replace("_", "").isalnum()
        ):
            raise ValueError
        parsed.port
        return f"{parsed.scheme}://{parsed.netloc}/enroll#{payload}"
    except (ValueError, TypeError):
        raise MeshLinkUnavailable() from None

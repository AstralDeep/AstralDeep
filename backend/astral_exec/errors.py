"""Exception and refusal types of the execution policy: PolicyError for a bad policy, ContextError for a bad caller input, Refusal for a selection that cannot run, ExecRefused to raise one.
Callers show the message of a refusal and branch on its code, and constants.py holds the texts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import HOST_MESSAGES, MESSAGES


class PolicyError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ContextError(Exception):
    pass


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    reason: str | None = None
    extras: tuple[tuple[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return False

    def __bool__(self) -> bool:
        return False

    @property
    def details(self) -> dict[str, Any]:
        return dict(self.extras)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            **({"reason": self.reason} if self.reason else {}),
            **self.details,
            "message": self.message,
        }


class ExecRefused(Exception):
    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.message)
        self.refusal = refusal
        self.code = refusal.code


def refuse(code: str, reason: str | None = None, **extra: Any) -> Refusal:
    template = {**MESSAGES, **HOST_MESSAGES}[reason or code]
    return Refusal(
        code=code,
        message=template.format(**extra) if extra else template,
        reason=reason,
        extras=tuple(extra.items()),
    )

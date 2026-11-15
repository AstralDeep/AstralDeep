"""The single error type raised by every reference check, carrying a stable dotted code.
The contract documents list which code each invalid case raises, and the vectors assert on the code.
"""

from __future__ import annotations


class MeshError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

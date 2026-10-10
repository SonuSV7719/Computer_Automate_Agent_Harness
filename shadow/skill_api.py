"""What generated skill files import."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ElRef:
    id: str


def el(element_id: str) -> ElRef:
    """Reference an element by its id in the skill's locators."""
    return ElRef(element_id)

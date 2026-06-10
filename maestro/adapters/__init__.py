"""Specialist adapter package.

All specialist CLIs are accessed through a unified SpecialistAdapter interface.
Adding a new specialist = adding one file in this package.
"""

from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput

__all__ = ["SpecialistAdapter", "TaskInput", "TaskOutput"]

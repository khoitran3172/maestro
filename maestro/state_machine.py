"""Task state machine with validated transitions.

Enforces valid state transitions for tasks. Invalid transitions are rejected
with clear error messages — prevents bugs from putting tasks in impossible states.

State diagram:
    [*] → pending → queued → running → grading → done
                                    ↘ kicked_back → queued
                                    ↘ failed
                         running → failed (error/timeout)
                         running → queued (retry)
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class TaskState(str, Enum):
    """Valid states for a Maestro task."""
    PENDING = "pending"         # Created, waiting for dependencies
    QUEUED = "queued"           # Dependencies met, waiting for scheduler
    RUNNING = "running"         # Specialist is executing
    GRADING = "grading"         # Output being graded
    DONE = "done"               # Passed rubric, complete
    FAILED = "failed"           # Failed after all retries exhausted
    KICKED_BACK = "kicked_back" # Failed rubric, will retry with feedback


class RunState(str, Enum):
    """Valid states for a Maestro run."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


# Valid transitions: {from_state: [allowed_to_states]}
_TASK_TRANSITIONS: dict[TaskState, list[TaskState]] = {
    TaskState.PENDING:     [TaskState.QUEUED],
    TaskState.QUEUED:      [TaskState.RUNNING],
    TaskState.RUNNING:     [TaskState.GRADING, TaskState.FAILED, TaskState.QUEUED],
    TaskState.GRADING:     [TaskState.DONE, TaskState.KICKED_BACK, TaskState.FAILED],
    TaskState.KICKED_BACK: [TaskState.QUEUED],
    TaskState.DONE:        [],  # Terminal state
    TaskState.FAILED:      [],  # Terminal state
}

_RUN_TRANSITIONS: dict[RunState, list[RunState]] = {
    RunState.RUNNING:   [RunState.COMPLETED, RunState.FAILED, RunState.PAUSED],
    RunState.PAUSED:    [RunState.RUNNING],
    RunState.COMPLETED: [],  # Terminal
    RunState.FAILED:    [RunState.RUNNING],  # Allow retry of failed runs
}


class InvalidTransitionError(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


def validate_task_transition(
    current: str | TaskState,
    target: str | TaskState,
) -> TaskState:
    """Validate and return the target TaskState if transition is allowed.

    Args:
        current: Current task state
        target: Desired target state

    Returns:
        The validated target TaskState

    Raises:
        InvalidTransitionError: If transition is not allowed
    """
    current_state = TaskState(current)
    target_state = TaskState(target)

    allowed = _TASK_TRANSITIONS.get(current_state, [])
    if target_state not in allowed:
        allowed_str = ", ".join(s.value for s in allowed) or "none (terminal state)"
        raise InvalidTransitionError(
            f"Cannot transition task from '{current_state.value}' to "
            f"'{target_state.value}'. Allowed transitions: [{allowed_str}]"
        )

    return target_state


def validate_run_transition(
    current: str | RunState,
    target: str | RunState,
) -> RunState:
    """Validate and return the target RunState if transition is allowed."""
    current_state = RunState(current)
    target_state = RunState(target)

    allowed = _RUN_TRANSITIONS.get(current_state, [])
    if target_state not in allowed:
        allowed_str = ", ".join(s.value for s in allowed) or "none (terminal state)"
        raise InvalidTransitionError(
            f"Cannot transition run from '{current_state.value}' to "
            f"'{target_state.value}'. Allowed transitions: [{allowed_str}]"
        )

    return target_state


def is_terminal_task_state(state: str | TaskState) -> bool:
    """Check if a task state is terminal (done or failed)."""
    return TaskState(state) in (TaskState.DONE, TaskState.FAILED)


def is_terminal_run_state(state: str | RunState) -> bool:
    """Check if a run state is terminal."""
    return RunState(state) in (RunState.COMPLETED,)


def can_retry(state: str | TaskState) -> bool:
    """Check if a task in this state can be retried."""
    return TaskState(state) in (TaskState.KICKED_BACK, TaskState.FAILED)


def get_resumable_states() -> list[str]:
    """States that should be re-queued on resume."""
    return [TaskState.RUNNING.value, TaskState.QUEUED.value,
            TaskState.PENDING.value, TaskState.KICKED_BACK.value,
            TaskState.GRADING.value]

"""Data model for the install plan the UI renders.

The installer builds an ``InstallPlan`` up front (phases of steps mirroring
the Ansible playbook/role structure) and hands it to the UI. From then on,
the installer only reports status transitions for individual steps by id -
it never touches Textual widgets directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import monotonic


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


STATUS_ICON = {
    StepStatus.PENDING: "○",  # ○
    StepStatus.RUNNING: "◐",  # ◐ (animated via spinner in the UI)
    StepStatus.SUCCESS: "✔",  # ✔
    StepStatus.FAILED: "✖",  # ✖
    StepStatus.SKIPPED: "⊘",  # ⊘
}


@dataclass
class Step:
    id: str
    name: str
    phase_id: str
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    started_at: float | None = None
    finished_at: float | None = None
    output: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else monotonic()
        return end - self.started_at

    # State transitions - shared by every reporter (the Textual dashboard and
    # the plain-text CI reporter) so they record a run identically.

    def mark_running(self) -> None:
        self.status = StepStatus.RUNNING
        self.started_at = monotonic()

    def mark_finished(self, success: bool, error: str | None = None) -> None:
        self.status = StepStatus.SUCCESS if success else StepStatus.FAILED
        self.finished_at = monotonic()
        self.error = error

    def mark_skipped(self, reason: str | None = None) -> None:
        self.status = StepStatus.SKIPPED
        self.error = reason


@dataclass
class Phase:
    id: str
    name: str
    steps: list[Step] = field(default_factory=list)


@dataclass
class InstallPlan:
    """Ordered phases that make up the full install."""

    phases: list[Phase] = field(default_factory=list)

    def all_steps(self) -> list[Step]:
        return [step for phase in self.phases for step in phase.steps]

    def steps_with_status(self, *statuses: StepStatus) -> list[Step]:
        return [step for step in self.all_steps() if step.status in statuses]

    def get_step(self, step_id: str) -> Step:
        for step in self.all_steps():
            if step.id == step_id:
                return step
        raise KeyError(f"Unknown step id: {step_id!r}")

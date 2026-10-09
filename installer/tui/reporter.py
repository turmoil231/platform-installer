"""Reporter interface, plus plain-text implementations for CI logs and log files.

The installer is written against ``Reporter`` - the same five methods
``InstallerUIController`` exposes - so it doesn't know or care whether it's
driving the Textual dashboard, writing a CI log, or both plus a log file.

- ``LogWriter`` formats each event as one timestamped line and writes it to
  a stream, flushed immediately (so a CI log streams live, and a log file
  is complete up to the moment of a crash). Status markers are ASCII words
  (``START``, ``OK``, ``FAIL``, ``SKIP``) so they're greppable and render in
  any log viewer. It only writes - it never changes step state.
- ``PlainReporter`` is a ``LogWriter`` on stdout that also records each event
  on the plan. It's what runs when there's no terminal (CI jobs, piped
  output, ``--ui plain``), where nothing else is tracking state.
- ``TeeReporter`` fans each event out to several reporters - e.g. a log file
  ``LogWriter`` alongside the dashboard.
"""

from __future__ import annotations

import re
import sys
import threading
from datetime import datetime
from typing import Protocol, TextIO

from .models import InstallPlan, StepStatus


class Reporter(Protocol):
    def start_step(self, step_id: str) -> None: ...

    def finish_step(self, step_id: str, success: bool, error: str | None = None) -> None: ...

    def skip_step(self, step_id: str, reason: str | None = None) -> None: ...

    def append_output(self, step_id: str, line: str) -> None: ...

    def log(self, message: str, level: str = "info") -> None: ...


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


LEVEL_TAG = {"info": "INFO", "warning": "WARN", "error": "ERROR"}

# Ansible (and plenty of other tools) colour their output. CI log viewers
# render the colours, but in a log file they're unreadable noise.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


class LogWriter:
    """Writes each event as a timestamped line. Safe to call from any thread.

    Reads step names and start times from the plan but never changes step
    state - pair it with something that does (the dashboard, or see
    ``PlainReporter``).
    """

    def __init__(
        self,
        plan: InstallPlan,
        stream: TextIO,
        *,
        timestamp_format: str = "%H:%M:%S",
        strip_colors: bool = False,
    ) -> None:
        self.plan = plan
        self._stream = stream
        self._timestamp_format = timestamp_format
        self._strip_colors = strip_colors
        # Continuation lines are indented to line up under the marker column.
        self._indent = " " * (len(datetime.now().strftime(timestamp_format)) + 3)
        # Parallel steps report from several threads at once; the lock keeps
        # each event (including a multi-line error block) contiguous.
        self._lock = threading.Lock()

    def start_step(self, step_id: str) -> None:
        step = self.plan.get_step(step_id)
        with self._lock:
            self._write("START", f"{step.id}: {step.name}")

    def finish_step(self, step_id: str, success: bool, error: str | None = None) -> None:
        step = self.plan.get_step(step_id)
        duration = fmt_duration(step.duration)
        with self._lock:
            if success:
                self._write("OK", f"{step.id} ({duration})")
                return
            self._write("FAIL", f"{step.id} ({duration})")
            for line in (error or "(no error detail supplied)").splitlines():
                self._raw(f"{self._indent}| {line}")

    def skip_step(self, step_id: str, reason: str | None = None) -> None:
        step = self.plan.get_step(step_id)
        with self._lock:
            self._write("SKIP", f"{step.id}" + (f": {reason}" if reason else ""))

    def append_output(self, step_id: str, line: str) -> None:
        step = self.plan.get_step(step_id)
        with self._lock:
            for part in line.splitlines() or [""]:
                self._write("", f"{step.id} | {part}")

    def log(self, message: str, level: str = "info") -> None:
        lines = message.splitlines() or [""]
        with self._lock:
            self._write(LEVEL_TAG.get(level, "INFO"), lines[0])
            for line in lines[1:]:
                self._raw(f"{self._indent}{line}")

    # ------------------------------------------------------------------ #

    def _write(self, marker: str, text: str) -> None:
        timestamp = datetime.now().strftime(self._timestamp_format)
        self._raw(f"[{timestamp}] {marker:<5} {text}")

    def _raw(self, line: str) -> None:
        if self._strip_colors:
            line = strip_ansi(line)
        self._stream.write(line + "\n")
        self._stream.flush()


class PlainReporter(LogWriter):
    """``LogWriter`` that also records each event on the plan.

    Each event is logged before the state change, so the log line still
    gets written if recording it fails.
    """

    def __init__(self, plan: InstallPlan, stream: TextIO | None = None) -> None:
        super().__init__(plan, stream if stream is not None else _safe_stdout())

    def start_step(self, step_id: str) -> None:
        super().start_step(step_id)
        self.plan.get_step(step_id).mark_running()

    def finish_step(self, step_id: str, success: bool, error: str | None = None) -> None:
        super().finish_step(step_id, success, error)
        self.plan.get_step(step_id).mark_finished(success, error)

    def skip_step(self, step_id: str, reason: str | None = None) -> None:
        super().skip_step(step_id, reason)
        self.plan.get_step(step_id).mark_skipped(reason)

    def append_output(self, step_id: str, line: str) -> None:
        super().append_output(step_id, line)
        self.plan.get_step(step_id).output.append(line)


class TeeReporter:
    """Sends every event to each reporter in turn, in the order given."""

    def __init__(self, *reporters: Reporter) -> None:
        self._reporters = reporters

    def start_step(self, step_id: str) -> None:
        for reporter in self._reporters:
            reporter.start_step(step_id)

    def finish_step(self, step_id: str, success: bool, error: str | None = None) -> None:
        for reporter in self._reporters:
            reporter.finish_step(step_id, success, error)

    def skip_step(self, step_id: str, reason: str | None = None) -> None:
        for reporter in self._reporters:
            reporter.skip_step(step_id, reason)

    def append_output(self, step_id: str, line: str) -> None:
        for reporter in self._reporters:
            reporter.append_output(step_id, line)

    def log(self, message: str, level: str = "info") -> None:
        for reporter in self._reporters:
            reporter.log(message, level)


def write_summary(plan: InstallPlan, elapsed: float, stream: TextIO | None = None) -> None:
    """Print the end-of-run summary - last thing in the log, where people look first."""
    stream = stream if stream is not None else _safe_stdout()
    steps = plan.all_steps()
    succeeded = plan.steps_with_status(StepStatus.SUCCESS)
    failed = plan.steps_with_status(StepStatus.FAILED)
    skipped = plan.steps_with_status(StepStatus.SKIPPED)
    not_run = plan.steps_with_status(StepStatus.PENDING, StepStatus.RUNNING)

    lines = [
        "",
        "=" * 20 + " install summary " + "=" * 20,
        f"{len(steps)} steps: {len(succeeded)} succeeded, {len(failed)} failed, "
        f"{len(skipped)} skipped, {len(not_run)} not run   (elapsed {fmt_duration(elapsed)})",
    ]
    for step in failed:
        lines.append(f"  {'FAILED':<14} {step.id}: {step.name}")
    for step in not_run:
        state = "DID NOT FINISH" if step.status == StepStatus.RUNNING else "NOT RUN"
        lines.append(f"  {state:<14} {step.id}: {step.name}")
    lines.append("RESULT: " + ("FAILED" if failed or not_run else "SUCCESS"))

    stream.write("\n".join(lines) + "\n")
    stream.flush()


def _safe_stdout() -> TextIO:
    # Some minimal CI images run with an ASCII locale. Installer output is
    # arbitrary text, and a UnicodeEncodeError while logging would take the
    # install down with it - so degrade unencodable characters instead.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    return sys.stdout

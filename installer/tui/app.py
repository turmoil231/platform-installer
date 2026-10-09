"""The Textual dashboard itself.

All widget mutation happens on the methods defined on ``InstallerApp`` below,
and all of those methods are only ever called on the app's own thread (either
directly, or marshalled there by ``InstallerUIController.call_from_thread``).
Nothing outside this module touches Textual widgets directly - that keeps the
threading rules (Textual objects are not thread-safe) in one place.
"""

from __future__ import annotations

import threading
from datetime import datetime
from time import monotonic

from rich.rule import Rule
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Footer,
    Header,
    ProgressBar,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode

from .models import InstallPlan, Step, StepStatus
from .reporter import fmt_duration

SPINNER_FRAMES = "|/-\\"

STATUS_COLOR = {
    StepStatus.PENDING: "grey58",
    StepStatus.RUNNING: "cyan",
    StepStatus.SUCCESS: "green",
    StepStatus.FAILED: "red",
    StepStatus.SKIPPED: "yellow",
}

STATUS_ICON = {
    StepStatus.PENDING: "○",
    StepStatus.SUCCESS: "✔",
    StepStatus.FAILED: "✖",
    StepStatus.SKIPPED: "⊘",
}

# Placeholder mark for "Framework" - swap for real branding later.
LOGO = "\n".join(
    [
        "█████",
        "█",
        "████",
        "█",
        "█",
    ]
)


class InstallerApp(App[None]):
    """Live dashboard for an airgapped platform bootstrap run."""

    # Inline rather than CSS_PATH so a PyInstaller --onefile build has no
    # data file to bundle (and none to lose).
    CSS = """
    Screen {
        background: $surface;
    }

    #summary {
        height: auto;
        padding: 1 2 0 2;
    }

    #summary-left {
        width: 1fr;
        height: auto;
    }

    #overall-progress {
        width: 100%;
    }

    #logo {
        width: auto;
        padding: 0 2;
        color: $accent;
        text-style: bold;
    }

    #summary-line {
        height: 1;
        margin-top: 1;
    }

    #running-now {
        height: 1;
        margin-top: 1;
    }

    #main {
        height: 1fr;
        padding: 1 2;
    }

    #step-tree {
        width: 52%;
        border: round $primary;
        padding: 0 1;
    }

    #detail-tabs {
        width: 1fr;
        border: round $primary;
    }

    RichLog {
        background: $surface;
    }

    Tab.-has-errors {
        color: $error;
        text-style: bold;
    }
    """
    TITLE = "Platform Bootstrap"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("e", "show_errors", "Errors"),
        Binding("o", "show_output", "Output"),
    ]

    def __init__(self, plan: InstallPlan) -> None:
        super().__init__()
        self.plan = plan
        self._step_nodes: dict[str, TreeNode] = {}
        self._error_count = 0
        self._spinner_frame = 0
        self._start_time = monotonic()

        # Signalled once on_mount has built the tree, so a controller running
        # on a worker thread knows it's safe to start dispatching updates.
        self.ready = threading.Event()
        self.ui_thread_id: int | None = None

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="summary"):
            with Vertical(id="summary-left"):
                yield ProgressBar(
                    total=len(self.plan.all_steps()), id="overall-progress", show_eta=False
                )
                yield Static(id="summary-line")
                yield Static(id="running-now")
            # Placeholder mark - swap LOGO for real branding later.
            yield Static(LOGO, id="logo", markup=False)
        with Horizontal(id="main"):
            yield Tree("Install Plan", id="step-tree")
            with TabbedContent(id="detail-tabs"):
                with TabPane("Output", id="tab-output"):
                    yield RichLog(id="output-log", wrap=True, highlight=True)
                with TabPane("Errors", id="tab-errors"):
                    yield RichLog(id="error-log", wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.ui_thread_id = threading.get_ident()

        tree = self.query_one("#step-tree", Tree)
        tree.show_root = False
        tree.root.expand()
        for phase in self.plan.phases:
            phase_node = tree.root.add(phase.name, expand=True)
            for step in phase.steps:
                leaf = phase_node.add_leaf(self._step_label(step), data=step.id)
                self._step_nodes[step.id] = leaf

        self._refresh_summary()
        self.set_interval(0.5, self._tick)
        self.ready.set()

    # ------------------------------------------------------------------ #
    # Public API - called only from the app's own thread. Use
    # InstallerUIController from anywhere else (e.g. ansible-runner
    # callbacks firing on a worker thread).
    # ------------------------------------------------------------------ #

    def mark_step_running(self, step_id: str) -> None:
        step = self.plan.get_step(step_id)
        step.mark_running()
        self._step_nodes[step_id].set_label(self._step_label(step))
        line = Text("▶ started  ", style="cyan")
        line.append(step.name)
        self._output_log().write(line)
        self._refresh_summary()

    def mark_step_finished(self, step_id: str, success: bool, error: str | None = None) -> None:
        step = self.plan.get_step(step_id)
        step.mark_finished(success, error)
        self._step_nodes[step_id].set_label(self._step_label(step))

        duration = fmt_duration(step.duration)
        if success:
            line = Text("✔ finished ", style="green")
            line.append(f"{step.name} ({duration})")
            self._output_log().write(line)
        else:
            line = Text("✖ failed   ", style="red")
            line.append(f"{step.name} ({duration})")
            self._output_log().write(line)
            self._record_error(step)
        self._refresh_summary()

    def mark_step_skipped(self, step_id: str, reason: str | None = None) -> None:
        step = self.plan.get_step(step_id)
        step.mark_skipped(reason)
        self._step_nodes[step_id].set_label(self._step_label(step))
        line = Text("⊘ skipped  ", style="yellow")
        line.append(step.name)
        if reason:
            line.append(f" — {reason}", style="dim")
        self._output_log().write(line)
        self._refresh_summary()

    def append_output(self, step_id: str, line: str) -> None:
        step = self.plan.get_step(step_id)
        step.output.append(line)
        rendered = Text(f"{step.name} | ", style="dim")
        rendered.append(line)
        self._output_log().write(rendered)

    def log_message(self, message: str, level: str = "info") -> None:
        color = {"info": "white", "warning": "yellow", "error": "red"}.get(level, "white")
        self._output_log().write(Text(message, style=color))

    # ------------------------------------------------------------------ #
    # Key bindings
    # ------------------------------------------------------------------ #

    def action_show_errors(self) -> None:
        self.query_one("#detail-tabs", TabbedContent).active = "tab-errors"

    def action_show_output(self) -> None:
        self.query_one("#detail-tabs", TabbedContent).active = "tab-output"

    # ------------------------------------------------------------------ #
    # Internal rendering helpers
    # ------------------------------------------------------------------ #

    def _output_log(self) -> RichLog:
        return self.query_one("#output-log", RichLog)

    def _step_label(self, step: Step) -> Text:
        label = Text()
        if step.status == StepStatus.RUNNING:
            frame = SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)]
            label.append(f"{frame} ", style=STATUS_COLOR[step.status])
        else:
            label.append(f"{STATUS_ICON[step.status]} ", style=STATUS_COLOR[step.status])
        name_style = STATUS_COLOR[step.status] if step.status == StepStatus.FAILED else ""
        label.append(step.name, style=name_style)
        if step.status in (StepStatus.RUNNING, StepStatus.SUCCESS, StepStatus.FAILED):
            if step.duration is not None:
                label.append(f"  ({fmt_duration(step.duration)})", style="dim")
        return label

    def _record_error(self, step: Step) -> None:
        self._error_count += 1
        error_log = self.query_one("#error-log", RichLog)
        timestamp = datetime.now().strftime("%H:%M:%S")
        error_log.write(Rule(Text(f"{step.name} · {timestamp}"), style="red"))

        header = Text("step failed: ", style="bold red")
        header.append(step.id)
        error_log.write(header)

        # Written as plain text (RichLog markup is off) so arbitrary installer
        # output - Ansible task names, JSON, IPs - can contain "[...]" without
        # being misread as style tags.
        error_log.write(step.error if step.error else Text("(no error detail supplied)", style="dim"))

        tabs = self.query_one("#detail-tabs", TabbedContent)
        tabs.get_tab("tab-errors").label = f"Errors ({self._error_count})"
        tabs.get_tab("tab-errors").add_class("-has-errors")

    def _tick(self) -> None:
        self._spinner_frame += 1
        for step in self.plan.all_steps():
            if step.status == StepStatus.RUNNING:
                node = self._step_nodes.get(step.id)
                if node is not None:
                    node.set_label(self._step_label(step))
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        steps = self.plan.all_steps()
        total = len(steps)
        finished_states = (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED)
        done = sum(1 for s in steps if s.status in finished_states)
        failed = sum(1 for s in steps if s.status == StepStatus.FAILED)
        skipped = sum(1 for s in steps if s.status == StepStatus.SKIPPED)
        running = [s for s in steps if s.status == StepStatus.RUNNING]

        bar = self.query_one("#overall-progress", ProgressBar)
        bar.update(total=total, progress=done)

        pct = (done / total * 100) if total else 0.0
        elapsed = fmt_duration(monotonic() - self._start_time)

        summary = Text()
        summary.append(f"{done}/{total} steps complete ({pct:.0f}%)", style="bold")
        summary.append(f"   elapsed {elapsed}", style="dim")
        if failed:
            summary.append(f"   {failed} failed", style="bold red")
        if skipped:
            summary.append(f"   {skipped} skipped", style="yellow")
        self.query_one("#summary-line", Static).update(summary)

        # Always keep this row present (never toggle `display`) so the panels
        # below don't jump up and down as steps start/finish - only its
        # content changes.
        running_widget = self.query_one("#running-now", Static)
        chips = Text()
        if running:
            frame = SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)]
            chips.append("running now:  ", style="bold cyan")
            for index, step in enumerate(running):
                if index:
                    chips.append("    ")
                chips.append(f"{frame} {step.name}", style="cyan")
                if step.duration is not None:
                    chips.append(f" ({fmt_duration(step.duration)})", style="dim")
        else:
            chips.append("running now:  (none)", style="dim")
        running_widget.update(chips)

        if total and done >= total:
            self.sub_title = (
                f"install finished with {failed} failure(s)" if failed else "install complete"
            )

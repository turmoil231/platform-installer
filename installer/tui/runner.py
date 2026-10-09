"""Run an installer under the right reporter and turn the result into an exit code.

This is the one entry point an installer's ``main()`` needs::

    sys.exit(run_installer(plan, install, ui=args.ui, junit_path=args.junit))

where ``install(reporter)`` is the orchestration function. It's written
against the ``Reporter`` interface, so the same function drives either:

- the Textual dashboard, when a person is watching at a real terminal; or
- ``PlainReporter``, a streaming line-per-event log, in CI or whenever
  output isn't a terminal.

With ``log_path`` set, every event is also appended to a log file in either
mode - the dashboard's on-screen logs are gone once it closes, so the file
is the durable record of the run.

Either way the run ends the same: an install summary printed to stdout (and
the log file), an optional JUnit XML report, and an exit code a pipeline can
gate on -
0 only if every step succeeded or was skipped, 1 if any step failed, never
ran, or the installer itself raised, 130 if interrupted with Ctrl-C.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Callable, TextIO

from .app import InstallerApp
from .controller import InstallerUIController
from .junit import write_junit_report
from .models import InstallPlan, StepStatus
from .reporter import LogWriter, PlainReporter, Reporter, TeeReporter, write_summary

UI_MODES = ("auto", "tui", "plain")

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130

Installer = Callable[[Reporter], None]


def choose_ui(requested: str = "auto") -> str:
    """Resolve ``auto`` to ``tui`` or ``plain`` based on where we're running."""
    if requested not in UI_MODES:
        raise ValueError(f"ui must be one of {UI_MODES}, got {requested!r}")
    if requested != "auto":
        return requested
    # Most CI systems set CI=true. The tty check covers the ones that don't
    # (e.g. Jenkins), plus piping or redirecting the installer's output.
    if os.environ.get("CI", "").lower() not in ("", "0", "false"):
        return "plain"
    if os.environ.get("TERM") == "dumb":
        return "plain"
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "plain"
    return "tui"


def run_installer(
    plan: InstallPlan,
    installer: Installer,
    *,
    ui: str = "auto",
    junit_path: str | Path | None = None,
    log_path: str | Path | None = None,
    exit_when_done: bool = False,
) -> int:
    """Run ``installer`` to completion and return the process exit code.

    ``log_path`` is appended to, not overwritten, so re-running after a
    failure (e.g. with ``--resume-from``) keeps the failed run's log too.
    Each run starts with a header line. If the file can't be opened the
    install doesn't start: a run nobody can investigate afterwards is worse
    than one that refuses to begin.

    ``exit_when_done`` only affects the dashboard: by default it stays open
    after the install so the person watching can read the result, and quits
    on ``q``. Set it for unattended runs that still have a terminal attached.
    """
    mode = choose_ui(ui)

    log_file = None
    if log_path is not None:
        try:
            log_file = _open_log(log_path, mode)
        except OSError as exc:
            print(f"error: cannot open log file {log_path}: {exc}", file=sys.stderr, flush=True)
            return EXIT_FAILURE

    try:
        # The log file gets dated timestamps (it outlives the run, and runs
        # can cross midnight) and no colour codes.
        file_log = (
            LogWriter(plan, log_file, timestamp_format="%Y-%m-%d %H:%M:%S", strip_colors=True)
            if log_file is not None
            else None
        )
        started = monotonic()

        if mode == "tui":
            installer_crashed = _run_tui(plan, installer, exit_when_done, file_log, log_path)
            interrupted = False
        else:
            installer_crashed, interrupted = _run_plain(plan, installer, file_log)

        elapsed = monotonic() - started
        write_summary(plan, elapsed)
        if log_file is not None:
            write_summary(plan, elapsed, log_file)
            print(f"Log written to {log_path}", flush=True)
        if junit_path is not None:
            write_junit_report(plan, junit_path)
            print(f"JUnit report written to {junit_path}", flush=True)
    finally:
        if log_file is not None:
            log_file.close()

    if interrupted:
        return EXIT_INTERRUPTED
    return exit_code(plan, installer_crashed)


def exit_code(plan: InstallPlan, installer_crashed: bool = False) -> int:
    if installer_crashed:
        return EXIT_FAILURE
    # A step left PENDING or RUNNING means the install didn't actually
    # finish, which must not pass as green even if nothing reported failure.
    if plan.steps_with_status(StepStatus.FAILED, StepStatus.PENDING, StepStatus.RUNNING):
        return EXIT_FAILURE
    return EXIT_SUCCESS


# ---------------------------------------------------------------------- #


def _open_log(path: str | Path, mode: str) -> TextIO:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_file = path.open("a", encoding="utf-8", errors="backslashreplace")
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n{'=' * 20} install run started {started} (ui={mode}) {'=' * 20}\n")
    log_file.flush()
    return log_file


def _with_file_log(reporter: Reporter, file_log: LogWriter | None) -> Reporter:
    # File first, so an event is on disk even if delivering it to the
    # dashboard fails (e.g. the app crashed or was closed).
    return TeeReporter(file_log, reporter) if file_log is not None else reporter


def _run_plain(
    plan: InstallPlan, installer: Installer, file_log: LogWriter | None
) -> tuple[bool, bool]:
    """Returns (installer_crashed, interrupted)."""
    reporter = _with_file_log(PlainReporter(plan), file_log)
    try:
        installer(reporter)
    except KeyboardInterrupt:
        reporter.log("interrupted - stopping install", level="error")
        return False, True
    except Exception:
        reporter.log("installer raised an exception:\n" + traceback.format_exc(), level="error")
        return True, False
    return False, False


def _run_tui(
    plan: InstallPlan,
    installer: Installer,
    exit_when_done: bool,
    file_log: LogWriter | None,
    log_path: str | Path | None,
) -> bool:
    """Returns installer_crashed."""
    app = InstallerApp(plan)
    controller = InstallerUIController(app)
    reporter = _with_file_log(controller, file_log)
    crash: list[str] = []
    app_closed = threading.Event()

    def target() -> None:
        try:
            installer(reporter)
        except Exception:
            # If the person quit the dashboard mid-install, the installer's
            # next UI call fails because the app is gone - that's not a crash.
            if app_closed.is_set():
                return
            crash.append(traceback.format_exc())
            _quietly(reporter.log, "installer raised an exception:\n" + crash[0], "error")

        if exit_when_done:
            _quietly(controller.quit)
        else:
            where = f" - full log in {log_path}" if log_path is not None else ""
            _quietly(controller.log, f"install finished{where} - press q to exit")

    threading.Thread(target=target, daemon=True).start()
    app.run()
    app_closed.set()

    if crash:
        # The dashboard's log is gone once it exits; keep the traceback.
        sys.stderr.write(crash[0])
    return bool(crash)


def _quietly(fn: Callable, *args) -> None:
    try:
        fn(*args)
    except Exception:
        pass  # the app has already shut down

"""Thread-safe façade the installer talks to.

The installer (ansible-runner orchestration layer) knows nothing about
Textual. It gets an ``InstallerUIController`` and calls plain methods like
``start_step`` / ``finish_step`` by step id - typically from whatever worker
thread ansible-runner's ``event_handler``/``status_handler`` callbacks land
on, since ansible_runner.run_async() executes on a background thread.

Textual widgets are not thread-safe, so every call here is marshalled onto
the app's own thread via ``App.call_from_thread`` before it touches a
widget. If a caller happens to already be on the app's thread (e.g. a
synchronous demo/test driving the UI directly), the update is applied
in-place instead - ``call_from_thread`` would otherwise raise.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .app import InstallerApp


class InstallerUIController:
    def __init__(self, app: "InstallerApp", ready_timeout: float = 10.0) -> None:
        self._app = app
        self._ready_timeout = ready_timeout

    def start_step(self, step_id: str) -> None:
        self._dispatch(self._app.mark_step_running, step_id)

    def finish_step(self, step_id: str, success: bool, error: str | None = None) -> None:
        self._dispatch(self._app.mark_step_finished, step_id, success, error)

    def skip_step(self, step_id: str, reason: str | None = None) -> None:
        self._dispatch(self._app.mark_step_skipped, step_id, reason)

    def append_output(self, step_id: str, line: str) -> None:
        self._dispatch(self._app.append_output, step_id, line)

    def log(self, message: str, level: str = "info") -> None:
        self._dispatch(self._app.log_message, message, level)

    def quit(self) -> None:
        self._dispatch(self._app.exit)

    # ------------------------------------------------------------------ #

    def _dispatch(self, method: Callable, *args) -> None:
        if not self._app.ready.wait(timeout=self._ready_timeout):
            raise RuntimeError(
                "InstallerApp did not finish mounting in time; is the app running?"
            )

        if threading.get_ident() == self._app.ui_thread_id:
            # Already on the app's thread - call straight through.
            method(*args)
        else:
            self._app.call_from_thread(method, *args)

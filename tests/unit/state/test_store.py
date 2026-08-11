"""
tests/unit/state/test_store.py

Unit tests for StateStore.

TODO: Implement tests for:
  - initialize() creates phase rows for all phase names
  - initialize() is idempotent (safe to call twice)
  - mark_running() → mark_complete() → get_phase() shows correct status
  - mark_running() → mark_failed() → get_phase() shows failed status
  - reset_phase() sets status back to pending
  - phase_context() marks complete on clean exit
  - phase_context() marks failed on exception
  - log_event() / events_for() round-trip
  - all_phases() returns phases in insertion order
"""
import pytest
import tempfile
from pathlib import Path
from installer.state.store import StateStore, PhaseStatus


def test_initialize_creates_phases():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "state.db")
        store.initialize(["preflight", "bootstrap", "hub_cluster"])
        phases = store.all_phases()
        assert len(phases) == 3
        assert all(p.status == PhaseStatus.PENDING for p in phases)


def test_phase_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "state.db")
        store.initialize(["preflight"])
        store.mark_running("preflight")
        assert store.get_phase("preflight").status == PhaseStatus.RUNNING
        store.mark_complete("preflight", message="All checks passed")
        record = store.get_phase("preflight")
        assert record.status == PhaseStatus.COMPLETE
        assert record.message == "All checks passed"
        assert record.attempt == 1


def test_reset_phase():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "state.db")
        store.initialize(["preflight"])
        store.mark_running("preflight")
        store.mark_complete("preflight")
        store.reset_phase("preflight")
        assert store.get_phase("preflight").status == PhaseStatus.PENDING


def test_phase_context_completes_on_clean_exit():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "state.db")
        store.initialize(["bootstrap"])
        with store.phase_context("bootstrap"):
            pass  # clean exit
        assert store.get_phase("bootstrap").status == PhaseStatus.COMPLETE


def test_phase_context_fails_on_exception():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(Path(tmpdir) / "state.db")
        store.initialize(["bootstrap"])
        with pytest.raises(RuntimeError):
            with store.phase_context("bootstrap"):
                raise RuntimeError("something went wrong")
        assert store.get_phase("bootstrap").status == PhaseStatus.FAILED

"""
tests/unit/test_cli_install.py

The deploy loop as run by the progress UI: build_plan() + make_install(),
driven end to end through tui.run_installer(ui="plain") with a real
AnsibleRunner whose container execution is faked out.
"""
from pathlib import Path

import pytest

from installer import tui
from installer.cli import (
    CONFIGURE_VAULT, CONFIGURE_VAULT_STEP_ID, LOCAL_SERVICES,
    build_plan, checkpoint_before, make_install,
)
from installer.phases.base import ALL_PHASES, PHASE_NAMES, VMwarePhase
from installer.runner.ansible import AnsibleRunner, PlaybookResult
from installer.state.store import PhaseStatus, StateStore

ANSIBLE_DIR = Path(__file__).resolve().parents[2] / "ansible"
Status = tui.StepStatus


class FakeLocalServices:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class Harness:
    def __init__(self, tmp_path: Path, *, dry_run: bool = False):
        self.tmp_path    = tmp_path
        self.dry_run     = dry_run
        self.fail_on:  set[str] = set()
        self.raise_on: set[str] = set()
        self.executed: list[str] = []
        self.store = StateStore(tmp_path / "state.db")
        self.store.initialize(PHASE_NAMES)
        self.local_services: FakeLocalServices | None = FakeLocalServices()
        self.hub_vault_addr = ""
        self.runner: AnsibleRunner | None = None

    def _fake_execute(self, playbook, extra_vars, tags, limit, step_id):
        self.executed.append(playbook)
        if playbook in self.raise_on:
            raise RuntimeError(f"container failed to start for {playbook}")
        if playbook in self.fail_on:
            return PlaybookResult(rc=2, status="failed", failed_hosts=["host1"])
        return PlaybookResult(rc=0, status="successful")

    def run(
        self,
        run_names:          set[str] | None = None,
        checkpoints:        set[str] | None = None,
        confirm=            lambda question: True,
        skip_health_checks: bool = False,
    ) -> tuple[int, tui.InstallPlan]:
        runner = AnsibleRunner(
            ansible_dir=ANSIBLE_DIR, private_data_dir=self.tmp_path / "pdd",
            container_image="platform-ansible-exec:test", vault_addr="http://vault:8200",
            vault_credentials_file=self.tmp_path / "approle.json",
            max_retries=0, retry_delay=0, dry_run=self.dry_run,
        )
        self.runner = runner
        runner._execute = self._fake_execute  # type: ignore[method-assign]
        phases = [Cls(runner=runner, store=self.store, config_vars={}) for Cls in ALL_PHASES]
        plan   = build_plan(phases)
        install = make_install(
            plan=plan, phases=phases,
            run_names=run_names if run_names is not None else set(PHASE_NAMES),
            store=self.store, runner=runner,
            checkpoints=checkpoints or set(), confirm=confirm,
            hub_vault_addr=self.hub_vault_addr, local_services=self.local_services,
            dry_run=self.dry_run, skip_health_checks=skip_health_checks,
        )
        rc = tui.run_installer(plan, install, ui="plain")
        return rc, plan

    def phase_status(self, name: str) -> str:
        return self.store.get_phase(name).status


def statuses(plan: tui.InstallPlan, phase_id: str) -> set[Status]:
    phase = next(p for p in plan.phases if p.id == phase_id)
    return {s.status for s in phase.steps}


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_plan_has_a_step_per_planned_playbook_plus_health_checks(harness):
    _, plan = harness.run()
    vmware = next(p for p in plan.phases if p.id == "vmware")
    assert [s.id for s in vmware.steps] == [
        "vmware.install_esxi", "vmware.deploy_vcenter", "vmware.configure_vcenter",
        "vmware.configure_storage", "vmware.health_check",
    ]
    assert [p.id for p in plan.phases] == [LOCAL_SERVICES, *PHASE_NAMES]


def test_success_runs_everything_and_exits_0(harness):
    rc, plan = harness.run()
    assert rc == 0
    assert plan.steps_with_status(Status.PENDING, Status.RUNNING, Status.FAILED, Status.SKIPPED) == []
    assert [s.name for s in plan.all_steps() if not s.id.endswith(".health_check")] == harness.executed
    assert all(r.status == PhaseStatus.COMPLETE for r in harness.store.all_phases())


def test_failure_exits_1_and_leaves_later_steps_pending(harness):
    harness.fail_on.add("management/configure_idm.yml")
    rc, plan = harness.run()

    assert rc == 1
    failed = plan.get_step("management_services.configure_idm")
    assert failed.status == Status.FAILED
    assert "Failed hosts: host1" in failed.error
    assert plan.get_step("management_services.deploy_kea").status == Status.PENDING
    assert statuses(plan, "hub_cluster") == {Status.PENDING}
    assert harness.phase_status("bootstrap") == PhaseStatus.COMPLETE
    assert harness.phase_status("management_services") == PhaseStatus.FAILED
    assert harness.phase_status("hub_cluster") == PhaseStatus.PENDING


def test_resume_skips_completed_phases_and_exits_0(harness):
    harness.fail_on.add("management/configure_idm.yml")
    assert harness.run()[0] == 1

    harness.fail_on.clear()
    harness.executed.clear()
    rc, plan = harness.run()

    assert rc == 0
    assert harness.executed[:2] == [CONFIGURE_VAULT, "management/deploy_idm.yml"]
    assert statuses(plan, "bootstrap") == {Status.SKIPPED}
    assert plan.get_step("bootstrap.start_vault").error == "already complete"
    assert statuses(plan, "management_services") == {Status.SUCCESS}


def test_exception_finishes_the_step_and_exits_1(harness):
    harness.raise_on.add("bootstrap/start_vault.yml")
    rc, plan = harness.run()

    assert rc == 1
    step = plan.get_step("bootstrap.start_vault")
    assert step.status == Status.FAILED
    assert "container failed to start" in step.error
    assert harness.phase_status("bootstrap") == PhaseStatus.FAILED


def test_dry_run_skips_everything_exits_0_and_leaves_state_alone(tmp_path):
    harness = Harness(tmp_path, dry_run=True)
    rc, plan = harness.run()

    assert rc == 0
    assert harness.executed == []
    assert {s.status for s in plan.all_steps()} == {Status.SKIPPED}
    assert all(r.status == PhaseStatus.PENDING for r in harness.store.all_phases())


def test_unselected_phases_are_skipped(harness):
    rc, plan = harness.run(run_names={"preflight"})
    assert rc == 0
    assert harness.executed == [CONFIGURE_VAULT, "preflight.yml"]
    assert plan.get_step("vmware.install_esxi").error == "not selected"


def test_configure_vault_runs_first_and_its_failure_stops_the_run(harness):
    harness.fail_on.add(CONFIGURE_VAULT)
    rc, plan = harness.run()

    assert rc == 1
    assert harness.executed == [CONFIGURE_VAULT]
    assert plan.get_step(CONFIGURE_VAULT_STEP_ID).status == Status.FAILED
    assert statuses(plan, "preflight") == {Status.PENDING}
    assert harness.phase_status("preflight") == PhaseStatus.PENDING


def test_configure_vault_runs_again_on_resume(harness):
    harness.run(run_names={"preflight"})
    harness.executed.clear()
    harness.run(run_names={"preflight"})
    assert harness.executed == [CONFIGURE_VAULT]


def test_retired_local_services_skip_configure_vault(harness):
    harness.local_services = None
    rc, plan = harness.run()

    assert rc == 0
    assert CONFIGURE_VAULT not in harness.executed
    assert plan.get_step(CONFIGURE_VAULT_STEP_ID).error == "local services retired"


def test_hub_services_completion_switches_vault_and_retires_local_services(harness, monkeypatch):
    harness.hub_vault_addr = "https://vault.hub"
    services = harness.local_services
    switched_at = []
    original = AnsibleRunner.switch_vault_addr

    def record_switch(runner, addr):
        switched_at.append(len(harness.executed))
        original(runner, addr)

    monkeypatch.setattr(AnsibleRunner, "switch_vault_addr", record_switch)
    rc, _ = harness.run()

    assert rc == 0
    # After the last hub_services playbook, before the first spoke_clusters one.
    assert harness.executed[switched_at[0] - 1].startswith("hub_services/")
    assert harness.runner.vault_addr == "https://vault.hub"
    assert harness.runner.vault_credentials_file is None
    assert services.stopped


def test_failed_hub_services_keeps_local_services(harness):
    harness.hub_vault_addr = "https://vault.hub"
    harness.fail_on.add("hub_services/install_operators.yml")
    rc, _ = harness.run()

    assert rc == 1
    assert harness.runner.vault_addr == "http://vault:8200"
    assert not harness.local_services.stopped


def test_failed_health_check_exits_1(harness, monkeypatch):
    monkeypatch.setattr(VMwarePhase, "health_check", lambda self: False)
    rc, plan = harness.run()

    assert rc == 1
    assert plan.get_step("vmware.health_check").status == Status.FAILED
    assert harness.phase_status("vmware") == PhaseStatus.FAILED
    assert statuses(plan, "bootstrap") == {Status.PENDING}


def test_skip_health_checks(harness, monkeypatch):
    monkeypatch.setattr(VMwarePhase, "health_check", lambda self: False)
    rc, plan = harness.run(skip_health_checks=True)

    assert rc == 0
    assert plan.get_step("vmware.health_check").status == Status.SKIPPED


def test_declined_checkpoint_stops_the_run(harness):
    questions = []

    def decline(question):
        questions.append(question)
        return False

    rc, plan = harness.run(checkpoints={"post_vmware"}, confirm=decline)

    assert rc == 1
    assert questions == ["Checkpoint post_vmware: continue with 'bootstrap'?"]
    assert harness.phase_status("vmware") == PhaseStatus.COMPLETE
    assert statuses(plan, "bootstrap") == {Status.PENDING}


@pytest.mark.parametrize("phase, checkpoints, expected", [
    ("bootstrap",      {"post_vmware"},        "post_vmware"),
    ("vmware",         {"post_vmware"},        None),
    ("spoke_clusters", {"pre_spoke_clusters"}, "pre_spoke_clusters"),
    ("preflight",      {"post_vmware"},        None),
    ("hub_services",   set(),                  None),
])
def test_checkpoint_before(phase, checkpoints, expected):
    assert checkpoint_before(phase, checkpoints) == expected

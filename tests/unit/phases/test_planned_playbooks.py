"""
tests/unit/phases/test_planned_playbooks.py

planned_playbooks() is what the progress UI's plan is built from, before
anything runs. These check it against what run() actually executes, for
representative configs, so the two can't drift.
"""
from pathlib import Path

import pytest

from installer.phases.base import (
    ALL_PHASES,
    BootstrapTeardownPhase,
    HubServicesPhase,
    SpokeClustersPhase,
    VDIPhase,
)
from installer.runner.ansible import PlaybookResult

ANSIBLE_DIR = Path(__file__).resolve().parents[3] / "ansible"


class RecordingRunner:
    """Stands in for AnsibleRunner; records every run_playbook() call."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def run_playbook(self, playbook, extra_vars=None, tags=None, limit=None, *, step_id):
        self.calls.append((playbook, step_id))
        if playbook == self.fail_on:
            return PlaybookResult(rc=2, status="failed")
        return PlaybookResult(rc=0, status="successful")


class NullStore:
    def log_event(self, *args, **kwargs):
        pass


CONFIGS = {
    "empty": {},
    "all_services": {
        "platform_hub_services": {svc: {"enabled": True} for svc in HubServicesPhase._SERVICE_ORDER},
        "platform_spoke_clusters": {"clusters": [{"name": "spoke1"}, {"name": "spoke2"}]},
        "platform_vdi": {"enabled": True, "platform": "vmware"},
    },
    "some_services_ocpvirt": {
        "platform_hub_services": {
            "gitlab": {"enabled": True},
            "acm":    {"enabled": True},
            "vault":  {"enabled": False},
        },
        "platform_vdi": {"enabled": True, "platform": "ocpvirt"},
        "platform_bootstrap": {"teardown": {"enabled": False}},
    },
}


def _phase(cls, config_vars, runner=None):
    return cls(runner=runner or RecordingRunner(), store=NullStore(), config_vars=config_vars)


@pytest.mark.parametrize("config_name", CONFIGS)
@pytest.mark.parametrize("phase_cls", ALL_PHASES, ids=lambda c: c.name)
def test_run_executes_exactly_the_planned_playbooks(phase_cls, config_name):
    runner = RecordingRunner()
    phase  = _phase(phase_cls, CONFIGS[config_name], runner)
    planned = [pb.playbook for pb in phase.planned_playbooks()]

    result = phase.run()

    assert result.success
    assert [playbook for playbook, _ in runner.calls] == planned
    assert [step_id for _, step_id in runner.calls] == [phase.step_id(pb) for pb in planned]


@pytest.mark.parametrize("config_name", CONFIGS)
@pytest.mark.parametrize("phase_cls", ALL_PHASES, ids=lambda c: c.name)
def test_planned_step_ids_are_unique_and_playbooks_exist(phase_cls, config_name):
    phase = _phase(phase_cls, CONFIGS[config_name])
    planned = [pb.playbook for pb in phase.planned_playbooks()]
    step_ids = [phase.step_id(pb) for pb in planned]
    if phase.has_health_check:
        step_ids.append(phase.health_check_step_id)

    assert len(step_ids) == len(set(step_ids))
    for playbook in planned:
        assert (ANSIBLE_DIR / "playbooks" / playbook).exists(), playbook


def test_run_stops_at_first_failure_with_its_message():
    runner = RecordingRunner(fail_on="hub_services/gitlab.yml")
    phase  = _phase(HubServicesPhase, CONFIGS["some_services_ocpvirt"], runner)

    result = phase.run()

    assert not result.success
    assert result.rc == 2
    assert result.message == "hub_services/gitlab deployment failed"
    assert [p for p, _ in runner.calls] == ["hub_services/install_operators.yml", "hub_services/gitlab.yml"]


def test_hub_services_order_and_migrations():
    phase = _phase(HubServicesPhase, CONFIGS["all_services"])
    planned = [pb.playbook for pb in phase.planned_playbooks()]
    assert planned[0] == "hub_services/install_operators.yml"
    assert planned[1:-2] == [f"hub_services/{s}.yml" for s in HubServicesPhase._SERVICE_ORDER]
    assert planned[-2:] == ["hub_services/migrate_vault.yml", "hub_services/migrate_artifactory.yml"]


def test_hub_services_skips_disabled_services():
    phase = _phase(HubServicesPhase, CONFIGS["some_services_ocpvirt"])
    assert [pb.playbook for pb in phase.planned_playbooks()] == [
        "hub_services/install_operators.yml",
        "hub_services/gitlab.yml",
        "hub_services/acm.yml",
    ]


def test_spoke_clusters_skips_when_no_clusters():
    phase = _phase(SpokeClustersPhase, {})
    assert phase.planned_playbooks() == []
    assert phase.run().message == "No spoke clusters defined — skipping"


def test_spoke_clusters_passes_spoke_names():
    phase = _phase(SpokeClustersPhase, CONFIGS["all_services"])
    wait = phase.planned_playbooks()[-1]
    assert wait.playbook == "spokes/wait_for_clusters.yml"
    assert wait.extra_vars == {"spoke_names": ["spoke1", "spoke2"]}
    assert phase.run().message == "All 2 spoke cluster(s) installed"


@pytest.mark.parametrize("vdi, expected", [
    ({"enabled": False},                         []),
    ({"enabled": True, "platform": "vmware"},    ["vdi/deploy_vmware_vdi.yml"]),
    ({"enabled": True, "platform": "ocpvirt"},   ["vdi/deploy_ocpvirt_vdi.yml"]),
])
def test_vdi_picks_platform(vdi, expected):
    phase = _phase(VDIPhase, {"platform_vdi": vdi})
    assert [pb.playbook for pb in phase.planned_playbooks()] == expected


def test_bootstrap_teardown_can_be_disabled():
    assert _phase(BootstrapTeardownPhase, CONFIGS["some_services_ocpvirt"]).planned_playbooks() == []
    assert len(_phase(BootstrapTeardownPhase, {}).planned_playbooks()) == 1


def test_has_health_check_matches_overrides():
    with_hc = {cls.name for cls in ALL_PHASES if _phase(cls, {}).has_health_check}
    assert with_hc == {"vmware", "bootstrap", "hub_cluster"}

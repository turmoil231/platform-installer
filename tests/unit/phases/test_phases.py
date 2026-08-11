"""
tests/unit/phases/test_phases.py

Unit tests for Phase classes.

TODO: Implement tests for:
  - ALL_PHASES contains all expected phase names in correct order
  - PHASE_NAMES matches [p.name for p in ALL_PHASES]
  - Each phase's depends_on references only valid phase names
  - PreflightPhase.run() returns success when playbook succeeds (mock runner)
  - PreflightPhase.run() returns failure when playbook fails (mock runner)
  - HubServicesPhase deploys services in correct order
  - HubServicesPhase skips disabled services
  - SpokeClustersPhase skips when clusters list is empty
  - VDIPhase skips when vdi.enabled is false
"""
import pytest
from installer.phases.base import ALL_PHASES, PHASE_NAMES


def test_all_phases_have_unique_names():
    names = [p.name for p in ALL_PHASES]
    assert len(names) == len(set(names)), "Duplicate phase names found"


def test_phase_names_match_all_phases():
    assert PHASE_NAMES == [p.name for p in ALL_PHASES]


def test_phase_dependencies_are_valid():
    valid_names = set(PHASE_NAMES)
    for phase_cls in ALL_PHASES:
        for dep in phase_cls.depends_on:
            assert dep in valid_names, (
                f"Phase '{phase_cls.name}' depends on unknown phase '{dep}'"
            )


def test_expected_phases_present():
    expected = [
        "preflight", "vmware", "bootstrap", "management_services",
        "mirror_registry", "hub_cluster", "hub_services", "spoke_clusters",
        "vdi_services", "validation", "bootstrap_teardown",
    ]
    assert PHASE_NAMES == expected
